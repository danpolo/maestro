# P13 W5 Pace, Budget and Sprint Implementation Plan

**Source:** root ignored `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md`, binding B6/B7/B12/B13/B15/B19/B20 and W5/proof.

**Goal:** Dispatch approved routes by the accepted pace pipeline, share subscription headroom safely across projects, and use expiring capacity through controllable sprints without overriding holds.

**Authority:** root AGENTS.md; accepted W5 sections of `handoffs/2026-10-22-p13-w3-scoped-then-w5-next.md` and `handoffs/2026-10-22-p13-w3-qualification-w4-tests-w5.md`; accepted diagram `/home/dan/.agent/diagrams/maestro-post-p12-plan-2026-10-10.html` figures2/3/W5 seams; `docs/DESIGN.md`17.5; `docs/EXECUTION.md`; `docs/W3_USAGE_SOURCES.md`; `docs/W2_USAGE_REPORT.md`.

**Approval:** Dan's current request says finish clean02 and safe integration, "then accepted W5 automatically"; both W4 prerequisites were verified (the result file has since been deleted). Planning and implementation are authorized; no new start/design/merge approval is pending.

**Execution:** `executing-plans`, subagent mode, sequential implementation with a fresh independent whole-range reviewer. Controller owns preservation, qualification and integration. No parallel implementers on this branch.

## Global constraints

- New native-managed `/home/dan/.codex/worktrees/7539/maestro`, branch `feat/p13-w5-pace-budget-sprint`, exact base63f474d8a89680da57f83a95d85d5f52f9f5c937/tree42250a4681fd5783e6dab6bfd6f0df779b0c0c26. Retained b58a W4 and W3/other owners are immutable. New W5 baseline (entry files since deleted): 248blocks includes247prior/all242original.
- Approved final85 adoption (Dan,2026-10-29): original63 creation history and entry snapshots above remain immutable. Final clean root/W11a is85d394ad4d1d67cf3462499227cde49c8ebd0766/tree11d571f036932f701857d65d4d64cea269649f6a; W11a is finished and its branch/worktree remain preserved. Controller protected-merged that root into this W5 workspace before qualification, preserving both complete scheduling-test additions. Root85 is the eventual integration base; this does not qualify the combined source or consume any original gate. Authority: Dan's approval of 2026-10-29 (the approval note and the combined-source proof were kept as files that have since been deleted).
- HOST require_escalated, explicit `/bin/bash`, login:false, PYTHONDONTWRITEBYTECODE=1. Literal host apply_patch for source/tests/docs. Every Git mutation command-locally disables hooks. No push/release/live adoption/inference/authcontents/Context Gate/GDT/W6 work.
- One dispatch choke point and one existing fresh/resume decision. Resume retains harness/model. Pace may never weaken strength/effort/context/tools/permissions, bypass pauses, or recover a pool from bridge data. Existing native recovery source rules remain.
- Unknown/stale/future/malformed quota and scope data stay unknown; never fabricate headroom, burn measurements or current banked credits. W13 off uses task class; forecasts never become announcements. Unknown 5h headroom under the pace objective produces an explained wait rather than invented zero usage. Other objectives retain their prior behavior.
- W5-specific accepted B7/W5 formulas govern over the older illustrative diagram's unadjusted cost formula and its contradictory behind-pace caption. Charge is relative_cost * burn * expected-runtime share before reset; budget is (target-used)/burn in comparable units, with equivalent accounting for charges. Preserve independent account and model-scoped hard constraints. No cross-pool comparison or summation of raw prices.
- Existing W2 account deltas are not attributable consumption. Learn burn only from eligible, source/window-consistent nonoverlapping observations; otherwise retain an explicit configurable initial estimate. Record coverage/inputs; do not reprice history or weaken W2's reports.
- Persist control state using existing atomic controller/state and host-ledger mechanisms. Host budget check+reservation must be one flock transaction, with dead-owner purge, live-owner retention, exact attempt identity, heartbeat/restart reconciliation and release on every real terminal/refused claim path.
- Resource-contained evidence lives on persistent disk outside the repo; separate disposable TMPDIR/pytest basetemp from durable logs/XML/exits/source maps. Before any large run verify actual device/capacity and previous terminal scratch archival. Never cleanup unknown/shared/other-owner data.

### Task 1: Implement and prove the complete W5 behavior

**Depends on:** verified integrated W4 and new owned W5 baseline (complete).

**Files**
- Create domain modules `maestro/workflows/pace.py` and `maestro/sprint.py` as needed for reusable typed calculations/state; retain established repository structure.
- Modify `maestro/backends/router.py`, `maestro/backends/catalog.py`, `maestro/workflows/routing.py`, `maestro/workflows/policy.py`, `maestro/control/resources.py`, `maestro/orchestrator.py`, and the existing runner/telemetry seam only where real dispatch/settlement needs wiring.
- Modify existing `maestro/config.py`, `maestro/hitl/commands.py`, Telegram command/callback registry and scheduling/backlog seams where sprint behavior requires them; packaged defaults and doctor validation where current conventions require.
- Tests: create `tests/test_pace_routing.py`, `tests/test_pace_budget.py`, `tests/test_sprint.py`, `tests/workflows/test_pace_runtime.py`; extend the existing relevant route/resource/command/scheduling tests rather than duplicate their setup.
- Update `docs/DESIGN.md`, `docs/EXECUTION.md`, and a concise `docs/W5_PACE_ROUTING.md` with actual behavior/config/control contracts and explicit proof limits. Do not declare qualification before it happens.

**Consumes**
- Real `DispatchRouting.refresh_pace`, `router.resolve_agent`, `QuotaObservation`, W3 usage pool/scoped maps and reset signals, verified OutcomeStatistics, W2 immutable attempt telemetry, and the existing HostReservationLedger/controller lifecycle.
- Project/task effective config and priority; existing soft family independence, session decision, user holds, backlog/polish eligibility and command mailbox/callback routing.

**Produces**
- `objective: pace` recognized by policy/config/runtime and the actual production dispatch path.
- Ordered eligibility/strength/hard limits -> soft independence -> estimated 5h headroom -> per-class evidence guard -> weekly pace ranking -> atomic cost-weighted host budget admission. Unmeasured evidence remains eligible; meaningful measured worse evidence is excluded before pace. Preserve canonical model evidence equivalence with separate harness/pool costs.
- Typed, replayable decision inputs/results attached to the existing durable routing/attempt decision journal for both admitted and waiting cases, without a second dispatch or Context Gate mechanism.
- B7 reset-aware charge/cap calculation and priority-weighted shared host reservation, not a per-process cap. Runtime estimate is positive/finite and learned from eligible historical shapes with explicit fallback; projected overlap shrinks as reset nears. Lower headroom never raises budget; near reset alone never reduces admission. Checked capacity, fairness and reservation are in one lock transaction.
- On-by-default configurable use-it-or-lose-it sprint within about24h of a weekly reset and a configurable behind-pace margin. Codex current announcement deadline, sufficiently confident forecast(defaultp24>=70/high) and current banked credit expiry drive distinct reasoned sprint states. Forecast only adjusts target within hard limits. Banked credit prompts Dan to activate it; Maestro cannot activate it. History never creates current signals.
- Durable `/sprint off` (skip coming/current week once, reset re-arms), `/sprint stop`, `/sprint on`, `/sprint status`, and the start notice's Skip-this-week callback. Permanent config disable remains distinct. Pull only eligible polish or budget-only waiting tasks; never held/rejected/paused/unmet-prerequisite work. Sprint inputs/forecast outcomes remain measurable for the later pilot.

**Steps**
- [ ] Inspect exact controlling source and existing stable integration tests; record any implementation ruling and genuinely missing material decision before dependent work.
- [ ] Add one requirement-based failing behavior test at a time, run against old behavior, preserve real expected RED rather than import/setup errors. Implement minimal GREEN and broaden relevant tests.
- [ ] Verify full5h pool cannot win even when behind pace; worse-evidence route loses before pace; unavailable/scoped/expired/future data stays explained; independent family and quality floor/resume unchanged.
- [ ] Verify two real ledger instances/processes contend atomically, priority weights share budget, live owners persist/restart reasserts, and every terminal/refused claim releases the exact charge. Test cross-reset partial charges, learned/unavailable burn and runtime coverage, and no invented W2 attribution.
- [ ] Property-test deterministic grids: fixed reset horizon with increasing used% never increases cap; fixed used% with nearer reset never decreases admitted cost. Include boundary/nonfinite/zero inputs and actual runtime/controller integration.
- [ ] Verify weekly, announced, forecast, banked-current/history-only sprint lanes; off/stop/on/status and callback persist across restart; skipped reset re-arms; held tasks never pulled and forecast cannot exceed hard5h/budget.
- [ ] Focused command from W5 with root Python, explicit W5 PYTHONPATH and owned persistent TMPDIR/basetemp: `python -m pytest -o addopts= -p no:cacheprovider tests/test_pace_routing.py tests/test_pace_budget.py tests/test_sprint.py tests/workflows/test_pace_runtime.py` plus affected existing tests. Expected RED for each new observable requirement before its production change; then GREEN without unexplained warnings.
- [ ] Finish source/docs together, report exact test commands/exits, affected files, open concerns and source state; do not run a whole suite, commit, or integrate without the controller's owned reviewed qualification harness. Controller will run complete unfiltered qualification before committing. No test-results-only commit or provisional completion claim.

**Done when:** all accepted behavior is implemented through real dispatch/lifecycle/command paths, intended RED/GREEN and affected regression evidence retained, and one independent whole-range review clears blocking findings. Task1 remains pending until those facts exist; focused tests alone are not whole-project completion.

### Task 2: Qualify the exact W5 source and safely integrate

**Depends on:** Task1 source/docs and independent review.

**Files:** qualification/preservation bindings (outside the repo), runner/observer scripts with fresh immutable W5 labels; exact source docs completion record; root ignored phase status only after verification.

**Consumes:** Task1 source and review, immutable W5 entry baseline/registry, all retained W4/W3 evidence and owners, original qualification resource/identity/preservation contract.

**Produces:** reviewed source binding; genuine complete full suite/owner regression result before commit; exact committed-tree clean qualification; independent raw-result/archive checks; clean exact-pin integration under standing authorization.

**Steps**
- [ ] Derive a new explicitly W5-owned preservation guard from actual entry baseline; allow only exact owned candidate/root transition and exact qualifying fixture additions. Preserve all248baseline blocks/all242original and all failures/owners/refs/history/auth-METADATA reconciliation. Do not consume old W4/W3 mutation helpers against W5.
- [ ] Review new qualifier/guard transformations and source binding before large suites; retain original required floors1280MiB/250000inodes per-stage, runtime128MiB/20000ownedPID/PGID/stampstop, nice10/NPROC1024/AS8GiB, owner360/Maestro600, 8workers/chunk2/threadlimits, quiet60within600, real syntheticOFFLINE and exact JUnit/knownskip/source/harness checks. Counts change only from genuine collection of new tests. No silent adaptation or automatic retry.
- [ ] Namedagents TMUX suite, declared5s grace then real quiet/setup/first-action health, exactly1terminalobserver and one foreground terminal wait. No source/reviewer/defaultsandbox overlap during frozen qualification.
- [ ] Independently verify raw exits/XML/unique node outcomes/exact known skips/source/index/status/HEAD/harness and every lossless archive plus original links. Failure remains failed and all actual terminal-owned evidence preserved before diagnosis/retry.
- [ ] After actual full PASS commit only exact reviewed/qualified source with hooks disabled; bind review to exact commit/tree, then genuine clean qualification of that same tree under original gates. No manufactured pass or replay of historical W4/W3/E103.
- [ ] After both required passes/archives, clean root still exact approved integration base85d394ad4d1d67cf3462499227cde49c8ebd0766/tree11d571f036932f701857d65d4d64cea269649f6a, clean exact reviewed candidate pin and fresh preservation guard: command-local hooks-disabled exact-pin fast-forward; immediately verify clean shared tree and integrated guard. Preserve original63 history and every original qualification gate. No push/release/live action.
- [ ] Record actual work/proof limits and update ignored P13 W5 status. Historical W4 real-inference and later real itv pilot remain separate unticked evidence; no inference is authorized by synthetic qualification.

**Done when:** all accepted W5 synthetic behavior/review/full+clean qualification and standing-safe integration are verified, retained owners/history/failures intact, and any genuine Dan-only external decision is explicit. Do not call W5 complete before these gates.

## Review Focus

- Task1: five-hour/scoped constraints are distinct and precede evidence/pace; real dispatch tests cover full/unknown/reset-crossing/future data and strength/resume invariants.
- Task1: budget race/priority/starvation/restart/release; real two-project ledger contention and monotonicity tests bind the atomic check+hold, not helper-only arithmetic.
- Task1: no fabricated burn/cost attribution or class evidence; coverage/unavailability/canonical-equivalence tests and W2 existing regressions remain.
- Task1: historical monitor events cannot create current banked credit; forecasts cannot bypass gates; durable commands/rearm/held-task exclusion tested through real scheduler/control paths.
- Task2: exact source/commit pins and lossless archive verification survive failures, all prior owners/refs/registry blocks and historical failed qualification identities remain immutable.

## Next action

Start Task1 in one fresh implementer context with the accepted sources and this plan;
controller develops only read-only/preservation/qualification support independently.
