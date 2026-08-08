# Maestro — Execution Protocol

**Read this first. It is the process source of truth for building Maestro.**

You are the orchestrating session for a multi-stage build. Your job is to **run stages, not to write
code yourself**. Almost all reading and editing is delegated to workflow subagents so that your own
context stays small enough to carry the whole programme.

## Document precedence

| Document | Authority |
|---|---|
| `docs/DESIGN.md` | **Design.** What is being built and why. All decisions are locked in §2 — do not relitigate them. |
| `docs/EXECUTION.md` (this file) | **Process.** How stages run, gates, context rules. Wins over the plan on any process conflict. |
| `docs/plans/2026-08-09-m0-m1-core-extraction.md` | **Task detail** for M0–M1 only, including the authoritative function → module mapping. |
| `docs/PROGRESS.md` | **Live state.** Which stage is done, what was measured, what is next. Update it after every stage. |

---

## Prime directive: context discipline

You are building from a 4,567-line reference file with a 592-line `main()` at cyclomatic complexity
249. **If you read that file yourself, you will exhaust your context before M2.**

Rules for you, the orchestrating session:

1. **Never open `orchestrator_run.py`.** Not once. Subagents read it; you read their structured results.
2. **Never read an extracted module to check it.** Run the tests and read the pass/fail counts.
3. Prefer `Workflow` fan-out over doing work inline. Each workflow returns a small structured
   summary; the bulk of the tokens stay inside the subagents.
4. Between stages, write findings to `docs/PROGRESS.md` and commit. That file, plus DESIGN.md and the
   plan, must be sufficient for a **fresh session to resume with no other context**.
5. Measure, don't estimate: `python3 ~/.claude/skills/close-session/scripts/context_usage.py`.
   Check it after each stage. Past **150K**, finish the current stage, update `PROGRESS.md`, and tell
   the user to start a fresh session rather than beginning the next stage.

---

## Invariants — true for every stage

- **`/home/dan/projects/AbuAliArchive` is READ-ONLY until M5.** Its autonomous loop is live
  (`orchestrator_run.py` + `watchdog.py`, tmux session `abuali-watchdog`). Never halt it, never write
  to it, never commit in it.
  It has exactly five pre-existing untracked files: `data/multivec_output_ft_v2.pkl.gz`,
  `data/reranker_onnx/`, `data/space_match_result.json`, `eval/gold_real_v1.json`,
  `handoffs/2026-06-28_p8-postmortem-diagnosis-and-plan.md`.
  **After every stage run `cd /home/dan/projects/AbuAliArchive && git status --porcelain`. If anything
  beyond those five appears, STOP the programme and report it.**
- **Behaviour-identical through M1.** Bugs found are recorded in `docs/FOUND_BUGS.md`, never fixed.
- **Purity.** No project-identifying content in maestro. `tests/test_purity.py` enforces it and must
  stay green.
- **Runtime dependencies** limited to `pyyaml`, `python-dotenv`, `requests`.
- **Never write to `~/.codex/` directly.** That directory belongs to Codex; changes there are made by
  invoking the Codex CLI (the operator's delegate-to-agent rule).
- **Commits are made by you, not by fan-out subagents.** Concurrent agents racing on `.git/index.lock`
  corrupt the run. Fan-out agents create and edit their own files only and run **no git commands**.

---

## Unattended operation

**This programme runs overnight with nobody watching.** The operator has explicitly authorised, in
advance, all three actions that would otherwise need approval:

1. **Editing `~/.claude/` and `~/.codex/`** (M3). Authorised. The `~/.codex/` side is still performed
   by invoking the Codex CLI, never by writing into that directory.
2. **Using the Telegram dev-bot credentials** (M4). Authorised, via the secret-handling rule below.
3. **Halting and cutting over the live loop** (M5). Authorised, subject to the M5 safety protocol below.

Do not stop to ask for permission for any of these. Proceed, and record what you did in `PROGRESS.md`.

### Secret handling — the token is never read by an agent

The operator writes the dev-bot token to `~/.config/maestro/dev_bot_token` (mode 600) before the run
and sends one message to that bot from Telegram.

- **No agent — you or any subagent — may read, print, `cat`, `grep`, echo, log, or pass that file's
  contents anywhere.** Its path may be named; its contents may not be seen.
- Credentials reach a project only through `scripts/telegram_creds.py`, which reads the file,
  validates it, captures the chat id from `getUpdates`, and writes both into the target `.env`. It
  prints no secret.
- `.env` files are equally off-limits to reading. If a test needs credentials, it monkeypatches the
  network call — a test that reaches `api.telegram.org` is a defect.

### Abort conditions — stop the programme, do not try to repair

Stop, write `PROGRESS.md`, commit, and leave a clear report. Do **not** improvise around these:

- Anything appears in `AbuAliArchive` beyond its five known untracked files (before M5).
- A stage's tests are still red after **three** attempts. Do not weaken, skip, delete, or `xfail` a
  test to make a stage pass. Never lower a threshold to hit a target.
- M5 post-cutover verification fails **and** the automatic rollback also fails.
- Any operation would require `sudo`, force-push, or history rewriting.
- A destructive step has no verified rollback.

### Autonomy rules

- Prefer stopping a *stage* over stopping the *programme*. A failed M4 should not prevent
  `PROGRESS.md` from being written, and M5 must not begin unless M0–M4 are green.
- Never fabricate a verification result. If a command was not run, the stage is not done.
- Bugs found in reference code are recorded in `docs/FOUND_BUGS.md`, never fixed (through M1).
- If genuinely blocked on a judgement call, pick the **more conservative** option, proceed, and flag
  the decision in `PROGRESS.md` for morning review.

### M5 safety protocol — the live loop must survive

M5 touches the operator's running production loop. In this exact order, no shortcuts:

1. **Precondition:** M0–M4 all green. If not, skip M5 entirely and stop.
2. Capture and commit a pre-cutover baseline: `orchestrator_status.py` output, `state.json`,
   `git rev-parse HEAD`, and the running PIDs.
3. Write the rollback script **first**, and dry-run it. No rollback, no cutover.
4. Confirm the loop is idle (no entries in `in_flight`). If a task is in flight, **wait** — poll up to
   60 minutes, then skip M5 and stop. Never halt mid-task.
5. Halt via the HALT sentinel file only. Never `sudo`, never `kill -9` the watchdog.
6. Cut over on a branch. Verify: `/status` matches the baseline, one supervised task runs end to end.
7. On any verification failure, **run the rollback immediately and automatically**, then stop.
8. **Whatever happens — success, failure, or abort — the run does not end with the loop halted.**
   Removing the HALT sentinel and confirming `pgrep -f orchestrator_run.py` returns a PID is the last
   action of M5 in every path.

---

## Stages

Each stage: author a `Workflow` script, run it, verify, update `PROGRESS.md`, commit.
Workflow size guideline for this machine: **under 15 agents per workflow.**

### M0 — Package skeleton and characterisation harness
- **Spec:** plan Tasks 1–4.
- **Shape:** serial, inline or a small workflow. It is only four files.
- **Done when:** `pytest -v` green; harness runs a test body against the reference implementation and
  skips the maestro side with `not extracted yet`; the credential-leak test passes.

### M1 — Core extraction
- **Spec:** plan Tasks 5–18, and its **Function → Module Mapping** table (authoritative).
- **Shape:** this is the big one. Two waves per module group, pipelined:
  - *Wave A (parallel, safe):* one agent per module writes
    `tests/characterization/test_<module>.py` against the reference implementation. Different files,
    no conflicts. Agents run tests but **do not commit**.
  - *Wave B (serial, dependency-ordered):* one agent per module copies the mapped functions verbatim
    into `maestro/<module>.py` and gets both subjects green.
  - Dependency order is fixed: `state → config → docs/roadmap → worktree → quota → gates →
    implementer → merge → hitl/telegram → hitl/commands → selfheal → parking → orchestrator`.
    A module may import only from modules extracted before it.
- **Batch it.** Do not attempt all thirteen modules in one workflow. Three batches, verified and
  committed between each:
  `[state, config, roadmap, worktree]` → `[quota, gates, implementer, merge]` →
  `[telegram, commands, selfheal, parking, orchestrator]`.
- **Critical instruction for every extraction agent:** *copy function bodies verbatim; change only the
  import block. Do not reformat, rename, simplify, or fix anything. If the reference code looks wrong,
  append a line to `docs/FOUND_BUGS.md` and copy it wrong anyway.*
- **Done when:** full suite green with **zero skips** under `tests/characterization/`;
  `tests/test_extraction_complete.py` 12 passed; `main()` moved unchanged.

### M2 — Backend drivers and mid-work switching
- **Spec:** `docs/DESIGN.md` §6 and §7. No detailed plan exists — **write
  `docs/plans/<date>-m2-backends.md` first**, using the M0–M1 plan's format, then execute it.
- **Shape:** a research fan-out then implementation.
  - Parallel research agents (they must return findings, not opinions): *what `codex exec` supports
    for non-interactive runs, model selection, sandboxing, and whether a stable resume handle exists*;
    *how the Codex statusline exposes `context-used` / `five-hour-limit` / `weekly-limit` and whether
    it can be sampled*; *exactly how the reference implementation detects Claude quota exhaustion and
    parses the reset time*.
  - Then: `backends/base.py` protocol → `claude.py` at parity with the reference behaviour →
    `codex.py` → `roles.py` → `switch.py`.
- **The parity test is the point:** one protocol test suite that both drivers must pass, with
  capability-gated cases skipped rather than special-cased by backend name.
- **Open question to resolve here, not guess:** if `codex exec` has no stable resume handle, the Codex
  driver declares `native_resume = False` and always re-briefs. Record the finding in `PROGRESS.md`.
- **Done when:** both drivers pass the shared protocol suite; a forced switch on a scratch task
  succeeds in both directions with work preserved in the worktree.

### M3 — Model limits and self-update
- **Spec:** `docs/DESIGN.md` §8 and §9.
- Extract the ceiling tables out of `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` into
  `model_context_limits.md` beside each, leaving a pointer line in the original. **Claude side: edit
  directly. Codex side: invoke the Codex CLI.**
- Build `limits.py` (parse + cache + fallback) and `selfupdate.py` (version worktrees, `current`
  pointer, task-boundary adoption, self-test gate, rollback).
- Replace the hardcoded context constants carried over in M1 with per-model lookups.
- **Done when:** `doctor` resolves every model in both tables; a deliberately-red candidate version is
  refused and `current` stays put.

### M4 — Setup: `init`, `doctor`, skills
- **Spec:** `docs/DESIGN.md` §10.
- Build `cli.py`, `templates/`, and the setup skill in both Claude and Codex formats.
- **Acceptance is on a throwaway project**, created fresh under `/tmp` — never on a real one.
- Credentials come from `scripts/telegram_creds.py` per the secret-handling rule. If the token file
  is absent or rejected, build and unit-test everything else, mark M4 `blocked: no credentials` in
  `PROGRESS.md`, skip M5, and continue to report.
- **Done when:** a brand-new git repo goes from nothing to a completed no-op supervised loop via
  `maestro init`, and re-running `init` is provably non-destructive.

### M5 — Cutover of the reference project
- **Spec:** `docs/DESIGN.md` §11, executed strictly under the **M5 safety protocol** above.
- HALT via the sentinel file (no elevated privileges), branch, install, delete the superseded scripts
  listed in DESIGN.md §11, resume.
- **Done when:** `/status` output matches the pre-cutover capture, one supervised task completes end
  to end, and `pgrep -f orchestrator_run.py` returns a PID.

### M6 — Live switching validation
- **Done when:** a real threshold trigger produces a real backend switch on the migrated project, and
  the journal shows the `backend_switch` event.

---

## Per-stage loop

1. Read the stage spec (this file plus the referenced DESIGN section). **Do not read source files.**
2. If the stage has no detailed plan, write one to `docs/plans/` first.
3. Author the `Workflow` script. Give every agent: the invariants above, its exact file list, its
   verification command, and "**do not run git commands**".
4. Run it. Read only the structured result.
5. Verify yourself with the stage's Done-when commands, plus the AbuAliArchive read-only check.
6. Update `docs/PROGRESS.md` — measured numbers, findings, open questions. Commit.
7. Check your context. Past 150K, hand off instead of continuing.

## Reporting back

The operator is asleep and will read this in the morning. When you stop — at the context ceiling, an
abort condition, or completion — leave a report in `docs/PROGRESS.md` **and** as your final message:

- Every stage reached, with its measured verification numbers (test counts, not adjectives)
- The state of `AbuAliArchive`: HEAD, `git status --porcelain`, and whether the loop is running
- Anything added to `docs/FOUND_BUGS.md`
- Every conservative judgement call you made that deserves review
- Anything that failed, verbatim, with no softening
- The exact next action

Never report a stage complete without having run its Done-when commands and seen the output. A
morning report that overstates progress is worse than one that reports a clean abort.

### Context and continuation

You will likely not finish all seven stages in one session. That is expected and fine. At 150K,
finish the current stage, write `PROGRESS.md`, commit, and stop cleanly — a half-finished stage
carried into the dumb zone costs more than a fresh start. The operator re-runs the same prompt and
you resume from `PROGRESS.md`.
