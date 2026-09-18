# Handoff: Graph P12, session 2f. Two thread fixes, docs, the s3 pilot handoff, the report

Continue Phase P12 on `feat/graph-engineering-foundation`. The working tree is clean (HEAD `d4af85f`).
`docs/graph-engineering/STATE.yaml` and `handoffs/` are gitignored, so edits to them live on disk only (as before). This file is the whole
prompt.

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s2-worktree-and-e2e.md`: its §1 (Dan's decisions, do not re-ask), §4 "Docs" and
   "Context Gate seams" bullets, §5 (the s3 pilot handoff spec), §6 (coordination) and §7 (report back) still apply.
   Its §3 and the rest of §4 are DONE.
2. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (short).
3. `docs/graph-engineering/STATE.yaml`: `in_progress_details` and `_p12_open_threads`. Search for them. Do not read
   the whole file.
4. `artifacts/graph-engineering/p12-qualification.json`: read `summary`, `modes`, `invariants` (without `lanes`),
   `retention`, `experiments` and `context_gate_mapping` with a jq/python one-liner. Do not cat the whole file
   (114 KB, mostly raw telemetry).

## 1. Done in session 2e (commits)
- `5f61b9d` **bug: manual prep never ran under the graph loop.** `_graph_main` read only `parse_runnable_tasks()`,
  which excludes `dispatch: manual`, so a B14 prep task never opened a run even though the compiler has a `manual`
  template. The loop now reads `parse_runnable_tasks() + parse_prep_tasks()`, as the legacy loop does. Under the
  graph, `prep` runs, then `human_action` holds its claim with no outcome, like `park`, and the run stays
  `running` until the operator's receipt.
- `5f61b9d` `tests/graph_engineering/test_qualification.py`: the six catalog classes under the subscription fixture
  mode, plus one happy path each for free, hybrid and script. Each lane records raw attempt telemetry,
  `.orchestrator` bytes and the invariant checks on `item._qualification`. The suite asserts the same record the
  report prints.
- `d4af85f` `scripts/graph_qualification.py`: runs that module through `pytest.main` (the `graph_baseline.py`
  pattern) with `--basetemp=<repo>/lanes`, and refuses a target under `~/projects` or a non-empty directory it did
  not create (marker `.maestro-graph-qualification`) unless `--deployment` is given. Without `--fixture-backends` it
  exits 2. It writes `artifacts/graph-engineering/p12-qualification.json`.
- `d4af85f` B15: `config.RETENTION_DEFAULTS = {prune: False, warn_bytes: 1 GiB}` plus `RETENTION_BASIS`,
  `config.retention()` (strict; `prune: true` is refused because A03 keeps raw outcomes and no pruning path exists),
  a `retention:` block in `project.yaml.tmpl`, and `cli._check_retention` (an optional doctor warning when
  `.orchestrator`, task trees excluded, passes `warn_bytes`).

Verified numbers (for the §7 report):
- Full suite: `junit tests=4744 failures=0 errors=0 skipped=1`, rc 0.
- Script: rc 0. 10/10 lanes, 1 task per class for 6 classes, invariants 6/6 checked and passed (INV-02, 03, 04,
  05, 07, 10). The other 6 cannot be checked in a fixture run; each is named with its covering tests. Every accepted
  run has 2 acceptance_decisions. 17,180,686 bytes across 9 runs. The largest accepted run was 2.09 MB (control DB
  plus WAL 2.07 MB, evidence 14 KB). Retention default `{prune: false, warn_bytes: 1073741824}`.
- Integration bugs, one line each: `55dcd15` merge/accept got no specs; `55f6d5e` a parked run never settled;
  `08e3a02` free mode never committed; `afd677e` the script lane never committed; `2fc8faf` an interrupted merge
  hung; `87ba99c` the acceptance commit was not atomic; `5f61b9d` manual prep never ran under the graph loop.

## 2. Traps
- The repo's pytest `addopts = "-q"`, so `-q` on the command line makes it silent. Run without `-q`.
- The qualification script's stdout ends in a pytest line. Check `$?` directly, not through a pipe.
- `project.yaml`'s path is fixed at import (`config.PROJECT_YAML`). A check that takes `repo_root` must read
  `repo_root / "project.yaml"` itself (as `_check_retention` does).

## 3. In scope, in order
1. **Fix the two open threads first.** The docs then describe the final behaviour. Neither needs Dan: both are
   wiring gaps with the intended behaviour already fixed by the spec (memory: gaps are not decisions). TDD, one lane
   each in `tests/graph_engineering/test_end_to_end.py`, then close the thread in `_p12_open_threads`.
   (b) **The gate verifies the working tree, not a commit.** The verification handler must refuse a task tree with
   uncommitted changes (`git status --porcelain` non-empty) and fail with a clear detail, so the run parks at the
   gate instead of at merge's freeze. Its evidence is then bound to the commit it checked. Lane: an agent_cli
   fixture that writes the file without committing parks at `gate`, with no acceptance decision.
   (a) **The candidate winner is never merged.** `merge_handler` integrates `impl/<task>`, and nothing hands it the
   `candidate_select` winner's branch (`CandidateSpec` in `maestro/workflows/candidates.py`). Feed the winner's
   branch/tree into merge's inputs (and `worktree_for(merge)`), keeping INV-06: exactly one candidate lands and
   nothing is mixed. Lane: a widened task with two fixture candidates lands only the winner's commit. Candidates
   stay *off* by default (E1 unqualified). This only makes the path correct when enabled. Only if the fix turns
   out to need a D01-D08 design change should it be recorded instead, owned by the E1 work, and that is the one
   case to raise with Dan.
2. **Docs** (`docs/DESIGN.md`, `docs/EXECUTION.md`, `docs/PROGRESS.md`). Cover everything in the two docs threads
   in `_p12_open_threads`: the worktree_unavailable claim refusal, TaskTreeReleased, the brief header, a parked run
   settling failed while park stays claimed, the harness/script commit (`task_tree.commit_all`, author
   `maestro harness`), `hybrid.paid_roles` taking role ids, B14 prep tasks under the graph loop, the qualification
   script and report, and the retention block plus doctor check. From s2d, add: the merge postcondition probe with
   the `interrupted` failure kind (blocked class, spends no budget), and that the acceptance commit is one
   transaction. Also cover controller-error handling, the two thread fixes from step 1, and what stays unqualified
   (every mode except paid subscription). Close both docs threads when done.
3. **Write the s3 pilot handoff** per s2 §5 and §1: DuetFlow (9 open tasks in
   `/home/dan/projects/duetflow/docs/ROADMAP.md`, switch to `engineering.runner: graph`) and instagram-to-value
   (only after Dan reports his grill-me run is done; config guards in s2 §1). Paid subscription only. Pin criterion
   weights and source revisions before any run. Retain raw per-attempt outcomes. Re-measure retention (the
   `owner: P12-pilots` thread), then run `scripts/graph_qualification.py` again and read the `lanes` array.
4. Update STATE `in_progress_details`: P12 stays `in_progress`, and `whats_left` is the pilots. Report per s2 §7 using
   the numbers in §1 above (re-run the suite and the script if code changed). Close with `close-session`.

## 4. Out of scope
Real pilots. The Context Gate. The failure-handling-phase threads (uncertain inline nodes; the unimplemented
probes): record only, do not fix.

## 5. Coordination
No other session is known to be writing maestro. Edit STATE.yaml with targeted replacements only (thread text is
hard-wrapped, so match exact lines). Keep work on persistent storage and commit early. Budget: the context-governor
thresholds (handoff at 160K).
