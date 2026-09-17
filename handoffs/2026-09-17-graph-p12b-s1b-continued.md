# Handoff — P12B S1b (continued): F2 (agent-CLI classification) and F3 (durable record)

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s1b-continued.md`. No other prompt follows and
there is no chat context.

**Why this file exists:** the prior session (`handoffs/2026-09-17-graph-p12b-s1b.md`) spent its
whole budget on orientation — reading the design contract, spec, prior handoff, and every file
F2/F3 touch — and hit the context-handoff threshold (125K/120K) before writing a single line of
code. **No code has changed.** `git status` is clean, branch is still
`feat/graph-engineering-foundation`. This file inlines everything that session learned so you
don't have to re-read those files: exact line numbers, exact function signatures, exact patterns
to copy. Skip straight to "What to build."

Working directory: `/home/dan/projects/maestro`. Branch `feat/graph-engineering-foundation`
(**no upstream**, so `git pull` fails; that is expected).

---

## Where things stand

- **P12B S1a is done** (2026-09-17). Commits `2b572de` (F0) and `761bacc` (F1). Full suite then:
  junit tests=4629 failures=0 skipped=1, exit 0.
- **F2 and F3 are not started.** This session's job.
- STATE (`docs/graph-engineering/STATE.yaml`, git-ignored): `current_phase: P12B`,
  `status: in_progress`. Leave as-is until F2+F3 are both committed and verified, then update
  `in_progress_details` (`what_was_done`/`whats_left`) with targeted replacements.
- The original S1b handoff (`handoffs/2026-09-17-graph-p12b-s1b.md`) and the S1 design contract
  (`handoffs/2026-09-17-graph-p12b-s1-design.md`) are both still on disk and still correct — this
  file summarizes their binding content plus extra research, but if anything here seems to
  contradict them, the design contract wins (it's the binding spec F0–F3 were built against).

## Binding spec (verbatim from `handoffs/2026-09-17-graph-p12b-s1-design.md`, F2/F3 sections)

### F2 — `worker._run_agent_cli`

- Records the worktree state before the call (`git rev-parse HEAD` + `git status --porcelain`,
  `None` when the cwd is not a git worktree) and returns the driver's `Completion`.
- `run()`: after the call, `_classify_cli(driver, completion, log, before, after)`:
  `timed_out` → `timeout`; else `classify_exit(driver.parse_exit(rc, tail))` where tail is the
  last 64 KiB of `impl.log` (missing log → `""`); else (ok) empty `text` **and** a before/after
  state that is known and equal → `empty_output`. A record → the worker writes
  `NodeResult(status="failed", exit_status=1, detail=<kind: evidence>, outputs={"failure": ...})`
  and **does not call the node handler** (for every class — a classified CLI failure never
  adopts). No record → handler adoption as today.
- `parse_exit` raising is a worker exception → `uncertain`, as any other (i.e. do NOT wrap it in
  a try/except inside `_run_agent_cli`/`_classify_cli` — let it propagate to `run()`'s existing
  outer `except Exception` block, which already produces `uncertain`).

### F3 — durable record and operator view

- `attempts` gains nullable `failure_kind`, `failure_class`, `failure_json` (schema +
  `_ADDED_COLUMNS`), allowed in `lifecycle._set_attempt_fields`.
- New `lifecycle.record_failure(attempt_id, *, run_id, record, at=None)` → same writer, the
  three columns. Called from `runner._commit_result` **only when `self.routing is not None`**
  and the committed phase is `failed`, via `failures.classify_node_result(result,
  handler_ref=node.handler_ref)`; a malformed `outputs["failure"]` is recorded as `unknown`
  with the raw value as evidence (i.e. wrap the `classify_node_result` call in
  `try/except ValueError`, and on catch build `FailureRecord(kind="unknown",
  evidence=str(result.outputs.get("failure")))`).
- `status.workflow_view` attempts already carry every column (it does `SELECT *` — see below);
  `explain_view` adds `"failure": {kind, class, evidence, retry_after_sec, reset_at} | None` per
  attempt, and `render_explain_text` prints `failure <kind> (<class>): <evidence>` under the
  attempt.
- `status.list_failures(task_id=None, *, kind=None, repo=None) -> list[dict]`: failed
  attempts with a `failure_kind`, oldest first, fields `task_id, run_id, node_id, attempt_id,
  backend_id, model_id, binding_id, failure_kind, failure_class, evidence, retry_after_sec,
  reset_at, started_at`. `render_failures_text`. Unknown `--kind` → `WorkflowViewError`.
- CLI: `workflow` verb choices gain `failures`; `task_id` becomes `nargs="?"`, required for
  `show`/`runs` (`parser.error`); `--kind K`. `failures` supports `--json`.

## Invariants (unchanged, binding — from the phase spec and implementation handoff)

- S1 only classifies and records. A classified failure still writes `failed`, exactly as today.
  Nothing here changes what is dispatched next (that's F4–F8, later sessions).
- Write the failure columns only when `routing is not None`. `compile_workflow` without
  escalation is untouched.
- One dispatch choke point. No Context Gate — do not build it, do not reference it beyond the
  existing `docs/maestro-context-gate-integration.md` note. Don't touch the `P12A_*` spec,
  `AGENTS.md` or `GEMINI.md`. Scratch files go in `.scratch/`.
- A PreToolUse hook rejects Bash text containing the privileged word (s-u-d-o). Never run it.
- pytest's summary line is suppressed: read the counts off the junit root `testsuite`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (this
  session is Sonnet 5, not Opus — use the right attribution). Make one commit for F2 and one for
  F3.
- Edit STATE with targeted replacements only.

---

## What to build

### F2: `maestro/workflows/worker.py`

Current `_run_agent_cli` (lines 135–155) throws away the `Completion` and calls nothing else.
Current `run()` (lines 158–219) calls `_run_agent_cli(args, context, brief)` then unconditionally
`handlers.resolve(node.handler_ref)(context)` (line 209–210).

1. Add a helper to snapshot worktree state:
   ```python
   def _worktree_state(cwd: Path | None) -> tuple[str, str] | None:
       """(HEAD sha, porcelain status) for a git worktree, or None when cwd isn't one."""
       import subprocess
       if cwd is None:
           return None
       try:
           head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                                  text=True, timeout=10)
           status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                                    capture_output=True, text=True, timeout=10)
       except (OSError, subprocess.SubprocessError):
           return None
       if head.returncode != 0 or status.returncode != 0:
           return None
       return (head.stdout.strip(), status.stdout)
   ```
   (Name/shape is a suggestion — match existing style. Use `context.worktree or
   context.workspace` as `cwd`, same as `_run_agent_cli` already does for `CompletionSpec.cwd`.)

2. Change `_run_agent_cli` to return `Completion` (import `Completion` isn't needed — just
   return what `driver.complete(...)` gives you):
   ```python
   def _run_agent_cli(args, context: HandlerContext, brief: str):
       from maestro.backends import registry
       from maestro.backends.base import CompletionSpec

       driver = registry.get_backend(registry.driver_name_for_backend_id(args.backend_id))
       config_pairs = tuple(
           tuple(item.split("=", 1)) for item in args.effort_config if "=" in item
       )
       return driver, driver.complete(
           CompletionSpec(
               prompt=brief, model=args.model,
               timeout=int(context.node.timeout_policy.execution_timeout_sec),
               cwd=context.worktree or context.workspace, writable=True,
               log_file=context.inbox.path / "impl.log",
               effort_args=tuple(args.effort_arg), effort_config=config_pairs,
           )
       )
   ```
   Returning `(driver, completion)` avoids a second `registry.get_backend` call in `run()` (the
   registry lookup should not run twice — check `registry.get_backend`/
   `driver_name_for_backend_id` are cheap/idempotent if you'd rather look it up again in `run()`
   instead; either is fine, but don't change driver-selection semantics).

3. Add `_classify_cli(driver, completion, log_path, before, after) -> FailureRecord | None`:
   ```python
   def _classify_cli(driver, completion, log_path: Path, before, after):
       from maestro.workflows.failures import classify_exit

       if completion.timed_out:
           from maestro.workflows.failures import FailureRecord
           return FailureRecord(kind="timeout", evidence=completion.text[:2000])
       tail = ""
       if log_path.exists():
           data = log_path.read_bytes()
           tail = data[-65536:].decode("utf-8", errors="replace")
       verdict = driver.parse_exit(completion.returncode, tail)
       record = classify_exit(verdict)
       if record is not None:
           return record
       if not completion.text and before is not None and after is not None and before == after:
           return FailureRecord(kind="empty_output", evidence="")
       return None
   ```
   Double check the exact `FailureRecord` import shape against `failures.py` (already read this
   session — `FailureRecord(kind=..., evidence=..., reset_at=...)`, `failure_class` derives
   automatically). `timeout` needs an import of `FailureRecord` too — hoist both imports to the
   top of the function or module-level under `TYPE_CHECKING`/lazy-import style matching the rest
   of the file (this file lazy-imports everywhere — follow that).

4. In `run()`, replace the unconditional `_run_agent_cli(...)` + handler call:
   ```python
   if args.backend_id:
       manifest = (...)
       brief = brief_from_manifest(manifest)
       if args.runtime_kind == "model_endpoint_harness":
           result = _run_model_endpoint(args, repo, revision, brief)
           return _finish(inbox, args, result)
       cwd = context.worktree or context.workspace
       before = _worktree_state(cwd)
       driver, completion = _run_agent_cli(args, context, brief)
       after = _worktree_state(cwd)
       failure = _classify_cli(driver, completion, context.inbox.path / "impl.log", before, after)
       if failure is not None:
           result = NodeResult(
               run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
               status="failed", exit_status=1,
               detail=f"{failure.kind}: {failure.evidence}",
               outputs={"failure": failure.to_dict()},
           )
           return _finish(inbox, args, result)
   result = handlers.resolve(node.handler_ref)(context)
   ```
   Note the early `return _finish(...)` on a classified failure — this is what "does not call
   the node handler" means. Make sure this sits *inside* the existing `try/except Exception`
   block (lines 193–218) so `parse_exit` raising still degrades to `uncertain` via the outer
   handler, not a crash.

Nothing else in `worker.py` changes. `_run_model_endpoint` (F1, already done) is untouched.

### F3: five files, in this order

**1. `maestro/control/store.py`** — around line 285 (attempts DDL) and line 398 (`_ADDED_COLUMNS`):
add
```python
CREATE TABLE IF NOT EXISTS attempts (
    ...
    reset_crossing INTEGER,
    failure_kind TEXT,
    failure_class TEXT,
    failure_json TEXT
);
```
and to `_ADDED_COLUMNS`:
```python
("attempts", "failure_kind", "TEXT"),
("attempts", "failure_class", "TEXT"),
("attempts", "failure_json", "TEXT"),
```

**2. `maestro/control/lifecycle.py`** — `_set_attempt_fields`'s `allowed` set is at line 398–403.
Add `"failure_kind", "failure_class", "failure_json"` to it. Then add a method near
`record_routing_settle` (after line 395):
```python
def record_failure(
    self, attempt_id: str, *, run_id: str, record, at: str | None = None
) -> None:
    """P12B F3: the classified failure onto a finished attempt. Append-only, like W3."""
    self._set_attempt_fields(attempt_id, {
        "failure_kind": record.kind,
        "failure_class": record.failure_class,
        "failure_json": json.dumps(record.to_dict()),
    })
```
Check whether `lifecycle.py` already imports `json` at module level (likely yes, given
`payload=...` uses elsewhere in the file write through `append_event` which JSON-encodes) —
if not, add the import. `run_id` and `at` are accepted for signature symmetry with
`record_routing_decision`/`record_routing_settle` but unused by `_set_attempt_fields` (same as
those two methods — don't overthink this, just match the existing pattern exactly).

**3. `maestro/workflows/runner.py`** — `_commit_result`, around line 683–690. Right after the
existing block:
```python
if phase is not None:
    self._record_settle_telemetry(row, at=at)
    if self.routing is not None:
        self.routing.on_attempt_finished(self, attempt_id)
```
add (still inside `if phase is not None:`, still gated on `self.routing is not None`):
```python
if self.routing is not None and phase == "failed":
    from maestro.workflows import failures as wf_failures
    try:
        record = wf_failures.classify_node_result(result, handler_ref=node.handler_ref)
    except ValueError:
        record = wf_failures.FailureRecord(
            kind="unknown",
            evidence=str((result.outputs or {}).get("failure"))[:2000],
        )
    if record is not None:
        self.lifecycle.record_failure(attempt_id, run_id=self.run_id, record=record, at=at)
```
`node` is already in scope (bound at the top of `_commit_result`, line 645). Put the import at
module level instead of inline if the file's style prefers that — check the top-of-file imports
(lines 1–60 were read this session; `runner.py` imports `Lifecycle`, `ControlStore`, etc. at
module level, not lazily, unlike `worker.py`/`status.py`). Match that: add
`from maestro.workflows import failures as wf_failures` to the top import block, not inline.

**4. `maestro/status.py`**:
- `workflow_view`'s attempt loop (around line 462–476) already does `_row_to_dict(row)` on a
  `SELECT * FROM attempts` row, so the three new columns already flow through as raw
  `failure_kind`/`failure_class`/`failure_json` fields on each attempt dict — no change needed
  there.
- `explain_view`'s per-attempt dict (in the `links` loop, around line 686–692, inside
  `for attempt in link["attempts"]`) currently builds:
  ```python
  {"attempt_id": a["attempt_id"], "status": a["status"],
   "started_at": a["started_at"], "outcome_ref": a["outcome_ref"],
   "routing": _attempt_routing(a)}
  ```
  Add a `"failure"` key. Write a small helper near `_attempt_routing` (around line 590):
  ```python
  def _attempt_failure(attempt: Mapping) -> dict | None:
      if not attempt.get("failure_kind"):
          return None
      return {
          "kind": attempt.get("failure_kind"),
          "class": attempt.get("failure_class"),
          "evidence": redact_secrets(json.loads(attempt["failure_json"]).get("evidence", ""))
                      if attempt.get("failure_json") else "",
          "retry_after_sec": json.loads(attempt["failure_json"]).get("retry_after_sec")
                      if attempt.get("failure_json") else None,
          "reset_at": json.loads(attempt["failure_json"]).get("reset_at")
                      if attempt.get("failure_json") else None,
      }
  ```
  (Simplify by parsing `failure_json` once into a local `dict` rather than three times — this is
  sketch-level, clean it up.) Then add `"failure": _attempt_failure(a)` to the per-attempt dict
  in `explain_view`.
- `render_explain_text` (around line 793–815): after the `attempt` line
  (`f"  │   attempt {attempt['attempt_id']} [{attempt['status']}]"`), append when present:
  ```python
  failure = attempt.get("failure")
  if failure:
      lines.append(
          f"  │     failure {failure['kind']} ({failure['class']}): {failure['evidence']}"
      )
  ```
- New `list_failures` and `render_failures_text`, placed near `list_runs`/`render_runs_text`
  (lines 391–419). Copy `list_runs`'s `_open_store`/close pattern. Something like:
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
  Verify `attempts.run_id` joins cleanly to `task_runs.run_id` and that `task_runs.task_id`
  exists (used already by `_task_runs`, line 363–377 — confirmed this session). Double-check
  column name collisions between `a.*` and `r.task_id` don't break `sqlite3.Row` key lookup
  (they won't — `r.task_id` is a distinct output column name from any `a.` column; `attempts`
  has no `task_id` column itself, confirmed from the DDL read this session).

**5. `maestro/cli.py`**:
- Parser, around line 1923–1934 (`p_workflow`): change
  `p_workflow.add_argument("verb", choices=["show", "runs"])` to
  `choices=["show", "runs", "failures"]`; change
  `p_workflow.add_argument("task_id")` to `p_workflow.add_argument("task_id", nargs="?")`; add
  `p_workflow.add_argument("--kind", default=None)`.
- `cmd_workflow` (starts line 1444). After the `verb == "runs"` branch (ends around line 1471)
  and before `if verb != "show":`, add a `failures` branch, and add the `task_id`-required check
  for `show`/`runs` at the top of the function:
  ```python
  def cmd_workflow(repo_root: Path, verb: str, task_id: Optional[str], *, as_html: bool = False,
                   as_json: bool = False, out: Optional[str] = None,
                   run_id: Optional[str] = None, kind: Optional[str] = None) -> int:
      ...
      if verb in ("show", "runs") and task_id is None:
          print(f"[workflow] {verb!r} requires a task_id")   # or use parser.error via caller
          return 2
      ...
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
  ```
  The spec says the `task_id`-required check should be `parser.error(...)` — that requires
  doing the check in `main()` where `parser` is in scope, not inside `cmd_workflow`. Prefer:
  in `main()`'s `if args.command == "workflow":` block (around line 1978–1981), add
  ```python
  if args.verb in ("show", "runs") and args.task_id is None:
      parser.error(f"workflow {args.verb} requires task_id")
  ```
  before calling `cmd_workflow`, and pass `kind=getattr(args, "kind", None)` through.

Sanity-check `cmd_workflow`'s call sites (just the one, in `main()`) and any other caller (grep
`cmd_workflow(` across the repo) before changing its signature.

---

## Tests

Per the design contract (§Tests), write/extend:

- `tests/backends/test_model_api.py`, `tests/backends/test_model_runtime.py`: **already done in
  S1a (F1)** — do not touch.
- `tests/graph_engineering/test_failure_ladder.py` (new file, under the loop, `run_lane`): F2/F3
  scenarios. Read `tests/graph_engineering/test_dispatch_wiring.py` — the helpers you need,
  already located this session:
  - Top-of-file imports (lines 28–57) — copy the shape, import from
    `.conftest import FixtureQueries, LaneScenario, fixture_cli_entry, fixture_routing`.
  - `_graph_scenario(lane, **overrides)` (line 119–123): builds a `LaneScenario` defaulted to
    graph mode with one task and `before_run=_create_control_db`.
  - `FREE_ENDPOINT` (line 278–281), `resolutions` fixture (line 284–296) — for F1-adjacent
    scenarios if needed, but F2 is CLI-backend, not free-tier, so you likely want a fixture CLI
    entry instead (`fixture_cli_entry`, imported from `.conftest`, used at line 1452).
  - `_windows(run)` (line 299–300), `_worker_argv(command)` (line 303–306), `_attempts()` (line
    309–312), `_attempt_rows()` (line 485, returns full rows not just 3 cols — check its exact
    body near line 485–499, wasn't fully read this session, grep for it), `_node_phase(node_id)`
    (line 315–319).
  - `_run_the_gate_worker(run)` (line 1256–1277) and `_answer_with_a_proposal(node_id,
    after_answer)` (line 1482–1510) are the patterns for driving `wf_worker.main(...)` in-process
    from an `on_poll` callback while monkeypatching results for other nodes. For F2 you'll want
    an `on_poll` that monkeypatches `registry.get_backend` (or seeds a fake driver class with
    `.complete()`/`.parse_exit()` you control) *before* calling `run_lane`, then runs
    `wf_worker.main(_worker_argv(...))` for the implementer node the same way
    `_run_the_gate_worker` does for `gate`.
  - To spy on adoption **not** being called: monkeypatch `handlers.resolve` (module is imported
    as `from maestro.workflows import handlers` in worker.py) to append to a list and raise/return
    a sentinel if called, and assert the list stays empty after a classified-failure run.
  - Scenarios to cover (from spec §Tests, F2/F3 acceptance in
    `P12B_MODEL_FAILURE_HANDLING.md` §3): fake driver `quota_exhausted`+reset → failed result
    with reset, adoption spy not called; `crashed` → `worker_crashed`; unrecognised verdict kind
    → `unknown`; `timed_out` → `timeout`; empty completion in an unchanged git worktree →
    `empty_output`; F3 — after the next poll the attempt row carries the columns, `explain` and
    `workflow failures --json` show them, `--kind unknown` lists only unknowns; routing-None
    ingest writes no failure columns (build a non-routed scenario, e.g. via
    `tests/graph_engineering` patterns for `routing is None`, or a legacy-loop equivalent — check
    how other routing-None tests in this suite are structured, e.g.
    `test_an_unrouted_verification_node_gets_no_selection`, line 1359–1384, which builds a
    `WorkflowRunner` directly with no `routing=` kwarg).
- `tests/test_workflow_status.py`: `list_failures` filter and render units — plain unit tests
  against a `ControlStore` you build directly (check this file's existing fixtures for
  `workflow_view`/`explain_view` tests — it wasn't read this session; skim it first for the
  `_open_store`/DB-seeding pattern already in use there before writing new tests, to match
  style).

---

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # must exit 0
```

Report the gate and full-suite junit counts (read off the junit root `testsuite` element — the
summary line is suppressed by this repo's pytest config), the F2/F3 commit hashes, and the path
of the S2 handoff you write at the end (S2 = F4, F5; needs its own design contract per
`handoffs/2026-09-17-graph-p12b-implementation.md`'s suggested split).

For Sonnet 5, prepare a handoff at 120K tokens and start fresh by 150K (this session's own
context-governor threshold — don't repeat the mistake of finishing all research before checking
the budget; check it after F2 is committed, before starting F3, and again after F3's code is
written but before tests). The next handoff must be a self-contained file in `handoffs/`. S2
needs its own design contract (`handoffs/…-p12b-s2-design.md`) before any code.
