# Handoff: Graph P12, session 3r. Build D26: agents ask Dan on Telegram when the spec is silent

Build a Dan-requested feature on `feat/graph-engineering-foundation` (maestro), in a **worktree**. This file is the
whole prompt. A separate pilot session (s3q, then its successor) keeps running the DuetFlow pilot at the same time;
§3 says how you two stay out of each other's way.

Before you start, read:
- `docs/graph-engineering/STATE.yaml`, the D26 thread (search `Pilot finding D26`). It is the problem statement.
- The D17/D24/D25 threads, for how the runner re-briefs nodes.
- `handoffs/2026-09-26-graph-p12-s3j-run9.md` §4 (traps: auto-mode refusals, pytest invocation, STATE.yaml quoting).
- Memory: `gaps-are-not-decisions`, `llm-failure-handling-policy`, `talk-in-local-time`, `handoffs-are-the-whole-prompt`.

`docs/graph-engineering/` and `handoffs/` are gitignored. **Talk to Dan in local time (IDT = UTC+3); journals and the
DB are UTC.**

## 1. Why (D26, 2026-09-27/28)
- DuetFlow `06-writer-first-light` run 2 (`run_mXPoi2Ts62MEghLB` → `run_MtSt1zmOdgA0N7xZ`, archived as run 11 in
  `.orchestrator/archive/2026-09-28-run11`) parked after 4 proof-review rounds, a diagnose and a final run.
- Round 1's 8 blocking findings were real and got fixed. Rounds 2–4 circled a case the ROADMAP and ADRs never define:
  at cold start, a track that is both in A's Source Playlist and among B's candidates.
- The reviewer, then the diagnoser (`poor_agent`, "maximal disjoint split"), invented a rule. The rework then broke
  the selector: 2 findings with origin `introduced_by_rework`. The final run failed and the task parked.
- The session then asked Dan by hand. He answered "shared counts for B". It went into DuetFlow aa6b439 (new check V5
  `shared_track`, a paragraph in ADR-0003) and maestro 92088ae (manifest amendment). 06 run 3 (`run_H6uFAoLksntRppN4`)
  is running on it.
- **Dan (2026-09-28):** fix the root cause. The role that hits such a gap must detect it and ask him on Telegram
  **immediately**, instead of spending quota spiralling.
  - The format is close to Claude Code's AskUserQuestion: 2–4 options, a recommended one, and free text, with
    buttons.
- Dan's decisions:
  1. **Who asks:** implementer, reviewer and diagnoser.
  2. **Where the answer goes:**
     - the task's ROADMAP block, as a `decisions:` entry;
     - every later brief for that task;
     - an ADR: append a dated paragraph to the most relevant one, or **create a new ADR if none fits**.
  3. **If Dan does not answer:** only that task waits, with the usual HITL reminders. Other ready tasks keep running,
     and it spends no quota while waiting.

## 2. In scope (design; fill the gaps yourself, per `gaps-are-not-decisions`)
1. **One question schema** shared by all three roles:
   - Fields: `question`; `why_undefined` (which criteria, notes or ADRs are silent, and why the choice matters);
     2–4 `options`, each `{label, consequence}`; `recommended` (index).
   - Ask only when both hold:
     - the correct behaviour is not determined by the task's criteria, notes, `decisions:`, ADRs or CONTEXT.md;
     - reasonable choices would change the code or tests.
   - Never ask for things the agent can look up or settle by engineering convention. State this in the brief,
     because Dan's attention is the cost.
   - Never re-ask something already answered in `decisions:`.
2. **Emitting a question:**
   - **Implementer:** writes `QUESTION` (JSON) into the WORKSPACE instead of guessing or writing FAILED.
     - Sentinel handling: `maestro/workflows/worker.py`, around line 195.
     - This also covers the D25 shape.
   - **Reviewer:** a `questions: [...]` list in the review JSON (brief schema near `worker.py:133`; parsed in
     `maestro/workflows/handlers.py` around 343–365).
     - A review can raise questions alongside findings.
     - It must not raise a blocking finding that depends on an unanswered question's answer.
   - **Diagnoser:** a third verdict, `spec_gap`, carrying the question(s).
     - `compiler.VERDICTS` (`compiler.py:1181`); runner checks around `runner.py:1600–1630`; brief text around
       `worker.py:178–190`.
3. **Runner:**
   - A question pauses the run in a waiting state, reusing the repolled-wait machinery: `REPOLLED_HANDLERS`,
     `dan_confirm` (`runner.py:118–125, 2080, 2205`), `handlers.py:1276`, and `hitl/verify.DanVerificationChannel`.
   - While waiting: no attempt runs, and the question round **does not consume** the review-round, rework or repair
     budget.
   - On the answer, resume where it makes sense:
     - an implementer question → the implementer re-runs with the decision;
     - a reviewer question → rework with the decision plus that review's other findings;
     - a diagnoser `spec_gap` → the failed node's rerun with the decision, and the repair budget untouched.
   - The wait must survive an orchestrator restart (control DB plus `.orchestrator/questions`).
   - Journal events: `dan_question_asked` and `dan_question_answered`.
4. **Telegram:**
   - Reuse `maestro/hitl/dan_request.build_message_and_markup` and the tap/free-text routing in
     `maestro/hitl/telegram.py` (`_danreq_tap`, `_handle_danreq_callback`, `_route_freetext_answer` ~493, the
     instant-tap listener, `_send_hitl_reminders`).
   - The message:
     - header "❓ Decision needed — <task title>";
     - the question and why it is undefined;
     - the options numbered with their consequences, the recommended one marked ⭐ (e.g. "1 ⭐");
     - one button per option;
     - "✍️ Other — reply to this message" (a free-text reply threads by message_id).
   - After the answer, edit the message to end with "✅ Chosen: …" (as V-DAN does now).
   - `/status` lists waiting questions.
5. **Recording the answer (one maestro commit on the project's main):**
   - (a) A `decisions:` list in the task's ROADMAP block: `{at, asked_by: <role>/<attempt>, question, answer}`.
     - Write it the way `maestro/docs/followups.queue_task` edits ROADMAP blocks.
     - Taskgraph: keep `decisions` in `legacy_fields`, or promote it, but it must round-trip
       (`tests/test_taskgraph.py`).
     - Include it in the context manifest and the brief as a binding "DAN'S DECISIONS" section, for every role.
   - (b) An ADR:
     - A small agent call (the diagnoser's role and model is fine) picks the most relevant `docs/adr/*.md` and appends
       a dated paragraph in the ADR's own voice, or writes a new `docs/adr/NNNN-<slug>.md` in the house format when
       none fits.
     - If the project has no `docs/adr/`, skip the ADR and journal why.
   - The commit must be merge-safe:
     - it touches only ROADMAP plus ADR paths (graduation-style);
     - it follows D18 (clean checkout);
     - it follows D20 (merges with parallel tasks trivially).
   - The manifest amendment for the pilot stays manual, done by the pilot session.
6. **Tests (lanes in the style of `test_a_gate_failure_is_reworked_from_the_failed_checks_output`):**
   - a reviewer question → a Telegram message with buttons (fake send), a tap, the ROADMAP decisions and ADR commit,
     then a rework brief carrying the decision, with the budget unchanged;
   - an implementer `QUESTION`;
   - a diagnoser `spec_gap`;
   - a free-text "Other" answer;
   - an orchestrator restart while waiting;
   - an already-answered question never re-asked (the decision is in the brief);
   - a new-ADR path and an append-ADR path;
   - a project without `docs/adr/`.

**Out of scope:**
- The Context Gate (AGENTS.md: not built, do not build).
- The failure-handling phase's stronger-model escalation.
- Changing D23 (follow-up reviewer scope).
- Any DuetFlow ROADMAP edit.
- Auto-answering after a timeout (Dan said wait).

## 3. Coordination with the pilot session
- Work in `/home/dan/projects/maestro-wt/dan-questions`, on branch `s3r/dan-questions` off
  `feat/graph-engineering-foundation`. Worktrees go on persistent storage, not /tmp (memory
  `agent-scratch-not-in-tmp`). Commit early.
- **Do not touch DuetFlow** (`/home/dan/projects/duetflow`) until integration. The pilot session owns it: its HALT,
  its archive, its ROADMAP.
- Integrate only when both hold:
  - the branch suite is green;
  - DuetFlow has no graph run in flight: the last `graph_run_settled` in `.orchestrator/journal.ndjson` is after the
    last `graph_run_opened`, and no attempt is `running` in `control.sqlite3`.
- Integration steps:
  1. Touch `.orchestrator/HALT` yourself.
  2. `git pull`/rebase onto the feat branch (the pilot session may have committed manifest amendments or exports),
     then merge.
  3. Run the full suite.
  4. Adopt per s3j §3 item 1 (`selfupdate.materialize_worktree` plus `adopt(sha, project_repo=...)`). Run doctor:
     expect OK plus 2 known WARNs, systemd_unit and meta_branch.
  5. Add a manifest `code_under_test` amendment with a `note`. Use `json.dumps(..., indent=2, ensure_ascii=False)`
     and check that the diff adds only your lines.
  6. Commit, then remove HALT.
  7. Journal-watch until `orchestrator_relaunched`.
- If a DuetFlow task is mid-run when you are ready, wait for it to settle. Use a background until-loop on the
  journal; foreground `sleep` is blocked. Never kill it.
- Auto mode refuses writing to the journal, reading bot token values, and `git worktree remove --force`. Hand such
  steps to Dan as one `! <command>` line.
- pytest: `.venv/bin/python -m pytest --junitxml=<file in repo root> -n auto` (no -q). Delete stray `.junit-*.xml`.
  The suite takes ~4 min on a quiet host; the baseline is 5069 passed plus 1 xfailed at a4ea0a9.
- Hand off again at ~180K context (Opus 5.5).

## 4. Verification and report back (numbers)
- Suite count before and after, and the new lanes by name.
- **One live Telegram check.** With Dan's OK in chat first (it sends him a real message):
  - send one sample decision question to DuetFlow's chat through the real bot;
  - Dan taps the recommended option once, and replies with free text on a second message;
  - report the message ids, that the listener recorded each answer within seconds, and that the ✅ edit appeared.
- The integration commit, the adopted sha, doctor output (OK/WARN count) and the manifest amendment commit.
- Update the STATE.yaml D26 thread: FIXED <sha>, the lane names, live pending. Then tell Dan, in local time.
- A live pilot confirmation happens when a real run first asks. The pilot session reports that.

## 5. Update from the pilot session (2026-09-28 01:55 local)
- 06 run 3 parked on a second undefined case (D27 in STATE.yaml): a Spotify write whose response is lost. It is
  archived as run 12 (maestro 396329f). **DuetFlow is HALTED with nothing in flight and nothing to rerun until D26
  lands**, so you may integrate as soon as your suite is green.
- After adopting, removing HALT reruns 06 (run 4) on your code. That is the first live test of D26: the reviewer
  should ask Dan about lost-write recovery instead of spiralling. Watch the run to a V-DAN or a settle, or leave
  HALT in place and say so if you stop first.
