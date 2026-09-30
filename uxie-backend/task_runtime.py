"""Durable runner for background tasks.

Replaces the fire-and-forget `asyncio.create_task(_run_task_loop(...))`,
which lost every in-flight task on a Railway deploy or crash.

How it works:
    - POST /tasks/create only inserts a `queued` row and pokes `wake()`.
    - `worker_loop()` (started from main.py's lifespan) claims rows with a
      lease: status=queued, or status=running whose lease has expired
      (the worker that held it died). On Postgres the claim uses
      FOR UPDATE SKIP LOCKED so several replicas never grab the same row.
    - While a task runs, a heartbeat extends the lease. `_run_task_loop`
      saves a checkpoint after every turn, so a reclaimed task resumes
      where it stopped instead of starting over.
    - Rows with lease_owner NULL (scheduled-brief shells, tasks started by
      the pre-runtime code) are never reclaimed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update

import db as _db
from db_ios import BackgroundTask, TaskEvent

_log = logging.getLogger("task_runtime")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
LEASE_S = 120
HEARTBEAT_S = 30
IDLE_POLL_S = 3.0
MAX_CONCURRENT = 8
MAX_ATTEMPTS = 3

_wake = asyncio.Event()


def wake() -> None:
    """Tell the local worker a task was just queued (skips the idle poll)."""
    _wake.set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def claim_one() -> BackgroundTask | None:
    """Lease one runnable task, or return None. Marks tasks that have
    exhausted MAX_ATTEMPTS as failed instead of running them again."""
    async with _db.SessionLocal() as db:
        now = _now()
        stmt = (
            select(BackgroundTask)
            .where(or_(
                BackgroundTask.status == "queued",
                and_(
                    BackgroundTask.status == "running",
                    BackgroundTask.lease_owner.is_not(None),
                    BackgroundTask.lease_expires_at < now,
                ),
            ))
            .order_by(BackgroundTask.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task = (await db.execute(stmt)).scalar_one_or_none()
        if task is None:
            return None

        if (task.attempt or 0) >= MAX_ATTEMPTS:
            task.status = "failed"
            task.error = f"Gave up after {MAX_ATTEMPTS} attempts (worker restarts or crashes)."
            task.lease_owner = None
            task.completed_at = now
            task.updated_at = now
            await db.commit()
            _log.warning("task %s exceeded max attempts", task.id)
            return None

        resumed = task.status == "running"
        task.status = "running"
        task.lease_owner = WORKER_ID
        task.lease_expires_at = now + timedelta(seconds=LEASE_S)
        task.attempt = (task.attempt or 0) + 1
        task.updated_at = now
        if resumed:
            last = (await db.execute(
                select(TaskEvent.seq).where(TaskEvent.task_id == task.id)
                .order_by(TaskEvent.seq.desc()).limit(1)
            )).scalar_one_or_none()
            db.add(TaskEvent(
                task_id=task.id, seq=(last + 1) if last is not None else 0,
                kind="step_start", data={"step": "resumed", "attempt": task.attempt},
            ))
        await db.commit()
        return task


async def _heartbeat(task_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            async with _db.SessionLocal() as db:
                await db.execute(
                    update(BackgroundTask)
                    .where(BackgroundTask.id == task_id, BackgroundTask.lease_owner == WORKER_ID)
                    .values(lease_expires_at=_now() + timedelta(seconds=LEASE_S))
                )
                await db.commit()
        except Exception:
            _log.warning("heartbeat failed for task %s", task_id, exc_info=True)


async def _release(task_id: str) -> None:
    async with _db.SessionLocal() as db:
        await db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id, BackgroundTask.lease_owner == WORKER_ID)
            .values(lease_owner=None, lease_expires_at=None)
        )
        await db.commit()


async def run_claimed(task: BackgroundTask) -> None:
    from tasks import _run_task_loop

    hb = asyncio.create_task(_heartbeat(task.id))
    try:
        await _run_task_loop(task.id, task.user_id, task.prompt)
    finally:
        hb.cancel()
    # Only reached when the loop returned normally. On shutdown (cancel) the
    # lease is deliberately kept so it expires and another worker resumes.
    try:
        await _release(task.id)
    except Exception:
        _log.warning("lease release failed for task %s", task.id, exc_info=True)


async def worker_loop() -> None:
    """Runs until cancelled. Launched from main.py's lifespan."""
    _log.info("task worker %s started", WORKER_ID)
    running: set[asyncio.Task] = set()
    while True:
        try:
            while len(running) < MAX_CONCURRENT:
                task = await claim_one()
                if task is None:
                    break
                t = asyncio.create_task(run_claimed(task))
                running.add(t)
                t.add_done_callback(running.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("task worker claim failed")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=IDLE_POLL_S)
        except asyncio.TimeoutError:
            pass
