"""Pinned behaviour of `scripts/check_verifications.py` — the deterministic verification gate.

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise. Nothing here asserts what the code *should* do.

Unlike the other characterised modules, the reference is not a library of importable
functions — it is a standalone CLI script (`sys.argv`-driven `main()`, printing to stdout,
returning a process exit code) that `maestro/gates.py` used to invoke as a subprocess. So the
"subject" here is not "a module whose functions we call" but "a process we run": the reference
side copies the real script's source verbatim into a fixture repo (its own `docs/ROADMAP.md`,
sitting next to a copied `scripts/check_verifications.py` so the script's own
`REPO = Path(__file__).resolve().parent.parent` resolves inside the fixture, never the live
read-only reference repo) and shells out to it for real with `subprocess.run`. The maestro side
is `maestro.verifications`, extracted in-process (no subprocess, no script on disk) — its own
fixture drives `run_cli` directly and wraps the result back into a `subprocess.CompletedProcess`
so every test in this file exercises both subjects through the identical `subject.run(*argv)`
call.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from tests.reference_repo import LEGACY_REPO

pytestmark = pytest.mark.maestro_module("verifications")

REFERENCE_SCRIPT_REL = Path("scripts") / "check_verifications.py"


def _reference_source() -> str:
    if LEGACY_REPO is None:
        pytest.skip(
            "reference repo unknown: set $MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    path = LEGACY_REPO / REFERENCE_SCRIPT_REL
    if not path.is_file():
        pytest.skip(f"reference check_verifications.py not found at {path}")
    return path.read_text(encoding="utf-8")


class ReferenceCLI:
    """The real check_verifications.py, copied verbatim, run for real against a fixture repo."""

    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "docs").mkdir(parents=True)
        self.script = self.repo / "scripts" / "check_verifications.py"
        self.script.write_text(_reference_source(), encoding="utf-8")
        self.roadmap = self.repo / "docs" / "ROADMAP.md"
        self.roadmap.write_text("", encoding="utf-8")

    def write_roadmap(self, text: str) -> None:
        self.roadmap.write_text(text, encoding="utf-8")

    def run(self, *argv: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.script), *argv],
            capture_output=True, text=True, timeout=timeout,
        )


class MaestroCLI:
    """`maestro.verifications.run_cli`, wrapped in the same `run(*argv)` shape as
    `ReferenceCLI` so every test below drives both subjects identically.

    No script on disk and no subprocess: `run_cli` is called in-process against a
    fixture repo whose `docs/ROADMAP.md` the module's own path globals are
    monkeypatched onto (mirroring how `ReferenceCLI` gets its own fixture `REPO` via
    the copied script's `__file__`-relative computation)."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.module = importlib.import_module("maestro.verifications")
        self.repo = tmp_path / "repo"
        (self.repo / "docs").mkdir(parents=True)
        self.roadmap = self.repo / "docs" / "ROADMAP.md"
        self.roadmap.write_text("", encoding="utf-8")
        monkeypatch.setattr(self.module, "REPO", self.repo)
        monkeypatch.setattr(self.module, "ROADMAP_FILE", self.roadmap)
        monkeypatch.setattr(
            self.module, "VENV_PYTHON", str(self.repo / ".venv" / "bin" / "python3")
        )

    def write_roadmap(self, text: str) -> None:
        self.roadmap.write_text(text, encoding="utf-8")

    def run(self, *argv: str, timeout: float = 30) -> subprocess.CompletedProcess:
        try:
            rc, output = self.module.run_cli(list(argv))
        except Exception as exc:  # an uncaught crash is a nonzero exit, same as a real process
            return subprocess.CompletedProcess(args=list(argv), returncode=1,
                                               stdout="", stderr=str(exc))
        return subprocess.CompletedProcess(args=list(argv), returncode=rc,
                                           stdout=output, stderr="")


@pytest.fixture(params=["reference", "maestro"])
def subject(request, tmp_path, monkeypatch):
    if request.param == "reference":
        return ReferenceCLI(tmp_path)
    try:
        importlib.import_module("maestro.verifications")
    except ModuleNotFoundError:
        pytest.skip("maestro.verifications not extracted yet")
    return MaestroCLI(tmp_path, monkeypatch)


# ── small roadmap-text builders ──────────────────────────────────────────────────────

def _block(task_id: str, verifications_yaml: str) -> str:
    return (
        f"```yaml\n"
        f"id: {task_id}\n"
        f"verifications:\n"
        f"{verifications_yaml}"
        f"```\n"
    )


def _auto_verif(vid: str, cmd: str, expect: str) -> str:
    return f"  - id: {vid}\n    kind: auto\n    cmd: {cmd!r}\n    expect: {expect!r}\n"


def _manual_verif(vid: str) -> str:
    return f"  - id: {vid}\n    kind: manual\n"


# ── argv handling ─────────────────────────────────────────────────────────────────────


def test_no_args_prints_usage_and_exits_1(subject):
    result = subject.run()
    assert result.returncode == 1
    assert "Usage: check_verifications.py" in result.stdout


def test_task_id_only_prints_usage_and_exits_1(subject):
    result = subject.run("T1")
    assert result.returncode == 1
    assert "Usage: check_verifications.py" in result.stdout


def test_auto_only_flag_alone_still_counts_as_missing_positionals(subject):
    """FOUND_BUGS candidate: --auto-only is stripped before the argv-length check, so it
    never satisfies the required <task_id> <workspace> pair on its own."""
    result = subject.run("--auto-only")
    assert result.returncode == 1
    assert "Usage: check_verifications.py" in result.stdout


def test_task_id_and_workspace_is_a_valid_invocation(subject, tmp_path):
    subject.write_roadmap(_block("T1", ""))
    ws = tmp_path / "ws"
    result = subject.run("T1", str(ws))
    assert result.returncode == 0
    assert "No verifications defined for T1" in result.stdout


def test_auto_only_flag_is_accepted_at_the_end(subject, tmp_path):
    subject.write_roadmap(_block("T1", ""))
    ws = tmp_path / "ws"
    result = subject.run("T1", str(ws), "--auto-only")
    assert result.returncode == 0


def test_auto_only_flag_is_accepted_at_the_start(subject, tmp_path):
    """FOUND_BUGS candidate: --auto-only is filtered out of sys.argv wherever it sits, so
    the remaining positionals are re-packed regardless of where the flag was typed."""
    subject.write_roadmap(_block("T1", ""))
    ws = tmp_path / "ws"
    result = subject.run("--auto-only", "T1", str(ws))
    assert result.returncode == 0
    assert "No verifications defined for T1" in result.stdout


def test_worktree_is_optional_third_positional(subject, tmp_path):
    subject.write_roadmap(_block("T1", _auto_verif("V1", "echo ok", "ok")))
    ws = tmp_path / "ws"
    wt = tmp_path / "wt"
    wt.mkdir()
    result = subject.run("T1", str(ws), str(wt))
    assert result.returncode == 0


def test_auto_only_slides_into_the_worktree_slot_when_no_worktree_is_given(subject, tmp_path):
    """FOUND_BUGS candidate: without a real worktree argument, --auto-only is simply
    filtered out of argv before the positional read — it never lands in argv[2]."""
    subject.write_roadmap(_block("T1", _auto_verif("V1", "echo present", "present")))
    ws = tmp_path / "ws"
    result = subject.run("T1", str(ws), "--auto-only")
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


# ── locating a task's verification stanzas in ROADMAP.md ───────────────────────────────


def test_no_matching_task_id_passes_with_no_gate_message(subject, tmp_path):
    subject.write_roadmap(_block("OTHER", _auto_verif("V1", "false", "never")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert result.stdout.strip() == "[check] No verifications defined for T1 — pass (no gate)."


def test_empty_verifications_list_is_treated_like_no_task(subject, tmp_path):
    subject.write_roadmap(_block("T1", ""))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "No verifications defined for T1" in result.stdout


def test_finds_the_matching_block_among_several(subject, tmp_path):
    roadmap = (
        _block("OTHER1", _auto_verif("X", "echo no", "no"))
        + "\n"
        + _block("T1", _auto_verif("V1", "echo yes", "yes"))
        + "\n"
        + _block("OTHER2", _auto_verif("Y", "echo no", "no"))
    )
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


def test_first_matching_block_wins_when_an_id_repeats(subject, tmp_path):
    roadmap = (
        _block("T1", _auto_verif("V1", "echo first", "first"))
        + "\n"
        + _block("T1", _auto_verif("V1", "echo second", "second"))
    )
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


def test_a_malformed_yaml_block_is_skipped_without_crashing_the_scan(subject, tmp_path):
    roadmap = (
        "```yaml\n"
        "id: [unterminated\n"
        "```\n"
        "\n"
        + _block("T1", _auto_verif("V1", "echo ok", "ok"))
    )
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


def test_a_non_dict_yaml_block_is_skipped(subject, tmp_path):
    """A fenced ```yaml block whose top level is a list (not a mapping) is ignored, not fatal."""
    roadmap = (
        "```yaml\n- a\n- b\n```\n\n"
        + _block("T1", _auto_verif("V1", "echo ok", "ok"))
    )
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


def test_task_id_is_matched_by_string_conversion(subject, tmp_path):
    """FOUND_BUGS candidate: `id: 7` (an unquoted YAML int) still matches CLI arg "7" because
    both sides are coerced through str()."""
    subject.write_roadmap(_block("7", _auto_verif("V1", "echo ok", "ok")))
    result = subject.run("7", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


def test_missing_roadmap_file_is_a_hard_error(subject, tmp_path):
    subject.roadmap.unlink()
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode != 0


# ── kind:auto vs kind:manual ─────────────────────────────────────────────────────────


def test_auto_check_with_no_cmd_fails_with_a_named_message(subject, tmp_path):
    roadmap = _block("T1", "  - id: V1\n    kind: auto\n")
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert "V1 (auto): no cmd defined in ROADMAP task" in result.stdout


def test_manual_check_defaults_kind_when_kind_key_is_absent(subject, tmp_path):
    """`kind` defaults to "manual" — an id-only stanza is treated as a manual check."""
    roadmap = _block("T1", "  - id: V1\n")
    subject.write_roadmap(roadmap)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "result.json").write_text(
        '{"verifications": {"V1": {"done": true, "proof": "checked by hand"}}}',
        encoding="utf-8",
    )
    result = subject.run("T1", str(ws))
    assert result.returncode == 0


def test_manual_check_fails_when_done_is_false(subject, tmp_path):
    subject.write_roadmap(_block("T1", _manual_verif("V1")))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "result.json").write_text(
        '{"verifications": {"V1": {"done": false, "proof": "x"}}}', encoding="utf-8"
    )
    result = subject.run("T1", str(ws))
    assert result.returncode == 1
    assert "V1 (manual): done=false — cannot write DONE sentinel." in result.stdout


def test_manual_check_fails_when_proof_is_empty(subject, tmp_path):
    subject.write_roadmap(_block("T1", _manual_verif("V1")))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "result.json").write_text(
        '{"verifications": {"V1": {"done": true, "proof": "   "}}}', encoding="utf-8"
    )
    result = subject.run("T1", str(ws))
    assert result.returncode == 1
    assert "V1 (manual): done=true but proof is empty." in result.stdout


def test_manual_check_fails_when_result_json_is_absent(subject, tmp_path):
    """No result.json → no implementer verifications → done defaults to False."""
    subject.write_roadmap(_block("T1", _manual_verif("V1")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert "V1 (manual): done=false" in result.stdout


def test_manual_check_passes_with_done_true_and_nonempty_proof(subject, tmp_path):
    subject.write_roadmap(_block("T1", _manual_verif("V1")))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "result.json").write_text(
        '{"verifications": {"V1": {"done": true, "proof": "screenshot attached"}}}',
        encoding="utf-8",
    )
    result = subject.run("T1", str(ws))
    assert result.returncode == 0
    assert "all 1 verification(s) passed" in result.stdout


def test_unknown_kind_fails_with_the_kind_named(subject, tmp_path):
    roadmap = _block("T1", "  - id: V1\n    kind: bogus\n")
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert "V1: unknown kind='bogus'" in result.stdout


def test_auto_only_flag_skips_manual_checks_entirely(subject, tmp_path):
    """A failing manual check does not fail the run under --auto-only — it is never inspected."""
    roadmap = _block(
        "T1",
        _auto_verif("V1", "echo ok", "ok") + _manual_verif("V2"),
    )
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"), "--auto-only")
    assert result.returncode == 0
    assert "V2" not in result.stdout


def test_manual_result_json_that_is_invalid_json_is_silently_ignored(subject, tmp_path):
    """FOUND_BUGS candidate: a corrupt result.json is swallowed by a bare except, leaving
    `result` empty rather than surfacing the parse error."""
    subject.write_roadmap(_block("T1", _manual_verif("V1")))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "result.json").write_text("{not json", encoding="utf-8")
    result = subject.run("T1", str(ws))
    assert result.returncode == 1
    assert "V1 (manual): done=false" in result.stdout


# ── the stdout.strip() == expect comparison ─────────────────────────────────────────


def test_auto_check_passes_on_an_exact_stdout_match(subject, tmp_path):
    subject.write_roadmap(_block("T1", _auto_verif("V1", "echo hi", "hi")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0


def test_auto_check_fails_on_a_mismatch_and_reports_both_values(subject, tmp_path):
    subject.write_roadmap(_block("T1", _auto_verif("V1", "echo nope", "hi")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert "V1 (auto): output mismatch" in result.stdout
    assert "expected: 'hi'" in result.stdout
    assert "got:      'nope'" in result.stdout


def test_last_nonblank_line_matches_when_the_full_output_does_not(subject, tmp_path):
    """A leading summary line does not false-fail a check whose last line is the sentinel."""
    subject.write_roadmap(
        _block("T1", _auto_verif("V1", "printf 'summary line\\nSENTINEL\\n'", "SENTINEL"))
    )
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0


def test_expect_with_trailing_whitespace_can_never_match(subject, tmp_path):
    """FOUND_BUGS candidate: `actual` is stripped but `expect` from the ROADMAP is compared
    verbatim, so a stray trailing newline in the declared expectation always mismatches."""
    roadmap = _block("T1", "  - id: V1\n    kind: auto\n    cmd: 'echo hi'\n    expect: \"hi\\n\"\n")
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert "V1 (auto): output mismatch" in result.stdout


def test_expect_defaults_to_empty_string_when_absent(subject, tmp_path):
    """FOUND_BUGS candidate: a stanza with no `expect` key passes iff the command is silent."""
    roadmap = _block("T1", "  - id: V1\n    kind: auto\n    cmd: 'true'\n")
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0


def test_the_commands_exit_code_is_never_consulted(subject, tmp_path):
    """FOUND_BUGS candidate: only stdout is compared — a command that exits non-zero but
    prints the expected string still counts as a pass."""
    subject.write_roadmap(_block("T1", _auto_verif("V1", "echo hi; exit 7", "hi")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0


def test_the_commands_own_stderr_is_never_surfaced(subject, tmp_path):
    """FOUND_BUGS candidate: only `r.stdout` feeds the comparison — the command's stderr is
    captured and then discarded, appearing nowhere in the gate's own output."""
    subject.write_roadmap(_block("T1", _auto_verif("V1", "echo hi; echo leaked >&2", "hi")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert "leaked" not in result.stdout
    assert "leaked" not in result.stderr


def test_auto_check_runs_in_the_repo_by_default(subject, tmp_path):
    subject.write_roadmap(_block("T1", _auto_verif("V1", "pwd", "")))
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1  # pwd prints something, expect is "" -> mismatch
    assert str(subject.repo.resolve()) in result.stdout


def test_auto_check_runs_in_the_given_worktree_when_one_is_supplied(subject, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    subject.write_roadmap(_block("T1", _auto_verif("V1", "pwd", "")))
    result = subject.run("T1", str(tmp_path / "ws"), str(wt))
    assert result.returncode == 1
    assert str(wt.resolve()) in result.stdout
    assert str(subject.repo.resolve()) not in result.stdout


# ── exit code / stdout+stderr shape on pass and fail ────────────────────────────────


def test_all_pass_prints_a_single_summary_line_and_exits_0(subject, tmp_path):
    subject.write_roadmap(
        _block("T1", _auto_verif("V1", "echo a", "a") + _auto_verif("V2", "echo b", "b"))
    )
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 0
    assert result.stdout.strip() == "[check] T1: all 2 verification(s) passed."
    assert result.stderr == ""


def test_failures_are_headed_by_a_count_and_bulleted_with_a_cross(subject, tmp_path):
    subject.write_roadmap(
        _block(
            "T1",
            _auto_verif("V1", "echo a", "a") + _auto_verif("V2", "echo wrong", "b"),
        )
    )
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[0] == "[check] T1: 1 verification(s) FAILED:"
    assert any(line.startswith("  ✗ V2 (auto):") for line in lines)
    assert not any("V1" in line for line in lines)  # only the failing check is reported
    assert result.stderr == ""


def test_multiple_failures_are_all_reported_and_counted(subject, tmp_path):
    subject.write_roadmap(
        _block(
            "T1",
            _auto_verif("V1", "echo x", "a") + _auto_verif("V2", "echo y", "b"),
        )
    )
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert result.stdout.startswith("[check] T1: 2 verification(s) FAILED:")
    assert "V1" in result.stdout and "V2" in result.stdout


def test_python_prefixed_commands_are_rewritten_to_the_venv_interpreter(subject, tmp_path):
    """FOUND_BUGS candidate: the gate always substitutes `<repo>/.venv/bin/python3` for a
    leading `python`/`python3`, even when that interpreter does not exist on disk — this
    fixture repo has no `.venv`, so a `python3 -c ...` verification hard-fails here."""
    roadmap = _block("T1", "  - id: V1\n    kind: auto\n    cmd: 'python3 -c \"print(1)\"'\n    expect: '1'\n")
    subject.write_roadmap(roadmap)
    result = subject.run("T1", str(tmp_path / "ws"))
    assert result.returncode == 1
    assert "V1 (auto)" in result.stdout
