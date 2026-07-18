# 🚀 llm-usage-widget 宣傳素材包

Banner 圖：repo 的 `docs/social-preview.png`（1280×640，發文都用它）

---

## X / Twitter（英文主推）

> Every LLM coding plan has a weekly cap. Nobody tells you when you're about to hit it.
>
> So I built a macOS desktop widget that does:
> ◈ live quota gauge + reset countdown
> ◈ burn-rate: "exhausted in 33h — 19h before reset"
> ◈ which model is eating your budget
>
> 100% local, zero deps, MIT.
> github.com/Cadmusyiu/llm-usage-dashboard

（附 social-preview.png，最好再錄一段 10 秒拖曳 widget 的實機影片，影片曝光通常 3-5 倍）

## X / Twitter（中文）

> 用 coding plan 的人都有過這種恐懼：週五晚上發現週配額用完了。
>
> 做了一個 macOS 桌面 widget 治療 compute anxiety：
> ◈ 配額 donut + 重置倒數，直接釘在桌布上
> ◈ 燒錢速度預測：「照這速度 33 小時後斷糧」
> ◈ 哪個模型吃掉你的預算，一目了然
>
> 100% 本地運行，開源 MIT
> github.com/Cadmusyiu/llm-usage-dashboard

---

## Reddit

### r/ClaudeAI 或 r/ClaudeCode（最對口）
**標題**：I built a macOS desktop widget that shows my coding-plan quota burn rate — no more "surprise, your week is gone"

**內文**：
Like a lot of you I'm on a capped coding plan (Z.AI GLM via Claude Code). The 5h/weekly windows are invisible until you hit them, which gave me constant compute anxiety.

So I built a desktop widget that parses Claude Code transcripts locally + hits the provider quota API, and shows: weekly gauge with reset countdown, a burn-rate projection ("at current pace: exhausted in 33h, 19h before reset"), and a per-model breakdown so you can see what's actually eating the budget.

100% local (keys read from where they already live, never stored), zero pip deps, MIT. Provider mix is a JSON config — quota fetchers are pluggable if you're on something else.

GitHub: https://github.com/Cadmusyiu/llm-usage-dashboard

### r/macapps
**標題**：LLM Usage Widget — open-source desktop widget for tracking AI coding-plan quotas (Übersicht-based)

### r/LocalLLaMA
角度：多 provider token 追蹤 + 本地隱私。強調「everything stays on your machine」。

---

## Hacker News（Show HN）

**標題**：Show HN: A macOS widget that projects when your LLM quota will run out
**URL**：https://github.com/Cadmusyiu/llm-usage-dashboard

**首條留言（自己發）**：
I use an LLM coding plan with a weekly token cap. The provider shows usage % buried in a web dashboard, but what I actually want to know is: *at my current pace, will I make it to the reset?*

So this widget computes a burn rate (usage % vs elapsed window %) and projects the exhaustion time. It parses Claude Code transcripts locally for per-model attribution, and providers are a pluggable JSON config. Python stdlib only, UI is pure SVG served from localhost, desktop layer is Übersicht.

Happy to answer anything about the quota APIs — the Z.AI one is undocumented and I had to dig it out of another client's bundle.

---

## 待辦（手動步驟）

1. **Social preview 上傳**（我無法用 API 做）：repo → Settings → General → Social preview → 上傳 `docs/social-preview.png` → X/HN 分享連結時就會顯示 banner
2. **錄實機影片**：10-15 秒，內容 = F11 顯示桌面 → widget 在桌布上 → 拖曳 → 指一下 AT RISK 警告。QuickTime 螢幕錄影即可
3. **Awesome lists PR**（穩定流量）：awesome-claude-code、awesome-mac、awesome-ubersicht——各發一個 PR 加一行連結
4. **時機**：HN 和 Reddit 美東早上 8-10 點（台北晚上 8-10 點）發，命中率最高
5. 有人開 issue/PR 時**當天回覆**——早期回應速度直接決定二次傳播

## 發文順序建議

Day 1：X（中英各一則）+ r/ClaudeAI
Day 2-3：看 r/ClaudeAI 反應調整文案 → Show HN
Day 4+：r/macapps、r/LocalLLaMA、awesome lists PR
