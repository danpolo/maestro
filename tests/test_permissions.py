"""P12 pilot finding D30: Maestro's Claude agents run on allow/deny lists, not a classifier.

Units for `maestro.permissions`: the global allow list and its seed, the fixed never-allow
list, the family matcher, a proposal's screening, and the Claude CLI flags. The lanes that
put a proposal to Dan are in `tests/graph_engineering/test_permission_ask.py`.
"""
from __future__ import annotations

import json
import shlex
import subprocess

from tests.hang_bound import HANG_SECONDS

import pytest

from maestro import permissions


# ── the family matcher ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rule, command, expected", [
    ("Bash(git status *)", "git status", True),
    ("Bash(git status *)", "git status --short", True),
    ("Bash(git status *)", "git statusx", False),
    ("Bash(git *)", "git push origin main", True),
    ("Bash(git:*)", "git log", True),
    ("Bash(*/.venv/bin/python *)", "/home/dan/p/.venv/bin/python -m pytest -q", True),
    ("Bash(*/.venv/bin/python *)", ".venv/bin/python -m pytest", False),
    ("Bash(python3 *)", "timeout 60 python3 -m pytest", True),
    ("Bash(python3 *)", "PYTHONPATH=. python3 -c 1", True),
    ("Bash(echo *)", "echo hi && touch x", False),
    ("Bash(echo *)", "echo hi", True),
    ("Bash", "anything at all", True),
])
def test_a_bash_family_matches_what_claude_code_matches(rule, command, expected):
    assert permissions.matches(rule, "Bash", command) is expected


def test_a_compound_command_is_covered_part_by_part():
    assert permissions.covered_by(["Bash(echo *)", "Bash(ls *)"], "Bash", "echo a && ls | echo b")
    assert not permissions.covered_by(["Bash(echo *)"], "Bash", "echo a; touch b")


@pytest.mark.parametrize("rule, path, expected", [
    ("Read(**/.env)", "/w/app/.env", True),
    ("Read(**/.env)", "/w/app/.envrc", False),
    ("Read(**/.env.*)", "/w/app/.env.local", True),
    ("Edit(//w/tree/**)", "/w/tree/src/a.py", True),
    ("Edit(//w/tree/**)", "/w/other/a.py", False),
    ("Write(~/.maestro/**)", "~/.maestro/permissions.json", True),
    ("Read", "/anything", True),
])
def test_a_path_family_matches_by_glob(rule, path, expected):
    tool = rule.split("(")[0]
    assert permissions.matches(rule, tool, path) is expected


def test_a_rule_names_one_tool():
    assert not permissions.matches("Bash(git *)", "Read", "git log")
    assert permissions.parse_rule("Bash(git *)") == ("Bash", "git *")
    assert permissions.parse_rule("Read") == ("Read", "")
    assert permissions.parse_rule("Bash(unclosed") is None
    assert permissions.parse_rule("") is None


# ── the never-allow list and over-broad families ─────────────────────────────────────


@pytest.mark.parametrize("command", [
    "sudo systemctl restart x",
    "git push --force origin main",
    "git push origin main --force-with-lease",
    "git push -f origin main",
    "cat .env",
    "grep TOKEN config/.env.local",
    "rm -rf /",
    "rm -rf ~/projects",
    "rm -rf ../other",
    "printenv",
    "env",
    "echo $TELEGRAM_BOT_TOKEN",
])
def test_the_never_allow_list_denies_the_dangerous_commands(command):
    assert permissions.never_allowed("Bash", command), command


@pytest.mark.parametrize("command", [
    "git push origin impl/x",
    "rm -rf build",
    "grep -n env maestro/config.py",
    "python3 -m pytest -q",
    "env FOO=1 python3 -c 1",
])
def test_the_never_allow_list_leaves_routine_commands_alone(command):
    assert not permissions.never_allowed("Bash", command), command


def test_the_never_allow_list_stays_consistent_with_the_merge_guard():
    """Every command form `merge._HARD_DENY_PATTERNS` names is also refused as a command."""
    for command in ('sudo apt install x', 'git push --force', 'git push -f',
                    'sqlite3 db "DROP TABLE t"', 'sqlite3 db "DELETE FROM t"',
                    'git update-index --force-reset-index', 'TELEGRAM_BOT_TOKEN=x run'):
        assert permissions.never_allowed("Bash", command), command


def test_secret_files_are_never_read_or_written():
    for tool in ("Read", "Edit", "Write"):
        assert permissions.never_allowed(tool, "/w/app/.env")
        assert permissions.never_allowed(tool, "/w/app/.env.production")
    assert permissions.never_allowed("Read", "/home/u/.claude/.credentials.json")
    assert permissions.never_allowed("Edit", "~/.maestro/permissions.json")
    assert not permissions.never_allowed("Read", "/w/app/src/env_loader.py")


@pytest.mark.parametrize("family", [
    "*", "Bash", "Bash(*)", "Bash( * )", "Bash(**)", "Edit", "Write", "Edit(**)",
    "Write(//**)", "Bash(bash *)", "Bash(sh -c *)", "Bash(eval *)", "Bash(xargs *)",
    "Bash(env *)",
])
def test_an_over_broad_family_is_refused(family):
    assert permissions.over_broad(family)
    assert "over-broad" in permissions.screen(family, "bash -c x")["refused"]


# ── screening a proposal ─────────────────────────────────────────────────────────────


def test_a_routine_proposal_is_put_to_dan():
    out = permissions.screen("Bash(git rebase *)", "git rebase main")
    assert out == {"refused": "", "tool": "Bash", "already_allowed": False}


def test_a_proposal_matching_the_never_allow_list_is_refused():
    assert "never-allow" in permissions.screen("Bash(sudo apt *)", "sudo apt install x")["refused"]
    # The family itself is fine but the denied command is a never-allow one.
    assert "never-allow" in permissions.screen("Bash(cat *)", "cat .env")["refused"]
    assert "never-allow" in permissions.screen("Read(**/.env)", "Read(/w/.env)")["refused"]


def test_a_proposal_must_cover_the_denied_command():
    assert "does not cover" in permissions.screen("Bash(git rebase *)", "touch x")["refused"]


@pytest.mark.parametrize("family,command", [
    ("Bash(* --version)", "claude --version"),
    ("Bash(*/.venv/bin/python *)", "/project/.venv/bin/python -m pytest"),
    ("Bash( * --version)", "docker --version"),
])
def test_a_leading_wildcard_proposal_never_reaches_dan(permissions_file, family, command):
    assert "leading wildcard" in permissions.screen(family, command)["refused"]
    with pytest.raises(ValueError):
        permissions.add(family, by="dan")


def test_named_version_family_covers_only_the_requested_program(permissions_file):
    assert not permissions.screen("Bash(claude --version)", "claude --version")["refused"]
    assert "does not cover" in permissions.screen("Bash(docker info*)", "docker --version")["refused"]


@pytest.mark.parametrize("harness", ["codex", "antigravity"])
def test_harness_lists_account_for_every_global_rule_and_bound_writes(permissions_file, tmp_path, harness):
    permissions.add("Bash(git rebase *)", by="dan")
    policy = permissions.harness_policy(harness, cwd=tmp_path, writable=True)
    assert policy["source_allow"] == permissions.allow_rules()
    assert policy["source_deny"] == list(permissions.NEVER_ALLOW)
    assert policy["write_roots"] == [str(tmp_path.resolve())]
    assert policy["activation_ready"] is False
    assert "Bash(git rebase *)" in policy["translated_allow"]
    assert set(policy["translated_deny"]) | set(policy["untranslated_deny"]) == set(permissions.NEVER_ALLOW)
    readonly = permissions.harness_policy(harness, cwd=tmp_path, writable=False)
    assert readonly["write_roots"] == []


def test_codex_rules_check_allows_routine_git_and_forbids_privilege(permissions_file, tmp_path):
    policy = permissions.harness_policy("codex", cwd=tmp_path, writable=False)
    # Parse the Python-compatible Starlark subset without starting a model CLI. The
    # native `codex execpolicy check` smoke is a separate qualification command.
    rules = []
    exec(permissions.codex_rules(policy), {"__builtins__": {}, "prefix_rule": lambda **rule: rules.append(rule)})
    for command, expected in [("git status --short", "allow"), ("sudo whoami", "forbidden")]:
        argv = shlex.split(command)
        matched = [r["decision"] for r in rules if argv[:len(r["pattern"])] == r["pattern"]]
        assert matched == [expected]


def test_allowed_interpreter_can_read_a_synthetic_secret_without_a_literal_secret_path(permissions_file, tmp_path):
    # Characterizes the declared limit, not a claim of sandbox enforcement.
    (tmp_path / ".env").write_text("synthetic-fixture")
    code = "print(__import__('pathlib').Path('.en' + 'v').read_text())"
    command = "python3 -c " + shlex.quote(code)
    assert permissions.never_allowed("Read", str(tmp_path / ".env"))
    assert not permissions.never_allowed("Bash", command)
    assert permissions.covered_by(permissions.allow_rules(), "Bash", command)
    proc = subprocess.run(shlex.split(command), cwd=tmp_path, capture_output=True, text=True,
                          timeout=HANG_SECONDS)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "synthetic-fixture"


def test_a_malformed_proposal_is_refused():
    assert "not a permission rule" in permissions.screen("git rebase", "git rebase")["refused"]


def test_an_already_allowed_family_is_not_asked(permissions_file):
    out = permissions.screen("Bash(git status *)", "git status")
    assert out["already_allowed"] and not out["refused"]


# ── the global list ──────────────────────────────────────────────────────────────────


@pytest.fixture
def permissions_file(tmp_path, monkeypatch):
    path = tmp_path / "maestro-home" / "permissions.json"
    monkeypatch.setenv(permissions.ENV_VAR, str(path))
    return path


def test_the_list_is_seeded_on_first_read(permissions_file):
    assert not permissions_file.exists()
    rules = permissions.allow_rules()
    assert rules == list(permissions.SEED_ALLOW)
    saved = json.loads(permissions_file.read_text())
    assert saved["allow"] == list(permissions.SEED_ALLOW)
    assert saved["seed_source"]


def test_the_seed_holds_the_routine_families_and_nothing_never_allowed():
    seed = permissions.SEED_ALLOW
    for rule in ("Bash(git status *)", "Bash(git add *)", "Bash(git commit *)",
                 "Bash(git diff *)", "Bash(git log *)", "Bash(pytest *)", "Read"):
        assert rule in seed
    for rule in seed:
        tool, spec = permissions.parse_rule(rule)
        assert not permissions.over_broad(rule), rule
        assert not permissions.never_allowed(tool, spec.replace("*", "")), rule


def test_allow_adds_a_family_with_its_history(permissions_file):
    permissions.add("Bash(git rebase *)", by="dan", request_id="pr-1", reason="rebase onto main",
                    at="2026-09-28T12:00:00Z")
    permissions.add("Bash(git rebase *)", by="dan", request_id="pr-2", reason="again",
                    at="2026-09-28T12:01:00Z")
    rules = permissions.allow_rules()
    assert rules.count("Bash(git rebase *)") == 1
    history = json.loads(permissions_file.read_text())["history"]
    assert [h["request_id"] for h in history] == ["pr-1"]
    assert history[0]["by"] == "dan"


def test_a_hand_edited_never_allow_rule_is_ignored(permissions_file):
    permissions.allow_rules()
    doc = json.loads(permissions_file.read_text())
    doc["allow"] += ["Bash(sudo *)", "Bash(*)", "Read(**/.env)"]
    permissions_file.write_text(json.dumps(doc))
    rules = permissions.allow_rules()
    assert "Bash(sudo *)" not in rules and "Bash(*)" not in rules
    assert "Read(**/.env)" not in rules


def test_an_unreadable_list_falls_back_to_the_seed(permissions_file):
    permissions_file.parent.mkdir(parents=True)
    permissions_file.write_text("{not json")
    assert permissions.allow_rules() == list(permissions.SEED_ALLOW)
    assert permissions_file.read_text() == "{not json"      # never overwritten


# ── the Claude CLI flags ─────────────────────────────────────────────────────────────


def test_cli_args_are_dont_ask_with_both_lists(permissions_file, tmp_path):
    tree, workspace = tmp_path / "tree", tmp_path / "ws"
    args = permissions.cli_args(cwd=tree, add_dirs=(workspace,), writable=True)
    assert args[-2:] == ["--permission-mode", "dontAsk"]
    allowed = args[args.index("--allowedTools") + 1: args.index("--disallowedTools")]
    denied = args[args.index("--disallowedTools") + 1: args.index("--permission-mode")]
    assert set(permissions.SEED_ALLOW) <= set(allowed)
    for root in (tree, workspace):
        assert f"Edit(/{root.resolve()}/**)" in allowed
    assert not any(rule.startswith("Write(") for rule in (*allowed, *denied))
    assert denied == list(permissions.NEVER_ALLOW)


def test_a_write_family_is_refused_because_claude_ignores_it(permissions_file):
    family = "Write(//w/tree/**)"
    assert "Edit" in permissions.screen(family, "Write(/w/tree/a.py)")["refused"]
    with pytest.raises(ValueError):
        permissions.add(family, by="dan")


def test_an_edit_family_can_cover_a_denied_file_write(permissions_file):
    result = permissions.screen("Edit(//w/tree/**)", "Write(/w/tree/a.py)")
    assert result["refused"] == ""


def test_a_read_only_call_gets_no_write_rules(permissions_file, tmp_path):
    args = permissions.cli_args(cwd=tmp_path, add_dirs=(), writable=False)
    assert not [a for a in args if a.startswith(("Edit(", "Write("))
                and a not in permissions.NEVER_ALLOW]


def test_cli_args_never_include_the_classifier_mode(permissions_file, tmp_path):
    args = permissions.cli_args(cwd=tmp_path, add_dirs=(), writable=True)
    assert "auto" not in args and "--dangerously-skip-permissions" not in args
