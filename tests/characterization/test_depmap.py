"""Pinned behaviour of the reference `scripts/gen_dependency_map.py` (dependency-map
generation) and `scripts/render_dependency_map.sh` (its mermaid render step).

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and `docs/FOUND_BUGS.md` records it. Nothing here
asserts what the code *should* do.

Two reference scripts feed one pipeline: `gen_dependency_map.py` parses `docs/ROADMAP.md`'s
fenced ``yaml`` task blocks and writes `docs/dependency_map.md` (a markdown doc with an
embedded mermaid graph); `render_dependency_map.sh` then shells out to the local mermaid-cli
build (`mmdc`, at `.mermaid/node_modules/.bin/mmdc`) to turn that into `docs/dependency_map.png`.
Per `docs/plans/2026-08-18-m4c-superseded-sidecars.md` §4 these become `maestro/docs/depmap.py`
(R5/R7) in **Wave B** of M4c batch 2. **This file is Wave A** — it pins the reference only.
`maestro.docs.depmap` does not exist yet, so every test parametrised on the shared `subject`
fixture from `conftest.py` skips its maestro half with "not extracted yet" (see conftest's
`_load_maestro`).

`gen_dependency_map.py` is a standalone script exactly like `scripts/watchdog.py` — its own
`REPO_ROOT = Path(__file__).resolve().parent.parent`, no import-time side effects, meant to be
*run*, not imported by `orchestrator_run.py`. So, like `test_watchdog.py`, it is loaded directly
by file path (`importlib.util.spec_from_file_location`) rather than through the shared `subject`
fixture's legacy half (which loads `orchestrator_run` — a different module that never imports
this one). Every one of its `Path`-valued module globals (`ROADMAP`, `DEP_MAP`, `STATE_JSON`,
`COMPLETED_TASKS`) is rebased into a temp tree by reflection before any test runs — never a
hand-written list — so no test can read or write the live reference repo. The anchor global is
named `REPO_ROOT` here, not `REPO` like every other characterised module; only the *name* differs,
the reflection approach does not.

`render_dependency_map.sh` is not importable at all. Like `test_verifications.py`'s subprocess
subject, it is copied verbatim into a throwaway fixture tree — alongside a stub `mmdc` executable
that records its argv (and a copy of the mermaid file it was given) instead of actually launching
headless Chromium — so its self-located `REPO_ROOT` resolves inside the fixture and the exact
command line it builds (`-i/-o/-p/-s/-w/--quiet`) is pinned without ever touching the real
`.mermaid/` install or emitting a real PNG.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.maestro_module("docs.depmap")


# --- locating the gen_dependency_map.py subject -------------------------------------------

_LEGACY_CACHE: dict[str, object] = {}


@pytest.fixture
def dm(subject):
    """The module that owns the dependency-map functions, whichever subject is under test."""
    return subject


# --- the sandbox ----------------------------------------------------------------------------


def _path_globals(module) -> dict[str, Path]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@pytest.fixture(autouse=True)
def box(request, tmp_path, monkeypatch):
    """Rebase every path global of the dependency-map module into a temp tree.

    Same reflection-based approach as `test_watchdog.py`'s `box` fixture: an un-rebased
    global here is a write (or a read of live project data) against the reference repo's
    `docs/ROADMAP.md` or `docs/dependency_map.md`. Never a hand-written list of names —
    every `Path`-valued module global is discovered and rebased relative to the module's
    own `REPO_ROOT`, and anything that somehow sits outside it is parked under `_outside/`
    instead. The exit assertion catches a test that reassigns a global itself.

    Autouse, but a no-op for the `render_dependency_map.sh` tests below: those never
    request `dm`, and pulling it in unconditionally here (via the fixture's own
    signature, the normal way) would force the shared `subject` fixture's legacy/maestro
    parametrisation onto every test in this module — including the render-script
    subprocess tests, which have no `subject` and would collide with `render_repo`'s own
    identically-named `tmp_path / "repo"` tree. `request.getfixturevalue` resolves `dm`
    lazily, only for tests that actually declare it.
    """
    if "dm" not in request.fixturenames:
        yield None
        return
    dm = request.getfixturevalue("dm")

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".orchestrator").mkdir()

    root = getattr(dm, "REPO_ROOT", None)
    assert isinstance(root, Path), (
        "the dependency-map module exposes no REPO_ROOT global; the reflection-based "
        "rebase cannot run and every path global would still point at a live repo"
    )
    root = root.resolve()

    original = {name: value for name, value in _path_globals(dm).items()}
    for name, value in original.items():
        resolved = value if value.is_absolute() else (Path.cwd() / value)
        if _under(resolved, root):
            target = repo / resolved.relative_to(root)
        else:
            target = repo / "_outside" / name
        monkeypatch.setattr(dm, name, target)

    def _leaks() -> list[str]:
        return sorted(
            name
            for name, value in _path_globals(dm).items()
            if not _under(value if value.is_absolute() else Path.cwd() / value, tmp_path)
        )

    assert not _leaks(), (
        f"path globals still resolve outside the temp tree: {_leaks()}. "
        "Calling the subject now could write to a live system."
    )

    def _p(name: str, fallback: Path) -> Path:
        value = getattr(dm, name, None)
        return value if isinstance(value, Path) else fallback

    yield SimpleNamespace(
        repo=repo,
        roadmap=_p("ROADMAP", repo / "docs" / "ROADMAP.md"),
        dep_map=_p("DEP_MAP", repo / "docs" / "dependency_map.md"),
        state=_p("STATE_JSON", repo / ".orchestrator" / "state.json"),
        completed=_p("COMPLETED_TASKS", repo / ".orchestrator" / "completed_tasks.json"),
    )

    assert not _leaks(), (
        f"a test left path globals resolving outside the temp tree: {_leaks()}"
    )


# --- fixture data -----------------------------------------------------------------------


def _task_full(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id, "title": f"Task {task_id}", "short_desc": "does a thing",
        "est_time": "1h", "mode": "autonomous", "eval_relevance": "med", "deps": [],
    }
    task.update(overrides)
    return task


# --- _parse_block / _manual_parse / parse_tasks -----------------------------------------


def test_parse_tasks_extracts_required_fields(dm):
    text = (
        "# ROADMAP\n\n"
        "```yaml\n"
        "id: T1\n"
        "title: First task\n"
        "short_desc: does a thing\n"
        "est_time: 30m\n"
        "mode: autonomous\n"
        "eval_relevance: high\n"
        "deps: []\n"
        "```\n"
    )
    tasks = dm.parse_tasks(text)
    assert len(tasks) == 1
    assert tasks[0]["id"] == "T1"
    assert tasks[0]["title"] == "First task"
    assert tasks[0]["deps"] == []


def test_parse_tasks_deps_bracketed_list(dm):
    text = "```yaml\nid: T2\ntitle: Second\ndeps: [T1, T3]\n```\n"
    [task] = dm.parse_tasks(text)
    assert task["deps"] == ["T1", "T3"]


def test_parse_tasks_missing_deps_defaults_to_empty_list(dm):
    text = "```yaml\nid: T1\ntitle: No deps field\n```\n"
    [task] = dm.parse_tasks(text)
    assert task["deps"] == []


def test_parse_tasks_block_without_id_is_skipped(dm):
    text = (
        "```yaml\ntitle: No id here\n```\n"
        "```yaml\nid: T1\ntitle: Has id\n```\n"
    )
    tasks = dm.parse_tasks(text)
    assert [t["id"] for t in tasks] == ["T1"]


def test_parse_tasks_ignores_non_yaml_fenced_blocks(dm):
    text = "```python\nprint('hi')\n```\n\n```yaml\nid: T1\ntitle: Real task\n```\n"
    tasks = dm.parse_tasks(text)
    assert [t["id"] for t in tasks] == ["T1"]


def test_manual_parse_single_line_fields(dm):
    text = "id: T1\ntitle: Manual task\nmode: autonomous\n"
    assert dm._manual_parse(text) == {"id": "T1", "title": "Manual task", "mode": "autonomous"}


def test_manual_parse_deps_bracket_and_quote_stripped(dm):
    text = "id: T1\ndeps: ['T2', \"T3\"]\n"
    assert dm._manual_parse(text)["deps"] == ["T2", "T3"]


def test_manual_parse_empty_deps_bracket_is_empty_list(dm):
    assert dm._manual_parse("id: T1\ndeps: []\n")["deps"] == []


def test_manual_parse_skips_blank_and_comment_lines(dm):
    text = "id: T1\n# a comment\n\ntitle: X\n"
    assert dm._manual_parse(text) == {"id": "T1", "title": "X"}


def test_manual_parse_skips_lines_without_colon(dm):
    text = "id: T1\nnot a key value line\ntitle: X\n"
    assert dm._manual_parse(text) == {"id": "T1", "title": "X"}


def test_parse_tasks_null_deps_value_produces_none_not_a_list(dm):
    """Bug #182 (docs/FOUND_BUGS.md): `deps:` with no value parses as `None`, not `[]`.

    `parse_tasks`'s `parsed.setdefault("deps", [])` only fires when the key is *absent* —
    here PyYAML parses the empty value as the key being *present* with value `None`, so the
    default never applies. Every downstream consumer that assumes `deps` is always a list
    (`_validate`, `_node_class`, `render`) crashes with an unhandled `TypeError` instead of
    the clean `SystemExit` a malformed roadmap block is supposed to produce.
    """
    text = "```yaml\nid: T1\ntitle: Null deps\nmode: autonomous\ndeps:\n```\n"
    [task] = dm.parse_tasks(text)
    assert task["deps"] is None


def test_validate_crashes_uncleanly_on_null_deps(dm):
    tasks = [{"id": "T1", "title": "A", "short_desc": "a", "est_time": "1h",
              "mode": "autonomous", "eval_relevance": "high", "deps": None}]
    with pytest.raises(TypeError):
        dm._validate(tasks)


def test_node_class_crashes_uncleanly_on_null_deps(dm):
    task = _task_full("T1", deps=None)
    with pytest.raises(TypeError):
        dm._node_class(task, active=set(), completed=set())


# --- _validate ----------------------------------------------------------------------------


def test_validate_passes_for_a_well_formed_task_set(dm):
    tasks = [_task_full("T1")]
    dm._validate(tasks)  # does not raise


def test_validate_raises_for_missing_required_fields(dm):
    tasks = [{"id": "T1", "deps": []}]
    with pytest.raises(SystemExit) as exc:
        dm._validate(tasks)
    assert "missing field 'title'" in str(exc.value)
    assert "missing field 'mode'" in str(exc.value)


def test_validate_raises_for_unknown_dep(dm):
    tasks = [_task_full("T1", deps=["GHOST"])]
    with pytest.raises(SystemExit, match="unknown dep 'GHOST'"):
        dm._validate(tasks)


def test_validate_accepts_dep_already_graduated_to_completed_registry(dm, box):
    box.completed.write_text(json.dumps([{"id": "OLD"}]))
    tasks = [_task_full("T1", deps=["OLD"])]
    dm._validate(tasks)  # does not raise: OLD graduated out of ROADMAP.md already


# --- _active_ids ----------------------------------------------------------------------------


def test_active_ids_empty_when_state_missing(dm, box):
    assert dm._active_ids() == set()


def test_active_ids_includes_in_flight_task_ids(dm, box):
    box.state.write_text(json.dumps({"in_flight": [{"task_id": "T1"}, {"task_id": "T2"}]}))
    assert dm._active_ids() == {"T1", "T2"}


def test_active_ids_includes_in_progress_phase(dm, box):
    box.state.write_text(json.dumps(
        {"in_flight": [], "phase": {"id": "T9", "status": "in_progress"}}
    ))
    assert dm._active_ids() == {"T9"}


def test_active_ids_excludes_phase_not_in_progress(dm, box):
    box.state.write_text(json.dumps(
        {"in_flight": [], "phase": {"id": "T9", "status": "parked"}}
    ))
    assert dm._active_ids() == set()


def test_active_ids_subtracts_parked_waiting_on_dan_tasks(dm, box):
    box.state.write_text(json.dumps({
        "in_flight": [{"task_id": "T1"}],
        "waiting_on_dan": {"1": {"task_id": "T1"}},
    }))
    assert dm._active_ids() == set()


def test_active_ids_malformed_json_reads_as_empty_set(dm, box):
    box.state.write_text("{not json")
    assert dm._active_ids() == set()


# --- _registry_completed ------------------------------------------------------------------


def test_registry_completed_empty_when_file_missing(dm, box):
    assert dm._registry_completed() == set()


def test_registry_completed_reads_ids(dm, box):
    box.completed.write_text(json.dumps([{"id": "T1"}, {"id": "T2"}]))
    assert dm._registry_completed() == {"T1", "T2"}


def test_registry_completed_malformed_json_reads_as_empty_set(dm, box):
    box.completed.write_text("not json")
    assert dm._registry_completed() == set()


# --- _node_class ----------------------------------------------------------------------------


def test_node_class_ready_when_nothing_blocks(dm):
    assert dm._node_class(_task_full("T1"), active=set(), completed=set()) == "ready"


def test_node_class_needs_dan_mode(dm):
    task = _task_full("T1", mode="needs-dan")
    assert dm._node_class(task, active=set(), completed=set()) == "dan"


def test_node_class_blocked_on_unmet_dep(dm):
    task = _task_full("T1", deps=["T0"])
    assert dm._node_class(task, active=set(), completed=set()) == "blocked"


def test_node_class_not_blocked_when_dep_already_completed(dm):
    task = _task_full("T1", deps=["T0"])
    assert dm._node_class(task, active=set(), completed={"T0"}) == "ready"


def test_node_class_held_beats_blocked(dm):
    task = _task_full("T1", hold=True, deps=["T0"])
    assert dm._node_class(task, active=set(), completed=set()) == "held"


def test_node_class_active_beats_held_blocked_and_needs_dan(dm):
    task = _task_full("T1", hold=True, deps=["T0"], mode="needs-dan")
    assert dm._node_class(task, active={"T1"}, completed=set()) == "active"


# --- _label -----------------------------------------------------------------------------


def test_label_formats_id_title_est_mode_relevance(dm):
    task = {"id": "T1", "title": "Do a thing", "est_time": "45m",
            "mode": "autonomous", "eval_relevance": "high"}
    assert dm._label(task) == 'T1["T1 — Do a thing<br/>45m<br/>autonomous · high"]'


def test_label_replaces_double_quotes_with_single_quotes_in_title_and_est(dm):
    task = {"id": "T1", "title": 'Say "hi"', "est_time": '2"', "mode": "m", "eval_relevance": "r"}
    assert dm._label(task) == "T1[\"T1 — Say 'hi'<br/>2'<br/>m · r\"]"


def test_label_defaults_missing_mode_and_relevance_to_question_mark(dm):
    task = {"id": "T1", "title": "X", "est_time": "1h"}
    assert dm._label(task) == 'T1["T1 — X<br/>1h<br/>? · ?"]'


# --- render -----------------------------------------------------------------------------


def test_render_includes_legend_and_classdefs(dm):
    text = dm.render([], active=set())
    assert "classDef ready   fill:#3b82f6,color:#fff,stroke:none" in text
    assert 'subgraph legend["Legend — node colour = why a task can / cannot run now"]' in text


def test_render_no_state_json_message_when_absent(dm, box):
    text = dm.render([_task_full("T1")], active=set())
    assert "orchestrator will supply the live highlight once the runtime exists" in text


def test_render_idle_message_when_state_json_present_but_nothing_active(dm, box):
    box.state.write_text("{}")
    text = dm.render([_task_full("T1")], active=set())
    assert "orchestrator idle" in text


def test_render_active_tasks_listed_in_progress_line(dm):
    text = dm.render([_task_full("T1")], active={"T1"})
    assert "In-progress (from `.orchestrator/state.json`): T1" in text


def test_render_completed_tasks_excluded_from_graph_and_counted_in_footer(dm):
    tasks = [_task_full("T1", status="complete"), _task_full("T2")]
    text = dm.render(tasks, active=set())
    assert "T1[" not in text
    assert "T2[" in text
    assert "registry of record: `.orchestrator/completed_tasks.json` (1 graduated)" in text


def test_render_draws_edge_for_unmet_dep(dm):
    tasks = [_task_full("T1"), _task_full("T2", deps=["T1"])]
    text = dm.render(tasks, active=set())
    assert "    T1 --> T2" in text


def test_render_no_edges_placeholder_when_all_tasks_independent(dm):
    tasks = [_task_full("T1"), _task_full("T2")]
    text = dm.render(tasks, active=set())
    assert "%% (no dependency edges — all tasks are independent)" in text


def test_render_edge_to_completed_dep_is_satisfied_not_drawn(dm):
    tasks = [_task_full("T1", status="complete"), _task_full("T2", deps=["T1"])]
    text = dm.render(tasks, active=set())
    assert "T1 --> T2" not in text


# --- main() ---------------------------------------------------------------------------------


def _full_yaml_block(**overrides) -> str:
    task = {
        "id": "T1", "title": "Solo task", "short_desc": "does a thing",
        "est_time": "30m", "mode": "autonomous", "eval_relevance": "high", "deps": [],
    }
    task.update(overrides)
    body = "\n".join(f"{k}: {v}" for k, v in task.items())
    return f"```yaml\n{body}\n```\n"


def test_main_writes_dep_map_from_roadmap(dm, box, monkeypatch, capsys):
    box.roadmap.write_text(_full_yaml_block())
    monkeypatch.setattr(sys, "argv", ["gen_dependency_map.py"])

    rc = dm.main()

    assert rc == 0
    assert box.dep_map.exists()
    assert 'T1["T1' in box.dep_map.read_text()
    out = capsys.readouterr().out
    assert "Wrote" in out
    assert "Tasks: T1" in out


def test_main_check_flag_reports_stale_and_does_not_write(dm, box, monkeypatch, capsys):
    box.roadmap.write_text(_full_yaml_block())
    monkeypatch.setattr(sys, "argv", ["gen_dependency_map.py", "--check"])

    rc = dm.main()

    assert rc == 1
    assert "STALE" in capsys.readouterr().err
    assert not box.dep_map.exists()


def test_main_check_flag_reports_up_to_date_after_a_write(dm, box, monkeypatch, capsys):
    box.roadmap.write_text(_full_yaml_block())
    monkeypatch.setattr(sys, "argv", ["gen_dependency_map.py"])
    dm.main()
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["gen_dependency_map.py", "--check"])

    rc = dm.main()

    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_main_missing_roadmap_raises_system_exit(dm, box, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["gen_dependency_map.py"])
    with pytest.raises(SystemExit, match="missing"):
        dm.main()


def test_main_empty_roadmap_raises_system_exit(dm, box, monkeypatch):
    box.roadmap.write_text("# ROADMAP\n\nno task blocks here\n")
    monkeypatch.setattr(sys, "argv", ["gen_dependency_map.py"])
    with pytest.raises(SystemExit, match="no task blocks found"):
        dm.main()
