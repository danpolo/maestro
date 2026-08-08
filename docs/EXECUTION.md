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

## Human gates — stop and ask

Three stages cannot run unattended. At each, stop, report status, and wait.

| Gate | Before | Why | What to ask for |
|---|---|---|---|
| **G1** | M3 | Edits `~/.claude/model_context_limits.md` and `~/.codex/` — machine-global, affects every project and session | Show the exact proposed diffs for both files and get explicit approval. The `~/.codex/` side must be performed by invoking the Codex CLI |
| **G2** | M4 acceptance | Needs a real Telegram bot token and chat id, which only the operator can create | Ask the operator to create a dev bot via @BotFather and provide the token. Scaffolding and `doctor` can be built *before* the gate; only the live acceptance run waits |
| **G3** | M5 | Halts and cuts over the operator's live production loop | Present the cutover checklist and rollback procedure, get explicit go-ahead |

M0, M1, M2 run unattended end to end. Expect to reach G1 in one session.

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

### M3 — Model limits and self-update  ⛔ **G1 first**
- **Spec:** `docs/DESIGN.md` §8 and §9.
- Extract the ceiling tables out of `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` into
  `model_context_limits.md` beside each, leaving a pointer line in the original. **Claude side: edit
  directly. Codex side: invoke the Codex CLI.**
- Build `limits.py` (parse + cache + fallback) and `selfupdate.py` (version worktrees, `current`
  pointer, task-boundary adoption, self-test gate, rollback).
- Replace the hardcoded context constants carried over in M1 with per-model lookups.
- **Done when:** `doctor` resolves every model in both tables; a deliberately-red candidate version is
  refused and `current` stays put.

### M4 — Setup: `init`, `doctor`, skills  ⛔ **G2 before the acceptance run**
- **Spec:** `docs/DESIGN.md` §10.
- Build `cli.py`, `templates/`, and the setup skill in both Claude and Codex formats.
- **Acceptance is on a throwaway project**, created fresh under `/tmp` — never on a real one.
- **Done when:** a brand-new git repo goes from nothing to a completed no-op supervised loop via
  `maestro init`, and re-running `init` is provably non-destructive.

### M5 — Cutover of the reference project  ⛔ **G3 first**
- **Spec:** `docs/DESIGN.md` §11.
- HALT via the sentinel file (no elevated privileges), branch, install, delete the superseded scripts
  listed in DESIGN.md §11, resume.
- **Rollback must be written down and verified before the halt.**
- **Done when:** `/status` output matches the pre-cutover capture, and one supervised task completes
  end to end.

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

When you stop — at a gate, at the context ceiling, or at completion — report:
- Stage reached and its measured verification numbers (test counts, not adjectives)
- Anything in `docs/FOUND_BUGS.md` added this session
- Open questions that need the operator
- The exact next action

Never report a stage complete without having run its Done-when commands and seen the output.
