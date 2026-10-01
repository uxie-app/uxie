"""Model roles → (provider, model).

Callers ask for a *role* ("task_planner", "router", …) instead of hardcoding
a model, so changing models — or providers, once decision D1 is made — is a
config change here (or an env override), not a hunt through call sites.

Env override per role: MODEL_ROLE_<ROLE>="provider:model",
e.g. MODEL_ROLE_TASK_PLANNER="openai:gpt-4o-mini".

proxy.py's /llm/* endpoints are a passthrough: the desktop engine names the
model in its request, so they don't use roles.
"""
from __future__ import annotations

import json
import os
from typing import Any

from proxy import _llm_base_and_key, get_http

# Today's models, unchanged — see the roadmap's model section for the
# proposed mapping once D1 is decided.
ROLE_MODELS: dict[str, tuple[str, str]] = {
    "task_planner": ("openai", "gpt-4o"),                     # tasks.py background loop
    "router": ("groq", "llama-3.1-8b-instant"),               # agents.py /route
    "voice_agent": ("groq", "llama-3.3-70b-versatile"),       # agent.py /agent/* command loop
    "dictation_fix": ("groq", "llama-3.3-70b-versatile"),     # agent.py dictation cleanup
    "briefing": ("openai", "gpt-4o"),                         # scheduled_tasks.py generators
}

KNOWN_PROVIDERS = {"openai", "groq"}


def resolve(role: str, override: str | None = None) -> tuple[str, str]:
    """(provider, model) for a role. `override` is an agent's model_policy
    entry: either "provider:model" or a bare model on the role's provider."""
    provider, model = ROLE_MODELS[role]
    env = os.environ.get(f"MODEL_ROLE_{role.upper()}")
    for spec in (env, override):
        if not spec:
            continue
        if ":" in spec and spec.split(":", 1)[0] in KNOWN_PROVIDERS:
            provider, model = spec.split(":", 1)
        else:
            model = spec
    return provider, model


async def complete_json(role: str, messages: list[dict], *, timeout: float = 15) -> dict[str, Any]:
    """One-shot call that must return a JSON object (used by the router)."""
    provider, model = resolve(role)
    base_url, api_key = _llm_base_and_key(provider)
    resp = await get_http().post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or "{}"
    return json.loads(content)
