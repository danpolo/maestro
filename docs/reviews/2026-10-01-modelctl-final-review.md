# Modelctl integration final independent review

Range: base/HEAD `a40cafc44ea00051a1e609833d15fd710c61cf30`, plus all tracked and untracked candidate changes in the isolated `feat/modelctl-integration` worktree. No commits. Reviewer: fresh `modelctl_final_review` agent using the spec-code-review rubric, approved integration plan, handoff chain through session 6, and execution ledger. Package: `.superpowers/sdd/2026-09-30-modelctl-integration/session7-review.diff`.

The reviewer independently reproduced four Important correctness findings in disposable state. No Critical, Minor, or unresolved design findings were reported.

1. **Provider lifecycle/native-window disagreement** (`model_policy.provider_lifecycle`, `invalid_routes`, `check_task_pin`). Legacy/pin/status accepted GPT-6 Luna with a 500K ceiling despite its 340K verified native window; graph excluded it. Valid. The common lifecycle validator now checks the model's verified native window, protecting status, legacy and bounded calls as well as graph filtering.
2. **Provider/removal state stale after lock wait** (`model_replacement.apply_preview`, `model_lifecycle.apply_policy_preview`). Provider checks occurred before waiting for publication; only policy/registry/config were rechecked under the lock. Valid. The locked callback now repeats provider metadata or owner removal-state validation. Replacement availability/table changes and withdrawn retirement signals refuse publication.
3. **Initial shipped policy unavailable after source upgrade** (`PolicyStore.publish`, `load_any`). First replacement referenced an unstored shipped digest, which a later source release could not regenerate for rollback. Valid. First publication now stores the exact shipped policy when it is the referenced predecessor, before moving the policy pointer. Rollback after changed shipped provenance restores the prior policy.
4. **Malformed project model values abort shared preview** (`effective_roles`). Model lists/maps and backend lists passed shape validation and later raised uncaught errors. Valid. Role/backend/model names and override values are validated before merging; malformed registered projects are reported invalid while valid projects proceed.

Test-first evidence in the execution directory:

- `session7-review-fixes-red.log`: 5 intended failures, 10 passed (native window and publication races).
- `session7-review-fixes-green.log`: 15 passed.
- `session7-review-more-red.log`: 6 intended failures (rollback across source upgrade and five malformed override cases).
- `session7-review-more-green.log`: 46 passed, including all fixes and lifecycle/pin/race regressions.

Review coverage: policy publication/CAS, controller-lock adoption, proposals, lifecycle validation, legacy/bounded consumers, graph routing, task binding/recovery, rollback, standards and test quality. Limitations: the reviewer did not rerun the full suite, deploy, infer, edit external owner code, or inspect/mutate DuetFlow. Existing task-freeze tests chiefly exercise helpers and revision equality and give limited evidence of complete controller restart behavior. Missing registration remains intentionally disabled pending owner hardening. Full frozen candidate gate and preservation results are recorded separately in the execution ledger.

Executor's final verification: corrected serial gate **5433 passed, 1 xfailed, exit 0,
373.40 s**; 342-input source/test/register/document manifest unchanged and pilot-aware
preservation exit 0. An additional test-only fixture correction explicitly clears an
inherited project root in self-update scenarios that assert global adoption (three
full-gate failures before correction; self-update plus halted Telegram: 27 passed after).
The original gate's worktree usage-artifact write and the obsolete pre-pilot preservation
baseline are recorded transparently in the session 7 handoff; live DuetFlow was not modified.
