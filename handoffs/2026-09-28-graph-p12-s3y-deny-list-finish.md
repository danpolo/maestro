# Handoff: Graph P12, session 3y. Finish D29 (deny-list hits ask Dan): gate input plumbing, lanes, suite, integrate, adopt, 06 run 7

This file is the whole prompt. Session s3x designed D29 and built most of it in a worktree, then stopped at the 180K
handoff line. **Your job:** finish the build (§3), then do s3x's original steps 2–6, repeated in §4.

## 0. Read first
- `handoffs/2026-09-28-graph-p12-s3x-deny-list-ask-dan.md`: the original brief. §2 has Dan's decision, §3.2–3.6 the
  integrate/amend/adopt/run-7/STATE steps, §4 what is out of scope, §5 the traps, §6 the report-back. **All of it
  still applies.**
- `docs/graph-engineering/STATE.yaml` → `Pilot finding D29`.
- Memory: `deny-list-hits-ask-dan-with-recommendation`, `agents-ask-dan-on-spec-gaps`, `talk-in-local-time` (IDT =
  UTC+3), `handoffs-are-the-whole-prompt`, `gaps-are-not-decisions`, `no-idle-subagent-polling`, `do-cleanup-yourself`.
- Don't re-read the whole diff. Run `git -C /home/dan/projects/maestro-wt/deny-list show --stat HEAD`, then open only
  what you touch.

## 1. State (2026-09-28 ~13:45 IDT, end of s3x)
- **Worktree** `/home/dan/projects/maestro-wt/deny-list`, branch `s3x/deny-list`, one WIP commit **62f4311** on
  d21ccb4. The tree is clean.
  - `docs/graph-engineering` in the worktree is a **symlink** to the main checkout's git-ignored directory
    (excluded via `.git/info/exclude`). **Never commit it.**
  - Run tests from the worktree with `/home/dan/projects/maestro/.venv/bin/python -m pytest …`.
- **Main checkout** `feat/graph-engineering-foundation` is still at d21ccb4. `artifacts/graph-engineering/p12-pilots/duetflow.json`
  is untracked; leave it.
- **DuetFlow** is still **HALTED**, pinned to 85d1cb7, with nothing in flight. Main is b35d98b. The real Duet exists
  (`duet.playlist_id` 1kOGVDMgPBM3fjqzwxS9NV), and the owner's re-consent is already done.
- At 62f4311, `test_merge_guard.py`, `characterization/test_merge.py` and `test_dan_decisions.py` give 190 passed.
  The full suite has **not** been run. Baseline: **5111 passed + 1 xfailed** (run it from the main checkout).

## 2. The design, as built in 62f4311 (s3x's own calls, not Dan's)
- **Binding key.** `merge.deny_hit_key(file, pattern, match)` is sha256(file, pattern, match with whitespace
  collapsed)[:16]. A ruling is bound to the task **and its workflow_id**:
  - a plan-repair successor run keeps the ruling;
  - a fresh run of the task (a new workflow) asks again;
  - a changed line gets a new key, so it asks again.
- **`merge.deny_list_hits(diff_text, changed, proj)`** returns `(hits, refusal)`:
  - each hit is `{key, kind: pattern|path, file, pattern, match, line}`;
  - the patterns are the same `_HARD_DENY_PATTERNS` + `deny_list_extra` + secrets/prod_stores, unchanged;
  - it matches per file, and also on the whole added text as before, so a cross-file match is still reported under
    `(across files)`;
  - an invalid regex is a hard `refusal`.
- **`merge.integration_guard(..., approved_hits=())`** refuses on the first unapproved hit, with the same wording as
  before (`hit_violation`). `integrate_candidate(..., approved_deny_hits=())` passes the keys through.
  `merge_handler` passes `ctx.inputs["deny_list_approved"]`.
- **`maestro/workflows/deny_list.py`** (new):
  - `screen(tree, config)` diffs the merge base of HEAD and `config.target` against HEAD, then splits the hits into
    approved, disapproved and unruled using `config.rulings`.
  - `recommend(...)` makes one agent call (seam `deny_list._agent(prompt, cwd)`, default
    `agentcall.ask("diagnoser", …)`, 300 s) with one retry. The prompt carries the task and its notes, the whole
    project.yaml (its deny-list comments), and each hit's diff hunk. The reply is JSON
    `{hits:[{key, recommendation: allow|send_back, reason, what_to_change}]}`. If the reply is unusable, the ask goes
    to Dan with "No recommendation" and the reason.
  - `finding(hit)` gives the blocking-finding text: the file, the line, the pattern, plus the agent's what_to_change
    and Dan's note.
- **`handlers.verification_handler`**:
  - it screens only when the checks passed **and** `ctx.inputs["deny_list"]` is present (`_deny_list_screen`);
  - on a disapproved hit it returns `failed`, `verification_failed`, `outputs.deny_list_disapproved`, which follows
    the normal gate-rework path;
  - on an unruled hit it calls `recommend` and returns `failed` + `outputs.deny_list_asks` + failure kind
    `dan_question`;
  - otherwise it annotates `outputs.deny_list`.
- **`verify.py`**:
  - `DENY_TYPE="deny-list-hit"`, ids `dl-<task>-<sha1(workflow|key)[:8]>` (`deny_request_id`);
  - `interpret_deny`: a tap gives "Approve"/"Disapprove"; a reply starting "approve" approves; any other reply
    disapproves, and its text becomes Dan's note;
  - `render_deny`: ⛔ header, the file, pattern and line, 🤖 the recommendation, reason and what to change, and the
    buttons `✅ Approve` / `❌ Disapprove` (`danreq:<id>:0|1`);
  - `deny_records(run_id=, task_id=, workflow_id=)`, `DanVerificationChannel.poll_deny(spec)`, and `outcome_line`
    ("✅ Approved" / "❌ Disapproved — …").
  - The wait **shares `awaiting_dan_questions`** (`_mark_question`, `mark_question_recorded`), so reminders,
    /waiting, /status and the watchdog already cover it.
- **`runner.py`**:
  - `_commit_result` routes `deny_list_asks` to `_screen_deny_asks`, then to `_ask_deny`.
    `requeue_for_question` runs with cone `(gate,)`, so no budget moves. It then sends one `poll_deny` per hit,
    writes journal `deny_list_hit_asked` and event `DenyListHitAsked`.
  - `_awaiting_dan` also counts deny records.
  - `tick` calls `_poll_deny_rulings`, which does `mark_question_recorded(ruling=, note=, by="dan", ruled_at=)` and
    writes journal `deny_list_hit_approved` / `deny_list_hit_disapproved` (with `by=dan key=… file: match`) and
    event `DenyListHitRuled`.
  - `deny_rulings()` / `deny_approved_keys()` read the records for (task, workflow).
  - `rework_note` adds the disapproved hits as blocking findings.
  - `node_inputs`: the gate gets `deny_list={target, rulings}` **only when a channel with `poll_deny` exists and the
    lane has a merge node**; the merge gets `deny_list_approved`.

## 3. Finish the build
1. **BUG to fix first: the gate never receives the `deny_list` input.** `node_inputs` feeds only in-process
   (controller) nodes through `_run_inline`. The gate is dispatched to a worker (`maestro.workflows.worker`), which
   builds its inputs from argv: `--affected-selection-artifact-id`, `--post-action-cmd`,
   `--failure-context-artifact-id` (worker.py ~515–535; argv in `runner._default_worker_argv` ~3340–3385). That is
   why the existing deny lane in `test_merge_guard.py` still passed unchanged.
   - Fix: in `_default_worker_argv`, for a `VERIFICATION_HANDLER` node, put the `deny_list` config in an artifact.
     Follow the affected-selection pattern (`self.artifacts.put_bytes(..., type="deny_list_config")`, runner
     ~715–732), pass `--deny-list-artifact-id`, and read it in the worker into `inputs["deny_list"]`. Check how
     `claim.selection_artifact_id` is threaded, and pick the smallest equivalent. One option: compute the config in
     `advance` just before `dispatch` for a verification node, and store it on the claim or in
     `_attempt_inputs`.
   - Keep the `node_inputs` branch for the in-process path, because lanes may run the gate inline.
   - Once fixed, check that `test_a_deny_list_hit_refuses_the_merge_and_the_run_parks` and its 2 siblings in
     `test_merge_guard.py` still pass. Their lanes install a `DanVerificationChannel` (via
     `orchestrator._graph_dan_channel`) that nobody answers, so they will now **wait on Dan** instead of parking at
     merge. Decide from evidence:
     - Preferred: those lanes keep testing the merge guard by swapping `_graph_dan_channel` for a channel without
       `poll_deny`, or by patching `DanVerificationChannel.poll_deny` away. The unapproved hard pattern must still
       refuse at merge. Add or keep a unit test on `integration_guard` for that too.
     - Don't weaken any pattern.
2. **Lanes** in a new `tests/graph_engineering/test_deny_list_ask.py`:
   - Reuse `test_merge_guard._WritingAgent` (extend it to write different files per implementer round, and a
     reviewer that can reject round 1). Reuse `test_end_to_end._DanAnswers` / `_question_lane` style: its `answer()`
     walks `awaiting_dan_questions`, which deny records share. `("tap", 0)` is Approve, `("tap", 1)` is Disapprove.
   - Patch `maestro.workflows.deny_list._agent`, record its prompts, and return the JSON.
   - Behaviour names:
     - `test_a_deny_list_hit_is_caught_in_the_gate_before_review`: the ask is sent before any reviewer brief; the
       gate attempts are `[failed (dan_question), succeeded]`; quality_failures stay 0.
     - `test_a_deny_list_hit_asks_dan_with_the_agents_recommendation_and_buttons`: the text has the file, the line,
       "Recommendation: Allow" and the reason; the buttons are `✅ Approve` / `❌ Disapprove`; the prompt carries
       the task, the project.yaml comment and the hunk.
     - `test_an_approved_hit_lets_the_same_change_merge`: the target has the `DELETE FROM duet_state` line; journal
       `deny_list_hit_approved` contains `by=dan`; event `DenyListHitRuled`; no `merge_refused`.
     - `test_a_changed_hit_after_approval_asks_again`: round 2 changes the line, so there are 2 asks with 2 distinct
       keys.
     - `test_a_disapproved_hit_returns_to_the_implementer_as_a_blocking_finding`: the rework brief contains
       "Deny-list hit disapproved by Dan" and the what_to_change; round 2 is clean and merges.
     - Unit tests: `deny_list_hits` (per file, across files, path hit, key stability under whitespace, a
       `+++`-looking content line), `interpret_deny`, `render_deny`, `recommend` retry/no-recommendation, and
       `integration_guard` with approved vs changed keys.
3. **Full suite** from the main checkout after integrating, or from the worktree with the symlink:
   `.venv/bin/python -m pytest` without `-q`, since `-q` hides the summary. xdist is not installed. Report the count
   against 5111 + 1 xfailed.
   - Likely collateral: tests that snapshot gate inputs or worker argv; `question_waiting_lines` wording (it says
     "decision question" for deny asks too, which is optional to refine).
4. **Squash** the WIP into one commit, `feat(graph): a deny-list hit is caught in the gate and asks Dan with an
   agent's recommendation (P12 pilot D29)`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## 4. Then do s3x §3 steps 2–6 unchanged
Integrate (fast-forward, remove the worktree and branch yourself), amend the manifest (85d1cb7 → new sha, reason D29,
note: no criterion or weight change, 06 re-run as run 7), adopt plus doctor (OK + 2 known WARNs), run 7 of 06 with a
Monitor, and update STATE (close D29 with run-7 evidence: the ask text, the tap, the time from ask to answer; add
run 7's rounds to D28). Tell Dan in chat that the deny-list ask is coming. At V-DAN his steps are **collect, refresh**,
then check in Spotify: the Refresh must reuse `duet.playlist_id`, with 0 create POSTs.

## 5. Known limits to record in STATE
- The recommender runs inside the gate worker. It adds up to 2 × 300 s to that gate attempt, and only when there are
  unruled hits.
- Rulings bind to the workflow, not the run: a plan-repair successor keeps them.
- The approver is recorded as `by=dan` (the Telegram tap on Dan's bot). The Telegram user id is not captured.

## 6. Coordination and traps
s3x §5 unchanged: halt before committing maestro code, adopt only while halted, the classifier probe, auto mode
refusing journal writes and token reads (hand those to Dan as one `! <command>` line), foreground `sleep` blocked,
Monitors expiring after 30 min. Hand off again at about 180K.
