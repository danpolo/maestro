# P13 W1, slice 2, part 2f: apply decision 6, integrate, migrate, close out

## Goal

Finish slice 2 of workstream **W1**. The spec review is done and its findings are fixed and committed in the worktree; the suite is green. What is left needs Dan's two answers (see "Blocked on Dan"): apply decision 6 if he chooses **A**, integrate the branch as one commit, commit the `project.yaml` migration in DuetFlow and itv, verify, close out.

Earlier handoffs still apply and are not repeated here:

- `handoffs/2026-10-10-p13-w1-slice2-part2e-next.md`: steps 3-5 (integrate, migrate, verify), the "Migration comment" text, findings 1-4, Open points 13-16.
- `handoffs/2026-10-10-p13-w1-slice2-part2d-next.md`, `...-part2c-next.md`, `...-part2b-next.md`, `...-roles-routes-next.md`: design decided, Open points 1-12, slices 3-6, "Out of scope".

## Read first (and what not to re-read)

Five sessions in a row reached the handoff threshold from reading. Keep reads narrow.

Read in full:

1. Root `AGENTS.md` and the memory index.
2. This file.
3. `handoffs/2026-10-10-p13-w1-slice2-part2e-next.md`: only "Steps, in order" items 3-5, "Migration comment", and "Verification" (the list "Report these numbers back").

Read by range, only if Dan chooses **A** on decision 6:

- `~/projects/maestro-wt/p13-w1/maestro/backends/router.py` about lines 370-395 (how risk and failure rate raise the demand) and 700-735 (`migration_effort`, the migrated-pair filter).
- The old behaviour: `git -C ~/projects/maestro show 5dd4dc6:maestro/backends/router.py`, the same two places (search `min_effort`).
- `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` §17.5, the last paragraph ("Not carried over from the old router").

Do not re-read: the review reports (their content is in this file and in the P13 spec §5), `catalog.py`, `roles.py`, `model_policy.py`, `orchestrator.py` in full, or the whole diff.

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`, on `5dd4dc6`. Head **`628f6c8`**. Clean tree. Nothing is integrated.
- The checkout `~/projects/maestro` is at `5dd4dc6`, released and announced (`~/.maestro/latest` reads `5dd4dc6...` at 16:10 IDT on 2026-10-10).
- **A parallel session works in the checkout** (tmux window `modelctl`). `tests/backends/test_model_runtime.py` is still uncommitted there and is its own: do not edit, stage, stash or revert it. Run `git -C ~/projects/maestro log -3 --oneline` before integrating; if it committed, rebase onto its commit first and re-run the suite.
- `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md` exists only in the checkout (gitignored; absent from the worktree). §5 there now holds the review's deferred items (edited this session).
- Both projects were idle at 14:44 IDT. Check again right before integrating (`in_flight` empty, `graph_runs` empty, nothing parked, no open frozen record in `.orchestrator/model_tasks/`).
- Not this work, leave unstaged: DuetFlow `docs/dependency_map.md`; itv (`~/projects/instagram-to-value`) `docs/dependency_map.md` and `scripts/agents_lib.py`.
- Review scratch at `/home/dan/.cache/maestro-review-scratch/` (probes, a mutation copy). Remove it at close-out. `probe2.py` there is the read-only routing probe used below.
- The Telegram MCP plugin failed to connect again. Not needed for slice 2.
- Files outside the working directory need a `Read` before `Edit`.

### What part 2e did

- Ran `spec-code-review` on `5dd4dc6..12d3f38` with two independent reviewers (production code against DESIGN §17 and the P13 W1 spec; test quality).
- `628f6c8` fixes, tests first:
  - `roles.py`: a `roles:` block the derivation refuses keeps the strengths it states validly on both paths. On the loaded path (the one production takes) it raised `PolicyError`, which was a regression against `5dd4dc6`.
  - `model_policy.py`: `derived_roles` breaks a cost tie the way the router does; `role_shape` refuses a `routing.harness_order` that is not a list of known harness names.
  - `tests/conftest.py`: `MAESTRO_GLOBAL_CONFIG` is set at conftest import. It was a session fixture, so `quota`, `switch` and `watchdog` read thresholds from the real `~/.maestro/config.yaml` during collection.
  - New tests: project-over-global at the loader, a strength set only in the global file is frozen with the task, `maestro doctor` runs `config_keys` and `role_shape`, an unparseable global file, the untested `role_shape` branches. The schema 2 legacy test no longer reads the developer's limits tables.
  - DESIGN §17: marks what is not built (auto-slim, §17.4, §17.6), `independent_of` not read yet, what the harness order changes.

### Review result

| Reviewer | Critical | Important | Minor | Questions |
|---|---|---|---|---|
| Production code | 0 | 0 | 12 | 5 |
| Test quality | 0 | 6 | 12 | 4 |

- Fixed: all 6 Important; 7 Minor in whole or in part (malformed roles, tie-break, `independent_of` note, harness-order doctor check, DESIGN text, unparseable global file, `role_shape` branches).
- Became decision 6: production Question 1.
- Recorded in the P13 spec §5, not fixed: everything else, grouped as "before a schema 2 policy is published", "when Dan first writes the global file", and "smaller".
- Not recorded there yet, add at close-out: doctor's preamble deny-list check has no global-layer test (`cli.py` about 925; `tests/test_global_config.py` covers `_deny_list_block` only).
- Settled: Open point 14 (two Haiku ids) is harmless. Open point 13 (operator pin outside the order) is part of the recorded "ranking or allow-list" gap.

## Finding that became decision 6

High-risk tasks lose high effort on the branch. Measured on both real projects with `probe2.py`, old code (`~/projects/maestro`) against the branch, at risk `high`:

| Role | itv old | itv branch | DuetFlow old | DuetFlow branch |
|---|---|---|---|---|
| implementer | Opus 5.5 at high | Opus 5.5 at medium | Codex 6.1 Sol at high | Opus 5.5 at medium |
| test_designer | Opus 5.5 at high | Opus 5.5 at medium | Codex 6.1 Sol at high | Opus 5.5 at medium |
| reviewer | Opus 5.5 at high | Opus 5.5 at medium | Opus 5.5 at high | Opus 5.5 at medium |

At risk `medium` and `low` nothing changes on either project, and planner and diagnoser stay at high. The earlier "itv unchanged" table was measured at medium risk only. Dan was told this on 2026-10-10 (this session) and asked decision 6.

Probe command (read-only): `cd /home/dan/.cache/maestro-review-scratch && PYTHONDONTWRITEBYTECODE=1 ~/projects/maestro/.venv/bin/python -B probe2.py <code dir> <project dir>`.

## Steps, in order

1. **Decision 6.** If Dan chose **A**: in the worktree, tests first, make a migrated entry bind `high` when risk or failure rate raised the demand to `strong`, as the old router did. Explicit schema 2 pairs are not touched. Update the last paragraph of DESIGN §17.5 (it must also name the failure-rate trigger, which the branch dropped without saying). Re-run `probe2.py`: itv must equal the old code at all three risk levels for all five roles. If Dan chose **B**: change no code; add the failure-rate trigger to that DESIGN paragraph. Either way re-run the full suite and commit in the worktree.
2. **Wait for Dan's go** if he has not given it. Do not integrate before it.
3. **Integrate as one commit.** Commits `59f3c3d` and `56efff1` are red on purpose (their messages say so) and the spec §3.1 requires a green suite at every commit, so squash: no red commit may become a checkout HEAD. Use `finishing-a-development-branch`. A commit in the checkout is a release that every project adopts at its next idle point. Watch `~/.maestro/release.log` until it prints `announced:<sha>`; if it prints `held_red`, stop and report.
4. **Migrate both projects** as part 2e's step 4 describes (delete `roles:` and `fallback_chain:`, replace the comment, add the `antigravity:` limits row, stage only `project.yaml`).
5. **Verify** (numbers below), then close out: add the one item above to the spec §5, `docs/PROGRESS.md`, remove the worktree, the branch and the review scratch yourself, tell Dan the result.

## In scope

- Steps 1-5 above.

## Out of scope

- Everything the earlier handoffs list as out of scope (slices 3-6, W2, W8, W9, agy pairs, pace routing, the Context Gate, modelctl's own repo).
- Every review item recorded in the P13 spec §5.
- Changing whether `routing.harness_order` is a ranking or an allow-list.
- Publishing a schema 2 policy (slice 3, after Dan answers decisions 3 and 4).

## Verification

Measured this session:

- Full suite at `628f6c8`: **5781 passed, 6 skipped, 2 xfailed, 0 failed** (127 s). Command: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider -n 4 -o addopts="" -q`. At `12d3f38` it was 5761 passed; the test reviewer reproduced that number.
- The 6 skips are all `tests/test_next_graph_prompt.py`: the worktree lacks the gitignored phase specs and `STATE.yaml`. They run in the checkout, so the count there differs.
- New tests seen red before their fix: 4 loaded-path malformed-roles cases, 2 malformed-sibling cases, the conftest import-time guard, the cost tie-break.
- `tests/test_model_policy_schema2.py` with `HOME` set to an empty directory: 17 passed.

Report these numbers back from the next session:

- Decision 6, if **A**: the `probe2.py` rows for itv at risk `high`, old against branch (must be equal), and the count of new tests.
- Full suite after step 1: passed / failed / xfailed.
- The release line for the integration commit in `~/.maestro/release.log` (`announced:<sha>`), and the suite result in the checkout.
- `maestro doctor --repo <project>` on the **real** DuetFlow and itv after the migration: `role_shape`, `config_keys`, `backends`, `model_limits`, `model_ids` all OK, and the exit code.
- `git -C <project> show --stat HEAD` for both migration commits: one file each, `project.yaml`.
- `maestro_version` in each project's `state.json` after its next idle point: the integration sha.

## Suggested skills

`test-driven-development` for decision 6, `verification-before-completion`, `finishing-a-development-branch` for the integration, `using-git-worktrees` (the worktree exists; do not create another), `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** If Dan answered in the message that launches the session, record the answer and do not ask again.

1. **Decision 6: effort on high-risk tasks (blocks step 1 and everything after it).** Asked on 2026-10-10 with the table above. Choices: **A** keep today's behaviour until the first schema 2 policy: a task raised to `strong` by risk or failure rate still binds at high, so itv routes exactly as today (recommended: the pilot baseline is not disturbed, and the spec's measure is "no drop in quality") / **B** accept medium for high-risk tasks from this release (cheaper on Opus quota). With either choice the DuetFlow high-risk implementer and test_designer move from Codex 6.1 Sol to Opus 5.5.
2. **The go for integration (blocks steps 3-5).** Choices: **A** integrate as soon as step 1 is green, and commit both migrations at once (recommended) / **B** wait for a pilot idle point Dan names / **C** wait until the pilot ends. Accepted with the go: the DuetFlow planner moves from Codex 6.1 Sol to Opus 5.5, and the migration adds the agy limits table to both `project.yaml` files. Dan answered **A** to the earlier form of this question (decision 5) before he knew of those two points and of decision 6; ask once more.
3. **Pair classes and planner/diagnoser effort (blocks slice 3 only).** `handoffs/2026-10-10-p13-w1-pair-classes-for-approval.md`. He answers by row number for decision 3 and with **A**, **B1**, **B2** or **C** for decision 4. Decision 4 now also covers high-risk effort after schema 2 (the same question as decision 6). Two statements in that file are no longer exact: "both projects keep routing as they do today" (see decision 6 and the DuetFlow planner), so say so when he answers. Reminded twice; do not resend the file.
4. **The single Maestro bot (blocks slice 4 only).** Dan's action: create `~/.maestro/.env` himself with mode 600, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...` copied from the DuetFlow project. Never print or send the token. It did not exist at 14:56 IDT on 2026-10-10. Confirm with `stat -c %a ~/.maestro/.env` (must print `600`) and one Telegram smoke per project.

## Dan's answers so far (2026-10-10)

- Decision 1: commit the two W0 files. Done: `5dd4dc6`, released.
- Decision 2: **B**, reuse the DuetFlow token.
- Decisions 3 and 4: sent as a file, not answered yet.
- Decision 5: **A** (approve the integration and the migration when the verification numbers are reported, no diff review). He added that he will not progress any Maestro instance until this part is finished, except the itv pilot.
- Decision 6: asked this session, not answered yet.
