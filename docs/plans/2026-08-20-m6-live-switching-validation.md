# M6 — Live switching validation: plan

**Spec:** `docs/EXECUTION.md`'s M6 stage entry. **Done when:** "a real threshold trigger produces a
real backend switch on the migrated project, and the journal shows the `backend_switch` event."

M2 already proved the **manual** trigger end to end with real CLIs (`docs/PROGRESS.md`, 2026-08-14,
"the live acceptance switch"). What M6 adds is the one trigger M2 didn't exercise for real: the
**usage-threshold-crossed** path (`docs/DESIGN.md` §7 trigger 2), on the actual migrated project
(`AbuAliArchive`), post-cutover.

## What the code actually does (read directly from source, not recalled)

- `maestro/switch.py`: `SWITCH_THRESHOLD_PCT = 70.0` — the **only** enforced threshold. `AbuAliArchive/
  project.yaml`'s `switch.on_usage_threshold: {five_hour_pct: 85, weekly_pct: 90}` is **not read
  anywhere in maestro** (`grep -rn "on_usage_threshold\|five_hour_pct\|weekly_pct"` across `maestro/`
  → zero hits) — it is the aspirational config the Open Questions "M2/M6 calibration" item already
  flags as unwired. M6 validates the code that ships today (the hardcoded 70%), not the unwired
  config value.
- `maestro/orchestrator.py`'s poll loop (`POLL_INTERVAL = 30`): each cycle calls `get_effective_cap()`
  (`cap, five_pct`, from `maestro.quota`, itself reading `.orchestrator/usage.json`'s
  `five_hour.used_pct`), then `_threshold_switches(in_flight, launch_times, five_pct)` — **before**
  that cycle's "Launch new tasks" section. A task launched *this* cycle is therefore only eligible
  for a threshold switch on the **next** cycle (30s later), once it is in `in_flight`.
- `_threshold_switches` requires, per entry: `_handover_ready` (role `implementer`, not `script`;
  worktree directory exists on disk) and `_under_usage_pressure` (samples the entry's own backend's
  `driver.usage()` — for `claude`, `ClaudeBackend.usage()` reads the same `usage.json` — and compares
  `Usage.max_used_pct()` against `SWITCH_THRESHOLD_PCT` via `threshold_crossed`).
- `usage.json` is written externally by the Claude Code `statusLine` hook (`maestro/backends/
  claude.py`'s `_read_usage_document` docstring) — maestro itself never writes it. Editing it directly
  is not a workaround; it is *literally* the channel the real usage-threshold trigger reads, the same
  file this whole mechanism already depends on in production.
- Once triggered: `switch_task` writes the `SWITCH` sentinel into the outgoing implementer's
  workspace, waits up to `DEFAULT_GRACE_SEC=120` for a checkpoint-and-exit (or kills the tmux window
  as fallback), builds a handoff brief from the worktree, launches the target backend (`codex`, the
  only other entry in `AbuAliArchive/project.yaml`'s `fallback_chain: [claude, codex]`) in the **same**
  worktree, and journals `backend_switch {task, from, to, reason}` flattened into `detail`.

## Procedure — conservative, reversible, mirrors the M5SCRATCH1 precedent

1. **Precondition:** M0–M5 all green (true — see `docs/PROGRESS.md` Status table). Re-verify the live
   baseline fresh (HEAD, `git status --porcelain`, both PIDs, `agents` tmux window, `maestro status`)
   before touching anything — done at the top of this session, matches the recorded baseline exactly.
2. **Confirm the loop is idle** (`In-flight: 0`) before adding a new task — true at baseline capture
   time.
3. **Add one scratch task**, `M6SCRATCH1`, to `AbuAliArchive/docs/ROADMAP.md` (uncommitted, exactly
   like every hand-added task and like `M5SCRATCH1` before it): `mode: autonomous`, `deps: []`,
   `eval_relevance: non-retrieval` (skips the smoke/recall gate, as it did for M5SCRATCH1), no `kind:
   script` this time — it must be a **real LLM implementer** task, because the threshold trigger only
   ever moves an implementer, never a script task (`_handover_ready`). The brief is scoped to a single,
   trivial, worktree-local deliverable (write one scratch file + commit) so the diff surface is tiny
   and touches nothing risky, matching every guardrail `_make_brief` already applies (no push, no
   `.env`, no bot files, no ROADMAP edits, stay inside the worktree).
4. **Let it launch on the default backend** (`claude`, per `project.yaml`'s `implementer.backend`).
   Confirm via `.orchestrator/state.json`'s `in_flight` entry and `tmux list-windows -t agents` that
   the window and worktree genuinely exist before doing anything else.
5. **Back up `.orchestrator/usage.json`** verbatim before any edit (its real content, captured before
   this plan was written: `context_used_pct=6, five_hour.used_pct=2, seven_day.used_pct=37,
   updated_at=2026-08-13T10:08:16Z` — over a week stale, confirming the statusLine hook is not
   actively refreshing it right now and is unlikely to race a short-lived edit).
6. **Write a crossing value**: `five_hour.used_pct = 72` — above `SWITCH_THRESHOLD_PCT` (70) but below
   `THROTTLE_75_PCT` (75) and `PAUSE_92_PCT` (92) in `maestro/quota.py`, so the *only* production
   behaviour this trips is the switch, not the concurrency throttle or the usage-limit pause. Every
   other field left as read (schema per `maestro/backends/base.py`'s `to_usage_json`/
   `from_usage_json`).
7. **Poll the journal** (`.orchestrator/journal.ndjson` tail) every ~10s until `backend_switch` (or
   `backend_switch_failed`) appears, or the task's own `DONE`/completion beats it (race: a trivial
   one-file task might finish inside one 30s poll cycle before the switch fires — if so, remove the
   task's leftovers and retry once as `M6SCRATCH2` with a slightly larger, still-trivial brief that
   costs a couple more tool calls, buying more real wall-clock before completion. `hold: true` is the
   documented kill switch — from the M5SCRATCH1 incident — if a retry starts misbehaving).
8. **On a genuine `backend_switch` event:** verify directly, not just from journal text — the new
   `in_flight` entry's `backend` is `codex`, the old `claude` tmux window is gone (checkpointed or
   killed), the worktree's content is unchanged (uncommitted work, if any, survived), and (ideally) the
   task goes on to complete under Codex exactly like any real switched task would. This satisfies M6's
   Done-when in full.
9. **Restore `.orchestrator/usage.json`** to its captured pre-edit content immediately after the
   switch event is confirmed (not after full task completion, which could take longer under a fresh
   Codex backend) — a stale artificially-high reading left in place would wrongly pressure every real
   future task (including `TUNE2`/`LAT1`, if ever unparked) into switching for no real reason.
10. **Re-verify the baseline** (git status, PIDs, HEAD) afterward, exactly as every M5 session did.
    Record the switch's real evidence (journal lines, entry contents) in `docs/PROGRESS.md`.

## Explicitly not in scope here

- Calibrating `SWITCH_THRESHOLD_PCT` or wiring `project.yaml`'s `switch.on_usage_threshold` — that is
  the standing "M2/M6 calibration" Open Question, deliberately left for a future decision once
  switching has run in anger (this run **is** "running in anger" for the first time, but changing the
  constant itself is a separate, deliberate change this plan does not make).
- Touching `TUNE2`/`LAT1` or their `parked_tasks` status — unrelated to this stage, still the
  operator's call per the M5 close-out.
