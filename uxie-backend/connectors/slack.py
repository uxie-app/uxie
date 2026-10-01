"""
Slack connector — server-side, async, httpx-based.

Adapted from miniflow-engine/connectors/slack.py. Differences:
  - Async (uses shared httpx client from proxy.get_http) instead of sync slack_sdk
  - Token comes from db_ios.OAuthToken row, not a local file
  - No new dependencies (httpx is already in requirements.txt)

Slack API uses a `user_token` (xoxp-...) which lives in OAuthToken.extra_json
under "authed_user.access_token", per Slack's OAuth v2 response shape. We fall
back to OAuthToken.access_token (xoxb-... bot token) if no user token is present.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from db_ios import OAuthToken

log = logging.getLogger("connectors.slack")

PROVIDER = "slack"

TOOLS = [
    {"type": "function", "function": {
        "name": "slack_send_message",
        "description": "Send a message to a Slack channel or user.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string", "description": "#channel-name, @user, or channel ID"},
            "text": {"type": "string"},
        }, "required": ["channel", "text"]},
    }},
    {"type": "function", "function": {
        "name": "slack_search",
        "description": "Search messages in Slack.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "slack_list_channels",
        "description": "List Slack channels the user is in.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "slack_read_channel",
        "description": "Read recent messages from a Slack channel.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        }, "required": ["channel"]},
    }},
]


def _resolve_token(token: OAuthToken) -> str:
    """Pick the right Slack token. User token (xoxp-) preferred for richer
    permissions; fall back to bot token (xoxb-)."""
    extra = token.extra_json or {}
    authed_user = (extra or {}).get("authed_user") or {}
    user_token = authed_user.get("access_token")
    return user_token or token.access_token


async def _slack_get(http: httpx.AsyncClient, slack_token: str, method: str, params: dict | None = None) -> dict:
    resp = await http.get(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {slack_token}"},
        params=params or {},
    )
    resp.raise_for_status()
    return resp.json()


async def _slack_post(http: httpx.AsyncClient, slack_token: str, method: str, body: dict) -> dict:
    resp = await http.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {slack_token}", "Content-Type": "application/json; charset=utf-8"},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


async def _resolve_channel(http: httpx.AsyncClient, slack_token: str, channel: str) -> str:
    """Accept #channel-name, @user, channel-name, or a channel ID (Cxxxx / Dxxxx)."""
    if channel.startswith(("C", "D", "G")) and len(channel) >= 9 and channel[1:].isalnum():
        return channel  # already an ID
    name = channel.lstrip("#").lstrip("@")
    try:
        res = await _slack_get(http, slack_token, "conversations.list",
                               {"types": "public_channel,private_channel,im,mpim", "limit": 200})
        for ch in res.get("channels", []):
            if ch.get("name") == name:
                return ch["id"]
    except Exception:
        log.warning("slack: conversations.list failed during channel resolve", exc_info=True)
    return channel  # last-ditch: pass it through and let Slack reject


async def execute(name: str, args: dict[str, Any], token: OAuthToken, http: httpx.AsyncClient) -> tuple[bool, Any]:
    slack_token = _resolve_token(token)
    try:
        if name == "slack_send_message":
            channel_id = await _resolve_channel(http, slack_token, args["channel"])
            res = await _slack_post(http, slack_token, "chat.postMessage",
                                    {"channel": channel_id, "text": args["text"]})
            if not res.get("ok"):
                return False, f"Slack rejected: {res.get('error', 'unknown')}"
            # channel_id + ts let a background task wait_for_slack_reply on it.
            return True, f"Message sent to {args['channel']} (channel_id: {channel_id}, ts: {res.get('ts', '?')})."

        if name == "slack_search":
            res = await _slack_get(http, slack_token, "search.messages",
                                   {"query": args["query"], "count": 5})
            if not res.get("ok"):
                return False, f"Slack rejected: {res.get('error', 'unknown')}"
            matches = (res.get("messages") or {}).get("matches") or []
            if not matches:
                return True, "No messages found."
            lines = [
                f"[#{(m.get('channel') or {}).get('name','')}] "
                f"{m.get('username','')}: {m.get('text','')}"
                for m in matches
            ]
            return True, "\n".join(lines)

        if name == "slack_list_channels":
            res = await _slack_get(http, slack_token, "conversations.list",
                                   {"types": "public_channel,private_channel",
                                    "exclude_archived": "true", "limit": 100})
            if not res.get("ok"):
                return False, f"Slack rejected: {res.get('error', 'unknown')}"
            channels = res.get("channels") or []
            if not channels:
                return True, "No channels found."
            return True, "\n".join(f"#{c['name']} (ID:{c['id']})" for c in channels)

        if name == "slack_read_channel":
            channel_id = await _resolve_channel(http, slack_token, args["channel"])
            res = await _slack_get(http, slack_token, "conversations.history",
                                   {"channel": channel_id, "limit": args.get("limit", 10)})
            if not res.get("ok"):
                return False, f"Slack rejected: {res.get('error', 'unknown')}"
            msgs = res.get("messages") or []
            if not msgs:
                return True, "No messages."
            lines = [
                f"{m.get('username') or m.get('user', 'unknown')}: {m.get('text','')}"
                for m in reversed(msgs)
            ]
            return True, "\n".join(lines)

        return False, f"unknown slack tool: {name}"

    except httpx.HTTPError as e:
        log.error(f"[slack/{name}] HTTP error: {e}")
        return False, f"network error: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception(f"[slack/{name}] unexpected error")
        return False, str(e)
