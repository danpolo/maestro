# Handoff: Graph P12, session 3x. D29, deny-list hits ask Dan with an agent's recommendation. Then 06 run 7

This file is the whole prompt. Session s3w adopted D28 (85d1cb7) and watched two runs of 06:
- Run 5 was lost to D4 after a harness outage.
- Run 6 passed 3 review rounds and Dan's real V-DAN. The merge was then **refused by the deny-list** on the app's own
  `DELETE FROM duet_state`.

Dan decided the fix. **Your job:**
1. Build D29 (TDD, lanes, full suite).
2. Amend the manifest and adopt into DuetFlow.
3. Watch 06 run 7 to an accept, or a park.

## 0. Read first
- `docs/graph-engineering/STATE.yaml`:
  - `Pilot finding D29`: the evidence and **Dan's decision**.
  - `Pilot finding D28`: runs 5 and 6, round by round.
  - `Pilot finding D4`: it recurred on run 5.
  - `Pilot finding D26`: the ask-Dan machinery you will reuse.
- `handoffs/2026-09-28-graph-p12-s3w-adopt-and-run5.md` §2 and §4: the adopt procedure and the traps. They still apply.
- `handoffs/2026-09-28-graph-p12-s3t-dan-questions-lanes.md`: how D26 was built, tested, integrated and adopted.
  Follow the same shape: worktree, lanes, full suite, manifest amendment, adopt, doctor.
- `handoffs/2026-09-26-graph-p12-s3j-run9.md` §3 item 3: the archive procedure.
- Memory: `deny-list-hits-ask-dan-with-recommendation`, `agents-ask-dan-on-spec-gaps`, `talk-in-local-time`
  (IDT = UTC+3), `handoffs-are-the-whole-prompt`, `gaps-are-not-decisions`, `no-idle-subagent-polling`,
  `do-cleanup-yourself`.

## 1. State (2026-09-28 ~13:10 IDT, end of s3w)
- **maestro** `feat/graph-engineering-foundation`, HEAD **d21ccb4**:
  - 98fcf9c is the manifest amendment 294922d → 85d1cb7.
  - d21ccb4 archives the run-14 and run-15 exports.
  - The suite baseline is **5111 passed + 1 xfailed**. Run it from the main checkout with `.venv/bin/python -m
    pytest` (s3v traps: system python fails 2 tests; a worktree fails 5 `test_next_graph_prompt` tests because
    specs are git-ignored; xdist is not installed; `-q` hides the summary).
  - `artifacts/graph-engineering/p12-pilots/duetflow.json` is untracked; leave it.
- **DuetFlow** is **HALTED**, pinned to 85d1cb7, with nothing in flight.
  - Main is b35d98b, s3w's commit of the orchestrator's dependency-map regen.
  - The real Duet **exists**: `config.yaml` `duet.playlist_id` 1kOGVDMgPBM3fjqzwxS9NV. The owner (Person A) was
    re-consented with `playlist-modify-public` during run 6's V-DAN, so **the V-DAN re-consent step is already
    done**; do not ask Dan to repeat it.
- **Run 14** = 06 run 5 (`run_LDCJrFlPGcIP48Ef`):
  - Implementer 2 could not use tools: the Claude CLI auto-mode classifier gave "no verdict" around 11:35–11:39 IDT.
    It exited 0 with no sentinel, so the node settled `uncertain` and got stuck (D4).
  - Archived: branch `archive/06-writer-first-light-run5`, `.orchestrator/archive/2026-09-28-run14`.
- **Run 15** = 06 run 6 (`run_mqSXWdrvV78A6xDk`):
  - Review rounds found 4 blocking findings, then 2, then an approve (plus 1 follow-up). There was no diagnose and no
    D26 ask. `scope_widened_for_rework` fired live 4 times (`docs/PLAN.md`).
  - Dan approved V-DAN at 12:55 IDT.
  - `merge_refused`: the pattern `DELETE\s+FROM` matched `conn.execute("DELETE FROM duet_state")` in
    `duetflow/playlist.py`. The run settled `failed` at node `merge`.
  - Archived: branch `archive/06-writer-first-light-run6` at **d2ed885** (the approved work),
    `.orchestrator/archive/2026-09-28-run15`.
- The run-6 follow-up is still open, because the run never accepted: `cmd_refresh` prints "nothing was written"
  after a successful empty PUT. Leave it to 06's own follow-up task once 06 accepts.

## 2. Dan's decision (2026-09-28, verbatim intent)
"Send this to an agent that sends to me with a recommendation, and I can approve/disprove with a button." So:
- **Gate first.** Run the same `merge.deny_list_violation` check on the candidate in the **gate**, before any review
  round or V-DAN, so a hit never costs Dan a real-world test again. Today it runs only in
  `merge.integration_guard` (merge.py ~190–225, called from `integrate_candidate` ~466, handler
  `handlers.py` ~1215–1270, journal `merge_refused`).
- **On a hit:**
  - An agent examines the hit in context: the matched text, the file, the surrounding diff, the task, and
    project.yaml's deny-list comments (which say why the pattern exists).
  - It writes a recommendation: allow, with a reason, or send back, with what to change.
  - Maestro sends Dan a Telegram message: the hit, the agent's recommendation and reason, and **Approve /
    Disapprove buttons**.
  - Reuse D26's ask machinery where it fits: `maestro/workflows/dan_questions.py`, `WorkflowRunner._ask_dan`
    (runner.py ~2375), `maestro/hitl/telegram.py`, `dan_request.py`, `verify.py`. While it waits, the cone spends
    no budget.
- **Approve:** the run proceeds, and the merge-time re-check accepts that exact hit.
  - Bind the approval to the hit (file plus matched text, or the diff hunk hash) and to the task/run. A new or
    changed hit asks again.
  - Record it durably: an event plus a journal line (`deny_list_hit_approved`) and the approver.
- **Disapprove:** the hit goes to the implementer as a **blocking finding**, with the agent's "what to change" as
  the rule/verify text. It follows the normal rework path.
- **Never:**
  - weaken `_HARD_DENY_PATTERNS` or `deny_list_extra`;
  - auto-allow a hit without Dan;
  - have the agent's recommendation stand in for Dan's tap.
- **Your own design choices** (gaps-are-not-decisions; ask Dan only real uncertainties, via AskUserQuestion):
  - which model recommends (a reviewer-strength model is reasonable);
  - whether the recommending agent is its own node or part of the gate handler;
  - the exact binding key.

## 3. In scope, in order
1. **Build D29** in a worktree on persistent storage (never /tmp), TDD. Name lanes as behaviours, for example:
   - `test_a_deny_list_hit_is_caught_in_the_gate_before_review`
   - `test_a_deny_list_hit_asks_dan_with_the_agents_recommendation_and_buttons`
   - `test_an_approved_hit_lets_the_same_change_merge`
   - `test_a_changed_hit_after_approval_asks_again`
   - `test_a_disapproved_hit_returns_to_the_implementer_as_a_blocking_finding`

   Also keep a check that the hard pattern still refuses when nobody approved it (the existing merge-guard tests
   in `tests/graph_engineering/test_merge_guard.py` and `tests/characterization/test_merge.py` must stay green).
   Run the full suite and report the count against 5111 + 1 xfailed.
2. **Integrate.** Fast-forward onto `feat/graph-engineering-foundation`. Remove the worktree and branch yourself.
3. **Manifest amendment.** Add `code_under_test` 85d1cb7… → the new sha, with a `reason` (D29) and a `note` (no
   criterion or weight change; 06 is re-run as run 7). Use `json.dumps(..., indent=2, ensure_ascii=False)` and check
   that the diff adds only your lines. Commit.
4. **Adopt and run doctor**:
   - `selfupdate.materialize_worktree` plus `adopt(sha, project_repo=Path('/home/dan/projects/duetflow'))`, with
     `MAESTRO_REPO=/home/dan/projects/duetflow`, all from the maestro `.venv`.
   - Doctor should show OK plus 2 known WARNs (systemd_unit, meta_branch).
5. **Run 7 of 06.** Remove HALT; the watchdog relaunches within ~10 s.
   - Watch with a Monitor. s3w's script `watch.sh` lived in its scratchpad, so recreate it:
     - Journal: `tail -F journal.ndjson`, grep for `graph_run_|"dan_|scope_widened|deny|watchdog_|orchestrator_|park|halt|error|crash|fail|refus`.
     - Node phases: a 20 s poll, printing only on change, of `node_states` for the newest `task_runs` row
       (`order by started_at desc`). Note that `node_states` is a **table**, not a column. Open the DB read-only
       (`file:...?mode=ro`).
   - Expect the new gate check to catch `DELETE FROM duet_state` (or whatever the implementer writes) **before
     review**, and Dan's Telegram ask with the recommendation. Tell Dan in chat that the ask is coming.
   - At V-DAN, tell Dan his steps: **collect, refresh**, then check in Spotify.
     - The re-consent is done.
     - The Duet already exists, so this Refresh must **reuse** `duet.playlist_id`: no new create POST. That is the
       notes' create-once rule, checked live.
   - After an accept:
     - DuetFlow main is clean, with no `stale_checkouts`.
     - Graduation touches only docs.
     - `push_skipped_no_remote` appears once.
     - `06-writer-first-light-followups` is created.
     - Report to Dan in IDT.
   - On a park or `uncertain`: halt, archive as run 16 (export first with `--since` = run 7's open time minus 1 min),
     keep HALT, and diagnose from evidence before any fix. Dan's standing choice for D4 is archive and restart.
6. Update STATE: close D29 with evidence from run 7 (the ask text, the tap, the time from ask to answer). Add run 7's
   rounds and findings to D28.

## 4. Out of scope
- The Context Gate: never build it (AGENTS.md).
- The D4 fix (an agent node with no sentinel should be retryable, not `uncertain`) belongs to the failure-handling
  phase. It is recorded in STATE; do not build it here.
- A reviewer depth bound (D23). The one `missed_earlier` in run 6 was round 2 catching what run 5's round 1 caught.
  Record it; don't fix it.
- Reusing run 6's approved work (d2ed885) for run 7. No seeding mechanism exists; don't invent one.

## 5. Coordination and traps
- No other session touches DuetFlow or maestro.
- Halt DuetFlow before committing maestro code. Adopt only while it is halted.
- The Claude CLI auto-mode classifier failed transiently in s3v and in run 5. If an agent node goes `uncertain` with
  "classifier gave no verdict" in its `impl.log`, probe it first:
  `claude -p --model claude-sonnet-5 --permission-mode auto "Use the Bash tool to run: echo probe-ok…"`
  Then archive and restart.
- On settle, the orchestrator removes the task worktree itself. When archiving, `git worktree prune` and then rename
  the `impl/` branch.
- Auto mode refuses journal writes and token reads. Hand those to Dan as one `! <command>` line.
- Foreground `sleep` is blocked. Monitors expire after 30 min, so re-arm them. While waiting only on Dan (a V-DAN or
  an ask), stop re-arming and ask him to message you.
- Hand off again at about 180K context.

## 6. Report back (with numbers)
Suite count, new sha, doctor result, run 7's id and open time (IDT), whether the gate caught the hit before review,
the ask's text and Dan's answer time, review rounds with blocking counts and origins, the V-DAN outcome, whether the
Refresh reused the existing Duet (0 create POSTs), and the accept or park.
