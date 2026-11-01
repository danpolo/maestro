"""What an autonomous change is allowed to touch in *this* project.

Three gates ask that question and, until now, each answered it with a tuple written for
the reference project:

* `selfheal/selffix.py` let an unattended self-fix touch `scripts/`, `orchestrator/` and
  `docs/`. That was where the reference kept its orchestrator. Since the extraction,
  maestro's code is an installed package and a scaffolded project has none of those
  directories — so every self-fix was rejected by its own path gate and downgraded to
  advisory. The feature was dead everywhere except the project it was written in.
* `selfheal/redo.py` let a `/redo` rewrite touch `colab/`, `data_export/`, `scripts/`,
  `docs/` and `tasks/` — that project's deliverable surface, and nobody else's.
* Both denied `main_bot.py`, `data/`, `models/`, `scripts/restart_bot.sh`,
  `scripts/watchdog.py`, `.service` and `requirements`. On another project those
  fragments match nothing, so the deny half failed *open*: the only thing standing
  between an autonomous rewrite and a file it should not touch was an allow-list written
  for someone else's directory layout.

The answers belong in `project.yaml`, next to the other confinement knobs the operator
already fills in during setup (`risky_set`, `deny_list_extra`, `secrets`). Two of those
are reused here rather than duplicated:

* **`secrets`** — files an autonomous task must never touch, on any path. Already the
  authority for `merge.deny_list_guard`'s secret check.
* **`prod_stores`** — production data stores. Declared in the template since the first
  version and read by *nothing* until now (`tests/test_templates.py`'s known-dead list):
  a project could name its production database here and maestro would let a self-fix
  rewrite it. It is a deny surface, which is what the name always meant.

`deny_list_extra` is deliberately *not* consulted: those are regular expressions matched
against **diff text** (`merge._HARD_DENY_PATTERNS` and friends), not path fragments, and
treating one as the other would silently match nothing or match everything.

Everything degrades to the documented default rather than raising, in the same spirit as
`maestro.config`: a malformed `confinement:` block must not stop the loop, and — the
important half — must never widen what an autonomous change may touch.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional, Sequence

from maestro.config import load_project_yaml

__all__ = [
    "SELF_FIX",
    "REDO",
    "DEFAULT_ALLOW",
    "allow_prefixes",
    "deny_fragments",
    "path_ok",
    "rules_prose",
    "available",
    "BranchNameError",
    "BRANCH_KINDS",
    "BranchRef",
    "branch_name",
    "branch_ref",
    "parse_branch",
    "is_namespaced",
    "worktree_for",
    "CommitBoundary",
    "bubblewrap_argv",
]

#: The two gates. Named rather than passed as strings so a typo is an `AttributeError`
#: here instead of a silently empty allow-list at merge time.
SELF_FIX = "self_fix"
REDO = "redo"

#: Each gate's key under `confinement:`. Spelled out rather than built from `kind`,
#: because `tests/test_templates.py` searches `maestro/` for the literal key name to
#: prove a declared knob has a reader — and it is right to: a key that exists only as an
#: f-string is one nobody can grep for, which is how a knob quietly stops being read.
_ALLOW_KEYS: dict[str, str] = {
    SELF_FIX: "self_fix_allow",
    REDO: "redo_allow",
}

#: How each gate names itself in generated prose (`rules_prose`, below). Never matched by
#: code — a third gate adds a label here in the same place it adds an allow-key. Asserted
#: in sync with `_ALLOW_KEYS` so a kind added to one and not the other fails at import
#: time (loud) rather than as prose that silently falls back to the raw `kind` string.
_LABELS: dict[str, str] = {
    SELF_FIX: "self-fix",
    REDO: "/redo rewrite",
}
assert set(_LABELS) == set(_ALLOW_KEYS), "_LABELS and _ALLOW_KEYS must name the same kinds"

#: What each gate may touch when `project.yaml` says nothing.
#:
#: Both default to the *scaffolding* `maestro init` creates, because that is the only
#: surface maestro can know exists in a project it has just been pointed at. A self-fix
#: repairs this project's orchestration setup — its adapters, its role profiles, its
#: docs — and notably not maestro's own package, which lives outside the repo and is
#: `self_modifying` territory in any case. `/redo` reshapes a deliverable, and maestro
#: cannot guess where a project keeps those, so the default is deliberately narrow: a
#: project that wants `/redo` to rewrite its notebooks says so.
DEFAULT_ALLOW: dict[str, tuple[str, ...]] = {
    SELF_FIX: ("adapters/", "profiles/", "docs/"),
    REDO: ("docs/",),
}

#: Denied for every gate regardless of configuration. `.git/` is not in `secrets` and
#: never should be listed there; an autonomous rewrite of the repository's own object
#: store is not a change, it is a loss.
_ALWAYS_DENY: tuple[str, ...] = (".git/", ".orchestrator/")


def _strings(value: object) -> tuple[str, ...]:
    """`value` as a tuple of non-empty strings; `()` for anything unusable.

    A scalar is accepted as a one-element list, because `deny: docs/` is the mistake a
    hand-edited YAML file makes and reading it as "no denials" would quietly remove a
    protection the operator thought they had added.
    """
    if isinstance(value, str):
        items: Iterable = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = value
    else:
        return ()
    return tuple(str(item).strip() for item in items if str(item).strip())


def _block(config: Optional[Mapping]) -> Mapping:
    document = load_project_yaml() if config is None else config
    if not isinstance(document, Mapping):
        return {}
    block = document.get("confinement")
    return block if isinstance(block, Mapping) else {}


def allow_prefixes(kind: str, config: Optional[Mapping] = None) -> tuple[str, ...]:
    """Path prefixes `kind` may touch, falling back to `DEFAULT_ALLOW[kind]`.

    An explicitly empty list is *not* the same as an absent one: `self_fix_allow: []`
    means "this project permits no unattended self-fix at all", which is a reasonable
    thing to say and must not be silently replaced by the default.
    """
    block = _block(config)
    key = _ALLOW_KEYS.get(kind, "")
    if not key or key not in block:
        return DEFAULT_ALLOW.get(kind, ())
    return _strings(block.get(key))


def deny_fragments(config: Optional[Mapping] = None) -> tuple[str, ...]:
    """Patterns no autonomous change may touch, whatever the allow-list says.

    `confinement.deny`, plus the project's `secrets` and `prod_stores`. Returned verbatim
    rather than pre-cleaned, because how one is matched depends on how it is written —
    see `_denied_by`.
    """
    document = load_project_yaml() if config is None else config
    document = document if isinstance(document, Mapping) else {}
    patterns = list(_ALWAYS_DENY)
    patterns += list(_strings(_block(config).get("deny")))
    patterns += list(_strings(document.get("secrets")))
    patterns += list(_strings(document.get("prod_stores")))
    return tuple(dict.fromkeys(patterns))           # de-duplicated, order preserved


def _denied_by(path: str, pattern: str) -> bool:
    """Whether `pattern` denies `path`.

    A pattern with glob metacharacters is matched as a glob, against the whole path and
    against every trailing part of it, so it names the same files at any depth — the
    template's own `secrets` entry is `**/*.pem`, and `merge.deny_list_guard` reduces that
    to a `*.pem` *substring*, which matches no real filename at all. A leading `**/` is
    dropped for the trailing parts, so `**/*.pem` also denies a root `key.pem`. The glob
    is matched whole: reducing `secrets/*` to its last segment would deny every file.
    `**/` is zero or more directories wherever it stands (`config/**/*.pem` denies
    `config/a.pem`), and a glob spelled from the root (`/secrets/*`, `./secrets/*`) or as
    a directory (`secrets/*/`) names the same files as `secrets/*`: `fnmatch` alone gives
    none of those, and a deny entry that silently matches nothing is the unsafe reading.
    A pattern without metacharacters is a plain substring, so `.env` denies
    `config/.env` and `data/prod.db` denies itself, which is the behaviour every caller
    here already expected.
    """
    if any(char in pattern for char in "*?["):
        tail = pattern.strip()
        while tail.startswith(("/", "./")):
            tail = tail[tail.index("/") + 1:]
        tail = tail.rstrip("/")
        while (collapsed := re.sub(r"(^|/)\*\*/", r"\1", tail)) != tail:
            tail = collapsed
        parts = PurePosixPath(path).parts
        return fnmatch(path, pattern) or any(
            fnmatch("/".join(parts[i:]), tail) for i in range(len(parts))
        )
    return pattern in path


def path_ok(files: object, *, kind: str,
            config: Optional[Mapping] = None) -> tuple[bool, str]:
    """Whether every path in `files` is inside `kind`'s allowed surface.

    Returns `(ok, reason)`. The reason names the first offending path, because that is
    what the operator is shown when a gate rejects a rewrite.

    An empty file list is a failure, matching both gates' existing behaviour: a self-fix
    with no `target_files` has nothing to confine, and a `/redo` that changed nothing has
    produced nothing to ship.

    Note the `lstrip("./")` the original gates used is deliberately gone. It strips a
    *character set*, so `../etc/passwd` became `etc/passwd` — a path outside the repo
    laundered into one that looks inside it (`docs/found_bugs_inbox/selfheal.md`). Paths
    are normalised properly here instead, and anything that still escapes is refused.
    """
    allowed = allow_prefixes(kind, config)
    denied = deny_fragments(config)

    cleaned = []
    for raw in _strings(files):
        # A leading slash is a repo-relative path spelled with one — that is how a diff
        # sometimes arrives — so it is normalised, not refused. `.` components are dropped
        # (`./docs/a.md` is `docs/a.md`); `..` is refused, never stripped.
        parts = [part for part in PurePosixPath(raw.lstrip("/")).parts if part != "."]
        if ".." in parts:
            return False, f"path escapes the repository: {raw}"
        cleaned.append("/".join(parts))

    if not cleaned:
        return False, "no files identified"

    for path in cleaned:
        for pattern in denied:
            if _denied_by(path, pattern):
                return False, f"denied path: {path}"
        if not allowed:
            return False, f"no path is permitted for {kind}: {path}"
        if not path.startswith(allowed):
            return False, f"out-of-scope path: {path}"
    return True, "ok"


def rules_prose(kind: str, config: Optional[Mapping] = None) -> str:
    """`kind`'s allow/deny surface as one prompt-ready sentence pair: "A <kind> may only
    change files under: X. It must NEVER change: Y." Drop the result straight into a
    system prompt or a RULES bullet.

    The single place `selfheal/diagnose.py`, `selfheal/redo.py`'s `_redo_rules`, and
    `selfheal/selffix.py`'s runner brief all render their confined surface. Each used to
    hand-write its own version, and two of the three drifted: `selffix.py`'s runner
    prompt kept naming `main_bot.py` and a `scripts/`/`orchestrator/` allow-list — the
    reference project's layout, hardcoded — long after `path_ok` had moved on to reading
    `project.yaml`, and `redo.py`'s version never mentioned the deny list at all. The gate
    itself was never wrong (`path_ok` always read the real surface), but a prompt
    describing a surface that is not the agent's own is exactly the kind of mismatch this
    module exists to prevent.

    The empty-allow-list case names the actual `confinement:` key to fix, the same detail
    `redo.py`'s own hand-written fallback used to carry before this shared it — an
    operator or the agent reading the prompt should not have to guess which key controls
    it. `_LABELS[kind]`, not `.get`: an unrecognised `kind` here is a caller bug, and
    should raise loudly rather than render a prompt naming the raw internal string.
    """
    allowed = allow_prefixes(kind, config)
    denied = deny_fragments(config)
    label = _LABELS[kind]
    if allowed:
        allow_str = ", ".join(allowed)
    else:
        allow_str = (f"(nothing — {label} is disabled here: confinement.{_ALLOW_KEYS[kind]} "
                     f"is empty in project.yaml)")
    return (f"A {label} may only change files under: {allow_str}. "
            f"It must NEVER change: {', '.join(denied)}.")


def available(path: Path) -> bool:
    """Whether `path` — an optional, project-owned verification script (`CHECK_NB` in
    `implementer.py`/`selfheal/redo.py`, `CANARY_DEPLOY` in `merge.py`) — actually exists.

    Each of those constants stays exactly where it is declared, built the same literal
    way it always was — `tests/test_no_reference_sidecars.py` scans source for that exact
    shape, so centralising the constant itself would make it invisible to that gate — but
    the `.is_file()` check guarding each one is genuine duplication, and goes through this
    single predicate instead of being retyped at every call site.
    """
    return path.is_file()


# ── [P7] §4: branch namespacing and the git commit boundary ──────────────────────────
#
# Everything above answers "which *files in the repository* may an autonomous change
# rewrite?" — a question about a diff. What follows answers a different one: "which
# *paths on the filesystem* may a confined implementer process write at all?" That is a
# question about a sandbox, and the two must not be conflated: `path_ok` would happily
# approve `docs/x.md` for a process that has no business holding a file descriptor
# anywhere outside its own worktree.
#
# `[INV-A08]` is what makes the second question answerable. A flat branch name puts every
# task's ref in the same directory as `refs/heads/main`, so "may write its own ref" and
# "may write the target branch" become the same filesystem permission and the boundary
# collapses to a convention. Namespacing them under `impl/`, `selffix/` and `redo/` gives
# each task a ref *path* of its own, which a bind mount can grant and a mask can deny.


class ConfinementError(RuntimeError):
    """A confined operation was asked to touch something outside its boundary."""


def bubblewrap_argv(
    argv: Sequence[str], *, cwd: Path, write_roots: Sequence[Path] = (),
    read_only_roots: Sequence[Path] = (), scratch: Optional[Path] = None,
) -> list[str]:
    """Wrap a process in a read-only filesystem with explicit writable bind mounts.

    This constructs argv only: it creates no host directories and launches nothing.
    Callers own preparation/lifetime of roots, the private scratch mount, and the
    post-run tree audit. A missing binary or invalid grant refuses; there is no
    unconfined retry. Scratch is a private tmpfs at an existing dedicated directory,
    not a writable /tmp that could shadow claimed writes to other host paths.

    Network access is unchanged. This is a filesystem write boundary, not a promise
    that an allowed program cannot use an external service or read host files.
    """
    binary = shutil.which("bwrap")
    if binary is None:
        raise ConfinementError("bubblewrap is unavailable; refusing an unconfined process")
    if not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise ConfinementError("a confined process needs a valid argv")
    directory = Path(cwd).resolve()
    if not directory.is_dir():
        raise ConfinementError(f"working directory does not exist: {directory}")
    broad = {Path("/"), Path.home().resolve(), Path("/home"), Path("/tmp"),
             Path("/run"), Path("/proc"), Path("/dev"), Path("/var"), Path("/etc")}
    roots: list[Path] = []
    for root in write_roots:
        path = Path(root).resolve()
        if not path.is_dir() or path in broad:
            raise ConfinementError(f"missing or overbroad writable directory: {path}")
        if path not in roots:
            roots.append(path)
    protected: list[Path] = []
    for root in read_only_roots:
        path = Path(root).resolve()
        if not path.exists():
            raise ConfinementError(f"read-only control path does not exist: {path}")
        protected.append(path)
    temp = Path(scratch).resolve() if scratch is not None else None
    if temp is not None and (not temp.is_dir() or temp in broad or temp == directory
                             or any(root.is_relative_to(temp) for root in roots)):
        raise ConfinementError(f"invalid private scratch directory: {temp}")
    wrapped = [binary, "--die-with-parent", "--new-session", "--unshare-user",
               "--unshare-pid", "--unshare-ipc", "--cap-drop", "ALL",
               "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
    runtime = Path("/run/user") / str(os.getuid())
    if runtime.is_dir():
        wrapped += ["--tmpfs", str(runtime)]
    for root in sorted(roots, key=lambda path: len(path.parts)):
        wrapped += ["--bind", str(root), str(root)]
    if temp is not None:
        wrapped += ["--tmpfs", str(temp)]
        for name in ("TMPDIR", "TMP", "TEMP"):
            wrapped += ["--setenv", name, str(temp)]
    for root in sorted(protected, key=lambda path: len(path.parts)):
        wrapped += ["--ro-bind", str(root), str(root)]
    return [*wrapped, "--chdir", str(directory), "--", *argv]


class BranchNameError(ValueError):
    """A branch name that `[INV-A08]` forbids, or an unparseable one."""


#: The three namespaces, and whether each requires an attempt id. An implementation
#: branch belongs to the task; a self-fix or redo belongs to one *attempt* at it, and
#: two attempts sharing a branch would make the second one's history the first one's.
BRANCH_KINDS: dict[str, bool] = {
    "impl": False,
    "selffix": True,
    "redo": True,
}

#: Refs no confined process may write under any circumstance, whatever its namespace.
#: `packed-refs` is here because it is the *other* way to move `main`: a loose ref file
#: is not the only writable representation of a branch head.
_PROTECTED_REF_FILES: tuple[str, ...] = ("packed-refs", "HEAD", "config", "hooks")


@dataclass(frozen=True)
class BranchRef:
    """One namespaced branch, parsed into its parts."""

    kind: str
    task_id: str
    attempt_id: str = ""
    candidate: str = ""

    @property
    def name(self) -> str:
        """The branch as git spells it — `impl/T-001`, `impl/T-001/c2`, `redo/T-001_a7`."""
        leaf = self.task_id if not self.attempt_id else f"{self.task_id}_{self.attempt_id}"
        return f"{self.kind}/{leaf}" + (f"/{self.candidate}" if self.candidate else "")

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.name}"

    def __str__(self) -> str:
        return self.name


def _segment(value: object, *, what: str) -> str:
    """One path segment of a branch name, refused if it could escape the namespace.

    A `/` in a task id would silently deepen the namespace; a `..` or a leading `-` are
    the shapes that turn a ref path into a traversal or a git option.
    """
    text = str(value or "").strip()
    if not text:
        raise BranchNameError(f"{what} is empty")
    if "/" in text or text.startswith(("-", ".")) or ".." in text or text.endswith(".lock"):
        raise BranchNameError(f"{what} {text!r} is not usable in a branch name")
    return text


def branch_name(
    kind: str, task_id: object, *, attempt_id: object = "", candidate: object = ""
) -> str:
    """The one place a branch name for autonomous work is spelled (`[INV-A08]`).

    Called rather than formatted at each launch site, because a flat name is not a
    cosmetic slip: it is the thing that makes the commit boundary below unenforceable.
    """
    return branch_ref(kind, task_id, attempt_id=attempt_id, candidate=candidate).name


def branch_ref(
    kind: str, task_id: object, *, attempt_id: object = "", candidate: object = ""
) -> BranchRef:
    if kind not in BRANCH_KINDS:
        raise BranchNameError(
            f"unknown branch namespace {kind!r} (expected one of {sorted(BRANCH_KINDS)})"
        )
    needs_attempt = BRANCH_KINDS[kind]
    attempt = str(attempt_id or "").strip()
    if needs_attempt and not attempt:
        raise BranchNameError(f"a {kind} branch is per-attempt and needs an attempt_id")
    if attempt and not needs_attempt:
        raise BranchNameError(f"a {kind} branch belongs to the task, not to one attempt")
    return BranchRef(
        kind=kind,
        task_id=_segment(task_id, what="task_id"),
        attempt_id=_segment(attempt, what="attempt_id") if attempt else "",
        candidate=_segment(candidate, what="candidate") if candidate else "",
    )


def parse_branch(branch: object) -> Optional[BranchRef]:
    """`BranchRef` for a namespaced branch, `None` for anything else.

    `None` rather than an exception: the caller asking is usually deciding whether a name
    it was handed is one of ours, and "no" is an ordinary answer to that.
    """
    text = str(branch or "").strip()
    text = text[len("refs/heads/"):] if text.startswith("refs/heads/") else text
    parts = text.split("/")
    if len(parts) not in (2, 3) or parts[0] not in BRANCH_KINDS:
        return None
    kind, leaf = parts[0], parts[1]
    candidate = parts[2] if len(parts) == 3 else ""
    task_id, sep, attempt = leaf.partition("_")
    if BRANCH_KINDS[kind] and not sep:
        return None
    if not BRANCH_KINDS[kind] and sep:
        task_id, attempt = leaf, ""
    try:
        return branch_ref(kind, task_id, attempt_id=attempt, candidate=candidate)
    except BranchNameError:
        return None


def is_namespaced(branch: object) -> bool:
    """Whether `branch` is a name `[INV-A08]` permits an agent to write."""
    return parse_branch(branch) is not None


def worktree_for(repo: Path, task_id: object, *, candidate: object = "") -> Path:
    """`§4.2`'s assigned worktree: `<repo>/.orchestrator/worktrees/<task>[/<candidate>]`.

    Candidates get a directory *under* the task's, not a sibling with a decorated name,
    so that one bind mount of the task directory grants exactly the candidates of that
    task and nothing else (`[INV-06]` is a filesystem fact here, not a convention).
    """
    root = Path(repo) / ".orchestrator" / "worktrees" / _segment(task_id, what="task_id")
    return root / _segment(candidate, what="candidate") if candidate else root


@dataclass(frozen=True)
class CommitBoundary:
    """The four paths a confined implementer may write, and nothing else (`§4.2`).

    Constructed from the branch, never from the task id alone: the ref a process may
    advance is *its own*, and deriving it from anything but the branch it was given would
    reintroduce the ambient authority the namespacing exists to remove.

    `permits` answers with a reason because the reason is the diagnostic. A sandbox that
    merely denies a write leaves an operator reading "permission denied" on a path with
    no indication of which rule fired; the two that matter here — the target branch is
    the controller's (`[INV-05]`), and anything outside the worktree fails closed — read
    very differently in a journal.
    """

    repo: Path
    branch: str
    worktree: Path

    @classmethod
    def for_task(
        cls, repo: Path, task_id: object, *, kind: str = "impl",
        attempt_id: object = "", candidate: object = "",
    ) -> "CommitBoundary":
        ref = branch_ref(kind, task_id, attempt_id=attempt_id, candidate=candidate)
        return cls(
            repo=Path(repo),
            branch=ref.name,
            worktree=worktree_for(repo, ref.task_id, candidate=ref.candidate),
        )

    def __post_init__(self) -> None:
        if not is_namespaced(self.branch):
            raise BranchNameError(
                f"branch {self.branch!r} is not namespaced — a flat branch name cannot be "
                f"confined at the filesystem level ([INV-A08])"
            )
        object.__setattr__(self, "repo", Path(self.repo))
        object.__setattr__(self, "worktree", Path(self.worktree))

    @property
    def git_dir(self) -> Path:
        """The shared repository's own `.git`. Not the worktree's."""
        return self.repo / ".git"

    @property
    def object_store(self) -> Path:
        return self.git_dir / "objects"

    @property
    def ref_path(self) -> Path:
        return self.git_dir / "refs" / "heads" / Path(self.branch)

    @property
    def reflog_path(self) -> Path:
        return self.git_dir / "logs" / "refs" / "heads" / Path(self.branch)

    def writable_roots(self) -> tuple[Path, ...]:
        """The whole write surface, in `§4.2`'s order. Four entries, never five.

        The worktree's own `.git` is inside the worktree and so needs no separate entry;
        it is listed anyway because `§4.2` enumerates it, and because a sandbox builder
        reading this tuple is building mounts, not evaluating a predicate.
        """
        return (self.worktree, self.worktree / ".git", self.object_store, self.ref_path)

    def target_ref(self, target_branch: str = "main") -> Path:
        return self.git_dir / "refs" / "heads" / Path(str(target_branch))

    def permits(self, path: object, *, target_branch: str = "main") -> tuple[bool, str]:
        """Whether a confined process may write `path`. Fails closed on anything unclear.

        Relative paths are read against the repository, absolute ones taken as given; a
        path containing `..` is refused outright rather than normalised, because the only
        honest reading of a traversal inside a sandbox request is that something is
        trying to leave.
        """
        raw = Path(str(path))
        if ".." in raw.parts:
            return False, f"path escapes its sandbox: {raw}"
        candidate = raw if raw.is_absolute() else (self.repo / raw)
        candidate = Path(os.path.normpath(str(candidate)))

        if _within(candidate, self.worktree):
            return True, "task worktree"
        if _within(candidate, self.object_store):
            return True, "shared object store"
        if _within(candidate, self.ref_path):
            return True, "own namespaced ref"
        if _within(candidate, self.reflog_path):
            return True, "own namespaced reflog"

        target = self.target_ref(target_branch)
        if _within(candidate, target):
            return False, (
                f"target branch {target_branch!r} is the controller's to write "
                f"([INV-05]): {candidate}"
            )
        heads = self.git_dir / "refs" / "heads"
        if _within(candidate, heads):
            return False, f"ref outside this task's namespace: {candidate}"
        for name in _PROTECTED_REF_FILES:
            if _within(candidate, self.git_dir / name):
                return False, f"repository {name} is never agent-writable: {candidate}"
        if _within(candidate, self.git_dir):
            return False, f"shared git directory beyond the object store: {candidate}"
        return False, f"outside the task worktree: {candidate}"

    def restore(self, sha: object, *, runner=None) -> None:
        """Put this boundary's worktree back at `sha`, discarding everything after it.

        P12B F6: a retry after a bad output starts from where its predecessor started, not
        from the half-finished tree it left behind — otherwise the second attempt inherits
        the first one's mistakes and the retry measures nothing.

        This is the one destructive git operation the ladder performs, so it is confined
        the same way every write is: the target must be *this* boundary's own worktree,
        under `.orchestrator/worktrees/`, and never the repository root. A refusal raises
        rather than returning a reason, because there is no safe partial restore.
        """
        import subprocess

        run = subprocess.run if runner is None else runner
        target = Path(os.path.normpath(str(self.worktree)))
        text = str(sha or "").strip()
        if not text:
            raise ConfinementError("a restore needs the sha it is restoring to")
        if target == Path(os.path.normpath(str(self.repo))):
            raise ConfinementError(
                f"a restore never touches the repository root: {target}"
            )
        expected = worktree_for(self.repo, "x").parent
        if not _within(target, expected):
            raise ConfinementError(
                f"{target} is not a worktree under {expected}; refusing to reset it"
            )
        for argv in (
            ["git", "-C", str(target), "reset", "--hard", text],
            ["git", "-C", str(target), "clean", "-fdq"],
        ):
            done = run(argv, capture_output=True, text=True)
            if getattr(done, "returncode", 0) != 0:
                raise ConfinementError(
                    f"restoring {target} to {text} failed: "
                    f"{(getattr(done, 'stderr', '') or '').strip()[:400]}"
                )

    def confirm_persisted(self, paths: Iterable[object]) -> tuple[bool, str]:
        """Whether every write the agent claims to have made actually survived (`§4.2`).

        The mount hazard the spec names: masking a parent directory while binding a child
        leaves a writable tmpfs between them, so a write *succeeds*, the process sees its
        own file, and the bytes vanish when the sandbox unmounts. That is indistinguishable
        from success at the moment it happens and must therefore be checked afterwards —
        a claimed write that the boundary permits and the filesystem does not have is an
        execution failure, not a missing file.
        """
        for raw in paths:
            ok, reason = self.permits(raw)
            if not ok:
                return False, f"write outside the boundary: {reason}"
            target = Path(str(raw))
            target = target if target.is_absolute() else (self.repo / target)
            if not target.exists():
                return False, (
                    f"write did not survive the sandbox (masked parent, writable tmpfs "
                    f"between mask and bind): {target}"
                )
        return True, "ok"


def _within(path: Path, root: Path) -> bool:
    """Whether `path` is `root` or lies under it. Pure lexical containment."""
    root = Path(os.path.normpath(str(root)))
    path = Path(os.path.normpath(str(path)))
    return path == root or root in path.parents
