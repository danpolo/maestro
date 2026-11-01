"""The third-party verification gate (B06 + B17).

Two tiers, deliberately separated, because they fail for different reasons and must not
share an exit code:

* **Structural invariants** — computed from the repository alone (the register, the
  backend registry, `pyproject.toml`). A failure here is a repo defect: something entered
  the tree without a verification row, or a row claims something it did not evidence.
  These run in the ordinary suite and must never depend on what is installed on the host.
* **Host status** — computed by comparing the register against the versions actually
  installed. That is a property of *this machine at this moment*, so it belongs to
  `maestro doctor --thirdparty`, not to the suite. The tests below exercise that logic
  through injected probes and never look at the real host.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro import thirdparty
from maestro.backends.registry import BACKENDS


REPO = Path(__file__).resolve().parents[1]


def _record(**overrides) -> dict:
    base = {
        "subject": "example",
        "kind": "dependency",
        "scope": "runtime",
        "verified_version": "1.0.0",
        "verified_on": "2026-09-08",
        "verified_by": "test",
        "evidence": ["artifacts/example.json"],
        "free_use": {
            "license": "MIT",
            "source": "installed distribution metadata",
            "no_paid_tier_for_normal_operation": True,
        },
        "findings": {},
        "open_questions": [],
        "notes": "",
    }
    base.update(overrides)
    return base


def _register(*records: dict) -> dict:
    return {"schema_version": 1, "records": list(records)}


# ── schema: a row may not claim more than it evidenced ──


def test_a_verified_row_without_evidence_is_rejected():
    with pytest.raises(ValueError, match="evidence"):
        thirdparty.parse_register(_register(_record(evidence=[])))


def test_a_row_with_no_verified_version_may_omit_evidence():
    """A placeholder row is how a *candidate* is tracked before anyone verifies it.

    It is the honest representation of "we know this exists and we have checked nothing",
    and `status_for` turns it into `unverified`, which blocks. Requiring evidence here
    would push the candidate out of the register entirely — the one place it must not go.
    """
    register = thirdparty.parse_register(_register(_record(verified_version="", evidence=[])))

    assert register["example"].verified_version == ""


def test_a_dependency_row_must_carry_a_licence_and_where_it_came_from():
    with pytest.raises(ValueError, match="free_use"):
        thirdparty.parse_register(_register(_record(free_use=None)))
    with pytest.raises(ValueError, match="licen"):
        thirdparty.parse_register(
            _register(_record(free_use={"license": "", "source": "x",
                                        "no_paid_tier_for_normal_operation": True}))
        )


def test_a_backend_row_must_say_which_controls_were_actually_observed():
    with pytest.raises(ValueError, match="controls_observed"):
        thirdparty.parse_register(
            _register(_record(subject="b", kind="backend", free_use=None, findings={}))
        )


def test_duplicate_subjects_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        thirdparty.parse_register(_register(_record(), _record()))


def test_a_malformed_register_raises_rather_than_degrading_to_nothing_to_check():
    """Unlike `maestro.limits`, which tolerates an operator's unparseable table.

    This file ships *inside the package*: an unreadable one is a defect in this
    repository, and the failure mode of "degrade to empty" is a gate that silently passes
    everything — the exact outcome the gate exists to prevent.
    """
    with pytest.raises(ValueError, match="schema"):
        thirdparty.parse_register({"schema_version": 99, "records": []})


# ── status: what the host says about a verified row ──


def test_an_unchanged_installed_version_is_verified():
    record = thirdparty.parse_register(_register(_record()))["example"]

    assert thirdparty.status_for(record, "1.0.0").status == "verified"


def test_a_changed_installed_version_drifts_and_blocks():
    """The whole point of B06/B17: a fact about version X says nothing about version Y."""
    record = thirdparty.parse_register(_register(_record()))["example"]
    status = thirdparty.status_for(record, "1.0.1")

    assert status.status == "drifted"
    assert status.blocking
    assert "1.0.0" in status.detail and "1.0.1" in status.detail


def _backend(**overrides) -> dict:
    fields = dict(
        subject="cli", kind="backend", free_use=None,
        findings={"controls_observed": ["--model", "resume"]},
    )
    fields.update(overrides)
    return _record(**fields)


def test_an_auto_updated_backend_whose_controls_are_all_still_there_is_reobserved():
    """Agent CLIs update constantly; a bump that changed nothing recorded must not block."""
    record = thirdparty.parse_register(_register(_backend()))["cli"]
    status = thirdparty.status_for(record, "1.0.1", help_text="usage: --model M\n  resume")

    assert status.status == "reobserved"
    assert not status.blocking
    assert "1.0.1" in status.detail and "1.0.0" in status.detail


def test_an_auto_updated_backend_that_lost_a_recorded_control_drifts_and_names_it():
    record = thirdparty.parse_register(_register(_backend()))["cli"]
    status = thirdparty.status_for(record, "1.0.1", help_text="usage: --model M")

    assert status.status == "drifted" and status.blocking
    assert "resume" in status.detail


def test_an_auto_updated_backend_whose_help_cannot_be_read_still_drifts():
    record = thirdparty.parse_register(_register(_backend()))["cli"]
    assert thirdparty.status_for(record, "1.0.1", help_text=None).status == "drifted"


def test_a_dependency_version_bump_is_never_reobserved_from_help_text():
    record = thirdparty.parse_register(_register(_record()))["example"]
    status = thirdparty.status_for(record, "1.0.1", help_text="anything at all")
    assert status.status == "drifted" and status.blocking


def test_audit_reads_help_only_for_a_backend_whose_version_moved():
    asked: list[str] = []
    register = thirdparty.parse_register(
        _register(_backend(), _backend(subject="same"))
    )
    statuses = thirdparty.audit(
        register=register,
        required={"cli": "backend", "same": "backend"},
        binary_version=lambda name: "1.0.1" if name == "cli" else "1.0.0",
        distribution_version=lambda name: None,
        binary_help=lambda name: asked.append(name) or "--model resume",
    )

    assert asked == ["cli"]
    assert {s.subject: s.status for s in statuses} == {"cli": "reobserved", "same": "verified"}


def test_a_never_verified_row_blocks_even_when_installed():
    record = thirdparty.parse_register(_register(_record(verified_version="", evidence=[])))["example"]
    status = thirdparty.status_for(record, "1.0.0")

    assert status.status == "unverified"
    assert status.blocking


def test_a_candidate_nobody_has_adopted_is_tracked_rather_than_blocking():
    """A shortlisted provider must be carried *before* adoption without failing the gate.

    Blocking on it would push it out of the register, which is the one place it has to be
    for the gate to catch the moment someone enables it.
    """
    record = thirdparty.parse_register(_register(_record(verified_version="", evidence=[])))["example"]
    status = thirdparty.status_for(record, None, in_use=False)

    assert status.status == "candidate"
    assert not status.blocking


def test_drift_in_something_maestro_does_not_drive_is_reported_but_not_blocking():
    record = thirdparty.parse_register(_register(_record()))["example"]
    status = thirdparty.status_for(record, "2.0.0", in_use=False)

    assert status.status == "drifted"
    assert not status.blocking


def test_a_subject_missing_from_this_host_is_reported_but_does_not_block():
    """A register row is a fact about software, not a requirement that it be installed.

    Antigravity is carried as a candidate; a machine without it is not a defect.
    """
    record = thirdparty.parse_register(_register(_record()))["example"]
    status = thirdparty.status_for(record, None)

    assert status.status == "absent"
    assert not status.blocking


def test_audit_reports_a_registered_backend_that_has_no_row_at_all():
    """The failure the gate exists for: a driver lands, nobody verifies the CLI."""
    statuses = thirdparty.audit(
        register=thirdparty.parse_register(_register()),
        required={"newbackend": "backend"},
        binary_version=lambda name: "9.9.9",
        distribution_version=lambda name: None,
    )

    missing = [s for s in statuses if s.subject == "newbackend"]
    assert missing and missing[0].status == "unverified" and missing[0].blocking


def test_audit_does_not_probe_the_host_for_a_subject_it_was_not_asked_about():
    probed: list[str] = []
    thirdparty.audit(
        register=thirdparty.parse_register(_register(_record())),
        required={"example": "dependency"},
        binary_version=lambda name: probed.append(name) or None,
        distribution_version=lambda name: probed.append(name) or "1.0.0",
    )

    assert probed == ["example"]


# ── coverage: the repository invariants ──


def test_every_registered_backend_has_a_verification_row():
    register = thirdparty.load_register()

    missing = sorted({ref.binary for ref in BACKENDS.values()} - set(register))
    assert not missing, (
        f"backend(s) {missing} are registered but carry no third-party verification row. "
        "Add one to maestro/thirdparty.json recording the exact CLI version verified, "
        "what was observed, and where the evidence lives (B06)."
    )


def test_every_declared_python_dependency_has_a_verification_row():
    register = thirdparty.load_register()
    declared = thirdparty.declared_dependencies(REPO / "pyproject.toml")

    missing = sorted(set(declared) - set(register))
    assert not missing, (
        f"distribution(s) {missing} are declared in pyproject.toml but carry no "
        "third-party verification row. Record the exact version, its licence and where "
        "that licence was read from before Maestro depends on it (B17)."
    )


def test_declared_dependencies_reads_runtime_and_optional_extras():
    parsed = thirdparty.declared_dependencies(REPO / "pyproject.toml")

    assert parsed["pyyaml"] == "runtime"
    assert parsed["pytest"] == "dev"


def test_declared_dependency_names_are_canonicalised():
    """`PyYAML`, `pyyaml` and `py_yaml` are one distribution (PEP 503)."""
    assert thirdparty.canonical_name("PyYAML") == "pyyaml"
    assert thirdparty.canonical_name("python_dotenv") == "python-dotenv"


def test_the_shipped_register_parses_and_covers_its_own_claims():
    register = thirdparty.load_register()

    assert register, "the shipped register must not be empty"
    for record in register.values():
        if record.free_use is not None:
            assert record.free_use["source"], f"{record.subject}: licence source is unstated"
        if record.verified_version:
            assert record.evidence, f"{record.subject}: verified without evidence"


def test_the_shipped_register_is_valid_json_with_sorted_subjects():
    """Sorted so two sessions adding rows produce a reviewable diff, not a merge fight."""
    document = json.loads(thirdparty.REGISTER_JSON.read_text(encoding="utf-8"))
    subjects = [row["subject"] for row in document["records"]]

    assert subjects == sorted(subjects)
