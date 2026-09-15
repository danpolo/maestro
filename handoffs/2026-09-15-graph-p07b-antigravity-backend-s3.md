# Handoff — P07B: Antigravity (agy) as a light / balanced backend (session 3)

Paste into a fresh Claude Code session in `/home/dan/projects/maestro`.

---

## Goal

Implement Phase P07B end to end. Session 1 probed the CLI. Session 2 read every seam the phase touches and settled all remaining design points, but wrote no code. **This session writes the code.** Do not re-read the seams below to re-derive them. Open a file only when you are about to edit it.

**Read first, in this order:**

1. `handoffs/2026-09-15-graph-p07b-antigravity-backend.md`: session 1's settled decisions and established facts. They all still hold. Don't re-probe.
2. `docs/graph-engineering/specs/phases/P07B_ANTIGRAVITY_BACKEND.md`: the checklist and acceptance criteria.
3. `artifacts/graph-engineering/p07b-agy-1.2.2-probe.json`: the evidence.
4. This file's **Implementation plan**.
5. `.venv/bin/python scripts/next_graph_prompt.py`, for the §6 close-out rules.

## New decisions and facts (session 2, 2026-09-15)

1. **Dan: no shipped role floor changes.** Every role in `maestro/templates/roles/` stays at `balanced` or `strong`. Prove "a light task binds to an agy light model" with a **test-local** role or policy whose `min_reasoning_strength` is `light`, driven with a manifest where `risk="low"` and `churn_files<=1`. Record the resulting gap under `_p07b_open_threads` with `disposition: accepted`: no shipped role yields light, so agy light models get no real work yet.
2. **agy auto-updated to 1.2.3.** `agy --version` prints exactly `1.2.3`. The thirdparty row's `verified_version` must be that exact string (`thirdparty.status_for` compares for equality). Evidence is the 1.2.2 probe plus a cheap 1.2.3 re-check. `agy models` and a client-side-failing `--effort` probe both cost zero quota. Say so in `verified_by`.
3. **Fallback-chain hazard.** `roles.default_chain()` returns *every* `registry.known_backends()`. Registering `antigravity` unguarded would put it in the implicit fallback chain, and quota pressure on claude would move real implementer work onto agy. Fix it as data: add `in_default_chain: bool = True` to `DriverRef`, set it `False` on the antigravity row, and filter on it in `default_chain()` (roles.py, which is allowlisted anyway). An explicit `fallback_chain:` or `/backend antigravity` still works, because `_clean_chain` uses `known_backends()`. Add a test.
4. **Register subject vs registry name.** `thirdparty.required_subjects()` keys on `BACKENDS` names, and so does `tests/test_thirdparty.py::test_every_registered_backend_has_a_verification_row`. The register subject is `agy` (the binary), and the registry name is `antigravity`. Switch both to `ref.binary`. `tests/conftest.py` already sources binaries that way. Also update `tests/backends/test_registry.py:57`, which compares `known_backends()` against a hardcoded tuple.
5. **backend_id: add the override.** Add `backend_id=str(base.get("backend_id") or subject)` in `catalog_from_register`, and use `antigravity_cli` as the spec asks. That leaves three names for one backend: catalog `antigravity_cli`, register `agy`, registry `antigravity`. Add a `_p07b_open_threads` entry `owner: P12A` for the backend_id → driver-name mapping dispatch will need.
6. **No production catalog defaults exist.** `catalog_from_register` is only ever called from tests, and the claude/codex defaults live in `tests/test_routing.py::shipped_catalog`. Put the agy judgement in `maestro/backends/catalog.py` as a constant, e.g. `ANTIGRAVITY_CATALOG_DEFAULTS = {"agy": {...}}`, and have `shipped_catalog` merge it:
   - `backend_id: antigravity_cli`, `billing_category: subscription`, `usage_pool_id: antigravity-gemini`
   - `available_models`: the 14 ids from the probe, each with an explicit `ModelProfile` (only `gemini-3.8-flash-high` is `balanced`)
   - `context_limits`, the entry default for the Flash family: `max_input_tokens=1_048_576` (the status line's `context_window_size` at 1.2.2) and `effective_context_ceiling=120_000` (the `agy/` table's prepare-handoff high)
   - per-model `ContextLimits` overrides:
     - 3.1 Pro: 128K input / 108K ceiling
     - GPT-OSS: 80K input / 60K ceiling
     - the two Claude 3p models: 160K input (their AGY profile) / 120K ceiling
   - `max_output_tokens`: leave at the 32_000 default.
7. **Effort stays unsupported without special-casing.** Add a `findings.effort_control` block to the thirdparty row with `kind: flag` and an `observed` note citing the probe, and **no `native_values`**. `effort_controls_from_findings` then yields `supported=False` with a reason. Assert `plan.effort_args == ()` for an agy binding (mirror `test_no_effort_flag_is_invented_...`).
8. **Limits: raw ids don't fold onto `agy/` rows.** `gemini-3.8-flash-high` carries a `-high` suffix. `Claude Opus 4.6 (Thinking)` has parens. The `claude-sonnet-4-6` id lacks "thinking". The status line's `model.id` is the display form `Gemini 3.8 Flash (High)`. The plan is a last-resort fold in `limits._lookup`, applied **only when the name carries a harness prefix** (any value of `AGENT_MODEL_PREFIX`). It normalises both sides, drops `(`/`)`, and strips one trailing `-(high|medium|low|thinking)`. That maps all 14 ids plus the display form onto the 7 fixture rows. Test every id against `tests/fixtures/model_limits/antigravity.md`. An unknown agy id must still give `None` plus a warning, and a backend-less lookup must never reach an `agy/` row.
9. **Call sites.**
   - `switch.context_crossed`: add a `backend=None` kwarg, and call `(resolve or limits.resolve)(limits.scoped_model_name(name, backend))`. The resolve seam stays single-argument, so `monkeypatch.setattr(orchestrator, "resolve_model_limits", lambda name: ROW)` in `tests/test_orchestrator_switch_hooks.py` / `tests/test_per_session_usage.py` keeps working, and the memo is keyed on the scoped name.
   - `orchestrator.py` ~L1447: pass `backend=backend`, which L1446 already computes.
   - `cli._check_model_limits`: iterate `role["models"].items()` and resolve `limits.scoped_model_name(model, backend)`.
10. **Driver shape** (`maestro/backends/antigravity.py`, `name = "antigravity"`, `BINARY = "agy"`):
    - **Launch argv:** `agy --output-format stream-json --model <id> -p <brief>`. **Resume argv:** `agy --output-format stream-json --conversation <cid> [--model <id>] -p <prompt>`. Write your own launcher-source renderer (one `repr()` element per line). claude's `_render_flags` pairs elements two at a time, which would misalign here. Keep the brief-from-file, `impl.log` open, stderr→stdout, and tmux window conventions.
    - **Session id:** a conversation id cannot be set at launch, so `Handle.native_id=None`. Resume and `usage(handle)` recover `conversation_id` from the `init` event in `<workspace>/impl.log`.
    - **Capabilities:** `native_resume=True`, `system_prompt_file=False`, `usage_telemetry=True`, `sandbox=False`. `--sandbox` has never been exercised.
    - **`complete()`:** `agy --output-format json [--model m] [--conversation id] -p <prompt>`. Return text=`response` only when rc==0 and `status=="SUCCESS"`. Use an injectable `runner=` seam, like claude.
    - **`parse_exit`:** run `quota._LIMIT_RE` over the **non-JSON lines plus `result.error`** only, never over `text_delta` agent output, where code can say "rate limit". A match gives `quota_exhausted`. Otherwise it's `ok` when rc==0 and the last `result.status=="SUCCESS"`, and `crashed` with `result.error` as evidence in every other case.
    - **`usage()` with no handle:** read `~/.gemini/statusline_dump.json` (`gemini_home()` method honouring an override, like `claude_home`). Map `quota.gemini-5h`→300 and `quota.gemini-weekly`→10080, with `used_pct=(1-remaining_fraction)*100`, `resets_at` from the `reset_time` ISO, `model=model.id`, and `updated_at`=file mtime. The 3p pools are not mapped (P12A thread). **The real dump contains the operator's email; build fixtures synthetically, and never copy it.**
    - **`usage(handle)`:** find the last `step_update` carrying `usage` in impl.log and set `context_total_input_tokens = input_tokens + cache_read_tokens`, `model`=`init.model`, `session_id=handle.session_id`, `updated_at`=log mtime. Return `None` when there's no usable turn. Skip `result` events, whose usage is cumulative.
    - **Forward compatibility (§4):** give `base.Usage` two additive optional fields, `token_split` (a small frozen dataclass: `fresh_input`, `cache_read`, `cache_write=None`, `output`) and `cli_version=None`. Keep both out of `to_usage_json`. First check that `tests/backends/test_protocol.py` doesn't pin `Usage`'s field set.
11. **The stream-json event key is unconfirmed.** Is the discriminator `"type"`? Session 1's raw output is in `~/.claude/projects/-home-dan-projects-maestro/5ddbed44-d8d6-4d68-8261-d8fbd6fc3370.jsonl`. Extract a sample with Python, because ugrep rejects the long-context regex. Parse tolerantly: accept a `type` or an `event` key.
12. `cli._probe_model_id` has no agy marker, so it reports "no probe defined — skipped". That's fine; leave it.

## Implementation plan (order)
1. Registry row with `in_default_chain`, the `default_chain` filter, `required_subjects`→binary, and the registry/thirdparty/roles test updates.
2. Limits prefixed fold, the `context_crossed` backend kwarg, the orchestrator and cli call sites, and their tests (`tests/test_limits.py`, `tests/test_switch.py`).
3. The `Usage` additive fields, then the driver and `tests/backends/test_antigravity.py`, with the `_Runs` firewall copied from `test_claude_driver.py`. Check whether the parametrised half of `tests/backends/test_protocol.py` needs agy added.
4. Catalog: the `backend_id` override, `ANTIGRAVITY_CATALOG_DEFAULTS`, and a catalog test that fails on any listed agy id without an explicit profile and matches the probe's id list.
5. The thirdparty row: `scope: runtime`, `maestro_driver: true`, `verified_version: "1.2.3"`, the effort_control block, resume/compaction facts, pruned `open_questions`, and `quota_measurement` set to push-only.
6. Routing tests in `tests/test_routing.py`: balanced binds only `gemini-3.8-flash-high` (agy-only catalog); strong never binds agy; a paused `antigravity-gemini` pool sends balanced work to claude/codex; the light path goes through the test-local role; no effort args are sent.
7. STATE.yaml: P07B complete, `_p08_open_threads` #5/#6 closed with evidence, and `_p07b_open_threads` (3p pool misattribution → P12A, backend_id mapping → P12A, no light shipped role → accepted, `--sandbox`/permission mode for headless edits never exercised → P12A).

## Parallel-session coordination
- Branch `feat/graph-engineering-foundation`. It has no upstream, so `git pull` fails. Check `git log --oneline -3` instead. HEAD at handoff was the commit carrying this file.
- STATE.yaml: targeted edits only. Leave the P12A spec files alone.

## In scope / Out of scope
- In: everything above, plus `_p08_open_threads` #5 and #6.
- Out: the Context Gate, JSONL logs, compaction control, passing effort into `LaunchSpec`, an agy quota pull path, shipped role floor changes, and an agy doctor model-id probe.

## Verification (report the numbers)
```bash
.venv/bin/python -m pytest -q tests/backends/test_antigravity.py tests/test_routing.py tests/backends/test_catalog.py tests/test_thirdparty.py tests/test_limits.py
.venv/bin/python -m pytest -q tests/test_no_name_branching.py tests/backends tests/test_switch.py tests/test_orchestrator_switch_hooks.py tests/test_roles.py tests/workflows/test_policy.py tests/test_per_session_usage.py
.venv/bin/maestro doctor --thirdparty   # agy verified at 1.2.3, no drift
```
Report the pass counts for both runs and doctor's thirdparty line. Also confirm three things: `roles.default_chain()` does not contain `antigravity`; all 14 agy ids resolve to an `agy/` row; and a backend-less lookup of `Gemini 3.8 Flash` returns `None`.
