"""The shared backend protocol suite.

`maestro/backends/` is new code, not an extraction, so these tests live here rather than
under `tests/characterization/` — there is no legacy counterpart to characterise.

This file is in two halves:

1. **The base-level tests below** pin the value types and the usage.json normalisation
   that both drivers and `maestro.quota` have to agree on. They import no driver, spend
   no quota and touch no network. Every one is named `test_base_*` so the plan's
   `pytest tests/backends/test_protocol.py -k base` selects exactly this half.

2. **The two-driver parametrised suite**, added at the extension point at the bottom of
   this file, runs one test body against every driver. Optional cases there are gated on
   a **declared capability** — `driver.capabilities().native_resume` — never on a driver
   name. A skip condition that mentions a backend by name is a defect, not a test.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from maestro.backends.base import (
    FIVE_HOUR_MINUTES,
    SEVEN_DAY_MINUTES,
    AgentBackend,
    Capabilities,
    ExitVerdict,
    Handle,
    LaunchSpec,
    Usage,
    WindowUsage,
    from_usage_json,
    to_usage_json,
)

# The exact document the Claude statusline sampler writes and `maestro.quota` reads.
# Shape is pinned by the M2 research pass (finding F4); it is the normalisation target
# for every driver.
F4_USAGE_JSON = {
    "context_used_pct": None,
    "context_total_input_tokens": 0,
    "five_hour": {"used_pct": 95, "resets_at": 1786373400},
    "seven_day": {"used_pct": 91, "resets_at": 1786453200},
    "model": "a-model",
    "updated_at": "2026-08-10T10:36:59Z",
}


def _protocol_attrs(protocol) -> set:
    """The members a Protocol requires. Spelled differently across Python versions."""
    attrs = getattr(protocol, "__protocol_attrs__", None)
    if attrs is None:  # Python < 3.12
        import typing

        attrs = typing._get_protocol_attrs(protocol)
    return set(attrs)


class _StubBackend:
    """A minimal conforming driver. Executes nothing; exists to test the protocol."""

    name = "stub"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            native_resume=False,
            system_prompt_file=False,
            usage_telemetry=False,
            sandbox=False,
        )

    def launch(self, spec: LaunchSpec) -> Handle:
        return Handle(
            backend=self.name,
            session_id=spec.session_id,
            native_id=None,
            workspace=spec.workspace,
            window=f"agents:impl-{spec.task_id}",
        )

    def resume(self, handle: Handle, prompt: str) -> Handle:
        return handle

    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict:
        return ExitVerdict(kind="ok" if rc == 0 else "crashed")

    def usage(self):
        return None


# ── value types ──


def test_base_capabilities_declares_the_four_axes():
    caps = Capabilities(
        native_resume=True, system_prompt_file=False, usage_telemetry=True, sandbox=False
    )
    assert caps._fields == (
        "native_resume",
        "system_prompt_file",
        "usage_telemetry",
        "sandbox",
    )
    assert caps.native_resume is True
    assert caps.sandbox is False


def test_base_launch_spec_is_frozen_with_optional_extras(tmp_path):
    spec = LaunchSpec(
        task_id="T-1",
        session_id="s-1",
        model="a-model",
        brief="do the thing",
        workspace=tmp_path / "ws",
        worktree=tmp_path / "wt",
    )
    assert spec.system_prompt_file is None
    assert spec.sandbox is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.model = "other"  # frozen: a spec cannot be mutated after construction


def test_base_handle_carries_the_native_resume_token(tmp_path):
    handle = Handle(
        backend="stub",
        session_id="s-1",
        native_id="0199-abcd",
        workspace=tmp_path,
        window="agents:impl-T-1",
    )
    assert handle.native_id == "0199-abcd"
    assert Handle("stub", "s-1", None, tmp_path, "w").native_id is None


@pytest.mark.parametrize("kind", ["ok", "quota_exhausted", "crashed"])
def test_base_exit_verdict_kinds(kind):
    verdict = ExitVerdict(kind=kind)
    assert verdict.kind == kind
    assert verdict.reset_at is None
    assert verdict.evidence == ""


def test_base_exit_verdict_carries_reset_and_evidence():
    verdict = ExitVerdict(
        kind="quota_exhausted",
        reset_at="2026-08-10T15:30:00Z",
        evidence="you have hit your usage limit",
    )
    assert verdict.reset_at == "2026-08-10T15:30:00Z"
    assert "usage limit" in verdict.evidence


# ── Usage: keyed by window_minutes (G5) ──


def test_base_usage_windows_are_keyed_by_window_minutes():
    usage = Usage(
        windows={
            FIVE_HOUR_MINUTES: WindowUsage(used_pct=95.0, resets_at=1786373400),
            SEVEN_DAY_MINUTES: WindowUsage(used_pct=91.0, resets_at=1786453200),
        }
    )
    assert set(usage.windows) == {300, 10080}
    assert usage.window(300).used_pct == 95.0
    assert usage.window(10080).resets_at == 1786453200


def test_base_named_window_lookups_are_convenience_over_the_minute_keys():
    usage = Usage(windows={FIVE_HOUR_MINUTES: WindowUsage(used_pct=12.0)})
    assert usage.five_hour is usage.window(FIVE_HOUR_MINUTES)
    assert usage.five_hour.used_pct == 12.0


def test_base_named_window_lookup_is_none_when_the_account_has_no_such_window():
    """G5: an account whose only window is weekly must not fake a five-hour one."""
    weekly_only = Usage(windows={SEVEN_DAY_MINUTES: WindowUsage(used_pct=98.0)})
    assert weekly_only.five_hour is None
    assert weekly_only.seven_day.used_pct == 98.0
    assert weekly_only.window(1440) is None


def test_base_usage_defaults_to_no_information():
    usage = Usage()
    assert usage.windows == {}
    assert usage.context_used_pct is None
    assert usage.five_hour is None and usage.seven_day is None
    assert usage.max_used_pct() is None


def test_base_max_used_pct_is_window_agnostic():
    """A threshold must be able to read pressure without knowing a window's name."""
    usage = Usage(
        windows={
            SEVEN_DAY_MINUTES: WindowUsage(used_pct=98.0),
            1440: WindowUsage(used_pct=40.0),
        }
    )
    assert usage.max_used_pct() == 98.0


def test_base_context_used_pct_may_be_none():
    """G6: one backend can only compute it, so nothing may require it."""
    usage = Usage(context_used_pct=None, windows={SEVEN_DAY_MINUTES: WindowUsage(50.0)})
    assert usage.context_used_pct is None
    assert usage.max_used_pct() == 50.0


# ── usage.json normalisation (F4) ──


def test_base_to_usage_json_emits_the_f4_shape():
    usage = Usage(
        context_used_pct=None,
        windows={
            FIVE_HOUR_MINUTES: WindowUsage(used_pct=95.0, resets_at=1786373400),
            SEVEN_DAY_MINUTES: WindowUsage(used_pct=91.0, resets_at=1786453200),
        },
        model="a-model",
        updated_at="2026-08-10T10:36:59Z",
    )
    document = to_usage_json(usage)
    for key, value in F4_USAGE_JSON.items():
        assert document[key] == value, key
    assert json.loads(json.dumps(document)) == document


def test_base_to_usage_json_writes_integer_percentages():
    document = to_usage_json(Usage(windows={FIVE_HOUR_MINUTES: WindowUsage(94.6)}))
    assert document["five_hour"]["used_pct"] == 95
    assert isinstance(document["five_hour"]["used_pct"], int)


def test_base_to_usage_json_nulls_a_window_the_account_does_not_have():
    document = to_usage_json(Usage(windows={SEVEN_DAY_MINUTES: WindowUsage(98.0)}))
    assert document["five_hour"] is None
    assert document["seven_day"]["used_pct"] == 98


def test_base_from_usage_json_reads_the_f4_shape():
    usage = from_usage_json(F4_USAGE_JSON)
    assert usage.five_hour == WindowUsage(used_pct=95.0, resets_at=1786373400)
    assert usage.seven_day == WindowUsage(used_pct=91.0, resets_at=1786453200)
    assert usage.model == "a-model"
    assert usage.updated_at == "2026-08-10T10:36:59Z"
    assert usage.context_used_pct is None


def test_base_usage_json_round_trips():
    usage = Usage(
        context_used_pct=31.0,
        windows={
            FIVE_HOUR_MINUTES: WindowUsage(used_pct=95.0, resets_at=1786373400),
            SEVEN_DAY_MINUTES: WindowUsage(used_pct=91.0, resets_at=1786453200),
        },
        model="a-model",
        updated_at="2026-08-10T10:36:59Z",
        context_total_input_tokens=4242,
    )
    assert from_usage_json(to_usage_json(usage)) == usage


def test_base_usage_json_round_trips_a_window_with_no_name():
    """G5 again: a window keyed only by its duration must survive a write/read cycle."""
    usage = Usage(windows={1440: WindowUsage(used_pct=42.0, resets_at=1786453200)})
    restored = from_usage_json(to_usage_json(usage))
    assert restored.windows == usage.windows
    assert restored.five_hour is None


def test_base_from_usage_json_survives_garbage():
    for document in (None, [], "not a document", 7):
        assert from_usage_json(document) == Usage()
    partial = from_usage_json({"five_hour": "nonsense", "updated_at": 12})
    assert partial.windows == {}
    assert partial.updated_at == ""


def test_base_from_usage_json_ignores_an_empty_window_record():
    assert from_usage_json({"five_hour": {}}).five_hour is None
    assert from_usage_json({"five_hour": {"used_pct": None}}).five_hour is None


def test_base_from_usage_json_coerces_string_numbers():
    usage = from_usage_json({"five_hour": {"used_pct": "95", "resets_at": "1786373400"}})
    assert usage.five_hour == WindowUsage(used_pct=95.0, resets_at=1786373400)


def test_base_usage_json_is_readable_by_the_existing_quota_reader(tmp_path, monkeypatch):
    """The whole point of the F4 shape: `quota.get_effective_cap` must not change."""
    from maestro import quota

    usage_json = tmp_path / "usage.json"
    monkeypatch.setattr(quota, "USAGE_JSON", usage_json)

    usage_json.write_text(json.dumps(to_usage_json(Usage(
        windows={FIVE_HOUR_MINUTES: WindowUsage(used_pct=80.0)}
    ))))
    assert quota.get_effective_cap() == (quota.THROTTLE_75_CAP, 80.0)

    usage_json.write_text(json.dumps(to_usage_json(Usage(
        windows={FIVE_HOUR_MINUTES: WindowUsage(used_pct=95.0)}
    ))))
    assert quota.get_effective_cap() == (0, 95.0)


# ── the protocol itself ──


def test_base_agent_backend_is_runtime_checkable():
    assert isinstance(_StubBackend(), AgentBackend)


def test_base_non_conforming_object_is_not_an_agent_backend():
    class MissingParseExit:
        name = "partial"

        def capabilities(self):
            return Capabilities()

        def launch(self, spec):
            raise NotImplementedError

        def resume(self, handle, prompt):
            raise NotImplementedError

        def usage(self):
            return None

    assert not isinstance(MissingParseExit(), AgentBackend)


def test_base_protocol_members_are_the_documented_six():
    members = {
        "name",
        "capabilities",
        "launch",
        "resume",
        "parse_exit",
        "usage",
    }
    assert _protocol_attrs(AgentBackend) == members


def test_base_stub_driver_round_trips_a_launch(tmp_path):
    """Sanity: the protocol's own types compose without any backend-specific knowledge."""
    driver = _StubBackend()
    spec = LaunchSpec(
        task_id="T-1",
        session_id="s-1",
        model="a-model",
        brief="brief",
        workspace=tmp_path / "ws",
        worktree=tmp_path / "wt",
    )
    handle = driver.launch(spec)
    assert isinstance(handle, Handle)
    assert handle.backend == driver.name
    assert driver.parse_exit(0, "").kind == "ok"
    assert driver.parse_exit(1, "").kind == "crashed"
    assert driver.usage() is None


# ─────────────────────────────────────────────────────────────────────────────
# EXTENSION POINT — the two-driver parametrised suite goes below this line.
#
# Add a fixture parametrised over every real driver and run one test body against
# all of them (plan Task 7). Rules for whoever writes it:
#   * gate optional cases on a declared capability, e.g.
#         pytest.mark.skipif(not caps.native_resume, reason="no native resume")
#     never on a driver's name;
#   * monkeypatch every subprocess call — a test that actually launches an agent,
#     spends quota or reaches the network is a defect;
#   * leave the `test_base_*` half above alone: it is driver-free on purpose.
# ─────────────────────────────────────────────────────────────────────────────
