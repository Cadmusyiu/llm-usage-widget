{
  "name": "LLM Usage Dashboard Widget",
  "description": "KWGT-compatible HTTP widget config for the LLM Usage Dashboard.\n\nReplace <DASHBOARD_HOST> with the host running dashboard.py — e.g. 127.0.0.1 locally, or your machine's Tailscale IP for phone access.\n\n## Setup\n\n1. Install **KWGT Pro** (paid) or **KWGT** (free, limited) from Play Store\n2. Make dashboard.py reachable from your phone (same LAN, or Tailscale)\n3. Add a KWGT widget to your home screen\n4. Create a new widget → Import → paste the KWGT formula below\n\n## Widget Formula (KWGT)\n\nUse the **HTTP Request** module in KWGT:\n\n```\n$wf(http://<DASHBOARD_HOST>:8099/api/summary)$\n```\n\nThen parse JSON fields:\n\n| KWGT Variable | JSON Path | Display |\n|:---|:---|:---|\n| `$wf(today.sessions)$` | `today.sessions` | Sessions today |\n| `$wf(today.tokens)$` | `today.tokens` | Tokens today |\n| `$wf(week.sessions)$` | `week.sessions` | Sessions this week |\n| `$wf(week.tokens)$` | `week.tokens` | Tokens this week |\n| `$wf(total.sessions)$` | `total.sessions` | Total sessions |\n| `$wf(total.tokens)$` | `total.tokens` | Total tokens |\n| `$wf(deepseek.balance)$` | `deepseek.balance` | DeepSeek balance |\n| `$wf(timestamp)$` | `timestamp` | Last updated |\n\n## Alternative: Tasker + Minimal Text Widget\n\nIf you prefer Tasker (no KWGT needed):\n\n### Tasker Profile: \"LLM Dashboard Update\"\n\n**Trigger:** Time → Every 15 minutes\n\n**Task:** HTTP Request\n- Method: GET\n- URL: `http://<DASHBOARD_HOST>:8099/api/summary`\n- Output: Variable `%llm_response`\n\n**Task (continued):** JavaScript\n```javascript\nvar data = JSON.parse(global('%llm_response'));\nvar today = data.today.tokens;\nvar week = data.week.tokens;\nvar balance = data.deepseek ? data.deepseek.balance : 'N/A';\nvar sessions = data.today.sessions;\n\nvar text = '🧠 LLM Usage\\n' +\n  'Today: ' + formatTokens(today) + ' tokens\\n' +\n  'Week: ' + formatTokens(week) + ' tokens\\n' +\n  'DS: $' + balance + '\\n' +\n  'Sessions: ' + sessions;\n\nfunction formatTokens(n) {\n  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';\n  if (n >= 1000) return (n/1000).toFixed(1) + 'K';\n  return n.toString();\n}

setLocal('llm_widget_text', text);
```\n\n**Task (continued):** Set Widget\n- Widget: Minimal Text\n- Text: `%llm_widget_text`\n\n## Quick Test\n\nOpen in Chrome on your phone (with Tailscale connected):\n`http://<DASHBOARD_HOST>:8099/api/summary`\n\n## Notes\n\n- **Tailscale must be connected** on the phone for this to work\n- Dashboard auto-starts via LaunchAgent (`com.example.llm-dashboard`)\n- Port: 8099 (not 8080 to avoid conflicts)\n- Host (LAN or Tailscale IP): `<DASHBOARD_HOST>`\n- Logs: `dashboard.log`\n\n## Widget Layout Suggestion (KWGT)\n\n```\n┌─────────────────────────┐\n│ 🧠 LLM Usage            │\n│                         │\n│ Today     419K tokens   │\n│ ████████████░░░░ 46 sess │\n│                         │\n│ Week      1.4M tokens   │\n│ ████████████████ 301    │\n│                         │\n│ 💳 DeepSeek $4.62       │\n│ █████████████████░ 95%  │\n│                         │\n│ Updated 18:51 HKT       │\n└─────────────────────────┘\n```\n",
  "setup_steps": [
    "1. Ensure Tailscale is running on both Mac mini and Android phone",
    "2. Test in phone browser: http://<DASHBOARD_HOST>:8099/api/summary",
    "3. Install KWGT Pro from Play Store",
    "4. Add KWGT widget → Edit → Add HTTP Request module",
    "5. URL: http://<DASHBOARD_HOST>:8099/api/summary",
    "6. Parse JSON fields and display in widget layout",
    "7. Set refresh interval (recommended: 15-30 min)"
  ],
  "endpoints": {
    "summary": "http://<DASHBOARD_HOST>:8099/api/summary",
    "data": "http://<DASHBOARD_HOST>:8099/api/data",
    "export": "http://<DASHBOARD_HOST>:8099/api/export",
    "html": "http://<DASHBOARD_HOST>:8099/"
  }
}
