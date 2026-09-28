# Handoff: Graph P12, session 3v. Put a task's ROADMAP notes in every brief (D28), then rerun DuetFlow 06 (run 5)

This file is the whole prompt. Session s3u watched 06 run 4. The run parked, and s3u traced the cause to a gap:
**a task's `notes:` never reach any agent.** Dan (2026-09-28 ~03:00 IDT) said to fix maestro first and rerun 06
after. **Your job: build the D28 fix, test it, integrate it, adopt it into DuetFlow, and watch 06 run 5 to a
V-DAN, an accept or a park.**

## 0. Read first
- `docs/graph-engineering/STATE.yaml`: search `Pilot finding D28` (this finding). Also read D26 (the ask path,
  held on target in run 4) and D27 (the lost-write case, now RESOLVED).
- `handoffs/2026-09-26-graph-p12-s3j-run9.md` §3 item 3 is the archive procedure. §4 covers traps.
- `handoffs/2026-09-28-graph-p12-s3t-dan-questions-lanes.md` shows how s3t built, tested, integrated and adopted
  (worktree, lanes, full suite, manifest `code_under_test` amendment, adopt, doctor).
- Memory to read first: `talk-in-local-time` (IDT = UTC+3), `handoffs-are-the-whole-prompt`, `gaps-are-not-decisions`,
  `llm-failure-handling-policy`, `no-idle-subagent-polling`, `agents-ask-dan-on-spec-gaps`.

## 1. State (2026-09-28 ~03:05 IDT)
- maestro `feat/graph-engineering-foundation`, HEAD 5226381. It archives run 13 with the export
  `artifacts/graph-engineering/p12-pilots/duetflow-run13-archived.json`. The suite baseline is 5106 passed plus
  1 xfailed.
- DuetFlow is HALTED (HALT in place) and pinned to 294922d.
  - 06 run 4 = `run_ORxN6bPzdwJhdXDy` → repaired as `run_murTOs4eiqQ0qvQa`. It is archived in
    `.orchestrator/archive/2026-09-28-run13`, on branch `archive/06-writer-first-light-run4` at c919411, which has
    nearly complete work (972 lines).
  - DuetFlow main 62a335a is a ROADMAP edit for 06: `auth.py` and `tests/test_auth.py` join the scope. The notes
    now pin the owner-scope rule (`playlist-modify-public`) and the no-repeat create-POST rule. V-DAN's first step
    re-consents the owner. DuetFlow main e0778f3 settles the owner: Person A, role `a`, who holds the Source
    Playlist (Dan, 2026-09-28).
- Run 4's timeline: review rounds with 4, then 1, then 1, then 1 blocking findings. Rounds 2 and 3 each found an
  incomplete fix of a round-1 finding. Then came 1 diagnose (`poor_agent`, the 429 create retry) and the final run.
  Round 4 raised a `missed_earlier` finding: the owner consent lacks `playlist-modify-public`, so the real create
  gets a 403. The repair budget was exhausted, so the run parked. No D26 question was asked, and none was due.
- STATE.yaml is git-ignored and local; edit it in place.

## 2. The finding (evidence)
- `maestro/workflows/routing.py:254 build_manifest` → `maestro/repository/context.py:167 build_context_manifest`.
  The manifest keys are `acceptance_criteria` (from verifications), `mandatory_instructions`, `relevant_decisions`
  (empty) and retrieval. The task's `notes` key is not among `_CONSUMED_KEYS` (`maestro/taskgraph.py:84`), so it
  lands in `legacy_fields`, which nothing renders.
- `agent_brief` in `maestro/workflows/worker.py:213` has no notes section.
- Proof: in `.orchestrator/archive/2026-09-28-run13/artifacts`, no stored artifact contains 06's notes text
  ("retry the whole call", `playlist-modify-public`). Review 4 found the rule only by opening `docs/ROADMAP.md`
  itself.
- Retrieval (`retrieval_paths`) reads only the scope's files, so `auth.py` was never in context. Scope is also the
  D20 overlap prediction. It is not enforced on writes: `writeset.run_write_set` is the scope plus the changed files.

## 3. In scope, in order
1. **Notes in every brief.** Carry the task's `notes:` into the context manifest, as a real field (hashed, and
   counted in `context_estimate_tokens`), and render it in `agent_brief` for every role: implementer, reviewer,
   diagnoser, prep and follow-ups.
   - Tell the reviewer that each requirement in the notes is part of the contract: check each one in round 1, and
     a missing one is a blocking finding in that round.
   - Keep the notes verbatim (no summarizing: the ROADMAP line 50 rule is that paraphrase loses criteria).
   - Bound it the way the other sections are bounded. If notes are over the bound, say what was cut in
     `omitted_material`; never drop them silently.
2. **Auto-widen scope (Dan's choice, 2026-09-28).** When a blocking finding names files outside the task's
   `scope:`, the rework does not stall. The next implementer and reviewer manifests add those files to retrieval,
   and the D20 write set already tracks them.
   - Journal it (`scope_widened_for_rework` or similar).
   - Do not rewrite the ROADMAP.
   - First check that nothing else relies on scope as a write limit.
3. **Lanes** (name them as behaviours):
   - `test_every_role_brief_carries_the_task_notes_verbatim`
   - `test_a_reviewer_is_told_each_notes_requirement_is_checked_in_round_one`
   - `test_over_long_notes_are_bounded_and_reported_as_omitted`
   - `test_a_finding_on_an_out_of_scope_file_puts_it_in_the_rework_context`
   - Run the full suite; report the count against 5106 + 1 xfailed.
4. **Integrate, amend, adopt.**
   - Commit on the branch.
   - Amend the manifest `code_under_test` from 294922d to the new sha, with a `note`. Use
     `json.dumps(..., indent=2, ensure_ascii=False)`.
   - Adopt into DuetFlow and run doctor. Expect OK plus the 2 known WARNs.
5. **Run 5 of 06.**
   - Remove HALT and watch it with a Monitor on the journal plus node phases (s3u used a 20 s node_states poll on
     the newest `task_runs` row).
   - Check that the implementer and review-1 manifests contain the notes, and whether round 1 now raises the
     owner-scope requirement.
   - Record in STATE D28: rounds, findings per round, their origins, and whether any `missed_earlier` finding
     remains.
   - If D26 asks anything, record whether the ask was on target, and measure the ADR tick hold:
     `dan_question_answered` minus the tap time.
6. **On a V-DAN**, tell Dan to do the owner re-consent first (V-DAN step 1). On an accept, report to Dan. On a
   park, archive as run 14 per s3j §3 item 3 (export first with `--since` = run 5's open time minus 1 min), keep
   HALT, and diagnose from evidence before any fix.

## 4. Out of scope
- The Context Gate: not built, and never build it here (AGENTS.md).
- A reviewer depth bound (D23). Revisit it only if run 5 escalates past round 1 on new classes of findings.
- Moving D26's ADR call off the controller tick; that happens only if Dan asks.
- Reusing run 4's branch work for run 5. No seeding mechanism exists; don't invent one.

## 5. Coordination and traps
- No other session touches DuetFlow. The old `maestro-wt/dan-questions` worktree is already removed.
- Do cleanup (worktree/branch removal) yourself; never hand Dan remove commands. Hand him only what auto mode
  refuses outright (journal writes, token reads), as one `! <command>` line.
- `sqlite3.connect` on a missing path creates an empty DB, so open it read-only with `file:...?mode=ro`.
- Foreground `sleep` is blocked; use Monitor or background until-loops.
- Use a worktree on persistent storage (`/home/dan/projects/maestro-wt/...`), never /tmp, and commit early.
- Hand off again at about 180K context.
