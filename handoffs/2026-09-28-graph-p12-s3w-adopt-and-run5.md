# Handoff: Graph P12, session 3w. Adopt D28 (85d1cb7) into DuetFlow, then watch 06 run 5

This file is the whole prompt. Session s3v built, tested and merged the D28 fix and stopped at the 180K line before
adopting. **Your job: amend the manifest, adopt 85d1cb7 into DuetFlow, run doctor, remove HALT, and watch 06 run 5 to a
V-DAN, an accept or a park.**

## 0. Read first
- `docs/graph-engineering/STATE.yaml`: search `Pilot finding D28` (FIXED 85d1cb7). Also read D26 (the ask path) and D27.
- `handoffs/2026-09-28-graph-p12-s3v-task-notes-in-briefs.md` §3 items 4–6 and §5: the steps you now own and the traps.
- `handoffs/2026-09-26-graph-p12-s3j-run9.md`: §3 item 1 covers adopt (`selfupdate.materialize_worktree` plus
  `adopt(sha, project_repo=...)`, with `MAESTRO_REPO=/home/dan/projects/duetflow`). §3 item 3 is the archive procedure.
  §4 covers traps.
- `handoffs/2026-09-28-graph-p12-s3r-dan-spec-questions.md` §3 (integration steps 4–7) and
  `handoffs/2026-09-28-graph-p12-s3u-watch-06-run4.md` (how run 4 was watched).
- Memory to read first: `talk-in-local-time` (IDT = UTC+3), `handoffs-are-the-whole-prompt`, `gaps-are-not-decisions`,
  `llm-failure-handling-policy`, `no-idle-subagent-polling`, `agents-ask-dan-on-spec-gaps`.

## 1. State (2026-09-28, s3v end)
- maestro `feat/graph-engineering-foundation` HEAD is **85d1cb7** (the D28 fix, fast-forwarded). s3v's worktree and
  branch `s3v/task-notes` are removed. `artifacts/graph-engineering/p12-pilots/duetflow.json` is untracked; leave it.
- **Suite: 5111 passed + 1 xfailed** (baseline 5106 + 1). Run it from the main checkout with **`.venv/bin/python -m pytest`**.
  Traps:
  - System `python3` fails 2 tests: thirdparty version drift, and `No module named maestro` in the audit script.
  - A git worktree fails 5 `test_next_graph_prompt` tests, because `docs/graph-engineering/specs` is git-ignored.
  - xdist is not installed.
  - `addopts=-q` plus `-q` hides the summary line; count the progress characters instead.
- What 85d1cb7 does:
  - `ContextManifest.task_notes` holds the ROADMAP `notes:` verbatim, via the `TaskSpec.notes` property reading
    `legacy_fields`.
    - It is counted in `context_estimate_tokens` and never dropped for the budget.
    - It is bounded by `MAX_NOTES_TOKENS=4000`; a cut is named in `omitted_material` and shown in the brief.
  - `agent_brief(..., role=)` renders a "TASK NOTES (" section for every role. A reviewer also gets "Check each
    requirement in the TASK NOTES in this round, the first one included…".
  - Auto-widen:
    - `WorkflowRunner.rework_paths` collects path-like tokens from blocking findings.
    - `context._widened_for_rework` keeps the tokens that are snapshot files: an exact path, or a unique bare file name.
    - Those files are added to the entrypoints of writer and reviewer nodes.
    - `retrieval_paths` records each one as `widened_for_rework:<path>`.
    - The journal gets `scope_widened_for_rework`, and the store gets the event `ScopeWidenedForRework`.
  - Scope was confirmed not to be a write limit anywhere: only writeset (D20) and retrieval use it.
- DuetFlow is still **HALTED**, pinned to 294922d, and has nothing in flight. DuetFlow main e0778f3 has 06's
  updated notes and scope.
- The STATE D28 thread is already updated ("FIXED 85d1cb7, not yet adopted; 06 run 5 pending").

## 2. In scope, in order
1. Add a manifest `code_under_test` amendment to `artifacts/graph-engineering/p12-pilots/manifest.json` (see the 8be7937
   format):
   - `maestro_from` 294922d0131cecac7b475e9ed04faf23ae24c582, `maestro_to` the full sha of 85d1cb7, `adopted_by_duetflow`
     `/home/dan/.maestro/versions/<sha>`, and a `reason` (D28: notes reached no agent; auto-widen).
   - Add a `note`: no criterion or weight change; 06 is re-run (run 5).
   - Write it with `json.dumps(..., indent=2, ensure_ascii=False)` and check that the diff adds only your lines.
   - Commit.
2. Adopt 85d1cb7 into DuetFlow and run doctor. Expect OK plus 2 known WARNs (systemd_unit, meta_branch).
3. Remove HALT and relaunch per s3u (the watchdog relaunches; confirm `orchestrator_relaunched`).
   - Watch with a Monitor on the journal: `graph_run_*`, `dan_*`, `scope_widened_for_rework`, `watchdog_*`,
     `orchestrator_*`, and any park/halt/error/crash/fail/refus event.
   - Also poll `node_states` of the newest `task_runs` row every 20 s. Open the DB read-only (`file:...?mode=ro`).
4. Check and record in STATE D28:
   - The implementer and review-1 `context_manifest` artifacts contain `task_notes` with 06's notes
     (`playlist-modify-public`, "never repeat it automatically").
   - Whether round 1 raises the owner-scope requirement, if it is unmet.
   - Rounds, blocking findings per round and their origins, and whether any `missed_earlier` finding remains.
   - Any `scope_widened_for_rework`.
   - If D26 asks anything: whether the ask was on target, and the ADR tick hold (`dan_question_answered` minus the
     tap time).
5. On a V-DAN, tell Dan to do the owner re-consent first (V-DAN step 1). On an accept, report to Dan in IDT. On a park,
   archive as run 14 per s3j §3 item 3: export first with `--since` = run 5's open time minus 1 min, keep HALT, and
   diagnose from evidence before any fix.

## 3. Out of scope
- The Context Gate: never build it (AGENTS.md).
- A reviewer depth bound (D23). Revisit it only if run 5 escalates past round 1 on new classes of findings.
- Moving D26's ADR call off the controller tick.
- Reusing run 4's branch work (`archive/06-writer-first-light-run4`).

## 4. Coordination and traps
- No other session touches DuetFlow or maestro. DuetFlow main has uncommitted `docs/dependency_map.md`/`.png` changes that predate s3v (s3v never wrote DuetFlow); inspect them before adopting, and commit or restore them as their origin warrants.
- Do cleanup yourself. Hand Dan only what auto mode refuses outright (journal writes, token reads), as one `! <command>` line.
- Foreground `sleep` is blocked; use Monitor or background until-loops. Worktrees go on persistent storage, never /tmp.
- The Bash auto-mode classifier failed transiently for a stretch in s3v; retry, or use Read/Edit meanwhile.
- Hand off again at about 180K context.
