"""
Uxie Backend — FastAPI entry point.

Endpoints:
  POST /auth/send-otp
  POST /auth/verify-otp
  POST /llm/stream
  POST /llm/chat
  POST /stt/session
  POST /referral/redeem
  GET  /referral/stats
  GET  /user/status
  GET  /health
"""

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

# Sentry — wire BEFORE any FastAPI app is instantiated so the
# integration's middleware hooks fire on every request. DSN from env
# var so we can keep the same code path in dev (no env var → no-op).
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        # Don't pay for performance traces yet — error reporting only.
        # Bump these knobs once we know the volume.
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        # Don't auto-attach user emails / IPs; we'll set user context
        # explicitly per-request via current_user dependency.
        send_default_pii=False,
        environment=os.getenv("RAILWAY_ENVIRONMENT", "production"),
        release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown")[:7],
    )

import admin
import auth
import debug
import limits
import proxy
import referral
from auth import current_user
from db import User, get_db, init_db


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_log = logging.getLogger("main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        _log.info("Database ready")
    except Exception as e:
        _log.critical(
            f"Database connection failed: {e}\n"
            "ACTION REQUIRED: Add a PostgreSQL service in Railway and ensure "
            "DATABASE_URL is set in this service's environment variables."
        )
        raise

    # Scheduled-task cron worker (v1.2 — Morning Brief etc). Runs forever
    # until lifespan exits; cancellation is best-effort.
    import scheduled_tasks as _sched
    cron_task = __import__("asyncio").create_task(_sched.cron_worker())

    # Durable background-task worker. Leases queued tasks and resumes ones
    # whose worker died (task_runtime.py).
    import task_runtime as _runtime
    task_worker = __import__("asyncio").create_task(_runtime.worker_loop())

    try:
        yield
    finally:
        for bg in (cron_task, task_worker):
            bg.cancel()
            try:
                await bg
            except BaseException:
                pass
        await proxy.close_http()


app = FastAPI(title="Uxie Backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Electron renderer runs as file://
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _timing(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        dt_ms = (time.perf_counter() - t0) * 1000
        _log.exception("%s %s EXC %.0fms", request.method, request.url.path, dt_ms)
        raise
    dt_ms = (time.perf_counter() - t0) * 1000
    # Health gets a lot of traffic from uptime pings — keep it quiet
    if request.url.path != "/health":
        _log.info("%s %s %d %.0fms", request.method, request.url.path, resp.status_code, dt_ms)
    return resp


# ── Auth ──────────────────────────────────────────────────────────────────────

app.add_api_route("/auth/send-otp", auth.send_otp, methods=["POST"])
app.add_api_route("/auth/verify-otp", auth.verify_otp, methods=["POST"])


# ── LLM + STT proxy ───────────────────────────────────────────────────────────

app.add_api_route("/llm/stream", proxy.llm_stream, methods=["POST"])
app.add_api_route("/llm/chat",   proxy.llm_chat,   methods=["POST"])
app.add_api_route("/llm/structure-meeting", proxy.llm_structure_meeting, methods=["POST"])
app.add_api_route("/stt/session", proxy.stt_session, methods=["POST"])


# ── Referral ──────────────────────────────────────────────────────────────────

app.add_api_route("/referral/redeem", referral.redeem_referral, methods=["POST"])
app.add_api_route("/referral/stats", referral.get_referral_stats, methods=["GET"])


# ── Admin dashboard ───────────────────────────────────────────────────────────

app.add_api_route("/admin/dashboard",    admin.dashboard_html, methods=["GET"], response_class=__import__("fastapi").responses.HTMLResponse)
app.add_api_route("/admin/stats.json",   admin.stats_json,     methods=["GET"])
app.add_api_route("/admin/users.json",   admin.users_json,     methods=["GET"])
app.add_api_route("/admin/sessions.json", admin.sessions_json, methods=["GET"])
app.add_api_route("/admin/meetings.json", admin.meetings_json, methods=["GET"])
app.add_api_route("/admin/user/{user_id}.json", admin.user_detail_json, methods=["GET"])
app.add_api_route("/admin/audio/{session_row_id}", admin.audio_redirect, methods=["GET"])


# ── Debug (client audio upload) ───────────────────────────────────────────────

app.add_api_route("/debug/upload-audio", debug.upload_audio, methods=["POST"])
app.add_api_route("/meetings/upload", debug.upload_meeting, methods=["POST"])


# ── User status ───────────────────────────────────────────────────────────────

@app.get("/user/status")
async def user_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    usage = await limits.get_usage_summary(db, user)
    return {
        "user_id": user.id,
        "email": user.email,
        **usage,
        "referral_code": user.referral_code,
        "referral_link": f"https://uxie.ai/r/{user.referral_code}",
    }


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import text
    from settings import get_settings
    from fastapi import Response
    import fastapi

    s = get_settings()
    dg = (s.deepgram_api_key or "").strip()

    # Probe DB with a lightweight query
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "unreachable — add PostgreSQL in Railway",
        "deepgram": "ok" if dg else "NOT SET — add DEEPGRAM_API_KEY in Railway",
        "deepgram_key_prefix": (dg[:8] + "...") if dg else None,
        "groq": "ok" if s.groq_api_key else "NOT SET",
        "openai": "ok" if s.openai_api_key else "NOT SET",
    }

    status_code = 200 if db_ok else 503
    return fastapi.responses.JSONResponse(content=payload, status_code=status_code)


# ── iOS additions (Phase 0) ──────────────────────────────────────────────────
# Strictly additive: new endpoints + new tables. Mac/Windows clients ignore
# everything in this block; their existing flow is unchanged. Importing
# `db_ios` registers the new SQLAlchemy models on `Base.metadata` BEFORE
# init_db() runs (lifespan), so create_all picks them up on next deploy.

import db_ios  # noqa: F401, E402 — import-for-side-effect (registers models)
import agent as _ios_agent  # noqa: E402
import history as _ios_history  # noqa: E402
import auth_refresh as _ios_auth_refresh  # noqa: E402

# /agent/* — server-side tool-calling loop with SSE streaming
app.add_api_route("/agent/execute", _ios_agent.execute, methods=["POST"])
app.add_api_route("/agent/approve/{session_id}", _ios_agent.approve, methods=["POST"])
app.add_api_route(
    "/agent/client_tool_result/{session_id}/{tool_call_id}",
    _ios_agent.client_tool_result,
    methods=["POST"],
)

# /history/* — cross-device conversation persistence
app.add_api_route("/history", _ios_history.list_conversations, methods=["GET"])
app.add_api_route("/history/{conversation_id}", _ios_history.conversation_detail, methods=["GET"])
app.add_api_route("/history/{conversation_id}", _ios_history.delete_conversation, methods=["DELETE"])

# /auth/refresh — refresh-token rotation (iOS only; existing /auth/verify-otp unchanged)
app.add_api_route("/auth/issue-refresh", _ios_auth_refresh.issue_refresh, methods=["POST"])
app.add_api_route("/auth/refresh", _ios_auth_refresh.refresh, methods=["POST"])

# /oauth/google/* — connector OAuth for Gmail (and later Calendar / Drive)
import oauth_google as _oauth_google  # noqa: E402
app.add_api_route("/oauth/google/start", _oauth_google.start, methods=["GET"])
app.add_api_route(
    "/oauth/google/callback",
    _oauth_google.callback,
    methods=["GET"],
    name="oauth_google_callback",
)

# /oauth/slack/* — same shape as Google. Requires SLACK_CLIENT_ID +
# SLACK_CLIENT_SECRET env vars on Railway; start endpoint 500s clearly
# if either is missing.
import oauth_slack as _oauth_slack  # noqa: E402
app.add_api_route("/oauth/slack/start",    _oauth_slack.start,    methods=["GET"])
app.add_api_route("/oauth/slack/callback", _oauth_slack.callback, methods=["GET"], name="oauth_slack_callback")

# /user/connections — list providers the user has connected (used by iOS Connectors UI)
import connectors as _connectors_pkg  # noqa: E402
from auth import current_user as _current_user  # noqa: E402
from db import get_db as _get_db  # noqa: E402
from sqlalchemy import select as _select  # noqa: E402
from db_ios import OAuthToken as _OAuthToken  # noqa: E402

async def _list_connections(
    user=Depends(_current_user),
    db=Depends(_get_db),
):
    rows = (await db.execute(
        _select(_OAuthToken.provider).where(_OAuthToken.user_id == user.id)
    )).scalars().all()
    return {"connected": list(rows)}

async def _disconnect(
    provider: str,
    user=Depends(_current_user),
    db=Depends(_get_db),
):
    row = (await db.execute(
        _select(_OAuthToken).where(
            _OAuthToken.user_id == user.id, _OAuthToken.provider == provider
        )
    )).scalar_one_or_none()
    if row is None:
        return {"ok": True, "already_disconnected": True}
    await db.delete(row)
    await db.commit()
    return {"ok": True}

app.add_api_route("/user/connections", _list_connections, methods=["GET"])
app.add_api_route("/user/connections/{provider}", _disconnect, methods=["DELETE"])

# /user/connector_token/{provider} — hand a short-lived access_token to a
# JWT-authenticated client. Mac engine uses this so it never needs the
# provider's client_secret baked into the DMG.
from connector_tokens import connector_token as _connector_token  # noqa: E402
app.add_api_route("/user/connector_token/{provider}", _connector_token, methods=["GET"])

# /tasks/* — background agent tasks (v1.1.0). Detached from the HTTP
# request; client polls /tasks/{id} for progress.
import tasks as _tasks  # noqa: E402
app.add_api_route("/tasks/create",      _tasks.tasks_create, methods=["POST"])
app.add_api_route("/tasks",             _tasks.tasks_list,   methods=["GET"])
app.add_api_route("/tasks/stream",      _tasks.tasks_stream, methods=["GET"])  # before /tasks/{task_id}
app.add_api_route("/tasks/{task_id}",   _tasks.tasks_get,    methods=["GET"])
app.add_api_route("/tasks/{task_id}/cancel", _tasks.tasks_cancel, methods=["POST"])
app.add_api_route("/tasks/{task_id}/approve", _tasks.tasks_approve, methods=["POST"])

# /scheduled_tasks/* — recurring user workflows (Morning Brief etc, v1.2)
import scheduled_tasks as _sched_routes  # noqa: E402
app.add_api_route("/scheduled_tasks",        _sched_routes.scheduled_list,   methods=["GET"])
app.add_api_route("/scheduled_tasks",        _sched_routes.scheduled_create, methods=["POST"])
app.add_api_route("/scheduled_tasks/{st_id}", _sched_routes.scheduled_patch,  methods=["PATCH"])
app.add_api_route("/scheduled_tasks/{st_id}", _sched_routes.scheduled_delete, methods=["DELETE"])
app.add_api_route("/scheduled_tasks/{st_id}/fire", _sched_routes.scheduled_fire_now, methods=["POST"])
