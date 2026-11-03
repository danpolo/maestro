# P13 qualification pilot protocol

**Status: approved by Dan on 2026-11-03.** This is how `docs/P13_ROADMAP.md` acceptance criterion 4 is measured. It has been applied to itv, and the pilot has not started.

## How the pilot starts

`~/projects/instagram-to-value/.orchestrator/HALT` keeps it parked. A live itv watchdog relaunches `maestro run` whenever no loop is alive, and both the watchdog and the loop stop on HALT. Lifting a task's hold does not start it; removing HALT does. While halted, the watchdog answers `/help` and `/status` and queues other commands, which run when the loop restarts.

1. A supervising session starts the agy refresher (see "Agy needs a fresh reading").
2. Dan runs `rm ~/projects/instagram-to-value/.orchestrator/HALT`. The watchdog launches the loop within about 30 seconds.

itv must already have adopted the release that holds the agy token reader. The loop adopts a new release only when idle, and with runnable tasks it launches first, so adoption is done explicitly before the start.

## Scope

The pilot runs on `~/projects/instagram-to-value` against its approved `docs/GOALS.md` (format 2).

| Task | `serves:` (from itv `docs/PLAN.md`) | Contribution | Criteria it advances |
|---|---|---|---|
| `02-acquisition-gate` | G1 | direct | AC-1, OP-1 |
| `03-selected-intake` | G2, G5 | G2 direct, G5 enabling | AC-2, AC-9, AC-17 |
| `05-setup-evaluation` | G2, G3 | direct | AC-2, AC-3, AC-4, AC-15, OP-3 |
| `06-safe-experiments` | G3, G4 | G3 direct, G4 enabling | AC-13, AC-5 |
| `07-approval-ledger` | G4 | direct | AC-5, AC-6 |
| `08-agent-executor` | G4 | direct | AC-6, AC-7, AC-9 |
| `09-operator-gates` | G1, G3, G4 | direct | OP-1, OP-2, OP-3, AC-5 |

`09`'s contribution is `direct` because the goals parser rejects any other value for an operator sign-off. `01-runtime-evidence` and `04-quota-adapter` were accepted on 2026-10-08 and are not re-run; `02` re-checks the report for staleness. Dan released the holds on `02`, `03`, `05`, `06`, `07` and `08`. `09-operator-gates` stays `hold: true`, so the first run stops there.

Two `kind: dan` verifications stop the graph before merge. A verification's request id carries its snapshot, so a restarted loop finds the same request on disk (`maestro/hitl/verify.py`; read from the code, not run):

- **`OP-1` on `02`:** without it, `03` would launch the moment `02` is accepted, before Dan's sign-off.
- **`PILOT-PIN` on `03`:** holds `05` until the temporary pin is off. It is removed after the pilot.

`03` also declares a `scope`, so `tests/**` compiles its `proof_review` node. The scope gate blocks changes outside a declared scope (`maestro/workflows/scope_gate.py`), so the scope spans the whole task and leaves out only itv's workflow configuration and unrelated directories.

## What must hold before the pilot

1. `~/.maestro/latest` includes the agy token reader (`bfcfad8` or later).
2. A full modelctl scan has published agy routes that resolve to real variant ids. The scan of 2026-11-03 05:44 Israel time did: every remaining route's ids appear in `agy models`.
3. `gemini-3-1-pro`, which that scan found removed, is out of the live routes. See "A removed model leaves routing by itself".
4. itv holds the approved documents, the objective, `serves:` on the pilot's tasks, and released holds.
5. The manifest is pinned: `docs/p13-pilot-manifest.template.json` is copied to itv's `.orchestrator/pilot-manifest.json` and filled in. itv's root `project.yaml` has `pilot: {manifest: .orchestrator/pilot-manifest.json}`. That directory is gitignored, so an automatic amendment cannot dirty the tree or block the goal gate's clean-tree rule. At close-out the manifest is copied to `artifacts/graph-engineering/p13-pilot/`.

## GOALS.md revision 2

Revision 1 could not pass the gate, so Dan approved a mechanical revision 2 on 2026-11-03. It removes 8 periods that hid `INV-1` to `INV-8`'s inline checks (`maestro/docs/goals.py:744`), moves the trade-off sentence unchanged to Non-goals, adds AC-7 to S-1's criteria, and adds a change-history row. The parser reports 0 gate and 0 draft problems on itv's committed file.

## How the verdict is computed

Apply these in order. The first one that fails sets the verdict.

| # | Check | Otherwise | Source |
|---|---|---|---|
| 1 | At least 2 main tasks accepted, matching the baseline's 2 | **Blocked, not failed** | `maestro report usage --task <id>` |
| 2 | At least 90% of agent attempts have tokens | **Not measurable** | same, `attempt_details[].measurements` |
| 3 | Tokens per accepted main task below 6.15 M, and work tokens per accepted main task not above 535,679 | **Fail** | same, summed over pilot tasks |
| 4 | agy completed one implementer node and one review node | **Not exercised**, Dan decides | same, `harness == antigravity_cli` |
| 5 | Zero `rate_limit` and `quota_exhausted` pauses | **Fail** | `.orchestrator/journal.ndjson` `pool_paused`, cross-checked with table `pool_pauses` filtered to `failure_kind` `rate_limited` and `quota_exhausted`, so the pin's `pilot_pin` rows do not count |
| 6 | One goal check ends with a typed verdict and a report | **Fail** | `maestro goals finish`, `.orchestrator/goal_outcome.json` and `.md` |

If all six hold, the verdict is **pass**. A pass is flagged when it came from moving work to a costlier route, because tokens are not quota: the same count costs more on Opus than on Sonnet. Tokens per route are shown beside the result.

On a slice, `maestro goals finish` is expected to return `incomplete`. A typed `incomplete` with its report counts as end to end. This reading comes from the code and has not been run.

Pass `--task` for each pilot task and sum. Without it the report covers the whole database and mixes in P12's 4 tasks.

### Why quota % and cost units are reported, not gated

Criterion 4 says "quota % and cost units per accepted task are below the P12 figures". Neither can be measured that way:

- **Quota %:** `quota_consumption` is always null and `observed_change_pct` is account-wide, so it cannot be attributed to one attempt.
- **Cost units:** P12 recorded no price and W2 forbids repricing history, so no P12 figure can exist. `cost_units` = `relative_cost` x tokens, per pool, never summed across pools. The pilot's figures become the P13 baseline.

## Baseline

P12's report left out the agy attempts, because the driver had no token reader. Their real counts are still in each attempt's `impl.log`, and the figures below add them. The database rows stay as they are because they are append-only.

- itv `att_run_Z3CTBZyR1D1RAMSG_implementer_04` (`01-runtime-evidence`): fresh 228,844, cache read 2,398,313, output 18,705, sum 2,645,862.
- DuetFlow `att_run_qG3zSNhcUbdHKyDm_implementer_03` (`07-reconciliation`): fresh 512,122, cache read 6,754,899, output 86,923, sum 7,353,944.

| | itv | DuetFlow (live database only) |
|---|---|---|
| Accepted tasks, main + followups | 2 + 2 | 3 + 3 |
| Tokens, all tasks, as the report shows | 12,286,052 | 13,217,471 |
| Tokens, all tasks, corrected | 14,931,914 | 20,571,415 |
| **Per accepted main task, corrected, main-task attempts only** | **6.15 M** | 5.36 M |
| **Work tokens (fresh + cache write + output) per main task** | **535,679** | 439,862 |
| Per followups task | 1.32 M, work 90,421 | 1.49 M, work 146,298 |
| `pool_paused` events | 0 | 11 |

The comparison is itv against itv, main tasks only. Cache read is 90% of the total, so work tokens get their own check. W7 moved followups to an end-of-project polish stage, so the pilot has no per-task followups tasks. Triage and polish spend are reported separately. DuetFlow is a reference only: 7 of its accepted tasks are in archived databases and some of that spend was never measured.

**Limits of this comparison:** 2 tasks is a tiny sample. One of them is an outlier (`01-runtime-evidence`, 5 implementer cycles). P12's tasks have no recorded class or complexity, while the pilot's are feature tasks of complexity 3 to 4.

## What is reported beside the verdict

| Number | Source | How to read it |
|---|---|---|
| Quota change | usage report | Account-wide, weak signal |
| Cost units per pool | usage report | Becomes the P13 baseline |
| W13 triggers per 100 attempts | events `RunawayTriggered` / attempts | Show samples per band: a band needs 10 samples, only forecast-stamped attempts count, and only failed attempts with retries left can trigger. Zero can be a plain lack of samples |
| W13 false-alarm share | Count `runaway_false_alarm` diagnoser verdicts | Do not use `false_alarms_per_task`: it also counts `deny_approved`, `permission_allowed` and replans (`maestro/improvement_metrics.py:52`) |
| W13 quota saved vs cost | Per acted trigger: retries stopped x payload `p90`, minus the diagnoser's tokens | An estimate, labelled as one |
| W5 pace decisions | events `PaceDecisionRecorded` | Exists only under `objective: pace` |
| W5 sprint | `state.json` key `sprint` | `real_reset_observed: None` means unknown, not a miss |
| W10 digest | `.orchestrator/digest.json`, journal `digest_failed` / `digest_undelivered`, the Telegram message | Its totals include P12 |
| W12 effects | Digest's improvement section | |
| Code under test | Manifest `amendments`, journal `pilot_manifest_amended` | |

## Agy needs a fresh reading

Pace admits an agy route only with a quota reading under 300 seconds old (`maestro/usage_sources.py`). A route is admitted before its session starts, so a session Maestro launches cannot supply one.

| Measured on 2026-11-03 | Result |
|---|---|
| Headless, the way Maestro launches agy (`-p`) | No status-line write, no quota in its output |
| Interactive, at startup | Writes real quota before any turn, at no token cost |
| Interactive, left idle for 15 minutes | No further write, and the reading expires |

So the supervising session runs `.orchestrator/pilot-agy-refresh.sh [minutes]` in itv for as long as agy should be eligible. It reopens a session every 180 seconds in an already trusted directory, never answers a trust prompt, closes its window each time, and ends at its bound. Without it, agy routes wait with `quota_unknown`.

## Temporary agy pin

Dan's answer, verbatim: "approve, temporarly pin agy at the right moment for a bounded part of the pilot at the right timing to test it, the pin shouldnt be part of the final product, just a part of testing in the pilot". Maestro has no way to require a harness (`maestro/model_policy.py`, `harness_drivers`), matching W1's "no fixed harness priority", so the pin works by removing the alternatives.

**Mechanism.** `.orchestrator/pilot-agy-pin.py` in itv is gitignored and absent from Maestro's tree and release. `on` writes a `pool_pauses` row with `failure_kind` `pilot_pin` for every non-agy pool, and `paused_until` (default 6 hours) means a forgotten pin ends by itself. `reconcile_pauses` retires only `quota_exhausted` and `rate_limited`, so it leaves these rows alone. They journal no `pool_paused`, so check 5 is unaffected. `off` uses `record_pool_recovery`, which keeps the history. Both append to the manifest's `amendments`. It refuses without HALT, a manifest, an agy pool or a fresh agy reading, refuses if already on, and the live lease refuses it while a loop runs. Run it with `MAESTRO_REPO=<itv> PYTHONPATH=$(cat <itv>/.orchestrator/current) <itv>/.venv/bin/python <itv>/.orchestrator/pilot-agy-pin.py on|off`.

**Task and timing.** `03-selected-intake`: low risk, complexity 3, and the first task after OP-1. `02` is high risk and stays unpinned. The supervising session runs the pin, because it also runs the refresher.

1. While `02` waits on OP-1: `maestro ctl halt`, wait for the loop to exit, confirm a fresh agy reading, pin `on`, remove HALT. Dan then confirms OP-1.
2. `03` runs on agy and stops at `PILOT-PIN`: `maestro ctl halt`, wait for the loop to exit, pin `off`, remove HALT. Dan then confirms `PILOT-PIN`, and `05` can launch.

**Claude via agy is exhausted until Wednesday 4 Nov 21:02 Israel time** (`3p-weekly` at 100%). Until then a pinned review can only wait, because the Gemini implementer rules out Gemini for independence. So switch the pin on only when `03`'s review will fall after that time, or set `--hours` to reach it.

**What each proof shows.** All ran on a scratch copy of itv against the real policy, never on its live database. Routes depend on the quota at that moment.

| Case | Implementer | Review | Proved by |
|---|---|---|---|
| `02` and `03`, no pin, real readings, Claude available | `claude-sonnet-5-5` on Claude | `gemini-4-argon@high` on agy, independent | Real readings, 05:54 on 11-03 |
| `03`, pin on, real readings | `gemini-3-8-flash@medium` on agy | Waits `quota_exhausted` until the `3p-weekly` reset | Real readings |
| `03`, pin on, agy pools with headroom | `claude-sonnet-5-5@medium` on `antigravity-3p` | `gemini-4-argon@high` on agy, independent | A stand-in reading, not a real one |
| Pin on, no agy reading | Waits `quota_unknown` | Waits `quota_unknown` | Real absence; the script now refuses this |

**Check 4.** Agy's review can happen unpinned on `02` with the refresher running, and the pin gives agy its implementer node on `03`. If neither completes, check 4 reads "not exercised" and Dan decides what that means.

## A removed model leaves routing by itself

Dan, 2026-11-03: a model its provider removed should not wait for a command. Until then, `maestro models consume` failed on every run once a routed model had gone, that backend's events stayed unacknowledged, and the only symptom was "handoff deferred" in `~/.local/state/modelctl/scan.log`.

`consume` now takes such a model's routes out, reads the outbox again and tells Dan. If the model is listed again, its routes return. If a role still needs it, it stays routed and Dan is alerted once. Details are in `docs/MODEL_TRANSITIONS.md`.

## QuotaPulse is not connected

QuotaPulse listens on the NetBird address rather than `127.0.0.1:8787`, and no Maestro process holds its token. W3's bridge-first path is therefore not live, and Maestro uses its native readers. Claude's Opus-scoped window may be unknown as a result, which has not been verified.

## Phase 02 can end the pilot early

`02-acquisition-gate` makes real requests to Instagram from the server's IP. One corpus failure stops the acquisition design and returns to Dan, and OP-1 is his sign-off straight after 02. If the pilot stops there, its verdict is **blocked, not failed**, and P13's criterion 4 stays open.

## Decisions

Dan's answers of 2026-11-03:

1. GOALS.md revision 2 is approved.
2. The new documents are committed to itv by explicit path.
3. The Maestro pilot builds this in the existing itv repo.
4. Phases 01 and 04 stay accepted, and 02 re-checks the report for staleness.
5. The holds are lifted on `02`, `03`, `05`, `06`, `07` and `08`. If `02` stops the pilot, the verdict is "blocked, not failed".
6. A separate Maestro Telegram bot. It already exists: Maestro loads only `~/.maestro/.env` (@MaestroGenericTestingBot), and itv's own bot (@insta2value_bot) is polled by no Maestro process. No update has been routed through that poller yet, so Dan's `/status` to it would prove the reply path.
7. itv's objective is `pace`. The `paid_efficiency` option was not chosen.
8. This protocol is approved, with the temporary agy pin above.
9. `03` gets a review, by declaring a test path in its `scope`.
10. `scripts/agents_lib.py` in itv keeps Dan's change, with its one test assertion updated.
11. Automatic W12 code changes stay manual.
