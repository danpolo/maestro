# Handoff — P12B implementation: model-failure handling

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-implementation.md`. No other prompt follows and
there is no chat context. Read this file in full, then follow the read order below.

Working directory: `/home/dan/projects/maestro`. Branch `feat/graph-engineering-foundation`
(**no upstream**, so `git pull` fails; that is expected).

---

## Where things stand

- **P12A is complete** (last commit `2ee754c`; full suite then: junit tests=4560 failures=0 skipped=1).
- **P12B was planned on 2026-09-17.** The planning commit is on the branch: spec, generator registration and its tests.
  - Spec: `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md`, items **F0–F8**.
  - `scripts/next_graph_prompt.py` now runs `... P12A → P12B → P12`. With no `--phase` it prints the P12B directive, exit 0.
- **STATE** (`docs/graph-engineering/STATE.yaml`, git-ignored): `current_phase: P12B`, `status: not_started`. The status comment records Dan's decisions.
  - `_p12b_open_threads` holds **3 threads with `owner: P12B`** (dead-writer candidate → F8; `apply_repair` refusal limit → F7; no failure handling at all → F0–F7). All three must be closed or re-owned before P12B is marked complete.
  - A backup from before planning: `.scratch/p12b/STATE.before-planning.yaml`.
- **The visual plan** was published at https://claude.ai/artifact/3CVdjn3w3eJs8sK9HfXFVF (source `~/.agent/diagrams/p12b-failure-ladder.html`) and sent to Dan's inbox.

## Read order

1. `.venv/bin/python scripts/next_graph_prompt.py > .scratch/p12b/prompt.md`, then read that prompt. It is the directive and lists the spec and context docs.
2. Read the spec in full. Its §1 ladder and §4 invariants bind.
3. Memories: `llm-failure-handling-policy`, `context-management-not-built-independent`, `harness-auto-updates-must-not-break-gates`.

## Dan's binding decisions

- D-FAIL-1/2 (2026-09-16): the full text is at the end of `handoffs/2026-09-16-graph-p12a-s3-design.md`.
- **2026-09-17:**
  - only `bad_output` failures use up the 2 retries; `blocked` failures switch model and never use one;
  - retry 1 uses the same model in a fresh session; retry 2 uses another equal-or-better model, or the same one if none exists;
  - a step-down needs both the ROADMAP flag `fallback: step_down` and a controller check (a terminal gate, authoritative specs, full suite forced);
  - the free-mode diagnoser uses **free models only**: the hybrid overlay is not consulted, and with no stronger free route the task parks.

## Suggested session split (the spec is large)

- **S1:** F0, F1, F2, F3 (classification and record). One commit per item or per pair.
- **S2:** F4, F5 (the blocked branch).
- **S3:** F6, F7, F8 (the bad-output branch and dead writers), then the close-out.

Write a design contract for each session in `handoffs/` before coding, as P12A S2/S3 did. Measure context between sessions.

## Invariants

- There is one dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`). There is one fresh-vs-resume decision (`routing.session_decision`). There is no context rotation. The Context Gate is NOT built.
- New transitions run only when `routing is not None`. With `compile_workflow` called without `escalation`, output stays byte-identical (`tests/workflows/test_compatibility.py`).
- Keep `Usage.pools` out of `to_usage_json`. Keep `Capabilities.sandbox=False` for agy; `--mode accept-edits` is uncharacterised, and whether to probe it is Dan's call. Don't flip `engineering.runner`, and don't touch the `P12A_*` spec.

## Coordination

- `STATE.yaml` and `handoffs/` are git-ignored: edit STATE with targeted replacements only. Never touch `AGENTS.md` or `GEMINI.md`. Scratch files go in `.scratch/`, not `/tmp`.
- A PreToolUse hook rejects Bash text containing the privileged word (s-u-d-o).
- pytest's summary line is suppressed: read the counts off the junit root `testsuite`.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## In scope / Out of scope

- **In:** F0–F8 with their §3 acceptance tests; closing the 3 P12B threads; recording P12B in STATE `completed_phases`/`phase_records` at close-out.
- **Out:** P12; the Context Gate; the agy probe (unless Dan approves it); flipping `legacy`.

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # must exit 0
```

Report:
- the gate and full-suite junit counts;
- `owned_threads(state, "P12B")` (must be 0 at close-out);
- the commit hashes.

For Opus 5, prepare a handoff at 160K tokens and start fresh by 200K. The next handoff must be a self-contained file in `handoffs/`.
