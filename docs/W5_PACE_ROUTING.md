# W5 pace routing

`objective: pace` is accepted by the project policy. The existing dispatch resolver
keeps eligibility, quality requirements and soft family independence, then checks
estimated five-hour headroom and measured class success before ranking weekly pace.
A pace wait never takes the legacy strength step-down fallback. Other objectives
retain their fallback behavior. Existing session selection remains the single
fresh/resume decision.

Five-hour headroom must be fresh, finite and current. Expired, future-sampled,
carried, absent or unusable measurements produce `quota_unknown`; an account
window at its hard limit or an estimate that cannot fit produces `quota_exhausted`.
Independent scoped windows survive refresh and decision serialization. Claude
subscription Opus candidates also require usable `scoped_windows.opus`; a full
scope rejects Opus alone. The scope is never charged to account usage or borrowed
by another pool. Malformed optional scope containers/durations preserve healthy
account/sibling data; malformed scoped reset values become unknown.

Initial estimates are explicit configuration, not measured consumption:

```yaml
routing:
  pace:
    initial_burn_pct_per_cost: 1.0
    initial_runtime_sec: 1800
    pools:
      codex-subscription:
        initial_burn_pct_per_cost: 1.0
```

Global and project settings use the existing deep merge. ROADMAP task `routing`
overrides merge last in both graph routing factories without mutating shared
defaults. Pool settings inherit the common initial values. Burn and runtime must
be positive finite numbers; doctor rejects invalid shapes, values and unknown
pace keys. The initial estimate is labelled `configured_initial_estimate`, with
zero measured coverage. Only the expected runtime fraction before the current
reset is charged by the five-hour check.

The factories read optional task overrides once from the current project's ROADMAP,
independently of frozen model choices. An absent ROADMAP or matching task retains
project defaults; malformed YAML blocks follow the existing scanner's skip behavior.
Other source-read failures and invalid override settings still refuse construction.
Actual task-spec use still requires its ROADMAP source.

The existing real claim path reads durable attempts on its controller connection
and can replace these initial estimates after five eligible observations. Runtime
uses completed successful attempts of the exact eight-field A07 shape in the same
pool; role/template/fragment changes never borrow that runtime. Burn uses the
recorded pair price and positive account-counter changes from source/window-consistent
300-minute observations that were fresh at their recorded read times. The sample
interval must agree with the attempt interval and must not overlap other known
activity in that pool. Unknown intervals, missing prices, malformed/stale/future
samples, source changes, scoped-only samples and reset crossings remain ineligible.
This is `estimated_nonoverlapping_pool_burn`, with explicit nonattribution; W2's
`quota_consumption` stays null. Counts, input references, exclusion reasons and
insufficient/unavailable fallback are serialized with the estimate.

The evidence guard uses verified outcomes and complete recorded W2 token costs.
Real claims read the latest append-only controller verdict by recorded timestamp
and row order; amendments replace the conclusion without rewriting history. A
succeeded attempt without a verified outcome contributes no success evidence.
Reconstructed routing and a reopened database connection retain the amended
verdict. Meaningful measured quality losers are removed first, then recorded cost
is compared within each surviving pool frontier, so a lower-quality cheap route
cannot also exclude its quality winner.

Own measured quality wins; otherwise an explicit canonical model identity and
matching native effort can share a quality prior across harnesses. The other A07
shape fields must still match. Recorded token costs and runtime remain local to
the exact own shape and pool. Each harness keeps its own frozen price and token
counts; raw costs are never compared across pools. Incomplete, zero or overflowing
token costs supply no cost preference. Nonfinite aggregate cost or success-per-cost
also stays unavailable with an explicit fallback, while usable quality evidence
remains eligible for the guard.

The separate nullable planner input is
`DispatchRouting.planner_complexity_reader(task_id)`. Both graph factories read
effective `runaway.mode` from global, project and task overrides, with task last.
The default is `off`, which uses class-only evidence and never calls the forecast
reader. In `on`/`shadow`, only matching integer forecasts 1–5 supply band evidence;
missing or invalid current/historical forecasts cannot borrow a band. An unavailable
forecast keeps routes unmeasured and eligible. Demand records the validated nullable
`planner_complexity`. For example, configure `runaway: {mode: "shadow"}`. This is
only W5's evidence input: no W13 producer, schema, brief, detection or control is
implemented, and the eight-field A07 shape is unchanged.

Replay separates quality coverage from own-pool cost coverage. Detailed quality
records carry their actual attempt IDs, contributing shapes, observation and
success counts, evidence floor and whether it was met. Off-mode aggregate
`OutcomeStatistics` can supply quality when detailed telemetry is insufficient;
its source is `verified_outcome_statistics`, with actual counts/shapes and null
attempt IDs because that aggregate does not retain them. It never supplies token
costs. Real pace resolutions refresh this aggregate from authoritative outcomes
as well as rereading detailed rows, so an in-memory fold cannot hide amendments
when the detailed attempt telemetry is absent. Existing per-pool `observations`
and `successes` describe detailed own-pool rows; the `success_*` coverage fields
describe the quality evidence actually used.

History reads project only consumed evidence and identity columns, but retain every
attempt, including unverified rows and unknown pool/interval activity needed for
burn overlap checks. There is no arbitrary last-N cap or cross-resolution verdict
cache. Reading and retaining history grows linearly with its size; the burn
overlap scan can be quadratic and repeats for each candidate. The history review
has preserved correctness, not established a hard memory or runtime bound.

After resolution, the sole graph dispatch path reserves the actual selected pair's
cost before claiming or launching it. The existing host ledger checks and inserts
the hold in one flock transaction, shared across projects and processes. Its
`budget:<account pool>` rows hold `relative_cost * burn_pct_per_cost * runtime_share`
percentage points against `target_5h - used_5h`; burn is applied once. The current
target is the resource pool's hard pause threshold (default 92%). Pools never sum
raw relative costs. Weekly and model-scoped observed hard gates remain independent;
a learned five-hour percentage rate cannot supply an unmeasured weekly/scoped rate.

Registered demand shares capacity by effective project priority (default 1).
Production reads the current validated config at each new selected-cost budget
transaction, including those of existing waiting runners. A successful committed
`/priority` command therefore changes their next admission without restarting or
reconstructing the runner. Invalid current weights are not replaced with the
factory's cached scalar: they follow the existing controller-error failed audit
path without dispatching a worker. Nonpace admission returns before reading this
input. An already admitted attempt's charge, binding and prior journal evidence,
and another project's held reservation, are not repriced by the control change.
When a peer waits below its share, new holds cannot take this project's share
above its fraction of the current cap. If no such waiting peer's own request fits
inside its share, the oldest waiter is admitted instead of every contender idling
until the starvation guard. Idle capacity can be borrowed when no peer
waits. Waiting priority gains one point per 600 seconds; the oldest demand waiting
1800 seconds prevents younger backfill until it fits. Running work is never
preempted. Unpolled demand expires after the 300-second lease, while an admitted
hold survives lease expiry over a live owner. These are coordination rules, not
a promise about work that has not yet registered demand.

The admitted `pace_budget` is frozen in the existing attempt binding. Tick recovery
heartbeats and reasserts running holds from that binding without readmitting or
repricing them; ordinary slot/count sync preserves budget rows. Result ingest,
blocked requeue, operator cancellation, failed launch, refused claim and controller
error cleanup release the exact attempt hold. Recovery and release follow that
admitted attempt even if the current routing objective changes.

The original partial charge, full five-hour estimate, expected runtime and start
time remain frozen in the binding and host row. Full estimates must be finite,
nonnegative and consistent with the actual partial charge. Same-window holds do
not decay with elapsed time. A fresh observed deadline advancing by more than the
existing 600-second reset jitter tolerance identifies a new window, including a
reset earlier than originally scheduled. Admission and project-share accounting
then use the remaining expected runtime before the new deadline, in the same
host lock. The original row stays held and its exact attempt identity is retained.
If a writer outlives its runtime estimate, the full frozen cost is held until real
settlement; legacy rows without a projection keep their known charge. These are
estimates of continuing exposure, not measurements of consumption. No weekly or
scoped percentage is inferred from the five-hour estimate.

Version 2 pace decisions carry ordered typed stage records for eligibility/hard
limits, soft independence, five-hour headroom, evidence, weekly pace and atomic
budget. Each record has `name`, `status`, `inputs` and `result`. Status distinguishes
passed, waiting, refused, blocked, skipped and pending work. A resolver selection
alone leaves budget pending; only the actual host transaction can pass or refuse
that stage. A quota wait skips the downstream guard/ranking/budget stages.

Eligibility freezes the catalog, role requirements, context and demand. Later
records retain actual survivors and rejected routes, family constraints/relaxation,
account/scoped observations, estimates and coverage/fallback, evidence frontiers,
weekly pool order and its tie keys, and actual pause/concurrency admission trials.
The budget transaction returns its real requested percentages, caps, reset/start/
runtime projection, priority, existing holds, registered demand and purged identities.
It records the shared exposure and fairness calculations actually reached, plus
the refusal or exact admitted reservation. A capacity refusal does not invent
unreached fairness calculations. Account and scoped units remain independent.

Before the sole claim, the runner mandatorily appends `PaceDecisionRecorded` to the
existing controller event store, keyed by run and node, with workflow/task/policy
identity, frozen context, complete resolution and decision. Both routing waits and
budget refusals survive reopening the store without an attempt or worker. Each
real retry evaluation appends its own event; historical snapshots are not rewritten.
Admitted attempts also retain the same completed decision in `binding_json`, through
the lifecycle's existing sole writer. An admitted event proves budget admission,
not successful claim or launch. Session mode is still decided once after claim
and recorded on the attempt. Existing nullable route-wait diagnostics stay optional.

If the mandatory event write fails, the node keeps its preclaim phase and reports
`pace_journal_unavailable`; the existing same-tick reconciliation releases an unused
hold. It creates no failed attempt, and a reconstructed runner can retry when the
store recovers. The host file and SQLite are separate durability boundaries: a
controller crash between ledger admission and event commit can leave an unclaimed
hold without a complete event. No worker can launch in that interval; existing
reconciliation removes that unused hold. There is no cross-store transaction or
second outbox. Waiting ledger demand is coordination metadata, not the journal.

Old version 1 names-only decisions remain historical records and do not establish
budget admission. Nonpace bindings/waits retain their existing serialized shape.
Nonfinite numeric snapshot inputs use an explicit `unavailable_number` marker
(`nan`, `inf` or `-inf`) so durable evidence is standard JSON. The original
observation and its unknown/refusal semantics are unchanged; the marker is never
a usable quota measurement or invented headroom.
New snapshots repeat catalog and decision inputs and have no history-size bound.
Sprint decisions and bounded controls now run through the existing graph quota
refresh and controller state writer. Effective global/project configuration is:

```yaml
sprint:
  enabled: true
  window_hours: 24
  behind_margin_pct: 5
  forecast_p24: 70
  forecast_confidence: high
```

Doctor rejects unknown keys, invalid booleans, nonfinite/out-of-range numeric
values and unsupported confidence levels. At runtime an invalid `sprint` block is
journaled once as `sprint_refresh_unavailable` and skips sprint evaluation; dispatch
continues. Sprint (B12, on by default) runs for every routing objective: it sends
start notices and pulls eligible polish and budget-only work forward in any project.
Only `objective: pace` also uses the sprint target in route ranking and the B7 budget.
A refresh whose decisions are unchanged apart from the evaluation time keeps the
stored decision and does not rewrite state. A second `/sprint stop` in the same
week adds to the pools already stopped rather than replacing them. A fresh usable weekly observation
within the horizon starts a sprint only when its elapsed-minus-used pace delta
meets the margin. Deadline corrections within 600 seconds keep the same control
week; a newly observed materially advanced reset identifies the next week.

Only the Codex subscription pool consumes the current reset-monitor fields.
An announced deadline and an available positive banked credit's expiry must be
future and inside the configured horizon. Forecast p24 and confidence must meet
the configured bar; a forecast has no announced reset deadline. Current source
freshness is checked again at evaluation. Stale/future/malformed signals and
historical events never become current quota. A banked decision sets
`activate_credit: true`; Maestro provides no activation or acquisition API.

The actual `/sprint on|off|stop|status` registry handles existing shared-bot local
mailbox deliveries and persists through the same authoritative state event and
atomic projection as the controller. Off stores the known current/coming weekly
identities once; with no known week it waits to bind the first usable observation.
Stop suppresses active sprints immediately. Unknown-week pools retain independent
pending skip/stop bindings even beside a known pool; each binds its first usable
week and re-arms on a later observed reset. Status includes active signals with no
weekly identity. On clears runtime overrides while leaving permanent
`sprint.enabled: false` intact.

Only sprint on/off/stop commands with a frozen control payload recover from a
claimed inbox row. Original raw command data stays immutable. Their original week
and pending-pool fields are applied after restart; they are never reinterpreted as
a new week's command. Receipts are scoped by project, shared bot and chat. Later
global controls supersede older global controls; pool button receipts protect that
pool from older global recovery without suppressing independent pool controls.
Status reads and unrelated Telegram updates do not advance these receipts. Other
operator verbs retain their existing fail-closed claimed-command behavior.

The existing graph refresh sends a tagged start notice through the shared Telegram
transport. Weekly resets, announced forced resets, forecast likelihood and banked
credit expiry have distinct explanations. Forecast notices never claim an
announcement; banked notices ask Dan to activate credit, without an activation API.
The Skip-this-week button affects only its notice's pool/week/episode, whereas
`/sprint off` applies to the whole project. A persisted random token binds the exact
project, bot, chat, delivered message ID, pool and week. Unknown or expired cached
weeks remain unknown and a successful button binds the next actually observed week.
The shared poller reads the exported projection only for addressing. The listener
acknowledges the spinner without state writes; the controller alone validates and
applies the durable inbox command. Restart, claimed/crash recovery, repeated taps,
different-project token collisions, independent-pool ordering and stale/foreign/
unbound delivery paths have synthetic coverage. Old buttons never skip a later week.

Notice intent is persisted before sending, and the returned message ID afterward.
Those are not an atomic external operation: a crash or lost receipt can cause an
orphaned duplicate notice and a retry. Only the exact durably bound message can
control the episode. No exactly-once external delivery is claimed. Notice/receipt
and forecast history has no size bound.

Typed snapshots freeze config, usable weekly input, normalized current signal
input, reason, control week and target. `target_pct: 100` denotes a weekly sprint
target; it never modifies observed quota or the five-hour hard limit. Forecast
episodes retain their actual input, start and 24-hour observation horizon. A fresh
weekly rollover within that horizon can set `real_reset_observed: true`; absence
or unavailable evidence stays null/unknown. These records have no history bound
and do not establish a live pilot or a negative "no reset" measurement.

The existing graph routing factory supplies a current controller-state reader to
the existing resolver. Only `objective: pace` consumes these decisions. For an
active own-pool decision with usable current weekly evidence, ranking uses
`target_pct - weekly_used_pct` instead of elapsed-week percent minus weekly use.
The target is never added to five-hour capacity. The observation remains immutable.
Inactive decisions cannot apply a leftover `target_pct`; off/stop/Skip-this-week
therefore take effect even before the next quota refresh. On re-enables evaluation
on the existing refresh path. An admitted attempt retains its frozen route and
session on restart, regardless of a later sprint target or control.

The decision's week must match the observed week within the existing 600-second
jitter tolerance. Future evaluations, inactive/foreign-pool/malformed targets,
expired deadlines and stale/future/missing current signal evidence cannot supply
a target. Absent, stale, carried, future-sampled, malformed or expired weekly
observations stay neutral in pace ranking. Unknown five-hour headroom still waits;
full five-hour windows, worse measured evidence, paused/saturated pools and the
actual atomic shared budget can still refuse the selected route. Other objectives
keep their prior weekly ranking and serialized decision shape.

The durable weekly stage freezes `sprints`, `observed_pool_paces`, `pool_paces` and
`sprint_targets` alongside its actual route order and admission trials. Its separate
observation remains the original measured input. Real admitted and budget-waiting
controller events survive reopening with these same inputs; admitted bindings retain
the same completed decision. Synthetic proof covers actual weekly, announced,
forecast and banked selection changes, immediate controls and exact skip buttons,
quality/headroom/pause/concurrency limits, real host budget refusal and restart.

Current valid own-pool sprint decisions also pull eligible work forward through
the existing graph loop. Before reading its normal runnable frontier, the loop
can stage accepted review follow-ups through `backlog.stage`; ordinary exhaustion
still stages them before the goal-completion gate. Ineligible items remain open.
Source holds, rejected/paused/parked state, operator waits, unmet prerequisites,
drift and goal/replan gates and other owners' resource mutexes are rechecked after
staging and at admission. Merged duplicates retain source provenance and gates.
Current ROADMAP source requirements take precedence over saved source metadata;
graduated sources keep the saved metadata. Mutex labels union and counted amounts
take per-dimension maxima for sequential follow-ups. Existing compiled nodes carry
task minima, preserving larger fragment requests. Stricter current polish-source
amounts feed the same admission round and host ledger on retries. Its own inherited
mutex does not block a waiting run; another live owner still does. An oversized
request records the existing W9 failed audit attempt, with no worker or reservation.

Already-open polish runs also participate with their effective current inherited
mutexes, even when their raw ROADMAP rows predate those source labels. The existing
host admission round retains a single selected owner across polls. It publishes
that owner's `mutex_labels` on its existing `graph_runs` state row before ticking
runners; the existing atomic state writer persists them. Restart restores ownership
only for matching live run identities, then the same admission round drops stale
owners. Active attempts and raw/frozen mutex ownership remain protected. Legacy
rows without this metadata prefer frozen resource owners, then active attempts,
then stable run age and identity. A compatible selected idle owner keeps retry
priority, including after an operator retry releases its attempt and through
restart; an older waiting peer cannot take a compatible bundle merely by waiting.
When idle inherited requirements converge, retaining partial ownership such as
x=A and y=B while both need {x,y} would prevent either from progressing. The same
admission round reconciles those incompatible idle allocations as whole available
bundles in stable run age/identity order, after protecting active/frozen owners.
A blocked idle contender receives no new partial bundle. Cancellation or settlement
releases ownership for a peer on the next poll. This uses the existing state and
admission seams, with no additional scheduler or resource ledger.

Synthetic live/restart regressions exercise converging idle bundles, compatible
owner retry, active polish and ordinary frozen ownership, current source gates,
and useful peer progress after real mailbox cancellation. These bounded cases do
not prove starvation freedom under arbitrary source edits or eventual progress
while a protected owner remains live or another admission gate remains closed.

Actual B7 refusals retain each node's selected pool and reason in
`admission_waiting.nodes`, alongside run identity and `budget_only`. Sprint orders
a run early only when its complete current ready frontier consists solely of
actual budget refusals for active sprint pools, with no active attempt, dispatch,
refusal or other wait. Saved reasons cannot erase a concurrent resource wait.
Explicit task priority and ordinary relative order remain. Every retry still uses
normal quality/headroom, host resources, shared budget, claim and launch paths;
off/stop/skip, changed holds and current quota apply immediately. An unreadable or
cyclic ROADMAP graph journals `sprint_order_unavailable` and retains ordinary run
order without bypassing eligibility or admission. Real loop/restart tests cover
these scheduler paths and actual goal-replan-store holds; this remains synthetic
proof with the qualification limits below.

Known limits: the host ledger's `budget` field ships under the same schema
version, so a controller still running pre-W5 code that rewrites the shared ledger
drops budget holds until every controller has adopted the release (only `objective:
pace` projects create them). A five-hour reset that passes between resolution and
the ledger transaction fails that attempt as a controller error rather than waiting.

Routing, config, band, canonical cost and amendment regressions are synthetic
evidence. The execution reports retain their exact source bindings and results;
an earlier affected pass does not qualify a later correction. W5 requires
independent review and controller-owned full/clean qualification before completion.
No live inference, pilot, release or push is implied by these tests.
