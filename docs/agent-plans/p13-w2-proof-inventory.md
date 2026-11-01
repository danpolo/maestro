# W2 requirement and runtime proof inventory

Authority: P13 W2 and B10/B20; `p13-w2-remainder.md`. This inventory maps
requirements to executable evidence, rather than treating a passing cause table as
a full graph replay. Tests use private stores, repositories, transports and channels.
No live pilot, root integration, release, push or Context Gate is involved.

`lanes` below means `tests/graph_engineering/test_w2_runtime_lanes.py`;
`runtime` means `tests/workflows/test_w2_runtime.py`;
`report` means `tests/test_usage_report.py`.

| Requirement | Production evidence |
|---|---|
| Normal first happy path has no extra allocation | lanes `test_one_happy_graph_has_no_extra_step_allocations`; runtime `test_first_happy_path_has_no_extra_row` |
| Every one of the seven later-review origins | lanes `test_each_later_review_origin_is_one_graph_runtime_allocation` (seven parameters); runtime `test_real_later_review_keeps_origins_and_one_attempt` (mixed/missing) |
| Gate failure and its actual writer/gate reruns | lanes `test_gate_rework_links_all_reexecuted_nodes_to_the_actual_failed_gate` |
| Broken check / missing tool / dirty tree | lanes `test_gate_failures_travel_the_complete_graph`, `test_normal_roadmap_shell_checks_keep_observed_faults` (normal ROADMAP gate and staged execution, quoted missing path, real pytest selecting nothing, unknown compound); runtime direct-check and dirty-tree tests; `tests/test_validation_evidence.py::test_roadmap_shell_evidence_uses_command_identity_not_output` |
| Bad output / missing sentinel | lanes `test_reviewer_garbage_output_is_observed_without_agent_blame`, `test_worker_classification_reaches_runtime_ledger_once`, `test_empty_output_retry_keeps_actual_worker_cause`, `test_malformed_endpoint_tool_calls_keep_actual_runtime_cause` (real CLI classification/retries and three undecodable endpoint turns) |
| Worker death | lanes `test_real_dead_worker_recovery_is_one_allocation_per_attempt`; runtime standalone reconciliation test |
| Quota / rate limit / endpoint | lanes worker-classification, endpoint and real-reroute tests; runtime route-wait tests (repeated ticks, restart, separate later episode, ineligible routes) |
| Judged diagnosis / final run | lanes `test_judged_diagnosis_and_final_run_share_durable_cause`; malformed/multiple-role/refused/apply-refused/final-review tests |
| Question to Dan | lanes `test_real_question_poll_and_reexecution_keep_spec_gap` |
| Deny approved / disapproved | lanes `test_deny_channel_poll_and_gate_rework_keep_cause` |
| Permission allowed / declined | lanes `test_permission_channel_poll_and_rerun_keep_cause` |
| Manual Redo reasons | lanes `test_existing_manual_redo_channel_keeps_explicit_reason`; runtime handler test preserves unknown free text |
| Acceptance rejection reasons | lanes `test_existing_acceptance_channel_keeps_explicit_rejection_reason`; missing later-review origins remain unknown |
| Staged integration check failure / actual merge conflict | lanes `test_real_integration_failures_travel_the_complete_graph`; runtime `test_real_merge_pipeline_retains_only_observed_causes` also covers guard refusal |
| Controller error / uncertain result | lanes `test_real_controller_error_after_claim_records_maestro_fault`; runtime uncertain-result test |
| Unknown cause | runtime `test_actual_result_ingest_emits_one_row_and_replay_is_noop` unknown parameter; report malformed/contextual cases |

Every extra attempt has one logical `attempt:<id>` allocation. There may be several
append-only amendments to that allocation, never several token allocations for its
findings or causes. A whole graph can have many real extra attempts; the proof asserts
one row for each applicable attempt, rather than one row for the entire failed run.
Cause-table completeness is additionally checked by
`tests/workflows/test_w2_telemetry.py::test_one_of_each_extra_step_has_one_durable_row_with_its_mapped_fault`.

## Future-only seams and honest limits

`runtime::test_future_owners_can_record_observations_without_product_behavior`
proves W6 brief/check disagreement, W11 `task_did_not_serve_goal`, W13 false alarm
and diagnosed-role capture through the durable API. Each explicit future occurrence
is idempotent, has no invented attempt spend and changes none of the attempts,
node states, runs, workflow revisions or operation intents. These are external
observer calls in a test, not production W6 step scripts, new reason buttons, goal
checks, runaway policy, early diagnosis triggers or planner forecasts.

Production quota/rate/endpoint evidence has **unknown** 5h admission because W5 is
unbuilt. It is unclassified with `fault_context_missing`. Table-contract tests alone
exercise explicit true/false admission supplied by a future observer; they do not
claim routing currently makes that admission decision. Account quota differences
stay observations, never attributed or summed consumption.

## Robustness, accounting and CLI

- Runtime tests cover append-only corrections, repeated/stale callbacks, actual
  store/runner restart after mixed question-family rulings and a later failure,
  first/prior downstream reset triggers, repaired-run inheritance and stale overrides.
- Missing artifact, non-dictionary/malformed manifest and Git failure preserve the
  review score with flagged null diff statistics. Actual claim/base/head and recorded
  route price remain frozen despite current HEAD/catalog changes and runner restart.
- Capture/savepoint failures preserve successful ingest, core events and diagnosed
  repair behavior; standalone recovery remains idempotent.
- Report tests prove task/fault/role/route coverage, partial metrics, mixed origins,
  contextual roles, current-table remapping plus immutable captured allocation,
  pool-local cost sums, mixed-pool unavailable totals and strict JSON under malformed
  contextual evidence. Missing measurements stay null with coverage.
- CLI tests exercise controller-owned WAL read-only access, a real subprocess,
  legacy missing-column schemas, task/absent-task filters, missing/invalid databases
  and required arguments. No controller acquisition, migration or current repricing.
- The original saved P12 fixture is unchanged: one accepted DuetFlow run, 14 attempts,
  4,718,781 known tokens (5/14), 2,031 known wall seconds (6/14). It cannot establish
  historical complexity, blame, prices or the unavailable ITV/whole-P12 history.

Final independent whole-range review, both finding closures and frozen/clean exact
source qualification are recorded in `p13-w2-completion.md`. The final docs-only SHA,
its own qualification and full W2 close-out live in root
`handoffs/2026-10-15-p13-w2-complete.md`; this inventory supplies requirement evidence.

## Final independent review corrections

The final whole-range review found two Important gaps; neither was covered by the
previous passing gates. F1 omitted existing BAD_OUTPUT kinds `empty_output` and
`malformed_tool_calls`. Both now normalize to `garbage_output`, preserving the worker's
original typed failure in its result and attempt. Real graph regressions failed before
the adapter fix; the CLI retry and endpoint runtime now allocate their own role.

F2 exposed normal ROADMAP `cmd` shell execution rather than injected direct argv.
Literal commands preserve shell execution and its outcome/status. An absent executable
plus shell status 127 supplies environment evidence; an identifiable pytest invocation's
status 5 supplies no-tests evidence. Composition, expansion and unidentifiable commands
stay unknown. Arbitrary commands returning 5/127 or printing diagnostic-like text cannot
masquerade as pytest or a missing executable. Staged integration retains these causes;
only an actually observed `check_failed` becomes `escaped_defect`. Mixed staged causes
retain one flagged unknown allocation and all observations.

The archived `~/maestro-evidence/w2-remainder-evidence/final-independent-review.md`
is the review; the run logs (deleted) were
`review-f1-red` (two intended failures), `review-f1-green` (107 passed),
`review-f2-red` (11 intended failures, two safeguards passed), and
`review-fixes-green` (350 passed). The first F2 green run exposed two test expectations
for null rather than the public ledger's `unclassified` label; no production defect was
inferred from them. Wider regression passed 1075 tests in 105.34s; the absolute shell
wrapper safeguard passed 14 tests in 5.26s after its failure-first correction. Frozen
and clean exact final source gates each passed 6510 tests, 6 skipped, 1 xfailed,
229.51s / 230.36s, both exit 0 without warnings. Docs-only gates remain separately
pinned to the final completion commit in the root record.
