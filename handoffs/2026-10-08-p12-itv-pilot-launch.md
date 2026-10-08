# P12 second pilot: launch and supervise itv 01 + 04, then close P12

## Goal

This continues `handoffs/2026-10-08-p12-itv-pilot-next.md`, which is still the scope and verification contract: its "In scope", "Out of scope" and "Verification to report back" sections all apply. The previous session (2026-10-08, 20:15–21:10 IDT) did the following:

- met the grill precondition;
- onboarded instagram-to-value (itv) onto maestro;
- got three decisions from Dan;
- started the F1 and watchdog-pgrep fixes in a background fork.

This session should:

1. **Check the F1/pgrep work landed** on maestro `feat/graph-engineering-foundation`. Look in `git log` for commits about the reviewer read-only boundary (F1) and the watchdog liveness pgrep, and check that the STATE `_p12_open_threads` entries say CLOSED with shas. If they are missing or incomplete, do the work yourself; the spec is in "Decisions" below.
2. **Pin itv to a maestro version that includes those fixes, then launch its watchdog.** See "Launch recipe".
3. **Supervise the two pilot tasks** `01-runtime-evidence` → `04-quota-adapter` the way DuetFlow was supervised.
4. **Close P12:**
   - fold the itv evidence into B01/B18;
   - give every `_p12_open_threads` entry a disposition (the audit counts 25 FIXED-BUT-NOT-CLOSED, plus the gaps below);
   - run the full suite and `scripts/graph_qualification.py`;
   - update PROGRESS/EXECUTION;
   - build the verdict as a `visual-explainer` artifact and send it with `send-to-me`;
   - ask Dan about the post-P12 queue.

## Read first

- Root `AGENTS.md` and the memory index. In particular:
  - context-management-not-built
  - no-idle-subagent-polling
  - talk-in-local-time
  - do-cleanup-yourself
  - handoffs-are-the-whole-prompt
  - post-p12-feature-queue
  - maestro-gate-uses-venv
- `handoffs/2026-10-08-p12-itv-pilot-next.md`: the contract. Its precondition is now met.
- `handoffs/2026-10-08-p12-closeout/p12-verdict-evidence.md`: the audit and the full-PASS conditions. Use it; don't redo it.
- `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` §2 (itv safeguards) and §3.
- `tasks/lessons.md`, the 2026-10-08 entries. **Never wait on the controller with a `pgrep -f "maestro.cli run"` loop** (unless the fork's fix changed that; verify first). **Add any new ROADMAP task with `hold: true`**, because the controller reads the working tree live.
- In itv: `docs/PLAN.md` §6 maps the prior attempt to the new phases, and `docs/ROADMAP.md` holds the task blocks.

## Decisions Dan made on 2026-10-08 (AskUserQuestion, ~20:35 IDT)

1. **The new build lives in the same repo, and the old code is kept.** The grill docs are the source of truth. The old root `PLAN.md` is archived at `docs/archive/2026-08-25-PLAN-prior-attempt.md`. `docs/PLAN.md` §6 maps the old `scripts/` onto the phases as reuse candidates and marks `fetch.py` (which uses cookies) as an I1 violation.
2. **The pilot tasks are `01-runtime-evidence` and `04-quota-adapter` only.** Every other phase is `hold: true`. Phase 02 is held because it would make real requests to Instagram from the server's IP.
3. **Fix both F1 and the watchdog pgrep pattern in P12**, before the verdict.
   - **F1:** a non-writer agent node (reviewer, planner or diagnoser) runs with cwd = workspace and write access only there. The worktree and its git dirs are not granted, and the model-endpoint ToolBox refuses writes to the worktree. A dispatch-level regression test must cover this (see field notes F1 in `docs/graph-engineering/reports/05-field-notes-artifact-assessment.md`).
   - **pgrep:** the watchdog's liveness check must match only the real controller.

## Done in the previous session

**Grill data.** It was pulled from Drive `ChatGPT/instagram-to-value-grill` with `rclone copy gdrive:... --drive-export-formats md`, then the escaping was cleaned up. The files are:

- `CONTEXT.md`
- `PROJECT-BRIEF.md`
- `AGENT-DATA-REQUEST.md`
- `docs/PLAN.md`
- ADRs 0001–0004. Dan added 0003 (credentialless acquisition) and 0004 (delegated integration ownership) mid-session; both agree with the plan.

Commit: itv `b3c5535`.

**itv `.venv` created.** It had been missing. It is set up with `requirements.txt` plus an editable maestro install from `~/projects/maestro`, which mirrors DuetFlow. The suite baseline is **374 passed in 1.2–1.3 s**. The spec said 235 in 1.44 s, but the repo has grown since.

**Onboarding, itv `0a18dce`.**

- It was run with `MAESTRO_BOOTSTRAPPED=1 ~/projects/maestro/.venv/bin/python -m maestro.cli init .`; see gap G1 below for why.
- `project.yaml`:
  - `engineering.runner: graph`;
  - roles copied from DuetFlow: implementer sonnet-5-5 / gpt-6-luna; judge and diagnoser opus-5-5 / gpt-6.1-sol;
  - `gate.chain: [test]`;
  - `bot_files: [scripts/telegram_bot.py, scripts/worker.py]`. The spec said `scripts/bot.py`, which doesn't exist; this is fixed in the yaml comment;
  - `confinement.deny: [jobs/ staging/ media/ out/ logs/ config/ state/ extracted/]`. The spec put these paths in `deny_list_extra`, but that key holds diff-text regexes, so paths belong in `confinement.deny`;
  - `deny_list_extra`: two regexes for cookie flags and session cookies (I1). They were tested against 5 lines that must block and 5 that must pass;
  - `risky_set`: 10 prior-attempt modules.
- `docs/PROJECT.md` has:
  - Source documents, in reading order;
  - What it is;
  - What good looks like: the gate certifies only that the old suite doesn't regress, and no I1–I12 test exists yet;
  - What's risky;
  - Glossary.
- `operating_preamble.md` has the merged deny list and the ADR clause.
- `docs/ROADMAP.md` has 16 blocks:
  - `01` is autonomous, with scope `[AGENT-DATA-REPORT.md]`, V1 checking the DATA-001/002 headings and V2 a secret-pattern grep;
  - `04` is autonomous, with scope `[itv/, tests/, conftest.py]` and V1/V2 running pytest;
  - every other block is `hold: true`;
  - `09` and `12` are `needs-dan`.
- The rendered `adapters/test` runs `.venv/bin/python3 -m pytest -q`, and it passes 374 tests.

**`maestro doctor`** reported 0 failures and four warnings:

- `systemd_unit` is not installed. Use a tmux watchdog, as DuetFlow does.
- `adopted_code`: no project pin yet.
- `meta_branch`: itv **has no git remote**, so push fails and merges stay local. The meta worktree is at `/home/dan/projects/instagram-to-value-meta`.
- `model_policy`: "installed version lacks the shared policy reader". That should clear once itv is pinned to a recent version.

Telegram is **not configured** for itv: there is no `.env` and no inbound message.

**Live services.** `itv-bot` and `itv-worker` were **not running at any point** (no tmux sessions, no `bot.py`/`worker.py` processes). Report "untouched, still not running".

## Launch recipe (do in order)

1. Confirm that the F1/pgrep commits exist and that the full maestro suite passes (`.venv/bin/python -m pytest -q`, monitored in an `agents` tmux window).
2. **Create a version checkout for the new HEAD** the way `maestro/selfupdate.py` does (`git worktree add ~/.maestro/versions/<sha> <sha>`; read `selfupdate.py` ~line 135 for the exact helper). Then write `/home/dan/projects/instagram-to-value/.orchestrator/current` containing that path, just as DuetFlow's `.orchestrator/current` points at `~/.maestro/versions/0c7eb92…`. **Do not touch `~/.maestro/current`**: the machine pin is out of scope. Re-run `doctor` and confirm that the `adopted_code` and `model_policy` warnings are gone.
3. **Telegram decision.** itv has no maestro bot.
   - Don't reuse DuetFlow's token: two pollers on one token would conflict.
   - The pilot tasks are autonomous, so the run can go ahead without Telegram. Say so in the report.
   - If a needs-dan or HITL path fires, ask Dan whether to give itv its own maestro bot. That needs @BotFather, so it is his to do.
4. Launch the watchdog in a tmux session `itv-watchdog`, mirroring DuetFlow's pane command. **Prepare it as a script Dan runs with `!`**, because relaunching from the agent shell was classifier-denied last time:
   `set -a; [ -f /home/dan/projects/instagram-to-value/.env ] && source …; set +a; MAESTRO_REPO=/home/dan/projects/instagram-to-value /home/dan/projects/instagram-to-value/.venv/bin/maestro watchdog`
   Keep it isolated from the `itv-bot`/`itv-worker` session names (P12 §2).
5. Wait on the journal (`.orchestrator/` journal events such as `orchestrator_relaunched` and run settle) or on attempt result files. **Don't use a pgrep loop.** Use the `monitor-long-running-tasks` skill and one Monitor, not idle polling.

## Supervision: per-task evidence to collect

For each task:

- the run id;
- the node models from the attempts table;
- quality failures and rework rounds;
- whether it was accepted;
- the `acceptance_decisions` rows (expect 2);
- confirmation that the reviewer/planner ran read-only, i.e. its CompletionSpec cwd = workspace (the F1 fix) — the only live evidence for F1;
- that no diff touches a `confinement.deny` path;
- for `01`: the report has no secrets. Read it.

## Gaps found in the previous session (record in STATE `_p12_open_threads`, fix if small)

- **G1: `maestro init` silently runs the machine-pinned version.** `bootstrap.reexec_into_adopted_version` re-execs into `~/.maestro/current` (`2f555c28…`, old), so a new project gets a stale scaffold: the old `adapters/test` stub with no `TEST_COMMAND`, no `workflows/project.yaml`, no `engineering` or `retention` blocks. The only sign is the missing files. **Small fix:** have `init` print which checkout it runs from, and warn when that is not the invoking checkout. It is maestro code, so it is in P12 scope as a pilot-found defect. Add a test.
- **G2:** the P12 spec §2 itv safeguards are stale: `scripts/bot.py` should be `telegram_bot.py`, path entries were put under `deny_list_extra`, and the baseline is 235 tests where the repo now has 374. Note this in the verdict; the spec text can stay.
- **G3:** itv has no git remote. Merges stay local. Report it; it isn't a defect.

## State at handoff

- **Maestro:** branch `feat/graph-engineering-foundation`. The F1/pgrep fork commits there. The `maestro/modelctl_bridge.py` modification and the untracked `artifacts/graph-engineering/p12-pilots/duetflow.json`, `docs/GRAPH_ENGINEERING_ARTIFACT.md` and `docs/reviews/*` files are **not this line's**. Leave them. Never run `git add -A`.
- **itv:** branch `main` at `0a18dce`. `.orchestrator/` and `.venv/` are gitignored. The controller is not running yet. The roadmap's only runnable task is `01-runtime-evidence`.
- **DuetFlow:** idle, as in the prior handoff. Its watchdog is in tmux `duetflow-watchdog`. Leave it alone.
- **Scratch:** the grill raw and clean exports are in the session scratchpad. They are disposable, and the canonical copies are in itv.
