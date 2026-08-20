"""Pinned behaviour of the implementer layer: briefs, question gating, launch, sentinels.

Characterisation, not specification. Where the reference implementation does something
surprising the test pins the surprise and the structured result reports it; nothing here
asserts what the code *should* do.

Nothing in this file starts a real agent, a real tmux window or a real network call:

* ``launch_implementer`` and ``_ask_question`` get a fake ``subprocess.run`` and are
  asserted on the exact argv/shell string, the keyword arguments and the files they leave
  behind. The generated ``launch.py`` is parsed with ``ast`` so the *agent* argv is pinned
  structurally rather than by substring;
* everything else is pure text or reads/writes inside the ``sandbox`` tree.

Generated prose is asserted by section contract (the headings and the interpolated values
that a consumer depends on), so harmless rewording does not false-fail. Structural things —
dict keys, journal event names, sentinel file names, argv, return values — are exact.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro.backends import registry

pytestmark = pytest.mark.maestro_module("implementer")


def _is_maestro(subject) -> bool:
    """True for the extracted package, false for the legacy reference module. Mirrors
    `test_commands.py`'s `_is_maestro`: only used to branch an assertion whose *shape*
    is shared but whose exact command line differs — the reference always spawns
    `scripts/send_dan_request.py`, maestro spawns `python -m maestro.hitl.dan_request`."""
    return getattr(subject, "__name__", "").split(".")[0] == "maestro"


# --- fakes and helpers -------------------------------------------------------------


class _Runs:
    """Stand-in for subprocess.run recording positional args verbatim (argv *or* shell str)."""

    def __init__(self, handler=None):
        self.calls: list[SimpleNamespace] = []
        self._handler = handler

    def __call__(self, *args, **kwargs):
        self.calls.append(SimpleNamespace(args=args, kwargs=dict(kwargs)))
        if self._handler is not None:
            return self._handler(args, kwargs)
        return subprocess.CompletedProcess(args[0] if args else [], 0, b"", b"")

    @property
    def first(self) -> SimpleNamespace:
        assert self.calls, "subprocess.run was never called"
        return self.calls[0]


def _fake_run(monkeypatch, handler=None) -> _Runs:
    runs = _Runs(handler)
    monkeypatch.setattr(subprocess, "run", runs)
    return runs


def _journal(sandbox) -> list[dict]:
    path = sandbox.orch_dir / "journal.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _events(sandbox) -> list[str]:
    return [record["event"] for record in _journal(sandbox)]


def _workspace(sandbox, name: str = "impl-T1-1") -> Path:
    ws = sandbox.workspaces / name
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _argv_from_run_call(tree: ast.AST) -> list[str] | None:
    """``subprocess.run([...])`` spelled as a literal list in the generated source."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            out = []
            for element in node.args[0].elts:
                if isinstance(element, ast.Constant):
                    out.append(element.value)
                elif isinstance(element, ast.Name):
                    out.append(f"<{element.id}>")
                else:  # pragma: no cover - defensive
                    out.append(f"<{type(element).__name__}>")
            return out
    return None


def _argv_from_json_header(tree: ast.AST) -> list[str] | None:
    """``ARGV = json.loads('[...]')`` — the other launcher shape a driver may generate.

    A launcher that has to watch its own output stream cannot inline the argv as source,
    so it embeds it as JSON; the first such assignment is the invocation, any later one
    is a fallback.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        name = ast.unparse(call.func)
        if name != "json.loads" or len(call.args) != 1:
            continue
        if not (isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
            continue
        try:
            decoded = json.loads(call.args[0].value)
        except ValueError:  # pragma: no cover - defensive
            continue
        if isinstance(decoded, list) and all(isinstance(e, str) for e in decoded):
            return decoded
    return None


def _agent_argv(launch_py: Path, brief: str = "") -> list[str]:
    """The argv the generated launch.py would hand the agent CLI.

    Parsed rather than string-matched, and it understands both launcher shapes M2 can
    produce: a literal ``subprocess.run([...])`` list and an ``ARGV = json.loads(...)``
    header. Constants come through as themselves; the brief — a variable in one shape,
    an inlined string in the other — comes through as ``<brief>`` either way when the
    caller passes the brief text.
    """
    tree = ast.parse(launch_py.read_text(encoding="utf-8"))
    argv = _argv_from_run_call(tree)
    if argv is None:
        argv = _argv_from_json_header(tree)
    if argv is None:
        raise AssertionError("generated launch.py hands no argv to an agent CLI")
    return ["<brief>" if brief and element == brief else element for element in argv]


TASK = {"id": "T1", "title": "Do a thing", "short_desc": "the description"}


# --- backend selection -------------------------------------------------------------
#
# The `implementer` role resolves through `maestro.roles`, which reads the project.yaml
# the `sandbox` fixture has already rebased into the temp tree. Writing that file is
# therefore the whole of "launch under this backend"; nothing here reaches a real repo,
# a real binary or a real agent.

#: Backend the reference module can produce an invocation for. It has one and only one.
_LEGACY_BACKEND = "claude"

#: A model to configure per backend, where the task-level `model:` allowlist (which holds
#: Claude model ids) cannot say anything meaningful.
_CONFIGURED_MODELS = {"codex": "gpt-5.4-codex"}


def _configure_backend(sandbox, backend: str) -> None:
    """Point the `implementer` role at `backend` through the sandbox's project.yaml."""
    lines = ["roles:", "  implementer:", f"    backend: {backend}"]
    model = _CONFIGURED_MODELS.get(backend)
    if model:
        lines += ["    models:", f"      {backend}: {model}"]
    (sandbox.repo / "project.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _delegates_to_a_driver(subject) -> bool:
    """True for the extracted package, which resolves a driver per `maestro.roles`.

    The reference module is a single file that predates backend drivers: it ignores the
    `roles:` block entirely. So this is a statement about which *subject* is running, not
    about which backend is configured — no test here branches on a backend name.
    """
    return getattr(subject, "__name__", "").startswith("maestro.")


#: backend -> the exact argv its driver hands the CLI, given (subject, launch, session).
_AGENT_ARGV = {
    "claude": lambda subject, out, sess: [
        "claude", "-p",
        "--model", subject._DEFAULT_IMPLEMENTER_MODEL,
        "--session-id", sess,
        "<brief>",
    ],
    "codex": lambda subject, out, sess: [
        "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "-c", 'approval_policy="never"',
        "-m", _CONFIGURED_MODELS["codex"],
        "-s", "workspace-write",
        "-C", str(out.worktree),
        "<brief>",
    ],
}

# The ordinary brief's resumable heading. Qualified because the prep brief contains an
# unrelated "CHECKPOINT-RESUMABLE" paragraph that a bare "RESUMABLE" would match.
RESUMABLE_HEADING = "RESUMABLE (multi-day"


# ===================================================================================
# _task_questions
# ===================================================================================


def test_task_questions_absent_block_is_empty(subject):
    assert subject._task_questions({}) == []


@pytest.mark.parametrize("raw", [None, "q1", 7, {"id": "q1", "prompt": "p"}])
def test_task_questions_non_list_block_is_empty(subject, raw):
    assert subject._task_questions({"questions": raw}) == []


def test_task_questions_normalizes_to_three_keys(subject):
    got = subject._task_questions({"questions": [{"id": "q1", "prompt": "Which?"}]})
    assert got == [{"id": "q1", "prompt": "Which?", "options": None}]
    assert set(got[0]) == {"id", "prompt", "options"}


def test_task_questions_strips_whitespace_from_id_prompt_and_options(subject):
    got = subject._task_questions(
        {"questions": [{"id": "  q1 ", "prompt": "  Which? ", "options": ["  a", "b  "]}]}
    )
    assert got == [{"id": "q1", "prompt": "Which?", "options": ["a", "b"]}]


@pytest.mark.parametrize(
    "entry",
    [
        {"prompt": "no id"},
        {"id": "q1"},
        {"id": "   ", "prompt": "blank id"},
        {"id": "q1", "prompt": "   "},
        {"id": None, "prompt": "p"},
        {"id": "q1", "prompt": None},
        "not-a-dict",
        None,
        ["q1", "p"],
    ],
)
def test_task_questions_silently_drops_malformed_entries(subject, entry):
    assert subject._task_questions({"questions": [entry]}) == []


def test_task_questions_drops_only_the_bad_entries(subject):
    got = subject._task_questions(
        {"questions": [{"id": "q1", "prompt": "a"}, "junk", {"prompt": "no id"},
                       {"id": "q2", "prompt": "b"}]}
    )
    assert [q["id"] for q in got] == ["q1", "q2"]


def test_task_questions_preserves_declaration_order_and_duplicates(subject):
    got = subject._task_questions(
        {"questions": [{"id": "q2", "prompt": "b"}, {"id": "q1", "prompt": "a"},
                       {"id": "q2", "prompt": "c"}]}
    )
    assert [q["id"] for q in got] == ["q2", "q1", "q2"]


def test_task_questions_coerces_non_string_ids_and_prompts(subject):
    got = subject._task_questions({"questions": [{"id": 12, "prompt": 34}]})
    assert got == [{"id": "12", "prompt": "34", "options": None}]


def test_task_questions_drops_a_falsy_numeric_id(subject):
    """Surprise: `str(q.get("id") or "")` means the perfectly good id `0` is discarded."""
    assert subject._task_questions({"questions": [{"id": 0, "prompt": "p"}]}) == []


@pytest.mark.parametrize("opts", [[], "a,b", {"a": 1}, 5, None])
def test_task_questions_non_list_or_empty_options_become_none(subject, opts):
    got = subject._task_questions({"questions": [{"id": "q1", "prompt": "p", "options": opts}]})
    assert got[0]["options"] is None


def test_task_questions_stringifies_option_values(subject):
    got = subject._task_questions(
        {"questions": [{"id": "q1", "prompt": "p", "options": [1, True, None]}]}
    )
    assert got[0]["options"] == ["1", "True", "None"]


# ===================================================================================
# _question_req_id
# ===================================================================================


def test_question_req_id_shape(subject):
    assert subject._question_req_id("T1", "q1") == "q-T1-q1"


def test_question_req_id_is_deterministic(subject):
    assert subject._question_req_id("T1", "q1") == subject._question_req_id("T1", "q1")


def test_question_req_id_varies_with_both_inputs(subject):
    assert subject._question_req_id("T1", "q1") != subject._question_req_id("T2", "q1")
    assert subject._question_req_id("T1", "q1") != subject._question_req_id("T1", "q2")


def test_question_req_id_collides_across_the_hyphen_boundary(subject):
    """Surprise: the id is a plain `-` join, so (`a-b`, `c`) and (`a`, `b-c`) share one id.

    Both would be parked as the same Dan-request, so the second question is never asked and
    the first question's answer is silently reused for it.
    """
    assert subject._question_req_id("a-b", "c") == subject._question_req_id("a", "b-c")


def test_question_req_id_does_not_sanitise_path_separators(subject):
    """Surprise: the result is used as a filename, but a `/` in either half is kept."""
    assert subject._question_req_id("a/b", "q1") == "q-a/b-q1"


# ===================================================================================
# _answer_choice
# ===================================================================================


def _answer(subject, req_id: str, text: str) -> Path:
    subject.QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = subject.QUESTIONS_DIR / f"{req_id}.answer"
    path.write_text(text, encoding="utf-8")
    return path


def test_answer_choice_missing_file_is_none(subject, sandbox):
    assert subject._answer_choice("q-T1-q1") is None


def test_answer_choice_reads_the_answer_suffix_beside_questions_dir(subject, sandbox):
    _answer(subject, "q-T1-q1", '{"choice": "yes"}')
    assert (subject.QUESTIONS_DIR / "q-T1-q1.answer").is_file()
    assert subject._answer_choice("q-T1-q1") == "yes"


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_answer_choice_blank_file_is_none(subject, sandbox, text):
    _answer(subject, "q-T1-q1", text)
    assert subject._answer_choice("q-T1-q1") is None


def test_answer_choice_bare_string_fallback(subject, sandbox):
    _answer(subject, "q-T1-q1", "  use the small model  \n")
    assert subject._answer_choice("q-T1-q1") == "use the small model"


def test_answer_choice_dict_without_choice_key_is_none(subject, sandbox):
    _answer(subject, "q-T1-q1", '{"other": "yes"}')
    assert subject._answer_choice("q-T1-q1") is None


def test_answer_choice_dict_with_null_choice_is_none(subject, sandbox):
    _answer(subject, "q-T1-q1", '{"choice": null}')
    assert subject._answer_choice("q-T1-q1") is None


def test_answer_choice_stringifies_a_non_string_choice(subject, sandbox):
    _answer(subject, "q-T1-q1", '{"choice": 3}')
    assert subject._answer_choice("q-T1-q1") == "3"


def test_answer_choice_empty_choice_is_an_empty_string_not_none(subject, sandbox):
    """Surprise: `""` counts as answered for the gate but renders as "no answer" in the brief.

    `_questions_ready` only tests `is None`, so the task dispatches; `_answers_section`
    tests truthiness, so the implementer is told the answer was never recorded.
    """
    _answer(subject, "q-T1-q1", '{"choice": ""}')
    assert subject._answer_choice("q-T1-q1") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("null", "None"),
        ("true", "True"),
        ("false", "False"),
        ("7", "7"),
        ('"yes"', "yes"),
        ("[1, 2]", "[1, 2]"),
    ],
)
def test_answer_choice_stringifies_bare_json_scalars(subject, sandbox, raw, expected):
    """Surprise: a JSON `null` answer becomes the literal text "None", not an absent answer."""
    _answer(subject, "q-T1-q1", raw)
    assert subject._answer_choice("q-T1-q1") == expected


def test_answer_choice_malformed_json_is_returned_verbatim(subject, sandbox):
    _answer(subject, "q-T1-q1", "{not json")
    assert subject._answer_choice("q-T1-q1") == "{not json"


def test_answer_choice_directory_raises(subject, sandbox):
    """Surprise: the only guard is `.exists()`, so a directory of that name raises."""
    (subject.QUESTIONS_DIR / "q-T1-q1.answer").mkdir(parents=True)
    with pytest.raises(OSError):
        subject._answer_choice("q-T1-q1")


# ===================================================================================
# _ask_question
# ===================================================================================


def _park_json(subject, req_id: str, payload: dict | None = None) -> Path:
    subject.QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = subject.QUESTIONS_DIR / f"{req_id}.json"
    path.write_text(json.dumps(payload or {"id": req_id, "status": "pending"}), encoding="utf-8")
    return path


def _parks(subject, returncode=0):
    """A subprocess.run stub that behaves like a successful send_dan_request.py."""

    def handler(args, kwargs):
        argv = args[0]
        req_id = argv[argv.index("--id") + 1]
        if returncode == 0:
            _park_json(subject, req_id)
        return subprocess.CompletedProcess(argv, returncode, b"", b"")

    return handler


def test_ask_question_argv_and_kwargs(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which model?"})
    argv = runs.first.args[0]
    assert argv[0] == str(subject.VENV_PYTHON)
    if _is_maestro(subject):
        assert argv[1] == "-m" and argv[2] == "maestro.hitl.dan_request"
        rest = argv[3:]
    else:
        assert argv[1] == str(subject.SEND_DANREQ)
        rest = argv[2:]
    assert rest[0:4] == ["--type", "manual-task", "--id", "q-T1-q1"]
    assert rest[4] == "--question"
    assert "T1" in rest[5] and "q1" in rest[5] and "Which model?" in rest[5]
    assert "--options" not in argv
    assert runs.first.kwargs == {"cwd": str(subject.REPO), "capture_output": True}


def test_ask_question_passes_options_comma_joined(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?", "options": ["a", "b", "c"]})
    argv = runs.first.args[0]
    assert argv[argv.index("--options") + 1] == "a,b,c"


def test_ask_question_options_are_ambiguous_when_an_option_contains_a_comma(
    subject, sandbox, monkeypatch
):
    """Surprise: options are comma-joined with no escaping, so one option becomes two."""
    runs = _fake_run(monkeypatch, _parks(subject))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?", "options": ["a,b", "c"]})
    argv = runs.first.args[0]
    assert argv[argv.index("--options") + 1] == "a,b,c"


def test_ask_question_empty_options_list_is_omitted(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?", "options": []})
    assert "--options" not in runs.first.args[0]


def test_ask_question_journals_only_on_a_successful_park(subject, sandbox, monkeypatch):
    _fake_run(monkeypatch, _parks(subject))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})
    records = _journal(sandbox)
    assert [r["event"] for r in records] == ["questions_asked"]
    assert records[0]["detail"] == "T1 q1 req=q-T1-q1"
    assert records[0]["session_id"] == ""


def test_ask_question_does_not_journal_when_the_cap_refuses(subject, sandbox, monkeypatch):
    _fake_run(monkeypatch, _parks(subject, returncode=1))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})
    assert _events(sandbox) == []


def test_ask_question_does_not_journal_when_no_request_file_appears(
    subject, sandbox, monkeypatch
):
    _fake_run(monkeypatch)  # rc==0 but writes nothing
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})
    assert _events(sandbox) == []


def test_ask_question_journals_when_the_stub_returns_none(subject, sandbox, monkeypatch):
    """Pinned: `getattr(res, "returncode", 0)` exists so a None-returning stub still journals."""

    def handler(args, kwargs):
        _park_json(subject, args[0][args[0].index("--id") + 1])
        return None

    _fake_run(monkeypatch, handler)
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})
    assert _events(sandbox) == ["questions_asked"]


def test_ask_question_is_idempotent_when_already_parked(subject, sandbox, monkeypatch):
    _park_json(subject, "q-T1-q1")
    runs = _fake_run(monkeypatch, _parks(subject))
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})
    assert runs.calls == []
    assert _events(sandbox) == []


def test_ask_question_does_not_create_the_questions_directory(subject, sandbox, monkeypatch):
    """Surprise: the helper never mkdirs QUESTIONS_DIR — it relies on the sender to do it."""
    _fake_run(monkeypatch)
    assert not subject.QUESTIONS_DIR.exists()
    subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})
    assert not subject.QUESTIONS_DIR.exists()


def test_ask_question_requires_id_and_prompt_keys(subject, sandbox, monkeypatch):
    _fake_run(monkeypatch)
    with pytest.raises(KeyError):
        subject._ask_question("T1", {"prompt": "Which?"})
    with pytest.raises(KeyError):
        subject._ask_question("T1", {"id": "q1"})


def test_ask_question_propagates_a_missing_sender_binary(subject, sandbox, monkeypatch):
    """Surprise: no try/except around the sender — an OSError escapes into the launch loop."""

    def boom(*args, **kwargs):
        raise FileNotFoundError(str(subject.VENV_PYTHON))

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        subject._ask_question("T1", {"id": "q1", "prompt": "Which?"})


# ===================================================================================
# _questions_ready
# ===================================================================================


def test_questions_ready_true_and_silent_when_no_questions(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    assert subject._questions_ready({"id": "T1"}) is True
    assert runs.calls == []


def test_questions_ready_true_when_every_question_is_answered(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    _answer(subject, "q-T1-q1", "a")
    _answer(subject, "q-T1-q2", "b")
    task = {"id": "T1", "questions": [{"id": "q1", "prompt": "p1"}, {"id": "q2", "prompt": "p2"}]}
    assert subject._questions_ready(task) is True
    assert runs.calls == []


def test_questions_ready_false_and_asks_only_the_first_unanswered(
    subject, sandbox, monkeypatch
):
    runs = _fake_run(monkeypatch, _parks(subject))
    task = {"id": "T1", "questions": [{"id": "q1", "prompt": "p1"}, {"id": "q2", "prompt": "p2"}]}
    assert subject._questions_ready(task) is False
    assert len(runs.calls) == 1
    assert "q-T1-q1" in runs.first.args[0]
    assert not (subject.QUESTIONS_DIR / "q-T1-q2.json").exists()


def test_questions_ready_advances_to_the_next_question_once_answered(
    subject, sandbox, monkeypatch
):
    runs = _fake_run(monkeypatch, _parks(subject))
    _answer(subject, "q-T1-q1", "a")
    task = {"id": "T1", "questions": [{"id": "q1", "prompt": "p1"}, {"id": "q2", "prompt": "p2"}]}
    assert subject._questions_ready(task) is False
    assert "q-T1-q2" in runs.first.args[0]


def test_questions_ready_reasks_nothing_on_a_second_cycle(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    task = {"id": "T1", "questions": [{"id": "q1", "prompt": "p1"}]}
    assert subject._questions_ready(task) is False
    assert subject._questions_ready(task) is False
    assert len(runs.calls) == 1
    assert _events(sandbox) == ["questions_asked"]


def test_questions_ready_ignores_malformed_questions(subject, sandbox, monkeypatch):
    """A `questions:` block that normalizes to nothing gates nothing."""
    runs = _fake_run(monkeypatch, _parks(subject))
    assert subject._questions_ready({"id": "T1", "questions": ["junk", {"id": "q1"}]}) is True
    assert runs.calls == []


def test_questions_ready_coerces_a_non_string_task_id(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    assert subject._questions_ready({"id": 7, "questions": [{"id": "q1", "prompt": "p"}]}) is False
    assert "q-7-q1" in runs.first.args[0]


def test_questions_ready_raises_on_a_task_without_an_id(subject, sandbox, monkeypatch):
    """Surprise: `task["id"]` is read before the questions block, so even a question-free
    task raises rather than returning True."""
    _fake_run(monkeypatch, _parks(subject))
    with pytest.raises(KeyError):
        subject._questions_ready({})


# ===================================================================================
# _answers_section
# ===================================================================================


def test_answers_section_empty_without_questions(subject, sandbox):
    assert subject._answers_section({"id": "T1"}) == ""
    assert subject._answers_section({"id": "T1", "questions": []}) == ""
    assert subject._answers_section({"id": "T1", "questions": "junk"}) == ""


def test_answers_section_renders_every_question_and_answer(subject, sandbox):
    _answer(subject, "q-T1-q1", '{"choice": "small"}')
    _answer(subject, "q-T1-q2", "free text")
    task = {
        "id": "T1",
        "questions": [{"id": "q1", "prompt": "Which model?"},
                      {"id": "q2", "prompt": "Which dataset?"}],
    }
    text = subject._answers_section(task)
    assert text.startswith("\n\n")
    assert "ANSWERS FROM DAN" in text
    for token in ("q1", "Which model?", "small", "q2", "Which dataset?", "free text"):
        assert token in text


def test_answers_section_marks_unanswered_questions(subject, sandbox):
    text = subject._answers_section({"id": "T1", "questions": [{"id": "q1", "prompt": "P?"}]})
    assert "(no answer recorded)" in text


def test_answers_section_marks_an_empty_answer_as_unrecorded(subject, sandbox):
    """Surprise: `or` on the answer text, so a deliberately empty answer reads as missing."""
    _answer(subject, "q-T1-q1", '{"choice": ""}')
    text = subject._answers_section({"id": "T1", "questions": [{"id": "q1", "prompt": "P?"}]})
    assert "(no answer recorded)" in text


def test_answers_section_has_one_line_pair_per_question(subject, sandbox):
    task = {"id": "T1", "questions": [{"id": f"q{i}", "prompt": f"p{i}"} for i in range(3)]}
    text = subject._answers_section(task)
    assert sum(1 for line in text.splitlines() if line.startswith("  q")) == 3


def test_answers_section_does_not_ask_anything(subject, sandbox, monkeypatch):
    runs = _fake_run(monkeypatch, _parks(subject))
    subject._answers_section({"id": "T1", "questions": [{"id": "q1", "prompt": "P?"}]})
    assert runs.calls == []
    assert not (subject.QUESTIONS_DIR / "q-T1-q1.json").exists()


def test_answers_section_raises_on_a_task_without_an_id_only_when_questions_exist(
    subject, sandbox
):
    assert subject._answers_section({}) == ""
    with pytest.raises(KeyError):
        subject._answers_section({"questions": [{"id": "q1", "prompt": "P?"}]})


# ===================================================================================
# _make_brief
# ===================================================================================


def test_make_brief_contains_the_paths_and_task_identity(subject, sandbox, tmp_path):
    ws, wt = tmp_path / "ws", tmp_path / "wt"
    brief = subject._make_brief(TASK, ws, wt)
    for token in ("T1", "Do a thing", "the description", str(ws), str(wt),
                  str(subject.VENV_PYTHON), str(subject.REPO)):
        assert token in brief


def test_make_brief_declares_the_result_and_sentinel_contract(subject, sandbox, tmp_path):
    brief = subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    for token in ("result.json", "DONE", "FAILED", "BLOCKED", "NEEDS-REPLAN",
                  "WORKTREE", "WORKSPACE", "TASK DESCRIPTION", "DELIVERABLES", "GUARDRAILS"):
        assert token in brief


def test_make_brief_defaults_the_title_to_the_task_id(subject, sandbox, tmp_path):
    brief = subject._make_brief({"id": "T9"}, tmp_path / "ws", tmp_path / "wt")
    assert "T9: T9" in brief


def test_make_brief_requires_an_id(subject, sandbox, tmp_path):
    with pytest.raises(KeyError):
        subject._make_brief({}, tmp_path / "ws", tmp_path / "wt")


def test_make_brief_missing_short_desc_renders_as_blank(subject, sandbox, tmp_path):
    brief = subject._make_brief({"id": "T9"}, tmp_path / "ws", tmp_path / "wt")
    assert "TASK DESCRIPTION" in brief


def test_make_brief_retry_note_is_optional_and_verbatim(subject, sandbox, tmp_path):
    plain = subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    assert "NOTE FROM ORCHESTRATOR" not in plain
    noted = subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt",
                                "previous attempt timed out")
    assert "NOTE FROM ORCHESTRATOR" in noted
    assert "previous attempt timed out" in noted


def test_make_brief_empty_retry_note_adds_nothing(subject, sandbox, tmp_path):
    assert subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt", "") == \
        subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt")


def test_make_brief_omits_the_verification_section_when_there_are_none(
    subject, sandbox, tmp_path
):
    for verifs in ([], None):
        brief = subject._make_brief({**TASK, "verifications": verifs},
                                    tmp_path / "ws", tmp_path / "wt")
        assert "VERIFICATIONS" not in brief


def test_make_brief_renders_an_auto_verification_with_cmd_and_expect(
    subject, sandbox, tmp_path
):
    task = {**TASK, "verifications": [
        {"id": "V1", "kind": "auto", "check": "tests pass", "cmd": "pytest -q", "expect": "ok"}
    ]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "VERIFICATIONS" in brief
    assert "V1 [auto]: tests pass" in brief
    assert "cmd: pytest -q" in brief
    assert "expect: 'ok'" in brief  # `!r` — the expectation is repr'd, not interpolated raw


def test_make_brief_renders_a_manual_verification_without_cmd(subject, sandbox, tmp_path):
    task = {**TASK, "verifications": [{"id": "V2", "kind": "manual", "check": "eyeball it"}]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "V2 [manual]: eyeball it" in brief
    assert "cmd:" not in brief


def test_make_brief_treats_an_unknown_kind_as_manual(subject, sandbox, tmp_path):
    """Surprise: only `auto` is special — `kind: automatic` silently becomes a manual item."""
    task = {**TASK, "verifications": [{"id": "V3", "kind": "automatic", "check": "c",
                                       "cmd": "run me"}]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "V3 [manual]: c" in brief
    assert "run me" not in brief


def test_make_brief_verification_defaults_are_placeholders(subject, sandbox, tmp_path):
    task = {**TASK, "verifications": [{}]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "? [manual]: " in brief


def test_make_brief_auto_verification_missing_cmd_renders_empty(subject, sandbox, tmp_path):
    task = {**TASK, "verifications": [{"id": "V1", "kind": "auto", "check": "c"}]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "cmd: \n" in brief
    assert "expect: ''" in brief


@pytest.mark.parametrize("verifs", [{"V1": {"check": "c"}}, "V1"])
def test_make_brief_raises_on_a_non_list_verifications_block(
    subject, sandbox, tmp_path, verifs
):
    """Surprise: a dict/str `verifications:` block iterates its keys/chars and blows up."""
    with pytest.raises(AttributeError):
        subject._make_brief({**TASK, "verifications": verifs},
                            tmp_path / "ws", tmp_path / "wt")


def test_make_brief_adds_the_hitl_note_only_for_needs_dan(subject, sandbox, tmp_path):
    plain = subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    assert "HITL NOTE" not in plain
    hitl = subject._make_brief({**TASK, "mode": "needs-dan"}, tmp_path / "ws", tmp_path / "wt")
    assert "HITL NOTE" in hitl
    other = subject._make_brief({**TASK, "mode": "autonomous"},
                                tmp_path / "ws", tmp_path / "wt")
    assert "HITL NOTE" not in other


def test_make_brief_adds_the_resumable_note_for_any_truthy_flag(subject, sandbox, tmp_path):
    assert RESUMABLE_HEADING not in subject._make_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    for flag in (True, "yes", 1):
        brief = subject._make_brief({**TASK, "resumable": flag},
                                    tmp_path / "ws", tmp_path / "wt")
        assert RESUMABLE_HEADING in brief
    for flag in (False, "", 0, None):
        brief = subject._make_brief({**TASK, "resumable": flag},
                                    tmp_path / "ws", tmp_path / "wt")
        assert RESUMABLE_HEADING not in brief


def test_make_brief_appends_the_answers_section(subject, sandbox, tmp_path):
    _answer(subject, "q-T1-q1", '{"choice": "small"}')
    task = {**TASK, "questions": [{"id": "q1", "prompt": "Which model?"}]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "ANSWERS FROM DAN" in brief
    assert "small" in brief


def test_make_brief_sections_appear_in_a_fixed_order(subject, sandbox, tmp_path):
    _answer(subject, "q-T1-q1", "an answer")
    task = {**TASK, "mode": "needs-dan", "resumable": True,
            "verifications": [{"id": "V1", "kind": "manual", "check": "c"}],
            "questions": [{"id": "q1", "prompt": "P?"}]}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt", "retry once")
    order = [brief.index(marker) for marker in
             ("VERIFICATIONS", "NOTE FROM ORCHESTRATOR", "HITL NOTE", RESUMABLE_HEADING,
              "ANSWERS FROM DAN")]
    assert order == sorted(order)


def test_make_brief_delegates_a_dispatch_manual_task_to_the_prep_brief(
    subject, sandbox, tmp_path
):
    task = {**TASK, "dispatch": "manual"}
    assert subject._make_brief(task, tmp_path / "ws", tmp_path / "wt") == \
        subject._make_prep_brief(task, tmp_path / "ws", tmp_path / "wt")


def test_make_brief_dispatch_delegation_is_case_sensitive(subject, sandbox, tmp_path):
    """Surprise: `dispatch: Manual` is not normalized, so it gets the ordinary brief."""
    brief = subject._make_brief({**TASK, "dispatch": "Manual"}, tmp_path / "ws", tmp_path / "wt")
    assert "PREP implementer" not in brief


def test_make_brief_prep_delegation_forwards_the_retry_note(subject, sandbox, tmp_path):
    task = {**TASK, "dispatch": "manual"}
    brief = subject._make_brief(task, tmp_path / "ws", tmp_path / "wt", "second attempt")
    assert "second attempt" in brief


def test_make_brief_is_pure_and_writes_nothing(subject, sandbox, tmp_path):
    ws, wt = tmp_path / "ws", tmp_path / "wt"
    subject._make_brief(TASK, ws, wt)
    assert not ws.exists() and not wt.exists()


# ===================================================================================
# _make_prep_brief
# ===================================================================================


def test_prep_brief_contains_identity_paths_and_schema(subject, sandbox, tmp_path):
    ws, wt = tmp_path / "ws", tmp_path / "wt"
    brief = subject._make_prep_brief(TASK, ws, wt)
    for token in ("T1", "Do a thing", "the description", str(ws), str(wt),
                  str(subject.VENV_PYTHON), "dan_action", "post_action_cmd",
                  "result.json", "DONE", "FAILED", "DELIVERABLES"):
        assert token in brief


def test_prep_brief_schema_interpolates_the_task_id(subject, sandbox, tmp_path):
    brief = subject._make_prep_brief({"id": "P8b"}, tmp_path / "ws", tmp_path / "wt")
    assert '"task_id":"P8b"' in brief


def test_prep_brief_status_set_excludes_needs_replan(subject, sandbox, tmp_path):
    """Pinned: a prep task may not ask for a replan — the ordinary brief's fourth status
    is absent from the prep schema."""
    brief = subject._make_prep_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    assert '"status":"DONE|FAILED|BLOCKED"' in brief
    assert "NEEDS-REPLAN" not in brief


def test_prep_brief_defaults_the_title_to_the_task_id(subject, sandbox, tmp_path):
    assert "T9: T9" in subject._make_prep_brief({"id": "T9"}, tmp_path / "ws", tmp_path / "wt")


def test_prep_brief_requires_an_id(subject, sandbox, tmp_path):
    with pytest.raises(KeyError):
        subject._make_prep_brief({}, tmp_path / "ws", tmp_path / "wt")


def test_prep_brief_echoes_a_known_dan_action_as_a_hint(subject, sandbox, tmp_path):
    brief = subject._make_prep_brief({**TASK, "dan_action": "open the notebook"},
                                     tmp_path / "ws", tmp_path / "wt")
    assert "ROADMAP already suggests" in brief
    assert "'open the notebook'" in brief  # `!r`


def test_prep_brief_echoes_a_known_post_action_cmd(subject, sandbox, tmp_path):
    brief = subject._make_prep_brief({**TASK, "post_action_cmd": "make import"},
                                     tmp_path / "ws", tmp_path / "wt")
    assert "'make import'" in brief


def test_prep_brief_omits_hints_when_absent(subject, sandbox, tmp_path):
    brief = subject._make_prep_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    assert "ROADMAP already suggests" not in brief


def test_prep_brief_retry_note_is_appended(subject, sandbox, tmp_path):
    brief = subject._make_prep_brief(TASK, tmp_path / "ws", tmp_path / "wt", "try again")
    assert "NOTE FROM ORCHESTRATOR" in brief
    assert brief.rstrip().endswith("try again")


def test_prep_brief_forbids_the_human_step(subject, sandbox, tmp_path):
    brief = subject._make_prep_brief(TASK, tmp_path / "ws", tmp_path / "wt")
    assert "DO NOT" in brief


def test_prep_brief_ignores_needs_dan_and_resumable_flags(subject, sandbox, tmp_path):
    """Pinned: the prep brief has no HITL/RESUMABLE sections at all.

    It does carry an unrelated "CHECKPOINT-RESUMABLE" paragraph about notebooks, which is
    unconditional and has nothing to do with the task's `resumable` flag.
    """
    brief = subject._make_prep_brief({**TASK, "mode": "needs-dan", "resumable": True},
                                     tmp_path / "ws", tmp_path / "wt")
    assert "HITL NOTE" not in brief
    assert RESUMABLE_HEADING not in brief
    assert "CHECKPOINT-RESUMABLE" in \
        subject._make_prep_brief(TASK, tmp_path / "ws", tmp_path / "wt")


def test_prep_brief_ignores_the_verifications_block(subject, sandbox, tmp_path):
    task = {**TASK, "verifications": [{"id": "V1", "kind": "auto", "check": "c",
                                       "cmd": "run", "expect": "ok"}]}
    brief = subject._make_prep_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "VERIFICATIONS" not in brief
    assert "V1" not in brief


def test_prep_brief_drops_dans_answers(subject, sandbox, tmp_path):
    """Surprise: `_answers_section` is only appended by `_make_brief`.

    A dispatch:manual task that also declares `questions:` is gated on Dan answering them,
    but the prep implementer is never shown the answers.
    """
    _answer(subject, "q-T1-q1", '{"choice": "the answer"}')
    task = {**TASK, "dispatch": "manual", "questions": [{"id": "q1", "prompt": "Which?"}]}
    brief = subject._make_prep_brief(task, tmp_path / "ws", tmp_path / "wt")
    assert "ANSWERS FROM DAN" not in brief
    assert "the answer" not in brief


def test_prep_brief_is_pure_and_writes_nothing(subject, sandbox, tmp_path):
    ws, wt = tmp_path / "ws", tmp_path / "wt"
    subject._make_prep_brief(TASK, ws, wt)
    assert not ws.exists() and not wt.exists()


# ===================================================================================
# launch_implementer
# ===================================================================================


def _launch(subject, sandbox, monkeypatch, task=None, session_id="impl-T1-1",
            ws_name="impl-T1-1", retry_note=""):
    task = task if task is not None else TASK
    runs = _fake_run(monkeypatch)
    ws = _workspace(sandbox, ws_name)
    wt = sandbox.repo / "worktrees" / ws_name
    subject.launch_implementer(task, session_id, ws, wt, retry_note)
    return SimpleNamespace(runs=runs, workspace=ws, worktree=wt)


def test_launch_implementer_returns_none_and_writes_three_artifacts(
    subject, sandbox, monkeypatch
):
    runs = _fake_run(monkeypatch)
    ws = _workspace(sandbox)
    assert subject.launch_implementer(TASK, "impl-T1-1", ws, sandbox.repo / "wt") is None
    assert sorted(p.name for p in ws.iterdir()) == ["brief.txt", "launch.py", "session_uuid.txt"]


def test_launch_implementer_brief_file_matches_make_brief(subject, sandbox, monkeypatch):
    out = _launch(subject, sandbox, monkeypatch, retry_note="note")
    assert (out.workspace / "brief.txt").read_text(encoding="utf-8") == \
        subject._make_brief(TASK, out.workspace, out.worktree, "note")


def test_launch_implementer_writes_a_prep_brief_for_a_manual_task(
    subject, sandbox, monkeypatch
):
    task = {**TASK, "dispatch": "manual"}
    out = _launch(subject, sandbox, monkeypatch, task=task)
    assert "PREP implementer" in (out.workspace / "brief.txt").read_text(encoding="utf-8")


def test_launch_implementer_writes_a_uuid_sidecar(subject, sandbox, monkeypatch):
    out = _launch(subject, sandbox, monkeypatch)
    text = (out.workspace / "session_uuid.txt").read_text(encoding="utf-8")
    assert uuid.UUID(text)  # a bare uuid4, no trailing newline
    assert text == text.strip()


def test_launch_implementer_uuid_is_fresh_per_launch(subject, sandbox, monkeypatch):
    first = _launch(subject, sandbox, monkeypatch, ws_name="impl-T1-1")
    second = _launch(subject, sandbox, monkeypatch, ws_name="impl-T1-2")
    assert (first.workspace / "session_uuid.txt").read_text(encoding="utf-8") != \
        (second.workspace / "session_uuid.txt").read_text(encoding="utf-8")


def test_launch_implementer_generated_launcher_is_valid_python(subject, sandbox, monkeypatch):
    out = _launch(subject, sandbox, monkeypatch)
    source = (out.workspace / "launch.py").read_text(encoding="utf-8")
    compile(source, "launch.py", "exec")


@pytest.mark.parametrize("configured_backend", sorted(_AGENT_ARGV))
def test_launch_implementer_agent_argv(subject, sandbox, monkeypatch, configured_backend):
    """The argv belongs to the driver the `implementer` role resolves to.

    One body, both drivers: the `roles:` block in the sandbox's project.yaml selects the
    backend and the argv is pinned exactly, flag for flag, for whichever one that is.

    Subject-aware on purpose. The reference module predates backend drivers — it spells
    one CLI's argv inline and never reads a `roles:` block — so under the legacy subject
    every parameter pins the old single-backend argv. That divergence is what M2
    introduces; it is recorded here rather than smoothed over into an assertion loose
    enough to accept either answer from either subject.
    """
    # Keep the version probe (a real `codex --version`) out of the launch path: the
    # binary this suite would find is irrelevant to the argv and must not be executed.
    monkeypatch.setattr(registry, "resolve_binary", lambda *args, **kwargs: None)
    _configure_backend(sandbox, configured_backend)

    out = _launch(subject, sandbox, monkeypatch)
    sess = (out.workspace / "session_uuid.txt").read_text(encoding="utf-8")
    brief = (out.workspace / "brief.txt").read_text(encoding="utf-8")

    expected = configured_backend if _delegates_to_a_driver(subject) else _LEGACY_BACKEND
    assert _agent_argv(out.workspace / "launch.py", brief) == _AGENT_ARGV[expected](
        subject, out, sess
    )


@pytest.mark.parametrize("model_key", ["haiku", "sonnet", "opus"])
def test_launch_implementer_honours_the_model_allowlist(
    subject, sandbox, monkeypatch, model_key
):
    out = _launch(subject, sandbox, monkeypatch, task={**TASK, "model": model_key})
    argv = _agent_argv(out.workspace / "launch.py")
    assert argv[argv.index("--model") + 1] == subject.IMPLEMENTER_MODELS[model_key]


def test_launch_implementer_model_allowlist_keys(subject, sandbox):
    assert set(subject.IMPLEMENTER_MODELS) == {"haiku", "sonnet", "opus"}
    assert subject._DEFAULT_IMPLEMENTER_MODEL == subject.IMPLEMENTER_MODELS["sonnet"]


@pytest.mark.parametrize("model_key", ["  OPUS  ", "Opus", "opus\n"])
def test_launch_implementer_model_key_is_lowercased_and_stripped(
    subject, sandbox, monkeypatch, model_key
):
    out = _launch(subject, sandbox, monkeypatch, task={**TASK, "model": model_key})
    argv = _agent_argv(out.workspace / "launch.py")
    assert argv[argv.index("--model") + 1] == subject.IMPLEMENTER_MODELS["opus"]


@pytest.mark.parametrize("model_key", [None, "", "gpt-4", "haiku-4-5", 0, False])
def test_launch_implementer_unknown_model_falls_back_to_the_default(
    subject, sandbox, monkeypatch, model_key
):
    out = _launch(subject, sandbox, monkeypatch, task={**TASK, "model": model_key})
    argv = _agent_argv(out.workspace / "launch.py")
    assert argv[argv.index("--model") + 1] == subject._DEFAULT_IMPLEMENTER_MODEL


def test_launch_implementer_title_keywords_never_escalate_the_model(
    subject, sandbox, monkeypatch
):
    """A task with no explicit `model:` field falls to the default regardless of its
    title — resolving a model from title/description text was never part of the B8
    allowlist design and must not silently reappear."""
    task = {**TASK, "title": "Core extraction and architecture refactor",
            "short_desc": "redesign the protocol engine, a cutover"}
    out = _launch(subject, sandbox, monkeypatch, task=task)
    argv = _agent_argv(out.workspace / "launch.py")
    assert argv[argv.index("--model") + 1] == subject._DEFAULT_IMPLEMENTER_MODEL


def test_launch_implementer_non_string_model_raises(subject, sandbox, monkeypatch):
    """Surprise: the "never crash" fall-safe only covers falsy values — `model: 3` raises."""
    _fake_run(monkeypatch)
    ws = _workspace(sandbox)
    with pytest.raises(AttributeError):
        subject.launch_implementer({**TASK, "model": 3}, "impl-T1-1", ws, sandbox.repo / "wt")


def test_launch_implementer_omits_the_system_prompt_when_the_file_is_absent(
    subject, sandbox, monkeypatch
):
    assert not subject.SYS_PROMPT.exists()
    out = _launch(subject, sandbox, monkeypatch)
    assert "--system-prompt-file" not in _agent_argv(out.workspace / "launch.py")


def test_launch_implementer_passes_the_system_prompt_when_present(
    subject, sandbox, monkeypatch
):
    subject.SYS_PROMPT.parent.mkdir(parents=True, exist_ok=True)
    subject.SYS_PROMPT.write_text("be careful\n", encoding="utf-8")
    out = _launch(subject, sandbox, monkeypatch)
    argv = _agent_argv(out.workspace / "launch.py")
    assert argv[6:8] == ["--system-prompt-file", str(subject.SYS_PROMPT)]
    assert argv[-1] == "<brief>"


def test_launch_implementer_launcher_reads_the_brief_and_captures_output(
    subject, sandbox, monkeypatch
):
    out = _launch(subject, sandbox, monkeypatch)
    source = (out.workspace / "launch.py").read_text(encoding="utf-8")
    assert str(out.workspace / "brief.txt") in source
    assert str(out.workspace / "impl.log") in source
    assert "stdout=log" in source
    assert "stderr=subprocess.STDOUT" in source


def test_launch_implementer_does_not_create_impl_log_itself(subject, sandbox, monkeypatch):
    """Pinned: impl.log only appears once the generated launcher actually runs."""
    out = _launch(subject, sandbox, monkeypatch)
    assert not (out.workspace / "impl.log").exists()


def test_launch_implementer_makes_the_launcher_executable(subject, sandbox, monkeypatch):
    out = _launch(subject, sandbox, monkeypatch)
    assert (out.workspace / "launch.py").stat().st_mode & 0o777 == 0o755


def test_launch_implementer_tmux_command_and_kwargs(subject, sandbox, monkeypatch):
    out = _launch(subject, sandbox, monkeypatch)
    call = out.runs.first
    assert call.args == (
        f"tmux new-window -t {subject.TMUX_SESSION} -n impl-T1 "
        f"'cd {out.worktree} && {subject.VENV_PYTHON} {out.workspace / 'launch.py'}'",
    )
    assert call.kwargs == {"shell": True, "check": True}
    assert len(out.runs.calls) == 1


def test_launch_implementer_window_is_named_after_the_task_not_the_session(
    subject, sandbox, monkeypatch
):
    """Surprise: `session_id` is accepted and never used.

    The tmux window is `impl-<task_id>`, so a retry launched while a stale window of the
    same name still exists produces two windows tmux cannot tell apart, and
    `_kill_tmux_window` can only target one of them.
    """
    out = _launch(subject, sandbox, monkeypatch, session_id="impl-T1-20260101-000000")
    assert " -n impl-T1 " in out.runs.first.args[0]
    assert "20260101" not in out.runs.first.args[0]


def test_launch_implementer_journals_the_model_choice(subject, sandbox, monkeypatch):
    _launch(subject, sandbox, monkeypatch, task={**TASK, "model": "haiku"})
    records = _journal(sandbox)
    assert [r["event"] for r in records] == ["implementer_model"]
    assert records[0]["detail"] == f"T1 model={subject.IMPLEMENTER_MODELS['haiku']}"
    assert records[0]["session_id"] == ""


def test_launch_implementer_journal_omits_the_session_id(subject, sandbox, monkeypatch):
    """Surprise: the one journal record it writes cannot be correlated with the session."""
    _launch(subject, sandbox, monkeypatch, session_id="impl-T1-20260101-000000")
    assert _journal(sandbox)[0]["session_id"] == ""


def test_launch_implementer_prints_the_model_and_the_window(subject, sandbox, monkeypatch,
                                                            capsys):
    _launch(subject, sandbox, monkeypatch)
    printed = capsys.readouterr().out
    assert subject._DEFAULT_IMPLEMENTER_MODEL in printed
    assert f"{subject.TMUX_SESSION}:impl-T1" in printed


def test_launch_implementer_requires_an_existing_workspace(subject, sandbox, monkeypatch):
    """Surprise: no mkdir — the caller owns workspace creation or the launch raises."""
    _fake_run(monkeypatch)
    with pytest.raises(FileNotFoundError):
        subject.launch_implementer(TASK, "impl-T1-1", sandbox.workspaces / "absent",
                                   sandbox.repo / "wt")


def test_launch_implementer_requires_an_id(subject, sandbox, monkeypatch):
    _fake_run(monkeypatch)
    ws = _workspace(sandbox)
    with pytest.raises(KeyError):
        subject.launch_implementer({}, "impl-T1-1", ws, sandbox.repo / "wt")


def test_launch_implementer_propagates_a_tmux_failure_after_writing_everything(
    subject, sandbox, monkeypatch
):
    """Surprise: `check=True` and no cleanup — a failed launch leaves a complete workspace
    and an `implementer_model` journal record behind, indistinguishable from a live one."""

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(subprocess, "run", boom)
    ws = _workspace(sandbox)
    with pytest.raises(subprocess.CalledProcessError):
        subject.launch_implementer(TASK, "impl-T1-1", ws, sandbox.repo / "wt")
    assert (ws / "brief.txt").is_file()
    assert (ws / "launch.py").is_file()
    assert (ws / "session_uuid.txt").is_file()
    assert _events(sandbox) == ["implementer_model"]


def test_launch_implementer_never_touches_state_json(subject, sandbox, monkeypatch):
    """Pinned: the launch clock is persisted by the caller, not here."""
    _launch(subject, sandbox, monkeypatch)
    assert not (sandbox.orch_dir / "state.json").exists()


def test_launch_implementer_does_not_gate_on_questions(subject, sandbox, monkeypatch):
    """Pinned: `_questions_ready` is the caller's gate — launching bypasses it entirely."""
    task = {**TASK, "questions": [{"id": "q1", "prompt": "Which?"}]}
    out = _launch(subject, sandbox, monkeypatch, task=task)
    assert "(no answer recorded)" in (out.workspace / "brief.txt").read_text(encoding="utf-8")
    assert not (subject.QUESTIONS_DIR / "q-T1-q1.json").exists()


# ===================================================================================
# _synthesize_sentinel
# ===================================================================================


def _result(workspace: Path, payload) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "result.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def test_synthesize_sentinel_writes_done_for_status_done(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    _result(ws, {"status": "DONE"})
    assert subject._synthesize_sentinel(ws) == "DONE"
    assert (ws / "DONE").is_file()
    assert not (ws / "FAILED").exists()
    assert "DONE" in (ws / "DONE").read_text()


@pytest.mark.parametrize("status", ["done", " Done ", "dOnE"])
def test_synthesize_sentinel_normalizes_case_and_whitespace(subject, sandbox, tmp_path,
                                                            status):
    ws = tmp_path / "ws"
    _result(ws, {"status": status})
    assert subject._synthesize_sentinel(ws) == "DONE"


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED", "NEEDS-REPLAN", "in-progress"])
def test_synthesize_sentinel_writes_failed_for_any_other_status(subject, sandbox, tmp_path,
                                                                status):
    ws = tmp_path / "ws"
    _result(ws, {"status": status})
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert (ws / "FAILED").is_file()
    assert not (ws / "DONE").exists()
    assert status.upper() in (ws / "FAILED").read_text()


def test_synthesize_sentinel_missing_result_json(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert "no terminal status" in (ws / "FAILED").read_text()


def test_synthesize_sentinel_malformed_result_json(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    _result(ws, "{not json")
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert "no terminal status" in (ws / "FAILED").read_text()


def test_synthesize_sentinel_result_json_without_status(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    _result(ws, {"task_id": "T1"})
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert "no terminal status" in (ws / "FAILED").read_text()


def test_synthesize_sentinel_null_status_reads_as_the_word_none(subject, sandbox, tmp_path):
    """Surprise: `str(None)` wins over the empty-string default, so `"status": null`
    reports `status=NONE` instead of the "no terminal status" wording."""
    ws = tmp_path / "ws"
    _result(ws, {"status": None})
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert "status=NONE" in (ws / "FAILED").read_text()


def test_synthesize_sentinel_non_string_status_is_stringified(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    _result(ws, {"status": 5})
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert "status=5" in (ws / "FAILED").read_text()


def test_synthesize_sentinel_non_dict_result_json_is_failed(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    _result(ws, ["DONE"])
    assert subject._synthesize_sentinel(ws) == "FAILED"
    assert "no terminal status" in (ws / "FAILED").read_text()


def test_synthesize_sentinel_overwrites_an_existing_sentinel(subject, sandbox, tmp_path):
    """Surprise: no `exists()` guard — a real FAILED reason is replaced by the synthetic one."""
    ws = tmp_path / "ws"
    _result(ws, {"status": "FAILED"})
    (ws / "FAILED").write_text("the real reason", encoding="utf-8")
    subject._synthesize_sentinel(ws)
    assert "the real reason" not in (ws / "FAILED").read_text()


def test_synthesize_sentinel_writes_nothing_else(subject, sandbox, tmp_path):
    ws = tmp_path / "ws"
    _result(ws, {"status": "DONE"})
    subject._synthesize_sentinel(ws)
    assert sorted(p.name for p in ws.iterdir()) == ["DONE", "result.json"]


def test_synthesize_sentinel_does_not_journal(subject, sandbox):
    """Surprise: a synthesized terminal state leaves no trace in the journal."""
    ws = _workspace(sandbox)
    _result(ws, {"status": "DONE"})
    subject._synthesize_sentinel(ws)
    assert _events(sandbox) == []


def test_synthesize_sentinel_raises_when_the_workspace_is_gone(subject, sandbox, tmp_path):
    """Surprise: the read is guarded, the write is not."""
    with pytest.raises(FileNotFoundError):
        subject._synthesize_sentinel(tmp_path / "absent")


# ===================================================================================
# _tail_text
# ===================================================================================


def _log(tmp_path: Path, text: str, name: str = "impl.log") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_tail_text_returns_the_last_n_lines(subject, tmp_path):
    path = _log(tmp_path, "".join(f"line{i}\n" for i in range(10)))
    assert subject._tail_text(path, 3) == "line7\nline8\nline9"


def test_tail_text_default_is_25_lines(subject, tmp_path):
    path = _log(tmp_path, "".join(f"line{i}\n" for i in range(100)))
    assert subject._tail_text(path).splitlines() == [f"line{i}" for i in range(75, 100)]


def test_tail_text_shorter_file_returns_everything(subject, tmp_path):
    path = _log(tmp_path, "a\nb\n")
    assert subject._tail_text(path, 25) == "a\nb"


def test_tail_text_drops_the_trailing_newline(subject, tmp_path):
    path = _log(tmp_path, "a\nb\n")
    assert not subject._tail_text(path, 25).endswith("\n")


def test_tail_text_empty_file_is_empty(subject, tmp_path):
    assert subject._tail_text(_log(tmp_path, ""), 5) == ""


def test_tail_text_missing_file_is_empty(subject, tmp_path):
    assert subject._tail_text(tmp_path / "absent.log", 5) == ""


def test_tail_text_directory_is_empty(subject, tmp_path):
    (tmp_path / "adir").mkdir()
    assert subject._tail_text(tmp_path / "adir", 5) == ""


def test_tail_text_normalizes_crlf_and_lone_cr(subject, tmp_path):
    """Pinned: `splitlines()` treats \\r, \\r\\n and \\n alike, so line count is not byte-based."""
    path = tmp_path / "x.log"
    path.write_bytes(b"a\r\nb\rc\nd\n")
    assert subject._tail_text(path, 2) == "c\nd"


def test_tail_text_replaces_undecodable_bytes_instead_of_failing(subject, tmp_path):
    path = tmp_path / "x.log"
    path.write_bytes(b"ok\n\xff\xfe bad\n")
    tail = subject._tail_text(path, 5)
    assert "ok" in tail
    assert "�" in tail


def test_tail_text_zero_lines_returns_the_whole_file(subject, tmp_path):
    """Surprise: `[-0:]` is `[0:]`, so asking for no lines returns every line."""
    path = _log(tmp_path, "a\nb\nc\n")
    assert subject._tail_text(path, 0) == "a\nb\nc"


def test_tail_text_negative_n_drops_leading_lines(subject, tmp_path):
    """Surprise: `[--2:]` is `[2:]` — a negative count silently becomes a head-skip."""
    path = _log(tmp_path, "a\nb\nc\nd\n")
    assert subject._tail_text(path, -2) == "c\nd"


def test_tail_text_accepts_a_string_path(subject, tmp_path):
    """Surprise: annotated `Path`, but any object with `.read_text` works — and anything
    else (including a plain str in some call paths) is swallowed into ""."""
    path = _log(tmp_path, "a\nb\n")
    assert subject._tail_text(str(path), 5) == ""


# ===================================================================================
# _latest_impl_tail
# ===================================================================================


def _impl_workspace(sandbox, name: str, log: str | None, mtime: float) -> Path:
    ws = sandbox.workspaces / name
    ws.mkdir(parents=True, exist_ok=True)
    if log is not None:
        (ws / "impl.log").write_text(log, encoding="utf-8")
    os.utime(ws, (mtime, mtime))
    return ws


def test_latest_impl_tail_no_workspaces_is_empty(subject, sandbox):
    assert subject._latest_impl_tail("T1") == ""


def test_latest_impl_tail_reads_the_newest_workspace(subject, sandbox):
    _impl_workspace(sandbox, "impl-T1-old", "old log\n", 1_000_000)
    _impl_workspace(sandbox, "impl-T1-new", "new log\n", 2_000_000)
    assert subject._latest_impl_tail("T1") == "new log"


def test_latest_impl_tail_falls_back_when_the_newest_log_is_missing(subject, sandbox):
    _impl_workspace(sandbox, "impl-T1-old", "old log\n", 1_000_000)
    _impl_workspace(sandbox, "impl-T1-new", None, 2_000_000)
    assert subject._latest_impl_tail("T1") == "old log"


def test_latest_impl_tail_falls_back_when_the_newest_log_is_empty(subject, sandbox):
    _impl_workspace(sandbox, "impl-T1-old", "old log\n", 1_000_000)
    _impl_workspace(sandbox, "impl-T1-new", "", 2_000_000)
    assert subject._latest_impl_tail("T1") == "old log"


def test_latest_impl_tail_empty_when_no_workspace_has_a_log(subject, sandbox):
    _impl_workspace(sandbox, "impl-T1-a", None, 1_000_000)
    _impl_workspace(sandbox, "impl-T1-b", "", 2_000_000)
    assert subject._latest_impl_tail("T1") == ""


def test_latest_impl_tail_forwards_the_line_count(subject, sandbox):
    _impl_workspace(sandbox, "impl-T1-a", "".join(f"l{i}\n" for i in range(50)), 1_000_000)
    assert subject._latest_impl_tail("T1", 2) == "l48\nl49"
    assert len(subject._latest_impl_tail("T1").splitlines()) == 25


def test_latest_impl_tail_ignores_other_tasks(subject, sandbox):
    _impl_workspace(sandbox, "impl-T2-a", "other task\n", 2_000_000)
    _impl_workspace(sandbox, "impl-T1-a", "mine\n", 1_000_000)
    assert subject._latest_impl_tail("T1") == "mine"


def test_latest_impl_tail_ignores_files(subject, sandbox):
    (sandbox.workspaces / "impl-T1-a").write_text("not a dir", encoding="utf-8")
    assert subject._latest_impl_tail("T1") == ""


def test_latest_impl_tail_requires_the_trailing_separator(subject, sandbox):
    """Surprise: the glob is `impl-<id>-*`, so a workspace named exactly `impl-<id>` is
    invisible to failure diagnosis."""
    _impl_workspace(sandbox, "impl-T1", "would be useful\n", 2_000_000)
    assert subject._latest_impl_tail("T1") == ""


def test_latest_impl_tail_leaks_across_hyphenated_sibling_task_ids(subject, sandbox):
    """Surprise: `impl-T1-*` also matches task `T1-b`'s workspaces.

    Diagnosing T1 can therefore quote a completely different task's implementer log.
    """
    _impl_workspace(sandbox, "impl-T1-b-2026", "sibling log\n", 2_000_000)
    assert subject._latest_impl_tail("T1") == "sibling log"


def test_latest_impl_tail_missing_workspaces_dir_is_empty(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "WORKSPACES", sandbox.repo / "absent-workspaces")
    assert subject._latest_impl_tail("T1") == ""


def test_latest_impl_tail_survives_a_stat_failure(subject, sandbox, monkeypatch):
    """The bare `except` around glob+sort means a vanished workspace yields "" not a crash."""
    _impl_workspace(sandbox, "impl-T1-a", "log\n", 1_000_000)
    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name.startswith("impl-T1-"):
            raise OSError("vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    assert subject._latest_impl_tail("T1") == ""


def test_latest_impl_tail_writes_nothing(subject, sandbox):
    _impl_workspace(sandbox, "impl-T1-a", "log\n", 1_000_000)
    before = sorted(p.name for p in (sandbox.workspaces / "impl-T1-a").iterdir())
    subject._latest_impl_tail("T1")
    assert sorted(p.name for p in (sandbox.workspaces / "impl-T1-a").iterdir()) == before
    assert _events(sandbox) == []
