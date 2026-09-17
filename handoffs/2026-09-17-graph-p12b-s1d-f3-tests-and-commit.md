# Handoff — P12B S1d: write `test_failure_ladder.py`, verify, commit F3, write S2 design

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s1d-f3-tests-and-commit.md`. No other prompt
follows and there is no chat context.

**Why this file exists:** the prior session
(`handoffs/2026-09-17-graph-p12b-s1c-f3-remainder.md`) finished `status.py` and `cli.py` (F3's
last two source files), added and verified the `explain_view`/`list_failures`/
`render_failures_text` tests in `tests/test_workflow_status.py`, then hit the context-governor
threshold (126K/120K) before writing `tests/graph_engineering/test_failure_ladder.py` — the
integration-level suite that drives a real graph run through `WorkflowRunner`/`worker.main` and
proves the failure ladder end to end. **Do not redo `status.py`, `cli.py`, or the
`test_workflow_status.py` additions — they are done, verified, and just need to ride along in
F3's single commit.**

Working directory: `/home/dan/projects/maestro`. Branch `feat/graph-engineering-foundation`
(**no upstream**, so `git pull` fails; that is expected).

---

## Where things stand

`git status --porcelain` shows **six modified, uncommitted files** — all of F3 except the new
test file, intentionally left uncommitted (spec: one commit per feature, and the new test file is
still missing):

- `maestro/control/store.py` — `attempts` DDL gained `failure_kind TEXT, failure_class TEXT,
  failure_json TEXT`; `_ADDED_COLUMNS` gained the matching three tuples.
- `maestro/control/lifecycle.py` — `Lifecycle.record_failure(attempt_id, *, run_id, record,
  at=None)` writes the three columns via `_set_attempt_fields`, whose `allowed` set includes them.
- `maestro/workflows/runner.py` — `_commit_result`, right after
  `self.routing.on_attempt_finished(...)`, classifies a `failed` phase via
  `wf_failures.classify_node_result(result, handler_ref=node.handler_ref)` (falling back to a raw
  `unknown` `FailureRecord` on `ValueError`) and writes it with `self.lifecycle.record_failure(...)`
  — but **only when `self.routing is not None`**. Don't relax that guard.
- `maestro/status.py` — `_attempt_failure(attempt)` (right before `explain_view`, mirrors
  `_attempt_routing`'s shape); `explain_view`'s per-attempt dict now carries a fifth key,
  `"failure": _attempt_failure(a)`; `render_explain_text` prints
  `f"  │     failure {failure['kind']} ({failure['class']}): {failure['evidence']}"` right after
  the routing block. New `FAILURE_LIST_FIELDS`, `list_failures(task_id=None, *, kind=None,
  repo=None)`, `render_failures_text(rows)` — placed right after `render_runs_text`. All verified
  this session against the real file structure (line numbers in the prior handoff were spot-on).
- `maestro/cli.py` — `cmd_workflow`'s `task_id` param is now `Optional[str]`, gained `kind:
  Optional[str] = None`; a new `verb == "failures"` branch calls `status.list_failures(task_id,
  kind=kind, repo=repo_root)`; the `verb != "show"` guard message now says "`show`, `runs` and
  `failures`". Parser: `p_workflow`'s `verb` choices gained `"failures"`, `task_id` is now
  `nargs="?"`, and a new `--kind` argument was added. `main()`'s dispatch: a
  `parser.error(f"workflow {args.verb} requires task_id")` guard for `verb in ("show", "runs")`
  with `task_id is None`, and `kind=args.kind` is threaded through to `cmd_workflow`.
- `tests/test_workflow_status.py` — imports `FailureRecord` from `maestro.workflows.failures`.
  Four new tests added right after `test_an_attempt_with_no_routing_decision_claims_no_binding`
  (before the `# ── the HTML export` marker): `test_explain_shows_the_classified_failure_
  recorded_on_an_attempt`, `test_an_attempt_with_no_recorded_failure_has_none`,
  `test_list_failures_filters_by_task_and_kind`, `test_render_failures_text_lists_one_line_
  per_failure_and_redacts`. **Verified this session:** `.venv/bin/python -m pytest -q
  tests/test_workflow_status.py --junit-xml=.scratch/p12b/f3_status_tests.xml` → junit root
  `testsuite`: `tests=29 failures=0 errors=0 skipped=0`.
- Also verified this session (before the `status.py`/`cli.py` edits, confirming no regressions
  from those changes alone): `.venv/bin/python -m pytest -q tests/graph_engineering/
  test_dispatch_wiring.py tests/workflows tests/test_workflow_status.py --junit-xml=
  .scratch/p12b/f3_status_cli.xml` → `tests=323 failures=0 errors=0 skipped=0`.
- `maestro.cli`, `maestro.status` both import cleanly (`.venv/bin/python -c "import maestro.cli,
  maestro.status"` → OK).
- STATE (`docs/graph-engineering/STATE.yaml`, git-ignored): unchanged, `current_phase: P12B`,
  `status: in_progress`. Leave as-is until F3 is committed and verified, then update
  `in_progress_details` (`what_was_done`/`whats_left`) with targeted replacements.
- The S1 design contract (`handoffs/2026-09-17-graph-p12b-s1-design.md`) is still the binding
  spec for F3's shape. The prior handoff
  (`handoffs/2026-09-17-graph-p12b-s1c-f3-remainder.md`) has the full `list_failures`/
  `render_failures_text` source verbatim if you need to cross-check against what actually landed.

## Invariants (unchanged, binding)

- Write the failure columns only when `routing is not None`. Already enforced in `runner.py`.
- One dispatch choke point. No Context Gate — do not build it. Don't touch `P12A_*` spec,
  `AGENTS.md` or `GEMINI.md`. Scratch files go in `.scratch/`.
- A PreToolUse hook rejects Bash text containing the privileged word (s-u-d-o). Never run it.
- pytest's summary line is suppressed: read the counts off the junit root `testsuite` element.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (Sonnet 5,
  not Opus). **One commit for all of F3** — `git add` all six modified files plus the new test
  file, commit once when everything is green.
- Edit STATE with targeted replacements only.

---

## What's left to build

### `tests/graph_engineering/test_failure_ladder.py` (new file)

This is the only source/test surface not yet written. Read `tests/graph_engineering/
test_dispatch_wiring.py` and `tests/graph_engineering/conftest.py` in full first — both were read
in the prior session, key facts below, but the files are ~1400 and ~700 lines respectively and
worth having open directly rather than trusting a summary for the parts not quoted here.

**Harness facts confirmed in the prior session (still true, re-verify quickly):**

- `_graph_scenario(lane, **overrides)` (`test_dispatch_wiring.py:119`) defaults `routing=None`,
  which `LaneScenario` (`conftest.py:169`, `routing` field at line 200) resolves to
  `fixture_routing()` (`conftest.py:334`) — an inert catalog with exactly one entry,
  `fixture_cli_entry()` (`conftest.py:313`): `backend_id="fixture_cli"`,
  `runtime_kind="agent_cli"`, `available_models=("fixture-1",)`. The default graph scenario
  already binds the implementer node to an `agent_cli` backend named `fixture_cli` — confirmed via
  `test_the_worker_argv_carries_the_dispatch_plan_of_the_resolved_binding`
  (`test_dispatch_wiring.py:339`).
- **`fixture_cli` has no registered driver** (`registry.driver_name_for_backend_id("fixture_cli")`
  raises `UnknownBackend` — proven by `test_an_unknown_backend_id_has_no_driver`, line 789-791).
  To run `wf_worker.main(...)` for the implementer node, monkeypatch
  `maestro.backends.registry.driver_name_for_backend_id` and `.get_backend` — `worker.py`'s
  `_run_agent_cli` (line 135-155) imports `registry` locally inside the function, so
  `monkeypatch.setattr(registry, "get_backend", ...)` done anywhere before the call is picked up.
  Sketch (unchanged from prior handoff, still accurate against the current `worker.py`):
  ```python
  class _FakeCliDriver:
      def __init__(self, completion, verdict=None, parse_exit_raises=None):
          self._completion = completion
          self._verdict = verdict
          self._raises = parse_exit_raises

      def complete(self, spec):
          return self._completion

      def parse_exit(self, rc, tail):
          if self._raises is not None:
              raise self._raises
          return self._verdict

  def _patch_fixture_cli_driver(monkeypatch, driver) -> None:
      from maestro.backends import registry
      monkeypatch.setattr(registry, "driver_name_for_backend_id", lambda backend_id: "fixture_cli")
      monkeypatch.setattr(registry, "get_backend", lambda name, *a, **kw: driver)
  ```
  `Completion(text=..., returncode=..., timed_out=...)` and `ExitVerdict(kind=..., reset_at=...,
  evidence=...)` are both in `maestro.backends.base`.
- **How `worker.py` actually gets from a fake driver to a classified failure** (read this session,
  more precise than the prior handoff's sketch): `worker.run()` (line 195) calls `_run_agent_cli`
  (line 135) to get `(driver, completion)`, captures worktree state before/after, then calls
  `_classify_cli(driver, completion, log_path, before, after)` (line 178-192):
  - `completion.timed_out` → `FailureRecord(kind="timeout", ...)`.
  - else reads the tail of `impl.log` (won't exist in a fake-driver test unless you create it —
    `log_path.exists()` guards that, so an absent log file is fine and `tail=""`) and calls
    `classify_exit(driver.parse_exit(completion.returncode, tail))` — this is where
    `_FakeCliDriver.parse_exit`'s scripted `ExitVerdict` (or raised exception) feeds in.
  - if `classify_exit` returns `None` (verdict was `"ok"`) and `completion.text` is empty and
    `before == after` (unchanged worktree) → `FailureRecord(kind="empty_output", ...)`.
  - otherwise `None` (no failure).
  - If `_classify_cli` returns a record, `worker.run()` (line 253-260) writes a `NodeResult(
    status="failed", outputs={"failure": failure.to_dict()}, ...)` **without ever calling
    `handlers.resolve(node.handler_ref)(context)`** — i.e. adoption is skipped entirely on a
    classified failure. This is the mechanism scenario 1-5's "adoption spy stays empty" assertion
    rests on; when `_classify_cli` returns `None`, `worker.run()` falls through to
    `result = handlers.resolve(node.handler_ref)(context)` (line 261) — the mechanism scenario 6
    ("adoption called once") rests on.
- **Spying on adoption:** `worker.py` imports `from maestro.workflows import handlers` at module
  level and calls `handlers.resolve(node.handler_ref)(context)`.
  `monkeypatch.setattr(handlers, "resolve", ...)` wrapping the real one, appending to a list, lets
  a test assert the list stayed empty for a classified-failure run and got exactly one call for a
  clean run.
- **Driving the implementer's worker in-process from `on_poll`**, adapted from
  `_run_the_gate_worker` (`test_dispatch_wiring.py:1256-1277`, which does the same for the `gate`
  node) for `node_id == "implementer"`:
  ```python
  def _run_the_implementer_worker(run) -> None:
      if _node_phase("implementer") in ("succeeded", "failed"):
          run.halt()
          return
      store = maestro.state.control_store()
      [row] = store.conn.execute(
          "SELECT attempt_id, run_id, node_id FROM attempts "
          "WHERE node_id = 'implementer' AND status IN ('claimed', 'running')"
      ).fetchall()
      inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
      if inbox.result() is None:
          wf_worker.main(_worker_argv(_window_for(run, row["attempt_id"])))
  ```
  `_window_for` (`test_dispatch_wiring.py:1251`), `_windows` (line 299), `_worker_argv` (line 303),
  `_node_phase` (line 315), `_attempts` (line 309) are all defined in `test_dispatch_wiring.py` —
  either import them via a relative import from that module (`from .test_dispatch_wiring import
  ...`), or copy the ~5-line bodies into the new file. Copying is more in keeping with this
  suite's style, since `conftest.py` is the only shared module and these helpers live in the test
  file itself — but a relative import is less duplication and this codebase doesn't have a strong
  precedent either way in this directory, so either is fine; pick one and be consistent.
- Scenarios to cover (design contract's F2/F3 acceptance list):
  1. fake driver returns `ExitVerdict(kind="quota_exhausted", reset_at="...")` with evidence text
     matching `_RATE_LIMIT_RE` (e.g. `"429 rate limit exceeded"`) → `classify_exit` maps this to
     `kind="rate_limited"` (see `failures.py:166-167`: `quota_exhausted` + rate-limit-shaped
     evidence becomes `rate_limited`); evidence *not* matching the regex stays
     `quota_exhausted`. Write one test for each branch, or one parametrized test covering both.
     `_finish` writes a `failed` result whose `outputs["failure"]["kind"]` is the expected one;
     adoption spy stays empty; **after driving `ingest_results()` (or a poll cycle), assert the
     attempt row in `attempts` has `failure_kind`/`failure_class`/`failure_json` populated** — this
     is F3's actual new behavior, not just F2's (F2 only proved the worker's own output; F3 proves
     the runner reads it back onto the attempt row via `Lifecycle.record_failure`).
  2. `ExitVerdict(kind="crashed")` → `_EXIT_KINDS["crashed"]` is `"worker_crashed"`
     (`failures.py:152`).
  3. An unrecognised `ExitVerdict.kind` (e.g. `"weird"`) → `unknown` (via `classify_exit`'s
     `_EXIT_KINDS.get(verdict_kind)` returning `None`, `failures.py:163-165`).
  4. `Completion(text="", returncode=0, timed_out=True)` → `timeout`, regardless of what
     `parse_exit` would say (timeout is checked first in `_classify_cli`, before `parse_exit` is
     even called — you can pass `parse_exit_raises=AssertionError("should not be called")` to
     `_FakeCliDriver` to prove this ordering, or simply not care what `_verdict` is set to).
  5. Empty completion (`Completion(text="", returncode=0, timed_out=False)`) with
     `ExitVerdict(kind="ok")` in an **unchanged git worktree** → `empty_output`. Needs a real git
     worktree under the lane's repo with a committed initial state and no diff across the call.
     Grep `git init` / `_create_control_db` in `conftest.py` and `test_dispatch_wiring.py` first
     to see whether any existing lane scenario already sets up a worktree this way (the lane
     sandbox's `repo` from `lane_sandbox` fixture is a plain temp dir, not a git repo, by default
     — check `context.worktree`/`context.workspace` resolution in `worker.py`'s
     `HandlerContext` construction (line 220-229) and how `_worktree_state` (line 158-176) reads
     `cwd`: if `cwd` is `None` (no worktree, no workspace passed), `_worktree_state` returns
     `None` for both `before` and `after`, and `_classify_cli`'s `empty_output` branch requires
     `before is not None and after is not None and before == after` — so **if the test's node has
     no worktree, `empty_output` cannot fire via that path**, and you need to check whether the
     implementer node in this harness's default compiled workflow gets a worktree/workspace by
     default. If it doesn't, either (a) construct a minimal real git repo at the resolved cwd path
     before running the worker, or (b) accept that this scenario needs `context.workspace` to be a
     directory that exists — `_worktree_state` doesn't require it to be a git repo *per se*, it
     requires `git rev-parse HEAD` and `git status --porcelain` to both succeed, so a `git init` +
     one commit in the workspace dir before invoking `wf_worker.main` is enough, no worktree
     machinery needed.
  6. Non-empty completion, `ExitVerdict(kind="ok")` → no failure; handler adoption spy gets called
     once (the control scenario proving classification doesn't fire on a clean run); after
     `ingest_results()`, the attempt row's three failure columns are `NULL`.
  7. F3 read-path: after the failing run from scenario 1 (or a fresh one) settles, assert
     `status.explain_view(status.workflow_view(...))` shows the failure under the implementer's
     attempt, and `maestro workflow failures --json` (call `cli.cmd_workflow(repo, "failures",
     None, as_json=True, repo=...)` directly, or go through `cli.main([...])` with `capsys`) lists
     it; `--kind unknown` (via `status.list_failures(None, kind="unknown", repo=...)`) lists only
     the unknown-kind one from scenario 3.
  8. Routing-None ingest writes no failure columns — build a `WorkflowRunner` directly with no
     `routing=` kwarg, the way `test_an_unrouted_verification_node_gets_no_selection`
     (`test_dispatch_wiring.py:1359-1384`) does (see that test's full body for the
     `wf_runner.start_run(store, revision, repo=repo, dispatcher=..., artifacts=...)` shape —
     no `routing=` kwarg passed at all), drive it to a `failed` attempt (claim the node, then
     write a `NodeResult(status="failed", ...)` result + receipt into its `AttemptInbox` directly,
     the way `_run_the_gate_worker`/worker-driving tests do, then call `graph_runner.
     ingest_results()`), then assert the attempt row's three failure columns are still `NULL`
     (query `attempts` directly, same shape as `_attempts()` in `test_dispatch_wiring.py:309` but
     selecting the failure columns too).

Run just this new file first, in isolation, before folding it into the full gate command — it's
new and most likely to reveal a wrong assumption about the harness:
```bash
.venv/bin/python -m pytest -q tests/graph_engineering/test_failure_ladder.py --junit-xml=.scratch/p12b/f3_ladder.xml -x
```

---

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # must exit 0; run with a long timeout or in the background — the full suite exceeds 120s
```

Report the gate and full-suite junit counts (read off the junit root `testsuite` element — the
summary line is suppressed by this repo's pytest config).

Then:
1. `git add` all six already-modified files plus the new `tests/graph_engineering/
   test_failure_ladder.py`, and commit once — message summarizing F3 (routing-null-guarded
   failure classification written onto attempts, `status.py`/`cli.py` operator surfaces, the
   failure-ladder integration suite), ending with
   `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
2. Update `docs/graph-engineering/STATE.yaml`'s `in_progress_details` (`what_was_done`/
   `whats_left`) with targeted replacements — F0-F3 done, S2 (F4, F5) next.
3. Write `handoffs/2026-09-17-graph-p12b-s2-design.md` — the design contract for S2 (F4, F5),
   per `handoffs/2026-09-17-graph-p12b-implementation.md`'s suggested split. Read that
   implementation handoff first for what F4/F5 actually are before drafting the contract; this
   session's job is the contract, not the implementation.
4. Report: the F3 commit hash, the gate/full-suite junit counts, and the path of the S2 design
   contract.

For Sonnet 5, prepare a handoff at 120K tokens and start fresh by 150K (this session's own
context-governor threshold). Check it right after `test_failure_ladder.py` passes in isolation
(before folding into the gate command), again after the gate command is green (before the full
suite), and again after the commit (before drafting the S2 design contract) — this surface has
already cost two sessions their full budget on "finish everything then check"; check early and
often. If a handoff is needed, it must be a self-contained file in `handoffs/`.
