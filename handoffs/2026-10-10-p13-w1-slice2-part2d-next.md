# P13 W1, slice 2, part 2d: last leftovers, then verify, review, integrate and migrate

## Goal

Finish slice 2 of workstream **W1**. Policy schema 2 and most of the step 4 leftovers are committed in the worktree (`72284dd`) and the suite is **green**. What is left: a few small leftovers (below), then step 5: rebase, the verification numbers, `spec-code-review`, and, with Dan's go, the integration and the migration of DuetFlow's and itv's `project.yaml`.

Earlier handoffs still apply and are not repeated here:

- `handoffs/2026-10-10-p13-w1-slice2-part2c-next.md`: "What part 2b built", Open points 1-7, the Verification list.
- `handoffs/2026-10-10-p13-w1-slice2-part2b-next.md`: "Design decided last session" (items 1-7).
- `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md`: slices 3-6, "Out of scope".

## Read first (and what not to re-read)

Three sessions in a row reached the handoff threshold from reading. Keep reads narrow. Give the test cleanup in step 4b to one subagent with a precise brief.

Read in full:

1. Root `AGENTS.md` and the memory index.
2. `git -C ~/projects/maestro-wt/p13-w1 show 72284dd --stat`, and from that commit only the diff of `maestro/model_policy.py` and `maestro/cli.py`.
3. `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` §17.3 (about lines 1025-1062; it grew this session).
4. This file's "What part 2c built", "Steps" and "Open points".

Read by range only when a step needs it:

- `maestro/implementer.py` 568-610 (`_implementer_backend`).
- `handoffs/2026-10-10-p13-w1-slice2-part2c-next.md` lines 72-91 (Open points and Verification).

Do not re-read: `catalog.py`, `router.py`, `roles.py`, the part 2b diff.

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`: `ada43d2`, `d5cdb18`, `401b5ca`, `1f4d7f9`, `72284dd`. Clean tree. Nothing is integrated.
- Suite at `72284dd`: **5758 passed, 6 skipped, 2 xfailed, 0 failed**. Command: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider -n 4 -o addopts="" -q` (about 2.5 minutes).
- **The checkout moved**: Dan's decision 1 was carried out. `5dd4dc6` (the two W0 script edits) is committed in `~/projects/maestro` on top of `e753cc2`. Its release was still being tested when this session closed (`maestro.cli release`, started 11:47 IDT). First check: `cat ~/.maestro/latest` should read `5dd4dc6...`; if it still reads `e753cc2...`, read `~/.maestro/release.log` and report to Dan.
- The branch is **not rebased** onto `5dd4dc6` yet. Rebase before step 5; the two commits touch disjoint files.
- **A parallel session works in the checkout** (tmux window `modelctl`). At close it had `tests/backends/test_model_runtime.py` uncommitted there. That file is its own: do not edit, stage, stash or revert it. Run `git -C ~/projects/maestro log -3 --oneline` before rebasing; if it committed, rebase onto its commit.
- **The itv pilot is running.** Dan said he will not progress any Maestro instance until this part is finished, except the itv pilot. See Blocked on Dan item 3.
- The DuetFlow repo has an uncommitted `docs/dependency_map.md` that is not this work. The migration commit there stages only `project.yaml`.
- The Telegram MCP plugin failed to connect again. Not needed for slice 2.
- Files outside the working directory need a `Read` before `Edit`.

### What part 2c built (`72284dd`)

- **Schema 2.** `model_policy.validate` reads schema 1 and 2 (`POLICY_SCHEMAS`). A schema 2 row is `{backend_id, model_id, effort, strength, relative_cost}`, unique on the first three; `effort` is a word or `null`. Schema 2 `roles` hold the §17.2 shape (checked with `role_shape_issues`). `retired` stays `[backend, model]`.
- **Catalog.** `catalog_defaults` sets explicit `pair_profiles` under schema 2 and leaves the key absent under schema 1. Under schema 2 a model with no row is not served, and the old-shape `models:` pin block does not run. `apply_provider_limits` drops the pairs of a model it drops.
- **Guards.** `replace_route`, `assign_roles`, `retire_routes`, `rollback_policy` refuse a schema 2 policy with a `PolicyError` naming schema 1 (`_schema_1_tool`). `effective_roles` reads a schema 2 role as one with no backend or models, so `freeze_task` works under a schema 2 policy.
- **Fixture.** `tests/fixtures/model_policy/schema2.json`: 16 pairs, **3 undeliverable** (no effort on a dial harness; an effort the harness cannot map; an effort on agy, which has no dial). It uses `claude-haiku-4-5-20251001` because the shipped catalog does not know `claude-haiku-5-5`. Tests: `tests/test_model_policy_schema2.py` (17).
- **Doctor.** `_check_backends` reads `model_policy.harness_drivers`. `_check_model_limits` and `_check_model_ids` read the derived role models (`cli._role_model_pairs`) instead of the retired `roles.<r>.models`; `model_ids` skips a backend with no probe. `_check_retention` and the preamble deny list read the effective config (`cli._repo_config`). `_repo_project_yaml` stays the project layer alone, for the per-layer checks.
- **`fallback_chain`** stays in `config.KNOWN_KEYS` on purpose, so only `role_shape` reports it; the comment says so.
- **Decided, no code change:** `model_inventory.project_document` keeps reading `project.yaml` directly. It reads the project's identity, which a global file must not supply, and it belongs to the modelctl flow (slice 3).

## Steps, in order

4. **Leftovers.**
   - a. Stale wording in `implementer._implementer_backend`: the docstring ("one read of `project.yaml`, degrading to the default backend on every malformed `roles:` entry") and the comment at about line 606 ("Under an adopted shared policy").
   - b. Tests that still write the retired shape as dead input: about nine configs in `tests/test_orchestrator_switch_hooks.py` (near 390, 595, 616, 653, 709, 780, 875, 1155, 1201; the one near 1201 says "deliberately no fallback" through `fallback_chain: [CLAUDE]`, check why it still passes) and `tests/characterization/test_implementer.py::test_launch_implementer_task_model_overrides_the_configured_role_table`. The `tests/test_cli.py` ones are done.
   - c. Two tests are owed for code written this session: `_check_retention` and `_deny_list_block` read a value set only in the global file (add to `tests/test_global_config.py`).
5. **Verify, review, integrate.** Rebase onto the checkout. Trace part 2c's Open point 2 (an exhausted implementer route raises `PolicyError`; does the task park or wait?). Run the verification list. Run `spec-code-review`. Report the numbers to Dan and ask for the go (Blocked on Dan item 3). Then integrate and commit the migration in the DuetFlow and itv repos (delete `roles:` and `fallback_chain:` from each `project.yaml`; stage only that file).

## In scope

- Steps 4 and 5 above.

## Out of scope

- Everything the earlier handoffs list as out of scope (slices 3-6, W2, W8, W9, agy pairs, pace routing, the Context Gate, modelctl's own repo).
- Moving the schema 1 mutation tools, `effective_roles`, `invalid_routes` and the modelctl flow to pairs (slice 3).
- Publishing a schema 2 policy (slice 3, after Dan approves the table).

## Open points

Part 2c's Open points 1-7 still stand. New from this session:

8. **Schema 2 `roles` are validated and nothing reads them.** A role's strength comes from its template and from config. Slice 3 decides whether the policy keeps the field.
9. **`MODEL_POLICY_READER_VERSION` is still 4.** A Maestro source older than `72284dd` refuses a schema 2 policy as "unsupported schema". Slice 3 must bump the reader version before it publishes one, so a project pinned to older code is deferred, not broken.
10. **The modelctl flow is not guarded against schema 2** (`model_detection`, `model_inventory`, `model_commands`, `model_asks` index `policy['roles'][..]['models']`). Safe today: the live policy is schema 1 and only slice 3 publishes schema 2.
11. **Doctor now checks derived models.** `model_limits` asks for a limits row for every derived model, agy included (`agy/gemini-3.8-flash-high`). Confirm on both real projects that the check passes (verification below).
12. **Under the proposed table the Codex implementer becomes Luna at `high`**, not Sol at `low` (cheaper row). Dan was told in the approval file.

## Verification

Report these numbers back:

- Full suite in the worktree after the rebase: passed / failed / xfailed. At `72284dd`: 5758 passed, 6 skipped, 2 xfailed. Baseline at `e753cc2`: 5683 passed, 1 xfailed.
- `maestro doctor` on a `project.yaml` whose role has `effort`: exit code (must be non-zero) and the `role_shape` message.
- `maestro config show --effective --json` for DuetFlow and itv before and after the migration: the keys that differ (expected: only `roles.*` and `fallback_chain`).
- For each of the five roles, the pair the graph router picks under the live schema 1 policy `42e771ed...`, next to the pick at `e753cc2` (expected: implementer and test_designer `claude-sonnet-5-5`; reviewer, planner and diagnoser `claude-opus-5-5`), with the effort of each. Also the model `roles.resolve` returns for `implementer`, `judge` and `diagnoser`: it must equal the graph's pick.
- Pairs in the schema 2 fixture refused as undeliverable: **3** (already measured; `test_the_fixture_pairs_no_harness_delivers_are_refused_as_routes`).
- `maestro doctor` on DuetFlow and itv after the migration: `role_shape`, `config_keys`, `backends`, `model_limits` and `model_ids` all OK.
- Before the migration: neither project has an unfinished task frozen with the old role shape (`.orchestrator/model_tasks/`), and no graph run is open.

## Suggested skills

`test-driven-development`, `systematic-debugging` for a failure that is not the design change, `dispatching-parallel-agents` for step 4b, `verification-before-completion`, `using-git-worktrees` (the worktree exists; do not create another), `spec-code-review` before integrating, `finishing-a-development-branch`, `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** Steps 4 and the verification part of step 5 are not blocked by any of them.

1. **Pair classes and planner/diagnoser effort (blocks slice 3 and the first schema 2 policy, not slice 2).** Sent to Dan on 2026-10-10 as `handoffs/2026-10-10-p13-w1-pair-classes-for-approval.md`. He answers by row number for decision 3 and with **A**, **B1**, **B2** or **C** for decision 4. If he has not answered, remind him once; do not resend the file.
2. **The single Maestro bot (blocks slice 4 only).** Dan chose to reuse the DuetFlow bot token. His action: create `~/.maestro/.env` himself with mode 600, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...` copied from the DuetFlow project. Never print or send the token. Confirm with `stat -c %a ~/.maestro/.env` (must print `600`) and one Telegram smoke per project.
3. **The go for integration, and its timing against the itv pilot (blocks the integration only).** Dan chose "approve both when the verification numbers are reported", with no diff review. Integrating is a release that itv adopts at its next idle point, and from that release doctor fails on itv's old role shape, so itv's `project.yaml` migration must land with it. Report the numbers, then ask one question. Choices: **A** integrate now and commit both migrations at once (recommended if the pilot has no task in flight and no task frozen with the old shape) / **B** wait for a pilot idle point Dan names / **C** wait until the pilot ends.

## Dan's answers (2026-10-10)

- Decision 1: commit the two W0 files now. Done: `5dd4dc6`.
- Decision 2: **B**, reuse the DuetFlow token.
- Decisions 3 and 4: he asked for them in a file; sent, not answered yet.
- Decision 5: **A**. He added that he will not progress any Maestro instance until this part is finished, except the itv pilot.
