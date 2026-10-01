"""
iOS-facing server-side agent endpoints.

Runs the LLM tool-calling loop server-side, streams progress to the client via
SSE. Strictly additive: existing /llm/chat, /llm/stream, /stt/session keep
working exactly as before — Mac/Windows desktop clients are unaffected.

Endpoints (registered in main.py):
  POST /agent/execute                                    → SSE stream
  POST /agent/approve/{session_id}                       → resume on approval
  POST /agent/client_tool_result/{session_id}/{tcid}     → resume on client-tool result

SSE wire format (standard):
    event: tool_call_start
    data: {"id":"tc_abc","name":"send_slack","args":{...}}

Event taxonomy (frozen, must match docs/PROTOCOL.md in uxie-ios):
  session              first event; carries session_id and conversation_id
  tool_call_start      LLM dispatched a tool (server- or client-side)
  tool_call_result     a tool finished
  approval_needed      destructive tool — client must POST /agent/approve
  client_tool_invoke   LLM picked a client-only tool — client runs + posts back
  final_text           user-visible final answer
  error                terminal error (code, message, retryable)
  done                 clean stream end

Approval timeout: 60s. Client-tool timeout: 30s. Server keep-alive ping every 15s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

import model_gateway
from auth import current_user
from db import User, get_db
from db_ios import Conversation, Turn
from limits import check_and_increment
import usage as _usage
from proxy import _llm_base_and_key, get_http

try:
    import connectors as _connectors
except Exception:  # noqa: BLE001 — connectors package optional for early phases
    _connectors = None  # type: ignore[assignment]

_log = logging.getLogger("agent")


# ── Constants ─────────────────────────────────────────────────────────────────

APPROVAL_TIMEOUT_S = 300        # 5 min — was 60s, but reading "send email
                                # to X saying Y" + tapping Do It easily takes
                                # >60s. Production logs showed users hitting
                                # 404 on /agent/approve because the park had
                                # already timed out.
CLIENT_TOOL_TIMEOUT_S = 30
KEEPALIVE_INTERVAL_S = 15
MAX_TURNS = 4

# Groq's Llama 3.3 70B is ~6× faster than gpt-4o-mini at tool-calling
# tasks of this size, and quality is plenty for an agent that picks one
# tool from a small registry. Falls back to OpenAI implicitly via
# proxy._llm_base_and_key if GROQ_API_KEY isn't set.
# Models come from model_gateway roles: "voice_agent" (this loop) and
# "dictation_fix".

# Tools that always require user approval before executing. Server-side
# (connector) tools and client-side tools both can be marked destructive —
# the gate is the same either way. Names match the schema definitions.
DESTRUCTIVE_TOOLS: set[str] = {
    # Generic / legacy (kept so existing tests don't break)
    "send_slack", "send_email", "send_message",
    # Slack connector
    "slack_send_message",
    # Calendar
    "create_calendar_event", "create_calendar_event_local", "delete_calendar_event",
    # Files / generic destructive
    "delete_file",
    # Future connectors (placeholder — names will be confirmed when ported)
    "gmail_send", "github_create_issue", "linear_create_issue", "notion_append",
}

# Server-side tools the agent can invoke (Phase 0.5: minimal demo set;
# Phase 0.6 adds connectors: Slack/Gmail/Calendar/Notion/Linear/GitHub).
SERVER_TOOL_NAMES: set[str] = {"echo"}

# Client-side tool schemas advertised to the LLM. These are the iOS-only
# tools (open_url, share_sheet, EventKit). When advertised by the client
# in tools_available_on_client, the LLM sees them; when picked, the server
# emits client_tool_invoke and parks for the result POST.
CLIENT_TOOL_SCHEMAS: dict[str, dict] = {
    "open_url": {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open a URL on the user's device. "
                "When the user says 'open the X app' or names a specific app, "
                "PREFER its native URL scheme so the app launches instead of a web page. "
                "Common iOS schemes: "
                "twitter:// (or x://), instagram://, slack://, notion://, "
                "googlegmail:// (Gmail app), comgooglemaps:// (Google Maps), maps:// (Apple Maps), "
                "youtube://, whatsapp://, spotify:, linkedin://, discord://, fb:// (Facebook), "
                "tg:// (Telegram), reddit://, snapchat://, music:// (Apple Music), "
                "tel:<number>, sms:<number>, mailto:<address>. "
                "Only fall back to https://… when the user explicitly wants the website "
                "(e.g. 'open uxie.ai') or when no app scheme exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    "share_sheet": {
        "type": "function",
        "function": {
            "name": "share_sheet",
            "description": "Show the iOS share sheet for the given text.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    "copy_to_clipboard": {
        "type": "function",
        "function": {
            "name": "copy_to_clipboard",
            "description": "Copy text to the user's clipboard.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    "create_calendar_event_local": {
        "type": "function",
        "function": {
            "name": "create_calendar_event_local",
            "description": "Create an event in the user's local Calendar via EventKit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "starts_at": {"type": "string", "description": "ISO-8601 timestamp"},
                    "ends_at": {"type": "string", "description": "ISO-8601 timestamp"},
                    "notes": {"type": "string"},
                },
                "required": ["title", "starts_at", "ends_at"],
            },
        },
    },
    "add_reminder": {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": "Add a reminder to the user's Reminders app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due_at": {"type": "string", "description": "ISO-8601 timestamp"},
                },
                "required": ["title"],
            },
        },
    },
}

SYSTEM_PROMPT = """You are Uxie, a voice-controlled agent. The user spoke a command on their phone; you decide what to do.

Rules:
1. Prefer calling a tool over describing what you'd do. The user expects action, not explanation.
2. If the user wants to send a message, create an event, or do anything destructive, call the appropriate tool — the user will approve it before it actually fires.
3. If a tool needs information you don't have (e.g. a recipient's email address), ask one short clarifying question via final_text instead of guessing.
4. After tools complete, return a one-sentence confirmation via final_text.
5. Be terse. Voice output is short.
6. If the user just wants to dictate text (no command), return their cleaned-up text via final_text.
7. TIMESTAMPS: when calling a tool that takes a date/time, emit ISO-8601 with the user's timezone offset, NOT UTC.
   Example: user is in {timezone} and says "tomorrow at 9am" — emit "{tomorrow_example}".
   Never emit a naked "Z" / UTC timestamp; always include the offset for {timezone}.

8. EMAIL FORMATTING (gmail_send / gmail_draft):
   - Generate a short, specific subject line yourself from the body's intent — never use the user's raw transcript as subject. 3-7 words. Title-case ok. No trailing period.
   - The `body` argument MUST be a single string with literal \\n newline escapes between sections. Use plain text, no markdown, no greeting/sign-off duplication if the user already dictated one.
   - Required structure (note the \\n placements — there is a newline between "Best regards," and the name):

     "Hi <FirstName>,\\n\\n<polished message body>\\n\\nBest regards,\\n{user_name}"

   - Concrete example. User said "tell john we're meeting tomorrow at 4". Emit:

     {{"to": "john@example.com", "subject": "Meeting Tomorrow at 4", "body": "Hi John,\\n\\nJust confirming our meeting tomorrow at 4. Let me know if anything changes.\\n\\nBest regards,\\n{user_name}"}}

   - Polish grammar/punctuation, expand voice-style filler, but do NOT add facts the user didn't say.
   - If the recipient's first name is unknown, use "Hi there,".
   - If the user says "draft" instead of "send", still produce the same fully formatted body.

9. EMAIL CONTEXT FROM PRIOR THREADS (gmail_search + gmail_read first, THEN compose):
   When the user's request continues an existing conversation, ALWAYS pull
   the prior thread BEFORE composing. Don't ship a blind email when a
   thread already exists.

   Trigger gmail_search → gmail_read before gmail_send/gmail_reply when:
     * "reply to X" / "respond to X" / "answer X"
     * "follow up with X about Y" / "ping X again"
     * "send X about Y" where Y is an ongoing topic
     * any phrasing implying continuation: "as we discussed", "the
       proposal", "that meeting", "your last email"

   Skip the lookup (go straight to gmail_send) when:
     * brand-new outreach to a contact ("email john@x.com saying hi")
     * the user dictates the full body themselves
     * a forward of generic content with no prior thread

   When you DO have context from gmail_read, summarize the prior thread
   in your message body so the recipient sees the lineage (e.g. "Following
   up on the meeting we discussed last Tuesday — …"). Don't paste large
   quotes; reference the topic in 1-2 lines.

Today is {today} in the user's timezone ({timezone}). The user is on {device}.
The user's name is {user_name}. Use it for email sign-offs.
"""


# ── In-memory pending-gate state ──────────────────────────────────────────────
# Each parked agent loop registers an asyncio.Event keyed by an opaque gate-id.
# /agent/approve and /agent/client_tool_result look up the gate, store the
# client's response payload, and set the event to wake the loop.
#
# Process-local for Phase 0. Multi-replica deployment: swap for Redis pub/sub.

@dataclass
class _PendingGate:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    payload: dict[str, Any] | None = None


_PENDING: dict[str, _PendingGate] = {}
_PENDING_LOCK = asyncio.Lock()


async def _park(gate_id: str, timeout_s: float) -> dict[str, Any]:
    """Block until /agent/approve or /agent/client_tool_result fires `gate_id`.
    Returns the client's payload, or raises asyncio.TimeoutError."""
    gate = _PendingGate()
    async with _PENDING_LOCK:
        _PENDING[gate_id] = gate
    try:
        await asyncio.wait_for(gate.event.wait(), timeout=timeout_s)
        return gate.payload or {}
    finally:
        async with _PENDING_LOCK:
            _PENDING.pop(gate_id, None)


async def _park_with_keepalive(gate_id: str, timeout_s: float, ping_every_s: float = 15.0):
    """Async generator: yields `_sse_ping()` byte frames every `ping_every_s`
    seconds while parked, then yields the resolved payload dict and returns.
    Raises asyncio.TimeoutError if the overall `timeout_s` elapses.

    Why: an SSE response with no bytes flowing for ~3min gets killed by
    intermediate proxies (Railway edge) and iOS URLSession's HTTP/2 idle
    handling. Periodic `: ping\\n\\n` SSE comments keep the socket warm
    without surfacing as events to the client parser."""
    gate = _PendingGate()
    async with _PENDING_LOCK:
        _PENDING[gate_id] = gate
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                await asyncio.wait_for(gate.event.wait(), timeout=min(ping_every_s, remaining))
                yield gate.payload or {}
                return
            except asyncio.TimeoutError:
                yield _sse_ping()
    finally:
        async with _PENDING_LOCK:
            _PENDING.pop(gate_id, None)


async def _wake(gate_id: str, payload: dict[str, Any]) -> bool:
    async with _PENDING_LOCK:
        gate = _PENDING.get(gate_id)
    if not gate:
        return False
    gate.payload = payload
    gate.event.set()
    return True


# ── SSE formatting ────────────────────────────────────────────────────────────

def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n".encode("utf-8")


def _sse_ping() -> bytes:
    return b": ping\n\n"


# ── LLM call (extracted so tests can monkey-patch) ────────────────────────────

async def _call_llm(messages: list[dict], tools: list[dict], model: str, provider: str) -> tuple[dict, int, dict]:
    """Single non-streaming chat-completions call. Returns (response_json, duration_ms, usage_dict).
    Tests monkey-patch this with a deterministic stub."""
    base, key = _llm_base_and_key(provider)
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    client = get_http()
    t0 = time.perf_counter()
    resp = await client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code >= 400:
        body_preview = resp.text[:1000]
        _log.error(
            "llm.call provider=%s model=%s status=%s body=%s",
            provider, model, resp.status_code, body_preview,
        )
        resp.raise_for_status()
    data = resp.json()
    dt_ms = int((time.perf_counter() - t0) * 1000)
    return data, dt_ms, (data.get("usage") or {})


# ── Server-side tool dispatcher ───────────────────────────────────────────────
# Phase 0.6: connector tools (Slack today; Google/GitHub/Notion/Linear later)
# dispatch via the connectors registry, which handles OAuth-token lookup.
# `echo` is kept as a no-OAuth demo tool used by tests.

def _display_name(user: User) -> str:
    """Best-effort human name for email sign-offs. The User model only stores
    `email` for now, so derive a first-name from the local part:
        rounak@smallest.ai     → "Rounak"
        rounak.lenka@x.com     → "Rounak"
        rounak_lenka@x.com     → "Rounak"
        r@x.com                → "R"
    Once we add a real `name` column to User, this becomes
    `user.name or _from_email(user.email)`."""
    email = (user.email or "").strip()
    if not email or "@" not in email:
        return "there"
    local = email.split("@", 1)[0]
    # First component before . or _ — that's the first name in most cases.
    first = re.split(r"[._\-+]", local, maxsplit=1)[0]
    return first.capitalize() if first else "there"


async def _server_tool_execute(
    name: str, args: dict, user: User, db: AsyncSession
) -> Any:
    if name == "echo":
        return {"echoed": args.get("text", "")}
    if _connectors is not None and name in _connectors.all_tool_names():
        ok, result = await _connectors.execute(db, user.id, name, args)
        if not ok:
            raise RuntimeError(str(result))
        return result
    raise ValueError(f"unknown server tool: {name}")


def _is_server_tool(name: str) -> bool:
    if name in SERVER_TOOL_NAMES:
        return True
    if _connectors is not None and name in _connectors.all_tool_names():
        return True
    return False


async def _default_agent(db: AsyncSession, user_id: int):
    """The user's default agent, or None if it can't be loaded (the loop
    then runs with built-in behavior)."""
    import agents as _agents
    try:
        return await _agents.ensure_default_agent(db, user_id)
    except Exception:
        _log.warning("default agent lookup failed", exc_info=True)
        return None


# ── Tool schema assembly ──────────────────────────────────────────────────────

async def _build_tool_schemas(
    client_tools: list[str], db: AsyncSession, user: User
) -> list[dict]:
    """Combine server-side tool schemas (per user's connected providers) with
    the client-side ones the caller advertised. The LLM sees one merged list."""
    schemas: list[dict] = []
    # Server-side tools — only providers the user has connected get advertised.
    if _connectors is not None:
        try:
            schemas.extend(await _connectors.tool_schemas_for_user(db, user.id))
        except Exception:
            _log.warning("connector schema lookup failed", exc_info=True)
    # Client-side tools the caller advertised
    for name in client_tools:
        schema = CLIENT_TOOL_SCHEMAS.get(name)
        if schema:
            schemas.append(schema)
    return schemas


# ── Endpoints ─────────────────────────────────────────────────────────────────

async def execute(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Run the server-side agent loop and stream events to the client.

    Request body (JSON):
        transcript: str                       — user's spoken text
        conversation_id: str | null           — null → start a new thread
        tools_available_on_client: [str]      — names of tools the client can run
        mode: "command" | "dictation"         — "command" runs the loop; "dictation" returns corrected text only
    """
    body = await request.json()
    transcript: str = (body.get("transcript") or "").strip()
    conversation_id: str = body.get("conversation_id") or _ulid()
    client_tools: list[str] = list(body.get("tools_available_on_client") or [])
    mode: str = body.get("mode") or "command"
    user_timezone: str = (body.get("timezone") or "UTC")

    if not transcript:
        raise HTTPException(400, "transcript required")
    if mode not in ("command", "dictation"):
        raise HTTPException(400, "mode must be 'command' or 'dictation'")

    # Per-user rate / tier check (reuses existing /llm/chat machinery).
    await check_and_increment(db, user, "command" if mode == "command" else "dictation")

    session_id = _ulid()
    _log.info(
        "agent.execute user=%s session=%s mode=%s conv=%s client_tools=%d transcript_len=%d",
        user.id, session_id, mode, conversation_id, len(client_tools), len(transcript),
    )

    return StreamingResponse(
        _run_loop(session_id, conversation_id, transcript, mode, client_tools, user, db, user_timezone),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def approve(session_id: str, request: Request, user: User = Depends(current_user)):
    """Resume a parked agent loop with the user's approval decision.

    Body: {"approved": bool}
    """
    body = await request.json()
    approved = bool(body.get("approved", False))
    woke = await _wake(f"approve:{session_id}", {"approved": approved})
    if not woke:
        raise HTTPException(404, "session not awaiting approval (or expired)")
    return {"ok": True}


async def client_tool_result(
    session_id: str,
    tool_call_id: str,
    request: Request,
    user: User = Depends(current_user),
):
    """Resume a parked agent loop with a client-tool execution result.

    Body: {"result": <any> | null, "error": <str> | null}
    """
    body = await request.json()
    woke = await _wake(
        f"client_tool:{session_id}:{tool_call_id}",
        {"result": body.get("result"), "error": body.get("error")},
    )
    if not woke:
        raise HTTPException(404, "session not awaiting this tool result (or expired)")
    return {"ok": True}


# ── Loop body ─────────────────────────────────────────────────────────────────

async def _run_loop(
    session_id: str,
    conversation_id: str,
    transcript: str,
    mode: str,
    client_tools: list[str],
    user: User,
    db: AsyncSession,
    user_timezone: str,
) -> AsyncIterator[bytes]:
    try:
        yield _sse("session", {"session_id": session_id, "conversation_id": conversation_id})

        # Persist user turn before doing any work — even if the loop fails,
        # we have a record of what was asked.
        await _ensure_conversation(db, conversation_id, user.id, transcript)
        await _append_turn(db, conversation_id, "user", transcript)

        if mode == "dictation":
            # Dictation mode: cleaner / grammar fix only, no tool calls.
            text = await _dictation_fix(transcript, user)
            await _append_turn(db, conversation_id, "assistant", text)
            yield _sse("final_text", {"text": text})
        else:
            async for chunk in _command_loop(session_id, conversation_id, transcript, client_tools, user, db, user_timezone):
                yield chunk

        yield _sse("done", {"conversation_id": conversation_id})

    except asyncio.CancelledError:
        _log.info("agent.execute session=%s cancelled by client", session_id)
        raise
    except Exception as e:  # noqa: BLE001 — terminal user-visible error
        _log.exception("agent.execute session=%s failed", session_id)
        yield _sse("error", {"code": "internal", "message": str(e), "retryable": False})


async def _command_loop(
    session_id: str,
    conversation_id: str,
    transcript: str,
    client_tools: list[str],
    user: User,
    db: AsyncSession,
    user_timezone: str,
) -> AsyncIterator[bytes]:
    """The real LLM tool-calling loop. Runs up to MAX_TURNS turns."""
    # Compute "today" and a "tomorrow at 9am" example IN THE USER'S TIMEZONE
    # so the LLM has a concrete pattern to imitate when emitting timestamps.
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(user_timezone)
    except Exception:
        tz = timezone.utc
        user_timezone = "UTC"
    now_local = datetime.now(tz)
    today = now_local.strftime("%A %Y-%m-%d")
    tomorrow_9am = (now_local.replace(hour=9, minute=0, second=0, microsecond=0)
                    + timedelta(days=1))
    tomorrow_example = tomorrow_9am.isoformat()  # includes the offset

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(
            today=today,
            device="iPhone",
            timezone=user_timezone,
            tomorrow_example=tomorrow_example,
            user_name=_display_name(user),
        )},
        {"role": "user", "content": transcript},
    ]
    tools = await _build_tool_schemas(client_tools, db, user)
    # The user's default agent (agents.py) can only tighten this loop:
    # "deny" hides + blocks a tool, "require_approval" adds the approval
    # gate. "allow" never removes the gate on DESTRUCTIVE_TOOLS here.
    default_agent = await _default_agent(db, user.id)
    agent_policy: dict = (default_agent.tool_policy or {}) if default_agent else {}
    tools = [t for t in tools if agent_policy.get((t.get("function") or {}).get("name")) != "deny"]
    if default_agent is not None and default_agent.instructions:
        messages[0]["content"] += "\n\nThe user's standing instructions:\n" + default_agent.instructions

    llm_provider, llm_model = model_gateway.resolve("voice_agent")

    for turn_num in range(MAX_TURNS):
        # ── 1. Call the LLM ──
        try:
            data, dt_ms, llm_usage = await _call_llm(messages, tools, llm_model, llm_provider)
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"code": "llm_call_failed", "message": str(e), "retryable": True})
            return

        # Per-turn timing — surfaces in Railway logs so we can see at a
        # glance whether the LLM call (server-bound) or the rest of the
        # loop is the dominant cost. Format chosen to be greppable:
        #   agent.turn session=… turn=0 model=… llm_ms=842 tools=12 tokens=…/…
        _log.info(
            "agent.turn session=%s turn=%d model=%s llm_ms=%d tools=%d "
            "prompt_tokens=%d completion_tokens=%d",
            session_id, turn_num, llm_model, dt_ms, len(tools),
            int(llm_usage.get("prompt_tokens", 0) or 0),
            int(llm_usage.get("completion_tokens", 0) or 0),
        )

        # Best-effort usage tracking; never blocks the loop.
        try:
            await _usage.record_llm_usage(
                db,
                user_id=user.id,
                provider=llm_provider,
                model=llm_model,
                action="command",
                prompt_tokens=int(llm_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(llm_usage.get("completion_tokens", 0) or 0),
                duration_ms=dt_ms,
            )
        except Exception:
            _log.warning("record_llm_usage failed", exc_info=True)

        choice = (data.get("choices") or [{}])[0].get("message") or {}
        tool_calls = choice.get("tool_calls") or []
        content = choice.get("content")

        # ── 2. No tool calls → final answer ──
        if not tool_calls:
            text = content or "(no response)"
            await _append_turn(db, conversation_id, "assistant", text)
            yield _sse("final_text", {"text": text})
            return

        # ── 3. Has tool calls — record assistant turn, dispatch each call ──
        # Groq's Llama-3.3 rejects `content: null` on assistant turns even
        # when tool_calls is populated; coerce to empty string.
        messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
        await _append_turn(db, conversation_id, "assistant", content or "", tool_calls_json=tool_calls)

        for tc in tool_calls:
            tc_id = tc.get("id") or _ulid()
            fn = tc.get("function") or {}
            tc_name = fn.get("name") or ""
            try:
                tc_args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                tc_args = {}

            yield _sse("tool_call_start", {"id": tc_id, "name": tc_name, "args": tc_args})

            tool_result_for_llm: str
            ok: bool
            yielded_result_event = False

            if agent_policy.get(tc_name) == "deny":
                tool_result_for_llm = "This tool is turned off in the user's agent settings. Tell the user."
                ok = False
                yield _sse("tool_call_result", {"id": tc_id, "ok": False, "result": "Blocked by agent permissions."})
                yielded_result_event = True
            elif tc_name in DESTRUCTIVE_TOOLS or agent_policy.get(tc_name) == "require_approval":
                # Approval gate — common to client- and server-side destructive tools.
                yield _sse("approval_needed", {
                    "session_id": session_id,
                    "tool": tc_name,
                    "summary": _summarize_tool(tc_name, tc_args),
                })
                decision = None
                try:
                    async for chunk in _park_with_keepalive(f"approve:{session_id}", APPROVAL_TIMEOUT_S):
                        if isinstance(chunk, (bytes, bytearray)):
                            yield chunk
                        else:
                            decision = chunk
                except asyncio.TimeoutError:
                    yield _sse("error", {"code": "approval_timeout", "message": f"no approval after {APPROVAL_TIMEOUT_S}s", "retryable": False})
                    return
                if decision is None:
                    decision = {}
                if not decision.get("approved"):
                    tool_result_for_llm = "User cancelled the action."
                    ok = False
                    yield _sse("tool_call_result", {"id": tc_id, "ok": False, "result": "User cancelled."})
                    yielded_result_event = True
                else:
                    # Approved — fall through to actual execution below
                    pass

            if not yielded_result_event:
                if tc_name in client_tools:
                    # Client-side dispatch: emit invoke, park for result POST
                    yield _sse("client_tool_invoke", {
                        "session_id": session_id,
                        "id": tc_id,
                        "name": tc_name,
                        "args": tc_args,
                    })
                    payload: dict[str, Any] | None = None
                    try:
                        async for chunk in _park_with_keepalive(f"client_tool:{session_id}:{tc_id}", CLIENT_TOOL_TIMEOUT_S):
                            if isinstance(chunk, (bytes, bytearray)):
                                yield chunk
                            else:
                                payload = chunk
                    except asyncio.TimeoutError:
                        yield _sse("error", {"code": "client_tool_timeout", "message": f"no result after {CLIENT_TOOL_TIMEOUT_S}s", "retryable": True})
                        return
                    if payload is None:
                        payload = {}
                    err = payload.get("error")
                    ok = err is None
                    tool_result_for_llm = json.dumps({"result": payload.get("result"), "error": err}, default=str)
                    yield _sse("tool_call_result", {"id": tc_id, "ok": ok, "result": payload.get("result")})
                elif _is_server_tool(tc_name):
                    # Server-side dispatch (echo or connector): execute now
                    try:
                        result = await _server_tool_execute(tc_name, tc_args, user, db)
                        ok = True
                        tool_result_for_llm = json.dumps(result, default=str)
                        yield _sse("tool_call_result", {"id": tc_id, "ok": True, "result": result})
                    except Exception as e:  # noqa: BLE001
                        ok = False
                        tool_result_for_llm = f"error: {e}"
                        yield _sse("tool_call_result", {"id": tc_id, "ok": False, "result": str(e)})
                else:
                    ok = False
                    tool_result_for_llm = f"Unknown tool: {tc_name}"
                    yield _sse("tool_call_result", {"id": tc_id, "ok": False, "result": tool_result_for_llm})

            # Append tool result to messages so the LLM sees it on the next turn
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result_for_llm})
            await _append_turn(db, conversation_id, "tool", tool_result_for_llm, tool_call_id=tc_id)

    # Reached MAX_TURNS without a final answer
    text = "I tried but couldn't finish that — try rephrasing or breaking it into steps."
    await _append_turn(db, conversation_id, "assistant", text)
    yield _sse("final_text", {"text": text})


async def _dictation_fix(transcript: str, user: User) -> str:
    """Light grammar / punctuation fix. Single non-tool LLM call."""
    messages = [
        {"role": "system", "content": (
            "You are a dictation cleaner. Take the user's raw speech-to-text "
            "output and return ONLY the cleaned text — fix punctuation, casing, "
            "and obvious disfluencies. Do not paraphrase. Do not add commentary."
        )},
        {"role": "user", "content": transcript},
    ]
    try:
        fix_provider, fix_model = model_gateway.resolve("dictation_fix")
        data, _dt, _u = await _call_llm(messages, [], fix_model, fix_provider)
        choice = (data.get("choices") or [{}])[0].get("message") or {}
        return choice.get("content") or transcript
    except Exception:
        _log.exception("dictation fix failed; returning raw transcript")
        return transcript


# ── Persistence helpers ──────────────────────────────────────────────────────

async def _ensure_conversation(db: AsyncSession, conversation_id: str, user_id: int, first_text: str) -> None:
    """Create the Conversation row if missing; otherwise bump last_active_at.
    If the conversation exists but belongs to a different user, raise 404 (no leak)."""
    existing = await db.get(Conversation, conversation_id)
    if existing:
        if existing.user_id != user_id:
            raise HTTPException(404, "conversation not found")
        existing.last_active_at = datetime.now(timezone.utc)
    else:
        title = (first_text or "")[:60]
        conv = Conversation(
            id=conversation_id,
            user_id=user_id,
            title=title,
            created_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
        )
        db.add(conv)
    await db.commit()


async def _append_turn(
    db: AsyncSession,
    conversation_id: str,
    role: str,
    text: str,
    *,
    tool_calls_json: Any | None = None,
    tool_call_id: str | None = None,
) -> None:
    turn = Turn(
        id=_ulid(),
        conversation_id=conversation_id,
        role=role,
        text=text,
        tool_calls_json=tool_calls_json,
        tool_call_id=tool_call_id,
    )
    db.add(turn)
    await db.commit()


# ── Tool-summary helper (for approval sheet text) ─────────────────────────────

def _summarize_tool(name: str, args: dict) -> str:
    if name in ("send_slack", "slack_send_message"):
        target = args.get("channel") or args.get("to") or "?"
        return f"Send Slack to {target}: \"{args.get('text', '')}\""
    if name in ("send_email", "gmail_send"):
        subj = args.get("subject", "")
        return f"Email {args.get('to', '?')}" + (f" — \"{subj}\"" if subj else "")
    if name in ("create_calendar_event", "create_calendar_event_local"):
        return f"Create event \"{args.get('title', '')}\" at {args.get('starts_at', '?')}"
    if name == "delete_file":
        return f"Delete {args.get('path', '?')}"
    return f"{name}({json.dumps(args, default=str)})"


# ── ULID-ish ─────────────────────────────────────────────────────────────────

def _ulid() -> str:
    """Timestamp + 64 random bits, lexicographically sortable."""
    ts = int(time.time() * 1000).to_bytes(6, "big").hex()
    rnd = secrets.token_hex(8)
    return f"{ts}{rnd}"
