"""Pinned behaviour of the self-heal layer: diagnosis, the self-fix gates, and `/redo`.

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and the found-bugs inbox records it. Nothing here
asserts what the code *should* do.

Three target modules live in one file because they are one behavioural story — a failure
is diagnosed, the diagnosis is gated, and the gated branch is landed — so every test
carries its own `maestro_module` marker (`DIAGNOSE` / `SELFFIX` / `REDO`) rather than a
module-wide `pytestmark`.

Nothing here shells out for real and nothing touches the network:

* `_judge_complete` is the only function that reaches an agent CLI, and every test of it
  swaps the subject's `subprocess` reference for a namespace that records argv;
* every other agent call goes through `_judge_complete`, which is stubbed;
* the two gate-then-merge helpers (`_merge_self_fix_branch` / `_merge_redo_branch`) are
  pinned against a real throwaway git repo in `test_merge.py`; here they are stubbed, so
  these tests pin the *loop* around them — which files are unlinked, renamed, journalled
  and notified — without a second copy of the git plumbing.

Where the behaviour depends on the consuming project's vocabulary (the allow/deny path
tuples, the whitelist of failure classes) the test reads the tuple off the subject and
loops over it instead of copying the vocabulary into this file.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

DIAGNOSE = pytest.mark.maestro_module("selfheal.diagnose")
SELFFIX = pytest.mark.maestro_module("selfheal.selffix")
REDO = pytest.mark.maestro_module("selfheal.redo")

TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


# --- shared plumbing ---------------------------------------------------------------


def _completed(returncode=0, stdout="", stderr="", args=()):
    return subprocess.CompletedProcess(args=list(args), returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _fake_subprocess(monkeypatch, subject, handler=None):
    """Swap the subject's `subprocess` module reference. Scoped to the subject module."""
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if handler is None:
            return _completed()
        return handler(*args, **kwargs)

    fake = SimpleNamespace(
        run=run,
        calls=calls,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
    )
    monkeypatch.setattr(subject, "subprocess", fake)
    return fake


class _Judge:
    """Records `_judge_complete(system=…, user=…)` calls and replays a canned answer."""

    def __init__(self, answer=""):
        self.answer = answer
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def _use_judge(monkeypatch, subject, answer="") -> _Judge:
    judge = _Judge(answer)
    monkeypatch.setattr(subject, "_judge_complete", judge)
    return judge


def _journal(sandbox) -> list[dict]:
    path = sandbox.orch_dir / "journal.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _events(sandbox) -> list[str]:
    return [r["event"] for r in _journal(sandbox)]


def _detail(sandbox, event: str) -> str:
    return [r["detail"] for r in _journal(sandbox) if r["event"] == event][0]


# =====================================================================================
# _judge_complete — the one function here that reaches an agent CLI
# =====================================================================================


@DIAGNOSE
def test_judge_builds_a_single_positional_prompt_for_the_agent_cli(subject, sandbox, monkeypatch):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="answer"))
    subject._judge_complete(system="SYS", user="USR")
    argv, kwargs = fake.calls[0][0][0], fake.calls[0][1]
    assert argv == ["claude", "-p", "--model", subject.JUDGE_MODEL, "SYS\n\nUSR"]
    assert kwargs["cwd"] == str(subject.REPO)
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 240


@DIAGNOSE
def test_judge_defaults_to_the_module_judge_model(subject, sandbox, monkeypatch):
    """The default model is a module global, resolved at definition time, not per call."""
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="x"))
    monkeypatch.setattr(subject, "JUDGE_MODEL", "some-other-model")
    subject._judge_complete(system="S", user="U")
    assert fake.calls[0][0][0][3] != "some-other-model"


@DIAGNOSE
def test_judge_forwards_an_explicit_model_and_timeout(subject, sandbox, monkeypatch):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="x"))
    subject._judge_complete(system="S", user="U", model="m9", timeout=7)
    assert fake.calls[0][0][0][3] == "m9"
    assert fake.calls[0][1]["timeout"] == 7


@DIAGNOSE
def test_judge_returns_the_stripped_stdout(subject, sandbox, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="  hello \n\n"))
    assert subject._judge_complete(system="S", user="U") == "hello"


@DIAGNOSE
def test_judge_returns_empty_on_a_nonzero_exit_even_with_output(subject, sandbox, monkeypatch, capsys):
    _fake_subprocess(
        monkeypatch, subject,
        lambda *a, **k: _completed(returncode=1, stdout="useful", stderr="E" * 300),
    )
    assert subject._judge_complete(system="S", user="U") == ""
    out = capsys.readouterr().out
    assert "rc=1" in out
    assert "E" * 160 in out
    assert "E" * 161 not in out


@DIAGNOSE
def test_judge_returns_empty_on_blank_stdout_at_exit_zero(subject, sandbox, monkeypatch, capsys):
    """FOUND_BUGS: a successful-but-silent agent is reported on stdout as `rc=0`."""
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="   \n"))
    assert subject._judge_complete(system="S", user="U") == ""
    assert "rc=0" in capsys.readouterr().out


@DIAGNOSE
def test_judge_tolerates_stdout_being_none(subject, sandbox, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=None, stderr=None))
    assert subject._judge_complete(system="S", user="U") == ""


@DIAGNOSE
def test_judge_swallows_every_exception_including_a_missing_binary(
    subject, sandbox, monkeypatch, capsys
):
    def boom(*args, **kwargs):
        raise FileNotFoundError("claude")

    _fake_subprocess(monkeypatch, subject, boom)
    assert subject._judge_complete(system="S", user="U") == ""
    assert "call failed" in capsys.readouterr().out


@DIAGNOSE
def test_judge_reports_a_timeout_as_an_empty_answer(subject, sandbox, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=240)

    _fake_subprocess(monkeypatch, subject, boom)
    assert subject._judge_complete(system="S", user="U") == ""
    assert "call failed" in capsys.readouterr().out


# =====================================================================================
# _diagnose_failure / _failure_looks_normal — same shape, opposite framing
# =====================================================================================


@pytest.fixture(params=["_diagnose_failure", "_failure_looks_normal"],
                ids=["diagnose", "skeptic"])
def judged(request, subject):
    """The two one-shot judgment passes; both parse their answer identically."""
    return SimpleNamespace(name=request.param, call=getattr(subject, request.param))


@DIAGNOSE
def test_judgment_pass_returns_empty_when_the_agent_says_nothing(subject, monkeypatch, judged):
    judge = _use_judge(monkeypatch, subject, answer="")
    assert judged.call("T1", "reason", "tail", "ctx") == {}
    assert len(judge.calls) == 1


@DIAGNOSE
def test_judgment_pass_parses_a_bare_json_object(subject, monkeypatch, judged):
    _use_judge(monkeypatch, subject, answer='{"verdict": "normal", "failure_class": "unknown"}')
    assert judged.call("T1", "r", "t", "c") == {"verdict": "normal", "failure_class": "unknown"}


@DIAGNOSE
def test_judgment_pass_digs_the_object_out_of_surrounding_prose(subject, monkeypatch, judged):
    _use_judge(monkeypatch, subject, answer='Sure!\n{"a": {"b": [1, 2]}}\nHope that helps.')
    assert judged.call("T1", "r", "t", "c") == {"a": {"b": [1, 2]}}


@DIAGNOSE
def test_judgment_pass_loses_both_objects_when_the_agent_prints_two(subject, monkeypatch, judged):
    """FOUND_BUGS: `\\{.*\\}` with DOTALL spans first-brace-to-last-brace, not one object."""
    _use_judge(monkeypatch, subject, answer='{"a": 1}\nand also {"b": 2}')
    assert judged.call("T1", "r", "t", "c") == {}


@DIAGNOSE
def test_judgment_pass_returns_empty_for_unbraced_and_malformed_answers(
    subject, monkeypatch, judged
):
    _use_judge(monkeypatch, subject, answer="I cannot do that")
    assert judged.call("T1", "r", "t", "c") == {}
    _use_judge(monkeypatch, subject, answer="{nope}")
    assert judged.call("T1", "r", "t", "c") == {}


@DIAGNOSE
def test_judgment_pass_returns_whatever_keys_the_agent_invented(subject, monkeypatch, judged):
    """No schema validation: the caller's key expectations are never enforced here."""
    _use_judge(monkeypatch, subject, answer='{"totally": "unrelated"}')
    assert judged.call("T1", "r", "t", "c") == {"totally": "unrelated"}


@DIAGNOSE
def test_judgment_pass_sends_the_same_user_prompt_from_both_passes(subject, monkeypatch):
    """The framing lives entirely in the system half; the evidence half is identical."""
    diag_judge = _use_judge(monkeypatch, subject)
    subject._diagnose_failure("T7", "boom", "tail", "ctx")
    skeptic_judge = _use_judge(monkeypatch, subject)
    subject._failure_looks_normal("T7", "boom", "tail", "ctx")
    assert diag_judge.calls[0]["user"] == skeptic_judge.calls[0]["user"]
    assert diag_judge.calls[0]["system"] != skeptic_judge.calls[0]["system"]


@DIAGNOSE
def test_judgment_pass_lays_the_evidence_out_under_labelled_headings(subject, monkeypatch, judged):
    judge = _use_judge(monkeypatch, subject)
    judged.call("T7", "the reason", "the tail", "the ctx")
    user = judge.calls[0]["user"]
    assert user.startswith("Task: T7\nFailure reason: the reason")
    assert "Implementer log tail (impl.log):\nthe tail" in user
    assert "Journal entries:\nthe ctx" in user


@DIAGNOSE
def test_judgment_pass_truncates_reason_tail_and_context(subject, monkeypatch, judged):
    judge = _use_judge(monkeypatch, subject)
    judged.call("T1", "r" * 700, "i" * 3000, "j" * 2500)
    user = judge.calls[0]["user"]
    assert "r" * 600 in user and "r" * 601 not in user
    assert "i" * 2500 in user and "i" * 2501 not in user
    assert "j" * 2000 in user and "j" * 2001 not in user


@DIAGNOSE
def test_diagnosis_asks_for_a_failure_class_and_target_files(subject, monkeypatch):
    judge = _use_judge(monkeypatch, subject)
    subject._diagnose_failure("T1", "r", "t", "c")
    system = judge.calls[0]["system"]
    assert "failure_class" in system
    assert "target_files" in system
    assert "verdict" not in system


@DIAGNOSE
def test_skeptic_asks_for_a_verdict_and_a_confidence(subject, monkeypatch):
    judge = _use_judge(monkeypatch, subject)
    subject._failure_looks_normal("T1", "r", "t", "c")
    system = judge.calls[0]["system"]
    assert "verdict" in system
    assert "confidence" in system
    assert "target_files" not in system


@DIAGNOSE
def test_skeptic_never_sees_the_diagnosis_it_counterweights(subject, monkeypatch):
    """The independence guarantee is structural: the signature has no slot for a diagnosis."""
    judge = _use_judge(monkeypatch, subject)
    subject._failure_looks_normal("T1", "r", "t", "c")
    assert "root_cause" not in judge.calls[0]["user"]
    assert "suggested_fix" not in judge.calls[0]["user"]


@DIAGNOSE
def test_skeptic_confidence_threshold_and_kill_switch_are_module_globals(subject):
    assert subject.SELF_FIX_SKEPTIC_MIN_CONF == 0.6
    assert isinstance(subject.ORCH_SELF_FIX_SKEPTIC, bool)


# =====================================================================================
# _persist_diagnosis
# =====================================================================================


def _persisted(subject, task_id: str) -> dict:
    return json.loads((subject.DIAGNOSES_DIR / f"{task_id}.json").read_text(encoding="utf-8"))


@DIAGNOSE
def test_persist_creates_the_diagnoses_directory_on_demand(subject, sandbox):
    assert not subject.DIAGNOSES_DIR.exists()
    subject._persist_diagnosis("T1", "boom", {})
    assert subject.DIAGNOSES_DIR.is_dir()


@DIAGNOSE
def test_persist_writes_the_five_diagnosis_fields_plus_a_timestamp(subject, sandbox):
    diag = {
        "failure_class": "observability",
        "root_cause": "no logs",
        "suggested_fix": "log it",
        "target_files": ["scripts/x.py"],
    }
    subject._persist_diagnosis("T1", "boom", diag)
    got = _persisted(subject, "T1")
    assert got["task_id"] == "T1"
    assert got["reason"] == "boom"
    assert got["failure_class"] == "observability"
    assert got["root_cause"] == "no logs"
    assert got["suggested_fix"] == "log it"
    assert got["target_files"] == ["scripts/x.py"]
    assert TS_RE.fullmatch(got["ts"])


@DIAGNOSE
def test_persist_drops_every_other_key_of_the_diagnosis(subject, sandbox):
    subject._persist_diagnosis("T1", "boom", {"confidence": 0.9, "rationale": "x"})
    assert set(_persisted(subject, "T1")) == {
        "task_id", "reason", "failure_class", "root_cause",
        "suggested_fix", "target_files", "ts",
    }


@DIAGNOSE
def test_persist_defaults_missing_keys_to_empty_strings_and_an_empty_list(subject, sandbox):
    subject._persist_diagnosis("T1", "boom", {})
    got = _persisted(subject, "T1")
    assert got["failure_class"] == ""
    assert got["root_cause"] == ""
    assert got["suggested_fix"] == ""
    assert got["target_files"] == []


@DIAGNOSE
def test_persist_keeps_an_explicit_none_rather_than_the_default(subject, sandbox):
    """FOUND_BUGS: `.get(k, "")` only defaults a *missing* key — a null survives as null."""
    subject._persist_diagnosis("T1", "boom", {"failure_class": None, "target_files": None})
    got = _persisted(subject, "T1")
    assert got["failure_class"] is None
    assert got["target_files"] is None


@DIAGNOSE
def test_persist_truncates_the_reason_to_800_characters(subject, sandbox):
    subject._persist_diagnosis("T1", "r" * 900, {})
    assert _persisted(subject, "T1")["reason"] == "r" * 800


@DIAGNOSE
def test_persist_overwrites_an_earlier_diagnosis_for_the_same_task(subject, sandbox):
    subject._persist_diagnosis("T1", "first", {"root_cause": "a"})
    subject._persist_diagnosis("T1", "second", {"root_cause": "b"})
    got = _persisted(subject, "T1")
    assert (got["reason"], got["root_cause"]) == ("second", "b")


@DIAGNOSE
def test_persist_swallows_an_unserialisable_diagnosis_and_writes_nothing(
    subject, sandbox, capsys
):
    subject._persist_diagnosis("T1", "boom", {"target_files": {1, 2}})
    assert not (subject.DIAGNOSES_DIR / "T1.json").exists()
    assert "[diagnose] persist failed for T1" in capsys.readouterr().out


@DIAGNOSE
def test_persist_swallows_a_non_string_reason(subject, sandbox, capsys):
    """FOUND_BUGS: `reason[:800]` raises on an int and the diagnosis is lost silently."""
    subject._persist_diagnosis("T1", 7, {})
    assert not (subject.DIAGNOSES_DIR / "T1.json").exists()
    assert "[diagnose] persist failed for T1" in capsys.readouterr().out


@DIAGNOSE
def test_persist_swallows_a_task_id_that_is_not_a_usable_filename(subject, sandbox, capsys):
    """FOUND_BUGS: the task id is interpolated straight into a path, unvalidated."""
    subject._persist_diagnosis("A/B", "boom", {})
    assert "[diagnose] persist failed for A/B" in capsys.readouterr().out
    assert list(subject.DIAGNOSES_DIR.iterdir()) == []


# =====================================================================================
# _self_fix_path_ok — the self-fix allowlist
# =====================================================================================


@SELFFIX
def test_self_fix_paths_are_the_orchestrators_own_surface(subject):
    assert subject.SELF_FIX_PATHS == ("scripts/", "orchestrator/", "docs/")
    assert subject.SELF_FIX_WHITELIST == {
        "observability", "orchestrator-logic", "transient-infra",
    }
    assert subject.SELF_FIX_MIN_HOURS == 6.0


@SELFFIX
def test_self_fix_path_gate_refuses_an_empty_target_list(subject):
    assert subject._self_fix_path_ok([]) == (False, "no target_files identified")
    assert subject._self_fix_path_ok(None) == (False, "no target_files identified")


@SELFFIX
def test_self_fix_path_gate_drops_blank_entries_before_deciding(subject):
    assert subject._self_fix_path_ok(["", "   ", "\n"]) == (False, "no target_files identified")
    assert subject._self_fix_path_ok(["", "scripts/x.py"]) == (True, "ok")


@SELFFIX
def test_self_fix_path_gate_accepts_every_allowed_prefix(subject):
    for prefix in subject.SELF_FIX_PATHS:
        assert subject._self_fix_path_ok([prefix + "thing.py"]) == (True, "ok")


@SELFFIX
def test_self_fix_path_gate_requires_the_prefix_to_include_its_slash(subject):
    """`scripts` (the directory itself) is out of scope; only `scripts/…` is allowed."""
    ok, why = subject._self_fix_path_ok(["scripts"])
    assert (ok, why) == (False, "out-of-scope path: scripts")


@SELFFIX
def test_self_fix_path_gate_refuses_an_unlisted_top_level_path(subject):
    assert subject._self_fix_path_ok(["README.md"]) == (False, "out-of-scope path: README.md")


@SELFFIX
def test_self_fix_path_gate_refuses_every_denied_fragment(subject):
    for denied in subject.SELF_FIX_DENY:
        name = "scripts/" + denied
        ok, why = subject._self_fix_path_ok([name])
        assert ok is False, name
        assert why.startswith("denied path: "), name


@SELFFIX
def test_self_fix_path_gate_matches_a_denied_fragment_anywhere_in_the_path(subject):
    """FOUND_BUGS: the deny list is a substring test, so it fires on unrelated names."""
    denied_fragment = subject.SELF_FIX_DENY[0]
    ok, why = subject._self_fix_path_ok(["docs/notes-about-" + denied_fragment + ".md"])
    assert ok is False
    assert why.startswith("denied path: ")


@SELFFIX
def test_self_fix_path_gate_checks_deny_before_allow(subject):
    """A path inside an allowed prefix is still vetoed by a deny fragment."""
    ok, why = subject._self_fix_path_ok(["scripts/" + subject.SELF_FIX_DENY[0]])
    assert ok is False
    assert why.startswith("denied path: ")


@SELFFIX
def test_self_fix_path_gate_vetoes_the_whole_set_on_one_bad_file(subject):
    files = ["scripts/a.py", "docs/b.md", "elsewhere/c.py", "orchestrator/d.py"]
    assert subject._self_fix_path_ok(files) == (False, "out-of-scope path: elsewhere/c.py")


@SELFFIX
def test_self_fix_path_gate_reports_the_first_offender_in_input_order(subject):
    files = ["one/a.py", "two/b.py"]
    assert subject._self_fix_path_ok(files)[1] == "out-of-scope path: one/a.py"


@SELFFIX
def test_self_fix_path_gate_strips_surrounding_whitespace(subject):
    assert subject._self_fix_path_ok(["  scripts/x.py  "]) == (True, "ok")


@SELFFIX
def test_self_fix_path_gate_strips_a_leading_dot_slash(subject):
    assert subject._self_fix_path_ok(["./scripts/x.py"]) == (True, "ok")


@SELFFIX
def test_self_fix_path_gate_lets_a_parent_traversal_in(subject):
    """FOUND_BUGS: `lstrip("./")` strips a *character set*, so `../scripts/x` normalises
    to `scripts/x` and passes the allowlist — the gate cannot see it points outside the
    repo at all. The same applies to an absolute path: `/scripts/x` is accepted too."""
    assert subject._self_fix_path_ok(["../scripts/x.py"]) == (True, "ok")
    assert subject._self_fix_path_ok(["../../../scripts/x.py"]) == (True, "ok")
    assert subject._self_fix_path_ok(["/scripts/x.py"]) == (True, "ok")


@SELFFIX
def test_self_fix_path_gate_reports_the_normalised_name_not_the_input(subject):
    assert subject._self_fix_path_ok(["  ./elsewhere/c.py "])[1] == (
        "out-of-scope path: elsewhere/c.py"
    )


@SELFFIX
def test_self_fix_path_gate_stringifies_a_non_string_entry(subject):
    assert subject._self_fix_path_ok([Path("scripts/x.py")]) == (True, "ok")
    assert subject._self_fix_path_ok([7]) == (False, "out-of-scope path: 7")


@SELFFIX
def test_self_fix_path_gate_iterates_a_bare_string_character_by_character(subject):
    """FOUND_BUGS: `target_files` arriving as a string is not detected — each character
    becomes a "file", so an allowed path is rejected on its own first letter."""
    assert subject._self_fix_path_ok("scripts/x.py") == (False, "out-of-scope path: s")


# =====================================================================================
# _self_fix_rate_ok — the per-class loop breaker
# =====================================================================================


def _applied_line(cls: str, ago_seconds: float, event: str = "self_fix_applied") -> str:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return json.dumps({"ts": ts, "event": event, "agent": "o",
                       "session_id": "", "detail": f"T1 class={cls} merged"})


def _seed_journal(subject, *lines: str) -> None:
    subject.JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    subject.JOURNAL.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")


@SELFFIX
def test_rate_gate_allows_when_there_is_no_journal_at_all(subject, sandbox):
    assert not subject.JOURNAL.exists()
    assert subject._self_fix_rate_ok("observability") is True


@SELFFIX
def test_rate_gate_allows_when_the_journal_holds_no_self_fix(subject, sandbox):
    _seed_journal(subject, _applied_line("observability", 60, event="task_failed"))
    assert subject._self_fix_rate_ok("observability") is True


@SELFFIX
def test_rate_gate_blocks_a_second_fix_of_the_same_class_inside_the_window(subject, sandbox):
    _seed_journal(subject, _applied_line("observability", 3600))
    assert subject._self_fix_rate_ok("observability") is False


@SELFFIX
def test_rate_gate_allows_again_once_the_window_has_passed(subject, sandbox):
    _seed_journal(subject, _applied_line("observability", 6 * 3600 + 60))
    assert subject._self_fix_rate_ok("observability") is True


@SELFFIX
def test_rate_gate_window_boundary_is_greater_than_or_equal(subject, sandbox, monkeypatch):
    """Pinned with a zero-hour window so the cutoff is 'now': a future-stamped entry is
    still inside it, a past-stamped one is not."""
    monkeypatch.setattr(subject, "SELF_FIX_MIN_HOURS", 0.0)
    _seed_journal(subject, _applied_line("observability", -5))
    assert subject._self_fix_rate_ok("observability") is False
    _seed_journal(subject, _applied_line("observability", 5))
    assert subject._self_fix_rate_ok("observability") is True


@SELFFIX
def test_rate_gate_is_per_class(subject, sandbox):
    _seed_journal(subject, _applied_line("observability", 60))
    assert subject._self_fix_rate_ok("transient-infra") is True


@SELFFIX
def test_rate_gate_matches_the_class_as_a_substring_of_the_raw_line(subject, sandbox):
    """FOUND_BUGS: the line is matched as text, never parsed — a class name that is a
    substring of another class (or of the detail, or of the task id) rate-limits it."""
    _seed_journal(subject, _applied_line("transient-infra", 60))
    assert subject._self_fix_rate_ok("infra") is False


@SELFFIX
def test_rate_gate_matches_the_event_name_as_a_substring_too(subject, sandbox):
    """A journal record that merely *mentions* self_fix_applied counts as one."""
    _seed_journal(subject, json.dumps({
        "ts": (datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": "note", "detail": "considered self_fix_applied for class=observability",
    }))
    assert subject._self_fix_rate_ok("observability") is False


@SELFFIX
def test_rate_gate_treats_an_empty_class_as_matching_every_self_fix(subject, sandbox):
    """FOUND_BUGS: `"" in line` is always true, so a class-less diagnosis is rate-limited
    by any self-fix at all."""
    _seed_journal(subject, _applied_line("observability", 60))
    assert subject._self_fix_rate_ok("") is False


@SELFFIX
def test_rate_gate_ignores_a_line_it_cannot_parse(subject, sandbox):
    _seed_journal(subject, "self_fix_applied observability but not json")
    assert subject._self_fix_rate_ok("observability") is True


@SELFFIX
def test_rate_gate_ignores_a_record_with_no_usable_timestamp(subject, sandbox):
    _seed_journal(
        subject,
        json.dumps({"event": "self_fix_applied", "detail": "class=observability"}),
        json.dumps({"ts": "yesterday", "event": "self_fix_applied",
                    "detail": "class=observability"}),
    )
    assert subject._self_fix_rate_ok("observability") is True


@SELFFIX
def test_rate_gate_reads_only_the_first_nineteen_characters_of_the_timestamp(subject, sandbox):
    """Both the `…Z` form this module writes and an offset-bearing ISO form parse, because
    the offset is chopped off — an entry stamped in a non-UTC zone is read as UTC."""
    stamp = datetime.now(timezone.utc) - timedelta(minutes=1)
    _seed_journal(subject, json.dumps({
        "ts": stamp.replace(tzinfo=None).isoformat() + "+05:00",
        "event": "self_fix_applied", "detail": "class=observability",
    }))
    assert subject._self_fix_rate_ok("observability") is False


@SELFFIX
def test_rate_gate_stops_at_the_first_recent_match(subject, sandbox):
    _seed_journal(
        subject,
        _applied_line("observability", 9 * 3600),
        _applied_line("observability", 60),
        "definitely not json",
    )
    assert subject._self_fix_rate_ok("observability") is False


@SELFFIX
def test_rate_gate_allows_when_the_journal_cannot_be_opened(subject, sandbox):
    """FOUND_BUGS: an unreadable journal fails OPEN — the loop breaker silently vanishes."""
    subject.JOURNAL.mkdir(parents=True)
    assert subject.JOURNAL.exists()
    assert subject._self_fix_rate_ok("observability") is True


# =====================================================================================
# _self_fix_eligible
# =====================================================================================


GOOD_DIAG = {"failure_class": "observability", "target_files": ["scripts/x.py"]}


@SELFFIX
def test_eligibility_accepts_a_whitelisted_in_scope_unrated_diagnosis(
    subject, sandbox, monkeypatch
):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    assert subject._self_fix_eligible(dict(GOOD_DIAG)) == (True, "eligible")


@SELFFIX
def test_eligibility_is_refused_first_by_the_kill_switch(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", False)
    assert subject._self_fix_eligible(dict(GOOD_DIAG)) == (False, "ORCH_SELF_FIX disabled")
    assert subject._self_fix_eligible({}) == (False, "ORCH_SELF_FIX disabled")


@SELFFIX
def test_eligibility_refuses_a_class_outside_the_whitelist(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    diag = dict(GOOD_DIAG, failure_class="implementer-quality")
    assert subject._self_fix_eligible(diag) == (
        False, "class 'implementer-quality' not in self-fix whitelist"
    )


@SELFFIX
def test_eligibility_refuses_a_missing_or_null_class_as_the_empty_string(
    subject, sandbox, monkeypatch
):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    expected = (False, "class '' not in self-fix whitelist")
    assert subject._self_fix_eligible({"target_files": ["scripts/x.py"]}) == expected
    assert subject._self_fix_eligible(dict(GOOD_DIAG, failure_class=None)) == expected
    assert subject._self_fix_eligible(dict(GOOD_DIAG, failure_class="")) == expected


@SELFFIX
def test_eligibility_tolerates_a_padded_class(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    diag = dict(GOOD_DIAG, failure_class="  observability\n")
    assert subject._self_fix_eligible(diag) == (True, "eligible")


@SELFFIX
def test_eligibility_crashes_on_a_non_string_class(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the `or ""` guard only covers a falsy class — `7.strip()` raises."""
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    with pytest.raises(AttributeError):
        subject._self_fix_eligible(dict(GOOD_DIAG, failure_class=7))


@SELFFIX
def test_eligibility_passes_the_path_gates_own_reason_through(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    diag = dict(GOOD_DIAG, target_files=["elsewhere/c.py"])
    assert subject._self_fix_eligible(diag) == (False, "out-of-scope path: elsewhere/c.py")
    assert subject._self_fix_eligible(dict(GOOD_DIAG, target_files=[]))[1] == (
        "no target_files identified"
    )


@SELFFIX
def test_eligibility_checks_the_path_gate_before_the_rate_limiter(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    _seed_journal(subject, _applied_line("observability", 60))
    diag = dict(GOOD_DIAG, target_files=["elsewhere/c.py"])
    assert subject._self_fix_eligible(diag)[1].startswith("out-of-scope path:")


@SELFFIX
def test_eligibility_refuses_a_class_fixed_within_the_rate_window(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    _seed_journal(subject, _applied_line("observability", 60))
    ok, why = subject._self_fix_eligible(dict(GOOD_DIAG))
    assert ok is False
    assert why == (
        f"rate-limited: a 'observability' self-fix ran within {subject.SELF_FIX_MIN_HOURS}h"
    )
    assert "6.0h" in why


@SELFFIX
def test_eligibility_rate_limits_on_the_stripped_class(subject, sandbox, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    _seed_journal(subject, _applied_line("observability", 60))
    diag = dict(GOOD_DIAG, failure_class="  observability  ")
    assert subject._self_fix_eligible(diag)[0] is False


# =====================================================================================
# attempt_self_fix
# =====================================================================================


def _request(subject) -> dict:
    files = sorted(subject.SELFFIX_DIR.glob("*.request.json"))
    return json.loads(files[-1].read_text(encoding="utf-8"))


@SELFFIX
def test_attempt_self_fix_writes_a_request_file_named_after_task_and_time(
    subject, sandbox, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    subject.attempt_self_fix("T1", "boom", dict(GOOD_DIAG))
    files = sorted(subject.SELFFIX_DIR.glob("*.request.json"))
    assert len(files) == 1
    fix_id = files[0].name[: -len(".request.json")]
    assert re.fullmatch(r"T1-\d{8}-\d{6}", fix_id)
    assert _request(subject)["fix_id"] == fix_id


@SELFFIX
def test_attempt_self_fix_creates_the_selffix_directory(subject, sandbox, monkeypatch):
    _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    assert not subject.SELFFIX_DIR.exists()
    subject.attempt_self_fix("T1", "boom", {})
    assert subject.SELFFIX_DIR.is_dir()


@SELFFIX
def test_attempt_self_fix_copies_the_diagnosis_into_the_request(subject, sandbox, monkeypatch):
    _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    diag = {"failure_class": "observability", "root_cause": "rc",
            "suggested_fix": "sf", "target_files": ["scripts/x.py"]}
    subject.attempt_self_fix("T1", "r" * 900, diag)
    req = _request(subject)
    assert req["task"] == "T1"
    assert req["reason"] == "r" * 800
    assert req["failure_class"] == "observability"
    assert req["root_cause"] == "rc"
    assert req["suggested_fix"] == "sf"
    assert req["target_files"] == ["scripts/x.py"]


@SELFFIX
def test_attempt_self_fix_defaults_every_missing_diagnosis_field(subject, sandbox, monkeypatch):
    _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    subject.attempt_self_fix("T1", "boom", {})
    req = _request(subject)
    assert req["failure_class"] == ""
    assert req["root_cause"] == ""
    assert req["suggested_fix"] == ""
    assert req["target_files"] == []
    assert req["impl_tail"] == ""


@SELFFIX
def test_attempt_self_fix_attaches_the_last_forty_lines_of_the_implementer_log(
    subject, sandbox, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    ws = subject.WORKSPACES / "impl-T1-1"
    ws.mkdir(parents=True)
    (ws / "impl.log").write_text("".join(f"line{i}\n" for i in range(60)), encoding="utf-8")
    subject.attempt_self_fix("T1", "boom", {})
    tail = _request(subject)["impl_tail"].splitlines()
    assert len(tail) == 40
    assert tail[0] == "line20"
    assert tail[-1] == "line59"


@SELFFIX
def test_attempt_self_fix_truncates_the_log_tail_to_three_thousand_characters(
    subject, sandbox, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    ws = subject.WORKSPACES / "impl-T1-1"
    ws.mkdir(parents=True)
    (ws / "impl.log").write_text("x" * 100 + "\n", encoding="utf-8")
    (ws / "impl.log").write_text("".join("x" * 100 + "\n" for _ in range(40)), encoding="utf-8")
    subject.attempt_self_fix("T1", "boom", {})
    assert len(_request(subject)["impl_tail"]) == 3000


@SELFFIX
def test_attempt_self_fix_spawns_a_detached_tmux_window_through_a_shell(
    subject, sandbox, monkeypatch
):
    fake = _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    subject.attempt_self_fix("T1", "boom", dict(GOOD_DIAG))
    cmd, kwargs = fake.calls[0][0][0], fake.calls[0][1]
    req = sorted(subject.SELFFIX_DIR.glob("*.request.json"))[0]
    assert kwargs == {"shell": True, "check": True}
    assert cmd.startswith(f"tmux new-window -t {subject.TMUX_SESSION} -n selffix-T1 ")
    assert f"cd {subject.REPO} && {subject.VENV_PYTHON} " in cmd
    assert str(subject.REPO / "scripts" / "maestro_selffix.py") in cmd
    assert cmd.endswith(f"{req}'")


@SELFFIX
def test_attempt_self_fix_journals_and_notifies_after_a_successful_spawn(
    subject, sandbox, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    notes = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: notes.append(msg))
    subject.attempt_self_fix("T1", "boom", dict(GOOD_DIAG))
    assert _events(sandbox) == ["self_fix_started"]
    assert _detail(sandbox, "self_fix_started") == "T1 class=observability"
    assert len(notes) == 1
    assert "T1" in notes[0] and "observability" in notes[0]


@SELFFIX
def test_attempt_self_fix_journals_the_class_as_none_when_absent(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the request file defaults the class to "", but the journal line and the
    Telegram message use a default-less `.get`, so they read `class=None`."""
    _fake_subprocess(monkeypatch, subject)
    notes = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: notes.append(msg))
    subject.attempt_self_fix("T1", "boom", {})
    assert _detail(sandbox, "self_fix_started") == "T1 class=None"
    assert "None" in notes[0]


@SELFFIX
def test_attempt_self_fix_leaves_the_request_behind_when_the_spawn_fails(
    subject, sandbox, monkeypatch, capsys
):
    """FOUND_BUGS: a failed spawn is only printed — the orphan request file stays on disk
    and nothing is journalled, so the failure is invisible to the operator."""
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "tmux")

    _fake_subprocess(monkeypatch, subject, boom)
    notes = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: notes.append(msg))
    subject.attempt_self_fix("T1", "boom", dict(GOOD_DIAG))
    assert len(list(subject.SELFFIX_DIR.glob("*.request.json"))) == 1
    assert _journal(sandbox) == []
    assert notes == []
    assert "[self-fix] spawn failed" in capsys.readouterr().out


@SELFFIX
def test_attempt_self_fix_never_consults_the_eligibility_gate(subject, sandbox, monkeypatch):
    """The gate is the caller's job: a denied, blacklisted diagnosis still spawns a runner."""
    fake = _fake_subprocess(monkeypatch, subject)
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: None)
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", False)
    subject.attempt_self_fix("T1", "boom", {"failure_class": "task-spec",
                                            "target_files": ["main_bot.py"]})
    assert len(fake.calls) == 1
    assert _events(sandbox) == ["self_fix_started"]


# =====================================================================================
# apply_ready_self_fixes
# =====================================================================================


def _ready(directory: Path, name: str, **info) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.ready.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


@pytest.fixture
def selffix_env(subject, sandbox, monkeypatch):
    """The self-fix apply loop's collaborators, all recorded rather than executed."""
    box = SimpleNamespace(merges=[], verdict=(True, "merged selffix-T1-x at abc1234"),
                          notes=[], dep_maps=[], argv=[])

    monkeypatch.setattr(
        subject, "_merge_self_fix_branch",
        lambda branch: box.merges.append(branch) or box.verdict,
    )
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: box.notes.append(msg))
    monkeypatch.setattr(subject, "run_dep_map", lambda: box.dep_maps.append(True))
    fake = _fake_subprocess(monkeypatch, subject)
    box.argv = fake.calls
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_MERGE", True)
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", True)
    return box


def _git_argv(box) -> list[list[str]]:
    return [list(call[0][0]) for call in box.argv]


@SELFFIX
def test_apply_self_fixes_is_a_no_op_without_a_selffix_directory(subject, sandbox, selffix_env):
    assert not subject.SELFFIX_DIR.exists()
    subject.apply_ready_self_fixes()
    assert selffix_env.merges == []
    assert _journal(sandbox) == []


@SELFFIX
def test_apply_self_fixes_ignores_a_directory_with_no_ready_files(
    subject, sandbox, selffix_env
):
    subject.SELFFIX_DIR.mkdir(parents=True)
    (subject.SELFFIX_DIR / "T1.request.json").write_text("{}", encoding="utf-8")
    subject.apply_ready_self_fixes()
    assert selffix_env.merges == []
    assert (subject.SELFFIX_DIR / "T1.request.json").exists()


@SELFFIX
def test_apply_self_fixes_discards_an_unreadable_ready_file(subject, sandbox, selffix_env):
    subject.SELFFIX_DIR.mkdir(parents=True)
    path = subject.SELFFIX_DIR / "T1.ready.json"
    path.write_text("not json", encoding="utf-8")
    subject.apply_ready_self_fixes()
    assert not path.exists()
    assert selffix_env.merges == []
    assert _journal(sandbox) == []


@SELFFIX
def test_apply_self_fixes_discards_a_ready_file_with_no_branch(subject, sandbox, selffix_env):
    a = _ready(subject.SELFFIX_DIR, "T1", task="T1")
    b = _ready(subject.SELFFIX_DIR, "T2", task="T2", branch="")
    subject.apply_ready_self_fixes()
    assert not a.exists() and not b.exists()
    assert selffix_env.merges == []


@SELFFIX
def test_apply_self_fixes_merges_notifies_and_regenerates_the_dep_map(
    subject, sandbox, selffix_env
):
    path = _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="selffix-T1-x",
                  failure_class="observability", summary="a summary")
    subject.apply_ready_self_fixes()
    assert selffix_env.merges == ["selffix-T1-x"]
    assert _events(sandbox) == ["self_fix_applied"]
    assert _detail(sandbox, "self_fix_applied") == (
        "T1 class=observability merged selffix-T1-x at abc1234"
    )
    assert len(selffix_env.notes) == 1
    assert "a summary" in selffix_env.notes[0]
    assert selffix_env.dep_maps == [True]
    assert not path.exists()


@SELFFIX
def test_apply_self_fixes_removes_the_runner_worktree_then_deletes_the_branch(
    subject, sandbox, selffix_env
):
    _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="selffix-T1-x")
    subject.apply_ready_self_fixes()
    worktree, branch = _git_argv(selffix_env)
    assert worktree[:4] == ["git", "worktree", "remove", "--force"]
    assert worktree[4].startswith("/tmp/")
    assert worktree[4].endswith("selffix-T1-x")
    assert branch == ["git", "branch", "-D", "selffix-T1-x"]


@SELFFIX
def test_apply_self_fixes_derives_the_worktree_by_chopping_eight_characters(
    subject, sandbox, selffix_env
):
    """FOUND_BUGS: the fix id is recovered as `branch[len("selffix-"):]` without checking
    the prefix, so a branch that is not named `selffix-…` yields a nonsense worktree path
    (here `hand-made-branch` loses its first eight characters) and the stale worktree is
    never removed."""
    _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="hand-made-branch")
    subject.apply_ready_self_fixes()
    worktree = _git_argv(selffix_env)[0]
    assert worktree[4].endswith("selffix-" + "hand-made-branch"[8:])
    assert not worktree[4].endswith("hand-made-branch")


@SELFFIX
def test_apply_self_fixes_journals_a_failed_merge_and_cleans_nothing_up(
    subject, sandbox, selffix_env
):
    selffix_env.verdict = (False, "dirty worktree blocks merge: ['x']")
    path = _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="selffix-T1-x")
    subject.apply_ready_self_fixes()
    assert _events(sandbox) == ["self_fix_merge_failed"]
    assert _detail(sandbox, "self_fix_merge_failed") == (
        "selffix-T1-x dirty worktree blocks merge: ['x']"
    )
    assert len(selffix_env.notes) == 1
    assert selffix_env.dep_maps == []
    assert _git_argv(selffix_env) == []
    assert not path.exists()


@SELFFIX
def test_apply_self_fixes_only_asks_when_auto_merge_is_off(subject, sandbox, selffix_env, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_MERGE", False)
    path = _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="selffix-T1-x",
                  summary="s" * 500)
    subject.apply_ready_self_fixes()
    assert selffix_env.merges == []
    assert _journal(sandbox) == []
    assert len(selffix_env.notes) == 1
    assert "s" * 400 in selffix_env.notes[0]
    assert "s" * 401 not in selffix_env.notes[0]
    assert not path.exists()
    assert (subject.SELFFIX_DIR / "T1.ready.notified").exists()


@SELFFIX
def test_apply_self_fixes_renames_only_the_last_suffix_when_parking_a_fix(
    subject, sandbox, selffix_env, monkeypatch
):
    """`Path.with_suffix` replaces `.json` only, so `T1.ready.json` parks as
    `T1.ready.notified` — still matched by no glob the loop uses, so it is never retried."""
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_MERGE", False)
    _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="selffix-T1-x")
    subject.apply_ready_self_fixes()
    assert sorted(p.name for p in subject.SELFFIX_DIR.iterdir()) == ["T1.ready.notified"]
    subject.apply_ready_self_fixes()
    assert selffix_env.notes and len(selffix_env.notes) == 1


@SELFFIX
def test_apply_self_fixes_ignores_the_master_kill_switch(subject, sandbox, selffix_env, monkeypatch):
    """FOUND_BUGS: only ORCH_SELF_FIX_MERGE is consulted here, so a gate-passed branch is
    merged even after the ORCH_SELF_FIX kill switch has been thrown."""
    monkeypatch.setattr(subject, "ORCH_SELF_FIX", False)
    _ready(subject.SELFFIX_DIR, "T1", task="T1", branch="selffix-T1-x")
    subject.apply_ready_self_fixes()
    assert selffix_env.merges == ["selffix-T1-x"]
    assert _events(sandbox) == ["self_fix_applied"]


@SELFFIX
def test_apply_self_fixes_processes_every_ready_file_in_sorted_order(
    subject, sandbox, selffix_env
):
    _ready(subject.SELFFIX_DIR, "b", task="T2", branch="selffix-b")
    _ready(subject.SELFFIX_DIR, "a", task="T1", branch="selffix-a")
    _ready(subject.SELFFIX_DIR, "c", task="T3", branch="selffix-c")
    subject.apply_ready_self_fixes()
    assert selffix_env.merges == ["selffix-a", "selffix-b", "selffix-c"]
    assert list(subject.SELFFIX_DIR.iterdir()) == []


@SELFFIX
def test_apply_self_fixes_journals_a_missing_task_as_none(subject, sandbox, selffix_env):
    _ready(subject.SELFFIX_DIR, "T1", branch="selffix-T1-x")
    subject.apply_ready_self_fixes()
    assert _detail(sandbox, "self_fix_applied").startswith("None class= ")


# =====================================================================================
# _redo_path_ok — the deliverable allowlist
# =====================================================================================


@REDO
def test_redo_allow_and_deny_lists_are_module_constants(subject):
    assert subject._REDO_ALLOW == ("colab/", "data_export/", "scripts/", "docs/", "tasks/")
    assert subject._REDO_GATE_IGNORE == ("redo_result.json", "redo_impl.log")


@REDO
def test_redo_path_gate_refuses_an_empty_change_set(subject):
    assert subject._redo_path_ok([]) == (False, "no files changed")
    assert subject._redo_path_ok(["", "  "]) == (False, "no files changed")


@REDO
def test_redo_path_gate_crashes_on_none(subject):
    """FOUND_BUGS: the self-fix twin guards with `files or []`; this one does not."""
    with pytest.raises(TypeError):
        subject._redo_path_ok(None)


@REDO
def test_redo_path_gate_accepts_every_allowed_prefix(subject):
    for prefix in subject._REDO_ALLOW:
        assert subject._redo_path_ok([prefix + "thing.txt"]) == (True, "ok")


@REDO
def test_redo_path_gate_refuses_an_unlisted_path(subject):
    assert subject._redo_path_ok(["notebooks/x.ipynb"]) == (
        False, "out-of-scope path touched: notebooks/x.ipynb"
    )


@REDO
def test_redo_path_gate_refuses_every_denied_fragment(subject):
    for denied in subject._REDO_DENY:
        name = "scripts/" + denied
        ok, why = subject._redo_path_ok([name])
        assert ok is False, name
        assert why.startswith("denied path touched: "), name


@REDO
def test_redo_path_gate_checks_deny_before_allow(subject):
    ok, why = subject._redo_path_ok(["scripts/" + subject._REDO_DENY[0]])
    assert ok is False
    assert why.startswith("denied path touched: ")


@REDO
def test_redo_path_gate_ignores_the_runners_own_sidecars_by_basename(subject):
    """The ignore list is matched on the *name*, so a sidecar anywhere in the tree is
    dropped from the gate — including one outside every allowed prefix."""
    for ignored in subject._REDO_GATE_IGNORE:
        assert subject._redo_path_ok([f"anywhere/deep/{ignored}"]) == (False, "no files changed")
    assert subject._redo_path_ok(
        ["docs/a.md", subject._REDO_GATE_IGNORE[0]]
    ) == (True, "ok")


@REDO
def test_redo_path_gate_ignores_sidecars_after_normalising_the_path(subject):
    assert subject._redo_path_ok(["./" + subject._REDO_GATE_IGNORE[0]]) == (
        False, "no files changed"
    )


@REDO
def test_redo_path_gate_strips_whitespace_and_leading_dot_slash(subject):
    assert subject._redo_path_ok(["  ./docs/a.md "]) == (True, "ok")


@REDO
def test_redo_path_gate_lets_a_parent_traversal_in(subject):
    """FOUND_BUGS: same `lstrip("./")` character-set bug as the self-fix gate."""
    assert subject._redo_path_ok(["../docs/a.md"]) == (True, "ok")
    assert subject._redo_path_ok(["/docs/a.md"]) == (True, "ok")


@REDO
def test_redo_path_gate_vetoes_the_whole_set_on_one_bad_file(subject):
    assert subject._redo_path_ok(["docs/a.md", "elsewhere/b.md"]) == (
        False, "out-of-scope path touched: elsewhere/b.md"
    )


@REDO
def test_redo_path_gate_stringifies_a_non_string_entry(subject):
    assert subject._redo_path_ok([Path("docs/a.md")]) == (True, "ok")
    assert subject._redo_path_ok([7]) == (False, "out-of-scope path touched: 7")


@REDO
def test_redo_path_gate_iterates_a_bare_string_character_by_character(subject):
    """FOUND_BUGS: same string-instead-of-list hazard as the self-fix gate."""
    assert subject._redo_path_ok("docs/a.md") == (False, "out-of-scope path touched: d")


# =====================================================================================
# apply_ready_redo
# =====================================================================================


@pytest.fixture
def redo_env(subject, sandbox, monkeypatch):
    box = SimpleNamespace(merges=[], verdict=(True, "merged redo-T1 at abc1234"),
                          notes=[], dep_maps=[], argv=[])
    monkeypatch.setattr(
        subject, "_merge_redo_branch",
        lambda branch: box.merges.append(branch) or box.verdict,
    )
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: box.notes.append(msg))
    monkeypatch.setattr(subject, "run_dep_map", lambda: box.dep_maps.append(True))
    fake = _fake_subprocess(monkeypatch, subject)
    box.argv = fake.calls
    return box


@REDO
def test_apply_redo_is_a_no_op_without_a_redo_directory(subject, sandbox, redo_env):
    assert not subject.REDO_DIR.exists()
    subject.apply_ready_redo()
    assert redo_env.merges == []


@REDO
def test_apply_redo_discards_an_unreadable_ready_file(subject, sandbox, redo_env):
    subject.REDO_DIR.mkdir(parents=True)
    path = subject.REDO_DIR / "T1.ready.json"
    path.write_text("{", encoding="utf-8")
    subject.apply_ready_redo()
    assert not path.exists()
    assert redo_env.merges == []


@REDO
def test_apply_redo_discards_a_ready_file_with_no_branch(subject, sandbox, redo_env):
    a = _ready(subject.REDO_DIR, "T1", task="T1")
    b = _ready(subject.REDO_DIR, "T2", task="T2", branch="")
    subject.apply_ready_redo()
    assert not a.exists() and not b.exists()
    assert redo_env.merges == []


@REDO
def test_apply_redo_merges_journals_and_regenerates_the_dep_map(subject, sandbox, redo_env):
    path = _ready(subject.REDO_DIR, "T1", task="T1", branch="redo-T1",
                  worktree="/tmp/redo-T1")
    subject.apply_ready_redo()
    assert redo_env.merges == ["redo-T1"]
    assert _events(sandbox) == ["redo_applied"]
    assert _detail(sandbox, "redo_applied") == "T1 merged redo-T1 at abc1234"
    assert redo_env.dep_maps == [True]
    assert not path.exists()


@REDO
def test_apply_redo_never_notifies_on_success(subject, sandbox, redo_env):
    """FOUND_BUGS: the self-fix twin sends an "applied" message; `/redo` sends nothing, so
    a landed rewrite is invisible unless the runner already spoke."""
    _ready(subject.REDO_DIR, "T1", task="T1", branch="redo-T1")
    subject.apply_ready_redo()
    assert redo_env.notes == []


@REDO
def test_apply_redo_removes_the_worktree_named_in_the_ready_file(subject, sandbox, redo_env):
    _ready(subject.REDO_DIR, "T1", task="T1", branch="redo-T1", worktree="/tmp/somewhere")
    subject.apply_ready_redo()
    assert [list(c[0][0]) for c in redo_env.argv] == [
        ["git", "worktree", "remove", "--force", "/tmp/somewhere"],
        ["git", "branch", "-D", "redo-T1"],
    ]


@REDO
def test_apply_redo_skips_the_worktree_removal_when_none_is_recorded(
    subject, sandbox, redo_env
):
    _ready(subject.REDO_DIR, "T1", task="T1", branch="redo-T1")
    subject.apply_ready_redo()
    assert [list(c[0][0]) for c in redo_env.argv] == [["git", "branch", "-D", "redo-T1"]]


@REDO
def test_apply_redo_journals_and_notifies_a_failed_merge(subject, sandbox, redo_env):
    redo_env.verdict = (False, "merge-time path gate failed: denied path touched: .env")
    path = _ready(subject.REDO_DIR, "T1", task="T1", branch="redo-T1", worktree="/tmp/w")
    subject.apply_ready_redo()
    assert _events(sandbox) == ["redo_merge_failed"]
    assert _detail(sandbox, "redo_merge_failed").startswith("redo-T1 merge-time path gate")
    assert len(redo_env.notes) == 1
    assert "redo-T1" in redo_env.notes[0]
    assert redo_env.dep_maps == []
    assert redo_env.argv == []
    assert not path.exists()


@REDO
def test_apply_redo_processes_every_ready_file_in_sorted_order(subject, sandbox, redo_env):
    _ready(subject.REDO_DIR, "b", task="T2", branch="redo-b")
    _ready(subject.REDO_DIR, "a", task="T1", branch="redo-a")
    subject.apply_ready_redo()
    assert redo_env.merges == ["redo-a", "redo-b"]
    assert list(subject.REDO_DIR.iterdir()) == []


@REDO
def test_apply_redo_has_no_kill_switch_and_no_ask_first_mode(subject, sandbox, redo_env):
    """Unlike the self-fix loop there is no ORCH_* flag here: a gate-passed `/redo` branch
    always auto-merges."""
    _ready(subject.REDO_DIR, "T1", task="T1", branch="redo-T1")
    subject.apply_ready_redo()
    assert redo_env.merges == ["redo-T1"]
