# P12A S2 — design contract (W1, W2, W3, W8, `_p07b` #1/#2/#4)

Written by the S2 orchestrating session after orientation. Implementation steps A–D are done
in order, each by one agent. Every step reads this file plus
`handoffs/2026-09-15-graph-p12a-s2.md` (context, harness, coordination rules) and
`docs/graph-engineering/specs/phases/P12A_GRAPH_DISPATCH_WIRING.md` §2 W1/W2/W3/W8, §3, §4.

The decisions below are fixed. If one proves impossible, stop and report why; don't
invent a substitute. The same applies if an item can only be built by bypassing the single
choke point, adding a second fresh-vs-resume decision, or adding a context rotation. That
is §4 design drift and needs Dan.

## Fixed shape

**S-1. One optional routing object.**
- `WorkflowRunner(..., routing=None)` adds the kwarg, and `start_run` passes it through via `**runner_kwargs`.
- `routing is None` must reproduce today's behaviour exactly, so every existing `tests/workflows` test stays untouched.
- New module `maestro/workflows/routing.py` defines `DispatchRouting` (a dataclass) with:
  - `catalog`, `policy`, `statistics`, `resources` (`ResourcePoolState`) and `trace=None`
  - `queries` (a `RepositoryQueries`-like object with `.resolve`)
  - `task_spec(task_id) -> TaskSpec`
  - `usage_reader(binding) -> Usage | None` (default: returns `None`)
  - `admit(runner, node) -> AdmissionAnswer | None` (W8; default `None`, meaning admitted)
  - `on_attempt_finished(runner, attempt_id) -> None` (W8 release; default no-op)
- `routing.py` is the only place that knows how the components fit together. The runner calls into it; it doesn't import catalog or router details inline.

**S-2. One function owns admit → manifest → resolve → claim → record → launch.**
- New `WorkflowRunner.advance(node, *, at, report)` becomes `tick`'s *only* per-frontier-node call, replacing the inline `claim` + `dispatch`.
- Order inside it:
  1. W8 `routing.admit`. A refusal adds `report.waiting[node_id]` and returns, with no claim.
  2. For `node.kind == "agent"` with routing set:
     - W2 builds the manifest;
     - W1 calls `resolve_agent` **exactly once**;
     - claim uses `deadline_sec = router.execution_deadline_sec(binding, node)` (`claim` gains an optional `deadline_sec`; the default stays `node.timeout_policy`);
     - W3 records the binding, the manifest artifact and start telemetry on the attempt;
     - then `dispatch`.
  3. Every other node: `claim` + `dispatch`, as today.
- `launch_worker` stays the single worker launcher. `Claim` gains optional `binding`, `plan` (`DispatchPlan`), `manifest_artifact_id` and `session` fields, all defaulting to `None`.

**S-3. Route unavailable.**
- **`wait`:** no claim; the node's phase is untouched. `report.waiting[node_id] = "route_<reason>: <detail>"`. `_graph_main` writes those waits (and W8's) through the existing `_record_admission_waits`, keyed by task id, reason `route_<reason>`.
- **`block`**, and a manifest refusal (`StaleSnapshotError` / `UnknownSnapshotError` / `ContextError`):
  - claim with the node's default deadline, then fail it through one shared helper `_fail_claim(node, claim, reason, at)`;
  - factor that helper out of `launch_worker`'s existing launch-failure path, so there is one failure path;
  - the reason goes into `report.refused[node_id]` and onto the attempt (`route_refusal` column, W3; step A may store it in a store event if W3's columns don't exist yet, and step B then moves it);
  - a stale snapshot never reaches `resolve_agent`.

**S-4. W2 manifest.**
- Built with `build_context_manifest(routing.task_spec(task_id), policy.agent_definitions[node.agent_definition_ref], revision.code_base_sha, policy, queries=routing.queries)`.
- `resolve_agent` receives the `ContextManifest` object itself. The router does `from_manifest`; nobody builds a `RoutingContext` by hand.
- After claim it is stored with `ArtifactStore.put_bytes(json, type="context_manifest", producer_attempt_id=claim.attempt_id, source_snapshot_id=manifest.snapshot_id)`.
- The runner needs an `ArtifactStore`. `_graph_main` constructs one (find the conventional root, e.g. how `tests/workflows/conftest.py::artifacts` does it).
- `node_inputs` includes `{"context_manifest": manifest.to_dict()}` for that attempt, and the worker argv carries `--manifest-artifact-id`.

**S-5. W1 argv and worker.**
- When `claim.plan` is set, `_default_worker_argv` appends, in this order:
  - `--backend-id <binding.backend_id> --runtime-kind <entry.runtime_kind> --model <plan.model>`
  - `--effort-arg <a>` for each `plan.effort_args`
  - `--effort-config <k>=<v>` for each `plan.effort_config`
  - `--manifest-artifact-id <id>` and `--session-id <id> --session-mode <fresh|resumed>`
- The test asserts the recorded tmux command's tail equals the rendering of `dispatch_plan(binding)`.
- `worker.py`:
  - **`runtime-kind == "model_endpoint_harness"`:** runs `ModelAgentRuntime`. It builds the endpoint from `config.free_tier_endpoints()`, matched by backend id, through a module-level factory `_model_api(endpoint)` that tests replace with a fake. The brief comes from the manifest artifact. The `RuntimeOutcome` becomes the `NodeResult`.
  - **An `agent_cli` binding:** resolves the driver through `registry.driver_name_for_backend_id` (thread #2, below). It runs one bounded agentic `driver.complete(CompletionSpec(prompt=brief, model=plan.model, cwd=worktree-or-workspace, writable=True, log_file=inbox/impl.log, effort_args=..., effort_config=...))`, then adopts the workspace through `agent_handler`, as today.
  - `CompletionSpec` gains `effort_args: Sequence[str] = ()` and `effort_config: Sequence[tuple[str,str]] = ()`. The claude, codex and antigravity drivers append them where their CLIs take them. Existing driver tests stay green.
  - No binding args at all means today's path, unchanged.

**S-6. Catalog and config.**
- `config.free_tier_endpoints(document=None)` reads `engineering.free_tier_endpoints: [{endpoint_id, base_url, model, api_key_env, verified_version, strength?, usage_pool_id?}]`.
  - Absent means `[]`.
  - Malformed raises `ConfigError`, following the `engineering_runner` precedent.
- `_graph_main` builds a `DispatchRouting` once, through one module-level function `_graph_routing()` that tests can monkeypatch:
  - catalog = `catalog_from_register(load_register(), defaults=<the existing shipped defaults>)` plus `model_api.capability_entry(endpoint, verified_version=...)` for each configured endpoint;
  - policy = `ProjectPolicy.load(REPO)`;
  - statistics loaded from the store (W3) or empty;
  - `queries=RepositoryQueries(REPO)`;
  - `task_spec` from the ROADMAP via `normalize_spec`.
- The lane conftest must not reach real drivers or the host. Give `LaneScenario` a `routing` factory field, and default the lane harness to an inert fixture routing so W0 tests keep passing unchanged.

**S-7. Thread #2 (backend_id → driver).** Add a `backend_id` to each `registry.DriverRef` (read the real ids from the shipped catalog defaults, e.g. `antigravity_cli`) and `registry.driver_name_for_backend_id(backend_id)`, which raises `UnknownBackend`. Unit-test all three.

**S-8. W3 durable routing decision.**
- **Attempt columns**, added through `ControlStore._ADDED_COLUMNS` (all nullable):
  - binding: `binding_id`, `binding_json`, `backend_id`, `model_id`, `effort`, `shape_key_json`, `manifest_artifact_id`, `route_refusal`
  - telemetry: `agent_version` (the catalog entry's `installed_version`), `session_id`, `session_mode`, `usage_start_json`, `usage_settle_json`, `wall_time_sec`, `reset_crossing` (INTEGER, `NULL` = unknown)
- **Session decision:** one function, `routing.session_decision(runner, node, claim) -> SessionDecision(mode, session_id, resume_id)`. The default is always `fresh`, `session_id=attempt_id`, `resume_id=None`. It is called only from `advance`, and nothing else decides fresh vs resume.
- **Usage snapshots:**
  - start: `routing.usage_reader(binding)` at claim;
  - settle: the same reader when `ingest_results` commits that attempt's result.
  - Serialize `Usage` with `None` kept as `None`; never estimate (G6).
  - `reset_crossing` = 1 if any window's `resets_at` lies in (start, settle], 0 if both snapshots have windows and none does, `NULL` otherwise.
  - `wall_time_sec` is settle minus claim stamp.
- **Outcomes:** an append-only table `routing_outcomes(outcome_id PK, attempt_id, run_id, shape_key_json, success INTEGER, verified_by TEXT, amends TEXT NULL, recorded_at)`.
  - When `settle()` finishes a run, the runner records one row per node's last bound attempt, with `success = (outcome == "succeeded")` and `verified_by="run_outcome"`. That is the controller's verdict and never the agent's result (D06).
  - It also calls `router.record_outcome(routing.statistics, binding, success=...)`.
  - A later differing verdict for the same attempt appends a row with `amends=<previous outcome_id>`; rows are never updated.
  - `statistics` are loaded by folding the latest row per attempt.
- **Explain:** `status.explain_view` links carry each attempt's routing fields plus `status_summary`-style text, and `render_explain_text` prints the binding identity (`metrics.binding_identity` form: `node_id` + `binding_id`). Update the docstring paragraph that says routing is not persisted.

**S-9. W8 graph admission.**
- `_graph_main` supplies `routing.admit` and `routing.on_attempt_finished`, reusing `_admission_round`, `_reserve_host_resources` and `_record_admission_waits` (don't copy them).
  - **Admission round:** one per poll, over this project's live graph reservations. Its `counted_live` is `attempt_id -> amounts` for graph attempts still holding a reservation.
  - **Per ready node:** `ResourceRequest.from_node(node, task_id=...)`, then `round.admit(candidate)`, then `_reserve_host_resources(task_id, attempt-key, amounts)`. A refusal triggers `withdraw` and records the wait.
  - **Release:** when the attempt finishes (ingest commit or launch failure), `on_attempt_finished` calls `HostReservationLedger().release`.
- Nodes that request no counted amounts skip the ledger.
- Test through `run_lane` with `_host_capacity` sized for one heavy node. Two heavy agent-or-script nodes are ready: 1 claimed and 1 `admission_waiting`. A second test seeds another project's reservation in the shared ledger and asserts the total `ram_mb` never exceeds capacity. Copy the setup patterns from `tests/test_host_resource_admission.py`.

**S-10. Thread #1 (agy 3p pools).**
- `ModelProfile` gains an optional `usage_pool_id`, and `BackendCapabilityEntry.pool_for(model_id)` returns it or the entry's pool.
- The router uses `pool_for` both for admission and for `binding.usage_pool_id`.
- The agy defaults give `claude-*` / `gpt-oss-*` the pool `antigravity-3p`.
- `Usage` gains `pools: Mapping[str, Mapping[int, WindowUsage]]` (default empty). The agy `usage()` fills `antigravity-gemini` from `gemini-5h/weekly` and `antigravity-3p` from `3p-5h/3p-weekly`, and leaves `windows` as the Gemini pools for compatibility.
- Remove the "not attributed (a P12A thread)" comments.

**S-11. Thread #4 (agy `--sandbox` / headless permission mode).** This needs a real observation.
- Read `agy --help`.
- Only if it runs non-interactively without login or quota risk, run one bounded headless edit in a scratch git repo under `/home/dan/projects/maestro/.scratch/` (not `/tmp`).
- Record what was observed in the agy register row's findings and in the driver's capability docstring.
- If it can't be exercised safely, record that as the finding with the exact reason. Don't flip `sandbox=True` without observed evidence.

## Steps
- **A:** S-1 … S-7 (W1, W2, thread #2) plus lane tests.
- **B:** S-8 (W3) plus lane tests.
- **C:** S-9 (W8) plus lane tests.
- **D:** S-10 and S-11, then the `STATE.yaml` targeted updates.

Each step ends with:
- its focused tests green, plus `tests/workflows`, `tests/backends` and `tests/graph_engineering` green;
- one commit (never `AGENTS.md` / `GEMINI.md`; message ending with the Co-Authored-By trailer);
- a ≤25-line report of what was built, test counts from junit, and any deviation from this file.
