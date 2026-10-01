"""
Google connector — Gmail (v1). Calendar and Drive deliberately deferred until
the OAuth flow + Gmail tools are verified end-to-end on iOS.

Adapted from miniflow-engine/connectors/google.py with three changes:
  - Async (httpx via proxy.get_http) instead of the sync google-api-python-client
    library, so we don't add a heavy dependency to Railway just for one connector.
  - Token comes from db_ios.OAuthToken row, not a local file.
  - Auto-refresh access_token on 401: Gmail short-lived tokens expire after ~1
    hour; we use the stored refresh_token to mint a new one and retry once.
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.text import MIMEText
from typing import Any

import httpx

from db_ios import OAuthToken
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("connectors.google")

PROVIDER = "google"

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


# ── Tool schemas ──────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "gmail_send",
        "description": "Send an email via Gmail. USE THIS when the user says 'send', 'email X saying...', 'mail X about...', or any phrasing that implies the email should actually be delivered. The user will see an approval sheet before it fires, so you don't need to ask for confirmation in chat — just call this tool.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["to", "subject", "body"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_search",
        "description": "Search Gmail. Returns a list of message IDs + From + Subject.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail search query (same syntax as the web UI)"},
            "limit": {"type": "integer", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_read",
        "description": "Read a Gmail message by ID. Returns From, Subject, and the first ~2500 chars of the body.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "gmail_draft",
        "description": "Create a Gmail draft only — no email is sent. ONLY use this when the user explicitly says 'draft', 'save a draft', or 'don't send yet'. Otherwise prefer gmail_send.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["to", "subject", "body"]},
    }},
    # ── Calendar ──────────────────────────────────────────────────────────────
    {"type": "function", "function": {
        "name": "calendar_list_events",
        "description": "List upcoming Google Calendar events on the user's primary calendar. USE THIS when the user asks about their schedule, meetings, calendar, or 'what's coming up'. Returns a list of events with title, start time, end time, and attendees.",
        "parameters": {"type": "object", "properties": {
            "days_ahead": {"type": "integer", "default": 7, "description": "How many days to look ahead from now. Use 1 for today, 7 for the week, 30 for the month."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "calendar_check_availability",
        "description": "Check the user's free/busy status on a specific date or datetime. USE THIS when the user asks 'am I free at X' or 'is Tuesday open'. Returns busy time ranges.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "ISO 8601 date or datetime (e.g. 2026-05-23 or 2026-05-23T14:00:00-07:00)"},
            "duration_hours": {"type": "number", "default": 1},
        }, "required": ["date"]},
    }},
    # ── Drive ─────────────────────────────────────────────────────────────────
    {"type": "function", "function": {
        "name": "drive_search",
        "description": "Search Google Drive for files matching a query. Returns matching files with id, name, and mimeType.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Free text search across file content + metadata"},
            "limit": {"type": "integer", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "drive_read",
        "description": "Export a Google Drive file (Doc / Sheet / Slide) as plain text. Use AFTER drive_search to read a specific file's contents.",
        "parameters": {"type": "object", "properties": {
            "fileId": {"type": "string"},
        }, "required": ["fileId"]},
    }},
]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_mime(to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
    msg = MIMEText(body)
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    out: dict[str, Any] = {"raw": raw}
    if thread_id:
        out["threadId"] = thread_id
    return out


def _decode_body(payload: dict) -> str:
    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            d = part.get("body", {}).get("data")
            if d:
                return base64.urlsafe_b64decode(d).decode("utf-8", errors="replace")
    return ""


async def _refresh_access_token(http: httpx.AsyncClient, token: OAuthToken, db: AsyncSession) -> str | None:
    """Mint a fresh access token from the stored refresh_token. Persists it
    to the OAuthToken row so subsequent calls don't need to refresh."""
    if not token.refresh_token:
        return None
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    try:
        resp = await http.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": token.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            log.warning("Google refresh failed (%d): %s", resp.status_code, resp.text[:200])
            return None
        new = resp.json()
        token.access_token = new["access_token"]
        await db.commit()
        return token.access_token
    except Exception as e:  # noqa: BLE001
        log.warning("Google refresh exception: %s", e)
        return None


async def _google_request(
    method: str,
    url: str,
    token: OAuthToken,
    http: httpx.AsyncClient,
    db: AsyncSession,
    *,
    params: dict | None = None,
    json: dict | None = None,
) -> dict:
    """One-shot Google API call with automatic 401-retry-after-refresh.
    Generic across Gmail / Calendar / Drive — caller passes the absolute URL."""
    async def _call(access_token: str) -> httpx.Response:
        return await http.request(
            method, url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            json=json,
            timeout=30,
        )
    resp = await _call(token.access_token)
    if resp.status_code == 401:
        new_token = await _refresh_access_token(http, token, db)
        if new_token:
            resp = await _call(new_token)
    if resp.status_code >= 400:
        raise RuntimeError(f"Google {method} {url} → {resp.status_code}: {resp.text[:200]}")
    # Some Drive export endpoints return text, not JSON.
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        return resp.json()
    return {"_raw_text": resp.text}


async def _gmail_request(
    method: str,
    path: str,
    token: OAuthToken,
    http: httpx.AsyncClient,
    db: AsyncSession,
    *,
    params: dict | None = None,
    json: dict | None = None,
) -> dict:
    """Backwards-compatible wrapper — Gmail-specific path prefix."""
    return await _google_request(
        method, f"{GMAIL_API}{path}", token, http, db, params=params, json=json,
    )


# ── Execute ──────────────────────────────────────────────────────────────────


async def execute(
    name: str,
    args: dict[str, Any],
    token: OAuthToken,
    http: httpx.AsyncClient,
    db: AsyncSession | None = None,
) -> tuple[bool, Any]:
    """Dispatch a Gmail tool call. The connector registry passes us
    (name, args, OAuthToken row, shared httpx client). We pass `db` through
    so we can persist refreshed tokens.

    The registry's current signature doesn't include `db` yet — we accept it
    optionally so a future registry change can pass it. For now refresh-on-401
    is best-effort: it runs the request again with the new token but doesn't
    persist if db is None.
    """
    try:
        if name == "gmail_send":
            sent = await _gmail_request(
                "POST", "/users/me/messages/send", token, http, db,
                json=_make_mime(args["to"], args["subject"], args["body"]),
            )
            # thread_id lets a background task wait_for_email_reply on it.
            return True, f"Email sent to {args['to']} (thread_id: {sent.get('threadId', '?')})."

        elif name == "gmail_search":
            res = await _gmail_request(
                "GET", "/users/me/messages", token, http, db,
                params={"q": args["query"], "maxResults": args.get("limit", 5)},
            )
            msgs = res.get("messages", [])
            if not msgs:
                return True, "No emails found."
            lines = []
            for m in msgs:
                hdr = await _gmail_request(
                    "GET", f"/users/me/messages/{m['id']}", token, http, db,
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
                )
                headers = {h["name"]: h["value"] for h in hdr.get("payload", {}).get("headers", [])}
                lines.append(
                    f"ID:{m['id']}  From:{headers.get('From','')}  "
                    f"Subject:{headers.get('Subject','')}"
                )
            return True, "\n".join(lines)

        elif name == "gmail_read":
            msg = await _gmail_request(
                "GET", f"/users/me/messages/{args['id']}", token, http, db,
                params={"format": "full"},
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            body = _decode_body(msg.get("payload", {}))
            return True, (
                f"From: {headers.get('From','')}\n"
                f"Subject: {headers.get('Subject','')}\n"
                f"ThreadID: {msg.get('threadId','')}\n\n"
                f"{body[:2500]}"
            )

        elif name == "gmail_draft":
            await _gmail_request(
                "POST", "/users/me/drafts", token, http, db,
                json={"message": _make_mime(args["to"], args["subject"], args["body"])},
            )
            return True, f"Draft created for {args['to']}."

        # ── Calendar ─────────────────────────────────────────────────────────
        elif name == "calendar_list_events":
            from datetime import datetime, timedelta, timezone as _tz
            now = datetime.now(_tz.utc)
            end = now + timedelta(days=int(args.get("days_ahead", 7)))
            res = await _google_request(
                "GET", f"{CALENDAR_API}/calendars/primary/events", token, http, db,
                params={
                    "timeMin": now.isoformat().replace("+00:00", "Z"),
                    "timeMax": end.isoformat().replace("+00:00", "Z"),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 50,
                },
            )
            events = res.get("items", [])
            if not events:
                return True, "No upcoming events in that window."
            lines = []
            for e in events:
                start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""
                title = e.get("summary", "(No title)")
                attendees = [a.get("email", "") for a in e.get("attendees", []) if a.get("email")]
                link = e.get("hangoutLink") or ""
                lines.append(
                    f"- {start}  {title}"
                    + (f"  (attendees: {', '.join(attendees[:5])})" if attendees else "")
                    + (f"  {link}" if link else "")
                )
            return True, "\n".join(lines)

        elif name == "calendar_check_availability":
            from datetime import datetime, timedelta, timezone as _tz
            try:
                dt = datetime.fromisoformat(args["date"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
            except Exception:
                dt = datetime.now(_tz.utc)
            window = timedelta(hours=float(args.get("duration_hours", 1)))
            res = await _google_request(
                "POST", f"{CALENDAR_API}/freeBusy", token, http, db,
                json={
                    "timeMin": dt.isoformat(),
                    "timeMax": (dt + window).isoformat(),
                    "items": [{"id": "primary"}],
                },
            )
            busy = res.get("calendars", {}).get("primary", {}).get("busy", [])
            if not busy:
                return True, f"Calendar is free from {args['date']} for {window.total_seconds()/3600}h."
            return True, "Busy:\n" + "\n".join(f"  {b['start']} – {b['end']}" for b in busy)

        # ── Drive ────────────────────────────────────────────────────────────
        elif name == "drive_search":
            q = f"fullText contains '{args['query']}' and trashed = false"
            res = await _google_request(
                "GET", f"{DRIVE_API}/files", token, http, db,
                params={"q": q, "pageSize": int(args.get("limit", 5)),
                        "fields": "files(id,name,mimeType)"},
            )
            files = res.get("files", [])
            if not files:
                return True, "No files found."
            return True, "\n".join(
                f"- {f['name']} (id={f['id']}, type={f['mimeType']})" for f in files
            )

        elif name == "drive_read":
            res = await _google_request(
                "GET", f"{DRIVE_API}/files/{args['fileId']}/export", token, http, db,
                params={"mimeType": "text/plain"},
            )
            text = res.get("_raw_text") or ""
            return True, text[:5000]

        return False, f"Unknown Google tool: {name}"

    except Exception as e:  # noqa: BLE001
        log.error("[google/%s] %s", name, e)
        return False, str(e)
