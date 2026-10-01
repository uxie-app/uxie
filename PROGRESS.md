# Roadmap Progress

Running log for the multi-agent / long-running agent roadmap. Process: `WORKFLOW.md`.
Newest first. Every entry notes its lane, what changed, the test results, and what's still unverified.

---

## In progress

_(none)_

## Up next

1. **Push instead of polling for Phase 3.** Gmail Pub/Sub `users.watch` and the Slack Events API for faster wakes. Needs user setup: a GCP Pub/Sub topic, a Slack signing secret, and new Railway env vars.
2. **Phase 5 slice: cloud browser worker.** A new Railway service, so it needs user OK.
3. **Decision D1** (model provider). The gateway makes it a config change.

## Blocked on user

- Staging is live on `staging` @ `5d9e295` (https://uxie-staging.up.railway.app). The column additions and startup were verified on real Postgres on 2026-09-30. **Remaining:** desktop smoke test against staging (checklist C + D), then merge to `main` and do the desktop release.
- Decisions D1–D5 (see the roadmap plan).

## Done

### 2026-09-30 — Phase 4: desktop context (Full lane, engine + backend + desktop)
- **Changed approach from the plan:** no Rust helper work. The Mac engine already captured the frontmost app bundle ID, the browser URL and the selected text at hotkey press, but only text transforms used them.
  - Added: focused window title (Mac via Accessibility, Windows via Win32 `GetWindowTextW` through ctypes; no native rebuild).
  - `agent.desktop_context()` builds {app, window_title, url, selected_text ≤2000 chars} and drops empty fields.
- **Voice commands:** the context goes in as a labelled block ahead of the request, so "this/here/that" resolve.
- **Background tasks:** the engine sends it with `/tasks/create`; the backend validates it (known keys, 2000 chars per field), stores it (migration `0004_desktop_context`) and prefixes the task prompt with it.
- **Privacy:** captured only at hotkey press, never continuously. New Settings → Account toggle "Use what's on screen for commands", default ON (`desktop_context_enabled`).
- **Tests:** engine `test_desktop_context.py`; backend `test_task_carries_desktop_context`. Backend 58 passed / 3 skipped; engine 51; Jest 24; tsc clean.
- **Unverified:** AX window title in a real app; the Windows title capture; answer quality with context in real voice commands.
- **Decision for user:** the toggle defaults ON, which sends window titles/URLs to the LLM on command-mode invocations. Flip the default if you prefer opt-in.

### 2026-09-30 — Phase 3: wait / wake (Full lane, backend + desktop)
- New wait tools for background tasks: `wait_for_email_reply(thread_id)`, `wait_for_slack_reply(channel_id, thread_ts)` and `wait_until(time)`.
- A wait parks the task: checkpoint saved, lease released, `waiting_for` set, no compute. Status stays `running`, so iOS and old clients don't see an unknown state.
- `events.py` watcher (60 s, started in the lifespan):
  - It checks only the threads that parked tasks wait on, using existing Google/Slack connections. Our own messages and messages from before the wait are ignored.
  - It wakes the task when a reply arrives, the time comes, or the wait expires. The event text is added to the checkpoint, and `attempt` resets so waits don't count as failures.
  - Wakes are row-locked, so a task can't be woken twice.
- `gmail_send` and `slack_send_message` now return `thread_id` / `channel_id` + `ts`, so a task can wait on what it just sent. This slightly changes the result text in interactive clients.
- Migration `0003_waiting_for`. `/tasks`, `/tasks/{id}` and the SSE stream expose `waiting`. The Tasks tab shows "Waiting".
- **Tests:** `test_events.py` covers validation, a full park → wake → resume cycle (no double wake), the Gmail reply check (own and old messages ignored), expiry, and the stream flag.
- **Unverified:** real Gmail/Slack thread polling. **Scale note:** one watcher pass checks all parked tasks one after another; fine for now, move to push before it's large.

### 2026-09-30 — All LLM call sites on model_gateway roles (Quick lane, backend)
- New roles `voice_agent` and `dictation_fix` (`agent.py`) and `briefing` (`scheduled_tasks.py`), pinned to exactly the previous models; a test locks this.
- `proxy.py` `/llm/*` stays a passthrough, because the engine names the model in its request.

### 2026-09-30 — Agent policy in the interactive /agent/* loop (Full lane, backend)
- The loop applies the user's default agent: "deny" hides and blocks the tool, "require_approval" adds the gate, and the agent's instructions are appended to the system prompt.
- It only tightens: "allow" never removes the approval gate on destructive tools here.
- No new SSE event names. A blocked call uses the existing `tool_call_result`.
- Test: `test_default_agent_policy_tightens_interactive_loop`.

### 2026-09-30 — Phase 2: multiple agents (Full lane, backend + engine + desktop)
- **Backend:**
  - `agents` table (migration 0002) and `background_tasks.agent_id`. Every user gets a default "Uxie" agent on first use.
  - `agents.py`: CRUD at `/agents` (validated names, max 10 per user, the default can't be deleted), and `/route`.
    - The router tries an explicit name mention first, then a small-model classifier, then falls back to the default. A classifier failure falls back to the default.
  - `tasks.py`: each task runs under its agent.
    - The per-tool policy (allow | require_approval | deny) overlays the built-in defaults. Denied tools are hidden from the model and blocked.
    - "allow" on a destructive tool skips the approval prompt, but is still recorded in `task_approvals`, so a resume can't re-run it.
    - The agent's instructions are added to the prompt.
  - `/tasks/create` routes when no `agent_id` is given. List and get return `agent_name`.
- **Model gateway (`model_gateway.py`):** roles map to (provider, model) with today's models, so D1 is not decided. `MODEL_ROLE_<ROLE>` env overrides are supported, as is a per-agent `model_policy`. Only tasks and the router use it so far.
- **Engine:** `agents_*` proxy commands via a shared `_backend_json` helper.
- **Desktop:** Agents tab (create, instructions, per-tool permissions, delete). Task rows show the agent name.
- **Fixed along the way:** an agent that denies every connected tool used to get a misleading "connect a provider" message. It now runs without tools.
- **Tests:** 9 backend (`test_agents.py`), 2 Jest (`AgentsTab.test.tsx`). Backend 52 passed / 3 skipped; engine 50; Jest 24; tsc clean.
- **Unverified:**
  - The router classifier against real Groq.
  - The Agents tab in the running app.
  - Voice "ask my X agent…" end-to-end.
- **Not done:** policy in the interactive `/agent/*` loop, and the other LLM call sites (both in Up next).

### 2026-09-30 — Alembic (Quick lane, backend)
- `alembic.ini` + `migrations/`. The folder isn't named `alembic/`, which would shadow the package.
- `0001_baseline` is an idempotent bootstrap: it creates missing baseline tables and applies the frozen `ADDITIVE_COLUMNS`, and changes nothing on an existing database.
- `db.init_db()` now runs `upgrade head` under a Postgres advisory lock (`migrations/env.py`), so replicas can't race.
- Rule: every revision after 0001 must be existence-guarded, because 0001 builds fresh tables from the live models.
- **Tests (`test_migrations.py`):** a fresh database upgrades to head, a second run is a no-op, and a pre-Alembic database (like prod) gets the new table and column.
- **Unverified:** the first real run on staging/prod Postgres. It should stamp `0002_agents` after the next deploy; check the deploy logs for "Running upgrade".

### 2026-09-30 — CI test workflow + all suites green (Standard lane)
- New `.github/workflows/test.yml`, run on PRs and on pushes to `main`/`staging`:
  - backend pytest (ubuntu, Python 3.12)
  - engine pytest (macos-14, Python 3.13, matching `build.yml`, 10-minute cap)
  - desktop `tsc` (both configs) + jest (ubuntu, Node 20)
- Stale tests updated to current intended behavior (no app code changed):
  - backend `test_health`: the response now includes more fields.
  - engine hotkey tests: two-binding schema, plus new migration and collision tests.
  - engine `test_default_llm_provider_is_uxie`: now enforces the CLAUDE.md rule.
  - engine agent tests: auto-approve in the routing test (it was waiting 60 s on the approval timeout); turn cap is 4.
  - desktop helper-dispatch: the `{"type":...,"mode":...}` protocol.
  - desktop HotkeyRecorder: the two-recorder UI.
- The 10–27 minute engine runs are gone; the full engine suite now takes 3.4 s.
- Also found: running backend and engine from one venv upgrades FastAPI and makes a backend auth test fail. Keep separate venvs; CI jobs are already separate.
- Results: backend 42 passed / 3 skipped; engine 50 passed; jest 22/22; tsc clean.
- **Unverified:** the engine job on Python 3.13 (verified locally on 3.12 only) and the first real CI run.

### 2026-09-30 — Engine: litellm re-pinned 1.52.0 → 1.53.9 (dependency, user-approved)
- 1.52.0 was removed from PyPI, which broke `pip install -r requirements.txt`, local dev setup and probably the CI PyInstaller build. 1.53.9 is the closest available release.
- `pip check` is clean with `openai` 2.54. `litellm` imports fine.
- Engine tests: 9 failing / 39 passing. The two `test_llm` failures that were due to missing `litellm` now pass; the remaining 9 are the pre-existing stale tests.
- **Test-harness quirk (not production):** the full engine suite takes 10–27 minutes with `litellm` installed, although each file on its own totals about 62 s. Likely a hang at exit; investigate in the CI unit. `test_agent.py::test_execute_command_routes_connector_tool` separately waits about 60 s, which was already the case before.
- **Unverified:** the PyInstaller build with the new pin, and non-Uxie providers (OpenAI/Anthropic direct) at runtime.

### 2026-09-30 — Engine test baseline corrected
- The earlier "12 failing" engine baseline was measured in a venv where the requirements install had silently failed (`pyperclip` and `litellm` missing). Re-measured with all deps installed except litellm: 11 failing / 37 passing. The same 11 fail on the pre-roadmap code, so our changes add none.

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
