# Web Search AI Agent 🔎

An AI research agent you **watch work in real time**. Ask a question in the browser; it searches
the web, reads pages, reasons step-by-step, and returns a cited answer — then you can keep asking
follow-up questions in a chat thread.

```
think  →  search  →  read page  →  think  →  read page  →  …  →  final cited answer
```

- **Zero dependencies** — pure Python standard library. The only thing you install is Python.
- **Brain:** Groq (free, fast) via your `GROQ_API_KEY`.
- **Search:** [Tavily](https://tavily.com) if `TAVILY_API_KEY` is set (cleaner/more reliable),
  otherwise it scrapes DuckDuckGo (no key needed).
- **UI:** minimalist light theme, search-engine loading animation, emoji feedback, and a
  conversational follow-up chat.

## 🌐 Live
Deployed (password-protected) on Hugging Face Spaces:
**https://aiautmationexplorer-web-search-agent.hf.space** — open the direct link in its own tab
and log in with username `user` + your `APP_PASSWORD`.

---

## Run locally

### 1. Install Python (one time)
```powershell
winget install Python.Python.3.12
```
Or download from <https://www.python.org/downloads/> and **tick "Add python.exe to PATH"**.
Then confirm in a fresh terminal: `python --version` (should show `Python 3.12.x`).

> If `python` opens the Microsoft Store instead: **Settings → Apps → Advanced app settings →
> App execution aliases**, and turn **OFF** the `python.exe` / `python3.exe` aliases.

### 2. Your Groq key (no setup needed)
The agent **reads your key automatically** from a file named `API KEY GROK.txt` placed next to
`research_agent.py`, on a line that starts with `gsk_`. The file is git-ignored, so it is never
committed. (Free key: <https://console.groq.com/keys>.)

> *(Optional)* You can instead set an environment variable, which overrides the file:
> `$env:GROQ_API_KEY = "gsk_..."`.

### 3. Run it
```powershell
cd D:\claude
python research_agent.py
```
It prints **"Groq key: found (ready to run)"** and opens **http://localhost:8000**. Ask a
question, watch the live loader, rate the answer, and ask follow-ups. Press **Ctrl+C** to stop.

---

## Using the app
- **Ask** anything in the search box; a live loader shows it *searching* and *reading sources*.
- **Answer** appears with a **Sources** list, rendered in clean markdown.
- **Feedback:** rate each answer with the emoji row (😍 😊 😐 😕 😞) — saved to `feedback.jsonl`.
- **Follow-ups:** the input bar stays at the bottom; follow-up questions keep the conversation
  context (so "how is *it* different?" knows what "it" is).

## Configuration (optional env vars)
| Variable | Default | Purpose |
|----------|---------|---------|
| `GROQ_API_KEY` | _(from key file)_ | Your Groq key. **Required on a server** (no key file there). |
| `APP_PASSWORD` | _(unset)_ | When set, the whole site requires this password (Basic auth). |
| `APP_USER` | `user` | Username that goes with `APP_PASSWORD`. |
| `PORT` | _(unset)_ | Set by cloud hosts → app binds publicly (`0.0.0.0`) and skips the browser pop. |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Which Groq model is the brain. |
| `AGENT_MAX_STEPS` | `6` | Max think/act loops before it must answer. |
| `AGENT_PORT` | `8000` | Local dashboard port. |
| `TAVILY_API_KEY` | _(unset)_ | Use Tavily search instead of DuckDuckGo. |

---

## 🚀 Deploy it live

The app is cloud-ready: when a host sets `PORT` it binds publicly, and `APP_PASSWORD` turns on a
login gate. A `Dockerfile` (portable) and `render.yaml` (Render) are included.

### Hugging Face Spaces (free, no credit card) — current live host
1. Create a Space at <https://huggingface.co/new-space> → **SDK: Docker → Blank**, CPU basic.
2. Upload **`research_agent.py`** and **`Dockerfile`** to the Space (root level).
3. **Settings → Variables and secrets → New secret:** add `GROQ_API_KEY` and `APP_PASSWORD`.
4. It builds and runs on port 7860. Open the direct `*.hf.space` URL → log in.

> Changing a secret only takes effect after a **Factory rebuild / restart** of the Space.
> Use the **direct** `*.hf.space` URL to log in — the browser blocks the password box inside the
> embedded `huggingface.co/spaces/...` frame.

### Render (alternative)
New + → **Blueprint** → pick this repo (it reads `render.yaml`) → set `GROQ_API_KEY` and
`APP_PASSWORD` as secrets → deploy. (Free tier may ask to verify a card.)

### Security & limits when public
- Keep `APP_PASSWORD` set so the site stays gated.
- Every search **spends your Groq quota** — don't share the password widely.
- Only **one research runs at a time** globally (a simple lock) — fine for a personal/demo site.

---

## How it works (1 paragraph)
`research_agent.py` runs a tiny standard-library web server (`ThreadingHTTPServer`). The dashboard
opens a **Server-Sent Events** stream (`/events`). When you submit a question, the agent loop asks
Groq for a JSON decision (`search` / `read` / `finish`), executes that tool, feeds the result back,
and **publishes every step** to the dashboard, which renders them as a live search loader and a
final cited answer. Follow-ups resend prior Q&A as context. No database, no framework, no packages.
