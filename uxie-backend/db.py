"""
Database models and async engine setup.

Tables:
  users    — registered users with tier and referral code
  otps     — short-lived 6-digit codes for email auth
  usage    — monthly per-user dictation/command counts
  referrals — referral code redemption log
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from settings import get_settings

_settings = get_settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _async_db_url(url: str) -> str:
    url = url.strip()  # env vars can have trailing newlines
    # Railway provides postgresql:// — asyncpg needs postgresql+asyncpg://
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    # Railway DATABASE_URL uses psycopg2-style sslmode=require; asyncpg needs ssl=require
    url = url.replace("sslmode=require", "ssl=require")
    url = url.replace("sslmode=prefer", "ssl=prefer")
    url = url.replace("sslmode=disable", "ssl=False")
    return url


engine = create_async_engine(_async_db_url(_settings.database_url), echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    tier = Column(String, nullable=False, default="free")  # "free" | "pro"
    referral_code = Column(String, unique=True, nullable=False)
    free_days_remaining = Column(Integer, nullable=False, default=30)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    usages = relationship("Usage", back_populates="user", lazy="dynamic")


class OTP(Base):
    __tablename__ = "otps"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, nullable=False, index=True)
    code = Column(String(6), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    attempts = Column(Integer, nullable=True, default=0)  # wrong guesses; locked at OTP_MAX_ATTEMPTS


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (UniqueConstraint("user_id", "month", name="uq_usage_user_month"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    month = Column(String(7), nullable=False)  # "YYYY-MM"
    dictation_count = Column(Integer, nullable=False, default=0)
    command_count = Column(Integer, nullable=False, default=0)

    user = relationship("User", back_populates="usages")


class Referral(Base):
    __tablename__ = "referrals"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, nullable=False, index=True)
    referrer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    redeemed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    redeemed_at = Column(DateTime(timezone=True), nullable=True)


# ── Usage telemetry (for admin dashboard + cost attribution) ──────────────────

from sqlalchemy import Float


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False)          # "groq" | "openai"
    model = Column(String, nullable=False)             # e.g. "gpt-4o", "llama-3.1-8b-instant"
    action = Column(String, nullable=False)            # "dictation" | "command"
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    cost_usd_est = Column(Float, nullable=False, default=0.0)
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)


class STTUsage(Base):
    __tablename__ = "stt_usage"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False, default="deepgram")
    deepgram_key_id = Column(String, nullable=True)    # Deepgram's api_key_id — reconcile later with their Usage API
    cost_usd_est = Column(Float, nullable=False, default=0.0)  # MVP estimate; reconcile later
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)


class SessionLog(Base):
    """Per-call transcript log for admin debugging. Captures raw STT text
    (the user message we got from the client) and the LLM's corrected output.
    Audio is attached separately as an R2 object key once client uploads it."""
    __tablename__ = "session_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String, nullable=True, index=True)  # client-generated UUID for audio correlation
    action = Column(String, nullable=False)             # "dictation" | "command"
    provider = Column(String, nullable=False)
    model = Column(String, nullable=False)
    input_text = Column(Text, nullable=False)           # raw STT transcript the client sent us
    output_text = Column(Text, nullable=False)          # LLM's response (dictation: corrected text; command: tool calls JSON)
    audio_r2_key = Column(String, nullable=True)        # filled by /debug/upload-audio when client uploads audio
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_referral_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_db():
    async with SessionLocal() as session:
        yield session


# create_all never alters existing tables, so columns added to a model after
# its table exists in prod are listed here. Additive + nullable only, so the
# previous deploy keeps working during a rolling deploy.
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("otps", "attempts", "INTEGER DEFAULT 0"),
    ("background_tasks", "lease_owner", "VARCHAR"),
    ("background_tasks", "lease_expires_at", "TIMESTAMP WITH TIME ZONE"),
    ("background_tasks", "attempt", "INTEGER DEFAULT 0"),
    ("background_tasks", "checkpoint", "JSON"),
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            for table, column, ddl in ADDITIVE_COLUMNS:
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"
                ))
