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

## 2. Your Groq key (no setup needed)
The agent **reads your key automatically** from a file named `API KEY GROK.txt` — placed either
next to `research_agent.py` or one folder up in `D:\claude`. The file just needs a line that
starts with `gsk_`. That's it; nothing to configure. (Free key: <https://console.groq.com/keys>.)

> This key file is git-ignored, so it is never committed or pushed.
>
> *(Advanced/optional)* You can instead provide the key via an environment variable, which
> overrides the file: `$env:GROQ_API_KEY = "gsk_..."`. For better search you can also set
> `$env:TAVILY_API_KEY = "tvly_..."`.

## 3. Run it
```powershell
cd D:\claude\research-agent
python research_agent.py
```
On start it prints **"Groq key: found (ready to run)."** and your browser opens to
**http://localhost:8000**. Type a question, hit **Research**, and watch it work. Press **Ctrl+C**
in the terminal to stop.

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
| `GROQ_API_KEY` | _(from key file)_ | Your Groq key. Required on a server (no key file there). |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Which Groq model is the brain. |
| `AGENT_MAX_STEPS` | `8` | Max think/act loops before it must answer. |
| `AGENT_PORT` | `8000` | Local dashboard port. |
| `PORT` | _(unset)_ | Set by cloud hosts → triggers public bind (`0.0.0.0`) + no browser auto-open. |
| `APP_PASSWORD` | _(unset)_ | When set, the whole site requires this password (Basic auth). |
| `APP_USER` | `user` | Username that goes with `APP_PASSWORD`. |
| `TAVILY_API_KEY` | _(unset)_ | Use Tavily search instead of DuckDuckGo. |

## How it works (1 paragraph)
`research_agent.py` runs a tiny standard-library web server (`ThreadingHTTPServer`). The
dashboard opens a **Server-Sent Events** stream (`/events`). When you submit a question, the
agent loop asks Groq for a JSON decision (`search` / `read` / `finish`), executes that tool,
feeds the result back, and **publishes every step** to the dashboard, which renders them as a
live search loader and a final cited answer. Follow-up questions are sent with prior Q&A as
context. No database, no framework, no external packages.

---

# 🚀 Deploy it live (public URL)

GitHub stores the code; a **Python host runs it**. The recommended path is **Render**, which
links to your GitHub repo and **redeploys on every push**. A `render.yaml` and a `Dockerfile`
are already included.

### Step 1 — Put the repo on GitHub (one time)
From `D:\claude`:
```powershell
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git   # create the empty repo on github.com first
git push -u origin main
```

### Step 2 — Deploy on Render
1. Go to <https://render.com> → sign in **with GitHub**.
2. **New + → Blueprint** → pick this repo. Render reads `render.yaml` automatically.
3. When prompted, set these as **secret** environment variables:
   - `GROQ_API_KEY` = your `gsk_…` key
   - `APP_PASSWORD` = a password you choose (this is what protects the site)
4. Click **Apply / Deploy**. After a few minutes you get a public URL like
   `https://research-agent-xxxx.onrender.com`.
5. Open it → the browser asks for a login. Username **`user`**, password = your `APP_PASSWORD`.

> **Free-tier note:** the service sleeps after ~15 min idle, so the first visit after a nap
> takes ~30–60s to wake. Fine for a demo. Render may ask to verify a card (no charge on free).

### Alternative — Hugging Face Spaces (no credit card)
The included `Dockerfile` makes this work anywhere Docker runs. On
<https://huggingface.co/spaces> → **Create Space → Docker (blank)**, push these files, and add
`GROQ_API_KEY` + `APP_PASSWORD` as **Secrets** in the Space settings. It listens on port 7860
(already set in the Dockerfile).

### Security & limits when public
- **Password gate** (Basic auth) is on whenever `APP_PASSWORD` is set — keep it set in the cloud.
- Every search **spends your Groq quota**, so don't share the password widely.
- Only **one research runs at a time** globally (a simple lock) — fine for a personal/demo site,
  not for many simultaneous users.
