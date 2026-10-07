# Resume the DuetFlow P12 pilot from HALT

> **Superseded 2026-10-07:** Dan chose to keep DuetFlow HALTed until a release whose graph
> routing covers gpt-6.1-sol is adopted. Continue from
> `handoffs/2026-10-07-duetflow-pilot-hold-next.md`, not from this file.

## Goal

Dan agreed on 2026-10-01 to continue the pilot. He accepted the recommendation: resume on
DuetFlow's current installed pin, in parallel with the separate modelctl-integration
session. Move DuetFlow from its deliberate HALT to one normal pinned controller running
its queued work. Collect the pilot evidence that D30, D31 and D33 are waiting on.

## Read first

- Root `AGENTS.md` and the Maestro auto-memory index (the build-loop/watchdog and
  permission-list memories apply).
- `handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md`, especially:
  - "No controller is running…" (around lines 80–85);
  - the "For further pilot continuation" paragraph (around lines 126–129);
  - the history follow-up it says must not be lost.
- `handoffs/2026-09-30-modelctl-integration-next.md` → the DuetFlow state block
  (around lines 67–75) and item 7 (D30/D31/D33 are pilot-dependent; don't close them
  with intake proof).
- STATE.yaml `_p12_open_threads` and `docs/graph-engineering/specs/README.md` §9
  (both gitignored), plus the pilot manifest under `artifacts/graph-engineering/p12-pilots/`.

## Done before this session (2026-10-01, by the modelctl session)

- With Dan's approval, DuetFlow's `project.yaml` judge/diagnoser Codex pins changed from
  `gpt-6-sol` to `gpt-6.1-sol`. The change is uncommitted in the duetflow repo.
  - Reason: the live `~/.codex/model_context_limits.md` no longer has a GPT-6 Sol row.
  - Pre-edit copy: `handoffs/duetflow-project.yaml.before-sol61-20261001`.
- Read-only check with the installed release's own parser (`limits.parse_limits_table` /
  `_lookup`, no cache write): opus-5-5, gpt-6.1-sol and gpt-6-luna all have rows.
- **Dan's decision (2026-10-01), effective immediately and already applied:** the implementer's
  Claude pin changed from `claude-sonnet-5` to `claude-sonnet-5-5` (the `~/.claude` table has
  no Sonnet 5 row), together with the two `gpt-6.1-sol` pins above.
  - Final roles: implementer claude-sonnet-5-5 / gpt-6-luna; judge and diagnoser
    claude-opus-5-5 / gpt-6.1-sol.
  - Every pin has a row in the live tables (checked with the installed release's parser).
  - `gpt-6.1-sol` is **not** in f13b2c7's graph catalog; that is what step 3 covers.
- Dan decided (2026-10-01) that pins will become class-level (plan § "Dan's decision
  changes", D-A). DuetFlow **keeps these exact pins** while it runs f13b2c7; do not remove
  them during the pilot.
- Untouched: HALT, the pin `.orchestrator/current` → `~/.maestro/versions/f13b2c7…`, the
  global pointer, and all task/run records. DuetFlow's pre-existing
  `docs/dependency_map.md` modification is still there.

## Steps

1. Inspect the real state:
   - HALT is set;
   - the pin is f13b2c7;
   - zero active runs, attempts, live leases and in_flight;
   - the queue head is `07-reconciliation-followups`, then manual prep `08-timers-notify`;
   - the stale controller lease row (PID 558897) is absent on the host. Let normal
     acquisition handle it; never delete it with direct SQL.
2. The pin decisions are made and applied. Do not ask about them again. Ask Dan once only about
   whatever step 3 finds.
3. Check how the installed f13b2c7 **graph** catalog treats models whose provider row is
   gone. Its codex `available_models` still lists `gpt-6-sol` and has no `gpt-6.1-sol`.
   Read the code and run nothing paid. Decide with Dan whether this blocks the resume.
4. Record the pin change as a pilot-manifest amendment if the pilot protocol requires
   one (operator config, no criteria or weight change). Commit the DuetFlow
   `project.yaml` change only if the pilot convention commits config.
5. Run the pinned doctor; it must exit 0. Then clear HALT and resume **exactly one**
   normal pinned controller through the existing project launch/lock workflow. Never
   start a second controller, and don't rerun the installation scripts.
6. Monitor without idle polling. On a real waiting `/ask`, give Dan the exact bot,
   message and expected reply (D33). Record evidence for D30, D31 and D33 only from
   genuine pilot events.

## In scope

- Resuming the pilot.
- Normal pilot operation and evidence.
- Telegram interactions the workflow itself produces.

## Out of scope

- The modelctl worktree `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`:
  another session owns it; leave it alone.
- The main checkout's five owner-edited files.
- Context Gate.
- Upgrading DuetFlow's pin to a new Maestro release.
- The post-P12 feature queue.
- Spotify writes beyond what accepted tasks do.

## Parallel-session coordination

- This session owns `/home/dan/projects/duetflow` and its `.orchestrator/`.
- The modelctl session owns the worktree and its evidence directory. It never touches
  DuetFlow, and this session never touches the worktree.
- Neither session changes `~/.maestro/current`, `~/.maestro/versions/` or
  `~/.codex`/`~/.claude` limits tables.
- No `git pull` is needed. Neither session commits to Maestro master.

## Verification to report back

- The pre-resume state snapshot (step 1 facts).
- Pinned doctor exit code.
- The HALT-clear time, in Israel local time.
- The controller identity (PID, tmux window) and a check that it is the only one.
- The first task's run id and outcome.
- Any D30/D31/D33 evidence, with its file paths.

## Suggested skills

monitor-long-running-tasks, systematic-debugging (only for an unexplained failure),
verification-before-completion, close-session, handoff.
