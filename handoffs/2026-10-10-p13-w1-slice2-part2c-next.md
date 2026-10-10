# P13 W1, slice 2, part 2c: policy schema 2, the last config leftovers, then verify and migrate

## Goal

Finish slice 2 of workstream **W1** in its worktree. Parts 1, 2a and 2b are committed there (`d5cdb18`, `401b5ca`, `1f4d7f9`) and the suite is **green**. What is left: policy schema 2 (step 3), the rest of step 4, then step 5 (verification numbers, `spec-code-review`, and, only with Dan's approval, the integration and the migration of DuetFlow's and itv's `project.yaml`).

Earlier handoffs still apply and are not repeated here:

- `handoffs/2026-10-10-p13-w1-slice2-part2b-next.md`: "Design decided last session" (items 1-7), the steps list, the open points.
- `handoffs/2026-10-10-p13-w1-slice2-part2-next.md`: the full "In scope" list for part 2.
- `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md`: slices 3-6, "Slice 1 leftovers", "Out of scope", the 12-row pair table.

## Read first (and what not to re-read)

The last two sessions each reached the handoff threshold from reading. Keep reads narrow, and give a large test rewrite to a subagent with a precise brief (that worked well in part 2b: three subagents by disjoint file sets).

Read in full:

1. Root `AGENTS.md` and the memory index.
2. `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` §17.2, §17.3 and §17.5 (about lines 1000-1090; §17.2 grew in part 2b). Change the design there before changing code.
3. `git -C ~/projects/maestro-wt/p13-w1 show 1f4d7f9 --stat` and, from that commit, only the diff of `maestro/model_policy.py`, `maestro/roles.py`, `maestro/backends/router.py`, `maestro/cli.py`.
4. This file's "What part 2b built" and "Open points".

Read by range only when a step needs it:

- `maestro/model_policy.py`: `validate` (about 70-119), `catalog_defaults` and `graph_catalog_defaults` (grep the `def`).
- `maestro/backends/catalog.py`: `PairProfile`, `pair_profiles`, `pairs_migrated`, `delivers`, `undeliverable_pairs`, `MIGRATION_ROLE_EFFORTS` (grep; do not read whole).
- `maestro/config.py` 28-36 (`KNOWN_KEYS` and its comment), `maestro/cli.py` 1185-1200 (`_check_backends` still reads `fallback_chain`).

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`: `ada43d2` (slice 1), `d5cdb18` (part 1), `401b5ca` (part 2a), `1f4d7f9` (part 2b). Clean tree. Nothing is integrated; the checkout and `~/.maestro/latest` are still `e753cc2`.
- Run tests from the worktree: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider -n 4 -o addopts="" -q` (about 2 minutes). `-o addopts=""` is needed to see the summary line: the project's own `-q` plus another `-q` hides it. A script that imports `maestro` from outside the worktree needs `PYTHONPATH=~/projects/maestro-wt/p13-w1`.
- Suite at `1f4d7f9`: **5738 passed, 6 skipped, 2 xfailed, 0 failed**. One of the two xfails is new and strict (see Open points 1).
- The checkout still has the two uncommitted W0 edits (Blocked on Dan item 1).
- **A parallel session is working in the checkout** (tmux window `modelctl`, seen 2026-10-10 11:35 IDT). It was editing `maestro/backends/codex.py`, `maestro/backends/model_runtime.py`, `tests/backends/test_codex_driver.py` and `tests/backends/test_model_runtime.py` there, uncommitted. Those files are its own: do not edit, stage, stash or revert them. This worktree touches none of the four. If that session commits, the checkout moves past `e753cc2` (a release): run `git -C ~/projects/maestro log -3 --oneline` before step 5 and rebase the branch onto it.
- The DuetFlow repo has an uncommitted `docs/dependency_map.md` that is not this work. The migration commit there stages only `project.yaml`.
- The Telegram MCP plugin failed to connect again. Not needed for slice 2.
- Files outside the working directory need a `Read` before `Edit` (the worktree is outside `~/projects/maestro`).

### What part 2b built

- **Legacy loop.** `model_policy.runtime_roles` returns `derived_roles`. `roles.role_config` reads only the derived entry (`roles._derived`, `DERIVED_MARKER`, `DERIVED_CHAIN_KEY`); `strict_models` is always true. A config passed in by a caller is derived under the shipped policy; a `roles:` block the derivation refuses reads as absent. A `project.yaml` that is not a mapping reads as no configuration.
- **Graph.** `DispatchRouting.role_strengths` holds only the strengths config states (`model_policy.configured_role_strengths`), filled at the three orchestrator sites. `routing.resolve` passes `role_strength=` to `router.resolve_agent`, and `estimate_demand` uses it as the role floor in place of the template's `min_reasoning_strength`. This differs from the earlier design note in one way, on purpose: a role config does not name keeps the floor of its own template, because a project may replace a role template.
- **Freeze.** `_freeze_task_locked` freezes the merged roles (`config.effective_config(repo)[0]`). `effective_roles` passes `strength` and `independent_of` through and no longer sets `_model_policy_roles`.
- **`effort_policy`** is gone from the five role templates and `AgentDefinition`; a template that still states it is refused with "effort_policy is retired". `runner.py` uses `entry.strength_rank_of(model)`. No role version was bumped (no test asks for it).
- **Doctor.** Required check `role_shape` (`cli._check_role_shape`), per config layer, message prefixed with the file. Test: `tests/test_global_config.py::test_doctor_fails_on_a_retired_role_key_in_either_file`.
- **Docs and template.** DESIGN.md §5, §7 step 1 and §17.2; `maestro/templates/project.yaml.tmpl` has the new `roles:` block and no `fallback_chain`.
- `graph_preferences` is deleted.

## Steps, in order

3. **Policy schema 2** in `validate` and `catalog_defaults`: a `routes` row is `{backend_id, model_id, effort, strength, relative_cost}`, unique on the first three; explicit `pair_profiles` under schema 2, unset under schema 1 (schema 1 stays readable under the §17.5 migration rule). Add a fixture policy with at least one pair the harness cannot deliver.
4. **Leftovers.**
   - `fallback_chain` in `config.KNOWN_KEYS` and its comment (`config.py` 31-34): decide so that doctor reports it once, from `role_shape`, with the plain message. `cli._check_backends` (about 1193) still reads `fallback_chain`; move it to `model_policy.harness_drivers`.
   - Stale wording: `implementer._implementer_backend` docstring and the comment at about line 606 still say "under an adopted shared policy".
   - Slice 1 leftovers still open: `cli._repo_project_yaml`, `cli._check_retention`, `model_inventory.py` line 21 read `project.yaml` directly; decide each.
   - Tests that still write the retired shape and pass only because it is ignored (not wrong, but dead input): about nine configs in `tests/test_orchestrator_switch_hooks.py` (near 390, 595, 616, 653, 709, 780, 875, 1155, 1201; the one near 1201 says "deliberately no fallback" through `fallback_chain: [CLAUDE]`, check why it still passes), `tests/characterization/test_implementer.py::test_launch_implementer_task_model_overrides_the_configured_role_table`, doctor fixtures in `tests/test_cli.py` near 502-530, 601-703, 1199.
5. **Verify, review, ask.** Full verification below, `spec-code-review`, rebase onto the checkout, then ask Dan before integrating and before committing in the DuetFlow and itv repos (Blocked on Dan item 5).

## In scope

- Steps 3-5 above.
- Tracing Open points 2 before step 5.

## Out of scope

- Everything the earlier handoffs list as out of scope (slices 3-6, W2, W8, W9, agy pairs, pace routing, the Context Gate, modelctl's own repo).
- Moving `effective_roles`, `invalid_routes`, the pin block of `catalog_defaults` and the modelctl flow to pairs (slice 3).
- Consuming `independent_of` in routing (it is validated and shipped in the template, and nothing routes by it yet; B19 is a later workstream).

## Open points

1. **Old-shape `models:` pin still reaches derivation (known gap, strict xfail).** The pin block of `catalog_defaults` re-admits a pin-only model; `gpt-6-sol` then ties with `gpt-6.1-sol` on class and cost and wins on id, for every strong role on Codex. Doctor fails on the key, so after the migration only a task record frozen before §17.2 can carry one. Marked in `tests/test_roles.py::test_the_retired_role_keys_and_the_fallback_chain_are_ignored` (the `gpt-6-sol` param). Slice 3 clears it. Check before the migration that neither project has an unfinished task frozen with the old shape (`.orchestrator/model_tasks/`).
2. **An exhausted, unusable implementer route is now a `PolicyError` in every project**, not only under an adopted policy: `implementer._implementer_backend` raises `no eligible implementer route: claude exhausted`, while `orchestrator._launch_backend` still returns the pin. Both real projects have an adopted policy, so they already behave this way. Not traced: what the orchestrator does when `launch_implementer` raises here. Trace it; if the task parks instead of waiting for quota, report it to Dan with a recommendation before integrating. Pinned by `tests/test_orchestrator_switch_hooks.py::test_an_exhausted_pin_with_no_fallback_is_recorded_as_the_pin_and_refused_at_launch`.
3. **`routing.harness_order` is not frozen with a task.** A retry of an in-flight entry that recorded no backend follows a live edit. Accepted earlier (W5 removes the key); `test_legacy_retry_without_old_backend_keeps_frozen_project_choice` says so in a comment.
4. **A registry missing a shipped driver** makes `roles.resolve` raise `PolicyError('unknown backend ...')` from `validate(shipped_policy())`. Only tests that edit `registry.BACKENDS` reach it; pinned in `tests/test_roles.py`.
5. **File ownership**, unchanged: `templates/roles/reviewer.yaml` (W2) and two lines of `runner.py` (W8) changed. Rebase onto the checkout before integrating and check both merge trivially.
6. **The shipped role templates changed** (a key removed), so the project policy identity recorded in new runs changes at the release. itv is idle and DuetFlow is finished; confirm no graph run is open in either before integrating.
7. Unchanged from the previous handoff: the fallback order change (Claude-saturated implementer falls to Codex before agy) and the Codex implementer deriving `gpt-6.1-sol` under the live policy until a `balanced` Codex pair is approved.

## Verification

Report these numbers back:

- Full suite in the worktree: passed / failed / xfailed. Baseline at `e753cc2`: 5683 passed, 1 xfailed. At `1f4d7f9`: 5738 passed, 6 skipped, 2 xfailed.
- `maestro doctor` on a `project.yaml` whose role has `effort`: exit code (must be non-zero) and the `role_shape` message.
- `maestro config show --effective --json` for DuetFlow and itv before and after the migration: the keys that differ (expected: only `roles.*` and `fallback_chain`).
- For each of the five roles, the pair the graph router picks under the live schema 1 policy `42e771ed...`, next to the pick at `e753cc2` (same harness and model expected: implementer and test_designer `claude-sonnet-5-5`; reviewer, planner and diagnoser `claude-opus-5-5`), with the effort of each. Also the model `roles.resolve` returns for `implementer`, `judge` and `diagnoser`: it must equal the graph's pick.
- Number of pairs in the schema 2 fixture policy refused as undeliverable (`undeliverable_pairs()`).
- `maestro doctor` on DuetFlow and itv after the migration: `role_shape` and `config_keys` both OK.

## Suggested skills

`test-driven-development`, `systematic-debugging` for a failure that is not the design change, `dispatching-parallel-agents` for a large test rewrite, `verification-before-completion`, `using-git-worktrees` (the worktree exists; do not create another), `spec-code-review` before integrating, `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** All five were put to Dan on 2026-10-10 (twice) and are not answered yet. If he answers before the next session starts, the answers are appended at the bottom of this file under "Dan's answers". Steps 3 and 4 are not blocked by any of them.

1. **Commit the two W0 script edits now?** (`scripts/next_graph_prompt.py`, `tests/test_next_graph_prompt.py`, uncommitted in the checkout.) A commit in the checkout is a release that DuetFlow and itv adopt at idle. Choices: **A** commit alone now (recommended) / **B** commit with the first W1 integration. Confirm with `git log -1 --oneline` and `cat ~/.maestro/latest`.
2. **The single Maestro bot (blocks slice 4 only).** Choices: **A** a new bot from BotFather (recommended) / **B** reuse the DuetFlow bot token / **C** reuse the itv bot token (keeps the two-poller conflict). Dan's action: create `~/.maestro/.env` with mode 600 himself, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...`, and send `/start` to a new bot once. Never print or send the token. Confirm with one Telegram smoke per project.
3. **Pair classes for the current models (blocks slice 3 and the first schema 2 policy).** The 12-row table and its notes are in `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md` under "Blocked on Dan" item 3. Show Dan the table again; he approves or edits by row number.
4. **Planner and diagnoser effort (blocks the first schema 2 policy only).** Today both run Opus at `high`. Under the proposed table both are `strong`, so the router picks the cheapest strong pair: Opus at `medium` (row 1), not `high` (row 2). Choices: **A** accept `medium` / **B** keep them at `high`, which needs a fourth class above `strong` or a lower class for row 1 / **C** something else Dan states. Until a schema 2 policy is published the migration keeps both at `high`.
5. **Integration and the two project commits (blocks step 5 only).** Integrating the branch into `~/projects/maestro` is a release both projects adopt, and from that release doctor fails on a `project.yaml` that still has the old role shape, so the migration commit in each of the DuetFlow and itv repos must land with it. Choices: **A** approve both when the verification numbers are reported / **B** review the diff first.
