# P12B S3 — design contract (F6, F7, F8: the bad-output branch and dead writers)

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s3-design.md` (or a build handoff that points
here). No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). S1 (F0–F3) and S2 (F4 `1e78300`, F5 `929248a`)
are committed — see `git log -6 --oneline`.

Binding sources, read in this order before coding:
1. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` — §1 ladder, F6/F7/F8
   in §2 (lines ~97–119), their §3 acceptance bullets (~136–150), §4 invariants (git-ignored,
   on disk only).
2. `handoffs/2026-09-17-graph-p12b-implementation.md` — Dan's decisions and invariants.
3. `handoffs/2026-09-17-graph-p12b-s2-design.md` — F4/F5 as designed; §"What S2 built" of
   `handoffs/2026-09-17-graph-p12b-s3-contract.md` — F4/F5 as built (helpers, lane facts).
4. Memories: `llm-failure-handling-policy`, `context-management-not-built-independent`,
   `harness-auto-updates-must-not-break-gates`, `gaps-are-not-decisions`.
5. STATE `_p12b_open_threads` (`grep -n "_p12b_open_threads" docs/graph-engineering/STATE.yaml`,
   ~:539): all three threads are closed by S3 (F8 → #1, F7 → #2, F6+F7 → #3).

S3 builds `bad_output` → 2 retries (F6), the stronger diagnoser and the final run (F7), and
dead writers (F8). **Order: F6, then F7, then F8; one commit each.** F8 reuses F6's retry path.
With `routing is None` every behaviour stays byte-identical.

---

## Code facts this contract rests on (verified 2026-09-17 at `929248a`; re-check line numbers)

- `runner._commit_result` (`runner.py:~698`): classifies under routing when `phase == "failed"`
  (`~:726`), takes `_requeue_blocked` for `blocked` agent failures (`~:735`), otherwise
  `finish_attempt` + `set_node_phase(running?, phase)` as **separate** transactions, then
  `record_failure` (`~:765`). A W7 proposal is decided before (`~:718`) and applied after
  (`~:777`) the phase commit.
- `runner.pre_validate` (`~:1032`): under routing, agent nodes check
  `retry_policy.permits(node_counters.quality_failures)`; every compiled node has
  `RetryPolicy(max_attempts=1)` (`compiler.py:~95…`). `RetryPolicy.permits(i) = i < max`
  (`workflows/models.py:~99`). **Nothing increments `quality_failures` yet.**
- `NODE_TRANSITIONS` (`control/models.py:~116`): `succeeded` is terminal, `failed → ready`
  only, `running → succeeded|failed|uncertain`. `Lifecycle.set_node_phase` (`lifecycle.py:~275`)
  enforces it and accepts `conn=`. `apply()`/`replay()` (`~:79`) fold `NODE_PHASE_CHANGED`
  **without** checking transitions, so an event-recorded forced move replays exactly.
- `Lifecycle.requeue_blocked` (`~:409`) is the one-transaction pattern to copy;
  `node_counters` (`~:455`); `stepped_down_attempts(run_id, node_id=None)` (`~:475`) is
  **run-scoped**; `_set_attempt_fields` allow-list (`~:488`).
- `store._ADDED_COLUMNS` (`store.py:~407`) — add columns there. `compiler.apply_repair`
  (`compiler.py:~1364`) seeds the new run's `node_states` with an **explicit column list**
  (carries `attempt_index`, `blocked_attempts`, `quality_failures`, `excluded_routes_json`,
  `first_blocked_at`); a new column not in that list starts at its default in the new run.
- `propagate_skips` / `node_is_skipped` / `run_outcome` (`runner.py:~141–231`): a failed
  implementer skips gate → review → merge → accept, so `run_outcome` returns `failed` in the
  same tick. `tick` calls `settle()` last (`~:407`); the orchestrator drops a runner whose run
  is not `running` (`orchestrator.py:~2235`). **So today a worker-launched node on a failure
  branch would never be ingested** — F7 must defer settlement (below).
- `routing.session_decision` (`routing.py:~271`) is always `fresh` today.
- `routing.resolve` (`routing.py:~210`): one call from `_advance_agent` (`runner.py:~502`);
  the F5 step-down re-resolve lives inside it. `router.resolve_agent(..., strength_offset=)`
  (`router.py:~627`, clamp `~:684`) already clamps **both** ways (`min(max(rank+offset,0),top)`).
  `AgentBinding.selection_reason` (`router.py:~152`, built `~:792`).
- `compiler.compose` (`compiler.py:~586`) wires `on_failure` edges to the stage's sink
  (`required=False`); widened candidate groups (`_Group.reaped_by_join`) get none — the join's
  own `on_failure → park` covers them. `BASELINE_TEMPLATES` (`~:423`): ordinary / script /
  manual / async / resumable (resumable's gate fails to `cooldown`). `COMPILER_VERSION =
  "p7.1"` (`~:67`), stamped into every revision (`~:810`). Graph-loop compile site:
  `orchestrator.py:~2184`.
- `PlanRepairProposal` (`compiler.py:~946`) has no `verdict`; `to_dict` (`~:980`) is the W7 wire
  form. `plan_repair` (`~:1072`) refuses when `repairs_used >= max_repairs`; invalidated =
  changed nodes + descendants (the on_failure edge makes `diagnose` a descendant of every
  agent). `runner._plan_repair` passes `self.phases() | {node: "succeeded"}` (`runner.py:~856`).
  `runner.PLAN_REPAIR_ROLES = {"planner", "diagnoser"}` (`~:95`); `roles.ROLE_DIAGNOSER`,
  `templates/roles/diagnoser.yaml`, `repository/context.py:~103` (a `diagnoser` RoleProfile)
  and `templates/workflows/project.yaml:~216` (permission `frozen_snapshot_reader`) exist.
- `Reconciler._adjudicate_dead_writer` (`control/reconcile.py:~195`) finishes a dead writer's
  attempt `failed` (repeatable) or `uncertain` (not) and releases the lease; it **never moves
  the node**, which stays `claimed`/`running`. Only lease-holding (writer) nodes are scanned.
- `runner.candidate_documents` (`runner.py:~1209`) gives a candidate a document only when its
  latest attempt has an `outcome_ref`; no document → the join reports it `abandoned`.
- `runner._select_affected_checks` (`~:441`) forces the full suite when
  `stepped_down_attempts(self.run_id)` is non-zero; it stores the selection as an artifact and
  the worker reads it via `--affected-selection-artifact-id` (`_default_worker_argv ~:1385`,
  `worker.run ~:230`) — the pattern F7 copies for the diagnoser's inputs.
- `confinement.CommitBoundary` (`confinement.py:~416`) has no restore today;
  `worktree_for(repo, task, candidate=)` (`~:404`) = `<repo>/.orchestrator/worktrees/<task>[/<c>]`.
  `runner.worktree_for(node)` (`~:1186`) returns `params["worktree"]` or `None` (overridable).
  `worker._worktree_state(cwd)` (`worker.py:~158`) reads `(HEAD, porcelain)`.

---

## F6 — `bad_output` → two retries

### Pure (`workflows/failures.py`)
- `QUALITY_ATTEMPTS = 3` (the first run plus 2 retries).
- `retry_cone(revision, node_id) -> tuple[str, ...]` (topological order):
  - an agent node whose role is **not** `diagnoser` → `(node_id,)`;
  - a `maestro.handlers.verification` node → its nearest upstream agent writer
    (`kind == "agent"` and `wants_writer_lease`), found by walking incoming non-`on_failure`
    edges backwards (shortest distance; ties → first in topological order), plus every node on
    a path between them, gate included. No writer upstream → `()`;
  - anything else (script, merge, join, human, diagnoser) → `()` — today's `failed` path.
- The cone's **root** is its first element (the writer). The root owns the counters.

### Store / lifecycle
- `node_states.retry_resets INTEGER NOT NULL DEFAULT 0` (`_ADDED_COLUMNS`). **Run-scoped on
  purpose:** not added to `apply_repair`'s carry list.
- `attempts.start_sha TEXT` (nullable; add to `_set_attempt_fields`).
- `Lifecycle.override_phase(run_id, node_id, phase, *, reason, attempt_id=None, at, conn=None)`:
  the one sanctioned bypass of `check_node_transition`, with a closed set — `* → pending`
  (F6 cone reset) and `succeeded → failed` (F7, a diagnoser whose repair is refused). Anything
  else raises `IllegalTransition`. Appends `NODE_PHASE_CHANGED` with `"override": reason` in
  the payload (replay folds it unchanged). `NODE_TRANSITIONS` is **not** edited.
- `Lifecycle.requeue_cone(attempt_id, *, run_id, root, cone, record, outcome_ref, at)` — one
  `store.transaction()`: `finish_attempt(failed)`, `record_failure`, root
  `quality_failures += 1`, every cone node `retry_resets += 1` and `override_phase(→ pending)`,
  plus every `skipped` descendant of the cone → `pending`.
- `Lifecycle.fail_quality(attempt_id, *, run_id, node_id, root, record, outcome_ref, at)` — the
  exhausted path in one transaction: today's finish/`running?`/`failed` moves, `record_failure`,
  root `quality_failures += 1` (so a 3rd failure stores 3, which F7's final run inherits).

### Ingest (`runner._commit_result`)
- Under routing, `phase == "failed"`, `record` is `bad_output`, `cone = retry_cone(...)` non-empty:
  `q = node_counters(root).quality_failures + 1`; `q < QUALITY_ATTEMPTS` → `requeue_cone`;
  otherwise → `fail_quality` (the node's `on_failure` edge opens: `park` in F6, `diagnose` in
  F7). Both replace the separate `finish_attempt`/`set_node_phase`/`record_failure` calls.
  Empty cone → today's path unchanged. Lease release, settle telemetry and
  `on_attempt_finished` still follow. This closes the carry-over: `quality_failures` now
  increments on every `bad_output`, inside the same transaction as the requeue.
- A stepped-down attempt's failure goes through this path like any other (F5); its retries do
  not step down again (`stepped_down_before`, widened in F7 below).

### Dispatch
- `pre_validate` under routing: for agent nodes **and** any node with `retry_resets > 0`,
  check `RetryPolicy(max_attempts=QUALITY_ATTEMPTS).permits(retry_resets)` instead of F4's
  `permits(quality_failures)` (cumulative `quality_failures` would refuse F7's final run).
  Blocked requeues never touch `retry_resets`, so a blocked attempt still spends nothing.
  Other nodes keep `permits(attempt_index)`.
- **Retry 1** (root `quality_failures == 1`): nothing new — `session_decision` is already
  `fresh`; state that in its docstring (a retry after `bad_output` is fresh) and assert
  `session_mode == "fresh"`.
- **Retry 2** (root `quality_failures == 2`, dispatching the root): `_advance_agent` passes
  `soft_exclude={(backend_id, model_id) of the root's latest bound attempt}` to
  `routing.resolve(..., soft_exclude=frozenset())`. Inside `resolve` (still one call site):
  first `attempt(exclude=exclude | soft_exclude)`; if that is not bound, `attempt(exclude=
  exclude)` and a bound result gets `selection_reason += "; retry 2: no other equal-or-better
  route, same model reused"`. The existing wait/step-down logic then applies to whichever
  result stands. `soft_exclude` is **not** persisted to `excluded_routes_json` (it must not
  follow the node into the final run).
- **Start SHA:** in `_advance_agent`, for a node with `wants_writer_lease` and
  `tree = self.worktree_for(node)` that is a git worktree, write `start_sha = HEAD` in
  `_record_routing_decision`. Before recording, if `retry_resets > 0` and the node's previous
  attempt has a `start_sha`, restore it: new `CommitBoundary.restore(sha, *, runner=subprocess.run)`
  → `git -C <worktree> reset --hard <sha>` then `git -C <worktree> clean -fdq`; it refuses
  (raises `ConfinementError`/`BranchNameError`-style) unless the target is the boundary's own
  worktree under `.orchestrator/worktrees/` and not the repo root. The boundary is
  `CommitBoundary.for_task(repo, task_id, candidate=params.get("candidate_id", ""))`, used only
  when its `worktree` resolves to `tree`; otherwise no restore and `start_sha` stays NULL.
  Restoring at claim time (not at ingest) is what makes it restart-safe: the restore happens
  exactly once per launched retry. A restore failure `_fail_claim`s the node with
  `retry_restore_failed: …`.

### F6 tests (`test_failure_ladder.py`, under `run_lane`)
1. Implementer fixture: attempts 1 and 2 end `bad_output` (e.g. `empty_output` via the fake
   driver), attempt 3 succeeds → the run proceeds to the gate; `quality_failures == 2`;
   attempt 2's `(backend_id, model_id)` equals attempt 1's and `session_mode == "fresh"`.
2. Two equal routes: attempt 3's model differs from attempt 1's. One route only: attempt 3
   equals attempt 1's and its `binding_json.selection_reason` names the reuse.
3. A failing gate (`cmd: "false"`) → the next polls re-run the implementer **and** the gate
   (two new attempts each, the gate not alone); `quality_failures` counts on the implementer.
4. Three `bad_output` → implementer `failed`, `park` reached, `quality_failures == 3`.
5. Start SHA: a real temp git worktree wired via `worktree_for` override; attempt 1 commits
   junk; the retry's claim restores `start_sha` (HEAD equals it, junk gone), and the restore
   never touches the repo root.
6. A blocked failure between two bad outputs spends no retry (`retry_resets` unchanged).
7. Spies: `launch_worker` once per launch, `routing.resolve` once per agent attempt.
8. routing-None: a bad output is written `failed` exactly as today.
Units: `retry_cone` shapes (agent, gate→writer with a node between, gate with no writer,
diagnoser, script); `override_phase` allowed/refused moves and replay equality;
`CommitBoundary.restore` refusals.

**Commit F6 alone** when green.

---

## F7 — escalation to a stronger diagnoser

### Compiler (`compiler.py`)
- `_diagnose_node(spec, *, node_id="diagnose")`: `kind="agent"`, handler `maestro.handlers.agent`,
  `agent_definition_ref="diagnoser"`, `permissions_ref="frozen_snapshot_reader"`, no writer
  lease, `inference_slots: 1`, `effect_class="pure_derivation"`, `cache_policy="never"`,
  `output_schema_ref="PlanRepairProposal.v1"`, `params={"task_id": …, "title": …}`;
  fragment `diagnose@1.0.0`.
- `compile_workflow(..., escalation=False)`. With `True`, after `compose`: every `on_failure`
  edge whose target is `park` and whose source is an agent node or a verification node is
  re-targeted to `diagnose`; `diagnose` gets `on_failure → park` (`required=False`); the node
  is added only if at least one edge was re-targeted. Other sinks (`cooldown`) and non-agent
  sources (script, async_launch, candidate join) keep `park`.
- **Version:** an escalated revision is stamped `COMPILER_VERSION = "p12b.1"`; an unescalated
  one keeps `"p7.1"` (new `BASELINE_COMPILER_VERSION`). This is how "COMPILER_VERSION moves"
  and "without escalation, byte-identical" both hold. `test_compatibility.py` unchanged.
- The orchestrator's graph compile (`~:2184`) passes `escalation=True`; nothing else does.
- `PlanRepairProposal.verdict: str | None = None`, `VERDICTS = {"bad_graph", "poor_agent"}`;
  unknown → `PlanRepairRejected`; `to_dict` emits it **only when set** (W7 wire form unchanged);
  `from_dict` reads it.

### Runner — inputs, routing, verdicts
- **Settle deferral:** under routing, `settle()` does not finish the run while any node is
  `claimed`/`running` or in `ready_frontier` (so `diagnose`/`park` get dispatched and ingested
  before the run settles). routing-None unchanged.
- **Inputs:** when advancing a `diagnoser` node, build `failure_context` = the failing node
  (the source of `diagnose`'s open `on_failure` edge; with several, the first in topological
  order): its `NodeSpec.to_dict()`, its latest manifest document (the brief), every attempt's
  `binding_json` + `failure_json`, and the latest verification attempt's evidence rows.
  Store it as a `failure_context` artifact; the worker gets `--failure-context-artifact-id`
  (like `--affected-selection-artifact-id`) and puts it in `context.inputs`.
- **Routing:** `routing.resolve(..., strength_floor=<rung above the failing attempt's model>,
  billing_only=True)` → `router.resolve_agent(..., min_strength_floor=)` raises the estimated
  `min_strength` to at least that rung (top stays top; the rung comes from the catalog profile
  of the failed attempt's `(backend_id, model_id)`), and `billing=OBJECTIVE_BILLING[objective]`
  (no hybrid grant — free stays free). No step-down and no `soft_exclude` for a diagnoser. A
  `block` → `_refuse` → `diagnose` failed → `park`, reason `route_…`. A `wait` (the stronger
  pool is rate-limited) waits like any node — a rate limit is not a quality verdict.
- **Budget:** a `diagnoser` node whose `repair_counters(...)` show `repairs_used >= max` is
  refused before routing with `repair_budget_exhausted: …` → failed → `park` (no launch).
- **Verdicts** (in `_commit_result`, diagnoser role only): no `plan_repair` → phase `failed`,
  `WorkflowRepairRefused` "diagnoser returned no proposal". A proposal with no `verdict`, a
  `poor_agent` whose `replace_nodes` does not include the failing node, or a `bad_graph` with no
  graph patch (no add/remove/replace nodes or edges) → refused the same way. The diagnoser is
  never retried (`retry_cone` → `()`).
- **Apply refusal** (closes thread #2): when `apply_repair` refuses a **diagnoser's** plan,
  `override_phase(diagnose, succeeded → failed, reason=<refusal>)` + `record_failure`
  (`garbage_output`, evidence = the refusal) so its `on_failure → park` opens. Planner
  behaviour (W7) unchanged; update the `_apply_repair` known-limit docstring.
- **Final run:** `apply_repair` already carries `quality_failures` (3 for the failing node) and
  not `retry_resets`, so the re-run node dispatches once (`pre_validate` permits 0) and its next
  `bad_output` goes to `fail_quality` → `diagnose` → `repair_budget_exhausted` → `park`.
  F4/F5 still apply in the final run.
- **Step-down history (carry-over decision):** carried. `stepped_down_before` and the forced
  full suite read **workflow-scoped** history: `Lifecycle.stepped_down_attempts(run_id,
  node_id=None, *, workflow_id=None)` counts attempts of every run of that workflow when
  `workflow_id` is given; `_advance_agent` and `_select_affected_checks` pass it. So "nothing
  ever steps down twice" holds across a repair, and a repaired run whose history stepped down
  is still checked by the full suite.

### F7 tests (under `run_lane`, escalated compile)
1. After the 3rd `bad_output`, the next poll dispatches `diagnose` on a route one rung stronger
   than the failed model (balanced implementer → strong diagnoser).
2. `free_quality` with no stronger free route and a hybrid overlay granting paid: `diagnose`
   fails with a `route_` reason, the task parks, no paid route bound.
3. `poor_agent` proposal (fixture diagnoser output): revision N+1's implementer carries the
   enhanced `params`; its next `bad_output` → `park` with no retry and no second `diagnose`
   launch (spy).
4. `bad_graph` proposal: same through a graph patch (e.g. an added prerequisite node).
5. `diagnose` with no proposal → park; with a proposal `apply_repair` refuses (a live writer
   outside the cone, or a stale phase) → `diagnose` `failed`, park reached.
6. The run does not settle while `diagnose` is live.
7. `compile_workflow` without `escalation` is byte-identical (existing compatibility test)
   and still stamped `p7.1`; escalated shapes for ordinary/script/resumable lanes.
Units: `PlanRepairProposal` verdict round-trip and refusal; `resolve_agent(min_strength_floor=)`.

**Commit F7 alone** when green.

---

## F8 — dead writers reach a decision

- `WorkflowRunner._decide_dead_writers(at)`, called in `tick` right after `reconcile`, under
  routing only. It reads durable state, not the reconcile report (restart-safe):
  `node_states` of this run in `claimed`/`running` whose `last_attempt_id`'s attempt is
  `uncertain` or `failed` and has no receipt on disk. For each, `record =
  FailureRecord(kind="worker_crashed", evidence=<reconcile detail or "writer died with no
  receipt">)`:
  - **candidate** (`params.candidate_id` set and the node has no `on_failure` edge, i.e. reaped
    by the join) → `set_node_phase(→ failed)` (legal from `claimed`/`running`) +
    `record_failure`, in one transaction. No `outcome_ref`, so `candidate_documents` gives it
    no document and the join reports it `abandoned`. The attempt keeps `uncertain` (A05's
    record stays truthful).
  - **non-candidate writer** → the F6 path (`requeue_cone` or `fail_quality`) with that record;
    the retry restores `start_sha` at claim (F6).
- `reconcile.py` is not changed; routing-None runs never call the new method.

### F8 tests (under `run_lane`)
1. 3-candidate run; c3's writer "killed" (fixture `is_alive` false for its PID, no receipt) →
   `candidate_select` runs, exactly one winner, c3 reported `abandoned`; no failed launch
   stands in for the kill (reuse the P12A W4 candidate lane test's harness).
2. Killed non-candidate implementer → retried once from `start_sha`
   (`quality_failures == 1`, `failure_kind == "worker_crashed"`).
3. routing-None: the same kill leaves the attempt `uncertain` and the node `running`.

**Commit F8 alone** when green. Then close-out (below).

---

## Invariants (binding)
- One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`); one
  fresh-vs-resume decision (`routing.session_decision`); every retry, final run and diagnoser
  goes through it. No Context Gate, no context rotation.
- New behaviour only when `routing is not None`. `tests/workflows/test_compatibility.py`
  unchanged; unescalated compiles byte-identical. Failure columns never change routing weights
  (D06). The diagnoser never uses the hybrid overlay (Dan, 2026-09-17).
- Keep `Usage.pools` out of `to_usage_json`; keep agy `Capabilities.sandbox=False` and its
  `--add-dir`/`--dangerously-skip-permissions` flags. Don't flip `engineering.runner`; don't
  touch `P12A_*`, `AGENTS.md`, `GEMINI.md`. Scratch goes in `.scratch/`.
- A PreToolUse hook rejects Bash text containing the privileged word (s-u-d-o).
- pytest's summary line is suppressed: read counts off the junit root `testsuite`.
- Commits end with `Co-Authored-By: <model actually running> <noreply@anthropic.com>`.
  STATE: targeted replacements only.
- If a step can only be built by bypassing the choke point or adding a second resume
  decision, stop and ask Dan (spec §4).

## Out of scope
P12, the Context Gate, the legacy loop, new failure kinds (pilot evidence later), editing
`NODE_TRANSITIONS`, retrying non-agent nodes outside a gate's cone.

## Verification (report the numbers)
```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; >120s, run in background and check the xml mtime is fresh
```
Baseline (F5, `929248a`): gate tests=915 failures=0; full tests=4672 failures=0 skipped=1.
Report per commit: hash, gate and full junit counts.

**Close-out after F8:** close the three `_p12b_open_threads` (disposition `closed` + note with
the commit), check `owned_threads(state, "P12B") == 0`, record P12B in STATE
`completed_phases`/`phase_records`, reset `in_progress_details` to null per its comment,
and move `current_phase` on to P12 (`scripts/next_graph_prompt.py` should then print P12).

Context: measure with `python3 ~/.claude/skills/close-session/scripts/context_usage.py` after
each commit. For Opus 5, write a handoff at 160K and start fresh by 200K; one F per session is
fine.
