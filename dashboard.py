#!/usr/bin/env python3
"""
LLM Usage Dashboard — Data Pipeline & Web Server

Parses OpenClaw sessions.json, computes daily/weekly/monthly stats,
serves a self-contained dashboard page.

Usage:
  python3 dashboard.py [--port 8080]
"""

import json
import os
import sys
import argparse
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HKT = timezone(timedelta(hours=8))

# ── Config ──────────────────────────────────────────────────────────────
# Override via env if your sessions live elsewhere, e.g.:
#   LLMDASH_SESSIONS_PATH=/path/to/sessions.json python3 dashboard.py
SESSIONS_PATH = os.environ.get("LLMDASH_SESSIONS_PATH") or os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
GF_SESSIONS_PATH = os.environ.get("LLMDASH_GF_SESSIONS_PATH") or os.path.expanduser("~/.openclaw/agents/gf/sessions/sessions.json")

import subprocess
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

def get_deepseek_balance():
    """Legacy helper（/api/summary 用）— key 一律走 providers.json 的 key_source。"""
    for p in load_providers():
        if p.get("quota_api") == "deepseek-balance":
            key = resolve_key(p.get("key_source"))
            if not key:
                return None
            try:
                data = fetch_deepseek_balance_q(key)
                if "balance_usd" in data:
                    return {"balance": data["balance_usd"]}
            except Exception:
                return None
    return None

MODEL_TIERS = {
    "paid": ["glm-5.2", "glm-5-turbo"],
    "free": ["glm-4.7-flash", "glm-4.7"],
    "deepseek": ["deepseek"],   # 泛匹配所有 deepseek 型號（chat/v4-pro/v4-flash…）
    "claude": ["claude", "fable", "opus", "sonnet", "haiku"],
    "grok": ["grok"],
}

MODEL_PRICES = {
    "glm-5.2": {"input": 0.0, "output": 0.0},       # Coding plan, free
    "glm-5-turbo": {"input": 0.0, "output": 0.0},    # Pro plan, free
    "glm-4.7-flash": {"input": 0.0, "output": 0.0},  # Free tier
    "glm-4.7": {"input": 0.0, "output": 0.0},        # Free tier
    "deepseek-chat": {"input": 3.0 / 1e6, "output": 12.0 / 1e6},
    "deepseek-v4-pro": {"input": 12.0 / 1e6, "output": 48.0 / 1e6},
}

# ── Data Loading ────────────────────────────────────────────────────────

def classify_model(model_str):
    """Classify a model string into a tier name."""
    for tier, models in MODEL_TIERS.items():
        for m in models:
            if m in model_str:
                return tier
    return "unknown"


def load_sessions(path):
    """Load sessions from JSON file, return list of enriched records."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)

    records = []
    for key, s in raw.items():
        model = (s.get("systemPromptReport") or {}).get("model", "") or ""
        cost = s.get("estimatedCostUsd", 0) or 0
        inp = s.get("inputTokens", 0) or 0
        out = s.get("outputTokens", 0) or 0
        started_ms = s.get("startedAt") or s.get("sessionStartedAt") or 0
        ended_ms = s.get("endedAt") or 0
        runtime = s.get("runtimeMs", 0) or 0

        if started_ms == 0:
            continue

        started_dt = datetime.fromtimestamp(started_ms / 1000, tz=HKT)
        ended_dt = datetime.fromtimestamp(ended_ms / 1000, tz=HKT) if ended_ms else None

        # Determine agent from session key
        agent_id = key.split(":")[1] if len(key.split(":")) > 1 else "main"
        # Simplify agent labels
        if ":" in agent_id:
            agent_id = agent_id.split(":")[0]

        records.append({
            "key": key,
            "agent": agent_id,
            "model": model,
            "tier": classify_model(model),
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "cost": cost,
            "cost_estimated": cost,
            "started_at": started_dt,
            "ended_at": ended_dt,
            "runtime_ms": runtime,
            "chat_type": s.get("chatType", ""),
        })

    return records


# ── Claude Code Data Source ─────────────────────────────────────────────
# 解析 ~/.claude/projects/**/*.jsonl transcript。每條 assistant 訊息帶 usage
# （input/output/cache tokens、model、timestamp）。按 (session, 小時) 聚合，
# 讓長 session 的用量正確落在各時段（Z.AI 5h 窗口估算需要）。

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

_cc_cache = {}   # path -> (mtime, size, per-hour aggregates)


def _parse_cc_file(path):
    """Parse one Claude Code transcript → list of per-(session,hour) aggregates."""
    # 以 message.id 去重：同一則回應的多個 content block 會重複同一份 usage
    msgs = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                ts = d.get("timestamp", "")
                sid = d.get("sessionId", os.path.basename(path))
                mid = m.get("id") or d.get("uuid", "")
                inp = u.get("input_tokens", 0) or 0
                out = u.get("output_tokens", 0) or 0
                cw = u.get("cache_creation_input_tokens", 0) or 0
                cr = u.get("cache_read_input_tokens", 0) or 0
                if inp + out + cw + cr == 0:
                    continue   # synthetic / 空訊息
                msgs[(sid, mid)] = {
                    "ts": ts, "model": m.get("model", ""),
                    "in": inp, "out": out, "cache_w": cw, "cache_r": cr,
                }
    except OSError:
        return []

    buckets = {}   # (sid, hour_key) -> agg
    for (sid, _), v in msgs.items():
        try:
            dt = datetime.fromisoformat(v["ts"].replace("Z", "+00:00")).astimezone(HKT)
        except ValueError:
            continue
        hour_key = dt.strftime("%Y-%m-%d %H")
        b = buckets.setdefault((sid, hour_key), {
            "first": dt, "last": dt, "model": v["model"],
            "in": 0, "out": 0, "cache_w": 0, "cache_r": 0, "msgs": 0,
        })
        b["first"] = min(b["first"], dt)
        b["last"] = max(b["last"], dt)
        if v["model"]:
            b["model"] = v["model"]
        b["in"] += v["in"]
        b["out"] += v["out"]
        b["cache_w"] += v["cache_w"]
        b["cache_r"] += v["cache_r"]
        b["msgs"] += 1
    return [(sid, hk, b) for (sid, hk), b in buckets.items()]


def load_claude_code():
    """Load Claude Code usage as records (schema 對齊 load_sessions)。"""
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return []
    records = []
    for root, _dirs, files in os.walk(CLAUDE_PROJECTS_DIR):
        rel = os.path.relpath(root, CLAUDE_PROJECTS_DIR)
        project = rel.split(os.sep)[0] if rel != "." else ""
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(root, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            cached = _cc_cache.get(path)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                buckets = cached[2]
            else:
                buckets = _parse_cc_file(path)
                _cc_cache[path] = (st.st_mtime, st.st_size, buckets)

            for sid, hour_key, b in buckets:
                # cache write 算新 input；cache read 另計（不灌水 total）
                inp = b["in"] + b["cache_w"]
                records.append({
                    "key": f"cc:{sid}:{hour_key}",
                    "agent": "claude-code",
                    "model": b["model"],
                    "tier": classify_model(b["model"]),
                    "input_tokens": inp,
                    "output_tokens": b["out"],
                    "total_tokens": inp + b["out"],
                    "cache_read_tokens": b["cache_r"],
                    "cost": 0.0,
                    "cost_estimated": 0.0,
                    "started_at": b["first"],
                    "ended_at": b["last"],
                    "runtime_ms": int((b["last"] - b["first"]).total_seconds() * 1000),
                    "chat_type": project,
                })
    return records


def load_all_records():
    """所有數據源：OpenClaw main + gf + Claude Code。"""
    return (load_sessions(SESSIONS_PATH)
            + load_sessions(GF_SESSIONS_PATH)
            + load_claude_code())


# ── Z.AI Usage Estimate ─────────────────────────────────────────────────
# Z.AI 無查詢 API。改為本地估算：所有走 Z.AI 的 GLM token（OpenClaw + Claude
# Code）落在 5h / 7 天窗口的總量。比手動抄網頁即時，但屬估算值。

ZAI_TIERS = {"paid", "free"}   # GLM 系列都走 Z.AI coding plan


def estimate_zai_usage(all_records):
    now = datetime.now(tz=HKT)
    cut_5h = now - timedelta(hours=5)
    cut_wk = now - timedelta(days=7)
    r5 = [r for r in all_records if r["tier"] in ZAI_TIERS and r["started_at"] >= cut_5h]
    rw = [r for r in all_records if r["tier"] in ZAI_TIERS and r["started_at"] >= cut_wk]
    return {
        "last_5h": {"tokens": sum(r["total_tokens"] for r in r5), "sessions": len(r5)},
        "this_week": {"tokens": sum(r["total_tokens"] for r in rw), "sessions": len(rw)},
        "estimated": True,
    }


# ── Provider Config & Quota Engine ──────────────────────────────────────
# providers.json 讓每個使用者配置自己的 LLM 組合（zai / deepseek / openai / …）。
# quota fetcher 帶 TTL 快取（120s），避免每次 render 都打 API。

PROVIDERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")
QUOTA_CACHE_TTL = 120   # seconds

_quota_cache = {}       # provider_id -> (fetched_at, data)
_key_cache = {}         # cache resolved keys


def load_providers():
    try:
        with open(PROVIDERS_PATH) as f:
            return json.load(f).get("providers", [])
    except (OSError, ValueError):
        return []


def resolve_key(key_source):
    """從 keychain / claude settings / env 解析 API key（不明文存 providers.json）。"""
    if not key_source:
        return None
    ck = json.dumps(key_source, sort_keys=True)
    if ck in _key_cache:
        return _key_cache[ck]
    key = None
    t = key_source.get("type")
    try:
        if t == "claude-settings":
            with open(os.path.expanduser("~/.claude/settings.json")) as f:
                key = json.load(f).get("env", {}).get(key_source.get("field", ""))
        elif t == "keychain":
            key = subprocess.run(
                ["security", "find-generic-password",
                 "-s", key_source.get("service", ""),
                 "-a", key_source.get("account", ""), "-w"],
                text=True, capture_output=True).stdout.strip() or None
        elif t == "env":
            key = os.environ.get(key_source.get("var", ""))
        elif t == "literal":
            key = key_source.get("value")
    except (OSError, ValueError):
        key = None
    _key_cache[ck] = key
    return key


ZAI_UNIT_LABELS = {1: "d", 3: "h", 5: "mo", 6: "w"}


def fetch_zai_quota(api_key):
    """Z.AI coding plan 配額（與 OpenClaw app 同一個 endpoint）。"""
    req = Request(
        "https://api.z.ai/api/monitor/usage/quota/limit",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("success") or data.get("code") != 200:
        return {"error": data.get("msg", "API error")}
    windows = []
    for lim in data.get("data", {}).get("limits", []):
        unit = ZAI_UNIT_LABELS.get(lim.get("unit"), "?")
        num = lim.get("number", 1)
        if lim.get("type") == "TOKENS_LIMIT":
            label = f"{num}{unit}"
        else:
            label = "monthly-tools"
        windows.append({
            "label": label,
            "type": lim.get("type"),
            "used_pct": lim.get("percentage", 0),
            "reset_at_ms": lim.get("nextResetTime"),
            "current": lim.get("currentValue"),
            "limit": lim.get("usage"),
        })
    return {"plan": data.get("data", {}).get("level", ""), "windows": windows}


def fetch_deepseek_balance_q(api_key):
    req = Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("is_available", False):
        return {"error": "unavailable"}
    return {"balance_usd": float(data["balance_infos"][0]["total_balance"])}


QUOTA_FETCHERS = {
    "zai": fetch_zai_quota,
    "deepseek-balance": fetch_deepseek_balance_q,
}


_UNIT_MS = {"h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def window_ms(label):
    """'5h' / '1w' → 窗口長度（毫秒）；不認得的 label 回傳 None。"""
    if not label or label[-1] not in _UNIT_MS:
        return None
    try:
        num = int(label[:-1]) if label[:-1] else 1
    except ValueError:
        return None
    return num * _UNIT_MS[label[-1]]


def budget_for_window(win):
    """燒錢速度：窗口已過時間 vs 已用配額 → on-track / over-pace + 預測耗盡時間。"""
    span = window_ms(win.get("label", ""))
    reset = win.get("reset_at_ms")
    used = win.get("used_pct", 0)
    if not span or not reset:
        return None
    now_ms = time.time() * 1000
    elapsed = max(0, span - (reset - now_ms))
    pace_pct = elapsed / span * 100
    out = {
        "pace_pct": round(pace_pct, 1),
        "hours_to_reset": round((reset - now_ms) / 3600e3, 1),
    }
    will_exhaust = False
    if used > 0 and elapsed > 0:
        burn = used / elapsed          # pct per ms
        exhaust_ms = (100 - used) / burn
        out["projected_exhaust_h"] = round(exhaust_ms / 3600e3, 1)
        will_exhaust = exhaust_ms < (reset - now_ms)
        out["will_exhaust_before_reset"] = will_exhaust
    # over-pace: 用量明顯超前進度；at-risk: 進度內但照目前速度會提前耗盡
    if used > pace_pct + 5:
        out["status"] = "over-pace"
    elif will_exhaust:
        out["status"] = "at-risk"
    else:
        out["status"] = "on-track"
    return out


def get_all_quotas(force=False):
    """所有 providers 的即時配額（TTL 快取）。"""
    now = time.time()
    results = []
    for p in load_providers():
        pid = p["id"]
        fetcher = QUOTA_FETCHERS.get(p.get("quota_api"))
        entry = {
            "id": pid, "label": p.get("label", pid), "kind": p.get("kind", "other"),
            "color": p.get("color", "#8b949e"), "plan_cost_usd": p.get("plan_cost_usd"),
            # 透傳計費相關配置（低額警戒、本地估算餘額、無 cost 記錄的計價表）
            "low_balance_warn_usd": p.get("low_balance_warn_usd"),
            "credit_topup_usd": p.get("credit_topup_usd"),
            "credit_since": p.get("credit_since"),
            "price_per_mtok": p.get("price_per_mtok"),
        }
        if fetcher:
            cached = _quota_cache.get(pid)
            if not force and cached and now - cached[0] < QUOTA_CACHE_TTL:
                data = cached[1]
            else:
                try:
                    data = fetcher(resolve_key(p.get("key_source")))
                except Exception as e:
                    data = {"error": str(e)[:120]}
                _quota_cache[pid] = (now, data)
            entry["quota"] = data
            for w in (data.get("windows") or []):
                b = budget_for_window(w)
                if b:
                    w["budget"] = b
        results.append(entry)
    return results


# ── Aggregation ─────────────────────────────────────────────────────────

def aggregate_by_period(records, period="day"):
    """Aggregate records by time period."""
    result = defaultdict(lambda: {
        "sessions": 0, "input_tokens": 0, "output_tokens": 0,
        "total_tokens": 0, "cost": 0.0,
        "by_tier": defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0}),
        "by_agent": defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0}),
        "agents_set": set(),
    })

    for r in records:
        dt = r["started_at"]
        if period == "day":
            key = dt.strftime("%Y-%m-%d")
        elif period == "week":
            iso = dt.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        elif period == "month":
            key = dt.strftime("%Y-%m")
        elif period == "hour":
            key = dt.strftime("%Y-%m-%d %H:00")
        else:
            key = dt.strftime("%Y-%m-%d")

        grp = result[key]
        grp["sessions"] += 1
        grp["input_tokens"] += r["input_tokens"]
        grp["output_tokens"] += r["output_tokens"]
        grp["total_tokens"] += r["total_tokens"]
        grp["cost"] += r["cost"]
        grp["agents_set"].add(r["agent"])

        grp["by_tier"][r["tier"]]["sessions"] += 1
        grp["by_tier"][r["tier"]]["tokens"] += r["total_tokens"]
        grp["by_tier"][r["tier"]]["cost"] += r["cost"]

        grp["by_agent"][r["agent"]]["sessions"] += 1
        grp["by_agent"][r["agent"]]["tokens"] += r["total_tokens"]
        grp["by_agent"][r["agent"]]["cost"] += r["cost"]

    # Sort by key
    sorted_result = sorted(result.items(), key=lambda x: x[0])
    return sorted_result


def get_totals(records):
    """Get overall totals and breakdowns."""
    totals = {
        "sessions": len(records),
        "input_tokens": sum(r["input_tokens"] for r in records),
        "output_tokens": sum(r["output_tokens"] for r in records),
        "total_tokens": sum(r["total_tokens"] for r in records),
        "cost": sum(r["cost"] for r in records),
    }

    # By tier
    by_tier = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    for r in records:
        by_tier[r["tier"]]["sessions"] += 1
        by_tier[r["tier"]]["tokens"] += r["total_tokens"]
        by_tier[r["tier"]]["cost"] += r["cost"]

    # By agent
    by_agent = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    for r in records:
        by_agent[r["agent"]]["sessions"] += 1
        by_agent[r["agent"]]["tokens"] += r["total_tokens"]
        by_agent[r["agent"]]["cost"] += r["cost"]

    # Last 7 days
    now = datetime.now(tz=HKT)
    week_ago = now - timedelta(days=7)
    recent = [r for r in records if r["started_at"] >= week_ago]
    week_total = {
        "sessions": len(recent),
        "tokens": sum(r["total_tokens"] for r in recent),
        "cost": sum(r["cost"] for r in recent),
    }

    return {
        "totals": totals,
        "by_tier": dict(by_tier),
        "by_agent": dict(by_agent),
        "week": week_total,
        "total_records": len(records),
    }


def get_timeline(records):
    """Get daily stats for the last 30 days."""
    now = datetime.now(tz=HKT)
    days = []
    for i in range(30, -1, -1):
        day = now - timedelta(days=i)
        date_key = day.strftime("%Y-%m-%d")
        day_records = [r for r in records if r["started_at"].strftime("%Y-%m-%d") == date_key]
        days.append({
            "date": date_key,
            "sessions": len(day_records),
            "tokens": sum(r["total_tokens"] for r in day_records),
            "cost": sum(r["cost"] for r in day_records),
            "agent_breakdown": {},
        })
    return days


# ── HTML Template ───────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root {
  /* Dark theme (default) */
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #1c2128;
  --border: #30363d;
  --border-light: #21262d;
  --text-primary: #c9d1d9;
  --text-secondary: #8b949e;
  --text-muted: #484f58;
  --accent-green: #3fb950;
  --accent-blue: #58a6ff;
  --accent-yellow: #d29922;
  --accent-purple: #bc8cff;
  --accent-gray: #8b949e;
  --btn-primary: #238636;
  --btn-primary-hover: #2ea043;
}

[data-theme="light"] {
  --bg-primary: #ffffff;
  --bg-secondary: #f6f8fa;
  --bg-tertiary: #eaeef2;
  --border: #d0d7de;
  --border-light: #e1e4e8;
  --text-primary: #1f2328;
  --text-secondary: #656d76;
  --text-muted: #8b949e;
  --accent-green: #1a7f37;
  --accent-blue: #0969da;
  --accent-yellow: #9a6700;
  --accent-purple: #8250df;
  --accent-gray: #6e7781;
  --btn-primary: #1a7f37;
  --btn-primary-hover: #116329;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: var(--bg-primary); color: var(--text-primary); padding: 20px; transition: background 0.3s, color 0.3s; }
h1, h2, h3 { margin-bottom: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 16px; transition: background 0.3s, border-color 0.3s; }
.card .label { font-size: 12px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; }
.card .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
.card .sub { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.tier-badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
.tier-paid { background: var(--accent-green); color: #fff; }
[data-theme="light"] .tier-paid { color: #fff; }
.tier-free { background: var(--accent-blue); color: #fff; }
.tier-deepseek { background: var(--accent-yellow); color: #fff; }
.tier-claude { background: var(--accent-purple); color: #fff; }
.tier-grok { background: #ff6e5e; color: #fff; }
.tier-unknown { background: var(--accent-gray); color: #fff; }
.chart-container { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; transition: background 0.3s, border-color 0.3s; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border-light); font-size: 14px; }
th { color: var(--text-secondary); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
tr:hover td { background: var(--bg-tertiary); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.section { margin-bottom: 32px; }
.status-bar { position: fixed; bottom: 0; left: 0; right: 0; background: var(--bg-secondary); border-top: 1px solid var(--border); padding: 10px 20px; font-size: 12px; color: var(--text-secondary); text-align: center; transition: background 0.3s, border-color 0.3s; }
.update-btn { background: var(--btn-primary); color: #fff; border: none; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; transition: background 0.2s; }
.update-btn:hover { background: var(--btn-primary-hover); }
.theme-toggle { position: fixed; top: 16px; right: 16px; z-index: 100; background: var(--bg-secondary); border: 1px solid var(--border); color: var(--text-primary); padding: 8px 14px; border-radius: 20px; cursor: pointer; font-size: 14px; transition: all 0.3s; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
.theme-toggle:hover { background: var(--bg-tertiary); border-color: var(--accent-blue); }
</style>
<script>
(function(){
  var saved = localStorage.getItem('dashboard-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  document.addEventListener('DOMContentLoaded', function(){
    var btn = document.getElementById('themeToggle');
    if(!btn) return;
    btn.textContent = saved === 'dark' ? '☀️ Light' : '🌙 Dark';
    btn.onclick = function(){
      var current = document.documentElement.getAttribute('data-theme');
      var next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('dashboard-theme', next);
      btn.textContent = next === 'dark' ? '☀️ Light' : '🌙 Dark';
      if (window.refreshChartTheme) window.refreshChartTheme();
    };
  });
})();
</script>
</head>
<body>
<button class="theme-toggle" id="themeToggle">☀️ Light</button>
<div style="max-width: 1200px; margin: 0 auto;">
<h1>🧠 LLM Usage Dashboard <button class="update-btn" onclick="location.reload()">⟳ Refresh</button></h1>
<p style="color:var(--text-secondary); margin-bottom:20px;">Last updated: $LAST_UPDATED</p>

<div class="grid">
<div class="card">
<div class="label">Total Sessions</div>
<div class="value">$TOTAL_SESSIONS</div>
<div class="sub">$WEEK_SESSIONS this week</div>
</div>
<div class="card">
<div class="label">Total Tokens</div>
<div class="value">$TOTAL_TOKENS_FORMATTED</div>
<div class="sub">$WEEK_TOKENS_FORMATTED this week</div>
</div>
<div class="card">
<div class="label">Input Tokens</div>
<div class="value">$INPUT_TOKENS_FORMATTED</div>
</div>
<div class="card">
<div class="label">Output Tokens</div>
<div class="value">$OUTPUT_TOKENS_FORMATTED</div>
</div>
<div class="card">
<div class="label">Estimated Cost</div>
<div class="value">$$TOTAL_COST</div>
<div class="sub">$$WEEK_COST this week</div>
</div>
</div>

<div class="grid">
$TIER_CARDS
</div>

<div class="section">
<h2>📈 Daily Usage (30 days)</h2>
<div class="chart-container">
<canvas id="dailyChart" height="80"></canvas>
</div>
<div class="chart-container">
<canvas id="costChart" height="80"></canvas>
</div>
</div>

<div class="section">
<h2>📊 Agent Usage</h2>
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
<div class="chart-container">
<h3>Tokens by Agent</h3>
<canvas id="agentPieChart" height="120"></canvas>
</div>
<div class="chart-container">
<h3>Cost by Agent</h3>
<canvas id="agentCostPieChart" height="120"></canvas>
</div>
</div>
<table>
<tr><th>Agent</th><th class="num">Sessions</th><th class="num">Tokens</th><th class="num">Cost</th></tr>
$AGENT_ROWS
</table>
</div>

<div class="section">
<h2>💲 Model Tier Breakdown</h2>
<table>
<tr><th>Tier</th><th class="num">Sessions</th><th class="num">Tokens</th><th class="num">Cost</th></tr>
$TIER_ROWS
</table>
</div>

<div class="section">
<h2>📋 Recent Sessions (last 20)</h2>
<table>
<tr><th>Time</th><th>Agent</th><th>Model</th><th class="num">Tokens</th><th class="num">Cost</th></tr>
$RECENT_ROWS
</table>
</div>

</div>
<div class="status-bar">LLM Usage Dashboard · Data from OpenClaw sessions.json</div>

<script>
const dailyData = $DAILY_DATA;
const COST_BY_AGENT = $COST_BY_AGENT;

function themeColors() {
  const s = getComputedStyle(document.documentElement);
  return { text: s.getPropertyValue('--text-secondary').trim(), grid: s.getPropertyValue('--border-light').trim() };
}

window.refreshChartTheme = function() {
  const tc = themeColors();
  Chart.defaults.color = tc.text;
  Chart.defaults.borderColor = tc.grid;
  Object.values(Chart.instances).forEach(ch => {
    if (ch.options.plugins && ch.options.plugins.legend && ch.options.plugins.legend.labels)
      ch.options.plugins.legend.labels.color = tc.text;
    Object.values(ch.options.scales || {}).forEach(sc => {
      sc.ticks = Object.assign({}, sc.ticks, { color: tc.text });
      sc.grid = Object.assign({}, sc.grid, { color: tc.grid });
    });
    ch.update('none');
  });
};

{ const tc = themeColors(); Chart.defaults.color = tc.text; Chart.defaults.borderColor = tc.grid; }

new Chart(document.getElementById('dailyChart'), {
type: 'bar',
data: {
labels: dailyData.map(d => d.date.slice(5)),
datasets: [
{ label: 'Tokens (×1000)', data: dailyData.map(d => d.tokens/1000), backgroundColor: '#58a6ff', borderRadius: 4 },
]
},
options: {
responsive: true, maintainAspectRatio: false,
scales: { y: { beginAtZero: true, ticks: { callback: v => v + 'k' } } },
plugins: { legend: { display: false } }
}
});

new Chart(document.getElementById('costChart'), {
type: 'line',
data: {
labels: dailyData.map(d => d.date.slice(5)),
datasets: [
{ label: 'Cost ($)', data: dailyData.map(d => d.cost), borderColor: '#d29922', backgroundColor: 'rgba(210,153,34,0.1)', fill: true, tension: 0.3, pointRadius: 2 },
]
},
options: {
responsive: true, maintainAspectRatio: false,
scales: { y: { beginAtZero: true, ticks: { callback: v => '$' + v } } },
plugins: { legend: { display: false } }
}
});

const agentCtx = document.getElementById('agentPieChart');
if (agentCtx) {
const labels = $AGENT_PIE_LABELS;
const vals = $AGENT_PIE_VALS;
const colors = ['#3fb950','#58a6ff','#d29922','#f85149','#bc8cff','#ff7b72','#79c0ff','#a5d6ff','#c9d1d9'];
new Chart(agentCtx, {
type: 'doughnut',
data: {
labels: labels,
datasets: [{ data: vals, backgroundColor: labels.map((_,i) => colors[i % colors.length]) }]
},
options: { responsive: true, plugins: { legend: { position: 'right', labels: { color: themeColors().text } } } }
});
}

const agentCostCtx = document.getElementById('agentCostPieChart');
if (agentCostCtx) {
const labels = $COST_BY_AGENT.map(d => d.agent);
const vals = $COST_BY_AGENT.map(d => d.cost);
const colors = ['#58a6ff','#d29922','#3fb950','#f85149','#bc8cff','#ff7b72','#79c0ff','#a5d6ff','#c9d1d9'];
new Chart(agentCostCtx, {
type: 'doughnut',
data: {
labels: labels,
datasets: [{ data: vals, backgroundColor: labels.map((_,i) => colors[i % colors.length]) }]
},
options: { responsive: true, plugins: { legend: { position: 'right', labels: { color: themeColors().text } } } }
});
}

window.refreshChartTheme();
</script>
</body>
</html>
"""


def format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1e6:.2f}M"
    elif n >= 1_000:
        return f"{n/1e3:.1f}K"
    return str(n)


def build_widget_html(records, force_quota=False):
    """Tech-vibe 桌面 widget：配額 gauge + burn-rate + 用量圖。純 SVG 無外部依賴。"""
    now = datetime.now(tz=HKT)
    today_str = now.strftime("%Y-%m-%d")
    week_ago = now - timedelta(days=7)

    providers = load_providers()

    def provider_of(model):
        m = (model or "").lower()
        for p in providers:
            for pat in p.get("model_patterns", []):
                if pat in m:
                    return p["id"]
        return "other"

    today_records = [r for r in records if r["started_at"].strftime("%Y-%m-%d") == today_str]
    week_records = [r for r in records if r["started_at"] >= week_ago]
    today_tokens = sum(r["total_tokens"] for r in today_records)
    week_tokens = sum(r["total_tokens"] for r in week_records)
    range_str = f"{week_ago.strftime('%m/%d')} → NOW"   # rolling 7 天窗口

    cc_today = sum(r["total_tokens"] for r in today_records if r["agent"] == "claude-code")
    oc_today = today_tokens - cc_today

    # 今日各 provider tokens（stacked bar）
    by_provider_today = defaultdict(int)
    for r in today_records:
        by_provider_today[provider_of(r["model"])] += r["total_tokens"]

    # ── 配額數據 ──
    quotas = get_all_quotas(force=force_quota)
    zai = next((q for q in quotas if q["kind"] == "coding-plan"), None)
    payg_list = [q for q in quotas if q["kind"] == "pay-as-you-go"]

    weekly_win = five_h_win = tools_win = None
    zai_err = None
    if zai:
        qd = zai.get("quota") or {}
        zai_err = qd.get("error")
        for w in qd.get("windows", []):
            if w["label"].endswith("w"):
                weekly_win = w
            elif w["label"].endswith("h"):
                five_h_win = w
            elif w["type"] == "TIME_LIMIT":
                tools_win = w

    STATUS_STYLE = {
        "on-track":  ("#00e5a0", "ON TRACK"),
        "at-risk":   ("#ffb454", "AT RISK"),
        "over-pace": ("#ff5370", "OVER PACE"),
    }

    # ── 週配額 donut gauge ──
    wk_pct = weekly_win["used_pct"] if weekly_win else 0
    wk_budget = (weekly_win or {}).get("budget") or {}
    status = wk_budget.get("status", "on-track")
    st_color, st_label = STATUS_STYLE.get(status, STATUS_STYLE["on-track"])
    hours_left = wk_budget.get("hours_to_reset", 0)
    reset_str = f"{int(hours_left // 24)}d {int(hours_left % 24)}h" if hours_left >= 24 else f"{hours_left:.0f}h"
    wk_reset_abs = ""
    if weekly_win and weekly_win.get("reset_at_ms"):
        wk_reset_abs = datetime.fromtimestamp(weekly_win["reset_at_ms"] / 1000, tz=HKT).strftime("%m/%d %H:%M")
    h5_reset_abs = ""
    if five_h_win and five_h_win.get("reset_at_ms"):
        h5_reset_abs = datetime.fromtimestamp(five_h_win["reset_at_ms"] / 1000, tz=HKT).strftime("%H:%M")
    gauge_color = "#00e5a0" if wk_pct < 60 else ("#ffb454" if wk_pct < 85 else "#ff5370")
    CIRC = 2 * 3.14159 * 54
    dash = wk_pct / 100 * CIRC
    pace_pct = wk_budget.get("pace_pct", 0)
    pace_dash = pace_pct / 100 * CIRC

    # 🔥 燃燒火花：掛在進度弧線的尖端（角度 = 用量%），白熱核心 + 同色光暈
    ember_svg = ""
    if wk_pct > 0:
        ember_svg = f'''<g transform="rotate({wk_pct * 3.6:.1f} 66 66)">
        <circle cx="66" cy="12" r="5.5" class="ember-halo" style="fill:{gauge_color};"/>
        <circle cx="66" cy="12" r="2.4" class="ember-core"/>
      </g>'''

    warn_html = ""
    if wk_budget.get("will_exhaust_before_reset"):
        eh = wk_budget.get("projected_exhaust_h", 0)
        warn_html = f'<div class="warn">▲ At current pace: exhausted in {eh:.0f}h ({hours_left - eh:.0f}h before reset)</div>'
    elif status == "on-track" and wk_pct > 0:
        warn_html = f'<div class="ok-line">pace {pace_pct:.0f}% · used {wk_pct:.0f}% · on budget</div>'

    def mini_bar(label, pct, color, right):
        return f'''<div class="mini-row">
      <span class="mini-label">{label}</span>
      <div class="mini-track"><div class="mini-fill" style="width:{min(100, pct):.0f}%;background:{color};box-shadow:0 0 6px {color};"></div></div>
      <span class="mini-val">{right}</span>
    </div>'''

    five_h_html = mini_bar(
        f"5H<i class='rst'>→{h5_reset_abs}</i>" if h5_reset_abs else "5H",
        five_h_win["used_pct"] if five_h_win else 0,
        "#00e5a0", f"{five_h_win['used_pct']:.0f}%" if five_h_win else "—")
    tools_html = ""
    if tools_win and tools_win.get("limit"):
        tp = tools_win["current"] / tools_win["limit"] * 100
        tools_html = mini_bar("TOOLS", tp, "#58a6ff", f"{tools_win['current']}/{tools_win['limit']}")

    # ── Pay-as-you-go providers（可多個：DeepSeek、Grok…）──
    # 有餘額 API 的顯示餘額；沒有的（如 xAI 無公開端點）顯示本地 7 天花費
    payg_html = ""
    payg_days = {}
    for pq in payg_list:
        pid = pq["id"]
        pcolor = pq.get("color", "#ffb454")
        price = pq.get("price_per_mtok")   # 記錄無 cost 時（如 Claude Code 來源）按此計價

        def rec_cost(r):
            if r["cost"]:
                return r["cost"]
            if price:
                return (r["input_tokens"] * price.get("input", 0)
                        + r["output_tokens"] * price.get("output", 0)) / 1e6
            return 0.0

        day_costs = []
        for i in range(13, -1, -1):
            dkey = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            day_costs.append(sum(
                rec_cost(r) for r in records
                if r["started_at"].strftime("%Y-%m-%d") == dkey and provider_of(r["model"]) == pid))
        max_c = max(day_costs) or 1
        pts = " ".join(f"{6 + i * (188 / 13):.1f},{40 - (c / max_c) * 32:.1f}" for i, c in enumerate(day_costs))
        spark_fill_pts = f"6,40 {pts} 194,40"
        week_cost = sum(day_costs[-7:])
        payg_days[pid] = [
            {"d": (now - timedelta(days=13 - i)).strftime("%m/%d"),
             "c": round(c, 4),
             "x": round(6 + i * (188 / 13), 1),
             "y": round(40 - (c / max_c) * 32, 1)}
            for i, c in enumerate(day_costs)
        ]

        bal = (pq.get("quota") or {}).get("balance_usd")
        low = pq.get("low_balance_warn_usd")
        topup = pq.get("credit_topup_usd")
        if bal is not None:
            headline = f"${bal:.2f}"
            h_color = "#ff5370" if (low and bal < low) else pcolor
        elif topup is not None:
            # 無餘額 API → 本地估算：儲值額 − 本地追蹤到的累計花費（credit_since 起）
            since = pq.get("credit_since", "")
            spent = sum(rec_cost(r) for r in records
                        if provider_of(r["model"]) == pid
                        and (not since or r["started_at"].strftime("%Y-%m-%d") >= since))
            est = topup - spent
            headline = f"≈${est:.2f} EST"
            h_color = "#ff5370" if (low and est < low) else pcolor
        else:
            headline = f"7D ${week_cost:.2f}"
            h_color = pcolor
        payg_html += f'''<div class="sec">
  <div class="sec-title"><span>◈ {pq.get("label", pid).upper()}</span>
    <span class="bal" style="color:{h_color};text-shadow:0 0 8px {h_color};">{headline}</span></div>
  <div class="spark-wrap" data-payg="{pid}" style="color:{pcolor};">
    <svg width="100%" height="44" viewBox="0 0 200 44" preserveAspectRatio="none">
      <polygon points="{spark_fill_pts}" fill="{pcolor}14"/>
      <polyline points="{pts}" fill="none" stroke="{pcolor}" stroke-width="1.5"
        style="filter:drop-shadow(0 0 3px {pcolor});"/>
    </svg>
    <div class="p-guide"></div>
    <div class="p-dot"></div>
    <div class="p-tip"></div>
  </div>
  <div class="spark-caption">14D SPEND · THIS WEEK ${week_cost:.3f}</div>
</div>

'''
    payg_days_json = json.dumps(payg_days)

    # ── 今日 provider stacked bar ──
    color_of = {p["id"]: p.get("color", "#8b949e") for p in providers}
    seg_total = sum(by_provider_today.values()) or 1
    segs = ""
    legend = ""
    for pid, tok in sorted(by_provider_today.items(), key=lambda x: -x[1]):
        c = color_of.get(pid, "#8b949e")
        segs += f'<div style="width:{tok / seg_total * 100:.1f}%;background:{c};box-shadow:0 0 8px {c};"></div>'
        legend += f'<span class="lg"><i style="background:{c}"></i>{pid} {format_tokens(tok)}</span>'

    # ── Top models（7 天，看哪個模型用最多）──
    by_model = defaultdict(int)
    for r in week_records:
        m = (r["model"] or "").split("[")[0].strip()   # glm-5.2[1m] → glm-5.2 合併
        if m:
            by_model[m] += r["total_tokens"]
    top_models = sorted(by_model.items(), key=lambda x: -x[1])[:5]
    max_m = top_models[0][1] if top_models else 1
    models_html = ""
    for m, tok in top_models:
        c = color_of.get(provider_of(m), "#8b949e")
        share = tok / (week_tokens or 1) * 100
        models_html += f"""<div class="mini-row">
      <span class="model-label" style="color:{c};">{m[:20]}</span>
      <div class="mini-track"><div class="mini-fill" style="width:{tok / max_m * 100:.0f}%;background:{c};box-shadow:0 0 6px {c};"></div></div>
      <span class="mini-val">{format_tokens(tok)}<i class="share">{share:.0f}%</i></span>
    </div>"""

    plan_label = (zai.get("quota", {}).get("plan") or "").upper() if zai and not zai_err else ""
    zai_title = (zai or {}).get("label", "Z.AI").upper()

    hero_html = f'''<div class="sec-title"><span>◈ {zai_title}</span><span class="plan-chip">{plan_label}</span></div>
  <div class="hero">
    <svg width="132" height="132" viewBox="0 0 132 132">
      <circle cx="66" cy="66" r="54" fill="none" stroke-width="11" style="stroke:var(--w-track);"/>
      <circle cx="66" cy="66" r="54" fill="none" stroke="{gauge_color}" stroke-width="11"
        stroke-dasharray="{dash:.1f} {CIRC:.1f}" stroke-linecap="round"
        transform="rotate(-90 66 66)" class="gauge-arc" style="--gc:{gauge_color};"/>
      <circle cx="66" cy="66" r="63" fill="none" stroke-width="1.5" style="stroke:var(--w-border);"
        stroke-dasharray="2 4"/>
      <line x1="66" y1="3" x2="66" y2="13" stroke-width="2" style="stroke:var(--w-faint);"
        transform="rotate({pace_pct * 3.6:.0f} 66 66)"/>
      {ember_svg}
      <text x="66" y="62" text-anchor="middle" font-size="26" font-weight="700" font-family="inherit" style="fill:var(--w-fg);">{wk_pct:.0f}%</text>
      <text x="66" y="80" text-anchor="middle" font-size="10" font-family="inherit" style="fill:var(--w-muted);">WEEKLY CAP</text>
    </svg>
    <div class="hero-right">
      <div class="status-chip" style="color:{st_color};border-color:{st_color};text-shadow:0 0 8px {st_color};">{st_label}</div>
      <div class="reset-line">RESET <span class="reset-t">{reset_str}</span> · {wk_reset_abs}</div>
      {five_h_html}
      {tools_html}
    </div>
  </div>
  {warn_html}''' if zai and not zai_err else f'<div class="sec-title"><span>◈ {zai_title}</span><span class="warn-chip">OFFLINE</span></div>'

    return f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#05080d">
<title>LLM USAGE</title>
<script>
// 渲染前套用已存主題，避免閃爍
(function() {{
  try {{
    var t = localStorage.getItem('llmwidget-theme');
    if (t) document.documentElement.setAttribute('data-theme', t);
  }} catch (e) {{}}
}})();
</script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{
  --w-bg:#05080d; --w-grid:rgba(0,229,160,0.025); --w-card:rgba(10,15,22,0.82);
  --w-border:#1c2530; --w-fg:#e6edf3; --w-muted:#7d8590; --w-faint:#3d4b5c; --w-track:#111820;
}}
[data-theme="light"] {{
  --w-bg:#eef1f6; --w-grid:rgba(0,150,110,0.06); --w-card:rgba(255,255,255,0.92);
  --w-border:#d5dbe3; --w-fg:#1f2328; --w-muted:#57606a; --w-faint:#8a939e; --w-track:#dfe4ea;
}}
html, body {{ scrollbar-width: none; }}
::-webkit-scrollbar {{ width:0; height:0; }}
body {{
  background:
    linear-gradient(var(--w-grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--w-grid) 1px, transparent 1px),
    var(--w-bg);
  background-size: 22px 22px, 22px 22px, 100% 100%;
  color:var(--w-fg);
  transition: background 0.3s, color 0.3s;
  font-family: ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, monospace;
  padding:14px; max-width:380px; margin:0 auto;
  font-variant-numeric: tabular-nums;
}}
.head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }}
.head .t {{ font-size:13px; font-weight:700; letter-spacing:2px; color:#00e5a0; text-shadow:0 0 10px rgba(0,229,160,.6); }}
.head .t::before {{ content:'▮ '; animation: blink 1.6s step-end infinite; }}
@keyframes blink {{ 50% {{ opacity:.25; }} }}
.head .time {{ font-size:10px; color:var(--w-muted); }}
.tgl {{ cursor:pointer; color:var(--w-muted); font-size:14px; line-height:1; user-select:none; }}
.tgl:hover {{ color:var(--w-fg); }}
.sec {{ border:1px solid var(--w-border); border-radius:10px; padding:12px; margin-bottom:10px;
       background:var(--w-card); transition: background 0.3s, border-color 0.3s; }}
.sec-title {{ display:flex; justify-content:space-between; align-items:center;
             font-size:10px; letter-spacing:1.5px; color:var(--w-muted); margin-bottom:8px; }}
.range {{ font-size:8.5px; color:var(--w-faint); letter-spacing:.5px; }}
.plan-chip {{ color:#00e5a0; border:1px solid #00e5a044; padding:1px 7px; border-radius:9px; font-size:9px; }}
.warn-chip {{ color:#ff5370; border:1px solid #ff537044; padding:1px 7px; border-radius:9px; font-size:9px; }}
.hero {{ display:flex; align-items:center; gap:14px; }}
.hero-right {{ flex:1; display:flex; flex-direction:column; gap:7px; }}
.status-chip {{ align-self:flex-start; font-size:11px; font-weight:700; letter-spacing:1.5px;
               border:1px solid; padding:3px 10px; border-radius:4px; }}
.reset-line {{ font-size:9px; color:var(--w-muted); letter-spacing:.3px; white-space:nowrap; }}
.reset-t {{ color:var(--w-fg); font-weight:700; }}
.mini-row {{ display:flex; align-items:center; gap:7px; }}
.mini-label {{ font-size:9px; color:var(--w-muted); width:58px; letter-spacing:1px; }}
.mini-label .rst {{ font-style:normal; color:var(--w-faint); font-size:8px; display:block; letter-spacing:0; }}
.mini-track {{ flex:1; height:7px; background:var(--w-track); border-radius:4px; overflow:hidden; }}
.mini-fill {{ height:100%; border-radius:4px; }}
.mini-val {{ font-size:10px; color:var(--w-fg); width:64px; text-align:right; }}
.mini-val .share {{ font-style:normal; color:var(--w-muted); font-size:8.5px; margin-left:3px; }}
.model-label {{ font-size:9.5px; width:104px; letter-spacing:.3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.sec .mini-row {{ margin-bottom:6px; }}
.sec .mini-row:last-child {{ margin-bottom:0; }}
.warn {{ margin-top:9px; font-size:10.5px; color:#ffb454; border:1px solid #ffb45433;
        background:rgba(255,180,84,0.06); border-radius:6px; padding:6px 9px; }}
.ok-line {{ margin-top:9px; font-size:10px; color:#3d8168; }}
.bal {{ font-size:16px; font-weight:700; }}
.spark-caption {{ font-size:9px; color:var(--w-muted); letter-spacing:1px; margin-top:3px; }}
.kpis {{ display:flex; gap:10px; margin-bottom:8px; }}
.kpi {{ flex:1; }}
.kpi .v {{ font-size:20px; font-weight:700; color:var(--w-fg); }}
.kpi .k {{ font-size:9px; color:var(--w-muted); letter-spacing:1.5px; margin-top:1px; }}
.stack {{ display:flex; height:9px; border-radius:5px; overflow:hidden; background:var(--w-track); margin:7px 0 6px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:8px; font-size:9.5px; color:var(--w-muted); }}
.lg i {{ display:inline-block; width:7px; height:7px; border-radius:2px; margin-right:4px; }}
.split {{ font-size:9.5px; color:var(--w-muted); margin-top:5px; letter-spacing:.5px; }}
.foot {{ text-align:center; font-size:9px; color:var(--w-faint); cursor:pointer; margin-top:4px; letter-spacing:1px; }}
/* 🔥 gauge 燃燒動畫 */
.gauge-arc {{ animation: arcBreath 2.4s ease-in-out infinite; }}
@keyframes arcBreath {{
  0%, 100% {{ filter: drop-shadow(0 0 4px var(--gc)); }}
  50%      {{ filter: drop-shadow(0 0 11px var(--gc)); }}
}}
.ember-halo {{ opacity:.55; transform-box: fill-box; transform-origin: center;
              animation: emberHalo 0.85s ease-in-out infinite; }}
.ember-core {{ fill:#fffbe6; animation: emberCore 0.85s ease-in-out infinite; }}
@keyframes emberHalo {{
  0%, 100% {{ transform: scale(1); opacity:.55; }}
  50%      {{ transform: scale(1.7); opacity:.2; }}
}}
@keyframes emberCore {{
  0%, 100% {{ opacity:1; }}
  45%      {{ opacity:.55; }}
  70%      {{ opacity:.85; }}
}}
/* 💰 sparkline hover */
.spark-wrap {{ position:relative; }}
.p-dot {{ position:absolute; width:8px; height:8px; border-radius:50%; background:currentcolor;
         transform:translate(-50%,-50%); pointer-events:none; display:none; z-index:4;
         transition: left .12s ease, top .12s ease; animation: dotPulse 1s ease-in-out infinite; }}
.p-guide {{ position:absolute; top:2px; bottom:2px; width:1px; background:currentcolor; opacity:.35;
           pointer-events:none; display:none; transition: left .12s ease; }}
.p-tip {{ position:absolute; top:-4px; transform:translate(-50%,-100%); background:var(--w-card);
         color:var(--w-fg); font-size:9.5px; padding:3px 8px; border:1px solid;
         border-radius:6px; white-space:nowrap; pointer-events:none; display:none; z-index:5;
         transition: left .12s ease; }}
@keyframes dotPulse {{
  0%, 100% {{ box-shadow: 0 0 4px currentcolor; }}
  50%      {{ box-shadow: 0 0 12px currentcolor; }}
}}
</style>
</head>
<body>
<div class="head">
  <span class="t">LLM USAGE</span>
  <span style="display:flex;align-items:center;gap:8px;">
    <span class="time">{now.strftime('%m/%d %H:%M')} HKT</span>
    <span class="tgl" id="tgl" onclick="toggleTheme()" title="Light/Dark">◐</span>
  </span>
</div>

<div class="sec">
  {hero_html}
</div>

{payg_html}

<div class="sec">
  <div class="sec-title"><span>◈ TOKEN FLOW</span><span class="range">{range_str}</span></div>
  <div class="kpis">
    <div class="kpi"><div class="v">{format_tokens(today_tokens)}</div><div class="k">TODAY</div></div>
    <div class="kpi"><div class="v">{format_tokens(week_tokens)}</div><div class="k">ROLLING 7D</div></div>
  </div>
  <div class="stack">{segs}</div>
  <div class="legend">{legend}</div>
  <div class="split">CLAUDE CODE {format_tokens(cc_today)} · OPENCLAW {format_tokens(oc_today)} today</div>
</div>

<div class="sec">
  <div class="sec-title"><span>◈ TOP MODELS</span><span class="range">{range_str}</span></div>
  {models_html}
</div>

<div class="foot" onclick="location.replace('/widget?force=1')">TAP TO FORCE REFRESH · AUTO 2MIN</div>
<script>
function toggleTheme() {{
  var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  try {{ localStorage.setItem('llmwidget-theme', next); }} catch (e) {{}}
  notifyTheme();
}}
function notifyTheme() {{
  try {{
    parent.postMessage({{ type: "llm-widget-theme",
      theme: document.documentElement.getAttribute('data-theme') || 'dark' }}, "*");
  }} catch (e) {{}}
}}
notifyTheme();

// 💰 Pay-as-you-go sparkline hover（每個 provider 一張圖，各自顏色）
const PAYG_DATA = {payg_days_json};
document.querySelectorAll('.spark-wrap[data-payg]').forEach(function(wrap) {{
  const days = PAYG_DATA[wrap.dataset.payg] || [];
  if (!days.length) return;
  const dot = wrap.querySelector('.p-dot');
  const guide = wrap.querySelector('.p-guide');
  const tip = wrap.querySelector('.p-tip');
  const col = getComputedStyle(wrap).color;
  tip.style.borderColor = col;
  wrap.addEventListener('mousemove', function(e) {{
    const r = wrap.getBoundingClientRect();
    const xv = (e.clientX - r.left) / r.width * 200;
    let best = 0, bd = 1e9;
    for (let i = 0; i < days.length; i++) {{
      const d = Math.abs(days[i].x - xv);
      if (d < bd) {{ bd = d; best = i; }}
    }}
    const p = days[best];
    const px = p.x / 200 * r.width;
    dot.style.left = px + 'px'; dot.style.top = p.y + 'px'; dot.style.display = 'block';
    guide.style.left = px + 'px'; guide.style.display = 'block';
    tip.style.left = Math.max(34, Math.min(r.width - 34, px)) + 'px';
    tip.innerHTML = p.d + ' · <b style="color:' + col + '">$' + p.c.toFixed(3) + '</b>';
    tip.style.display = 'block';
  }});
  wrap.addEventListener('mouseleave', function() {{
    dot.style.display = 'none'; guide.style.display = 'none'; tip.style.display = 'none';
  }});
}});

setTimeout(() => location.replace('/widget'), 120000);
// 回報內容高度給 Übersicht 母框架，iframe 自動貼合、不出現 scrollbar
const reportH = () => {{
  try {{ parent.postMessage({{ type: "llm-widget-height", h: document.body.scrollHeight }}, "*"); }} catch (e) {{}}
}};
window.addEventListener("load", reportH);
setTimeout(reportH, 300);
</script>
</body>
</html>'''


def build_html(records):
    totals_data = get_totals(records)
    totals = totals_data["totals"]
    week = totals_data["week"]

    # Tier cards
    tier_cards = ""
    tier_colors = {"paid": "#3fb950", "free": "#58a6ff", "deepseek": "#d29922", "claude": "#bc8cff", "grok": "#ff6e5e", "unknown": "#8b949e"}
    tier_labels = {"paid": "Paid (GLM-5)", "free": "Free (GLM-4.7-Flash)", "deepseek": "DeepSeek API", "claude": "Claude (Anthropic)", "grok": "Grok (xAI)", "unknown": "Unknown"}
    for tier in ["paid", "free", "deepseek", "claude", "grok", "unknown"]:
        t = totals_data["by_tier"].get(tier, {"sessions": 0, "tokens": 0, "cost": 0.0})
        tier_cards += f"""<div class="card"><div class="label"><span class="tier-badge tier-{tier}">{tier_labels[tier]}</span></div><div class="value">{format_tokens(t["tokens"])}</div><div class="sub">{t["sessions"]} sessions · ${t["cost"]:.4f}</div></div>"""

    # Agent table rows
    agent_rows = ""
    for agent in sorted(totals_data["by_agent"].keys()):
        a = totals_data["by_agent"][agent]
        agent_rows += f"<tr><td>{agent}</td><td class=\"num\">{a['sessions']}</td><td class=\"num\">{format_tokens(a['tokens'])}</td><td class=\"num\">${a['cost']:.4f}</td></tr>"

    # Tier table rows
    tier_rows = ""
    for tier in ["paid", "free", "deepseek", "claude", "grok", "unknown"]:
        t = totals_data["by_tier"].get(tier, {"sessions": 0, "tokens": 0, "cost": 0.0})
        tier_rows += f"<tr><td><span class=\"tier-badge tier-{tier}\">{tier_labels[tier]}</span></td><td class=\"num\">{t['sessions']}</td><td class=\"num\">{format_tokens(t['tokens'])}</td><td class=\"num\">${t['cost']:.4f}</td></tr>"

    # Recent sessions
    recent = sorted(records, key=lambda r: r["started_at"], reverse=True)[:20]
    recent_rows = ""
    for r in recent:
        time_str = r["started_at"].strftime("%m/%d %H:%M")
        recent_rows += f"<tr><td>{time_str}</td><td>{r['agent']}</td><td>{r['model']}</td><td class=\"num\">{format_tokens(r['total_tokens'])}</td><td class=\"num\">${r['cost']:.4f}</td></tr>"

    # Daily data (30 days)
    days = get_timeline(records)
    daily_data_json = json.dumps(days)

    # Agent pie chart data
    agents_sorted = sorted(totals_data["by_agent"].items(), key=lambda x: -x[1]["tokens"])
    agent_pie_labels = json.dumps([a[0] for a in agents_sorted])
    agent_pie_vals = json.dumps([a[1]["tokens"] for a in agents_sorted])

    cost_by_agent = [{"agent": a, "cost": round(totals_data["by_agent"][a]["cost"], 4)} for a in sorted(totals_data["by_agent"].keys())]
    cost_by_agent_json = json.dumps(cost_by_agent)

    now_str = datetime.now(tz=HKT).strftime("%Y-%m-%d %H:%M:%S HKT")

    html = HTML_TEMPLATE
    html = html.replace("$LAST_UPDATED", now_str)
    html = html.replace("$TOTAL_SESSIONS", str(totals["sessions"]))
    html = html.replace("$WEEK_SESSIONS", str(week["sessions"]))
    html = html.replace("$TOTAL_TOKENS_FORMATTED", format_tokens(totals["total_tokens"]))
    html = html.replace("$WEEK_TOKENS_FORMATTED", format_tokens(week["tokens"]))
    html = html.replace("$INPUT_TOKENS_FORMATTED", format_tokens(totals["input_tokens"]))
    html = html.replace("$OUTPUT_TOKENS_FORMATTED", format_tokens(totals["output_tokens"]))
    html = html.replace("$TOTAL_COST", f"{totals['cost']:.4f}")
    html = html.replace("$WEEK_COST", f"{week['cost']:.4f}")
    html = html.replace("$TIER_CARDS", tier_cards)
    html = html.replace("$TIER_ROWS", tier_rows)
    html = html.replace("$AGENT_ROWS", agent_rows)
    html = html.replace("$RECENT_ROWS", recent_rows)
    html = html.replace("$DAILY_DATA", daily_data_json)
    html = html.replace("$AGENT_PIE_LABELS", agent_pie_labels)
    html = html.replace("$AGENT_PIE_VALS", agent_pie_vals)
    html = html.replace("$COST_BY_AGENT", cost_by_agent_json)

    return html


# ── HTTP Server ────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            all_records = load_all_records()
            totals_data = get_totals(all_records)
            days = get_timeline(all_records)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"totals": totals_data, "days": days, "total_records": len(all_records)}).encode())
        elif parsed.path == "/widget":
            all_records = load_all_records()
            html = build_widget_html(all_records, force_quota="force=1" in (parsed.query or ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(html.encode())
        elif parsed.path == "/api/summary":
            all_records = load_all_records()
            now = datetime.now(tz=HKT)
            week_ago = now - timedelta(days=7)
            today = now.strftime("%Y-%m-%d")
            week_records = [r for r in all_records if r["started_at"] >= week_ago]
            today_records = [r for r in all_records if r["started_at"].strftime("%Y-%m-%d") == today]
            today_tokens = sum(r["total_tokens"] for r in today_records)
            week_tokens = sum(r["total_tokens"] for r in week_records)
            total_tokens = sum(r["total_tokens"] for r in all_records)
            total_cost = sum(r["cost"] for r in all_records)
            week_cost = sum(r["cost"] for r in week_records)
            total_sessions = len(all_records)
            week_sessions = len(week_records)
            today_sessions = len(today_records)
            deepseek = get_deepseek_balance()
            cc_today = [r for r in today_records if r["agent"] == "claude-code"]
            cc_week = [r for r in week_records if r["agent"] == "claude-code"]
            summary = {
                "timestamp": now.isoformat(),
                "today": {"sessions": today_sessions, "tokens": today_tokens},
                "week": {"sessions": week_sessions, "tokens": week_tokens, "cost": round(week_cost, 4)},
                "total": {"sessions": total_sessions, "tokens": total_tokens, "cost": round(total_cost, 4)},
                "deepseek": {"balance": round(deepseek["balance"], 2)} if deepseek else None,
                "zai_plan": {"cost_usd": 29.99, "model": "Pro"},
                "zai_usage_estimate": estimate_zai_usage(all_records),
                "claude_code": {
                    "today_tokens": sum(r["total_tokens"] for r in cc_today),
                    "week_tokens": sum(r["total_tokens"] for r in cc_week),
                },
                "top_models": {},
            }
            by_model = defaultdict(lambda: {"tokens": 0, "sessions": 0})
            for r in all_records:
                by_model[r["model"]]["tokens"] += r["total_tokens"]
                by_model[r["model"]]["sessions"] += 1
            summary["top_models"] = {m: d for m, d in sorted(by_model.items(), key=lambda x: -x[1]["tokens"])[:5]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(summary, ensure_ascii=False).encode())
        elif parsed.path == "/api/quota":
            force = "force=1" in (parsed.query or "")
            quotas = get_all_quotas(force=force)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "timestamp": datetime.now(tz=HKT).isoformat(),
                "providers": quotas,
            }, ensure_ascii=False).encode())
        elif parsed.path == "/api/export":
            all_records = sorted(load_all_records(), key=lambda r: r["started_at"], reverse=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # Convert datetimes for JSON
            export = []
            for r in all_records:
                er = dict(r)
                er["started_at"] = r["started_at"].isoformat()
                er["ended_at"] = r["ended_at"].isoformat() if r["ended_at"] else None
                export.append(er)
            self.wfile.write(json.dumps(export).encode())
        else:
            all_records = load_all_records()
            html = build_html(all_records)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

    def log_message(self, format, *args):
        # Quiet logs
        pass


def main():
    parser = argparse.ArgumentParser(description="LLM Usage Dashboard")
    parser.add_argument("--port", type=int, default=8099, help="HTTP port")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (use 0.0.0.0 for LAN/Tailscale access)")
    args = parser.parse_args()

    records = load_sessions(SESSIONS_PATH)
    gf_records = load_sessions(GF_SESSIONS_PATH)
    cc_records = load_claude_code()
    all_records = records + gf_records + cc_records

    print(f"📊 LLM Usage Dashboard")
    print(f"   Loaded {len(all_records)} records (main: {len(records)}, gf: {len(gf_records)}, claude-code: {len(cc_records)})")
    print(f"   Total tokens: {sum(r['total_tokens'] for r in all_records):,}")
    print(f"   Total cost: ${sum(r['cost'] for r in all_records):.4f}")
    print(f"")
    print(f"   Open http://127.0.0.1:{args.port} in browser")
    print(f"   Press Ctrl+C to stop")

    server = HTTPServer((args.host, args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
