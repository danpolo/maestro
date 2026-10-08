# Resume the DuetFlow P12 pilot after the gpt-6.1-sol routing release

> **2026-10-08 (later):** steps 1–3 are committed (Maestro 0c7eb92, modelctl 61c9de0). Continue from
> `handoffs/2026-10-08-modelctl-release-continue-next.md`: 2 owner-suite failures block the release.

> **2026-10-08:** Dan approved commit + release + DuetFlow-only adoption. Start from
> `handoffs/2026-10-08-modelctl-release-then-pilot-next.md`; it uses this file for the pilot-resume steps.

## Goal

Dan decided on 2026-10-07 to keep DuetFlow **HALTed** until a Maestro release whose graph
routing covers `gpt-6.1-sol` has been adopted. When that release exists, adopt it at the
idle boundary. Then resume exactly one normal pinned controller on DuetFlow's queued work
and collect the pilot evidence that D30, D31 and D33 are waiting on.

**Status 2026-10-08:** the fix exists but has not been released.
- The qualified D-A–D-E candidate is at `.scratch/worktrees/modelctl-integration`,
  `feat/modelctl-integration`, base a40cafc. Its gate: Maestro 5537 passed / 1 xfailed.
- Its `catalog.py` lists `gpt-6.1-sol` (strong, 1.0) and marks `gpt-6-sol` superseded.
- It is uncommitted (62 paths). There is no release under `~/.maestro/versions` and no
  adoption.
- The external modelctl tool's patch was applied on 2026-10-07
  (`docs/plans/2026-10-07-modelctl-owner-applied.md`), but that record explicitly does
  **not** authorize the Maestro commit, release or rollout. Dan must authorize those first.
- After the release is adopted, D-A makes DuetFlow's exact pins class choices. Step 1's
  pin-mapping question still applies.

**Do not start until that release exists.** If it doesn't exist yet, report that the pilot
is still on hold and stop. Don't clear HALT on f13b2c7. Don't build the release yourself
from this handoff, because the modelctl-integration line owns it.

## Read first

- Root `AGENTS.md` and the Maestro auto-memory index. These memories apply:
  build-loop/watchdog, permission lists, model pins class-level, context management not built.
- `handoffs/2026-10-01-duetflow-pilot-resume-next.md`: the previous resume plan. Its steps
  5–6 and its "Verification to report back" list still apply.
- The latest `handoffs/*modelctl*` handoff and `docs/plans/2026-09-30-modelctl-integration.md`,
  "Dan's decision changes, 2026-10-01" (D-A–D-E). These say what the release changes,
  including class-level pins (D-A).
- `docs/graph-engineering/STATE.yaml` `_p12_open_threads`. The first thread is this hold;
  the D33 thread comes next.
- `artifacts/graph-engineering/p12-pilots/manifest.json` → `amendments` (last three are from
  this session).

## Why the hold (step-3 finding, 2026-10-07)

- The installed f13b2c7 graph routing (`maestro/backends/catalog.py`
  `SHIPPED_CATALOG_DEFAULTS`, used via `orchestrator.py` ~l.1973) reads only its hardcoded
  catalog. It never reads `~/.codex|~/.claude/model_context_limits.md` or project.yaml `roles:`.
  - Codex `available_models`: gpt-6-astra (strong, 2.5), gpt-6-sol (strong, 1.0),
    gpt-6-luna (balanced, 0.5), gpt-5.6-luna.
  - Claude: opus-5-5 (strong, 1.25), sonnet-5-5 (balanced, 0.5), haiku-4-5.
  - Under paid_efficiency, strong nodes (proof review, diagnoser) therefore go to
    **gpt-6-sol**, not to the approved pin `gpt-6.1-sol`.
- project.yaml `roles:` binds only the legacy paths and graph `/ask`. In f13b2c7,
  `hitl/ask.py` takes the model from `roles.implementer`, which is now claude-sonnet-5-5.
- gpt-6-sol was still on Codex's server list: `~/.codex/models_cache.json` was fetched
  2026-09-28 and lists gpt-6-astra/sol/luna. `gpt-6.1-sol` exists only in the
  hand-edited `~/.codex/model_context_limits.md` (written 2026-09-30) and `~/.codex/model_catalog.json`.
- What a retired-model launch failure is classified as remains unverified.
- Dan chose "Hold until fixed" over "resume on f13b2c7 as-is".

## Done this session (2026-10-07)

- State snapshot (read-only, 10:00 IDT):
  - HALT is set (file from 2026-09-30 13:01). `.orchestrator/current` → f13b2c7. The global
    pointer is unchanged (2f555c28).
  - task_runs: 1 succeeded (07); 0 active runs; attempts 10 succeeded and 4 failed, 0 active.
  - `writer_leases` has one row: controller, PID **1755214**, claimed 2026-10-01T05:12Z,
    expired. Host `ps` shows that PID and the old 558897 are both dead. The row probably
    came from the modelctl session's doctor run. Leave it to normal acquisition and never
    delete it with SQL.
  - No `maestro run`/`watchdog` process (pgrep). The tmux sessions are all unrelated to DuetFlow.
  - Queue: `07-reconciliation-followups` (autonomous, hold false), then `08-timers-notify`
    (needs-dan, so manual prep). `03-source-playlist-reader-followups` is open but
    `hold: true` (Dan 2026-09-27, end-of-project polish).
- DuetFlow commit **6c0e66c**: project.yaml pins (implementer claude-sonnet-5-5 / gpt-6-luna;
  judge and diagnoser claude-opus-5-5 / gpt-6.1-sol). Committed alone, per the 2e4cde1 convention.
  `docs/dependency_map.md` is still the pre-existing uncommitted modification; it is preserved.
- Maestro commit **c3f8fd4** on `feat/graph-engineering-foundation` (manifest.json only) adds
  three amendments:
  - `code_under_test` ead3819→f46ffd4 (2026-09-30T12:14:06Z) and f46ffd4→f13b2c7
    (12:47:34Z). Both were idle HALTed adoptions with no run, and neither had been recorded
    before.
  - `operator_config` for the pin change, with a note about the hold.
- STATE.yaml: a new hold thread at the top of `_p12_open_threads` (STATE is gitignored).
- Nothing paid ran. No controller was started. HALT was never touched.

## Steps (once the release exists)

1. Confirm that the release exists and is qualified, from the modelctl owner's handoff and
   evidence: a committed SHA, a passing gate, and graph routing that can pick `gpt-6.1-sol`
   or a class that resolves to it.
   - Check with the release's own code, read-only and unpaid, what a strong DuetFlow node
     would bind to.
   - If D-A class pins land, decide with Dan, **once**, how DuetFlow's exact pins map to
     classes. Bundle that with any other material question.
2. Re-snapshot the step-1 facts above. Never assume the 2026-10-07 snapshot is still current.
3. Adopt the release for DuetFlow only, through the supported `selfupdate.adopt(project_repo=…)`
   path at the idle boundary.
   - Don't change `~/.maestro/current` unless Dan asks.
   - Record a `code_under_test` amendment (f13b2c7→new SHA, reason, criteria_or_weights_changed false).
4. Run the pinned doctor; it must exit 0. Expected known warnings: missing watchdog systemd
   unit and meta origin. Report any new warning.
5. Clear HALT and start **exactly one** controller through the normal project workflow.
   - That workflow is `launch.sh`: the `duetflow-watchdog` tmux session running `maestro
     watchdog`, which launches `maestro run` in the `agents` session.
   - The systemd unit isn't installed. Any privileged install goes through one script that
     Dan runs.
   - Before launching, check host processes as well as tmux windows. On 2026-09-30 a stale
     watchdog was found that the window list alone missed.
6. Monitor without idle polling. On a real waiting `/ask`, give Dan the exact bot
   (@MaestroGenericTestingBot), the message and the expected reply (D33).
   - Record D30/D31/D33 evidence only from genuine pilot events.
   - If a gpt-6-sol/6.1-sol launch fails, HALT and record it as a pilot finding.

## Out of scope

- The modelctl worktree `.scratch/worktrees/modelctl-integration` and its evidence; another
  session owns them.
- The main checkout's five owner-edited files.
- The Context Gate.
- The post-P12 feature queue.
- Spotify writes beyond what accepted tasks do.

## Parallel-session coordination

- This pilot line owns `/home/dan/projects/duetflow` and its `.orchestrator/`. The modelctl
  session never touches them.
- Neither session changes `~/.maestro/current` or the `~/.codex`/`~/.claude` limits tables.
- No `git pull` needed. Maestro work stays on `feat/graph-engineering-foundation`, never master.

## Verification to report back

- The Steps-2 snapshot.
- The adopted SHA and its amendment.
- Pinned doctor exit code.
- HALT-clear time in Israel local time.
- Controller PID/tmux window and proof that it is the only one.
- The first task's run id and outcome.
- Which model the first strong node actually used.
- Any D30/D31/D33 evidence, with file paths.
