"""Durable task runtime: leasing, reclaim, persisted approvals, resume."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import db as db_module
import task_runtime
import tasks
from db import User
from db_ios import BackgroundTask, TaskApproval, TaskEvent
from tests.conftest import _TestSessionLocal, create_user_and_token


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    monkeypatch.setattr(db_module, "SessionLocal", _TestSessionLocal)
    monkeypatch.setattr(tasks, "SessionLocal", _TestSessionLocal)


async def _user() -> int:
    async with _TestSessionLocal() as s:
        u = User(email=f"rt-{secrets.token_hex(4)}@x.com", referral_code=secrets.token_hex(4))
        s.add(u)
        await s.commit()
        return u.id


async def _task(user_id: int, **kw) -> str:
    tid = f"t_{secrets.token_hex(6)}"
    async with _TestSessionLocal() as s:
        s.add(BackgroundTask(id=tid, user_id=user_id, prompt="p", **kw))
        await s.commit()
    return tid


async def _get(tid: str) -> BackgroundTask:
    async with _TestSessionLocal() as s:
        return (await s.execute(select(BackgroundTask).where(BackgroundTask.id == tid))).scalar_one()


async def _drain_queue() -> None:
    """Leave no claimable rows from earlier tests behind."""
    while await task_runtime.claim_one() is not None:
        pass


async def test_claim_leases_queued_task():
    await _drain_queue()
    tid = await _task(await _user(), status="queued")
    claimed = await task_runtime.claim_one()
    assert claimed is not None and claimed.id == tid
    row = await _get(tid)
    assert row.status == "running"
    assert row.lease_owner == task_runtime.WORKER_ID
    assert row.attempt == 1
    assert await task_runtime.claim_one() is None  # leased, not claimable again


async def test_expired_lease_is_reclaimed_and_logged():
    await _drain_queue()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    tid = await _task(await _user(), status="running", lease_owner="dead-worker",
                      lease_expires_at=past, attempt=1)
    claimed = await task_runtime.claim_one()
    assert claimed is not None and claimed.id == tid
    row = await _get(tid)
    assert row.attempt == 2 and row.lease_owner == task_runtime.WORKER_ID
    async with _TestSessionLocal() as s:
        kinds = (await s.execute(select(TaskEvent.data).where(TaskEvent.task_id == tid))).scalars().all()
    assert {"step": "resumed", "attempt": 2} in kinds


async def test_unmanaged_running_rows_are_never_reclaimed():
    # Scheduled-brief shells are status=running with no lease owner.
    await _drain_queue()
    await _task(await _user(), status="running")
    assert await task_runtime.claim_one() is None


async def test_gives_up_after_max_attempts():
    await _drain_queue()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    tid = await _task(await _user(), status="running", lease_owner="dead",
                      lease_expires_at=past, attempt=task_runtime.MAX_ATTEMPTS)
    assert await task_runtime.claim_one() is None
    row = await _get(tid)
    assert row.status == "failed" and row.lease_owner is None


async def test_approve_endpoint_resolves_persisted_gate(client):
    token = await create_user_and_token(client, f"ap-{secrets.token_hex(3)}@x.com")
    h = {"Authorization": f"Bearer {token}"}
    async with _TestSessionLocal() as s:
        uid = (await s.execute(select(User.id).order_by(User.id.desc()).limit(1))).scalar_one()
    tid = await _task(uid, status="running")
    async with _TestSessionLocal() as s:
        s.add(TaskApproval(task_id=tid, tool_call_id="c1", tool="gmail_send"))
        await s.commit()

    r = await client.post(f"/tasks/{tid}/approve", json={"tool_call_id": "c1", "approved": True}, headers=h)
    assert r.json() == {"ok": True}
    r = await client.post(f"/tasks/{tid}/approve", json={"tool_call_id": "c1", "approved": True}, headers=h)
    assert r.json()["ok"] is False  # already resolved

    status, _, _ = await tasks._park_for_approval(tid, "c1", "gmail_send", 60)
    assert status == "approved"


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeHttp:
    def __init__(self):
        self.payloads = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json)
        return _Resp({"choices": [{"message": {"content": "Done."}}]})


class _FakeConnectors:
    def __init__(self):
        self.calls = []

    async def execute(self, db, user_id, name, args):
        self.calls.append(name)
        return True, "sent-again"


async def test_resume_does_not_rerun_executed_destructive_call(monkeypatch):
    uid = await _user()
    tool_call = {"id": "c1", "type": "function",
                 "function": {"name": "gmail_send", "arguments": '{"to": "a@b.com"}'}}
    checkpoint = {"turn": 0, "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "email a@b.com"},
        {"role": "assistant", "tool_calls": [tool_call]},
    ]}
    tid = await _task(uid, status="running", checkpoint=checkpoint)
    async with _TestSessionLocal() as s:
        s.add(TaskApproval(task_id=tid, tool_call_id="c1", tool="gmail_send",
                           status="executed", result="sent"))
        await s.commit()

    http, conn = _FakeHttp(), _FakeConnectors()
    schema = [{"type": "function", "function": {"name": "gmail_send", "description": "send"}}]

    async def _schemas(db, user_id):
        return schema

    monkeypatch.setattr(tasks, "_allowed_tool_schemas", _schemas)
    monkeypatch.setattr(tasks, "_connectors", conn)
    monkeypatch.setattr(tasks, "get_http", lambda: http)
    monkeypatch.setattr(tasks, "_llm_base_and_key", lambda p: ("http://llm", "k"))

    await tasks._run_task_loop(tid, uid, "email a@b.com")

    assert conn.calls == []  # not sent twice
    tool_msgs = [m for m in http.payloads[0]["messages"] if m.get("role") == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_call_id": "c1", "content": "sent"}]
    row = await _get(tid)
    assert row.status == "completed" and row.result_md == "Done."


async def test_snapshot_reports_status_and_approval_changes():
    uid = await _user()
    tid = await _task(uid, status="running")
    async with _TestSessionLocal() as s:
        before = await tasks._task_snapshot(s, uid)
        s.add(TaskApproval(task_id=tid, tool_call_id="c9", tool="gmail_send"))
        await s.commit()
        mid = await tasks._task_snapshot(s, uid)
    assert tasks._snapshot_changes(before, before) == []
    [change] = tasks._snapshot_changes(before, mid)
    assert change["id"] == tid and change["approval_needed"] is True


async def test_cron_claim_lets_only_one_replica_fire(monkeypatch):
    import scheduled_tasks
    from db_ios import ScheduledTask
    monkeypatch.setattr(scheduled_tasks, "SessionLocal", _TestSessionLocal)
    uid = await _user()
    async with _TestSessionLocal() as s:
        s.add(ScheduledTask(id="st_claim", user_id=uid, kind="morning_brief", run_time_local="08:00"))
        await s.commit()
    # Two replicas both observed last_fired_at=None; only the first claim wins.
    assert await scheduled_tasks._claim("st_claim", None) is True
    assert await scheduled_tasks._claim("st_claim", None) is False
