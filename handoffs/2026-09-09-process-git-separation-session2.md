# Continue: process/project git separation (session 2)

## Goal

Same task as `handoffs/2026-09-09-process-project-git-separation.md` — **read that file
first, in full; this handoff only records what changed since it was written and what to
do next.** It still governs scope, the three-tier carve-out table, out-of-scope items,
and the verification checklist. Do not re-derive those; they haven't changed.

Also read `docs/DESIGN.md` §5 and §10 (already done last session, still current).

## What happened in session 1 (2026-09-09, this branch)

1. Confirmed no P0 session is active in either repo — `.orchestrator/state.json` in
   maestro is empty/unparseable (not JSON), no `.orchestrator/` in duetflow, no
   `maestro-impl-*` worktrees, no relevant tmux windows. **Safe to proceed with git
   surgery** — re-confirm this is still true before your first `git rm --cached` if any
   time has passed, per the original handoff's coordination note.
2. Safety-committed the 5 previously-uncommitted files plus this task's own handoff —
   commit `ae06eff` on `feat/graph-engineering-foundation`.
3. Verified `docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md`'s gitignored
   "Scratch Project Pilot" edits survived (grepped for `maestro-setup skill` and
   `_derive_facts` — both present). Re-verify this again at the end per the original
   handoff's item 10 — it should be untouched by anything in this task.
4. Wrote `maestro/metabranch.py` — commit `3b122d6`. **Unwired and untested.** It has:
   - `META_BRANCH = "meta"`, `META_PATHS` (the tier-2 list: `handoffs`, `docs/plans`,
     `docs/superpowers`, `docs/graph-engineering`, `docs/PROGRESS.md`, `tasks/lessons.md`)
   - `meta_worktree_path(repo_root)` → `<repo_root>.parent/<repo_root.name>-meta`, e.g.
     `/home/dan/projects/maestro-meta` (sibling, not nested — confirmed this satisfies
     the original handoff's "do not let the meta worktree land inside the repo tree").
   - `ensure_meta_branch(repo_root, remote="origin")` — handles all three starting
     states (nothing exists / branch on remote only / already attached). Creates the
     orphan branch by hand (`worktree add --detach` + `checkout --orphan` inside it,
     since this repo's git is 2.39.5 and has no `worktree add --orphan` flag).
   - `sync_meta(repo_root)` — copies META_PATHS from the main tree into the meta
     worktree and commits if changed. **Design decision, not yet reviewed by Dan:**
     one-way sync (main tree stays the working copy everyone edits normally; meta
     worktree is a derived, committed mirror), not symlinks. Rationale: symlinks would
     make the meta worktree's existence load-bearing for every read of e.g.
     `docs/PROGRESS.md`, and a reconcile step that recreates the worktree would
     transiently break every reader. Copy+commit means the main tree is always
     self-sufficient. **This is a ruling, not a fact — flag it to Dan if he has a
     reaction, since it doubles disk usage for these paths (small — text docs only).**
   - `push_meta(repo_root, remote)` — checked return code (the thing merge.py's two
     call sites currently don't do for `main`).
   - `reconcile_meta(repo_root, remote)` — chains all three, never raises. This is the
     function meant to be called from three places (none wired yet — see below).

## Ruling made so far

- **Attach-point decision (original handoff asked for this, with a "say why"):** call
  `reconcile_meta` from all three places DESIGN.md's setup section and the daemon
  pattern support, not just one:
  1. `cli.py::cmd_init` — so a brand-new project has the meta branch from first setup.
  2. `cli.py::cmd_doctor` (non-`--pre-commit` path) — add a `Check` (see the `Check`
     dataclass at `maestro/cli.py:641` and `_check_docs` at `:648` for the pattern this
     project already uses) so drift is visible in normal doctor output, matching the
     register other doctor checks use (WARN/FAIL + a one-line detail).
  3. `orchestrator.py`'s startup path (see `reconcile_in_flight`, called "at startup AND
     once per poll cycle" per its own docstring around `maestro/orchestrator.py:1256`)
     — this is what actually satisfies "I don't want to manage anything manually" for
     the fresh-clone case, since it runs unattended without anyone invoking `init` or
     `doctor` by hand.
  - **Why all three, not one:** `init` seeds day one, `doctor` gives a human-visible
    signal, orchestrator startup is the only one of the three guaranteed to run
    automatically and repeatedly without a person triggering it. Costs: three call
    sites to keep in sync if `reconcile_meta`'s signature ever changes — acceptable,
    it's a 3-line call at each site.
  - Not yet implemented. This is the next task.

## In scope — carried over, annotated with status

From the original handoff's "In scope" list:

- [x] `maestro/metabranch.py` written (this session) — **not wired, not tested**
- [ ] Wire `reconcile_meta` into `cmd_init`, `cmd_doctor`, orchestrator startup
- [ ] Unit tests for `metabranch.py` — no test file exists yet. Look at
  `tests/characterization/test_worktree.py` (the `sandbox` fixture, `git init` inside
  `tmp_path`, `REPO` repointed via monkeypatch or `MAESTRO_REPO` env — check how
  `sandbox` actually repoints things, this session didn't get far enough to read it)
  for the established pattern of testing a git-shelling module against a throwaway
  repo instead of the real one. You'll also need a throwaway **remote** (a second
  `git init --bare` tmp dir) to test `push_meta` and the fresh-clone reconnect path in
  `ensure_meta_branch`.
- [ ] Fix `maestro/merge.py`'s two unchecked `git push origin main` calls — lines were
  **558 and 640 as of commit `5ac9bca`**; re-grep, they will have moved by ~184 lines
  in this file's numbering only if merge.py itself was touched (it wasn't this
  session, so line numbers should still be close). Check the return code, log/return a
  distinct failure (a new status string, e.g. `"push_failed"` — check what the two
  call sites currently return on other paths and match that shape), and call
  `metabranch.reconcile_meta(REPO)` right after a successful main push at both sites.
- [ ] Three-tier `.gitignore` for maestro (see the original handoff's table). Current
  `.gitignore` is 13 lines, ends with a `docs/graph-engineering/` blanket-ignore
  comment block that this task's tier-2 rule replaces (docs/graph-engineering stays
  ignored on master — it moves to being *tracked on meta*, not to being tracked on
  master).
- [ ] Migrate maestro's 27 tracked files to meta: `git rm --cached` (not `rm`) each of
  the 10 `handoffs/`, 12 `docs/plans/`, 3 `docs/superpowers/`, plus `docs/PROGRESS.md`
  and `tasks/lessons.md`, add the new `.gitignore` rules, commit on master. Then use
  `metabranch.ensure_meta_branch` + `sync_meta` + `push_meta` (or do it by hand with
  the same primitives while the module is still being tested) to get that content
  onto the meta branch's first real commit. **Recount before migrating — this session
  added `maestro/metabranch.py` (tier: project, stays on master, not a process doc)
  and one new handoff file (`handoffs/2026-09-09-process-git-separation-session2.md`,
  this file — it's tier 2, add it to the migration count, making it likely 28 handoffs
  content items once you're doing the count for real, i.e. 11 files under handoffs/
  not 10.**
- [ ] Apply the same layout to duetflow (`/home/dan/projects/duetflow`) — smaller: just
  `tasks/lessons.md` to migrate (it has no `handoffs/`, `docs/plans/`, or
  `docs/PROGRESS.md` yet), plus add the missing `.orchestrator/` ignore rule its
  `.gitignore` currently lacks (verified this session — see its full contents pasted
  into the original handoff, still accurate).
- [ ] `~/.claude/skills/maestro-setup/SKILL.md` Step 1 — add the pre-flight check next
  to the existing two (read `## Step 1` there, starts around line 60, for the register
  to match: state the failure mode and its consequence, not just the remedy).
- [ ] Visual artifact (Dan's standing rule) — two views: three-tier file/branch layout,
  and the automated git sequence from task-complete through merge to both pushes.
  Deliver via `send-to-me` per CLAUDE.md's artifact-sending rule. Not started.

## Out of scope

Unchanged from the original handoff — no history rewriting, P0 baseline work is a
separate task, no `instagram-to-value` onboarding, don't touch `implementer.py`'s task
brief or move `ROADMAP.md`/`PROJECT.md`, one remote only.

## Parallel-session coordination

Unchanged from the original: this task owns `.gitignore`, `maestro/merge.py`,
`maestro/cli.py`, `maestro/orchestrator.py` (startup call site), `maestro/metabranch.py`
(new), `~/.claude/skills/maestro-setup/SKILL.md`, and the branch/worktree layout of both
repos. Leave `tests/graph_engineering/`, `scripts/graph_baseline.py`, `maestro/metrics.py`
alone. **Re-run the P0-session check above before your first `git rm --cached`** — don't
trust this handoff's "confirmed clear" past its own timestamp.

## Verification to report back

Same 10 items as the original handoff's "Verification to report back" section — none of
them have been run yet this session. Report actual numbers and exit codes, not
adjectives, exactly as that section specifies.

## Notes

- Current branch: `feat/graph-engineering-foundation`. Two new commits since the
  original handoff was written: `ae06eff` (safety checkpoint) and `3b122d6`
  (`metabranch.py`, WIP).
- This handoff and the original one are themselves process docs — they migrate to the
  meta branch along with the other handoffs when you do that step. Don't forget to
  include this filename in the migration count (see the recount note above).
- Session ended here on a context-budget signal (Sonnet 5, ~117K tokens used), not
  because of a blocker. There is no open question waiting on Dan — the only ruling
  made (sync-vs-symlink, above) is recorded for him to react to if he wants, but
  nothing blocks continuing without his input.
