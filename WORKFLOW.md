# Engineering Workflow

How roadmap work gets done, one unit at a time. `PROCESS.md` decides the
**lane** for a change (the checks it needs before release). This file decides
**how the work is carried out and reported**. `PROGRESS.md` is the running log.

Roadmap: multi-agent / long-running agents (Phases 0–10). The current
sequence is in `PROGRESS.md` → "Up next".

---

## 1. Unit of work

- One unit = one shippable slice: small enough to review in one sitting,
  with one lane (Quick / Standard / Full / Release) from `PROCESS.md`.
- Split anything that crosses backend, engine and desktop into per-package
  units unless they must ship together, and say which.

## 2. The loop (every unit)

| Step | What happens | Output |
|---|---|---|
| **1. Think** | Restate the goal. Read the code it touches. Check whether it already exists. Challenge the framing if it looks wrong. | — |
| **2. Plan** | Add an "In progress" entry to `PROGRESS.md`: scope, files, lane, risks, test plan. | PROGRESS entry |
| **3. Build** | Smallest diff that does the job. Match the surrounding code. Keep schema changes additive and nullable. Keep old clients (desktop, iOS) working against the new backend. | Code |
| **4. Test** | Add tests for the new behavior. Run the affected suites and compare against the **baseline failures** (§5). A unit isn't done if it adds a failure. | Test results |
| **5. Review** | Self-review the diff against the `PROCESS.md` bug-class scan and security list: no secrets, no debug prints, SSE keepalives, tool names in sync, migration safety. | — |
| **6. Report** | Move the entry to "Done" in `PROGRESS.md`: what changed, test results, what's **unverified**, deploy notes. Send the user a short update. | PROGRESS + message |
| **7. Ship** | **Human only.** Commit, push, Railway deploy and releases are done by the user. Claude never commits or pushes. | — |

## 3. Stop and ask the user before

- Anything in the roadmap's **open decisions** (model provider, worker
  service, staging, connector order, app ID).
- New dependencies, new Railway services, or new env vars.
- Changes to auth or token storage, OAuth scopes, `electron-builder.yml`,
  `entitlements.mac.plist`, or anything in `miniflow-engine/oauth.py`.
- Destructive data changes: dropping or renaming columns, or backfills.
- Being stuck after **2 attempts** on the same problem.

## 4. Reporting rules

- Each update names what changed, what was tested, and what was **not**
  verified (real Postgres, real device, Windows).
- Test failures are reported with the actual output, never glossed over.
- Keep the "Done" entries in `PROGRESS.md` short. Detail lives in the diff.

## 5. Baseline test status

Known failures that were already failing before the roadmap work started. New
work must not add to these. Fix them in their own unit.

| Suite | Command | Baseline |
|---|---|---|
| Backend | `cd uxie-backend && pytest` | 1 failing: `test_auth.py::test_health` (stale assertion); 41 passing, 3 skipped |
| Engine | `cd miniflow-engine && pytest` | 12 failing; 36 passing |
| Desktop unit | `cd miniflow-electron && npx jest` | 9 failing; 13 passing |
| Desktop types | `npx tsc --noEmit -p tsconfig.json` and `-p tsconfig.main.json` | clean |

Update this table whenever a unit changes the baseline.
