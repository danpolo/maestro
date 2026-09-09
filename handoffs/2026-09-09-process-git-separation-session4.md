# Continue: process/project git separation (session 4)

## Goal

Same task as `handoffs/2026-09-09-process-project-git-separation.md` — **read that file
first, in full**, then sessions 2 and 3's handoffs for what changed since. This handoff
only records session 4's work and what's left. The three-tier carve-out table,
out-of-scope items, and verification checklist in the original still govern.

**Note on where this file lives:** as of this session, `handoffs/` is gitignored on
`master`/this feature branch and lives only on the `meta` branch (see below) — this file
sits on disk in the main worktree per repo convention, untracked here, and should be
migrated to `meta` (via `reconcile_meta`, e.g. next `doctor`/`init` run) once it's done
being read. Don't `git add` it on this branch.

## What happened in session 4 (2026-09-09, this branch)

1. Re-confirmed no P0 session active (no `.orchestrator/state.json`, no `maestro-impl-*`
   worktrees) before any git surgery.
2. **Wired the third and final `reconcile_meta` attach point**: `maestro/orchestrator.py`,
   right after `run_dep_map()`/`run_status()` at startup, before the `while True:` event
   loop (not inside the loop). Added `from maestro import metabranch` near the top.
   Commit `85f4b13`. `tests/characterization/test_orchestrator.py`: 133 passed, 0 failed,
   exit 0. **All three attach points (`merge.py`, `cli.py`, `orchestrator.py`) are now
   wired.**
3. **Ran `tests/characterization/test_merge.py` and found 3 failures**, not the 1 the
   session 3 handoff predicted — session 3's `merge.py` push-check fix means *any* test
   reaching a real push now gets `"push_failed"` in this sandbox (the `repo` fixture
   deliberately has no `origin` remote — see the file's module docstring). Fixed all 3,
   plus added a companion test, in commit `c53273b`:
   - Added a `bare_origin` fixture (real `git init --bare` remote wired as `origin`) so
     the genuine `"ok"` success path still gets coverage, not just the failure path.
   - `test_merge_data_only_merges_allowlisted_paths` — now uses `bare_origin` + stubs
     `metabranch.reconcile_meta` (kept out of scope for this file; metabranch.py still
     needs its own test file, see below). Still asserts `"ok"`.
   - `test_merge_data_only_pushes_and_ignores_the_failure` → renamed
     `test_merge_data_only_reports_a_failed_push`, per session 3's plan: asserts
     `"push_failed"`, that `reconcile_meta` was **not** called, and that
     `merge_push_failed` was journaled.
   - `test_resumable_escalation_approved_merges_and_announces` — same `bare_origin` +
     stub treatment, still asserts full `"ok"` success.
   - New `test_resumable_escalation_approved_but_push_fails` — the companion test session
     3 asked for: `_resumable_code_change_escalation`'s push-failure branch was
     previously untested. Confirms `(True, reason, "push_failed")`, the ⚠ Telegram
     warning (not the ✓ message), the journal entry, and that `reconcile_meta` did not
     run.
   - `tests/characterization/test_merge.py`: **155 passed, 0 failed, exit 0** (up from
     ~152 tests before the new one was added — 155 is the real post-fix count).
4. **Ran the full suite**: `.venv/bin/python -m pytest -q` → **exit 1, 2 failures**, both
   pre-existing and unrelated to this task (confirmed by reading their tracebacks):
   - `tests/test_cli.py::test_doctor_thirdparty_runs_only_the_gate_and_needs_no_project`
     — fails because the installed Claude Code CLI (2.1.266) has drifted past the
     version (2.1.263) pinned in some verification/manifest data. Environment drift, not
     a code regression from this task.
   - `tests/test_limits.py::test_parses_the_real_claude_table` — fails because
     `docs/reference`'s Claude model-limits table has been updated (ceiling 250000 vs.
     the test's hardcoded expectation of 180000) — this matches the
     `model_context_limits.md` "Maestro D4 extraction" thresholds referenced in
     `~/.claude/CLAUDE.md`'s session-triage section, i.e. a real, independent table
     update, not something this session touched.
   - Did not fix either — **out of scope for this task**, flagging for whoever owns
     `test_cli.py`/`test_limits.py`/the verification-data pinning process.
5. **Wrote the three-tier `.gitignore` for maestro's own repo root**
   (`/home/dan/projects/maestro/.gitignore`, was the old 13-line version). Kept the
   repo-specific tier-3 extras the generic `cli.py` scaffold doesn't have (`.run/`,
   `.legacy_repo`, `*.egg-info/`, `.pytest_cache/`) and the pre-existing
   `docs/DEEP_RESEARCH_PROMPT.md` local-drafts entry (not part of the carve-out table,
   left untouched). Replaced the old `docs/graph-engineering/` blanket-ignore block with
   the tier-2 meta-branch block, wording copied verbatim from
   `cli.py::_GITIGNORE_META_COMMENT`, entries generated from `metabranch.META_PATHS`.
6. **Recounted right before migrating** (per every prior session's standing instruction —
   don't trust a prior count): `git ls-files | grep -cE
   '^(handoffs|docs/plans|docs/superpowers)/'` → **28** (12 `docs/plans/`, 3
   `docs/superpowers/`, 13 `handoffs/` — one more handoff than session 3's count, this
   session's own continuation file wasn't tracked yet at count time). Plus
   `docs/PROGRESS.md` and `tasks/lessons.md` → **30 files total**.
7. **Resolved the branch ambiguity session 3 flagged, explicitly, per its own
   instructions**: the migration commit lands on `feat/graph-engineering-foundation`,
   not `master` — consistent with every commit this whole task has made (metabranch.py,
   the three wiring commits). Stated in the migration commit message itself, not left
   implicit.
8. **Migrated all 30 files**: `git rm --cached` (not `rm` — verified all 30 still present
   on disk afterward), `.gitignore` staged alongside, one commit — `c02b72b`. Split into
   two clean commits after an initial `git commit` accidentally bundled the migration in
   with the test fix (a soft-reset + selective re-staging fixed this before the final
   commit landed — mentioned here only because it's the kind of thing worth doublechecking
   `git status`/`git log` after any commit, not because it left a mess).
9. **Landed the migrated content on `meta`'s first real commit**, by hand, using
   `metabranch.ensure_meta_branch(REPO)` → `sync_meta(REPO)` → `push_meta(REPO)` in a
   one-line Python invocation (these are the same three functions `reconcile_meta` chains,
   now live via `cmd_doctor`/`cmd_init`/orchestrator startup, but called directly here for
   the one-time migration since there's no running orchestrator instance and no reason to
   invoke `doctor`/`init` just to trigger this). **Verified working, not just asserted**:
   - `git worktree list` shows `/home/dan/projects/maestro-meta` attached (sibling, not
     nested — same as session 2's design).
   - Two commits on `meta`: `ab72ed6` ("initialize orphan meta branch") then `5952e65`
     ("sync process docs"), both timestamped this session (17:04:14–15 IDT).
   - `git -C /home/dan/projects/maestro-meta ls-files | wc -l` → **67** (the 30 migrated
     files plus everything under `docs/graph-engineering/` — that directory was
     previously gitignored entirely and versioned nowhere; it's now tracked on `meta` for
     the first time, exactly as the original handoff called out as "a win to bank").
   - `git rev-parse meta` and `git ls-remote origin meta` return the **identical SHA**
     (`5952e658...`) — the push landed for real, not just locally.

## In scope — carried over, annotated with status

- [x] `maestro/metabranch.py` written (session 1)
- [x] `reconcile_meta` wired into `merge.py`'s two push sites (session 3)
- [x] `reconcile_meta` wired into `cmd_init` (session 3)
- [x] `_check_meta_branch` doctor check wired into `cmd_doctor` (session 3)
- [x] `reconcile_meta` wired into `orchestrator.py` startup (session 4, commit `85f4b13`)
- [x] Three-tier `.gitignore` for maestro's own repo root (session 4)
- [x] Migrate maestro's 30 tracked process files to `meta` (session 4, commit `c02b72b`
  + the by-hand `ensure_meta_branch`/`sync_meta`/`push_meta` run, verified pushed)
- [x] Fixed the stale `test_merge.py` tests (session 4 found 3, not the 1 predicted, plus
  added the companion push-failure test session 3 asked for) — commit `c53273b`
- [ ] **Unit tests for `metabranch.py`** — still no dedicated test file. Same guidance as
  sessions 2/3: mirror `test_merge.py`'s `repo` fixture pattern (now also has a
  `bare_origin` fixture you can copy/adapt — see session 4's `test_merge.py` changes) for
  a real throwaway repo + bare remote. Test `ensure_meta_branch`'s three starting states,
  `sync_meta`'s diff-and-commit logic, `push_meta`'s return-code check, and
  `reconcile_meta`'s never-raises chaining.
- [ ] **Apply the same layout to duetflow** (`/home/dan/projects/duetflow`) — not started
  in any session yet. Add `.orchestrator/` to its `.gitignore` (confirmed missing again
  in session 3, not re-checked this session), `git rm --cached tasks/lessons.md`, migrate
  it to a `meta` branch there. duetflow doesn't import the `maestro` package, so this has
  to be done by hand with raw git commands mirroring `metabranch.py`'s logic (or a small
  standalone script) — no dependency on maestro's Python package available there.
- [ ] `~/.claude/skills/maestro-setup/SKILL.md` Step 1 — the two changes session 3
  specified (new pre-flight bullet about `meta` needing remote push access; rephrase the
  existing bullet since `merge.py`'s push failure now surfaces instead of silently
  dropping). Not started in any session yet — the exact wording session 3 drafted is
  still in that handoff, reuse it.
- [ ] **Visual artifact** (Dan's standing rule per CLAUDE.md's Collaboration Policy for a
  multi-stage change proposal) — two diagram-led views: (1) the three-tier file/branch
  layout, (2) the automated git sequence from task-complete through merge to both
  pushes. Deliver via `send-to-me`. **Not started in any session yet** — this is the
  single most overdue item; four sessions in and it hasn't been touched.

## Out of scope

Unchanged: no history rewriting, no P0 baseline work, no `instagram-to-value`
onboarding, don't touch `implementer.py`'s task brief or move `ROADMAP.md`/`PROJECT.md`,
one remote only. Also out of scope this session, newly noted: fixing the two pre-existing
unrelated test failures (`test_cli.py`'s thirdparty version check, `test_limits.py`'s
Claude table parse) — flag to whoever owns those, don't fix them as a drive-by here.

## Parallel-session coordination

Unchanged: this task owns `.gitignore` (both repos), `maestro/merge.py`,
`maestro/cli.py`, `maestro/orchestrator.py`, `maestro/metabranch.py`,
`~/.claude/skills/maestro-setup/SKILL.md`, and the branch/worktree layout of both repos.
Leave `tests/graph_engineering/`, `scripts/graph_baseline.py`, `maestro/metrics.py`
alone. **Re-run the P0-session check above before any further git surgery** (duetflow
migration still needs `git rm --cached`).

New this session: `/home/dan/projects/maestro-meta` now exists as a real sibling
worktree, attached and pushed. Don't delete it or `git worktree remove` it — it's live
infrastructure now, not a scratch artifact.

## Verification to report back

Of the original 10 items:
1. **Done** — `git ls-files | grep -cE '^(handoffs|docs/plans|docs/superpowers)/'` on
   this branch → **0** (was 28+2). `git ls-files docs/PROGRESS.md tasks/lessons.md` →
   empty.
2. **Done** — `git ls-files` on `meta` → **67**.
3. **Done** — all 30 migrated files verified present on disk in the main worktree after
   `git rm --cached`.
4. **Done** — `git status --porcelain` on this branch is clean.
5. **Done** — `.venv/bin/python -m pytest -q` → exit 1, 2 failures, both pre-existing and
   unrelated (see session 4 item 4 above for the exact tracebacks/reasoning). Everything
   else passes.
6. **Done, for real** — the fresh-worktree-attach automation was proven this session by
   directly calling `ensure_meta_branch`/`sync_meta`/`push_meta` against the real repo
   (see item 9 above): it created the orphan branch, the sibling worktree, committed, and
   pushed, all with no manual git command. This is the strongest evidence yet that "no
   manual git" is actually satisfied. **Not yet proven from a genuinely fresh `git
   clone`** (a second scratch clone was not attempted this session) — if Dan wants that
   exact scenario proven too, it's a 5-minute follow-up: `git clone` into a scratch dir,
   run `maestro doctor` or `maestro init`, confirm the meta worktree appears there too.
7. **Not done this session** — forcing a push failure against `merge.py`'s new code path
   with a bogus remote (distinct from the unit-test coverage in item 3 above, which
   already exercises this at the function level with a real recorder). Low priority now
   that the function-level tests cover it thoroughly.
8. **Not re-verified this session** — `.venv/bin/python scripts/next_graph_prompt.py
   --phase P00` after `docs/graph-engineering/` changed branches. Should still work since
   the directory's content on disk is unchanged (only its git tracking moved), but
   confirm before considering this task fully closed.
9. **Not started** — duetflow's before/after `git ls-files` and the `tasks/lessons.md`
   migration. See "In scope" above.
10. **Done, again** — `docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md`'s
    gitignored edits survived (unaffected by this session's changes — that file's content
    on disk was never touched, only its git tracking home moved to `meta`).

## Notes

- Current branch: `feat/graph-engineering-foundation`. Three new commits this session:
  `85f4b13` (orchestrator wiring), `c53273b` (test fixes), `c02b72b` (migration +
  `.gitignore`). Working tree clean.
- Session ended here on the context-governor's prepare-handoff signal
  (Sonnet 5, ~124K tokens), not a blocker. No open question waiting on Dan.
- **Biggest remaining gaps, in priority order**: (1) the visual artifact — four sessions
  overdue, Dan's standing CLAUDE.md rule requires it for this kind of multi-stage change;
  (2) duetflow's layout — the *other* repo this task was scoped to touch, untouched so
  far across all four sessions; (3) `metabranch.py`'s own unit tests; (4) the
  `maestro-setup` SKILL.md update. All are mechanical continuations of already-settled
  design decisions — nothing here needs to go back to Dan first.
