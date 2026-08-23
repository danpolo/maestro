"""Pinned behaviour of the verification / smoke gate layer.

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and `docs/FOUND_BUGS.md` records it. Nothing here
asserts what the code *should* do.

Every subprocess that can be run for real IS run for real: the smoke adapter and (for the
legacy reference only — see below) the verification-gate script are genuine executable
shell scripts written into `tmp_path`, invoked through the subject's own
`VENV_PYTHON`/`SMOKE_ADAPTER`/`CHECK_VERIFICATIONS` globals (which `sandbox` has already
rebased out of any live repo). Only the branches a real process cannot reach in test time
— the 120 s / 45 min timeouts, an OSError from `subprocess.run` itself, and the agent-CLI
call in `sonnet_review_proofs` — are faked, by swapping the subject module's `subprocess`
reference for a namespace that records calls. No test shells out to an agent CLI and no
test touches the network.

`run_verification_gate` is the one exception to "the legacy script is a real subprocess
stand-in": M1 moved maestro's checking engine in-process (`maestro.verifications`, called
directly rather than shelled out to as `check_verifications.py`), so `maestro.gates` has
no `CHECK_VERIFICATIONS` global left to point a stand-in script at. Its tests below branch
on `_is_maestro(subject)`: the legacy half is untouched (real script, real subprocess); the
maestro half swaps `gates.verifications` for a small recording stub (`_fake_verifications`,
the in-process analogue of `_fake_subprocess`) for argv-shape/pass-through/exception cases,
plus one true end-to-end case (`test_gate_runs_the_checker_in_the_repo`) that writes a real
ROADMAP.md and drives the real `maestro.verifications` module to prove the wiring itself
works, not just the stub's contract.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.maestro_module("gates")

SH = Path("/bin/sh")


def _script(path: Path, body: str) -> Path:
    """A real, executable /bin/sh script. Invoked as `<VENV_PYTHON> <path>`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _completed(returncode=0, stdout="", stderr="", args=()):
    return subprocess.CompletedProcess(args=list(args), returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _fake_subprocess(monkeypatch, subject, handler):
    """Swap the subject's `subprocess` module reference. Scoped to the subject module."""
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
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


def _fake_verifications(monkeypatch, subject, rc=0, output="", raises=None):
    """Swap `gates.verifications` (`maestro.verifications`, imported by name into
    `maestro/gates.py`) for a stub that records the argv `run_verification_gate` builds
    and hands back a canned (rc, output) — or raises `raises`, if given. The in-process
    analogue of `_fake_subprocess` above: since M1, the gate no longer shells out for
    this check, so there is nothing left for `_fake_subprocess` to intercept."""
    calls = []

    def run_cli(argv):
        calls.append(list(argv))
        if raises is not None:
            raise raises
        return rc, output

    fake = SimpleNamespace(run_cli=run_cli, calls=calls)
    monkeypatch.setattr(subject, "verifications", fake)
    return fake


def _write_roadmap_auto_check(sandbox, task_id: str, cmd: str, expect: str) -> None:
    """A minimal real ROADMAP.md with one kind:auto verification, for the one
    end-to-end maestro test that drives the real `maestro.verifications` module rather
    than `_fake_verifications` (proving the in-process wiring itself works)."""
    roadmap = sandbox.repo / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    roadmap.write_text(
        f"```yaml\nid: {task_id}\nverifications:\n"
        f"  - id: V1\n    kind: auto\n    cmd: {cmd!r}\n    expect: {expect!r}\n"
        f"```\n",
        encoding="utf-8",
    )


# ── run_verification_gate ───────────────────────────────────────────────────────────


def test_gate_passes_when_the_checker_script_is_absent(subject, sandbox, monkeypatch, tmp_path):
    """FOUND_BUGS #17 (legacy only): a missing gate script is reported as a PASS, not as
    a broken gate. R3 close: maestro has no such branch to trigger — there is no on-disk
    script that can be absent, and the gate's verdict is always whatever the checking
    engine actually returns, even when that verdict is a failure."""
    assert not hasattr(subject, "CHECK_VERIFICATIONS")
    fake = _fake_verifications(monkeypatch, subject, rc=1, output="nope")
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert (ok, msg) == (False, "nope")
    return
    assert not subject.CHECK_VERIFICATIONS.exists()
    ok, msg = subject.run_verification_gate("T1", sandbox.repo / "ws")
    assert ok is True
    assert msg == "(check_verifications.py not found — gate skipped)"


def test_gate_returns_true_and_output_on_exit_zero(subject, sandbox, monkeypatch, tmp_path):
    _fake_verifications(monkeypatch, subject, rc=0, output="all good")
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert (ok, msg) == (True, "all good")
    return
    _script(subject.CHECK_VERIFICATIONS, 'echo "all good"\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert (ok, msg) == (True, "all good")


def test_gate_returns_false_and_output_on_nonzero_exit(subject, sandbox, monkeypatch, tmp_path):
    _fake_verifications(monkeypatch, subject, rc=1, output="V2 failed")
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert (ok, msg) == (False, "V2 failed")
    return
    _script(subject.CHECK_VERIFICATIONS, 'echo "V2 failed"; exit 1\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert (ok, msg) == (False, "V2 failed")


def test_gate_passes_task_id_and_workspace_as_the_first_two_arguments(
    subject, sandbox, monkeypatch, tmp_path
):
    fake = _fake_verifications(monkeypatch, subject)
    subject.run_verification_gate("T1", tmp_path / "ws")
    assert fake.calls == [["T1", str(tmp_path / "ws")]]
    return
    _script(subject.CHECK_VERIFICATIONS, 'echo "argv=[$*]"\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert msg == f"argv=[T1 {tmp_path / 'ws'}]"


def test_gate_appends_worktree_then_auto_only(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_verifications(monkeypatch, subject)
    subject.run_verification_gate(
        "T1", tmp_path / "ws", worktree=tmp_path / "wt", auto_only=True
    )
    assert fake.calls == [["T1", str(tmp_path / "ws"), str(tmp_path / "wt"), "--auto-only"]]
    return
    _script(subject.CHECK_VERIFICATIONS, 'echo "argv=[$*]"\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    _, msg = subject.run_verification_gate(
        "T1", tmp_path / "ws", worktree=tmp_path / "wt", auto_only=True
    )
    assert msg == f"argv=[T1 {tmp_path / 'ws'} {tmp_path / 'wt'} --auto-only]"


def test_gate_omits_the_worktree_slot_entirely_when_it_is_none(
    subject, sandbox, monkeypatch, tmp_path
):
    """FOUND_BUGS #18: --auto-only slides into the positional worktree slot when
    worktree is None. Preserved exactly at the new call site — relocating the checking
    engine in-process did not touch how `run_verification_gate` itself builds argv."""
    fake = _fake_verifications(monkeypatch, subject)
    subject.run_verification_gate("T1", tmp_path / "ws", auto_only=True)
    assert fake.calls == [["T1", str(tmp_path / "ws"), "--auto-only"]]
    return
    _script(subject.CHECK_VERIFICATIONS, 'echo "argv=[$*]"\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    _, msg = subject.run_verification_gate("T1", tmp_path / "ws", auto_only=True)
    assert msg == f"argv=[T1 {tmp_path / 'ws'} --auto-only]"


def test_gate_concatenates_stdout_and_stderr_with_no_separator(
    subject, sandbox, monkeypatch, tmp_path
):
    """FOUND_BUGS #19 (legacy only): unterminated stdout is glued straight onto stderr.
    There is no separate stderr stream once the checking engine runs in-process — the
    gate's message is just whatever the engine returned, stripped of surrounding
    whitespace."""
    _fake_verifications(monkeypatch, subject, rc=0, output="  out\n")
    _, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert msg == "out"
    return
    _script(subject.CHECK_VERIFICATIONS, 'printf out; printf err >&2\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    _, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert msg == "outerr"


def test_gate_runs_the_checker_in_the_repo(subject, sandbox, monkeypatch, tmp_path):
    """The one true end-to-end case for maestro: a real ROADMAP.md, the real
    `maestro.verifications` module (no `_fake_verifications` stub), driven through the
    real `run_verification_gate` — proving the in-process wiring itself works, not just
    the stub's contract."""
    _write_roadmap_auto_check(sandbox, "T1", "pwd", "")
    _, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert str(sandbox.repo.resolve()) in msg
    return
    _script(subject.CHECK_VERIFICATIONS, 'pwd\n')
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    _, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert Path(msg).resolve() == subject.REPO.resolve()


def test_gate_reports_a_timeout_as_a_failure(subject, sandbox, monkeypatch, tmp_path):
    """Legacy: the outer 120 s subprocess timeout is reported as a named failure.
    Maestro: that branch has no equivalent — nothing at this call site shells out any
    more — but an exception surfacing from the checking engine (of which a stray
    TimeoutExpired would be one instance) is still caught generically, not left to
    crash the gate."""
    _fake_verifications(
        monkeypatch, subject, raises=subprocess.TimeoutExpired(cmd="check", timeout=120)
    )
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert ok is False
    assert msg.startswith("verification gate error:")
    return
    _script(subject.CHECK_VERIFICATIONS, "true\n")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="check", timeout=120)

    fake = _fake_subprocess(monkeypatch, subject, boom)
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert (ok, msg) == (False, "check_verifications.py timed out (120 s)")
    assert fake.calls[0][1]["timeout"] == 120


def test_gate_reports_any_other_exception_as_a_failure(subject, sandbox, monkeypatch, tmp_path):
    _fake_verifications(monkeypatch, subject, raises=OSError("no exec"))
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert ok is False
    assert msg == "verification gate error: no exec"
    return

    def boom(*args, **kwargs):
        raise OSError("no exec")

    _fake_subprocess(monkeypatch, subject, boom)
    _script(subject.CHECK_VERIFICATIONS, "true\n")
    ok, msg = subject.run_verification_gate("T1", tmp_path / "ws")
    assert ok is False
    assert msg == "check_verifications.py error: no exec"


# ── sonnet_review_proofs ────────────────────────────────────────────────────────────


def test_review_returns_pass_when_there_are_no_manual_verifications(subject, monkeypatch):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed())
    assert subject.sonnet_review_proofs("T1", [], {}) == (True, "")
    assert subject.sonnet_review_proofs("T1", [{"id": "V1", "kind": "auto"}], {}) == (True, "")
    assert fake.calls == []


def test_review_prefilter_rejects_a_short_proof_without_calling_the_agent(subject, monkeypatch):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed())
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "short"}}
    )
    assert ok is False
    assert msg.startswith("Sonnet proof review failed:\n  ✗ V1: proof too vague")
    assert "deterministic pre-filter" in msg
    assert fake.calls == []


def test_review_prefilter_rejects_a_missing_proof(subject, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed())
    ok, msg = subject.sonnet_review_proofs("T1", [{"id": "V1", "kind": "manual"}], {})
    assert ok is False
    assert "got 0 non-ws chars" in msg


def test_review_prefilter_counts_non_whitespace_characters_only(subject, monkeypatch):
    """11 non-ws chars fails, 12 passes the floor — whitespace never counts toward it."""
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout='{"V1": {"pass": true}}'))
    eleven = "a b c d e f g h i j k"  # 11 non-ws chars, 21 raw
    ok, _ = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": eleven}}
    )
    assert ok is False
    ok, _ = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": eleven + " l"}}
    )
    assert ok is True


def test_review_prefilter_only_counts_spaces_tabs_and_newlines_as_whitespace(subject, monkeypatch):
    """FOUND_BUGS: the floor strips " \\t\\n" by hand, so an interior \\r counts as content.

    Seven letters and six carriage returns clears a floor of twelve *characters*.
    """
    fake = _fake_subprocess(
        monkeypatch, subject, lambda *a, **k: _completed(stdout='{"V1": {"pass": true}}')
    )
    ok, _ = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a\r" * 7}}
    )
    assert ok is True
    assert len(fake.calls) == 1


def test_review_prefilter_rejects_a_whole_filler_phrase_that_clears_the_length_floor(
    subject, monkeypatch
):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed())
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "Tested manually."}}
    )
    assert ok is False
    assert "proof too vague" in msg
    assert fake.calls == []


def test_review_prefilter_matches_filler_only_as_the_whole_proof(subject, monkeypatch):
    """A proof merely *containing* a filler word is escalated to the agent, not rejected."""
    fake = _fake_subprocess(
        monkeypatch, subject, lambda *a, **k: _completed(stdout='{"V1": {"pass": true}}')
    )
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "the token flow works"}}
    )
    assert (ok, msg) == (True, "")
    assert len(fake.calls) == 1


def test_review_prefilter_reports_every_bad_proof_at_once(subject, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed())
    verifs = [{"id": "V1", "kind": "manual"}, {"id": "V2", "kind": "manual"}]
    ok, msg = subject.sonnet_review_proofs("T1", verifs, {})
    assert ok is False
    assert msg.count("✗") == 2
    assert "V1" in msg and "V2" in msg


def test_review_raises_when_a_manual_verification_has_no_id(subject, monkeypatch):
    """FOUND_BUGS: `v["id"]` on a hand-edited ROADMAP block crashes the whole gate."""
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed())
    with pytest.raises(KeyError):
        subject.sonnet_review_proofs("T1", [{"kind": "manual"}], {})


def test_review_sends_check_and_proof_for_manual_items_only(subject, monkeypatch):
    fake = _fake_subprocess(
        monkeypatch, subject, lambda *a, **k: _completed(stdout='{"V1": {"pass": true}}')
    )
    verifs = [
        {"id": "V1", "kind": "manual", "check": "the button is blue"},
        {"id": "V2", "kind": "auto", "check": "never reviewed"},
    ]
    subject.sonnet_review_proofs(
        "T7", verifs, {"V1": {"proof": "screenshot attached, hex #0000ff"}}
    )
    argv = fake.calls[0][0][0]
    prompt = argv[-1]
    assert argv[0] == "claude"
    assert "T7" in prompt
    assert "the button is blue" in prompt
    assert "screenshot attached, hex #0000ff" in prompt
    assert "never reviewed" not in prompt


def test_review_passes_when_every_verdict_passes(subject, monkeypatch):
    out = '{"V1": {"pass": true, "reason": "solid"}}'
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=out))
    assert subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    ) == (True, "")


def test_review_fails_and_lists_the_reasons_of_failing_verdicts(subject, monkeypatch):
    out = '{"V1": {"pass": false, "reason": "no evidence"}, "V2": {"pass": true}}'
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=out))
    verifs = [{"id": "V1", "kind": "manual"}, {"id": "V2", "kind": "manual"}]
    impl = {"V1": {"proof": "a detailed proof here"}, "V2": {"proof": "another long proof"}}
    ok, msg = subject.sonnet_review_proofs("T1", verifs, impl)
    assert ok is False
    assert msg == "Sonnet proof review failed:\n  ✗ V1: no evidence"


def test_review_treats_a_missing_pass_key_as_a_failure(subject, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout='{"V1": {}}'))
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert ok is False
    assert msg == "Sonnet proof review failed:\n  ✗ V1: "


def test_review_judges_ids_the_agent_invented_and_ignores_ids_it_dropped(subject, monkeypatch):
    """FOUND_BUGS: the verdict map is never reconciled with the ids that were sent."""
    out = '{"V9": {"pass": false, "reason": "hallucinated"}}'
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=out))
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert (ok, msg) == (False, "Sonnet proof review failed:\n  ✗ V9: hallucinated")

    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="{}"))
    assert subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    ) == (True, "")


def test_review_extracts_json_greedily_from_surrounding_prose(subject, monkeypatch):
    out = 'Sure! Here you go:\n{"V1": {"pass": true}}\nHope that helps.'
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=out))
    assert subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    ) == (True, "")


def test_review_greedy_regex_spans_two_json_objects(subject, monkeypatch):
    """FOUND_BUGS: `\\{.*\\}` with DOTALL grabs first-brace-to-last-brace, not one object."""
    out = '{"V1": {"pass": true}}\nand also {"V1": {"pass": false}}'
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=out))
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert ok is False
    assert msg.startswith("Sonnet returned invalid JSON:")


def test_review_fails_on_a_nonzero_exit_and_truncates_stderr(subject, monkeypatch):
    _fake_subprocess(
        monkeypatch, subject, lambda *a, **k: _completed(returncode=3, stderr="E" * 500)
    )
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert ok is False
    assert msg == "Sonnet review failed (exit 3): " + "E" * 200


def test_review_fails_when_the_output_has_no_braces(subject, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="I cannot do that"))
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert (ok, msg) == (False, "Sonnet returned non-JSON: I cannot do that")


def test_review_fails_when_the_braced_span_is_not_valid_json(subject, monkeypatch):
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="{nope}"))
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert ok is False
    assert msg.startswith("Sonnet returned invalid JSON:")


def test_review_fails_when_a_verdict_is_not_an_object(subject, monkeypatch):
    """A well-formed but wrongly-shaped verdict lands in the catch-all, not the JSON branch."""
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout='{"V1": true}'))
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert ok is False
    assert msg.startswith("Sonnet review error:")


def test_review_reports_a_timeout(subject, monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    fake = _fake_subprocess(monkeypatch, subject, boom)
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert (ok, msg) == (False, "Sonnet review timed out (120 s)")
    assert fake.calls[0][1]["timeout"] == 120


def test_review_reports_a_missing_agent_binary(subject, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("claude")

    _fake_subprocess(monkeypatch, subject, boom)
    ok, msg = subject.sonnet_review_proofs(
        "T1", [{"id": "V1", "kind": "manual"}], {"V1": {"proof": "a detailed proof here"}}
    )
    assert ok is False
    assert msg.startswith("Sonnet review error:")


# ── _run_smoke ──────────────────────────────────────────────────────────────────────


def _stdin_echo_adapter(tmp_path: Path, payload: str) -> Path:
    return _script(
        tmp_path / "smoke_adapter.sh",
        'cat > "$(dirname "$0")/stdin.json"\n'
        f"cat <<'EOF'\n{payload}\nEOF\n",
    )


def test_run_smoke_returns_the_adapters_parsed_stdout(subject, sandbox, monkeypatch, tmp_path):
    adapter = _stdin_echo_adapter(tmp_path, '{"pass": true, "metrics": {"score": 0.9}, "reason": "ok"}')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    assert subject._run_smoke("T1") == {"pass": True, "metrics": {"score": 0.9}, "reason": "ok"}


def test_run_smoke_feeds_the_adapter_repo_task_id_and_a_hardcoded_n(
    subject, sandbox, monkeypatch, tmp_path
):
    adapter = _stdin_echo_adapter(tmp_path, "{}")
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    subject._run_smoke("T7")
    sent = json.loads((tmp_path / "stdin.json").read_text(encoding="utf-8"))
    assert sent == {"worktree": str(subject.REPO), "task_id": "T7", "n": 20}


def test_run_smoke_names_the_key_worktree_but_sends_the_repo(subject, sandbox, monkeypatch, tmp_path):
    """FOUND_BUGS: the smoke always evaluates REPO — the merged trunk, never a worktree."""
    adapter = _stdin_echo_adapter(tmp_path, "{}")
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    subject._run_smoke("T7")
    sent = json.loads((tmp_path / "stdin.json").read_text(encoding="utf-8"))
    assert Path(sent["worktree"]) == subject.REPO


def test_run_smoke_returns_whatever_json_the_adapter_prints(subject, sandbox, monkeypatch, tmp_path):
    """No schema validation: a JSON list comes straight back out of a `-> dict` function."""
    adapter = _stdin_echo_adapter(tmp_path, "[1, 2]")
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    assert subject._run_smoke("T1") == [1, 2]


def test_run_smoke_fails_on_a_nonzero_exit_and_keeps_the_trailing_newline(
    subject, sandbox, monkeypatch, tmp_path
):
    adapter = _script(tmp_path / "a.sh", 'echo boom >&2\nexit 3\n')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    assert subject._run_smoke("T1") == {
        "pass": False, "metrics": {}, "reason": "smoke.py exit=3: boom\n"
    }


def test_run_smoke_prefers_stderr_over_stdout_in_the_failure_reason(
    subject, sandbox, monkeypatch, tmp_path
):
    adapter = _script(tmp_path / "a.sh", 'printf out\nprintf err >&2\nexit 1\n')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    assert subject._run_smoke("T1")["reason"] == "smoke.py exit=1: err"


def test_run_smoke_falls_back_to_stdout_then_to_no_output(subject, sandbox, monkeypatch, tmp_path):
    adapter = _script(tmp_path / "a.sh", 'printf out\nexit 1\n')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    assert subject._run_smoke("T1")["reason"] == "smoke.py exit=1: out"

    _script(adapter, 'exit 1\n')
    assert subject._run_smoke("T1")["reason"] == "smoke.py exit=1: no output"


def test_run_smoke_fails_on_a_silent_success(subject, sandbox, monkeypatch, tmp_path):
    """FOUND_BUGS: exit 0 with blank stdout is reported as `exit=0` — a success-shaped failure."""
    adapter = _script(tmp_path / "a.sh", 'printf "   \\n"\n')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    got = subject._run_smoke("T1")
    assert got["pass"] is False
    assert got["reason"].startswith("smoke.py exit=0:")


def test_run_smoke_truncates_the_error_to_300_characters(subject, sandbox, monkeypatch, tmp_path):
    adapter = _script(tmp_path / "a.sh", 'printf "%0.sE" $(seq 1 500) >&2\nexit 1\n')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    assert subject._run_smoke("T1")["reason"] == "smoke.py exit=1: " + "E" * 300


def test_run_smoke_turns_malformed_adapter_json_into_a_failure(
    subject, sandbox, monkeypatch, tmp_path
):
    adapter = _script(tmp_path / "a.sh", 'echo "not json"\n')
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", adapter)
    monkeypatch.setattr(subject, "VENV_PYTHON", SH)
    got = subject._run_smoke("T1")
    assert got["pass"] is False
    assert got["reason"].startswith("smoke error:")


def test_run_smoke_turns_a_missing_adapter_into_a_failure(subject, sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "SMOKE_ADAPTER", tmp_path / "absent.py")
    monkeypatch.setattr(subject, "VENV_PYTHON", tmp_path / "absent-interpreter")
    got = subject._run_smoke("T1")
    assert got["pass"] is False
    assert got["reason"].startswith("smoke error:")


def test_run_smoke_uses_the_module_timeout_and_says_45_min(subject, sandbox, monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="smoke", timeout=kwargs["timeout"])

    fake = _fake_subprocess(monkeypatch, subject, boom)
    assert subject._run_smoke("T1") == {
        "pass": False, "metrics": {}, "reason": "smoke timed out (45 min)"
    }
    assert fake.calls[0][1]["timeout"] == subject.SMOKE_TIMEOUT
    assert subject.SMOKE_TIMEOUT == 45 * 60


def test_run_smoke_does_not_pin_the_adapters_working_directory(subject, sandbox, monkeypatch):
    """FOUND_BUGS: unlike every other subprocess here, no cwd= is passed."""
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="{}"))
    subject._run_smoke("T1")
    assert "cwd" not in fake.calls[0][1]


# ── _run_custom_smoke ───────────────────────────────────────────────────────────────


def test_custom_smoke_passes_on_exit_zero(subject, sandbox):
    assert subject._run_custom_smoke("T1", "true") == {
        "pass": True, "metrics": {}, "reason": "custom smoke ok: true"
    }


def test_custom_smoke_fails_on_a_nonzero_exit(subject, sandbox):
    assert subject._run_custom_smoke("T1", "echo bad >&2; exit 2") == {
        "pass": False, "metrics": {}, "reason": "custom smoke exit=2: bad\n"
    }


def test_custom_smoke_ignores_stdout_of_a_passing_command(subject, sandbox):
    """Only the exit code is consulted; metrics are always empty."""
    got = subject._run_custom_smoke("T1", 'echo "{\\"pass\\": false}"')
    assert got["pass"] is True
    assert got["metrics"] == {}


def test_custom_smoke_runs_in_the_repo(subject, sandbox, tmp_path):
    out = tmp_path / "cwd.txt"
    subject._run_custom_smoke("T1", f"pwd > {out}")
    assert Path(out.read_text(encoding="utf-8").strip()).resolve() == subject.REPO.resolve()


def test_custom_smoke_uses_a_shell(subject, sandbox, tmp_path):
    out = tmp_path / "piped.txt"
    got = subject._run_custom_smoke("T1", f"echo one two | tr ' ' '\\n' | tail -1 > {out}")
    assert got["pass"] is True
    assert out.read_text(encoding="utf-8").strip() == "two"


def test_custom_smoke_truncates_the_command_in_the_reason(subject, sandbox):
    cmd = "true # " + "x" * 200
    assert subject._run_custom_smoke("T1", cmd)["reason"] == "custom smoke ok: " + cmd[:80]


def test_custom_smoke_truncates_the_error_to_300_characters(subject, sandbox):
    got = subject._run_custom_smoke("T1", 'printf "%0.sE" $(seq 1 500) >&2; exit 1')
    assert got["reason"] == "custom smoke exit=1: " + "E" * 300


def test_custom_smoke_ignores_the_task_id_entirely(subject, sandbox):
    """FOUND_BUGS: task_id is accepted, never used — not even in the reason string."""
    a = subject._run_custom_smoke("T1", "true")
    b = subject._run_custom_smoke("T2", "true")
    assert a == b


def test_custom_smoke_reports_a_timeout_without_naming_the_limit(subject, sandbox, monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=kwargs["timeout"])

    fake = _fake_subprocess(monkeypatch, subject, boom)
    assert subject._run_custom_smoke("T1", "sleep 99") == {
        "pass": False, "metrics": {}, "reason": "custom smoke timed out"
    }
    assert fake.calls[0][1]["timeout"] == subject.SMOKE_TIMEOUT


def test_custom_smoke_reports_any_other_exception(subject, sandbox, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("fork failed")

    _fake_subprocess(monkeypatch, subject, boom)
    assert subject._run_custom_smoke("T1", "true") == {
        "pass": False, "metrics": {}, "reason": "custom smoke error: fork failed"
    }


# ── _smoke_for_task ─────────────────────────────────────────────────────────────────


def _journal(sandbox) -> list[dict]:
    path = sandbox.orch_dir / "journal.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def stub_roadmap(subject, monkeypatch):
    """`get_task_by_id` belongs to the roadmap layer; pin its answer here."""
    box = {"task": None}
    monkeypatch.setattr(subject, "get_task_by_id", lambda task_id: box["task"])
    return box


@pytest.fixture
def stub_retrieval_smoke(subject, monkeypatch):
    calls = []
    monkeypatch.setattr(
        subject, "_run_smoke",
        lambda task_id: calls.append(task_id) or {"pass": True, "metrics": {}, "reason": "SENTINEL"},
    )
    return calls


def test_smoke_for_task_runs_a_task_declared_command(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {"smoke": "true"}
    got = subject._smoke_for_task("T1")
    assert got == {"pass": True, "metrics": {}, "reason": "custom smoke ok: true"}
    assert stub_retrieval_smoke == []


def test_smoke_for_task_journals_the_custom_command_truncated_to_80(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {"smoke": "true # " + "y" * 200}
    subject._smoke_for_task("T1", session_id="s9")
    record = _journal(sandbox)[-1]
    assert record["event"] == "smoke_custom"
    assert record["session_id"] == "s9"
    assert record["detail"] == "T1 cmd=" + ("true # " + "y" * 200)[:80]


def test_smoke_for_task_beats_eval_relevance_with_a_custom_command(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {"smoke": "true", "eval_relevance": "non-retrieval"}
    assert subject._smoke_for_task("T1")["reason"] == "custom smoke ok: true"


def test_smoke_for_task_skips_a_non_retrieval_task(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {"eval_relevance": "non-retrieval"}
    got = subject._smoke_for_task("T1")
    assert got["pass"] is True
    assert got["metrics"] == {}
    assert "skipped" in got["reason"]
    assert stub_retrieval_smoke == []
    assert _journal(sandbox)[-1]["event"] == "smoke_skipped"


def test_smoke_for_task_normalises_case_and_surrounding_space(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {"eval_relevance": "  Non-Retrieval  "}
    assert subject._smoke_for_task("T1")["pass"] is True
    assert stub_retrieval_smoke == []
    assert _journal(sandbox)[-1]["event"] == "smoke_skipped"


def test_smoke_for_task_defaults_to_the_full_smoke(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {}
    assert subject._smoke_for_task("T1")["reason"] == "SENTINEL"
    assert stub_retrieval_smoke == ["T1"]


def test_smoke_for_task_defaults_to_the_full_smoke_for_an_unknown_task(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = None
    assert subject._smoke_for_task("T1")["reason"] == "SENTINEL"
    assert stub_retrieval_smoke == ["T1"]


def test_smoke_for_task_defaults_to_the_full_smoke_for_an_unrecognised_relevance(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    """Fail-safe: only the exact literal `non-retrieval` skips; a typo runs the full smoke."""
    stub_roadmap["task"] = {"eval_relevance": "nonretrieval"}
    assert subject._smoke_for_task("T1")["reason"] == "SENTINEL"


def test_smoke_for_task_treats_an_empty_smoke_command_as_absent(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {"smoke": ""}
    assert subject._smoke_for_task("T1")["reason"] == "SENTINEL"


def test_smoke_for_task_stringifies_a_non_string_smoke_value(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    """FOUND_BUGS: a YAML list under `smoke:` is str()'d and handed to a shell."""
    stub_roadmap["task"] = {"smoke": ["true"]}
    got = subject._smoke_for_task("T1")
    assert got["pass"] is False
    assert got["reason"].startswith("custom smoke exit=")


def test_smoke_for_task_does_not_journal_the_default_path(
    subject, sandbox, stub_roadmap, stub_retrieval_smoke
):
    stub_roadmap["task"] = {}
    subject._smoke_for_task("T1")
    assert _journal(sandbox) == []


# ── _await_absent ───────────────────────────────────────────────────────────────────


def test_await_absent_uses_a_named_artifact_over_the_stdout_signals(subject, tmp_path):
    art = tmp_path / "out.jsonl"
    v = {"await_artifact": str(art)}
    assert subject._await_absent(v, "no eval row tagged T1", tmp_path) is True
    art.write_text("", encoding="utf-8")
    assert subject._await_absent(v, "no eval row tagged T1", tmp_path) is False


def test_await_absent_resolves_a_relative_artifact_against_cwd(subject, tmp_path):
    (tmp_path / "eval").mkdir()
    v = {"await_artifact": "eval/history.jsonl"}
    assert subject._await_absent(v, "", tmp_path) is True
    (tmp_path / "eval" / "history.jsonl").write_text("", encoding="utf-8")
    assert subject._await_absent(v, "", tmp_path) is False


def test_await_absent_accepts_a_directory_as_a_present_artifact(subject, tmp_path):
    """FOUND_BUGS: `exists()` not `is_file()` — an empty directory counts as the artifact."""
    (tmp_path / "artifact").mkdir()
    assert subject._await_absent({"await_artifact": "artifact"}, "", tmp_path) is False


def test_await_absent_accepts_an_empty_artifact_as_present(subject, tmp_path):
    """A zero-byte file satisfies the wait even though no row has been written."""
    (tmp_path / "a.jsonl").write_text("", encoding="utf-8")
    assert subject._await_absent({"await_artifact": "a.jsonl"}, "", tmp_path) is False


def test_await_absent_falls_back_to_stdout_when_the_artifact_key_is_empty(subject, tmp_path):
    v = {"await_artifact": ""}
    assert subject._await_absent(v, "fail: no eval row tagged T1", tmp_path) is True
    assert subject._await_absent(v, "fail: score=0.1 below 0.5", tmp_path) is False


@pytest.mark.parametrize(
    "actual",
    ["fail: no eval row tagged T1", "history file not found", "no baseline row tagged T1"],
)
def test_await_absent_recognises_each_absence_signal(subject, tmp_path, actual):
    assert subject._await_absent({}, actual, tmp_path) is True
    assert subject._await_absent({}, actual.upper(), tmp_path) is True


def test_await_absent_is_false_for_a_present_but_failing_result(subject, tmp_path):
    assert subject._await_absent({}, "fail: score=0.31 below 0.40", tmp_path) is False


def test_await_absent_is_false_for_empty_output(subject, tmp_path):
    """FOUND_BUGS: a crashed gate that printed nothing reads as present-and-failing."""
    assert subject._await_absent({}, "", tmp_path) is False


def test_await_absent_matches_the_signal_anywhere_in_the_output(subject, tmp_path):
    assert subject._await_absent({}, "note: history file not found, retrying", tmp_path) is True


# ── _classify_verifications ─────────────────────────────────────────────────────────


@pytest.fixture
def real_python(subject, monkeypatch):
    """VENV_PYTHON is rebased into the sandbox and does not exist; point it at a real one."""
    monkeypatch.setattr(subject, "VENV_PYTHON", Path(sys.executable))
    return Path(sys.executable)


def test_classify_ignores_non_auto_verifications(subject, sandbox, real_python, tmp_path):
    verifs = [{"id": "V1", "kind": "manual", "cmd": "exit 1"}, {"id": "V2", "cmd": "exit 1"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [])


def test_classify_omits_a_passing_check_from_both_lists(subject, sandbox, real_python, tmp_path):
    verifs = [{"id": "V1", "kind": "auto", "cmd": "echo hi", "expect": "hi"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [])


def test_classify_hard_fails_a_verification_with_no_cmd(subject, sandbox, real_python, tmp_path):
    verifs = [{"id": "V1", "kind": "auto", "await": True}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_hard_fails_an_empty_cmd_even_when_awaited(subject, sandbox, real_python, tmp_path):
    verifs = [{"id": "V1", "kind": "auto", "cmd": "", "await": True}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_reports_a_missing_id_as_an_empty_string(subject, sandbox, real_python, tmp_path):
    """FOUND_BUGS: an id-less verification is reported as `''` rather than named."""
    verifs = [{"kind": "auto"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [""])


def test_classify_stringifies_a_non_string_id(subject, sandbox, real_python, tmp_path):
    assert subject._classify_verifications([{"kind": "auto", "id": 7}], tmp_path) == ([], ["7"])


def test_classify_hard_fails_a_deterministic_mismatch(subject, sandbox, real_python, tmp_path):
    verifs = [{"id": "V1", "kind": "auto", "cmd": "echo nope", "expect": "hi"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_defaults_expect_to_empty_string(subject, sandbox, real_python, tmp_path):
    """FOUND_BUGS: a verification with no `expect` passes iff the command prints nothing."""
    silent = [{"id": "V1", "kind": "auto", "cmd": "true"}]
    noisy = [{"id": "V2", "kind": "auto", "cmd": "echo something"}]
    assert subject._classify_verifications(silent, tmp_path) == ([], [])
    assert subject._classify_verifications(noisy, tmp_path) == ([], ["V2"])


def test_classify_ignores_the_exit_code(subject, sandbox, real_python, tmp_path):
    """FOUND_BUGS: only stdout is compared — a command that exits 1 still passes."""
    verifs = [{"id": "V1", "kind": "auto", "cmd": "echo hi; exit 1", "expect": "hi"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [])


def test_classify_strips_actual_but_not_expect(subject, sandbox, real_python, tmp_path):
    """FOUND_BUGS: a ROADMAP `expect` with trailing whitespace can never match."""
    verifs = [{"id": "V1", "kind": "auto", "cmd": "echo hi", "expect": "hi\n"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_substitutes_the_venv_interpreter_for_a_leading_python(
    subject, sandbox, real_python, tmp_path
):
    verifs = [{
        "id": "V1", "kind": "auto",
        "cmd": 'python3 -c "import sys; print(sys.executable)"',
        "expect": str(real_python),
    }]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [])


def test_classify_substitutes_bare_python_too(subject, sandbox, real_python, tmp_path):
    verifs = [{
        "id": "V1", "kind": "auto",
        "cmd": 'python -c "import sys; print(sys.executable)"',
        "expect": str(real_python),
    }]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [])


def test_classify_only_substitutes_at_the_start_of_the_command(
    subject, sandbox, real_python, tmp_path
):
    """A `python3` that is not the first token survives verbatim into the shell."""
    verifs = [{"id": "V1", "kind": "auto", "cmd": "echo python3", "expect": "python3"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], [])


def test_classify_mangles_a_versioned_interpreter(subject, sandbox, tmp_path, monkeypatch):
    """FOUND_BUGS: `^(python3?)\\b` matches inside `python3.11` — `.` is a word boundary,
    so only the `python3` prefix is replaced and the version tail is glued onto the
    substituted path: `python3.11 -c …` becomes `<VENV_PYTHON>.11 -c …`.

    Pinned without depending on any real interpreter: VENV_PYTHON points at
    `<tmp>/interp`, which does not exist, while `<tmp>/interp.11` does — the rewritten
    command runs the *`.11`* file, which proves both that the substitution fires and
    that the tail is appended rather than replaced. With no `<VENV_PYTHON>.11` on disk
    the command cannot run at all and the check hard-fails.

    (The whole token is *not* replaced, so this only ever resolves by accident. It does
    resolve when VENV_PYTHON is `/usr/bin/python3` and `/usr/bin/python3.11` exists,
    which is why the naive form of this test passed; the real repo's
    `.venv/bin/python3` has no `.11` sibling.)
    """
    _script(tmp_path / "interp.11", "echo spliced\n")
    monkeypatch.setattr(subject, "VENV_PYTHON", tmp_path / "interp")
    assert not (tmp_path / "interp").exists()
    spliced = [{"id": "V1", "kind": "auto", "cmd": 'python3.11 -c "print(1)"',
                "expect": "spliced"}]
    assert subject._classify_verifications(spliced, tmp_path) == ([], [])

    monkeypatch.setattr(subject, "VENV_PYTHON", tmp_path / "missing-interpreter")
    verifs = [{"id": "V1", "kind": "auto", "cmd": 'python3.11 -c "print(1)"', "expect": "1"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_runs_each_command_in_the_given_cwd(subject, sandbox, real_python, tmp_path):
    where = tmp_path / "elsewhere"
    where.mkdir()
    (where / "marker.txt").write_text("here", encoding="utf-8")
    verifs = [{"id": "V1", "kind": "auto", "cmd": "cat marker.txt", "expect": "here"}]
    assert subject._classify_verifications(verifs, where) == ([], [])
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_parks_an_awaited_check_whose_row_has_not_landed(
    subject, sandbox, real_python, tmp_path
):
    verifs = [{
        "id": "V1", "kind": "auto", "await": True,
        "cmd": 'echo "fail: no eval row tagged T1"', "expect": "ok",
    }]
    assert subject._classify_verifications(verifs, tmp_path) == (["V1"], [])


def test_classify_hard_fails_an_awaited_check_that_landed_below_threshold(
    subject, sandbox, real_python, tmp_path
):
    verifs = [{
        "id": "V1", "kind": "auto", "await": True,
        "cmd": 'echo "fail: score=0.10 below 0.40"', "expect": "ok",
    }]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_uses_the_await_artifact_when_the_verification_names_one(
    subject, sandbox, real_python, tmp_path
):
    verifs = [{
        "id": "V1", "kind": "auto", "await": True, "await_artifact": "done.txt",
        "cmd": 'echo "fail: no eval row tagged T1"', "expect": "ok",
    }]
    assert subject._classify_verifications(verifs, tmp_path) == (["V1"], [])
    (tmp_path / "done.txt").write_text("", encoding="utf-8")
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])


def test_classify_only_parks_when_await_is_truthy(subject, sandbox, real_python, tmp_path):
    base = {"id": "V1", "kind": "auto", "cmd": 'echo "no eval row tagged T1"', "expect": "ok"}
    assert subject._classify_verifications([dict(base)], tmp_path) == ([], ["V1"])
    assert subject._classify_verifications([dict(base, **{"await": False})], tmp_path) == ([], ["V1"])
    assert subject._classify_verifications([dict(base, **{"await": "yes"})], tmp_path) == (["V1"], [])


def test_classify_hard_fails_when_the_subprocess_itself_raises(subject, sandbox, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=120)

    fake = _fake_subprocess(monkeypatch, subject, boom)
    verifs = [{"id": "V1", "kind": "auto", "await": True, "cmd": "sleep 999", "expect": "ok"}]
    assert subject._classify_verifications(verifs, tmp_path) == ([], ["V1"])
    assert fake.calls[0][1]["timeout"] == 120
    assert fake.calls[0][1]["shell"] is True


def test_classify_preserves_input_order_within_each_list(subject, sandbox, real_python, tmp_path):
    verifs = [
        {"id": "H1", "kind": "auto", "cmd": "echo x", "expect": "ok"},
        {"id": "P1", "kind": "auto", "await": True, "cmd": 'echo "history file not found"',
         "expect": "ok"},
        {"id": "OK", "kind": "auto", "cmd": "echo ok", "expect": "ok"},
        {"id": "H2", "kind": "auto", "cmd": "echo y", "expect": "ok"},
        {"id": "P2", "kind": "auto", "await": True, "cmd": 'echo "no baseline row tagged Z"',
         "expect": "ok"},
    ]
    assert subject._classify_verifications(verifs, tmp_path) == (["P1", "P2"], ["H1", "H2"])


def test_classify_of_an_empty_list_is_two_empty_lists(subject, sandbox, real_python, tmp_path):
    assert subject._classify_verifications([], tmp_path) == ([], [])


# ── _await_timed_out ────────────────────────────────────────────────────────────────

NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta_seconds: float) -> str:
    return (NOW - timedelta(seconds=delta_seconds)).isoformat()


def test_await_timeout_is_eight_hours(subject):
    assert subject.AWAIT_VERIFY_TIMEOUT_SEC == 8 * 3600


def test_await_not_timed_out_within_the_window(subject):
    assert subject._await_timed_out(_iso(3600), now=NOW) is False


def test_await_timed_out_past_the_window(subject):
    assert subject._await_timed_out(_iso(9 * 3600), now=NOW) is True


def test_await_timeout_boundary_is_strictly_greater_than(subject):
    limit = subject.AWAIT_VERIFY_TIMEOUT_SEC
    assert subject._await_timed_out(_iso(limit), now=NOW) is False
    assert subject._await_timed_out(_iso(limit + 1), now=NOW) is True


def test_await_not_timed_out_for_a_start_in_the_future(subject):
    assert subject._await_timed_out(_iso(-9 * 3600), now=NOW) is False


def test_await_accepts_the_zulu_suffix_this_module_writes(subject):
    assert subject._await_timed_out("2024-01-01T00:00:00Z", now=NOW) is True
    assert subject._await_timed_out("2024-01-01T11:00:00Z", now=NOW) is False


@pytest.mark.parametrize("bad", ["", "not-a-date", "2024-13-01T00:00:00+00:00", "None"])
def test_await_never_times_out_on_an_unparseable_timestamp(subject, bad):
    """FOUND_BUGS: a corrupt `awaiting_since` parks the task forever, silently."""
    assert subject._await_timed_out(bad, now=NOW) is False


def test_await_raises_when_a_naive_timestamp_meets_the_aware_default_now(subject):
    """FOUND_BUGS: the parse is guarded, the subtraction is not — naive input raises TypeError."""
    with pytest.raises(TypeError):
        subject._await_timed_out("2024-01-01T00:00:00", now=NOW)


def test_await_compares_naive_against_naive_without_complaint(subject):
    naive_now = datetime(2024, 1, 1, 12, 0, 0)
    assert subject._await_timed_out("2024-01-01T00:00:00", now=naive_now) is True
    assert subject._await_timed_out("2024-01-01T11:00:00", now=naive_now) is False


def test_await_defaults_now_to_utc_wall_clock(subject):
    long_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    just_now = datetime.now(timezone.utc).isoformat()
    assert subject._await_timed_out(long_ago) is True
    assert subject._await_timed_out(just_now) is False


def test_await_honours_a_non_utc_offset(subject):
    started = datetime(2024, 1, 1, 3, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    # 03:00-05:00 == 08:00Z, i.e. 4 h before NOW.
    assert subject._await_timed_out(started.isoformat(), now=NOW) is False
