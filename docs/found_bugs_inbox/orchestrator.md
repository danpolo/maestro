# Found bugs — orchestrator

Behaviour observed in the reference `scripts/orchestrator_run.py` while writing the
characterisation suite. Pinned as-is by the tests; **not** fixed in the reference.

- **`_do_retry(..., retry_counts, ...)` ignores its `retry_counts` argument entirely**
  (`orchestrator_run.py:3602`). The body never reads or writes the dict; it calls
  `read_state()` itself and writes back only `in_flight`. So the retry allowance is
  persisted *only* where a caller happens to do it by hand.
- **Retry-count increments are lost on restart for the re-brief paths.** The
  timeout/FAILED path persists the bump explicitly (`orchestrator_run.py:4124-4128`,
  "B11"), but the gate-failure re-brief (`:4202-4205`) and the Sonnet-proof-review
  re-brief (`:4221-4223`) bump `retry_counts[task_id]` in the in-process dict only.
  Because `_do_retry` re-reads state from disk, the bump is never written through, and
  the in-memory dict dies with the process. After an orchestrator restart the same task
  is eligible for another "first" retry through those paths — the `< 1` cap becomes
  unbounded across restarts instead of one retry per task.
