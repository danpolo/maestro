# PHASE P2: MAKE ROADMAP A VALIDATED, REVISIONED DEPENDENCY MODEL (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P02
Depends On:      P01
Output:          Strict roadmap parser, mutation API, cycle detector, CAS projection
Target Files:
  - Create:  maestro/taskgraph.py
  - Create:  tests/test_taskgraph.py
  - Adapt:   maestro/docs/roadmap.py
  - Adapt:   maestro/docs/complete.py
  - Adapt:   maestro/docs/consistency.py
  - Adapt:   maestro/docs/depmap.py
  - Adapt:   maestro/docs/upcoming.py
  - Adapt:   maestro/status.py
  - Adapt:   maestro/cli.py
Verification:    .venv/bin/python -m pytest -q tests/test_taskgraph.py tests/characterization/test_roadmap.py tests/characterization/test_consistency.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Replace loose regex/text parsing of `ROADMAP.md` with a strict AST and dependency model (`maestro/taskgraph.py`). Persist task definitions and dependency graphs in SQLite with revision control, cycle detection, and CAS document synchronization.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Normalization & Schema**: Normalize roadmap markdown YAML blocks into `TaskSpec`. Preserve surrounding free-form prose and unknown fields in `legacy_fields`.
- [ ] **Strict Validation**: Fail with exact line/block locations for malformed YAML, duplicate IDs, missing prerequisites, or cycles using `graphlib.TopologicalSorter`.
- [ ] **Mutation API**: Implement transactional roadmap mutation commands:
  - `add_task(spec)`
  - `add_dependency(child, parent)`
  - `split_task(parent_id, child_specs[])`: Parent becomes a group; entry children inherit prerequisites, exit children satisfy downstream edges.
  - `supersede_task(old_id, new_id)`
  - `invalidate_inputs(task_id)`
- [ ] **CLI Commands**: Implement `maestro roadmap validate` and `maestro roadmap apply` with compare-and-swap (CAS) document hash validation.
- [ ] **Conflict Handling**: If `ROADMAP.md` is hand-edited while controller is active, flag document drift conflict and block affected dispatch; NEVER overwrite human edits.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Cyclic dependencies (e.g. `A -> B -> C -> A`) fail with exact cycle paths.
  - Task splits preserve all incoming and outgoing obligations.
  - Legacy completions imported as `legacy_accepted`; they satisfy roadmap dependencies but CANNOT manufacture fresh test evidence or cache hits.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_taskgraph.py tests/characterization/test_roadmap.py tests/characterization/test_consistency.py
  ```\n