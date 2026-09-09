# PHASE P5: CONSTRUCT PROJECT WORKFLOWS & DISTINCT AGENT DEFINITIONS (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P05
Depends On:      P04
Output:          Applied project policy, safe maestro init, distinct role definitions
Target Files:
  - Create:  maestro/workflows/policy.py
  - Create:  maestro/templates/workflows/project.yaml
  - Create:  role profile templates (implementer, planner, reviewer, test_designer, diagnoser)
  - Create:  tests/workflows/test_policy.py
  - Create:  tests/workflows/test_compiler.py
  - Create:  tests/test_init_workflows.py
  - Modify:  maestro/cli.py
  - Modify:  maestro/config.py
Verification:    .venv/bin/python -m pytest -q tests/workflows/test_policy.py tests/workflows/test_compiler.py tests/test_init_workflows.py tests/test_templates.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement project engineering policy (`workflows/project.yaml`), safe repository fact discovery in `maestro init`, and distinct, reusable `AgentDefinition` contracts for specialized cognitive roles.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Project Policy Schema**: Implement `maestro/workflows/policy.py` parsing `workflows/project.yaml` (templates, quality gates, permissions, objective).
- [ ] **Safe `maestro init` Discovery**:
  - Discover project facts from tracked manifests (pyproject.toml, package.json, Makefile, CI workflows).
  - **Negative Invariant**: MUST NOT execute arbitrary project code during discovery.
  - Record facts with source path, hash, and confidence. Record `unresolved[]` facts that fail closed.
  - Surface proposed changes as `.new` diffs; never overwrite existing project files.
- [ ] **Modular Template Composition (`[INV-A06]`)**: Compose templates from named, versioned fragments. The compiler's invariant is the minimal shape that satisfies required criteria.
- [ ] **Specialized Agent Roles**:
  - Implementer: Owns code edit in assigned worktree.
  - Planner: Read-only context analysis; emits `PlanProposal`.
  - Reviewer: Scrutinizes diff against criteria; emits findings; no write permissions.
  - Test Designer: Generates regression/adversarial tests in scratch workspace.
  - Diagnoser: Formulates falsifiable failure hypotheses on repeat errors.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Identical inputs produce identical compiled workflow content hashes.
  - Rerunning `maestro init` leaves active flags and existing files untouched.
  - A low-risk task omits planner and reviewer in Paid Efficiency Mode.
  - High-risk changes enforce mandatory independent review.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/workflows/test_policy.py tests/workflows/test_compiler.py tests/test_init_workflows.py tests/test_templates.py
  ```\n