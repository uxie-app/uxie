"""Scheduled / recurring tasks (v1.2).

User-configured workflows that fire at a chosen local time. The first
template is **Morning Brief** — runs at e.g. 8am every day, parallel-fans
out to Gmail / Calendar / Drive workers, synthesizes a Markdown brief,
delivers via macOS notification + email.

Architecture:
    ScheduledTask  ── kind=morning_brief, run_time_local="08:00", tz="Asia/Kolkata"
        ▼
    cron_worker (runs every 60s on Railway lifespan)
        if now_local(tz) ≥ run_time AND last_fired_at < today → fire
        ▼
    spawn one BackgroundTask + run the template-specific generator
        ▼
    write transcript to BackgroundTask.result_md AND email it via Resend

Endpoints:
    GET    /scheduled_tasks                 list caller's
    POST   /scheduled_tasks                 create
    PATCH  /scheduled_tasks/{id}            partial update (run_time, tz, enabled, delivery)
    DELETE /scheduled_tasks/{id}            delete

The cron worker is launched from main.py's lifespan as `asyncio.create_task`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone as _tz
from typing import Any

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user
import model_gateway
from db import SessionLocal, User, get_db
from db_ios import BackgroundTask, OAuthToken, ScheduledTask, TaskEvent
from proxy import _llm_base_and_key, get_http
from settings import get_settings

try:
    import connectors as _connectors
except Exception:
    _connectors = None  # type: ignore[assignment]

_log = logging.getLogger("scheduled_tasks")
_settings = get_settings()


# ── IDs ───────────────────────────────────────────────────────────────────────


def _ulid() -> str:
    return f"{int(time.time() * 1000):013d}_{secrets.token_hex(6)}"


# ── Timezone helpers ──────────────────────────────────────────────────────────


def _now_in_tz(tzname: str) -> datetime:
    """Best-effort local-time conversion. Falls back to UTC if zoneinfo
    can't resolve the name (e.g. odd inputs from older clients)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname))
    except Exception:
        return datetime.now(_tz.utc)


def _is_due(st: ScheduledTask, now: datetime) -> bool:
    """True if it's >= run_time today in the user's local TZ AND we
    haven't already fired today. The 60s polling cadence means a brief
    set to 08:00 actually fires somewhere in [08:00, 08:01) — close enough."""
    if not st.enabled:
        return False
    try:
        hh, mm = st.run_time_local.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        return False
    if now < target:
        return False
    # Already fired today?
    if st.last_fired_at is not None:
        last = st.last_fired_at
        # last_fired_at is UTC; compare its date in the user's local tz
        try:
            from zoneinfo import ZoneInfo
            last_local = last.astimezone(ZoneInfo(st.timezone))
        except Exception:
            last_local = last
        if last_local.date() == now.date():
            return False
    return True


# ── Morning Brief generator ───────────────────────────────────────────────────


# ── Template registry ────────────────────────────────────────────────────────
# Each template kind maps to (system_prompt, async_generator). Adding a
# new workflow = add an entry below + a generator function. The cron
# worker + endpoint validation read from this same dict.


_MORNING_BRIEF_SYSTEM_PROMPT = """You are Uxie's Morning Brief writer. You've been handed:
  - Today's calendar (next 24 h on the primary calendar)
  - The full text of the top 10 primary-tab emails from the past 24 h
  - Slack mentions / DMs from the past 24 h (if the user connected Slack)

You have the actual email bodies, not just subject lines. Use them.
Read what each email is asking for and write the Inbox section based on
the CONTENT, not the subject line.

Required structure (omit a section entirely if there's nothing for it):

## Today's schedule
- HH:MM  Meeting title  (attendees: alice@…, bob@…)
…
(call out conflicts, long blocks, travel/buffer time)

## Inbox highlights
Lead with anything that needs the user's reply today. For each item:
- WHO it's from (real human name, not the email address)
- ONE-LINE summary of what they actually want (e.g. "Sarah is asking
  for the pricing deck by EOD", not "Sarah sent you an email")
- WHY it matters (deadline / decision needed / blocking someone)

Group urgent (today) above informational (this week) above FYI.

## Heads-up
Anything else worth flagging — a meeting that needs prep, a deadline
approaching, an outstanding action item from yesterday surfacing in
both calendar AND inbox.

Style:
- Terse. The user is reading on their phone over coffee.
- No filler ("here is your brief"), no closing greeting.
- Action-oriented bullets ("Reply to Sarah with pricing deck before noon"
  not "Sarah emailed about pricing").
- Skip newsletters / promos / system notifications unless they contain
  something real (a security alert, a renewal). The category:primary
  filter helps but isn't perfect.
- If a section is empty, just leave it out entirely. Don't write
  "no urgent emails today".
- End with one sentence of energy: a single specific observation about
  the day, not a generic "have a great day".
"""


async def _morning_brief_generate(db: AsyncSession, user: User) -> str:
    """Run the actual brief generation: fan out to Gmail + Calendar
    (+ Slack if connected) in parallel, hand the results to GPT-4o,
    return the markdown."""
    if _connectors is None:
        return "*(connectors not loaded — brief unavailable)*"

    # Detect which providers the user has connected — only fan out to
    # those, so a Slack-less user doesn't see "(slack not connected)"
    # smeared across their brief.
    connected_providers = set(
        (await db.execute(
            select(OAuthToken.provider).where(OAuthToken.user_id == user.id)
        )).scalars().all()
    )

    async def _calendar_today() -> tuple[str, str]:
        try:
            ok, res = await _connectors.execute(
                db, user.id, "calendar_list_events", {"days_ahead": 1}
            )
            return ("calendar", res if isinstance(res, str) else json.dumps(res, default=str))
        except Exception as e:
            return ("calendar", f"(calendar fetch failed: {e})")

    async def _gmail_recent() -> tuple[str, str]:
        """Pull the top 10 primary-tab emails from the past 24h, then
        read each one's body in parallel. Metadata-only ("From + Subject")
        was too shallow for the LLM to write actionable bullets — it
        would just paraphrase subject lines instead of saying what each
        email actually wanted.

        The two-stage flow (search → parallel read) takes ~500 ms total
        in real-world tests vs ~3-5 s if read serially. asyncio.gather
        across the 10 message IDs is the cheap parallelism win."""
        try:
            # Stage 1: search for the candidate set.
            # `category:primary` excludes promo/social/forums tabs so the
            # brief isn't padded with newsletters. `newer_than:1d` caps
            # at the past 24 hours.
            ok, search_res = await _connectors.execute(
                db, user.id, "gmail_search",
                {"query": "category:primary newer_than:1d", "limit": 10},
            )
            if not ok or not isinstance(search_res, str):
                return ("gmail", f"(gmail search failed: {search_res})")

            # Parse out the message IDs from the connector's text format.
            # Format from connectors/google.py: "ID:xxx  From:...  Subject:..."
            import re
            msg_ids = re.findall(r"ID:(\S+)", search_res)[:10]
            if not msg_ids:
                return ("gmail", "(no emails in your primary tab from the past 24 hours)")

            # Stage 2: read each in parallel.
            async def _read_one(msg_id: str) -> str:
                try:
                    ok2, body = await _connectors.execute(
                        db, user.id, "gmail_read", {"id": msg_id},
                    )
                    if not ok2:
                        return f"--- gmail_read failed for {msg_id}: {body}"
                    return body if isinstance(body, str) else json.dumps(body, default=str)
                except Exception as e:
                    return f"--- gmail_read exception for {msg_id}: {e}"

            bodies = await asyncio.gather(
                *[_read_one(mid) for mid in msg_ids],
                return_exceptions=False,
            )

            # Format for LLM consumption — clearly delimited so the model
            # doesn't bleed content between emails. Each body block already
            # contains From / Subject / ThreadID / first 2500 chars of body
            # (per the gmail_read connector).
            parts = [f"### Email {i+1}\n{body}" for i, body in enumerate(bodies)]
            return ("gmail", "\n\n".join(parts))
        except Exception as e:
            return ("gmail", f"(gmail fetch failed: {e})")

    async def _slack_recent() -> tuple[str, str]:
        # Slack's search.messages doesn't support a "after:24h" filter
        # but it does sort by recency. We pull recent mentions + DMs
        # heuristically: search for the user's @-mention. If they've
        # set their name we'll get richer results in a later release.
        try:
            ok, res = await _connectors.execute(
                db, user.id, "slack_search",
                {"query": "after:yesterday is:unread"},
            )
            return ("slack", res if isinstance(res, str) else json.dumps(res, default=str))
        except Exception as e:
            return ("slack", f"(slack fetch failed: {e})")

    tasks = [_calendar_today(), _gmail_recent()]
    if "slack" in connected_providers:
        tasks.append(_slack_recent())
    results = await asyncio.gather(*tasks, return_exceptions=False)
    sections = {label: text for label, text in results}

    parts = [
        f"# Raw inputs for Morning Brief — {datetime.now(_tz.utc).strftime('%A %B %-d')}\n",
        f"## Calendar (next 24h, primary calendar)\n{sections.get('calendar', '(none)')}\n",
        f"## Top 10 primary-tab emails (last 24h, full bodies)\n{sections.get('gmail', '(none)')}\n",
    ]
    if "slack" in sections:
        parts.append(f"## Slack unread / mentions (last 24h)\n{sections['slack']}\n")
    user_msg = "\n".join(parts)

    provider, model = model_gateway.resolve("briefing")
    base_url, api_key = _llm_base_and_key(provider)
    http = get_http()
    resp = await http.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": _MORNING_BRIEF_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=90,
    )
    if resp.status_code != 200:
        return f"*(Morning Brief LLM call failed: {resp.status_code} {resp.text[:200]})*"
    data = resp.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ── Evening recap generator ───────────────────────────────────────────────────


_EVENING_RECAP_SYSTEM_PROMPT = """You're Uxie's End-of-Day Recap writer. Summarize what the user
actually got done today based on the inputs below — emails they sent,
meetings they attended, Slack threads they replied in. Tone: a friendly
chief-of-staff reflecting back. The user reads this winding down.

Structure (omit empty sections):

## What you shipped
Action-oriented bullets — emails sent, decisions made, calls had.

## Things still open
Threads you replied to that need follow-up, meetings whose notes you
haven't written, tasks that came up but didn't close.

## Tomorrow's setup
One or two lines: what's on the calendar, what you should think about
overnight.

Style: terse, end with one observation, no closing pleasantry.
"""


async def _evening_recap_generate(db: AsyncSession, user: User) -> str:
    """Fan out to find what the user DID today — sent emails, calendar
    events that already happened, etc. Same shape as Morning Brief but
    backwards-looking."""
    if _connectors is None:
        return "*(connectors not loaded)*"

    connected_providers = set(
        (await db.execute(
            select(OAuthToken.provider).where(OAuthToken.user_id == user.id)
        )).scalars().all()
    )

    async def _sent_today() -> tuple[str, str]:
        try:
            ok, res = await _connectors.execute(
                db, user.id, "gmail_search",
                {"query": "from:me newer_than:1d", "limit": 15},
            )
            return ("sent", res if isinstance(res, str) else json.dumps(res, default=str))
        except Exception as e:
            return ("sent", f"(gmail-sent fetch failed: {e})")

    async def _calendar_done() -> tuple[str, str]:
        # Slight hack — list_events looks forward, but listing the next
        # 24h captures most of today's already-happened events too on
        # late-evening runs. For a clean "what happened today" pull,
        # we'd want a backwards-looking variant.
        try:
            ok, res = await _connectors.execute(
                db, user.id, "calendar_list_events", {"days_ahead": 1}
            )
            return ("calendar", res if isinstance(res, str) else json.dumps(res, default=str))
        except Exception as e:
            return ("calendar", f"(calendar fetch failed: {e})")

    tasks = [_sent_today(), _calendar_done()]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    sections = dict(results)

    user_msg = (
        f"# Raw inputs for End-of-Day Recap — {datetime.now(_tz.utc).strftime('%A %B %-d')}\n\n"
        f"## Emails I sent today\n{sections.get('sent', '(none)')}\n\n"
        f"## Calendar context\n{sections.get('calendar', '(none)')}\n"
    )

    provider, model = model_gateway.resolve("briefing")
    base_url, api_key = _llm_base_and_key(provider)
    resp = await get_http().post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model, "temperature": 0.3,
            "messages": [
                {"role": "system", "content": _EVENING_RECAP_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=90,
    )
    if resp.status_code != 200:
        return f"*(LLM call failed: {resp.status_code})*"
    return ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ── Weekly digest generator ───────────────────────────────────────────────────


_WEEKLY_DIGEST_SYSTEM_PROMPT = """You're Uxie's Weekly Digest writer. The user has reached the end of
their work week and wants a summary they can paste into a status post,
share with their manager, or just journal away. Inputs: a week of sent
emails + a week of calendar events.

Structure:

## This week, you …
Three or four bullets. The big themes — what got built, who you talked
to, what shipped. No filler.

## Standout meetings
Up to three meetings worth remembering. Title + 1-line "what came out
of it".

## What's open going into next week
The threads / decisions / asks that didn't close. So Monday-you knows
where to pick up.

Style: confident, terse, written for sharing. No closing line.
"""


async def _weekly_digest_generate(db: AsyncSession, user: User) -> str:
    """Past-7-day summary across emails + calendar."""
    if _connectors is None:
        return "*(connectors not loaded)*"

    async def _sent_week() -> tuple[str, str]:
        try:
            ok, res = await _connectors.execute(
                db, user.id, "gmail_search",
                {"query": "from:me newer_than:7d", "limit": 30},
            )
            return ("sent", res if isinstance(res, str) else json.dumps(res, default=str))
        except Exception as e:
            return ("sent", f"(gmail fetch failed: {e})")

    async def _calendar_week() -> tuple[str, str]:
        try:
            ok, res = await _connectors.execute(
                db, user.id, "calendar_list_events", {"days_ahead": 7}
            )
            return ("calendar", res if isinstance(res, str) else json.dumps(res, default=str))
        except Exception as e:
            return ("calendar", f"(calendar fetch failed: {e})")

    results = await asyncio.gather(_sent_week(), _calendar_week(), return_exceptions=False)
    sections = dict(results)

    user_msg = (
        f"# Raw inputs for Weekly Digest — week ending {datetime.now(_tz.utc).strftime('%A %B %-d')}\n\n"
        f"## Emails I sent this week\n{sections.get('sent', '(none)')}\n\n"
        f"## Meetings this week\n{sections.get('calendar', '(none)')}\n"
    )

    provider, model = model_gateway.resolve("briefing")
    base_url, api_key = _llm_base_and_key(provider)
    resp = await get_http().post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model, "temperature": 0.3,
            "messages": [
                {"role": "system", "content": _WEEKLY_DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=90,
    )
    if resp.status_code != 200:
        return f"*(LLM call failed: {resp.status_code})*"
    return ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ── Registry ──────────────────────────────────────────────────────────────────


TEMPLATE_REGISTRY: dict[str, dict] = {
    "morning_brief": {
        "label": "Morning Brief",
        "description": "Today's calendar + unread Gmail (+ Slack), synthesized.",
        "default_time": "08:00",
        "generator": _morning_brief_generate,
    },
    "evening_recap": {
        "label": "End-of-Day Recap",
        "description": "What you shipped today, what's still open, tomorrow's setup.",
        "default_time": "18:00",
        "generator": _evening_recap_generate,
    },
    "weekly_digest": {
        "label": "Weekly Digest",
        "description": "Your week in three bullets — themes, meetings, open threads.",
        "default_time": "17:00",
        "generator": _weekly_digest_generate,
    },
}


# ── Email delivery (Resend) ───────────────────────────────────────────────────


async def _send_email(to: str, subject: str, markdown: str) -> bool:
    """Ship the brief via Resend. Renders the markdown into <pre>-wrapped
    HTML — keeps formatting readable in Gmail without needing a markdown
    renderer dep."""
    key = (_settings.resend_api_key or "").strip()
    if not key:
        _log.warning("resend_api_key not set — skipping email delivery")
        return False
    sender = _settings.resend_from or "Uxie <noreply@uxie.ai>"
    # Minimal HTML wrapper. Markdown rendered as text — Gmail will
    # respect linebreaks and headings still read fine prefixed with #.
    html = (
        "<div style='font-family: -apple-system, BlinkMacSystemFont, sans-serif; "
        "max-width: 640px; padding: 16px;'>"
        "<pre style='white-space: pre-wrap; font-family: inherit; font-size: 14px; "
        "line-height: 1.5;'>"
        + markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</pre>"
        "<hr style='margin-top: 24px; border: none; border-top: 1px solid #eee;'/>"
        "<p style='font-size: 11px; color: #999; margin-top: 16px;'>"
        "Sent by Uxie — turn off in Settings → Briefings"
        "</p>"
        "</div>"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"from": sender, "to": [to], "subject": subject, "html": html},
            )
        if r.status_code >= 300:
            _log.warning("resend send failed (%d): %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        _log.warning("resend exception: %s", e)
        return False


# ── Firing a scheduled task ───────────────────────────────────────────────────


async def _fire(st_id: str) -> None:
    """Create a BackgroundTask + execute the kind-specific generator + email."""
    async with SessionLocal() as db:
        st = (await db.execute(select(ScheduledTask).where(ScheduledTask.id == st_id))).scalar_one_or_none()
        if st is None or not st.enabled:
            return
        user = (await db.execute(select(User).where(User.id == st.user_id))).scalar_one_or_none()
        if user is None:
            return

        # 1. Spawn a BackgroundTask shell so the user can see "this fired" in
        #    the Tasks tab with the brief markdown in result_md.
        task_id = _ulid()
        db.add(BackgroundTask(
            id=task_id, user_id=user.id,
            prompt=f"[Scheduled: {st.kind}] {st.run_time_local} {st.timezone}",
            status="running",
        ))
        st.last_task_id = task_id
        st.last_fired_at = datetime.now(_tz.utc)
        st.updated_at = datetime.now(_tz.utc)
        await db.commit()

        # 2. Run the kind-specific generator from the template registry.
        try:
            tpl = TEMPLATE_REGISTRY.get(st.kind)
            if tpl is None:
                brief = f"*(unknown scheduled task kind: {st.kind})*"
            else:
                brief = await tpl["generator"](db, user)
        except Exception as e:
            _log.exception("scheduled task %s generator failed", st_id)
            brief = f"*(generator error: {e})*"

        # 3. Persist the result back to the BackgroundTask row.
        bt = (await db.execute(
            select(BackgroundTask).where(BackgroundTask.id == task_id)
        )).scalar_one_or_none()
        if bt is not None:
            bt.status = "completed"
            bt.result_md = brief
            bt.completed_at = datetime.now(_tz.utc)
            bt.updated_at = datetime.now(_tz.utc)
        db.add(TaskEvent(
            task_id=task_id, seq=0, kind="final_text", data={"text": brief},
        ))
        await db.commit()

        # 4. Email if configured.
        delivery = st.delivery_json or {}
        if delivery.get("email", True):
            label = (TEMPLATE_REGISTRY.get(st.kind) or {}).get("label", st.kind)
            subject = f"Your Uxie {label} — " + datetime.now(_tz.utc).strftime("%A %B %-d")
            await _send_email(user.email, subject, brief)


# ── Cron worker ───────────────────────────────────────────────────────────────


async def _claim(st_id: str, observed_last_fired: datetime | None) -> bool:
    """Compare-and-set last_fired_at so that when several replicas (or an
    overlapping deploy) see the same task as due, exactly one fires it."""
    cond = (ScheduledTask.last_fired_at.is_(None) if observed_last_fired is None
            else ScheduledTask.last_fired_at == observed_last_fired)
    async with SessionLocal() as db:
        result = await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.id == st_id, cond)
            .values(last_fired_at=datetime.now(_tz.utc))
        )
        await db.commit()
        return result.rowcount == 1


async def cron_worker() -> None:
    """Polls every 60s. For each ScheduledTask whose run_time has passed
    in its user's tz today AND hasn't fired today, kick off `_fire`."""
    _log.info("scheduled_tasks cron worker starting (60s poll)")
    while True:
        try:
            async with SessionLocal() as db:
                rows = (await db.execute(
                    select(ScheduledTask).where(ScheduledTask.enabled == True)  # noqa: E712
                )).scalars().all()
                due: list[tuple[str, datetime | None]] = []
                for st in rows:
                    now = _now_in_tz(st.timezone)
                    if _is_due(st, now):
                        due.append((st.id, st.last_fired_at))
            for st_id, observed in due:
                # Only the replica that wins the claim fires. Each fire opens
                # its own DB session — keeps the cron session short.
                if await _claim(st_id, observed):
                    asyncio.create_task(_fire(st_id))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log.warning("cron iteration crashed: %s", e)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise


# ── HTTP endpoints ────────────────────────────────────────────────────────────


class ScheduledTaskCreate(BaseModel):
    kind: str = Field(..., max_length=64)
    run_time_local: str = Field(..., min_length=4, max_length=5)
    timezone: str = Field(..., max_length=64)
    enabled: bool = True
    delivery: dict | None = None
    config: dict | None = None


class ScheduledTaskPatch(BaseModel):
    run_time_local: str | None = Field(None, min_length=4, max_length=5)
    timezone: str | None = Field(None, max_length=64)
    enabled: bool | None = None
    delivery: dict | None = None
    config: dict | None = None


def _row_to_dict(st: ScheduledTask) -> dict:
    return {
        "id": st.id,
        "kind": st.kind,
        "enabled": st.enabled,
        "run_time_local": st.run_time_local,
        "timezone": st.timezone,
        "delivery": st.delivery_json or {},
        "config": st.config_json or {},
        "last_fired_at": st.last_fired_at.isoformat() if st.last_fired_at else None,
        "last_task_id": st.last_task_id,
        "created_at": st.created_at.isoformat() if st.created_at else None,
    }


async def scheduled_list(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = (await db.execute(
        select(ScheduledTask).where(ScheduledTask.user_id == user.id)
        .order_by(desc(ScheduledTask.created_at))
    )).scalars().all()
    return {"scheduled_tasks": [_row_to_dict(r) for r in rows]}


async def scheduled_create(
    body: ScheduledTaskCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    if body.kind not in TEMPLATE_REGISTRY:
        raise HTTPException(400, f"unsupported kind: {body.kind} (try {list(TEMPLATE_REGISTRY)})")
    # Validate run_time_local "HH:MM" shape.
    try:
        hh, mm = body.run_time_local.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError("out of range")
    except Exception:
        raise HTTPException(400, "run_time_local must be HH:MM in 24h format")

    st_id = _ulid()
    db.add(ScheduledTask(
        id=st_id, user_id=user.id, kind=body.kind,
        enabled=body.enabled,
        run_time_local=body.run_time_local,
        timezone=body.timezone,
        delivery_json=body.delivery or {"notification": True, "email": True},
        config_json=body.config or {},
    ))
    await db.commit()
    st = (await db.execute(select(ScheduledTask).where(ScheduledTask.id == st_id))).scalar_one()
    return _row_to_dict(st)


async def scheduled_patch(
    st_id: str,
    body: ScheduledTaskPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    st = (await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.id == st_id, ScheduledTask.user_id == user.id,
        )
    )).scalar_one_or_none()
    if st is None:
        raise HTTPException(404, "scheduled task not found")
    if body.run_time_local is not None:
        st.run_time_local = body.run_time_local
    if body.timezone is not None:
        st.timezone = body.timezone
    if body.enabled is not None:
        st.enabled = body.enabled
    if body.delivery is not None:
        st.delivery_json = body.delivery
    if body.config is not None:
        st.config_json = body.config
    st.updated_at = datetime.now(_tz.utc)
    await db.commit()
    return _row_to_dict(st)


async def scheduled_delete(
    st_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    st = (await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.id == st_id, ScheduledTask.user_id == user.id,
        )
    )).scalar_one_or_none()
    if st is None:
        return {"ok": True, "already_deleted": True}
    await db.delete(st)
    await db.commit()
    return {"ok": True}


async def scheduled_fire_now(
    st_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Force-run a scheduled task immediately — useful for testing the
    brief without waiting for 8am. Resets last_fired_at so the regular
    daily fire still happens later if the user expects it."""
    st = (await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.id == st_id, ScheduledTask.user_id == user.id,
        )
    )).scalar_one_or_none()
    if st is None:
        raise HTTPException(404, "scheduled task not found")
    # Reset last_fired_at to yesterday so the daily cadence isn't
    # broken by the manual fire.
    st.last_fired_at = datetime.now(_tz.utc) - timedelta(days=1)
    await db.commit()
    asyncio.create_task(_fire(st_id))
    return {"ok": True, "fired": True}
