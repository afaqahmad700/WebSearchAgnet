# Live Web Research Agent 🔎

An AI agent you **watch work in real time**. Ask a question in the browser; it searches the
web, reads pages, reasons step-by-step, and writes a cited answer — every step streamed live
to a dashboard.

```
think  →  search  →  read page  →  think  →  read page  →  …  →  final cited answer
```

- **Zero dependencies** — pure Python standard library. The only thing you install is Python.
- **Brain:** Groq (free, fast) via your `GROQ_API_KEY`.
- **Search:** [Tavily](https://tavily.com) if `TAVILY_API_KEY` is set (cleaner/more reliable),
  otherwise it scrapes DuckDuckGo HTML (no key needed, but flakier).

---

## 1. Install Python (one time)
You currently only have the Microsoft Store *stub*, not real Python.

- Easiest: open **PowerShell** and run:
  ```powershell
  winget install Python.Python.3.12
  ```
- Or download from <https://www.python.org/downloads/> and **tick "Add python.exe to PATH"** during install.

Close and reopen your terminal, then confirm:
```powershell
python --version
```
You should see `Python 3.12.x` (not the "not found / Store" message).

> If `python` still opens the Microsoft Store: **Settings → Apps → Advanced app settings →
> App execution aliases**, and turn **OFF** the two `python.exe` / `python3.exe` aliases.

## 2. Set your Groq key
Free key at <https://console.groq.com/keys>. In the same terminal:
```powershell
# PowerShell
$env:GROQ_API_KEY = "gsk_your_key_here"
# (Optional, for better search) $env:TAVILY_API_KEY = "tvly_your_key_here"
```

## 3. Run it
```powershell
cd D:\claude\research-agent
python research_agent.py
```
Your browser opens to **http://localhost:8000**. Type a question, hit **Research**, and watch
it work. Press **Ctrl+C** in the terminal to stop.

---

## What each live card means
| Card | Meaning |
|------|---------|
| **Thinking** (grey) | The agent's reasoning about what to do next. |
| **Searching the web** (blue) | A web search it's running, with the query. |
| **Reading page** (blue) | A URL it decided to open and read. |
| **Result / Read** (purple) | What the tool returned (search hits or page text). |
| **Final answer** (green) | The finished, cited answer. |

## Tuning (optional env vars)
| Variable | Default | Purpose |
|----------|---------|---------|
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Which Groq model is the brain. |
| `AGENT_MAX_STEPS` | `8` | Max think/act loops before it must answer. |
| `AGENT_PORT` | `8000` | Dashboard port. |
| `TAVILY_API_KEY` | _(unset)_ | Use Tavily search instead of DuckDuckGo. |

## How it works (1 paragraph)
`research_agent.py` runs a tiny standard-library web server (`ThreadingHTTPServer`). The
dashboard opens a **Server-Sent Events** stream (`/events`). When you submit a question, the
agent loop asks Groq for a JSON decision (`search` / `read` / `finish`), executes that tool,
feeds the result back, and **publishes every step** to all connected dashboards — which is why
you see it happen live. No database, no framework, no external packages.
