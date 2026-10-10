# Auto-pin the newest tested maestro to every project (hybrid)

> **DONE 2026-10-09** (1c058f4, 4b90216, af638a9, e753cc2; hook installed; itv + DuetFlow follow
> `~/.maestro/latest` automatically). Evidence and the one open follow-up: `docs/graph-engineering/STATE.yaml`
> `_p12_open_threads` (first two entries).

## Goal

Dan (2026-10-09): when a new commit lands in the maestro checkout, every maestro project should end up pinned to it automatically, with no manual `.orchestrator/current` edits. Dan chose the **hybrid** owner model:

- The maestro checkout **announces** the newest commit, after testing it once.
- Each **project controller adopts** the announced version at an idle moment, without re-running the suite, and then restarts so the watchdog relaunches it on the new pin.

## Read first

- Root `AGENTS.md`, `tasks/lessons.md` (last entry: **edit code with the Edit tool, not python/sed scripts via Bash**), and the memory index (especially maestro-gate-uses-venv, do-cleanup-yourself, talk-in-local-time, context-management-not-built).
- `maestro/selfupdate.py`: module docstring, `adopt`, `maybe_self_update`, `self_test`, and the new `release` / `read_latest` / `SelfUpdatePaths.latest_file|held_file`.
- `maestro/orchestrator.py`: `_self_update_maybe` (~line 1907) and its two call sites (~2980, ~3775), both in idle branches.
- `maestro/watchdog.py` (liveness relaunch when the controller exits).
- `maestro/bootstrap.py` (`MAESTRO_BOOTSTRAPPED` sentinel, `project_pin_file`, G1 re-exec into `~/.maestro/current`).
- Findings behind this work are in the local `docs/graph-engineering/STATE.yaml` `_p12_open_threads`.

## State at handoff

- Maestro HEAD `3785ee6` on `feat/graph-engineering-foundation`. Tag `p12-qualified` is on `d10cf6e` (local, not pushed). P12 is closed; the verdict was sent to Dan.
- **One uncommitted edit: `maestro/selfupdate.py`.** It adds `release()`, `read_latest()`, the `latest_file` / `held_file` properties, and an "announced version" branch at the top of `maybe_self_update`. It is inert until something writes `~/.maestro/latest`. `tests/test_selfupdate.py` passes (14). `release()` itself is untested.
- Root cause found: a controller running from a pinned worktree calls `maybe_self_update()` with `_default_repo()` = that worktree, whose HEAD never moves, so it never sees new commits. This is why itv stays on `62eed26`.
- itv is pinned to `62eed26` and idle (tasks 02+ wait on Dan). DuetFlow is idle on `0c7eb92` and its watchdog is running. Neither has G4/G10 fixes live yet.
- Controllers do **not** restart after `adopt`. They keep the old code until relaunched.

## In scope

1. A `release` CLI subcommand in `maestro/cli.py` that calls `selfupdate.release()` and prints the result.
2. `scripts/install-release-hook.sh`: installs a `.git/hooks/post-commit` in the maestro checkout that runs `MAESTRO_BOOTSTRAPPED=1 .venv/bin/python -m maestro.cli release` detached, logging to `~/.maestro/release.log`. Dan runs the installer (it writes a git hook).
3. After `_self_update_maybe` adopts a *new* version while idle (not the first-run adoption), the controller journals it and exits 0 so the watchdog relaunches on the new pin. Check that a clean exit mid-roadmap is really relaunched, and that the graph runner path (`engineering.runner: graph`) reaches the same idle branch.
4. Tests: `release` (green announces, red writes `latest.held` and leaves `latest`, same sha is `up_to_date`, lock/queueing), the announced branch of `maybe_self_update`, and the restart-on-adopt.
5. `maestro doctor` (optional, small): WARN when a project's pin lags `~/.maestro/latest`.
6. Re-pin itv and DuetFlow through the new path as the live proof.

## Out of scope

- Context Gate (unbuilt, per AGENTS.md). Pushing the tag. Touching DuetFlow tasks or itv roadmap tasks.
- Central pinning of projects by a registry (`~/.maestro/models/projects.json` only lists model-policy adopters, so it is not a project registry).
- Mid-task adoption: adoption happens only at idle, as `adopt` requires.

## Verification (report these numbers)

- Full suite with `.venv/bin/python -m pytest -q` in a clean version worktree: passed / failed / skipped counts.
- `release()` live run on the real checkout: `~/.maestro/latest` equals `git rev-parse HEAD`.
- itv and DuetFlow: pin file `.orchestrator/current` equals `~/.maestro/versions/<latest>` after the idle adoption plus relaunch, and the journal shows `self_update_adopted` then a relaunch. Do not interrupt a running task; both are idle now.
- A deliberately red commit on a scratch branch is held: `latest` unchanged, `latest.held` written.

## Suggested skills

`test-driven-development`, `verification-before-completion`, `systematic-debugging` if the relaunch does not happen.

## Notes

- Privileged commands: none are needed. Installing the git hook is a normal file write, but ask Dan to run the installer.
- A PreToolUse hook blocks any Bash line containing the word `sudo`, commit messages included. Use `git commit -F <file>`. Add the Co-Authored-By trailer given in the session reminder.
- `AGENTS.md`, `tasks/lessons.md`, `docs/PROGRESS.md` and `docs/graph-engineering/` are gitignored here: edits stay local.
- Report times to Dan in Israel local time.
