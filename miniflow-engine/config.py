"""
Config — reads/writes ~/miniflow/*.json plus Keychain-backed LLM keys.

Layout:
    ~/miniflow/miniflow_keys.json      (legacy flat keys: smallest, openai)
    ~/miniflow/miniflow_settings.json  (app settings)
    ~/miniflow/llm_providers.json      (new: active provider + per-provider model/base_url)
    macOS Keychain: service="miniflow-llm", username=<provider>  (new: LLM API keys)

The legacy "openai" key in miniflow_keys.json is still honored so existing
installs keep working. On first read we migrate it into llm_providers.json
with provider="openai" and into Keychain under ("miniflow-llm", "openai").
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

log = logging.getLogger("config")

CONFIG_DIR = Path.home() / "miniflow"
KEYS_FILE = CONFIG_DIR / "miniflow_keys.json"
SETTINGS_FILE = CONFIG_DIR / "miniflow_settings.json"
LLM_FILE = CONFIG_DIR / "llm_providers.json"
_UXIE_AUTH_FILE = CONFIG_DIR / "uxie_auth.json"

# Keychain service name — keys are stored as (service, provider) → api_key
KEYCHAIN_SERVICE = "miniflow-llm"

DEFAULT_SETTINGS = {
    "language": "multi",
    "whisper_mode": False,
    "developer_mode": False,
    "filler_removal": True,
    "user_name": None,
}

DEFAULT_LLM_CONFIG: dict[str, Any] = {
    "active": "uxie",
    "providers": {
        "openai":    {"model": "gpt-4o",                         "base_url": None},
        "anthropic": {"model": "claude-3-5-sonnet-20241022",     "base_url": None},
        "gemini":    {"model": "gemini-1.5-pro",                 "base_url": None},
        "groq":      {"model": "llama-3.1-70b-versatile",        "base_url": None},
        "openrouter":{"model": "openrouter/anthropic/claude-3.5-sonnet", "base_url": None},
        "ollama":    {"model": "llama3.1:8b-instruct-q4_K_M",    "base_url": "http://localhost:11434"},
        "uxie":      {"model": "gpt-4o",                         "base_url": None},
    },
}


def _ensure_dir():
    CONFIG_DIR.mkdir(exist_ok=True)


def _read_json(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return dict(default)


def _write_json(path: Path, data: dict):
    _ensure_dir()
    path.write_text(json.dumps(data, indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # chmod 600


# ── Legacy API keys (smallest / openai) ───────────────────────────────────────
#
# Kept for backward compatibility. The OpenAI key written here is also
# mirrored into Keychain so the new LLM layer can read it.

def _sanitize_key(key: str) -> str:
    """Strip whitespace, quotes, and stray closing brackets/parens from API keys.
    Users often paste with trailing junk from docs — be permissive."""
    k = (key or "").strip()
    # Strip surrounding quotes
    if len(k) >= 2 and k[0] in "\"'" and k[-1] == k[0]:
        k = k[1:-1]
    # Strip stray trailing punctuation common in copy-paste
    while k and k[-1] in ")\"'}]`":
        k = k[:-1]
    # Nothing in an API key should have internal whitespace
    return "".join(c for c in k if not c.isspace())


def save_api_key(service: str, key: str):
    key = _sanitize_key(key)
    keys = _read_json(KEYS_FILE, {})
    keys[service] = key
    _write_json(KEYS_FILE, keys)
    if service == "openai":
        set_llm_api_key("openai", key)


def get_api_key(service: str) -> str:
    keys = _read_json(KEYS_FILE, {})
    if service not in keys or not keys[service]:
        raise ValueError(f"{service} API key not set")
    return keys[service]


def has_api_keys() -> dict:
    keys = _read_json(KEYS_FILE, {})
    return {"smallest": keys.get("smallest"), "openai": keys.get("openai")}


def get_openai_key() -> str:
    return get_api_key("openai")


def get_smallest_key() -> str:
    return get_api_key("smallest")


# ── Settings ──────────────────────────────────────────────────────────────────

def _read_settings() -> dict:
    return {**DEFAULT_SETTINGS, **_read_json(SETTINGS_FILE, {})}


def _write_settings(settings: dict):
    _write_json(SETTINGS_FILE, settings)


def save_language(language: str):
    s = _read_settings()
    s["language"] = language
    _write_settings(s)


def get_language() -> str:
    return _read_settings()["language"]


def get_advanced_settings() -> dict:
    s = _read_settings()
    return {
        "whisper_mode": s["whisper_mode"],
        "developer_mode": s["developer_mode"],
        "filler_removal": s["filler_removal"],
    }


def save_advanced_setting(key: str, value: bool):
    s = _read_settings()
    if key not in ("whisper_mode", "developer_mode", "filler_removal"):
        raise ValueError(f"Unknown setting: {key}")
    s[key] = value
    _write_settings(s)


def get_auto_detect_meetings() -> bool:
    """When true, the meeting watcher subprocess runs in the background
    and fires notifications when known meeting-app windows appear."""
    return bool(_read_settings().get("auto_detect_meetings", False))


def set_auto_detect_meetings(enabled: bool) -> None:
    s = _read_settings()
    s["auto_detect_meetings"] = bool(enabled)
    _write_settings(s)


def get_share_meetings_with_admin() -> bool:
    """When true, the engine uploads each finished meeting's WAV + a
    transcript preview to Railway after recording stops. They appear
    in /admin/dashboard for review. Default off — opt-in only."""
    return bool(_read_settings().get("share_meetings_with_admin", False))


def set_share_meetings_with_admin(enabled: bool) -> None:
    s = _read_settings()
    s["share_meetings_with_admin"] = bool(enabled)
    _write_settings(s)


def save_user_name(name: str):
    s = _read_settings()
    s["user_name"] = name.strip() or None
    _write_settings(s)


def get_user_name() -> str | None:
    return _read_settings().get("user_name")


def get_current_language() -> str:
    return get_language()


# ── LLM provider config ───────────────────────────────────────────────────────

def _read_llm_config() -> dict:
    cfg = _read_json(LLM_FILE, DEFAULT_LLM_CONFIG)
    # Merge any missing providers from defaults so users who upgraded get new ones
    providers = dict(DEFAULT_LLM_CONFIG["providers"])
    providers.update(cfg.get("providers", {}))
    cfg["providers"] = providers
    cfg.setdefault("active", DEFAULT_LLM_CONFIG["active"])
    return cfg


def _write_llm_config(cfg: dict):
    _write_json(LLM_FILE, cfg)


def get_llm_config() -> dict:
    """Return the full LLM config (no keys, just active + model/base_url per provider)."""
    return _read_llm_config()


def get_active_llm() -> dict:
    """Return {provider, model, base_url, api_key} for the currently active provider.

    api_key comes from Keychain (or legacy miniflow_keys.json for openai).
    Raises ValueError if no API key is set and the provider requires one.
    """
    cfg = _read_llm_config()
    provider = cfg["active"]
    entry = cfg["providers"].get(provider)
    if not entry:
        raise ValueError(f"LLM provider not configured: {provider}")
    return {
        "provider": provider,
        "model": entry["model"],
        "base_url": entry.get("base_url"),
        "api_key": _get_llm_api_key(provider),
    }


def set_active_llm_provider(provider: str):
    cfg = _read_llm_config()
    if provider not in cfg["providers"]:
        raise ValueError(f"Unknown provider: {provider}")
    cfg["active"] = provider
    _write_llm_config(cfg)


def set_llm_provider_model(provider: str, model: str, base_url: str | None = None):
    cfg = _read_llm_config()
    cfg["providers"].setdefault(provider, {})["model"] = model
    if base_url is not None:
        cfg["providers"][provider]["base_url"] = base_url
    _write_llm_config(cfg)


# ── LLM API keys (Keychain) ───────────────────────────────────────────────────
#
# We use the `keyring` library (already in requirements.txt). Fall back to a
# file-based store if Keychain is unavailable (CI, headless, or when running
# inside certain sandboxes).

_FALLBACK_KEYS_FILE = CONFIG_DIR / "llm_keys_fallback.json"


LLM_KEYS_FILE = CONFIG_DIR / "llm_keys.json"  # human-editable, mode 600


def _keyring_module():
    """Keychain is now opt-in via MINIFLOW_USE_KEYCHAIN=1. Otherwise we use a
    plain JSON file at ~/miniflow/llm_keys.json so users can find and edit keys."""
    if os.environ.get("MINIFLOW_USE_KEYCHAIN") != "1":
        return None
    try:
        import keyring  # type: ignore
        return keyring
    except Exception as e:  # pragma: no cover
        log.warning(f"keyring requested but unavailable: {e}")
        return None


def llm_keys_file_path() -> str:
    """Return the path to the LLM keys JSON file (for UI 'reveal' action)."""
    return str(LLM_KEYS_FILE)


def _get_llm_api_key(provider: str) -> str | None:
    # 1. File store (primary)
    data = _read_json(LLM_KEYS_FILE, {})
    v = data.get(provider)
    if v:
        return v
    # 2. Legacy fallback — old file location
    data_old = _read_json(_FALLBACK_KEYS_FILE, {})
    if data_old.get(provider):
        return data_old[provider]
    # 3. Legacy: openai key may live in miniflow_keys.json
    if provider == "openai":
        keys = _read_json(KEYS_FILE, {})
        if keys.get("openai"):
            return keys["openai"]
    # 4. Keychain (opt-in)
    kr = _keyring_module()
    if kr:
        try:
            val = kr.get_password(KEYCHAIN_SERVICE, provider)
            if val:
                return val
        except Exception as e:
            log.warning(f"keyring get_password failed: {e}")
    return None


def get_llm_api_key(provider: str) -> str | None:
    """Public accessor for a provider's stored API key."""
    return _get_llm_api_key(provider)


def set_llm_api_key(provider: str, api_key: str):
    """Store a provider's API key. Default: mode-600 JSON file you can edit."""
    api_key = _sanitize_key(api_key)
    if not api_key:
        clear_llm_api_key(provider)
        return
    data = _read_json(LLM_KEYS_FILE, {})
    data[provider] = api_key
    _write_json(LLM_KEYS_FILE, data)
    kr = _keyring_module()
    if kr:
        try:
            kr.set_password(KEYCHAIN_SERVICE, provider, api_key)
        except Exception as e:
            log.warning(f"keyring mirror failed (ignored): {e}")


def clear_llm_api_key(provider: str):
    for path in (LLM_KEYS_FILE, _FALLBACK_KEYS_FILE):
        data = _read_json(path, {})
        if provider in data:
            del data[provider]
            _write_json(path, data)
    kr = _keyring_module()
    if kr:
        try:
            kr.delete_password(KEYCHAIN_SERVICE, provider)
        except Exception:
            pass


def llm_provider_status() -> dict:
    """Return per-provider connection status for the Settings UI.

    Shape: {provider_id: {"configured": bool, "model": str, "base_url": str|None}}
    """
    cfg = _read_llm_config()
    out: dict = {}
    for pid, entry in cfg["providers"].items():
        out[pid] = {
            "configured": _get_llm_api_key(pid) is not None or not _requires_key(pid),
            "model": entry["model"],
            "base_url": entry.get("base_url"),
            "is_active": pid == cfg["active"],
        }
    return out


def _requires_key(provider: str) -> bool:
    return provider not in ("ollama", "uxie")


# ── Uxie backend auth (JWT) ───────────────────────────────────────────────────

def get_uxie_backend_url() -> str:
    url = os.environ.get("UXIE_BACKEND_URL", "")
    if not url:
        url = _read_settings().get("uxie_backend_url", "")
    if not url:
        url = "https://uxie-production.up.railway.app"
    return url.rstrip("/")


# The JWT lives in Electron's safeStorage (OS keychain / DPAPI) when
# available. Electron hands it to us at connect time via set_session_token
# and we keep it in memory only. Right after login (or on first run after
# upgrading) it's still in uxie_auth.json until Electron adopts it with
# take_file_token, which strips it from the file.
_session_token: str | None = None
_token_in_secure_store = False


def get_jwt() -> str | None:
    return _session_token or _read_json(_UXIE_AUTH_FILE, {}).get("access_token") or None


def save_jwt(
    token: str,
    email: str = "",
    tier: str = "free",
    referral_code: str = "",
    free_days_remaining: int = 30,
):
    global _session_token
    _session_token = token
    _ensure_dir()
    data = _read_json(_UXIE_AUTH_FILE, {})
    data.update({
        "email": email,
        "tier": tier,
        "referral_code": referral_code,
        "free_days_remaining": free_days_remaining,
    })
    if _token_in_secure_store:
        data.pop("access_token", None)
    else:
        data["access_token"] = token
    _write_json(_UXIE_AUTH_FILE, data)


def set_session_token(token: str) -> dict:
    """Electron decrypted the token from safeStorage — hold it in memory."""
    global _session_token, _token_in_secure_store
    _session_token = token or None
    _token_in_secure_store = bool(token)
    return {"ok": True}


def take_file_token() -> dict:
    """Hand the on-disk token to Electron for secure storage and remove it
    from the file. Returns {"token": None} if there's nothing to adopt."""
    global _session_token, _token_in_secure_store
    data = _read_json(_UXIE_AUTH_FILE, {})
    token = data.pop("access_token", None)
    if not token:
        return {"token": None}
    _write_json(_UXIE_AUTH_FILE, data)
    _session_token = token
    _token_in_secure_store = True
    return {"token": token}


def clear_jwt():
    global _session_token, _token_in_secure_store
    _session_token = None
    _token_in_secure_store = False
    _write_json(_UXIE_AUTH_FILE, {})


def get_uxie_user() -> dict:
    """Profile for the UI. The token itself is never returned — the UI only
    checks `access_token` for truthiness to decide signed-in state."""
    data = _read_json(_UXIE_AUTH_FILE, {})
    if get_jwt():
        data["access_token"] = "stored"
    else:
        data.pop("access_token", None)
    return data
