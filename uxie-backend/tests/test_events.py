"""Phase 3: wait tools park a task, the watcher wakes it, the loop resumes."""
from __future__ import annotations

import copy
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import db as db_module
import events
import tasks
from db import User
from db_ios import BackgroundTask
from tests.conftest import _TestSessionLocal

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    monkeypatch.setattr(db_module, "SessionLocal", _TestSessionLocal)
    monkeypatch.setattr(tasks, "SessionLocal", _TestSessionLocal)


def test_build_wait_validation():
    w, _ = events.build_wait("wait_until", {"time": "2026-10-02T09:00:00-07:00"}, NOW)
    assert w == {"type": "time", "wake_at": "2026-10-02T16:00:00+00:00"}
    assert events.build_wait("wait_until", {"time": "2026-09-01T09:00:00Z"}, NOW)[0] is None      # past
    assert events.build_wait("wait_until", {"time": "2026-10-02T09:00:00"}, NOW)[0] is None       # no tz
    assert events.build_wait("wait_until", {"time": "2027-01-01T00:00:00Z"}, NOW)[0] is None      # too far
    assert events.build_wait("wait_for_email_reply", {}, NOW)[0] is None
    w, _ = events.build_wait("wait_for_email_reply", {"thread_id": "t1", "max_hours": 9999}, NOW)
    assert w["expires_at"] == (NOW + timedelta(hours=events.MAX_WAIT_HOURS)).isoformat()


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _ScriptedHttp:
    def __init__(self, replies):
        self.replies, self.payloads = list(replies), []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(copy.deepcopy(json))  # the loop keeps appending to messages
        return _Resp({"choices": [{"message": self.replies.pop(0)}]})


async def _new_task() -> tuple[int, str]:
    async with _TestSessionLocal() as s:
        u = User(email=f"ev-{secrets.token_hex(4)}@x.com", referral_code=secrets.token_hex(4))
        s.add(u)
        await s.commit()
        t = BackgroundTask(id=f"t_{secrets.token_hex(5)}", user_id=u.id, prompt="remind", status="running")
        s.add(t)
        await s.commit()
        return u.id, t.id


async def _get(tid):
    async with _TestSessionLocal() as s:
        return (await s.execute(select(BackgroundTask).where(BackgroundTask.id == tid))).scalar_one()


def _patch_llm(monkeypatch, http):
    schema = [{"type": "function", "function": {"name": "gmail_search", "description": "search"}}]

    async def _schemas(db, user_id):
        return schema

    monkeypatch.setattr(tasks, "_allowed_tool_schemas", _schemas)
    monkeypatch.setattr(tasks, "get_http", lambda: http)
    monkeypatch.setattr(tasks, "_llm_base_and_key", lambda p: ("http://llm", "k"))


async def test_park_wake_resume_cycle(monkeypatch):
    uid, tid = await _new_task()
    wake_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    http = _ScriptedHttp([
        {"content": None, "tool_calls": [{"id": "w1", "type": "function",
            "function": {"name": "wait_until", "arguments": f'{{"time": "{wake_at}"}}'}}]},
        {"content": "Reminder sent."},
    ])
    _patch_llm(monkeypatch, http)

    await tasks._run_task_loop(tid, uid, "remind")
    row = await _get(tid)
    assert row.status == "running" and row.waiting_for["type"] == "time"
    assert len(http.payloads) == 1  # parked, no further LLM calls

    assert await events.check_parked_once() == 0  # not due yet

    monkeypatch.setattr(events, "_now", lambda: datetime.now(timezone.utc) + timedelta(hours=3))
    assert await events.check_parked_once() == 1
    row = await _get(tid)
    assert row.status == "queued" and row.waiting_for is None and row.attempt == 0
    assert await events.wake(tid, "again") is False  # already woken; no double wake

    await tasks._run_task_loop(tid, uid, "remind")
    row = await _get(tid)
    assert row.status == "completed" and row.result_md == "Reminder sent."
    resumed = http.payloads[1]["messages"]
    assert resumed[-1]["role"] == "user" and resumed[-1]["content"].startswith("[Resumed]")


async def test_gmail_reply_check_and_expiry(monkeypatch):
    import connectors
    from connectors import google

    uid, tid = await _new_task()
    since_ms = int(NOW.timestamp() * 1000)
    thread = {"messages": [
        {"internalDate": str(since_ms - 1000), "labelIds": ["SENT"], "snippet": "our email"},
        {"internalDate": str(since_ms + 5000), "labelIds": ["SENT"], "snippet": "our follow-up"},
    ]}

    async def _token(db, user_id, provider):
        return object()

    async def _req(method, path, token, http, db, **kw):
        return thread

    monkeypatch.setattr(connectors, "get_token", _token)
    monkeypatch.setattr(google, "_gmail_request", _req)
    wait = {"type": "gmail_reply", "thread_id": "th1", "since_ms": since_ms,
            "expires_at": (NOW + timedelta(hours=1)).isoformat()}
    task = BackgroundTask(id=tid, user_id=uid, waiting_for=wait)

    assert await events.check_wait(None, task, NOW) is None  # only our own messages
    thread["messages"].append({"internalDate": str(since_ms + 9000), "labelIds": ["INBOX"], "snippet": "Tuesday works",
                               "payload": {"headers": [{"name": "From", "value": "Sarah <s@x.com>"}]}})
    msg = await events.check_wait(None, task, NOW)
    assert "Sarah" in msg and "Tuesday works" in msg

    thread["messages"].pop()
    expired = await events.check_wait(None, task, NOW + timedelta(hours=2))
    assert "No reply arrived" in expired


async def test_list_and_stream_show_waiting():
    uid, tid = await _new_task()
    async with _TestSessionLocal() as s:
        row = (await s.execute(select(BackgroundTask).where(BackgroundTask.id == tid))).scalar_one()
        before = await tasks._task_snapshot(s, uid)
        row.waiting_for = {"type": "time", "wake_at": NOW.isoformat()}
        await s.commit()
        after = await tasks._task_snapshot(s, uid)
    [change] = tasks._snapshot_changes(before, after)
    assert change["waiting"] is True
