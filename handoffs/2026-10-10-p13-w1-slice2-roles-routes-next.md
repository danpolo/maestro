# P13 W1, slice 2: strength-only roles and the route model

## Goal

Continue workstream **W1** of phase P13 in its worktree. Slice 1 (config layering) is built. This session builds **slice 2**: a role is `{strength, independent_of}`, a route is `(harness, model, effort)` with its own class and cost, the policy moves to schema 2, and `doctor` fails on a role that still has `effort`, `backend`, `model` or `models`.

W1 is too large for one session. The slices, in order:

| Slice | Content | State |
|---|---|---|
| 1 | Global config under `project.yaml`, `config show --effective`, doctor unknown-key check | core built this session (leftovers below) |
| 2 | Role shape, route model, policy schema 2, catalog per pair, router reads pairs | **this handoff** |
| 3 | Pair approval through the modelctl Telegram flow | waits for slice 2 and Dan's table |
| 4 | One bot, `[project]` tags, command registry, `telegram commands --botfather`, `/priority` | waits for Dan's bot token |
| 5 | Auto-slim at the idle adoption, equivalence proof on DuetFlow and itv | waits for slices 1-2 |
| 6 | Close the two `workstream: W1` STATE threads | last |

## Read first (and what not to re-read)

The previous session spent about 100K tokens reading whole files. Read these in full:

1. Root `AGENTS.md` and the memory index (`maestro-commits-auto-release`, `maestro-gate-uses-venv`, `agent-scratch-not-in-tmp`, `edit-code-with-edit-tool-not-shell`, `modelctl-approvals-are-meta-telegram`, `model-pins-class-level-auto-switch`).
2. `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md`: only §1.1 (B1-B19), §2 W1, §4.
3. `~/projects/maestro-wt/p13-w1/docs/DESIGN.md` **§17** (written last session). It is the design for slices 1-5 and W4/W5 build on it. Change it there before changing code.
4. `handoffs/2026-10-10-p13-w1-global-config-next.md`: only the "Parallel sessions" table (which files W2, W8 and W9 own). It still applies.

Read these by range, not whole:

- `maestro/backends/catalog.py`: `ModelProfile` (162-218), `EffortControls` (221-310), `BackendCapabilityEntry.profile/pool_for` (427-455), `CapabilityCatalog.routes` (531-539), the three `*_CATALOG_DEFAULTS` and `PIN_ONLY_MODELS` (626-766).
- `maestro/model_policy.py`: `validate` (70-119), `shipped_policy` (122-140), `effective_roles` (252-306), `graph_preferences` (602-624), `catalog_defaults` (627-681).
- `maestro/backends/router.py` (937 lines, not read in full yet): `estimate_demand` (359-437) reads the role's `effort_policy`; `_hard_constraints` (477-528) compares strength; `_Candidate` (530-560); `_order_key` (561-589); `resolve_agent` (616-857) enumerates `catalog.routes()` and resolves effort at 729; `dispatch_plan` (875).
- `maestro/templates/roles/*.yaml`: each has `min_reasoning_strength` and an `effort_policy` block (`paid_default`: medium for implementer, reviewer, test_designer; high for planner, diagnoser).
- `maestro/roles.py` `DEFAULT_MODELS` (111-121) and the functions that read `roles:` and `fallback_chain`.

Facts already established (do not re-derive):

- Effort vocabulary in `maestro/thirdparty.json`: claude takes `--effort low|medium|high|xhigh|max`; codex takes `-c model_reasoning_effort=` with the same five words; agy has **no** usable effort dial (the effort is in the model id, and `claude-*` on agy rejects `--effort`).
- The live shared policy (`~/.maestro/models/current`, revision `42e771ed...`, schema 1) has these non-agy routes: opus-5-5 strong 1.0, sonnet-5-5 balanced 0.7, haiku-5-5 light 0.18, gpt-6.1-sol strong 1.0, gpt-6-luna light 0.06, gpt-6-astra strong 4.0. It differs from the shipped catalog (which still lists `claude-haiku-4-5-20251001` and luna as balanced 0.5).
- DuetFlow and itv `project.yaml` both carry the old role shape (`{backend, models: {claude, codex}}`) and `fallback_chain: [claude, codex]`. A doctor check that fails on the old shape must land together with the migration of those two files, or every adoption fails.
- The release hook does not fire for commits in a linked worktree (`.git/hooks/post-commit` line 3). Integrating into `~/projects/maestro` does.
- `docs/MODEL_TRANSITIONS.md` "Open follow-ups from the live rollout": every Maestro item is already struck as fixed on 2026-10-08. The two left are not Maestro work (codex-bridge callback bug, spec §5) or decided (Codex 272K floor, Dan 2026-10-08). So that STATE thread can close after a recheck of the struck items against current code; nothing is left to build.

## State at handoff

- Worktree `~/projects/maestro-wt/p13-w1`, branch `p13/w1-global-config`, based on `e753cc2`. Slice 1 is committed there as `ada43d2`. Nothing is integrated into the checkout, and `~/.maestro/latest` is still `e753cc2`.
- Full suite at `ada43d2`: 5698 collected (5684 at `e753cc2` plus 14 new), the run printed no failure. `pyproject.toml` has `addopts = "-q"`, so passing `-q` again hides the summary line; run without `-q` to get the passed / xfailed counts printed.
- The checkout `~/projects/maestro` is still at `e753cc2` with the two uncommitted W0 edits (`scripts/next_graph_prompt.py`, `tests/test_next_graph_prompt.py`). See "Blocked on Dan" item 1.
- Run tests from the worktree with the checkout's venv: `cd ~/projects/maestro-wt/p13-w1 && ~/projects/maestro/.venv/bin/python -m pytest -p no:cacheprovider`. The worktree shadows the installed package (verified).
- Gitignored files (the P13 spec, `STATE.yaml`, `handoffs/`) exist only in the checkout, not in the worktree.

### What slice 1 built (in the worktree)

- `maestro/config.py`: `GLOBAL_CONFIG_ENV` (`MAESTRO_GLOBAL_CONFIG`), `global_config_path()`, `load_global_config()`, `deep_merge()`, `config_leaves()`, `effective_config(repo)`, `KNOWN_KEYS`, `unknown_keys()`. `load_project_yaml()` now returns the project document laid over the global one; with no global file it returns exactly what it did before.
- `maestro/cli.py`: `maestro config show [--effective] [--json] [--repo]`; doctor check `config_keys` (required: fails on an unknown top-level key in either file, or an unreadable global file).
- `tests/conftest.py`: a session fixture points `MAESTRO_GLOBAL_CONFIG` at a path that does not exist, so no test reads the real global file.
- `tests/test_global_config.py`: 14 tests.
- `docs/DESIGN.md` §17.
- Measured on the real projects (read-only, no global file): DuetFlow 37 effective keys, itv 39, all sourced from `project.yaml`. These are the "before" numbers for the auto-slim proof.

### Slice 1 leftovers (small; do them when the files are open anyway)

- Readers that open `project.yaml` directly and so do not see the global layer: `cli._repo_project_yaml` (about line 384), `cli._check_retention`, `model_policy._freeze_task_locked` (line 421), `model_inventory.py` (line 21). Decide each: most should use `config.effective_config(repo)[0]`. The freeze must keep freezing only the project's own `roles` choices until slice 2 changes what a choice is.
- The ROADMAP-task layer ("a task overrides last") is not built.
- `priority:` is in `KNOWN_KEYS` but nothing reads it yet (slice 4 owns `/priority`).
- `maestro/templates/project.yaml.tmpl` and DESIGN.md §5 still show the old shape; update them in slice 2 with the role change.
- Nothing creates `~/.maestro/config.yaml` yet. It does not exist on this host.

## In scope (slice 2)

- Role = `{strength, independent_of}` in config; shipped default of `strength` from each role template's `min_reasoning_strength`; `effort_policy` removed from the templates.
- Policy schema 2 with `effort` in each route row; schema 1 stays readable under the migration rule in DESIGN.md §17.5.
- Catalog: class and cost per `(model_id, effort)`; `routes()` yields `(entry, model_id, effort)`; a pair whose effort `EffortControls.resolve` cannot deliver is not a route (INV-A07).
- Router: candidates are pairs; ordering per §17.5 (`routing.harness_order`, then the existing `_order_key`); an unapproved pair is never a candidate.
- Remove `backend`, `model`, `models`, `effort` from roles and `fallback_chain` from config; `doctor` fails on any of them; migrate DuetFlow's and itv's `project.yaml` in the same release (commit in each project repo; itv is idle, DuetFlow is finished).
- Leftovers listed above.

## Out of scope

- Slices 3-6, W2, W8, W9 and every later workstream.
- agy pairs and the canonical model family (W4; modelctl had no agy provider at `7b4d0e6`). Do not hardcode agy routes.
- Pace routing, budget, sprint (W5). `routing.harness_order` is the stand-in until then.
- The Context Gate (AGENTS.md standing rule; spec §4). Keep one dispatch choke point and one fresh-vs-resume decision.
- Changing modelctl's own repo. If the policy schema change needs it, write an owner patch plan like `docs/plans/2026-10-07-modelctl-owner-applied.md` and ask Dan before applying.

## Verification

Report these numbers back:

- Full suite in the worktree: passed / failed / xfailed. Baseline at `e753cc2`: 5683 passed, 1 xfailed. At `ada43d2`: 5698 collected, no failure.
- `maestro doctor` on a `project.yaml` whose role has `effort`: exit code (must be non-zero) and the message.
- `maestro config show --effective --json` for DuetFlow and itv before and after migrating their roles: the keys that differ, listed (expected: only `roles.*` and `fallback_chain`).
- For each of the five roles, the pair the router picks under the migrated schema 1 policy, next to what it picks at `e753cc2` (must be the same harness and model).
- Number of pairs in the schema 2 fixture policy that are refused as undeliverable (at least one test lane).

## Suggested skills

`test-driven-development`, `verification-before-completion`, `using-git-worktrees` (the worktree exists; do not create another), `spec-code-review` before integrating, `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** They were put to Dan on 2026-10-10 in the previous session; if he answered there, the answers are appended at the bottom of this file under "Dan's answers". Slice 2 itself is not blocked by any of them.

1. **Commit the two W0 script edits now?** A commit in the checkout is a release that DuetFlow and itv adopt at idle. Choices: **A** commit alone now (recommended: the checkout is clean before W1 integrates) / **B** commit with the first W1 integration. Confirm with `git log -1 --oneline` and `cat ~/.maestro/latest`.
2. **The single Maestro bot (blocks slice 4 only).** Choices: **A** a new bot from BotFather (recommended) / **B** reuse the DuetFlow bot token / **C** reuse the itv bot token (keeps the two-poller conflict the STATE thread describes). Dan's action: create `~/.maestro/.env` with mode 600 himself, holding `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_ALERT_CHAT_ID=...`, and send `/start` to a new bot once. Never print or send the token. Confirm with one Telegram smoke per project.
3. **Pair classes for the current models (blocks slice 3 and the first schema 2 policy).** Dan approves or edits by row number. The formal approval still runs through the Telegram flow that slice 3 builds; this table is its starting point.

| # | Harness | Model | Effort | Class | Cost | Basis |
|---|---|---|---|---|---|---|
| 1 | claude | claude-opus-5-5 | medium | strong | 1.0 | live policy |
| 2 | claude | claude-opus-5-5 | high | strong | 1.5 | estimate |
| 3 | claude | claude-sonnet-5-5 | low | light | 0.4 | estimate |
| 4 | claude | claude-sonnet-5-5 | medium | balanced | 0.7 | live policy |
| 5 | claude | claude-sonnet-5-5 | high | balanced | 1.0 | estimate |
| 6 | claude | claude-haiku-5-5 | medium | light | 0.18 | live policy |
| 7 | codex | gpt-6.1-sol | low | balanced | 0.6 | estimate |
| 8 | codex | gpt-6.1-sol | medium | strong | 1.0 | live policy |
| 9 | codex | gpt-6.1-sol | high | strong | 1.5 | estimate |
| 10 | codex | gpt-6-luna | medium | light | 0.06 | live policy |
| 11 | codex | gpt-6-luna | high | balanced | 0.1 | estimate |
| 12 | codex | gpt-6-astra | medium | strong | 4.0 | live policy |

Notes Dan was given with the table:

- The live policy has one class and cost per model and no effort; those values are pinned to medium. Low (about x0.6) and high (about x1.5) are unmeasured estimates until W2 telemetry.
- Rows 7 and 11 matter: gpt-6-luna is `light` in the live policy and serves the implementer on Codex only through the explicit role map that slice 2 removes. With strength-only roles, the implementer (`balanced`) has no Codex route unless one of those rows is `balanced`.
- `xhigh` and `max` are accepted by both harnesses but not proposed as routes. All agy pairs wait for W4.
- Not yet verified: that Haiku accepts `--effort medium`. If not, row 6 is not a route.
