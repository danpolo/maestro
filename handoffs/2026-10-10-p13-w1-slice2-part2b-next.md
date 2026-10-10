# P13 W1, slice 2, part 2b: wire the strength-only role shape, then schema 2, doctor and the migration

## Goal

Finish slice 2 of workstream **W1** in its worktree. Parts 1 and 2a are committed there (`d5cdb18`, `401b5ca`). The suite is **red on purpose on 2 tests**. This session wires the role shape that part 2a prepared, then builds what is left of slice 2: policy schema 2, removal of `effort_policy`, the doctor check and the migration of DuetFlow's and itv's `project.yaml`.

Earlier handoffs still apply and are not repeated here:

- `handoffs/2026-10-10-p13-w1-slice2-part2-next.md`: the full "In scope" list for part 2, "What part 1 built", the open points.
- `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md`: slices 3-6, "Facts already established", "Slice 1 leftovers", "Out of scope", and the 12-row pair table (Blocked on Dan item 3).

## Read first (and what not to re-read)

The last session reached the handoff threshold at about 185K tokens, mostly from reading whole files. Keep reads narrow.

Read in full:

1. Root `AGENTS.md` and the memory index.
2. `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` §17.2, §17.3 and §17.5 (about lines 993-1075). Change the design there before changing code.
3. `git -C ~/projects/maestro-wt/p13-w1 show 401b5ca -- maestro/model_policy.py maestro/backends/router.py`: the part 2a diff. It holds the new helpers; do not re-read `model_policy.py` whole.
4. This file's "Design decided last session" section below.

Read by range only when you reach the step that needs it:

- `maestro/roles.py`: `_project` (180-191), `RoleConfig` (244-272), `role_config` (293-324), `resolve` (340-402). The rest is docstrings and unchanged helpers.
- `maestro/model_policy.py`: `validate` (70-119), `_freeze_task_locked` (the `record['choices']` lines), `runtime_model_inputs`, `catalog_defaults`.
- `maestro/workflows/routing.py`: `DispatchRouting.harness_order` (about 203) and `resolve` (380-420), to mirror for role strengths.
- `maestro/orchestrator.py` `_graph_routing` (about 1969-2032).
- `maestro/workflows/policy.py` 416, 465, 482: the three `effort_policy` lines of `AgentDefinition`.
- `maestro/workflows/runner.py` 989 and 3247: `profile.strength_rank`.
- `maestro/implementer.py` 599-610 and 640-657: the only readers of `eligible_models` and `strict_models`.

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`: `ada43d2` (slice 1), `d5cdb18` (part 1), `401b5ca` (part 2a). Clean tree. Nothing is integrated; the checkout and `~/.maestro/latest` are still `e753cc2`.
- Run tests from the worktree: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider -n 4` (about 2 minutes). A script that imports `maestro` from outside the worktree needs `PYTHONPATH=~/projects/maestro-wt/p13-w1`, or it imports the checkout.
- Suite at `401b5ca`: **2 failed, 5708 passed, 6 skipped, 1 xfailed**.
- The two red tests both read `DispatchRouting.preferences`, which part 1 removed: `tests/test_model_frozen_choices.py::test_recovered_graph_preferences_keep_frozen_project_family` and `tests/test_model_sol_replacement.py::test_detected_gpt_6_1_sol_replaces_gpt_6_sol_end_to_end`. They are rewritten in step 1 below.
- The checkout still has the two uncommitted W0 edits (Blocked on Dan item 1).
- The Telegram MCP plugin failed to connect in the last session (`CONNECTION_CLOSED`). Not needed for slice 2.

### What part 2a did

- **Router fix (a real part 1 bug).** On a migrated entry, a harness that cannot deliver the role's old effort lost its route. It now keeps the efforts it does deliver. Nine of the twelve red router tests had caught this. Test: `test_a_migrated_harness_that_cannot_deliver_the_roles_effort_keeps_its_route`.
- The other red tests were rewritten: demand effort assertions dropped, `preferences=` tests became `harness_order=` tests in `tests/test_model_graph_routing.py`.
- **New in `model_policy.py`, tested only by a probe, not wired:** `ROLE_KEYS`, `RETIRED_ROLE_KEYS`, `LEGACY_ROLE_NAMES` (`judge` reads `reviewer`), `shipped_role_strengths()`, `role_shape_issues(document)`, `role_strengths(project)`, `derived_roles(project, policy)`, `harness_drivers(project)`. `harness_order` now maps `harness_drivers` to backend ids; a list that names no known harness reads as absent.
- `runtime_roles` still returns the old shape (a comment says so). `graph_preferences` is still defined, only because `tests/graph_engineering/test_model_choice_recovery.py` imports it.
- Probe result for `derived_roles({}, policy)` under the live policy `42e771ed`: implementer and test_designer `claude-sonnet-5-5`; reviewer, judge, planner and diagnoser `claude-opus-5-5`; every role's `backend` is `claude`. On Codex the implementer derives `gpt-6.1-sol` (the live policy grades `gpt-6-luna` as `light`); under the shipped policy it derives `gpt-6-luna`.

## Design decided last session (build this; do not re-derive)

1. **Config role** is `{strength, independent_of}` under the graph role ids. The shipped default of `strength` is each role template's `min_reasoning_strength`. Stated in DESIGN.md §17.2.
2. **Legacy loop.** `runtime_roles(project, repo)` returns `derived_roles(project, policy)`. It writes each role as `{strength, backend, models}` plus `_model_policy_roles: True` and `_harness_chain`. `roles.role_config` then reads `backend` and `models` from that derived entry and `chain` from `_harness_chain`. When the marker is absent (a caller passed raw config), `role_config` calls `derived_roles(project, None)` itself. It no longer reads `backend`, `models`, `model` or `fallback_chain` from user config. Keep `RoleConfig`, `Resolution` and every public function. `strict_models` becomes always true; `invalid_models` becomes always empty. Update the module docstring's purity claim (the derivation reads the packaged register and role templates).
3. **Graph.** Add `DispatchRouting.role_strengths` (from `model_policy.role_strengths(project)`, built in `_graph_routing` beside `harness_order`, and refreshed at the same two other orchestrator sites). `routing.resolve` passes `role_strength=` to `router.resolve_agent`, where it replaces the template's `min_reasoning_strength` as the role floor in `estimate_demand`. Risk may still raise the demand above it. Do not edit the `ProjectPolicy` object: its identity is recorded in runs.
4. **Frozen choices.** `runtime_model_inputs` already swaps `project['roles']` for the frozen ones, so `role_strengths` reads the frozen strengths with no further change. Freeze the merged roles (`config.effective_config(repo)[0].get('roles')`), not the raw project file. Old frozen records hold the old shape: `effective_roles` and `role_strengths` must keep reading them without raising. `routing.harness_order` is not frozen; accepted, because W5 removes the key.
5. **Leave for slice 3:** the project-pin block in `catalog_defaults`, `effective_roles`, `invalid_routes` and the modelctl inventory and detection code. They keep working on schema 1 documents and return nothing for a migrated project. `roles.DEFAULT_MODELS` stays, because `shipped_policy()` builds the schema 1 `roles` from it.
6. **Doctor.** One required check that fails on any item `role_shape_issues` returns, for each config layer (global and project). Land it in the same release as the migration.
7. **Migration of DuetFlow and itv.** Both hold only the old role shape, so the migration deletes `roles:` and `fallback_chain:` from each `project.yaml` (one commit in each project repo).

## Steps, in order

1. Wire the role shape (design items 2-4). Rewrite the two red tests to frozen strengths. Move `tests/graph_engineering/test_model_choice_recovery.py` off `graph_preferences`, then delete `graph_preferences`. Expect more tests to go red when `roles.py` stops reading the old shape; the largest users are `tests/test_roles.py`, `tests/test_orchestrator_switch_hooks.py`, `tests/test_cli.py`, `tests/characterization/test_implementer.py`, `tests/test_switch.py`, `tests/test_model_task_pins.py`. A test that pins a role to a model through `models:` is rewritten to a strength or to a fixture policy; a test of the modelctl flow's reports is left alone (design item 5).
2. Remove `effort_policy` from the five role templates and from `AgentDefinition`. `runner.py`'s two `profile().strength_rank` reads move to `entry.strength_rank_of(model)`. Tests that mention it: `tests/test_routing.py:390`, `tests/workflows/test_policy.py:283`. Check whether a test requires a version bump when a role template changes.
3. Policy schema 2 in `validate` and `catalog_defaults` (explicit `pair_profiles` under schema 2, unset under schema 1), with a fixture policy that has at least one undeliverable pair.
4. Doctor check, `templates/project.yaml.tmpl`, DESIGN.md §5, `fallback_chain` out of `config.KNOWN_KEYS` handling as the doctor message needs, slice 1 leftovers.
5. Full verification, `spec-code-review`, then ask Dan before integrating into the checkout (an integration is a release) and before committing in the DuetFlow and itv repos.

## In scope

- Everything in the "In scope" list of `handoffs/2026-10-10-p13-w1-slice2-part2-next.md` that is not done. Done so far: the 18 red tests (16 fixed, 2 deferred to step 1), the `harness_order` reader.

## Out of scope

- Everything the two earlier handoffs list as out of scope (slices 3-6, W2, W8, W9, agy pairs, pace routing, the Context Gate, modelctl's own repo).
- Moving the modelctl flow, `effective_roles`, `invalid_routes` and the pin block of `catalog_defaults` to pairs (slice 3).
- Removing `ModelProfile.strength` and `relative_cost` (slice 3).

## Open points

1. **File ownership.** Removing `effort_policy` touches `templates/roles/reviewer.yaml` (W2 owns it) and two lines of `runner.py` (W8 owns it). Rebase onto the checkout before integrating and check both merge trivially.
2. **A project role template with `effort_policy`** now fails to load with "unknown role definition keys". DuetFlow and itv have none (checked). Decide whether doctor should say it more plainly.
3. **Fallback order change**, unchanged from the previous handoff: with `harness_order` before cost, a Claude-saturated implementer falls to Codex before agy.
4. **Codex implementer under the live policy** derives `gpt-6.1-sol`, not `gpt-6-luna`, until a `balanced` Codex pair is approved (rows 7 and 11 of the pair table).

## Verification

Report these numbers back:

- Full suite in the worktree: passed / failed / xfailed. Baseline at `e753cc2`: 5683 passed, 1 xfailed. At `401b5ca`: 2 failed, 5708 passed, 6 skipped, 1 xfailed.
- `maestro doctor` on a `project.yaml` whose role has `effort`: exit code (must be non-zero) and the message.
- `maestro config show --effective --json` for DuetFlow and itv before and after the migration: the keys that differ (expected: only `roles.*` and `fallback_chain`).
- For each of the five roles, the pair the graph router picks under the live schema 1 policy `42e771ed...`, next to the pick at `e753cc2` (same harness and model expected: implementer and test_designer `claude-sonnet-5-5`; reviewer, planner and diagnoser `claude-opus-5-5`), with the effort of each. Also the model `roles.resolve` returns for `implementer`, `judge` and `diagnoser`: it must equal the graph's pick.
- Number of pairs in the schema 2 fixture policy refused as undeliverable (`undeliverable_pairs()`).

## Suggested skills

`test-driven-development`, `systematic-debugging` for a test whose failure is not the design change, `verification-before-completion`, `using-git-worktrees` (the worktree exists; do not create another), `spec-code-review` before integrating, `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** All four were put to Dan on 2026-10-10 and are not answered yet. If he answers before the next session starts, the answers are appended at the bottom of this file under "Dan's answers". Steps 1-4 above are not blocked by any of them.

1. **Commit the two W0 script edits now?** (`scripts/next_graph_prompt.py`, `tests/test_next_graph_prompt.py`, uncommitted in the checkout.) A commit in the checkout is a release that DuetFlow and itv adopt at idle. Choices: **A** commit alone now (recommended) / **B** commit with the first W1 integration. Confirm with `git log -1 --oneline` and `cat ~/.maestro/latest`.
2. **The single Maestro bot (blocks slice 4 only).** Choices: **A** a new bot from BotFather (recommended) / **B** reuse the DuetFlow bot token / **C** reuse the itv bot token (keeps the two-poller conflict). Dan's action: create `~/.maestro/.env` with mode 600 himself, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...`, and send `/start` to a new bot once. Never print or send the token. Confirm with one Telegram smoke per project.
3. **Pair classes for the current models (blocks slice 3 and the first schema 2 policy).** The 12-row table and its notes are in `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md` under "Blocked on Dan" item 3. Show Dan the table again; he approves or edits by row number.
4. **Planner and diagnoser effort (blocks the first schema 2 policy only).** Today both run Opus at `high`. A role has no effort, and under the proposed table both are `strong`, so the router picks the cheapest strong pair: Opus at `medium` (row 1, cost 1.0), not Opus at `high` (row 2, cost 1.5). Choices: **A** accept `medium` for planner and diagnoser / **B** keep them at `high`, which needs a fourth class above `strong` or a lower class for row 1 / **C** something else Dan states. Until a schema 2 policy is published the migration keeps both at `high`.
5. **Integration and the two project commits (new; blocks step 5 only).** Integrating the branch into `~/projects/maestro` is a release that both projects adopt, and the migration is one commit in each of the DuetFlow and itv repos. Choices: **A** approve both when the verification numbers are reported / **B** review the diff first.
