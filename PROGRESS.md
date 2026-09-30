# Roadmap Progress

Running log for the multi-agent / long-running agent roadmap. Process: `WORKFLOW.md`.
Newest first. Every entry notes its lane, what changed, the test results, and what's still unverified.

---

## In progress

_(none)_

## Up next

1. **CI test workflow + fix baseline failures** (Standard lane).
2. **Alembic** (Quick lane, backend). Needs user OK because it changes how migrations run.
3. **Phase 2: multiple agents.** Blocked on decision D1, the model provider.

## Blocked on user

- Run Phase 1 on real Postgres (staging) and do the Mac + Windows smoke test, then commit and deploy (backend first).
- Decisions D1–D5 (see the roadmap plan).

## Done

### 2026-09-29 — Cron briefings fire once (Quick lane, backend)
- `scheduled_tasks.py`: new `_claim()` does a compare-and-set on `last_fired_at` before `_fire`. When two replicas or an overlapping deploy both see a brief as due, only one sends it. The manual "fire now" path is unchanged.
- Tests: new `test_cron_claim_lets_only_one_replica_fire`. Backend 41 passed / 1 baseline failure.
- **Unverified:** the timestamp equality round-trip on real Postgres (it should hold with timestamptz).

### 2026-09-29 — Security: no more Deepgram master-key fallback (Quick lane, backend)
- User confirmed `DEEPGRAM_PROJECT_ID` is set in Railway prod.
- `proxy.py` `stt_session`: retries the short-lived key mint once, then returns 503 "Voice is temporarily unavailable". The master key is never sent to clients. The engine already treats a failed `/stt/session` as no key (it shows a transcription error).
- Tests: new `test_stt_session_never_returns_master_key`. Backend 40 passed / 1 baseline failure.
- **Unverified:** real mint behavior. After deploying, do one dictation.

### 2026-09-29 — JWT moved to Electron safeStorage (Full lane, engine + desktop)
- Decision (user): Electron `safeStorage` instead of Python keyring. Keychain in the engine had been turned off on purpose; the likely reason is password prompts after updates, since the engine binary's signature changes.
- Desktop: new `main/sessionToken.ts`.
  - On every engine WebSocket connect (app start or engine restart) it adopts a plaintext token from the engine's file (`take_file_token`), encrypts it to `userData/session.enc`, or else decrypts the stored one and hands it over (`set_session_token`).
  - Login runs this sync and no longer returns the real token to the renderer. Logout deletes `session.enc`.
- Engine `config.py`: the token is held in memory once it's secured. Profile refreshes no longer write it back to disk. `get_uxie_user` returns `access_token: "stored"` instead of the real token (it used to leak the real token to any local caller of :8765). If `safeStorage` is unavailable, it falls back to the old file behavior.
- Existing installs migrate on their first launch: the token moves from the file into `session.enc`.
- Tests: 5 new engine tests (`tests/test_session_token.py`). Engine 36 passed / 12 baseline failures; Jest at baseline; typecheck clean.
- **Unverified:**
  - The app hasn't been launched, so these are untested on real hardware: the Keychain prompt behavior on a signed build, migration of an existing login, and Windows DPAPI.
  - Known small gap: the engine's startup Deepgram key prefetch can run before Electron hands over the token, so that one prefetch fails. The first dictation then fetches its own key.
- Deploy: needs a desktop release (DMG + EXE). No backend change. Signed Mac build: check you stay logged in across an update and see no Keychain prompt.

### 2026-09-29 — Security: OTP hardening (Quick lane, backend)
- `auth.py`: OTP codes are now generated with `secrets` instead of `random`. Verify looks up the email's active code and compares in constant time (`compare_digest` on bytes). 5 wrong guesses lock the code ("Too many attempts — request a new code").
- `db.py`: new nullable `otps.attempts` column, added to the live table at startup.
- Tests: new `test_otp_locks_after_max_wrong_attempts`. Backend 39 passed / 1 baseline failure.
- **Unverified:** the column addition on real Postgres.
- Deploy: backend only, safe to deploy on its own. After deploying, log in once with a real code.

### 2026-09-29 — Phase 1b: live updates, notifications, voice handoff (Full lane)
- Backend: `GET /tasks/stream` SSE endpoint (polls the database every 2s, keepalive ping every 15s), registered before `/tasks/{task_id}`.
- Engine: relays that stream to the desktop as a WebSocket `task-update` event. New `start_background_task` tool the agent can use to hand off a request.
- Desktop: `taskNotifications.ts` shows a notification when a task is done, failed or needs approval; clicking it opens the Tasks tab. The Tasks tab now refreshes on each update, with polling kept as a fallback.
- Tests: backend 38 passed / 1 baseline failure; engine and Jest at baseline; typecheck clean.
- **Unverified:** the app hasn't been launched, so the end-to-end flow on Mac and Windows is untested.

### 2026-09-29 — Phase 1a: durable task runtime (Quick lane, backend)
- `task_runtime.py`: a worker loop that claims tasks with a lease (`FOR UPDATE SKIP LOCKED`), renews the lease with a heartbeat, reclaims tasks whose lease expired, and gives up after 3 attempts.
- `tasks.py`: checkpoint and resume after each turn; approvals stored in the database (`task_approvals` table); a destructive tool call isn't re-run on resume; fixed the stale status read in the cancel check.
- `db.py`: new columns added to the live table at startup (additive, `ADD COLUMN IF NOT EXISTS`).
- Tests: 7 new tests in `tests/test_task_runtime.py`, all passing.
- **Unverified:** the column additions and two workers competing for tasks on real Postgres (the tests use SQLite).
