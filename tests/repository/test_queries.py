"""P9: Layer 0/1 queries answer about a commit, degrade visibly, and gate grammars."""
from __future__ import annotations

import json

import pytest

from maestro import thirdparty
from maestro.repository import providers
from maestro.repository.index import StaleSnapshotError, UnknownSnapshotError
from maestro.repository.providers import GRAMMARS, PARSER_SUBJECT, grammar_gate, select_provider
from maestro.repository.queries import RepositoryQueries

from .conftest import commit_all


def _queries(repo, selection, **kw):
    root, _sha = repo
    return RepositoryQueries(root, selection=selection, **kw)


# ── definitions ──


def test_definitions_are_exact_on_a_commit(repo, selection):
    q = _queries(repo, selection)
    result = q.definitions("helper", repo[1])
    assert [(d.path, d.start_line, d.end_line, d.kind) for d in result] == [("pkg/core.py", 1, 2, "function")]
    assert result.freshness == "exact_match"
    assert result.completeness == "complete"
    assert selection.provider.name in result.providers


def test_methods_carry_their_class_in_the_qualname(repo, selection):
    result = _queries(repo, selection).definitions("run", repo[1])
    assert [(d.qualname, d.kind) for d in result] == [("Engine.run", "method")]


def test_a_query_answers_the_commit_not_the_checkout(repo, selection):
    root, sha = repo
    (root / "pkg/core.py").write_text("# helper moved away\n")
    result = _queries(repo, selection).definitions("helper", sha)
    assert [d.start_line for d in result] == [1]


def test_a_branch_name_is_not_a_snapshot(repo, selection):
    with pytest.raises(UnknownSnapshotError):
        _queries(repo, selection).definitions("helper", "main")


def test_an_unknown_commit_is_rejected(repo, selection):
    with pytest.raises(StaleSnapshotError):
        _queries(repo, selection).definitions("helper", "0" * 40)


def test_a_non_identifier_is_refused(repo, selection):
    with pytest.raises(ValueError):
        _queries(repo, selection).definitions("helper; rm", repo[1])


# ── references ──


def test_references_in_skip_the_definition_and_name_their_enclosing_scope(repo, selection):
    result = _queries(repo, selection).references("helper", repo[1], direction="in", depth=1)
    rows = {(r.path, r.line, r.enclosing) for r in result}
    assert ("pkg/core.py", 1, None) not in rows
    assert ("pkg/core.py", 7, "Engine.run") in rows
    assert ("pkg/app.py", 6, "main") in rows
    assert any(r.path == "README.md" for r in result), "non-Python text is searched too"
    assert result.completeness == "partial", "lexical references never claim precision"


def test_references_follow_enclosing_definitions_to_the_requested_depth(repo, selection):
    q = _queries(repo, selection)
    shallow = q.references("helper", repo[1], direction="in", depth=1)
    deep = q.references("helper", repo[1], direction="in", depth=2)
    assert not any(r.path == "cli.py" for r in shallow)
    assert any(r.path == "cli.py" and r.depth == 2 and r.symbol == "main" for r in deep)


def test_references_out_list_what_a_body_calls(repo, selection):
    result = _queries(repo, selection).references("run", repo[1], direction="out")
    assert {r.target for r in result} == {"helper"}


def test_references_reject_an_unknown_direction(repo, selection):
    with pytest.raises(ValueError):
        _queries(repo, selection).references("helper", repo[1], direction="sideways")


def test_a_result_over_the_limit_is_truncated_and_says_so(repo, selection):
    result = _queries(repo, selection, limit=1).references("helper", repo[1])
    assert len(result) == 1 and result.truncated and result.completeness == "partial"


# ── importers / tests_for ──


def test_importers_resolve_absolute_and_relative_imports(repo, selection):
    result = _queries(repo, selection).importers("pkg/core.py", repo[1])
    assert {(r.importer, r.module) for r in result} == {
        ("pkg/app.py", "pkg.core"),
        ("pkg/app.py", "pkg"),
        ("tests/test_core.py", "pkg"),
    }
    assert all(r.resolved_path == "pkg/core.py" for r in result)


def test_importers_of_a_non_python_file_are_unresolved_not_empty_and_complete(repo, selection):
    result = _queries(repo, selection).importers("README.md", repo[1])
    assert len(result) == 0 and result.completeness == "partial" and result.unresolved


def test_tests_for_links_by_import_and_by_name(repo, selection):
    result = _queries(repo, selection).tests_for(["pkg/core.py"], repo[1])
    assert {(t.test_path, t.reason) for t in result} == {("tests/test_core.py", "imports")}


# ── malformed syntax falls back to search/read ──


def _commit_broken(repo):
    root, _ = repo
    (root / "pkg/broken.py").write_text(
        "def before():\n    return 1\n\ndef half_typed(:\n    pass\n\ndef after_error():\n    return before()\n"
    )
    return commit_all(root, "broken")


def test_ast_layer_falls_back_to_lexical_search_on_a_broken_file(repo, ast_only):
    sha = _commit_broken(repo)
    result = _queries(repo, ast_only).definitions("after_error", sha)
    assert [(d.path, d.start_line, d.provider, d.partial) for d in result] == [
        ("pkg/broken.py", 7, "search", True)
    ]
    assert result.completeness == "partial"
    assert any("pkg/broken.py" in note for note in result.unresolved)


def test_tree_sitter_keeps_definitions_around_a_syntax_error(repo, tree_sitter_selection):
    sha = _commit_broken(repo)
    result = _queries(repo, tree_sitter_selection).definitions("after_error", sha)
    assert [(d.start_line, d.end_line, d.provider) for d in result] == [(7, 8, "tree_sitter")]
    assert result.completeness == "partial"


def test_references_out_from_a_broken_file_still_answer(repo, selection):
    sha = _commit_broken(repo)
    result = _queries(repo, selection).references("after_error", sha, direction="out")
    assert "before" in {r.target for r in result}


# ── tree-sitter absent ──


def test_a_failed_gate_selects_layer_zero_and_says_why():
    selection = select_provider(gate=providers.GateResult(False, ("tree-sitter: drifted",)))
    assert selection.provider.name == "ast"
    assert "drifted" in selection.detail


def test_a_parser_that_will_not_load_selects_layer_zero(monkeypatch):
    def boom(*_a, **_k):
        raise ImportError("No module named 'tree_sitter'")

    monkeypatch.setattr(providers, "TreeSitterProvider", boom)
    selection = select_provider(gate=providers.GateResult(True, grammars=("python",)))
    assert selection.provider.name == "ast"
    assert "tree_sitter" in selection.detail


def test_every_query_works_without_tree_sitter(repo, ast_only):
    q = _queries(repo, ast_only)
    sha = repo[1]
    assert len(q.definitions("helper", sha)) == 1
    assert len(q.references("helper", sha)) >= 3
    assert len(q.importers("pkg/core.py", sha)) == 3
    assert len(q.tests_for(["pkg/core.py"], sha)) == 1


def test_the_parse_cache_never_serves_one_extractor_s_parse_to_another(repo, ast_only, tree_sitter_selection):
    root, _ = repo
    sha = _commit_broken(repo)
    shared = RepositoryQueries(root, selection=ast_only).index
    lexical = RepositoryQueries(root, index=shared, selection=ast_only).definitions("after_error", sha)
    tolerant = RepositoryQueries(root, index=shared, selection=tree_sitter_selection).definitions("after_error", sha)
    assert [d.provider for d in lexical] == ["search"]
    assert [d.provider for d in tolerant] == ["tree_sitter"]


# ── grammar licensing gate [INV-A09] ──


def _versions(**overrides):
    table = {"tree-sitter": "0.25.2", "tree-sitter-python": "0.25.0", **overrides}
    return lambda subject: table.get(subject)


def test_the_gate_passes_when_the_parser_and_every_grammar_are_verified():
    gate = grammar_gate(distribution_version=_versions())
    assert gate.enabled, gate.reasons
    assert gate.grammars == ("python",)


def test_a_drifted_grammar_closes_the_gate():
    gate = grammar_gate(distribution_version=_versions(**{"tree-sitter-python": "0.26.0"}))
    assert not gate.enabled
    assert any("tree-sitter-python" in r and "drifted" in r for r in gate.reasons)


def test_a_grammar_without_its_own_row_closes_the_gate():
    grammars = {**GRAMMARS, "rust": ("tree-sitter-rust", "tree_sitter_rust", (".rs",))}
    gate = grammar_gate(distribution_version=_versions(), grammars=grammars)
    assert not gate.enabled
    assert any("tree-sitter-rust: no row" in r for r in gate.reasons)


def test_an_uninstalled_parser_closes_the_gate():
    gate = grammar_gate(distribution_version=_versions(**{"tree-sitter": None}))
    assert not gate.enabled and any(r.startswith(f"{PARSER_SUBJECT}: absent") for r in gate.reasons)


def test_the_shipped_register_has_a_verified_row_per_parser_and_grammar():
    register = json.loads(thirdparty.REGISTER_JSON.read_text())
    rows = {r["subject"]: r for r in register["records"]}
    for subject in (PARSER_SUBJECT, *(entry[0] for entry in GRAMMARS.values())):
        row = rows[subject]
        assert row["kind"] == "dependency" and row["scope"] == "optional"
        assert row["verified_version"], f"{subject} is not verified"
        assert row["free_use"]["license"] not in ("", "unverified")
        assert row["evidence"]
    assert rows["tree-sitter-python"]["findings"]["role"] == "grammar"


def test_declared_structural_extra_matches_the_gate():
    declared = thirdparty.declared_dependencies()
    assert declared.get("tree-sitter") == "structural"
    for subject, _module, _suffixes in GRAMMARS.values():
        assert declared.get(subject) == "structural"
