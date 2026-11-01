"""`[P10]`: affected-check selection, and why it can never narrow past what is known.

The three acceptance conditions each get a section: an internal behaviour change under an
unchanged public signature still invalidates evidence; unknown dependency coverage falls
back to the full required suite; shared config, fixtures and lockfiles expand to it too.
Everything else here pins the E3 shadow → authoritative graduation rule.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from maestro import validation
from maestro.control.store import ControlStore
from maestro.validation import (
    CheckRunner,
    EvidenceRegistry,
    ShadowComparison,
    VerificationSpec,
    changed_paths,
    path_matches,
    qualify_affected,
    select_affected_checks,
)


def check(check_id: str, inputs=(), *, completeness="exact", cache="exact_snapshot_only", **kw):
    return VerificationSpec(
        check_id=check_id,
        argv_or_adapter_ref=(f"echo {check_id}",),
        shell=True,
        acceptance_criterion_ids=(check_id,),
        input_dependencies=tuple(inputs),
        dependency_completeness=completeness,
        cache_policy=cache,
        **kw,
    )


@pytest.fixture
def suite():
    return [
        check("unit_api", ["src/api/**/*.py", "tests/api/**"]),
        check("unit_db", ["src/db/**/*.py", "tests/db/**"]),
    ]


# ── selection by exact input dependencies ───────────────────────────────────────────


def test_authoritative_selection_runs_only_checks_whose_inputs_changed(suite):
    sel = select_affected_checks(suite, ["src/api/routes.py"], mode="authoritative")
    assert sel.selected == ("unit_api",)
    assert sel.skipped == ("unit_db",)
    assert not sel.full_suite
    assert "src/api/routes.py" in sel.reasons["unit_api"]
    assert sel.reasons["unit_db"] == "no declared input changed"


def test_shadow_mode_runs_everything_but_records_the_affected_subset(suite):
    sel = select_affected_checks(suite, ["src/api/routes.py"], mode="shadow")
    assert sel.selected == ("unit_api", "unit_db")
    assert sel.affected == ("unit_api",)
    assert sel.skipped == ()


def test_shadow_is_the_default_mode(suite):
    assert select_affected_checks(suite, ["src/api/routes.py"]).mode == "shadow"


def test_a_non_cacheable_check_is_always_selected(suite):
    smoke = check("staging_smoke", ["deploy/**"], cache="never")
    sel = select_affected_checks([*suite, smoke], ["src/api/x.py"], mode="authoritative")
    assert set(sel.selected) == {"unit_api", "staging_smoke"}
    assert sel.reasons["staging_smoke"] == "not cacheable: always re-run"


def test_nothing_changed_selects_only_the_uncacheable(suite):
    sel = select_affected_checks(suite, [], mode="authoritative")
    assert sel.selected == ()


def test_a_rename_touches_checks_reading_either_side(suite):
    sel = select_affected_checks(
        suite, ["src/api/old.py", "src/db/new.py"], mode="authoritative"
    )
    assert set(sel.selected) == {"unit_api", "unit_db"}


# ── expansion to the full suite ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "requirements-dev.txt",
        "package-lock.json",
        "tests/conftest.py",
        "tests/api/conftest.py",
        "tests/fixtures/users.json",
        "src/db/fixtures/seed.sql",
    ],
)
def test_shared_config_fixtures_and_lockfiles_expand_to_the_full_suite(suite, path):
    sel = select_affected_checks(suite, [path, "src/api/x.py"], mode="authoritative")
    assert sel.full_suite
    assert sel.selected == ("unit_api", "unit_db")
    assert path in sel.expansion_reason


def test_unknown_change_set_expands_to_the_full_suite(suite):
    sel = select_affected_checks(suite, None, mode="authoritative")
    assert sel.full_suite
    assert sel.selected == ("unit_api", "unit_db")
    assert sel.expansion_reason == "change set unknown"


def test_unknown_dependency_coverage_falls_back_to_the_full_required_suite(suite):
    """Acceptance condition 2. One check that cannot say what it reads makes every
    narrowing a guess — including the narrowing of the *other* checks' selection."""
    vague = check("integration", (), completeness="unknown", cache="never")
    sel = select_affected_checks([*suite, vague], ["src/api/x.py"], mode="authoritative")
    assert sel.full_suite
    assert sel.selected == ("unit_api", "unit_db", "integration")
    assert "integration" in sel.expansion_reason


def test_an_optional_check_with_unknown_coverage_does_not_expand(suite):
    vague = check("lint_extra", (), completeness="unknown", cache="never", required=False)
    sel = select_affected_checks([*suite, vague], ["src/api/x.py"], mode="authoritative")
    assert not sel.full_suite
    assert set(sel.selected) == {"unit_api", "lint_extra"}


def test_legacy_roadmap_checks_without_inputs_always_expand():
    legacy = VerificationSpec.from_roadmap({"id": "V1", "kind": "auto", "cmd": "pytest"})
    sel = select_affected_checks([legacy], ["README.md"], mode="authoritative")
    assert sel.full_suite


# ── behaviour change with an unchanged public signature (acceptance condition 1) ────────


@pytest.fixture
def store(tmp_path):
    (tmp_path / ".orchestrator").mkdir()
    s = ControlStore.open_controller(tmp_path / ".orchestrator" / "control.sqlite3")
    yield s
    s.close()


class CountingRunner:
    def __init__(self):
        self.calls = 0

    def __call__(self, command, **kwargs):
        self.calls += 1
        return subprocess.CompletedProcess(command, 0, "ok\n", "")


def test_body_change_under_an_unchanged_signature_invalidates_evidence(tmp_path, store):
    tree = tmp_path / "wt"
    (tree / "src" / "api").mkdir(parents=True)
    module = tree / "src" / "api" / "pricing.py"
    module.write_text("def price(qty: int) -> int:\n    return qty * 10\n")
    spec = check("unit_api", ["src/api/**/*.py"])

    fake = CountingRunner()
    runner = CheckRunner(registry=EvidenceRegistry(store=store), runner=fake)
    first = runner.run(spec, tree, attempt_id="a1", snapshot="snap")
    again = runner.run(spec, tree, attempt_id="a2", snapshot="snap")
    assert fake.calls == 1 and again.reused_from == first.evidence_id

    before = validation.input_fingerprint(tree, spec.input_dependencies)
    module.write_text("def price(qty: int) -> int:\n    return qty * 11\n")
    after = validation.input_fingerprint(tree, spec.input_dependencies)
    assert before != after, "the signature is unchanged; the bytes are not"

    sel = select_affected_checks([spec], ["src/api/pricing.py"], mode="authoritative")
    assert sel.selected == ("unit_api",)

    # Same snapshot id on purpose: the input fingerprint alone must refuse the reuse.
    third = runner.run(spec, tree, attempt_id="a3", snapshot="snap")
    assert fake.calls == 2
    assert third.reused_from is None

    moved = validation.invalidate_affected_evidence(store, sel)
    assert moved >= 1
    stale_fp = validation.Fingerprints(
        source_snapshot_id="snap",
        command_hash=first.command_hash,
        input_fingerprint=before,
        environment_fingerprint=first.environment_fingerprint,
    )
    assert runner.registry.reusable(spec, stale_fp) is None


# ── change sets from git ─────────────────────────────────────────────────────────────


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_changed_paths_reads_the_diff_and_reports_unknown_as_none(tmp_path):
    repo = tmp_path / "g"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.py").write_text("y = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "a.py").write_text("x = 2\n")
    _git(repo, "mv", "b.py", "c.py")
    _git(repo, "commit", "-qam", "change")

    assert changed_paths(repo, base) == ("a.py", "b.py", "c.py")
    assert changed_paths(repo, "0" * 40) is None


# ── glob semantics ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("src/a.py", "src/**/*.py", True),
        ("src/pkg/deep/a.py", "src/**/*.py", True),
        ("src/a.txt", "src/**/*.py", False),
        ("src/pkg/a.py", "src", True),
        ("srcx/a.py", "src", False),
        ("./src/a.py", "src/*.py", True),
        (".github/workflows/ci.yml", ".github/**", True),
        ("a/b.py", "*.py", False),
    ],
)
def test_path_matches(path, pattern, expected):
    assert path_matches(path, [pattern]) is expected


# ── E3 graduation ────────────────────────────────────────────────────────────────────


def _comparison(suite, changed, outcomes):
    return ShadowComparison(select_affected_checks(suite, changed, mode="shadow"), outcomes)


def test_a_skipped_check_that_failed_is_a_discrepancy(suite):
    comp = _comparison(suite, ["src/api/x.py"], {"unit_api": "passed", "unit_db": "failed"})
    assert comp.discrepancies == ("unit_db",)


def test_a_selected_check_that_failed_is_not_a_discrepancy(suite):
    comp = _comparison(suite, ["src/api/x.py"], {"unit_api": "failed", "unit_db": "passed"})
    assert comp.discrepancies == ()


def test_graduation_needs_enough_clean_runs_and_no_discrepancy_ever(suite):
    clean = _comparison(suite, ["src/api/x.py"], {"unit_api": "passed", "unit_db": "passed"})
    assert qualify_affected([clean] * 3, minimum_runs=5)[0] == "shadow"
    assert qualify_affected([clean] * 5, minimum_runs=5)[0] == "authoritative"

    missed = _comparison(suite, ["src/api/x.py"], {"unit_api": "passed"})
    mode, reason = qualify_affected([clean] * 50 + [missed], minimum_runs=5)
    assert mode == "shadow"
    assert "unit_db" in reason


def test_unknown_mode_is_refused(suite):
    with pytest.raises(validation.ValidationError):
        select_affected_checks(suite, [], mode="optimistic")


# ── P12A W5/W6: the selection travels, the comparison persists ───────────────────────


def test_a_selection_round_trips_through_its_document(suite):
    sel = select_affected_checks(suite, ["src/api/x.py", "pyproject.toml"], mode="authoritative")
    doc = sel.to_dict()
    assert doc["mode"] == "authoritative" and doc["selected"] == list(sel.selected)
    assert validation.AffectedSelection.from_dict(doc) == sel
    narrow = select_affected_checks(suite, ["src/api/x.py"], mode="authoritative")
    assert validation.AffectedSelection.from_dict(narrow.to_dict()) == narrow


def _row(comp, *, run_id="run-1", node_id="gate", snapshot_id="snap-1", attempt_id="a-1"):
    return comp.to_row(run_id=run_id, node_id=node_id, snapshot_id=snapshot_id,
                       attempt_id=attempt_id, recorded_at="2026-09-16T00:00:00Z")


def test_a_comparison_round_trips_through_its_row(suite):
    comp = _comparison(suite, ["src/api/x.py"], {"unit_api": "passed", "unit_db": "failed"})
    row = _row(comp)
    assert row["run_id"] == "run-1" and row["node_id"] == "gate"
    assert row["snapshot_id"] == "snap-1" and row["attempt_id"] == "a-1"
    assert json.loads(row["discrepancies_json"]) == ["unit_db"]
    back = ShadowComparison.from_row(row)
    assert back == comp
    assert back.discrepancies == ("unit_db",)


def test_stored_comparisons_load_in_insert_order_and_reingest_is_idempotent(suite, store):
    clean = _comparison(suite, ["src/api/x.py"], {"unit_api": "passed", "unit_db": "passed"})
    missed = _comparison(suite, ["src/api/x.py"], {"unit_api": "passed"})
    store.record_shadow_comparison(_row(clean, run_id="run-2"))
    store.record_shadow_comparison(_row(missed, run_id="run-1"))
    store.record_shadow_comparison(_row(missed, run_id="run-1", attempt_id="a-2"))

    assert [r["run_id"] for r in store.shadow_comparisons()] == ["run-2", "run-1"]
    loaded = validation.load_shadow_comparisons(store)
    assert loaded == [clean, missed]
    assert qualify_affected(loaded, minimum_runs=1)[0] == "shadow"


def test_only_the_controller_records_a_comparison(suite, store, tmp_path):
    from maestro.control.store import ControlStoreError

    client = ControlStore.open_client(tmp_path / ".orchestrator" / "control.sqlite3")
    try:
        comp = _comparison(suite, [], {"unit_api": "passed", "unit_db": "passed"})
        with pytest.raises(ControlStoreError):
            client.record_shadow_comparison(_row(comp))
    finally:
        client.close()
