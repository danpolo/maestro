# Handoff — P00 checklist item 4: the DuetFlow scratch pilot

Paste into a fresh Claude Code session in `/home/dan/projects/maestro`.

---

## Goal

Close the last open item of **Phase P00** of the Maestro graph-engineering migration: the
instrumented scratch-project pilot that closes the first half of **B01**. Items 1–3 (fixtures,
lane-capture harness, metrics) are done, verified and committed.

**Read first, in this order, and nothing else yet:**

1. `docs/graph-engineering/STATE.yaml` — especially `operator_decisions_2026_09_11`, which
   carries Dan's three answers verbatim. They are B01 evidence, not preamble.
2. `docs/PROGRESS.md`, entry `2026-09-11 — P00 lane-capture harness and migration baseline`.
3. `docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md` §2, item 4 only.

Do **not** read `maestro/orchestrator.py`. The harness already characterises it, and it will
cost you the context this pilot needs.

## What Dan decided, and what it changes

Asked how to resolve DuetFlow's empty `test_command`, Dan rejected all three options offered:

> "that is a problem in the init skill, on a real project what i do is run the grill skill,
> establish some basic doc files and then move on to the init skill which should handle setting
> up all the maestro related things, like the manifest/pyproject.toml for example"

So the work order is: **fix `~/.claude/skills/maestro-setup/SKILL.md` first, then run it.**
The skill owns the span between a grilled project and a working maestro install, and a project
reaching `init` with no manifest is its gap to close. **Do not hand-add a `pyproject.toml` to
DuetFlow** — that hides the gap this answer identified.

The other two: the git remote stays unset (local-only merges are a recorded finding, not a
thing to fix), and the stale INV-12 gate is to be genuinely re-verified and re-pinned.

## In scope

- Fix `maestro-setup` so it creates a root manifest when the project has tests but none.
- Run `maestro-setup` against `/home/dan/projects/duetflow` (invoke the skill; never call
  `maestro init` directly). Retain **every question asked, answer given and correction made,
  verbatim** — a summary does not close B01.
- Seed `TASK-001` in DuetFlow's `docs/ROADMAP.md` and run one real coding task under baseline
  instrumentation, retaining raw per-task outcomes including every rework attempt.
- Re-verify `claude` and `codex` against their installed versions and re-pin
  `maestro/thirdparty.json` until `maestro doctor --thirdparty` exits 0 (A12 blocks P00's close
  on this).
- Write the pilot's raw outcomes to `artifacts/graph-engineering/` and update
  `docs/PROGRESS.md` + `docs/graph-engineering/STATE.yaml`.

## Out of scope

- Any P01+ work. Do not start the control store.
- Fixing the three pinned baseline findings (the script lane's diagnoser call, the risky lane's
  duplicate invocations, the state read/write asymmetry). P00 characterises; later phases change.
- `tests/test_limits.py::test_parses_the_real_claude_table` — pre-existing, unrelated.
- Widening `tests/graph_engineering/conftest.py`'s doubles. A double that changes which branch
  the loop takes is a different program; see `docs/EXECUTION.md`.

## Coordination

- Branch `feat/graph-engineering-foundation`. Code commits land there; `docs/PROGRESS.md` and
  `docs/graph-engineering/` are gitignored on it and live on the `meta` branch —
  `metabranch.reconcile_meta` syncs them, so just edit them in place.
- DuetFlow (`/home/dan/projects/duetflow`) is authorized for free modification: `project.yaml`,
  `.orchestrator/`, `docs/ROADMAP.md`, `docs/PROJECT.md`, and the task's source/test files.
  It is a scratch project — commit freely. Its default branch is `main`, it has 5 commits and a
  `meta` branch. (The spec says "3 commits on master"; that is stale.)
- `~/.claude/skills/maestro-setup/SKILL.md` is outside both repos and not tracked by either.

## Verification — report these numbers back

1. `.venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py` — expect
   **41 passed**, exit 0.
2. `.venv/bin/python scripts/graph_baseline.py --check` — expect `[OK] lanes match`. If a lane
   differs, the loop moved: say what and why before re-recording.
3. `.venv/bin/python -m maestro.cli doctor --thirdparty` — must exit **0**.
4. In DuetFlow after setup: the rendered `adapters/test` runs the real pytest suite and exits 0,
   with no `TODO` in it.
5. The pilot task's raw outcome record: number of agent calls, rework attempts, and human
   interventions, each with the verbatim text of the intervention.
