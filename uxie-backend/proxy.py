"""
Proxy routes — forwards LLM and STT requests on behalf of authenticated users.

Routes:
  POST /llm/stream   — SSE streaming to Groq (dictation) or OpenAI (commands)
  POST /llm/chat     — non-streaming chat with optional tool calling
  POST /stt/session  — mints a short-lived Deepgram key scoped to this user
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx
from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user
from db import SessionLog, User, get_db
from limits import check_and_increment, check_burst
from settings import get_settings
import usage

_settings = get_settings()
_log = logging.getLogger("proxy")


# ── Shared HTTP client (connection pool + TLS reuse) ──────────────────────────

_http: httpx.AsyncClient | None = None


def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


# ── LLM streaming proxy ───────────────────────────────────────────────────────

class LLMStreamRequest(BaseModel):
    messages: list[dict]
    model: str = "llama-3.1-8b-instant"
    provider: str = "groq"   # "groq" | "openai"
    temperature: float = 0.3
    max_tokens: int = 1024
    # Client-generated UUID to correlate this LLM call with an audio upload.
    # Optional — if the client is pre-v1.0.13 it simply won't send one and we'll
    # still log the session (without audio).
    session_id: str | None = None


_GROQ_BASE = "https://api.groq.com/openai/v1"
_OPENAI_BASE = "https://api.openai.com/v1"
_DEEPGRAM_BASE = "https://api.deepgram.com/v1"


def _llm_base_and_key(provider: str) -> tuple[str, str]:
    if provider == "groq":
        key = _settings.groq_api_key
        if not key:
            raise HTTPException(500, "Groq API key not configured on server")
        return _GROQ_BASE, key
    elif provider == "openai":
        key = _settings.openai_api_key
        if not key:
            raise HTTPException(500, "OpenAI API key not configured on server")
        return _OPENAI_BASE, key
    raise HTTPException(400, f"Unknown provider: {provider}")


async def _stream_chunks(
    base_url: str,
    api_key: str,
    payload: dict,
    *,
    on_usage: "callable | None" = None,
    on_content_delta: "callable | None" = None,
) -> AsyncIterator[bytes]:
    """Proxy SSE from provider -> client. While proxying, sniff `data: {...}`
    lines to extract the final `usage` chunk (for billing) and the streaming
    content deltas (for session logging)."""
    import json as _json
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    client = get_http()
    async with client.stream("POST", f"{base_url}/chat/completions",
                             headers=headers, json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                body = line[6:].strip()
                if body and body != "[DONE]":
                    try:
                        obj = _json.loads(body)
                        if on_usage:
                            u = obj.get("usage")
                            if isinstance(u, dict):
                                on_usage(u)
                        if on_content_delta:
                            choices = obj.get("choices") or []
                            if choices:
                                delta = (choices[0] or {}).get("delta") or {}
                                content = delta.get("content")
                                if content:
                                    on_content_delta(content)
                    except Exception:
                        pass
            yield (line + "\n\n").encode()


def _extract_user_text(messages: list[dict]) -> str:
    """Pull the last user turn out of the messages array — that's the raw STT
    transcript we want to log. Falls back to empty string if malformed."""
    for m in reversed(messages or []):
        if (m or {}).get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Multimodal content arrays — join text parts.
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                return " ".join(parts)
    return ""


async def _record_session(
    db: AsyncSession,
    *,
    user_id: int,
    session_id: str | None,
    action: str,
    provider: str,
    model: str,
    input_text: str,
    output_text: str,
    duration_ms: int,
) -> None:
    try:
        row = SessionLog(
            user_id=user_id,
            session_id=session_id,
            action=action,
            provider=provider,
            model=model,
            input_text=input_text[:50_000],   # sanity cap; 99.9th percentile well under
            output_text=output_text[:50_000],
            duration_ms=duration_ms,
        )
        db.add(row)
        await db.commit()
    except Exception as e:
        _log.warning("record_session failed: %s", e)


async def llm_stream(
    body: LLMStreamRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> StreamingResponse:
    action = "dictation" if body.provider == "groq" else "command"
    await check_and_increment(db, user, action)

    base_url, api_key = _llm_base_and_key(body.provider)
    payload = {
        "model": body.model,
        "messages": body.messages,
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "stream": True,
        # Opt in to usage accounting on the final chunk; both OpenAI and Groq
        # respect this. Without it, `usage` comes back null.
        "stream_options": {"include_usage": True},
    }

    import time as _time
    input_text = _extract_user_text(body.messages)
    output_buf: list[str] = []
    captured: dict = {"prompt_tokens": 0, "completion_tokens": 0, "started_at": _time.perf_counter()}

    def _on_usage(u: dict) -> None:
        captured["prompt_tokens"] = int(u.get("prompt_tokens", 0) or 0)
        captured["completion_tokens"] = int(u.get("completion_tokens", 0) or 0)

    def _on_content(c: str) -> None:
        output_buf.append(c)

    async def _wrap():
        try:
            async for chunk in _stream_chunks(base_url, api_key, payload,
                                              on_usage=_on_usage, on_content_delta=_on_content):
                yield chunk
        finally:
            duration_ms = int((_time.perf_counter() - captured["started_at"]) * 1000)
            await usage.record_llm_usage(
                db,
                user_id=user.id,
                provider=body.provider,
                model=body.model,
                action=action,
                prompt_tokens=captured["prompt_tokens"],
                completion_tokens=captured["completion_tokens"],
                duration_ms=duration_ms,
            )
            await _record_session(
                db,
                user_id=user.id,
                session_id=body.session_id,
                action=action,
                provider=body.provider,
                model=body.model,
                input_text=input_text,
                output_text="".join(output_buf),
                duration_ms=duration_ms,
            )

    return StreamingResponse(_wrap(), media_type="text/event-stream")


# ── LLM non-streaming (tool calling / commands) ───────────────────────────────

class LLMChatRequest(BaseModel):
    messages: list[dict]
    model: str = "gpt-4o"
    provider: str = "openai"
    temperature: float = 0.2
    tools: list[dict] | None = None
    tool_choice: str | None = None
    session_id: str | None = None  # see LLMStreamRequest.session_id


async def llm_chat(
    body: LLMChatRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    import time as _time
    await check_and_increment(db, user, "command")
    base_url, api_key = _llm_base_and_key(body.provider)
    payload: dict = {
        "model": body.model,
        "messages": body.messages,
        "temperature": body.temperature,
    }
    if body.tools:
        payload["tools"] = body.tools
        payload["tool_choice"] = body.tool_choice or "auto"

    client = get_http()
    t0 = _time.perf_counter()
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    duration_ms = int((_time.perf_counter() - t0) * 1000)

    u = data.get("usage") or {}
    await usage.record_llm_usage(
        db,
        user_id=user.id,
        provider=body.provider,
        model=body.model,
        action="command",
        prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
        completion_tokens=int(u.get("completion_tokens", 0) or 0),
        duration_ms=duration_ms,
    )

    # Session log — extract input (last user msg) + output (completion text or
    # serialized tool calls so we can see what the LLM decided to do).
    import json as _json
    choice = (data.get("choices") or [{}])[0].get("message") or {}
    out_parts: list[str] = []
    if choice.get("content"):
        out_parts.append(str(choice["content"]))
    if choice.get("tool_calls"):
        out_parts.append("tool_calls=" + _json.dumps(choice["tool_calls"], ensure_ascii=False))
    await _record_session(
        db,
        user_id=user.id,
        session_id=body.session_id,
        action="command",
        provider=body.provider,
        model=body.model,
        input_text=_extract_user_text(body.messages),
        output_text="\n".join(out_parts),
        duration_ms=duration_ms,
    )
    return data


# ── Structure meeting (Granola-style summary from transcript + notes) ────────


_MEETING_SYSTEM_PROMPT = """You are a meeting-notes assistant. The user is going to give you a
raw conversation transcript and optionally their own rough notes typed during
the meeting. Your job:

1. Use the user's rough notes (if any) as a ROADMAP — anchor your output to
   the topics, decisions and action items they flagged. Don't drop their
   bullet points; expand them with context from the transcript.
2. Produce clean, scannable Markdown with these sections in this order:

   ## TL;DR
   2-4 bullets — the meeting in 30 seconds.

   ## Decisions
   Each decision on one line, prefixed with `- `. Skip the section if none.

   ## Action items
   `- [ ] <owner>: <task>` — one line each. Use "@me" if owner is unclear
   but the speaker took it on. Skip the section if none.

   ## Discussion notes
   Topic-organized paragraphs covering the substance. Quote a name + short
   line if a specific person made a notable point.

   ## Open questions
   Bullets — anything raised but not resolved. Skip if none.

3. Never hallucinate. If the transcript is sparse or unclear, say so in
   the TL;DR rather than inventing structure.
4. Strip filler ("um", "uh", "you know"), but preserve substance.
5. Output PLAIN markdown — no preamble, no "Sure, here's the summary".
"""


class StructureMeetingRequest(BaseModel):
    transcript: str
    user_notes: str = ""
    title: str = ""
    model: str = "gpt-4o"
    # Per-meeting client UUID for log correlation; same shape as session_id.
    session_id: str | None = None


# Server-side input caps. Each token ≈ 4 chars for English; 200k chars is
# roughly 50k tokens which is well within gpt-4o's window AND keeps the worst-
# case cost per call bounded.
_MAX_TRANSCRIPT_CHARS = 200_000
_MAX_NOTES_CHARS = 20_000
_MAX_TITLE_CHARS = 200


async def llm_structure_meeting(
    body: StructureMeetingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Turn a raw transcript + the user's typed notes into a Granola-style
    structured summary. Counted under the monthly `command` quota AND a per-
    hour/day burst bucket (defence-in-depth)."""
    # Burst limit FIRST so monthly counter isn't consumed when rejecting.
    check_burst(
        user.id,
        "structure_meeting",
        per_hour=_settings.burst_structure_meeting_per_hour,
        per_day=_settings.burst_structure_meeting_per_day,
    )
    await check_and_increment(db, user, "command")

    transcript = (body.transcript or "")[:_MAX_TRANSCRIPT_CHARS]
    notes = (body.user_notes or "")[:_MAX_NOTES_CHARS]
    title = (body.title or "")[:_MAX_TITLE_CHARS]

    if not transcript.strip():
        raise HTTPException(400, "transcript is empty")

    user_msg_parts = []
    if title:
        user_msg_parts.append(f"# Meeting: {title}\n")
    if notes.strip():
        user_msg_parts.append("## My rough notes (use as roadmap)\n" + notes.strip() + "\n")
    user_msg_parts.append("## Raw transcript\n" + transcript)
    user_msg = "\n".join(user_msg_parts)

    payload = {
        "model": body.model,
        "messages": [
            {"role": "system", "content": _MEETING_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "temperature": 0.2,
    }

    base_url, api_key = _llm_base_and_key("openai")
    client = get_http()
    import time as _time
    t0 = _time.perf_counter()
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    duration_ms = int((_time.perf_counter() - t0) * 1000)

    u = data.get("usage") or {}
    await usage.record_llm_usage(
        db,
        user_id=user.id,
        provider="openai",
        model=body.model,
        action="command",
        prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
        completion_tokens=int(u.get("completion_tokens", 0) or 0),
        duration_ms=duration_ms,
    )

    structured = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    # Truncated input goes to the session log; transcripts themselves aren't
    # logged so we keep the privacy story clean ("listens, writes, forgets").
    await _record_session(
        db,
        user_id=user.id,
        session_id=body.session_id,
        action="command",
        provider="openai",
        model=body.model,
        input_text=f"[structure_meeting] title={title} transcript_chars={len(transcript)} notes_chars={len(notes)}",
        output_text=structured,
        duration_ms=duration_ms,
    )

    return {"structured": structured, "model": body.model}


# ── STT session token ─────────────────────────────────────────────────────────
# If DEEPGRAM_PROJECT_ID is set, mint a per-session scoped key with short TTL.
# If minting fails, stt_session returns 503 — the master key never leaves the server.

class STTSessionResponse(BaseModel):
    token: str
    expires_in: int = 300
    sample_rate: int = 16000


async def _mint_deepgram_key(user_id: int) -> tuple[str | None, str | None]:
    """Returns (key, api_key_id). key is None on failure."""
    master = (_settings.deepgram_api_key or "").strip()
    project_id = (_settings.deepgram_project_id or "").strip()
    if not master or not project_id:
        return None, None
    try:
        client = get_http()
        resp = await client.post(
            f"{_DEEPGRAM_BASE}/projects/{project_id}/keys",
            headers={
                "Authorization": f"Token {master}",
                "Content-Type": "application/json",
            },
            json={
                "comment": f"uxie-user-{user_id}",
                "scopes": ["usage:write"],
                "time_to_live_in_seconds": _settings.deepgram_session_ttl_seconds,
                "tags": [f"user:{user_id}"],
            },
        )
        if resp.status_code in (200, 201):
            j = resp.json()
            return j.get("key"), j.get("api_key_id")
        _log.warning(
            "Deepgram key mint failed: %s %s", resp.status_code, resp.text[:200]
        )
        return None, None
    except Exception as e:
        _log.warning("Deepgram key mint exception: %s", e)
        return None, None


async def stt_session(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> STTSessionResponse:
    # One retry absorbs a transient Deepgram hiccup. The master key is never
    # handed to clients — if minting keeps failing, voice fails closed.
    ephemeral, key_id = await _mint_deepgram_key(user.id)
    if not ephemeral:
        ephemeral, key_id = await _mint_deepgram_key(user.id)
    if not ephemeral:
        _log.error("Deepgram ephemeral key mint failed twice for user %s", user.id)
        raise HTTPException(503, "Voice is temporarily unavailable — please try again")
    await usage.record_stt_usage(db, user_id=user.id, deepgram_key_id=key_id)
    return STTSessionResponse(
        token=ephemeral,
        expires_in=_settings.deepgram_session_ttl_seconds,
    )
