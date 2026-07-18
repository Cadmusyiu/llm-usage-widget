#!/usr/bin/env python3
"""
LLM Usage Dashboard — Static HTML Generator + Telegram Card + Provider Cap Tracking

Features:
1. Project daily line charts (SVG)
2. Provider cap tracking (DeepSeek API balance, Z.AI Pro Plan)
3. Visual usage bars

Usage:
  python3 generate.py                    # → llm-usage.html
  python3 generate.py --telegram         # → Telegram card
  python3 generate.py --watch            # auto-regenerate
"""

import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

HKT = timezone(timedelta(hours=8))
OUTDIR = os.path.dirname(os.path.abspath(__file__))

# Override via env if your sessions live elsewhere, e.g.:
#   LLMDASH_SESSIONS_PATH=/path/to/sessions.json python3 generate.py
SESSIONS_PATH = os.environ.get("LLMDASH_SESSIONS_PATH") or os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
GF_SESSIONS_PATH = os.environ.get("LLMDASH_GF_SESSIONS_PATH") or os.path.expanduser("~/.openclaw/agents/gf/sessions/sessions.json")

# ── Provider Cap Config ─────────────────────────────────────────────────
# Read DeepSeek key from env (set it in your shell or .env), never hardcoded.
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")

# Z.AI Pro Plan monthly cost — set to your own plan's price via env if needed.
ZAI_PRO_MONTHLY_COST = float(os.environ.get("ZAI_PRO_MONTHLY_COST", "29.99"))
ZAI_CAP_Sessions = 1000000  # Pro plan sessions cap (estimated)

# Z.AI Pro Plan Rate Limits (known caps)
# 5-hour window: 500K tokens
# Weekly: 2M tokens
ZAI_5H_TOKEN_CAP = 500_000
ZAI_WEEK_TOKEN_CAP = 2_000_000

# Z.AI Usage Data - manually maintained (or from browser scraper)
# Format: {period: {"tokens": 0, "cost_usd": 0, "sessions": 0}}
# period keys: "last_5h", "this_week", "this_month"
ZAI_USAGE_PATH = os.path.join(OUTDIR, "zai_usage.json")
ZAI_USAGE = {
    "last_5h": {"tokens": 0, "cost_usd": 0, "sessions": 0},
    "this_week": {"tokens": 0, "cost_usd": 0, "sessions": 0},
    "this_month": {"tokens": 0, "cost_usd": 0, "sessions": 0},
}
if os.path.exists(ZAI_USAGE_PATH):
    try:
        with open(ZAI_USAGE_PATH) as f:
            ZAI_USAGE.update(json.load(f))
    except:
        pass

MODEL_TIERS = {
    "paid": ["glm-5.2", "glm-5-turbo"],
    "free": ["glm-4.7-flash", "glm-4.7"],
    "deepseek": ["deepseek-chat", "deepseek-v4-pro", "deepseek-v4-flash"],
}

TIER_LABELS = {
    "paid": "Paid (GLM-5)",
    "free": "Free (GLM-4.7-Flash)",
    "deepseek": "DeepSeek API",
    "unknown": "Unknown",
}

TIER_COLORS = {
    "paid": "#3fb950",
    "free": "#58a6ff",
    "deepseek": "#d29922",
    "unknown": "#8b949e",
}


def get_deepseek_balance():
    """Query DeepSeek balance API."""
    if not DEEPSEEK_KEY:
        return None
    try:
        req = Request(
            "https://api.deepseek.com/user/balance",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Accept": "application/json",
            }
        )
        with urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data.get("is_available", False):
                balance = float(data["balance_infos"][0]["total_balance"])
                sessions = int(balance / 0.005) if balance > 0 else 0  # approx: $0.005 per session
                return {"balance": balance, "sessions": sessions}
    except (URLError, HTTPError, ValueError, KeyError) as e:
        return None
    return None


# ── Session Loading ─────────────────────────────────────────────────────

def load_sessions(path):
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

        if started_ms == 0:
            continue

        started_dt = datetime.fromtimestamp(started_ms / 1000, tz=HKT)

        # Determine agent from session key
        parts = key.split(":")
        agent_id = parts[1] if len(parts) > 1 else "main"

        # Map agent to project name
        project_name = agent_id
        if agent_id == "main":
            project_name = "CadAI (Main)"
        elif agent_id == "gf":
            project_name = "GF (mac mini)"
        elif agent_id == "algo-trading":
            project_name = "MT5 EA Builder"
        elif agent_id == "travel-planner":
            project_name = "Travel Planner"
        elif agent_id == "stock-analyzer":
            project_name = "Stock Analyzer Bot"
        elif agent_id == "daily-commentary":
            project_name = "Daily Commentary"
        elif agent_id == "nlp-sentiment-trader":
            project_name = "NLP Sentiment Trader"
        elif agent_id == "game-lab":
            project_name = "Game Lab"
        elif agent_id == "market-intel":
            project_name = "Market Trend Researching"

        records.append({
            "key": key,
            "agent": agent_id,
            "project": project_name,
            "model": model,
            "tier": classify_model(model),
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "cost": cost,
            "started_at": started_dt,
            "chat_type": s.get("chatType", ""),
        })
    return records


def classify_model(model_str):
    for tier, models in MODEL_TIERS.items():
        for m in models:
            if m in model_str:
                return tier
    return "unknown"


def format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1e6:.2f}M"
    elif n >= 1_000:
        return f"{n/1e3:.1f}K"
    return str(n)


def format_tokens_short(n):
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    elif n >= 1_000:
        return f"{n/1e3:.0f}K"
    return str(n)


# ── SVG Line Chart Generator ────────────────────────────────────────────

def generate_svg_line_chart(labels, values, color, min_val, max_val, height=80, width=300):
    """Generate a simple SVG line chart."""
    n = len(values)
    if n == 0:
        return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"><text x="{width/2}" y="{height/2}" fill="#484f58" font-size="12" text-anchor="middle">No data</text></svg>'
    if max_val == min_val:
        max_val = min_val + 1
    max_val = float(max_val)
    min_val = float(min_val)

    # Scale
    def to_coords(idx, val):
        x = 30 + (idx / max(1, n - 1)) * (width - 60)
        y = height - 20 - ((val - min_val) / (max_val - min_val)) * (height - 40)
        return x, y

    # Grid lines
    grid = ""
    for i in range(5):
        y = 20 + (i / 4) * (height - 40)
        grid += f'<line x1="30" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" stroke="#21262d" stroke-width="1" />'

    # Points + polyline
    points_list = []
    for i, v in enumerate(values):
        x, y = to_coords(i, v)
        points_list.append(f"{x:.1f},{y:.1f}")
    points_str = " ".join(points_list)
    line = f'<polyline points="{points_str}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round" />'

    # Value labels on top
    value_labels = ""
    for i, v in enumerate(values):
        x, _ = to_coords(i, v)
        value_labels += f'<text x="{x}" y="{10}" fill="{color}" font-size="8" text-anchor="middle" opacity="0.8">{format_tokens_short(v)}</text>'

    # Labels at bottom
    label_labels = ""
    for i in range(0, len(labels), max(1, len(labels) // 8)):
        x, _ = to_coords(i, values[i] if i < len(values) else 0)
        label_labels += f'<text x="{x}" y="{height + 12}" fill="#8b949e" font-size="7" text-anchor="middle">{labels[i]}</text>'

    return f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
{grid}
{line}
{value_labels}
{label_labels}
</svg>'''


# ── HTML Generation ─────────────────────────────────────────────────────

def generate_html(records):
    """Generate a dark-themed self-contained HTML dashboard."""
    now = datetime.now(tz=HKT)

    # ── Provider Cap ──
    deepseek_cap = get_deepseek_balance()
    if deepseek_cap:
        deepseek_balance = deepseek_cap["balance"]
        deepseek_pct = min(100, (1 - deepseek_balance / 100) * 100)  # % spent of initial $100
    else:
        deepseek_cap = None
        deepseek_pct = 0

    # ── Compute Stats ──
    total_sessions = len(records)
    total_input = sum(r["input_tokens"] for r in records)
    total_output = sum(r["output_tokens"] for r in records)
    total_tokens = total_input + total_output
    total_cost = sum(r["cost"] for r in records)

    # Last 7 days
    week_ago = now - timedelta(days=7)
    week_records = [r for r in records if r["started_at"] >= week_ago]
    week_tokens = sum(r["total_tokens"] for r in week_records)
    week_cost = sum(r["cost"] for r in week_records)
    week_sessions = len(week_records)

    # By project (agent)
    by_project = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0, "records": []})
    for r in records:
        by_project[r["project"]]["sessions"] += 1
        by_project[r["project"]]["tokens"] += r["total_tokens"]
        by_project[r["project"]]["cost"] += r["cost"]
        by_project[r["project"]]["records"].append(r)

    project_list = sorted(by_project.items(), key=lambda x: -x[1]["tokens"])

    # Last 30 days daily data
    daily = []
    daily_max_tokens = 0
    for i in range(30, -1, -1):
        day = now - timedelta(days=i)
        date_key = day.strftime("%Y-%m-%d")
        day_records = [r for r in records if r["started_at"].strftime("%Y-%m-%d") == date_key]
        day_tokens = sum(r["total_tokens"] for r in day_records)
        day_cost = sum(r["cost"] for r in day_records)
        day_sessions = len(day_records)
        daily.append({
            "date": date_key, "label": day.strftime("%m/%d"),
            "tokens": day_tokens, "cost": day_cost, "sessions": day_sessions,
        })
        if day_tokens > daily_max_tokens:
            daily_max_tokens = day_tokens

    # By tier
    by_tier = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    for r in records:
        by_tier[r["tier"]]["sessions"] += 1
        by_tier[r["tier"]]["tokens"] += r["total_tokens"]
        by_tier[r["tier"]]["cost"] += r["cost"]

    # Recent sessions
    recent = sorted(records, key=lambda r: r["started_at"], reverse=True)[:30]

    # Tier order
    tier_order = ["paid", "free", "deepseek", "unknown"]

    # ── Build HTML ──
    # Daily bar chart
    max_bar_width = 100
    def bar_width(tokens):
        if daily_max_tokens == 0:
            return 0
        return max(1, int(tokens / daily_max_tokens * max_bar_width))

    daily_bars = ""
    for d in daily:
        bw = bar_width(d["tokens"])
        tokens_str = format_tokens_short(d["tokens"])
        daily_bars += f"""<div class="day-bar-row"><span class="day-label">{d['label']}</span><div class="bar-track"><div class="bar" style="width:{bw}%"></div></div><span class="day-val">{tokens_str}</span></div>"""

    # Cost bar
    cost_max = max(d["cost"] for d in daily) if daily else 1
    def cost_bar_width(cost):
        if cost_max == 0:
            return 0
        return max(1, int(cost / cost_max * max_bar_width))

    cost_bars = ""
    for d in daily:
        bw = cost_bar_width(d["cost"])
        cost_bars += f"""<div class="day-bar-row"><span class="day-label">{d['label']}</span><div class="bar-track"><div class="bar cost-bar" style="width:{bw}%"></div></div><span class="day-val">${d['cost']:.4f}</span></div>"""

    # Tier summary bars
    max_tier_tokens = max((by_tier[t]["tokens"] for t in tier_order), default=1)
    tier_widgets = ""
    for t in tier_order:
        data = by_tier[t]
        label = TIER_LABELS[t]
        color = TIER_COLORS[t]
        bw = int(data["tokens"] / max_tier_tokens * 100) if max_tier_tokens > 0 else 0
        percent = (data["tokens"] / total_tokens * 100) if total_tokens > 0 else 0
        tier_widgets += f"""<div class="tier-row"><div><span class="tier-dot" style="background:{color}"></span>{label}</div><div class="bar-track"><div class="bar" style="width:{bw}%;background:{color}"></div></div><span class="tier-pct">{percent:.0f}%</span><span class="tier-tok">{format_tokens(data['tokens'])}</span></div>"""

    # Project summary bars
    max_project_tokens = max((by_project[p]["tokens"] for p, _ in project_list), default=1)
    project_widgets = ""
    for project, data in project_list:
        label = project
        bw = int(data["tokens"] / max_project_tokens * 100) if max_project_tokens > 0 else 0
        percent = (data["tokens"] / total_tokens * 100) if total_tokens > 0 else 0
        project_widgets += f"""<div class="tier-row"><div>{label}</div><div class="bar-track"><div class="bar" style="width:{bw}%;background:#58a6ff"></div></div><span class="tier-pct">{percent:.0f}%</span><span class="tier-tok">{format_tokens(data['tokens'])}</span></div>"""

    # Project table
    project_rows = ""
    for project, data in project_list:
        pct = (data["tokens"] / total_tokens * 100) if total_tokens > 0 else 0
        project_rows += f"""<tr><td>{project}</td><td class="num-cell">{data['sessions']}</td><td class="num-cell">{format_tokens(data['tokens'])}</td><td class="num-cell">{pct:.0f}%</td><td class="num-cell">${data['cost']:.4f}</td></tr>"""

    # Project line chart data — compute per-project per-day tokens
    chart_labels = [d["label"] for d in daily]
    chart_values = {p: [] for p, _ in project_list}
    for d in daily:
        date_key = d["date"]
        for project, data in project_list:
            day_project_tokens = sum(
                r["total_tokens"] for r in data["records"]
                if r["started_at"].strftime("%Y-%m-%d") == date_key
            )
            chart_values[project].append(day_project_tokens)

    # ── Provider Cap Widgets ──
    cap_widgets = ""
    if deepseek_cap:
        cap_widgets += f"""<div class="provider-row"><div class="provider-name">DeepSeek API</div><div class="bar-track"><div class="bar deepseek-bar" style="width:{deepseek_pct:.1f}%"></div></div><span class="provider-pct">${deepseek_cap['balance']:.2f} remaining</span><span class="provider-cost">{deepseek_pct:.0f}% spent</span></div>"""

    # Z.AI Pro - Rate Limit Tracking (5h + Weekly)
    zai_5h = ZAI_USAGE.get("last_5h", {})
    zai_wk = ZAI_USAGE.get("this_week", {})
    zai_5h_pct = min(100, (zai_5h.get("tokens", 0) / ZAI_5H_TOKEN_CAP) * 100) if ZAI_5H_TOKEN_CAP > 0 else 0
    zai_wk_pct = min(100, (zai_wk.get("tokens", 0) / ZAI_WEEK_TOKEN_CAP) * 100) if ZAI_WEEK_TOKEN_CAP > 0 else 0
    cap_widgets += f"""<div class="provider-row"><div class="provider-name">Z.AI Pro</div><div class="provider-bar-group">
<div class="sub-bar-row"><span class="sub-label">5h</span><div class="bar-track"><div class="bar zai-bar" style="width:{zai_5h_pct:.1f}%"></div></div><span class="provider-pct">{format_tokens(zai_5h.get('tokens', 0))}</span></div>
<div class="sub-bar-row"><span class="sub-label">Week</span><div class="bar-track"><div class="bar zai-bar" style="width:{zai_wk_pct:.1f}%"></div></div><span class="provider-pct">{format_tokens(zai_wk.get('tokens', 0))}</span></div>
</div><span class="provider-cost">${ZAI_PRO_MONTHLY_COST}/mo</span></div>"""

    # Recent sessions
    recent_rows = ""
    for r in recent:
        time_str = r["started_at"].strftime("%m/%d %H:%M")
        tier_badge = f'<span style="color:{TIER_COLORS.get(r["tier"], TIER_COLORS["unknown"])}">{r["tier"]}</span>'
        recent_rows += f"""<tr><td>{time_str}</td><td>{r["project"]}</td><td>{r["model"]} {tier_badge}</td><td class="num-cell">{format_tokens(r["total_tokens"])}</td><td class="num-cell">${r["cost"]:.4f}</td></tr>"""

    # Join big-bar segments
    big_bar_segments = ""
    for t in tier_order:
        if by_tier[t]["tokens"] > 0:
            bw = by_tier[t]["tokens"] / max(total_tokens,1) * 100
            big_bar_segments += f'<div class="seg {t}" style="width:{bw:.1f}%"></div>'

    # Legend items
    legend_items = ""
    for t in tier_order:
        if by_tier[t]["sessions"] > 0:
            color = TIER_COLORS[t]
            label = TIER_LABELS[t]
            n = by_tier[t]["sessions"]
            t_ = format_tokens(by_tier[t]["tokens"])
            legend_items += f'<span class="legend-item"><span class="legend-dot" style="background:{color}"></span>{label} {n}次 {t_}</span>'

    # Chart legend
    chart_legend_items = ""
    top_8 = list(project_list[:8])
    for p_name, p_data in top_8:
        c = TIER_COLORS.get(p_data.get("tier", "unknown"), TIER_COLORS["unknown"])
        chart_legend_items += f'<span class="chart-legend-item"><span class="chart-legend-dot" style="background:{c}"></span>{p_name}</span>'

    # Project mini line charts
    project_line_charts = ""
    # Find global max across all projects for consistent Y-axis
    all_vals = []
    for p_name, p_data in top_8:
        all_vals.extend(chart_values[p_name])
    global_max = max(all_vals) if all_vals else 0
    chart_y_max = global_max if global_max > 0 else 1000
    for p_name, p_data in top_8:
        c = TIER_COLORS.get(p_data.get("tier", "unknown"), TIER_COLORS["unknown"])
        svg = generate_svg_line_chart(chart_labels, chart_values[p_name], c, 0, chart_y_max, height=60, width=240)
        project_line_charts += f'<div class="mini-chart-box"><div class="chart-name">{p_name}</div>{svg}</div>'

    ftime = now.strftime('%Y-%m-%d %H:%M')
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>🧠 LLM Usage Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 16px; max-width: 700px; margin: 0 auto; }}
h1 {{ font-size: 18px; margin-bottom: 4px; }}
h2 {{ font-size: 14px; color: #8b949e; margin-bottom: 8px; }}
.subtitle {{ color: #8b949e; font-size: 12px; margin-bottom: 16px; }}

/* ── KPI Grid ── */
.kpi-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 16px; }}
.kpi {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px; }}
.kpi .label {{ font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.3px; }}
.kpi .value {{ font-size: 20px; font-weight: 700; margin-top: 2px; }}
.kpi .sub {{ font-size: 11px; color: #8b949e; margin-top: 2px; }}
.kpi-cost .value {{ color: #d29922; }}

/* ── Usage Bar ── */
.usage-bar {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-bottom: 16px; }}
.usage-bar .bar-label {{ font-size: 11px; color: #8b949e; margin-bottom: 6px; display: flex; justify-content: space-between; }}
.usage-bar .big-bar {{ height: 20px; background: #0d1117; border-radius: 10px; overflow: hidden; display: flex; }}
.usage-bar .big-bar .seg {{ height: 100%; transition: width 0.3s; }}
.usage-bar .big-bar .seg.paid {{ background: #3fb950; }}
.usage-bar .big-bar .seg.free {{ background: #58a6ff; }}
.usage-bar .big-bar .seg.deepseek {{ background: #d29922; }}
.usage-bar .big-bar .seg.unknown {{ background: #8b949e; }}
.usage-bar .legend {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }}
.usage-bar .legend-item {{ font-size: 11px; color: #8b949e; display: flex; align-items: center; gap: 4px; }}
.usage-bar .legend-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}

/* ── Provider Cap ── */
.provider-section {{ margin-bottom: 16px; }}
.provider-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; }}
.provider-row > :first-child {{ width: 130px; flex-shrink: 0; }}
.provider-name {{ font-weight: 600; }}
.bar-track {{ flex: 1; height: 12px; background: #0d1117; border-radius: 6px; overflow: hidden; }}
.bar {{ height: 100%; background: #58a6ff; border-radius: 6px; min-width: 2px; }}
.deepseek-bar {{ background: #d29922; }}
.zai-bar {{ background: #3fb950; }}
.provider-pct {{ width: 80px; text-align: right; font-size: 11px; color: #8b949e; }}
.provider-cost {{ width: 100px; text-align: right; font-size: 11px; color: #8b949e; }}
.provider-bar-group {{ flex: 1; display: flex; flex-direction: column; gap: 3px; }}
.sub-bar-row {{ display: flex; align-items: center; gap: 4px; }}
.sub-label {{ width: 32px; font-size: 10px; color: #8b949e; flex-shrink: 0; }}
.sub-bar-row .bar-track {{ height: 8px; }}

/* ── Tier Section ── */
.section {{ margin-bottom: 16px; }}
.tier-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; }}
.tier-row > :first-child {{ width: 130px; flex-shrink: 0; display: flex; align-items: center; gap: 4px; }}
.tier-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
.bar-track {{ flex: 1; height: 12px; background: #0d1117; border-radius: 6px; overflow: hidden; }}
.bar {{ height: 100%; background: #58a6ff; border-radius: 6px; min-width: 2px; }}
.cost-bar {{ background: #d29922; }}
.tier-pct {{ width: 32px; text-align: right; font-size: 11px; color: #8b949e; }}
.tier-tok {{ width: 55px; text-align: right; font-size: 11px; font-weight: 600; }}

/* ─── Project Line Chart ─── */
.project-chart {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; margin-bottom: 16px; }}
.chart-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.chart-title {{ font-size: 13px; font-weight: 600; }}
.chart-legend {{ display: flex; flex-wrap: wrap; gap: 8px; font-size: 11px; }}
.chart-legend-item {{ display: flex; align-items: center; gap: 4px; }}
.chart-legend-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.mini-chart-box {{ background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 6px; margin-bottom: 4px; }}
.mini-chart-box .chart-name {{ font-size: 10px; color: #8b949e; margin-bottom: 2px; text-align: center; }}

/* ── Daily Chart ── */
.daily-section {{ margin-bottom: 16px; }}
.daily-scroll {{ max-height: 250px; overflow-y: auto; }}
.daily-scroll::-webkit-scrollbar {{ width: 4px; }}
.daily-scroll::-webkit-scrollbar-thumb {{ background: #30363d; border-radius: 2px; }}
.day-bar-row {{ display: flex; align-items: center; gap: 6px; margin-bottom: 3px; font-size: 11px; }}
.day-label {{ width: 36px; flex-shrink: 0; color: #8b949e; text-align: right; }}
.day-val {{ width: 48px; flex-shrink: 0; text-align: right; color: #8b949e; font-size: 10px; }}

/* ─── Project Table ─── */
.project-table {{ margin-bottom: 16px; }}
.project-table table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.project-table th, .project-table td {{ padding: 5px 8px; text-align: left; border-bottom: 1px solid #21262d; }}
.project-table th {{ color: #8b949e; font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px; }}
.project-table tr:hover td {{ background: #1c2128; }}
.num-cell {{ text-align: right; font-variant-numeric: tabular-nums; }}
.time-cell {{ white-space: nowrap; }}

/* ─── Recent Table ─── */
.recent-section {{ margin-bottom: 16px; }}
.recent-section table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.recent-section th, .recent-section td {{ padding: 5px 8px; text-align: left; border-bottom: 1px solid #21262d; }}
.recent-section th {{ color: #8b949e; font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px; }}
.recent-section tr:hover td {{ background: #1c2128; }}
.recent-section .time-cell {{ white-space: nowrap; }}

/* ─── Footer ─── */
.footer {{ text-align: center; font-size: 10px; color: #484f58; padding: 12px; }}
a {{ color: #58a6ff; text-decoration: none; }}
</style>
</head>
<body>

<h1>🧠 LLM Usage Dashboard</h1>
<div class="subtitle">{now.strftime('%Y-%m-%d %H:%M HKT')}</div>

<div class="kpi-grid">
<div class="kpi"><div class="label">Sessions</div><div class="value">{total_sessions}</div><div class="sub">+{week_sessions} this week</div></div>
<div class="kpi"><div class="label">Tokens</div><div class="value">{format_tokens(total_tokens)}</div><div class="sub">{format_tokens(total_input)} in / {format_tokens(total_output)} out</div></div>
<div class="kpi kpi-cost"><div class="label">Cost</div><div class="value">${total_cost:.4f}</div><div class="sub">+${week_cost:.4f} this week</div></div>
</div>

<div class="usage-bar">
<div class="bar-label">
<span>Model Usage Distribution</span>
<span>{total_sessions} sessions</span>
</div>
<div class="big-bar">
{big_bar_segments}
</div>
<div class="legend">
{legend_items}
</div>
</div>

<div class="provider-section">
<h2>💳 Provider Cap Tracking</h2>
{cap_widgets}
</div>

<div class="section">
<h2>📊 Project Daily Line Charts (31 days)</h2>
<div class="project-chart">
<div class="chart-header">
<div class="chart-title">Tokens (×1000)</div>
<div class="chart-legend">
{chart_legend_items}
</div>
</div>
<div style="display:flex;flex-wrap:wrap;gap:4px;">
{project_line_charts}
</div>
</div>
</div>

<div class="section">
<h2>🤖 Project Summary</h2>
{project_widgets}
</div>

<div class="project-table">
<h2>📋 Project Table</h2>
<table>
<tr><th>Project</th><th class="num-cell">Sessions</th><th class="num-cell">Tokens</th><th class="num-cell">%</th><th class="num-cell">Cost</th></tr>
{project_rows}
</table>
</div>

<div class="daily-section">
<h2>📈 Daily Usage (31 days)</h2>
<div class="daily-scroll">
{daily_bars}
</div>
<br>
<h2>💲 Daily Cost</h2>
<div class="daily-scroll">
{cost_bars}
</div>
</div>

<div class="recent-section">
<h2>🕐 Recent Sessions</h2>
<div style="max-height: 300px; overflow-y: auto;">
<table>
<tr><th>Time</th><th>Project</th><th>Model</th><th class="num-cell">Tokens</th><th class="num-cell">Cost</th></tr>
{recent_rows}
</table>
</div>
</div>

<div class="footer">LLM Usage Dashboard · Generated {ftime} · Data from OpenClaw sessions</div>
</body>
</html>"""

    return html



def generate_telegram_card(records):
    """Generate a Telegram-friendly text card with visual bars."""
    now = datetime.now(tz=HKT)
    total_tokens = sum(r["total_tokens"] for r in records)
    total_cost = sum(r["cost"] for r in records)
    total_sessions = len(records)

    week_ago = now - timedelta(days=7)
    week = [r for r in records if r["started_at"] >= week_ago]

    by_tier = defaultdict(lambda: {"sessions": 0, "tokens": 0})
    for r in records:
        by_tier[r["tier"]]["sessions"] += 1
        by_tier[r["tier"]]["tokens"] += r["total_tokens"]

    by_project = defaultdict(lambda: {"tokens": 0})
    for r in records:
        by_project[r["project"]]["tokens"] += r["total_tokens"]

    top_projects = sorted(by_project.items(), key=lambda x: -x[1]["tokens"])[:5]

    # ASCII bar
    tier_order = ["paid", "free", "deepseek"]
    max_t = max((by_tier[t]["tokens"] for t in tier_order), default=1)
    bar_chars = 16

    def ascii_bar(tokens, total):
        w = int(tokens / max_t * bar_chars) if max_t > 0 else 0
        return "█" * w + "░" * (bar_chars - w)

    lines = []
    lines.append("🧠 **LLM Usage Summary**")
    lines.append(f"📅 {now.strftime('%Y-%m-%d %H:%M HKT')}")
    lines.append("")
    lines.append(f"**Sessions:** {total_sessions} (+{len(week)} this week)")
    lines.append(f"**Tokens:** {format_tokens(total_tokens)}")
    lines.append(f"**Cost:** \u20bf{total_cost:.4f} USD")
    lines.append("")
    lines.append("📊 **Model Usage**")
    for t in tier_order:
        data = by_tier[t]
        pct = data["tokens"] / max(total_tokens, 1) * 100
        lines.append(f"{ascii_bar(data['tokens'], max_t)} {TIER_LABELS[t].replace('Paid (','').replace(')','')} {pct:.0f}% ({format_tokens(data['tokens'])})")
    lines.append("")
    lines.append("🤖 **Top Projects**")
    for project, data in top_projects:
        pct = data["tokens"] / max(total_tokens, 1) * 100
        bar_w = int(data["tokens"] / max(top_projects[0][1]["tokens"], 1) * 12)
        bar = "▓" * bar_w + "░" * (12 - bar_w)
        lines.append(f"{bar} {project}: {pct:.0f}% ({format_tokens(data['tokens'])})")
    lines.append("")
    lines.append("💳 **Provider Cap**")
    deepseek_cap = get_deepseek_balance()
    if deepseek_cap:
        ds_pct = min(100, (1 - deepseek_cap['balance'] / 100) * 100)
        lines.append(f"DeepSeek: ${deepseek_cap['balance']:.2f} ({ds_pct:.0f}% spent)")
    zai_5h = ZAI_USAGE.get("last_5h", {})
    zai_wk = ZAI_USAGE.get("this_week", {})
    lines.append(f"Z.AI Pro: 5h {format_tokens(zai_5h.get('tokens', 0))} | Week {format_tokens(zai_wk.get('tokens', 0))}")
    lines.append(f"       ${ZAI_PRO_MONTHLY_COST}/mo")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Also print Telegram card")
    parser.add_argument("--watch", action="store_true", help="Auto-regenerate every 5 min")
    parser.add_argument("--out", default="llm-usage.html", help="Output HTML filename")
    parser.add_argument("--set-zai", nargs=6, metavar=("5H_TOKENS","5H_SESSIONS","WK_TOKENS","WK_SESSIONS","MO_TOKENS","MO_SESSIONS"),
                        help="Update Z.AI usage: --set-zai 50000 10 200000 40 800000 200")
    args = parser.parse_args()

    if args.set_zai:
        import json as _json
        zai_path = os.path.join(OUTDIR, "zai_usage.json")
        zai_data = {
            "last_5h": {"tokens": int(args.set_zai[0]), "cost_usd": 0, "sessions": int(args.set_zai[1])},
            "this_week": {"tokens": int(args.set_zai[2]), "cost_usd": 0, "sessions": int(args.set_zai[3])},
            "this_month": {"tokens": int(args.set_zai[4]), "cost_usd": 0, "sessions": int(args.set_zai[5])},
        }
        with open(zai_path, "w") as f:
            _json.dump(zai_data, f, indent=2)
        print(f"✅ Z.AI usage saved to {zai_path}")
        print("   Run without --set-zai to use these values")
        return

    records = load_sessions(SESSIONS_PATH)
    gf_records = load_sessions(GF_SESSIONS_PATH)
    all_records = records + gf_records

    # Generate HTML
    html = generate_html(all_records)
    outpath = os.path.join(OUTDIR, args.out)
    with open(outpath, "w") as f:
        f.write(html)
    print(f"✅ Dashboard → {outpath}")
    print(f"   {len(all_records)} sessions, {format_tokens(sum(r['total_tokens'] for r in all_records))} tokens")

    # Generate Telegram card
    if args.telegram:
        card = generate_telegram_card(all_records)
        print("\n" + "=" * 50)
        print("TELEGRAM CARD:")
        print("=" * 50)
        print(card)
        print("=" * 50)

    if args.watch:
        print("\n👀 Watching for changes (every 5 min)...")
        last_mtime = os.path.getmtime(SESSIONS_PATH) if os.path.exists(SESSIONS_PATH) else 0
        while True:
            time.sleep(300)
            mtime = os.path.getmtime(SESSIONS_PATH) if os.path.exists(SESSIONS_PATH) else 0
            if mtime != last_mtime:
                records = load_sessions(SESSIONS_PATH)
                gf_records = load_sessions(GF_SESSIONS_PATH)
                all_records = records + gf_records
                html = generate_html(all_records)
                with open(outpath, "w") as f:
                    f.write(html)
                print(f"🔄 Regenerated → {outpath}")
                last_mtime = mtime


if __name__ == "__main__":
    main()
