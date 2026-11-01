# P13 W2 remaining telemetry implementation

**Source:** root `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md`, W2, B10/B20.
**Goal:** capture complexity and every extra happy-path step, and expose honest per-fault usage.
**Authority:** root AGENTS.md; `handoffs/2026-10-13-p13-w2-all-next.md`.
**Approval:** Dan authorized ALL remaining W2, isolated commits, review and qualification,
with automatic same-pane fresh sessions until finished. Dan resolved the base choice:
build on the finished W3/W8/W9 root. That completed base `33a170d` is included in
qualified W2 merge `38bb6f2`, preserving all W2 history. No approval is pending.
**Execution:** `executing-plans`, inline, reuse qualified native source W2 `89e0817` on
`feat/p13-w2-usage`. Tasks 1–3 and the independent review corrections are complete;
Task 4's W2-owned completion docs and exact docs-only qualification are recorded
separately in the durable root completion record; source qualification is not
used as a substitute for that gate.
Source gates: 6510 passed, 6 skipped, 1 xfailed, frozen 229.51s / clean 230.36s,
both exit 0 with unchanged fingerprints and no warnings. See `p13-w2-completion.md`.

## Global constraints

Preserve clean root `62f12a0`, W8 `1925a05`, all other owners and stable refs.
Completed W8 reliability changes are included; W2 runtime wiring is authorized here.
Work only in the W2 checkout and W2 root-only
evidence/docs. No root integration/release/push/live changes, Telegram or Context Gate.
Telemetry is nullable, append-only and best effort; it never changes a verdict.
Agents observe cause, code maps fault through one revisable table. Unknowns are flagged.
Use only immutable claim-time relative_cost, never today's policy for old attempts.
Costs stay inside quota pools; quota deltas are account evidence, never summed consumption.
W6/W11/W13 get capture seams only; their product behaviors are outside this workstream.

## Verification

Root `.venv/bin/python`, explicit native PYTHONPATH, private MAESTRO_HOME and /tmp
pytest state; bridge/reset/auth overrides cleared. Evidence archived outside the repo (narrative notes only). Focused tests before and after each behavior change.
Full suite on each frozen candidate before any commit, then clean exact-commit full
qualification. Use bounded named `agents` TMUX windows and runner-owned exit files,
source fingerprints/status/index/diff checks. Fresh independent spec-code-review
is authorized for the final W2 range; fix valid blockers test-first. Root hooks must
skip linked worktree commits. No test/source mutation during qualification.

### Task 1: complexity and cause contracts

Create `maestro/workflows/telemetry.py`, `tests/workflows/test_w2_telemetry.py`.
Extend `review_origins.py` through one cause-to-fault table while preserving the seven
review origins. Fixed rubric 1–5 covers files, reach, new behavior and risk; validate
nullable reviewer score and preserve missing/invalid flags. Capture Git numstat for a
frozen base/head as a cross-check, including binary files and Git failure unknowns.
Normalize every spec cause with context for own-role/rebrief-role/5h admission and
Dan's reasons; ignore agent-provided blame. Future product seams accept observations
without implementing future scheduling or questions.

Steps: public test RED, minimal implementation GREEN for each contract; focused tests
`tests/workflows/test_w2_telemetry.py tests/workflows/test_review_origins.py`.
Done when every listed cause maps correctly, unknowns are flagged, malformed complexity
is harmless, real Git cross-check works, and legacy origin tests stay green.

### Task 2: runtime and storage capture

Depends on completed Task 1 and qualified finished base. Modify W2 hunks only in router binding,
store/lifecycle/runner/handlers/worker and role templates; add dedicated integration tests.
Append nullable complexity/diff and one extra-step record per attempt/event with durable
idempotent keys. Hook claim, settle, retry/rework, waiting, reconciliation and operator
rulings; preserve unknown causes and costs. Record review causes without multiplying
attempt usage by finding count. Record route cost at resolution/claim from the chosen
pair. Reviewer prompt/schema asks score using Task 1 rubric; planner forecast stays W13.
Prove normal first happy-path attempts have no extra-step row; each listed extra step
has exactly one durable row through real runtime seams, including restart/idempotence.
Telemetry failure must not fail a node/run.

Steps: behavior regressions RED/GREEN, then widest runtime/control/route focused suite.
Done when real claim/settle/ruling/reconciliation paths retain facts and no result changes.

### Task 3: per-fault roll-up and report CLI

Depends on Task 2. Extend `usage_report.py`, add read-only `maestro report usage`
with explicit database and task filter through existing CLI conventions; tests in
`tests/test_usage_report.py` and dedicated W2 CLI tests. Views group task/fault and
fault/role/route with coverage, no duplicated attempt metrics or cross-pool cost total.
Expose complexity/diff cross-check and extra-step evidence, retaining old-schema behavior
and unknown historical data. Preserve P12 saved replay and add runtime proof fixture.
CLI never takes ControlStore ownership or creates/migrates/writes a database.

Steps: public report/CLI tests RED/GREEN, focused API/CLI/runtime verification.
Done when immutable evidence drives per-fault sums, legacy reports remain honest,
and controller-owned WAL report succeeds read-only.

### Task 4: review, qualify and record full completion

Depends on Tasks 1–3. Fresh independent whole-range spec-code-review; resolve blockers
with failure-first tests. Update W2-owned docs, spec and thread evidence only after full
requirements are verified. Frozen full qualification before isolated commit(s), clean
exact-commit full run after. Save durable completion with exact SHA, counts/times/exits,
review limits and preservation. Do not claim W2 complete on a partial slice.
Automatically replace sessions at close-session boundaries until all four tasks finish.

## Review focus

Task 1: one table and unchanged seven origins; invalid agent evidence never decides fault.
Task 2: missed sentinel/controller error from W8 must not be invented on older base;
unknown capture fails harmlessly; duplicate callbacks/restarts do not duplicate ledger.
Task 3: multiple origins never multiply tokens; quota remains observation-only;
mixed pools suppress grand cost totals; older databases require no DDL.
Task 4: qualify exact frozen/native sources, preserve all other owners and root hooks.
