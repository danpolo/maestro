# Handoff — P07B: Antigravity (agy) as a light / balanced backend (session 2)

Paste into a fresh Claude Code session in `/home/dan/projects/maestro`.

---

## Goal

Implement Phase P07B end to end: the agy driver, its registry row, a catalog row grading the agy models (`gemini-3.8-flash-high` `balanced`, every other id `light`), the re-verified thirdparty row, and agy-scoped limits lookups. Session 1 wrote no code. It **probed the installed CLI (1.2.2)** and recorded every fact the phase needs, so do not re-probe anything that is already recorded.

**Read first, in this order:**

1. `docs/graph-engineering/specs/phases/P07B_ANTIGRAVITY_BACKEND.md` (the checklist and acceptance criteria)
2. `artifacts/graph-engineering/p07b-agy-1.2.2-probe.json`: the measured facts. **Use it as the evidence for the thirdparty row.**
3. `docs/graph-engineering/STATE.yaml` → `in_progress_details` and `_p08_open_threads` #5, #6
4. Run `.venv/bin/python scripts/next_graph_prompt.py` for the directive (§6 close-out rules)

Dan's instruction for this phase: **assume Maestro's context management (the Context Gate) is built later.** Build §4's forward-compatible shapes only where they fall out naturally. Add nothing gate-shaped.

## Decisions (Dan, 2026-09-15 — settled, do not re-ask)

1. **Grading: only `gemini-3.8-flash-high` is `balanced`.** Every other listed id is `light`, including `gemini-3.8-flash-medium` / `-low`, all 3.7/3.6 Flash, 3.1 Pro, and the 3p models (`claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`). Nothing is `strong`. List the 14 exact ids from `agy models`, each with an explicit profile; backend effort `unsupported` (effort is fixed by the variant id — no `--effort` is ever sent). Acceptance restated: a `balanced` task binds to `gemini-3.8-flash-high` and no other agy id.
2. **Pool attribution:** single `usage_pool_id` for the Gemini pool (`antigravity-gemini`); the 3p models are light and their `3p-5h/3p-weekly` misattribution is an open thread owned by P12A (record it under `_p07b_open_threads`).

## Facts already established (do not re-derive)

- The argv must end in `-p <prompt>`. `agy -p --output-format json …` fails with rc=2.
- Resume works: `--conversation <conversation_id>`. The id is in the `init`/`result` events.
- The launcher should run with `--output-format stream-json`. Each `agent_response` `step_update` carries `usage{input_tokens, output_tokens, thinking_tokens, cache_read_tokens}`, and `result.usage` is the **cumulative sum**. For `usage(handle)`, take the **last turn's** `input_tokens + cache_read_tokens` from impl.log, never the sum (see `claude.context_reading`'s rationale). Cache-write tokens and the CLI version are not exposed: leave them `None`.
- The transcript under `~/.gemini/antigravity-cli/brain/<id>/…/transcript.jsonl` has no tokens and no model.
- Quota is not available headlessly. The only source is the push file `~/.gemini/statusline_dump.json` (4 pools, `remaining_fraction`, `reset_time`), written by an interactive status line. Set `telemetry_quality` to match.
- `quota_exhausted` evidence: no exhaustion string was observed. Classify `result.status=="ERROR"` as crashed unless the text matches `quota._LIMIT_RE`, and record the gap in the thirdparty `open_questions`.
- Limits: table rows are family display names (`agy/Gemini 3.8 Flash`), which cover the High/Medium/Low variants. An id needs mapping to its family display name (strip `-high|-medium|-low`, and use `agy models` display names) before `limits.resolve(..., backend="antigravity")`.
- The call sites that must pass a backend: `maestro/orchestrator.py` ~L1400 (`resolve` closure; memoise on `(backend, name)`, where the backend comes from `_entry_backend(entry)`), `maestro/switch.py` `context_crossed` (the `resolve` seam), and `maestro/cli.py` `_check_model_limits` (roles map `models: {backend: model}`, so use that key).
- The name-branching gate (`tests/test_no_name_branching.py`) allowlists only `registry.py` and `roles.py`. `limits.AGENT_MODEL_PREFIX` already exists, so do not add `if backend == "antigravity"` anywhere else.
- `catalog_from_register` uses the register subject (`agy`) as `backend_id`. The spec asks for `antigravity_cli`. Either add a `backend_id` override in `defaults` or accept the subject id, and **say which**.
- Tests may not run a real agent. Copy `tests/backends/test_claude_driver.py`'s `_Runs` firewall.

## Parallel-session coordination

- Branch `feat/graph-engineering-foundation`. Run `git pull` first, in case another session committed.
- STATE.yaml: targeted edits only.
- Leave the P12A files alone (`docs/graph-engineering/specs/phases/P12A_*`).

## In scope
- Everything in the P07B checklist, plus open threads #5 and #6.

## Out of scope
- The Context Gate, JSONL logs, compaction control, and passing effort into `LaunchSpec`/dispatch (P12A).
- A quota pull path for agy.

## Verification (report the numbers)
```bash
.venv/bin/python -m pytest -q tests/backends/test_antigravity.py tests/test_routing.py tests/backends/test_catalog.py tests/test_thirdparty.py tests/test_limits.py
.venv/bin/python -m pytest -q tests/test_no_name_branching.py tests/backends tests/test_switch.py tests/test_orchestrator_switch_hooks.py
.venv/bin/maestro doctor --thirdparty   # agy at runtime scope, no drift
```
Report: pass counts for both runs, and doctor's agy line. Also confirm routing tests that a `strong` demand never binds to agy, and that a `balanced` demand binds only to `gemini-3.8-flash-high` or falls to claude/codex when the agy pool is paused.
