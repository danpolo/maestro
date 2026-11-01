"""The shared enforcement matcher, with no model or harness process involved."""
from pathlib import Path

import pytest

from maestro import tool_policy


@pytest.fixture
def policy(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return tool_policy.ToolPolicy(
        allow=("Read", "Glob", "Grep", "Bash(git status *)", "Bash(grep *)",
               "Bash(cat *)", "Bash(echo *)", "Bash(docker --version)",
               "Bash(python3 *)"),
        write_roots=(tree,),
    ), tree, outside


def test_compound_requires_each_command_without_splitting_quoted_data(policy):
    guard, tree, _ = policy
    assert guard.command('git status --short && grep "a|b; c" file.txt', cwd=tree).allowed
    assert not guard.command('git status --short && unapproved argument', cwd=tree).allowed


@pytest.mark.parametrize("command", [
    "docker --version extra", "cat .env", "cat '.en'v", "cat sub/.env.local",
    "echo $SERVICE_TOKEN", "echo ok; sudo whoami", "cat ~/.ssh/config",
    "echo $(cat secret)", "echo `cat secret`", "echo <(cat secret)",
    "echo ${PATH}", "echo ok &", "echo ok\nunknown", "(echo ok)",
    "FOO=bar echo ok", "echo ok > ../outside/result", "echo ok > .env",
    "echo ok >&2", "echo ok <<EOF\nsecret\nEOF", "echo ok # ignored ; unknown",
])
def test_denies_unlisted_exact_extra_secret_or_unsupported_shell(policy, command):
    guard, tree, _ = policy
    result = guard.command(command, cwd=tree)
    assert not result.allowed
    assert result.reason


def test_redirection_is_path_scoped_and_resolves_symlinks(policy):
    guard, tree, outside = policy
    assert guard.command('echo "a > b" > result.txt', cwd=tree).allowed
    (tree / "escape").symlink_to(outside, target_is_directory=True)
    assert not guard.command("echo ok > escape/result.txt", cwd=tree).allowed
    assert not guard.command("echo ok > /dev/null", cwd=tree).allowed


def test_file_rules_and_writes_use_resolved_paths(policy):
    guard, tree, outside = policy
    assert guard.file("Read", tree / "source.txt").allowed
    assert guard.file("Edit", tree / "source.txt").allowed
    assert not guard.file("Edit", outside / "source.txt").allowed
    assert not guard.file("Read", tree / ".env").allowed
    (tree / "alias").symlink_to(tree / ".env")
    assert not guard.file("Read", tree / "alias").allowed


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_file_inspection_cannot_bypass_a_literal_read_deny(policy, tool):
    guard, tree, _ = policy
    assert not guard.file(tool, tree / ".env").allowed


def test_absolute_privileged_executable_is_denied_even_by_a_broad_path_family(policy):
    _, tree, _ = policy
    guard = tool_policy.ToolPolicy(allow=("Bash(/usr/bin/*)",), write_roots=(tree,))
    assert not guard.command("/usr/bin/sudo whoami", cwd=tree).allowed


@pytest.mark.parametrize("separator", ["\u00a0", "\u2003", "\r", "\v", "\f"])
def test_non_shell_whitespace_does_not_turn_another_executable_into_an_allowed_one(policy, separator):
    guard, tree, _ = policy
    assert not guard.command("python3" + separator + "unlisted", cwd=tree).allowed


@pytest.mark.parametrize("command", ['"python3 unlisted"', '"python3 "', '"python3\u00a0"'])
def test_quoted_executable_name_is_not_confused_with_separate_arguments(policy, command):
    guard, tree, _ = policy
    assert not guard.command(command, cwd=tree).allowed


def test_allowed_interpreter_still_has_documented_constructed_path_limit(policy):
    guard, tree, _ = policy
    assert guard.command("python3 -c \"print(__import__('pathlib').Path('.en' + 'v').read_text())\"",
                         cwd=tree).allowed
