# Handoff: Graph P12, session 3s. Build D26 (agents ask Dan on Telegram when the spec is silent): design settled, start coding

This file is the whole prompt. Session s3r read the code and settled the design. It wrote no code: it reached the
180K handoff line while mapping. **Your job is to implement, test and integrate D26.**

## 0. Read first
- `handoffs/2026-09-28-graph-p12-s3r-dan-spec-questions.md`, **all of it**. It is still the spec: why (§1), scope
  (§2), coordination with the pilot session (§3), and verification and report-back (§4). Everything below adds to
  it and does not replace it.
- Memory: `gaps-are-not-decisions`, `llm-failure-handling-policy`, `talk-in-local-time`,
  `handoffs-are-the-whole-prompt`, `agent-scratch-not-in-tmp`.
- `docs/graph-engineering/STATE.yaml`, the D26 thread only (search `Pilot finding D26`).
- **Don't** re-read the code map below end to end. Open only the ranges you are editing.

## 1. State
- Worktree: `/home/dan/projects/maestro-wt/dan-questions`, branch `s3r/dan-questions`, created off
  `feat/graph-engineering-foundation` at 92088ae.
  - No commits yet; the tree is clean.
  - Use the main repo's venv: `/home/dan/projects/maestro/.venv/bin/python -m pytest ...` from the worktree.
    First check that it imports the worktree's `maestro` (`python -c 'import maestro; print(maestro.__file__)'`
    run from the worktree). If it resolves to the main repo, run with `PYTHONPATH=.`.
- Baseline: 5069 passed and 1 xfailed at a4ea0a9. Re-measure on the branch before changing anything.
- Coordination rules (s3r §3) are unchanged:
  - The pilot session owns DuetFlow. Don't touch it until integration.
  - Integrate only when DuetFlow has no graph run in flight.

## 2. Settled design (implement this; small gaps are yours per `gaps-are-not-decisions`)

### 2.1 New module `maestro/workflows/dan_questions.py` (schema, parsing, brief text)
- `QUESTION_FILE = "QUESTION"` is the implementer's file in the WORKSPACE (JSON).
  - The shape is `{"questions": [...]}`, or one bare question object.
- Question shape: `{question, why_undefined, options: [{label, consequence}] (2–4), recommended: <0-based index>}`.
  - `parse(raw) -> (questions, error)` validates the shape. It caps a round at **3 questions**.
- `BRIEF_RULE` is the shared text for all three roles. It says when to ask:
  - both conditions in s3r §2.1 must hold;
  - never for what can be looked up or settled by convention ("Dan's attention is the cost");
  - never re-ask anything under DAN'S DECISIONS.
- Reviewer-only addition: never raise a blocking finding that depends on an unanswered question.
- `render_decisions(decisions) -> str` produces the binding "DAN'S DECISIONS" section.

### 2.2 Emission (handlers)
All three roles report the same outputs key, `outputs["dan_questions"] = [...]`.

- **Implementer:** in `agent_handler` (`maestro/workflows/handlers.py:680`), check for `QUESTION` **before**
  `result_from_workspace`. The agent writes QUESTION instead of DONE, so there may be no sentinel.
  - A valid file gives `status="failed"`, `detail="asked Dan: …"` and `outputs.dan_questions`.
  - A malformed file is an ordinary `garbage_output` failure, with evidence that names the parse error. It spends
    budget.
- **Reviewer:** in `_adopt_review` (`handlers.py:312`), read `document["questions"]` and put the valid ones in the
  outputs.
  - The review's disposition and findings are still recorded as usual, so the round shows in `review_rounds` and
    `rework_note`.
- **Diagnoser:** in `_adopt_plan_repair` (`handlers.py:382`), `{"verdict": "spec_gap", "questions": [...]}` gives
  `outputs.dan_questions`.
  - Also add `"spec_gap"` to `compiler.VERDICTS` (`compiler.py:~1181`).
- **Briefs** (`maestro/workflows/worker.py`):
  - `task_header` (L95–194): the reviewer JSON schema (L133) gains `"questions": [...]`. The diagnoser text
    (L178–190) gains the `spec_gap` verdict. Every sentinel-reporting role gets the QUESTION instruction and
    `BRIEF_RULE`.
  - `agent_brief` (L197–289): render `manifest["dan_decisions"]` as "DAN'S DECISIONS (binding)", placed near the
    criteria.

### 2.3 Runner (`maestro/workflows/runner.py`)
- **Intercept in `_commit_result` (L1351).** When `outputs.dan_questions` is non-empty, take a new path before
  classification: `self._ask_dan(row, node, result, outcome_ref, at)`.
- **Which nodes go back to pending:**
  - implementer: `wf_failures.retry_cone(rev, node_id)`, which gives `(node,)`;
  - reviewer: `retry_cone(rev, node_id, kind="verification_failed")`, i.e. writer → … → review;
  - diagnoser: `retry_cone(rev, failing, kind="verification_failed")`, with `failing = self._failing_node(node)`
    (L894), **plus the diagnoser node itself**, plus `self._skipped_below(cone)` (L1469).
- **New `lifecycle.requeue_for_question(...)`** in `maestro/control/lifecycle.py`. Model it on `requeue_cone`
  (L363), in one transaction, with these differences:
  - `finish_attempt(..., "failed")`, then `record_failure` with the new kind `dan_question`;
  - **no** `quality_failures` increment and **no** `retry_resets` increment;
  - `override_phase(..., "pending")` for the cone, the extras and the skipped nodes.
- **`FAILURE_KINDS`** (`maestro/workflows/failures.py:~45`): add `"dan_question": BLOCKED` with a comment (blocked on
  Dan, spends no retry). Check any test that enumerates the kinds.
- **`_restore_start_tree` (L1178)** must not restore when the previous attempt's `failure_kind == "dan_question"`.
  Select `failure_kind` in its query. That keeps the implementer's pre-question commits (the D14 lesson).
- **Repair budget:** a `spec_gap` applies no repair, so `compiler.repair_counters` is untouched automatically.
  - The failing node's `quality_failures` is still spent (3). Its rerun therefore gets exactly one attempt; if that
    fails, the diagnoser runs again.
- **The wait:** in `advance` (L618), at the top, if `self._awaiting_dan(node)` is true, set
  `report.waiting[node_id] = "dan_question: <request ids>"` and return.
  - That value already flows into `_record_admission_waits` and the state.
  - A waiting run stays in `runners`, so other tasks keep running: `orchestrator.py:~2740` ticks every run
    independently.
- **Polling answers:** at the start of `tick` (L451), next to `_repoll_operator_nodes`, call
  `_poll_dan_questions(at)`:
  - for each pending question record of this run, call `self.dan_channel.poll_question(record)`;
  - on an answer: `decisions.record(...)` (§2.5), then journal `dan_question_answered`, append the store event
    `DanQuestionAnswered`, and mark the record `recorded`;
  - the node is released on the next advance, once **all** of that round's questions are recorded.
- **On ask:** journal `dan_question_asked` and append the store event `DanQuestionAsked`. The payload carries the
  node, the attempt, `asked_by` as `<role>/<node>#<attempt_index>`, and the request ids.
- **Restart safety:** after a restart the node is `pending`/`ready` in the DB and the question records are on disk
  (`.orchestrator/questions/dq-<task>-<sha8 of run+attempt+n>.json`). `_awaiting_dan` reads the records, so the
  wait survives.
- **Re-ask guard:**
  - A question whose normalised text equals one already under `decisions:` is not sent. If every question in the
    round is a re-ask, the attempt is an ordinary `garbage_output` ("re-asked an answered question").
  - Cap: **4 question rounds per workflow**. From the 5th, questions are treated the same way. (This is a gap filled
    by s3r.)
- **`rework_note` (L2222):** for any node whose latest committed outputs carry `dan_questions`, add:
  "<role> (<node>) asked Dan: <q>. Dan decided: <answer>. Make the code and tests follow this decision."
  - Answers come from the question records or the ROADMAP decisions.
- **`_advance_agent` (L705), every role:** set `manifest_doc["dan_decisions"]` from
  `_roadmap_task(task_id).get("decisions")`, next to the existing `dan_answers` (B12, L845).

### 2.4 Telegram (`maestro/hitl/verify.py` plus `telegram.py`)
- Add `DanVerificationChannel.poll_question(record-spec)`, mirroring `poll` / `poll_action` (verify.py L385–520):
  - new `REQUEST_TYPE_QUESTION = "spec-question"`; save the record, `_deliver`, then read `_answer`;
  - on an answer, return it and clear the awaiting entry;
  - wait list: the new state key `awaiting_dan_questions` (`{task_id: {run_id, request_ids, title, requested_at,
    sent}}`), pruned with `prune_awaiting`, which is called at `orchestrator.py:2836`. Extend it, or add
    `prune_questions`.
- **Message** (`render_question`):
  - header `❓ <b>Decision needed — <task title></b>`, then the question and the reason it is undefined;
  - numbered options with their consequences, the recommended one marked `1 ⭐`;
  - one button per option (`danreq:<id>:<i>`, the label being the number plus ⭐);
  - the line `✍️ Other — reply to this message`.
  - Store `markup` and the exact `question` body on the record, so `_close_request_message` (telegram.py L375)
    re-renders it with the outcome.
- **`verify.outcome_line`** (L284): for a `spec-question`, return `✅ Chosen: <option label>`, or
  `✅ Chosen: <free text>` for an Other reply.
  - A tap records the option label (`_danreq_tap` reads `rec["options"][idx]`). Store the option labels as
    `options`.
- **Free text** already routes by reply `message_id` (`_route_freetext_answer`, L493). Nothing new is needed; the
  lane proves it.
- **Reminders:** extend `_remind_dan_verifications` (telegram.py L584) or add a sibling for
  `awaiting_dan_questions`, using the same 24 h / `REMINDER_INTERVAL_SEC` throttle.
- **Watchdog:** `maestro/watchdog.py:488` must also treat `awaiting_dan_questions` as waiting.
- **`/status`** (`maestro/hitl/commands.py` ~L1325, where `verify.waiting_lines` is used): list waiting questions.

### 2.5 Recording the answer: new module `maestro/docs/decisions.py`
- **`add_decision(content, task_id, entry) -> str`** is a *textual* edit of the task's yaml block. Don't re-dump
  the block: Dan's comments and style must survive.
  - Find the block with `taskgraph.iter_yaml_matches` / `parse_block`, as `complete.task_section_span` does
    (`maestro/docs/complete.py:87`).
  - With no top-level `decisions:`, append `decisions:\n` plus the rendered item before the closing fence.
  - Otherwise insert after the last line of the existing list, i.e. before the next top-level key line.
  - Render the item with `yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=100)`, indented by
    2 spaces.
  - Then re-parse and assert `decisions == old + [entry]` and every other key is unchanged, or raise.
- Entry: `{at: <UTC iso>, asked_by: <role>/<node>#<n>, question, answer}`.
- **ADR** (`adr_change(repo, task, question, answer) -> (path, new_text) | None`):
  - No `docs/adr/` means skip it and journal `dan_decision_adr_skipped` with the reason.
  - Otherwise make **one** agent call, `agentcall.ask("diagnoser", prompt, timeout=300, cwd=repo)`, behind a
    module-level `_adr_agent` seam that lanes patch. The prompt carries every ADR's title and full text (bounded,
    ~40k chars total) and asks for JSON only:
    - `{"action":"append","path":"docs/adr/000N-….md","paragraph":"<in the ADR's own voice, ending (Dan, YYYY-MM-DD)>"}`, or
    - `{"action":"new","slug":"…","title":"…","body":"<house format>"}`.
  - The controller writes the file. The agent never edits: the path is confined to `docs/adr/*.md`, and a new ADR
    number is the next free `NNNN`.
  - On failure or unusable JSON, retry once. After that, skip the ADR and journal `dan_decision_adr_failed`. The
    ROADMAP decision still lands (it is binding); the ADR is documentation.
  - House format: see DuetFlow `docs/adr/0003-…md`, which is prose under an `# Title` and whose last paragraph ends
    "(Dan, 2026-09-28)".
- **`record(repo, task_id, entry, adr) -> sha | None`:** model it on `docs/roadmap.reduce_roadmap_deps`
  (`maestro/docs/roadmap.py:379`).
  - Write the ROADMAP and the ADR.
  - CAS: update the block hash only if the block matched the CAS before (never launder a hand edit). Update
    `document_hash` the same way.
  - `git add -- <roadmap> <adr>`, then
    `git commit --no-verify -m "roadmap(<task>): record Dan's decision (<asked_by>)" -- <paths>`, in `REPO`
    (the project main checkout, as graduation does; D18).
  - The commit touches only those paths, so it merges trivially with task branches (D20).
- **Taskgraph:** unknown keys already survive in `legacy_fields` (`maestro/taskgraph.py:83, 364`). Add a
  round-trip test for `decisions` in `tests/test_taskgraph.py`.

### 2.6 Lanes (`tests/graph_engineering/test_end_to_end.py`, in the style of `_ReviewingAgent` / D24 at L700–780)
- **Harness:** the channel is swapped through `orchestrator._graph_dan_channel` (orchestrator.py:2098). Give the
  lanes a `DanVerificationChannel(send=fake, configured=lambda: True, …)` that records sends and returns
  `message_id`s.
  - Simulate a tap with `telegram.ack_danreq_callback` / `_handle_danreq_callback` on
    `{"data": "danreq:<id>:<i>", ...}` from an `on_poll` hook.
  - Simulate a reply with `_route_freetext_answer(text, {"message_id": mid})`.
  - Check how the existing `kind: dan` lanes do this before writing a new harness: grep `dan_confirm` / `verify`
    in `tests/graph_engineering/`.
- **The lanes:**
  1. `test_a_reviewer_question_asks_dan_and_the_rework_carries_his_decision`: the message has buttons and ⭐; a tap;
     the ROADMAP `decisions:` and the ADR commit on `main`; the rework brief has DAN'S DECISIONS plus the
     reviewer's findings; the writer's `quality_failures` is unchanged.
  2. `test_an_implementer_question_reruns_the_implementer_with_the_decision`.
  3. `test_a_diagnoser_spec_gap_reruns_the_failed_node_with_the_repair_budget_untouched`.
  4. `test_a_free_text_other_answer_is_recorded_as_the_decision`.
  5. `test_a_question_wait_survives_an_orchestrator_restart`: halt while waiting, `run_lane` again, then answer.
  6. `test_an_answered_question_is_in_every_later_brief_and_never_re_asked`.
  7. `test_a_decision_appends_to_the_most_relevant_adr` / `test_a_decision_with_no_fitting_adr_writes_a_new_one`.
  8. `test_a_project_without_adrs_records_the_decision_and_skips_the_adr`.
- **Unit tests:** `decisions.add_decision` (no key, existing key, comments preserved, CAS),
  `dan_questions.parse`, `render_question`, `outcome_line`.

## 3. Out of scope
Unchanged from s3r:
- the Context Gate (AGENTS.md: not built, don't build it);
- stronger-model escalation;
- D23;
- DuetFlow ROADMAP edits;
- auto-answering after a timeout.

## 4. Verification and report
Unchanged from s3r §4:
- suite counts before and after, and the lane names;
- one live Telegram check, **only with Dan's OK in chat first**: a tap on the recommended option and a free-text
  reply; report the message ids and the ✅ edits;
- integration per s3r §3: HALT, rebase, full suite, adopt, doctor (OK plus the 2 known WARNs), manifest
  `code_under_test` amendment, remove HALT, watch for `orchestrator_relaunched`;
- update the STATE.yaml D26 thread: FIXED <sha>, the lanes, live pending;
- tell Dan, in IDT.

Traps (from s3j §4):
- auto mode refuses journal writes, token reads and `git worktree remove --force`; hand those to Dan as one
  `! <command>` line;
- pytest: `-m pytest --junitxml=<file in repo root> -n auto`, without `-q`, and delete stray `.junit-*.xml`;
- foreground `sleep` is blocked.

Hand off again at ~180K context (Opus 5.5). Commit to the branch early and often.
