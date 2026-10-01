"""Phase 4: desktop context captured at hotkey press, injected on demand."""
from __future__ import annotations


def test_context_block_and_toggle(monkeypatch):
    import agent
    import config
    monkeypatch.setattr(agent, "_target_bundle_id", "com.tinyspeck.slackmacgap")
    monkeypatch.setattr(agent, "_window_title", "#product — Acme")
    monkeypatch.setattr(agent, "_target_page_url", None)
    monkeypatch.setattr(agent, "_selected_text", "x" * 5000)

    ctx = agent.desktop_context()
    assert ctx["app"] == "com.tinyspeck.slackmacgap" and ctx["window_title"] == "#product — Acme"
    assert "url" not in ctx                                   # empty fields dropped
    assert len(ctx["selected_text"]) == agent.MAX_CONTEXT_SELECTION
    block = agent._context_block(ctx)
    assert block.startswith("[Desktop context") and "Window: #product — Acme" in block

    config.set_desktop_context_enabled(False)
    assert agent.desktop_context() == {}
    assert agent._context_block({}) == ""
