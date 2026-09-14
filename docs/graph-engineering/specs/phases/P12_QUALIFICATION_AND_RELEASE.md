# PHASE P12: QUALIFY COMPLETE EVOLUTION BEFORE LIVE ADOPTION (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P12
Depends On:      Core phases + P11 + the P08 -> P09 -> P10 -> P10B -> P07B branch + P12A (graph dispatch wiring; all feature experiment outcomes recorded)
Output:          End-to-end qualification report, raw task outcomes for B01/B18, release tag
Target Files:
  - Create:  tests/graph_engineering/test_end_to_end.py
  - Create:  scripts/graph_qualification.py
  - Update:  docs/DESIGN.md
  - Update:  docs/EXECUTION.md
  - Update:  docs/PROGRESS.md
Verification:    .venv/bin/python -m pytest -q && .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends
```

---

## 1. OBJECTIVE & DELIVERABLES
Execute full-system end-to-end qualification across script, subscription, free, and hybrid fixture backends. Execute real tasks on two scratch projects. Retain raw per-task outcomes to close B01 and B18. Measure actual artifact volume to establish retention defaults (B15).

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **End-to-End Test Suite**: Run full pytest suite and fault matrix (transaction rollback, lost controller, writer crash, quota switch, interrupted integration).
- [ ] **Scratch Project Pilot**:
  - Run representative tasks on two independent repositories under each qualified mode:
    1. **DuetFlow** (`/home/dan/projects/duetflow`): Greenfield scratch project progressing from P0. Validates multi-task autonomous progression and clean algorithm development.
    2. **instagram-to-value** (`/home/dan/projects/instagram-to-value`): Brownfield live system with 235 passing tests in 1.44s and an active backlog in `PLAN.md`. Validates regression avoidance, worktree isolation, and file confinement on a living codebase.
  - **Considerations & Safeguards for instagram-to-value:**
    - **Service & Daemon Protection (`bot_files`):** Set `bot_files: ["scripts/bot.py", "scripts/worker.py"]` in `project.yaml`. Because `itv-bot` and `itv-worker` run live in background tmux sessions, touching these entrypoints must halt the no-eval resumable auto-merge path and require Dan's explicit review before landing.
    - **Queue & Media Confinement:** Add `jobs/`, `staging/`, `media/`, `out/`, `logs/`, and `config/` to `deny_list_extra` in `project.yaml`. Implementers in worktrees must never mutate live queues or staged proposals.
    - **Fast, Deterministic Verification Chain:** Set `gate.chain: [test]` in `project.yaml` to run the project's existing 1.44s unit test suite (`pytest -q`), avoiding external network calls (Instagram/Telegram APIs) or heavy ASR/OCR model invocations during worktree verification.
    - **TMUX Session Isolation:** Background services operate in their dedicated sessions (`itv-bot`, `itv-worker`); Maestro implementer tasks run in the `agents` session without terminal/process collision.
  - Retain raw per-task outcomes (transcripts, rework counts, tool logs) rather than summaries.
- [ ] **Artifact Volume Measurement (`[INV-A03]`, B15)**:
  - Measure actual bytes of artifacts and transcripts produced during qualification run.
  - Set default retention settings in `maestro init` based on empirical measurement.
- [ ] **Qualification Script**: Implement `scripts/graph_qualification.py`. Refuse non-scratch target directory unless explicit deployment flag is provided.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - All 12 system invariants pass.
  - Every uncertain optimization has an enabled/disabled decision backed by empirical evidence.
  - Unqualified backends remain labeled unqualified; mock passes cannot claim production readiness.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q
  .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends
  ```\n