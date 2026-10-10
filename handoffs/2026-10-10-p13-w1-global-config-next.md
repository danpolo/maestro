# P13 stage 1, W1: global config, model+effort routes, one Maestro bot

## Goal

Build workstream **W1** of phase P13. Defaults move to `~/.maestro/config.yaml`, `project.yaml` overrides any key, a role states only `strength` and `independent_of`, a route is `(harness, model, effort)` with its own class and cost, and one Maestro Telegram bot serves every project. W1 is on the critical path: W3, W4 and W5 all need it.

W0 (housekeeping) finished on 2026-10-10. Nothing from it is in flight.

## Read first

1. Root `AGENTS.md`, `tasks/lessons.md`, and the memory index (especially `post-p12-feature-queue`, `maestro-commits-auto-release`, `maestro-gate-uses-venv`, `agent-scratch-not-in-tmp`, `edit-code-with-edit-tool-not-shell`, `modelctl-approvals-are-meta-telegram`, `model-pins-class-level-auto-switch`).
2. `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md`: §1.1 (binding decisions B1-B19), §2 W1, §3, §4. This file is the spec; do not re-ask what §1.1 decides.
3. `~/.agent/diagrams/maestro-post-p12-plan-2026-10-10.html`: figure 4 (config layering) and the W1 card in section 7 (code seams with line numbers, audited at `e753cc2`). The spec wins where they differ. Note the plan still says a role has an effort; B3/B4 removed that.
4. `docs/graph-engineering/STATE.yaml`: the entries with `owner: P13` and `workstream: W1` (2 of them: the shared-Telegram-bot thread and the model-transition follow-ups).
5. Code seams: `maestro/config.py`; `maestro/model_policy.py` (`effective_roles`, `catalog_defaults`); `maestro/backends/catalog.py` (`EffortControls`, `CANONICAL_EFFORTS`, strength per model today); `maestro/templates/project.yaml.tmpl`; init/doctor in `maestro/cli.py`; the hitl Telegram env loading; `maestro/selfupdate.py` idle hook; `docs/MODEL_TRANSITIONS.md`.

`scripts/next_graph_prompt.py` now knows P13 and prints the directive with the W-checklists and the P13 open threads.

## State at handoff

- Branch `feat/graph-engineering-foundation`, HEAD `e753cc2`.
- **Two uncommitted tracked edits** from W0: `scripts/next_graph_prompt.py` (P13 registered) and `tests/test_next_graph_prompt.py` (sequence assertion). `tests/test_next_graph_prompt.py`: 13 passed. Commit them with the first W1 commit, or alone if Dan says so. A commit in this checkout is a release.
- Gitignored, already written: the P13 spec, `specs/README.md` §9, STATE.yaml (`current_phase: P13`, `status: not_started`, P12 added to `completed_phases`, `in_progress_details: null`, new `_p13_open_threads`). Backup of the previous STATE: `handoffs/STATE.before-p13-w0-2026-10-10.yaml`.
- STATE thread count after W0: 87 closed, 14 accepted, 18 owned by P13 (W1 2, W6 1, W7 1, W8 8, W9 5, W10 1), 0 unstructured.
- Projects: DuetFlow is finished. itv is idle, with about 14 tasks on `hold: true`; it is the P13 pilot (stage 4).

## In scope

- Every checkbox under "W1" in the spec's §2.
- Closing the two `workstream: W1` threads in STATE.yaml with evidence.
- A design note for the route model (per-pair class and cost) before coding, because W4 and W5 build on it. Put it in `docs/DESIGN.md`.

## Out of scope

- W2, W8, W9 (same stage, other sessions; see below) and every later workstream.
- The Context Gate (AGENTS.md standing rule; spec §4).
- Changing modelctl's own repo beyond what pair approval needs. If modelctl must change, write it as an owner patch plan the way `docs/plans/2026-10-07-modelctl-owner-applied.md` did, and ask Dan before applying.
- Hardcoding agy routes. modelctl had no agy provider at `7b4d0e6`; Dan is arranging it (W4 checks).

## Parallel sessions

W2, W8 and W9 may run at the same time in other sessions. They must merge trivially.

| Workstream | Owns | W1 must leave alone |
|---|---|---|
| W1 (this) | `maestro/config.py`, `maestro/model_policy.py`, `maestro/backends/catalog.py`, `templates/project.yaml.tmpl`, init/doctor config checks, Telegram env loading and command registry, selfupdate auto-slim | |
| W2 | `maestro/control/store.py` attempts + migration, `maestro/workflows/review_origins.py` and the new ledger module, `templates/roles/reviewer.yaml`, `maestro report usage` | yes |
| W8 | `maestro/workflows/runner.py` probes and uncertain handling, `handlers.py` sentinel path, ctl verbs, command inbox consumer, selfupdate self-test | yes (W1 touches only the idle auto-slim hook in `selfupdate.py`; coordinate if W8 is live) |
| W9 | `maestro/permissions.py`, gate scope check, `pre_validate` | yes |

Work in a linked worktree on persistent storage (`~/projects/maestro-wt/`), never `/tmp`. `git pull`/rebase onto the checkout before integrating. Integrating into `~/projects/maestro` is live: the release hook runs the suite and every project adopts at idle.

## Verification

Report these numbers back:

- `.venv/bin/python -m pytest -q` full suite: passed / failed / xfailed (baseline at `e753cc2`: 5683 passed, 1 xfailed).
- `maestro config show --effective` for DuetFlow and itv before and after the auto-slim: number of keys that differ (must be 0), and the number of keys removed from each `project.yaml`.
- `maestro doctor` on a `project.yaml` that has a role with `effort`: exit code (must be non-zero) and the message.
- One Telegram smoke per project through the single bot: message ids, each tagged `[project]`.
- `maestro telegram commands --botfather`: the number of commands listed.
- Number of `owner: P13` threads left in STATE.yaml (18 now; 16 after W1).

## Suggested skills

`using-git-worktrees`, `test-driven-development`, `verification-before-completion`, `context7-cli` (only if a library question comes up), `handoff` and `close-session` at the end.

## Blocked on Dan

**The next session's first reply must tell Dan these items and wait for his answer before starting the dependent work.** Everything else in W1 can start at once.

1. **Commit the two W0 script edits now?** They are harmless, but a commit triggers a release and an idle adoption by DuetFlow and itv. Choices: commit alone now / commit with the first W1 commit. Confirm with `git log -1 --oneline` and `cat ~/.maestro/latest`.
2. **The single Maestro bot (blocks only the "one bot" checkbox).** Does Dan create a new bot with BotFather, or reuse an existing token? The token goes in `~/.maestro/.env` (a secret: Dan puts it there himself; never print or send it). Confirm with the Telegram smoke above.
3. **Pair classes for the current models (blocks only the pair-approval smoke).** Under B4 Dan approves the class of each model+effort pair. The session proposes a starting table from today's catalog (one row per harness, model and effort worth using, with class and `relative_cost`) and Dan approves or edits it on Telegram.
