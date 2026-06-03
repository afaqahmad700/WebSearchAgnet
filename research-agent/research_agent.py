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
import queue
import threading
import webbrowser
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser

# ============================== CONFIG ==============================
PORT          = int(os.environ.get("AGENT_PORT", "8000"))
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_STEPS     = int(os.environ.get("AGENT_MAX_STEPS", "8"))   # safety cap on the think/act loop
SEARCH_RESULTS = 5         # results pulled per search
PAGE_CHARS    = 3500       # max characters of a page handed to the model
HEARTBEAT_SEC = 15         # SSE keep-alive ping interval


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


# ============================== THE BRAIN (Groq) ==============================
def groq_chat(messages, temperature=0.3, force_json=True):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys")
    body = {"model": GROQ_MODEL, "temperature": temperature, "messages": messages}
    if force_json:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


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


def run_agent(question):
    """Run the think/act loop for one question, streaming every step live."""
    publish("run_started", "Research started", question)
    sources_seen = []   # urls actually read
    history = []        # textual transcript fed back to the model

    for step in range(1, MAX_STEPS + 1):
        # Build the conversation for this turn.
        convo = (
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
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Research Agent</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --muted:#94a3b8; --line:#334155; --text:#e2e8f0; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: Segoe UI, system-ui, Arial, sans-serif; background:var(--bg); color:var(--text); }
  header { padding:18px 24px; border-bottom:1px solid var(--line); background:#0b1220; }
  h1 { margin:0; font-size:18px; }
  .sub { color:var(--muted); font-size:13px; margin-top:4px; }
  .wrap { max-width:960px; margin:0 auto; padding:20px 24px 80px; }
  form { display:flex; gap:10px; margin:18px 0; }
  input[type=text] { flex:1; padding:12px 14px; border-radius:10px; border:1px solid var(--line);
                     background:var(--panel); color:var(--text); font-size:15px; }
  button { padding:12px 20px; border:0; border-radius:10px; background:#2563eb; color:#fff;
           font-size:15px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .card { border:1px solid var(--line); background:var(--panel); border-radius:12px; padding:12px 14px;
          margin:10px 0; animation: pop .18s ease-out; }
  @keyframes pop { from { opacity:0; transform: translateY(6px);} to {opacity:1; transform:none;} }
  .card .label { font-size:12px; text-transform:uppercase; letter-spacing:.04em; font-weight:700; margin-bottom:6px; }
  .card .body { white-space:pre-wrap; word-break:break-word; font-size:14px; line-height:1.5; color:#cbd5e1; }
  .thought  { border-left:4px solid #64748b; }
  .action   { border-left:4px solid #3b82f6; }
  .observation { border-left:4px solid #a855f7; }
  .answer   { border-left:4px solid #22c55e; background:#0c2a1a; }
  .answer .body { color:#e6ffe9; }
  .error, .note { border-left:4px solid #ef4444; }
  .run_started { border-left:4px solid #eab308; }
  a { color:#7dd3fc; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e; margin-right:6px;
         vertical-align:middle; }
  .live { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>Live Web Research Agent</h1>
  <div class="sub">Ask a question — watch it search, read, and reason in real time.</div>
</header>
<div class="wrap">
  <form id="f">
    <input id="q" type="text" placeholder="e.g. What are the main differences between Groq and OpenAI for developers?" autofocus>
    <button id="go" type="submit">Research</button>
  </form>
  <div class="live" id="status">Idle. <span id="conn"></span></div>
  <div id="feed"></div>
</div>
<script>
  const feed = document.getElementById('feed');
  const statusEl = document.getElementById('status');
  const connEl = document.getElementById('conn');
  const go = document.getElementById('go');

  function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function linkify(s){ return esc(s).replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank">$1</a>'); }

  function addCard(ev){
    if (ev.type === 'run_started') { feed.innerHTML=''; statusEl.textContent='Running…'; go.disabled=true; }
    if (ev.type === 'done')        { statusEl.textContent='Finished. ' + esc(ev.content||''); go.disabled=false; }
    if (ev.type === 'heartbeat')   return;

    const card = document.createElement('div');
    card.className = 'card ' + ev.type;
    const label = document.createElement('div'); label.className='label';
    label.textContent = ev.title || ev.type;
    const body = document.createElement('div'); body.className='body';
    body.innerHTML = linkify(ev.content || '');
    card.appendChild(label); card.appendChild(body);
    feed.appendChild(card);
    window.scrollTo(0, document.body.scrollHeight);
  }

  const es = new EventSource('/events');
  es.onopen = () => { connEl.textContent = '● connected'; };
  es.onerror = () => { connEl.textContent = '● reconnecting…'; };
  es.onmessage = (e) => { try { addCard(JSON.parse(e.data)); } catch(_){} };

  document.getElementById('f').addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = document.getElementById('q').value.trim();
    if (!q) return;
    feed.innerHTML=''; statusEl.textContent='Starting…'; go.disabled=true;
    await fetch('/run', { method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify({question:q}) });
  });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet console

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
        elif self.path == "/events":
            self._stream_events()
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/run":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                question = (json.loads(raw).get("question") or "").strip()
            except Exception:
                question = ""
            if not question:
                self._send(400, "application/json", b'{"error":"missing question"}')
                return
            self._send(200, "application/json", b'{"ok":true}')
            threading.Thread(target=self._guarded_run, args=(question,), daemon=True).start()
        else:
            self._send(404, "text/plain", b"not found")

    def _guarded_run(self, question):
        if not _run_lock.acquire(blocking=False):
            publish("note", "Busy", "A research run is already in progress; please wait.")
            return
        try:
            run_agent(question)
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
    if not os.environ.get("GROQ_API_KEY"):
        print("\n[!] GROQ_API_KEY is not set. The dashboard will load but runs will fail.")
        print("    PowerShell:  $env:GROQ_API_KEY=\"your_key\"")
        print("    CMD:         set GROQ_API_KEY=your_key\n")
    url = f"http://localhost:{PORT}"
    print(f"Live Research Agent running at {url}")
    print("Open that URL in your browser (it should open automatically). Ctrl+C to stop.")
    try:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
