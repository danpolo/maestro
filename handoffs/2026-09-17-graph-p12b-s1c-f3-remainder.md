# Handoff — P12B S1c: finish F3 (status.py + cli.py), write F2/F3 tests, commit F3

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s1c-f3-remainder.md`. No other prompt follows and
there is no chat context.

**Why this file exists:** the prior session (`handoffs/2026-09-17-graph-p12b-s1b-continued.md`)
wrote and committed F2, then wrote F3's `store.py`/`lifecycle.py`/`runner.py` changes and verified
them against the existing suite, then hit the context-governor threshold (121K/120K) before
touching `status.py`, `cli.py`, or any new tests. **F2 is committed** (`b469f71`). **F3's first
three files are done but uncommitted** — do not redo them, just pick up at `status.py`.

Working directory: `/home/dan/projects/maestro`. Branch `feat/graph-engineering-foundation`
(**no upstream**, so `git pull` fails; that is expected).

---

## Where things stand

- `git log --oneline -3`: `b469f71 feat(graph): P12B F2 — classify agent-CLI worker failures`,
  then `761bacc` (F1), `2b572de` (F0). F2 is fully done, tested, and committed.
- `git status --porcelain` shows **three modified, uncommitted files** — this is F3's first half,
  intentionally left uncommitted because F3 is not finished yet (the spec asks for one commit per
  feature, and `status.py`/`cli.py`/tests are still missing):
  - `maestro/control/store.py` — `attempts` DDL gained `failure_kind TEXT, failure_class TEXT,
    failure_json TEXT`; `_ADDED_COLUMNS` gained the matching three tuples.
  - `maestro/control/lifecycle.py` — `import json` added at module level; new
    `Lifecycle.record_failure(attempt_id, *, run_id, record, at=None)` (right after
    `record_routing_settle`) writes the three columns via `_set_attempt_fields`; that method's
    `allowed` set now includes `"failure_kind", "failure_class", "failure_json"`.
  - `maestro/workflows/runner.py` — top import block gained
    `from maestro.workflows import failures as wf_failures`. `_commit_result`, immediately after
    the existing `if self.routing is not None: self.routing.on_attempt_finished(...)` line (inside
    the `if phase is not None:` block), gained:
    ```python
    if self.routing is not None and phase == "failed":
        try:
            record = wf_failures.classify_node_result(result, handler_ref=node.handler_ref)
        except ValueError:
            record = wf_failures.FailureRecord(
                kind="unknown",
                evidence=str((result.outputs or {}).get("failure")),
            )
        if record is not None:
            self.lifecycle.record_failure(
                attempt_id, run_id=self.run_id, record=record, at=at
            )
    ```
  - Verified this session: `.venv/bin/python -m pytest -q tests/graph_engineering/
    test_dispatch_wiring.py tests/workflows --junit-xml=.scratch/p12b/f3_pre.xml` — all green
    (no failures) after these three files changed. `import maestro.workflows.runner,
    maestro.control.lifecycle, maestro.control.store` also verified clean.
  - Full-suite run after the F2 commit (`.scratch/p12b/f2_full.xml`): junit root `testsuite`
    reports `tests=4629 failures=0 errors=0 skipped=1` — identical to the S1a baseline, so F2
    introduced zero regressions.
- STATE (`docs/graph-engineering/STATE.yaml`, git-ignored): unchanged, `current_phase: P12B`,
  `status: in_progress`. Leave as-is until F3 is committed and verified, then update
  `in_progress_details` (`what_was_done`/`whats_left`) with targeted replacements.
- The S1 design contract (`handoffs/2026-09-17-graph-p12b-s1-design.md`) is still the binding spec
  for F3's shape. This file's F3 section is a condensed, line-number-accurate restatement of it
  for the two remaining files — if anything here seems to contradict it, the design contract wins.

## Invariants (unchanged, binding)

- Write the failure columns only when `routing is not None`. Already enforced in `runner.py`'s
  new block above — don't relax it in `status.py`/`cli.py`.
- One dispatch choke point. No Context Gate — do not build it. Don't touch `P12A_*` spec,
  `AGENTS.md` or `GEMINI.md`. Scratch files go in `.scratch/`.
- A PreToolUse hook rejects Bash text containing the privileged word (s-u-d-o). Never run it.
- pytest's summary line is suppressed: read the counts off the junit root `testsuite` element.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (Sonnet 5,
  not Opus). **One commit for all of F3** (store.py + lifecycle.py + runner.py + status.py +
  cli.py + the new test file + any test_workflow_status.py additions) — those three files are
  already staged-worthy in the working tree; just `git add` everything F3 touches and commit once
  when it's all green.
- Edit STATE with targeted replacements only.

---

## What's left to build

### F3, file 4: `maestro/status.py`

Exact current line numbers (read this session, so these are accurate right now):

- `_row_to_dict` at line 346, `_open_store` at 350, `_task_runs` at 363, `list_runs` at 396,
  `render_runs_text` at 411 (ends 419) — copy this file's `_open_store`/close pattern for the new
  `list_failures`.
- `workflow_view`'s attempt loop already does `_row_to_dict(row)` on `SELECT * FROM attempts`, so
  the three new columns already flow through as raw `failure_kind`/`failure_class`/`failure_json`
  on each attempt dict returned by `workflow_view` — confirmed this session, **no change needed
  there**.
- `_attempt_routing` at line 583 (ends 622) — model the new `_attempt_failure` helper on this
  one's shape and placement (put it right after `_attempt_routing`, before `explain_view`).
- `explain_view` at line 629. The per-attempt dict is built at lines 695-699:
  ```python
                "attempts": [
                    {"attempt_id": a["attempt_id"], "status": a["status"],
                     "started_at": a["started_at"], "outcome_ref": a["outcome_ref"],
                     "routing": _attempt_routing(a)}
                    for a in node["attempts"]
                ],
  ```
  Add `"failure": _attempt_failure(a)` as a fifth key in that dict.
  ```python
  def _attempt_failure(attempt: Mapping) -> dict | None:
      if not attempt.get("failure_kind"):
          return None
      failure = json.loads(attempt["failure_json"]) if attempt.get("failure_json") else {}
      return {
          "kind": attempt.get("failure_kind"),
          "class": attempt.get("failure_class"),
          "evidence": redact_secrets(failure.get("evidence", "")),
          "retry_after_sec": failure.get("retry_after_sec"),
          "reset_at": failure.get("reset_at"),
      }
  ```
  (Parses `failure_json` once, unlike the design contract's sketch — cleaner, same result.)
- `render_explain_text` at line 803. The attempt line is at line 822:
  `lines.append(f"  │   attempt {attempt['attempt_id']} [{attempt['status']}]")`. The routing
  block right after it (lines 823-829) is the model to copy — add, right after that routing
  `if`/`elif`:
  ```python
                failure = attempt.get("failure")
                if failure:
                    lines.append(
                        f"  │     failure {failure['kind']} ({failure['class']}): "
                        f"{failure['evidence']}"
                    )
  ```
- New `list_failures` / `render_failures_text`, placed near `list_runs`/`render_runs_text`
  (after line 419). Confirmed this session: `attempts` has no `task_id` column of its own (so
  `a.*` and `r.task_id` cannot collide in a `sqlite3.Row`), and `task_runs.task_id` /
  `task_runs.run_id` both exist (used already by `_task_runs`). Use the shape from the S1b-continued
  handoff verbatim (it was verified against this file's real structure this session):
  ```python
  FAILURE_LIST_FIELDS = (
      "task_id", "run_id", "node_id", "attempt_id", "backend_id", "model_id", "binding_id",
      "failure_kind", "failure_class", "evidence", "retry_after_sec", "reset_at", "started_at",
  )

  def list_failures(task_id: str | None = None, *, kind: str | None = None,
                     repo: Path | None = None) -> list[dict]:
      from maestro.workflows.failures import FAILURE_KINDS

      if kind is not None and kind not in FAILURE_KINDS:
          raise WorkflowViewError(f"unknown failure kind {kind!r}")
      store = _open_store(repo)
      try:
          query = ("SELECT a.*, r.task_id FROM attempts a"
                   " JOIN task_runs r ON a.run_id = r.run_id"
                   " WHERE a.failure_kind IS NOT NULL")
          params: list = []
          if task_id is not None:
              query += " AND r.task_id = ?"
              params.append(task_id)
          if kind is not None:
              query += " AND a.failure_kind = ?"
              params.append(kind)
          query += " ORDER BY a.started_at, a.rowid"
          rows = []
          for row in store.conn.execute(query, params):
              d = _row_to_dict(row)
              failure = json.loads(d["failure_json"]) if d.get("failure_json") else {}
              rows.append({
                  "task_id": d["task_id"], "run_id": d["run_id"], "node_id": d["node_id"],
                  "attempt_id": d["attempt_id"], "backend_id": d["backend_id"],
                  "model_id": d["model_id"], "binding_id": d["binding_id"],
                  "failure_kind": d["failure_kind"], "failure_class": d["failure_class"],
                  "evidence": redact_secrets(failure.get("evidence", "")),
                  "retry_after_sec": failure.get("retry_after_sec"),
                  "reset_at": failure.get("reset_at"), "started_at": d["started_at"],
              })
          return rows
      finally:
          store.close()

  def render_failures_text(rows: list[dict]) -> str:
      lines = [
          f"{row['task_id']:<14} {row['node_id']:<20} {row['failure_kind']:<20} "
          f"({row['failure_class']}) {row['backend_id'] or '—'}/{row['model_id'] or '—'} "
          f"— {row['evidence'][:80]}"
          for row in rows
      ]
      return redact_secrets("\n".join(lines) + "\n")
  ```
  `redact_secrets` and `WorkflowViewError` are already defined earlier in this file — no new
  import needed for those. `attempts.rowid` — sqlite gives every table an implicit `rowid` unless
  declared `WITHOUT ROWID`; `attempts` isn't, so `a.rowid` in `ORDER BY` is safe (same trick
  `_task_runs` already uses on `task_runs`).

### F3, file 5: `maestro/cli.py`

Exact current line numbers (read this session):

- `cmd_workflow` signature at line 1444:
  ```python
  def cmd_workflow(repo_root: Path, verb: str, task_id: str, *, as_html: bool = False,
                   as_json: bool = False, out: Optional[str] = None,
                   run_id: Optional[str] = None) -> int:
  ```
  Change `task_id: str` to `task_id: Optional[str]`, add `kind: Optional[str] = None` to the
  keyword-only args.
- Lines 1460-1474 are the `verb == "runs"` branch and the `verb != "show"` guard:
  ```python
      if verb == "runs":
          ...
          return 0
      if verb != "show":
          print(f"[workflow] unknown verb {verb!r} — the verbs are `show` and `runs`")
          return 2
  ```
  Change the guard message to mention `failures` too, and insert a `failures` branch before it
  (after the `runs` branch, same shape):
  ```python
      if verb == "failures":
          try:
              rows = status.list_failures(task_id, kind=kind, repo=repo_root)
          except status.WorkflowViewError as exc:
              print(f"[workflow] {exc}")
              return 1
          if as_json:
              print(json.dumps(rows, indent=2, default=str))
          else:
              print(status.render_failures_text(rows), end="")
          return 0
      if verb != "show":
          print(f"[workflow] unknown verb {verb!r} — the verbs are `show`, `runs` and `failures`")
          return 2
  ```
  `status` is already imported locally at the top of `cmd_workflow` (line 1458: `from maestro
  import status`) — no new import needed.
- Parser: `p_workflow.add_argument("verb", choices=["show", "runs"])` at line 1926 and
  `p_workflow.add_argument("task_id")` at line 1927. Change to
  `choices=["show", "runs", "failures"]` and `p_workflow.add_argument("task_id", nargs="?")`.
  Add `p_workflow.add_argument("--kind", default=None)` right after.
- `main()`'s dispatch at lines 1982-1987:
  ```python
      if args.command == "workflow":
          if args.verb == "show" and args.as_html and args.as_json:
              parser.error("--html and --json are two different renderings; pick one")
          return cmd_workflow(_resolve_repo(args.repo), args.verb, args.task_id,
                              as_html=args.as_html, as_json=args.as_json, out=args.out,
                              run_id=args.run_id)
  ```
  Add the required-`task_id` check for `show`/`runs` before the existing `--html`/`--json` check,
  and pass `kind` through:
  ```python
      if args.command == "workflow":
          if args.verb in ("show", "runs") and args.task_id is None:
              parser.error(f"workflow {args.verb} requires task_id")
          if args.verb == "show" and args.as_html and args.as_json:
              parser.error("--html and --json are two different renderings; pick one")
          return cmd_workflow(_resolve_repo(args.repo), args.verb, args.task_id,
                              as_html=args.as_html, as_json=args.as_json, out=args.out,
                              run_id=args.run_id, kind=args.kind)
  ```
- `cmd_workflow(` has exactly one call site (the one above) — grep to confirm before changing the
  signature (`grep -rn "cmd_workflow(" maestro/ tests/` — the S1b-continued handoff already
  checked this, worth a 5-second re-check since it's cheap).

---

## Tests

Per the design contract (§Tests). Two test surfaces, both unwritten so far:

### `tests/graph_engineering/test_failure_ladder.py` (new file)

Read `tests/graph_engineering/test_dispatch_wiring.py` (imports at lines 27-59; `.conftest`
exposes `FixtureQueries`, `LaneScenario`, `fixture_cli_entry`, `fixture_routing`) and
`tests/graph_engineering/conftest.py` for the harness shapes — both read this session, key facts:

- `_graph_scenario(lane, **overrides)` (`test_dispatch_wiring.py:119`) defaults `routing=None`,
  which `LaneScenario` (`conftest.py:169`) resolves to `fixture_routing()` — an inert catalog with
  exactly one entry, `fixture_cli_entry()` (`conftest.py:313`): `backend_id="fixture_cli"`,
  `runtime_kind="agent_cli"`, `available_models=("fixture-1",)`. This means **the default graph
  scenario already binds the implementer node to an `agent_cli` backend named `fixture_cli`** —
  confirmed via `test_the_worker_argv_carries_the_dispatch_plan_of_the_resolved_binding`
  (`test_dispatch_wiring.py:339`), which asserts the exact argv shape without overriding routing.
- **`fixture_cli` has no registered driver** (`registry.driver_name_for_backend_id("fixture_cli")`
  raises `UnknownBackend` — proven by `test_an_unknown_backend_id_has_no_driver`, line 789-791).
  So to actually run `wf_worker.main(...)` for the implementer node, monkeypatch
  `maestro.backends.registry.driver_name_for_backend_id` and `.get_backend` directly — `worker.py`
  imports `registry` locally inside `_run_agent_cli` (`from maestro.backends import registry`),
  so a `monkeypatch.setattr(registry, "get_backend", ...)` done anywhere before the call is picked
  up (module-attribute lookup happens at call time, not import time). Sketch:
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
  evidence=...)` are both in `maestro.backends.base` (read this session, fields confirmed:
  `Completion.text/returncode/timed_out`, `ExitVerdict.kind/reset_at/evidence`).
- Pattern for driving the implementer's worker in-process from `on_poll`, copied from
  `_run_the_gate_worker` (`test_dispatch_wiring.py:1256-1277`) which does the same for the `gate`
  node — adapt it for `node_id == "implementer"` instead:
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
  (`_window_for`, `_windows`, `_worker_argv`, `_node_phase`, `_attempts` are all defined in
  `test_dispatch_wiring.py` — either import them via a relative import from that module, or copy
  the ~5-line bodies into the new file; copying is more in keeping with this suite's style, since
  `conftest.py` is the only shared module and these helpers live in the test file itself.)
- **Spying on adoption not being called**: `worker.py` imports `from maestro.workflows import
  handlers` at module level and calls `handlers.resolve(node.handler_ref)(context)`.
  `monkeypatch.setattr(handlers, "resolve", ...)` wrapping the real one, appending to a list, lets
  a test assert the list stayed empty for a classified-failure run and got exactly one call for a
  clean run.
- Scenarios to cover (design contract's F2/F3 acceptance list — restated, unchanged from the
  S1b-continued handoff):
  1. fake driver returns `ExitVerdict(kind="quota_exhausted", reset_at="...")` → `_finish` writes
     a `failed` result whose `outputs["failure"]["kind"]` is `rate_limited` or `quota_exhausted`
     depending on whether the evidence matches `_RATE_LIMIT_RE`; adoption spy stays empty.
  2. `ExitVerdict(kind="crashed")` → `worker_crashed`.
  3. An unrecognised `ExitVerdict.kind` (e.g. `"weird"`) → `unknown` (via `classify_exit`'s
     `_EXIT_KINDS.get(verdict_kind)` returning `None`).
  4. `Completion(text="", returncode=0, timed_out=True)` → `timeout`.
  5. Empty completion (`Completion(text="", returncode=0, timed_out=False)`) with
     `ExitVerdict(kind="ok")` in an **unchanged git worktree** → `empty_output`. Needs a real git
     worktree under the lane's repo with a committed initial state and no diff across the call —
     check how other lanes get a git-initialized repo (grep `git init` / `_create_control_db` in
     `conftest.py`; the lane sandbox may already be a git repo — confirm before assuming you need
     to init one).
  6. Non-empty completion, `ExitVerdict(kind="ok")` → no failure, handler adoption spy gets called
     once (the control scenario proving classification doesn't fire on a clean run).
  7. F3: after the next poll, the attempt row carries `failure_kind`/`failure_class`/
     `failure_json`; `status.explain_view(...)` and `maestro workflow failures --json` (via
     `cli.main` or `cmd_workflow` directly) show them; `--kind unknown` lists only the unknown one.
  8. Routing-None ingest writes no failure columns — build a `WorkflowRunner` directly with no
     `routing=` kwarg, the way `test_an_unrouted_verification_node_gets_no_selection`
     (`test_dispatch_wiring.py:1359-1384`) does, drive it to a `failed` attempt (e.g. write a
     `FAILED` sentinel / a `NodeResult(status="failed", ...)` result file and call
     `ingest_results()`), then assert the attempt row's three failure columns are still `NULL`.

### `tests/test_workflow_status.py`

Read this session (438 lines total). Copy the shape of
`test_explain_shows_the_durable_routing_decision_recorded_on_an_attempt` (line 268-292) — it
builds a `ControlStore` on the `project` fixture's `.orchestrator/control.sqlite3`, calls
`Lifecycle(store).record_routing_decision(...)` directly (no graph run needed), closes the store,
then reads back through `status.explain_view(status.workflow_view(...))`. Do the exact same thing
with `Lifecycle(store).record_failure("att_verify", run_id="run_1", record=FailureRecord(kind=...,
evidence=...))` (import `FailureRecord` from `maestro.workflows.failures`) and assert:
- `explain_view(...)["chains"][...]["links"][0]["attempts"][0]["failure"]` has the right
  `kind`/`class`/`evidence`.
- `render_explain_text(...)` contains `f"failure {kind} ({cls}): {evidence}"`.
- A sibling test mirroring `test_an_attempt_with_no_routing_decision_claims_no_binding`
  (line 295-301): an attempt with no recorded failure has `attempt["failure"] is None`.

Also add plain unit tests for `list_failures`/`render_failures_text` against a `ControlStore` +
`Lifecycle` built directly (same `project`/`ControlStore.open_controller` pattern), covering: the
`task_id` filter, the `--kind` filter, an unknown `--kind` raising `WorkflowViewError`, and that
`redact_secrets` scrubs a planted `FAKE_TOKEN`-shaped secret in the evidence (this file already
defines `FAKE_TOKEN` at line 33 for exactly this purpose — reuse it, don't invent a new one).

---

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # must exit 0; run with a long timeout or in the background — the full suite exceeds 120s
```

Report the gate and full-suite junit counts (read off the junit root `testsuite` element — the
summary line is suppressed by this repo's pytest config), the F3 commit hash, and the path of the
S2 handoff you write at the end (S2 = F4, F5; needs its own design contract per
`handoffs/2026-09-17-graph-p12b-implementation.md`'s suggested split).

For Sonnet 5, prepare a handoff at 120K tokens and start fresh by 150K (this session's own
context-governor threshold). Check it right after `status.py` is written (before `cli.py`), again
after `cli.py` (before tests), and again after the new test file is passing (before
`test_workflow_status.py` additions) — F3's remaining surface is wide enough that "finish
everything then check" is the mistake two sessions in a row have now made; check early and often
this time. The next handoff, if one is needed, must be a self-contained file in `handoffs/`. S2
needs its own design contract (`handoffs/…-p12b-s2-design.md`) before any code.
