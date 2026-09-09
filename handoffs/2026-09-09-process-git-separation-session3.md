# Continue: process/project git separation (session 3)

## Goal

Same task as `handoffs/2026-09-09-process-project-git-separation.md` — **read that file
first, in full**, then `handoffs/2026-09-09-process-git-separation-session2.md` for
session 2's additions. This handoff only records what changed in session 3 and what's
next. The three-tier carve-out table, out-of-scope items, and verification checklist in
the original still govern — don't re-derive them.

## What happened in session 3 (2026-09-09, this branch)

1. Re-confirmed no P0 session active (`.orchestrator/state.json` empty in both repos, no
   `maestro-impl-*` worktrees, no relevant tmux windows) — **re-check again before your
   first `git rm --cached`**, same standing rule as every prior session.
2. Re-verified `docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md`'s gitignored
   edits survived. Note for whoever checks next: the handoff's exact grep string
   `"maestro-setup skill"` doesn't literally match — the doc has `` **`maestro-setup`
   skill** `` (markdown emphasis splits the string). `_derive_facts` matches fine. Content
   is present and correct; the earlier grep instruction is just too literal. Don't treat a
   miss on that exact string as data loss — check for the substance instead.
3. Read `maestro/metabranch.py` in full (written session 1, unwired). It's solid — no
   changes made to it this session.
4. Wired two of the three `reconcile_meta` attach points ruled on in session 2 — commit
   `02b7430` on this branch:
   - **`maestro/merge.py`** — both `git push origin main` call sites (`_merge_data_only`
     line ~558, `_resumable_code_change_escalation` line ~640) now check the push's
     return code. On failure: `append_journal("merge_push_failed", ...)` with the
     stderr, and return a new `"push_failed"` status instead of silently claiming `"ok"`
     (the second site also sends a `notify_telegram` warning, since it previously always
     told Dan "✓ merged" regardless of push outcome). On success: call
     `metabranch.reconcile_meta(REPO)` right after, per session 2's ruling. Added
     `from maestro import metabranch` to the imports.
     - `parking.py:530`'s `elif merge_status not in ("ok", "empty"): _park(...)` already
       handles any unrecognized status by parking for Dan with the status string in the
       message — so `"push_failed"` on the first call site surfaces correctly with **no
       parking.py change needed**. Confirmed by reading, not yet by test.
   - **`maestro/cli.py`**:
     - Replaced the `_GITIGNORE_BLOCK` constant with `_gitignore_block()`, a function
       that generates the tier-2 (versioned-on-`meta`) ignore lines from
       `metabranch.META_PATHS` instead of hardcoding a second copy of that list —
       honoring the "single source of truth" claim already written into
       `metabranch.py`'s own docstring. The tier-3 ("Neither") lines stay hardcoded in a
       new `_GITIGNORE_HEADER` constant. A path with a `.` in its basename (e.g.
       `docs/PROGRESS.md`) is emitted as a file; everything else gets a trailing `/`.
     - `cmd_init` now calls `metabranch.reconcile_meta(repo_root)` right after all
       scaffolding (`_ensure_venv_python_symlink`) and before the created/unchanged
       summary, printing `[init] meta branch (process docs): <detail>`.
     - Added `_check_meta_branch(repo_root) -> Check` (WARN, not required — same
       register as `_check_model_limits` etc.) right before `_check_adapters`, calling
       `reconcile_meta` and flagging not-ok only if the detail string contains "error" or
       "failed". Wired into `cmd_doctor`'s non-`--pre-commit` check list, right after
       `_check_thirdparty()`.
   - Verified both files still import cleanly (`python -c "import maestro.cli"` /
     `maestro.merge"`, zero output = success) before committing.
5. **Did not touch `orchestrator.py` yet** — this is the third attach point session 2
   ruled on, and it's the most important one per that ruling ("the only one of the three
   guaranteed to run automatically... without a person triggering it"). Do this first
   next session.

## Session ended here on context-budget signal (~115.6K tokens), not a blocker

No open question waiting on Dan. Everything below is mechanical continuation of session
2's already-approved plan.

## In scope — carried over, annotated with status

- [x] `maestro/metabranch.py` written (session 1) — not wired
- [x] `reconcile_meta` wired into `merge.py`'s two push sites (session 3, commit `02b7430`)
- [x] `reconcile_meta` wired into `cmd_init` (session 3)
- [x] `_check_meta_branch` doctor check wired into `cmd_doctor` (session 3)
- [ ] **Wire `reconcile_meta` into `orchestrator.py`'s startup path.** Exact location
  found this session: `maestro/orchestrator.py:1589`, right after
  `in_flight: list[dict] = _reconcile_and_persist_startup_state(state, launch_times)` and
  `run_dep_map()` / `run_status()`, **before** the `while True:` event loop begins (do
  this once at startup, NOT inside the loop — reconcile_meta's own docstring says "safe
  to call at orchestrator startup... and after every main push", not every poll cycle;
  calling it every iteration would `shutil.copytree` `docs/graph-engineering/` on every
  poll for no reason). Add `from maestro import metabranch` alongside the existing
  `from maestro import metrics` import near the top (line ~68) — this file uses
  top-level imports, unlike `cli.py`'s lazy-import convention, so match that. Print
  something like `print(f"[meta] {metabranch.reconcile_meta(REPO)}")`.
- [ ] Unit tests for `metabranch.py` — still no test file. Look at
  `tests/characterization/test_worktree.py`'s `sandbox` fixture (repoints `REPO` into
  `tmp_path`, real `git init`) — same pattern `tests/characterization/test_merge.py`'s
  own `repo` fixture uses (see that file, lines 48-70, read this session — it sets
  `GIT_CONFIG_GLOBAL`/`GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars so commits succeed
  without touching the operator's real git config). You'll need a second throwaway
  **bare** remote (`git init --bare` in another `tmp_path` dir, `git remote add origin
  <path>`) to test `push_meta` and the fresh-clone reconnect branch of
  `ensure_meta_branch` (the `_branch_exists_remote` → fetch → `worktree add -b` path).
- [ ] **Fix the now-stale test**: `tests/characterization/test_merge.py:1040`
  `test_merge_data_only_pushes_and_ignores_the_failure` — docstring says "the result is
  still ok"; asserts `subject._merge_data_only({"branch": "b"}) == "ok"`. This is now
  **wrong** — the sandboxed repo has no `origin` remote, so `git push` fails locally
  (confirmed this is deliberate test infra per the module docstring's third bullet,
  lines 11-12), meaning the fixed code will return `"push_failed"`, not `"ok"`. Rename
  the test to something like `test_merge_data_only_reports_a_failed_push` and assert
  `== "push_failed"` instead; keep the `rec.argvs` push-was-attempted assertion. Also add
  a companion test for `_resumable_code_change_escalation`'s push-failure path (currently
  no test covers line ~640's push at all — grep `tests/characterization/test_merge.py`
  for "push" found only the one now-stale test). Check `_passthrough`'s definition
  (used at line 1044) for the recording-subprocess pattern already in this test file.
  **Run this file in isolation first** (`.venv/bin/python -m pytest
  tests/characterization/test_merge.py -q`) before the full suite, since this is the
  one file guaranteed to need edits.
- [ ] Three-tier `.gitignore` for **maestro's own repo root** (not the scaffolding
  template in `cli.py` — that's done; this is `/home/dan/projects/maestro/.gitignore`
  itself, still the old 13-line version). Replace the `docs/graph-engineering/`
  blanket-ignore block (current lines 11-13) with entries for all of
  `metabranch.META_PATHS` (handoffs/, docs/plans/, docs/superpowers/,
  docs/graph-engineering/, docs/PROGRESS.md, tasks/lessons.md) plus a comment matching
  the one now in `cli.py::_GITIGNORE_META_COMMENT` (reuse that wording, it's already
  written).
- [ ] **Migrate maestro's tracked process files to `meta`.** Recount right before doing
  this — session 2 estimated 28 (11 handoffs, 12 docs/plans, 3 docs/superpowers, plus
  PROGRESS.md and lessons.md), but this session added handoff files too (this file is
  the 12th under `handoffs/` once you count it — check with
  `git ls-files | grep -cE '^(handoffs|docs/plans|docs/superpowers)/'` for the real
  number right before running `git rm --cached`, don't trust any prior session's count).
  Steps: `git rm --cached` each file individually (not `rm` — keep them on disk), add
  the new `.gitignore` rules, commit on `master`... except **this branch is
  `feat/graph-engineering-foundation`, not `master`** — re-read the original handoff's
  "Current state" section: HEAD is on the feature branch, but the migration commit
  target is described as "on master" throughout. Resolve this ambiguity before cutting
  the commit: either the migration happens on this feature branch (consistent with every
  other commit this whole task has made) and merges to master later with the rest of the
  branch, or it needs `git checkout master` first. **This wasn't resolved in any prior
  session — flag it, don't guess silently.** The safest default given three sessions of
  precedent (every commit so far, including metabranch.py itself, went onto
  `feat/graph-engineering-foundation`) is to keep doing that and let the branch merge
  normally later — but say so explicitly when you do it, don't let it pass silently.
  Then use `metabranch.ensure_meta_branch(REPO) → sync_meta(REPO) → push_meta(REPO)` (now
  wired live via `cmd_doctor`/`cmd_init`, or call the three functions by hand) to land
  the content on `meta`'s first real commit.
- [ ] Apply the same layout to **duetflow** — add `.orchestrator/` to its `.gitignore`
  (confirmed missing again this session), `git rm --cached tasks/lessons.md`, migrate it
  to a `meta` branch there via the same three `metabranch` calls (duetflow doesn't import
  the `maestro` package, so this has to be done by hand with raw git commands mirroring
  what `metabranch.py` does, or via a small standalone script — duetflow has no
  dependency on maestro's Python package to call into). duetflow has no `handoffs/`,
  `docs/plans/`, or `docs/PROGRESS.md` yet, so it's just the one file.
- [ ] `~/.claude/skills/maestro-setup/SKILL.md` Step 1 (starts line 60) — two changes:
  1. Add a third pre-flight bullet after the existing two (lines 83-86), same register
     (state the failure mode and its consequence): something like — *"Is the `meta`
     branch reachable? `reconcile_meta` needs the same remote push access as `main` to
     keep process docs (handoffs, plans, progress, lessons) versioned — if the remote
     check above already failed, `meta` will fail identically and process docs will only
     ever exist in the local sibling worktree. Not fatal — `init`, `doctor`, and
     orchestrator startup all retry automatically — but say so up front."*
  2. **Update the second bullet (lines 83-86)** — it currently says `merge.py` runs the
     push "with its return code unchecked... silently never leaves the machine." That's
     no longer true as of this session's `merge.py` fix. Rephrase to something like: "a
     failed push now surfaces (a `push_failed` status, a journal entry, and — on the
     Opus-approved path — a Telegram warning) rather than silently claiming success, but
     if there's no remote at all, nothing pushes regardless. Say so plainly."
- [ ] Visual artifact (Dan's standing rule, mandatory per CLAUDE.md's Collaboration
  Policy for a multi-stage change proposal) — two diagram-led views: (1) the three-tier
  file/branch layout (project vs. process vs. neither, master vs. meta vs. gitignored),
  (2) the automated git sequence from task-complete through merge to both pushes
  (`merge.py` → push main → `reconcile_meta` → push meta). Deliver via `send-to-me`.
  **Not started in any session yet.**

## Out of scope

Unchanged: no history rewriting, no P0 baseline work, no `instagram-to-value`
onboarding, don't touch `implementer.py`'s task brief or move `ROADMAP.md`/`PROJECT.md`,
one remote only.

## Parallel-session coordination

Unchanged from prior sessions: this task owns `.gitignore` (both repos),
`maestro/merge.py`, `maestro/cli.py`, `maestro/orchestrator.py` (startup call site),
`maestro/metabranch.py`, `~/.claude/skills/maestro-setup/SKILL.md`, and the
branch/worktree layout of both repos. Leave `tests/graph_engineering/`,
`scripts/graph_baseline.py`, `maestro/metrics.py` alone.
**Re-run the P0-session check above before your first `git rm --cached`.**

## Verification to report back

Same 10 items as the original handoff's "Verification to report back" section — **none
have been run yet across any of the three sessions**. Report actual numbers and exit
codes, not adjectives. Given how much is now wired, this is close enough to attempt a
subset now: item 1 and 2 (file counts) can be checked after the migration step above;
item 6 (fresh-clone automation proof) can finally be exercised for real now that
`ensure_meta_branch`/`cmd_init` are wired; item 7 (forced push failure) can be exercised
directly against `merge.py`'s new code path with a bogus remote — this is probably the
highest-value thing to prove first since it's the actual bug fix.

## Notes

- Current branch: `feat/graph-engineering-foundation`. One new commit this session:
  `02b7430` ("wip(meta): wire reconcile_meta into merge.py push sites, cli.py
  init/doctor"). Working tree is clean as of session end.
- This handoff is itself a process doc — it migrates to `meta` along with the others
  once the migration step runs. Include it in whatever count you take at that time.
- Nothing here blocks on Dan. The unresolved item flagged above (which branch the
  migration commit lands on) is a judgment call for whoever continues, not a question
  that needs to go back to Dan first — the "keep it on this feature branch" default is
  reasonable and consistent with every commit so far; just say explicitly that's what
  you're doing when you do it.
