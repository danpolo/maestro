# Maestro — Execution Protocol

**Read this first. It is the process source of truth for building Maestro.**

## Current P13 execution boundary

The extraction programme below records M0–M6. For P13, root AGENTS.md, the current
phase spec and the approved slice plan govern. W1 slice 6 rechecks the model-transition
carryovers in an isolated candidate, including the pending slice5 completion docs. Its
qualification suite uses a disposable Maestro home outside the candidate source tree.
Use the canonical shared `maestro-setup` skill in `~/agent-skills`, with static validation
and isolated install checks for its narrow priority/shared-bot wording change.

Telegram credentials now come from the existing shared `~/.maestro/.env` (mode 600),
never a per-project setup prompt. One standalone `maestro telegram poll` owns network
updates; controllers and halted watchdogs consume durable local mailboxes. Tests use fake
credentials and injected HTTP boundaries. W1 slice 4 requires one combined concrete
rollout approval after suite/spec review before root integration/release, live polling
switch or one tagged smoke per project. That slice4 rollout is complete. Slice5's exact
candidate0f0d2f9 was explicitly approved through async on2026-10-12 and released through
the normal gate (5964passed/6skipped/1xfailed,534.50s,exit0). Stopped/locked idle adoption
installed eight reviewed global defaults, committed project.yaml only on both projects,
confirmed tagged notices185/186, and restarted/verified both watchdogs/controllers and
the sole poller. Effective differences0; all preservation contracts pass. Evidence:
`handoffs/2026-10-12-p13-w1-slice5-complete.md`. Slice6 preparation does not replay
model asks or alter live model values, policy or project pins. Its plan and ledger are
`docs/plans/2026-10-12-p13-w1-slice6.md` and
`handoffs/2026-10-12-p13-w1-slice6-ledger.md`. The historical unattended M4/M5
authorizations do not authorize later rollouts. Current W2 qualification is isolated
as described below. Other P13 workstreams retain their own authority and status.
Context Gate remains unbuilt.

W5 is in progress in its accepted isolated worktree, with no whole-W5 qualification
or completion claim. The implemented routing slice recognizes `objective: pace`,
checks current five-hour and applicable Claude Opus scope headroom, keeps the quality
floor on waits, and consumes explicit initial per-pool burn/runtime configuration
with task overrides. Real claims now learn eligible exact-shape runtime and estimated
nonoverlapping pool burn from durable attempts, with recorded prices and explicit
coverage/fallback. Canonical equivalents share verified quality while recorded
costs stay local to each harness and pool. Both graph factories consume effective
global/project/task runaway mode and a separate nullable planner forecast; there
is no W13 producer or A07 shape change. Quality replay identifies its real source,
counts, shapes and evidence floor, with null attempt IDs for aggregate statistics.
Real pace decisions refresh authoritative aggregate verdicts as well as detailed
history, preserving amendments even without detailed telemetry.

The lossless history read has no arbitrary cap or cross-resolution verdict cache.
Retained history grows linearly; burn overlap checking can be quadratic per
candidate. This review does not establish a hard history bound. The current B7 slice
reserves the selected five-hour estimated percentage cost under the existing host
ledger lock before claim; it shares capacity by project priority with waiting aging
and starvation protection. Existing runners now consume the current effective
priority on their next selected-cost admission after a committed `/priority`
command, without restart. Invalid current weights follow the existing failed audit
path without launching a worker or falling back to cached priority. Other objectives
do not consult this input. Already admitted bindings, charges and prior decisions
remain frozen. Different account pools, scoped hard gates and weekly
hard gates retain independent units. Durable bindings freeze admitted holds for
restart/reassertion, heartbeat and exact terminal/refused-claim release; controller
errors also sweep unused holds in the same tick. Original charges and runtime/full
five-hour projections stay frozen across restart. A fresh reset deadline advancing
by more than 600 seconds uses remaining runtime exposure for new-window admission
and priority shares; small deadline drift retains the same-window charge. Live
runtime overruns keep the full frozen charge, and legacy holds keep their known
charge. An objective change cannot hide or leak an already admitted hold.
Durable version 2 stage decisions now record actual inputs, survivors/rejections,
unreached stages and the ledger transaction's budget result. Mandatory preclaim
`PaceDecisionRecorded` events survive store reopening for both routing and budget
waits with no attempt; admitted bindings retain the same decision. Injected journal
write failures wait with no attempt/worker and no unused held cost, and restart can
retry. Budget admission remains distinct from claim/launch, and the separate ledger
and event store still have a preclaim crash interval without a complete event.
There is no cross-store transaction or new dispatch/session mechanism. Legacy
names-only decisions are not upgraded to admission proof; nonfinite snapshot inputs
use explicit unavailable-number markers without changing admission. Snapshots have no size
bound. Sprint domain/config and bounded controls now persist through the actual
graph quota-refresh/controller and Telegram command mailbox. Weekly behind-pace,
current announced, sufficiently confident forecast and current available banked
signals produce distinct frozen reasons. Controls survive restart; observed next
weekly windows re-arm skips/stops, while permanent config disable remains separate.
Forecast episode evidence keeps absent outcomes unknown and records only a matching
observed rollover as a reset. No credit activation/acquisition path exists.
Mixed pools with missing week identities now retain independent first-week skip/stop
bindings and reset rearming. Claimed sprint controls recover their frozen original
week across crashes; global and pool-scoped receipts preserve newer operator choices
without losing independent pool controls. Reasoned start notices and their
Skip-this-week callbacks use the real shared transport, addressing, listener,
mailbox, inbox and controller state. Buttons bind project/bot/chat/message/week/
episode; the button controls that pool, while `/sprint off` controls the project.
Synthetic crash/restart/stale/source/unknown-week tests cover those paths. Sending
and storing the delivery receipt are separate: an orphaned duplicate has no control.
Real graph routing now consumes the current active weekly sprint target, keeping
original observations and five-hour, quality and shared-budget admission intact.
Real selection changes for weekly/announced/forecast/banked reasons and immediate
off/stop/skip effects have bounded synthetic proof. Admitted and budget-waiting
events preserve actual sprint inputs and observed/effective weekly scores across
store reopening; existing attempts retain their original route on restart.
Unknown, malformed, expired or future weekly evidence cannot supply a sprint target;
nonpace selection retains its existing behavior. Current own-pool sprint decisions
now stage eligible polish before exhaustion and prioritize solely budget-waiting
ready frontiers through the existing graph loop. Current source holds, merged
provenance, prerequisites, goal/replan gates and other owners' resource mutexes
remain binding. Compiled node minima and stricter current source requirements use
the same host round/ledger; oversized requests record a failed audit attempt and
launch no worker. A run cannot block its own counted-resource retry on its inherited
mutex. Explicit task priority and every normal route/admission gate remain intact.
Current inherited mutexes of already-open polish peers now reach that same admission
round. One selected owner can retry while its peers wait; its ownership is stored
on existing `graph_runs` rows before dispatch and survives restart with no active
attempt, including after an operator retry. Cancellation or settlement lets a
waiting peer progress on the next poll. Real multi-poll, mailbox cancellation and
idle-owner restart regressions cover these boundaries.
Compatible idle ownership keeps its retry priority. If idle inherited requirements
converge into conflicting partial allocations, the existing admission round assigns
whole available bundles in stable run age/identity order. Active attempts and
raw/frozen owners remain protected, and a blocked idle contender receives no new
partial bundle. Bounded live/restart tests cover convergence, protected ownership
and peer progress after cancellation; they do not prove starvation freedom under
arbitrary source changes or progress while another gate remains closed.
A malformed/cyclic ROADMAP graph disables sprint reordering, with a journal reason;
ordinary eligibility and admission still run. Focused
synthetic proof does not establish a live forecast pilot.
The execution reports retain exact affected-run source bindings and
results; later corrections require fresh affected verification.
`docs/W5_PACE_ROUTING.md` records the current behavior and proof limits. Independent
review plus controller-owned full and exact committed clean qualification are required
before any W5 commit/integration; focused synthetic tests do not replace these gates.

W2's full source is qualified at `89e0817` on `feat/p13-w2-usage`, incorporating the
completed W3/W8/W9 base. Tasks 1–3 and both Important independent review findings
are closed with failure-first evidence. The frozen and clean exact-source full suites
each passed 6510 tests, 6 skipped, 1 xfailed, exit 0, without warnings. Contracts and
requirement evidence are in `W2_USAGE_REPORT.md` and `agent-plans/p13-w2-proof-inventory.md`.
`agent-plans/p13-w2-completion.md` pins the source gates and review resolution.

W2 completion uses an exact docs-only allowlist, frozen full suite before its isolated
commit and clean exact-commit full suite afterward. Run from the native W2 worktree
with root's Python, explicit native PYTHONPATH, private MAESTRO_HOME, cleared bridge/
reset/auth overrides and bounded named `agents` TMUX runners. Retain fingerprints,
status/index/binary diff and runner-owned exit/signal; no mutations while frozen.
Verify linked hooks skip release, root stays clean at `62f12a0`, all other refs and
the original saved P12 fixture survive. Final SHA/results and workspace archival/
cleanup are recorded in root `handoffs/2026-10-15-p13-w2-complete.md`. This authorizes
no root integration, release, push, live change, future W6/W11/W13 behavior or Context Gate.

Session continuation follows root AGENTS.md and the canonical close-session same-pane
helper, including verified pane/thread ownership; the historical 150K/overnight driver
procedure below does not govern current sessions.

You are the orchestrating session for a multi-stage build. Your job is to **run stages, not to write
code yourself**. Almost all reading and editing is delegated to workflow subagents so that your own
context stays small enough to carry the whole programme.

## Document precedence

| Document | Authority |
|---|---|
| `docs/DESIGN.md` | **Design.** What is being built and why. All decisions are locked in §2 — do not relitigate them. |
| `docs/EXECUTION.md` (this file) | **Process.** How stages run, gates, context rules. Wins over the plan on any process conflict. |
| `docs/plans/2026-08-09-m0-m1-core-extraction.md` | **Task detail** for M0–M1 only, including the authoritative function → module mapping. |
| `docs/PROGRESS.md` | **Live state.** Which stage is done, what was measured, what is next. Update it after every stage. |

---

## Prime directive: context discipline

You are building from a 4,567-line reference file with a 592-line `main()` at cyclomatic complexity
249. **If you read that file yourself, you will exhaust your context before M2.**

Rules for you, the orchestrating session:

1. **Never open `orchestrator_run.py`.** Not once. Subagents read it; you read their structured results.
2. **Never read an extracted module to check it.** Run the tests and read the pass/fail counts.
3. Prefer `Workflow` fan-out over doing work inline. Each workflow returns a small structured
   summary; the bulk of the tokens stay inside the subagents.
4. Between stages, write findings to `docs/PROGRESS.md` and commit. That file, plus DESIGN.md and the
   plan, must be sufficient for a **fresh session to resume with no other context**.
5. Measure, don't estimate: `python3 ~/.claude/skills/close-session/scripts/context_usage.py`.
   Check it after each stage. Past **150K**, finish the current stage, update `PROGRESS.md`, and tell
   the user to start a fresh session rather than beginning the next stage.

---

## Invariants — true for every stage

- **`/home/dan/projects/AbuAliArchive` is READ-ONLY until M5.** Its autonomous loop normally runs live
  (`orchestrator_run.py` + `watchdog.py`, tmux session `abuali-watchdog`) — **except that as of
  2026-08-09 the loop is deliberately paused by operator decision, via the HALT sentinel
  `.orchestrator/HALT`, until this extraction finishes.** See `docs/PROGRESS.md` and
  `handoffs/2026-08-09_pause-reference-loop-and-harden-invariant.md`. Never halt or resume it
  yourself, never write to it, never commit in it, and never touch the HALT sentinel — that is the
  operator's call, not the build's.
  It has exactly five pre-existing untracked files: `data/multivec_output_ft_v2.pkl.gz`,
  `data/reranker_onnx/`, `data/space_match_result.json`, `eval/gold_real_v1.json`,
  `handoffs/2026-06-28_p8-postmortem-diagnosis-and-plan.md`.
  **After every stage, check BOTH of the following. Either one showing a change means STOP the
  programme and report it — not repair it, not reason past it:**
  1. `cd /home/dan/projects/AbuAliArchive && git status --porcelain` — must show only the five files
     above, nothing else. This now explicitly includes `docs/UPCOMING.md`, `docs/dependency_map.md`
     and `docs/dependency_map.png`: earlier in the build these three were tolerated as the live loop's
     own regenerated artefacts, but with the loop halted they cannot change on their own, so **any**
     change to them is damage, not activity, and aborts the programme like anything else.
  2. Reference-project **runtime state**, independent of git: `stat -c '%s %Y'
     .orchestrator/state.json` compared against the last-recorded baseline in `PROGRESS.md`. Any
     change in size or mtime is an abort even when `git status` is clean —
     `.orchestrator/` is gitignored, so git will never see this category of damage. This is exactly
     the gap that let the M0 near-miss go unflagged; see the abort-conditions rule below.
- **Behaviour-identical through M1.** Bugs found are recorded in `docs/FOUND_BUGS.md`, never fixed.
- **Purity.** No project-identifying content in maestro. `tests/test_purity.py` enforces it and must
  stay green.
- **Runtime dependencies** limited to `pyyaml`, `python-dotenv`, `requests`.
- **Never write to `~/.codex/` directly.** That directory belongs to Codex; changes there are made by
  invoking the Codex CLI (the operator's delegate-to-agent rule).
- **Commits are made by you, not by fan-out subagents.** Concurrent agents racing on `.git/index.lock`
  corrupt the run. Fan-out agents create and edit their own files only and run **no git commands**.

---

## Testing — two tiers, not one

The suite is about 8500 tests. About 20 tests in `tests/test_cli.py` shell out to a real
`maestro init`/`run`/`doctor` subprocess (one of them pays a real 30s `POLL_INTERVAL` sleep). Those
are marked `@pytest.mark.slow` (registered in `pyproject.toml`; see `tests/test_cli.py`'s module
docstring for the convention — any new test that takes the `_healthy_project` fixture must carry the
marker too).

- **Iterating on a single change:** the test files it touches, or
  `.venv/bin/python -m pytest -m "not slow" -n auto`.
- **Phase/stage boundary, before a commit that closes an item, and always before merge/adoption:**
  `.venv/bin/python -m pytest -n auto` (no marker filter), the whole suite. `pytest-xdist` is a dev
  dependency. `maestro/selfupdate.py`'s `self_test()` runs the same self-update gate; never add `-m`
  there. The repo's `addopts` already has `-q`.
- **Flushing to disk is off in tests.** `tests/conftest.py` replaces `os.fsync` and the store's
  SQLite sync setting with stand-ins, because no test simulates a power loss. A test that asserts
  flushing or its order needs `@pytest.mark.real_fsync`, as the git durability modules have.
- Report collected count + exit code from the **full** run when a Done-when or Verification section
  asks for suite numbers — the filtered count is for your own iteration speed, not for reporting.

---

## The graph-engineering lane baseline

`tests/graph_engineering/test_baseline_lanes.py` is not an ordinary suite member. It runs the
real `orchestrator.main()` under instrumentation and records what each of the seven execution
lanes costs, so that P1–P12 can show what the migration changed instead of asserting it.

Two rules for anyone touching the event loop while that migration is in flight:

- **A changed lane number is a finding, not a test to fix.** If
  `.venv/bin/python scripts/graph_baseline.py --check` reports a lane differing from
  `artifacts/graph-engineering/baseline.json`, the loop's behaviour moved. Say what moved and
  why in `docs/PROGRESS.md`, then re-record the baseline deliberately — never re-record it to
  make the check green.
- **Never widen the fixture to make a lane pass.** The doubles in
  `tests/graph_engineering/conftest.py` stand in for seams that leave the process; a double
  that changes which *branch* the loop takes is not instrumentation, it is a different
  program. (This already bit once: stubbing `mark_roadmap_complete` left every completed task
  looking pending and the loop relaunched it, which read as an orchestrator defect and was
  not one.)

Assertions there deliberately pin behaviour the later phases exist to change — a script task
paying for a diagnosis when it parks, a refused proof review costing a duplicate invocation,
`read_state()` re-parsing `state.json` 22 times per ordinary task. Those are the baseline.

## Graph qualification (P12)

`tests/graph_engineering/test_end_to_end.py` drives whole tasks through the real
`orchestrator.main` under `engineering.runner: graph`, with fixture backends only: the happy path
per mode (subscription `agent_cli`, free model endpoint, hybrid, script), a parked run, the gate
refusing an uncommitted tree, a widened task landing only its candidate winner, a proof review
that rejects (the implementer reworks from its findings) or gives no verdict (fails closed), an
accepted task graduating so its dependent runs next, controller errors,
and the fault matrix (lost controller after the claim, writer crash mid-task, quota switch,
controller lost inside merge before and after the swap, acceptance commit failing part-way).
The runtime behaviour those lanes pin is in `docs/DESIGN.md` §16.

`scripts/graph_qualification.py` runs `tests/graph_engineering/test_qualification.py` (the six
catalog classes under subscription, plus one happy path each for free, hybrid and script) and
writes `artifacts/graph-engineering/p12-qualification.json`:

```bash
.venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends
echo $?     # check the exit code directly: stdout ends in a pytest line
```

- It refuses a target under `~/projects`, or a non-empty directory it did not create (it drops a
  `.maestro-graph-qualification` marker), unless `--deployment` is given. Without
  `--fixture-backends` it exits 2: there is no real-backend mode yet.
- The report's `summary`, `modes`, `invariants`, `fault_matrix`, `retention`, `experiments` and
  `context_gate_mapping` are the readable part; `lanes` is raw per-attempt telemetry (model,
  agent version, session, token split, quota windows, wall time, reset crossing, verification
  pass/fail; unmeasurable values are `null`). Read it with jq or python, not `cat`.
- Invariants a fixture run cannot check are listed with the tests that cover them, never
  counted as passed.
- **Every mode in the report is labelled unqualified** until a real pilot qualifies it, and only
  paid subscription is planned for that. A green fixture run is wiring evidence, not production
  readiness (P12 §3).
- The repo's pytest `addopts` already has `-q`; adding `-q` makes a run silent.

## Reliability carry-overs (P13 W8)

The design is in `docs/DESIGN.md` §16 "Reliability carry-overs (P13 W8)". What an operator
or a rollout needs:

- **A stuck graph node.** `maestro ctl retry <task_id> <node_id>` or
  `maestro ctl cancel <task_id> [<node_id>]`, from any terminal while the loop runs. The loop
  applies it on its next poll and says on Telegram what it did or why it refused. A refusal at
  the terminal lists the nodes that can be stopped now and exits 1. Do not edit the control
  database.
- **The command inbox.** `command_applied` / `command_rejected` in the journal show what the
  loop did with each watchdog or `ctl` request. A handler error other than a refusal is
  journaled but not sent to Telegram.
- **Publish a new policy shape only after the readers moved.** A controller whose code cannot
  read the shared `current` policy keeps running on what it adopted and journals
  `model_policy_unreadable` once; it adopts after idle self-update. That is a safety net, not
  the procedure: an incompatible publication still comes after every running project has the
  reader (2026-10-11, W1 slice 3: a reader-4 project stopped at graph startup). A project
  whose **adopted** revision is unreadable still stops, on purpose.
- **A held release.** `maestro release` retries a red head once before writing
  `~/.maestro/latest.held`; the first red is in the hook's `release.log`. A held sha is still
  never retested: remove the held file and release again. The self-test's child has its own
  empty `MAESTRO_HOME`, but the slow tier still starts real `maestro run` processes in the
  shared `agents` tmux session.
- **A paused controller does not adopt.** A controller parked in the `paused_by_user` poll
  loop adopts a new release only after it is resumed. By design.
- **A pilot's manifest.** Declare `pilot: {manifest: <path>}` in the pilot project's
  `project.yaml` before the pilot starts; each idle adoption is then appended to the manifest
  (`pilot_manifest_amended`). The pilot is not pinned.
- **Lanes.** `tests/graph_engineering/test_w8_settlement.py`, `test_w8_probes.py`,
  `test_w8_command_inbox.py`, `test_w8_operator_verbs.py`, and the W8 cases in
  `test_end_to_end.py` and `test_dispatch_wiring.py`.

---

## Unattended operation

**This programme runs overnight with nobody watching.** The operator has explicitly authorised, in
advance, all three actions that would otherwise need approval:

1. **Editing `~/.claude/` and `~/.codex/`** (M3). Authorised. The `~/.codex/` side is still performed
   by invoking the Codex CLI, never by writing into that directory.
2. **Using the Telegram dev-bot credentials** (M4). Authorised, via the secret-handling rule below.
3. **Halting and cutting over the live loop** (M5). Authorised, subject to the M5 safety protocol below.

Do not stop to ask for permission for any of these. Proceed, and record what you did in `PROGRESS.md`.

### Secret handling — the token is never read by an agent

The operator writes the dev-bot token to `~/.config/maestro/dev_bot_token` (mode 600) before the run
and sends one message to that bot from Telegram.

- **No agent — you or any subagent — may read, print, `cat`, `grep`, echo, log, or pass that file's
  contents anywhere.** Its path may be named; its contents may not be seen.
- Credentials reach a project only through `scripts/telegram_creds.py`, which reads the file,
  validates it, captures the chat id from `getUpdates`, and writes both into the target `.env`. It
  prints no secret.
- `.env` files are equally off-limits to reading. If a test needs credentials, it monkeypatches the
  network call — a test that reaches `api.telegram.org` is a defect.

### Abort conditions — stop the programme, do not try to repair

Stop, write `PROGRESS.md`, commit, and leave a clear report. Do **not** improvise around these:

- Anything appears in `AbuAliArchive` beyond its five known untracked files (before M5), **or its
  runtime state changes** — `.orchestrator/state.json`, or anything else under `.orchestrator/`,
  shows a different size or mtime than the last-recorded baseline in `PROGRESS.md`. Check both; do
  not rely on `git status` alone — `.orchestrator/` is gitignored and git will never see this
  category of damage.
- **Any write to the reference project's runtime state is an immediate abort.** Full stop, no
  exceptions: regardless of whether `git status` notices it, regardless of whether the write looks
  reversible, and regardless of whether you can show the data was not actually lost. "No data was
  lost" is a finding to report, never a reason to keep going — the M0 near-miss was reasoned past on
  exactly that basis and it was wrong to do so.
- A stage's tests are still red after **three** attempts. Do not weaken, skip, delete, or `xfail` a
  test to make a stage pass. Never lower a threshold to hit a target.
- M5 post-cutover verification fails **and** the automatic rollback also fails.
- Any operation would require `sudo`, force-push, or history rewriting.
- A destructive step has no verified rollback.

### Autonomy rules

- Prefer stopping a *stage* over stopping the *programme*. A failed M4 should not prevent
  `PROGRESS.md` from being written, and M5 must not begin unless M0–M4 are green.
- Never fabricate a verification result. If a command was not run, the stage is not done.
- Bugs found in reference code are recorded in `docs/FOUND_BUGS.md`, never fixed (through M1).
- If genuinely blocked on a judgement call, pick the **more conservative** option, proceed, and flag
  the decision in `PROGRESS.md` for morning review.

### M5 safety protocol — the live loop must survive

M5 touches the operator's running production loop. In this exact order, no shortcuts:

1. **Precondition:** M0–M4 all green. If not, skip M5 entirely and stop.
2. Capture and commit a pre-cutover baseline: `orchestrator_status.py` output, `state.json`,
   `git rev-parse HEAD`, and the running PIDs.
3. Write the rollback script **first**, and dry-run it. No rollback, no cutover.
4. Confirm the loop is idle (no entries in `in_flight`). If a task is in flight, **wait** — poll up to
   60 minutes, then skip M5 and stop. Never halt mid-task.
5. Halt via the HALT sentinel file only. Never `sudo`, never `kill -9` the watchdog.
6. Cut over on a branch. Verify: `/status` matches the baseline, one supervised task runs end to end.
7. On any verification failure, **run the rollback immediately and automatically**, then stop.
8. **Whatever happens — success, failure, or abort — the run does not end with the loop halted.**
   Removing the HALT sentinel and confirming `pgrep -f orchestrator_run.py` returns a PID is the last
   action of M5 in every path.

---

## Stages

Each stage: author a `Workflow` script, run it, verify, update `PROGRESS.md`, commit.
Workflow size guideline for this machine: **under 15 agents per workflow.**

### M0 — Package skeleton and characterisation harness
- **Spec:** plan Tasks 1–4.
- **Shape:** serial, inline or a small workflow. It is only four files.
- **Done when:** `pytest -v` green; harness runs a test body against the reference implementation and
  skips the maestro side with `not extracted yet`; the credential-leak test passes.

### M1 — Core extraction
- **Spec:** plan Tasks 5–18, and its **Function → Module Mapping** table (authoritative).
- **Shape:** this is the big one. Two waves per module group, pipelined:
  - *Wave A (parallel, safe):* one agent per module writes
    `tests/characterization/test_<module>.py` against the reference implementation. Different files,
    no conflicts. Agents run tests but **do not commit**.
  - *Wave B (serial, dependency-ordered):* one agent per module copies the mapped functions verbatim
    into `maestro/<module>.py` and gets both subjects green.
  - Dependency order is fixed: `state → config → docs/roadmap → worktree → quota → gates →
    implementer → merge → hitl/telegram → hitl/commands → selfheal → parking → orchestrator`.
    A module may import only from modules extracted before it.
- **Batch it.** Do not attempt all thirteen modules in one workflow. Three batches, verified and
  committed between each:
  `[state, config, roadmap, worktree]` → `[quota, gates, implementer, merge]` →
  `[telegram, commands, selfheal, parking, orchestrator]`.
- **Critical instruction for every extraction agent:** *copy function bodies verbatim; change only the
  import block. Do not reformat, rename, simplify, or fix anything. If the reference code looks wrong,
  append a line to `docs/FOUND_BUGS.md` and copy it wrong anyway.*
- **Done when:** full suite green with **zero skips** under `tests/characterization/`;
  `tests/test_extraction_complete.py` 12 passed; `main()` moved unchanged.

### M2 — Backend drivers and mid-work switching
- **Spec:** `docs/DESIGN.md` §6 and §7. No detailed plan exists — **write
  `docs/plans/<date>-m2-backends.md` first**, using the M0–M1 plan's format, then execute it.
- **Shape:** a research fan-out then implementation.
  - Parallel research agents (they must return findings, not opinions): *what `codex exec` supports
    for non-interactive runs, model selection, sandboxing, and whether a stable resume handle exists*;
    *how the Codex statusline exposes `context-used` / `five-hour-limit` / `weekly-limit` and whether
    it can be sampled*; *exactly how the reference implementation detects Claude quota exhaustion and
    parses the reset time*.
  - Then: `backends/base.py` protocol → `claude.py` at parity with the reference behaviour →
    `codex.py` → `roles.py` → `switch.py`.
- **The parity test is the point:** one protocol test suite that both drivers must pass, with
  capability-gated cases skipped rather than special-cased by backend name.
- **Open question to resolve here, not guess:** if `codex exec` has no stable resume handle, the Codex
  driver declares `native_resume = False` and always re-briefs. Record the finding in `PROGRESS.md`.
- **Done when:** both drivers pass the shared protocol suite; a forced switch on a scratch task
  succeeds in both directions with work preserved in the worktree.

### M3 — Model limits and self-update
- **Spec:** `docs/DESIGN.md` §8 and §9.
- Extract the ceiling tables out of `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` into
  `model_context_limits.md` beside each, leaving a pointer line in the original. **Claude side: edit
  directly. Codex side: invoke the Codex CLI.**
- Build `limits.py` (parse + cache + fallback) and `selfupdate.py` (version worktrees, `current`
  pointer, task-boundary adoption, self-test gate, rollback).
- Replace the hardcoded context constants carried over in M1 with per-model lookups.
- **Done when:** `doctor` resolves every model in both tables; a deliberately-red candidate version is
  refused and `current` stays put.

### M4 — Setup: `init`, `doctor`, skills
- **Spec:** `docs/DESIGN.md` §10.
- Build `cli.py`, `templates/`, and the setup skill in both Claude and Codex formats.
- **Acceptance is on a throwaway project**, created fresh under `/tmp` — never on a real one.
- Credentials come from `scripts/telegram_creds.py` per the secret-handling rule. If the token file
  is absent or rejected, build and unit-test everything else, mark M4 `blocked: no credentials` in
  `PROGRESS.md`, skip M5, and continue to report.
- **Done when:** a brand-new git repo goes from nothing to a completed no-op supervised loop via
  `maestro init`, and re-running `init` is provably non-destructive.

### M5 — Cutover of the reference project
- **Spec:** `docs/DESIGN.md` §11, executed strictly under the **M5 safety protocol** above.
- HALT via the sentinel file (no elevated privileges), branch, install, delete the superseded scripts
  listed in DESIGN.md §11, resume.
- **Done when:** `/status` output matches the pre-cutover capture, one supervised task completes end
  to end, and `pgrep -f orchestrator_run.py` returns a PID.

### M6 — Live switching validation
- **Done when:** a real threshold trigger produces a real backend switch on the migrated project, and
  the journal shows the `backend_switch` event.

---

## Per-stage loop

1. Read the stage spec (this file plus the referenced DESIGN section). **Do not read source files.**
2. If the stage has no detailed plan, write one to `docs/plans/` first.
3. Author the `Workflow` script. Give every agent: the invariants above, its exact file list, its
   verification command, and "**do not run git commands**".
4. Run it. Read only the structured result.
5. Verify yourself with the stage's Done-when commands, plus the AbuAliArchive read-only check.
6. Update `docs/PROGRESS.md` — measured numbers, findings, open questions. Commit.
7. Check your context. Past 150K, hand off instead of continuing.

## Reporting back

The operator is asleep and will read this in the morning. When you stop — at the context ceiling, an
abort condition, or completion — leave a report in `docs/PROGRESS.md` **and** as your final message:

- Every stage reached, with its measured verification numbers (test counts, not adjectives)
- The state of `AbuAliArchive`: HEAD, `git status --porcelain`, and whether the loop is running
- Anything added to `docs/FOUND_BUGS.md`
- Every conservative judgement call you made that deserves review
- Anything that failed, verbatim, with no softening
- The exact next action

Never report a stage complete without having run its Done-when commands and seen the output. A
morning report that overstates progress is worse than one that reports a clean abort.

### Context and continuation — you are one link in a chain

**You are not expected to finish the programme.** `scripts/run_overnight.sh` runs a chain of fresh
sessions: when you exit, it launches a brand-new session that resumes from `PROGRESS.md` with a clean
context. Stopping early costs nothing; degrading in the dumb zone costs a lot.

So: at **150K**, finish the current stage, update `PROGRESS.md`, commit, and **exit**. Do not try to
squeeze in another stage. Do not compact.

Before exiting — on every path, including an abort — set the `PROGRAMME-STATUS:` line at the top of
`docs/PROGRESS.md` to exactly one of:

| Value | Meaning | Driver does |
|---|---|---|
| `IN-PROGRESS` | more stages remain | relaunches a fresh session |
| `COMPLETE` | all seven stages verified green | stops |
| `ABORTED` | an abort condition fired | stops |

Only set `COMPLETE` when every stage in the Status table is done **and you have seen its Done-when
commands pass**. Setting it early silently ends the build and the operator wakes to an unfinished
project reported as finished.

The driver also stops on its own if two consecutive sessions leave `PROGRESS.md` unchanged — so a
session that achieves nothing must still record *why* in Findings, or the chain will halt.
