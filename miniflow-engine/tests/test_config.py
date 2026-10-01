"""Tests for config.py — settings, legacy keys, LLM config, Keychain keys."""

from __future__ import annotations

import json

import pytest


# ── Legacy key API ────────────────────────────────────────────────────────────

def test_legacy_save_and_get_openai_key(fake_keyring):
    import config
    config.save_api_key("openai", "sk-legacy-1234")
    # Legacy fetch still works
    assert config.get_openai_key() == "sk-legacy-1234"
    # And it was mirrored into the keyring so the new LLM layer sees it too
    assert fake_keyring.store[(config.KEYCHAIN_SERVICE, "openai")] == "sk-legacy-1234"


def test_has_api_keys_reflects_presence():
    import config
    assert config.has_api_keys() == {"smallest": None, "openai": None}
    config.save_api_key("smallest", "sm-1")
    config.save_api_key("openai", "sk-1")
    assert config.has_api_keys() == {"smallest": "sm-1", "openai": "sk-1"}


def test_missing_key_raises():
    import config
    with pytest.raises(ValueError, match="not set"):
        config.get_api_key("nonexistent")


# ── Settings ──────────────────────────────────────────────────────────────────

def test_settings_defaults_then_override():
    import config
    assert config.get_language() == "multi"
    config.save_language("hi")
    assert config.get_language() == "hi"


def test_save_advanced_setting_rejects_unknown_key():
    import config
    with pytest.raises(ValueError, match="Unknown setting"):
        config.save_advanced_setting("rogue", True)


def test_save_and_clear_user_name():
    import config
    assert config.get_user_name() is None
    config.save_user_name("Rounak")
    assert config.get_user_name() == "Rounak"
    config.save_user_name("")
    assert config.get_user_name() is None  # empty → cleared


# ── LLM config ────────────────────────────────────────────────────────────────

def test_default_llm_provider_is_uxie():
    # CLAUDE.md: the default must stay "uxie" so all LLM calls go through
    # the Railway backend (no user-facing API keys).
    import config
    cfg = config.get_llm_config()
    assert cfg["active"] == "uxie"
    assert "anthropic" in cfg["providers"]
    assert cfg["providers"]["openai"]["model"] == "gpt-4o"


def test_set_active_llm_provider_persists():
    import config
    config.set_active_llm_provider("anthropic")
    assert config.get_llm_config()["active"] == "anthropic"


def test_set_active_llm_unknown_provider_raises():
    import config
    with pytest.raises(ValueError, match="Unknown provider"):
        config.set_active_llm_provider("nope")


def test_set_llm_provider_model_and_base_url():
    import config
    config.set_llm_provider_model("ollama", "qwen2.5:14b", "http://192.168.1.10:11434")
    cfg = config.get_llm_config()
    assert cfg["providers"]["ollama"]["model"] == "qwen2.5:14b"
    assert cfg["providers"]["ollama"]["base_url"] == "http://192.168.1.10:11434"


def test_get_active_llm_returns_key_from_keychain(fake_keyring):
    import config
    config.set_active_llm_provider("anthropic")
    config.set_llm_api_key("anthropic", "sk-ant-abc")
    active = config.get_active_llm()
    assert active == {
        "provider": "anthropic",
        "model": "claude-3-5-sonnet-20241022",
        "base_url": None,
        "api_key": "sk-ant-abc",
    }


def test_get_active_llm_returns_none_key_when_unset(fake_keyring):
    import config
    config.set_active_llm_provider("gemini")
    active = config.get_active_llm()
    assert active["api_key"] is None


def test_clear_llm_api_key(fake_keyring):
    import config
    config.set_llm_api_key("groq", "gsk-1")
    config.clear_llm_api_key("groq")
    active = config.get_active_llm()  # default active is openai, but groq key should be gone
    import config as cfg_mod
    assert cfg_mod._get_llm_api_key("groq") is None


def test_llm_provider_status_marks_active_and_configured(fake_keyring):
    import config
    config.set_active_llm_provider("openai")
    config.set_llm_api_key("openai", "sk-1")
    status = config.llm_provider_status()
    assert status["openai"]["is_active"] is True
    assert status["openai"]["configured"] is True
    assert status["anthropic"]["is_active"] is False
    assert status["anthropic"]["configured"] is False
    # Ollama requires no key → should be "configured" by default
    assert status["ollama"]["configured"] is True


def test_legacy_openai_key_migrates_without_keyring():
    """If miniflow_keys.json has openai but keyring is empty, _get_llm_api_key
    should still return the legacy value."""
    import config
    config.save_api_key("openai", "sk-legacy")
    # Clear the keyring mirror so only the legacy file remains
    config.clear_llm_api_key("openai")
    # Legacy file still has it
    data = json.loads(config.KEYS_FILE.read_text())
    assert data["openai"] == "sk-legacy"
    # And the fallback read path finds it
    assert config._get_llm_api_key("openai") == "sk-legacy"


def test_llm_config_merges_new_defaults_on_upgrade():
    """A user who already has an older llm_providers.json with only 2 providers
    should automatically gain newly-added defaults (e.g. groq) on next read."""
    import config
    config.LLM_FILE.parent.mkdir(exist_ok=True)
    config.LLM_FILE.write_text(json.dumps({
        "active": "openai",
        "providers": {"openai": {"model": "gpt-4o-mini", "base_url": None}},
    }))
    cfg = config.get_llm_config()
    assert "groq" in cfg["providers"]
    assert cfg["providers"]["openai"]["model"] == "gpt-4o-mini", "user override preserved"
