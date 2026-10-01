"""Phase 2: agents CRUD, router, per-agent tool policy, model gateway."""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select

import agents
import db as db_module
import model_gateway
import tasks
from db_ios import Agent, BackgroundTask, TaskApproval, TaskEvent
from tests.conftest import _TestSessionLocal, create_user_and_token


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    monkeypatch.setattr(db_module, "SessionLocal", _TestSessionLocal)
    monkeypatch.setattr(tasks, "SessionLocal", _TestSessionLocal)


async def _login(client):
    token = await create_user_and_token(client, f"ag-{secrets.token_hex(4)}@x.com")
    return {"Authorization": f"Bearer {token}"}


async def test_list_creates_default_agent_once(client):
    h = await _login(client)
    first = (await client.get("/agents", headers=h)).json()["agents"]
    second = (await client.get("/agents", headers=h)).json()["agents"]
    assert [a["name"] for a in first] == ["Uxie"] and first[0]["is_default"]
    assert first == second
    tools = {t["name"]: t["default"] for t in (await client.get("/agents", headers=h)).json()["tools"]}
    assert tools["gmail_read"] == "allow" and tools["gmail_send"] == "require_approval"


async def test_create_update_delete_and_validation(client):
    h = await _login(client)
    r = await client.post("/agents", headers=h, json={
        "name": "Research", "role": "Research companies",
        "tool_policy": {"gmail_send": "deny"},
    })
    assert r.status_code == 200, r.text
    agent = r.json()
    assert (await client.post("/agents", headers=h, json={"name": "research"})).status_code == 409
    assert (await client.post("/agents", headers=h, json={
        "name": "Bad", "tool_policy": {"gmail_send": "sometimes"}})).status_code == 422
    assert (await client.post("/agents", headers=h, json={
        "name": "Bad2", "tool_policy": {"rm_rf": "allow"}})).status_code == 422

    r = await client.patch(f"/agents/{agent['id']}", headers=h, json={"instructions": "Be terse."})
    assert r.json()["instructions"] == "Be terse."

    default = next(a for a in (await client.get("/agents", headers=h)).json()["agents"] if a["is_default"])
    assert (await client.delete(f"/agents/{default['id']}", headers=h)).status_code == 422
    assert (await client.delete(f"/agents/{agent['id']}", headers=h)).json() == {"ok": True}


async def test_route_explicit_mention_classifier_and_fallback(client, monkeypatch):
    h = await _login(client)
    only = (await client.post("/route", headers=h, json={"utterance": "hi"})).json()
    assert only["reason_code"] == "only_default"

    await client.post("/agents", headers=h, json={"name": "Research", "role": "Research companies"})
    r = (await client.post("/route", headers=h, json={"utterance": "ask my research agent to compare X and Y"})).json()
    assert r["agent_name"] == "Research" and r["reason_code"] == "explicit_mention"

    async def _pick(role, messages, **kw):
        return {"agent": "Research"}
    monkeypatch.setattr(model_gateway, "complete_json", _pick)
    r = (await client.post("/route", headers=h, json={"utterance": "compare pricing of these vendors"})).json()
    assert r["agent_name"] == "Research" and r["reason_code"] == "classifier"

    async def _boom(role, messages, **kw):
        raise RuntimeError("provider down")
    monkeypatch.setattr(model_gateway, "complete_json", _boom)
    r = (await client.post("/route", headers=h, json={"utterance": "compare pricing"})).json()
    assert r["agent_name"] == "Uxie" and r["reason_code"] == "classifier_failed"


async def test_create_task_with_explicit_agent(client):
    h = await _login(client)
    agent = (await client.post("/agents", headers=h, json={"name": "Work"})).json()
    r = (await client.post("/tasks/create", headers=h, json={"prompt": "do it", "agent_id": agent["id"]})).json()
    assert r["agent_name"] == "Work"
    listed = (await client.get("/tasks", headers=h)).json()["tasks"]
    assert listed[0]["agent_name"] == "Work"


def test_effective_policy_overlays_agent():
    a = Agent(id="x", user_id=1, name="R", tool_policy={"gmail_send": "deny", "gmail_read": "require_approval", "bogus": "allow"})
    p = tasks._effective_policy(a)
    assert p["gmail_send"] == "deny"
    assert p["gmail_read"] == "require_approval"
    assert p["calendar_list_events"] == "allow"          # default read-only
    assert p["slack_send_message"] == "require_approval"  # default destructive
    assert "bogus" not in p


def test_gateway_resolve_overrides(monkeypatch):
    assert model_gateway.resolve("task_planner") == ("openai", "gpt-4o")
    # Moving call sites onto roles must not change today's models.
    assert model_gateway.resolve("voice_agent") == ("groq", "llama-3.3-70b-versatile")
    assert model_gateway.resolve("dictation_fix") == ("groq", "llama-3.3-70b-versatile")
    assert model_gateway.resolve("briefing") == ("openai", "gpt-4o")
    assert model_gateway.resolve("task_planner", "gpt-4o-mini") == ("openai", "gpt-4o-mini")
    monkeypatch.setenv("MODEL_ROLE_ROUTER", "openai:gpt-4o-mini")
    assert model_gateway.resolve("router") == ("openai", "gpt-4o-mini")


# ── Policy enforced inside the task loop ─────────────────────────────────────

class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _ScriptedHttp:
    """First LLM turn calls gmail_send; second turn finishes."""
    def __init__(self):
        self.payloads = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json)
        if len(self.payloads) == 1:
            return _Resp({"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "gmail_send", "arguments": '{"to": "a@b.com"}'}}]}}]})
        return _Resp({"choices": [{"message": {"content": "Done."}}]})


class _Conn:
    def __init__(self):
        self.calls = []

    async def execute(self, db, user_id, name, args):
        self.calls.append(name)
        return True, "sent"


async def _run_with_policy(monkeypatch, policy):
    from db import User
    async with _TestSessionLocal() as s:
        u = User(email=f"p-{secrets.token_hex(4)}@x.com", referral_code=secrets.token_hex(4))
        s.add(u)
        await s.commit()
        a = Agent(id=f"agt_{secrets.token_hex(4)}", user_id=u.id, name="Sales", tool_policy=policy)
        t = BackgroundTask(id=f"t_{secrets.token_hex(4)}", user_id=u.id, prompt="email", status="running", agent_id=a.id)
        s.add_all([a, t])
        await s.commit()
        uid, tid = u.id, t.id

    http, conn = _ScriptedHttp(), _Conn()
    schema = [{"type": "function", "function": {"name": "gmail_send", "description": "send"}}]

    async def _schemas(db, user_id):
        return schema

    monkeypatch.setattr(tasks, "_allowed_tool_schemas", _schemas)
    monkeypatch.setattr(tasks, "_connectors", conn)
    monkeypatch.setattr(tasks, "get_http", lambda: http)
    monkeypatch.setattr(tasks, "_llm_base_and_key", lambda p: ("http://llm", "k"))
    await tasks._run_task_loop(tid, uid, "email")
    async with _TestSessionLocal() as s:
        kinds = (await s.execute(select(TaskEvent.kind).where(TaskEvent.task_id == tid))).scalars().all()
        approval = (await s.execute(select(TaskApproval).where(TaskApproval.task_id == tid))).scalar_one_or_none()
    return http, conn, kinds, approval


async def test_agent_deny_hides_and_blocks_tool(monkeypatch):
    http, conn, kinds, _ = await _run_with_policy(monkeypatch, {"gmail_send": "deny"})
    assert conn.calls == []
    offered = [t["function"]["name"] for t in http.payloads[0].get("tools", [])]
    assert "gmail_send" not in offered  # denied tool not offered to the model


async def test_agent_allow_skips_prompt_but_records_for_idempotency(monkeypatch):
    http, conn, kinds, approval = await _run_with_policy(monkeypatch, {"gmail_send": "allow"})
    assert conn.calls == ["gmail_send"]
    assert "approval_needed" not in kinds
    assert approval is not None and approval.status == "executed"


async def test_task_carries_desktop_context(client):
    h = await _login(client)
    r = (await client.post("/tasks/create", headers=h, json={
        "prompt": "handle this",
        "desktop_context": {"app": "Slack", "window_title": "#product", "junk": "dropped"},
    })).json()
    async with _TestSessionLocal() as s:
        row = (await s.execute(select(BackgroundTask).where(BackgroundTask.id == r["id"]))).scalar_one()
    assert row.desktop_context == {"app": "Slack", "window_title": "#product"}
    text = tasks._with_context("handle this", row.desktop_context)
    assert "Window: #product" in text and text.endswith("handle this")
