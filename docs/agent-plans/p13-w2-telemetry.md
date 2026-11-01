# P13 W2 initial usage reporting slice

**Source:** root `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md`, W2.
**Goal:** report durable task/route usage honestly without taking controller ownership.
**Authority:** root AGENTS.md and `handoffs/2026-10-13-p13-w2-start.md`.
**Approval:** Dan explicitly authorized planning, implementation, review, full qualification,
an isolated commit and clean-commit qualification on 2026-10-13.
**Execution:** `executing-plans`, inline. Base is exact qualified merge
`6d758b061bf5b0d0e3d718cce3bb8e74aea42c64`. No pending decision.

## Constraints

Only new W2 reporting modules/tests/docs. Preserve W8, root edits and all parent branches.
No storage/runtime/CLI edits, migrations, live pilots, release, push or integration.
Context Gate stays unbuilt. Use a stable read-only SQLite transaction; never open
ControlStore in production reporting. Missing measurements remain unknown, including
partial totals. Recorded relative_cost is required; never consult current policy.
Costs are comparable only within their recorded quota pool. Account quota observations
are not attempt consumption and must never be summed across attempts.

## Verification commands

Use root `.venv/bin/python` with explicit worktree PYTHONPATH and private MAESTRO_HOME,
pytest temp directory and credentials/source variables cleared. Logs and exit files have since
been deleted. Full-suite runs use a resource-bounded named agents TMUX
window. Expected baseline: 6086 passed, 6 skipped, 1 xfailed; reproduce independently.

### Task 1: read-only task/route roll-up

Create `maestro/usage_report.py` and `tests/test_usage_report.py`.
Consumes attempts, task_runs, node_states, workflow_revisions and durable events.
Produces `build_usage_report(database, *, task_id=None)` and a deterministic renderer.
Define counts by workflow node role/kind and attempt outcome, across all runs/retries;
include failed/canceled/unfinished attempts and distinguish unavailable classification.
Expose split token and wall/cost measured sums with measured/total coverage. Preserve
nullable historical columns, malformed telemetry and missing route facts explicitly.
Keep quota start/settle evidence per attempt/window, reset/conflict/missing reasons and
scope. No fabricated task quota total. Handle older schema without adding columns.

Steps: write one public behavior test; run focused pytest and observe intended RED;
implement and run GREEN; repeat vertically for retries, malformed/partial telemetry,
route/pool separation and quota boundaries. Prove no database mutation or ownership
claim alongside a synthetic controller, and consistent snapshot under concurrent writes.
Focused command: root Python `-m pytest tests/test_usage_report.py -q` (private env).
Done when these public behaviors pass and the baseline full suite is green.

### Task 2: P12 replay and review

Depends on Task 1. Create sanitized saved-evidence fixture(s) under
`tests/fixtures/usage_report/` and `docs/W2_USAGE_REPORT.md`.
Locate verdict/manifest and immutable saved snapshots; pin provenance/hash and hand
counts. Replay only saved/copied evidence; report every unavailable historical metric.
Add regression(s), observe RED for any newly required behavior, then focused GREEN.
Use spec-code-review contract for the whole slice; fix valid Important findings with
failure-first evidence. Inline execution explicitly requires a fresh reviewer when
available; that skill authorizes one focused independent review, no implementation
delegation. Record the review snapshot and finding resolutions.
Done when replay agrees with observable hand counts and review has no unresolved blocker.

### Task 3: frozen candidate and exact clean commit qualification

Depends on Tasks 1-2. Run full pytest with private state, bounded resources and source,
diff/index/status before/after checks. Expected: all tests pass, no source mutation,
six root hashes intact, parent refs intact (W8 may independently advance).
Commit only the new W2 files on a new W2 branch, after full qualification.
Run full qualification again on clean exact commit, record SHA, counts, skips/xfails,
timings and exit codes. Verify root and other checkouts preserved. Save completion
record with remaining W2 seams. Do not mark full W2 closed.

## Review focus

Task 1: tokens must be cumulative attempt splits, not account context counters;
partial components do not create a complete cost. Role counts must not parse IDs.
Task 1: no sum of overlapping account deltas, reset crossings, unknown costs or
cross-pool comparisons; stable read-only WAL snapshot without migration/ownership.
Task 2: historical evidence classification follows recorded DAG/outcomes/events;
hand counts must name unavailable telemetry and avoid claiming full P12 coverage.
Task 3: full qualification must execute worktree code with private host state and
source checks; root hooks must never run a release.

## Later W2 slices (not authorized here)

Reviewer complexity rubric/schema and diff stats; append-only blame capture and
cause-to-role mapping; nullable storage where required; per-fault roll-up and existing
`maestro report usage` CLI wiring after W8 ownership recheck. No new public command.
Next safe slice: refresh W8/dependency boundary, then plan blame/complexity capture
against accepted W2 contracts. Full W2 checklist remains open.

## Verified session checkpoint (2026-10-13)

Fresh session and continuation replacement verified. Native W2 worktree/branch created
at exact base. No production code or commit yet. Initial baseline used pytest temp
state under root .scratch: 8 failed/6078 passed/6 skipped/1 xfailed,857.25s,exit1;
frozen file/index/status/diff checks all0. Tracebacks prove Git parent discovery and
pytest config inheritance contaminated non-repo/scratch fixtures. All eight failing
tests passed in3.03s with /tmp test temp location,exit0. Full corrected baseline still
owed. New qualification runner uses private /tmp temp state and private MAESTRO_HOME.
The first usage reporting test was observed RED (API missing),1failed,0.08s,exit1.
Tests remain a draft outside the worktree until the successor resumes TDD; none GREEN.
Saved P12 projection and role-DAG provenance are prepared and hash-verified.
Close-out record and next actions: root `handoffs/2026-10-13-p13-w2-next.md`.

## Implementation/review checkpoint (2026-10-13)

Corrected frozen baseline6086passed/6skipped/1xfailed,237.14s,exit0; import,
source/hash/status/index/diff checks all0. Initial implementation uses exactly five
new files: reporting module, dedicated tests, saved P12 fixture, this plan and
W2_USAGE_REPORT. No shared runtime/storage/CLI source was modified.
Public tests30passed,0.75s,exit0 after vertical behavior cycles and review regressions.
Fresh independent spec reviewer inspected all five files and verified provenance,
ran24focused tests, and found0Critical/Important,4Minor. All four were addressed
within the existing malformed-data/provenance contract: derived integer overflow,
quota metadata/window validation, recorded W3 envelope fields and corrected capture
documentation. Behavior fixes observed RED then GREEN; documentary correction checked
against exact qualified-base routing.py. Root review record:
handoffs/2026-10-13-p13-w2-review.md. No pending question or unresolved finding.
Task3 frozen candidate and exact clean-commit qualification remain the final gates.
