# Separate process docs from project files, automatically, across maestro and duetflow

## Goal

Dan's rule: **only actual project files belong in a project's committed history.** Handoffs,
plans, research packets, progress records and lessons are *process* artifacts and must live
somewhere else — versioned, but off the project branch.

The agreed shape is an **orphan `meta` branch in the same repo, checked out as a sibling
worktree**. One clone, one remote, two branches; `master` history stays clean for the portfolio.

**Hard requirement from Dan (2026-09-09):** *"I want everything handled automatically. If needed
change a bit how maestro works with git to fit this. I don't want to manage anything manually
related to git."* This is not a docs-only change — maestro's git layer has to create, attach,
commit and push the meta branch on its own. A solution that ends with "then run `git worktree add`"
does not meet the requirement.

## Read first

1. `docs/DESIGN.md` §5 (`project.yaml` shape) and §10 (setup/doctor) — the contract `maestro init`
   and `doctor` implement.
2. `maestro/merge.py` around lines **558** and **640** — two `git push origin main` calls whose
   return codes are unchecked. Both must be fixed as part of "no manual git", and both must learn
   to push the meta branch too.
3. `maestro/implementer.py` — the task-brief builder. It tells every agent to read
   `docs/ROADMAP.md` and `docs/PROJECT.md`. This is the source of the carve-out below.
4. `~/.claude/skills/maestro-setup/SKILL.md` — Step 1's pre-flight checks and Step 2b. The new
   `.gitignore` / meta-branch step belongs in Step 1, next to the existing manifest and git-remote
   checks.
5. `docs/PROGRESS.md`, most recent entries, for readiness caveats.

## The carve-out — do not get this wrong

Three tiers, not two. The middle tier is what makes this non-obvious:

| Tier | Where it lives | Contents |
|---|---|---|
| **Project** | committed to `master` | source, tests, `README.md`, `CONTEXT.md`, `docs/adr/`, **and** `project.yaml` + `docs/ROADMAP.md` + `docs/PROJECT.md` |
| **Process** | orphan `meta` branch, sibling worktree | `handoffs/`, `docs/plans/`, `docs/superpowers/`, `docs/graph-engineering/`, `docs/PROGRESS.md`, `tasks/lessons.md` |
| **Neither** | `.gitignore` only | `.orchestrator/`, `.venv/`, `.run/`, caches, `.env`, `*.egg-info/` |

`project.yaml`, `docs/ROADMAP.md` and `docs/PROJECT.md` **stay on `master`** even though maestro
generated them. They are runtime state read by relative path from the project root:
`implementer.py` names the latter two in every task brief, and `maestro/docs/roadmap.py` parses the
roadmap in place. Move them to a sibling worktree and the running orchestrator cannot find its own
state — every task brief then points at a file that is not there.

## Current state (verified 2026-09-09)

**maestro** — branch `feat/graph-engineering-foundation`, HEAD `5ac9bcac1d58b5e58238b02ed148ebcdba08323b`,
main branch `master`.

Tracked files that belong on the meta branch: **10** under `handoffs/`, **12** under `docs/plans/`,
**3** under `docs/superpowers/`, plus `docs/PROGRESS.md` and `tasks/lessons.md` — 27 files.

`.gitignore:13` already ignores `docs/graph-engineering/` entirely. That directory holds the P00–P12
phase specs, `STATE.yaml` and the architecture HTML — i.e. the files
`scripts/next_graph_prompt.py` reads to generate every phase prompt. **They are currently versioned
nowhere.** The meta branch fixes this; treat it as a win to bank, not a detail.

`git worktree list` already shows 31 entries (30 detached under `~/.maestro/versions/`). One more
row is acceptable; do not let the meta worktree land inside the repo tree.

**duetflow** (`/home/dan/projects/duetflow`) — 3 commits on `master`, tracked: `.gitignore`,
`CONTEXT.md`, `README.md`, `docs/PLAN.md`, 5 files under `docs/adr/`, `tasks/lessons.md`,
`tests/test_smoke.py`. Its `.gitignore` has **no `.orchestrator/` rule** and there is no root
manifest (`pyproject.toml`/`requirements.txt`) — see the P00 spec for why that second one matters.

**Uncommitted in this working tree — do not discard:**

- `.claude/settings.json` — added `permissions.additionalDirectories` and Read/Write/Edit allow
  rules for `/home/dan/projects/duetflow`.
- `scripts/next_graph_prompt.py` — new `EXTERNAL_WORKSPACES` map and the "Authorized External
  Workspaces" prompt section.
- `tasks/lessons.md` — 2026-09-09 entry on skill-owned prerequisites.
- `GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md`, `handoffs/2026-09-07-graph-engineering-p0.md` —
  pre-existing, untouched by that session.
- `docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md` — rewritten "Scratch Project
  Pilot" section. **Gitignored, so it will not appear in `git status`.** Verify it survives before
  and after any git surgery.

## Parallel-session coordination

No other session is known to be running. If one is, it will most likely be the **P0 implementation
session** working in `tests/graph_engineering/`, `maestro/metrics.py` and `/home/dan/projects/duetflow`.

- This task owns: `.gitignore`, `maestro/merge.py`, `maestro/cli.py`, the git-layer modules,
  `~/.claude/skills/maestro-setup/SKILL.md`, and the branch/worktree layout of both repos.
- Leave alone: `tests/graph_engineering/`, `scripts/graph_baseline.py`, `maestro/metrics.py`.
- **Do not run git surgery while a P0 session is mid-task in either repo** — `git rm --cached`
  across 27 files plus a branch creation will collide with an in-flight merge gate. Confirm with
  Dan that no P0 session is active before the first `git rm`.

## In scope

- A **three-tier `.gitignore`** for maestro and for duetflow, matching the table above.
- **Automated meta-branch lifecycle in maestro's git layer.** At minimum: create the orphan branch
  if absent, attach the sibling worktree if absent, commit process-doc changes on their own, and
  push both branches. Reconcile after a clone where the worktree does not yet exist. Decide and
  record where the attach point belongs — `init`, `doctor`, or the daemon's startup reconcile —
  and say why.
- **Fix `merge.py`'s two unchecked `git push origin main` calls** (lines 558, 640). Today a failed
  push is silent, so merged work can sit local forever while the operator believes it shipped.
  Check the return code, surface the failure, and extend both sites to push the meta branch.
- **Migrate maestro's 27 tracked process files** to the meta branch: `git rm --cached`, add the
  ignore rules, commit them onto `meta`. Files stay on disk. **History is not rewritten** — Dan
  ruled out the scrub; the old commits still contain them.
- **Bring `docs/graph-engineering/` onto the meta branch**, replacing the blanket `.gitignore:13`
  rule. The phase specs and `STATE.yaml` get versioned for the first time.
- **Apply the same layout to duetflow**, including the missing `.orchestrator/` ignore rule.
- **Write the step into `maestro-setup` Step 1**, alongside the existing manifest and git-remote
  pre-flight checks, so every future onboarded project gets this without being asked.
- A **visual artifact** for the design (Dan's standing rule for multi-stage change proposals):
  diagram-led, two complementary views — the three-tier file/branch layout, and the automated
  git sequence from task-complete through merge to both pushes. Deliver it to Dan's inbox with
  `send-to-me`.

## Out of scope

- Rewriting published history. `git filter-repo`/BFG is explicitly not authorized.
- The P0 baseline work itself (fixtures, metrics, the DuetFlow setup rehearsal). Separate task,
  separate session — see `handoffs/2026-09-07-graph-engineering-p0.md` and the P00 spec.
- Onboarding `instagram-to-value`. That is P12.
- Changing what `implementer.py` puts in a task brief, or moving `ROADMAP.md`/`PROJECT.md`. See
  the carve-out.
- A second git remote, or a separate process repo. Decided against — one remote, two branches.

## Verification to report back

Report actual numbers and exit codes, not adjectives:

1. `git ls-files | grep -cE '^(handoffs|docs/plans|docs/superpowers)/'` on `master` → expect **0**
   (was 25). Plus `git ls-files docs/PROGRESS.md tasks/lessons.md` → empty (was 2 files).
2. `git ls-files` on the `meta` branch → expect **≥ 27**, plus whatever `docs/graph-engineering/`
   contributes. State the real total.
3. All 27 files still present on disk in the main worktree: `ls handoffs/ | wc -l` → **11**
   (10 migrated + this handoff).
4. `git status --porcelain` on `master` clean of process docs — no stray untracked handoff.
5. `.venv/bin/python -m pytest -q` — full suite. Report pass/fail/skip counts and exit code.
   `maestro/merge.py` is on the critical path; a green focused run is not sufficient here.
6. Prove the automation, do not assert it: from a **fresh clone into a scratch directory**, show
   that the meta worktree gets created and attached with no human git command. Paste the commands
   and their output.
7. Prove the push fix: force a push failure (e.g. a bogus remote) and show `merge.py` now reports
   it instead of returning success.
8. `.venv/bin/python scripts/next_graph_prompt.py --phase P00` still renders, including the
   "Authorized External Workspaces" section, after `docs/graph-engineering/` changes branches.
9. duetflow: `git -C /home/dan/projects/duetflow ls-files` before/after, and confirm
   `tasks/lessons.md` moved while `CONTEXT.md`, `docs/PLAN.md` and `docs/adr/` did not.
10. Confirm the gitignored `P00_MIGRATION_BASELINE.md` edits survived — grep it for
    `maestro-setup skill` and for `_derive_facts`.

## Notes

- Commit the five uncommitted files listed above **before** starting git surgery, so a bad
  `git rm --cached` is recoverable.
- This handoff is itself a process doc. It goes to `handoffs/` now because the meta branch does
  not exist yet; migrate it with the other 10.
- `maestro-setup`'s Step 1 already documents two quiet-failure pre-flight checks. Match that
  register — state the failure mode and its consequence, not just the remedy.
