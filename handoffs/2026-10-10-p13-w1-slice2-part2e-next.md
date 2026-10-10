# P13 W1, slice 2, part 2e: spec review, then integrate and migrate with Dan's go

## Goal

Finish slice 2 of workstream **W1**. All code and test work is committed in the worktree and the suite is green. The verification numbers are measured and were reported to Dan. What is left: run `spec-code-review` on the branch, fix what it finds, and, with Dan's go, integrate the branch and commit the `project.yaml` migration in the DuetFlow and itv repos.

Earlier handoffs still apply and are not repeated here:

- `handoffs/2026-10-10-p13-w1-slice2-part2d-next.md`: "What part 2c built", Open points 8-12.
- `handoffs/2026-10-10-p13-w1-slice2-part2c-next.md`: Open points 1-7.
- `handoffs/2026-10-10-p13-w1-slice2-part2b-next.md`: "Design decided last session" (items 1-7).
- `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md`: slices 3-6, "Out of scope".

## Read first (and what not to re-read)

Four sessions in a row reached the handoff threshold from reading. Keep reads narrow. Give the review to the `spec-code-review` skill and read its findings, not the whole diff.

Read in full:

1. Root `AGENTS.md` and the memory index.
2. This file.
3. `git -C ~/projects/maestro-wt/p13-w1 log --oneline 5dd4dc6..HEAD` (8 commits) and `git diff --stat 5dd4dc6..HEAD` (42 files, +2433 / -760).

Governing sources for the review (hand them to the skill, do not read them whole yourself):

- `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` §17 (about lines 985-1075).
- `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md`, workstream W1.

Do not re-read: `catalog.py`, `router.py`, `roles.py`, `orchestrator.py` in full. Everything this file states about them was traced last session.

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`, **rebased onto `5dd4dc6`**. Head `12d3f38`. Clean tree. Nothing is integrated.
- Suite at `12d3f38`: see "Verification" (command there).
- The checkout `~/projects/maestro` is at `5dd4dc6`, released and announced (`~/.maestro/latest` reads `5dd4dc6...`). Both projects adopted it (`maestro_version` in each `state.json`).
- **A parallel session works in the checkout** (tmux window `modelctl`). It still has `tests/backends/test_model_runtime.py` uncommitted there. That file is its own: do not edit, stage, stash or revert it. The branch does not touch that file. Run `git -C ~/projects/maestro log -3 --oneline` before integrating; if it committed, rebase onto its commit first and re-run the suite.
- Both projects were **idle** at 14:44 IDT on 2026-10-10: `in_flight` empty, `graph_runs` empty, phase idle, nothing parked. Check again right before integrating.
- Not this work, leave unstaged: DuetFlow has `docs/dependency_map.md` modified; itv (`~/projects/instagram-to-value`) has `docs/dependency_map.md` and `scripts/agents_lib.py` modified.
- The Telegram MCP plugin failed to connect again. Not needed for slice 2.
- Files outside the working directory need a `Read` before `Edit`.

### What part 2d built

- `119b319`: stale wording in `implementer._implementer_backend`; the retired role shape removed from `tests/test_orchestrator_switch_hooks.py` (nine configs) and from one test in `tests/characterization/test_implementer.py`; two tests in `tests/test_global_config.py` (doctor's retention check and the preamble deny list read a value set only in the global file). Both new tests were shown to fail when the readers see the project layer alone.
- `12d3f38`: `maestro/templates/project.yaml.tmpl` lists the agy limits table under `model_limits`, with a test that the template shows every default table.

## Findings Dan was told (2026-10-10)

1. **DuetFlow has no adopted model policy.** `model_policy.adopted_revision` returns `None` for it; itv adopted `42e771ed...`. The earlier handoff said both had one. DuetFlow runs on the shipped policy.
2. **One route changes, on DuetFlow only: the planner.** At `e753cc2` the graph router gave DuetFlow's planner `codex:gpt-6.1-sol` at `high`. On the branch it gets `claude:claude-opus-5-5` at `high`, because the router now ranks by harness order before cost. On itv all five roles are unchanged.
3. **Open point 2 is traced.** `launch_implementer` raises `PolicyError` when every harness is exhausted, and the legacy loop does not catch it (`orchestrator.py` about line 3730, inside `main()`): the process exits. It neither parks nor waits. Neither project can reach it: both set `engineering.runner: graph`, and `main()` returns `_graph_main(state)` before that code. The same raise existed at `e753cc2` under an adopted policy. Recommendation given: do not block the integration; record a follow-up to make the legacy loop wait instead of exit. **Record it** in the project's open-items doc at close-out if Dan has not said otherwise.
4. **Both projects' `model_limits:` mapping names only the claude and codex tables.** The mapping replaces the defaults whole, so an agy model has no context ceiling there. Doctor's `model_limits` check now reads derived models and warns: `no context-limit entry for: agy/gemini-3.8-flash-high`. The graph can reach agy (a harness outside the order is ranked last, not excluded), so the gap is real and older than this branch. Fix: one line in each `project.yaml`, part of the migration below.

## Steps, in order

1. **Review.** Run `spec-code-review` on `5dd4dc6..HEAD` against the two governing sources. Fix findings in the worktree with tests first. Re-run the suite.
2. **Wait for Dan's go** (Blocked on Dan item 3). Do not integrate before it.
3. **Integrate.** Use `finishing-a-development-branch`. A commit in the checkout is a release that every project adopts at its next idle point (memory: "Maestro commits auto-release"). Watch `~/.maestro/release.log` until it prints `announced:<sha>`; if it prints `held_red`, stop and report.
4. **Migrate both projects**, so the migration lands with the release. In each of `~/projects/duetflow/project.yaml` and `~/projects/instagram-to-value/project.yaml`:
   - Delete the `roles:` block (three rows) and the `fallback_chain:` line.
   - Replace the comment block above `roles:` (it describes `models:`; on itv it runs to "never change.") with the text under "Migration comment" below.
   - Add `  antigravity: ~/.gemini/model_context_limits.md` under `model_limits:`, after the `codex:` row.
   - Stage only `project.yaml`. Commit in each repo.
5. **Verify after the migration** (numbers below), then close out: `PROGRESS`/spec notes, remove the worktree and branch yourself, tell Dan the result.

### Migration comment

```yaml
# No `roles:` block: every role runs at its shipped strength. Which backend and model run
# a role is derived from the model policy this project adopted (`maestro models status`),
# or the shipped one when none is published, in the order of `routing.harness_order`
# (default [claude, codex]). To change a role, state only its strength, e.g.
# `roles: {implementer: {strength: strong}}` (maestro `docs/DESIGN.md` §17.2).
#
# The derived model is a default, not the last word: a ROADMAP task's own `model:` field
# overrides it for that one task (implementer only). `model: opus` (a size keyword —
# haiku/sonnet/opus) resolves through whichever backend actually runs the task, using
# that backend's own keyword map — today only `claude` has one, so the same keyword on
# `codex` has nowhere to resolve, is ignored with a warning logged, and the role's derived
# model applies instead. A `model:` that is not one of those three keywords is taken as a
# literal model id and passed straight through — pin one directly (e.g. `model:
# gpt-6.1-sol`) and it is then implicitly specific to whichever backend the task runs on.
```

## In scope

- Steps 1-5 above.

## Out of scope

- Everything the earlier handoffs list as out of scope (slices 3-6, W2, W8, W9, agy pairs, pace routing, the Context Gate, modelctl's own repo).
- Changing the legacy loop's handling of the exhausted-route `PolicyError` (finding 3 is a recorded follow-up).
- Publishing a schema 2 policy (slice 3, after Dan answers decisions 3 and 4).

## Open points

Earlier Open points 1-12 still stand, except point 2 (traced, finding 3) and point 11 (measured, finding 4). New from this session:

13. **An operator pin outside the harness order still launches.** `roles.resolve` puts the preferred backend at the head whatever `routing.harness_order` lists, so `/backend codex` runs on a harness the order does not name. Seen in a test run, not checked against DESIGN §17.2. Decide in the review whether it is intended.
14. **Two Haiku ids.** The policy's light Claude model is `claude-haiku-4-5-20251001`; `implementer.IMPLEMENTER_MODELS["haiku"]` is `claude-haiku-4-5`. Seen in probe output, not investigated.
15. **Four switch-hook tests pass with either harness order** (the ones that set the order near old lines 655, 882, 1162, 1208 of `tests/test_orchestrator_switch_hooks.py`). Their config states the scenario and does not decide the outcome. In the last one the pause is forced by the `_available_backends` stub; its comment now says so. This was true before the cleanup.
16. **Doctor on a copied project reports `docs` FAIL** ("controller pid ... already owns ... control.sqlite3"), because the copy carries the live controller's ownership row. It is an artefact of the copy. Run the after-migration doctor on the real repos.

## Verification

Measured this session (reported to Dan):

- Full suite, rebased, at `12d3f38`: **5761 passed, 6 skipped, 2 xfailed, 0 failed**. Command: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider -n 4 -o addopts="" -q` (about 2.5 minutes). Baseline at `e753cc2`: 5683 passed, 1 xfailed.
- `maestro doctor` on a `project.yaml` whose role has `effort`: exit **1**; `[FAIL] role_shape: project.yaml: roles.implementer.effort is retired: a role states only strength and independent_of`.
- `maestro config show --effective --json`, before against after the migration: DuetFlow 37 keys to 27, itv 39 to 29. Removed in both: `fallback_chain` and nine `roles.*` keys. Added: none. Changed: none. (The agy limits line was not in that comparison; it adds one key, `model_limits.antigravity`.)
- Graph router picks, baseline `e753cc2` against the branch (same with the migrated file):

  | Role | itv (policy `42e771ed`) | DuetFlow (shipped policy) |
  |---|---|---|
  | implementer | Sonnet 5.5 at medium, unchanged | Sonnet 5.5 at medium, unchanged |
  | test_designer | Sonnet 5.5 at medium, unchanged | Sonnet 5.5 at medium, unchanged |
  | reviewer | Opus 5.5 at medium, unchanged | Opus 5.5 at medium, unchanged |
  | diagnoser | Opus 5.5 at high, unchanged | Opus 5.5 at high, unchanged |
  | planner | Opus 5.5 at high, unchanged | **was Codex 6.1 Sol at high, now Opus 5.5 at high** |

  `roles.resolve` on both projects, all three states: implementer `claude-sonnet-5-5`, judge and diagnoser `claude-opus-5-5`. Equal to the graph's pick.
- Schema 2 fixture pairs refused as undeliverable: **3**.
- Doctor on migrated copies of both projects (with the agy limits line): `role_shape`, `config_keys`, `backends`, `model_limits`, `model_ids` all OK. Without the agy line `model_limits` is a WARN.
- Before the migration: every frozen record in `.orchestrator/model_tasks/` of both projects belongs to a completed task (5 in DuetFlow, 4 in itv), and no graph run is open.

Report these numbers back from the next session:

- Review findings: count by severity, and how many were fixed.
- Full suite after the review fixes: passed / failed / xfailed.
- The release line for the integration commit in `~/.maestro/release.log` (`announced:<sha>`).
- `maestro doctor --repo <project>` on the **real** DuetFlow and itv after the migration: the five checks above, all OK, and the exit code.
- `git -C <project> show --stat HEAD` for both migration commits: one file each, `project.yaml`.
- `maestro_version` in each project's `state.json` after its next idle point: the integration sha.

## Suggested skills

`spec-code-review` first, `receiving-code-review` for its findings, `test-driven-development` for each fix, `verification-before-completion`, `finishing-a-development-branch` for the integration, `using-git-worktrees` (the worktree exists; do not create another), `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** Step 1 (the review and its fixes) is not blocked by any of them.

1. **Pair classes and planner/diagnoser effort (blocks slice 3 and the first schema 2 policy, not slice 2).** `handoffs/2026-10-10-p13-w1-pair-classes-for-approval.md`. He answers by row number for decision 3 and with **A**, **B1**, **B2** or **C** for decision 4. He was reminded once on 2026-10-10 (this session). If he has answered in the meantime, record it; do not remind again and do not resend the file.
2. **The single Maestro bot (blocks slice 4 only).** Dan's action: create `~/.maestro/.env` himself with mode 600, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...` copied from the DuetFlow project. Never print or send the token. It did not exist at 11:49 IDT on 2026-10-10. Confirm with `stat -c %a ~/.maestro/.env` (must print `600`) and one Telegram smoke per project.
3. **The go for integration (blocks steps 3-5).** The numbers were reported and the question was asked at the end of this session. If Dan has not answered, ask again in the first reply. Choices: **A** integrate as soon as the review is clean, and commit both migrations at once (recommended: both projects are idle and no task is frozen with the old shape) / **B** wait for a pilot idle point Dan names / **C** wait until the pilot ends. Two things he was asked to accept with the go: the DuetFlow planner moves from Codex 6.1 Sol to Opus 5.5 (finding 2), and the migration also adds the agy limits table to both `project.yaml` files (finding 4).

## Dan's answers so far (2026-10-10)

- Decision 1: commit the two W0 files. Done: `5dd4dc6`, released.
- Decision 2: **B**, reuse the DuetFlow token.
- Decisions 3 and 4: sent as a file, not answered yet.
- Decision 5: **A** (approve the integration and the migration when the verification numbers are reported, no diff review). He added that he will not progress any Maestro instance until this part is finished, except the itv pilot.
