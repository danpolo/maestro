# P12 verdict evidence audit (read-only)

This is a read-only audit, taken at **2026-10-08 ~09:02 UTC (12:02 IDT)**. Maestro HEAD was `4b97273` on `feat/graph-engineering-foundation`. DuetFlow was pinned at `0c7eb92`. Its controller was live (PID 1943955) and HALT was absent.

Sources:
- the spec, `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (P12.md below);
- `docs/graph-engineering/STATE.yaml` (STATE below);
- `docs/graph-engineering/specs/README.md` §3, §8, §9 and §10;
- the qualification report, `artifacts/graph-engineering/p12-qualification.json` (qual.json below);
- the pilot exports, `artifacts/graph-engineering/p12-pilots/*.json`;
- the DuetFlow `.orchestrator/` directory, read with `sqlite3 -readonly`.

Note: `docs/graph-engineering/` and `docs/PROGRESS.md` are gitignored on this branch (`.gitignore:26-27`), so STATE and PROGRESS are on-disk content only.

UNVERIFIED in this audit:
- The full pytest suite and `scripts/graph_qualification.py` were **not re-run** at `4b97273`. The latest recorded full suite is "5538 passed / 1 xfailed" on `0c7eb92` (STATE `_p12_open_threads[3]` note). Commit `4b97273` adds the D33 `.env` fix and its regression test after that run.

---

## 1. Acceptance criteria and checklist

### Acceptance criteria (P12.md §3)

| Criterion | Verdict | Evidence | Gap |
|---|---|---|---|
| All 12 system invariants pass | **PARTLY** | qual.json `summary`: `invariants_checked 6, passed 6, not_checkable 6` (INV-02/03/04/05/07/10 checked by fixture lanes). Each unchecked one (INV-01/06/08/09/11/12) names its covering tests in `invariants[].covered_by`. `scripts/graph_qualification.py:53-66` gives the reasons. The pilot gives indirect live evidence: one controller PID throughout (INV-01), pinned doctor exit 0 at each adoption (INV-12), and deny-list and D30 permission holds fail closed (INV-04). | 6 of 12 invariants are proven only by unit or lane tests, not by the qualification run. qual.json dates from **2026-09-18** (fixture only) and has not been regenerated since the pilot. Not re-run at HEAD (UNVERIFIED). An INV-09-adjacent leak is open: the `/ask` tmux window lacks PYTHONPATH and so runs the **live maestro checkout, not the pinned version** (thread #4 note, carry-over). Thread #41 says the D13 negative control (codex touching `refs/heads/<other>`) was never run live. |
| Every uncertain optimization has an enabled/disabled decision backed by empirical evidence | **PARTLY** | qual.json `experiments` E1–E5 each carry a decision: E1, E2 and E3 "stays off", E4 and E5 "stays as shipped". | Every `evidence` field says there is no measurement: "fixture lanes only; no token or pass-rate measurement", "none recorded", "no ... comparison". The decisions are conservative defaults, not empirically backed. Pilot data relevant to E4 exists (diagnose and plan repair ran live; 14 diagnose attempts) but was never folded back into the report. |
| Unqualified backends remain labelled unqualified; mock passes cannot claim production readiness | **MET** (with a note) | qual.json `modes`: subscription is "fixture-passed; unqualified until the real pilots (s3) qualify paid subscription"; free, hybrid and script are "unqualified (Dan, 2026-09-18 ...)". `docs/EXECUTION.md` "Graph qualification (P12)" says "Every mode in the report is labelled unqualified" (around lines 147-149). `docs/DESIGN.md:841-842` says the same. | Paid subscription has not yet been formally relabelled as qualified; that is the pending verdict. Note: **agy (`antigravity_cli`, gemini-3.8-flash-high) did real implementer work** in the pilot (3 attempts, including one in accepted run `run_qG3zSNhcUbdHKyDm`), although README §9 says agy is graded below claude and codex and "gets no real work". That is a fallback route, not a qualified backend. |

### Checklist (P12.md §2) and target files

| Item | Verdict | Evidence | Gap |
|---|---|---|---|
| End-to-end suite plus fault matrix (rollback, lost controller, writer crash, quota switch, interrupted integration) | **MET** (on record) | `tests/graph_engineering/test_end_to_end.py` exists (2431 lines). qual.json `fault_matrix` lists all 5 fault types (6 tests, interrupted integration split before and after the swap), every one `present: true`. STATE `in_progress_details.what_was_done`: "s2d end: full suite tests=4732 failures=0" and "test_end_to_end.py 15 lanes green". Last full run: 5538 passed on `0c7eb92`. | The fault-matrix tests are "not re-run by this script" (qual.json note). The suite has not been re-run at `4b97273` (UNVERIFIED). |
| Scratch pilot on **two** repos (DuetFlow + instagram-to-value) | **NOT MET** (DuetFlow MET; ITV absent) | DuetFlow: 13 tasks accepted and graduated (see §3). instagram-to-value: manifest `projects.instagram-to-value.status = "pending: waits for Dan's grill-me run; not onboarded"`. `/home/dan/projects/instagram-to-value/.orchestrator` does **not exist**, and there is no `project.yaml`. STATE `in_progress_details.whats_left[0]` says "ITV waits for Dan's grill-me". The handoff `2026-09-26-...-s3k-telemetry.md:77` says "ITV only if Dan reports grill-me done". | No brownfield pilot was run, so regression avoidance, `bot_files` and `deny_list_extra` confinement on a living codebase are unvalidated. No record exists of Dan waiving ITV. |
| Raw per-task outcomes retained (transcripts, rework counts, tool logs) | **MET** | 18 operator archives under `duetflow/.orchestrator/archive/` (each with the control DB, attempts and journal). 19 tracked exports in `p12-pilots/` (run1–18 plus `duetflow-07-run4-accepted-20260930.json`) hold per-attempt records and `summary.rework_attempts` / `human_interventions`. Live `attempts/`: all 20 agent attempts keep `impl.log`, `session_uuid.txt` and `result.json` (reviewers also keep `review.json`). | `p12-pilots/duetflow.json` (untracked) is **stale**: exported 2026-09-24, it covers run 1 only. No final export covers the 6 live runs after run 18, apart from the 07 run-4 export. |
| Per-attempt P12A telemetry (model, agent version, session, fresh/resume, token split, quota windows, wall time, reset crossing) | **MET** | Live DB, agent attempts (backend not null): `agent_version`, `session_id`, `session_mode`, `usage_settle_json` and `wall_time_sec` are set on 20/20; `usage_start_json` and `reset_crossing` on 19/20. The settle JSON carries `token_split` (fresh_input, cache_read, ...). Thread #13 (D1/D2, closed 9b21e3e) fixed the earlier NULLs. | One attempt has no start usage. Archived runs 1–2 predate the D1 fix. |
| B01 / B18 closed | **NOT MET** | The evidence register `GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md:102` says B01 is "Open — closes in a pilot"; line 107 says B18 "Machinery closed; evidence blocked on B01". `docs/graph-engineering/graph-engineering-research-blind-spots.md:30,40` still lists both as open actions. A grep of the specs, STATE and docs found no closure record for either. B01 asks for "two materially different scratch projects". | B01 needs the second project (ITV). B18 needs a stratified matched-task set with paired results and uncertainty; nothing like that exists from the pilot. |
| Artifact volume (B15, INV-A03) and retention default | **MET** | Thread #50 was CLOSED by `6e1c10e` (basis text updated, no value change): DuetFlow `.orchestrator` was 84,069,125 B after 11 accepted tasks, about 7.6 MB per task; `retention.warn_bytes` stays at 1 GiB with `prune: false`. Today, `du -sb .orchestrator` = 87,833,187 B. | None for P12. B15 disk-full and restore scale remain outside this phase. |
| `scripts/graph_qualification.py` refuses a non-scratch target without a flag | **MET** | `scripts/graph_qualification.py:128-140` (`refuse_target`, `--deployment`). Commit `d4af85f`. | The report it writes is fixture-only (2026-09-18). It has not been re-run with pilot data. |
| Docs: DESIGN, EXECUTION, PROGRESS updated | **PARTLY** | DESIGN §16 "graph runner as qualified in P12" (`docs/DESIGN.md:655`; last commit `c0f484b` 2026-10-08). EXECUTION "Graph qualification (P12)" (`docs/EXECUTION.md:117`; last commit `3898496` 2026-09-25). PROGRESS P12 entry at `docs/PROGRESS.md:4283`, "real pilots next". | `docs/PROGRESS.md` mtime is **2026-09-18**. It has no pilot outcome and no subscription verdict. EXECUTION has nothing about the pilot result. |
| Release tag (P12.md header "Output") | **NOT MET** | `git tag --list` is empty. | No release tag. |
| §4 Context Gate mapping in the qualification report | **MET** | qual.json `context_gate_mapping` covers Goal / WorkUnit / Session / ContextSnapshot / GateDecision / OutcomeRecord. GateDecision is stated as "no maestro structure yet; the gate is not built". The gate is **not built** (AGENTS.md), and nothing here claims it is. | No gate-seam thread was found with `disposition: needs-dan`. None appears to be needed. |
| §10 field notes F1 (reviewer read-only boundary) rechecked at close-out | **NOT MET** (open, not tracked) | `maestro/workflows/worker.py:408` still passes `writable=True` to every agent, reviewers included. `docs/graph-engineering/reports/05-field-notes-artifact-assessment.md:48-72,112` requires F1 to be resolved "before claiming reviewers are read-only" and before production adoption. | F1 is not in `_p12_open_threads`. Independent review is not a qualified isolation guarantee. |
| Close-out ask (P12.md:43-44): tell Dan the §9 queued phase is next and ask him to elaborate | **NOT MET** (pending) | Thread #5 (owner P12) is still open. | Do this at close and set disposition `needs-dan`. |

---

## 2. `_p12_open_threads` (STATE.yaml:746, 52 entries, 0-indexed)

| # | Short title | Owner / disposition | Class | Justification |
|---|---|---|---|---|
| 0 | Manual-step contract: brief cmd, Done = ran, V-DAN ordering | post-P12 queue item (4), QUEUED | OPEN-CARRY-OVER | Dan queued it post-P12 on 2026-10-08; 08 still passed live post-merge. |
| 1 | Operator intake: bugs/features into normal workflow | operator-intake-workflow | OPEN-CARRY-OVER | A separate workstream (Dan 2026-09-30); core built, not P12 scope. |
| 2 | D32: explain scores only role-b, wrong V-DAN instruction | closed | CLOSED | Fixed in DuetFlow ROADMAP 1d135ff; no defect. |
| 3 | DuetFlow pilot HALT for gpt-6.1-sol routing | P12-pilots (RESUMED on 0c7eb92) | FIXED-BUT-NOT-CLOSED | Resume condition met (0c7eb92 adopted); 6 runs since; needs a closing disposition. |
| 4 | D33: graph /ask genuine Telegram smoke | closed | CLOSED | Live /ask delivered after fix 4b97273; carry-over noted: the ask runs the live checkout. |
| 5 | Tell Dan the post-P12 queued phase; ask him to elaborate | P12 | OPEN-P12-BLOCKING | An explicit close-out step (P12.md:43); not yet done. |
| 6 | Graph loop never creates the task worktree | closed | CLOSED | 387e902 (+55dcd15, 55f6d5e). |
| 7 | Candidate winner never merged | closed | CLOSED | 0f14a1b; candidates stay off (E1). |
| 8 | Count acceptance_decisions per run (2 by design) | P12 | OPEN-P12-BLOCKING | The data satisfies it (2 per accepted run, §3); it only needs the count reported and a disposition. |
| 9 | Docs owed: worktree refusal, TaskTreeReleased, brief header... | closed | CLOSED | s2f docs commit. |
| 10 | Inline node raising leaves the run uncertain forever | failure-handling-phase | OPEN-CARRY-OVER | Owned by a later phase; a liveness gap, not a correctness gap. |
| 11 | Only merge has a postcondition probe; others unimplemented | failure-handling-phase | OPEN-CARRY-OVER | Later phase; INV-07 holds (no blind retry), but nodes can hang. |
| 12 | Gate verified a dirty working tree, not a commit | closed | CLOSED | 46b0dae: the gate refuses an uncommitted tree. |
| 13 | D1/D2: NULL quota windows, wrong agent_version | closed | CLOSED | 9b21e3e; live telemetry is populated. |
| 14 | D3: Codex reviewer cannot write DONE | closed | CLOSED | 396e2cc add_dirs. |
| 15 | Hardcoded graph model catalog; roles ignored | modelctl-integration-review | OPEN-CARRY-OVER | Largely addressed by the modelctl release 0c7eb92; owned outside P12. |
| 16 | D4 recurred: classifier outage burned the final run | failure-handling phase (record only) | OPEN-CARRY-OVER | Record-only; the proposal is superseded by D30 dontAsk. |
| 17 | D5: ctl halt crashed under the controller lease | closed | CLOSED | 3d4bd09. |
| 18 | D6: proof review could not reject | closed | CLOSED | 3898496 review.json verdict. |
| 19 | D7: graph loop never graduates accepted tasks | closed | CLOSED | 96570a0. |
| 20 | D8: merge left the main checkout's index stale (revert) | closed | CLOSED | 9c61367. |
| 21 | D9: watchdog stall-kills long graph runs | P12-pilots ("s3e, fixing now") | FIXED-BUT-NOT-CLOSED | a86542b fixed it; the owner label is stale and there is no disposition. |
| 22 | D29: deny-list hit asks Dan at the gate | P12-pilots | FIXED-BUT-NOT-CLOSED | Fixed c40a89d and confirmed live (06 run 8, 07 run 4); no disposition. |
| 23 | D31: automatic live quota-pause recovery | closed | CLOSED | Observed live with a labelled induced limit, 2026-10-08. |
| 24 | 07 run 18 archived after a third blocking review | P12-pilots | FIXED-BUT-NOT-CLOSED | Resolved by outcome: 07 accepted in run_qG3zSNhcUbdHKyDm on 2026-09-30. |
| 25 | D30: permission lists, Telegram Allow/Decline | closed | CLOSED | Two genuine live asks in 08 prep. |
| 26 | D28: brief carries ROADMAP notes; rework widens scope | P12-pilots (FIXED 85d1cb7) | FIXED-BUT-NOT-CLOSED | Fixed, adopted, and verified live by the 06 accept; no disposition. |
| 27 | D27: undefined lost-write recovery spiral | P12-pilots (RESOLVED) | FIXED-BUT-NOT-CLOSED | Resolved in 06 run 4; no disposition. |
| 28 | D26: agents ask Dan when the spec is silent | P12-pilots (FIXED 294922d) | FIXED-BUT-NOT-CLOSED | Fixed and adopted; one live implementer dan_question in the archives; no disposition. |
| 29 | D25: kind:dan criteria told not to attempt | P12-pilots (FIXED a4ea0a9) | FIXED-BUT-NOT-CLOSED | Fixed; no disposition. |
| 30 | D24: gate rework brief names failed checks | P12-pilots (FIXED 1d19dce) | FIXED-BUT-NOT-CLOSED | Fixed; later adopted (manifest 2621262); label still says "not yet adopted". |
| 31 | D23: follow-up reviewer has unbounded edge-case scope | P12-pilots (RECORDED, not built) | OPEN-CARRY-OVER | Explicitly not built in P12; belongs to the follow-up polish redesign (§9 item 3). |
| 32 | D22: agy successes classified worker_crashed | P12-pilots (FIXED a9156ec) | FIXED-BUT-NOT-CLOSED | Fixed and adopted; no disposition. |
| 33 | D20: parallel merge conflict; writeset scheduling | P12-pilots ("found s3l; OPEN") | FIXED-BUT-NOT-CLOSED | The thread text says FIXED 7b77d14; the owner label still says OPEN. |
| 34 | D21: transitively redundant ROADMAP deps | P12-pilots ("found s3l; OPEN") | FIXED-BUT-NOT-CLOSED | The thread text says FIXED 077f2f8; the owner label still says OPEN. |
| 35 | D19: another runner's reconciler strands a node | P12-pilots (FIXED 00ff57d) | FIXED-BUT-NOT-CLOSED | Fixed; no disposition. |
| 36 | D18: graduation deleted the wrong ROADMAP section | P12-pilots (FIXED 99553cd) | FIXED-BUT-NOT-CLOSED | Fixed and adopted; no disposition. |
| 37 | D17: diagnoser repair grants one final writer run | P12-pilots (FIXED, LIVE-CONFIRMED) | FIXED-BUT-NOT-CLOSED | 3fd165c, confirmed live in run 9; no disposition. |
| 38 | D14: rework reset worktree to the first start_sha | P12-pilots (FIXED 0e80da0) | FIXED-BUT-NOT-CLOSED | Fixed; no disposition. |
| 39 | D15: reviewer brief lacked prior findings | P12-pilots (FIXED 0e80da0) | FIXED-BUT-NOT-CLOSED | Fixed; no disposition. |
| 40 | D16: review could not converge (no severity split) | P12-pilots (FIXED 0e80da0) | FIXED-BUT-NOT-CLOSED | Fixed per Dan's 2026-09-26 rule; no disposition. |
| 41 | D13 widening narrowed: git dir grants | P12-pilots (NARROWED 0e80da0) | FIXED-BUT-NOT-CLOSED | Narrowed and verified live; the negative control was never run live. |
| 42 | D13: worktree git commit failed under sandbox | P12-pilots (FIXED dd9a777) | FIXED-BUT-NOT-CLOSED | Fixed; no disposition. |
| 43 | D12: tmux window lost .env; Telegram silent | P12-pilots | FIXED-BUT-NOT-CLOSED | The thread says FIXED ad742b7; the same family recurred as D33 in /ask (fixed 4b97273). |
| 44 | D11: quota/limit text classification, pool pause | P12-pilots (FIXED, CONFIRMED live) | FIXED-BUT-NOT-CLOSED | 2b80705, confirmed in run 7; no disposition. |
| 45 | G7 legacy-parity items (pause nodes, unpark, verbs...) | P12-pilots (s3e G7) | FIXED-BUT-NOT-CLOSED | 508a9a7 / c24ba9a / 9e74c01 carried or replaced it; item 7 (self-update pin) is UNVERIFIED. |
| 46 | D10 fix edges: reviewer floor, deny-list matching | P12-pilots (record) | OPEN-CARRY-OVER | Record-only liveness edges; revisit in the failure-handling phase. |
| 47 | D10: risky_set edits gated by a stronger reviewer | P12-pilots ("s3e, fixing now") | FIXED-BUT-NOT-CLOSED | da16113 + d0452f8 + f98c778; the owner label is stale. |
| 48 | Legacy-loop parity audit G1–G6 | P12-pilots (s3e audit) | FIXED-BUT-NOT-CLOSED | G1–G6 fixed at dd9a2ff (DESIGN §16); G7 is #45; no disposition. |
| 49 | Watchdog commands nobody consumes (dead letters) | control-plane (record only) | OPEN-CARRY-OVER | Record-only; does not block the pilot. |
| 50 | B15 retention default re-measure on real pilots | closed | CLOSED | 6e1c10e; 7.6 MB per accepted task; 1 GiB kept. |
| 51 | Docs owed s2e: prep tasks, qualification script, retention | closed | CLOSED | s2f docs commit. |

**Totals:**
- CLOSED: 16 (#2, 4, 6, 7, 9, 12, 13, 14, 17, 18, 19, 20, 23, 25, 50, 51).
- FIXED-BUT-NOT-CLOSED: 25 (#3, 21, 22, 24, 26–30, 32–45, 47, 48).
- OPEN-P12-BLOCKING: 2 (#5, #8). Both are trivial close-out actions.
- OPEN-CARRY-OVER: 9 (#0, 1, 10, 11, 15, 16, 31, 46, 49).

Not in the list but relevant to P12:
- **Field-notes F1**: the reviewer is writable (§1).
- **instagram-to-value not piloted.**
- **B01/B18 open.**
- **No release tag.**

---

## 3. DuetFlow run tally

The live DB (`control.sqlite3`, read at 09:01:46Z) holds only the runs since the last operator archive (2026-09-29). The earlier runs are in the 19 exports in `p12-pilots/`. Their union deduplicates by attempt_id; 14 attempts overlap between the 07 run-4 export and the live DB.

- **Accepted tasks:** 13 (`completed_tasks.json`), all graduated to `docs/PROJECT.md`:
  - 02, 03, 04, 02-followups, 04-followups, 05, 06, 07;
  - 07-followups, 08-timers-notify, 03-followups;
  - 10-followups-sweep and 10-followups-sweep-followups (the second finished 09:01:24Z during this audit).
  - The prerequisite 01-auth was completed before the pilot and is not counted.
- **Succeeded runs in all data:** 14. Task 02 has 2 succeeded runs because run 2 was wrongly accepted over a rejecting review (D6) and never graduated.
- **task_runs by status:**
  - Live DB: `succeeded 6`.
  - All 42 unique runs (exports plus live): `succeeded 14, failed 14, canceled 9, running 5`. The 5 "running" runs were abandoned at archive time.
- **acceptance_decisions per run (live DB):** each of the 6 succeeded runs has exactly **2** rows (merge, then accept), all `accepted`.
  - The 6 runs: run_qG3zSNhcUbdHKyDm, run_UfEWmL3Y5cb3VLHN, run_oyOUdfP3xtxsV571, run_buS3h8lRrBqDdZf9, run_TmlHz98N6pniAwcd, run_trJJrmpqyEzdKsiV.
  - This matches the design (thread #8) and qual.json `acceptance_decisions_per_accepted_run: [2]`.
  - The archive exports do not carry acceptance_decisions; per-run counts for runs 1–18 are UNVERIFIED.
- **Archived runs:** 18 operator archives (`.orchestrator/archive/2026-09-24-run1` to `2026-09-29-run18`), totalling 75,939,773 B (thread #50).
  - **14 archives contain no accepted task:** runs 1, 3–8, 11–16 and 18.
  - 4 archives contain accepted tasks: run 2 (02, the D6 case), run 9 (02, 03, 04, 02-followups), run 10 (04-followups, 05) and run 17 (06).
- **Attempts:** 321 unique overall. The live DB has 49: 39 succeeded, 10 failed.

**Models per node type** (all attempts, attempt counts):

| Node | Backend:model (count) |
|---|---|
| implementer | claude:claude-sonnet-5 (55), claude:claude-sonnet-5-5 (5), codex:gpt-5.6-terra (10), codex:gpt-6-luna (15), antigravity_cli:gemini-3.8-flash-high (3) |
| proof_review | codex:gpt-5.6-sol (29), codex:gpt-6-sol (37), claude:claude-opus-5 (6), claude:claude-opus-5-5 (5) |
| diagnose | codex:gpt-6-sol (7), codex:gpt-5.6-sol (4), claude:claude-opus-5 (3) |
| prep | claude:claude-sonnet-5-5 (4) |
| gate, merge, accept, dan_confirm, human_action | controller or inline nodes, no model |

Since the 0c7eb92 adoption, the live runs use only:
- implementer: claude-sonnet-5-5, plus codex gpt-6-luna as the fallback after the induced quota on run_trJJ;
- proof_review: claude-opus-5-5.

**Failures** (all attempts):

Quality failures (`failure_class = bad_output`): **70**.
- proof_review `verification_failed`: 54. These are reviewer rejections that sent the implementer back to rework.
- gate `verification_failed`: 4.
- implementer `unknown`: 3, and `worker_crashed`: 3. The worker_crashed ones include the D22 agy misclassification.
- proof_review `worker_crashed`: 2.
- merge `unknown`: 2.
- diagnose `worker_crashed`: 1.
- human_action `verification_failed`: 1 (the 08 manual Redo).

In the live DB alone there are 4 quality failures: 2 proof-review rejections in 07 run 4, 1 gate failure in 10, and 1 human_action Redo in 08.

Non-quality failures: 31.
- diagnose refused or failed without a kind: 11.
- dan_question holds: 9 (gate 6, prep 2, implementer 1).
- quota_exhausted: 7 (proof_review 4, implementer 2, diagnose 1). One of the implementer ones is the labelled induced D31 limit on run_trJJ.
- rate_limited: 4.

---

## 4. Recommended verdict

**Recommendation: QUALIFIED PASS (DuetFlow, paid subscription only), with carry-overs. It is not a full PASS.**

Why it passes:
- DuetFlow progressed autonomously through 13 accepted and graduated tasks under `engineering.runner: graph`.
- Every accepted run has the 2 acceptance decisions the design expects.
- Raw per-attempt outcomes and P12A telemetry are retained, and B15 is closed on real data.
- The fixture qualification (10/10 lanes, all 5 fault types present) and the unqualified labels for free, hybrid and script stand.
- D29, D30, D31 and D33 were each proven live.

Why it is not a full PASS:
- The spec requires a second, brownfield pilot (instagram-to-value). It was never onboarded.
- Without it, B01 and B18 stay open.
- Only 6 of 12 invariants are checked by the qualification run itself.
- The E1–E5 decisions are defaults with no empirical evidence.
- qual.json, PROGRESS and EXECUTION predate the pilot.
- There is no release tag.
- Field-notes F1 (reviewers can write at the CLI boundary) is unresolved and untracked.

Carry-overs to list:
- the ITV pilot, plus B01 and B18;
- F1;
- threads #0, 1, 10, 11, 15, 16, 31, 46 and 49;
- the `/ask` live-checkout leak.

Close-out housekeeping:
- give the 25 FIXED-BUT-NOT-CLOSED threads a `disposition: closed` with their commits;
- close #8 with the counts above;
- do #5 (ask Dan about the §9 phase).

**To upgrade to a full PASS:**
1. Dan finishes grill-me for ITV. ITV is onboarded with `bot_files`, `deny_list_extra` and `gate.chain: [test]` as P12.md §2 specifies, and representative tasks are accepted there with raw outcomes retained. That closes B01, and B18 is addressed with the paired results.
2. `.venv/bin/python -m pytest -q` and `scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends` are re-run at the release commit. qual.json is regenerated so that it folds in the pilot data, including the subscription label and E1–E5 evidence or explicit evidence-gap decisions.
3. F1 is resolved: a read-only reviewer source boundary with a writable verdict workspace, plus a dispatch-level regression test. Alternatively, Dan explicitly accepts it as a carry-over.
4. PROGRESS and EXECUTION record the pilot outcome, and a release tag is cut.
5. Every `_p12_open_threads` entry has a disposition.

If Dan explicitly waives ITV for P12, the best attainable verdict is still QUALIFIED PASS, because B01 by definition needs two projects.
