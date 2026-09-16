# PHASE P12B: HANDLE FAILING MODELS WITHOUT LOWERING QUALITY (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P12B
Depends On:      P12A (graph loop, one dispatch choke point, durable routing decision, plan repair in the loop), P07, P06
Output:          In every graph run, a failing or blocked model is classified, routed around without lowering quality, retried, escalated and parked by one fixed ladder
Target Files:
  - Create:  maestro/workflows/failures.py
  - Extend:  maestro/backends/model_api.py
  - Extend:  maestro/backends/model_runtime.py
  - Extend:  maestro/backends/router.py
  - Extend:  maestro/backends/catalog.py
  - Extend:  maestro/workflows/worker.py
  - Extend:  maestro/workflows/runner.py
  - Extend:  maestro/workflows/routing.py
  - Extend:  maestro/workflows/compiler.py
  - Extend:  maestro/control/store.py
  - Extend:  maestro/control/reconcile.py
  - Extend:  maestro/config.py
  - Extend:  maestro/taskgraph.py (the `fallback:` task field, next to `resources:`)
  - Extend:  maestro/orchestrator.py
  - Extend:  maestro/status.py
  - Extend:  maestro/cli.py
  - Create:  tests/workflows/test_failures.py
  - Create:  tests/graph_engineering/test_failure_ladder.py
  - Create:  tests/backends/test_model_api.py
  - Extend:  tests/backends/test_model_runtime.py
  - Extend:  tests/test_workflow_status.py
Verification:    .venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows
```

---

## 1. OBJECTIVE & DELIVERABLES
P12A made the graph loop real, but it treats every failure the same way. Every compiled node has `RetryPolicy(max_attempts=1)`, and a `failed` node is terminal, so nothing is ever retried. `worker._run_agent_cli` ignores the driver's result, so a CLI that hit a rate limit looks like a run that simply changed nothing. `ModelAgentRuntime` turns every HTTP error into `endpoint_unavailable`. This phase adds Dan's failure policy (D-FAIL-1 and D-FAIL-2, 2026-09-16) to the graph loop.

**The principle:** a failing model must not lower the quality of the work. If one model is blocked, use another of equal or better quality.

**The ladder (the whole phase is this):**
1. **Classify** every failed attempt into a closed vocabulary, in one of two classes:
   - **`blocked`**: the model could not work (rate limit, quota exhausted, provider 5xx or outage, timeout, auth failure, context overflow).
   - **`bad_output`**: the model worked badly (empty or garbage output, repeated malformed tool calls, failed verification, a crashed worker, or anything unrecognised).
2. **`blocked` → switch.** Pause that model's pool until its reset and route again. The router already sends the node to an equally qualified alternative. A blocked attempt never uses up a retry.
3. **No equal-or-better model available → wait, or drop one tier with checks.** A task that opts in (`fallback: step_down`) *and* passes the controller's checkability check may drop one strength tier inside the same billing categories. It then runs full verification and is accepted only if that passes. Every other task waits for the reset.
4. **`bad_output` → 2 retries.** Retry 1 uses the same binding in a fresh session. Retry 2 uses a different model of equal or better quality when one exists, otherwise the same one.
5. **Retries used up → a stronger diagnoser.** It runs one strength tier above the model that failed, inside the objective's own billing categories. Free mode never uses a paid model for this (Dan, 2026-09-17). The diagnoser decides between two causes:
   - *bad graph*: plan repair, then **one final run**;
   - *poor agent*: `replace_nodes` with an enhanced brief and context, then **one last relaunch**.

   Both are one `PlanRepairProposal` and use up the single `engineering.max_plan_repairs` (default 1, built in P12A W7). The final run has no retries left.
6. **Anything else → park** with the reason and the full attempt history.

Rules that hold throughout:
- **Graph loop only.** The new transitions run only when `WorkflowRunner.routing is not None`. With `routing is None`, every P03–P11 behaviour and test stays byte-for-byte the same. The legacy loop is unchanged, except that it reads and writes the shared pool-pause record (F4).
- **"Handled" means proven under the loop**, as in P12A: each item's test drives graph polls through `orchestrator.main` or `WorkflowRunner.tick`, not a direct unit call. The test uses fixture failures (a fake `Transport`, a fake driver `Completion` or `ExitVerdict`) and asserts what the *next* dispatch was.
- **Unknown is not fatal.** A failure the classifier does not recognise is `unknown` (`bad_output` class). Its raw evidence is kept, so Dan's free-model pilots can name it later. A harness update that changes an error message degrades to `unknown`; it never fails a gate (memory `harness-auto-updates-must-not-break-gates`).

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **F0: Failure vocabulary** (`maestro/workflows/failures.py`): a closed `FAILURE_KINDS` mapping of kind → class.
  - `blocked`: `rate_limited`, `quota_exhausted`, `provider_unavailable`, `timeout`, `auth_failed`, `context_overflow`.
  - `bad_output`: `empty_output`, `malformed_tool_calls`, `garbage_output`, `verification_failed`, `worker_crashed`, `unknown`.

  A frozen `FailureRecord(kind, failure_class, evidence, retry_after_sec, reset_at)` with `to_dict`/`from_dict`, and `classify_node_result(result) -> FailureRecord | None` that reads `NodeResult.outputs["failure"]` when present. A failed result without that key is `unknown`, except that a verification or gate handler's failure is `verification_failed`. An unknown kind string is refused (`ValueError`), never accepted silently.
- [ ] **F1: Classify free-tier endpoint failures** (`model_api`, `model_runtime`): `ModelAPI.complete` raises typed subclasses of `TransportError`, each carrying the HTTP status and parsed `Retry-After`/reset headers:
  - `RateLimited` (429, or a provider quota body) → `rate_limited`, or `quota_exhausted` when the body says the daily or monthly quota is gone;
  - `AuthFailed` (401/403) → `auth_failed`;
  - `ProviderUnavailable` (5xx, connection error) → `provider_unavailable`;
  - `RequestTimeout` → `timeout`;
  - `ContextOverflow` (400 whose body names the context or token length, per wire) → `context_overflow`.

  `ModelAgentRuntime` maps each to a `stop_reason` equal to the kind. It also detects `empty_output` (a first turn with no text and no tool calls) and `malformed_tool_calls` (3 consecutive turns whose tool-call arguments do not decode). The worker writes `outputs["failure"]` from the runtime outcome. Existing `endpoint_unavailable` callers keep working through the base class.
- [ ] **F2: Classify agent-CLI failures** (`worker._run_agent_cli`): keep the driver's `Completion` (`text`, `returncode`, `timed_out`). `timed_out` → `timeout`. Otherwise classify with the driver's existing `parse_exit(returncode, <tail of the attempt's impl.log>)` → `ExitVerdict`:
  - `quota_exhausted` with `reset_at` → `quota_exhausted` or `rate_limited`. The claude CLI's "monthly spend limit" text arrives as a 429 and is a `rate_limited` with a reset (memory);
  - `crashed` → `worker_crashed`;
  - an empty completion that changed nothing → `empty_output`.

  A `blocked` CLI run **does not run the node's adoption handler**: the worker returns a failed `NodeResult` carrying `outputs["failure"]`. It never adopts a worktree the model never touched. Classification reads only what the drivers already parse; no new CLI-text matching beyond one table in `failures.py` whose misses are `unknown`.
- [ ] **F3: Durable failure record and operator view** (`store`, `status`, `cli`): new nullable `attempts` columns `failure_kind`, `failure_class`, `failure_json`, written from the ingested result through `lifecycle.record_routing_decision`/`record_routing_settle` (append-only, like W3's telemetry). `status.explain_view` shows the kind and evidence per attempt. `maestro workflow failures [<task_id>] [--kind K] [--json]` lists failed attempts with binding, kind and evidence; `--kind unknown` is the pilot report.
- [ ] **F4: `blocked` → pause the pool, route again without using a retry** (`runner`, `routing`, `catalog`, `store`, `orchestrator`):
  - A new append-only `pool_pauses` control-store table `(pool_id, paused_until, reason, attempt_id, recorded_at)`. Each poll it is loaded into the `ResourcePoolState` passed to `resolve`. For a CLI backend the same pause is mirrored to `quota.record_backend_exhausted`, so the legacy loop and the graph loop agree on which backend is exhausted.
  - `paused_until` comes from, in order: the reset the failure reported; `Retry-After`; otherwise a backoff of 60 s × 2ⁿ, capped at 30 min. For `auth_failed` it is `engineering.auth_failure_pause_sec` (default 3600), and the pause is journaled and sent to Dan.
  - `context_overflow` pauses nothing. It adds `(backend_id, model_id)` to the node's `excluded_routes`, which `routing.resolve` passes to `resolve_agent(..., exclude=...)` as one more hard constraint (reason code `route_excluded`).
  - On ingest, a `blocked` failure moves the node `failed → ready` in the same transaction that decides the attempt, so no tick can see it `failed` and open its `on_failure` edge. The node's `blocked_attempts` counter increments; `quality_failures` does not.
  - The next `advance` calls `resolve` exactly once, as today. `RetryPolicy.permits` is checked against `quality_failures`, not the raw `attempt_index`, for agent nodes under routing. `[INV-08]`: both counters are cumulative across repairs.
- [ ] **F5: No equal-or-better model → wait, or step down with checks** (`routing`, `router`, `roadmap`, `compiler`, `config`):
  - When `resolve` returns `wait` because every qualified pool is paused, the node waits with the soonest `retry_after_sec`. A known reset is waited for, however far away. With no known reset, the node parks after `engineering.max_blocked_wait_sec` (default 86400) from its first block.
  - **The checkability check:** a ROADMAP task field `fallback: step_down` (default absent) is accepted only if *all* of these hold:
    - the compiled revision's terminal requirements include a verification (gate) node;
    - the task has at least one authoritative check spec;
    - the step-down attempt's verification runs the **full** suite. The W5 affected selection is forced to full for that attempt, whatever `affected_mode` says.

    Otherwise the flag is refused at compile time with a reason on the task (`fallback_refused: …`), and the task behaves as if the flag were absent.
  - **The step-down:** for an accepted task whose resolution is a `wait`, `resolve` is re-run **once, in the same call**, with `demand.min_strength` lowered by exactly one rung (`MODEL_STRENGTHS`). Billing categories never change: free stays free. The binding is marked `stepped_down=True` (stored in `binding_json` and in a new `attempts.stepped_down` column). A stepped-down attempt is accepted only when the run's terminal verification succeeds. Its failure is a `bad_output` like any other. Nothing ever steps down twice.
  - Still one `resolve` per attempt: the relaxed pass is internal to `routing.resolve`, not a second call site.
- [ ] **F6: `bad_output` → two retries** (`runner`, `routing`):
  - **The retry cone** of a `bad_output` failure: the failed agent node itself; or, for a failed verification or gate node, its nearest upstream agent (writer) node plus every node on the paths between them.
  - While `quality_failures < 3` (the first run plus 2 retries), ingest resets the cone's phases to `pending` in one transaction instead of writing `failed`. Attempts are kept, and `attempt_index` and counters carry on.
  - **Retry 1** uses the same `(backend, model)` with `session_decision → fresh`.
  - **Retry 2** adds the model that produced the output to `excluded_routes`, so `resolve` picks another equal-or-better route. If none exists, the exclusion is dropped for that one resolution, and the binding's `selection_reason` says so.
  - The third `bad_output` failure writes `failed` as today, so the node's `on_failure` edge opens (F7).
  - A writer node's retry starts from the worktree SHA recorded at its claim (new `attempts.start_sha`), restored inside the node's own worktree only, through `CommitBoundary`.
- [ ] **F7: Escalation to a stronger diagnoser** (`compiler`, `runner`, `routing`):
  - **The `diagnose` stage.** `compile_workflow(..., escalation=True)`, which only the graph loop passes, adds a `diagnose` stage to every lane. Every agent stage's and gate stage's `on_failure` edge goes to `diagnose`, and `diagnose`'s own `on_failure` edge goes to `park`. Without `escalation`, output is byte-identical to today (`tests/workflows/test_compatibility.py` unchanged). `COMPILER_VERSION` moves.
  - **Inputs.** `diagnose` is an agent node with role `diagnoser`. Its inputs are the failing node's spec, its brief, every attempt's failure record and evidence, and the verification output.
  - **Routing.** Its demand is **one strength rung above the failed attempt's model** (the top rung stays the top). Its billing is the objective's own permitted set: in `free_quality` that is free-tier only, and the hybrid overlay is **not** consulted for it. With no route at that strength, `resolve` blocks, and `diagnose` fails to `park` with the reason.
  - **Verdicts.** The diagnoser's output is one `PlanRepairProposal` in `outputs["plan_repair"]`, with a `verdict: bad_graph | poor_agent` field added to the proposal (round-trips through `to_dict`/`from_dict`):
    - `bad_graph`: a graph patch plus `rerun_nodes`;
    - `poor_agent`: `replace_nodes` for the failing node with an enhanced brief or context (`params`), plus `rerun_nodes`.

    P12A W7 consumes it, and it uses the single `max_plan_repairs` repair.
  - **The final run.** Nodes carried into the repaired revision as `rerun_nodes` start with `quality_failures = 3`, so they get no retries: the next `bad_output` goes straight to `failed`. Their `diagnose` edge is skipped because the repair budget is used up (`repair_counters`), so they park. `blocked` switching (F4) and waiting (F5) still apply during the final run: a rate limit is not a quality verdict.
  - **Refusals.** A diagnoser with no proposal, or whose proposal is refused, fails to `park`. This resolves P12A's `apply_repair` known limit for escalation: when `apply_repair` refuses a diagnoser's proposal, that `diagnose` node is written `failed`, not left `succeeded`.
- [ ] **F8: Dead writers reach a decision** (P12A W4 known limit, `_p12b_open_threads` #1; `reconcile`, `runner`): a writer whose process is dead with no receipt is still reconciled to attempt `uncertain` (A05). Under routing, the reconciler now also decides the node:
  - a **candidate** node (isolated worktree, discarded if it loses) moves to `failed` with `worker_crashed`, so `candidate_select` receives no document for it and marks it `abandoned`. The join proceeds;
  - a **non-candidate** writer is handled as a `worker_crashed` `bad_output` through F6, starting from its `start_sha`.

  With `routing is None`, reconciliation is unchanged.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - F0: every kind in `FAILURE_KINDS` round-trips through `FailureRecord`. An unknown kind string raises. A failed result without `outputs["failure"]` is `unknown` (or `verification_failed` from a gate handler).
  - F1: a fake `Transport` returning 429 with `Retry-After: 120`, 401, 503, a timeout, and a 400 context-length body produce the five matching kinds. The runtime's `stop_reason` equals the kind. A first empty turn gives `empty_output`, and 3 undecodable tool-call turns give `malformed_tool_calls`.
  - F2: a fake driver whose `parse_exit` says `quota_exhausted` with a reset gives a failed `NodeResult` with that reset, and a spy shows the node's adoption handler was **not** called. A `crashed` verdict gives `worker_crashed`, and an unrecognised message gives `unknown`.
  - F3: after a failed graph poll, `maestro explain <task>` and `maestro workflow failures <task> --json` show the attempt's kind, class and evidence. `--kind unknown` lists only unknowns.
  - F4: two equal-strength fixture routes, and the first returns 429 with a reset. The next poll dispatches the node on the second route, `quality_failures == 0`, a `pool_pauses` row exists, and no `on_failure` edge opened. The pause survives a controller restart. A CLI backend's pause is visible to `quota.exhausted_backends()`. A `context_overflow` excludes only that route, for that node. An `auth_failed` pause lasts `auth_failure_pause_sec` and journals a notification.
  - F5, with the only strong route paused:
    - a task without the flag waits (`admission_waiting`/`waiting` reason names the reset) and is still waiting past a known reset 3 days out;
    - with an unknown reset, it parks after `max_blocked_wait_sec`;
    - with `fallback: step_down` and full verification, it dispatches on the one-rung-weaker route in the same billing category, `stepped_down == 1`, the gate runs the full suite even in `authoritative` mode, and the run succeeds only if the gate passes;
    - the same flag on a lane with no verification node is refused with `fallback_refused`, and the task waits;
    - a free-objective task never steps into a paid route.
  - F6: an implementer whose first two attempts end `bad_output` (fixture) runs a third time, and succeeds if the fixture then succeeds:
    - attempt 2 has the same binding identity with `session_mode == fresh`;
    - attempt 3's model differs when an equal route exists, and equals attempt 1's (with the reason recorded) when none does;
    - a failed gate re-runs the implementer and the gate, not the gate alone;
    - each writer retry starts from `start_sha`.
  - F7:
    - after the third `bad_output`, the poll dispatches `diagnose` on a model one rung stronger than the failed one;
    - in a `free_quality` project with no stronger free route, `diagnose` fails with a `route_` reason and the task parks, while a paid route in the catalog is **not** chosen even with a hybrid overlay present;
    - a `poor_agent` proposal yields revision N+1 whose replaced node carries the enhanced brief, and whose next `bad_output` goes straight to `park` with no retry and no second `diagnose`;
    - a `bad_graph` proposal does the same through a graph patch;
    - a `diagnose` with no proposal parks;
    - `compile_workflow` without `escalation` is byte-identical to before.
  - F8: a 3-candidate run whose c3 writer is killed with no receipt reaches `candidate_select`, selects exactly one winner, and reports c3 `abandoned`, with no failed launch standing in for the kill. A killed non-candidate implementer is retried once from `start_sha`. With `routing is None`, the same kill leaves today's `uncertain` state untouched.
  - Every retry, switch, step-down and final run passes through `WorkflowRunner.launch_worker` exactly once per launch (spy), and `resolve` is called exactly once per agent attempt.
  - The whole suite (`.venv/bin/python -m pytest -q`) exits 0.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows
  ```

---

## 4. FORWARD COMPATIBILITY: CONTEXT GATE (DO NOT IMPLEMENT)
`docs/maestro-context-gate-integration.md` (scheduled after P12) inserts a Context Gate at the agent-node dispatch boundary that P12A built. It is built independently, so this phase builds none of it. It must keep P12A §4's seams intact:

- **One dispatch choke point.** Every retry (F6), model switch (F4), step-down (F5), diagnoser (F7) and final-run node goes through `advance` → `resolve` → `claim` → `launch_worker`. No failure path launches a worker directly or calls the router a second time.
- **One fresh-vs-resume decision.** F6's "fresh session for retry 1" is an input to `routing.session_decision`, not a second decision elsewhere. A switched model is always fresh, because a session cannot move between backends.
- **No context rotation here.** A `context_overflow` is a routing fact (exclude that route), not a compaction or hand-off trigger. Do not add a threshold, a compaction or a summary step. The gate owns that decision later.
- **Failures are recorded, not interpreted beyond the ladder.** F3's columns are nullable and append-only. Nothing here reads them to change routing weights (P06 statistics stay run-outcome only, D06).
- If a ladder step can only be built by bypassing the choke point or by adding a second resume decision, that is design drift: stop and ask Dan.

---

## 5. OPEN ITEMS CARRIED, NOT BUILT
- agy's `--mode accept-edits` is still not characterised, so `Capabilities.sandbox` stays `False` for agy. A probe was refused by the permission classifier; Dan decides whether to run it. Until then, agy is a normal route for F4 switching, never a special case.
- New failure kinds found in Dan's free-model pilots are added to `FAILURE_KINDS` by a later change, from `maestro workflow failures --kind unknown` evidence.
