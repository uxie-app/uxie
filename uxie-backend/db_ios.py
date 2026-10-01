"""
iOS-related tables. Imports `Base` from db.py so models register with the same
metadata; importing this module before init_db() runs is enough to make
`Base.metadata.create_all` create the new tables.

Strictly additive — does NOT touch existing tables (users, otps, usage,
referrals, llm_usage, stt_usage, session_log).

Tables:
  conversations   — one per agent thread (cross-device for iOS, eventually desktop too)
  turns           — user / assistant / tool messages within a conversation
  agent_sessions  — currently-running agent loops (state: running | awaiting_* | done | error)
  refresh_tokens  — long-lived refresh tokens (iOS), opaque + sha256-hashed
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True)  # ULID-ish (timestamp + random)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=True)  # auto-derived from first user turn
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    last_active_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    turns = relationship("Turn", back_populates="conversation", cascade="all, delete-orphan", lazy="dynamic")


class Turn(Base):
    __tablename__ = "turns"

    id = Column(String, primary_key=True)
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)        # "user" | "assistant" | "tool"
    text = Column(Text, nullable=True)
    tool_calls_json = Column(JSON, nullable=True)
    tool_call_id = Column(String, nullable=True)  # populated when role="tool"
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)

    conversation = relationship("Conversation", back_populates="turns")


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    state = Column(String, nullable=False)  # running | awaiting_approval | awaiting_client_tool | done | error
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class RefreshToken(Base):
    """Opaque refresh tokens for iOS. The raw token leaves the server only once
    (in the response to /auth/issue-refresh); we store sha256(raw)."""
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    device_id = Column(String, nullable=True)  # client-supplied opaque ID
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_used_at = Column(DateTime(timezone=True), nullable=True)


class BackgroundTask(Base):
    """One row per user-initiated background task. Runs detached from any
    HTTP request — survives Mac sleep/quit because the loop lives on
    Railway. Status flows: queued → running → (completed | failed | cancelled).
    A future state `needs_approval` will gate destructive tool calls (v1.2)."""
    __tablename__ = "background_tasks"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="queued", index=True)
    result_md = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Durable runtime (task_runtime.py). A worker leases the row while it
    # runs; if the process dies the lease expires and another worker
    # resumes from `checkpoint`. Rows with lease_owner NULL are not managed
    # by the runtime (e.g. scheduled-brief shells) and are never reclaimed.
    # Added to existing prod tables by db.ADDITIVE_COLUMNS.
    lease_owner = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    attempt = Column(Integer, nullable=True, default=0)
    checkpoint = Column(JSON, nullable=True)  # {"messages": [...], "turn": int}
    agent_id = Column(String, nullable=True)  # agents.id (migration 0002)
    # Set while the task is parked on an event or time (events.py); status
    # stays "running" so older clients don't see an unknown state. (0003)
    waiting_for = Column(JSON, nullable=True)
    # What the user was looking at when they asked (app, window_title, url,
    # selected_text), captured at hotkey press by the engine. (0004)
    desktop_context = Column(JSON, nullable=True)


class Agent(Base):
    """A persistent, user-visible agent (Phase 2). Every user gets a default
    "Uxie" agent on first use (agents.ensure_default_agent). `tool_policy`
    maps tool name → allow | require_approval | deny; tools not listed fall
    back to the built-in defaults in tasks.py. Created by migration 0002."""
    __tablename__ = "agents"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    role = Column(Text, nullable=True)
    instructions = Column(Text, nullable=True)
    icon = Column(String(16), nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    tool_policy = Column(JSON, nullable=True)
    model_policy = Column(JSON, nullable=True)  # {"task_planner": "<model>"} overrides
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class TaskApproval(Base):
    """Persisted approval gate for a destructive tool call inside a task.
    Survives restarts and works across replicas (the in-memory gate did
    neither). `status`: pending → approved | denied → executed. The stored
    `result` makes execution idempotent when a task resumes mid-turn."""
    __tablename__ = "task_approvals"
    __table_args__ = (UniqueConstraint("task_id", "tool_call_id", name="uq_task_approval_call"),)

    id = Column(Integer, primary_key=True)
    task_id = Column(String, ForeignKey("background_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_call_id = Column(String, nullable=False)
    tool = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    edited_args = Column(JSON, nullable=True)
    result = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    decided_at = Column(DateTime(timezone=True), nullable=True)


class TaskEvent(Base):
    """Append-only log of everything that happens during a background task —
    LLM reasoning, tool calls, tool results, errors, final text. The Tasks
    tab on the Mac polls /tasks/{id} which returns this sequence for live
    progress rendering."""
    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True)
    task_id = Column(String, ForeignKey("background_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)  # ordering within a task; 0-based
    kind = Column(String, nullable=False)  # step_start | tool_call | tool_result | thinking | final_text | error
    data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)


class ScheduledTask(Base):
    """User-configured recurring task. The cron worker on Railway picks
    these up at their scheduled time and spawns a one-off BackgroundTask
    (so each run has its own event log + status, while the schedule
    config lives here).

    `kind` is the workflow template. v1 ships only "morning_brief"; future
    kinds (end_of_day_recap, weekly_digest, custom) just need a new
    handler in scheduled_tasks.py.

    `run_time_local` is "HH:MM" in the user's timezone. The cron worker
    polls every minute; whenever the local time matches a scheduled
    task's run_time and we haven't fired today, we kick it off."""
    __tablename__ = "scheduled_tasks"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, nullable=False)             # "morning_brief" | ...
    enabled = Column(Boolean, nullable=False, default=True)
    run_time_local = Column(String(5), nullable=False)  # "08:00", "21:30", ...
    timezone = Column(String, nullable=False, default="UTC")  # IANA name, e.g. "Asia/Kolkata"
    delivery_json = Column(JSON, nullable=True)       # {"notification": true, "email": true}
    config_json = Column(JSON, nullable=True)         # kind-specific config (subject prefix, scopes, etc.)
    last_fired_at = Column(DateTime(timezone=True), nullable=True)
    last_task_id = Column(String, nullable=True)      # links to background_tasks.id of the most recent run
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class MeetingRecording(Base):
    """Server-side index of meeting recordings uploaded for admin review.
    Audio bytes themselves live in R2; this row holds the metadata + the
    R2 key + a transcript preview so the dashboard can render without
    pulling the full audio.

    Strictly opt-in: clients only upload when the user toggles
    "share meetings with admin" in Settings. Without that, meetings
    stay local (per PRIVACY.md)."""
    __tablename__ = "meeting_recordings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    local_meeting_id = Column(Integer, nullable=False)   # the ID on the user's local meetings.db
    title = Column(String, nullable=False)
    duration_seconds = Column(Integer, nullable=False, default=0)
    audio_r2_key = Column(String, nullable=True)         # filled after R2 upload
    transcript_preview = Column(Text, nullable=True)     # first ~2000 chars; full transcript stays local
    structured_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)


class OAuthToken(Base):
    """Server-side OAuth tokens for connectors (Slack, Google, GitHub, ...).
    One row per (user_id, provider). The OAuth callback flow (TODO) writes
    the row; the agent loop reads it before calling a connector."""
    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False)             # "slack" | "google" | "github" | ...
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, nullable=True, default="Bearer")
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    extra_json = Column(JSON, nullable=True)              # provider-specific bits (e.g. Slack's authed_user, team_id)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)
