"""
LLM provider abstraction.

Wraps litellm to give the agent a provider-agnostic chat + tool-call interface.
All OpenAI-compatible and Anthropic/Gemini/Ollama providers are supported.

Config shape (see config.llm_config):
    {
      "active": "openai",
      "providers": {
        "openai":    {"model": "gpt-4o",                     "base_url": null},
        "anthropic": {"model": "claude-3-5-sonnet-20241022", "base_url": null},
        "gemini":    {"model": "gemini-1.5-pro",             "base_url": null},
        "groq":      {"model": "llama-3.1-70b-versatile",    "base_url": null},
        "ollama":    {"model": "llama3.1:8b-instruct-q4_K_M","base_url": "http://localhost:11434"}
      }
    }

API keys live in the macOS Keychain (service="miniflow-llm", username=<provider>).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("llm")


# ── Provider catalog ──────────────────────────────────────────────────────────
#
# Each entry defines how litellm identifies the provider's model string and
# whether an API key is required. Extend freely.

PROVIDERS: dict[str, dict] = {
    "openai": {
        "display_name": "OpenAI",
        "litellm_prefix": "",  # "gpt-4o" passes through as-is
        "requires_key": True,
        "supports_tools": True,
        "default_model": "gpt-4o",
        "suggested_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1-preview"],
    },
    "anthropic": {
        "display_name": "Anthropic Claude",
        "litellm_prefix": "anthropic/",
        "requires_key": True,
        "supports_tools": True,
        "default_model": "claude-3-5-sonnet-20241022",
        "suggested_models": [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ],
    },
    "gemini": {
        "display_name": "Google Gemini",
        "litellm_prefix": "gemini/",
        "requires_key": True,
        "supports_tools": True,
        "default_model": "gemini-1.5-pro",
        "suggested_models": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash-exp"],
    },
    "groq": {
        "display_name": "Groq",
        "litellm_prefix": "groq/",
        "requires_key": True,
        "supports_tools": True,
        "default_model": "llama-3.1-70b-versatile",
        "suggested_models": ["llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    },
    "openrouter": {
        "display_name": "OpenRouter",
        "litellm_prefix": "openrouter/",
        "requires_key": True,
        "supports_tools": True,
        "default_model": "openrouter/anthropic/claude-3.5-sonnet",
        "suggested_models": [
            "openrouter/anthropic/claude-3.5-sonnet",
            "openrouter/openai/gpt-4o",
            "openrouter/meta-llama/llama-3.1-405b-instruct",
        ],
    },
    "ollama": {
        "display_name": "Ollama (local)",
        "litellm_prefix": "ollama/",
        "requires_key": False,
        "supports_tools": True,  # only for certain models — see docs
        "default_model": "llama3.1:8b-instruct-q4_K_M",
        "suggested_models": [
            "llama3.1:8b-instruct-q4_K_M",
            "llama3.1:70b-instruct-q4_K_M",
            "qwen2.5:14b-instruct",
            "mistral-nemo:12b-instruct-2407-q4_K_M",
        ],
    },
    "uxie": {
        "display_name": "Uxie Cloud",
        "litellm_prefix": "",
        "requires_key": False,
        "supports_tools": True,
        "default_model": "gpt-4o",
        "suggested_models": ["gpt-4o", "llama-3.1-8b-instant"],
    },
}

# Tool-calling in local models is finicky. Keep a known-good allowlist so the UI
# can warn users when they pick a model that is unlikely to honor tools.
OLLAMA_TOOL_CAPABLE_FAMILIES = ("llama3.1", "llama3.2", "qwen2.5", "mistral-nemo", "qwen3")


# ── Uxie cloud proxy helpers ──────────────────────────────────────────────────

def _uxie_backend_provider(model: str) -> str:
    """Map model name → groq | openai for the Uxie backend."""
    groq_families = ("llama", "mixtral", "gemma", "whisper")
    return "groq" if any(f in model.lower() for f in groq_families) else "openai"


def _current_session_id() -> str | None:
    """Read the current audio session id from audio.py, if any. Threaded into
    /llm/stream and /llm/chat requests so the backend's SessionLog row can be
    correlated with the uploaded audio blob of the same session."""
    try:
        import audio as _audio
        return getattr(_audio, "_session_id", None) or None
    except Exception:
        return None


async def _uxie_chat_stream(
    messages: list[dict],
    model: str,
    temperature: float,
    jwt: str,
):
    """Stream text chunks from Uxie backend /llm/stream (SSE)."""
    import config as _config
    backend_provider = _uxie_backend_provider(model)
    payload = {
        "messages": messages,
        "model": model,
        "provider": backend_provider,
        "temperature": temperature,
        "max_tokens": 1024,
    }
    sid = _current_session_id()
    if sid:
        payload["session_id"] = sid
    headers = {"Authorization": f"Bearer {jwt}"}
    base = _config.get_uxie_backend_url()
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", f"{base}/llm/stream",
                                 headers=headers, json=payload) as resp:
            if resp.status_code == 401:
                _config.report_unauthorized()
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    piece = (data.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                    if piece:
                        yield piece
                except Exception:
                    continue


async def _uxie_chat(
    messages: list[dict],
    tools: list[dict] | None,
    model: str,
    temperature: float,
    jwt: str,
) -> "LLMResponse":
    """Non-streaming chat (with optional tool calling) via Uxie backend /llm/chat."""
    import config as _config
    backend_provider = _uxie_backend_provider(model)
    payload: dict = {
        "messages": messages,
        "model": model,
        "provider": backend_provider,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    sid = _current_session_id()
    if sid:
        payload["session_id"] = sid

    headers = {"Authorization": f"Bearer {jwt}"}
    base = _config.get_uxie_backend_url()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{base}/llm/chat", headers=headers, json=payload)
        if resp.status_code == 401:
            _config.report_unauthorized()
        resp.raise_for_status()
        data = resp.json()

    msg = (data.get("choices") or [{}])[0].get("message", {})
    raw_tools = msg.get("tool_calls") or []
    tool_calls = [
        ToolCall(
            id=tc["id"],
            name=tc["function"]["name"],
            arguments_json=tc["function"].get("arguments") or "{}",
        )
        for tc in raw_tools
    ]
    return LLMResponse(content=msg.get("content"), tool_calls=tool_calls)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]


# ── Public API ────────────────────────────────────────────────────────────────

def list_providers() -> list[dict]:
    """Return serializable provider catalog for the UI."""
    return [
        {
            "id": pid,
            "display_name": meta["display_name"],
            "requires_key": meta["requires_key"],
            "supports_tools": meta["supports_tools"],
            "default_model": meta["default_model"],
            "suggested_models": meta["suggested_models"],
        }
        for pid, meta in PROVIDERS.items()
    ]


def ollama_model_is_tool_capable(model: str) -> bool:
    return any(model.startswith(fam) for fam in OLLAMA_TOOL_CAPABLE_FAMILIES)


def build_model_string(provider: str, model: str) -> str:
    """Prefix the model with litellm's provider tag when required."""
    meta = PROVIDERS.get(provider)
    if not meta:
        raise ValueError(f"Unknown provider: {provider}")
    prefix = meta["litellm_prefix"]
    if not prefix or model.startswith(prefix):
        return model
    return f"{prefix}{model}"


async def chat_stream(
    *,
    provider: str,
    model: str,
    messages: list[dict],
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
):
    """Async generator yielding text chunks as they arrive from the LLM.

    Used for dictation: we pipe each token to the native helper so typing
    begins ~200 ms after the LLM receives the request, rather than waiting
    for the full response.
    """
    if provider == "uxie":
        import config as _config
        jwt = _config.get_jwt()
        if not jwt:
            raise ValueError("Not signed in to Uxie — please sign in from Settings")
        async for chunk in _uxie_chat_stream(messages, model, temperature, jwt):
            yield chunk
        return

    from litellm import acompletion
    meta = PROVIDERS.get(provider)
    if not meta:
        raise ValueError(f"Unknown provider: {provider}")

    kwargs: dict[str, Any] = {
        "model": build_model_string(provider, model),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if api_key: kwargs["api_key"] = api_key
    if base_url: kwargs["api_base"] = base_url

    response = await acompletion(**kwargs)
    async for event in response:
        try:
            delta = event.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece
        except Exception:
            continue


async def chat(
    *,
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.2,
) -> LLMResponse:
    """Invoke the configured LLM with optional tools.

    Returns a normalized `LLMResponse` so the agent loop code stays identical
    across providers.
    """
    if provider == "uxie":
        import config as _config
        jwt = _config.get_jwt()
        if not jwt:
            raise ValueError("Not signed in to Uxie — please sign in from Settings")
        return await _uxie_chat(messages, tools, model, temperature, jwt)

    # Import inside function so the module loads even before litellm is installed
    # (useful when running tests that stub this out).
    from litellm import acompletion

    meta = PROVIDERS.get(provider)
    if not meta:
        raise ValueError(f"Unknown provider: {provider}")

    kwargs: dict[str, Any] = {
        "model": build_model_string(provider, model),
        "messages": messages,
        "temperature": temperature,
    }
    if tools and meta["supports_tools"]:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["api_base"] = base_url

    try:
        response = await acompletion(**kwargs)
    except Exception as e:
        log.error(f"[llm/{provider}] completion failed: {e}")
        raise

    msg = response.choices[0].message
    tool_calls_raw = getattr(msg, "tool_calls", None) or []
    tool_calls = [
        ToolCall(id=tc.id, name=tc.function.name, arguments_json=tc.function.arguments or "{}")
        for tc in tool_calls_raw
    ]
    return LLMResponse(content=msg.content, tool_calls=tool_calls)
