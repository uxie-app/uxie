"""Tests for agent.py — the multi-turn loop, local tools, connector routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def configured_agent(fake_keyring, monkeypatch):
    """Make agent.execute_command runnable with a configured OpenAI-like provider."""
    import config
    config.set_active_llm_provider("openai")
    config.set_llm_api_key("openai", "sk-test")

    # Silence event broadcasts (must be awaitable)
    import agent
    async def _noop(*a, **k): pass
    agent.set_event_broadcaster(_noop)
    return agent


@pytest.mark.asyncio
async def test_execute_command_no_provider_falls_back_to_dictation(fake_keyring):
    """With no API key and no Ollama, the agent should return a plain dictation action
    (so the shell still types what the user said)."""
    import agent
    async def _noop(*a, **k): pass
    agent.set_event_broadcaster(_noop)

    result = await agent.execute_command("hello world")
    assert len(result) == 1
    assert result[0]["action"] == "dictation"
    assert result[0]["message"] == "hello world"


@pytest.mark.asyncio
async def test_execute_command_no_tool_calls_is_dictation(configured_agent):
    """If the LLM returns plain text (no tool calls), we treat the user's
    ORIGINAL transcript as the dictation payload — never the model's output."""
    import llm
    fake_response = llm.LLMResponse(content="DICTATION", tool_calls=[])

    with patch("llm.chat", AsyncMock(return_value=fake_response)):
        result = await configured_agent.execute_command("My dog has fleas")
    assert result[-1]["action"] == "dictation"
    assert result[-1]["message"] == "My dog has fleas"


@pytest.mark.asyncio
async def test_execute_command_runs_local_tool(configured_agent):
    """A tool call for open_application should end up in the subprocess runner."""
    import llm
    tool_call = llm.ToolCall(
        id="call_1", name="open_application",
        arguments_json='{"name": "Finder"}',
    )
    # First turn: tool call. Second turn: no tool calls (loop exits).
    responses = [
        llm.LLMResponse(content=None, tool_calls=[tool_call]),
        llm.LLMResponse(content="Done", tool_calls=[]),
    ]

    with patch("llm.chat", AsyncMock(side_effect=responses)), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        result = await configured_agent.execute_command("Open Finder")

    open_results = [r for r in result if r["action"] == "open_application"]
    assert len(open_results) == 1
    assert open_results[0]["success"] is True
    # The subprocess call should have been `open -a Finder`
    mock_run.assert_any_call(
        ["open", "-a", "Finder"], capture_output=True, text=True, timeout=10
    )


@pytest.mark.asyncio
async def test_execute_command_routes_connector_tool(configured_agent, monkeypatch):
    """A tool call with a connector prefix (slack_, gmail_, etc.) should go
    through connector_registry.execute_connector_tool."""
    import llm
    tool_call = llm.ToolCall(
        id="c1", name="slack_send_message",
        arguments_json='{"channel": "#general", "text": "hi"}',
    )
    responses = [
        llm.LLMResponse(content=None, tool_calls=[tool_call]),
        llm.LLMResponse(content="ok", tool_calls=[]),
    ]

    from connectors import registry as reg
    monkeypatch.setattr(reg, "execute_connector_tool",
                        lambda name, args, tok: (True, "Message sent to #general."))
    import oauth
    monkeypatch.setattr(oauth, "get_connected_providers", lambda: ["slack"])
    monkeypatch.setattr(oauth, "get_token", lambda p: {"access_token": "xoxb-test"})
    # slack_send_message is approval-gated; this test is about routing, so approve.
    monkeypatch.setattr(configured_agent, "_approval_gate", AsyncMock(return_value=True))

    with patch("llm.chat", AsyncMock(side_effect=responses)):
        result = await configured_agent.execute_command("Send hi to #general on slack")

    slack_results = [r for r in result if r["action"] == "slack_send_message"]
    assert slack_results and slack_results[0]["success"] is True


@pytest.mark.asyncio
async def test_execute_command_halts_on_llm_error(configured_agent):
    """If the LLM call itself raises, we should emit an llm-error action and stop."""
    with patch("llm.chat", AsyncMock(side_effect=RuntimeError("rate-limited"))):
        result = await configured_agent.execute_command("Anything")
    assert result[-1]["action"] == "llm-error"
    assert result[-1]["success"] is False
    assert "rate-limited" in result[-1]["message"]


@pytest.mark.asyncio
async def test_execute_command_respects_max_turns(configured_agent):
    """If the LLM keeps calling tools forever, we must stop after max_turns (4)."""
    import llm
    # Always return a tool call — an infinite loop unless max_turns saves us
    infinite_tool = llm.ToolCall(id="x", name="clipboard_read", arguments_json="{}")
    inf_resp = llm.LLMResponse(content=None, tool_calls=[infinite_tool])

    with patch("llm.chat", AsyncMock(return_value=inf_resp)), \
         patch("pyperclip.paste", return_value="clipboard contents"):
        result = await configured_agent.execute_command("read clipboard forever")

    # 4 turns, each produces 1 tool result → exactly 4 clipboard_read actions
    tool_actions = [r for r in result if r["action"] == "clipboard_read"]
    assert len(tool_actions) == 4
