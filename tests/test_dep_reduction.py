"""P12 pilot finding D21: transitively redundant ROADMAP deps are removed, deterministically.

A task that depends on C while it already depends on B, and B reaches C, carries an edge
that says nothing: the map draws it as noise. `taskgraph.transitive_reduction` is the one
rule; the ROADMAP rewrite, the dependency map and every writer of deps apply it.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from maestro import taskgraph
from maestro.docs import followups


reduce = taskgraph.transitive_reduction


# ── the helper ──────────────────────────────────────────────────────────────


def test_a_chain_drops_the_shortcut():
    deps = {"A": ["B", "C"], "B": ["C"], "C": []}
    assert reduce(deps) == {"A": ["B"], "B": ["C"], "C": []}


def test_a_long_chain_drops_every_shortcut_keeping_order():
    deps = {"D": ["A", "C", "B"], "C": ["B"], "B": ["A"], "A": []}
    assert reduce(deps)["D"] == ["C"]


def test_a_diamond_keeps_both_arms_and_drops_the_base():
    deps = {"D": ["B", "A", "C"], "B": ["A"], "C": ["A"], "A": []}
    assert reduce(deps)["D"] == ["B", "C"]


def test_order_of_the_remaining_deps_is_preserved():
    deps = {"X": ["Z", "Q", "Y", "P"], "Y": ["P"], "Z": [], "Q": [], "P": []}
    assert reduce(deps)["X"] == ["Z", "Q", "Y"]


def test_registry_deps_supply_reachability_for_graduated_tasks():
    # B graduated (no block): its deps are known only from the registry.
    deps = {"A": ["B", "C"]}
    assert reduce(deps, known={"B": ["C"]}) == {"A": ["B"]}
    assert reduce(deps) == {"A": ["B", "C"]}


def test_registry_deps_never_reduce_a_graduated_tasks_own_edges():
    assert set(reduce({"A": ["B"]}, known={"B": ["C", "D"], "C": ["D"]})) == {"A"}


def test_a_graduated_task_does_not_vouch_for_an_open_one():
    # B is done; its old dep on the open task C cannot make A wait for C any more.
    deps = {"A": ["B", "C"], "C": []}
    assert reduce(deps, known={"B": ["C"]})["A"] == ["B", "C"]


def test_unknown_dep_ids_are_kept_and_can_be_reached():
    assert reduce({"A": ["ghost", "B"], "B": []})["A"] == ["ghost", "B"]
    assert reduce({"A": ["ghost", "B"], "B": ["ghost"]})["A"] == ["B"]


def test_cycles_do_not_crash_and_are_left_untouched():
    deps = {"A": ["B", "C"], "B": ["C", "A"], "C": []}
    assert reduce(deps) == deps
    assert reduce({"S": ["S", "T"], "T": []})["S"] == ["S", "T"]


def test_deps_inside_one_cycle_are_not_used_to_remove_each_other():
    # X is outside the cycle B<->C; both B and C reach each other, neither may go.
    deps = {"X": ["B", "C"], "B": ["C"], "C": ["B"]}
    assert reduce(deps)["X"] == ["B", "C"]


def test_the_reduction_is_deterministic_and_idempotent():
    deps = {"A": ["B", "C", "D"], "B": ["C"], "C": ["D"], "D": []}
    once = reduce(deps)
    assert once == reduce(dict(reversed(list(deps.items()))))
    assert reduce(once) == once


def test_a_string_dep_and_ints_are_read_as_ids():
    assert reduce({"A": "B", "B": []}) == {"A": ["B"], "B": []}
    assert reduce({"A": [1, 2], "1": [2]})["A"] == ["1"]


# ── the ROADMAP rewrite ─────────────────────────────────────────────────────


ROADMAP = """\
# Roadmap

Some prose that must survive.

### 03 — reader

```yaml
id: '03'
status: open
deps: []
```

### 05 — selector

```yaml
id: '05'
deps: ['03']   # a comment elsewhere
title: Selector
```

### 06 — writer

```yaml
id: '06'
deps: ['05']
```

### 07 — reconcile

```yaml
id: '07'
deps: ['03', '06']
title: Reconcile
```

### 08 — timers

```yaml
id: '08'
mode: autonomous
deps:
- '06'
- '07'
hold: false
```

### 09 — observe

```yaml
id: '09'
deps:
  - '08'
```
"""


def test_the_rewrite_edits_only_the_deps_lines_of_redundant_blocks():
    new, removed = taskgraph.reduce_document_deps(ROADMAP)
    assert removed == {"07": ["03"], "08": ["06"]}
    old_lines, new_lines = ROADMAP.splitlines(), new.splitlines()
    assert len(new_lines) == len(old_lines) - 1          # 08 lost one list item
    assert "deps: ['06']" in new_lines                   # 07's flow list, quoting kept
    assert "- '06'" not in new[new.index("id: '08'"):new.index("id: '09'")]
    changed = [l for l in old_lines if l not in new_lines]
    assert changed == ["deps: ['03', '06']", "- '06'"]
    by_id = {t["id"]: t for t in taskgraph.scan_tasks(new)}
    assert by_id["07"]["deps"] == ["06"] and by_id["07"]["title"] == "Reconcile"
    assert by_id["08"]["deps"] == ["07"] and by_id["08"]["hold"] is False


def test_the_rewrite_is_idempotent_and_a_no_op_when_clean():
    new, _ = taskgraph.reduce_document_deps(ROADMAP)
    again, removed = taskgraph.reduce_document_deps(new)
    assert (again, removed) == (new, {})


def test_the_rewrite_uses_registry_deps_and_skips_named_blocks():
    text = "```yaml\nid: A\ndeps: [B, C]\n```\n"
    assert taskgraph.reduce_document_deps(text) == (text, {})
    new, removed = taskgraph.reduce_document_deps(text, known={"B": ["C"]})
    assert removed == {"A": ["C"]} and "deps: [B]" in new
    assert taskgraph.reduce_document_deps(text, known={"B": ["C"]}, skip={"A"}) == (text, {})


def test_a_deps_spelling_the_rewrite_cannot_edit_exactly_is_left_alone():
    text = "```yaml\nid: A\ndeps: [B,\n  C]\n```\n\n```yaml\nid: B\ndeps: [C]\n```\n"
    assert taskgraph.reduce_document_deps(text) == (text, {})


def test_add_document_deps_edits_only_the_deps_line_in_both_spellings():
    """GDT-11 S1: the twin of the reduction. A dependent gains a `before_dependents`
    follow-up task in its `deps:` line (flow) or as one more list item (block); a dep
    already there, a block in `skip` and a spelling it cannot edit exactly are left."""
    wanted = {"06": ["10"], "09": ["10"], "05": ["10"], "07": ["06"], "03": ["10"]}
    new, added = taskgraph.add_document_deps(ROADMAP, wanted, skip={"03"})
    assert added == {"06": ["10"], "09": ["10"]}
    old_lines, new_lines = ROADMAP.splitlines(), new.splitlines()
    assert len(new_lines) == len(old_lines) + 1                  # 09 gained one list item
    assert [l for l in old_lines if l not in new_lines] == ["deps: ['05']"]
    assert "deps: ['05', '10']" in new_lines                     # quoted: it reads as a string
    assert new[new.index("id: '09'"):].startswith("id: '09'\ndeps:\n  - '08'\n  - '10'\n")
    by_id = {t["id"]: t for t in taskgraph.scan_tasks(new)}
    assert by_id["06"]["deps"] == ["05", "10"] and by_id["09"]["deps"] == ["08", "10"]
    assert by_id["05"]["deps"] == ["03"] and by_id["07"]["deps"] == ["03", "06"]
    assert taskgraph.add_document_deps(new, wanted, skip={"03"}) == (new, {})


def test_the_registry_deps_are_read_from_the_completion_registry(tmp_path):
    reg = taskgraph.registry_path(tmp_path)
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps([{"id": "B", "deps": ["C"]}, {"id": "Z"}]))
    assert taskgraph.read_registry_deps(tmp_path) == {"B": ["C"], "Z": []}
    reg.write_text("not json")
    assert taskgraph.read_registry_deps(tmp_path) == {}


def test_a_transaction_reduces_the_deps_it_writes():
    text = ("```yaml\nid: A\ndeps: []\n```\n\n```yaml\nid: B\ndeps: [A]\n```\n\n"
            "```yaml\nid: C\ndeps: [B]\n```\n")
    txn = taskgraph.RoadmapTransaction(taskgraph.parse_document(text))
    txn.add_dependency("C", "A")
    assert {t["id"]: t.get("deps") for t in taskgraph.scan_tasks(txn.render())}["C"] == ["B"]


# ── writers ─────────────────────────────────────────────────────────────────


def test_a_follow_up_task_depends_on_its_parent_only():
    # The parent's own deps are never copied: they would all be redundant through it.
    task = followups.build_task({"id": "04", "deps": ["02", "03"]}, [])
    assert task["deps"] == ["04"]


# ── the loop-start normalisation ────────────────────────────────────────────


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    from maestro.docs import roadmap as doc_roadmap

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text(ROADMAP)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    journal: list = []
    monkeypatch.setattr(doc_roadmap, "REPO", tmp_path)
    monkeypatch.setattr(doc_roadmap, "ROADMAP_FILE", tmp_path / "docs" / "ROADMAP.md")
    monkeypatch.setattr(doc_roadmap, "append_journal", lambda *a: journal.append(a))
    return tmp_path, doc_roadmap, journal


def test_loop_start_rewrites_journals_and_commits_docs_only(project):
    repo, doc_roadmap, journal = project
    (repo / "other.txt").write_text("staged elsewhere")
    _git(repo, "add", "other.txt")
    assert doc_roadmap.reduce_roadmap_deps() == {"07": ["03"], "08": ["06"]}
    assert journal == [("roadmap_deps_reduced", "07: -03; 08: -06")]
    assert _git(repo, "show", "--name-only", "--format=%s", "HEAD").stdout.split("\n")[:3] == [
        "roadmap: drop transitively redundant deps (07, 08)", "", "docs/ROADMAP.md"]
    assert "other.txt" in _git(repo, "diff", "--cached", "--name-only").stdout
    head = _git(repo, "rev-parse", "HEAD").stdout
    assert doc_roadmap.reduce_roadmap_deps() == {}
    assert len(journal) == 1 and _git(repo, "rev-parse", "HEAD").stdout == head


def test_loop_start_leaves_an_uncommitted_hand_edit_alone(project):
    repo, doc_roadmap, journal = project
    path = repo / "docs" / "ROADMAP.md"
    path.write_text(ROADMAP + "\nDan is typing\n")
    assert doc_roadmap.reduce_roadmap_deps() == {}
    assert path.read_text().endswith("Dan is typing\n") and "'03', '06'" in path.read_text()
    assert journal == []


def test_loop_start_keeps_the_cas_in_step(project):
    repo, doc_roadmap, _ = project
    taskgraph.record_applied(repo, taskgraph.load_document(repo / "docs" / "ROADMAP.md"))
    doc_roadmap.reduce_roadmap_deps()
    assert taskgraph.drift(repo) is None


def test_loop_start_does_not_launder_a_drifted_block(project):
    repo, doc_roadmap, _ = project
    path = repo / "docs" / "ROADMAP.md"
    taskgraph.record_applied(repo, taskgraph.load_document(path))
    path.write_text(path.read_text().replace("title: Reconcile", "title: Reconcile!"))
    _git(repo, "commit", "-qam", "hand edit")
    assert doc_roadmap.reduce_roadmap_deps() == {"08": ["06"]}
    conflict = taskgraph.drift(repo)
    assert conflict is not None and conflict.changed == ("07",)


# ── the dependency map ──────────────────────────────────────────────────────


def test_the_map_draws_the_reduced_graph(tmp_path, monkeypatch):
    from maestro.docs import depmap

    registry = tmp_path / "completed_tasks.json"
    registry.write_text(json.dumps([{"id": "G", "deps": ["C"]}]))
    monkeypatch.setattr(depmap, "COMPLETED_TASKS", registry)
    monkeypatch.setattr(depmap, "STATE_JSON", tmp_path / "state.json")
    tasks = [{"id": i, "title": i, "mode": "autonomous", "deps": d} for i, d in (
        ("C", []), ("B", ["C"]), ("A", ["B", "C"]), ("X", ["G", "C"]))]
    edges = [l.strip() for l in depmap.render(tasks, set()).splitlines() if "-->" in l]
    # A->C goes (via B); X->C stays: the graduated G no longer makes X wait for open C.
    assert edges == ["C --> B", "B --> A", "C --> X"]


def test_an_approved_proposal_is_written_with_reduced_deps(tmp_path, monkeypatch):
    from maestro import orchestrator

    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("```yaml\nid: A\ndeps: []\n```\n\n```yaml\nid: B\ndeps: [A]\n```\n")
    questions = tmp_path / "questions"
    questions.mkdir()
    (questions / "q1.json").write_text(json.dumps(
        {"id": "q1", "proposals": [{"title": "Speed", "deps": ["A", "B"]}]}))
    (questions / "q1.answer").write_text("1")
    state = {"pending_proposal_qid": "q1"}
    monkeypatch.setattr(orchestrator, "ROADMAP_FILE", roadmap)
    monkeypatch.setattr(orchestrator, "QUESTIONS_DIR", questions)
    monkeypatch.setattr(orchestrator, "REPO", tmp_path)
    monkeypatch.setattr(orchestrator, "read_state", lambda: dict(state))
    monkeypatch.setattr(orchestrator, "write_state", lambda s: None)
    monkeypatch.setattr(orchestrator, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "append_journal", lambda *a, **k: None)
    orchestrator.poll_proposal_answer()
    assert 'deps: ["B"]' in roadmap.read_text()


def test_graduation_records_the_tasks_deps_in_the_registry(tmp_path, monkeypatch):
    from maestro.docs import complete

    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir()
    roadmap.write_text("### B — b\n\n```yaml\nid: B\ntitle: b\ndeps: [C, D]\n```\n")
    registry = tmp_path / ".orchestrator" / "completed_tasks.json"
    monkeypatch.setattr(complete, "REPO", tmp_path)
    monkeypatch.setattr(complete, "ROADMAP_FILE", roadmap)
    monkeypatch.setattr(complete, "COMPLETED_TASKS", registry)
    monkeypatch.setattr(complete, "inferred_doc", lambda t: tmp_path / "docs" / "PROJECT.md")
    monkeypatch.setattr(complete, "ensure_graduation_marker", lambda *a: True)
    monkeypatch.setattr(complete.doc_depmap, "run_cli", lambda argv: (0, ""))
    monkeypatch.setattr(complete.doc_consistency, "run_cli", lambda argv: (0, ""))
    monkeypatch.setattr(complete.sys, "argv", ["complete", "B", "--no-verify"])
    assert complete.main() == 0
    assert taskgraph.read_registry_deps(tmp_path) == {"B": ["C", "D"]}
