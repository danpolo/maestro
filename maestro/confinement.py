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
    against the basename — the template's own `secrets` entry is `**/*.pem`, and
    `merge.deny_list_guard` reduces that to a `*.pem` *substring*, which matches no real
    filename at all. A pattern without them is a plain substring, so `.env` denies
    `config/.env` and `data/prod.db` denies itself, which is the behaviour every caller
    here already expected.
    """
    if any(char in pattern for char in "*?["):
        return (fnmatch(path, pattern)
                or fnmatch(PurePosixPath(path).name, PurePosixPath(pattern).name))
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
