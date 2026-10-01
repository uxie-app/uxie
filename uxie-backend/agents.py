"""Persistent agents (Phase 2).

Every user has a default "Uxie" agent (created lazily on first use) and can
add specialized ones with their own instructions and per-tool policy. The
router picks which agent handles a request.

Endpoints:
    GET    /agents              — list (creates the default agent if missing)
    POST   /agents              — create
    PATCH  /agents/{id}         — update
    DELETE /agents/{id}         — delete (not the default)
    POST   /route               — {utterance} → {agent_id, agent_name, confidence, reason_code}
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import model_gateway
from auth import current_user
from db import User, get_db
from db_ios import Agent, BackgroundTask

_log = logging.getLogger("agents")

MAX_AGENTS_PER_USER = 10
POLICY_VALUES = {"allow", "require_approval", "deny"}
DEFAULT_AGENT_NAME = "Uxie"


def _new_id() -> str:
    return f"agt_{int(time.time() * 1000):013d}_{secrets.token_hex(4)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _known_tools() -> set[str]:
    from tasks import ALL_ALLOWED_TOOLS
    return ALL_ALLOWED_TOOLS


def _serialize(a: Agent) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "role": a.role,
        "instructions": a.instructions,
        "icon": a.icon,
        "is_default": a.is_default,
        "tool_policy": a.tool_policy or {},
        "model_policy": a.model_policy or {},
    }


async def ensure_default_agent(db: AsyncSession, user_id: int) -> Agent:
    agent = (await db.execute(
        select(Agent).where(Agent.user_id == user_id, Agent.is_default == True)  # noqa: E712
    )).scalars().first()
    if agent is None:
        agent = Agent(
            id=_new_id(), user_id=user_id, name=DEFAULT_AGENT_NAME,
            role="General assistant for anything not handled by a specialized agent",
            is_default=True,
        )
        db.add(agent)
        await db.commit()
    return agent


async def user_agents(db: AsyncSession, user_id: int) -> list[Agent]:
    await ensure_default_agent(db, user_id)
    return list((await db.execute(
        select(Agent).where(Agent.user_id == user_id).order_by(Agent.created_at)
    )).scalars().all())


async def get_user_agent(db: AsyncSession, user_id: int, agent_id: str) -> Agent:
    agent = (await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id)
    )).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


def _validate_policy(policy: dict | None) -> dict | None:
    if policy is None:
        return None
    known = _known_tools()
    for tool, decision in policy.items():
        if tool not in known:
            raise HTTPException(422, f"unknown tool: {tool}")
        if decision not in POLICY_VALUES:
            raise HTTPException(422, f"invalid policy for {tool}: {decision} (use {sorted(POLICY_VALUES)})")
    return dict(policy)


# ── CRUD ──────────────────────────────────────────────────────────────────────


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    role: str | None = Field(None, max_length=500)
    instructions: str | None = Field(None, max_length=4000)
    icon: str | None = Field(None, max_length=16)
    tool_policy: dict[str, str] | None = None
    model_policy: dict[str, str] | None = None


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    role: str | None = Field(None, max_length=500)
    instructions: str | None = Field(None, max_length=4000)
    icon: str | None = Field(None, max_length=16)
    tool_policy: dict[str, str] | None = None
    model_policy: dict[str, str] | None = None


async def agents_list(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    from tasks import DESTRUCTIVE_TOOLS, READ_ONLY_TOOLS
    return {
        "agents": [_serialize(a) for a in await user_agents(db, user.id)],
        # What the policy editor can configure, with each tool's default.
        "tools": [{"name": t, "default": "allow"} for t in sorted(READ_ONLY_TOOLS)]
                 + [{"name": t, "default": "require_approval"} for t in sorted(DESTRUCTIVE_TOOLS)],
    }


async def agents_create(body: AgentCreate, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    existing = await user_agents(db, user.id)
    if len(existing) >= MAX_AGENTS_PER_USER:
        raise HTTPException(422, f"at most {MAX_AGENTS_PER_USER} agents")
    name = body.name.strip()
    if any(a.name.lower() == name.lower() for a in existing):
        raise HTTPException(409, "an agent with that name already exists")
    agent = Agent(
        id=_new_id(), user_id=user.id, name=name, role=body.role, instructions=body.instructions,
        icon=body.icon, is_default=False, tool_policy=_validate_policy(body.tool_policy),
        model_policy=body.model_policy,
    )
    db.add(agent)
    await db.commit()
    return _serialize(agent)


async def agents_update(
    agent_id: str, body: AgentUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
):
    agent = await get_user_agent(db, user.id, agent_id)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        name = fields["name"].strip()
        others = [a for a in await user_agents(db, user.id) if a.id != agent.id]
        if any(a.name.lower() == name.lower() for a in others):
            raise HTTPException(409, "an agent with that name already exists")
        fields["name"] = name
    if "tool_policy" in fields:
        fields["tool_policy"] = _validate_policy(fields["tool_policy"])
    for k, v in fields.items():
        setattr(agent, k, v)
    agent.updated_at = _now()
    await db.commit()
    return _serialize(agent)


async def agents_delete(agent_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    agent = await get_user_agent(db, user.id, agent_id)
    if agent.is_default:
        raise HTTPException(422, "the default agent can't be deleted")
    # Existing tasks keep running under the default agent's policy.
    await db.execute(update(BackgroundTask).where(BackgroundTask.agent_id == agent.id).values(agent_id=None))
    await db.delete(agent)
    await db.commit()
    return {"ok": True}


# ── Router ────────────────────────────────────────────────────────────────────

_ROUTER_PROMPT = """You route a user's request to one of their assistant agents.
Agents (name — what it's for):
{agents}

Reply with JSON only: {{"agent": "<exact agent name>"}}. If none clearly fits, use "{default}"."""


async def route_utterance(db: AsyncSession, user_id: int, utterance: str) -> dict:
    """Pick the agent for a request: explicit mention → classifier → default."""
    agents = await user_agents(db, user_id)
    default = next(a for a in agents if a.is_default)
    specialized = [a for a in agents if not a.is_default]

    def result(a: Agent, confidence: float, reason: str) -> dict:
        return {"agent_id": a.id, "agent_name": a.name, "confidence": confidence, "reason_code": reason}

    text = utterance.lower()
    for a in sorted(specialized, key=lambda a: -len(a.name)):
        if re.search(rf"\b{re.escape(a.name.lower())}\b", text):
            return result(a, 1.0, "explicit_mention")
    if not specialized:
        return result(default, 1.0, "only_default")

    listing = "\n".join(f"- {a.name} — {a.role or 'no description'}" for a in agents)
    try:
        out = await model_gateway.complete_json("router", [
            {"role": "system", "content": _ROUTER_PROMPT.format(agents=listing, default=default.name)},
            {"role": "user", "content": utterance[:2000]},
        ])
        picked = str(out.get("agent", "")).strip().lower()
        for a in agents:
            if a.name.lower() == picked:
                return result(a, 0.7, "classifier")
    except Exception:
        _log.warning("router classifier failed; using default agent", exc_info=True)
        return result(default, 0.0, "classifier_failed")
    return result(default, 0.5, "default")


class RouteRequest(BaseModel):
    utterance: str = Field(..., min_length=1, max_length=4000)


async def route(body: RouteRequest, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return await route_utterance(db, user.id, body.utterance)
