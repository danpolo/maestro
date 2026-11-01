"""P12 pilot finding D30: Maestro's Claude agents run on allow/deny lists, not a classifier.

DuetFlow 06 run 5 and the final run of 06 run 7 died in Claude Code's auto-mode classifier
outages (D4): every Bash call "gave no verdict" and the agents could do nothing. Auto mode
came from Dan's global `~/.claude/settings.json` (`defaultMode: auto`), not from Maestro.
Dan's decision (2026-09-28):

- Maestro's Claude agents run with `--permission-mode dontAsk`, which denies anything not
  allowed and never calls a classifier, and explicit lists (`--allowedTools`,
  `--disallowedTools`). Dan rejected `--dangerously-skip-permissions`: nothing would guard
  the commands agents run.
- ONE global allow list for all projects, `~/.maestro/permissions.json`, seeded with the
  families agents already use (`SEED_ALLOW`, derived from real transcripts).
- A fixed never-allow list (`NEVER_ALLOW`), passed as `--disallowedTools` on every call and
  never offered to Dan: sudo, force pushes, secrets, `rm -rf` outside the tree, and the
  command forms `merge._HARD_DENY_PATTERNS` guards in code.
- A denied call the agent thinks should be allowed becomes a proposal of the broadest
  family covering it; Dan allows or declines it on Telegram
  (`maestro.workflows.permission_asks`). An allowed family is added here, for every
  project's agents.

`harness_policy` builds Codex and Antigravity lists from this same global source.
Native prefix formats cannot express every deny rule; residual rules are explicit and
these exports are not activated on a launch path. W4 owns agy's confined enforcement.

The matcher here approximates Claude Code's own rule matching closely enough to screen a
proposal (does it cover the denied call, is it over-broad, does it touch the never-allow
list). Enforcement is Claude Code's, from the flags `cli_args` builds. The list guards the
commands agents run; it is not a sandbox: an allowed interpreter (`python3 *`) can still do
anything a program can, as it always could.

Claude Code itself (2.1.283, probed 2026-09-28) denies a compound that changes directory
and then touches paths (`cd sub && ls`, `cd <tree> && git status`) whatever the lists say,
even with an exact rule for it; a bare `cd` needs no rule and persists between calls. The
agents' brief says so (`permission_asks.BRIEF_RULE`).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

#: Overrides the list's location (tests; a second Maestro home).
ENV_VAR = "MAESTRO_PERMISSIONS_FILE"
FILE_NAME = "permissions.json"
VERSION = 1

#: Where the seed came from, recorded in the file so a reader knows what it is.
SEED_SOURCE = (
    "D30 seed, 2026-09-28: the command families in 36 real Claude agent transcripts of "
    "DuetFlow graph runs 1-16 (505 Bash, 178 Edit, 176 Read, 31 Write calls; "
    ".orchestrator/archive/*/attempts/*/session_uuid.txt -> ~/.claude/projects/*/<uuid>.jsonl). "
    "Left out on purpose: xargs, rm, env, git checkout, send-to-me (each seen at most 4 times; "
    "an agent that needs one asks Dan)."
)

#: The routine families agents already use. File edits are not here: every call gets
#: path-scoped Edit rules for its own tree and workspace (`cli_args`).
SEED_ALLOW: tuple[str, ...] = (
    # Read-only tools.
    "Read", "Glob", "Grep",
    # Inspection (grep 262, head 188, sed 169, tail 148, cat 134, ls 56, find 29, wc 19,
    # sort 5, which 6, pwd 1). `sed -i` edits, as the Edit tool would.
    "Bash(grep *)", "Bash(head *)", "Bash(sed *)", "Bash(tail *)", "Bash(cat *)",
    "Bash(ls *)", "Bash(find *)", "Bash(wc *)", "Bash(sort *)", "Bash(which *)",
    "Bash(pwd *)",
    # Writing a sentinel or report (`echo ... > WORKSPACE/DONE`, 89; the redirect is
    # checked against the Write rules) and copying a file aside (cp 4).
    "Bash(echo *)", "Bash(cp *)",
    # Python and the test suite (a project venv by absolute path 69, python3 116,
    # python 26, pytest 8, pip list 2).
    "Bash(*/.venv/bin/python *)", "Bash(.venv/bin/python *)", "Bash(python3 *)",
    "Bash(python *)", "Bash(pytest *)", "Bash(pip list *)", "Bash(*/.venv/bin/pip list *)",
    # Git inside the task tree (status 41, log 40, commit 31, add 30, diff 24, stash 7,
    # show 5, blame 1, ls-files 1). Never push: landing is the controller's.
    "Bash(git status *)", "Bash(git log *)", "Bash(git commit *)", "Bash(git add *)",
    "Bash(git diff *)", "Bash(git stash *)", "Bash(git show *)", "Bash(git blame *)",
    "Bash(git ls-files *)",
    # Seen in the P12 pilots (Dan 2026-10-09): read-only service state, a version probe
    # of one named tool (never `* --version`, which is any command), the exec bit on a
    # script and a tracked rename.
    "Bash(systemctl is-active *)", "Bash(systemctl is-enabled *)",
    "Bash(docker --version)", "Bash(git --version)", "Bash(node --version)",
    "Bash(npm --version)", "Bash(tmux -V)", "Bash(sqlite3 --version)",
    "Bash(chmod +x *)", "Bash(git mv *)",
)

#: Refused outright, on every call and for every proposal; never sent to Dan. Keep in
#: step with `merge._HARD_DENY_PATTERNS` (the same guard, on the code an agent writes).
NEVER_ALLOW: tuple[str, ...] = (
    # Privilege.
    "Bash(sudo *)", "Bash(su *)", "Bash(doas *)", "Bash(pkexec *)",
    # Force pushes.
    "Bash(git push --force*)", "Bash(git push * --force*)",
    "Bash(git push -f*)", "Bash(git push * -f*)",
    # Secrets: .env files, tokens, credentials, the environment.
    "Bash(*.env)", "Bash(*.env *)", "Bash(*.env.*)",
    "Bash(*_TOKEN*)", "Bash(*credentials*)", "Bash(printenv*)", "Bash(env)",
    "Read(**/.env)", "Read(**/.env.*)", "Read(**/.credentials*)", "Read(~/.ssh/**)",
    "Edit(**/.env)", "Edit(**/.env.*)",
    # The lists themselves: an agent never grants itself a permission.
    "Edit(~/.maestro/**)",
    "Edit(~/.claude/settings*.json)",
    # Recursive deletes outside the tree (absolute, home, parent).
    "Bash(rm -rf /*)", "Bash(rm -rf ~*)", "Bash(rm -rf ..*)", "Bash(rm -rf $*)",
    "Bash(rm -fr /*)", "Bash(rm -fr ~*)", "Bash(rm -fr ..*)", "Bash(rm -fr $*)",
    "Bash(rm -r /*)", "Bash(rm -r ~*)", "Bash(rm -r ..*)",
    # merge._HARD_DENY_PATTERNS, as commands.
    "Bash(*DROP TABLE*)", "Bash(*DELETE FROM*)", "Bash(*--force-reset-index*)",
)

#: A family is over-broad when it would let an agent run anything: no command at all, or
#: a program that runs another command given as its argument. (Privilege escalation is on
#: the never-allow list, which says so more precisely.)
_ANY_COMMAND = frozenset({"bash", "sh", "zsh", "dash", "fish", "eval", "exec", "env",
                          "xargs", "nohup", "command", "builtin", "source", "."})
_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep"})
_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z_][\w-]*)(?:\((?P<spec>.*)\))?$", re.DOTALL)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=\S*\s+")
_WRAPPER_RE = re.compile(r"^(?:timeout\s+(?:-\S+\s+)*\d+[smhd]?|time|nice(?:\s+-n\s*-?\d+)?)\s+")


# ── rules ────────────────────────────────────────────────────────────────────────────


def parse_rule(rule: str) -> Optional[tuple[str, str]]:
    """`(tool, specifier)` for a Claude Code permission rule, or `None` if it is not one."""
    match = _RULE_RE.match(str(rule or "").strip())
    if match is None:
        return None
    return match.group("tool"), (match.group("spec") or "")


def split_command(command: str, *, strip: bool = True) -> list[str]:
    """The simple commands of a shell line, each stripped of `VAR=` and `timeout N`.

    Claude Code checks each part of `a && b | c` against the rules on its own; so does
    Maestro when it screens a proposal. Quotes are not parsed: this screens, it does not
    enforce. `strip=False` keeps the `VAR=` prefixes (a secret can sit in one).
    """
    parts = []
    for part in re.split(r"&&|\|\||;|\||\n", str(command or "")):
        part = part.strip()
        while strip:
            stripped = _WRAPPER_RE.sub("", _ENV_ASSIGN_RE.sub("", part)).strip()
            if stripped == part:
                break
            part = stripped
        if part:
            parts.append(part)
    return parts


def _bash_matches(spec: str, command: str) -> bool:
    if spec.endswith(":*"):                               # legacy prefix syntax
        spec = spec[:-2] + " *"
    pattern = re.escape(spec).replace(r"\*", ".*")
    if re.fullmatch(pattern, command, re.DOTALL):
        return True
    # `git status *` also matches the bare `git status`.
    return spec.endswith(" *") and command == spec[:-2]


def _normal_path(path: str) -> str:
    text = str(path or "").strip()
    home = str(Path.home())
    if text.startswith("~/"):
        text = home + text[1:]
    return text


def _path_pattern(spec: str) -> str:
    """A path rule's specifier as a regex over absolute paths."""
    spec = _normal_path(spec)
    if spec.startswith("//"):
        spec = spec[1:]
    anchored = spec.startswith("/")
    out = []
    i = 0
    while i < len(spec):
        if spec.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif spec.startswith("**", i):
            out.append(r".*")
            i += 2
        elif spec[i] == "*":
            out.append(r"[^/]*")
            i += 1
        else:
            out.append(re.escape(spec[i]))
            i += 1
    body = "".join(out)
    return body if anchored else r"(?:.*/)?" + body


def _matches_one(rule: str, tool: str, subject: str) -> bool:
    parsed = parse_rule(rule)
    if parsed is None or parsed[0] != tool:
        return False
    spec = parsed[1]
    if not spec:
        return True
    if tool == "Bash":
        return _bash_matches(spec, str(subject).strip())
    return re.fullmatch(_path_pattern(spec), _normal_path(subject), re.DOTALL) is not None


def _subjects(tool: str, subject: str) -> list[str]:
    return split_command(subject) if tool == "Bash" else [str(subject)]


def matches(rule: str, tool: str, subject: str) -> bool:
    """Whether `rule` alone covers a `tool` call on `subject` (a command line, or a path).

    A command line is covered only when every one of its simple commands is.
    """
    subjects = _subjects(tool, subject)
    return bool(subjects) and all(_matches_one(rule, tool, s) for s in subjects)


def covered_by(rules: Iterable[str], tool: str, subject: str) -> bool:
    """Whether every part of the call is covered by some rule (Claude Code's own test)."""
    rules = list(rules)
    subjects = _subjects(tool, subject)
    return bool(subjects) and all(any(_matches_one(r, tool, s) for r in rules) for s in subjects)


def never_allowed(tool: str, subject: str) -> bool:
    """Whether any part of the call hits the never-allow list."""
    if tool == "Write":
        tool = "Edit"  # Claude Code applies Edit(path) rules to all file-editing tools.
    subjects = _subjects(tool, subject)
    if tool == "Bash":
        subjects += split_command(subject, strip=False)
    return any(_matches_one(rule, tool, s) for rule in NEVER_ALLOW for s in subjects)


def over_broad(rule: str) -> bool:
    """A family that would let an agent run any command or write anywhere."""
    text = str(rule or "").strip()
    if text in ("", "*"):
        return True
    parsed = parse_rule(text)
    if parsed is None:
        return False
    tool, spec = parsed
    bare = spec.replace("*", "").replace(":", "").strip()
    if tool == "Bash":
        if not bare:
            return True
        first = bare.split()[0]
        return first in _ANY_COMMAND
    if tool in _WRITE_TOOLS:
        return not bare.strip("/~.")
    return False


def _subject_of(denied: str, tool: str) -> str:
    """The denied call's subject: `Write(/p)` -> `/p`, a bare command stays itself."""
    parsed = parse_rule(denied)
    if parsed is not None and (parsed[0] == tool or (tool == "Edit" and parsed[0] == "Write")) and "(" in str(denied):
        return parsed[1]
    return str(denied or "").strip()


def screen(family: str, denied: str) -> dict:
    """What to do with an agent's proposal: `{refused, tool, already_allowed}`.

    `refused` is the reason it is refused without asking Dan (empty when it may be put to
    him): not a rule, over-broad, on the never-allow list, or not covering the call that
    was denied. `already_allowed` says the list already has it (Dan allowed it since the
    agent was launched), so the agent just runs again.
    """
    family = str(family or "").strip()
    out = {"refused": "", "tool": "", "already_allowed": False}
    if over_broad(family):
        out["refused"] = f"over-broad family {family!r}: it would allow any command"
        return out
    parsed = parse_rule(family)
    if parsed is None:
        out["refused"] = f"{family!r} is not a permission rule (expected e.g. Bash(git rebase *))"
        return out
    tool, spec = parsed
    out["tool"] = tool
    if tool == "Bash" and spec.lstrip().startswith("*"):
        out["refused"] = "leading wildcard family: name the executable and its operation"
        return out
    if tool == "Write":
        out["refused"] = "Claude Code ignores Write rules; propose an Edit(path) family instead"
        return out
    subject = _subject_of(denied, tool)
    sample = spec.replace(":*", " ").replace("*", "").strip()
    if never_allowed(tool, subject) or (sample and never_allowed(tool, sample)):
        out["refused"] = f"{family!r} is on the never-allow list"
        return out
    if not covered_by([family], tool, subject):
        out["refused"] = f"{family!r} does not cover the denied call {subject[:200]!r}"
        return out
    out["already_allowed"] = family in allow_rules()
    return out


# ── the global list ──────────────────────────────────────────────────────────────────


def path() -> Path:
    raw = os.environ.get(ENV_VAR)
    if raw:
        return Path(raw).expanduser()
    from maestro import bootstrap

    return bootstrap.home() / FILE_NAME


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_document() -> dict:
    return {"version": VERSION, "seed_source": SEED_SOURCE, "allow": list(SEED_ALLOW),
            "history": []}


@contextmanager
def _locked(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.with_suffix(target.suffix + ".lock"), "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write(target: Path, document: dict) -> None:
    tmp = target.with_suffix(target.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def _read(target: Path) -> Optional[dict]:
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _usable(rules) -> list[str]:
    out = []
    for rule in rules if isinstance(rules, list) else ():
        parsed = parse_rule(str(rule))
        if parsed is None or parsed[0] == "Write" or over_broad(str(rule)):
            continue
        tool, spec = parsed
        sample = spec.replace(":*", " ").replace("*", "").strip()
        if sample and never_allowed(tool, sample):
            continue
        if str(rule) not in out:
            out.append(str(rule))
    return out


def allow_rules() -> list[str]:
    """The global allow list, seeded on first read.

    Never raises. An unreadable file is left alone (Dan may be editing it) and the seed is
    used; a hand-added over-broad or never-allow rule is ignored.
    """
    target = path()
    try:
        if not target.exists():
            with _locked(target):
                if not target.exists():
                    _write(target, _seed_document())
    except OSError:
        return list(SEED_ALLOW)
    document = _read(target)
    if document is None:
        return list(SEED_ALLOW)
    return _usable(document.get("allow"))


def add(rule: str, *, by: str, request_id: str = "", reason: str = "", at: str = "") -> bool:
    """Add a family Dan allowed; `False` if it was already there (history unchanged).

    Refuses (raises `ValueError`) a rule `screen` would refuse: the list never holds an
    over-broad or never-allow family, whoever asks.
    """
    parsed = parse_rule(rule)
    if (parsed is None or over_broad(rule) or not _usable([rule])
            or (parsed[0] == "Bash" and parsed[1].lstrip().startswith("*"))):
        raise ValueError(f"refusing to allow {rule!r}")
    allow_rules()                                         # seeds the file if missing
    target = path()
    with _locked(target):
        document = _read(target) or _seed_document()
        allow = list(document.get("allow") or [])
        if rule in allow:
            return False
        allow.append(rule)
        history = list(document.get("history") or [])
        history.append({"rule": rule, "by": by, "request_id": request_id,
                        "reason": str(reason)[:500], "at": at or _now()})
        _write(target, {**document, "allow": allow, "history": history})
    return True


# ── the Claude CLI flags ─────────────────────────────────────────────────────────────


def _write_rules(root: Path) -> list[str]:
    # Claude Code: `/abs` in a rule is relative to the settings file; `//abs` is absolute.
    absolute = Path(root).resolve()
    return [f"Edit(/{absolute}/**)"]


def cli_args(*, cwd: Optional[Path], add_dirs: Sequence[Path] = (), writable: bool) -> list[str]:
    """The permission flags for one `claude -p` call.

    `--allowedTools` and `--disallowedTools` take a variadic list, so `--permission-mode
    dontAsk` comes last and closes it: whatever follows (the model, the prompt) is never
    read as a rule. A `writable` call may edit and write inside `cwd` and its extra
    directories (the workspace it reports into), and nowhere else.
    """
    allowed = allow_rules()
    if writable:
        for root in (cwd, *add_dirs):
            if root is not None:
                allowed += [r for r in _write_rules(Path(root)) if r not in allowed]
    return ["--allowedTools", *allowed, "--disallowedTools", *NEVER_ALLOW,
            "--permission-mode", "dontAsk"]


# ── staged native lists (P13 W9; activation requires full enforcement) ────────────────


def _literal_prefix(rule: str) -> list[str] | None:
    parsed = parse_rule(rule)
    if parsed is None or parsed[0] != "Bash":
        return None
    spec = parsed[1]
    if spec.endswith(":*"):
        spec = spec[:-2]
    elif spec.endswith(" *"):
        spec = spec[:-2]
    if not spec or "*" in spec:
        return None
    try:
        return shlex.split(spec) or None
    except ValueError:
        return None


def harness_policy(harness: str, *, cwd: Optional[Path], add_dirs: Sequence[Path] = (),
                   writable: bool) -> dict:
    """Build inspectable lists for Codex and agy, without writing harness settings.

    Codex uses token-prefix Starlark rules; agy uses command/file action targets.
    Neither native prefix grammar implements arbitrary Claude command/path globs.
    Every source rule is accounted for as translated or untranslated. Consumers must
    enforce the residuals and scope before activation; `activation_ready` is false.
    An allowed interpreter can construct a secret path that command text never names.
    No command list is an OS read sandbox, including Claude's existing list.
    """
    from maestro.backends import registry

    harness = registry.normalise_name(harness)
    formats = {"codex": "token_prefix", "antigravity": "action_target"}
    try:
        rule_format = formats[harness]
    except KeyError:
        raise ValueError(f"unsupported permission harness {harness!r}") from None
    roots = list(dict.fromkeys(str(Path(p).resolve()) for p in (cwd, *add_dirs) if p is not None))
    document = {"harness": harness, "format": rule_format, "source_allow": allow_rules(), "source_deny": list(NEVER_ALLOW),
                "allow": [], "deny": [], "translated_allow": [], "translated_deny": [],
                "untranslated_allow": [], "untranslated_deny": [],
                "write_roots": roots if writable else [], "activation_ready": False}
    for side in ("allow", "deny"):
        for rule in document[f"source_{side}"]:
            prefix = _literal_prefix(rule)
            native = None
            if prefix is not None:
                native = prefix if rule_format == "token_prefix" else f"command({' '.join(prefix)})"
            elif rule_format == "action_target":
                tool, spec = parse_rule(rule)
                # agy's file targets are literal recursive roots, not Claude path globs.
                if tool in {"Read", "Edit"} and spec and "*" not in spec:
                    native = f"{'read_file' if tool == 'Read' else 'write_file'}({_normal_path(spec)})"
            if native is None:
                document[f"untranslated_{side}"].append(rule)
            else:
                document[side].append(native)
                document[f"translated_{side}"].append(rule)
    if rule_format == "action_target":
        document["allow"] += [f"read_file({root})" for root in roots]
        document["allow"] += [f"write_file({root})" for root in document["write_roots"]]
    return document


def codex_rules(policy: dict) -> str:
    """The staged native subset; residuals in `harness_policy` still need enforcement."""
    if policy.get("format") != "token_prefix":
        raise ValueError("codex_rules needs a Codex policy")
    return "".join(
        f"prefix_rule(pattern={json.dumps(prefix)}, decision={json.dumps(decision)})\n"
        for side, decision in (("allow", "allow"), ("deny", "forbidden"))
        for prefix in policy[side]
    )
