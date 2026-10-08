# Continue the modelctl release: fix 2 owner-suite failures, then stage, adopt for DuetFlow, resume the pilot

## Goal

Finish `handoffs/2026-10-08-modelctl-release-then-pilot-next.md` (the parent; **read it first**,
its approvals, limits and "Verification to report back" list still apply). Steps 1–3 are
committed. The modelctl owner suite is **not yet green** against the integrated Maestro.
Diagnose and fix that first, then do parent steps 4–6.

## Read first

- Parent handoff: `handoffs/2026-10-08-modelctl-release-then-pilot-next.md`.
- Pilot resume steps: `handoffs/2026-10-07-duetflow-pilot-hold-next.md` (steps 2, 4–6 + its report list).
- Root `AGENTS.md` and the auto-memory index (do-cleanup-yourself, agent-scratch-not-in-tmp,
  model-pins-class-level-auto-switch, context-management-not-built, no-idle-subagent-polling).
- Release dir (all evidence + scripts for this release): `handoffs/2026-10-08-modelctl-release/`.
- Evidence E (preserved, persistent): `.superpowers/sdd/2026-09-30-modelctl-integration/`
  in the main checkout. The session13 owner gate recipe is `modelctl-session13-gate.py` there.

## Done (2026-10-08, this session)

**Step 1, reconciliation.** Of the five old main-checkout edits, the worktree version won on
everything already covered: gpt-6.1-sol, gpt-6-sol pin-only, Opus judge default, and owner-policy
family resolution in place of the main checkout's `latest_family_row` table derivation. The one
real disagreement was the Codex light tier. Dan was asked: worktree gpt-5.6-luna vs main gpt-5.6-terra.
**Dan answered: "it should be gpt-6-luna".** Implemented as Luna-class succession:
- gpt-5.6-luna was added to `catalog.PIN_ONLY_MODELS["codex"]`, so it serves explicit pins only.
- Codex light nodes now fall to gpt-6-luna, the cheapest shared route; its balanced profile is unchanged.
- It was done test-first: `tests/test_model_sol_replacement.py::test_gpt_6_luna_supersedes_gpt_5_6_luna_as_the_codex_light_route`.
- The legacy-proposal tests in `tests/test_model_lifecycle.py` now start from `_older_release_policy()`.
- The pre-integration copies of the five files are in `handoffs/2026-10-08-main-checkout-model-switch-preintegration/`.
- The main checkout has no leftover model-switch edits. Its remaining untracked files are Dan's to preserve.

**Step 2, Maestro commits.** These are on `feat/graph-engineering-foundation`, not pushed. The
worktree branch was rebased onto c3f8fd4 and fast-forwarded.
- 072d399 feat(models): class-level shared policy, bridge, adoption
- c4fc6b7 feat(models): per-task freeze + runtime wiring
- c0f484b docs(models): MODEL_TRANSITIONS, template inheritance, review record
- **0c7eb92** fix(models): gpt-6-luna supersedes gpt-5.6-luna. This is the integrated HEAD and the release SHA
  `0c7eb92be656b3ca7d2d0c7d783bac9998c38fce`.

**Step 2, full serial gate in the main checkout** (`gate-maestro-0c7eb92.log`): 5535 passed, 3 failed, 1 xfailed, 442 s.
- All 3 failures were `tests/test_purity.py` scanning the then-present nested worktree
  `.scratch/worktrees/modelctl-integration`. The worktree is now removed, and `tests/test_purity.py`
  reran with 5 passed (`purity-rerun.log`).
- The candidate self-test (step A below) is the clean full gate on the exact SHA.

**Step 2, cleanup.** The worktree and the `feat/modelctl-integration` branch were removed.
- Evidence E was copied in full into the main checkout.
- The worktree's ignored leftovers (old plan copies and test `.orchestrator`) are in
  `handoffs/2026-10-08-modelctl-release/worktree-ignored-leftovers/`. They must stay out of
  `.superpowers`, because the purity test scans there.

**Step 3, modelctl commits** (`/home/dan/projects/modelctl`, master, clean). A secrets scan was clean.
- f39bbc8: the pre-existing staged baseline.
- 61c9de0: the D-A–D-E owner changes. These are 21 files, not 19: the 19 applied plus 2 that were
  already staged and modified. Compare against E/`session13-applied-final.json` if it matters.

**Release prep.** The scripts in `handoffs/2026-10-08-modelctl-release/` are written but not run:
- `stage-candidate.py`: materializes the SHA and copies the 27 hash-verified doc inputs from the f13b2c7 version.
- `selftest-candidate.py` and `run-candidate-gate.sh`.
- `adopt-at-boundary.py` and `install-at-boundary.sh`, adapted from
  `handoffs/2026-09-30-operator-intake-release/`. The boundary is now generic idle: HALT set, no
  running/paused runs, no active attempts, no live lease, previous pin == f13b2c7, no in_flight.

**Binding probe** (`probe-strong-binding.py`, result in `probe-strong-binding-main.json`). It is
read-only and unpaid, and ran on a disposable copy of DuetFlow's project.yaml with no shared policy.
- reviewer and diagnoser bind to **claude-opus-5-5**, by role preference.
- Strong demand on Codex binds to **gpt-6.1-sol**, as implementer/high showed.
- The Codex shared routes are astra, 6.1-sol and 6-luna. gpt-6-sol and gpt-5.6-luna are absent.
- `proof_review` returned no_agent_definition. That is a probe artifact: it is an alias, not a policy role.
- DuetFlow's exact pins needed **no project.yaml change**, because the probe shows they route correctly.

**DuetFlow snapshot** (read-only, matches 2026-10-07):
- HALT is set (2026-09-30 13:01). The pin is f13b2c7, and the global `~/.maestro/current` is 2f555c28.
- task_runs: 1 succeeded, 0 active. Attempts: 10 succeeded, 4 failed.
- The only lease is the expired controller PID 1755214 (dead).
- There is no maestro run/watchdog process, and `~/.maestro/models` does not exist.
- A tmux session named `agents` exists. Check what it runs before launching.

## Blocker: 2 owner-suite failures (not caused by the Luna change)

These run in `/home/dan/projects/modelctl` with the env of E/`modelctl-session13-gate.py`: a
disposable MAESTRO_REPO/HOME/MODELS_HOME, a send-to-me stub on PATH, and
`MODELCTL_TEST_MAESTRO_SOURCE`, `MODELCTL_MAESTRO_TEST_SOURCE` and `MAESTRO_SOURCE` all set to the
Maestro source. The result was 195 passed, 2 failed (`gate-owner-0c7eb92.log`):
- `test_scan_auto_registers_existing_family_and_keeps_old_rows`: `assert any(...)` False.
- `tests/test_real_maestro_retention.py::test_unknown_inventory_source_refuses_deletion`: DID NOT RAISE UserError.

Both fail identically with the source at **c0f484b**, which is pre-Luna and code-identical to the
session13-qualified tree. Session13 passed 197 with source = the old worktree. Suspects:
- the old worktree had ignored files the fresh checkouts lack: `.orchestrator/`, `docs/graph-engineering/`, `AGENTS.md`;
- host state that changed since 2026-10-07 (e.g. `~/.codex` tables, `~/.maestro`);
- the env differing from the session13 script.

Use `systematic-debugging`: read both tests and the session13 gate env exactly, then reproduce
with source = a temp checkout of c0f484b plus the leftovers. Fix in the right repo, test-first, and
keep the fix minimal. If it is a test-environment issue, record it, don't paper over it. A Maestro
code change means a new SHA: update `SHA` in all release scripts and re-run the main gate.

## Remaining steps

A. Make the owner suite green (197) against the release source. Record counts and exit.
B. Stage and self-test. These write `candidate-integrity.json`, `candidate-selftest.json` and `.exit`.
   - `cd /tmp && /home/dan/projects/maestro/.venv/bin/python /home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/stage-candidate.py`
   - `bash .../run-candidate-gate.sh` runs the full suite in the immutable checkout, about 7 min.
   - Run it in the background and wait for the notification. It must pass, with counts close to 5538 passed / 1 xfailed.
C. `bash .../install-at-boundary.sh`. It adopts for DuetFlow only via `selfupdate.adopt(project_repo=…)`
   and runs the pinned doctor. Expected doctor warnings: missing watchdog unit, meta origin. Then verify:
   - DuetFlow's `.orchestrator/current` is `~/.maestro/versions/0c7eb92…`;
   - the global pointer is still 2f555c28;
   - HALT is retained.
D. Record a `code_under_test` amendment in `artifacts/graph-engineering/p12-pilots/manifest.json`:
   f13b2c7→0c7eb92, criteria_or_weights_changed false. Use the shape of the c3f8fd4 amendments.
   Commit that file alone, promptly, because the parallel session rebases onto this branch.
E. Resume the pilot per the hold handoff:
   - re-snapshot, then clear HALT;
   - start exactly one controller via DuetFlow `launch.sh`, checking host processes and tmux;
   - monitor without idle polling;
   - for D33 `/ask`, give Dan the bot @MaestroGenericTestingBot, the exact message and the expected reply.
F. Update STATE.yaml `_p12_open_threads` (DuetFlow hold, modelctl-integration-review, D33) and the
   status header of `docs/plans/2026-09-30-modelctl-integration.md` with the commits, the release SHA and the adoption.

## In scope
- The owner-suite diagnosis and fix, staging, self-test, DuetFlow-only adoption, the manifest amendment, the pilot resume, and the STATE/doc updates.

## Out of scope
- Live modelctl rollout: no `modelctl scan`/`consume`, no scan timer.
- `~/.maestro/current`.
- Push or merge to master.
- Other projects.
- The Context Gate.
- The Telegram approval-flow design. That belongs to the parallel session
  `handoffs/2026-10-08-modelctl-telegram-approval-flow-next.md`, which never touches DuetFlow.
  gpt-5.6-terra waits for that flow; it becomes its first real case.

## Parallel-session notes
- This line owns DuetFlow, `handoffs/2026-10-08-modelctl-release/` and the manifest amendment.
- The approval-flow session may start now; steps 1–3 are committed. It must not edit the release
  scripts or DuetFlow.
- Both work on `feat/graph-engineering-foundation`. Commit small, and `git pull --rebase` isn't
  needed: everything is local.

## Verification to report back
- The owner suite before and after: 195/2 → 197/0, with exit codes and the root cause.
- Self-test counts and exit, and the release path `~/.maestro/versions/0c7eb92…` (or a new SHA if code changed).
- DuetFlow pin before (f13b2c7) and after, and proof the global pointer is unchanged.
- The pinned doctor exit code and its warnings.
- The manifest amendment commit SHA.
- The strong-node binding: claude-opus-5-5, with gpt-6.1-sol as the Codex fallback.
- The parent's pilot items: HALT-clear time in Israel local time, the single controller's
  PID/window, the first run id and outcome, the model the first strong node used, and any
  D30/D31/D33 evidence with file paths.
- Restate this session's commits: Maestro 072d399, c4fc6b7, c0f484b, 0c7eb92; modelctl f39bbc8,
  61c9de0. Also restate Dan's light-tier answer.

## Suggested skills
`systematic-debugging` (blocker), `test-driven-development` (any fix), `monitor-long-running-tasks`
(self-test, pilot), `verification-before-completion`, `close-session` at the end.
