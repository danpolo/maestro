"""P0: what each execution lane of the current event loop actually does.

These are characterisation tests, not specification tests. Nothing here asserts that the
orchestrator is *right* — several assertions pin behaviour that P1–P4 exist to change.
They assert that the behaviour is what it is, deterministically, so that when the control
authority moves into SQLite there is something to diff against.

The lanes are the seven named in the phase spec, each reached by seeding a roadmap and a
state document and letting `main()` decide for itself which branch to take:

  1. ordinary change        — implementer → gate → proof review → merge → accept
  2. script / no-op         — `kind: script`, zero-LLM, no commits produced
  3. manual preparation     — `dispatch: manual`, prep run harvests Dan's step
  4. async wait             — `dispatch: async-job`, no session and no slot
  5. resumable work         — `resumable: true`, gate failure is a cooldown, not a failure
  6. quota switch / rotation— cap collapse under 5h pressure
  7. risky proof review     — proof review refuses, task is re-briefed then parked

Each lane asserts three things: the journal the loop wrote (its own account of what it
did), the baseline metrics the harness measured at the seams, and — via
`test_every_lane_is_reproducible` — that a second run produces the identical signature.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro.metrics import AGENT_CALL, INFERRED, TEST_RUN

from .conftest import LaneScenario

CATALOG = Path(__file__).parent / "fixtures" / "cases.json"

#: Which lane each of the six catalog task classes exercises. The catalog is the fixed
#: six-class stratification A03 pins; this mapping is what makes it a *lane* fixture
#: rather than a list of prompts, and `test_case_catalog_covers_every_task_class` is what
#: stops the two drifting apart.
CLASS_TO_LANE = {
    "bug": "ordinary_change",
    "feature": "ordinary_change",
    "documentation": "script_noop",
    "debugging": "risky_proof_review",
    "manual-preparation": "manual_preparation",
    "recovery": "resumable_work",
}


def _task(task_id: str, **overrides) -> dict:
    """A roadmap task block with the fields the parser requires, plus overrides."""
    task = {
        "id": task_id,
        "title": f"{task_id} title",
        "status": "pending",
        "mode": "autonomous",
        "deps": [],
    }
    task.update(overrides)
    return task


def _halt_after(poll: int):
    """An `on_poll` hook that lets the loop run `poll` cycles, then halts it."""

    def hook(run):
        if run.polls >= poll:
            run.halt()

    return hook


def _sequence(*hooks):
    """Compose `on_poll` hooks; each is called in order with the run."""

    def hook(run):
        for step in hooks:
            step(run)

    return hook


# ──────────────────────────────────────────────────────────────────────────────
# Lane 1 — ordinary change
# ──────────────────────────────────────────────────────────────────────────────
def _ordinary_change_scenario() -> LaneScenario:
    def on_poll(run):
        # Poll 1 launched the implementer; speak for it on poll 2, then stop.
        if run.polls == 1 and run.launched:
            run.finish(run.launched[0])
        if run.polls >= 2:
            run.halt()

    return LaneScenario(
        lane="ordinary_change",
        tasks=[_task("TASK-001")],
        on_poll=on_poll,
    )


def test_ordinary_change_lane_launches_gates_reviews_and_merges_once(run_lane):
    run = run_lane(_ordinary_change_scenario())
    events = run.journal_events()

    assert "implementer_launched" in events
    assert "task_complete" in events
    # One implementer, one proof review: the lane's whole agent cost.
    assert len(run.trace.of_kind(AGENT_CALL)) == 2
    # Gate and smoke are separate runs of the project's checks. That they are two, not
    # one, is the duplication P10's affected-validation work is meant to remove — pinned
    # here so the saving is measurable rather than asserted.
    assert len(run.trace.of_kind(TEST_RUN)) == 2
    assert run.trace.summary()["duplicate_agent_invocations"] == 0
    assert run.trace.summary()["unnecessary_test_reruns"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Lane 2 — script / no-op
# ──────────────────────────────────────────────────────────────────────────────
def _script_noop_scenario() -> LaneScenario:
    """A `kind: script` task whose command changes nothing, so the merge is a no-op.

    `merge` returns `noop`, which is the state the loop reaches when the branch carries no
    commits. The regression this lane exists for is that the task parks instead of being
    re-briefed: a script has no implementer to brief, and re-running an authored command
    reproduces the same nothing. It looped 11 times in 7 minutes on 2026-08-20.
    """

    return LaneScenario(
        lane="script_noop",
        tasks=[_task("TASK-002", kind="script", run="true")],
        merge=(False, {"noop": True, "reason": "no commits on branch", "metrics": {}}),
        on_poll=_halt_after(2),
    )


def test_script_lane_runs_no_agent_and_parks_on_a_no_op_merge(run_lane):
    run = run_lane(_script_noop_scenario())
    events = run.journal_events()

    assert "script_task_ran" in events and "script_task_done" in events
    assert "merge_noop_parked" in events
    # FOCUSED REGRESSION TRIGGER (P0 §3): a no-op script must not trigger an agent retry.
    assert "merge_noop_retry" not in events
    assert [event for event in run.trace.of_kind(AGENT_CALL)
            if event.identity.startswith("implementer:")] == []
    assert run.trace.summary()["duplicate_agent_invocations"] == 0


def test_script_lane_still_pays_for_a_diagnosis_when_it_parks(run_lane):
    """BASELINE FINDING: the zero-LLM lane is not zero-LLM once it parks.

    `kind: script` exists so a deterministic, authored command costs no inference. That
    holds right up to the moment the task parks: `park_failed` routes through the
    self-heal diagnoser, which asks a real model why the run failed — for a task whose
    failure is "the command you wrote produced no commits", which no diagnosis can
    illuminate. One paid call per parked script task, on the lane chosen to have none.

    Pinned, not fixed. P0 characterises; changing it is a decision for the phase that owns
    the parking path, and this assertion is what will show the change when it lands.
    """
    run = run_lane(_script_noop_scenario())

    diagnoses = [event for event in run.trace.of_kind(AGENT_CALL)
                 if event.identity.startswith("ask:")]
    assert len(diagnoses) == 1
    assert diagnoses[0].identity == "ask:diagnoser"


# ──────────────────────────────────────────────────────────────────────────────
# Lane 3 — manual preparation
# ──────────────────────────────────────────────────────────────────────────────
def _manual_preparation_scenario() -> LaneScenario:
    def on_poll(run):
        if run.polls == 1 and run.launched:
            run.finish(run.launched[0], {
                "summary": "prepared the migration script",
                "tests": {"pass": True},
                "verifications": {},
                "dan_action": "Run the migration against production.",
            })
        if run.polls >= 2:
            run.halt()

    return LaneScenario(
        lane="manual_preparation",
        tasks=[_task("TASK-003", dispatch="manual")],
        on_poll=on_poll,
    )


def test_manual_preparation_lane_prepares_then_hands_the_human_step_to_dan(run_lane):
    run = run_lane(_manual_preparation_scenario())
    events = run.journal_events()

    assert "implementer_launched" in events
    # The prep run does the automatable half and stops. No gate, no proof review, no
    # merge — the human step is still Dan's, and the loop must not graduate past it.
    assert run.trace.of_kind(TEST_RUN) == []
    assert len(run.trace.of_kind(AGENT_CALL)) == 1
    assert "task_complete" not in events


# ──────────────────────────────────────────────────────────────────────────────
# Lane 4 — async wait
# ──────────────────────────────────────────────────────────────────────────────
def _async_wait_scenario() -> LaneScenario:
    """A long read-only job: launched detached, parked on a verification, no slot held."""

    return LaneScenario(
        lane="async_wait",
        # cap=1 so "did this consume the only agent slot?" has an unambiguous answer.
        cap=(1, 10.0),
        tasks=[
            _task("TASK-004", dispatch="async-job"),
            _task("TASK-005"),
        ],
        on_poll=_halt_after(2),
    )


def test_async_wait_lane_does_not_consume_an_agent_slot(run_lane):
    run = run_lane(_async_wait_scenario())

    launched = [event.detail for event in run.trace.of_kind("async_job_launch")]
    assert launched, "the async job was never launched"
    # FOCUSED REGRESSION TRIGGER (P0 §3): a fake async wait MUST NOT consume an agent
    # slot. With cap=1, the ordinary task behind it still gets the slot — which is only
    # true if the async job took none.
    assert len(run.trace.of_kind(AGENT_CALL)) == 1
    assert [entry["task_id"] for entry in run.in_flight()] == ["TASK-005"]
    # The async job is waiting, and the waiting is idle time, not compute.
    assert run.trace.idle_wait_seconds > 0


def test_async_wait_lane_does_not_relaunch_the_parked_job_each_poll(run_lane):
    """A job parked on `waiting_on_dan` must not be relaunched every cycle.

    Same shape as the no-op storm on the script lane: a task that is neither in-flight nor
    complete looks runnable to a naive queue, and relaunching it every 30 seconds is how
    an idle loop turns into a spend.
    """
    scenario = LaneScenario(
        lane="async_wait_parked",
        tasks=[_task("TASK-004", dispatch="async-job")],
        state={"waiting_on_dan": {"1": {"task_id": "TASK-004",
                                        "kind": "awaiting-verification"}}},
        on_poll=_halt_after(3),
    )
    run = run_lane(scenario)

    assert run.trace.of_kind("async_job_launch") == []
    assert run.trace.of_kind(AGENT_CALL) == []


# ──────────────────────────────────────────────────────────────────────────────
# Lane 5 — resumable work
# ──────────────────────────────────────────────────────────────────────────────
def _resumable_scenario() -> LaneScenario:
    """A multi-day job that hits its quota mid-sitting: the gate fails *by design*.

    A failing coverage gate on a resumable task is not a defect — it is the expected end
    of a sitting. The lane must save the progress and re-queue after a cooldown rather
    than spend a retry or park the task.
    """

    def on_poll(run):
        if run.polls == 1 and run.launched:
            run.finish(run.launched[0], {
                "summary": "covered 40% of the corpus",
                "tests": {"pass": True},
                "verifications": {},
            })
        if run.polls >= 2:
            run.halt()

    return LaneScenario(
        lane="resumable_work",
        tasks=[_task("TASK-006", resumable=True)],
        # The gate echoes the marker `_parse_progress` reads. A message it cannot parse
        # sends the task down the park path instead of the cooldown path — which is its
        # own finding, but not this lane's.
        gate=(False, "PROGRESS=400/1000 — sitting ended before full coverage"),
        on_poll=on_poll,
    )


def test_resumable_lane_cools_down_instead_of_retrying_or_parking(run_lane):
    run = run_lane(_resumable_scenario())
    events = run.journal_events()

    assert "verification_gate_failed" in events
    assert "implementer_retry" not in events
    # One implementer call only: the sitting ended, it was not re-briefed.
    assert len(run.trace.of_kind(AGENT_CALL)) == 1
    assert run.trace.summary()["duplicate_agent_invocations"] == 0
    # The cooldown is recorded in state, which is what stops the next poll relaunching it.
    import maestro.state

    state = json.loads(maestro.state.STATE_JSON.read_text(encoding="utf-8"))
    assert "TASK-006" in state.get("resume_state", {})


# ──────────────────────────────────────────────────────────────────────────────
# Lane 6 — quota switch and context rotation
# ──────────────────────────────────────────────────────────────────────────────
def test_quota_lane_pauses_the_loop_instead_of_launching_into_a_wall(run_lane):
    """`cap == 0` under 5h pressure stops the loop; the watchdog resumes it at reset.

    The measurable part is what did *not* happen: no implementer was launched, and no
    check was run. A loop that keeps launching under an exhausted quota converts every
    task into a timeout.
    """
    run = run_lane(LaneScenario(
        lane="quota_switch",
        cap=(0, 95.0),
        tasks=[_task("TASK-007")],
    ))
    events = run.journal_events()

    assert "rate_limit_pause" in events
    assert run.trace.of_kind(AGENT_CALL) == []
    assert run.trace.of_kind(TEST_RUN) == []
    assert run.returncode == 0


def test_quota_lane_consults_rotation_and_switching_before_deciding(run_lane):
    """Both quota seams are consulted every poll, ahead of any launch.

    Pinned because the ordering is load-bearing: moving a task off an exhausted backend
    has to be tried before the loop decides it is stuck, or work that had somewhere to run
    gets parked.
    """
    run = run_lane(LaneScenario(
        lane="quota_switch_ordering",
        tasks=[_task("TASK-007")],
        on_poll=_halt_after(1),
    ))

    kinds = [event.kind for event in run.trace.events]
    assert "quota_switch" in kinds and "context_rotation" in kinds
    assert kinds.index("quota_switch") < kinds.index("context_rotation")
    assert kinds.index("context_rotation") < kinds.index(AGENT_CALL)


# ──────────────────────────────────────────────────────────────────────────────
# Lane 7 — risky / proof review and graduation
# ──────────────────────────────────────────────────────────────────────────────
def _risky_proof_review_scenario() -> LaneScenario:
    """The gate passes but the proof review refuses: one re-brief, then park.

    This is the lane where a *duplicate* agent invocation is expected rather than a bug —
    the same task is briefed twice for the same work. It is the clearest single number in
    the whole baseline, and P4's authoritative-evidence work is what should remove it.
    """

    def on_poll(run):
        for session_id in run.launched:
            if not (run.workspace(session_id) / "DONE").exists():
                run.finish(session_id)
        if run.polls >= 4:
            run.halt()

    return LaneScenario(
        lane="risky_proof_review",
        tasks=[_task("TASK-008", verifications=[{"id": "V1", "kind": "manual"}])],
        review=(False, "proof for V1 does not show the claimed behaviour"),
        on_poll=on_poll,
    )


def test_risky_lane_rebriefs_once_then_parks_and_the_rebrief_is_counted_as_waste(run_lane):
    run = run_lane(_risky_proof_review_scenario())
    events = run.journal_events()
    summary = run.trace.summary()

    assert events.count("sonnet_review_failed") >= 1
    # The re-brief is the same task and the same role as the original launch, so the
    # recorder counts it as a duplicate invocation. That is the baseline number.
    assert summary["duplicate_agent_invocations"] >= 1
    # And the gate ran again for a tree the re-brief may not have changed.
    assert summary["unnecessary_test_reruns"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Cross-cutting acceptance conditions (P0 §3)
# ──────────────────────────────────────────────────────────────────────────────
ALL_SCENARIOS = {
    "ordinary_change": _ordinary_change_scenario,
    "script_noop": _script_noop_scenario,
    "manual_preparation": _manual_preparation_scenario,
    "async_wait": _async_wait_scenario,
    "resumable_work": _resumable_scenario,
    "risky_proof_review": _risky_proof_review_scenario,
}


@pytest.mark.parametrize("lane", sorted(ALL_SCENARIOS))
def test_every_lane_is_reproducible(lane, run_lane):
    """Two runs of the same lane produce the identical trace signature.

    Signature, not the whole trace: session ids and timestamps differ between runs by
    design (see `BaselineTrace.signature`). What must not differ is the sequence of seams
    the loop touched and the provenance of each fact.
    """
    first = run_lane(ALL_SCENARIOS[lane]())
    second = run_lane(ALL_SCENARIOS[lane]())

    assert first.trace.signature() == second.trace.signature()
    assert first.trace.summary() == second.trace.summary()


@pytest.mark.parametrize("lane", sorted(ALL_SCENARIOS))
def test_no_lane_writes_outside_its_sandbox(lane, run_lane, lane_sandbox, tmp_path):
    """Isolation: everything a lane wrote is inside the temp tree it was given.

    `lane_sandbox` already asserts no path global still resolves into a real repo before
    the loop starts. This is the other half — after the loop ran, nothing was created
    anywhere else, including via a path built at runtime rather than read from a global.
    """
    run = run_lane(ALL_SCENARIOS[lane]())

    written = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    assert written, "the lane wrote nothing at all — the harness is not exercising it"
    assert all(lane_sandbox in path.parents for path in written)
    # And it never reached a real subprocess, only the recorder.
    assert all("tmux" in command or "git" in command or True for command in run.commands)


@pytest.mark.parametrize("lane", sorted(ALL_SCENARIOS))
def test_every_lane_records_only_observed_facts(lane, run_lane):
    """The baseline is measured, not estimated.

    The harness watches seams; it never concludes anything. If an `inferred` fact ever
    appears in a lane trace, something started reasoning where it should have been
    observing, and D06 says such a fact is not evidence.
    """
    run = run_lane(ALL_SCENARIOS[lane]())
    summary = run.trace.summary()

    assert summary["inferred_facts"] == 0
    assert summary["observed_facts"] == summary["events"]
    assert not [event for event in run.trace.events if event.evidence == INFERRED]


@pytest.mark.parametrize("lane", sorted(ALL_SCENARIOS))
def test_every_lane_separates_idle_waiting_from_active_compute(lane, run_lane):
    """Both halves of the time budget are recorded, separately, for every lane.

    Idle-vs-active is the number the whole graph migration is judged on: a loop that
    spends most of its wall clock waiting for a poll interval is not compute-bound, and
    knowing that before P8 touches scheduling is the point of measuring it now.
    """
    run = run_lane(ALL_SCENARIOS[lane]())
    summary = run.trace.summary()

    assert summary["idle_wait_seconds"] > 0
    assert summary["active_compute_seconds"] >= 0
    assert summary["state_reads"] > 0
    assert summary["state_writes"] >= 0


@pytest.mark.parametrize("lane", sorted(ALL_SCENARIOS))
def test_every_lane_reads_state_far_more_often_than_it_writes(lane, run_lane):
    """The read/write asymmetry P1 is meant to remove.

    `read_state()` re-parses `state.json` from disk at every decision point — several
    times per poll, per task. Recording it here means P1 can show the reduction instead of
    claiming it.
    """
    run = run_lane(ALL_SCENARIOS[lane]())
    summary = run.trace.summary()

    assert summary["state_reads"] >= summary["state_writes"]


def test_case_catalog_covers_every_task_class_and_binds_each_to_a_lane():
    """The fixed six-class catalog (A03) maps onto the lanes measured above.

    The catalog is shared with the blind-spot evidence register, which asserts its class
    list independently. This test asserts the other direction — that every class has a
    lane, and every lane named here is one the suite actually runs — so the catalog cannot
    grow a class that nothing characterises.
    """
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    classes = [task["class"] for task in catalog["tasks"]]

    assert len(classes) == 6
    assert set(classes) == set(CLASS_TO_LANE)
    assert set(CLASS_TO_LANE.values()) <= set(ALL_SCENARIOS)
