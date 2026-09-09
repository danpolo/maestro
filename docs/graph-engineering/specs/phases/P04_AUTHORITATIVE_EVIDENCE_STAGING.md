# PHASE P4: MAKE EVIDENCE AND STAGING INTEGRATION AUTHORITATIVE (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P04
Depends On:      P03
Output:          Validation engine, cryptographic evidence registry, isolated staging worktree
Target Files:
  - Create:  maestro/validation.py
  - Create:  tests/test_validation_evidence.py
  - Create:  tests/test_integration_acceptance.py
  - Adapt:   maestro/verifications.py
  - Adapt:   maestro/gates.py
  - Adapt:   maestro/merge.py
  - Adapt:   maestro/adapters.py
Verification:    .venv/bin/python -m pytest -q tests/test_validation_evidence.py tests/test_integration_acceptance.py tests/characterization/test_gates.py tests/characterization/test_merge.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Establish authoritative quality gating (`maestro/validation.py`). Eliminate self-reported agent "passes". Execute checks on frozen snapshots, generate cryptographic evidence receipts, enforce isolated staging merges, and protect the target branch.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Evidence Data Structures**: Implement `VerificationSpec`, `VerificationEvidence`, and `AcceptanceDecision`.
- [ ] **Closed-World Evaluation**: Validate checks against required criterion keys. Missing proof keys or empty reviewer outputs fail closed (`FAILED`).
- [ ] **Exact Snapshot Fingerprinting**: Cache check results ONLY for identical `(source_snapshot_id, command_hash, input_fingerprint, environment_fingerprint)` tuples.
- [ ] **5-Stage Staging Pipeline**:
  1. Freeze candidate worktree.
  2. Merge candidate into fresh target branch snapshot inside `.orchestrator/worktrees/staging/`.
  3. Run mandatory integration checks on merged snapshot.
  4. Fast-forward `project.main_branch` under exclusive target lease.
  5. Execute external side-effect nodes (push/deploy) with intent/receipt.
- [ ] **Postcondition Probes (`[INV-A05]`)**: Non-idempotent adapters without postcondition probes MUST NOT be retried automatically.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - An agent-authored `DONE` or `tests.pass` file CANNOT accept a task.
  - Source code drift immediately invalidates cached check evidence.
  - Two candidate worktrees cannot cross-pollinate proofs or patches.
  - Advancement of target branch during validation forces re-integration on fresh target head.
  - Failed staging check leaves target branch completely untouched.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_validation_evidence.py tests/test_integration_acceptance.py tests/characterization/test_gates.py tests/characterization/test_merge.py
  ```\n