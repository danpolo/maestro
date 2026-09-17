# P12B S3 — F7 (stronger diagnoser): build it, then F8 and the close-out

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-18-graph-p12b-s3-f7-build.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). No parallel session is active on this branch.

## Where the previous session stopped

Nothing was written. `b8ef073` is still HEAD, the tree is clean, and **F7 has not been
started**. That session spent its whole budget reading, and stopped at the 160K handoff
threshold before touching code rather than land F7 half-built.

**What it bought:** §"Code survey" below. Every seam F7 has to touch, located and quoted, at
`b8ef073`. Do not re-read the files listed there to re-derive those facts — go straight to
writing code against them. Re-read a specific function only when you are about to edit it.

## Read first (binding, in this order)

1. `handoffs/2026-09-17-graph-p12b-s3-design.md` §F7 (lines 184–260) — the specification.
   **Binding.** §F8 (264–289) and §"Close-out after F8" (322–325) follow it. §"Invariants"
   (293–308) applies to all three.
2. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` — §2's F7 bullet
   (line 104–114), §3's F7 acceptance bullets (141–147), §4 invariants (158–165).
3. The §"Code survey" and §"Test harness facts" below, in place of re-reading the source.

Do **not** re-derive the design. §F7 of the contract is the specification; implement it.
`handoffs/2026-09-18-graph-p12b-s3-f7-diagnoser.md` is this file's predecessor — same three
jobs, no code survey. Prefer this file; they do not conflict.

## Job 1 — F7, from §F7 of the design contract

All of it, in one commit: the `diagnose` stage and `escalation=True` compile,
`BASELINE_COMPILER_VERSION` / `p12b.1`, the settle deferral, the `failure_context` artifact,
`strength_floor` + `billing_only`, the repair-budget refusal, the verdicts, the apply-refusal
override, the final run, and workflow-scoped step-down history. Seven lane tests plus the
units listed there. **Commit F7 alone** when green.

## Job 2 — F8, from §F8 of the design contract

Only after F7 is green and committed. §F8 is small and unchanged: `_decide_dead_writers(at)`
in `tick` right after `reconcile`, under routing only, reading durable state rather than the
reconcile report; candidates go to `failed` + `record_failure` with no `outcome_ref`, other
writers take F6's path. Three lane tests. **Commit F8 alone.**

## Job 3 — the close-out, from §"Close-out after F8"

Close the three `_p12b_open_threads` (disposition `closed` + a note naming the commit), check
`owned_threads(state, "P12B") == 0`, record P12B in STATE `completed_phases` / `phase_records`,
reset `in_progress_details` to null per its comment, and move `current_phase` on to P12 —
`scripts/next_graph_prompt.py` should then print P12.

---

## Code survey (verified at `b8ef073`, 2026-09-18)

Line numbers are from `b8ef073`; they move as you edit. Everything here was read, not recalled.

### What F6 already built, that F7 builds on

- `maestro/workflows/failures.py`: `QUALITY_ATTEMPTS = 3` (:~70), `ROLE_DIAGNOSER =
  "diagnoser"` (:75), `retry_cone(revision, node_id)` (:78). A diagnoser's cone is `()`
  (:99), so **the diagnoser is already never retried** — F7 adds nothing there.
- `maestro/control/lifecycle.py`: `override_phase` (:309) already permits `succeeded →
  failed` (the check is at :332), which is exactly F7's apply-refusal move; `requeue_cone`
  (:361); `fail_quality` (:405); `node_counters` (:584, returns `retry_resets`);
  `stepped_down_attempts(run_id, node_id=None)` (:607) — **run-scoped, and F7 widens it**;
  `record_failure` (:528) takes `conn=`; `_transaction(conn)` (:616) joins a caller's txn.
- `maestro/workflows/runner.py`: `_commit_quality_failure` (:873) is where the third
  `bad_output` becomes `fail_quality`, which opens the node's `on_failure` edge — F7 points
  that edge at `diagnose` instead of `park`. `pre_validate` (:1168) keys the budget off
  `retry_resets` (:1180–1186), which `apply_repair` deliberately does **not** carry, so F7's
  final run dispatches once.
- `maestro/control/store.py` `_ADDED_COLUMNS` (~:407): `node_states.retry_resets`,
  `attempts.start_sha` are already there. F7 needs no new column.

### Compiler (`maestro/workflows/compiler.py`, 1412 lines)

- `COMPILER_VERSION = "p7.1"` at **:67**, stamped at **:810** inside `compile_workflow`.
  Add `BASELINE_COMPILER_VERSION = "p7.1"` and `COMPILER_VERSION = "p12b.1"`, and stamp
  `COMPILER_VERSION if escalation else BASELINE_COMPILER_VERSION`.
  **Consumers to check after the rename:** `maestro/workflows/__init__.py:15,78` re-exports
  `COMPILER_VERSION`; `tests/workflows/test_compiler.py:74` asserts `p7.1`;
  `tests/test_workflow_status.py:72` hardcodes `"p7.1"` in a fixture. Those two tests pass
  unescalated compiles, so they should keep passing unchanged — confirm, don't assume.
- Node constructors are one function each, `:87–331`. `_planner_node` (**:260**) is the
  closest shape to `_diagnose_node` — copy it: same `permissions_ref="frozen_snapshot_reader"`,
  same `resource_request={"writer_lease": False, "inference_slots": 1}`, same `params`.
  Differences per the contract: `agent_definition_ref="diagnoser"`, `cache_policy="never"`
  (planner has `"recheck_required"`), `output_schema_ref="PlanRepairProposal.v1"`.
- `FRAGMENTS` table at **:363**; add `_fragment("diagnose", "1.0.0", _diagnose_node)`.
  `resolve_fragment` (:394) refuses an unknown name or stale version.
- `BASELINE_TEMPLATES` at **:423**: ordinary (:424), script (:442), resumable (:455 — this
  is the `prep` lane), async (:468), and the one whose gate fails to `cooldown` (:481).
  Note `_stage(...)` (:415) takes `on_failure=` as a **node id string**.
- `compose` at **:586**. The `on_failure` edges are built at **:654–661**:
  `if group.stage.on_failure in live_sinks and not reaped:` → `EdgeSpec(source,
  group.stage.on_failure, guard_predicate="on_failure", required=False)`. `_Group.reaped_by_join`
  (:575) is the widened-candidate exception that gets no failure edge. Sinks are filtered to
  the ones actually used at **:617–618**.
  **F7's re-targeting runs after `compose` returns**, in `compile_workflow`, per the contract:
  every `on_failure` edge whose target is `park` and whose source is an agent node or a
  verification node (`handler_ref == "maestro.handlers.verification"`) is re-pointed at
  `diagnose`; `diagnose` gets its own `on_failure → park` (`required=False`); the node is added
  only if at least one edge was re-targeted. `cooldown` targets and non-agent sources keep `park`.
- `compile_workflow` at **:754**; `compose` is called at :801, `check_criteria` at :804, the
  `WorkflowRevision` built at :805. Add `escalation: bool = False` to the signature.
- `PlanRepairProposal` at **:945**: frozen dataclass, `__post_init__` :967 (this is where an
  unknown `verdict` must raise `PlanRepairRejected`), `to_dict` :980 (emit `verdict` **only
  when set**, so the W7 wire form is unchanged), `from_dict` :999. Add
  `verdict: str | None = None` and `VERDICTS = {"bad_graph", "poor_agent"}`.
- `plan_repair` **:1072** refuses on `repairs_used >= max_repairs` at :1104. `repair_counters`
  **:1220**. `apply_repair` **:1251**; its `node_states` seed with the explicit column list is
  at **:1374–1386** — it carries `attempt_index`, `blocked_attempts`, `quality_failures`,
  `excluded_routes_json`, `first_blocked_at`, and **not** `retry_resets`, which is what makes
  F7's final run dispatch exactly once.
- The orchestrator's graph compile site is **`maestro/orchestrator.py:2184`** — the only
  caller that passes `escalation=True`.

### Runner (`maestro/workflows/runner.py`, 1649 lines)

- `Claim` dataclass **:262**; `selection_artifact_id` is at :280. Add the
  `failure_context_artifact_id` field beside it.
- `tick` **:382**: ingest (:388), reconcile (:396 — F8 hooks in right after), `apply_skips`
  (:401), `advance` per ready node (:404), `settle` last (:408).
- `advance` **:411** → `_advance_agent` **:488** for agent nodes under routing.
  `_advance_agent`'s shape: manifest (:496) → `node_counters` (:501) → `wf_routing.resolve`
  (:503, **the one call site**) → not-bound handling (:512–535, where a `block` goes to
  `_refuse` and a `wait` to `report.waiting`) → `claim` (:538) → manifest artifact (:546–556)
  → `dataclasses.replace(claim, binding=…, plan=…)` (:558) → `session_decision` (:564) →
  `_restore_start_tree` (:568) → `_record_routing_decision` (:573) → `dispatch` (:574).
  F7's diagnoser branch (the `failure_context` artifact, `strength_floor`, `billing_only`, the
  repair-budget refusal) all attach inside this one method — **not** a second path.
- `_select_affected_checks` **:442** is the pattern to copy for the diagnoser's inputs: build
  the document, `self.artifacts.put_bytes(..., type="affected_selection", producer_attempt_id=
  claim.attempt_id, at=at)` (:478), stash it in `self._attempt_inputs[node.node_id]` (:485),
  and `dataclasses.replace(claim, selection_artifact_id=…)` (:486). Its forced-full-suite read
  of `stepped_down_attempts(self.run_id)` is at **:465** — this is one of the two call sites
  F7 widens to workflow scope; the other is `_advance_agent:507`.
- `_soft_exclude` **:577** — F7 must **not** apply it to a diagnoser.
- `_refuse` **:711** claims then `_fail_claim`s; `_fail_claim` **:1433** records a
  `route_refusal` column and a `ROUTE_REFUSED_EVENT` (:1455–1466). This is the path a
  diagnoser's `block` and its `repair_budget_exhausted` refusal both take.
- `_commit_result` **:782**. Order matters and is load-bearing: evidence (:797), shadow
  comparison (:798), outcome artifact (:799), **the W7 proposal decided at :802–808** (this is
  where the verdict checks go — a refusal sets `phase = "failed"` before anything is
  committed), classification (:810–817), then the blocked / cone / ordinary three-way at
  :823–847, telemetry and `record_failure` at :849–860, lease release :862, and **`_apply_repair`
  last, at :870–871** — after the phase was already committed, which is precisely why the
  apply-refusal override is needed.
- `_plan_repair_proposal` **:967** (gated on `PLAN_REPAIR_ROLES` at :976, `{"planner",
  "diagnoser"}` at **:96**); `_plan_repair` **:986**; `_apply_repair` **:1006** — its
  docstring at :1009–1013 states the known limit F7 closes, and must be updated;
  `_record_repair_refused` **:1027**.
- `settle` **:1470**: reads `task_runs.status`, then `run_outcome(self.revision, self.phases())`
  (:1478). The deferral goes here — under routing, do not finish while any node is
  `claimed`/`running` or in `ready_frontier`. `routing is None` must stay byte-identical.
- `node_inputs` **:1334** merges `self._attempt_inputs` (:1347) — the `failure_context` reaches
  an in-process node through it, exactly as `affected_selection` does.
- `_default_worker_argv` **:1528**; `--affected-selection-artifact-id` is appended at :1559.
  Add `--failure-context-artifact-id` beside it.
- `candidate_documents` **:1352** — F8's candidates rely on "no `outcome_ref` → no document"
  (the `WHERE ... outcome_ref IS NOT NULL` at :1376).
- `launch_worker` **:1411** is the single choke point. Nothing F7 adds may bypass it.

### Worker (`maestro/workflows/worker.py`, 289 lines)

`--affected-selection-artifact-id` is declared at **:59**, read at **:231–235** into
`context.inputs` via `_read_manifest` (**:77**, a stored JSON artifact reader). Add
`--failure-context-artifact-id` the same way, in both places.

### Routing / router

- `maestro/workflows/routing.py` `resolve` **:210**. Its inner `attempt(**overrides)` helper
  (:231) forwards to `router.resolve_agent`; the F5 step-down calls `attempt(strength_offset=-1,
  billing=billing)` at :261; F6's soft-exclude two-pass is :239–251. F7 adds `strength_floor=`
  and `billing_only=` parameters that forward to `resolve_agent(min_strength_floor=, billing=)`,
  **inside this same call** — still one resolve per attempt. `router.OBJECTIVE_BILLING` is
  already imported/used at :257–259; reuse that for `billing_only` (no hybrid grant).
- `maestro/backends/router.py` `resolve_agent` **:616**. `strength_offset` is applied at
  **:683–689** (`rank = min(max(demand.strength_rank + strength_offset, 0),
  len(MODEL_STRENGTHS) - 1)`, clamped both ways); `billing` is resolved at :690–698, where a
  passed-in set is **intersected** with the role's declared categories. `min_strength_floor=`
  goes right beside the offset block: raise `demand.min_strength` to at least that rung, never
  lower it, top stays top.
- The rung of the failed attempt's model: `catalog.entry(backend_id).profile(model_id).strength`
  (`catalog.py` `entry` :509, `profile` :419, `ModelProfile.strength_rank` :200) against
  `catalog.MODEL_STRENGTHS = ("light", "balanced", "strong")` (**:115**). `profile()` defaults
  an ungraded-but-listed model to the middle rung rather than raising.

### The diagnoser role already exists — do not create it

`maestro/templates/roles/diagnoser.yaml` (loaded into `policy.agent_definitions`),
`maestro/workflows/policy.py:801` `ROLE_FOR_FRAGMENT["diagnoser"] = "diagnoser"`,
`maestro/repository/context.py:103` a `diagnoser` `RoleProfile` (receives `prior_failures`,
`test_results`, `diff`, `source`, `related_tests`), and
`maestro/templates/workflows/project.yaml:216` `diagnoser: frozen_snapshot_reader`.

**A trap worth knowing before you write test 1:** the shipped diagnoser role declares
`permitted_backend_capabilities.min_reasoning_strength: strong`, so its demand floor is
*already* `strong` with no help from F7. A lane that just asserts "the diagnoser bound a
strong model" would pass with `strength_floor` deleted. Lower the role's floor in that test's
policy (the `_strong_floor` helper at `test_failure_ladder.py:606` shows how to rewrite one
role's `permitted_backend_capabilities`) so only F7's floor can raise it — otherwise the test
is vacuous. `frozen_snapshot_reader` is **not** in `CONTROLLER_PERMISSIONS`
(`runner.py:82` — `{"controller_only", "controller_target_branch"}`), so `diagnose` really
launches a worker, like the planner.

## Test harness facts, learned the hard way — read before writing a lane test

These cost three sessions between them. `tests/graph_engineering/test_failure_ladder.py`
carries helpers for all of it at the end of the file; reuse them rather than rebuilding.

- **`MAX_POLLS` is 12** (`tests/graph_engineering/conftest.py:52`). A ladder lane burns polls
  fast. F7's lanes are longer than F6's — three bad outputs, then `diagnose`, then possibly a
  repaired run — so budget the polls before writing the lane, and halt on a **durable**
  condition (an attempt count, a row that exists), never on "the node reached a terminal
  phase": F6 resets a failed writer's cone to `pending`, and a lane watching the phase spins
  to the limit.
- **`_ScriptedCliDriver(*answers)`** (**:931**) — a fake `agent_cli` driver with one
  `(Completion, ExitVerdict)` per call, the last repeating. This is how one node answers badly
  twice and then well. `_EMPTY` (**:923**) classifies `empty_output` (needs `--worktree`
  pointing at a real git repo — use `_committed_repo(tmp_path / "wt")`, **:212**); `_GOOD`
  (**:924**) plus a `DONE` sentinel in the attempt workspace is how an adopted agent answer
  becomes `succeeded` rather than `uncertain`.
- **`_drive_implementer(predicate, *, worktree=, succeed_from=)`** (**:975**) — runs the newest
  live implementer worker each poll until `predicate()` holds. For a diagnoser lane you will
  want a sibling that drives whichever node is live, or `_run_the_gate_worker` (in
  `test_dispatch_wiring.py`), which runs the gate for real and succeeds everything else.
- **`_halt_when(predicate, each)`** (**:964**), `_retry_rows()` (**:955**),
  `_settled_implementer()` (**:961**), `_counters(node_id="implementer")` (**:392**, selects
  `retry_resets`), `_attempts_by_node` (**:855**), `_two_bad_then_good` (**:999** — the
  canonical "two bad answers then a good one" lane), `_patch_two_drivers` (**:335**),
  `_two_route_routing` (**:328**, `ROUTE_A`/`ROUTE_B` at :324–325), `_strong_and_balanced`
  (**:617**, one `strong` route + one `balanced` route), `_strong_floor` (**:606**),
  `_seed_strong_pause` (**:640**), `_high_risk` (**:671**), `resolve_spy` / `ladder_spies`
  (note: `ladder_spies["resolve"]` counts **`router.resolve_agent`**, which fires twice in one
  `routing.resolve` when a soft-exclude pass misses — use `resolve_spy["routing"]` for "once
  per attempt").
- `fixture_routing(...)` and `fixture_cli_entry(...)` are in `conftest.py` (**:334**, **:313**);
  `fixture_cli_entry` takes `model_profiles=` for grading a route's strength.
- **Prove the tests aren't vacuous.** F6's lane tests were checked by mutating
  `QUALITY_ATTEMPTS` to 1 and confirming all nine failed. Do the equivalent for F7 (compile
  with `escalation=False` and confirm the escalation lanes fail; delete `strength_floor` and
  confirm test 1 fails) and say so in the commit message.
- `worktree_for` is overridable per test by monkeypatching `wf_runner.WorkflowRunner.worktree_for`;
  `_restore_start_tree` only acts when it resolves to the node's own
  `CommitBoundary.for_task(repo, task_id, candidate=...)` worktree under
  `.orchestrator/worktrees/`.

## Invariants (binding)

One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`) and one
fresh-vs-resume decision, with no Context Gate. New behaviour only when `routing is not None`.
`tests/workflows/test_compatibility.py` stays unchanged and an unescalated compile stays
byte-identical. Don't edit `NODE_TRANSITIONS`. The diagnoser never uses the hybrid overlay
(Dan, 2026-09-17). Leave `P12A_*`, `AGENTS.md` and `GEMINI.md` alone. Scratch files go in
`.scratch/`. A PreToolUse hook rejects Bash text containing the privileged word. pytest's
summary line is suppressed, so read the counts off the junit root `testsuite`. Commits end
with `Co-Authored-By: <model actually running> <noreply@anthropic.com>`. STATE: targeted
replacements only. If a step can only be built by bypassing the choke point or adding a
second resume decision, stop and ask Dan (spec §4).

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py tests/control tests/test_confinement.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; run in background, check xml mtime
```

Baseline at `b8ef073`: **gate tests=1207 failures=0 skipped=0; full tests=4697 failures=0
skipped=1.** Report per commit: hash, gate and full junit counts.

## Budget

F7 is the largest of the three Fs, and the survey above is what a session normally spends
~70K of context earning. Starting cold, F7 alone is a full session. Measure with
`python3 ~/.claude/skills/close-session/scripts/context_usage.py` after each commit; for
Opus 5, write a handoff at 160K and start fresh by 200K. One F per session is fine — if F7
lands and the budget is spent, hand off F8 + the close-out rather than starting them.
