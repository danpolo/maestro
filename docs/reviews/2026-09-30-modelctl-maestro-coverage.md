# modelctl coverage of Maestro requirements

Reviewed 2026-09-30. Main conclusion: reuse modelctl for model registration,
discovery and lifecycle-table management. It does **not** implement Dan's requested
selection and replacement of Maestro's current model across projects. The missing
work belongs at Maestro's shared routing/default layer; adding another model tool
would duplicate working capability.

Dan clarified that the focus is Maestro-relevant coverage, especially changing
the model Maestro currently uses in all projects. General modelctl quality findings
are secondary to this integration question.

## Requirement coverage

| Agreed requirement | Evidence and current coverage |
| --- | --- |
| Argument-free interaction: select current Maestro model, then replacement | Missing. `modelctl/cli.py:build_parser` requires a subcommand; bare `modelctl` exits 2. `status` shows lifecycle-table rows, not effective project/task bindings. `add` selects unconfigured latest models, not a current Maestro binding to replace. |
| Fresh discovery across supported harnesses | Reusable for Codex and Claude. Fresh `discover()` metadata calls succeeded on this host without inference, global writes or notifications. Codex scanner uses account metadata; add candidates come from the bundled catalog. Codex add suppresses account-discovery errors into `account=None`, so its interactive output can lose the reason for degraded discovery. No Antigravity adapter exists in modelctl. |
| Context lookup, missing row handling, table ownership | Real Maestro `limits.resolve` and refreshed cache are checked exactly by `core/maestro.py`. Registration validates/persists user-supplied thresholds. The requested switch-time insertion from the selected harness's unknown-model row with an explicit customization notice is not implemented. Preserve modelctl ownership; its separate SPEC requires approval/supply of new lifecycle values rather than automatic activation. Any integration must reconcile that difference explicitly. |
| Persistent change to Maestro routing, including another harness | Missing. Provider file plans write provider limits/catalog or alias files and refresh one Maestro cache. They do not write a shared Maestro model policy, graph catalog or effective backend/role preference. A public CLI add in a disposable project succeeded while its project routing config remained byte-identical. |
| Change all projects inheriting Maestro's shared default | Missing. `Env` targets one `maestro_repo`, with a source path override for verification. No project inventory, inheritance provenance, chosen-default replacement or cross-project adoption mechanism. Cache refresh does not change the dispatched model. |
| Preserve/report deliberate project overrides | No override inventory/report. Existing project configs happen to be untouched, but a superseded model's table row can be removed while an explicit project pin still references it. DuetFlow has explicit `claude-sonnet-5` and `gpt-6-sol` pins; the installation doctor previously warned that their rows are missing. Preserve these pending an explicit decision; do not silently reinterpret them as inherited defaults. |
| Delay active projects; preserve immutable running versions | Missing in modelctl. No task/run/controller inspection or pending adoption report. Codex add can restart its daemon directly. Maestro's own `selfupdate.adopt` idle-boundary rule remains the mechanism for code-version adoption; it is not called by modelctl. |
| Validation, rollback and truthful completion | Strong reusable deterministic table/catalog/governor/resolver checks; tests cover rollback on verification/restart/explicit model rejection and retaining valid config on inconclusive inference. Completion says to restart open sessions; it does not report actual project updates, preserved overrides or delays. A later state-save exception is outside the transaction handler (secondary finding below). |
| Local disposable behavior, doctor/model-limit checks | 130 modelctl tests passed fresh. Additional public CLI probes exercised both add providers with real table/hook/governor/Maestro resolution, simulated discovery/inference/daemon. They prove registration, not a Maestro model switch. Full live account inference and daemon restart were deliberately not run for this comparison. |
| Reuse for future Telegram model selection | Provider/core separation is useful. A Maestro selection/use-case layer is still needed; Telegram model-switch UI remains outside the agreed original scope. |

## Actual Maestro consumers

- `maestro/orchestrator.py:_graph_routing` constructs the graph catalog from
  `SHIPPED_CATALOG_DEFAULTS` in `maestro/backends/catalog.py`. Those model IDs,
  strengths and costs are source defaults, not modelctl state or provider limits rows.
- Graph routing chooses a eligible model per node/objective/capability; a singular
  “current model” must be shown as effective bindings/preferences, not inferred from
  the list of context rows. Project `roles:` currently drives legacy/bounded calls,
  not this graph routing policy. Its relationship to graph preferences must be settled
  in the shared-default integration rather than promising that changing it changes a graph.
- The preserved uncommitted `maestro/limits.py` edits derive some legacy Claude default
  names from the newest installed family table row. This is partial automatic family
  default behavior; it does not implement selection of an arbitrary replacement, a
  cross-harness switch or the graph catalog. `implementer.py` and
  `selfheal/diagnose.py:JUDGE_MODEL` still have fixed model fallbacks.
- An installed project pin executes its immutable Maestro version. The current
  uncommitted catalog/limits edits are not adopted by DuetFlow and must not be
  presented as live routing coverage.

## Smallest remaining integration

**Ownership recommendation:** keep modelctl as the independent provider-management
tool. Add a small **Maestro-owned integration/use-case layer** for selection of
effective defaults, graph/legacy routing, project inventory, override provenance and
safe propagation. Put reusable discovery, registration and lifecycle transaction
hardening in modelctl; put orchestration semantics in Maestro. Do not create a third
standalone manager or make modelctl infer Maestro's run state and scheduling policy.
The local Maestro switch command can consume modelctl adapters through a documented
interface. There is no need to copy modelctl's implementation into this repository.

1. Persist one shared Maestro model/routing policy outside immutable source versions.
   Include model eligibility/strength/cost and the effective binding/preference concept
   needed by graph and legacy/bounded consumers. Do not duplicate lifecycle numbers.
   Resolve the existing `roles:` versus graph-policy relationship explicitly.
2. Add the local interactive switch use case requested on 2026-09-29: inventory effective
   current bindings, select the old binding/default, discover replacements through
   modelctl adapters, preview affected inheritors/explicit overrides/active delays,
   validate the whole backend/model/effort/limit change, then persist transactionally.
   Reuse modelctl rather than moving it into Maestro or editing catalog.py every switch.
3. Make all consumers read the shared policy with deliberate project overrides.
   Adopt changes at a task boundary; report actual updated, deferred, skipped and
   invalid-override projects. A retired/unsupported route must fail closed and permit
   the next eligible candidate. Test same-harness and cross-harness switches on
   disposable inheriting/overridden/active projects, including routing, doctor,
   rollback and a no-mid-task-adoption proof.

These are recommendations, not built/approved integration architecture. No shared
setting, project role override, runtime pin, daemon or global provider table was changed.

## Secondary modelctl findings

- `core/addflow.py:run_add` catches errors around file writes and `after_write`, but
  `plan.smoke()` and `plan.record()` run outside that handler. An injected deterministic
  state-save failure escaped while the new limits file remained changed. Harden this
  transaction boundary before making it the switch transaction's foundation.
- `core/scan.py:scan_provider` assigns access `VERIFIED` to a configured row if there
  is no prior access value, without inference. A disposable first-scan probe reproduced
  `VERIFIED` with zero checks. Configuration and account verification must stay distinct.
- The Codex picker catalog intentionally retains upstream historical visible models;
  latest-only registration is not latest-only routing/picker coverage.
- The earlier modelctl close handoff's timer-install request is stale: read-only host
  `systemctl --user` checks returned **enabled**, **active**, next run
  **2026-10-01 05:46:50 IDT**. Dan need not reinstall it on this evidence. modelctl itself
  is staged with no commits; no edits were made there in this session.

## Verification evidence

- Modelctl tests: from `/home/dan/projects/modelctl`,
  `PYTHONDONTWRITEBYTECODE=1 /home/dan/projects/maestro/.venv/bin/python -m pytest -q -p no:cacheprovider tests --basetemp=/tmp/modelctl-review-pytest-20260930`
  → **130 passed**, 3.96 s, exit 0. Running from Maestro initially collided with
  `tests.conftest`; the successful run used modelctl's cwd, no source changes.
- Bare installed command → required-subcommand error, exit 2. Installed `status`
  printed Codex and Claude lifecycle rows; its VERIFIED labels are recorded state,
  not new live inference evidence.
- Fresh direct provider discovery → Codex latest families `gpt-6.1-sol`,
  `gpt-6-astra`, `gpt-6-luna`, `gpt-5.6-terra`, `gpt-5.5`; Claude
  `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-4-5-20251001`.
  Sandbox DNS initially failed; host read-only retry succeeded. No `scan` was run
  against global state because it may send notifications and perform inference.
- Disposable probes: `/tmp/modelctl-maestro-review-smoke.py`, `.log`, `.json`.
  Codex and Claude public add flows returned 0 with real local resolution/governor
  checks. Two secondary gaps above reproduced. Discovery, inference and daemon
  side effects were simulated; no access-availability claim is derived from them.
- Context7 official `/openai/codex` docs confirmed that `model/list` reads supported
  models and may involve existing cache/refresh behavior; its name alone does not
  prove a fresh account call. Source:
  https://github.com/openai/codex/blob/main/codex-rs/app-server/src/request_processors/catalog_processor.rs
  No recommendation to replace the working discovery adapter was implemented.
