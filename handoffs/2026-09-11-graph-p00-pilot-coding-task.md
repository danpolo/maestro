# Handoff — P00 checklist item 4, second half: the DuetFlow coding task

Paste into a fresh Claude Code session in `/home/dan/projects/maestro`.

---

## Goal

Run **one real coding task** on DuetFlow under baseline instrumentation and retain its raw
per-task outcomes. That is all that is left of Phase P00. The setup rehearsal is done and
committed; the thirdparty gate is green.

**Read first, in this order, and nothing else yet:**

1. `artifacts/graph-engineering/b01-duetflow-pilot.json` — the rehearsal's raw record. Its
   `human_interventions` and `findings` are B01 evidence, not background.
2. `docs/graph-engineering/STATE.yaml`, `operator_decisions_2026_09_11_pilot`.
3. `/home/dan/projects/duetflow/docs/ROADMAP.md`, the `01-auth` block only.

Do **not** read `maestro/orchestrator.py`, and do not re-read `docs/PLAN.md` in full until you
need a specific section — the task brief and the ADRs carry what you need.

## The task, and why you may not change it

Dan chose **`01-auth`** over the recommended `04-scoring`. It is `mode: needs-dan`: it needs the
Client ID and secret for the registered Development Mode Spotify app and two interactive consent
flows. The recommendation is on the record and was overridden; **do not re-optimise back to a
cheap autonomous task** because it would be easier to measure. The choice of what B01 measures
is his.

Done when (verbatim from `docs/PLAN.md` §7): *"Both accounts are authorized; tokens survive a
reboot; `duetflow status` prints two expiry countdowns."*

Two operator notes bind this task, both already quoted in the task brief:

- *"tokens should never be encoded in python files like auth.py but rather in config/env files
  in the relevant place."* Two `deny_list_extra` regexes enforce it against the diff.
- *"the final product playlist can be fetched using the spotify api for testing and gating."*
  Not this task, but it is the named path to a real gate — do not let it get lost.

## What you need from Dan before any code can be verified

Ask for these **in one question, early**, and say plainly that the task cannot reach its "Done
when" without them: the Spotify app's Client ID and secret, and his willingness to walk two
consent flows (his own, and the partner's, on the partner's device). `auth.py` is in
`risky_set`, so the merge goes through a proof review either way.

## In scope

- Build `01-auth` on DuetFlow: `config.py`, `auth.py`, the `accounts` table, `duetflow status`.
  Follow `docs/PLAN.md` §2's module boundaries and §3's schema exactly; ADR-0001 is the why.
- Instrument it. Retain **every** agent call, rework attempt and human intervention verbatim,
  and append them to `artifacts/graph-engineering/b01-duetflow-pilot.json` as a
  `first_coding_task` section beside the existing `setup_rehearsal` record.
- Then close P00: update `docs/PROGRESS.md` and `docs/graph-engineering/STATE.yaml`, set
  `status: completed` and `completed_phases: [P00]`.

## Out of scope

- Any P01+ work. Do not start the control store.
- Fixing findings F2–F5 in `b01-duetflow-pilot.json`, or the three pinned baseline findings.
  P00 characterises; later phases change.
- Any DuetFlow phase past `01-auth`.
- `tests/test_limits.py::test_parses_the_real_claude_table` — pre-existing, unrelated.

## Coordination

- Branch `feat/graph-engineering-foundation` in maestro; `main` in DuetFlow (no remote, merges
  stay local — a recorded finding, not a thing to fix). `docs/PROGRESS.md` and
  `docs/graph-engineering/` are gitignored on the maestro branch and live on `meta`; edit them
  in place and `reconcile_meta` syncs them.
- DuetFlow is a scratch project authorized for free modification. Commit freely. It is at
  `521822b` with maestro fully installed and `doctor` exit 0.
- `~/.claude/skills/maestro-setup/SKILL.md` is outside both repos and untracked. It was fixed
  this session (Step 1a, the Step 6 adapter check, the preamble deny-list instruction).

## Verification — report these numbers back

1. `.venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py` — expect
   **41 passed**, exit 0.
2. `.venv/bin/python scripts/graph_baseline.py --check` — expect `[OK] lanes match`.
3. `.venv/bin/python -m maestro.cli doctor --thirdparty` — must exit **0**.
4. In DuetFlow: `maestro doctor` exit 0, and `duetflow status` printing two real expiry
   countdowns against authorized accounts.
5. The coding task's raw outcome record: number of agent calls, rework attempts, and human
   interventions, each with the verbatim text of the intervention.
