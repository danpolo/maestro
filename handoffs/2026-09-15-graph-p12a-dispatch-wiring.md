# Handoff — P12A: wire the built graph components into live dispatch (session 1)

Paste this into a fresh Claude Code session in `/home/dan/projects/maestro`.

---

## Goal

Start implementing Phase P12A. The orientation session (2026-09-15) read the spec and mapped every seam, but wrote **no code**. P12A is too big for one session, so this session does **S1** below and hands off the rest.

**Standing rule from Dan (2026-09-15, holds until he says otherwise):** Maestro's context management, the Context Gate in `docs/maestro-context-gate-integration.md`, is **not built** and **will be built independently**. Do not implement any part of it, and do not assume it exists. The spec's §4 seam rules still apply: one dispatch choke point, one fresh-vs-resume decision, no second rotation mechanism, and nullable telemetry. They are just good seams, not a schedule that ties the gate to P12. The memory `context-management-not-built-independent` records this.

**Read first, in this order:**

1. `docs/graph-engineering/specs/phases/P12A_GRAPH_DISPATCH_WIRING.md`: all of it. It is the spec.
2. Run `.venv/bin/python scripts/next_graph_prompt.py`. It gives the §4A list of 18 owned threads and the §6 close-out and §7 escalation rules.
3. `docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md` §1.1 and §3. The tick and 6-step protocol are already implemented in `runner.py`.
4. Don't read the gate plan, the specs README (already summarised here) or `STATE.yaml` in full. It is 88 KB, so grep `owner: P12A`.

## Session plan (proposed split)

- **S1 (this session):** thread #2, then W12 (F2–F5), then W0.
  - Thread #2 is the order-dependent `test_selfupdate` leak. The spec says to fix it first, because acceptance needs the full suite at exit 0.
  - W12 (F2–F5) is small and independent.
  - W0 is the runner selector plus the graph poll loop. Every other W-item depends on it.
- **S2:** W1 (binding at dispatch, including `_p07b` #2 backend_id→driver mapping, #4 agy sandbox, #1 3p pools), W2 (manifest→routing), W3 (durable binding + telemetry), W8 (graph admission).
- **S3:** W4 (candidate reap), W5/W6 (affected checks + shadow table), W7 (plan repair), W9 (oversized park), W10 (async hold snapshot), W11 (`workflow runs`), full suite, STATE close-out.

## What orientation established (don't re-check)

**Nothing outside tests calls the graph runner.** `start_run` and `WorkflowRunner` have no callers under `maestro/` except `compiler.apply_repair`, which calls `life.start_run` at `compiler.py:1276`.

**`maestro/config.py` (94 lines)**
- `_load_project_yaml()` collapses every failure to `{}`, and `thresholds()` / `threshold()` are lenient by design.
- For W0, the spec says an **unknown** `engineering.runner` value is a config error. That is the opposite of `thresholds`' leniency, so follow the `workflow_policy_path` precedent: raise, don't collapse.

**`maestro/orchestrator.py` (2518 lines)**
- `main()` starts at L1783: startup reconcile, `_sync_host_ledger`, `metabranch.reconcile_meta`.
- The `while True` loop runs, in order:
  - halt/pause checks
  - telegram, self-fix and redo
  - `poll_awaiting_verifications`
  - `get_effective_cap` + `_threshold_switches`
  - `_context_rotations` (L~1480, D4, the only rotation seam)
  - `reconcile_in_flight`
  - DONE/FAILED handling
  - `# ── Launch new tasks ──` at L~2165
- The launch section:
  - builds `runnable = _order_runnable(parse_runnable_tasks(), now) + parse_prep_tasks()`
  - runs the gates (running / async-waiting / complete / parked / resume cooldown / B8 resource mutex / `_questions_ready`)
  - does P10B admission: `ResourceRequest.from_task`, `_admission_round(_counted_live, now)`, `admission.admit`, `_reserve_host_resources`, `admission.withdraw` + `admission_waits`, then `_record_admission_waits`
  - then branches to `launch_async_job`, the `kind: script` inline path, or `launch_implementer`
- W8 must reuse `_admission_round` / `_reserve_host_resources` / `_record_admission_waits` (L733–790). W10 touches `_live_counted` (L708) and `launch_async_job` (L428).

**`maestro/workflows/runner.py` (802 lines)**
- `WorkflowRunner(store, revision, *, run_id, repo, lifecycle, dispatcher, artifacts, is_alive, worker_argv)`
- `tick()`: `ingest_results` → `reconciler.reconcile` → `apply_skips` → for each `ready_frontier`: `claim` (raises `DispatchRefused`), then `dispatch` → `settle`
- `dispatch()` (L679) is the natural **single choke point**. Controller-scoped permissions go to `execute_inline`. Everything else goes `window_name` + `self._worker_argv(self, node, claim)` + `TmuxDispatcher.dispatch`.
- `claim()` (L542) sets the lease TTL and deadline from `node.timeout_policy.execution_timeout_sec`. W1 swaps that for `router.execution_deadline_sec(binding, node)`.
- `node_inputs()` reads an optional `self.inputs_provider`. That is W4's hook.
- `start_run(store, revision, *, repo, run_id=None, ...)` registers the task, stores the revision, starts the run, and returns the runner.

**`maestro/workflows/worker.py` (110 lines)**
- Argparse `--repo --run-id --node-id --attempt-id --operation-id --workflow-id --revision --workspace --worktree`
- It then does `handlers.resolve(node.handler_ref)(ctx)` → `_finish` (heartbeat, result, receipt). It never writes the control DB.
- W1's `ModelAgentRuntime` branch goes here.

**`maestro/workflows/handlers.py`**
- Registered refs: `agent, script, human, wait, noop, verification, merge, acceptance, candidate_select`
- `agent_handler` (L496) only adopts a workspace via `result_from_workspace`. It **does not launch a CLI**, so W1 must add the launch.
- `TmuxDispatcher(runner=subprocess.run)` is the test seam, and `tests/workflows/conftest.py::FakeTmux` records windows.

**`maestro/workflows/compiler.py`**
- `compile_workflow(spec, *, workflow_id, revision, policy, code_base_sha, execution_objective, backend_eligibility, project_profile_hash, catalog_snapshot_hash)`
- `head_sha(repo)` is at L725. `plan_repair` is at L988, `repair_counters` at L1136, `apply_repair` at L1167.
- ROADMAP block → `TaskSpec` goes through `maestro.taskgraph.normalize_spec(dict)` (L532).

**`maestro/backends/router.py`**
- `resolve_agent(node, context_manifest, resources, catalog, policy, *, statistics, now_epoch, trace)` returns `AgentBinding | RouteUnavailable`. Tell them apart with `.is_bound`.
- `RouteUnavailable.disposition` is `wait` or `block`.
- Seams: `dispatch_plan` (L801), `execution_deadline_sec` (L820), `token_account_key` (L832), `status_summary` (L837), `record_outcome(statistics, binding, *, success)` (L846).
- `RoutingContext.from_manifest` is at L271.

**Other locations**

| Symbol | Location |
|---|---|
| `metrics.binding_identity`, `ROUTE_RESOLVED` | L148–152 |
| `model_api.capability_entry` | L613 |
| `ModelAgentRuntime` | `model_runtime.py:434` |
| `build_context_manifest` | `repository/context.py:167` |
| `RepositoryQueries` | `repository/queries.py:126` |
| `select_affected_checks`, `ShadowComparison`, `qualify_affected` | `validation.py:1633`, `:1712`, `:1731` |
| `status._select_run` | L361 |
| `status.explain_view` | L539 |
| `ResourceRequest.from_node` | `scheduling.py:105` |
| `HostReservationLedger.reserve_counted`, `held_amounts` | `control/resources.py` |
| `ControlStore.open_controller(path)` / `open_client(path)` | `control_db_path(repo)` at `store.py:281` |

**Test harnesses to reuse**
- `tests/workflows/conftest.py` has `repo`, `store` (open_controller), `lifecycle`, `artifacts`, `FakeTmux`, `dispatcher`, `task_spec()` (normalize_spec), `revision`, `Worker`, and `dead_pids`.
- `tests/graph_engineering/conftest.py` has `lane_sandbox` and `run_lane`, which run the **real `orchestrator.main`** with every path global rebased and every seam doubled (`_SEAMS`, `_install_doubles`, `MAX_POLLS=12`). W0's "one poll opens a run, `launch_implementer` spy sees 0 calls" test belongs on this harness.

**W12 pointers (`maestro/cli.py`)**
- F2: `_derive_repo_facts` (L187) builds `str((git_root/".venv"/"bin"/"python3").resolve(strict=False))`. Drop `.resolve()` and keep the path absolute.
- F3: `_scaffold_file` is at L261. `.new` is written when dest exists and differs.
- F4: `_gitignore_block()` is at L327 and scaffolded at L694 through `_scaffold_file`, which is why it proposes a replacement.
- F5: the deny-list placeholder is in the `mapping` dict at L622–625, and `operating_preamble.md` is rendered at L659.
- The full F2–F5 evidence is in `artifacts/graph-engineering/b01-duetflow-pilot.json`.

**Thread #2 (selfupdate leak)**
- `tests/test_selfupdate.py::_sealed` (L59, autouse) sets `selfupdate.ENV_VAR` to `tmp_path/"maestro_home"`.
- `tests/conftest.py` L172–181 deliberately leaves `MAESTRO_HOME` alone, because `test_self_test_does_not_leak_this_machines_operating_pointers` asserts that a child sees it unset.
- `tests/test_graph_update_compatibility.py` sets `bootstrap.ENV_VAR` / `PROJECT_ENV_VAR`.
- Suspect module-level state cached at import (`selfupdate`, `bootstrap` or `paths`) by an earlier test, not the env var itself. Bisect with `pytest -p no:randomly <prefix-files> tests/test_selfupdate.py`.

**Repo quirks**
- pytest's summary line is suppressed in this repo. Get counts with `--junit-xml=<scratch>/r.xml` and read `tests`/`failures` off the root `testsuite`.
- `maestro/backends/catalog.py` is 48 KB, so grep it; don't cat it.

## Parallel-session coordination

- Branch `feat/graph-engineering-foundation` has no upstream, so `git pull` fails. Run `git log --oneline -3`. HEAD should be at or after `cc1aecf`.
- Edit `STATE.yaml` only with targeted replacements. At the end of this session, follow §6 case 2: `status: in_progress`, fill `in_progress_details`, and tick off the §4A threads S1 closed.
- Leave `docs/graph-engineering/specs/phases/P12A_*` alone unless §7 escalation leads Dan to change it.

## In scope / Out of scope

- **In (S1):**
  - `_p12a_open_threads` #2 (selfupdate leak)
  - W12 F2–F5 (`_p12a_open_threads` #3–#6)
  - W0: the `engineering.runner` selector in `config.py`, and the graph branch in `orchestrator.main`. That branch resumes open `task_runs`, runs `compile_workflow` + `start_run` per ready task, then `tick/ingest_results/settle` once per poll, and refuses a selector switch while a run of the other kind is active.
  - A `WorkflowRunner` dispatch choke point shaped for W1, even if W1 fills it later.
  - New `tests/graph_engineering/test_dispatch_wiring.py`
- **Out:**
  - W1–W11 (S2/S3)
  - Any Context Gate piece: no gate, no JSONL log, no evaluator, no policy bundle, and no new threshold rotation in the graph loop
  - P12 qualification
  - Flipping any default. `runner` stays `legacy`, and affected selection stays `shadow`.

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/test_selfupdate.py tests/test_cli.py tests/graph_engineering/test_dispatch_wiring.py --junit-xml=<scratch>/s1.xml
.venv/bin/python -m pytest -q --junit-xml=<scratch>/full.xml     # full suite, must exit 0 (thread #2)
.venv/bin/python -m pytest -q tests/graph_engineering/test_dispatch_wiring.py tests/test_host_resource_admission.py tests/test_workflow_status.py tests/workflows
```

Report:
- the junit `tests`/`failures` for all three runs, plus the full-suite exit code
- which test leaked into `test_selfupdate` and how isolation was fixed
- W0 evidence:
  - the `start_run`/`tick` spy count on a legacy poll (expect 0)
  - the `launch_implementer`/`launch_async_job` spy count on a graph poll (expect 0)
  - whether the graph poll opened a run and dispatched its first node
  - the refusal text for a selector switch while a run is active
- the F2–F5 acceptance results (venv path kept, no `state.json.new`, 56-line `.gitignore` fully kept, deny list rendered or doctor non-zero)
- the final `STATE.yaml` `status` / `current_phase`
