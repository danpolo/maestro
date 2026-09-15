# PHASE P12A: WIRE THE BUILT GRAPH COMPONENTS INTO LIVE DISPATCH (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P12A
Depends On:      P10B (host ledger, admission round), P07B (agy in the catalog), P11 (operator views), P10, P09, P07, P06
Output:          A project run that selects the graph runner drives every built P06–P10B component from the live loop
Target Files:
  - Extend:  maestro/config.py
  - Adapt:   maestro/orchestrator.py
  - Extend:  maestro/workflows/runner.py
  - Extend:  maestro/workflows/worker.py
  - Extend:  maestro/validation.py
  - Extend:  maestro/control/store.py
  - Extend:  maestro/status.py
  - Extend:  maestro/cli.py
  - Create:  tests/graph_engineering/test_dispatch_wiring.py
  - Extend:  tests/test_host_resource_admission.py
  - Extend:  tests/test_workflow_status.py
  - Extend:  tests/test_cli.py
Verification:    .venv/bin/python -m pytest -q tests/graph_engineering/test_dispatch_wiring.py tests/test_host_resource_admission.py tests/test_workflow_status.py tests/workflows
```

---

## 1. OBJECTIVE & DELIVERABLES
P06–P10B built routing, the free-model runtime, candidate selection, repository context, affected-check selection, plan repair and host admission. Each one is unit-tested, but **no controller loop calls it**. `WorkflowRunner` and `start_run` are only called from tests: `maestro run` still runs only the legacy launch loop. This phase adds the graph runner loop to `maestro run` behind a per-project selector and connects each built component at the seam where the live loop must call it. It closes every open thread in `STATE.yaml` with `owner: P12A`, so P12 qualifies a system that actually runs these components.

Rules that hold throughout:
- **D08 / MIGRATION_GUARDRAILS §2**: in any one project run, only the legacy runner or only the graph runner runs, never both. The selector defaults to `legacy`. With the default, every existing test and ROADMAP behaves exactly as it does today.
- **MIGRATION_GUARDRAILS §3**: P8–P10 optimizations stay OFF by default. Affected selection is wired in `shadow` mode. `authoritative` mode is refused until `qualify_affected` passes on persisted evidence.
- **"Wired" means proven under the loop**: each item's test drives one poll of the graph loop (not a direct unit call) and asserts that the component was called and that its result changed what was dispatched.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **W0: Runner selection and the graph loop** (new gap found while planning; `_p12a_open_threads` #1): `config` reads `engineering.runner: legacy | graph` from `project.yaml` (default `legacy`; an unknown value is a config error). In `graph` mode, `orchestrator.main` resumes open `task_runs`. For each ready task it builds a revision with `compiler.compile_workflow` and opens it with `runner.start_run`. Then it calls `WorkflowRunner.tick()` once per poll, followed by `ingest_results()` and `settle()`. It never enters the legacy launch path (`launch_implementer`, `launch_async_job`). Changing the selector while a run of the other kind is active is refused, with a reason.
- [ ] **W1: Binding resolution at dispatch** (P07 #1): before `WorkflowRunner.dispatch` launches an agent node, call `router.resolve_agent(node, manifest, resources, catalog, policy)`. On `RouteUnavailable` with `wait`, the node stays ready and the reason is recorded. On `block`, the node fails with that reason. The binding is consumed only through P06's four seams: `dispatch_plan` supplies the worker argv/effort, `execution_deadline_sec` supplies the claim deadline, `token_account_key` supplies quota accounting, and `status_summary` supplies status output. The loop's catalog adds `model_api.capability_entry()` for each configured free-tier endpoint. A node bound to a `free_tier_api` backend runs under `ModelAgentRuntime` in `workflows/worker.py`; a CLI backend runs through the tmux argv.
- [ ] **W2: ContextManifest feeds routing** (P09 #1): for each agent node, the runner builds `build_context_manifest(task, role, snapshot, policy, queries=RepositoryQueries(...))` against the run's `code_base_sha` before dispatch. It stores the manifest as an artifact and passes it to `resolve_agent`, which turns it into a `RoutingContext` via `from_manifest`, and into the node's inputs. No dispatch path builds a `RoutingContext` by hand. A stale snapshot rejects instead of routing on old facts.
- [ ] **W3: Durable routing decision** (P11 #2): at claim, the chosen binding (`metrics.binding_identity`, backend, model, effort, shape key) is stored on the attempt, and `status.explain_view` shows it as the binding link. When a node settles with a controller-verified outcome, `router.record_outcome` is called. Agent self-reports are never counted (D06). **Forward compatibility (§4):** the same attempt record also stores nullable telemetry that is already cheap to read at claim and settle. That is the exact model id and agent CLI version, the maestro `session_id` plus whether the attempt started a fresh session or resumed one, the driver's `Usage` at start and at settle (token split where the driver reports it, and each quota window's `used_pct` and `resets_at`), and wall time. A window whose `resets_at` falls between start and settle is flagged as crossing a reset. A value the driver cannot measure stays `NULL` and is never estimated (G6). A verified outcome that is later reopened is written as an appended amendment, never an in-place overwrite.
- [ ] **W4: Candidate reap** (P07 #2): when a node's candidate attempts settle, the controller assembles one document per `candidate_id` from ingested evidence and outcome artifacts. It hands them to `maestro.handlers.candidate_select` as `inputs['candidates']` through `WorkflowRunner.node_inputs`. A candidate with no settled attempt gets no document, so the handler marks it `abandoned` (never dropped) (`[INV-06]`).
- [ ] **W5: Affected checks at verification** (P10 #1, selection half): the input of a verification node is `validation.select_affected_checks(specs, changed, mode=policy_mode)`, where `changed` is the diff between the node worktree and the run's `code_base_sha` (`None` when the diff cannot be computed, which selects the full suite). The default mode is `shadow`.
- [ ] **W6: Persisted shadow comparisons** (P10 #2): each run in `shadow` mode records one `ShadowComparison` per verification node in a control-store table, keyed by run, node and snapshot. `qualify_affected` reads that table. The policy may switch to `authoritative` only when `qualify_affected` passes on stored rows; otherwise the switch is refused with the count still missing.
- [ ] **W7: Plan repair in the loop** (P10 #1, repair half): when a planner or diagnoser node outputs a `PlanRepairProposal`, the controller runs `compiler.plan_repair(previous, proposal, phases, counters=repair_counters(...))`, then `compiler.apply_repair(store, plan, old_run_id=...)`, and moves the loop to the new run id. A refusal is recorded and fails that node. Counters, deadlines and repair quotas are carried forward, never reset (`[INV-08]`).
- [ ] **W8: Host admission for graph nodes** (P10B #6): before `WorkflowRunner.claim`, the graph loop admits each ready node with the same `_admission_round` as the legacy loop: `ResourceRequest.from_node`, `AdmissionEngine` with other projects' `held_amounts` subtracted, and `HostReservationLedger.reserve_counted` before launch. The reservation is released when the attempt finishes. A waiting node writes `admission_waiting`, just as a legacy task does.
- [ ] **W9: Oversized requests park** (P10B #4): a request larger than the whole detected `HostCapacity` on any dimension no longer waits forever. In both loops it parks with a reason naming the dimension, the amount requested and the capacity.
- [ ] **W10: Async hold snapshot** (P10B #5): `launch_async_job` records the amounts it was admitted with on the `awaiting-verification` entry. `_live_counted` reads those recorded amounts instead of re-deriving them from the ROADMAP, so editing or removing the block while the job runs keeps its hold.
- [ ] **W11: Run listing** (P11 #3): `maestro workflow runs <task_id>` (next to `status._select_run`) lists each run of the task with run id, revision, status, and start and finish times. `--json` is supported. Plan repair (W7) makes multi-run tasks normal, so the operator needs this list.
- [ ] **W12: `maestro init` setup defects from the P00 rehearsal** (`_p12a_open_threads` #3–#6, F2–F5; P12 onboards instagram-to-value, so they must be fixed before then):
  - **F2**: `_derive_repo_facts` stops resolving the `.venv/bin/python3` symlink, so the derived test command stays inside the virtualenv.
  - **F3**: a first `init` on a repo with no `.orchestrator/` writes no `state.json.new`, because init's own mid-run `state.json` is not treated as a pre-existing file.
  - **F4**: when a `.gitignore` exists, init proposes only the lines it is missing and never a replacement that drops existing ignores.
  - **F5**: the deny-list block in `operating_preamble.md` is re-rendered from `project.yaml` (`risky_set` / `deny_list_extra`) once they are set, or `doctor` fails while the placeholder text remains.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - With `engineering.runner` absent, the whole suite passes unchanged. A spy on `start_run` / `WorkflowRunner.tick` records zero calls across a legacy poll.
  - With `engineering.runner: graph`, one poll over a one-task ROADMAP opens a run and dispatches its first node. Spies on `launch_implementer` and `launch_async_job` record zero calls. A selector switch while a graph run is active is refused.
  - In a graph poll with a fixture catalog, `resolve_agent` is called exactly once per agent attempt. The worker argv equals `dispatch_plan(binding)`. A `free_tier_api` binding executes `ModelAgentRuntime` against a fake `ModelAPI`. A `wait` leaves the node ready and records a reason.
  - The `RoutingContext` that `resolve_agent` receives equals `RoutingContext.from_manifest(stored_manifest)`, and the manifest artifact exists for the attempt.
  - `maestro explain <task>` after a graph poll shows the binding identity chosen in that poll. A verified outcome increments the statistics for that shape key.
  - A 3-candidate free-quality run with one crashed candidate selects exactly one winner and reports the crashed one as `abandoned`.
  - A shadow-mode verification node runs the full suite, and the control store holds one `ShadowComparison` row for it that survives a controller restart. Switching to `authoritative` with fewer than `minimum_runs` rows is refused.
  - A fixture diagnoser proposal moves the loop to revision N+1 under a new run id with cumulative counters preserved. A proposal past the repair quota is refused and the node fails.
  - With RAM for one of two ready heavy graph nodes, exactly one is claimed and the other writes an `admission_waiting` reason. Two projects sharing the ledger never reserve more `ram_mb` than capacity.
  - A task requesting more RAM than the host has parks with a capacity reason in both loops.
  - Editing an awaiting-verification job's ROADMAP `resources:` block mid-run leaves its recorded hold unchanged.
  - `maestro workflow runs <task>` lists both runs of a repaired task in start order.
  - On a scratch repo with a `.venv` whose `python3` is a symlink, the derived test command starts with the venv path. A first `init` leaves no `state.json.new`. Against an existing 56-line `.gitignore`, the proposal keeps every existing line. With a deny list set in `project.yaml`, the preamble shows that list, or `doctor` exits non-zero.
  - A graph poll's claimed attempt carries model id, agent version, session id, fresh-vs-resumed and a start `Usage` snapshot (fields `NULL` where the fixture driver reports none). A fixture whose quota window resets mid-attempt is flagged as reset-crossing. Every agent-node launch in the graph loop, including nodes created by W7's repaired revision, passes through one dispatch function (a spy on it sees each launch exactly once).
  - The whole suite (`.venv/bin/python -m pytest -q`) exits 0, including `tests/test_selfupdate.py` (`_p12a_open_threads` #2).
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/graph_engineering/test_dispatch_wiring.py tests/test_host_resource_admission.py tests/test_workflow_status.py tests/workflows
  ```

---

## 4. FORWARD COMPATIBILITY: CONTEXT GATE (DO NOT IMPLEMENT)
`docs/maestro-context-gate-integration.md` (scheduled after P12) will insert a Context Gate **at the Work Unit dispatch boundary**. At that boundary it will decide whether the node's agent session continues, compacts, or hands off to a fresh session. It will do this through existing orchestration abstractions, with no second wrapper process. In the graph loop, a WU boundary is an agent node dispatch. This phase builds that boundary, so shape it for the gate, but build none of the gate itself: no gate, no canonical JSONL log, no semantic evaluator, no exploration, no policy bundle. Do not read the gate plan unless a constraint below is unclear.

- **One dispatch choke point.** Every agent-node launch goes through one function between `claim` and the worker launch. That covers planned nodes, retries, candidates (W4) and nodes from a repaired revision (W7). No path may launch around it. The gate will be inserted there, and the plan requires that "discovered subtasks cannot bypass the gate".
- **Session continuity is one decision, made in one place.** Whether a node starts a fresh session or resumes an existing one (`Handle.native_id` / `LaunchSpec.resume_id`) is chosen by a single function whose default reproduces today's behaviour. The choice is recorded on the attempt (W3). Do not scatter fresh-vs-resume logic across the runner, the worker and the drivers.
- **No second context-rotation mechanism.** The legacy loop's D4 rotation (`orchestrator._context_rotations` → `switch.context_crossed`, fixed at `prepare_handoff_high`) is the only context action maestro has. The graph loop must not grow a parallel threshold-based rotation. If a graph node needs rotation in this phase, it reuses that seam; the gate replaces it later.
- **Telemetry and outcomes are recorded, not interpreted** (W3): nullable, append-only, reset-crossing flagged. Nothing here makes a decision from them.
- Treat `docs/maestro-context-gate-integration.md` §3–§5 as the future caller of these seams. If a W-item can only be built in a way that would force the gate into a second wrapper process or a bypass path, that is design drift: stop and ask Dan (§7 of the directive).
