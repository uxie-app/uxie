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

- **Turn on computer use (staging):** add `E2B_API_KEY` to Railway → staging → uxie-backend → Variables. OpenAI is now the default provider and `OPENAI_API_KEY` is already set there; no Anthropic key is needed. Without both, the `use_computer` tool is simply not offered.
- **Smoke test on staging** (desktop checklist, plus agents, wait/reply, "summarize this", and now: "go to news.ycombinator.com and tell me the top 3 stories" → watch it live in Tasks → Computer).
- **Push + deploy:** this round is uncommitted on `staging` (the one-time push was used). Commit/push yourself, or tell me to.
- **Decisions (user keeps these):** Gmail Pub/Sub + Slack Events push; desktop-context default ON vs opt-in.
- After the smoke test: merge `staging` → `main` and cut the Mac + Windows release.

## Done

### 2026-10-02 — Computer use: OpenAI as the default provider (user decision)
- The Anthropic account had no credits. The user chose OpenAI.
- `computer_use.py` is now provider-swappable through the gateway role `computer_use` (default `openai:gpt-6.1-sol`; `MODEL_ROLE_COMPUTER_USE="anthropic:claude-opus-5-5"` switches back). `available()` checks the active provider's key.
- OpenAI loop (Responses API, tool `{"type":"computer"}`, shapes from the current docs fetched 2026-10-02):
  - A `computer_call` carries a batch of `actions` (click/double_click/move/drag/scroll/keypress/type/wait/screenshot), and each call is answered with a `computer_call_output` screenshot (`detail: "original"`). Context chains via `previous_response_id`.
  - `request_approval` / `request_takeover` are function tools.
  - `pending_safety_checks` → the user approves first (otherwise stop), then they're sent back as `acknowledged_safety_checks`.
  - Key names are mapped (CTRL/ENTER/ArrowUp → xdotool keysyms); scroll pixels → wheel clicks (100 px per click).
  - Raw HTTP via the existing shared httpx client, like the rest of the backend.
- Tests: OpenAI action mapping, the loop (actions → screenshot output → approval denied → final text), a declined safety check stops before acting, and provider-aware availability. Backend 66 passed / 3 skipped.
- **Unverified:**
  - A real OpenAI call. It needs the OpenAI key (on Railway; not available locally).
  - Whether this account has access to `gpt-6.1-sol`.
  - Gaps the docs left open: the exact keypress key format, the drag path shape, the wait field, and whether `pending_safety_checks` still exists.

### 2026-10-01 — UI polish to DESIGN.md (Standard lane, desktop)
- **Tasks:**
  - Status reflects what the user needs to act on: NEEDS YOU (amber) when an approval is pending, WAITING (gray) when parked; `/tasks` now returns `approval_needed`.
  - The header shows agent · started · what it's waiting for. Replaced the stale "v1.1 read-only" copy and removed the "polling…" debug label.
  - New Computer section (live view, Take over, "I'm done — continue"); COMPUTER rows in Activity; the take-over approval reads "I'm done" / "Stop".
- **Agents:** rebuilt as the two-pane list/detail layout (§4.1). Tool names are in plain language ("Send email", "Use a cloud computer"); permissions are in a grid card; Save / Delete (danger) with a confirm.
- Tests: `TasksComputer.test.tsx`, updated `AgentsTab.test.tsx`. Jest 25; tsc clean.

### 2026-10-01 — Phase 5: computer use on cloud VMs (Full lane, backend + desktop)
- User decisions: managed sandbox (E2B Desktop), Claude for computer use only, "ask before risky steps".
- `computer_use.py`: the `use_computer(goal, start_url?)` tool for background tasks.
  - A sub-loop drives an E2B Linux desktop (1280×800) with Claude's `computer_toolset_20260801` (model via gateway role `computer_use` = `claude-opus-5-5`, effort medium, server-side refusal fallback `"default"` with retry-without on rejection, top-level prompt caching). API shapes were verified against the docs and the installed SDKs (anthropic 1.11.0, e2b-desktop 2.6.0).
  - Keys go straight to xdotool (Claude's names are keysyms; E2B's `press` lowercases them). Zoom is disabled (no image library needed).
  - Batch semantics: stop at the first failure; the rest get "Not executed".
  - Safety: Claude must call `request_approval` before submit/purchase/send/sign-in/terms/delete/download, and `request_takeover` for logins, captchas, 2FA and payment. Both go through `task_approvals` (notification + Tasks card). Pages are treated as data. Caps: 60 turns and 30 minutes per session; the VM is always killed.
  - Live view: authenticated noVNC stream, a view-only URL for watching and an interactive URL for take-over.
  - Resume: the sandbox ID is stored per call, so a resumed task reconnects (`Sandbox.connect`) and a finished session's result is reused instead of re-run.
  - Offered only when `E2B_API_KEY` and `ANTHROPIC_API_KEY` are set.
- New dependencies `anthropic==1.11.0` and `e2b-desktop==2.6.0` (clean resolve with the existing pins). New settings `anthropic_api_key` and `e2b_api_key`.
- Tests: `test_computer_use.py` (action mapping, session loop with approval denied + batch failure + live-view event + VM cleanup, result reuse, hidden without keys). Backend 62 passed / 3 skipped.
- **Verified live on E2B (2026-10-02, user key, run locally and not stored):** sandbox create 4.0 s; authenticated view-only and control stream URLs; click, `ctrl+l`, typing, Escape, scroll, cursor position and screenshot (1280×800 PNG; Firefox loaded example.com and the text landed in the address bar); kill.
- **Still unverified:** the Claude side (needs `ANTHROPIC_API_KEY`), task quality, latency/cost per session, and the noVNC iframe inside Electron.

### 2026-10-01 — Expired login handling (Full lane, engine + desktop)
- Any backend 401 (`/invoke` handlers, `_backend_json`, the task stream relay, the LLM client, STT key fetch) → `config.report_unauthorized()`. It clears the session and broadcasts `auth-expired`. Electron deletes `session.enc` and the app shows sign-in, instead of failing every call ("Signature has expired").
- Test: `test_report_unauthorized_signs_out_and_notifies`. Engine 53 passed.

### 2026-10-01 — Fix: re-login kept the old (expired) token (engine, found in user testing)
- Bug in the safeStorage JWT work: signing in again *without* signing out first saved the new token, but the next WebSocket sync restored the old token from `session.enc`. Result: "Invalid token: Signature has expired" on every call.
- Fix (`config.save_jwt`): a token different from the current one is treated as a new login and put on disk for Electron to adopt.
- Regression test `test_relogin_while_secured_hands_new_token_to_electron`. Engine 52 passed.
- **Still open (needs user OK, auth):** the app doesn't notice an expired token at all and stays "signed in". Proposed: clear the session on a 401 and show sign-in.

### 2026-10-01 — Pushed to staging + verified (user-approved one-time push)
- Committed `ac52891` on `staging` (45 files, staged by explicit path, no `oauth.py`, secret scan clean) and pushed.
- CI run 36804127787 passed: backend, engine (macOS, Python 3.13, now verified) and desktop.
- Staging redeployed: `/agents`, `/route` and `/tasks/stream` are live and health is OK. The backend only starts after `init_db` (Alembic `upgrade head`) succeeds, so migrations 0002–0004 ran on real Postgres.
- CI annotations to handle later: actions/checkout@v4 and setup-python@v5 run on deprecated Node 20, and `ubuntu-latest` moves to Ubuntu 26 on 2026-10-19.

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
