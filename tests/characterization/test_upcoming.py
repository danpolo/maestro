"""Pinned behaviour of the reference `docs/UPCOMING.md` generator (M4c batch 2, Wave A).

`scripts/gen_upcoming.py` is a **standalone script** in the reference, not a function
living inside the `orchestrator_run.py` monolith — same shape as `watchdog.py` (M4b) and
`orchestrator_status.py` (M4c batch 1, R13). Unlike those two, though, it depends on
another extracted module (`gen_dependency_map` → `maestro.docs.depmap`), so it is placed
alongside it at `maestro/docs/upcoming.py` — the same package `docs.roadmap` and
`docs.depmap` already live in, and the marker below is `"docs.upcoming"` to match (not
bare `"upcoming"`, which would resolve to a top-level `maestro.upcoming` that does not
exist). Before extraction, every test here is written against the shared
`subject`/`sandbox` machinery in `conftest.py` purely for its skip plumbing:
`pytestmark = pytest.mark.maestro_module(...)` makes the "maestro" half of the
parametrised `subject` fixture skip with "maestro.docs.upcoming not extracted yet", and
the "legacy" half is redirected (via the local `up` fixture, mirroring
`test_watchdog.py`'s `wd`) to a directly-loaded copy of the real `scripts/gen_upcoming.py`,
ignoring whatever `subject` handed back.

Three things are specific to this module:

* the reference script pulls in two more read-only siblings at runtime: `import
  gen_dependency_map as gdm` at module scope (for `parse_tasks`/`_registry_completed`),
  and a lazy `import prepared_actions` inside `_prepared_action` (best-effort, swallowed on
  any failure). Both are self-locating the same way `gen_upcoming.py` is (`Path(__file__)
  .resolve().parent(.parent)`), so the autouse `box` fixture below rebases the `Path`
  globals of all three modules — `up`, `up.gdm`, and `sys.modules["prepared_actions"]`
  (pre-imported so it has something to rebase) — by reflection into one temp tree, the same
  "never a hand-written name list" discipline `sandbox`/`test_watchdog.py`'s `box` use;
* `_opus_complete` is the one function that reaches an agent CLI (`claude -p --model
  claude-opus-4-8 <prompt>`, billed against the Max subscription — see the module
  docstring). Every test of it, and of `refresh_explanations` which calls it, swaps the
  subject's own `subprocess` module reference for a small recording stand-in
  (`_fake_subprocess`, mirroring `test_selfheal.py`'s `_fake_subprocess` for
  `_judge_complete`) so no test can ever spawn a real `claude` process;
* `main()` reads `sys.argv` directly (plain `argparse`, no injectable args parameter), so
  CLI-level tests monkeypatch `sys.argv` rather than calling a `run_cli(argv)` seam — this
  script never grew one.

Characterisation, not specification: several surprises here are pinned, not endorsed —
see `docs/FOUND_BUGS.md` entries #182–#184 (a `task_status` precedence bug, an unanchored
substring match in `detect_terms`, and a greedy JSON-extraction regex shared with
`orchestrator_run.py`'s own `#23`/`#118`).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.maestro_module("docs.upcoming")

REFERENCE_SCRIPT = "gen_upcoming.py"


# --- locating the subject -----------------------------------------------------------------

_LEGACY_CACHE: dict[str, object] = {}


@pytest.fixture
def up(subject):
    """The module that owns the upcoming-guide functions, whichever subject is under test."""
    return subject


# --- the sandbox ---------------------------------------------------------------------------


def _path_globals(module) -> dict[str, Path]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _module_root(module) -> Path:
    """The real-repo root this module's own `Path` globals are computed relative to.

    Both `up` and `up.gdm` are self-anchored: `maestro.docs.upcoming` / `maestro.docs.depmap`
    expose a `REPO_ROOT` global (derived from `Paths.from_env()`) precisely so callers never
    have to hand-wire which repo they mean. The only module that ever lacked one was the
    legacy `prepared_actions` sibling script, which fell back to the reference repo's root;
    with the legacy subject retired that module can no longer load, so a missing `REPO_ROOT`
    now means a module this rebase cannot make safe — raise rather than guess, since guessing
    wrong leaves path globals pointed at a live repo.
    """
    root = getattr(module, "REPO_ROOT", None)
    if isinstance(root, Path):
        return root.resolve()
    raise AssertionError(
        f"{getattr(module, '__name__', module)} exposes no REPO_ROOT; its path globals "
        "cannot be rebased into the sandbox and would still resolve to a live repo"
    )


@pytest.fixture(autouse=True)
def box(up, tmp_path, monkeypatch):
    """Rebase every path global of `up`, `up.gdm`, and `prepared_actions` into a temp tree.

    Autouse and not optional: an un-rebased global here is a read against a live system's
    `.orchestrator/` (worse, `refresh_explanations` writes its cache there too). Nothing is
    ever a hand-written name list — every `Path`-valued module global under each module's
    own repo root (see `_module_root`) is discovered by reflection and rebased, and the
    exit assertion catches a test that reassigns one itself.
    """
    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    (repo / "docs").mkdir()

    try:
        prepared_actions = importlib.import_module("prepared_actions")
    except ImportError:
        prepared_actions = None

    modules = [up, up.gdm] + ([prepared_actions] if prepared_actions is not None else [])
    # Captured once, before any rebasing: `REPO_ROOT` is itself one of the `Path` globals
    # rebased below, so recomputing `_module_root` after the loop would read the
    # already-sandboxed value and make every subsequent global look "still real".
    roots = {id(module): _module_root(module) for module in modules}

    for module in modules:
        root = roots[id(module)]
        for name, value in _path_globals(module).items():
            resolved = value if value.is_absolute() else (Path.cwd() / value)
            if not _under(resolved, root):
                continue
            target = repo / resolved.relative_to(root)
            monkeypatch.setattr(module, name, target)

    def _leaks() -> list[str]:
        return sorted(
            f"{m.__name__}.{n}"
            for m in modules
            for n, v in _path_globals(m).items()
            if _under(v if v.is_absolute() else Path.cwd() / v, roots[id(m)])
        )

    assert not _leaks(), (
        f"path globals still resolve inside the read-only reference repo: {_leaks()}. "
        "Calling the subject now could read/write a live system."
    )

    yield SimpleNamespace(
        repo=repo,
        roadmap=repo / "docs" / "ROADMAP.md",
        upcoming=repo / "docs" / "UPCOMING.md",
        state=repo / ".orchestrator" / "state.json",
        completed=repo / ".orchestrator" / "completed_tasks.json",
        cache=repo / ".orchestrator" / "upcoming_explanations.json",
        prepared=repo / ".orchestrator" / "prepared_actions.json",
    )

    assert not _leaks(), f"a test left path globals resolving inside the reference repo: {_leaks()}"


# --- the LLM call site: fake `subprocess` -----------------------------------------------


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_subprocess(monkeypatch, up, handler):
    """Swap `up`'s own `subprocess` module reference for a recording stand-in. Scoped to
    `up` only — the real `subprocess` module used elsewhere in the process is untouched."""
    fake = SimpleNamespace(
        run=handler,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
    )
    monkeypatch.setattr(up, "subprocess", fake)
    return fake


# --- task dict helper ------------------------------------------------------------------


def _task(id="T1", title="Title", short_desc="desc", est_time="2h", mode="autonomous",
          deps=None, **extra) -> dict:
    t = {
        "id": id, "title": title, "short_desc": short_desc, "est_time": est_time,
        "mode": mode, "deps": deps or [],
    }
    t.update(extra)
    return t


# ===========================================================================================
# module constants
# ===========================================================================================

def test_judge_model_and_timeout_constants(up):
    assert up.JUDGE_MODEL == "claude-opus-4-8"
    assert up.REFRESH_TIMEOUT == 240


# ===========================================================================================
# _task_fingerprint
# ===========================================================================================

def test_task_fingerprint_ignores_the_task_id(up):
    a = up._task_fingerprint(_task(id="T1", title="A", short_desc="B", est_time="1h", deps=["X"]))
    b = up._task_fingerprint(_task(id="T99", title="A", short_desc="B", est_time="1h", deps=["X"]))
    assert a == b


def test_task_fingerprint_changes_when_short_desc_changes(up):
    a = up._task_fingerprint(_task(short_desc="B"))
    b = up._task_fingerprint(_task(short_desc="C"))
    assert a != b


def test_task_fingerprint_changes_when_deps_change(up):
    a = up._task_fingerprint(_task(deps=["X"]))
    b = up._task_fingerprint(_task(deps=["X", "Y"]))
    assert a != b


def test_task_fingerprint_unaffected_by_mode_or_hold(up):
    """Only title/short_desc/est_time/deps drive the fingerprint — `mode` and `hold`
    changing does not invalidate a cached explanation."""
    a = up._task_fingerprint(_task(mode="autonomous", hold=False))
    b = up._task_fingerprint(_task(mode="needs-dan", hold=True))
    assert a == b


def test_task_fingerprint_is_a_sha1_hex_digest(up):
    fp = up._task_fingerprint(_task())
    assert len(fp) == 40
    int(fp, 16)  # raises ValueError if not hex


# ===========================================================================================
# load_cache / get_prose
# ===========================================================================================

def test_load_cache_missing_file_returns_empty_dict(up, box):
    assert up.load_cache() == {}


def test_load_cache_corrupt_json_returns_empty_dict(up, box):
    box.cache.parent.mkdir(parents=True, exist_ok=True)
    box.cache.write_text("{not json", encoding="utf-8")
    assert up.load_cache() == {}


def test_get_prose_uses_a_fresh_cache_entry(up):
    task = _task(id="T1", title="A", short_desc="B", est_time="1h", deps=[])
    fp = up._task_fingerprint(task)
    cache = {"T1": {"hash": fp, "what_why": "W", "pipeline_fit": "P", "model": "claude-opus-4-8"}}

    prose = up.get_prose(task, cache)

    assert prose == {"what_why": "W", "pipeline_fit": "P", "source": "explained by claude-opus-4-8"}


def test_get_prose_falls_back_when_the_cached_hash_is_stale(up):
    task = _task(short_desc="short desc here")
    cache = {task["id"]: {"hash": "stale-hash", "what_why": "old", "pipeline_fit": "old"}}

    prose = up.get_prose(task, cache)

    assert prose["what_why"] == "short desc here"
    assert prose["pipeline_fit"] == ""
    assert prose["source"] == "auto (run `--refresh-explanations` for a fuller explanation)"


def test_get_prose_falls_back_when_there_is_no_cache_entry(up):
    prose = up.get_prose(_task(short_desc="x"), {})
    assert prose["what_why"] == "x"
    assert prose["source"] == "auto (run `--refresh-explanations` for a fuller explanation)"


# ===========================================================================================
# detect_terms
# ===========================================================================================

def test_detect_terms_matches_alias_case_insensitively(up):
    terms = up.detect_terms("This task improves RRF fusion between legs.")
    assert any(g["term"] == "RRF (Reciprocal Rank Fusion)" for g in terms)


def test_detect_terms_returns_empty_list_when_nothing_matches(up):
    assert up.detect_terms("Completely unrelated infrastructure cleanup.") == []


def test_detect_terms_preserves_glossary_order_not_mention_order(up):
    # MaxSim is mentioned first in the text but sits *after* RRF in GLOSSARY.
    text = "Tune MaxSim scoring; also touches RRF."
    terms = [g["term"] for g in up.detect_terms(text)]
    assert terms == ["RRF (Reciprocal Rank Fusion)", "MaxSim"]


def test_detect_terms_false_positive_on_substring_of_an_unrelated_word(up):
    """FOUND_BUGS #183: alias matching is an unanchored substring test, not a word-boundary
    match. "supervision" contains the SigLIP-2 alias "vision"."""
    terms = up.detect_terms("Add supervision to the watchdog restart loop.")
    assert any(g["term"] == "SigLIP-2 (image embeddings)" for g in terms)


# ===========================================================================================
# _detail_paths
# ===========================================================================================

def test_detail_paths_maps_task_id_to_the_detail_link(up):
    text = (
        "```yaml\nid: T1\ntitle: A\n```\n\nDetail: `retired/tasks/Phase01.md`\n\n"
        "```yaml\nid: T2\ntitle: B\n```\n"
    )
    assert up._detail_paths(text) == {"T1": "retired/tasks/Phase01.md"}


def test_detail_paths_empty_when_no_detail_line_follows_the_block(up):
    assert up._detail_paths("```yaml\nid: T1\n```\n") == {}


# ===========================================================================================
# task_status — precedence and wording
# ===========================================================================================

def test_task_status_default_is_ready_to_run(up):
    st = up.task_status(_task(), {}, set())
    assert st == {
        "label": "🔵 Ready to run",
        "detail": "Deps are met; Maestro can run this autonomously.",
        "action": "",
    }


def test_task_status_unmet_dep_is_blocked(up):
    st = up.task_status(_task(deps=["T0"]), {}, set())
    assert st["label"] == "⛔ Blocked"
    assert st["detail"] == "Waiting on T0 to finish first."
    assert st["action"] == ""


def test_task_status_completed_dep_is_not_blocking(up):
    st = up.task_status(_task(deps=["T0"]), {}, {"T0"})
    assert st["label"] == "🔵 Ready to run"


def test_task_status_hold_true_is_held(up):
    st = up.task_status(_task(hold=True), {}, set())
    assert st["label"] == "⏸️ Held"
    assert st["detail"] == (
        "Pinned out (`hold: true`) — Maestro will not launch it until the hold is cleared."
    )
    assert st["action"] == ""


def test_task_status_needs_dan_mode_is_ready_needs_action(up):
    st = up.task_status(_task(mode="needs-dan", dan_action="do X"), {}, set())
    assert st["label"] == "🟠 Ready — needs your action"
    assert st["detail"] == "Deps are met; this one needs a manual step from you to start."
    assert st["action"] == "do X"


def test_task_status_dispatch_manual_is_ready_needs_action(up):
    st = up.task_status(_task(mode="autonomous", dispatch="manual", dan_action="do Y"), {}, set())
    assert st["label"] == "🟠 Ready — needs your action"
    assert st["action"] == "do Y"


def test_task_status_in_flight_is_running_now(up):
    state = {"in_flight": [{"task_id": "T1"}]}
    st = up.task_status(_task(id="T1"), state, set())
    assert st["label"] == "🟢 Running now"
    assert st["detail"] == "Maestro has an implementer working on this right now."
    assert st["action"] == ""


def test_task_status_parked_is_awaiting_you(up):
    state = {"waiting_on_dan": {"1": {"task_id": "T1"}}}
    st = up.task_status(_task(id="T1", dan_action="fallback action"), state, set())
    assert st["label"] == "🟡 Awaiting you"
    assert st["detail"] == "Maestro has prepared this and is waiting on your one action (below)."
    assert st["action"] == "fallback action"


def test_task_status_parked_prefers_the_prepared_action_sidecar_over_dan_action(up, box):
    box.prepared.parent.mkdir(parents=True, exist_ok=True)
    box.prepared.write_text(json.dumps({"T1": {"action": "click the colab link"}}), encoding="utf-8")
    state = {"waiting_on_dan": {"1": {"task_id": "T1"}}}

    st = up.task_status(_task(id="T1", dan_action="fallback"), state, set())

    assert st["action"] == "click the colab link"


def test_task_status_falls_back_to_dan_action_when_sidecar_has_no_entry(up, box):
    box.prepared.parent.mkdir(parents=True, exist_ok=True)
    box.prepared.write_text(json.dumps({"T2": {"action": "irrelevant"}}), encoding="utf-8")
    state = {"waiting_on_dan": {"1": {"task_id": "T1"}}}

    st = up.task_status(_task(id="T1", dan_action="fallback"), state, set())

    assert st["action"] == "fallback"


def test_task_status_parked_beats_in_flight_contradicting_its_own_docstring(up):
    """FOUND_BUGS #182: the docstring claims precedence "running > awaiting-you", but the
    code checks `is_parked` before `tid in in_flight` — the opposite order."""
    state = {"in_flight": [{"task_id": "T1"}], "waiting_on_dan": {"1": {"task_id": "T1"}}}

    st = up.task_status(_task(id="T1"), state, set())

    assert st["label"] == "🟡 Awaiting you"  # not "🟢 Running now"


def test_task_status_hold_beats_unmet_deps(up):
    st = up.task_status(_task(hold=True, deps=["T0"]), {}, set())
    assert st["label"] == "⏸️ Held"


def test_task_status_unmet_deps_beat_needs_dan_mode(up):
    st = up.task_status(_task(mode="needs-dan", deps=["T0"]), {}, set())
    assert st["label"] == "⛔ Blocked"


# ===========================================================================================
# _fmt_deps
# ===========================================================================================

def test_fmt_deps_no_deps(up):
    assert up._fmt_deps(_task(deps=[]), set(), set()) == "none — ready as soon as Maestro reaches it"


def test_fmt_deps_completed_dep(up):
    assert up._fmt_deps(_task(deps=["T0"]), {"T0"}, set()) == "T0 ✅ (done)"


def test_fmt_deps_pending_dep(up):
    assert up._fmt_deps(_task(deps=["T0"]), set(), {"T0"}) == "T0 ⏳ (still pending)"


def test_fmt_deps_multiple_mixed(up):
    result = up._fmt_deps(_task(deps=["T0", "T1"]), {"T0"}, {"T1"})
    assert result == "T0 ✅ (done), T1 ⏳ (still pending)"


def test_fmt_deps_unknown_dep_renders_with_no_marker(up):
    assert up._fmt_deps(_task(deps=["Tghost"]), set(), set()) == "Tghost"


# ===========================================================================================
# _mode_phrase
# ===========================================================================================

def test_mode_phrase_manual_dispatch(up):
    assert up._mode_phrase(_task(dispatch="manual")) == (
        "you perform it (Maestro preps everything, then hands you one action)"
    )


def test_mode_phrase_needs_dan_mode(up):
    assert up._mode_phrase(_task(mode="needs-dan")) == (
        "Maestro runs the prep, pauses for your manual step, then finishes"
    )


def test_mode_phrase_script_kind(up):
    assert up._mode_phrase(_task(kind="script")) == "deterministic script — Maestro runs it with no LLM"


def test_mode_phrase_default_autonomous(up):
    assert up._mode_phrase(_task()) == "Maestro runs it end-to-end autonomously"


def test_mode_phrase_manual_dispatch_wins_over_needs_dan(up):
    assert up._mode_phrase(_task(mode="needs-dan", dispatch="manual")) == (
        "you perform it (Maestro preps everything, then hands you one action)"
    )


# ===========================================================================================
# _extract_json
# ===========================================================================================

def test_extract_json_parses_a_bare_object(up):
    assert up._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_parses_a_fenced_json_block(up):
    text = "Sure, here:\n```json\n{\"a\": 1}\n```\nDone."
    assert up._extract_json(text) == {"a": 1}


def test_extract_json_digs_the_object_out_of_surrounding_prose(up):
    assert up._extract_json('Sure!\n{"a": {"b": [1, 2]}}\nHope that helps.') == {"a": {"b": [1, 2]}}


def test_extract_json_returns_none_when_there_is_no_object(up):
    assert up._extract_json("no json here") is None


def test_extract_json_greedy_regex_loses_both_objects_across_two_blobs(up):
    """FOUND_BUGS #184: `re.search(r"\\{.*\\}", ..., re.DOTALL)` is greedy from the first
    `{` to the last `}` — the same shape as `orchestrator_run.py`'s #23/#118."""
    assert up._extract_json('{"a": 1}\nand also {"b": 2}') is None


# ===========================================================================================
# _opus_complete — the LLM call site
# ===========================================================================================

def test_opus_complete_builds_the_claude_cli_argv(up, box, monkeypatch):
    calls = []

    def handler(argv, **kwargs):
        calls.append((argv, kwargs))
        return _completed(stdout="ok")

    _fake_subprocess(monkeypatch, up, handler)

    result = up._opus_complete("PROMPT TEXT")

    assert result == "ok"
    argv, kwargs = calls[0]
    assert argv == ["claude", "-p", "--model", up.JUDGE_MODEL, "PROMPT TEXT"]
    assert kwargs["cwd"] == str(up.REPO_ROOT)
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 240


def test_opus_complete_returns_the_stripped_stdout(up, box, monkeypatch):
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout="  hi \n"))
    assert up._opus_complete("p") == "hi"


def test_opus_complete_returns_empty_on_a_nonzero_exit_even_with_output(up, box, monkeypatch, capsys):
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(returncode=1, stdout="partial"))
    assert up._opus_complete("p") == ""
    assert "[opus] rc=1" in capsys.readouterr().err


def test_opus_complete_returns_empty_on_blank_stdout_at_exit_zero(up, box, monkeypatch):
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout="   \n"))
    assert up._opus_complete("p") == ""


def test_opus_complete_swallows_every_exception_including_a_missing_binary(up, box, monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError("claude")

    _fake_subprocess(monkeypatch, up, boom)

    assert up._opus_complete("p") == ""
    assert "[opus] call failed" in capsys.readouterr().err


def test_opus_complete_reports_a_timeout_as_an_empty_answer(up, box, monkeypatch, capsys):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=240)

    _fake_subprocess(monkeypatch, up, boom)

    assert up._opus_complete("p") == ""


# ===========================================================================================
# refresh_explanations
# ===========================================================================================

def test_refresh_explanations_writes_a_cache_entry_from_the_opus_reply(up, box, monkeypatch):
    task = _task(id="T1", title="A", short_desc="B", est_time="1h", deps=[])
    reply = json.dumps({"what_why": "W", "pipeline_fit": "P"})
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout=reply))

    n = up.refresh_explanations([task], {})

    assert n == 1
    entry = json.loads(box.cache.read_text())["T1"]
    assert entry["hash"] == up._task_fingerprint(task)
    assert entry["what_why"] == "W"
    assert entry["pipeline_fit"] == "P"
    assert entry["model"] == up.JUDGE_MODEL


def test_refresh_explanations_skips_a_fresh_cache_entry_without_calling_the_llm(up, box, monkeypatch):
    task = _task(id="T1")
    fp = up._task_fingerprint(task)
    box.cache.parent.mkdir(parents=True, exist_ok=True)
    box.cache.write_text(json.dumps({"T1": {"hash": fp, "what_why": "old", "pipeline_fit": "old"}}))

    def boom(*a, **k):
        raise AssertionError("must not call the LLM for a fresh cache entry")

    _fake_subprocess(monkeypatch, up, boom)

    assert up.refresh_explanations([task], {}) == 0


def test_refresh_explanations_force_rewrites_even_a_fresh_entry(up, box, monkeypatch):
    task = _task(id="T1")
    fp = up._task_fingerprint(task)
    box.cache.parent.mkdir(parents=True, exist_ok=True)
    box.cache.write_text(json.dumps({"T1": {"hash": fp, "what_why": "old", "pipeline_fit": "old"}}))
    reply = json.dumps({"what_why": "NEW", "pipeline_fit": "NEW"})
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout=reply))

    n = up.refresh_explanations([task], {}, force=True)

    assert n == 1
    assert json.loads(box.cache.read_text())["T1"]["what_why"] == "NEW"


def test_refresh_explanations_keeps_old_prose_when_the_reply_is_unusable(up, box, monkeypatch, capsys):
    task = _task(id="T1")
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout="not json at all"))

    n = up.refresh_explanations([task], {})

    assert n == 0
    assert not box.cache.exists()
    assert "no usable response" in capsys.readouterr().err


def test_refresh_explanations_skips_a_reply_missing_what_why(up, box, monkeypatch):
    task = _task(id="T1")
    reply = json.dumps({"pipeline_fit": "P only"})
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout=reply))

    assert up.refresh_explanations([task], {}) == 0


# ===========================================================================================
# render / _build
# ===========================================================================================

def test_render_at_a_glance_row_for_a_ready_task(up):
    tasks = [_task(id="T1", title="Do the thing", est_time="2h")]
    text = up.render(tasks, {}, set(), {}, {})
    assert "| **T1** | Do the thing | 🔵 Ready to run | 2h | — |" in text.splitlines()


def test_render_at_a_glance_row_flags_needs_you(up):
    tasks = [_task(id="T1", title="Manual step", mode="needs-dan", dan_action="click go")]
    text = up.render(tasks, {}, set(), {}, {})
    assert "| **T1** | Manual step | 🟠 Ready — needs your action | 2h | ✋ yes |" in text.splitlines()


def test_render_your_move_callout_present_when_action_prepared(up):
    tasks = [_task(id="T1", mode="needs-dan", dan_action="click go")]
    text = up.render(tasks, {}, set(), {}, {})
    assert "> **👉 Your move:** click go" in text.splitlines()


def test_render_your_move_callout_absent_when_no_action(up):
    tasks = [_task(id="T1")]
    text = up.render(tasks, {}, set(), {}, {})
    assert "👉 Your move" not in text


def test_render_terms_box_present_when_terms_are_detected(up):
    tasks = [_task(id="T1", short_desc="Improve RRF fusion of the legs")]
    text = up.render(tasks, {}, set(), {}, {})
    assert "<details><summary>📖 Terms in this task</summary>" in text
    assert "- **RRF (Reciprocal Rank Fusion)** —" in text


def test_render_terms_box_absent_when_no_terms_detected(up):
    tasks = [_task(id="T1", short_desc="unrelated repo cleanup")]
    text = up.render(tasks, {}, set(), {}, {})
    assert "Terms in this task" not in text


def test_render_glossary_lists_every_term_once_in_order(up):
    text = up.render([], {}, set(), {}, {})
    for g in up.GLOSSARY:
        assert f"- **{g['term']}** — {g['definition']}" in text.splitlines()


def test_render_deep_brief_link_when_detail_path_known(up):
    tasks = [_task(id="T1")]
    text = up.render(tasks, {}, set(), {"T1": "retired/tasks/Phase01.md"}, {})
    assert "_Deep technical brief: `retired/tasks/Phase01.md`." in text


def test_render_excludes_completed_tasks_from_the_pending_view(up):
    tasks = [_task(id="T1"), _task(id="T2")]
    text = up.render(tasks, {}, {"T1"}, {}, {})
    assert "## T2 — Title" in text
    assert "## T1 — Title" not in text


def test_build_raises_when_roadmap_is_missing(up, box):
    with pytest.raises(SystemExit):
        up._build()


def test_build_raises_when_roadmap_has_no_task_blocks(up, box):
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text("# ROADMAP\nnothing here\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        up._build()


def test_build_returns_the_parsed_tasks_and_rendered_markdown(up, box):
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text(
        "```yaml\nid: T1\ntitle: A\nshort_desc: d\nest_time: 1h\nmode: autonomous\ndeps: []\n```\n",
        encoding="utf-8",
    )

    tasks, rendered = up._build()

    assert [t["id"] for t in tasks] == ["T1"]
    assert "## T1 — A" in rendered


# ===========================================================================================
# main() — CLI
# ===========================================================================================

_MINIMAL_ROADMAP = (
    "```yaml\nid: T1\ntitle: A\nshort_desc: d\nest_time: 1h\nmode: autonomous\ndeps: []\n```\n"
)


def test_main_check_exits_1_when_upcoming_is_stale(up, box, monkeypatch, capsys):
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text(_MINIMAL_ROADMAP, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gen_upcoming.py", "--check"])

    rc = up.main()

    assert rc == 1
    assert "STALE" in capsys.readouterr().err


def test_main_check_exits_0_when_upcoming_is_up_to_date(up, box, monkeypatch, capsys):
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text(_MINIMAL_ROADMAP, encoding="utf-8")
    _, rendered = up._build()
    box.upcoming.parent.mkdir(parents=True, exist_ok=True)
    box.upcoming.write_text(rendered, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gen_upcoming.py", "--check"])

    rc = up.main()

    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_main_writes_upcoming_and_prints_task_ids(up, box, monkeypatch, capsys):
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text(_MINIMAL_ROADMAP, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gen_upcoming.py"])

    rc = up.main()

    assert rc == 0
    assert box.upcoming.exists()
    out = capsys.readouterr().out
    assert "Wrote docs/UPCOMING.md from 1 pending task(s)." in out
    assert "Tasks: T1" in out


def test_main_refresh_explanations_flag_calls_the_llm_before_writing(up, box, monkeypatch):
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text(_MINIMAL_ROADMAP, encoding="utf-8")
    reply = json.dumps({"what_why": "W", "pipeline_fit": "P"})
    _fake_subprocess(monkeypatch, up, lambda *a, **k: _completed(stdout=reply))
    monkeypatch.setattr(sys, "argv", ["gen_upcoming.py", "--refresh-explanations"])

    rc = up.main()

    assert rc == 0
    cache = json.loads(box.cache.read_text())
    assert cache["T1"]["what_why"] == "W"
