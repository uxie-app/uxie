"""
MiniFlow Engine — FastAPI backend

HTTP:      POST http://localhost:8765/invoke/:command
           GET  http://localhost:8765/health
           GET  http://localhost:8765/callback        ← OAuth token receiver
WebSocket: ws://localhost:8765/ws
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

# ── SSL setup for PyInstaller-frozen builds ──────────────────────────────────
# When frozen, Python can't see the system keychain, so any HTTPS call fails
# with CERTIFICATE_VERIFY_FAILED. Point every SSL-using library at certifi's
# CA bundle BEFORE any of them get imported (requests/httpx/openai/websockets).
if getattr(sys, "frozen", False):
    try:
        import certifi
        _cert = certifi.where()
        os.environ["SSL_CERT_FILE"] = _cert
        os.environ["REQUESTS_CA_BUNDLE"] = _cert
        os.environ["CURL_CA_BUNDLE"] = _cert
    except Exception:
        pass

# tiktoken encodings live in a namespace package; force-import so cl100k_base registers.
try:
    import tiktoken_ext.openai_public  # noqa: F401
except Exception:
    pass

# Tell litellm to use its bundled (offline) cost map instead of fetching
# raw.githubusercontent.com on every request. That GET was adding
# 200–500 ms of latency to each command.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "WARNING")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# Sentry — DSN baked in for the packaged DMG (it's a public credential).
# Override-able via SENTRY_DSN env var for dev / staging. Wire BEFORE
# importing any other engine modules so unhandled errors during their
# import time are reported.
_SENTRY_DSN = os.getenv(
    "SENTRY_DSN",
    "https://ea0c1c17aa52e2194f863903a36d0ef8@o4511445819719680.ingest.us.sentry.io/4511445861728256",
).strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.0,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            environment="production" if getattr(sys, "frozen", False) else "dev",
        )
    except Exception:
        # Sentry init failure must NEVER crash the engine.
        pass

import config
import agent
import audio
import dictation
import history
import dictionary
import snippets
import styles
import oauth
import llm as llm_module
import hotkey as hotkey_module
import mcp_client
import meetings
import audio_meeting
import meeting_watcher
from connectors import registry

import pathlib
_log_path = pathlib.Path.home() / "miniflow" / "miniflow.log"
_log_path.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s %(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_log_path), encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ── WebSocket connection manager ──

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.remove(ws)

    async def broadcast(self, event: str, payload: Any):
        msg = json.dumps({"event": event, "payload": payload})
        for ws in list(self.connections):
            try:
                await ws.send_text(msg)
            except Exception:
                self.connections.remove(ws)

manager = ConnectionManager()

# ── App lifespan ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("MiniFlow engine starting on http://localhost:8765")
    audio.set_event_broadcaster(manager.broadcast)
    dictation.set_event_broadcaster(manager.broadcast)
    agent.set_event_broadcaster(manager.broadcast)
    meetings.set_event_emitter(manager.broadcast)
    audio_meeting.set_event_emitter(manager.broadcast)
    meeting_watcher.set_event_emitter(manager.broadcast)
    # Start MCP servers (Playwright always-on + any with saved credentials)
    asyncio.create_task(mcp_client.start())
    # Relay Railway /tasks/stream → WS "task-update" (notifications + Tasks tab).
    asyncio.create_task(_relay_task_stream())
    # Warm up litellm in the background so the first real LLM call doesn't
    # pay for module init + cost-map load.
    asyncio.create_task(_warm_litellm())
    # Pre-fetch a Deepgram ephemeral key so the first hotkey press doesn't
    # eat the 300–800ms /stt/session round-trip (which was dropping the
    # first word of an utterance).
    asyncio.create_task(audio.prewarm_deepgram_key())
    # Calendar → meeting detection. Runs forever; cancellation is handled
    # by the surrounding lifespan teardown.
    meetings_task = asyncio.create_task(meetings.start_poll_loop())
    # Slack-huddle / Zoom / Teams / Meet auto-detection. Only runs if the
    # user has explicitly enabled it (default off) since it needs Screen
    # Recording permission for the watcher subprocess.
    if config.get_auto_detect_meetings():
        asyncio.create_task(meeting_watcher.start())
    yield
    log.info("MiniFlow engine shutting down")
    meetings_task.cancel()
    try:
        await meetings_task
    except (asyncio.CancelledError, Exception):
        pass
    await meeting_watcher.stop()
    await mcp_client.stop()


async def _warm_litellm():
    try:
        import litellm  # noqa: F401
        from litellm import get_supported_openai_params  # noqa: F401
        import tiktoken
        # Force the cl100k encoding to load so subsequent calls are hot.
        tiktoken.get_encoding("cl100k_base").encode("warmup")
        log.info("litellm + tiktoken warmed up")
    except Exception as e:
        log.warning(f"litellm warmup failed: {e}")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health check ──

@app.get("/health")
async def health():
    return {"status": "ok"}

# ── Debug monitor ──

_DEBUG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Uxie — Live Debug Monitor</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;
       background:#0d1117;color:#e6edf3;min-height:100vh;padding:20px}
  h1{font-size:16px;font-weight:600;color:#58a6ff;margin-bottom:4px}
  .sub{font-size:12px;color:#8b949e;margin-bottom:16px}
  #status{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;
          background:#21262d;color:#8b949e;margin-bottom:16px}
  #status.live{background:#0d3321;color:#3fb950}
  .controls{display:flex;gap:8px;margin-bottom:16px;align-items:center}
  button{background:#21262d;border:1px solid #30363d;color:#e6edf3;
         padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px}
  button:hover{background:#30363d}
  #log{display:flex;flex-direction:column;gap:6px}
  .entry{border-radius:8px;padding:10px 14px;border:1px solid #21262d;
         font-size:12px;line-height:1.5}
  .entry.stt{border-color:#1f4068;background:#0d1f35}
  .entry.llm{border-color:#1a3a1a;background:#0d1f0d}
  .entry.status{border-color:#2d2a1e;background:#1a1a0f}
  .entry.other{border-color:#21262d;background:#161b22}
  .entry.approval{border-color:#4a1f1f;background:#2a0f0f}
  .tag{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
       margin-bottom:4px;display:flex;justify-content:space-between}
  .tag .label{color:#8b949e}
  .entry.stt .tag .label{color:#58a6ff}
  .entry.llm .tag .label{color:#3fb950}
  .entry.approval .tag .label{color:#f85149}
  .time{color:#484f58;font-size:10px}
  .text{color:#e6edf3;white-space:pre-wrap;word-break:break-word}
  .entry.stt .text{color:#a5d6ff}
  .entry.llm .text{color:#7ee787}
  .diff-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
  .diff-col{}
  .diff-label{font-size:10px;font-weight:600;text-transform:uppercase;
              letter-spacing:.06em;color:#8b949e;margin-bottom:3px}
  .diff-col.stt-col .diff-label{color:#58a6ff}
  .diff-col.llm-col .diff-label{color:#3fb950}
  .diff-col .text{font-size:12px}
  .entry.pair{border-color:#2a3a2a;background:#111a11}
  .empty{color:#484f58;text-align:center;padding:40px;font-size:13px}
</style>
</head>
<body>
<h1>Uxie — Live Debug Monitor</h1>
<div class="sub">Real-time view of STT → LLM pipeline. Open at <strong>http://localhost:8765/debug</strong> while Uxie is running.</div>
<span id="status">● connecting…</span>
<div class="controls">
  <button onclick="clearLog()">Clear</button>
  <label style="font-size:12px;color:#8b949e">
    <input type="checkbox" id="pairMode" checked style="margin-right:4px">
    Show STT + LLM side by side
  </label>
</div>
<div id="log"><div class="empty">No events yet — start dictating or run a command in Uxie.</div></div>

<script>
const log = document.getElementById('log');
const statusEl = document.getElementById('status');
let pendingStt = null;   // holds latest STT entry waiting for matching LLM

function ts() {
  const d = new Date();
  return d.toTimeString().slice(0,8) + '.' + String(d.getMilliseconds()).padStart(3,'0');
}

function clearLog() {
  log.innerHTML = '<div class="empty">Log cleared.</div>';
  pendingStt = null;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function prepend(html) {
  const empty = log.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.innerHTML = html;
  log.insertBefore(div.firstChild, log.firstChild);
}

function handleEvent(ev, payload) {
  const t = ts();
  const pair = document.getElementById('pairMode').checked;

  if (ev === 'debug') {
    if (payload.type === 'stt') {
      pendingStt = { text: payload.text, app: payload.app, t };
      if (!pair) {
        prepend(`<div class="entry stt">
          <div class="tag"><span class="label">STT — Heard</span><span class="time">${t} · ${esc(payload.app)}</span></div>
          <div class="text">${esc(payload.text)}</div>
        </div>`);
      }
    } else if (payload.type === 'llm') {
      if (pair && pendingStt) {
        prepend(`<div class="entry pair">
          <div class="tag"><span class="label">STT → LLM</span><span class="time">${t} · ${esc(payload.app)}</span></div>
          <div class="diff-row">
            <div class="diff-col stt-col">
              <div class="diff-label">Heard (raw STT)</div>
              <div class="text">${esc(pendingStt.text)}</div>
            </div>
            <div class="diff-col llm-col">
              <div class="diff-label">Typed (LLM cleaned)</div>
              <div class="text">${esc(payload.text)}</div>
            </div>
          </div>
        </div>`);
        pendingStt = null;
      } else {
        prepend(`<div class="entry llm">
          <div class="tag"><span class="label">LLM — Typed</span><span class="time">${t} · ${esc(payload.app)}</span></div>
          <div class="text">${esc(payload.text)}</div>
        </div>`);
      }
    }
    return;
  }

  if (ev === 'agent-status') {
    prepend(`<div class="entry status">
      <div class="tag"><span class="label">Agent status</span><span class="time">${t}</span></div>
      <div class="text">${esc(payload)}</div>
    </div>`);
    return;
  }

  if (ev === 'approval-needed') {
    prepend(`<div class="entry approval">
      <div class="tag"><span class="label">Approval needed — ${esc(payload.tool)}</span><span class="time">${t}</span></div>
      <div class="text">${esc(payload.summary)}\\n\\nParams: ${esc(JSON.stringify(payload.params, null, 2))}</div>
    </div>`);
    return;
  }

  if (ev === 'action-result') {
    const action = payload?.action ?? ev;
    if (action === 'dictation-final' || action === 'dictation') return; // covered by LLM debug
    prepend(`<div class="entry other">
      <div class="tag"><span class="label">Action: ${esc(action)}</span><span class="time">${t}</span></div>
      <div class="text">${esc(payload?.message ?? JSON.stringify(payload))}</div>
    </div>`);
    return;
  }

  if (ev === 'transcription') {
    prepend(`<div class="entry stt">
      <div class="tag"><span class="label">Transcription (command)</span><span class="time">${t}</span></div>
      <div class="text">${esc(payload?.transcript ?? '')}</div>
    </div>`);
    return;
  }
}

function connect() {
  const ws = new WebSocket('ws://localhost:8765/ws');
  ws.onopen = () => {
    statusEl.textContent = '● live';
    statusEl.className = 'live';
  };
  ws.onclose = () => {
    statusEl.textContent = '● disconnected — retrying…';
    statusEl.className = '';
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleEvent(msg.event, msg.payload);
    } catch {}
  };
}
connect();
</script>
</body>
</html>"""

@app.get("/debug")
async def debug_monitor():
    return HTMLResponse(_DEBUG_HTML)

# ── OAuth callback (local loopback — Google/Slack redirect here directly) ──

_SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head><title>MiniFlow — Connected</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex;justify-content:center;align-items:center;
       height:100vh;margin:0;background:#0f1923;color:#fff}
  .box{text-align:center}
  h2{font-size:1.4rem;font-weight:600;margin-bottom:.5rem}
  p{color:#8899aa;font-size:.9rem}
</style></head>
<body><div class="box">
  <h2>✓ Connected successfully</h2>
  <p>You can close this window and return to MiniFlow.</p>
</div></body></html>
"""

_FAIL_HTML = """
<!DOCTYPE html>
<html>
<head><title>MiniFlow — Error</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex;justify-content:center;align-items:center;
       height:100vh;margin:0;background:#0f1923;color:#fff}
  .box{text-align:center}
  h2{font-size:1.4rem;font-weight:600;margin-bottom:.5rem;color:#ff6b6b}
  p{color:#8899aa;font-size:.9rem}
</style></head>
<body><div class="box">
  <h2>Connection failed</h2>
  <p>{error}</p>
</div></body></html>
"""

@app.get("/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    """Legacy local-loopback endpoint. New OAuth flow goes through Railway
    and redirects to /oauth/{provider}/done instead — this route is
    retained only for backward compatibility with old installs."""
    if error:
        log.error(f"OAuth error from provider: {error} — {error_description}")
        return HTMLResponse(_FAIL_HTML.format(error=error_description or error), status_code=400)
    if not code or not state:
        return HTMLResponse(_FAIL_HTML.format(error="Missing code or state."), status_code=400)
    try:
        provider = await oauth.handle_callback(code, state)
        await manager.broadcast("oauth-connected", {"provider": provider})
        return HTMLResponse(_SUCCESS_HTML)
    except Exception as e:
        log.error(f"OAuth callback error: {e}")
        return HTMLResponse(_FAIL_HTML.format(error=str(e)), status_code=400)


@app.get("/oauth/{provider}/done")
async def oauth_done(provider: str, error: str = "", message: str = ""):
    """Railway redirects the user's browser here after a successful OAuth
    handshake. We just confirm + notify the renderer so the Connectors UI
    refreshes; the actual token already lives in Railway's DB."""
    if error:
        log.error(f"OAuth error from Railway for {provider}: {error} — {message}")
        return HTMLResponse(_FAIL_HTML.format(error=message or error), status_code=400)
    await manager.broadcast("oauth-connected", {"provider": provider})
    log.info(f"OAuth complete via Railway: {provider}")
    return HTMLResponse(_SUCCESS_HTML)

# ── WebSocket endpoint ──

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(ws)

# ── Invoke dispatcher ──

async def _start_listening(b: dict):
    bundle_id = b.get("bundleID")
    if bundle_id:
        agent.set_target_app(bundle_id)
    return await audio.start_listening(
        sample_rate=b.get("sampleRate", 16000),
        mode=b.get("mode", "dictation"),
    )


async def _send_otp(email: str, referral_code: str | None = None):
    import httpx
    base = config.get_uxie_backend_url()
    payload: dict = {"email": email}
    if referral_code:
        payload["referral_code"] = referral_code
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{base}/auth/send-otp", json=payload)
        resp.raise_for_status()
        return resp.json()


async def _verify_otp(email: str, code: str):
    import httpx
    base = config.get_uxie_backend_url()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{base}/auth/verify-otp", json={"email": email, "code": code})
        resp.raise_for_status()
        data = resp.json()
    config.save_jwt(
        token=data["access_token"],
        email=email,
        tier=data.get("tier", "free"),
        referral_code=data.get("referral_code", ""),
        free_days_remaining=data.get("free_days_remaining", 30),
    )
    return data


async def _get_user_status():
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not_logged_in"}
    base = config.get_uxie_backend_url()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{base}/user/status",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        resp.raise_for_status()
        data = resp.json()
    config.save_jwt(
        token=jwt,
        email=data.get("email", ""),
        tier=data.get("tier", "free"),
        referral_code=data.get("referral_code", ""),
        free_days_remaining=data.get("free_days_remaining", 30),
    )
    return data


async def _relay_task_stream() -> None:
    """Hold an SSE connection to Railway /tasks/stream and rebroadcast each
    `task-update` to Electron. Reconnects forever; idles while signed out."""
    import httpx
    backoff = 2
    while True:
        jwt = config.get_jwt()
        if not jwt:
            await asyncio.sleep(10)
            continue
        try:
            timeout = httpx.Timeout(10, read=60)  # server pings every 15s
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "GET", f"{config.get_uxie_backend_url()}/tasks/stream",
                    headers={"Authorization": f"Bearer {jwt}"},
                ) as r:
                    if r.status_code != 200:
                        raise httpx.HTTPError(f"status {r.status_code}")
                    backoff = 2
                    event = None
                    async for line in r.aiter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "task-update":
                            try:
                                await manager.broadcast("task-update", json.loads(line[5:]))
                            except ValueError:
                                pass
                        elif not line:
                            event = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"task stream disconnected: {e}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def _backend_json(method: str, path: str, body: dict | None = None) -> dict:
    """Authenticated call to the Railway backend; errors come back as {"error": ...}."""
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(
                method, f"{config.get_uxie_backend_url()}{path}",
                headers={"Authorization": f"Bearer {jwt}"}, json=body,
            )
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            return {"error": str(detail)[:300]}
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _tasks_create(prompt: str) -> dict:
    """Create a background task on Railway. Returns {id, status}."""
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base}/tasks/create",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"prompt": prompt},
            )
        if r.status_code == 429:
            return {"error": r.json().get("detail", "rate limit hit")}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _tasks_list() -> dict:
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"tasks": []}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{base}/tasks",
                headers={"Authorization": f"Bearer {jwt}"},
            )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"tasks": [], "error": str(e)}


async def _tasks_get(task_id: str) -> dict:
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{base}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {jwt}"},
            )
        if r.status_code == 404:
            return {"error": "task not found"}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _tasks_approve(task_id: str, tool_call_id: str, approved: bool, edited_args: dict | None) -> dict:
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base}/tasks/{task_id}/approve",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"tool_call_id": tool_call_id, "approved": approved, "edited_args": edited_args},
            )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _tasks_cancel(task_id: str) -> dict:
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base}/tasks/{task_id}/cancel",
                headers={"Authorization": f"Bearer {jwt}"},
            )
        if r.status_code == 404:
            return {"error": "task not found"}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


# ── Scheduled tasks (Briefings, v1.2) ─────────────────────────────────────────


async def _sched_request(method: str, path: str, body: dict | None = None) -> dict:
    """Thin proxy from engine to Railway /scheduled_tasks/* — keeps
    JWT/auth in one place."""
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(
                method, f"{base}{path}",
                headers={"Authorization": f"Bearer {jwt}"},
                json=body,
            )
        if r.status_code == 404:
            return {"error": "not found"}
        r.raise_for_status()
        return r.json() if r.text else {"ok": True}
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _sched_list() -> dict:
    return await _sched_request("GET", "/scheduled_tasks")


async def _sched_create(body: dict) -> dict:
    return await _sched_request("POST", "/scheduled_tasks", body=body)


async def _sched_patch(st_id: str, body: dict) -> dict:
    return await _sched_request("PATCH", f"/scheduled_tasks/{st_id}", body=body)


async def _sched_delete(st_id: str) -> dict:
    return await _sched_request("DELETE", f"/scheduled_tasks/{st_id}")


async def _sched_fire(st_id: str) -> dict:
    return await _sched_request("POST", f"/scheduled_tasks/{st_id}/fire")


async def _referral_stats() -> dict:
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{base}/referral/stats",
                headers={"Authorization": f"Bearer {jwt}"},
            )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _referral_redeem(code: str) -> dict:
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return {"error": "not signed in"}
    base = config.get_uxie_backend_url()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base}/referral/redeem",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"code": code},
            )
        if r.status_code == 400 or r.status_code == 404:
            return {"error": r.json().get("detail", f"HTTP {r.status_code}")}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {"error": str(e)}


async def _set_auto_detect_meetings(enabled: bool) -> dict:
    """Toggle persistent setting AND start/stop the watcher subprocess
    so the change takes effect immediately."""
    config.set_auto_detect_meetings(enabled)
    if enabled:
        await meeting_watcher.start()
    else:
        await meeting_watcher.stop()
    return {"ok": True, "enabled": enabled}


async def _mcp_connect_server(server_id: str, credentials: dict):
    """Save all credentials for a server then restart it."""
    for key, value in credentials.items():
        mcp_client.set_credential(server_id, key, value)
    await mcp_client.get_manager().restart_server(server_id)
    return {"ok": True, "status": mcp_client.get_server_status()}


async def _mcp_disconnect_server(server_id: str):
    """Clear credentials and stop the server."""
    creds = mcp_client.get_credentials()
    creds.pop(server_id, None)
    mcp_client.save_credentials(creds)
    await mcp_client.get_manager().restart_server(server_id)
    return {"ok": True}


@app.post("/invoke/{command}")
async def invoke(command: str, body: dict = {}):
    handlers = {
        # Audio
        "start_listening":       lambda b: _start_listening(b),
        "stop_listening":        lambda b: audio.stop_listening(),
        "send_audio_chunk":      lambda b: audio.send_audio_chunk(b["chunk"]),
        # Agent
        "execute_command":       lambda b: agent.execute_command(b["command"]),
        # Config
        "save_api_key":          lambda b: config.save_api_key(b["service"], b["key"]),
        "get_api_key":           lambda b: config.get_api_key(b["service"]),
        "has_api_keys":          lambda b: config.has_api_keys(),
        "save_language":         lambda b: config.save_language(b["language"]),
        "get_language":          lambda b: config.get_language(),
        "get_advanced_settings": lambda b: config.get_advanced_settings(),
        "save_advanced_setting": lambda b: config.save_advanced_setting(b["key"], b["value"]),
        "save_user_name":        lambda b: config.save_user_name(b["name"]),
        "get_user_name":         lambda b: config.get_user_name(),
        # Dictation
        "start_dictation":       lambda b: dictation.start_dictation(),
        "stop_dictation":        lambda b: dictation.stop_dictation(),
        "get_dictation_status":  lambda b: dictation.get_dictation_status(),
        "check_accessibility":   lambda b: dictation.check_accessibility(),
        "open_accessibility_settings": lambda b: dictation.open_accessibility_settings(),
        # History
        "get_history":           lambda b: history.get_history(),
        "clear_history":         lambda b: history.clear_history(),
        # OAuth / Connectors
        "start_oauth":           lambda b: oauth.start_oauth(b["provider"]),
        "disconnect_provider":   lambda b: oauth.disconnect_provider(b["provider"]),
        "get_connected_providers": lambda b: oauth.get_connected_providers(),
        "is_provider_connected": lambda b: oauth.is_provider_connected(b["provider"]),
        "list_connectors":       lambda b: registry.list_connectors(),
        # Dictionary
        "add_dictionary_word":   lambda b: dictionary.add_word(b["from"], b["to"]),
        "remove_dictionary_word": lambda b: dictionary.remove_word(b["from"]),
        "get_dictionary":        lambda b: dictionary.get_dictionary(),
        "import_dictionary":     lambda b: dictionary.import_dictionary(b["entries"]),
        # Snippets
        "add_snippet":           lambda b: snippets.add_snippet(b["trigger"], b["expansion"]),
        "remove_snippet":        lambda b: snippets.remove_snippet(b["trigger"]),
        "get_snippets":          lambda b: snippets.get_snippets(),
        # Styles
        "get_style_preferences": lambda b: styles.get_style_preferences(),
        "save_style_preference": lambda b: styles.save_style_preference(b["category"], b["tone"]),
        # LLM providers
        "list_llm_providers":    lambda b: llm_module.list_providers(),
        "get_llm_config":        lambda b: config.get_llm_config(),
        "get_llm_status":        lambda b: config.llm_provider_status(),
        "set_active_llm":        lambda b: config.set_active_llm_provider(b["provider"]),
        "set_llm_model":         lambda b: config.set_llm_provider_model(
                                    b["provider"], b["model"], b.get("base_url")),
        "set_llm_api_key":       lambda b: config.set_llm_api_key(b["provider"], b["api_key"]),
        "clear_llm_api_key":     lambda b: config.clear_llm_api_key(b["provider"]),
        "get_llm_keys_file":     lambda b: {"path": config.llm_keys_file_path()},
        # Hotkey
        "get_hotkey":            lambda b: hotkey_module.get_hotkey(),
        "set_hotkey":            lambda b: hotkey_module.set_hotkey(b),
        "reset_hotkey":          lambda b: hotkey_module.reset_hotkey(),
        # Uxie auth
        "send_otp":              lambda b: _send_otp(b["email"], b.get("referral_code")),
        "verify_otp":            lambda b: _verify_otp(b["email"], b["code"]),
        "get_user_status":       lambda b: _get_user_status(),
        "logout_uxie":           lambda b: config.clear_jwt(),
        "get_uxie_user":         lambda b: config.get_uxie_user(),
        "set_session_token":     lambda b: config.set_session_token(b.get("token") or ""),
        "get_desktop_context_enabled": lambda b: {"enabled": config.get_desktop_context_enabled()},
        "set_desktop_context_enabled": lambda b: (config.set_desktop_context_enabled(bool(b.get("enabled", True))) or {"ok": True}),
        "agents_list":           lambda b: _backend_json("GET", "/agents"),
        "agents_create":         lambda b: _backend_json("POST", "/agents", b),
        "agents_update":         lambda b: _backend_json("PATCH", f"/agents/{b['id']}", {k: v for k, v in b.items() if k != "id"}),
        "agents_delete":         lambda b: _backend_json("DELETE", f"/agents/{b['id']}"),
        "take_file_token":       lambda b: config.take_file_token(),
        # Approval widget
        "resolve_approval":      lambda b: agent.resolve_approval(
            bool(b.get("approved", False)),
            b.get("edited_params") if isinstance(b.get("edited_params"), dict) else None,
        ),
        # MCP connector management
        "mcp_get_status":        lambda b: mcp_client.get_server_status(),
        "mcp_connect_server":    lambda b: _mcp_connect_server(b["server_id"], b.get("credentials", {})),
        "mcp_disconnect_server": lambda b: _mcp_disconnect_server(b["server_id"]),
        # Meetings (Note Taker tab)
        "list_meetings":         lambda b: meetings.list_meetings(int(b.get("limit", 50))),
        "get_meeting":           lambda b: meetings.get_meeting(int(b["id"])),
        "delete_meeting":        lambda b: meetings.delete_meeting(int(b["id"])),
        "delete_all_meetings":   lambda b: meetings.delete_all_meetings(),
        "update_meeting_notes":  lambda b: meetings.update_user_notes(int(b["id"]), b.get("notes", "")),
        "start_meeting_recording": lambda b: meetings.mark_recording_started(int(b["id"])),
        "stop_meeting_recording":  lambda b: meetings.mark_recording_ended(
            int(b["id"]), b.get("transcript", "")
        ),
        "skip_meeting":          lambda b: meetings.mark_skipped(int(b["id"])),
        "structure_meeting":     lambda b: meetings.structure_meeting(int(b["id"])),
        "get_auto_detect_meetings": lambda b: {"enabled": config.get_auto_detect_meetings()},
        "set_auto_detect_meetings": lambda b: _set_auto_detect_meetings(bool(b.get("enabled", False))),
        "get_share_meetings_with_admin": lambda b: {"enabled": config.get_share_meetings_with_admin()},
        "set_share_meetings_with_admin": lambda b: (config.set_share_meetings_with_admin(bool(b.get("enabled", False))) or {"ok": True, "enabled": bool(b.get("enabled", False))}),
        # Background tasks (v1.1.0)
        "tasks_create": lambda b: _tasks_create(b["prompt"]),
        "tasks_list":   lambda b: _tasks_list(),
        "tasks_get":    lambda b: _tasks_get(b["id"]),
        "tasks_cancel": lambda b: _tasks_cancel(b["id"]),
        "tasks_approve": lambda b: _tasks_approve(
            b["id"], b["tool_call_id"], bool(b.get("approved", False)),
            b.get("edited_args") if isinstance(b.get("edited_args"), dict) else None,
        ),
        # Scheduled tasks / Briefings (v1.2)
        "sched_list":   lambda b: _sched_list(),
        "sched_create": lambda b: _sched_create(b),
        "sched_patch":  lambda b: _sched_patch(b["id"], {k: v for k, v in b.items() if k != "id"}),
        "sched_delete": lambda b: _sched_delete(b["id"]),
        "sched_fire":   lambda b: _sched_fire(b["id"]),
        # Referrals
        "referral_stats":  lambda b: _referral_stats(),
        "referral_redeem": lambda b: _referral_redeem(b["code"]),
        # App
        "open_settings":         lambda b: None,
    }

    handler = handlers.get(command)
    if not handler:
        return {"error": f"Unknown command: {command}"}

    try:
        result = handler(body)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception as e:
        log.error(f"[{command}] {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    import sys
    import uvicorn

    # When frozen (PyInstaller bundle), GUI apps don't inherit shell env vars —
    # SSL_CERT_FILE / REQUESTS_CA_BUNDLE are unset and all HTTPS calls fail.
    # Auto-configure from the certifi bundle that PyInstaller packages.
    if getattr(sys, "frozen", False):
        try:
            import certifi
            cert = certifi.where()
            os.environ.setdefault("SSL_CERT_FILE", cert)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", cert)
        except Exception:
            pass

    uvicorn.run(app, host="127.0.0.1", port=8765, reload=False)
