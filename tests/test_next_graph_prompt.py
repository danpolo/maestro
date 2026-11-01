"""scripts/next_graph_prompt.py: phase order, and open threads routed to their owning phase."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "next_graph_prompt.py"


@pytest.fixture(scope="module")
def ngp():
    spec = importlib.util.spec_from_file_location("next_graph_prompt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: docs/graph-engineering/ is gitignored, so a clean checkout (a self-update version
#: worktree) has no phase specs; the spec-file checks only mean something where they exist.
needs_specs = pytest.mark.skipif(
    not (ROOT / "docs" / "graph-engineering" / "specs" / "phases").is_dir(),
    reason="phase specs are gitignored and absent from this checkout",
)


def _state(**threads) -> dict:
    return {"current_phase": "P07B", "status": "not_started", "completed_phases": [], **threads}


def test_wiring_phase_sits_between_p07b_and_p12(ngp):
    seq = ngp.PHASE_SEQUENCE
    assert seq[-6:] == ["P10B", "P07B", "P12A", "P12B", "P12", "P13"]
    assert set(seq) == set(ngp.ALL_PHASES) == set(ngp.CONTEXT_ROUTING)


@needs_specs
def test_every_routed_phase_has_a_spec_file(ngp):
    for phase in ngp.CONTEXT_ROUTING:
        assert ngp.find_phase_spec_file(phase) is not None, phase


def test_directive_lists_only_threads_the_phase_owns(ngp):
    state = _state(
        _p10_open_threads=[
            {"owner": "P12A", "thread": "wire select_affected_checks"},
            {"owner": "P12", "thread": "qualify something"},
            {"disposition": "accepted", "thread": "design note", "note": "by design"},
        ],
        _p09_open_threads=[{"owner": "P12A", "thread": "wire build_context_manifest"}],
    )
    prompt = ngp.construct_prompt(state, "P12A")
    assert "wire select_affected_checks" in prompt
    assert "wire build_context_manifest" in prompt
    assert "_p10_open_threads #1" in prompt
    assert "qualify something" not in prompt
    assert "design note" not in prompt
    assert "close or re-own every thread" in prompt


def test_phase_without_threads_says_so(ngp):
    prompt = ngp.construct_prompt(_state(), "P12")
    assert "No open threads are owned by this phase" in prompt


def test_unstructured_threads_are_flagged(ngp):
    state = _state(_p99_open_threads=["a bare string", {"thread": "no owner"},
                                      {"owner": "P12", "disposition": "closed", "thread": "both"},
                                      {"owner": "P12", "thread": "fine"}])
    bad = ngp.unstructured_threads(state)
    assert [ref for ref, _ in bad] == ["_p99_open_threads #1", "_p99_open_threads #2",
                                       "_p99_open_threads #3"]
    assert "UNSTRUCTURED OPEN THREADS" in ngp.construct_prompt(state, "P12")


def test_next_phase_after_p07b_is_the_wiring_phase(ngp):
    prompt = ngp.construct_prompt(_state(), "P07B")
    assert "Set `current_phase: P12A`" in prompt


def test_next_phase_after_the_wiring_phase_is_failure_handling(ngp):
    prompt = ngp.construct_prompt(_state(), "P12A")
    assert "Set `current_phase: P12B`" in prompt


def test_live_state_threads_are_all_structured(ngp):
    if not ngp.DEFAULT_STATE_FILE.exists():
        pytest.skip("STATE.yaml is untracked and absent in this checkout")
    state = yaml.safe_load(ngp.DEFAULT_STATE_FILE.read_text(encoding="utf-8"))
    assert ngp.unstructured_threads(state) == []


@pytest.mark.parametrize("phase", ["P07B", "P12A", "P12B", "P12"])
@needs_specs
def test_remaining_phases_point_at_the_context_gate_constraints(ngp, phase):
    assert ngp.FORWARD_COMPAT_HEADING in ngp.find_phase_spec_file(phase).read_text(encoding="utf-8")
    assert "Forward compatibility" in ngp.construct_prompt(_state(), phase)


def test_phases_without_the_section_get_no_pointer(ngp):
    assert "Forward compatibility" not in ngp.construct_prompt(_state(), "P10")
