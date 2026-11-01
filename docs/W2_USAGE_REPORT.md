# P13 W2 usage reporting and runtime capture

W2 captures runtime telemetry and reads durable SQLite facts without report-side controller ownership,
migrations, leases or writes. Callers supply a saved or local
database path; tests use synthetic stores and one sanitized saved P12 run.

```python
from maestro.usage_report import build_usage_report, render_usage_report

report = build_usage_report(database, task_id="optional-task-id")
print(render_usage_report(report), end="")
```

The read-only CLI emits the same deterministic JSON:

```sh
maestro report usage --database /absolute/path/control.sqlite3 --task TASK-001
```

`--database` is required; `--task` is optional. A missing database exits 1 without
creating it. The CLI does not acquire a ControlStore or load today's route prices.

`read_snapshot(database)` opens SQLite `mode=ro`, `query_only=ON` and an explicit
transaction. Its first schema read establishes the snapshot before yielding. Reading
a WAL database may coordinate read marks in its existing SHM file; application data,
schema, events and controller/writer ownership remain unchanged. Missing files raise
SQLite's open error without creating a database. Core tables are required; older
attempt schemas may omit telemetry columns, which are named in the report.

## Counts and grouping

`tasks` contains every recorded task with a run, sorted by task ID. An optional filter
limits task output, while overlap detection still considers all recorded attempts.
Every claimed attempt counts, including retries, failed, canceled and running attempts.
Run status counts and attempt outcomes remain separate. Current `node_states` and
`node_phases` do not replace attempt history; a skipped node without an attempt adds
no cycle. Each run's exact immutable workflow revision supplies role/kind/handler.
Node IDs never infer roles. Unknown or malformed DAG facts remain unclassified.

Counts distinguish implementer cycles, reviewer rounds, gate attempts, explicit failed
checks (`failed`, `bad_output`, `verification_failed`), classified holds and unclassified
gate failures. Review rejections do not become failed gate checks. Other handlers retain
their recorded handler identity as the role. `routes` groups by role plus harness,
model, effort and quota pool; no current route catalog or policy is consulted.

## Measurements and unknowns

Each task/route metric has `known_sum`, `measured_attempts`, `total_attempts`, `complete`.
The denominator includes every durable attempt in that group, including function/human
attempts with unmeasured telemetry. No measured values yields null, including empty
groups; measured zero remains zero. Partial sums stay explicit. Nonfinite/negative,
boolean and fractional token measurements are discarded with issues. An overflowing
sum becomes null/incomplete even when its individual values were measurable.

Tokens come only from settle `token_split`: fresh input, cache read, cache write and
output. A total requires all four. Codex output already includes reasoning tokens;
`reasoning_output` is never added again. Context counters are not usage splits.
Cost units require complete tokens, positive finite *recorded* `relative_cost`, pool,
and a complete harness/model/effort route. W2 now freezes the selected pair's price
in `AgentBinding` at resolution and serializes it for claim. Historical/manual bindings
without that recorded price retain unavailable cost.
Selection explanations and today's policy never supply a historical price.

Costs stay comparable within a quota pool. Mixed-pool tasks have null task cost with
`mixed_cost_pools`; individual route costs retain coverage and known sums. This is not
a dollar estimate. `attempt_details` names each attempt's missing measurements and
issues, allowing coverage gaps to be traced without exposing raw transcripts.

## Quota evidence

`quota_consumption` is always null. `quota_observations` preserves per-attempt/window
start/settle used percentage, source, observation/read time, reset and sample error.
W3's recorded observed_at, staleness_bound and sample pool_id are retained. Observation
intervals prefer the recorded provider observed_at and fall back to historical updated_at.
`observed_change_pct` is only an account counter difference, never attributed attempt
consumption; it is never summed into task usage. Reset crossings, decreasing counters,
source changes, missing/invalid windows, errors and unknown intervals/pools have explicit
unavailability reasons. Reset timestamps within 600 seconds share the runtime's jitter
tolerance. A recorded reset-crossing flag conservatively suppresses all its windows.

Every observation states freshness is unverified and attribution is unavailable. A
positive overlap flag considers observations from other tasks in the same pool/scope/
window. Unknown intervals leave overlap unknown. Explicit stored `pools` maps and
`scoped_windows` maps are accepted independently when present, in addition to headline
`windows`. The shape is `{pool_or_scope: {window_minutes: window}}`; the qualified W3
base already serializes these maps when its reader supplies them. W2 preserves those
samples without inventing capture for absent windows.

## Saved P12 replay

Fixture: `tests/fixtures/usage_report/p12-07-replay.json`. It is a sanitized projection
of one accepted DuetFlow run `07-reconciliation`, workflow `wf_07-reconciliation@1`.
Export: `artifacts/graph-engineering/p12-pilots/duetflow-07-run4-accepted-20260930.json`,
SHA256 `a932b488df8929602e76e5531d99e511a90ead43e4aaac4d335b54cdbfada0e3`.
The DAG roles come from the same saved revision in the archive named by the fixture;
raw `dag_json` SHA256 `556339087f2e8b55a965af7af894e2d6e17c29c3e6851b4522577ce5154799fc`.
Only read-only saved sources were used; tests require neither archive nor live store.

Hand counts: one succeeded run, 14 attempts, 3 implementer cycles, 3 review rounds,
5 gate attempts, 0 failed gate checks, 2 gate question holds. Two review rejections
are separate. Five measured Codex splits sum to 344,855 fresh input + 4,336,896 cache
read + 0 cache write + 37,030 output = **4,718,781 tokens**, coverage 5/14. Six agent
wall measurements sum to **2,031 seconds**, coverage 6/14. The agy implementer has no
split. No attempt has recorded relative cost. Quota remains diagnostic account evidence.

This fixture establishes one observable accepted-run replay, not all 321 P12 pilot
attempts or the ITV pilot. No saved ITV database/export was located in the bounded
search. Historical complexity, blame, final acceptance decisions and goals are not
covered. Current runtime coverage and qualification are recorded in
`agent-plans/p13-w2-proof-inventory.md` and `agent-plans/p13-w2-completion.md`;
they do not fill these historical gaps. The Context Gate remains unbuilt.

## Cause, complexity and capture contracts

The full remainder is authorized in `docs/agent-plans/p13-w2-remainder.md`. The cause
mapping now extends the seven review origins through one `CAUSE_FAULT` table;
`ORIGIN_FAULT` is its compatibility view for review findings. Dynamic rules require
recorded own-role/rebriefed-role/5h-admission context, otherwise they stay unclassified
and flagged. Dan's reason choices use explicit observed cause tokens; free text is
never guessed into blame. Future W6/W11/W13 behaviors remain their owners' work.

`telemetry.COMPLEXITY_RUBRIC` defines five fixed anchors across files, reach, new
behavior and risk. Reviewer scores are nullable; invalid/missing values only flag
telemetry. `diff_stats` records actual numstat between immutable base/head commits,
with binary-file count and unavailable Git evidence explicitly represented.

`telemetry_capture` appends nullable facts to the existing controller-owned events
table. No new writer or migration is introduced. Repeated observations are no-ops,
including an old callback delivered after a correction. New evidence appends an
amendment with the same logical ledger id. Capture failure never changes a verdict.
Production runner ingest, review parsing/prompts, operator events, dead-worker
reconciliation and CLI call these seams. The one-of-each-cause fixture proves the API
with a real ControlStore. Separate real graph and runtime lanes prove production
emission; the proof inventory names each requirement and its executable evidence.

The read-only report adds `blame_ledger`, `faults` and `complexity_reviews`. Each
fault has task measurements plus role/route groups, with each attempt counted once.
Wait occurrences without attempts have no invented spend. Amendments select the
latest logical row; code remaps cause with the current table and exposes the immutable
`captured_fault`. Mixed-pool fault totals have unavailable cost; routes retain their
own pool sums. Quota stays per-attempt account observations, never consumption totals.
Absent historical capture yields empty views and explicit history-unavailable issues.

The independent `telemetry_observation` adapter reads actual `NodeResult` envelopes
and recorded question/ruling events without storage or verdict changes. Typed causes
are mapped; free-text failure detail never implies planner blame. Multiple later-review
origins remain observations on one logical attempt. Mixed faults (including a missing
origin among known ones) leave allocation unclassified and flagged; no spend is split
or counted twice. Capture retains missing origins as null observations. Runtime and
graph tests below prove emission separately from the adapter/API contracts.
Reports recompute allocation from every recorded origin using the current fault table;
`captured_fault` and `captured_flags` retain the original allocation separately. A
typed bad-output failure or a question to Dan takes precedence over a later review's
empty origins; an unreadable review is attributed to its own reviewer role.

Report provenance preserves `review:<attempt>` identities, validates and retains
the captured rubric version (including unknown future positive versions), and exposes
the validated string flags in `diff_stats`. Missing/invalid recorded rubric versions
remain null with explicit flags, rather than being relabeled as the current rubric.

Mapping version 2 (P13 GDT-12 / W12, DESIGN §19.14): `task_did_not_serve_goal` maps to the
`unattributed` fault, a member of `FAULTS`. A stored W11a row (mapping version 1, fault
`planner`) reads `unattributed` with `captured_fault: planner` kept; nothing is migrated in
the database. Each task gains `goal_impacts`: one row per `GoalImpactObserved` ledger id with
the latest `attribution` (W12's evidence rules append amendments; `unattributed` until
evidence establishes a fault), the `captured_attribution`, the number of `amendments` and the
observation's provenance. The attribution never changes a run status or verdict.

## Runtime capture and frozen measurements

`tests/workflows/test_w2_runtime.py` uses real start_run, tick, inbox/receipt ingest,
ControlStore events, public review parsing and standalone Reconciler. Only process
launching and liveness are replaced. It proves typed worker/provider/controller
failures, later-review origins, nullable complexity, operator amendment linkage,
dead-worker capture, replay idempotence, a successful rework's trigger linkage,
and capture failures that preserve the core event/result. Reviewer instructions use
the same fixed rubric and ask for the nullable score after reviewing the frozen diff.
The diff cross-check uses the run's immutable base and claim manifest's reviewed commit;
missing evidence stays flagged. Actual Git, claim-manifest and runner-restart tests
prove this subject stays frozen when current HEAD or the route catalog changes.

Operator capture shares the controller's existing connection and event transaction,
behind savepoints. Repeated callback payloads remain no-ops; internal write failures
roll back only diagnostics. A rework's `trigger_attempt_id` and `source_event_id`
retain causality while report role/route groups describe the attempt spending tokens.
Its fault mapping uses the triggering role for role-dependent causes.

Runtime capture records one durable route-wait episode between claims,
including repeated ticks and runner reconstruction. Waiting without a claim spends
nothing. Typed quota refusal retains its cause with admission unknown; concurrency
saturation and unspecified pauses remain unclassified. W5 admission is not built.

Gate execution now carries nullable cause metadata in the result's evidence document:
no command or missing declared cwd is `check_broken`; a missing direct executable is
`environment_missing`; an observed ordinary failing check is `check_failed`. Direct
pytest selecting no tests is `check_broken`. Normal ROADMAP shell checks retain the
same causes only for defensible literal invocations: absent executable plus shell 127,
or identifiable pytest status 5. Composition, expansion, shell wrappers and unknown
invocations stay flagged unknown. Arbitrary status 5/127 or diagnostic-like output
cannot establish a special cause. Timeout/unknown execution errors stay unclassified.
Actual dirty-tree refusal records `uncommitted_tree`; a Git status error does not.
Cause metadata uses existing results/events, with no evidence-table migration.
Mixed check causes retain one unknown allocation. Command output never decides blame.

Real routed-claim tests prove recorded prices survive runner reconstruction after the
current catalog changes, and review diff statistics retain the claim's reviewed commit
and run's base after current HEAD changes. Invalid scores keep their parser flag;
unreadable manifests preserve the available score and flag an unavailable diff.

## Causal runtime and operator observations

Real Git integration fixtures capture observed staged `check_failed` as `escaped_defect`
and an actual conflict as `parallel_scope_overlap`. Broken checks and missing tools
retain their observed causes; mixed causes and guard refusals stay unknown. Human
redo and Dan rejection retain only exact recognized reason tokens. Free text never
sets blame. Actual channel ask/poll/ruling/rerun lanes prove deny and permission causes.
Multiple rulings retain every observation in one allocation; contrary faults stay
unclassified and pending answers remain flagged rather than being hidden by a later tap.
The gate retains a recorded deny disapproval when it asks the implementer to rework.

The controller captures diagnosis only after judging the proposal with its existing
verdict and repair checks. `poor_agent` uses the actual replaced role; `bad_graph`
maps to planner. A refused diagnosis stays unknown with a refusal flag. The existing
repair event carries the diagnoser attempt id, so first claims in its invalidated final
run inherit the durable cause across runs. No repair counter or verdict changes.
Claim observers only use pending overrides newer than the last claim, avoiding reuse
of an older override. Real gate rework and quota reroute lanes retain causal links.

Wait observations now exclude hard-ineligible route rejections from temporal causes.
A real wait after the next claim is a separate durable episode; repeated ticks still
do not add rows or spend. Typed quota failures retain unknown 5h admission.

The proof inventory covers existing production lanes, first/prior downstream claims,
stale overrides, repaired runs, actual store/runner restart, cross-family amendments,
capture failures and accounting/CLI checks. Future W6/W11/W13 calls are tested only
as idempotent observations with no invented spend or product behavior.

## Causal inheritance and amendments

Retry, question and operator reset events now carry an optional actual trigger id,
independent of the target node's prior attempt. First downstream reset claims and
targets with their own prior history inherit this id through runner reconstruction.
Only pending overrides newer than the previous claim supply reset causality; a later
blocked reroute cannot reuse an old quality override. Existing phase projections and
retry counters retain their meaning.

A concrete inherited cause survives alongside later result observations. Each cause
retains its own role, rebriefed role and admission context in `cause_observations`;
`observed_causes` remains display provenance. The report remaps every contextual fact
through the single current table and counts the spending attempt once. Contrary or
missing later observations flag one unknown allocation rather than erasing history.
An unknown provisional claim cause can be resolved by later typed evidence. A judged
diagnosis replaces its provisional failure observation; multiple actual rebriefed
roles remain explicit. A repair refused during application amends the diagnosis to
flagged unknown, and diagnostic observer failure cannot prevent the existing repair.

The graph tracer now covers all seven review origins, stale blocked reroutes, final
review recapture, malformed and multiple-role diagnoses, repair refusal and capture
failure, real spec-question polling, dead-worker recovery, existing manual/acceptance
reason channels, controller errors, worker quota/rate-limit/no-sentinel classification,
in addition to permission/deny/gate/repair lanes. Typed gate and merge cases use real
handlers plus controller ingest; whole graph lanes also exercise normal ROADMAP
checks, actual merge conflicts, a real 503 endpoint, malformed reviewer output and
the normal happy path without extra allocations. Existing BAD_OUTPUT `empty_output`
and three-turn endpoint `malformed_tool_calls` normalize to own-role `garbage_output`
at the telemetry adapter; the worker's typed failures and verdicts remain intact.

Malformed contextual evidence cannot silently become a confident allocation: invalid
members/contexts yield `cause_context_invalid`, an unclassified allocation and strict
JSON-safe provenance. Mixed deny, permission and spec-question observations survive
later result ingestion, actual store/runner restart and older callback replay.

## Qualification and limits

Qualified final source is `89e0817cff72bb9aa2daa190dd781d585d4dc7ad`. Frozen and clean
exact-source full suites each passed 6510 tests, with 6 skipped and 1 xfailed; both
exited 0 without warnings. Independent whole-range review found two Important gaps;
the bad-output and normal-shell fixes above each have failure-first runtime evidence
and both are closed. See the completion record for exact timing, evidence and docs gates.

Quota admission remains unknown because W5 is unbuilt; provider faults therefore retain
missing-context flags. W6/W11/W13 product behaviors and planner forecasts remain their
owners' work. No live W2 pilot, root integration, release, push or Context Gate is claimed.
