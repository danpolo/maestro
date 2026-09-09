# PHASE P7: DELIVER FREE-MODEL & HYBRID EXECUTION (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P07
Depends On:      P06
Output:          ModelAgentRuntime, multi-candidate groups, sandbox branch namespacing
Target Files:
  - Create:  maestro/backends/model_api.py
  - Create:  maestro/backends/model_runtime.py
  - Create:  tests/backends/test_model_runtime.py
  - Create:  tests/workflows/test_mode_policies.py
  - Create:  tests/workflows/test_candidates.py
  - Create:  tests/test_confinement.py
Verification:    .venv/bin/python -m pytest -q tests/backends/test_model_runtime.py tests/workflows/test_mode_policies.py tests/workflows/test_candidates.py tests/test_confinement.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement `ModelAgentRuntime` for API endpoints with free tiers. Enable Free Quality Mode with multi-candidate exploration (2+ isolated candidates). Enforce branch namespacing and directory-level sandbox confinement.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **`ModelAgentRuntime` Loop**: Implement bounded model/tool supervisor loop using standard HTTPS transport; handle turn checkpointing and strict tool allowlists.
- [ ] **Free Quality Compilation**:
  - Compile workflows with independent test designers, 2+ isolated candidate branches, and diverse reviewers.
  - Zero token-cost penalty in free mode.
- [ ] **Branch Namespacing (`[INV-A08]`)**:
  - Namespace all implementer branches: `impl/<task>`, `selffix/<id>`, `redo/<id>`. Prohibit flat branch names.
- [ ] **Commit Boundary Confinement**:
  - Confined implementer can write ONLY to: task worktree, worktree `.git`, shared `.git/objects/`, and its own namespaced ref `refs/heads/impl/<task>`.
  - Target branch `refs/heads/main` is physically blocked.
- [ ] **Candidate Selection Join**:
  - Candidate join evaluates all candidates and selects exactly one winner. NEVER mix patches or proofs across candidates (`[INV-06]`).

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Identical task spec succeeds across paid, free, and hybrid fixture backends.
  - Free compilation has zero token cost penalty.
  - Candidates never share writer worktrees.
  - Zero paid capacity in free mode never launches a subscription call.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/backends/test_model_runtime.py tests/workflows/test_mode_policies.py tests/workflows/test_candidates.py tests/test_confinement.py
  ```\n