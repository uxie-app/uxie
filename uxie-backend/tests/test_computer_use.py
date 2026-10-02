"""Computer use: action mapping, session loop (approvals, batch failure,
live view, cleanup), result reuse on resume. No real VM or model calls."""
from __future__ import annotations

import copy
import secrets
from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock

import computer_use
import db as db_module
import tasks
from db import User
from db_ios import BackgroundTask, TaskEvent
from tests.conftest import _TestSessionLocal


class FakeDesktop:
    sandbox_id = "sbx_1"

    def __init__(self):
        self.calls, self.killed = [], False
        self.stream = SimpleNamespace(
            start=lambda require_auth=False: self.calls.append(("stream", require_auth)),
            get_auth_key=lambda: "k",
            get_url=lambda view_only=False, auth_key=None: f"https://vnc?view={view_only}&pw={auth_key}",
        )
        self.commands = SimpleNamespace(run=lambda cmd: self.calls.append(("run", cmd)))

    def screenshot(self):
        return b"\x89PNG"

    def move_mouse(self, x, y):
        self.calls.append(("move", x, y))

    def left_click(self):
        self.calls.append(("left_click",))

    def write(self, text):
        self.calls.append(("write", text))

    def open(self, url):
        self.calls.append(("open", url))

    def kill(self):
        self.killed = True


def test_run_action_maps_to_desktop():
    d = FakeDesktop()
    assert computer_use.run_action(d, "screenshot", {}) == b"\x89PNG"
    computer_use.run_action(d, "left_click", {"coordinate": [10, 20], "text": "ctrl+shift"})
    computer_use.run_action(d, "key", {"text": "ctrl+l", "repeat": 2})
    computer_use.run_action(d, "scroll", {"scroll_direction": "right", "scroll_amount": 3, "coordinate": [5, 5]})
    computer_use.run_action(d, "type", {"text": "hello"})
    assert ("run", "xdotool keydown ctrl") in d.calls and ("run", "xdotool keyup ctrl") in d.calls
    assert ("move", 10, 20) in d.calls and ("left_click",) in d.calls
    assert ("run", "xdotool key --repeat 2 -- ctrl+l") in d.calls     # keysym passed through unchanged
    assert ("run", "xdotool click --repeat 3 7") in d.calls          # right = button 7
    assert ("write", "hello") in d.calls
    with pytest.raises(ValueError):
        computer_use.run_action(d, "zoom", {"region": [0, 0, 1, 1]})


def _tool(id_, name, inp, computer=True):
    return BetaToolUseBlock(id=id_, name=name, input=inp, type="tool_use",
                            toolset_name="computer" if computer else None)


class FakeClient:
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))  # the loop keeps appending to messages
        return SimpleNamespace(stop_reason="end_turn", content=self.turns.pop(0))


class Hooks(computer_use.Hooks):
    def __init__(self, approve):
        self.events, self.asks, self.approve = [], [], approve

    async def event(self, kind, data):
        self.events.append((kind, data))

    async def ask(self, call_id, tool, summary, timeout_s):
        self.asks.append((tool, summary))
        return self.approve


async def test_session_loop_approval_batch_failure_and_cleanup(monkeypatch):
    monkeypatch.setenv("MODEL_ROLE_COMPUTER_USE", "anthropic:claude-opus-5-5")
    desktop = FakeDesktop()
    client = FakeClient([
        [_tool("t1", "screenshot", {}), _tool("t2", "nonexistent_action", {}), _tool("t3", "type", {"text": "x"})],
        [_tool("t4", "request_approval", {"action": "Submit the order form"}, computer=False)],
        [BetaTextBlock(type="text", text="Couldn't submit: the user declined.")],
    ])
    hooks = Hooks(approve=False)
    out = await computer_use.run_session("buy a thing", hooks=hooks, client=client, desktop=desktop,
                                         start_url="https://shop.example")

    assert out == "Couldn't submit: the user declined."
    assert desktop.killed
    assert ("open", "https://shop.example") in desktop.calls
    session = dict(hooks.events)["computer_session"]
    assert session["view_url"].startswith("https://vnc?view=True") and "pw=k" in session["control_url"]
    assert hooks.asks == [("computer_action", "Submit the order form")]

    first_results = client.requests[1]["messages"][2]["content"]
    assert first_results[0]["content"][0]["type"] == "image" and first_results[0]["toolset_name"] == "computer"
    assert first_results[1]["is_error"] and first_results[2]["content"].startswith("Not executed")
    assert ("write", "x") not in desktop.calls                       # skipped after the failure
    approval_result = client.requests[2]["messages"][-1]["content"][0]
    assert approval_result["content"].startswith("Denied") and "toolset_name" not in approval_result

    req = client.requests[0]
    assert req["tools"][0]["type"] == "computer_toolset_20260801"
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"


async def test_finished_session_result_is_reused_on_resume(monkeypatch):
    monkeypatch.setattr(db_module, "SessionLocal", _TestSessionLocal)
    monkeypatch.setattr(tasks, "SessionLocal", _TestSessionLocal)
    monkeypatch.setattr(computer_use, "available", lambda: True)

    async def _no_session(*a, **k):
        raise AssertionError("must not start a new session")
    monkeypatch.setattr(computer_use, "run_session", _no_session)

    async with _TestSessionLocal() as s:
        u = User(email=f"cu-{secrets.token_hex(4)}@x.com", referral_code=secrets.token_hex(4))
        s.add(u)
        await s.commit()
        t = BackgroundTask(id=f"t_{secrets.token_hex(4)}", user_id=u.id, prompt="p", status="running")
        s.add(t)
        s.add(TaskEvent(task_id=t.id, seq=0, kind="computer_result", data={"call_id": "c1", "result": "found 3 prices"}))
        await s.commit()
        tid = t.id
    assert await tasks._run_computer(tid, "c1", {"goal": "compare prices"}) == "found 3 prices"


def test_tool_hidden_without_keys(monkeypatch):
    from settings import get_settings
    monkeypatch.setattr(get_settings(), "e2b_api_key", "")
    assert computer_use.available() is False


# ── OpenAI computer tool ─────────────────────────────────────────────────────

def test_openai_actions_map_to_desktop():
    d = FakeDesktop()
    d.right_click = lambda: d.calls.append(("right_click",))
    d.drag = lambda a, b: d.calls.append(("drag", a, b))
    computer_use.run_openai_action(d, {"type": "click", "button": "right", "x": 5, "y": 6})
    computer_use.run_openai_action(d, {"type": "keypress", "keys": ["CTRL", "L"]})
    computer_use.run_openai_action(d, {"type": "keypress", "keys": ["ENTER"]})
    computer_use.run_openai_action(d, {"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300})
    computer_use.run_openai_action(d, {"type": "drag", "path": [{"x": 1, "y": 1}, {"x": 5, "y": 5}, {"x": 9, "y": 9}]})
    assert ("move", 5, 6) in d.calls and ("right_click",) in d.calls
    assert ("run", "xdotool key -- ctrl+l") in d.calls and ("run", "xdotool key -- Return") in d.calls
    assert ("run", "xdotool click --repeat 3 5") in d.calls        # 300px down = 3 wheel clicks
    assert ("drag", (1, 1), (9, 9)) in d.calls
    with pytest.raises(ValueError):
        computer_use.run_openai_action(d, {"type": "teleport"})


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.payloads = list(responses), []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(copy.deepcopy(json))
        data = self.responses.pop(0)
        return SimpleNamespace(status_code=200, json=lambda: data, text="")


async def test_openai_loop_actions_approval_and_result(monkeypatch):
    import proxy
    monkeypatch.setattr(proxy, "_llm_base_and_key", lambda p: ("http://openai", "k"))
    desktop = FakeDesktop()
    http = FakeHttp([
        {"id": "r1", "output": [{"type": "computer_call", "call_id": "c1", "status": "completed",
                                 "actions": [{"type": "click", "button": "left", "x": 10, "y": 20},
                                             {"type": "type", "text": "penguin"}]}]},
        {"id": "r2", "output": [{"type": "function_call", "call_id": "f1", "name": "request_approval",
                                 "arguments": '{"action": "Submit the form"}'}]},
        {"id": "r3", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Top 3: A, B, C"}]}]},
    ])
    hooks = Hooks(approve=False)
    out = await computer_use.run_session("read HN", hooks=hooks, http=http, desktop=desktop)

    assert out == "Top 3: A, B, C" and desktop.killed
    first = http.payloads[0]
    assert first["model"] == "gpt-6.1-sol" and first["tools"][0] == {"type": "computer"}
    assert {t["name"] for t in first["tools"][1:]} == {"request_approval", "request_takeover"}
    second = http.payloads[1]
    assert second["previous_response_id"] == "r1"
    shot = second["input"][0]
    assert shot["type"] == "computer_call_output" and shot["call_id"] == "c1"
    assert shot["output"]["type"] == "computer_screenshot" and shot["output"]["image_url"].startswith("data:image/png;base64,")
    assert ("move", 10, 20) in desktop.calls and ("write", "penguin") in desktop.calls
    assert http.payloads[2]["input"] == [{"type": "function_call_output", "call_id": "f1",
                                          "output": "Denied — don't do it. Find another way or stop and explain."}]
    assert hooks.asks == [("computer_action", "Submit the form")]


async def test_openai_safety_check_declined_stops_before_acting(monkeypatch):
    import proxy
    monkeypatch.setattr(proxy, "_llm_base_and_key", lambda p: ("http://openai", "k"))
    desktop = FakeDesktop()
    http = FakeHttp([{"id": "r1", "output": [{"type": "computer_call", "call_id": "c1",
        "pending_safety_checks": [{"id": "s1", "code": "malicious_instructions", "message": "Page asks to ignore the user"}],
        "actions": [{"type": "type", "text": "secret"}]}]}])
    out = await computer_use.run_session("x", hooks=Hooks(approve=False), http=http, desktop=desktop)
    assert out.startswith("Stopped: you declined a safety check")
    assert ("write", "secret") not in desktop.calls and desktop.killed


def test_available_follows_provider(monkeypatch):
    from settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "e2b_api_key", "e2b")
    monkeypatch.setattr(s, "openai_api_key", "sk")
    monkeypatch.setattr(s, "anthropic_api_key", "")
    assert computer_use.available() is True                       # default provider: openai
    monkeypatch.setenv("MODEL_ROLE_COMPUTER_USE", "anthropic:claude-opus-5-5")
    assert computer_use.available() is False                      # no anthropic key
