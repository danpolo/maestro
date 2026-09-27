# Handoff: Graph P12, session 3t. D26 (agents ask Dan when the spec is silent): core is built, write the lanes, finish the Telegram surface, integrate

This file is the whole prompt. Session s3s implemented most of D26 on the branch but wrote no end-to-end lanes yet.
It stopped at the 180K handoff line. **Your job: lanes, the remaining Telegram pieces, a full suite, integration.**

## 0. Read first
- `handoffs/2026-09-28-graph-p12-s3r-dan-spec-questions.md`: the spec. §1 why, §2 scope, §3 coordination with the
  pilot session, §4 verification and report-back. All still apply.
- `handoffs/2026-09-28-graph-p12-s3s-dan-questions-build.md`: the settled design. §2.4 (Telegram), §2.6 (lanes),
  §3 (out of scope) and §4 (verification, traps) still apply. §2.1–2.3 and §2.5 are now built; see below.
- Memory: `gaps-are-not-decisions`, `llm-failure-handling-policy`, `talk-in-local-time`,
  `handoffs-are-the-whole-prompt`, `agent-scratch-not-in-tmp`.
- Don't re-read the whole diff. `git diff 92088ae..HEAD --stat`, then open only what a lane touches.

## 1. State
- Worktree `/home/dan/projects/maestro-wt/dan-questions`, branch `s3r/dan-questions`. Two commits on 92088ae:
  - `cd41369`: question schema, handler emission, decisions record, Telegram question channel;
  - `a5cef7d`: the runner asks Dan, holds the cone and polls answers into decisions; briefs carry the rule and
    DAN'S DECISIONS.
  - The tree is clean.
- Run the venv from the worktree: `/home/dan/projects/maestro/.venv/bin/python -m pytest ...`. It imports the
  worktree's `maestro`, which s3s checked.
- `docs/graph-engineering` in the worktree is a **symlink** to the main repo's gitignored directory, excluded via
  the common `.git/info/exclude`. Without it, 5 `tests/test_next_graph_prompt.py` tests fail for environmental
  reasons. **Don't commit it.**
- **Baseline at 92088ae:** 5068 passed, 1 skipped, 1 xfailed. That is 5063 plus the 5 that need the symlink,
  i.e. the 5069 total the handoff quoted.
- **At a5cef7d:** `tests/workflows tests/graph_engineering tests/test_dan_decisions.py` gives 686 passed. The full
  suite has not been run yet.
- New unit tests, 27 in all:
  - `tests/test_dan_decisions.py`: parse, render_question, outcome_line, add_decision (no key, existing key,
    column-0 list, comments kept), record (append ADR, new ADR, path confinement, no `docs/adr/`, CAS not
    laundered, dirty ROADMAP left uncommitted);
  - `tests/test_taskgraph.py::test_dan_decisions_round_trip_through_the_document_vocabulary`;
  - `test_failures` updated to 14 kinds.

## 2. What is built (where to look)
- **`maestro/workflows/dan_questions.py`:**
  - `QUESTION_FILE`, `parse` (caps a round at 3; the round is all or nothing), `BRIEF_RULE`, `REVIEWER_RULE`,
    `implementer_instruction()`, `decision_answer(spec, raw)` (a tap gives "label (consequence)", Other gives the
    text), `normalise`, `render_decisions`;
  - `MAX_ROUNDS_PER_WORKFLOW = 4`.
- **`handlers.py`:**
  - `_adopt_question` runs before `result_from_workspace` for every agent role. Valid: `failed` plus
    `outputs.dan_questions`. Malformed: `garbage_output`.
  - `_adopt_review`: `document["questions"]` gives `status=failed` plus `dan_questions`, with `review` kept.
  - `_adopt_plan_repair`: `verdict: spec_gap` gives `dan_questions`. Unusable questions give
    `plan_repair.questions_error`, then `_verdict_refusal` returns "spec_gap verdict carries no usable questions".
- `compiler.VERDICTS` gains `spec_gap`. `failures.FAILURE_KINDS` gains `dan_question: BLOCKED`, and `pause_for`
  returns None for it.
- **`lifecycle.requeue_for_question(attempt, run_id, node_id, cone, record, outcome_ref, skipped)`:** one
  transaction; no budget counters move.
- **`runner.py`:**
  - `_commit_result` calls `_screen_questions` before the outcome artifact:
    - drops re-asks (normalised against `dan_decisions()`);
    - refuses a round of only re-asks, a round past the cap, a run with no channel, or a diagnoser with no failed
      node, as `garbage_output` with `outputs.dan_questions_refused`;
    - otherwise calls `_ask_dan`.
  - `_ask_dan` calls `requeue_for_question`, then:
    - `channel.poll_question(spec)` per question; the spec carries `id, task_id, run_id, workflow_id, attempt_id,
      node_id, hold=cone[0], asked_by=<role>/<node>#<attempt_index>, round=attempt_id, n, title, spec`;
    - journal `dan_question_asked`, store event `DanQuestionAsked`;
    - settle telemetry, `routing.on_attempt_finished`, lease release.
  - `_question_cone`: implementer `(node,)`; reviewer `retry_cone(kind=verification_failed)`; diagnoser: the
    failing node's cone, with the diagnoser itself added by the lifecycle.
  - `advance` checks `_awaiting_dan(node_id)` first and waits with `"dan_question: <ids>"`.
  - `tick` calls `_poll_dan_questions` after `_repoll_operator_nodes`. An answer goes through `_record_dan_answer`:
    - `decisions.record(roadmap.REPO, task, item, roadmap=roadmap.ROADMAP_FILE, journal=channel.journal)`;
    - `channel.mark_question_recorded(id, decision=item, decision_commit=sha)`;
    - journal `dan_question_answered`, event `DanQuestionAnswered`.
  - `dan_decisions()` returns the ROADMAP `decisions:` plus recorded decisions from question records that the
    ROADMAP lacks. `_advance_agent` sets `manifest_doc["dan_decisions"]` for every role.
  - `rework_note` gets `_question_note` for any node whose committed outputs carry `dan_questions`: the question,
    Dan's decision and "Make the code and tests follow this decision", plus that review's findings. Such nodes
    `continue`, which skips the reviewer, gate and dan branches.
  - `_restore_start_tree` does not restore after a `dan_question` attempt.
- **`worker.py`:**
  - the reviewer header gains `BRIEF_RULE` and `REVIEWER_RULE`;
  - the diagnoser header gains the `spec_gap` verdict and `BRIEF_RULE`;
  - every other sentinel role gets `implementer_instruction()`;
  - `agent_brief` renders DAN'S DECISIONS after the acceptance criteria.
- **`maestro/docs/decisions.py`:** `entry`, `add_decision` (textual, re-parse verified), `adr_change` (the
  `_adr_agent(prompt, cwd)` seam, one retry, confined to `docs/adr/*.md`, the next NNNN), and `record` (CAS
  without laundering, a dirty ROADMAP written but not committed, a commit touching only those paths).
- **`maestro/hitl/verify.py`:**
  - `QUESTION_TYPE="spec-question"`, `QUESTIONS_STATE_KEY="awaiting_dan_questions"`, `question_request_id`,
    `render_question` (buttons `1 ⭐`/`2`…, `danreq:<id>:<i>`, the Other line), `question_records(run_id=,
    task_id=)`;
  - `DanVerificationChannel.poll_question(spec)`, `mark_question_recorded`, `_mark_question`;
  - `outcome_line` gives "✅ Chosen: …";
  - `question_waiting_lines`, prepended to `waiting_lines`; `prune_awaiting` also prunes the questions key.
  - Record `options` are the option labels, so a tap records the label.

## 3. Still to do
1. **Lanes** (s3s §2.6, all 8, in `tests/graph_engineering/test_end_to_end.py`, in the style of `_ReviewingAgent` /
   the D24 lane at ~L700–780):
   - first grep `dan_confirm` / `DanVerificationChannel` in `tests/graph_engineering/` for the existing harness;
   - swap the channel through `orchestrator._graph_dan_channel`;
   - tap via `telegram.ack_danreq_callback` / `_handle_danreq_callback`; reply via
     `telegram._route_freetext_answer(text, {"message_id": mid})`;
   - patch `decisions._adr_agent`.
   - Points to prove in the lanes:
     - diagnoser: the failing node's rerun gets exactly one attempt (`quality_failures` stays 3), and a fail
       re-runs the diagnoser;
     - restart: `_awaiting_dan` reads disk records only.
2. **Telegram leftovers** (s3s §2.4):
   - reminders for `awaiting_dan_questions` (extend `telegram._remind_dan_verifications`, ~L584, or add a
     sibling; same 24 h throttle);
   - `maestro/watchdog.py:~488`: treat `awaiting_dan_questions` as waiting;
   - `/status`: `commands.py ~L1325` uses `verify.waiting_lines`, which now includes questions. Confirm it, and
     add a unit test.
3. **Known limit, not a blocker:** `_record_dan_answer` runs the ADR agent call synchronously in the controller
   tick (≤2×300 s). Record it in the STATE thread. Moving it off-thread is a follow-up only if Dan wants it.
4. **Full suite:**
   - `-n auto --junitxml=<file in worktree root>`, no `-q`; delete stray `.junit-*.xml`;
   - expect 5068 plus the new tests passed, 1 skipped, 1 xfailed.
   - Then squash or reword the wip commits into one `feat(graph): …` commit (P12 pilot D26), or keep them with
     proper messages.
5. **Integration, live check, STATE, report to Dan:** s3r §3–§4 exactly. The live Telegram check needs Dan's OK
   in chat first. Report times in IDT.

## 4. Coordination and traps (unchanged)
- The pilot session owns DuetFlow. Integrate only when no graph run is in flight: the last `graph_run_settled` is
  after the last `graph_run_opened`, and no attempt is `running`.
- Auto mode refuses journal writes, token reads and `git worktree remove --force`. Hand those to Dan as one
  `! <command>` line. Foreground `sleep` is blocked.
- Hand off again at ~180K context (Opus 5.5). Commit early.
