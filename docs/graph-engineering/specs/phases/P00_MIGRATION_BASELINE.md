# PHASE P0: ESTABLISH THE MIGRATION BASELINE (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P00
Depends On:      None
Output:          Behavioral fixture suite, measurable baseline metrics, cases.json
Target Files:
  - Create:  tests/graph_engineering/conftest.py
  - Create:  tests/graph_engineering/test_baseline_lanes.py
  - Create:  tests/graph_engineering/fixtures/cases.json
  - Create:  scripts/graph_baseline.py
  - Extend:  maestro/metrics.py
  - Update:  docs/PROGRESS.md
  - Update:  docs/EXECUTION.md
Verification:    .venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Establish a rigorous, reproducible behavioral baseline of existing Maestro execution lanes before modifying control or state architecture. Capture execution traces, duplicate calls, waits, test runs, touched snapshots, and lane transitions under isolated fixtures.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Characterization Fixtures**: Implement `tests/graph_engineering/fixtures/cases.json` covering the complete fixed six-class task catalog:
  1. `bug`: Ordinary bugfix in existing code.
  2. `feature`: Standard feature addition with new test.
  3. `documentation`: Markdown/doc update.
  4. `difficult_debugging`: Complex multi-file failure requiring diagnostic reasoning.
  5. `manual_prep`: Human-in-the-loop task requiring manual step.
  6. `recovery`: Interrupted run or quota exhaustion recovery.
- [ ] **Lane Capture Harness**: Capture behavior across current execution lanes (`maestro/orchestrator.py:1924`):
  - Ordinary change lane
  - Script / no-op lane
  - Manual preparation lane
  - Async wait lane
  - Resumable work lane
  - Quota switch and context rotation
  - Risky / proof review and graduation
- [ ] **Instrumentation & Metrics**: Extend `maestro/metrics.py` to record:
  - Duplicate agent invocations
  - Unnecessary test re-runs
  - Idle wait time vs active compute
  - State file read/write operations
  - Inferences vs verified deterministic observations
- [ ] **Scratch Project Pilot**: Run an instrumented setup rehearsal and one real coding task on an authorized scratch project. Retain raw per-task outcomes (every rework attempt and human intervention) to close the first half of B01.
  - **Designated Project:** **DuetFlow** (`/home/dan/projects/duetflow`, based on `/home/dan/projects/project-ideas/DuetFlow-README.md`).
  - **Rationale:** Greenfield headless Python service with SQLite, pure algorithmic components (50/50 playlist balancing, recency decay, track deduplication), sub-second deterministic `pytest` unit tests, and zero GUI/hardware dependencies.
  - **Current state (verified 2026-09-09):** the repo already exists and has been through `grill-with-docs` — 3 commits on `master`, `CONTEXT.md`, `docs/adr/`, `docs/PLAN.md`, `tests/test_smoke.py`, `.venv`, `tasks/lessons.md`. It has **no** `project.yaml`, **no** `.orchestrator/`, and **no root manifest** (`pyproject.toml` / `requirements.txt`). Do not re-create the skeleton; it is there.
  - **Procedure:** the setup rehearsal is performed by invoking the **`maestro-setup` skill**, not by calling `maestro init` directly. The skill owns the judgment inputs `init` cannot derive (`risky_set`, `deny_list_extra`, `gate.chain`, `bot_files`, glossary, first roadmap tasks) and harvests them from DuetFlow's existing `CONTEXT.md` / `docs/adr/` / `docs/PLAN.md`. Then seed `TASK-001` in `docs/ROADMAP.md` and execute the first real coding task under baseline instrumentation.
  - **Known pre-flight defect (expected to fire):** `maestro/cli.py::_derive_facts` detects a Python test command only from `pyproject.toml`/`requirements.txt` *plus* a pytest signal. DuetFlow has `tests/` and a `.venv` but no root manifest, so `init` will derive an **empty `test_command`** and render an `adapters/test` that fails closed with a `TODO` on every run — the gate then certifies nothing and every task parks at the merge gate. `maestro-setup` Step 1 catches this; resolve it with the operator (add `pyproject.toml` before `init`, or fix `adapters/test` immediately after). **Record it as a rehearsal finding, not as a chore** — a quiet setup failure that only a pre-flight check catches is exactly the kind of outcome B01 exists to measure.
  - **Also check:** `git remote get-url origin` in DuetFlow. If empty, `maestro/merge.py` still runs `git push origin main` with its return code unchecked, so merged work lands locally and silently never leaves the machine. Report it either way.
  - **Instrumentation:** `maestro-setup` is interactive and will ask Dan questions. Those answers are **recorded human interventions** for B01, not out-of-band setup. Retain the questions asked, the answers given, and every correction — a summary does not close B01.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Each lane produces a deterministic, reproducible trace.
  - Fixture execution is strictly isolated and CANNOT write to any live project directory.
  - Metrics distinguish empirical observation from inference.
- **Focused Regression Trigger**:
  - A fake async wait MUST NOT consume an agent slot.
  - A no-op script MUST NOT trigger an agent retry.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py
  ```\n