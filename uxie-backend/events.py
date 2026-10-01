"""Wait / wake for background tasks (Phase 3).

A task can park itself with one of the WAIT_TOOLS ("wait for a reply to
this email", "wait until Monday 9am"). It stops using compute: the loop saves
a checkpoint and exits, the lease is released, and `waiting_for` records the
condition. `watcher_loop()` checks parked tasks every minute and wakes them —
appending what happened to the checkpoint as a user message and re-queueing
the task — when the condition is met or the wait expires.

v1 checks conditions by polling only the specific Gmail/Slack threads that
tasks are waiting on, with the user's existing connections. Push (Gmail
Pub/Sub, Slack Events API) can replace the polling later without changing
the task side.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select

import db as _db
from db_ios import BackgroundTask, TaskEvent

_log = logging.getLogger("events")

WATCH_INTERVAL_S = 60
DEFAULT_WAIT_HOURS = 72
MAX_WAIT_HOURS = 24 * 7
MAX_WAIT_UNTIL_DAYS = 30

WAIT_TOOLS: set[str] = {"wait_for_email_reply", "wait_for_slack_reply", "wait_until"}

_hours = {"type": "number", "description": f"Give up after this many hours (default {DEFAULT_WAIT_HOURS}, max {MAX_WAIT_HOURS})."}
WAIT_TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "wait_for_email_reply",
        "description": "Pause this task until someone replies in a Gmail thread (e.g. after gmail_send, "
                       "which returns the thread_id). You'll be resumed with the reply, or told if none arrived in time.",
        "parameters": {"type": "object", "properties": {
            "thread_id": {"type": "string"}, "max_hours": _hours,
        }, "required": ["thread_id"]},
    }},
    {"type": "function", "function": {
        "name": "wait_for_slack_reply",
        "description": "Pause this task until someone replies in a Slack thread (slack_send_message returns "
                       "channel_id and ts). You'll be resumed with the reply, or told if none arrived in time.",
        "parameters": {"type": "object", "properties": {
            "channel_id": {"type": "string"}, "thread_ts": {"type": "string"}, "max_hours": _hours,
        }, "required": ["channel_id", "thread_ts"]},
    }},
    {"type": "function", "function": {
        "name": "wait_until",
        "description": f"Pause this task until a specific time (ISO 8601 with timezone offset), at most {MAX_WAIT_UNTIL_DAYS} days ahead.",
        "parameters": {"type": "object", "properties": {
            "time": {"type": "string", "description": "e.g. 2026-10-05T09:00:00-07:00"},
        }, "required": ["time"]},
    }},
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_wait(name: str, args: dict[str, Any], now: datetime | None = None) -> tuple[dict | None, str]:
    """Validate a wait tool call. Returns (waiting_for, message_for_llm);
    waiting_for is None when the arguments are invalid."""
    now = now or _now()
    if name == "wait_until":
        try:
            at = datetime.fromisoformat(str(args.get("time", "")).replace("Z", "+00:00"))
        except ValueError:
            return None, "Invalid time — use ISO 8601 with a timezone offset."
        if at.tzinfo is None:
            return None, "The time needs a timezone offset (e.g. -07:00)."
        if at <= now or at - now > timedelta(days=MAX_WAIT_UNTIL_DAYS):
            return None, f"The time must be in the future and within {MAX_WAIT_UNTIL_DAYS} days."
        return {"type": "time", "wake_at": at.astimezone(timezone.utc).isoformat()}, \
            f"Paused until {at.isoformat()}. You'll be resumed then."

    try:
        hours = float(args.get("max_hours") or DEFAULT_WAIT_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_WAIT_HOURS
    hours = min(max(hours, 0.1), MAX_WAIT_HOURS)
    expires = (now + timedelta(hours=hours)).isoformat()
    if name == "wait_for_email_reply":
        thread_id = str(args.get("thread_id") or "").strip()
        if not thread_id:
            return None, "thread_id is required."
        return {"type": "gmail_reply", "thread_id": thread_id,
                "since_ms": int(now.timestamp() * 1000), "expires_at": expires}, \
            f"Paused until a reply arrives in thread {thread_id} (up to {hours:g}h)."
    if name == "wait_for_slack_reply":
        channel, ts = str(args.get("channel_id") or "").strip(), str(args.get("thread_ts") or "").strip()
        if not channel or not ts:
            return None, "channel_id and thread_ts are required."
        return {"type": "slack_reply", "channel_id": channel, "thread_ts": ts,
                "since_ts": f"{now.timestamp():.6f}", "expires_at": expires}, \
            f"Paused until a reply arrives in that Slack thread (up to {hours:g}h)."
    return None, f"unknown wait tool {name}"


# ── Condition checks ──────────────────────────────────────────────────────────

async def _gmail_reply(db, user_id: int, wait: dict) -> str | None:
    """Text describing a new reply in the thread since the wait began, or None."""
    import connectors
    from connectors import google
    from proxy import get_http

    token = await connectors.get_token(db, user_id, "google")
    if token is None:
        return None
    thread = await google._gmail_request(
        "GET", f"/users/me/threads/{wait['thread_id']}", token, get_http(), db,
        params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
    )
    for msg in thread.get("messages") or []:
        if int(msg.get("internalDate") or 0) <= wait["since_ms"] or "SENT" in (msg.get("labelIds") or []):
            continue
        headers = {h["name"]: h["value"] for h in (msg.get("payload") or {}).get("headers") or []}
        return (f"New email reply in thread {wait['thread_id']} from {headers.get('From', 'unknown')}: "
                f"{msg.get('snippet', '')}")
    return None


async def _slack_reply(db, user_id: int, wait: dict) -> str | None:
    import connectors
    from connectors import slack
    from proxy import get_http

    token = await connectors.get_token(db, user_id, "slack")
    if token is None:
        return None
    me = ((token.extra_json or {}).get("authed_user") or {}).get("id")
    res = await slack._slack_get(get_http(), slack._resolve_token(token), "conversations.replies", {
        "channel": wait["channel_id"], "ts": wait["thread_ts"], "oldest": wait["since_ts"],
    })
    for m in res.get("messages") or []:
        if m.get("ts") == wait["thread_ts"] or float(m.get("ts", 0)) <= float(wait["since_ts"]):
            continue
        if me and m.get("user") == me:
            continue
        return f"New Slack reply in the thread from <@{m.get('user', '?')}>: {m.get('text', '')[:1000]}"
    return None


async def check_wait(db, task: BackgroundTask, now: datetime | None = None) -> str | None:
    """Message to resume the task with, or None to keep waiting."""
    now = now or _now()
    wait = task.waiting_for or {}
    if wait.get("type") == "time":
        if datetime.fromisoformat(wait["wake_at"]) <= now:
            return f"[Resumed] It is now {now.isoformat()} — the time you waited for. Continue the task."
        return None
    checker = {"gmail_reply": _gmail_reply, "slack_reply": _slack_reply}.get(wait.get("type"))
    if checker is None:
        return "[Resumed] The wait condition was invalid. Continue without it."
    try:
        found = await checker(db, task.user_id, wait)
    except Exception:
        _log.warning("wait check failed for task %s", task.id, exc_info=True)
        found = None
    if found:
        return f"[Resumed] {found}\nContinue the task."
    if datetime.fromisoformat(wait["expires_at"]) <= now:
        return "[Resumed] No reply arrived before the wait expired. Decide the next step (follow up, or finish and tell the user)."
    return None


# ── Park / wake ───────────────────────────────────────────────────────────────

def parked_filter():
    return and_(
        BackgroundTask.status == "running",
        BackgroundTask.lease_owner.is_(None),
        BackgroundTask.waiting_for.is_not(None),
    )


async def wake(task_id: str, message: str) -> bool:
    """Re-queue a parked task with `message` appended to its checkpoint.
    Row-locked so two watchers (replicas) can't both wake it."""
    async with _db.SessionLocal() as db:
        task = (await db.execute(
            select(BackgroundTask).where(BackgroundTask.id == task_id, parked_filter())
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if task is None:
            return False
        cp = dict(task.checkpoint or {})
        cp["messages"] = list(cp.get("messages") or []) + [{"role": "user", "content": message}]
        task.checkpoint = cp
        waited = task.waiting_for
        task.waiting_for = None
        task.status = "queued"
        task.attempt = 0  # a wait isn't a failure; don't count it toward MAX_ATTEMPTS
        task.updated_at = _now()
        last = (await db.execute(
            select(TaskEvent.seq).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq.desc()).limit(1)
        )).scalar_one_or_none()
        db.add(TaskEvent(task_id=task_id, seq=(last + 1) if last is not None else 0,
                         kind="step_start", data={"step": "woken", "waited_for": waited, "message": message[:500]}))
        await db.commit()
    import task_runtime
    task_runtime.wake()
    return True


async def check_parked_once() -> int:
    """One watcher pass. Returns how many tasks were woken."""
    async with _db.SessionLocal() as db:
        parked = (await db.execute(select(BackgroundTask).where(parked_filter()))).scalars().all()
        due: list[tuple[str, str]] = []
        for task in parked:
            msg = await check_wait(db, task)
            if msg:
                due.append((task.id, msg))
    woken = 0
    for task_id, msg in due:
        if await wake(task_id, msg):
            woken += 1
    return woken


async def watcher_loop() -> None:
    """Runs until cancelled. Launched from main.py's lifespan."""
    _log.info("event watcher started (%ss)", WATCH_INTERVAL_S)
    while True:
        try:
            n = await check_parked_once()
            if n:
                _log.info("woke %d parked task(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("event watcher pass failed")
        await asyncio.sleep(WATCH_INTERVAL_S)
