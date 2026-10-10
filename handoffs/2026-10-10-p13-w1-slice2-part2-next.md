# P13 W1, slice 2, part 2: finish the pair model (policy schema 2, role shape, doctor, migration)

## Goal

Continue slice 2 of workstream **W1** in its worktree. Part 1 (catalog and router on `(model, effort)` pairs) is committed there as `d5cdb18`, **red on purpose**: 18 older tests still assert the old behaviour. This session makes the suite green again, then builds the rest of slice 2: policy schema 2, the strength-only role shape in config, the doctor check, and the migration of DuetFlow's and itv's `project.yaml`.

Slice table and everything about slices 3-6: `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md` (the previous handoff). Its "Facts already established", "Slice 1 leftovers", "Out of scope" and "Parallel sessions" pointers still apply; do not re-derive them.

## Read first (and what not to re-read)

The last session reached the handoff threshold after reading and building part 1 only. Keep reads narrow.

Read in full:

1. Root `AGENTS.md` and the memory index.
2. `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` **§17** (lines 967 to about 1080). §17.3 and §17.5 were rewritten last session and are the design for what is built. Change the design there before changing code.
3. `git -C ~/projects/maestro-wt/p13-w1 show d5cdb18 --stat` and the diff of `maestro/backends/catalog.py` and `maestro/backends/router.py` in that commit. This is the new API; do not re-read those two files whole.
4. `tests/backends/test_route_pairs.py` in the worktree (17 tests, all pass): the behaviour contract of part 1.

Read by range only when you reach the step that needs it:

- `maestro/model_policy.py`: `validate` (70-119), `shipped_policy` (122-140), `effective_roles` (252-306), lines 310-600 (**not read yet**: freeze, `runtime_roles`, `runtime_model_inputs`, `task_model_context`), `harness_order` and `graph_preferences` (about 602-645), `catalog_defaults` (about 647-700).
- `maestro/roles.py` (449 lines, read last session): the legacy loop's resolver. `role_config` (293-324) reads `backend`, `models`, `model` and `fallback_chain`.
- `maestro/workflows/policy.py` 400-485: `AgentDefinition.effort_policy`.
- `maestro/workflows/runner.py` 986-989 and about 3241-3247: two reads of `entry.profile(model).strength_rank`.

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`: `ada43d2` (slice 1) then `d5cdb18` (slice 2 part 1, red). Clean tree. Nothing is integrated; the checkout and `~/.maestro/latest` are still `e753cc2`.
- Run tests from the worktree: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider -n 4`. With `-n 4` the suite takes about 2 minutes 20 seconds.
- Suite at `d5cdb18` plus the last fix: 18 failed, 5690 passed, 6 skipped, 1 xfailed (measured before the last one-test fix as 19 failed, 5689 passed).
- The checkout still has the two uncommitted W0 edits. See "Blocked on Dan" item 1.

### What part 1 built

- `catalog.PairProfile(model_id, effort, strength, relative_cost)`; `BackendCapabilityEntry.pair_profiles` keyed `(model_id, effort)`; `delivers(effort)`, `pairs(model)`, `pair(model, effort)`, `undeliverable_pairs()`, `strength_rank_of(model)`; `CapabilityCatalog.models()` (the old `routes()`) and `routes()` yielding `(entry, model, effort)`.
- `effort=None` is the pair of a harness with no dial. It is a route only there; a named effort is a route only where `EffortControls.resolve` delivers it.
- `pair_profiles=None` means "read `model_profiles` under the schema 1 migration rule": each graded model at `medium`, `high` and `None`, same class and cost; `pairs_migrated` is then true. `pair_profiles={}` means no route. A model that is only listed, with no profile, has no route (the old "balanced by default" is gone, per B4).
- Router: candidates are pairs; `TaskDemand.min_effort` and the `effort_policy` reads are gone; `resolve_agent(..., harness_order=...)` replaces `preferences=`; new reason code `no_approved_pair`; on a migrated entry a role binds the effort in `catalog.MIGRATION_ROLE_EFFORTS` (planner and diagnoser `high`, others `medium`; free objective: highest).
- `model_policy.harness_order(project)` reads `routing.harness_order`, default `roles.default_chain()`, as backend ids. `DispatchRouting.harness_order` and the three orchestrator sites use it. `graph_preferences` is still defined but no longer called by the orchestrator.

### The 18 red tests (fix or rewrite first)

- `tests/backends/test_router.py` (12): `test_a_fully_saturated_frontier_waits_with_the_soonest_reset`, `test_a_mapped_effort_uses_the_backends_own_word_and_says_it_was_mapped`, `test_an_excluded_route_is_rejected_and_the_next_one_binds`, `test_a_role_floor_may_raise_the_demand_and_never_lower_it`, `test_a_role_may_narrow_the_permitted_models_and_the_router_obeys`, `test_a_role_requiring_a_physical_sandbox_is_not_given_directory_isolation`, `test_a_saturated_pool_routes_to_an_equally_qualified_alternate`, `test_a_strength_floor_raises_the_demand_and_never_lowers_it`, `test_a_strength_offset_lowers_the_demand_one_rung`, `test_dispatch_plan_emits_a_flag_a_config_key_or_nothing_at_all`, `test_high_risk_work_is_never_routed_to_a_low_effort_small_model`, `test_routine_low_risk_work_is_allowed_to_be_cheap`.
- `tests/test_model_graph_routing.py` (4): `test_ineligible_or_paused_preference_falls_back_with_explanation`, `test_preference_cannot_override_context_or_permission_demand`, `test_production_graph_builder_reads_adopted_policy`, `test_qualified_preference_precedes_cost_ranking`.
- `tests/test_model_frozen_choices.py::test_recovered_graph_preferences_keep_frozen_project_family`.
- `tests/test_model_sol_replacement.py::test_detected_gpt_6_1_sol_replaces_gpt_6_sol_end_to_end`.

None was diagnosed one by one. Expected causes: assertions on `demand.min_effort`, on effort chosen from risk, on `preferences=`, and helper entries that list a model without a profile (now no route). Check each: a test that fails because real behaviour was lost, not because the design changed, is a bug in part 1.

## In scope

- Make the 18 tests green (rewrite to the pair model, or fix part 1).
- Policy schema 2: `validate` accepts `routes` rows `{backend_id, model_id, effort, strength, relative_cost}` unique on the first three, and `roles` in the §17.2 shape. `catalog_defaults` emits explicit `pair_profiles` under schema 2 and leaves `pair_profiles` unset under schema 1 (the entry migrates). Schema 1 mutation tools (`replace_route`, `assign_roles`, `retire_routes`, `rollback_policy`, the modelctl flow) keep working on schema 1 documents; moving them to pairs is slice 3.
- Role shape in config: `{strength, independent_of}`. Shipped default of `strength` from each template's `min_reasoning_strength`. Remove `effort_policy` from the five templates and from `AgentDefinition`.
- `maestro/roles.py` (the legacy loop's resolver, also used by `switch.py`, `implementer.py`, `agentcall.py`): derive backend, model and chain from the role's strength, the approved pairs and `routing.harness_order`, instead of from `backend` / `models` / `fallback_chain`. Keep its public functions.
- Remove `graph_preferences` and what only it needed; `runner.py`'s two `profile().strength_rank` reads move to `entry.strength_rank_of(model)`.
- `doctor` fails on a role with `effort`, `backend`, `model` or `models`, and on `fallback_chain`. Land it together with the migration of DuetFlow's and itv's `project.yaml` (one commit in each project repo), or every adoption fails.
- `templates/project.yaml.tmpl`, DESIGN.md §5, `routing` documented in `config.KNOWN_KEYS` readers.
- The slice 1 leftovers listed in the previous handoff.

## Out of scope

- Everything the previous handoff lists as out of scope (slices 3-6, W2, W8, W9, agy pairs and the canonical family, pace routing, the Context Gate, modelctl's own repo).
- Removing `ModelProfile.strength` and `relative_cost`: they stay as the schema 1 reading until slice 3 (DESIGN.md §17.3).

## Open points found last session (decide, or put to Dan if marked)

1. **Role names.** The live policy and both projects use the legacy names `implementer`, `judge`, `diagnoser`; the graph maps `reviewer` and `proof_review` to `judge`. DESIGN.md §17.2 shows `reviewer` and `planner`. Pick one naming for the config and state the mapping in §17.2.
2. **File ownership.** Removing `effort_policy` touches `templates/roles/reviewer.yaml`, which W2 owns, and two lines of `runner.py`, which W8 owns. Both are small and at different lines; rebase onto the checkout before integrating and check they merge trivially.
3. **For Dan (see Blocked on Dan item 4):** planner and diagnoser effort under the proposed table.
4. **Fallback order changes.** With `harness_order` before cost, a Claude-saturated implementer now falls to Codex before agy. At `e753cc2` the order after Claude was by cost and class, which could pick agy's `gemini-3.8-flash-high` first (the live policy grades `gpt-6-luna` as `light`, so the implementer's Codex preference was already rejected). Primary routes are unchanged.
5. **agy under the first schema 2 policy.** Dan's table has no agy rows, so agy has no route from the first schema 2 publication until W4 adds pairs. Under schema 1 migration agy keeps routing through the no-effort pair.

## Verification

Report these numbers back:

- Full suite in the worktree: passed / failed / xfailed. Baseline at `e753cc2`: 5683 passed, 1 xfailed. At `d5cdb18`: 18 failed, 5690 passed, 6 skipped, 1 xfailed.
- `maestro doctor` on a `project.yaml` whose role has `effort`: exit code (must be non-zero) and the message.
- `maestro config show --effective --json` for DuetFlow and itv before and after migrating their roles: the keys that differ, listed (expected: only `roles.*` and `fallback_chain`).
- For each of the five roles, the pair the router picks under the migrated live schema 1 policy (revision `42e771ed...`), next to what it picks at `e753cc2` (must be the same harness and model; expected implementer and test_designer `claude-sonnet-5-5`, reviewer, planner and diagnoser `claude-opus-5-5`). Also report the effort of each.
- Number of pairs in the schema 2 fixture policy that are refused as undeliverable (`undeliverable_pairs()`, at least one test lane).

## Suggested skills

`test-driven-development`, `systematic-debugging` for any of the 18 whose cause is not the design change, `verification-before-completion`, `using-git-worktrees` (the worktree exists; do not create another), `spec-code-review` before integrating, `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** Items 1-3 were put to Dan on 2026-10-10 in two sessions and are not answered yet. If he answers before the next session starts, the answers are appended at the bottom of this file under "Dan's answers". Part 2 itself is not blocked by any of them.

1. **Commit the two W0 script edits now?** (`scripts/next_graph_prompt.py`, `tests/test_next_graph_prompt.py`, uncommitted in the checkout.) A commit in the checkout is a release that DuetFlow and itv adopt at idle. Choices: **A** commit alone now (recommended) / **B** commit with the first W1 integration. Confirm with `git log -1 --oneline` and `cat ~/.maestro/latest`.
2. **The single Maestro bot (blocks slice 4 only).** Choices: **A** a new bot from BotFather (recommended) / **B** reuse the DuetFlow bot token / **C** reuse the itv bot token (keeps the two-poller conflict). Dan's action: create `~/.maestro/.env` with mode 600 himself, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...`, and send `/start` to a new bot once. Never print or send the token. Confirm with one Telegram smoke per project.
3. **Pair classes for the current models (blocks slice 3 and the first schema 2 policy).** The 12-row table and its notes are in `handoffs/2026-10-10-p13-w1-slice2-roles-routes-next.md` under "Blocked on Dan" item 3. Show Dan the table again; he approves or edits by row number.
4. **Planner and diagnoser effort (new; blocks the first schema 2 policy, not part 2).** Today planner and diagnoser run Opus at `high`. A role now has no effort (B3), and under the proposed table both are `strong`, so the router picks the cheapest strong pair: Opus at `medium` (row 1, cost 1.0), not Opus at `high` (row 2, cost 1.5). Choices: **A** accept `medium` for planner and diagnoser (follows B3 as written) / **B** keep them at `high` by grading Opus-medium below what they need, which requires a fourth class above `strong` or a different class for row 1 / **C** something else Dan states. Until a schema 2 policy is published nothing changes: the schema 1 migration keeps both at `high`.
