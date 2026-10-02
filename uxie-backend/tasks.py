"""Background-task API.

Lets a client describe a long-running task ("summarize my unread Gmail and
draft a reply to the urgent ones") and walk away. The agent loop runs
detached from the HTTP request — once /tasks/create returns, the task
keeps executing on Railway even if the client disconnects, the Mac sleeps,
or the user quits Uxie. Client polls /tasks/{id} for progress.

Endpoints:
    POST  /tasks/create       — create a task, returns its id immediately
    GET   /tasks              — list the caller's recent tasks
    GET   /tasks/{id}         — current state + ordered event log
    POST  /tasks/{id}/cancel  — request cancellation (best-effort)

Design choices for v1.1.0:
    - Polling, not SSE. Client polls /tasks/{id} every 2s while running.
      SSE is on the roadmap (v1.2) but polling is simpler to ship and
      survives connection drops gracefully.
    - Read-only tools only. Sending email, creating events, posting to
      Slack, etc. are gated behind an approval flow we'll wire in v1.2.
      v1.1.0 ships safe tools: gmail_search/gmail_read, calendar_list_events,
      drive_search/drive_read.
    - Per-user burst limit (10/hour, 30/day) on top of the monthly
      `command` quota. Background tasks consume Groq/OpenAI tokens fast
      so we double-protect against a stolen JWT.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user
from db import SessionLocal, User, get_db
import computer_use
import model_gateway
from events import WAIT_TOOL_SCHEMAS, WAIT_TOOLS, build_wait
from db_ios import Agent, BackgroundTask, TaskApproval, TaskEvent
from limits import check_and_increment, check_burst
from proxy import _llm_base_and_key, get_http
from settings import get_settings

try:
    import connectors as _connectors
except Exception:  # noqa: BLE001
    _connectors = None  # type: ignore[assignment]

_log = logging.getLogger("tasks")
_settings = get_settings()

# Hard cap on agent turns. Each turn = one LLM call. 8 is generous — most
# multi-step tasks finish in 3-5 turns. Without this, an LLM stuck in a
# tool-call loop could burn unbounded tokens.
MAX_TASK_TURNS = 8

# Read-only tools the LLM can call freely — no approval required.
READ_ONLY_TOOLS: set[str] = {
    "gmail_search", "gmail_read",
    "calendar_list_events", "calendar_check_availability",
    "drive_search", "drive_read", "drive_list",
    "slack_search", "slack_list_channels", "slack_read_channel",
}

# Destructive tools — the LLM CAN call these in background tasks, but
# we park execution until the user clicks Approve in the Tasks tab.
# Each gets a 5-minute approval window before auto-cancelling.
DESTRUCTIVE_TOOLS: set[str] = {
    "gmail_send", "gmail_reply", "gmail_draft",
    "slack_send_message",
    "calendar_create_event",
}

# Union — what the agent loop will accept.
# Cloud-desktop computer use (computer_use.py). It gates its own risky steps.
COMPUTER_TOOLS: set[str] = {"use_computer"}

ALL_ALLOWED_TOOLS: set[str] = READ_ONLY_TOOLS | DESTRUCTIVE_TOOLS | WAIT_TOOLS | COMPUTER_TOOLS

# Approval window for a parked destructive tool call before auto-cancel.
APPROVAL_TIMEOUT_S = 300


# Per-user burst limit for /tasks/create. Belt-and-braces on top of the
# monthly command counter.
BURST_PER_HOUR = 10
BURST_PER_DAY = 30


# ── ULID-ish id ───────────────────────────────────────────────────────────────


def _ulid() -> str:
    """Lexicographically-sortable id (millisecond timestamp + random suffix)."""
    millis = int(time.time() * 1000)
    return f"{millis:013d}_{secrets.token_hex(6)}"


# ── Event persistence ────────────────────────────────────────────────────────


async def _append_event(
    db: AsyncSession, task_id: str, kind: str, data: dict | None = None
) -> None:
    """Append one row to task_events. Caller commits."""
    # seq is one greater than the current max for this task.
    last = (await db.execute(
        select(TaskEvent.seq).where(TaskEvent.task_id == task_id)
        .order_by(desc(TaskEvent.seq)).limit(1)
    )).scalar_one_or_none()
    next_seq = (last or 0) + 1 if last is not None else 0
    db.add(TaskEvent(task_id=task_id, seq=next_seq, kind=kind, data=data or {}))


async def _update_task_status(
    db: AsyncSession,
    task_id: str,
    *,
    status: str | None = None,
    result_md: str | None = None,
    error: str | None = None,
    completed: bool = False,
) -> None:
    """Patch the BackgroundTask row. Caller commits."""
    task = (await db.execute(
        select(BackgroundTask).where(BackgroundTask.id == task_id)
    )).scalar_one_or_none()
    if task is None:
        return
    if status is not None:
        task.status = status
    if result_md is not None:
        task.result_md = result_md
    if error is not None:
        task.error = error
    task.updated_at = datetime.now(timezone.utc)
    if completed:
        task.completed_at = datetime.now(timezone.utc)


# ── Tool schema filtering ─────────────────────────────────────────────────────


async def _allowed_tool_schemas(db: AsyncSession, user_id: int) -> list[dict]:
    """Tools the agent can see: read-only + destructive (gated). The
    destructive ones park on approval before actually executing."""
    if _connectors is None:
        return []
    try:
        all_schemas = await _connectors.tool_schemas_for_user(db, user_id)
    except Exception:
        _log.warning("connector schema lookup failed", exc_info=True)
        return []
    return [s for s in all_schemas if s.get("function", {}).get("name") in ALL_ALLOWED_TOOLS]


# ── Approval gate ────────────────────────────────────────────────────────────
# Persisted in task_approvals so a pending approval survives a restart and
# can be resolved from any replica. The task loop polls its row with a
# short-lived session of its own (the loop's session is shared across the
# parallel tool calls in a turn, so it can't be used concurrently).

APPROVAL_POLL_S = 2.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _park_for_approval(
    task_id: str, tool_call_id: str, tool: str, timeout_s: float,
) -> tuple[str, dict | None, str | None]:
    """Wait until the user decides on this tool call. Returns
    (status, edited_args, stored_result) where status is one of
    approved | denied | executed | timeout. `executed` means a previous
    attempt of this task already ran the call — reuse `stored_result`."""
    while True:
        async with SessionLocal() as s:
            row = (await s.execute(select(TaskApproval).where(
                TaskApproval.task_id == task_id, TaskApproval.tool_call_id == tool_call_id,
            ))).scalar_one_or_none()
            if row is None:
                s.add(TaskApproval(task_id=task_id, tool_call_id=tool_call_id, tool=tool))
                await s.commit()
            elif row.status in ("approved", "denied", "executed"):
                return row.status, row.edited_args, row.result
            elif datetime.now(timezone.utc) - _aware(row.created_at) > timedelta(seconds=timeout_s):
                row.status = "denied"
                row.decided_at = datetime.now(timezone.utc)
                await s.commit()
                return "timeout", None, None
        await asyncio.sleep(APPROVAL_POLL_S)


def _with_context(prompt: str, ctx: dict | None) -> str:
    """Prefix the task prompt with the desktop context it was created in."""
    if not ctx:
        return prompt
    labels = [("app", "App"), ("window_title", "Window"), ("url", "URL"), ("selected_text", "Selected text")]
    lines = [f"{label}: {ctx[k]}" for k, label in labels if ctx.get(k)]
    return ("[Desktop context when the user asked — use it to resolve 'this', 'here', 'that']\n"
            + "\n".join(lines) + "\n\n" + prompt)


def _effective_policy(agent: Agent | None) -> dict[str, str]:
    """tool → allow | require_approval | deny. Built-in defaults (read-only
    allowed, destructive gated) overlaid with the agent's own policy."""
    policy = {t: "allow" for t in READ_ONLY_TOOLS | WAIT_TOOLS | COMPUTER_TOOLS}
    policy.update({t: "require_approval" for t in DESTRUCTIVE_TOOLS})
    if agent is not None and agent.tool_policy:
        policy.update({k: v for k, v in agent.tool_policy.items() if k in policy})
    return policy


async def _load_agent(db: AsyncSession, user_id: int, agent_id: str | None) -> Agent:
    import agents as _agents
    if agent_id:
        agent = (await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id)
        )).scalar_one_or_none()
        if agent is not None:
            return agent
    return await _agents.ensure_default_agent(db, user_id)


async def _auto_approve(task_id: str, tool_call_id: str, tool: str) -> None:
    async with SessionLocal() as s:
        exists = (await s.execute(select(TaskApproval.id).where(
            TaskApproval.task_id == task_id, TaskApproval.tool_call_id == tool_call_id,
        ))).scalar_one_or_none()
        if exists is None:
            s.add(TaskApproval(task_id=task_id, tool_call_id=tool_call_id, tool=tool,
                               status="approved", decided_at=datetime.now(timezone.utc)))
            await s.commit()


async def _event(task_id: str, kind: str, data: dict) -> None:
    """Append a task event from outside the loop's own session."""
    async with SessionLocal() as s:
        await _append_event(s, task_id, kind, data)
        await s.commit()


class _ComputerHooks(computer_use.Hooks):
    def __init__(self, task_id: str, call_id: str):
        self.task_id, self.call_id = task_id, call_id

    async def event(self, kind: str, data: dict) -> None:
        await _event(self.task_id, kind, {**data, "call_id": self.call_id})

    async def ask(self, call_id: str, tool: str, summary: str, timeout_s: float) -> bool:
        await _event(self.task_id, "approval_needed", {"id": call_id, "name": tool, "args": {}, "summary": summary})
        decision, _, _ = await _park_for_approval(self.task_id, call_id, tool, timeout_s)
        await _event(self.task_id, "approval_resolved", {"id": call_id, "name": tool, "approved": decision == "approved"})
        return decision == "approved"


async def _run_computer(task_id: str, call_id: str, args: dict) -> str:
    """Run (or resume) a computer-use session for one use_computer call.
    A finished session's result is reused; an unfinished one reconnects to
    its sandbox."""
    if not computer_use.available():
        return "Computer use isn't configured on this server."
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return "use_computer needs a goal."
    async with SessionLocal() as s:
        rows = (await s.execute(select(TaskEvent.kind, TaskEvent.data).where(
            TaskEvent.task_id == task_id, TaskEvent.kind.in_(["computer_session", "computer_result"]),
        ).order_by(TaskEvent.seq))).all()
    sandbox_id = None
    for kind, data in rows:
        if (data or {}).get("call_id") != call_id:
            continue
        if kind == "computer_result":
            return data.get("result", "")
        sandbox_id = data.get("sandbox_id")
    try:
        result = await computer_use.run_session(
            goal, hooks=_ComputerHooks(task_id, call_id),
            start_url=args.get("start_url"), sandbox_id=sandbox_id,
        )
    except Exception as e:  # noqa: BLE001
        _log.exception("computer session failed for task %s", task_id)
        result = f"The computer session failed: {str(e)[:300]}"
    await _event(task_id, "computer_result", {"call_id": call_id, "result": result[:8000]})
    return result


async def _mark_executed(task_id: str, tool_call_id: str, result: str) -> None:
    async with SessionLocal() as s:
        await s.execute(update(TaskApproval).where(
            TaskApproval.task_id == task_id, TaskApproval.tool_call_id == tool_call_id,
        ).values(status="executed", result=result))
        await s.commit()


async def _save_checkpoint(db: AsyncSession, task_id: str, messages: list[dict], turn: int) -> None:
    """Persist conversation state so a reclaimed task resumes here. Commits."""
    await db.execute(update(BackgroundTask).where(BackgroundTask.id == task_id).values(
        checkpoint={"messages": list(messages), "turn": turn},
        updated_at=datetime.now(timezone.utc),
    ))
    await db.commit()


# ── Agent loop ────────────────────────────────────────────────────────────────


SYSTEM_PROMPT_TEMPLATE = """You are Uxie's background-task agent. The user has given you a task to
run in the background while they keep working.

AVAILABLE TOOLS (you MUST use these — do not say "I can't access X" if
a tool for X is in this list):
{tool_list}

Hard rules:
1. ALWAYS call the relevant tool when the user's request maps to one of
   the available tools. NEVER respond with "I can't check your calendar"
   or "you'll need to look that up" when a tool for that exact thing is
   listed above. Use the tool.
2. Plan briefly, then act. Don't narrate every step.
3. Don't ask the user clarifying questions — they're not around to answer.
   Make a reasonable assumption and proceed.
4. Be thorough. Multiple tool calls are encouraged — search first, read
   the relevant results, then summarize.
5. End with a clear Markdown summary of what you found. Use headings,
   bullets, and action items where it helps the user scan.
6. If a tool returns "user has not connected X" or a permission error,
   tell the user to open Settings → Connectors and reconnect that
   provider — don't try to guess the answer without the tool.
7. You have a hard cap of 8 turns. Prioritize getting useful information
   into the summary over thoroughness.

You cannot send messages, create events, or do anything destructive in
this mode — only the read-only tools above are available.
"""


def _build_system_prompt(tool_schemas: list[dict], agent: Agent | None = None) -> str:
    """Render SYSTEM_PROMPT_TEMPLATE with the actual list of tools the LLM
    has access to. Listing them inline (not just via the OpenAI tools=…
    field) measurably improves tool-call rates."""
    if not tool_schemas:
        tool_list = "(no tools are currently available — the user has not connected any data providers)"
    else:
        lines = []
        for s in tool_schemas:
            fn = s.get("function") or {}
            name = fn.get("name", "?")
            desc = (fn.get("description") or "").strip().split("\n")[0][:120]
            lines.append(f"  • {name} — {desc}")
        tool_list = "\n".join(lines)
    prompt = SYSTEM_PROMPT_TEMPLATE.format(tool_list=tool_list)
    if agent is not None and not agent.is_default:
        prompt += f"\nYou are acting as the user's agent \"{agent.name}\""
        prompt += f" ({agent.role}).\n" if agent.role else ".\n"
    if agent is not None and agent.instructions:
        prompt += "\nThe user's standing instructions for this agent:\n" + agent.instructions + "\n"
    return prompt


async def _run_task_loop(task_id: str, user_id: int, prompt: str) -> None:
    """The detached background loop. Owns its own DB session so it
    survives after the HTTP request that created the task has returned."""
    async with SessionLocal() as db:
        # Resolve user (we have user_id from the request context).
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            _log.error(f"task {task_id}: user {user_id} not found")
            return

        try:
            await _update_task_status(db, task_id, status="running")

            task_row = (await db.execute(
                select(BackgroundTask).where(BackgroundTask.id == task_id)
            )).scalar_one_or_none()
            agent = await _load_agent(db, user_id, task_row.agent_id if task_row else None)
            policy = _effective_policy(agent)
            connected_schemas = await _allowed_tool_schemas(db, user_id)
            tool_schemas = [
                s for s in connected_schemas + WAIT_TOOL_SCHEMAS
                + ([computer_use.TOOL_SCHEMA] if computer_use.available() else [])
                if policy.get(s.get("function", {}).get("name")) != "deny"
            ]

            # If the user hasn't connected any data providers, fail
            # immediately with a clear message rather than let the LLM
            # hallucinate / refuse a useless answer.
            if not connected_schemas:
                msg = (
                    "You haven't connected any data providers yet. "
                    "Open **Settings → Connectors** and connect Google (Gmail + Calendar + Drive) "
                    "to enable background tasks."
                )
                await _append_event(db, task_id, "final_text", {"text": msg})
                await _update_task_status(
                    db, task_id, status="completed", result_md=msg, completed=True,
                )
                await db.commit()
                return

            # Log which tools the agent actually saw — invaluable for
            # debugging "the LLM didn't call X" without forcing the user
            # to share their event log.
            tool_names = [s.get("function", {}).get("name", "?") for s in tool_schemas]
            await _append_event(db, task_id, "step_start", {
                "step": "agent_loop",
                "available_tools": tool_names,
            })
            await db.commit()

            # Resume from the last checkpoint if a previous worker died
            # mid-task (task_runtime reclaims expired leases).
            row = (await db.execute(
                select(BackgroundTask).where(BackgroundTask.id == task_id)
            )).scalar_one_or_none()
            cp = (row.checkpoint if row is not None else None) or {}
            messages: list[dict] = cp.get("messages") or [
                {"role": "system", "content": _build_system_prompt(tool_schemas, agent)},
                {"role": "user", "content": _with_context(prompt, task_row.desktop_context if task_row else None)},
            ]
            start_turn = int(cp.get("turn") or 0)

            provider, model = model_gateway.resolve(
                "task_planner", (agent.model_policy or {}).get("task_planner") if agent else None,
            )
            base_url, api_key = _llm_base_and_key(provider)
            http = get_http()
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            final_text: str | None = None

            for turn in range(start_turn, MAX_TASK_TURNS):
                # Refresh the row at top of every turn so a cancel request
                # (POST /tasks/{id}/cancel) takes effect on the next iteration.
                fresh = (await db.execute(
                    select(BackgroundTask).where(BackgroundTask.id == task_id)
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()
                if fresh is None or fresh.status == "cancelled":
                    await _append_event(db, task_id, "step_start", {"step": "cancelled"})
                    await db.commit()
                    return

                last = messages[-1] if messages else {}
                if last.get("role") == "assistant" and last.get("tool_calls"):
                    # Crashed after the LLM asked for tools but before the
                    # results were saved — re-run those same calls. Destructive
                    # ones are idempotent via task_approvals.
                    tool_calls = last["tool_calls"]
                else:
                    payload: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2,
                    }
                    if tool_schemas:
                        payload["tools"] = tool_schemas
                        # Force a tool call on the first turn so GPT-4o doesn't
                        # bail out with "I can't access your calendar" — even
                        # when the tool list is in the prompt, the model
                        # sometimes ignores it under "auto". After the first
                        # turn we switch to "auto" so the model can synthesize
                        # a final summary from the tool results.
                        payload["tool_choice"] = "required" if turn == 0 else "auto"

                    resp = await http.post(
                        f"{base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=90,
                    )
                    if resp.status_code != 200:
                        text = resp.text[:300]
                        await _append_event(db, task_id, "error", {"http": resp.status_code, "body": text})
                        await _update_task_status(
                            db, task_id, status="failed",
                            error=f"LLM call failed ({resp.status_code})",
                            completed=True,
                        )
                        await db.commit()
                        return

                    data = resp.json()
                    choice = (data.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    content = msg.get("content")
                    tool_calls = msg.get("tool_calls") or []

                    # Append assistant message verbatim (with tool_calls) so the
                    # next turn can include it in conversation history.
                    assistant_message: dict[str, Any] = {"role": "assistant"}
                    if content is not None:
                        assistant_message["content"] = content
                    if tool_calls:
                        assistant_message["tool_calls"] = tool_calls
                    messages.append(assistant_message)

                    if content:
                        await _append_event(db, task_id, "thinking", {"text": content[:4000]})

                    # No tool calls → terminal turn.
                    if not tool_calls:
                        final_text = content or ""
                        break

                    await _save_checkpoint(db, task_id, messages, turn)

                # Execute tool calls. Read-only ones run in parallel via
                # asyncio.gather (the "v1 boss/worker" — multiple finds at
                # once, real worker decomposition later). Destructive ones
                # serialize through the approval gate so we don't surprise-
                # send 4 emails before the user can blink.
                waits: list[dict] = []  # wait tools called this turn (events.py)

                async def _execute_one(tc: dict) -> dict:
                    tc_id = tc.get("id") or _ulid()
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}

                    if name not in ALL_ALLOWED_TOOLS or policy.get(name) == "deny":
                        await _append_event(db, task_id, "tool_call", {
                            "id": tc_id, "name": name, "args": args, "rejected": True,
                        })
                        return {
                            "tool_call_id": tc_id,
                            "content": f"Tool {name!r} is not available in background tasks.",
                        }

                    if name in COMPUTER_TOOLS:
                        await _append_event(db, task_id, "tool_call", {"id": tc_id, "name": name, "args": args})
                        await db.commit()
                        result_str = await _run_computer(task_id, tc_id, args)
                        await _append_event(db, task_id, "tool_result", {
                            "id": tc_id, "name": name, "ok": True, "result_preview": result_str[:1000],
                        })
                        return {"tool_call_id": tc_id, "content": result_str}

                    if name in WAIT_TOOLS:
                        wait, msg = build_wait(name, args)
                        await _append_event(db, task_id, "tool_call", {"id": tc_id, "name": name, "args": args})
                        if wait is not None:
                            waits.append(wait)
                        return {"tool_call_id": tc_id, "content": msg}

                    # Destructive (or agent-policy-gated) → park for approval.
                    # An agent policy of "allow" pre-approves it, but it still
                    # goes through task_approvals so a resume can't re-run it.
                    gated = name in DESTRUCTIVE_TOOLS or policy.get(name) == "require_approval"
                    if gated:
                        if policy.get(name) == "allow":
                            await _auto_approve(task_id, tc_id, name)
                        else:
                            await _append_event(db, task_id, "approval_needed", {
                                "id": tc_id, "name": name, "args": args,
                                "summary": _summarize_destructive(name, args),
                            })
                            await db.commit()
                        decision, edited_args, stored = await _park_for_approval(
                            task_id, tc_id, name, APPROVAL_TIMEOUT_S,
                        )
                        if decision == "executed":
                            return {"tool_call_id": tc_id, "content": stored or ""}
                        if decision != "approved":
                            await _append_event(db, task_id, "approval_resolved", {
                                "id": tc_id, "name": name, "approved": False,
                            })
                            return {
                                "tool_call_id": tc_id,
                                "content": f"User declined to {name}. Stop trying to run it.",
                            }
                        # Merge edited args (only fields the user changed).
                        if edited_args:
                            args = {**args, **edited_args}
                        await _append_event(db, task_id, "approval_resolved", {
                            "id": tc_id, "name": name, "approved": True, "args": args,
                        })

                    await _append_event(db, task_id, "tool_call", {
                        "id": tc_id, "name": name, "args": args,
                    })

                    if _connectors is None:
                        result_str = "connector registry unavailable"
                        ok = False
                    else:
                        try:
                            ok, result = await _connectors.execute(db, user_id, name, args)
                            result_str = result if isinstance(result, str) else json.dumps(result, default=str)
                        except Exception as e:
                            ok = False
                            raw = str(e)
                            if "401" in raw:
                                result_str = (
                                    "Your Google connection has expired or been revoked. "
                                    "Tell the user: open Settings → Connectors, disconnect Google, then reconnect. "
                                    f"(raw: {raw[:200]})"
                                )
                            elif "403" in raw:
                                result_str = (
                                    "Google denied the request — likely an OAuth scope is missing. "
                                    "Tell the user: disconnect and reconnect Google in Settings → Connectors. "
                                    f"(raw: {raw[:200]})"
                                )
                            else:
                                result_str = f"tool exception: {raw[:500]}"

                    if gated:
                        await _mark_executed(task_id, tc_id, result_str or "")
                    await _append_event(db, task_id, "tool_result", {
                        "id": tc_id, "name": name, "ok": ok,
                        "result_preview": (result_str or "")[:1000],
                    })
                    return {"tool_call_id": tc_id, "content": result_str or ""}

                # Run all read-only calls in parallel. Destructive ones are
                # in the same gather but each parks on its own approval gate,
                # so user can approve them in whatever order they want.
                tool_results = await asyncio.gather(
                    *[_execute_one(tc) for tc in tool_calls],
                    return_exceptions=False,
                )
                for tr in tool_results:
                    messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]})
                await db.commit()
                await _save_checkpoint(db, task_id, messages, turn + 1)
                if waits:
                    # Park: no compute until events.watcher_loop wakes us.
                    await db.execute(update(BackgroundTask).where(BackgroundTask.id == task_id)
                                     .values(waiting_for=waits[0]))
                    await _append_event(db, task_id, "step_start", {"step": "waiting", "waiting_for": waits[0]})
                    await db.commit()
                    return
            else:
                # Hit MAX_TASK_TURNS without an assistant final response.
                final_text = "Reached the maximum turn limit without producing a summary."

            await _append_event(db, task_id, "final_text", {"text": final_text or ""})
            await _update_task_status(
                db, task_id,
                status="completed",
                result_md=final_text or "",
                completed=True,
            )
            await db.commit()
        except Exception as e:
            _log.exception(f"task {task_id} failed")
            try:
                await _append_event(db, task_id, "error", {"message": str(e)})
                await _update_task_status(
                    db, task_id, status="failed", error=str(e)[:1000], completed=True,
                )
                await db.commit()
            except Exception:
                pass


# ── HTTP endpoints ────────────────────────────────────────────────────────────


class TaskCreateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    agent_id: str | None = None  # omit to let the router pick
    desktop_context: dict[str, str] | None = None  # from the engine (Phase 4)

    @field_validator("desktop_context")
    @classmethod
    def _small_context(cls, v):
        if v is None:
            return v
        allowed = {"app", "window_title", "url", "selected_text"}
        v = {k: str(val)[:2000] for k, val in v.items() if k in allowed and val}
        return v or None


async def tasks_create(
    body: TaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Create a task and fire the agent loop in the background."""
    check_burst(user.id, "background_task", per_hour=BURST_PER_HOUR, per_day=BURST_PER_DAY)
    await check_and_increment(db, user, "command")

    import agents as _agents
    if body.agent_id:
        agent = await _agents.get_user_agent(db, user.id, body.agent_id)
        routed = {"agent_id": agent.id, "agent_name": agent.name}
    else:
        routed = await _agents.route_utterance(db, user.id, body.prompt)

    task_id = _ulid()
    db.add(BackgroundTask(
        id=task_id, user_id=user.id, prompt=body.prompt.strip(), status="queued",
        agent_id=routed["agent_id"], desktop_context=body.desktop_context,
    ))
    await db.commit()

    # task_runtime's worker leases and runs it (survives restarts).
    import task_runtime
    task_runtime.wake()
    return {"id": task_id, "status": "queued",
            "agent_id": routed["agent_id"], "agent_name": routed["agent_name"]}


async def tasks_list(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Most recent 50 tasks for the caller."""
    import agents as _agents
    names = {a.id: a.name for a in await _agents.user_agents(db, user.id)}
    pending = set((await db.execute(
        select(TaskApproval.task_id).join(BackgroundTask, BackgroundTask.id == TaskApproval.task_id).where(
            BackgroundTask.user_id == user.id, TaskApproval.status == "pending",
        )
    )).scalars().all())
    rows = (await db.execute(
        select(BackgroundTask).where(BackgroundTask.user_id == user.id)
        .order_by(desc(BackgroundTask.created_at)).limit(50)
    )).scalars().all()
    return {
        "tasks": [
            {
                "id": r.id,
                "prompt": r.prompt,
                "agent_id": r.agent_id,
                "agent_name": names.get(r.agent_id),
                "status": r.status,
                "waiting": r.waiting_for is not None,
                "approval_needed": r.id in pending,
                "result_md": r.result_md,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in rows
        ],
    }


async def tasks_get(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Single task + full event log."""
    import agents as _agents
    names = {a.id: a.name for a in await _agents.user_agents(db, user.id)}
    task = (await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.id == task_id, BackgroundTask.user_id == user.id,
        )
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "task not found")
    events = (await db.execute(
        select(TaskEvent).where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.seq)
    )).scalars().all()
    return {
        "id": task.id,
        "prompt": task.prompt,
        "agent_id": task.agent_id,
        "agent_name": names.get(task.agent_id),
        "status": task.status,
        "waiting_for": task.waiting_for,
        "result_md": task.result_md,
        "error": task.error,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "events": [
            {
                "seq": e.seq,
                "kind": e.kind,
                "data": e.data,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


def _summarize_destructive(name: str, args: dict) -> str:
    """Short human-readable summary of a pending destructive action,
    shown in the approval card."""
    if name == "gmail_send":
        return f"Send email to {args.get('to', '?')} — subject: {(args.get('subject') or '')[:80]}"
    if name == "gmail_reply":
        return f"Reply on thread {args.get('threadId', '?')[:12]}…"
    if name == "gmail_draft":
        return f"Create draft to {args.get('to', '?')} — subject: {(args.get('subject') or '')[:80]}"
    if name == "slack_send_message":
        return f"Post to {args.get('channel', '?')} — {(args.get('text') or '')[:100]}"
    if name == "calendar_create_event":
        return f"Create event '{args.get('title', '?')}' {args.get('start','')} → {args.get('end','')}"
    return f"{name}({', '.join(args.keys())})"


class TaskApproveRequest(BaseModel):
    tool_call_id: str
    approved: bool
    edited_args: dict | None = None


async def tasks_approve(
    task_id: str,
    body: TaskApproveRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Approve or reject a pending destructive tool call inside a task."""
    # Sanity: confirm the task belongs to this user.
    task = (await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.id == task_id, BackgroundTask.user_id == user.id,
        )
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "task not found")
    result = await db.execute(update(TaskApproval).where(
        TaskApproval.task_id == task_id,
        TaskApproval.tool_call_id == body.tool_call_id,
        TaskApproval.status == "pending",
    ).values(
        status="approved" if body.approved else "denied",
        edited_args=body.edited_args,
        decided_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    if result.rowcount == 0:
        return {"ok": False, "reason": "no pending gate — likely timed out or already resolved"}
    return {"ok": True}


async def tasks_cancel(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Mark a task as cancelled. The background loop checks this at the top
    of each turn and exits cleanly — there's no mid-LLM-call cancellation."""
    task = (await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.id == task_id, BackgroundTask.user_id == user.id,
        )
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "task not found")
    if task.status in ("completed", "failed", "cancelled"):
        return {"ok": True, "already_terminal": True, "status": task.status}
    task.status = "cancelled"
    task.updated_at = datetime.now(timezone.utc)
    task.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "status": "cancelled"}


# ── Live updates (SSE) ────────────────────────────────────────────────────────
# Polls Postgres per connection rather than holding in-process pub/sub, so it
# works no matter which replica runs the task. One cheap query every 2s.

STREAM_POLL_S = 2.0
STREAM_PING_S = 15.0


async def _task_snapshot(db: AsyncSession, user_id: int) -> dict[str, dict]:
    """{task_id: {status, approval_needed, result_preview}} for recent tasks."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    rows = (await db.execute(
        select(BackgroundTask.id, BackgroundTask.status, BackgroundTask.result_md, BackgroundTask.prompt,
               BackgroundTask.waiting_for)
        .where(BackgroundTask.user_id == user_id, BackgroundTask.created_at >= since)
        .order_by(desc(BackgroundTask.created_at)).limit(50)
    )).all()
    pending = set((await db.execute(
        select(TaskApproval.task_id).where(
            TaskApproval.task_id.in_([r.id for r in rows]), TaskApproval.status == "pending",
        )
    )).scalars().all()) if rows else set()
    return {
        r.id: {
            "id": r.id,
            "status": r.status,
            "waiting": r.waiting_for is not None,
            "approval_needed": r.id in pending,
            "prompt": (r.prompt or "")[:120],
            "result_preview": (r.result_md or "")[:200],
        }
        for r in rows
    }


def _snapshot_changes(old: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    """Tasks whose status or approval state changed (or appeared)."""
    out = []
    for tid, cur in new.items():
        prev = old.get(tid)
        if prev is None or prev["status"] != cur["status"] or prev["approval_needed"] != cur["approval_needed"] \
                or prev["waiting"] != cur["waiting"]:
            out.append(cur)
    return out


async def tasks_stream(request: Request, user: User = Depends(current_user)):
    """SSE: `event: task-update` whenever one of the caller's recent tasks
    changes status or starts/stops waiting for approval. Pings every 15s."""
    from fastapi.responses import StreamingResponse

    user_id = user.id

    async def gen():
        async with SessionLocal() as db:
            snap = await _task_snapshot(db, user_id)
        last_ping = time.monotonic()
        yield b": connected\n\n"
        while not await request.is_disconnected():
            await asyncio.sleep(STREAM_POLL_S)
            async with SessionLocal() as db:
                new = await _task_snapshot(db, user_id)
            for change in _snapshot_changes(snap, new):
                yield f"event: task-update\ndata: {json.dumps(change)}\n\n".encode()
            snap = new
            if time.monotonic() - last_ping >= STREAM_PING_S:
                yield b": ping\n\n"
                last_ping = time.monotonic()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
