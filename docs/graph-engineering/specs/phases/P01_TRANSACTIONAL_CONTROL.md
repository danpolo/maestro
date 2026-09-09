# PHASE P1: INTRODUCE TRANSACTIONAL CONTROL & SINGLE WRITER (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P01
Depends On:      P00
Output:          SQLite WAL command/event store, content-addressed artifact store, single writer
Target Files:
  - Create:  maestro/control/models.py
  - Create:  maestro/control/store.py
  - Create:  maestro/control/lifecycle.py
  - Create:  maestro/control/artifacts.py
  - Create:  tests/control/test_store.py
  - Create:  tests/control/test_migration.py
  - Create:  tests/control/test_artifacts.py
  - Modify:  maestro/state.py
  - Modify:  maestro/paths.py
  - Modify:  maestro/hitl/commands.py
  - Modify:  maestro/watchdog.py
Verification:    .venv/bin/python -m pytest -q tests/control
```

---

## 1. OBJECTIVE & DELIVERABLES
Introduce `.orchestrator/control.sqlite3` as the single transactional state authority. Move state and event mutation behind a single-writer controller with an idempotent command inbox and append-only event store. Implement content-addressed artifact storage.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Database & Schema**: Implement `maestro/control/store.py` initializing `.orchestrator/control.sqlite3` in WAL mode with foreign keys and `busy_timeout=10000`.
- [ ] **Idempotent Command Inbox**: Create `command_inbox` table. External callers (CLI, Telegram) enqueue commands; controller ingests and executes them idempotently.
- [ ] **Event Sourcing & Projections**: Implement `events` table and reducers in `lifecycle.py` for task runs, node phases, attempts, and leases.
- [ ] **Content-Addressed Artifacts**: Implement `maestro/control/artifacts.py` storing files by SHA256 in `.orchestrator/artifacts/sha256/xx/yy/hash`. Enforce atomic tempfile write -> fsync -> rename.
- [ ] **Process Leasing & PID Binding**: Implement `writer_leases` with `writer_pid` binding to leaf process PID, not launcher PID (`[INV-02]`).
- [ ] **Legacy Shadowing & Export**: Shadow legacy `state.json` and journal; export sequence-stamped JSON projections after each SQLite commit. Legacy files become read-only outputs.

---

## 3. ACCEPTANCE CRITERIA & ROLLBACK
- **Acceptance Condition**:
  - Duplicate commands commit once; stale revisions fail CAS checks.
  - Replay of event stream reconstructs exact projections without external I/O.
  - A crash between artifact creation and SQL commit leaves only an unreferenced orphan artifact, never a missing referenced one.
  - Legacy files cannot independently overwrite SQLite authority.
- **Rollback Protocol**:
  - Prior to new execution side effects: restore quiescent legacy backup.
  - After side effects begin: down-export current state at a quiescent boundary.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/control
  ```\n