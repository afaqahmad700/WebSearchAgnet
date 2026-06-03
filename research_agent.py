#!/usr/bin/env python3
"""
Live Web Research Agent
=======================
A self-contained AI research agent you can WATCH work in real time.

You give it a question in the browser. It then loops:
    think  ->  search the web  ->  read pages  ->  think  ->  ... ->  final cited answer
Every single step is streamed live to a dashboard at http://localhost:8000.

Design goals:
  * ZERO third-party packages  -> only Python's standard library. Just install Python.
  * The "brain" is Groq (free, fast).            Set GROQ_API_KEY.
  * Web search uses Tavily if TAVILY_API_KEY is set (cleaner, more reliable),
    otherwise falls back to scraping DuckDuckGo's HTML (no key, but flakier).

Run:
    set GROQ_API_KEY=your_key      (Windows CMD)     or
    $env:GROQ_API_KEY="your_key"   (PowerShell)
    python research_agent.py
Then your browser opens automatically to the live dashboard.
"""

import os
import re
import json
import html
import time
import base64
import queue
import threading
import webbrowser
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser

# ============================== CONFIG ==============================
# Cloud hosts (Render, Railway, HF Spaces, etc.) inject a $PORT and expect the app to bind
# 0.0.0.0. Locally we fall back to 127.0.0.1:8000 and auto-open a browser.
DEPLOYED      = bool(os.environ.get("PORT"))
PORT          = int(os.environ.get("PORT") or os.environ.get("AGENT_PORT", "8000"))
HOST          = "0.0.0.0" if DEPLOYED else "127.0.0.1"
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_STEPS     = int(os.environ.get("AGENT_MAX_STEPS", "6"))   # safety cap on the think/act loop
SEARCH_RESULTS = 5         # results pulled per search
PAGE_CHARS    = 3000       # max characters of a page handed to the model
HEARTBEAT_SEC = 15         # SSE keep-alive ping interval

# Optional password gate. When APP_PASSWORD is set (e.g. on the host), every request needs
# HTTP Basic auth. Left unset locally, so local use stays friction-free.
AUTH_USER     = os.environ.get("APP_USER", "user")
AUTH_PASSWORD = os.environ.get("APP_PASSWORD", "")
FEEDBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.jsonl")


# ============================== LIVE EVENT BUS ==============================
# Every connected dashboard gets its own queue; the agent publishes to all of them.
_subscribers = []
_subs_lock = threading.Lock()
_run_lock = threading.Lock()    # only one research run at a time


def publish(ev_type, title="", content="", step=None):
    """Push one live event to every connected dashboard."""
    payload = json.dumps({"type": ev_type, "title": title, "content": content, "step": step})
    with _subs_lock:
        for q in list(_subscribers):
            q.put(payload)


_feedback_lock = threading.Lock()


def record_feedback(entry):
    """Append a feedback reaction to feedback.jsonl for later analytics."""
    if not isinstance(entry, dict):
        return
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rating": str(entry.get("rating", ""))[:40],
        "question": str(entry.get("question", ""))[:500],
        "answer_preview": str(entry.get("answer_preview", ""))[:300],
    }
    try:
        with _feedback_lock:
            with open(FEEDBACK_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ============================== THE BRAIN (Groq) ==============================
def load_api_key():
    """Find the Groq key. An environment variable wins; otherwise read it from a local
    key file so you never have to set anything by hand. Returns the key or None.
    Always stripped of surrounding whitespace/newlines (a stray '\\n' breaks the auth header)."""
    env_key = os.environ.get("GROQ_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "API KEY GROK.txt"),                 # next to this script
        os.path.join(os.path.dirname(here), "API KEY GROK.txt"),  # one folder up (D:\claude)
        os.path.join(here, "groq_key.txt"),
        os.path.join(here, ".groq_key"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    token = line.strip()
                    if token.startswith("gsk_"):
                        os.environ["GROQ_API_KEY"] = token
                        return token
        except OSError:
            continue
    return None


def groq_chat(messages, temperature=0.3, force_json=True):
    key = load_api_key()
    if not key:
        raise RuntimeError("No Groq key found. Put it in 'API KEY GROK.txt' (a line starting "
                           "with gsk_) or set the GROQ_API_KEY environment variable. "
                           "Free key: https://console.groq.com/keys")
    body = {"model": GROQ_MODEL, "temperature": temperature, "messages": messages}
    if force_json:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            # A browser-like User-Agent is required: Groq sits behind Cloudflare, which
            # blocks the default "Python-urllib" agent with a 403 (Cloudflare error 1010).
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        },
        method="POST",
    )

    last_msg = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last_msg = _groq_error_message(e.code, body)
            # 429 = rate limited, 503 = overloaded -> wait and retry with backoff.
            if e.code in (429, 503) and attempt < 3:
                retry_after = (e.headers.get("Retry-After") or "").strip()
                wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else (2 ** attempt) * 4
                wait = min(wait, 30)
                publish("note", "Rate limited — waiting",
                        f"Groq is busy ({e.code}); retrying in {int(wait)}s…")
                time.sleep(wait)
                continue
            raise RuntimeError(last_msg)
    raise RuntimeError(last_msg or "Groq request failed after several retries.")


def _groq_error_message(code, body):
    """Turn a Groq error body into a short, human-friendly message."""
    msg = body
    try:
        msg = json.loads(body).get("error", {}).get("message", body)
    except Exception:
        pass
    msg = (msg or "").strip()
    if code == 429:
        return "Groq rate limit reached (free tier). " + (msg or "Please wait a minute and try again.")
    return f"Groq error {code}: {msg[:300]}"


# ============================== TOOLS ==============================
class _TextExtractor(HTMLParser):
    """Strip a web page down to readable text (skips script/style/nav noise)."""
    _SKIP = {"script", "style", "noscript", "head", "svg", "form"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.parts.append(text)

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", " ".join(self.parts))


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ResearchAgent/1.0",
        "Accept": "text/html,application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def web_search(query):
    """Return a list of {title, url, snippet}. Tavily if available, else DuckDuckGo HTML."""
    tav = os.environ.get("TAVILY_API_KEY")
    if tav:
        try:
            return _tavily_search(query, tav)
        except Exception as e:
            publish("note", "Tavily failed, falling back to DuckDuckGo", str(e))
    return _ddg_search(query)


def _tavily_search(query, key):
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({
            "api_key": key, "query": query,
            "max_results": SEARCH_RESULTS, "include_answer": False,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for r in data.get("results", [])[:SEARCH_RESULTS]:
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content", "") or "")[:300],
        })
    return out


def _ddg_search(query):
    # The lite endpoint returns clean, parseable HTML on a plain GET. (The html.duckduckgo.com
    # endpoint now serves a bot-detection page on GET.) Result links are anchors whose href
    # carries a "uddg=" redirect param, regardless of quote style or class name.
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    try:
        page = _http_get(url, timeout=20)
    except Exception as e:
        return [{"title": "(search error)", "url": "", "snippet": str(e)}]

    results = []
    for m in re.finditer(r'<a\b[^>]*href="([^"]*uddg=[^"]*)"[^>]*>(.*?)</a>', page, re.S):
        href = html.unescape(m.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        real = _ddg_unwrap(href)
        if real and title:
            results.append({"title": title, "url": real, "snippet": ""})
        if len(results) >= SEARCH_RESULTS:
            break

    # Best-effort snippets (cells classed "result-snippet")
    snippets = [html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
                for s in re.findall(r"result-snippet[^>]*>(.*?)</td>", page, re.S)]
    for i, s in enumerate(snippets[:len(results)]):
        results[i]["snippet"] = s[:300]
    return results or [{"title": "(no results)", "url": "", "snippet": "DuckDuckGo returned nothing."}]


def _ddg_unwrap(href):
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs:
        return urllib.parse.unquote(qs["uddg"][0])
    return href if href.startswith("http") else ""


def read_url(url):
    """Fetch a page and return readable text (truncated)."""
    try:
        raw = _http_get(url, timeout=20)
    except Exception as e:
        return f"(could not open {url}: {e})"
    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        pass
    text = parser.text().strip()
    return text[:PAGE_CHARS] if text else "(page had no readable text)"


# ============================== THE AGENT LOOP ==============================
SYSTEM_PROMPT = """You are a meticulous web research agent.
You answer the user's question by searching the web and reading pages, then writing a well-sourced answer.

On EACH turn you reply with ONLY a JSON object of this exact shape:
{
  "thought": "<short reasoning about what to do next>",
  "action": "search" | "read" | "finish",
  "query": "<a web search query>      (only when action is 'search')",
  "url": "<a URL from earlier results> (only when action is 'read')",
  "answer": "<final answer in markdown, with a 'Sources' list of the URLs you used> (only when action is 'finish')"
}

Rules:
- Start by searching. Then READ the most promising results before answering.
- Read at least two different sources before you finish, unless the answer is trivial.
- Do NOT invent facts or URLs. Only cite URLs you actually read.
- When confident, action='finish' with a clear, structured markdown answer and a Sources list.
- Keep thoughts to one or two sentences."""


def run_agent(question, prior=None):
    """Run the think/act loop for one question, streaming every step live.

    `prior` is an optional list of earlier {"q":..., "a":...} turns in the same
    conversation, so follow-up questions keep context.
    """
    publish("run_started", "Research started", question)
    sources_seen = []   # urls actually read
    history = []        # textual transcript fed back to the model

    # Build a context preamble from earlier turns (for conversational follow-ups).
    convo_prefix = ""
    if prior:
        parts = []
        for turn in prior[-6:]:               # cap how much history we resend
            q = (turn.get("q") or "").strip()
            a = (turn.get("a") or "").strip()
            if q and a:
                parts.append("User asked: " + q + "\nYou answered: " + a[:900])
        if parts:
            convo_prefix = (
                "EARLIER IN THIS CONVERSATION (context for the new question, which may refer "
                "back to it with words like 'it', 'that', 'they'):\n" + "\n\n".join(parts) + "\n\n"
            )

    for step in range(1, MAX_STEPS + 1):
        # Build the conversation for this turn.
        convo = (
            convo_prefix +
            f"QUESTION: {question}\n\n"
            "TRANSCRIPT SO FAR:\n" + ("\n".join(history) if history else "(nothing yet)") +
            f"\n\nYou are on step {step} of {MAX_STEPS}. "
            "If you are running out of steps, finish with the best answer you have."
        )
        try:
            raw = groq_chat([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": convo},
            ])
            decision = _parse_decision(raw)
        except Exception as e:
            publish("error", "Model error", str(e), step)
            return

        thought = decision.get("thought", "").strip()
        action = (decision.get("action") or "").strip().lower()
        if thought:
            publish("thought", f"Step {step}: thinking", thought, step)

        if action == "search":
            q = decision.get("query", "").strip()
            publish("action", f"Step {step}: searching the web", q, step)
            results = web_search(q)
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
            obs = "\n".join(lines)
            publish("observation", f"{len(results)} result(s)", obs, step)
            history.append(f"[step {step}] SEARCH: {q}\nRESULTS:\n{obs}")

        elif action == "read":
            url = decision.get("url", "").strip()
            publish("action", f"Step {step}: reading page", url, step)
            text = read_url(url)
            if url.startswith("http") and url not in sources_seen and not text.startswith("("):
                sources_seen.append(url)
            preview = text[:600] + ("..." if len(text) > 600 else "")
            publish("observation", f"Read {url}", preview, step)
            history.append(f"[step {step}] READ {url}\nCONTENT:\n{text}")

        elif action == "finish":
            answer = decision.get("answer", "").strip() or "(the agent finished without an answer)"
            if sources_seen and "Sources" not in answer:
                answer += "\n\n**Sources**\n" + "\n".join(f"- {u}" for u in sources_seen)
            publish("answer", "Final answer", answer, step)
            publish("done", "Done", f"Finished in {step} step(s).")
            return

        else:
            publish("note", f"Step {step}: unrecognized action", raw, step)
            history.append(f"[step {step}] (model returned an unusable action; please pick search/read/finish)")

    publish("note", "Reached the step limit", "The agent stopped after the maximum number of steps.")
    publish("done", "Done", "Stopped at step limit.")


def _parse_decision(raw):
    """Parse the model's JSON, tolerating stray text around it."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError("Model did not return JSON: " + raw[:200])


# ============================== WEB SERVER + DASHBOARD ==============================
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Research</title>
<style>
  :root{
    --bg:#ffffff; --fg:#1f2328; --muted:#6b7280; --faint:#9aa1ab;
    --line:#ececf0; --soft:#f7f8fa; --soft2:#f1f2f5;
    --accent:#111418; --link:#2563eb;
    --shadow-sm:0 1px 2px rgba(16,24,40,.05);
    --shadow-md:0 6px 24px rgba(16,24,40,.08);
    --radius:16px;
    --maxw:740px;
    --ease:cubic-bezier(.22,.61,.36,1);
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; }
  body{
    margin:0; background:var(--bg); color:var(--fg);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,"Apple Color Emoji","Segoe UI Emoji",sans-serif;
    font-size:16px; line-height:1.6; -webkit-font-smoothing:antialiased;
  }
  a{ color:var(--link); text-decoration:none; } a:hover{ text-decoration:underline; }

  .page{ min-height:100%; display:flex; flex-direction:column; }
  .topbar{
    position:sticky; top:0; z-index:20; backdrop-filter:saturate(180%) blur(8px);
    background:rgba(255,255,255,.8); border-bottom:1px solid var(--line);
    display:flex; align-items:center; justify-content:space-between;
    padding:12px clamp(16px,4vw,28px);
  }
  .brand{ display:flex; align-items:center; gap:9px; font-weight:650; letter-spacing:-.01em; font-size:15px; }
  .brand-dot{ width:9px; height:9px; border-radius:50%; background:var(--accent);
              box-shadow:0 0 0 3px rgba(17,20,24,.08); animation:breathe 3s var(--ease) infinite; }
  @keyframes breathe{ 0%,100%{ transform:scale(1); opacity:.9 } 50%{ transform:scale(1.25); opacity:1 } }
  .conn{ font-size:12px; color:var(--faint); display:flex; align-items:center; gap:6px; }
  .conn::before{ content:""; width:7px; height:7px; border-radius:50%; background:#cbd2da; }
  .conn.ok::before{ background:#22c55e; }

  /* ---------- HERO (empty state) ---------- */
  .hero{ flex:1; display:none; flex-direction:column; align-items:center; justify-content:center;
         text-align:center; padding:8vh clamp(16px,5vw,24px) 12vh; gap:14px; animation:fade .5s var(--ease); }
  body.empty .hero{ display:flex; }
  body.empty .composer{ display:none; }
  .hero-title{ font-size:clamp(26px,4.5vw,38px); font-weight:680; letter-spacing:-.02em; margin:0; }
  .hero-sub{ color:var(--muted); margin:0 0 10px; max-width:520px; }
  .hero .searchbar{ width:min(620px,92vw); }
  .suggestions{ display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-top:6px; }
  .chip{ border:1px solid var(--line); background:var(--soft); color:var(--fg);
         padding:8px 14px; border-radius:999px; font-size:13.5px; cursor:pointer;
         transition:all .18s var(--ease); }
  .chip:hover{ background:#fff; border-color:#dfe2e8; box-shadow:var(--shadow-sm); transform:translateY(-1px); }

  /* ---------- SEARCH BAR ---------- */
  .searchbar{ display:flex; align-items:center; gap:8px; background:#fff;
              border:1px solid var(--line); border-radius:999px; padding:7px 8px 7px 18px;
              box-shadow:var(--shadow-sm); transition:box-shadow .2s var(--ease), border-color .2s var(--ease); }
  .searchbar:focus-within{ border-color:#d6dae1; box-shadow:var(--shadow-md); }
  .search-input{ flex:1; border:0; outline:0; background:transparent; font:inherit; color:var(--fg);
                 padding:8px 0; min-width:0; }
  .search-input::placeholder{ color:var(--faint); }
  .send-btn{ flex:none; width:40px; height:40px; border:0; border-radius:50%; cursor:pointer;
             background:var(--accent); color:#fff; display:grid; place-items:center;
             transition:transform .15s var(--ease), opacity .2s var(--ease), background .2s; }
  .send-btn:hover{ transform:scale(1.06); }
  .send-btn:active{ transform:scale(.94); }
  .send-btn:disabled{ opacity:.35; cursor:not-allowed; transform:none; }
  .send-btn svg{ width:18px; height:18px; }

  /* ---------- THREAD ---------- */
  .thread{ width:100%; max-width:var(--maxw); margin:0 auto; padding:22px clamp(16px,4vw,24px) 160px; flex:1; }
  body.empty .thread{ display:none; }
  .turn{ animation:rise .45s var(--ease) both; margin-bottom:30px; }
  @keyframes rise{ from{ opacity:0; transform:translateY(10px) } to{ opacity:1; transform:none } }
  @keyframes fade{ from{ opacity:0 } to{ opacity:1 } }

  .q-bubble{ display:inline-block; background:var(--soft2); color:var(--fg);
             padding:11px 16px; border-radius:16px 16px 4px 16px; font-weight:560;
             font-size:16.5px; letter-spacing:-.01em; max-width:100%; }
  .a-row{ display:flex; gap:14px; margin-top:18px; }
  .a-avatar{ flex:none; width:26px; height:26px; border-radius:50%; margin-top:2px;
             background:linear-gradient(135deg,#111,#3a3f47); display:grid; place-items:center; }
  .a-avatar i{ width:8px; height:8px; border-radius:50%; background:#fff; display:block; }
  .a-content{ flex:1; min-width:0; }

  /* ---------- LOADER (search experience) ---------- */
  .loader{ animation:fade .3s var(--ease); }
  .loader-status{ display:flex; align-items:center; gap:10px; color:var(--fg); font-weight:560; }
  .spinner{ width:16px; height:16px; border-radius:50%; border:2px solid #e6e8ec; border-top-color:var(--accent);
            animation:spin .7s linear infinite; flex:none; }
  @keyframes spin{ to{ transform:rotate(360deg) } }
  .status-text{ background:linear-gradient(90deg,#9aa1ab 25%,#1f2328 50%,#9aa1ab 75%);
                background-size:200% 100%; -webkit-background-clip:text; background-clip:text; color:transparent;
                animation:shimmerText 2s linear infinite; }
  @keyframes shimmerText{ to{ background-position:-200% 0 } }
  .status-sub{ color:var(--faint); font-size:13.5px; margin:6px 0 0 26px; min-height:1em;
               white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .skeleton{ margin:16px 0 0; display:flex; flex-direction:column; gap:11px; }
  .sk-line{ height:13px; border-radius:7px; background:linear-gradient(90deg,#f0f1f4 25%,#e7e9ed 37%,#f0f1f4 63%);
            background-size:400% 100%; animation:shimmer 1.4s ease infinite; }
  .sk-line:nth-child(1){ width:96%; } .sk-line:nth-child(2){ width:88%; }
  .sk-line:nth-child(3){ width:92%; } .sk-line:nth-child(4){ width:60%; }
  @keyframes shimmer{ 0%{ background-position:100% 0 } 100%{ background-position:0 0 } }

  /* ---------- ANSWER (markdown) ---------- */
  .answer{ animation:rise .4s var(--ease) both; }
  .answer h1,.answer h2,.answer h3{ letter-spacing:-.01em; line-height:1.3; margin:18px 0 8px; }
  .answer h1{ font-size:22px; } .answer h2{ font-size:19px; } .answer h3{ font-size:17px; }
  .answer p{ margin:10px 0; }
  .answer ul{ margin:10px 0; padding-left:22px; } .answer li{ margin:6px 0; }
  .answer strong{ font-weight:650; }
  .answer code{ background:var(--soft2); padding:2px 6px; border-radius:6px; font-size:.9em;
                font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .answer a{ border-bottom:1px solid transparent; } .answer a:hover{ border-color:var(--link); text-decoration:none; }

  .err{ color:#b42318; background:#fff4f3; border:1px solid #fee0de; padding:12px 14px; border-radius:12px; }

  /* ---------- FEEDBACK ---------- */
  .feedback{ margin-top:22px; padding-top:18px; border-top:1px solid var(--line); animation:fade .5s var(--ease) .1s both; }
  .fb-prompt{ font-size:14px; color:var(--muted); margin-bottom:12px; }
  .fb-row{ display:flex; flex-wrap:wrap; gap:8px; }
  .fb-btn{ display:flex; align-items:center; gap:8px; background:var(--soft); border:1px solid var(--line);
           border-radius:999px; padding:8px 14px 8px 12px; cursor:pointer; font:inherit; font-size:13.5px; color:var(--fg);
           transition:transform .16s var(--ease), box-shadow .2s var(--ease), background .2s, border-color .2s; }
  .fb-emoji{ font-size:19px; line-height:1; display:inline-block; transition:transform .2s var(--ease); }
  .fb-btn:hover{ background:#fff; border-color:#dfe2e8; box-shadow:var(--shadow-sm); transform:translateY(-2px); }
  .fb-btn:hover .fb-emoji{ transform:scale(1.35) rotate(-6deg); animation:bounce .6s var(--ease); }
  @keyframes bounce{ 0%,100%{ transform:scale(1.35) translateY(0) } 40%{ transform:scale(1.4) translateY(-4px) } }
  .fb-btn:active{ transform:scale(.95); }
  .fb-btn.chosen{ background:#fff; border-color:var(--accent); box-shadow:0 0 0 3px rgba(17,20,24,.06); }
  .fb-btn.chosen .fb-emoji{ animation:pulse .5s var(--ease); }
  @keyframes pulse{ 0%{ transform:scale(1) } 50%{ transform:scale(1.6) } 100%{ transform:scale(1.2) } }
  .fb-btn.dim{ opacity:.35; }
  .fb-thanks{ display:flex; align-items:center; gap:8px; color:var(--fg); font-size:14.5px; font-weight:560;
              animation:thanks .5s var(--ease) both; }
  .fb-thanks .big{ font-size:20px; animation:bounce2 .8s var(--ease); }
  @keyframes thanks{ from{ opacity:0; transform:scale(.96) } to{ opacity:1; transform:none } }
  @keyframes bounce2{ 0%{ transform:scale(0) } 60%{ transform:scale(1.3) } 100%{ transform:scale(1) } }

  /* ---------- COMPOSER (sticky) ---------- */
  .composer{ position:fixed; left:0; right:0; bottom:0; z-index:15;
             background:linear-gradient(180deg,rgba(255,255,255,0),#fff 22%);
             padding:14px clamp(16px,4vw,24px) 16px; animation:rise .4s var(--ease); }
  .composer-inner{ max-width:var(--maxw); margin:0 auto; }
  .composer .searchbar{ width:100%; }
  .composer-hint{ text-align:center; color:var(--faint); font-size:11.5px; margin-top:8px; }

  @media (max-width:640px){
    body{ font-size:15.5px; }
    .thread{ padding-bottom:150px; }
    .a-row{ gap:10px; }
    .fb-label{ display:none; }
    .fb-btn{ padding:9px 12px; }
    .fb-emoji{ font-size:21px; }
  }
  @media (prefers-reduced-motion:reduce){
    *{ animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.01ms !important; }
  }
</style>
</head>
<body class="empty">
  <div class="page">
    <div class="topbar">
      <div class="brand"><span class="brand-dot"></span> Research</div>
      <div class="conn" id="conn">connecting</div>
    </div>

    <section class="hero" id="hero">
      <h1 class="hero-title">What do you want to know?</h1>
      <p class="hero-sub">I search the web, read the sources, and answer with citations — then you can keep asking follow-ups.</p>
      <form class="searchbar" id="heroForm" autocomplete="off">
        <input class="search-input" id="heroInput" type="text" placeholder="Ask anything…" autofocus>
        <button class="send-btn" id="heroSend" type="submit" aria-label="Search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
        </button>
      </form>
      <div class="suggestions" id="suggestions"></div>
    </section>

    <main class="thread" id="thread"></main>
  </div>

  <div class="composer" id="composer">
    <div class="composer-inner">
      <form class="searchbar" id="chatForm" autocomplete="off">
        <input class="search-input" id="chatInput" type="text" placeholder="Ask a follow-up…">
        <button class="send-btn" id="chatSend" type="submit" aria-label="Send">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
        </button>
      </form>
      <div class="composer-hint">Answers use live web search and can be imperfect — check important facts.</div>
    </div>
  </div>

<script>
  const $ = (id) => document.getElementById(id);
  const thread = $('thread'), connEl = $('conn');
  const heroForm = $('heroForm'), heroInput = $('heroInput');
  const chatForm = $('chatForm'), chatInput = $('chatInput'), chatSend = $('chatSend'), heroSend = $('heroSend');

  let busy = false;
  let current = null;            // { contentEl, statusTextEl, statusSubEl, question }
  const conversation = [];       // [{q, a}] sent as context for follow-ups

  const SUGGESTIONS = [
    "What is the Groq LPU and how is it different from a GPU?",
    "Best free AI APIs for developers in 2025",
    "How does Server-Sent Events work?"
  ];

  function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function domainOf(u){ try{ return new URL(u).hostname.replace(/^www\./,''); }catch(_){ return (u||'').slice(0,60); } }
  function scrollDown(){ window.scrollTo({ top:document.body.scrollHeight, behavior:'smooth' }); }

  // Minimal, safe markdown -> HTML
  function renderMarkdown(md){
    let s = esc(md);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    const lines = s.split('\n'); const out = []; let inList = false;
    for (let line of lines){
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      const li = line.match(/^\s*[-*]\s+(.*)$/);
      if (h){ if(inList){ out.push('</ul>'); inList=false; } const lvl=Math.min(h[1].length,3); out.push('<h'+lvl+'>'+h[2]+'</h'+lvl+'>'); continue; }
      if (li){ if(!inList){ out.push('<ul>'); inList=true; } out.push('<li>'+li[1]+'</li>'); continue; }
      if (line.trim()===''){ if(inList){ out.push('</ul>'); inList=false; } continue; }
      out.push('<p>'+line+'</p>');
    }
    if (inList) out.push('</ul>');
    return out.join('\n');
  }

  function addTurn(question){
    const turn = document.createElement('article'); turn.className = 'turn';
    const q = document.createElement('div'); q.innerHTML = '<div class="q-bubble">'+esc(question)+'</div>';
    const aRow = document.createElement('div'); aRow.className = 'a-row';
    aRow.innerHTML =
      '<div class="a-avatar"><i></i></div>' +
      '<div class="a-content">' +
        '<div class="loader">' +
          '<div class="loader-status"><span class="spinner"></span><span class="status-text">Searching the web…</span></div>' +
          '<div class="status-sub"></div>' +
          '<div class="skeleton"><span class="sk-line"></span><span class="sk-line"></span><span class="sk-line"></span><span class="sk-line"></span></div>' +
        '</div>' +
      '</div>';
    turn.appendChild(q); turn.appendChild(aRow);
    thread.appendChild(turn);
    const contentEl = aRow.querySelector('.a-content');
    return {
      contentEl,
      statusTextEl: contentEl.querySelector('.status-text'),
      statusSubEl: contentEl.querySelector('.status-sub'),
      question
    };
  }

  function setBusy(v){
    busy = v;
    [chatInput, chatSend, heroInput, heroSend].forEach(el => el.disabled = v);
  }

  function updateStatus(ev){
    if (!current) return;
    const t = ev.type, title = ev.title||'', c = ev.content||'';
    let s = null, sub = null;
    if (t === 'thought'){ s = 'Thinking'; }
    else if (t === 'action' && /search/i.test(title)){ s = 'Searching the web'; sub = c ? '“'+c+'”' : ''; }
    else if (t === 'action' && /read/i.test(title)){ s = 'Reading source'; sub = domainOf(c); }
    else if (t === 'observation' && /result/i.test(title)){ s = 'Reviewing results'; }
    else if (t === 'observation' && /read/i.test(title)){ s = 'Reading source'; }
    if (s){ current.statusTextEl.textContent = s + '…'; if (sub !== null) current.statusSubEl.textContent = sub; }
  }

  function addFeedback(contentEl, question, answer){
    const opts = [
      ['😍','Excellent'], ['😊','Helpful'], ['😐','Okay'], ['😕','Not really'], ['😞','No']
    ];
    const fb = document.createElement('div'); fb.className = 'feedback';
    let row = '<div class="fb-prompt">Did you find what you were looking for?</div><div class="fb-row">';
    for (const [emoji,label] of opts){
      row += '<button class="fb-btn" data-r="'+label+'"><span class="fb-emoji">'+emoji+'</span><span class="fb-label">'+label+'</span></button>';
    }
    row += '</div>';
    fb.innerHTML = row;
    contentEl.appendChild(fb);
    fb.querySelectorAll('.fb-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const rating = btn.getAttribute('data-r');
        const emoji = btn.querySelector('.fb-emoji').textContent;
        fb.querySelectorAll('.fb-btn').forEach(b => { b.classList.add('dim'); b.disabled = true; });
        btn.classList.remove('dim'); btn.classList.add('chosen');
        fetch('/feedback', { method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ question, rating, answer_preview:(answer||'').slice(0,200) }) }).catch(()=>{});
        setTimeout(() => {
          fb.innerHTML = '<div class="fb-thanks"><span class="big">'+emoji+'</span> Thanks for your feedback!</div>';
        }, 420);
      });
    });
  }

  function onAnswer(md){
    if (!current) return;
    const c = current.contentEl;
    c.innerHTML = '';
    const ans = document.createElement('div'); ans.className = 'answer'; ans.innerHTML = renderMarkdown(md);
    c.appendChild(ans);
    addFeedback(c, current.question, md);
    conversation.push({ q: current.question, a: md });
    current = null; setBusy(false); chatInput.focus(); scrollDown();
  }

  function onError(message){
    if (current){
      current.contentEl.innerHTML = '<div class="err">'+esc(message||'Something went wrong.')+'</div>';
      current = null;
    }
    setBusy(false);
  }

  async function ask(question){
    question = (question||'').trim();
    if (!question || busy) return;
    document.body.classList.remove('empty');
    setBusy(true);
    heroInput.value = ''; chatInput.value = '';
    current = addTurn(question);
    scrollDown();
    try{
      await fetch('/run', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ question, history: conversation }) });
    }catch(e){ onError('Network error: ' + e.message); }
  }

  heroForm.addEventListener('submit', e => { e.preventDefault(); ask(heroInput.value); });
  chatForm.addEventListener('submit', e => { e.preventDefault(); ask(chatInput.value); });

  // Suggestion chips
  const sg = $('suggestions');
  SUGGESTIONS.forEach(text => {
    const c = document.createElement('button'); c.className = 'chip'; c.textContent = text;
    c.addEventListener('click', () => ask(text));
    sg.appendChild(c);
  });

  // Live event stream
  const es = new EventSource('/events');
  es.onopen  = () => { connEl.textContent = 'connected'; connEl.classList.add('ok'); };
  es.onerror = () => { connEl.textContent = 'reconnecting'; connEl.classList.remove('ok'); };
  es.onmessage = (e) => {
    let ev; try{ ev = JSON.parse(e.data); }catch(_){ return; }
    switch (ev.type){
      case 'heartbeat': return;
      case 'run_started': break;
      case 'thought': case 'action': case 'observation': case 'note': updateStatus(ev); break;
      case 'answer': onAnswer(ev.content || ''); break;
      case 'error': onError(ev.content || 'The agent hit an error.'); break;
      case 'done': setBusy(false); break;
    }
  };
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet console

    def _authed(self):
        """If a password is configured, require HTTP Basic auth. Returns True if allowed."""
        if not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                if user == AUTH_USER and pw == AUTH_PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Research Agent"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        # Diagnostics (no secret values revealed) — exempt from auth so it's always checkable.
        if self.path == "/_diag":
            info = {
                "auth_enabled": bool(AUTH_PASSWORD),
                "app_password_set": bool(AUTH_PASSWORD),
                "app_user": AUTH_USER,
                "groq_key_set": bool(load_api_key()),
                "model": GROQ_MODEL,
            }
            self._send(200, "application/json", json.dumps(info).encode("utf-8"))
            return
        if not self._authed():
            return
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
        elif self.path == "/events":
            self._stream_events()
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._authed():
            return
        if self.path == "/run":
            payload = self._read_json()
            question = (payload.get("question") or "").strip()
            history = payload.get("history") or []
            if not isinstance(history, list):
                history = []
            if not question:
                self._send(400, "application/json", b'{"error":"missing question"}')
                return
            self._send(200, "application/json", b'{"ok":true}')
            threading.Thread(target=self._guarded_run, args=(question, history), daemon=True).start()
        elif self.path == "/feedback":
            payload = self._read_json()
            record_feedback(payload)
            self._send(200, "application/json", b'{"ok":true}')
        else:
            self._send(404, "text/plain", b"not found")

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _guarded_run(self, question, history=None):
        if not _run_lock.acquire(blocking=False):
            publish("note", "Busy", "A research run is already in progress; please wait.")
            return
        try:
            run_agent(question, history)
        except Exception as e:
            publish("error", "Agent crashed", str(e))
        finally:
            _run_lock.release()

    def _stream_events(self):
        q = queue.Queue()
        with _subs_lock:
            _subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=HEARTBEAT_SEC)
                    self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b'data: {"type":"heartbeat"}\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    if load_api_key():
        print("Groq key: found (ready to run).")
    else:
        print("\n[!] No Groq key found. The dashboard will load but runs will fail.")
        print("    Easiest fix: put your key in a file named 'API KEY GROK.txt' next to this")
        print("    script (or in D:\\claude), on a line starting with gsk_ .")
        print("    Free key: https://console.groq.com/keys\n")
    if AUTH_PASSWORD:
        print(f"Password protection: ON (user '{AUTH_USER}').")
    if DEPLOYED:
        print(f"Research Agent serving on {HOST}:{PORT} (cloud mode).")
    else:
        url = f"http://localhost:{PORT}"
        print(f"Live Research Agent running at {url}")
        print("Your browser should open automatically. Ctrl+C to stop.")
        try:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
