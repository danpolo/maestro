"""Pinned behaviour of the merge layer: deny-list, risky reviewer, and every git merge path.

Characterisation, not specification. This is the git-heavy module, so almost every test
runs against a **real throwaway repository** created with ``git init`` inside the sandbox
tree (the ``sandbox`` fixture has already repointed the subject's ``REPO`` at ``tmp_path``,
and every function here runs git with ``cwd=REPO`` or ``git -C REPO``). Nothing that leaves
the machine is ever executed:

* the agent CLI (``_judge_complete``) is monkeypatched in every test that reaches it;
* ``notify_telegram`` / ``_danreq`` / ``_smoke_for_task`` / ``run_dep_map`` are stubs;
* ``git push origin main`` is left to fail against a repo with no remote — git refuses
  locally, without a network round trip, which is exactly the behaviour the callers rely on.

Where the reference's behaviour depends on project vocabulary (deny-list patterns, the
inline ``bot_files`` / ``secrets`` defaults, the regenerable-artifact tuple) the test reads
the value off the subject instead of hardcoding it — see ``_ProjSpy`` and the uses of
``subject._MERGE_DISCARDABLE`` / ``subject.RESUMABLE_MERGE_PREFIXES``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.maestro_module("merge")


# --- git plumbing for the throwaway repo -------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def repo(subject, sandbox, tmp_path, monkeypatch) -> Path:
    """`subject.REPO` (already rebased into tmp_path) turned into a real git repo on `main`.

    git config is pinned to nothing so the operator's global settings cannot change a
    result, and identity comes from the environment so the subject's own un-configured
    `git commit` calls succeed.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")

    r = subject.REPO
    r.mkdir(parents=True, exist_ok=True)
    _git(r, "init", "-q", "-b", "main")
    _write(r, "seed.txt", "seed\n")
    _write(r, subject._EVAL_HISTORY, '{"row": 0}\n')
    _git(r, "add", "--", "seed.txt", subject._EVAL_HISTORY)
    _git(r, "commit", "-q", "-m", "init")
    return r


def _branch(repo: Path, name: str, files: dict[str, str] | None = None,
            message: str = "work") -> str:
    """Create `name` off main, commit `files` on it (empty commit when none), back to main."""
    _git(repo, "checkout", "-q", "-b", name, "main")
    for rel, content in (files or {}).items():
        _write(repo, rel, content)
        _git(repo, "add", "--", rel)
    if files:
        _git(repo, "commit", "-q", "-m", message)
    else:
        _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "checkout", "-q", "main")
    return name


def _commit_on_main(repo: Path, files: dict[str, str], message: str = "main work") -> None:
    for rel, content in files.items():
        _write(repo, rel, content)
        _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)


def _conflict_branch(repo: Path, name: str, rel: str) -> str:
    """A branch that add/add-conflicts with main on `rel`."""
    _git(repo, "checkout", "-q", "-b", name, "main")
    _write(repo, rel, "branch side\n")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", "branch side")
    _git(repo, "checkout", "-q", "main")
    _write(repo, rel, "main side\n")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", "main side")
    return name


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _subjects(repo: Path) -> list[str]:
    return _git(repo, "log", "--pretty=%s").stdout.splitlines()


def _merge_in_progress(repo: Path) -> bool:
    return (repo / ".git" / "MERGE_HEAD").exists()


def _tracked(repo: Path) -> list[str]:
    return _git(repo, "ls-files").stdout.split()


def _dirty(repo: Path) -> list[str]:
    """`git status --porcelain` lines, minus the harness's own `.orchestrator/` directory.

    The `sandbox` fixture creates `<repo>/.orchestrator/workspaces/` *before* the `repo`
    fixture runs `git init`, so plain porcelain always reports `?? .orchestrator/` — a
    fixture artefact, not something the subject did. Everything else is kept, so an empty
    result still means what the tests want it to mean: no uncommitted change to a tracked
    file and no stray file left in the worktree.
    """
    lines = _git(repo, "status", "--porcelain").stdout.splitlines()
    return [
        line for line in lines
        if line.strip() and line[3:].split(" -> ")[-1].rstrip("/") != ".orchestrator"
    ]


# --- journal ------------------------------------------------------------------------


def _events(sandbox) -> list[dict]:
    p = sandbox.orch_dir / "journal.ndjson"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _event_names(sandbox) -> list[str]:
    return [e["event"] for e in _events(sandbox)]


def _detail(sandbox, event: str) -> str:
    for e in _events(sandbox):
        if e["event"] == event:
            return e["detail"]
    raise AssertionError(f"no {event!r} journal record in {_event_names(sandbox)}")


# --- fakes --------------------------------------------------------------------------


class _Recorder:
    """Stand-in for subprocess.run that records argv/kwargs and optionally delegates."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[SimpleNamespace] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(SimpleNamespace(argv=list(argv), kwargs=dict(kwargs)))
        return self._handler(list(argv), dict(kwargs))

    @property
    def argvs(self) -> list[list[str]]:
        return [c.argv for c in self.calls]


def _passthrough(monkeypatch) -> _Recorder:
    """Record every subprocess.run while still really running it."""
    real = subprocess.run
    rec = _Recorder(lambda argv, kw: real(argv, **kw))
    monkeypatch.setattr(subprocess, "run", rec)
    return rec


class _ProjSpy(dict):
    """A project.yaml stand-in that records the *default* each `.get()` was given.

    Lets a test assert the reference's inline fallbacks (`bot_files`, `secrets`) and build
    fixtures from them without copying the consuming project's vocabulary into this file.
    """

    def __init__(self, data: dict | None = None):
        super().__init__(data or {})
        self.defaults: dict = {}

    def get(self, key, default=None):
        self.defaults[key] = default
        return super().get(key, default)


def _use_project(monkeypatch, subject, data: dict | None = None) -> _ProjSpy:
    spy = _ProjSpy(data)
    monkeypatch.setattr(subject, "_load_project_yaml", lambda: spy)
    return spy


class _Judge:
    """Records `_judge_complete` calls and replays a canned answer (or raises)."""

    def __init__(self, answer="", raises=None):
        self.answer = answer
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.answer


def _use_judge(monkeypatch, subject, answer="", raises=None) -> _Judge:
    judge = _Judge(answer, raises)
    monkeypatch.setattr(subject, "_judge_complete", judge)
    return judge


def _project_yaml_path(subject, sandbox) -> Path:
    p = getattr(subject, "PROJECT_YAML", None)
    return p if isinstance(p, Path) else sandbox.repo / "project.yaml"


# =====================================================================================
# _diff_files
# =====================================================================================


def test_diff_files_lists_every_file_the_branch_changed(subject, repo):
    _branch(repo, "b", {"a.txt": "a\n", "seed.txt": "changed\n"})
    assert sorted(subject._diff_files("b")) == ["a.txt", "seed.txt"]


def test_diff_files_ignores_work_done_on_main_after_the_fork(subject, repo):
    _branch(repo, "b", {"a.txt": "a\n"})
    _commit_on_main(repo, {"m.txt": "m\n"})
    assert subject._diff_files("b") == ["a.txt"]


def test_diff_files_empty_commit_yields_empty_list(subject, repo):
    _branch(repo, "b")
    assert subject._diff_files("b") == []


def test_diff_files_unknown_branch_reads_as_no_changes(subject, repo):
    """FOUND_BUGS: the exit status is never inspected, so a typo'd branch looks clean."""
    assert subject._diff_files("no-such-branch") == []


def test_diff_files_outside_a_git_repo_reads_as_no_changes(subject, sandbox):
    assert subject._diff_files("b") == []


def test_diff_files_argv_and_three_dot_range(subject, repo, monkeypatch):
    _branch(repo, "b", {"a.txt": "a\n"})
    rec = _passthrough(monkeypatch)
    subject._diff_files("b")
    assert rec.argvs == [["git", "diff", "--name-only", "main...b"]]
    assert rec.calls[0].kwargs["cwd"] == str(subject.REPO)
    assert rec.calls[0].kwargs["capture_output"] is True
    assert rec.calls[0].kwargs["text"] is True


def test_diff_files_returns_git_escaped_names_for_non_ascii_paths(subject, repo):
    """FOUND_BUGS: a non-ASCII filename comes back quoted and octal-escaped, not as a path.

    Every consumer treats these strings as paths (substring matches against deny-lists,
    equality against `git status` output), so the escaped form silently changes meaning.
    """
    _branch(repo, "b", {"café.txt": "x\n"})
    assert subject._diff_files("b") == ['"caf\\303\\251.txt"']


# =====================================================================================
# deny_list_guard
# =====================================================================================


def test_deny_list_guard_clean_branch_passes(subject, repo):
    _branch(repo, "b", {"src/app.py": "print('hello')\n"})
    assert subject.deny_list_guard("b", "T1") == (True, "ok")


def test_deny_list_guard_blocks_a_hard_deny_pattern(subject, repo):
    _branch(repo, "b", {"db/migrate.sql": "DROP TABLE users;\n"})
    ok, reason = subject.deny_list_guard("b", "T1")
    assert ok is False
    assert "deny-list violation" in reason
    assert "pattern" in reason


def test_deny_list_guard_scans_the_diff_text_so_deletions_trip_it_too(subject, repo):
    """FOUND_BUGS: the guard matches the raw diff, so *removing* a dangerous line is denied.

    The removed line appears in the diff as a `-` line and matches exactly as an addition
    would; cleaning up a deny-listed statement therefore blocks its own merge.
    """
    _commit_on_main(repo, {"db/migrate.sql": "DELETE FROM users;\n"})
    _git(repo, "checkout", "-q", "-b", "b", "main")
    _write(repo, "db/migrate.sql", "-- cleaned up\n")
    _git(repo, "add", "--", "db/migrate.sql")
    _git(repo, "commit", "-q", "-m", "remove the dangerous statement")
    _git(repo, "checkout", "-q", "main")
    ok, _ = subject.deny_list_guard("b", "T1")
    assert ok is False


@pytest.mark.parametrize(
    "line, expected_ok",
    [
        ("sudo rm -rf /var\n", False),
        ("sudo systemctl restart svc\n", True),
        ("echo no privileges here\n", True),
    ],
)
def test_deny_list_guard_sudo_lookaheads(subject, repo, line, expected_ok):
    _branch(repo, "b", {"scripts/do.sh": line})
    ok, _ = subject.deny_list_guard("b", "T1")
    assert ok is expected_ok


def test_deny_list_guard_lookahead_is_line_scoped(subject, repo):
    """FOUND_BUGS: the exemption lookahead cannot see past a newline.

    A later line mentioning the exempted command does not rescue an earlier bare `sudo`,
    which is the behaviour you want — but the converse is the surprise: the exemption is
    granted purely by what follows on the *same* line, so `sudo rm -rf / # systemctl`
    would be waved through.
    """
    _branch(repo, "b", {"scripts/do.sh": "sudo rm -rf /var\nsystemctl restart svc\n"})
    assert subject.deny_list_guard("b", "T1")[0] is False
    _branch(repo, "c", {"scripts/do2.sh": "sudo rm -rf /var # systemctl\n"})
    assert subject.deny_list_guard("c", "T1")[0] is True


def test_deny_list_guard_appends_project_yaml_extra_patterns(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"deny_list_extra": [r"FORBIDDEN_TOKEN_XYZ"]})
    _branch(repo, "b", {"src/app.py": "FORBIDDEN_TOKEN_XYZ = 1\n"})
    ok, reason = subject.deny_list_guard("b", "T1")
    assert ok is False
    assert "FORBIDDEN_TOKEN_XYZ" in reason


def test_deny_list_guard_extra_patterns_do_not_replace_the_builtin_ones(
    subject, repo, monkeypatch
):
    _use_project(monkeypatch, subject, {"deny_list_extra": [r"FORBIDDEN_TOKEN_XYZ"]})
    _branch(repo, "b", {"db/migrate.sql": "DROP TABLE users;\n"})
    assert subject.deny_list_guard("b", "T1")[0] is False


def test_deny_list_guard_default_secret_list_is_read_from_the_reference(
    subject, repo, monkeypatch
):
    spy = _use_project(monkeypatch, subject, {})
    _branch(repo, "b", {"src/app.py": "x = 1\n"})
    subject.deny_list_guard("b", "T1")
    secrets = spy.defaults["secrets"]
    assert isinstance(secrets, list) and secrets
    assert all(isinstance(s, str) and s.strip() for s in secrets)


def test_deny_list_guard_secret_match_is_a_substring_not_a_filename(subject, repo, monkeypatch):
    """FOUND_BUGS: `clean in f` matches anywhere in the path, so lookalikes are blocked.

    A sample/template file whose name merely *contains* a secret filename is refused,
    and conversely a real secret in a directory named after something else is not.
    """
    spy = _use_project(monkeypatch, subject, {})
    _branch(repo, "b", {"src/app.py": "x = 1\n"})
    subject.deny_list_guard("b", "T1")
    needle = spy.defaults["secrets"][0]

    _branch(repo, "c", {f"docs/example{needle}.sample": "placeholder\n"})
    ok, reason = subject.deny_list_guard("c", "T1")
    assert ok is False
    assert "secret file in diff" in reason


def test_deny_list_guard_secret_patterns_are_stripped_of_stars_and_slashes(
    subject, repo, monkeypatch
):
    _use_project(monkeypatch, subject, {"secrets": ["*/vault/*"]})
    _branch(repo, "b", {"app/vault/key.txt": "placeholder\n"})
    ok, reason = subject.deny_list_guard("b", "T1")
    assert ok is False
    assert "app/vault/key.txt" in reason


def test_deny_list_guard_pattern_check_runs_before_the_secret_check(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"secrets": ["secretfile"]})
    _branch(repo, "b", {"secretfile": "DROP TABLE users;\n"})
    ok, reason = subject.deny_list_guard("b", "T1")
    assert ok is False
    assert "pattern" in reason
    assert "secret file in diff" not in reason


def test_deny_list_guard_unknown_branch_passes(subject, repo):
    """FOUND_BUGS: a branch that does not exist produces an empty diff, so the gate says ok."""
    assert subject.deny_list_guard("no-such-branch", "T1") == (True, "ok")


def test_deny_list_guard_ignores_its_task_id_argument(subject, repo):
    """FOUND_BUGS: `task_id` is accepted, never read, and never reaches the reason string."""
    _branch(repo, "b", {"db/migrate.sql": "DROP TABLE users;\n"})
    a = subject.deny_list_guard("b", "T1")
    b = subject.deny_list_guard("b", "COMPLETELY-OTHER")
    assert a == b
    assert "T1" not in a[1]


def test_deny_list_guard_reads_project_yaml_from_disk(subject, repo, sandbox):
    path = _project_yaml_path(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("deny_list_extra:\n  - 'FORBIDDEN_TOKEN_XYZ'\n", encoding="utf-8")
    _branch(repo, "b", {"src/app.py": "FORBIDDEN_TOKEN_XYZ = 1\n"})
    assert subject.deny_list_guard("b", "T1")[0] is False


def test_deny_list_guard_malformed_project_yaml_falls_back_to_defaults(subject, repo, sandbox):
    path = _project_yaml_path(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this: [is: not: valid", encoding="utf-8")
    _branch(repo, "b", {"src/app.py": "x = 1\n"})
    assert subject.deny_list_guard("b", "T1") == (True, "ok")


# =====================================================================================
# sonnet_risky_reviewer
# =====================================================================================


def test_risky_reviewer_no_risky_set_short_circuits(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    judge = _use_judge(monkeypatch, subject, "{}")
    _branch(repo, "b", {"src/app.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (True, "no risky_set defined")
    assert judge.calls == []


def test_risky_reviewer_no_risky_file_touched_short_circuits(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    judge = _use_judge(monkeypatch, subject, "{}")
    _branch(repo, "b", {"src/app.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (True, "no risky_set files touched")
    assert judge.calls == []


def test_risky_reviewer_approves_on_pass_true(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"pass": true, "reason": "benign"}')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (True, "benign")


def test_risky_reviewer_blocks_on_pass_false(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"pass": false, "reason": "unsafe"}')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (False, "unsafe")


def test_risky_reviewer_prompt_contract(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    judge = _use_judge(monkeypatch, subject, '{"pass": true, "reason": "ok"}')
    _branch(repo, "b", {"src/core.py": "SENTINEL_LINE = 1\n"})
    subject.sonnet_risky_reviewer("b", "T77")
    assert len(judge.calls) == 1
    call = judge.calls[0]
    assert set(call) == {"system", "user", "model"}
    assert call["model"] == "claude-sonnet-5"
    assert "JSON" in call["system"]
    assert '"pass"' in call["system"]
    assert "T77" in call["user"]
    assert "src/core.py" in call["user"]
    assert "SENTINEL_LINE" in call["user"]


def test_risky_reviewer_empty_judge_output_blocks(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, "")
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (
        False, "risky reviewer unavailable (CLI judge returned nothing)"
    )


def test_risky_reviewer_extracts_json_from_markdown(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject,
               'Sure!\n```json\n{"pass": true, "reason": "fine"}\n```\nhope that helps')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (True, "fine")


def test_risky_reviewer_prose_without_braces_is_a_parse_error(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, "Looks fine to me, ship it.")
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    ok, reason = subject.sonnet_risky_reviewer("b", "T1")
    assert ok is False
    assert reason.startswith("parse error:")


def test_risky_reviewer_broken_json_is_reported_as_an_error(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"pass": tru, }')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    ok, reason = subject.sonnet_risky_reviewer("b", "T1")
    assert ok is False
    assert reason.startswith("risky reviewer error:")


def test_risky_reviewer_judge_exception_fails_safe(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, raises=RuntimeError("cli exploded"))
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    ok, reason = subject.sonnet_risky_reviewer("b", "T1")
    assert ok is False
    assert reason.startswith("risky reviewer error:")
    assert "cli exploded" in reason


def test_risky_reviewer_missing_pass_key_blocks_with_empty_reason(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"verdict": "fine"}')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (False, "")


def test_risky_reviewer_any_truthy_pass_value_approves(subject, repo, monkeypatch):
    """FOUND_BUGS: `bool(obj.get("pass"))` — the string "false" approves the merge."""
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"pass": "false", "reason": "r"}')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    assert subject.sonnet_risky_reviewer("b", "T1") == (True, "r")


def test_risky_reviewer_risky_set_entries_match_as_substrings(subject, repo, monkeypatch):
    """FOUND_BUGS: `r in f`, so a short risky_set entry captures unrelated files."""
    _use_project(monkeypatch, subject, {"risky_set": ["core"]})
    judge = _use_judge(monkeypatch, subject, '{"pass": true, "reason": "ok"}')
    _branch(repo, "b", {"docs/hardcore-notes.md": "x\n"})
    assert subject.sonnet_risky_reviewer("b", "T1")[0] is True
    assert "docs/hardcore-notes.md" in judge.calls[0]["user"]


def test_risky_reviewer_truncates_the_diff_at_6000_chars(subject, repo, monkeypatch):
    """FOUND_BUGS: the diff is cut mid-hunk with no marker, so the reviewer judges a fragment."""
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    judge = _use_judge(monkeypatch, subject, '{"pass": true, "reason": "ok"}')
    body = "".join(f"line_{i}_padding_padding_padding\n" for i in range(400))
    _branch(repo, "b", {"src/core.py": body + "TAIL_SENTINEL\n"})
    subject.sonnet_risky_reviewer("b", "T1")
    user = judge.calls[0]["user"]
    assert "line_0_padding" in user
    assert "TAIL_SENTINEL" not in user


def test_risky_reviewer_announces_the_touched_files(subject, repo, monkeypatch, capsys):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"pass": true, "reason": "ok"}')
    _branch(repo, "b", {"src/core.py": "x = 1\n"})
    subject.sonnet_risky_reviewer("b", "T42")
    out = capsys.readouterr().out
    assert "T42" in out
    assert "src/core.py" in out


# =====================================================================================
# _touches_bot_files
# =====================================================================================


def test_touches_bot_files_uses_an_inline_default_when_project_yaml_is_silent(
    subject, repo, monkeypatch
):
    spy = _use_project(monkeypatch, subject, {})
    _branch(repo, "b", {"src/app.py": "x\n"})
    assert subject._touches_bot_files("b") is False
    default = spy.defaults["bot_files"]
    assert isinstance(default, list) and default
    _branch(repo, "c", {default[0]: "x\n"})
    assert subject._touches_bot_files("c") is True


def test_touches_bot_files_honours_a_configured_list(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"bot_files": ["svc/runner.py"]})
    _branch(repo, "b", {"svc/runner.py": "x\n"})
    assert subject._touches_bot_files("b") is True


def test_touches_bot_files_matches_substrings(subject, repo, monkeypatch):
    """FOUND_BUGS: `bf in f` — a bot_files entry captures any path containing it."""
    _use_project(monkeypatch, subject, {"bot_files": ["bot.py"]})
    _branch(repo, "b", {"src/chatbot.py": "x\n"})
    assert subject._touches_bot_files("b") is True


def test_touches_bot_files_empty_list_never_matches(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"bot_files": []})
    _branch(repo, "b", {"src/app.py": "x\n"})
    assert subject._touches_bot_files("b") is False


def test_touches_bot_files_no_changes_is_false(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"bot_files": ["svc/runner.py"]})
    _branch(repo, "b")
    assert subject._touches_bot_files("b") is False


# =====================================================================================
# _git_head
# =====================================================================================


def test_git_head_returns_the_full_sha(subject, repo):
    head = subject._git_head()
    assert head == _head(repo)
    assert len(head) == 40


def test_git_head_short_is_an_abbreviated_prefix(subject, repo):
    full, short = subject._git_head(), subject._git_head(short=True)
    assert short and len(short) < len(full)
    assert full.startswith(short)


def test_git_head_outside_a_repo_returns_empty_string(subject, sandbox):
    """FOUND_BUGS: the failure is silent, so callers compare "" against "" and see no change."""
    assert subject._git_head() == ""


@pytest.mark.parametrize(
    "short, expected",
    [(False, ["git", "rev-parse", "HEAD"]), (True, ["git", "rev-parse", "--short", "HEAD"])],
)
def test_git_head_argv(subject, repo, monkeypatch, short, expected):
    rec = _passthrough(monkeypatch)
    subject._git_head(short=short)
    assert rec.argvs == [expected]
    assert rec.calls[0].kwargs["cwd"] == str(subject.REPO)


# =====================================================================================
# _commit_eval_history
# =====================================================================================


def test_commit_eval_history_noop_when_clean(subject, repo, sandbox):
    before = _head(repo)
    assert subject._commit_eval_history("T1") is False
    assert _head(repo) == before
    assert _event_names(sandbox) == []


def test_commit_eval_history_commits_a_modified_row(subject, repo, sandbox):
    before = _head(repo)
    _write(repo, subject._EVAL_HISTORY, '{"row": 0}\n{"row": 1}\n')
    assert subject._commit_eval_history("T1") is True
    assert _head(repo) != before
    assert _dirty(repo) == []
    detail = _detail(sandbox, "eval_history_committed")
    assert "T1" in detail
    assert subject._EVAL_HISTORY in detail


def test_commit_eval_history_commits_an_untracked_file_at_that_path(subject, repo, tmp_path):
    _git(repo, "rm", "-q", "--cached", "--", subject._EVAL_HISTORY)
    _git(repo, "commit", "-q", "-m", "untrack")
    assert subject._EVAL_HISTORY not in _tracked(repo)
    assert subject._commit_eval_history("T2") is True
    assert subject._EVAL_HISTORY in _tracked(repo)


def test_commit_eval_history_message_mentions_the_task(subject, repo):
    _write(repo, subject._EVAL_HISTORY, '{"row": 1}\n')
    subject._commit_eval_history("T9")
    assert "T9" in _subjects(repo)[0]


def test_commit_eval_history_message_has_no_trailing_space_without_a_task(subject, repo):
    _write(repo, subject._EVAL_HISTORY, '{"row": 1}\n')
    subject._commit_eval_history()
    subject_line = _subjects(repo)[0]
    assert subject_line == subject_line.strip()


def test_commit_eval_history_reports_success_even_when_the_commit_fails(
    subject, repo, sandbox, monkeypatch
):
    """FOUND_BUGS: neither `git add` nor `git commit` is checked — True means "was dirty"."""
    real = subprocess.run

    def fake(argv, **kwargs):
        if "commit" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "nope")
        return real(argv, **kwargs)

    _write(repo, subject._EVAL_HISTORY, '{"row": 1}\n')
    monkeypatch.setattr(subprocess, "run", fake)
    assert subject._commit_eval_history("T1") is True
    monkeypatch.undo()
    assert "eval_history_committed" in _event_names(sandbox)
    # `git add` really ran, only the commit was faked: the row is left staged, uncommitted.
    assert _dirty(repo) == [f"M  {subject._EVAL_HISTORY}"]


# =====================================================================================
# _clean_worktree_for_merge
# =====================================================================================


def test_clean_worktree_no_overlap_is_a_noop(subject, repo, sandbox):
    _write(repo, "seed.txt", "dirty\n")
    assert subject._clean_worktree_for_merge(["other.txt"]) == (True, [])
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "dirty\n"
    assert _event_names(sandbox) == []


def test_clean_worktree_outside_a_repo_is_best_effort(subject, sandbox):
    assert subject._clean_worktree_for_merge(["a.txt"]) == (True, [])


def test_clean_worktree_discards_a_dirty_regenerable_artifact(subject, repo, sandbox):
    artifact = subject._MERGE_DISCARDABLE[0]
    _commit_on_main(repo, {artifact: "committed\n"})
    _write(repo, artifact, "locally regenerated\n")
    assert subject._clean_worktree_for_merge([artifact]) == (True, [])
    assert (repo / artifact).read_text(encoding="utf-8") == "committed\n"
    assert artifact in _detail(sandbox, "merge_worktree_cleaned")


def test_clean_worktree_commits_dirty_append_only_telemetry(subject, repo, sandbox):
    committable = subject._MERGE_COMMITTABLE[0]
    before = _head(repo)
    _write(repo, committable, '{"row": 0}\n{"row": 1}\n')
    assert subject._clean_worktree_for_merge([committable]) == (True, [])
    assert _head(repo) != before
    assert (repo / committable).read_text(encoding="utf-8") == '{"row": 0}\n{"row": 1}\n'
    assert committable in _detail(sandbox, "merge_worktree_committed")


def test_clean_worktree_refuses_when_real_work_is_uncommitted(subject, repo, sandbox):
    _write(repo, "seed.txt", "hand edited\n")
    assert subject._clean_worktree_for_merge(["seed.txt"]) == (False, ["seed.txt"])
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "hand edited\n"


def test_clean_worktree_still_discards_while_refusing(subject, repo):
    artifact = subject._MERGE_DISCARDABLE[0]
    _commit_on_main(repo, {artifact: "committed\n"})
    _write(repo, artifact, "regenerated\n")
    _write(repo, "seed.txt", "hand edited\n")
    ok, blocking = subject._clean_worktree_for_merge([artifact, "seed.txt"])
    assert (ok, blocking) == (False, ["seed.txt"])
    assert (repo / artifact).read_text(encoding="utf-8") == "committed\n"


def test_clean_worktree_ignores_untracked_files(subject, repo):
    """FOUND_BUGS: `--untracked-files=no` hides an untracked file the merge will collide with."""
    _write(repo, "new.txt", "untracked\n")
    assert subject._clean_worktree_for_merge(["new.txt"]) == (True, [])


def test_clean_worktree_sees_staged_but_uncommitted_changes(subject, repo):
    _write(repo, "seed.txt", "staged\n")
    _git(repo, "add", "--", "seed.txt")
    assert subject._clean_worktree_for_merge(["seed.txt"]) == (False, ["seed.txt"])


def test_clean_worktree_misses_paths_containing_a_space(subject, repo):
    """FOUND_BUGS: `git status --porcelain` quotes a path with a space, `git diff` does not.

    The set comparison is therefore false, the blocking file is not reported, and the
    caller's `git merge` fails with the misleading "local changes would be overwritten".
    """
    _commit_on_main(repo, {"two words.txt": "committed\n"})
    _write(repo, "two words.txt", "hand edited\n")
    assert subject._clean_worktree_for_merge(["two words.txt"]) == (True, [])


def test_clean_worktree_blocking_preserves_the_order_of_changed(subject, repo):
    _commit_on_main(repo, {"a.txt": "a\n", "b.txt": "b\n"})
    _write(repo, "a.txt", "dirty a\n")
    _write(repo, "b.txt", "dirty b\n")
    assert subject._clean_worktree_for_merge(["b.txt", "a.txt"]) == (False, ["b.txt", "a.txt"])


# =====================================================================================
# _autoresolve_doc_conflicts
# =====================================================================================


def _seed_regenerables(subject, repo) -> None:
    _commit_on_main(
        repo, {name: f"base {name}\n" for name in subject._MERGE_DISCARDABLE}, "seed artifacts"
    )


def _dep_map_stub(subject, repo, monkeypatch) -> list[str]:
    """Stand in for the dependency-map regeneration: rewrite every regenerable artifact."""
    calls: list[str] = []

    def regen():
        calls.append("run_dep_map")
        for name in subject._MERGE_DISCARDABLE:
            if (repo / name).exists():
                _write(repo, name, "regenerated\n")

    monkeypatch.setattr(subject, "run_dep_map", regen)
    return calls


def test_autoresolve_returns_false_when_nothing_is_conflicted(subject, repo, monkeypatch):
    calls = _dep_map_stub(subject, repo, monkeypatch)
    assert subject._autoresolve_doc_conflicts() is False
    assert calls == []


def test_autoresolve_refuses_a_code_conflict(subject, repo, monkeypatch, sandbox):
    calls = _dep_map_stub(subject, repo, monkeypatch)
    _conflict_branch(repo, "b", "src/app.py")
    _git(repo, "merge", "--no-ff", "b", "-m", "m", check=False)
    assert _merge_in_progress(repo)
    assert subject._autoresolve_doc_conflicts() is False
    assert calls == []
    assert _merge_in_progress(repo)          # left for the caller to abort
    assert _event_names(sandbox) == []


def test_autoresolve_refuses_a_mixed_conflict(subject, repo, monkeypatch):
    _seed_regenerables(subject, repo)
    calls = _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    _git(repo, "checkout", "-q", "-b", "b", "main")
    _write(repo, artifact, "branch\n")
    _write(repo, "src/app.py", "branch\n")
    _git(repo, "add", "-A", "--", artifact, "src/app.py")
    _git(repo, "commit", "-q", "-m", "branch")
    _git(repo, "checkout", "-q", "main")
    _write(repo, artifact, "main\n")
    _write(repo, "src/app.py", "main\n")
    _git(repo, "add", "-A", "--", artifact, "src/app.py")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "merge", "--no-ff", "b", "-m", "m", check=False)
    assert subject._autoresolve_doc_conflicts() is False
    assert calls == []


def test_autoresolve_completes_a_pure_doc_conflict(subject, repo, monkeypatch, sandbox):
    _seed_regenerables(subject, repo)
    calls = _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    _conflict_branch(repo, "b", artifact)
    _git(repo, "merge", "--no-ff", "b", "-m", "m", check=False)
    assert _merge_in_progress(repo)

    assert subject._autoresolve_doc_conflicts() is True
    assert calls == ["run_dep_map"]
    assert not _merge_in_progress(repo)
    assert (repo / artifact).read_text(encoding="utf-8") == "regenerated\n"
    assert "<<<<<<<" not in (repo / artifact).read_text(encoding="utf-8")
    assert _dirty(repo) == []
    assert artifact in _detail(sandbox, "merge_docs_autoresolved")


def test_autoresolve_fails_when_a_regenerable_artifact_is_missing_from_the_tree(
    subject, repo, monkeypatch, sandbox
):
    """FOUND_BUGS: `git add -- <all regenerables>` aborts on the first missing pathspec.

    The auto-resolution only works in a checkout where *every* file in the regenerable
    tuple exists. Seed just the conflicted one and the `git add` fails with "pathspec did
    not match", nothing is staged, `git commit --no-edit` refuses because the path is still
    unmerged, and the function reports False — leaving a conflicted merge in progress that
    looks to the caller like a genuine content conflict.
    """
    artifact = subject._MERGE_DISCARDABLE[0]
    assert len(subject._MERGE_DISCARDABLE) > 1
    _commit_on_main(repo, {artifact: "base\n"}, "seed one artifact only")
    calls = _dep_map_stub(subject, repo, monkeypatch)
    _conflict_branch(repo, "b", artifact)
    _git(repo, "merge", "--no-ff", "b", "-m", "m", check=False)

    assert subject._autoresolve_doc_conflicts() is False
    assert calls == ["run_dep_map"]          # regeneration ran; only the commit failed
    assert _merge_in_progress(repo)
    assert "merge_docs_autoresolved" not in _event_names(sandbox)


def test_autoresolve_regenerates_before_committing(subject, repo, monkeypatch):
    """The commit records the regenerated content, not the conflict markers."""
    _seed_regenerables(subject, repo)
    _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    _conflict_branch(repo, "b", artifact)
    _git(repo, "merge", "--no-ff", "b", "-m", "m", check=False)
    subject._autoresolve_doc_conflicts()
    committed = _git(repo, "show", f"HEAD:{artifact}").stdout
    assert committed == "regenerated\n"


# =====================================================================================
# _parse_progress
# =====================================================================================


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PROGRESS=3/10", (3, 10)),
        ("noise PROGRESS=0/1 noise", (0, 1)),
        ("done 7/12 items", (7, 12)),
        ("7 / 12", (7, 12)),
        ("7  /  12", (7, 12)),
        ("", None),
        ("no numbers here", None),
        ("only 42 here", None),
        ("PROGRESS=12/0", (12, 0)),
    ],
)
def test_parse_progress_table(subject, text, expected):
    assert subject._parse_progress(text) == expected


def test_parse_progress_prefers_the_explicit_marker_over_an_earlier_fraction(subject):
    assert subject._parse_progress("saw 1/2 files ... PROGRESS=5/6") == (5, 6)


def test_parse_progress_fallback_matches_anything_slash_shaped(subject):
    """FOUND_BUGS: the fallback reads a date or a version string as progress."""
    assert subject._parse_progress("finished on 2026/08") == (2026, 8)


def test_parse_progress_ignores_a_sign(subject):
    """FOUND_BUGS: `-3/10` parses as (3, 10) — the minus is simply not part of the match."""
    assert subject._parse_progress("PROGRESS=-3/10") == (3, 10)


def test_parse_progress_rejects_non_string_input(subject):
    with pytest.raises(TypeError):
        subject._parse_progress(None)


# =====================================================================================
# _merge_data_only
# =====================================================================================


def _allowed(subject, name: str) -> str:
    return subject.RESUMABLE_MERGE_PREFIXES[0] + name


@pytest.mark.parametrize("entry", [{}, {"branch": ""}])
def test_merge_data_only_without_a_branch_is_an_error(subject, repo, entry):
    assert subject._merge_data_only(entry) == "error"


def test_merge_data_only_unknown_branch_is_an_error(subject, repo):
    assert subject._merge_data_only({"branch": "no-such-branch"}) == "error"


def test_merge_data_only_empty_commit_is_empty(subject, repo):
    _branch(repo, "b")
    assert subject._merge_data_only({"branch": "b"}) == "empty"


def test_merge_data_only_out_of_allowlist_is_a_code_change(subject, repo):
    _branch(repo, "b", {"src/app.py": "x\n"})
    assert subject._merge_data_only({"branch": "b"}) == "code_change"


def test_merge_data_only_one_stray_file_taints_an_allowlisted_diff(subject, repo):
    _branch(repo, "b", {_allowed(subject, "rows.jsonl"): "r\n", "src/app.py": "x\n"})
    assert subject._merge_data_only({"branch": "b"}) == "code_change"


def test_merge_data_only_merges_allowlisted_paths(subject, repo):
    rel = _allowed(subject, "rows.jsonl")
    _branch(repo, "b", {rel: "row\n"})
    assert subject._merge_data_only({"branch": "b"}) == "ok"
    assert (repo / rel).read_text(encoding="utf-8") == "row\n"
    assert _subjects(repo)[0] == "merge(resume): partial data progress from b"


def test_merge_data_only_pushes_and_ignores_the_failure(subject, repo, monkeypatch):
    """There is no `origin` here, so the push fails locally — and the result is still ok."""
    rel = _allowed(subject, "rows.jsonl")
    _branch(repo, "b", {rel: "row\n"})
    rec = _passthrough(monkeypatch)
    assert subject._merge_data_only({"branch": "b"}) == "ok"
    assert ["git", "-C", str(subject.REPO), "push", "origin", "main"] in rec.argvs


def test_merge_data_only_dirty_worktree_escalates(subject, repo, sandbox):
    rel = _allowed(subject, "rows.jsonl")
    _commit_on_main(repo, {rel: "committed\n"})
    _branch(repo, "b", {rel: "branch row\n"})
    _write(repo, rel, "hand edited\n")
    assert subject._merge_data_only({"branch": "b"}) == "dirty_worktree"
    assert rel in _detail(sandbox, "merge_worktree_dirty")
    assert (repo / rel).read_text(encoding="utf-8") == "hand edited\n"


def test_merge_data_only_conflict_aborts_the_merge(subject, repo):
    rel = _allowed(subject, "rows.jsonl")
    _conflict_branch(repo, "b", rel)
    before = _head(repo)
    assert subject._merge_data_only({"branch": "b"}) == "conflict"
    assert not _merge_in_progress(repo)
    assert _head(repo) == before
    assert (repo / rel).read_text(encoding="utf-8") == "main side\n"


def test_merge_data_only_does_not_autoresolve_doc_conflicts(subject, repo, monkeypatch):
    """FOUND_BUGS: this path allows docs/ but never calls the doc auto-resolver.

    The same regenerable artifact that `merge_and_eval` and `_merge_prep_branch` recover
    from is a hard "conflict" here, so a resumable sitting parks for a human over a file
    that is regenerated from source.
    """
    calls = _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    assert artifact.startswith(subject.RESUMABLE_MERGE_PREFIXES)
    _seed_regenerables(subject, repo)
    _conflict_branch(repo, "b", artifact)
    assert subject._merge_data_only({"branch": "b"}) == "conflict"
    assert calls == []


# =====================================================================================
# _resumable_code_change_escalation
# =====================================================================================


@pytest.mark.parametrize("entry", [{}, {"branch": ""}])
def test_resumable_escalation_without_a_branch(subject, repo, entry):
    assert subject._resumable_code_change_escalation(entry, "T1") == (
        False, "no branch on entry", "error"
    )


def test_resumable_escalation_unknown_branch(subject, repo):
    assert subject._resumable_code_change_escalation({"branch": "nope"}, "T1") == (
        False, "diff failed", "error"
    )


def test_resumable_escalation_with_no_out_of_allowlist_files(subject, repo, monkeypatch):
    judge = _use_judge(monkeypatch, subject, "{}")
    _branch(repo, "b", {_allowed(subject, "rows.jsonl"): "r\n"})
    assert subject._resumable_code_change_escalation({"branch": "b"}, "T1") == (
        False, "no out-of-allowlist files (unexpected)", "error"
    )
    assert judge.calls == []


def test_resumable_escalation_hard_stop_never_reaches_the_judge(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    judge = _use_judge(monkeypatch, subject, '{"pass": true}')
    hard = subject._RESUMABLE_HARD_STOP[0]
    _branch(repo, "b", {hard: "x\n"})
    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, status) == (False, "code_change")
    assert reason.startswith("hard-stop files touched:")
    assert hard in reason
    assert judge.calls == []


def test_resumable_escalation_risky_set_is_a_hard_stop(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    judge = _use_judge(monkeypatch, subject, '{"pass": true}')
    _branch(repo, "b", {"src/core.py": "x\n"})
    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, status) == (False, "code_change")
    assert reason.startswith("hard-stop files touched:")
    assert judge.calls == []


def test_resumable_escalation_bot_files_default_is_a_hard_stop(subject, repo, monkeypatch):
    spy = _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": true}')
    _branch(repo, "probe", {"src/app.py": "x\n"})
    subject._resumable_code_change_escalation({"branch": "probe"}, "T1")
    default = spy.defaults["bot_files"]
    _branch(repo, "b", {default[0]: "x\n"})
    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, status) == (False, "code_change")
    assert reason.startswith("hard-stop files touched:")


def test_resumable_escalation_judge_unavailable(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, "")
    _branch(repo, "b", {"src/app.py": "x\n"})
    assert subject._resumable_code_change_escalation({"branch": "b"}, "T1") == (
        False, "Opus judge unavailable (CLI returned nothing)", "code_change"
    )


def test_resumable_escalation_judge_prose_is_a_parse_error(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, "looks fine")
    _branch(repo, "b", {"src/app.py": "x\n"})
    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, status) == (False, "code_change")
    assert reason.startswith("parse error:")


def test_resumable_escalation_judge_broken_json(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": tru,}')
    _branch(repo, "b", {"src/app.py": "x\n"})
    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, status) == (False, "code_change")
    assert reason.startswith("judge parse error:")


def test_resumable_escalation_judge_rejection(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": false, "reason": "touches the pipeline"}')
    _branch(repo, "b", {"src/app.py": "x\n"})
    assert subject._resumable_code_change_escalation({"branch": "b"}, "T1") == (
        False, "touches the pipeline", "code_change"
    )


def test_resumable_escalation_judge_rejection_without_a_reason(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": false}')
    assert _branch(repo, "b", {"src/app.py": "x\n"})
    assert subject._resumable_code_change_escalation({"branch": "b"}, "T1") == (
        False, "rejected by Opus", "code_change"
    )


def test_resumable_escalation_uses_the_default_judge_model(subject, repo, monkeypatch):
    """FOUND_BUGS: no `model=` is passed here, unlike the risky reviewer.

    The no-eval merge path therefore silently rides on whatever `_judge_complete`'s
    default model is, which is the expensive one.
    """
    _use_project(monkeypatch, subject, {})
    judge = _use_judge(monkeypatch, subject, '{"pass": false}')
    _branch(repo, "b", {"src/app.py": "x\n"})
    subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert "model" not in judge.calls[0]
    assert set(judge.calls[0]) == {"system", "user"}


def test_resumable_escalation_prompt_contract(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    judge = _use_judge(monkeypatch, subject, '{"pass": false}')
    _branch(repo, "b", {"src/app.py": "SENTINEL_LINE = 1\n"})
    subject._resumable_code_change_escalation({"branch": "b"}, "T55")
    call = judge.calls[0]
    assert "JSON" in call["system"]
    assert '"pass"' in call["system"]
    assert "T55" in call["user"]
    assert "src/app.py" in call["user"]
    assert "SENTINEL_LINE" in call["user"]


def test_resumable_escalation_approved_merges_and_announces(subject, repo, monkeypatch, sandbox):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": true, "reason": "benign logging tweak"}')
    sent: list[str] = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg, *a, **k: sent.append(msg))
    _branch(repo, "b", {"src/app.py": "x\n"})

    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, reason, status) == (True, "benign logging tweak", "ok")
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "x\n"
    assert _subjects(repo)[0] == "merge(resume): Opus-approved partial progress from b"
    assert len(sent) == 1 and "T1" in sent[0]
    detail = _detail(sandbox, "resume_code_change_approved")
    assert "T1" in detail and "src/app.py" in detail


def test_resumable_escalation_approved_but_conflicting(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": true, "reason": "ok"}')
    monkeypatch.setattr(subject, "notify_telegram", lambda *a, **k: None)
    _conflict_branch(repo, "b", "src/app.py")
    before = _head(repo)
    assert subject._resumable_code_change_escalation({"branch": "b"}, "T1") == (
        False, "merge conflict after Opus approval", "conflict"
    )
    assert not _merge_in_progress(repo)
    assert _head(repo) == before


def test_resumable_escalation_approved_but_dirty_worktree(subject, repo, monkeypatch):
    _use_project(monkeypatch, subject, {})
    _use_judge(monkeypatch, subject, '{"pass": true, "reason": "ok"}')
    monkeypatch.setattr(subject, "notify_telegram", lambda *a, **k: None)
    _commit_on_main(repo, {"src/app.py": "committed\n"})
    _branch(repo, "b", {"src/app.py": "branch\n"})
    _write(repo, "src/app.py", "hand edited\n")
    ok, reason, status = subject._resumable_code_change_escalation({"branch": "b"}, "T1")
    assert (ok, status) == (False, "conflict")
    assert reason.startswith("dirty worktree blocks merge:")
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "hand edited\n"


# =====================================================================================
# _merge_prep_branch
# =====================================================================================


def test_merge_prep_branch_no_commits_is_success(subject, repo):
    """A prep run with nothing to commit is legitimate, so the no-op is not refused."""
    _git(repo, "branch", "b", "main")
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "session_id": "S", "branch": "b"})
    assert ok is True
    assert detail == "no prep commits on b (nothing to merge)"


def test_merge_prep_branch_falls_back_to_the_lowercased_session_id(subject, repo):
    _branch(repo, "impl-t1-1", {"scripts/prep.py": "x\n"})
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "session_id": "IMPL-T1-1"})
    assert ok is True
    assert "impl-t1-1" in detail


def test_merge_prep_branch_merges_and_reports_the_sha(subject, repo):
    _branch(repo, "b", {"scripts/prep.py": "x\n"})
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "session_id": "S", "branch": "b"})
    assert ok is True
    assert detail == f"merged b at {subject._git_head(short=True)}"
    assert _subjects(repo)[0] == "prep(T1): b"
    assert (repo / "scripts" / "prep.py").is_file()


def test_merge_prep_branch_dirty_worktree_refuses(subject, repo):
    _commit_on_main(repo, {"scripts/prep.py": "committed\n"})
    _branch(repo, "b", {"scripts/prep.py": "branch\n"})
    _write(repo, "scripts/prep.py", "hand edited\n")
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "session_id": "S", "branch": "b"})
    assert ok is False
    assert detail.startswith("dirty worktree blocks prep merge:")
    assert "scripts/prep.py" in detail


def test_merge_prep_branch_conflict_aborts(subject, repo, monkeypatch):
    _dep_map_stub(subject, repo, monkeypatch)
    _conflict_branch(repo, "b", "src/app.py")
    before = _head(repo)
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "session_id": "S", "branch": "b"})
    assert ok is False
    assert detail.startswith("git merge failed:")
    assert not _merge_in_progress(repo)
    assert _head(repo) == before


def test_merge_prep_branch_autoresolves_a_doc_conflict(subject, repo, monkeypatch):
    _seed_regenerables(subject, repo)
    _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    _conflict_branch(repo, "b", artifact)
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "session_id": "S", "branch": "b"})
    assert ok is True
    assert detail.startswith("merged b at ")
    assert not _merge_in_progress(repo)


def test_merge_prep_branch_runs_no_deny_list_or_risky_check(subject, repo, monkeypatch):
    """FOUND_BUGS: the prep path merges straight to main with none of merge_and_eval's gates.

    The very diff `deny_list_guard` refuses lands on main here, unreviewed. The guard is
    asserted *before* the merge: `deny_list_guard` diffs `main...b`, so once the branch is
    an ancestor of main the diff is empty and the guard reads clean — asking it afterwards
    would say "ok" about content it never saw.
    """
    judge = _use_judge(monkeypatch, subject, '{"pass": false, "reason": "no"}')
    _branch(repo, "b", {"db/migrate.sql": "DROP TABLE users;\n"})
    assert subject.deny_list_guard("b", "T1")[0] is False
    ok, _ = subject._merge_prep_branch({"task_id": "T1", "session_id": "S", "branch": "b"})
    assert ok is True
    assert judge.calls == []
    assert "db/migrate.sql" in _tracked(repo)
    assert (repo / "db" / "migrate.sql").read_text(encoding="utf-8") == "DROP TABLE users;\n"


def test_merge_prep_branch_needs_a_session_id_only_when_no_branch_is_given(subject, repo):
    """`entry.get("branch") or entry["session_id"].lower()` short-circuits.

    An explicit branch is enough — `session_id` is never read, so no KeyError. It becomes
    mandatory the moment `branch` is missing or empty, which is the fallback path.
    """
    _branch(repo, "b", {"scripts/prep.py": "x\n"})
    ok, detail = subject._merge_prep_branch({"task_id": "T1", "branch": "b"})
    assert ok is True
    assert detail.startswith("merged b at ")

    with pytest.raises(KeyError):
        subject._merge_prep_branch({"task_id": "T1", "branch": ""})
    with pytest.raises(KeyError):
        subject._merge_prep_branch({"task_id": "T1"})


# =====================================================================================
# _merge_self_fix_branch / _merge_redo_branch (same shape, different gate + message)
# =====================================================================================


_SELF_FIX = ("_merge_self_fix_branch", "_self_fix_path_ok", "selffix: ")
_REDO = ("_merge_redo_branch", "_redo_path_ok", "redo: ")


@pytest.fixture(params=[_SELF_FIX, _REDO], ids=["selffix", "redo"])
def gated_merge(request, subject, monkeypatch):
    """The two gate-then-merge helpers, with their path gate replaced by a controllable stub."""
    merge_name, gate_name, prefix = request.param
    verdict = {"value": (True, "ok")}
    seen: list[list[str]] = []

    def gate(files):
        seen.append(list(files))
        return verdict["value"]

    monkeypatch.setattr(subject, gate_name, gate)
    return SimpleNamespace(
        merge=getattr(subject, merge_name),
        prefix=prefix,
        verdict=verdict,
        seen=seen,
    )


def test_gated_merge_no_commits(subject, repo, gated_merge):
    _git(repo, "branch", "b", "main")
    assert gated_merge.merge("b") == (False, "no commits on b")
    assert gated_merge.seen == []


def test_gated_merge_empty_commit_counts_as_commits(subject, repo, gated_merge):
    """An empty commit satisfies the `git log main..b` check, so the gate still runs."""
    _branch(repo, "b")
    ok, _ = gated_merge.merge("b")
    assert ok is True
    assert gated_merge.seen == [[]]


def test_gated_merge_path_gate_refusal(subject, repo, gated_merge):
    gated_merge.verdict["value"] = (False, "denied path touched: x")
    _branch(repo, "b", {"docs/notes.md": "x\n"})
    assert gated_merge.merge("b") == (
        False, "merge-time path gate failed: denied path touched: x"
    )
    assert gated_merge.seen == [["docs/notes.md"]]


def test_gated_merge_success(subject, repo, gated_merge):
    _branch(repo, "b", {"docs/notes.md": "x\n"})
    ok, detail = gated_merge.merge("b")
    assert ok is True
    assert detail == f"merged b at {subject._git_head(short=True)}"
    assert _subjects(repo)[0] == gated_merge.prefix + "b"
    assert (repo / "docs" / "notes.md").is_file()


def test_gated_merge_dirty_worktree(subject, repo, gated_merge):
    _commit_on_main(repo, {"docs/notes.md": "committed\n"})
    _branch(repo, "b", {"docs/notes.md": "branch\n"})
    _write(repo, "docs/notes.md", "hand edited\n")
    ok, detail = gated_merge.merge("b")
    assert ok is False
    assert detail.startswith("dirty worktree blocks merge:")
    assert "docs/notes.md" in detail


def test_gated_merge_conflict_aborts(subject, repo, gated_merge):
    _conflict_branch(repo, "b", "docs/notes.md")
    before = _head(repo)
    ok, detail = gated_merge.merge("b")
    assert ok is False
    assert detail.startswith("git merge failed:")
    assert not _merge_in_progress(repo)
    assert _head(repo) == before


def test_gated_merge_does_not_autoresolve_doc_conflicts(subject, repo, gated_merge, monkeypatch):
    """FOUND_BUGS: neither gated merge calls the doc auto-resolver `_merge_prep_branch` uses."""
    _seed_regenerables(subject, repo)
    calls = _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    _conflict_branch(repo, "b", artifact)
    ok, detail = gated_merge.merge("b")
    assert ok is False
    assert detail.startswith("git merge failed:")
    assert calls == []


def test_gated_merge_receives_whitespace_split_paths(subject, repo, gated_merge):
    """FOUND_BUGS: `.split()` on `git diff --name-only`, so a path with a space arrives as two.

    The gate then sees two files that do not exist, and its allow/deny prefixes are applied
    to fragments.
    """
    _branch(repo, "b", {"docs/two words.md": "x\n"})
    gated_merge.merge("b")
    assert gated_merge.seen == [["docs/two", "words.md"]]


# =====================================================================================
# merge_and_eval
# =====================================================================================


@pytest.fixture
def merge_env(subject, repo, monkeypatch):
    """merge_and_eval's collaborators: a controllable smoke result and a recording _danreq."""
    smoke = {"pass": True, "metrics": {"recall_at_5": 0.9, "latency_p95_ms": 12}, "reason": ""}
    box = SimpleNamespace(smoke=smoke, smoke_calls=[], danreqs=[], side_effect=None)

    def fake_smoke(task_id, session_id=""):
        box.smoke_calls.append((task_id, session_id))
        if box.side_effect is not None:
            box.side_effect()
        return box.smoke

    monkeypatch.setattr(subject, "_smoke_for_task", fake_smoke)
    monkeypatch.setattr(
        subject, "_danreq",
        lambda question, options, *a, **k: box.danreqs.append((question, list(options))),
    )
    return box


def _entry(branch="b", task_id="T1", session_id="impl-T1-1"):
    return {"task_id": task_id, "session_id": session_id, "branch": branch}


def test_merge_and_eval_refuses_a_no_op_merge(subject, repo, sandbox, merge_env):
    _git(repo, "branch", "b", "main")
    ok, result = subject.merge_and_eval(_entry())
    assert ok is False
    assert set(result) == {"pass", "metrics", "reason", "noop"}
    assert result["pass"] is False
    assert result["metrics"] == {}
    assert "no-op merge" in result["reason"]
    assert _event_names(sandbox) == ["merge_noop_refused"]
    assert merge_env.smoke_calls == []


def test_merge_and_eval_flags_a_no_op_refusal_as_noop(subject, repo, sandbox, merge_env):
    """The caller has to tell "produced nothing" apart from "produced a regression".

    A regression escalates to Dan and leaves the task runnable; a no-op must park, because
    re-running it reproduces the same nothing. The orchestrator used to separate the two by
    not separating them at all — every failed merge went to `park_regression`, which
    registers neither `parked_tasks` nor `waiting_on_dan`, so a `kind: script` task whose
    `run:` produced an empty branch relaunched every poll forever. Matching on the reason
    string would re-couple them; this flag is the seam.
    """
    _git(repo, "branch", "b", "main")
    _ok, result = subject.merge_and_eval(_entry())
    assert result["noop"] is True


def test_merge_and_eval_branch_falls_back_to_the_lowercased_session_id(
    subject, repo, sandbox, merge_env
):
    _git(repo, "branch", "impl-t1-1", "main")
    ok, result = subject.merge_and_eval({"task_id": "T1", "session_id": "IMPL-T1-1"})
    assert ok is False
    assert "impl-t1-1" in result["reason"]


def test_merge_and_eval_deny_list_escalates_to_a_human(subject, repo, sandbox, merge_env):
    _branch(repo, "b", {"db/migrate.sql": "DROP TABLE users;\n"})
    ok, result = subject.merge_and_eval(_entry())
    assert ok is False
    assert result["pass"] is False and result["metrics"] == {}
    assert "deny-list violation" in result["reason"]
    assert _event_names(sandbox) == ["merge_denied_denylist"]
    assert len(merge_env.danreqs) == 1
    question, options = merge_env.danreqs[0]
    assert "T1" in question and "b" in question
    assert len(options) == 2
    assert _head(repo) == _git(repo, "rev-parse", "main").stdout.strip()
    assert merge_env.smoke_calls == []


def test_merge_and_eval_risky_reviewer_escalates_to_a_human(
    subject, repo, sandbox, merge_env, monkeypatch
):
    _use_project(monkeypatch, subject, {"risky_set": ["src/core.py"]})
    _use_judge(monkeypatch, subject, '{"pass": false, "reason": "unsafe change"}')
    _branch(repo, "b", {"src/core.py": "x\n"})
    ok, result = subject.merge_and_eval(_entry())
    assert ok is False
    assert result["reason"] == "risky reviewer: unsafe change"
    assert _event_names(sandbox) == ["merge_risky_escalated"]
    assert len(merge_env.danreqs) == 1
    assert merge_env.smoke_calls == []


def test_merge_and_eval_dirty_worktree_refuses_before_merging(
    subject, repo, sandbox, merge_env
):
    _commit_on_main(repo, {"src/app.py": "committed\n"})
    _branch(repo, "b", {"src/app.py": "branch\n"})
    _write(repo, "src/app.py", "hand edited\n")
    before = _head(repo)
    ok, result = subject.merge_and_eval(_entry())
    assert ok is False
    assert result["reason"].startswith("dirty worktree blocks merge:")
    assert _event_names(sandbox) == ["merge_worktree_dirty"]
    assert _head(repo) == before
    assert merge_env.smoke_calls == []


def test_merge_and_eval_happy_path(subject, repo, sandbox, merge_env):
    _branch(repo, "b", {"src/app.py": "x\n"})
    before = _head(repo)
    ok, result = subject.merge_and_eval(_entry())
    assert ok is True
    assert result is merge_env.smoke
    assert _head(repo) != before
    assert _subjects(repo)[0] == "merge(T1): b"
    assert (repo / "src" / "app.py").is_file()
    assert _event_names(sandbox) == ["merge_commit", "merge_accepted"]
    assert merge_env.smoke_calls == [("T1", "impl-T1-1")]
    detail = _detail(sandbox, "merge_commit")
    assert "T1" in detail and "branch=b" in detail and "new_commit=True" in detail


def test_merge_and_eval_journal_records_carry_the_session_id(subject, repo, sandbox, merge_env):
    _branch(repo, "b", {"src/app.py": "x\n"})
    subject.merge_and_eval(_entry())
    assert {e["session_id"] for e in _events(sandbox)} == {"impl-T1-1"}


def test_merge_and_eval_reverts_to_the_pre_merge_sha_when_the_smoke_fails(
    subject, repo, sandbox, merge_env
):
    _branch(repo, "b", {"src/app.py": "x\n"})
    before = _head(repo)
    merge_env.smoke = {"pass": False, "metrics": {"recall_at_5": 0.1}, "reason": "regression"}
    ok, result = subject.merge_and_eval(_entry())
    assert ok is False
    assert result is merge_env.smoke
    assert _head(repo) == before
    assert not (repo / "src" / "app.py").exists()
    assert _event_names(sandbox) == ["merge_commit", "merge_reverted"]
    assert "regression" in _detail(sandbox, "merge_reverted")


def test_merge_and_eval_revert_also_undoes_the_eval_history_commit(
    subject, repo, sandbox, merge_env
):
    """The eval row is committed before the accept/revert split, so a revert loses it again.

    That is the documented intent (`sha_pre` always undoes it) — pinned here because it is
    the reason `_commit_eval_history` is called where it is.
    """
    _branch(repo, "b", {"src/app.py": "x\n"})
    before = _head(repo)
    merge_env.smoke = {"pass": False, "metrics": {}, "reason": "regression"}
    merge_env.side_effect = lambda: _write(
        repo, subject._EVAL_HISTORY, '{"row": 0}\n{"row": 1}\n'
    )
    subject.merge_and_eval(_entry())
    assert _head(repo) == before
    assert (repo / subject._EVAL_HISTORY).read_text(encoding="utf-8") == '{"row": 0}\n'
    assert "eval_history_committed" in _event_names(sandbox)


def test_merge_and_eval_keeps_the_eval_history_commit_when_the_smoke_passes(
    subject, repo, sandbox, merge_env
):
    _branch(repo, "b", {"src/app.py": "x\n"})
    merge_env.side_effect = lambda: _write(
        repo, subject._EVAL_HISTORY, '{"row": 0}\n{"row": 1}\n'
    )
    ok, _ = subject.merge_and_eval(_entry())
    assert ok is True
    assert _dirty(repo) == []
    assert (repo / subject._EVAL_HISTORY).read_text(encoding="utf-8") == '{"row": 0}\n{"row": 1}\n'


def test_merge_and_eval_conflict_aborts_and_reports(subject, repo, sandbox, merge_env, monkeypatch):
    _dep_map_stub(subject, repo, monkeypatch)
    _conflict_branch(repo, "b", "src/app.py")
    before = _head(repo)
    ok, result = subject.merge_and_eval(_entry())
    assert ok is False
    assert result["reason"].startswith("git merge failed:")
    assert result["metrics"] == {}
    assert _event_names(sandbox) == ["merge_failed"]
    assert not _merge_in_progress(repo)
    assert _head(repo) == before
    assert merge_env.smoke_calls == []


def test_merge_and_eval_autoresolves_a_pure_doc_conflict(
    subject, repo, sandbox, merge_env, monkeypatch
):
    _seed_regenerables(subject, repo)
    _dep_map_stub(subject, repo, monkeypatch)
    artifact = subject._MERGE_DISCARDABLE[0]
    _conflict_branch(repo, "b", artifact)
    ok, _ = subject.merge_and_eval(_entry())
    assert ok is True
    assert not _merge_in_progress(repo)
    assert "merge_docs_autoresolved" in _event_names(sandbox)
    assert "merge_accepted" in _event_names(sandbox)


def test_merge_and_eval_requires_task_id_and_session_id(subject, repo, merge_env):
    """FOUND_BUGS: both keys are read with `[]`, so a malformed entry raises out of the merge."""
    with pytest.raises(KeyError):
        subject.merge_and_eval({"session_id": "S", "branch": "b"})
    _git(repo, "branch", "b", "main")
    with pytest.raises(KeyError):
        subject.merge_and_eval({"task_id": "T1", "branch": "b"})
