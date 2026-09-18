# Handoff: Graph P12, session 2e. The qualification script, B15, docs, two threads, then the s3 handoff

Continue Phase P12 on `feat/graph-engineering-foundation`. The working tree is clean. `docs/graph-engineering/STATE.yaml` is
gitignored, so edits to it live on disk only (as before). This file is the whole prompt.

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s2-worktree-and-e2e.md`: its §1 (Dan's decisions, do not re-ask), §4 (the
   qualification-script spec, B15, docs, Context Gate seams), §5 (out of scope), §6 (coordination) and §7 (report back)
   all still apply. Its §3 and §4(a)-(c) are DONE.
2. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (short).
3. `docs/graph-engineering/STATE.yaml`: `in_progress_details` and `_p12_open_threads`. Search for them. Do not read
   the whole file.
4. `tests/graph_engineering/test_end_to_end.py`: skim the section headers (`grep -n "^# ──\|^def test_"`). The
   happy-path helpers (`_git_sandbox`, `_CommittingAgent`, `_drive_every_worker`, `_happy_scenario`, `_free_routing`,
   the hybrid routing, the script task) are how the qualification script can drive each mode.

## 1. Done in session 2d (commits)
The fault matrix is complete: the e2e file has 15 lanes, all green.
- `abd8078` writer crash mid-task (half commit, dirty file, dead PID). The retry restores to `start_sha` and only
  the retry lands. No bug.
- quota switch (`git log --grep="quota runs out"`). The first pool reports `quota_exhausted` and is paused; every
  later node lands on the second pool. No bug.
- `2fc8faf` **bug: interrupted integration.** Merge declared `reconciliation_handler: maestro.reconcile.merge_probe`,
  but no probe existed. A controller lost inside merge (after staging, before the fast-forward) left the node
  `running` forever. `runner._decide_dead_writers` now runs `_POSTCONDITION_PROBES[node.reconciliation_handler]`. If
  `impl/<task>` is absent from the target, the node is requeued via `Lifecycle.requeue_probed` with the new failure
  kind `interrupted` (blocked class, spends no budget: `pre_validate` subtracts `blocked_attempts` for non-agent
  nodes). If it is present or unknown, the node fails and the run settles failed: never replayed (A05).
  `fail_dead_candidate` was renamed `fail_dead_writer_node`. Two lanes cover it: a crash before the swap lands on
  the rerun, and a crash after the swap escalates.
- `87ba99c` **bug: transaction rollback.** `commit_acceptance` autocommitted the event, the decision row and the
  task status separately (the store uses `isolation_level=None`), so a failure part-way left an `accepted` decision
  on a task that was not accepted. It is now one `store.transaction()`, and `Lifecycle.set_task_status(conn=)`.
- New `_p12_open_threads` (owner failure-handling-phase, record only): an inline node whose handler raises is left
  `uncertain` and never settles the run; `worktree_probe`, `human_receipt_probe` and `async_job_probe` are declared
  but unimplemented.

The integration bugs so far, one line each (for the §7 report): `55dcd15` merge/accept got no specs; `55f6d5e` a
parked run never settled; `08e3a02` free mode never committed; `afd677e` the script lane never committed; `2fc8faf`
interrupted merge hung; `87ba99c` the acceptance commit was not atomic.

## 2. Traps (in addition to s2c/s2d's)
- The repo's pytest `addopts = "-q"`, so `-q` on the command line makes it silent. Run without `-q` to see results.
- `-k` matches test names. Check the name before trusting a "no tests ran" (exit 5).
- Merge and accept run **in the controller process** (`execute_inline`). In a lane, their inbox PID is the test's
  own PID, so a "dead controller" must `bind_pid(4_000_000)` on the merge inbox before the rerun.
- `run.polls` carries across `run.run()` calls. Reset it to 0 before a second run.
- The `attempts` table has no `billing_category`. `CatalogEntries` is a mapping (`.entries.values()`). There is no
  `python` on PATH.

## 3. In scope, in order
1. `scripts/graph_qualification.py` as specified in s2 §4. Count `acceptance_decisions` per run: there are two for
   each accepted run. The four fixture modes all reach accept. Label every mode except paid subscription
   *unqualified* (s2 §1). Include the fault-matrix outcome only as "exercised by tests/…/test_end_to_end.py" (do not
   re-run the faults in the script).
2. B15 retention: measure bytes during the fixture run, then set `maestro init`'s default from it
   (`grep -rn retention maestro/` first). Record a P12 thread saying the real-pilot measurement may revise it.
3. Docs (DESIGN, EXECUTION, PROGRESS), including the docs thread in `_p12_open_threads`. Add from s2d: the merge
   postcondition probe and the `interrupted` kind, and that the acceptance commit is atomic.
4. Decide two open threads. Fix each if it is small; otherwise record the owner and set `disposition: needs-dan`:
   (a) the candidate winner is never merged; (b) the gate verifies the working tree, not a commit.
5. Write the s3 pilot handoff (s2 §5). Report per s2 §7. Close with `close-session`.

## 4. Out of scope
Real pilots (DuetFlow, instagram-to-value). The Context Gate. The two new failure-handling-phase threads (uncertain
inline nodes; the unimplemented probes): record only, do not fix.

## 5. Verification to report
- `.venv/bin/python -m pytest -p no:randomly --junitxml=<scratch>/junit.xml`: tests/failures/errors (s2d's end
  state is in `in_progress_details`).
- `.venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends`:
  the exit code, tasks per class, invariants checked and passed, total artifact bytes, and the retention default.
- One line per integration bug with its commit (list in §1).

## 6. Coordination
No other session is known to be writing maestro. Edit STATE.yaml with targeted replacements only. Keep work on
persistent storage and commit early. Budget: the context-governor thresholds (handoff at 160K). s2d reached about
160K after 4 fault lanes and 2 fixes. Measure with `python3 ~/.claude/skills/close-session/scripts/context_usage.py`
after the script lands. If it runs long, hand off before the docs.
