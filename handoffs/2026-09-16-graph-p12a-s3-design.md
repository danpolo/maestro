# P12A S3 — design contract (W4, W5/W6, W7, W9, W10, W11)

Written by the S3 orchestrating session after orientation. Steps A–D run **sequentially**,
one agent each, because they share `maestro/workflows/runner.py`, `maestro/orchestrator.py`
and `tests/graph_engineering/test_dispatch_wiring.py`. Every step reads this file,
`handoffs/2026-09-16-graph-p12a-s3.md` (context, harness, invariants, coordination) and
`docs/graph-engineering/specs/phases/P12A_GRAPH_DISPATCH_WIRING.md` §2 (its own W-items),
§3 and §4. S2's shape is in `handoffs/2026-09-15-graph-p12a-s2-design.md`.

The decisions below are fixed. If one proves impossible, stop and report why; don't invent
a substitute. The same applies if an item can only be built by bypassing
`WorkflowRunner.advance` → `dispatch` → `launch_worker`, adding a second fresh-vs-resume
decision, or adding a context rotation. That is §4 design drift and needs Dan.

Tests for every W-item drive the loop through `run_lane` (P12A §1: "wired" means proven
under one poll of the loop), in `tests/graph_engineering/test_dispatch_wiring.py`, except
where a line below names another file. Use the existing helpers there (`_graph_scenario`,
`resolutions`, `_attempts`, `_node_phase`, `_events`, `fixture_routing`). Unit tests for new
pure helpers go in the file that already tests that module.

**Behaviour with `routing is None` (every `tests/workflows` test) must stay exactly as it is
today**, except where W4 says otherwise.

---

## Step A — W4 candidate reap

**A-1. The join must be reachable when a candidate fails.** Today `compose` joins every
candidate `on_success` (required) into the next stage and gives each candidate an
`on_failure` edge to `park`. One crashed candidate therefore *skips* `candidate_select`
and fires `park`, so the join never runs and nothing can be marked `abandoned`. Fix in
`compiler.compose`, **only for a group whose `stage.fan_out == "candidates"` and width ≥ 2**:
- edges from each candidate into the next stage use `guard_predicate="always"` (required);
- candidates get **no** per-candidate `on_failure` edge to the sink (the join's own
  `on_failure → park` handles "no conclusive candidate").
Width 1 must stay byte-identical (`tests/workflows/test_compatibility.py` untouched and
green). If a stored-graph compatibility test pins widened graphs, update its expectation and
say so in the report.

**A-2. Documents.** New `WorkflowRunner.candidate_documents(node) -> dict[str, dict]`:
- candidate nodes = `candidates.specs_from_nodes(self.repo, task_id, self.revision.nodes)`;
- for each, the **settled** attempt = the latest `attempts` row for that node in this run
  whose result was committed by `_commit_result` (i.e. `outcome_ref IS NOT NULL`). A
  candidate with no such attempt (crashed / reconciled with no receipt / never ran) gets
  **no document** → the handler marks it `abandoned`.
- document = `{"status": attempt status mapped into CANDIDATE_STATUSES (succeeded → succeeded,
  anything else → failed), "evidence": [rows from verification_evidence WHERE attempt_id =
  that attempt], "head_commit": outputs["head_commit"] from the stored node_result artifact
  (or "" ), "detail": result detail}`. Read the artifact through `self.artifacts`
  (`ArtifactStore`); with `artifacts is None` there are no documents (fail closed).
- `node_inputs(node)` sets `inputs["candidates"] = self.candidate_documents(node)` for
  `node.handler_ref == "maestro.handlers.candidate_select"`, **applied last** so an
  `inputs_provider` can never substitute its own documents (D06). This applies with or
  without routing (it only adds information the handler already expects).

**A-3. Tests (§3 W4).** A free-quality lane with 3 candidates (see the existing
`free_policy` usage at `test_dispatch_wiring.py:340` and how the lane's workers finish): c1
and c2 finish with passing evidence rows + `head_commit`; c3's worker dies with no receipt
(reconciled to `failed` with no outcome). Assert: `candidate_select` ran, exactly one winner
in its outputs, c3 reported `abandoned` (present, never dropped). Plus a compiler unit test
that width-3 candidate edges into the join are `always` and that width 1 is unchanged.
Every candidate launch passes through `launch_worker` once (spy).

---

## Step B — W5 affected checks + W6 persisted shadow comparisons

**B-1. Config.** `config.affected_checks_mode(document=None) -> "shadow" | "authoritative"`
reads `engineering.affected_checks` (absent → `"shadow"`; any other value → `ConfigError`,
following the `engineering_runner` precedent). `DispatchRouting` gains
`affected_mode: str = "shadow"`.

**B-2. The switch.** In `_graph_main`, after `load_statistics`: if the configured mode is
`authoritative`, compute `validation.qualify_affected(validation.load_shadow_comparisons(store))`.
If it does not return `authoritative`, the switch is **refused**: `routing.affected_mode`
stays `"shadow"`, the loop keeps running, and the reason (which carries the count still
missing, e.g. `3/20 clean shadow runs recorded`) is printed and journalled as
`affected_mode_refused`. Otherwise `routing.affected_mode = "authoritative"`.

**B-3. Selection at claim (W5).** In `advance`, for a node with
`handler_ref == "maestro.handlers.verification"` and `routing is not None`, after the claim:
- `specs = gates.authoritative_specs(task_id)`;
- `changed = validation.changed_paths(tree, revision.code_base_sha)` where `tree` is
  `self.worktree_for(node)` or `self.repo`; any exception, or a falsy `code_base_sha`,
  → `changed = None` (full suite);
- `selection = validation.select_affected_checks(specs, changed, mode=routing.affected_mode)`;
- store it: `artifacts.put_bytes(json, type="affected_selection", producer_attempt_id=...)`,
  put `{"affected_selection": selection.to_dict()}` into `_attempt_inputs[node_id]`, and
  carry the artifact id on `Claim.selection_artifact_id` (new optional field, default `None`).
- `_default_worker_argv` appends `--affected-selection-artifact-id <id>` when set; the worker
  parses it, reads the artifact and puts it into `HandlerContext.inputs["affected_selection"]`.
- `verification_handler`: when an `affected_selection` input is present, runs only
  `selection.selected` (new optional `check_ids` param on `gates.run_authoritative_gate`;
  `None` = all, today's behaviour) and echoes `outputs["affected_selection"]`. In shadow mode
  `selected` is the full suite, so behaviour is unchanged.
- `AffectedSelection.to_dict` / `from_dict` in `validation.py`.

**B-4. Persisted comparisons (W6).**
- New control-store table (SCHEMA + the store's existing migration path for new tables):
  `shadow_comparisons(comparison_id TEXT PK, run_id, node_id, snapshot_id, attempt_id,
  selection_json, outcomes_json, discrepancies_json, recorded_at, UNIQUE(run_id, node_id,
  snapshot_id))`. `ControlStore.record_shadow_comparison(row)` is `INSERT OR IGNORE`
  (idempotent re-ingest); `ControlStore.shadow_comparisons()` returns rows in insert order.
- In `_commit_result`, for a verification node whose result outputs carry an
  `affected_selection` with `mode == "shadow"`: build `ShadowComparison(selection, outcomes)`
  where `outcomes[check_id]` is `"passed"` for a passing evidence row of that result and the
  row's outcome otherwise; snapshot = `outputs["snapshot"]`. Record one row. Controller-only
  write, never from a worker.
- `validation.ShadowComparison.to_row` / `from_row` and
  `validation.load_shadow_comparisons(store) -> list[ShadowComparison]`.
  `qualify_affected` itself keeps its signature.

**B-5. Tests (§3 W5/W6).** A shadow-mode lane whose verification worker runs: the worker
received the full suite; exactly one `shadow_comparisons` row exists; it is still there after
closing and reopening the control store (the "controller restart"). With
`engineering.affected_checks: authoritative` and fewer than `minimum_runs` rows, the loop
runs in shadow and journals `affected_mode_refused` naming the count. A config unit test for
the new key (absent / valid / invalid). With a fixture of 20 clean stored rows the switch is
accepted.

---

## Step C — W7 plan repair in the loop

**C-1. Proposal wire form.** `PlanRepairProposal.to_dict` / `from_dict` in `compiler.py`
(nodes/edges through `NodeSpec` / `EdgeSpec` `to_dict`/`from_dict`). A node result carries it
at `outputs["plan_repair"]`. Only nodes with `agent_definition_ref in ("planner",
"diagnoser")` are listened to; the key on any other node is ignored (journal/event not
required).

**C-2. Quota.** `config.max_plan_repairs(document=None) -> int` reads
`engineering.max_plan_repairs` (absent → **1** — Dan's D-FAIL-2, see the end of this file; negative or non-int → `ConfigError`).
`DispatchRouting.max_plan_repairs: int | None = None` (None = unbounded, the runner-level
default); `_graph_main` sets it from config.

**C-3. Where it runs.** In `_commit_result`, before the node's phase is written, when the
result is `succeeded` and carries a valid proposal:
1. `phases = self.phases() | {node_id: "succeeded"}`;
2. `plan = compiler.plan_repair(self.revision, proposal, phases,
   counters=compiler.repair_counters(store, workflow_id, run_id=self.run_id,
   max_repairs=routing.max_plan_repairs if routing else None))`;
3. a `PlanRepairRejected` (including a malformed proposal, and quota exhaustion) → the node's
   phase is written as **`failed`** instead, the attempt finishes `failed`, and the refusal
   is recorded as a store event `WorkflowRepairRefused` (payload: node_id, reason) plus
   `route_refusal`-style durability is **not** reused — the event is the record;
4. otherwise the phase is written `succeeded`, then
   `new_run = compiler.apply_repair(store, plan, old_run_id=self.run_id,
   lifecycle=self.lifecycle, is_alive=self.is_alive, at=at)`. If `apply_repair` itself
   refuses (live writer outside the cone, stale phases), record `WorkflowRepairRefused`
   and leave the node `succeeded` and the old run as it is — document this in the method
   docstring as a known limit.
5. On success set `self.successor = self.spawn(new_run, plan.revision)` where
   `spawn(run_id, revision)` returns a `WorkflowRunner` with the **same** store, repo,
   lifecycle, dispatcher, artifacts, is_alive, worker_argv and routing.

**C-4. Moving the loop.** `TickReport.repaired_to: str | None`. `tick` stops after ingest when
`self.successor` is set (the old run is `canceled`; no skips, dispatch or settle on it) and
reports `repaired_to`. `_graph_main` replaces `runners[old]` with `runners[new] =
graph_runner.successor`, journals `graph_run_repaired` (`task old→new revision N→N+1`), and
the new runner's nodes dispatch on the **next** poll through the normal `advance` path.
The "opened" check in `_graph_main` already prevents reopening a task with any run.

**C-5. Tests (§3 W7).** A lane whose planner/diagnoser fixture outputs a proposal (e.g.
`rerun_nodes` or an added node): after the poll the old run is `canceled`, a new run exists at
revision N+1, `repair_counters` shows `repairs_used == 1`, `task_attempts` includes the old
run's attempts and carried nodes keep their `attempt_index`; the next poll launches the new
revision's node through `launch_worker` exactly once (spy). With
`engineering.max_plan_repairs: 0` the proposal is refused, the node is `failed`, a
`WorkflowRepairRefused` event names the quota, and no new run exists. Round-trip unit test
for `PlanRepairProposal.to_dict/from_dict` in `tests/workflows/test_plan_repair.py`.

---

## Step D — W9 oversized park, W10 async hold snapshot, W11 run listing

**D-1. W9 helper.** `scheduling.exceeds_capacity(request, capacity) -> str | None`: for the
first counted dimension (sorted) whose `capacity.limits` has a bound strictly smaller than the
requested amount, returns
`"exceeds_host_capacity: <dim> requested <amount:g>, host capacity <limit:g>"`; else `None`.
Unbounded dimensions never trigger it. A constant `EXCEEDS_CAPACITY = "exceeds_host_capacity"`.

**D-2. W9 legacy loop.** In `main`'s P10B block, before `admission.admit`: if
`exceeds_capacity(resource_request, _host_capacity())` → `park_failed(task_id, reason)` and
journal `launch_parked_oversized`; the task is not added to `admission_waits`.

**D-3. W9 graph loop.** `_GraphHostAdmission.admit` checks `exceeds_capacity` on the host
dimensions first and returns `scheduling.Admission(False, reason=EXCEEDS_CAPACITY,
detail=...)`. `WorkflowRunner.advance`: an admit answer whose `reason == EXCEEDS_CAPACITY`
is **not** a wait — it goes through `_refuse(node, "<reason>: <detail>")` (claim + the one
`_fail_claim` path), so the node fails with the reason on the attempt's `route_refusal` and
its `on_failure` edge reaches `park`. Nothing is added to `report.waiting`.

**D-4. W10.** `launch_async_job(task, resources=None)` stores `"resources": dict(resources)`
on the `awaiting-verification` entry when given; `main` passes `resource_amounts`.
`_live_counted` uses the entry's recorded `resources` when the key is present (even `{}`), and
only falls back to re-deriving from the ROADMAP for an entry written before this change (no
key) — say so in the docstring.

**D-5. W11.** `status.list_runs(task_id, *, repo=None) -> list[dict]` — every `task_runs` row
for the task in `ORDER BY started_at, rowid` (the same order `_select_run` uses; reuse its
query, don't copy it) with `run_id, workflow_id, workflow_revision, status, started_at,
finished_at`; unknown task → `WorkflowViewError`. `status.render_runs_text(rows)`.
`maestro workflow runs <task_id> [--json]`: add `runs` to the verb choices and to
`cmd_workflow` (`--html`/`--run` are ignored for `runs`; keep `show` unchanged).

**D-6. Tests (§3 W9/W10/W11).**
- W9: legacy lane (see `tests/test_host_resource_admission.py` setup) with a task whose
  `ram_mb` exceeds a fixture `_host_capacity` → parked with a reason containing `ram_mb`, the
  amount and the capacity; graph lane with the same → node `failed`, `route_refusal` carries
  the same three facts, no `admission_waiting` entry for it. Unit test for `exceeds_capacity`.
- W10: in `tests/test_host_resource_admission.py`: an async job launched with a
  `resources:` block, then the ROADMAP block edited (and, second case, removed) → the ledger
  hold / `_live_counted` amounts are unchanged.
- W11: in `tests/test_workflow_status.py` and `tests/test_cli.py`: a task with two runs (a
  repaired one — seed via `apply_repair` or two `start_run`s) lists both in start order, text
  and `--json`.

---

## Every step ends with

- its focused tests green, plus `tests/workflows`, `tests/backends` and
  `tests/graph_engineering` green (junit root `testsuite` counts, files under `.scratch/s3/`);
- one commit on `feat/graph-engineering-foundation` (never `AGENTS.md` / `GEMINI.md` /
  `.scratch/`; message ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`);
- **no STATE.yaml edits** (the orchestrating session does those);
- a ≤25-line report: what was built, junit counts, any deviation from this file.

## Dan's decisions of 2026-09-16 (after step A) — binding

**D-FAIL-1. Failure handling is a NEW PHASE after P12A, not part of S3.** P12A closes as
planned (W5-W11 + close-out); the W4 "dead-writer candidate stalls the join" limit is
recorded on the W4 thread as a known limit that the new phase resolves. The new phase
covers crashing/failing LLM calls in **every mode and every graph run**, not just
candidates. Principle: **a failing model must not lower the quality of the work** — if
one model is blocked, use another, and so on.
- Paid mode: failures are essentially rate limits; handle them (fall back to another model).
- Free mode: more failure reasons. Handle the ones anticipable now (429/rate limit, quota
  exhausted, provider 5xx/outage, timeout, auth failure, context overflow, malformed tool
  calls, empty/garbage output); the unknown ones are discovered in Dan's free-model pilots.
- When no model of equal-or-better quality is available: **if the task can be checked
  easily with full coverage (tests / evals), step down a tier, test it properly, and accept
  only on success; otherwise (riskier) wait for the reset.**

**D-FAIL-2. The escalation ladder (replaces "max_plan_repairs defaults to 3").**
After **2 failed retries** of a node, escalate to a **more expensive model** as diagnoser to
decide: *bad graph* or *an agent in the graph producing poor-quality results*.
- bad graph -> replan the graph (plan repair) and launch **one final run**, then park;
- poor agent -> enhance that node's prompt/context and relaunch it **one last time**, then park.
Consequence for S3 step C (W7): `engineering.max_plan_repairs` defaults to **1** (the one
final run), and both outcomes are expressible as one `PlanRepairProposal` (a graph patch, or
`replace_nodes` of the failing node with an enhanced brief + re-run), so both count against
that single repair. The automatic trigger (2 retries -> stronger diagnoser) belongs to the
new failure-handling phase, not to W7; W7 only has to consume proposals and enforce the quota.
