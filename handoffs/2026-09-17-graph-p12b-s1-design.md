# P12B S1 — design contract (F0, F1, F2, F3)

Written by the S1 session after orientation. Binding source: `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md`
§1–§4 and the decisions in `handoffs/2026-09-17-graph-p12b-implementation.md`. S1 only
**classifies and records**. Nothing in S1 changes what the loop dispatches next: pausing,
switching and retrying are S2/S3 (F4–F8). A classified failure still writes `failed` exactly
as today.

## F0 — `maestro/workflows/failures.py`

- `BLOCKED = "blocked"`, `BAD_OUTPUT = "bad_output"`, `FAILURE_CLASSES`.
- `FAILURE_KINDS: Mapping[str, str]` (read-only `MappingProxyType`), the 12 kinds of spec F0.
- `FailureRecord(kind, failure_class="", evidence="", retry_after_sec=None, reset_at=None)`,
  frozen. `failure_class` is derived from `kind` when empty; a mismatching class, or a kind
  not in `FAILURE_KINDS`, raises `ValueError`. `evidence` is clipped to 2000 chars.
  `to_dict`/`from_dict` round-trip; `from_dict` of a non-mapping raises `ValueError`.
  `is_blocked` property.
- `VERIFICATION_HANDLERS = {"maestro.handlers.verification", "maestro.handlers.acceptance"}`.
- `classify_node_result(result, *, handler_ref="") -> FailureRecord | None`:
  - `result.status != "failed"` → `None` (`uncertain` is reconciliation's, not a verdict);
  - `outputs["failure"]` present → `FailureRecord.from_dict` (an unknown kind there raises,
    so the caller decides; the runner catches it and records `unknown` with the raw value
    as evidence — a worker must not be able to crash ingest);
  - otherwise `verification_failed` for a `VERIFICATION_HANDLERS` handler, else `unknown`;
    evidence = `result.detail`.
- The CLI table (the one table the spec allows): `classify_exit(verdict) -> FailureRecord | None`.
  `ok` → `None`; `crashed` → `worker_crashed`; `quota_exhausted` → `rate_limited` when the
  evidence matches `_RATE_LIMIT_RE` (`rate[ -]?limit|too many requests|\b429\b|spend limit|overloaded`),
  else `quota_exhausted`; `reset_at` carried. Any other verdict kind → `unknown`, the raw
  kind and evidence kept.
- `record_from_runtime(outcome) -> FailureRecord | None`: `status != "failed"` → `None`;
  `stop_reason` in `FAILURE_KINDS` → that kind with the outcome's `retry_after_sec`/`reset_at`;
  otherwise `unknown` with `"<stop_reason>: <detail>"` as evidence.

## F1 — `model_api`, `model_runtime`

- `TransportError` gains `status: int | None`, `retry_after_sec: int | None`,
  `reset_at: str | None` (keyword args, default `None`) and a class attribute
  `failure_kind = "unknown"`. Subclasses: `RateLimited` (`rate_limited`, instance may be
  `quota_exhausted`), `AuthFailed`, `ProviderUnavailable`, `RequestTimeout`, `ContextOverflow`.
  `failure_kind` is an instance attribute set in `__init__` so `RateLimited` can carry either.
- `Transport.post` may return `(status, payload)` or `(status, payload, headers)`;
  `RequestsTransport` returns the 3-tuple and maps `requests.Timeout` → `RequestTimeout`,
  `requests.ConnectionError` → `ProviderUnavailable`. `ModelAPI.complete` also maps a
  builtin `TimeoutError` → `RequestTimeout` and `ConnectionError` → `ProviderUnavailable`.
- `classify_http_failure(wire, status, payload, headers, endpoint_id, url) -> TransportError`:
  order — quota body (any 4xx incl. 429 whose body matches `_QUOTA_RE`) or 429 → `RateLimited`
  (`quota_exhausted` if body matches `_PERIOD_QUOTA_RE`: daily/per-day/monthly/per-month);
  400/413 whose body matches the wire's context regex → `ContextOverflow`; 401/403 → `AuthFailed`;
  ≥500 → `ProviderUnavailable`; else base `TransportError`.
- Headers (case-insensitive): `Retry-After` (seconds or HTTP-date) → `retry_after_sec`;
  `anthropic-ratelimit-*-reset` (RFC 3339), `x-ratelimit-reset` (epoch secs),
  `x-ratelimit-reset-requests`/`-tokens` (Go duration `6m0s`, `1s`, `250ms`) → `reset_at`
  (Zulu ISO), durations filling `retry_after_sec` only when `Retry-After` is absent.
  Unparseable → `None`.
- `ToolCall` gains `malformed: bool = False` (not in `to_dict`). `_decode_arguments` returns
  `(dict, ok)`; a string that fails to parse or parses to a non-object, or a raw value that is
  neither mapping, string nor None, is `ok=False`.
- `RuntimeOutcome` gains `retry_after_sec: int | None = None`, `reset_at: str | None = None`
  (in `to_dict`).
- `ModelAgentRuntime.run`: `TransportError` → `status="failed"`, `stop_reason=exc.failure_kind`
  (base class keeps `endpoint_unavailable`, as does any other `ModelAPIError`, incl.
  `CredentialMissing`), carrying retry/reset. First turn (no prior assistant turn) with empty
  text and no tool calls → `failed/empty_output`. A turn with any malformed call increments a
  consecutive counter (the calls are still dispatched/refused as today); a clean turn resets
  it; reaching 3 → `failed/malformed_tool_calls` after that turn's tools ran.
- `worker._run_model_endpoint` adds `outputs["failure"]` from `record_from_runtime`.

## F2 — `worker._run_agent_cli`

- Records the worktree state before the call (`git rev-parse HEAD` + `git status --porcelain`,
  `None` when the cwd is not a git worktree) and returns the driver's `Completion`.
- `run()`: after the call, `_classify_cli(driver, completion, log, before, after)`:
  `timed_out` → `timeout`; else `classify_exit(driver.parse_exit(rc, tail))` where tail is the
  last 64 KiB of `impl.log` (missing log → `""`); else (ok) empty `text` **and** a before/after
  state that is known and equal → `empty_output`. A record → the worker writes
  `NodeResult(status="failed", exit_status=1, detail=<kind: evidence>, outputs={"failure": ...})`
  and **does not call the node handler** (for every class — a classified CLI failure never
  adopts). No record → handler adoption as today.
- `parse_exit` raising is a worker exception → `uncertain`, as any other.

## F3 — durable record and operator view

- `attempts` gains nullable `failure_kind`, `failure_class`, `failure_json` (schema +
  `_ADDED_COLUMNS`), allowed in `lifecycle._set_attempt_fields`.
- New `lifecycle.record_failure(attempt_id, *, run_id, record, at=None)` → same writer, the
  three columns. Called from `runner._commit_result` **only when `self.routing is not None`**
  and the committed phase is `failed`, via `failures.classify_node_result(result,
  handler_ref=node.handler_ref)`; a malformed `outputs["failure"]` is recorded as `unknown`
  with the raw value as evidence.
- `status.workflow_view` attempts already carry every column; `explain_view` adds
  `"failure": {kind, class, evidence, retry_after_sec, reset_at} | None` per attempt, and
  `render_explain_text` prints `failure <kind> (<class>): <evidence>` under the attempt.
- `status.list_failures(task_id=None, *, kind=None, repo=None) -> list[dict]`: failed
  attempts with a `failure_kind`, oldest first, fields `task_id, run_id, node_id, attempt_id,
  backend_id, model_id, binding_id, failure_kind, failure_class, evidence, retry_after_sec,
  reset_at, started_at`. `render_failures_text`. Unknown `--kind` → `WorkflowViewError`.
- CLI: `workflow` verb choices gain `failures`; `task_id` becomes `nargs="?"`, required for
  `show`/`runs` (`parser.error`); `--kind K`. `failures` supports `--json`.

## Tests

- `tests/workflows/test_failures.py`: F0 units (round-trip every kind, unknown kind raises,
  class mismatch raises, classify_node_result cases, classify_exit table, record_from_runtime).
- `tests/backends/test_model_api.py`: F1 fake-transport cases (429+Retry-After:120, quota body
  daily, 401, 503, timeout, connection error, 400 context body per wire, 404 base class,
  headers parsing, 2-tuple transport still works, malformed arguments flag).
- `tests/backends/test_model_runtime.py`: stop_reason == kind for the five; empty first turn;
  3 malformed turns; 2 malformed + clean + 2 malformed does not trip; base error still
  `endpoint_unavailable`.
- `tests/graph_engineering/test_failure_ladder.py` (under the loop, `run_lane`): F1 free-tier
  worker with a 429 fake transport → failed result with `rate_limited` and retry_after 120;
  F2 fake driver `quota_exhausted`+reset → failed result with reset, adoption spy not called;
  `crashed` → `worker_crashed`; unrecognised verdict kind → `unknown`; timed_out → `timeout`;
  empty completion in an unchanged git worktree → `empty_output`; F3 after the next poll the
  attempt row carries the columns, `explain` and `workflow failures --json` show them,
  `--kind unknown` lists only unknowns; routing-None ingest writes no failure columns.
- `tests/test_workflow_status.py`: `list_failures` filter and render units.
