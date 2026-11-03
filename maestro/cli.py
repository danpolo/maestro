"""The `maestro` console-script entry point (DESIGN.md §10, `docs/plans/2026-08-16-m4-setup.md`).

Subcommands: `init`, `doctor`, `status`, `run`, `ctl`, `roadmap`, `watchdog`,
`install-skills`, plus `[P11]`'s two operator projections over the execution graph,
`workflow show` and `explain`.
All six are Component D wiring `run_adapter` (Component A), the templates (Component B) and
the setup skill (Component C) together; none of the actual orchestration logic lives here.
`run` wraps `maestro.orchestrator.main()` and `watchdog` wraps `maestro.watchdog.main()`
(extracted in M4b, `docs/plans/2026-08-17-m4b-watchdog.md`) — both are three-line wrappers
that set `$MAESTRO_REPO` and call through.

**One project per process — read this before adding a subcommand.** Every extracted module
this file imports (`maestro.state`, `maestro.docs.roadmap`, `maestro.config`, `maestro.limits`,
`maestro.worktree`, `maestro.orchestrator`, ...) resolves its project root exactly once, at
*import* time, via `Paths.from_env()` reading `$MAESTRO_REPO` (see `maestro/paths.py`). A
module already imported in this interpreter keeps whatever root it first bound to — importing
it again after changing `$MAESTRO_REPO` does **not** rebind it. That is fine for a real `maestro
<command>` invocation (a fresh OS process every time, exactly one project) and it is why every
`cmd_*` function below sets `os.environ["MAESTRO_REPO"]` and only *then* lazily imports the
extracted modules it needs, inside the function body, never at this module's top level. It is
**not** fine across two different projects in one long-lived process — that is what makes
`tests/test_cli.py` invoke this file as a subprocess (`sys.executable -m maestro.cli ...`)
whenever a test needs two independent scratch projects in the same pytest session, rather than
calling `cmd_doctor()`/`cmd_status()`/`cmd_run()`/`cmd_ctl()` in-process back to back.

**The no-op supervised loop mechanism** (`docs/plans/2026-08-16-m4-setup.md`'s required research
step) is a *natural early return*, not an env var or an iteration cap. Traced by reading
`maestro/orchestrator.py`'s `main()`:

1. `main()` hard-requires a tmux session literally named `"agents"` (`maestro.worktree.
   TMUX_SESSION` — a single shared session name, not derived per-project) to already exist;
   absent, it prints an error and returns `1` immediately, before the event loop even starts.
   `_ensure_agents_tmux_session()` below creates it if missing and never kills it (other
   projects, and any real production orchestrator on this machine, may depend on it staying up).
2. With `in_flight` empty and `parse_runnable_tasks() + parse_prep_tasks()` empty (a roadmap
   with zero task blocks) and `waiting_on_dan` empty, the *first* iteration of `main()`'s `while
   True:` loop hits the `not in_flight and not runnable and not waiting_on_dan` branch: it logs
   `"[done] Queue exhausted."`, calls `_notify_proposal_test_due()` (a Telegram no-op here, since
   a fresh scaffold has no `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALERT_CHAT_ID` in its environment),
   which sets `state["paused_by_user"] = True`, then `time.sleep(POLL_INTERVAL)` (30s by
   default) and `continue`s.
3. The *second* iteration re-reads state at the very top of the loop, sees `paused_by_user`,
   prints `"[halt] ... — stopping."`, journals `halt_respected`, and `break`s out.
4. `main()` falls through to `run_status(); return 0`.

So an empty-roadmap run always completes and returns `0` on its own, in one `POLL_INTERVAL`-
bounded idle pass — no cap needed, *the first time*. `_run_noop_supervised_loop_check()` below
still wraps the call in a subprocess with a generous `timeout_s` as a defence-in-depth belt
(not the actual bounding mechanism) in case a future change to `main()` breaks the
natural-return property.

**Only the first time, though** — step 2 above leaves `state["paused_by_user"] = True` behind
as its own return mechanism, and a *second* `main()` call against the same project hits a
different branch at the very top of the function (`if state.get("paused_by_user"): ... while
True: ...`) that only exits on an external `/resume` or `halted` — it does **not** self-
terminate. Confirmed empirically while making `init` re-runs safe: see
`_reset_control_flags_for_loop_check()`, which `cmd_init` calls immediately before every
verification run so each `init` (first or repeated) deterministically re-exercises the true
natural-return path rather than inheriting whatever a previous run left in `state.json`.

**A landmine this file's `init` works around**: `main()` unconditionally calls `run_dep_map()`
(`maestro/docs/roadmap.py`) and `run_status()` (`maestro/hitl/commands.py`) every poll, and both
shell out to `<project_root>/.venv/bin/python3` with no existence check — `subprocess.run` raises
an *uncaught* `FileNotFoundError` if that path is missing, crashing `main()` outright (verified
empirically while writing this module: a scratch project with no `.venv/` reproduces it on the
very first poll). `cmd_init` scaffolds `.venv/bin/python3` as a symlink to `sys.executable` so a
fresh project survives its own no-op-loop check; see `docs/FOUND_BUGS.md` / `docs/PROGRESS.md`
for the write-up. This is a real defect in already-extracted code, not something this file fixes
at the source — cli.py only avoids triggering it.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

# Safe at module scope precisely because it imports nothing from `maestro` — see its
# docstring, and this module's own note on why the `cmd_*` functions import lazily.
from maestro import bootstrap

#: This checkout's own root — the directory *containing* the `maestro` package — used to
#: (a) find `templates/`, `skills/`, `scripts/telegram_creds.py` relative to this file rather
#: than relative to whatever the caller's cwd happens to be, and (b) make `maestro.*` importable
#: in the subprocesses this module spawns (`sys.executable -m maestro.cli ...`) when this
#: checkout is not `pip install`-ed. Both uses are read-only lookups; nothing here writes here.
_MAESTRO_SOURCE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

#: `maestro.worktree.TMUX_SESSION`'s value, duplicated here as a plain string rather than
#: imported, so this constant is available before `$MAESTRO_REPO` is set (importing
#: `maestro.worktree` binds its own `Paths.from_env()`-derived globals at import time — see the
#: module docstring's "one project per process" note). If `TMUX_SESSION` ever changes, this
#: string and `maestro/worktree.py`'s must be kept in sync by hand.
_TMUX_SESSION = "agents"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _render(text: str, mapping: dict) -> str:
    """Fill `{{ key }}` placeholders from `mapping`; an unrecognised token (e.g. the literal
    `{{ ... }}` inside `project.yaml.tmpl`'s own comment) is left exactly as written."""
    return _PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), text)


def _systemd_quote(value: str) -> str:
    """Quote one systemd directive/argv value and preserve literal specifier characters."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def _systemd_path_escape(value: str) -> str:
    """Escape a filesystem path for directives that treat quotes as path characters."""
    safe = b"/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return "".join(chr(byte) if byte in safe else f"\\x{byte:02x}" for byte in value.encode())


def _systemd_service_mapping(facts: dict) -> dict[str, str]:
    repo = facts["repo_path"]
    shell_command = (
        "set -a; [ ! -f .env ] || . ./.env; set +a; "
        "MAESTRO_REPO=. exec ./.venv/bin/maestro watchdog"
    )
    return {
        "systemd_repo_path": _systemd_path_escape(repo),
        "systemd_session": _systemd_quote(f"{facts['project_name']}-watchdog"),
        "systemd_watchdog_command": _systemd_quote(shell_command),
    }


def _subprocess_env(repo_root: Path, extra: Optional[dict] = None) -> dict:
    """`os.environ` plus `MAESTRO_REPO=<repo_root>` and this checkout on `PYTHONPATH`, for a
    subprocess that needs to `import maestro` (or `-m maestro.cli`) against a specific project
    without disturbing this process's own environment."""
    env = dict(os.environ)
    env["MAESTRO_REPO"] = str(repo_root)
    parent = str(_MAESTRO_SOURCE_ROOT)
    parts = env.get("PYTHONPATH", "").split(os.pathsep) if env.get("PYTHONPATH") else []
    if parent not in parts:
        parts.insert(0, parent)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    if extra:
        env.update(extra)
    return env


# ══════════════════════════════════════════════════════════════════════════
# init
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ScaffoldOutcome:
    """What `_scaffold_file` did for one destination path."""

    dest: Path
    action: str  # "created" | "unchanged" | "new_written"
    diff: str = ""


def _git(repo_root: Path, *args: str) -> str:
    """`git <args>` in `repo_root`; `""` on any failure (a missing remote, a repo with no
    commits yet, ...) — every caller here treats absence as "nothing to derive", not an error."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _derive_repo_facts(repo_root: Path) -> dict:
    """Everything `init` can derive mechanically (DESIGN.md §10's "Derives" bullet):
    git root, remote, current branch (used as `main_branch` — the *checked-out* branch, not
    the remote's default, since resolving the remote's default needs a network round-trip and
    a throwaway/offline repo must still `init` cleanly), project name (repo root's basename).

    Test discovery is intentionally root-scoped and conservative.  A package manifest alone
    does not prove a test command exists: Node needs a ``scripts.test`` entry and Python needs
    pytest configuration/dependency evidence or a tests directory.  The exact argv is rendered
    into the generated test adapter; ambiguous monorepo policy remains setup-adviser work.
    """
    git_root_raw = _git(repo_root, "rev-parse", "--show-toplevel")
    git_root = Path(git_root_raw).resolve() if git_root_raw else repo_root.resolve()
    remote = _git(git_root, "remote", "get-url", "origin")
    branch = _git(git_root, "symbolic-ref", "--short", "HEAD") or "main"

    test_command_argv: list[str] = []
    package_json = git_root / "package.json"
    pyproject = git_root / "pyproject.toml"
    requirements = git_root / "requirements.txt"
    if package_json.is_file():
        package_manager = "npm"
        try:
            package_document = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package_document = {}
        scripts = package_document.get("scripts") if isinstance(package_document, dict) else None
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str) and scripts["test"].strip():
            test_command_argv = ["npm", "test"]
    elif pyproject.is_file() or requirements.is_file():
        package_manager = "pip"
        pytest_signal = (git_root / "tests").is_dir()
        if pyproject.is_file():
            try:
                pyproject_document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                pyproject_document = {}
            tool = pyproject_document.get("tool") if isinstance(pyproject_document, dict) else None
            pytest_signal = pytest_signal or (isinstance(tool, dict) and "pytest" in tool)
            project = pyproject_document.get("project") if isinstance(pyproject_document, dict) else None
            dependency_text = json.dumps(project.get("dependencies", [])) if isinstance(project, dict) else ""
            optional_text = json.dumps(project.get("optional-dependencies", {})) if isinstance(project, dict) else ""
            pytest_signal = pytest_signal or "pytest" in (dependency_text + optional_text).lower()
        if requirements.is_file():
            try:
                pytest_signal = pytest_signal or "pytest" in requirements.read_text(encoding="utf-8").lower()
            except OSError:
                pass
        if pytest_signal:
            # Never `.resolve()` this: `.venv/bin/python3` is normally a symlink to the base
            # interpreter, and following it runs the tests outside the virtualenv, where
            # the first venv-only dependency turns every gate run into a collection error
            # (F2, P00 DuetFlow rehearsal). `git_root` is already absolute.
            test_command_argv = [
                str(git_root / ".venv" / "bin" / "python3"),
                "-m",
                "pytest",
                "-q",
            ]
    elif (git_root / "go.mod").is_file():
        package_manager = "go"
        test_command_argv = ["go", "test", "./..."]
    else:
        package_manager = ""

    test_command = " ".join(test_command_argv)

    return {
        "repo_path": str(git_root),
        "project_name": git_root.name,
        "remote": remote,
        "main_branch": branch,
        "test_command": test_command,
        "test_command_argv": test_command_argv,
        "package_manager": package_manager,
    }


def _scaffold_file(dest: Path, content: str, *, executable: bool = False) -> ScaffoldOutcome:
    """Idempotent write per DESIGN.md §10: a destination that already exists with *different*
    content is never clobbered — the rendered content goes to `dest.new` instead, with a unified
    diff. A destination that already exists with *identical* content is left alone (no `.new`
    noise on a plain re-run with nothing changed — the acceptance criterion is "re-running init
    is provably non-destructive", not "re-running init always produces new files"; DESIGN.md's
    rule protects existing content, and there is nothing to protect when the content matches).
    A destination that does not exist yet is simply created.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_text(content, encoding="utf-8")
        if executable:
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return ScaffoldOutcome(dest, "created")

    old = dest.read_text(encoding="utf-8")
    if old == content:
        return ScaffoldOutcome(dest, "unchanged")

    new_path = dest.with_name(dest.name + ".new")
    new_path.write_text(content, encoding="utf-8")
    if executable:
        new_path.chmod(new_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), content.splitlines(keepends=True),
        fromfile=str(dest), tofile=str(new_path),
    ))
    return ScaffoldOutcome(dest, "new_written", diff)


def _ensure_venv_python_symlink(repo_root: Path) -> None:
    """Work around the `.venv/bin/python3`-must-exist landmine documented in this module's
    docstring. Never touches an existing `.venv` (real or previously symlinked) — this is a
    compatibility shim, not a scaffolded config file, so it gets no `.new`-diff treatment."""
    target = repo_root / ".venv" / "bin" / "python3"
    if target.exists() or target.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(sys.executable)


def _ensure_orchestrator_dirs(repo_root: Path) -> list[ScaffoldOutcome]:
    """`state.json` is runtime state, never a scaffold: once it exists — from a previous
    `init`'s no-op loop check, or a live controller — there is nothing to diff it against,
    and a `state.json.new` holding `{}` is noise an operator might act on (F3)."""
    orch = repo_root / ".orchestrator"
    (orch / "workspaces").mkdir(parents=True, exist_ok=True)
    state_json = orch / "state.json"
    if state_json.exists():
        return [ScaffoldOutcome(state_json, "unchanged")]
    return [_scaffold_file(state_json, "{}\n")]


_GITIGNORE_HEADER = """\
# maestro (DESIGN.md §10 scaffolding)
.orchestrator/
.venv/
__pycache__/
*.pyc
.env
.env.local
"""

_GITIGNORE_META_COMMENT = """\
# Process docs (handoffs, plans, progress, lessons): versioned on the orphan `meta`
# branch (see maestro/metabranch.py), not on the main branch. Ignored here, not deleted
# — `maestro init`/`doctor`/the orchestrator's startup reconcile keep them committed
# there automatically; nothing about this is a manual git step."""


def _gitignore_block() -> str:
    """The `.gitignore` scaffold rendered by `init`. The tier-3 (never versioned)
    entries above are hardcoded; the tier-2 (versioned on `meta`, not main) entries
    below are generated from `metabranch.META_PATHS` — the single source of truth
    that module's docstring promises, so a newly added process-doc path only needs
    to be added there."""
    from maestro import metabranch
    lines = [_GITIGNORE_META_COMMENT]
    for rel in metabranch.META_PATHS:
        lines.append(rel if "." in Path(rel).name else f"{rel}/")
    return _GITIGNORE_HEADER + "\n" + "\n".join(lines) + "\n"


def _gitignore_key(line: str) -> str:
    return line.strip().rstrip("/")


def _scaffold_gitignore(dest: Path) -> ScaffoldOutcome:
    """Scaffold `.gitignore` by appending, never by replacing (F4).

    A project's own ignores are usually the ones guarding secrets (`*.token`, `config.yaml`,
    `data/`), so the generic `.new`-with-a-diff treatment is wrong here: an operator who
    merges a replacement as offered silently un-ignores them. An existing file gets a `.new`
    holding its own content unchanged plus only the entries it lacks, so the diff is pure
    insertions; a file that already has every entry is `unchanged`.
    """
    block = _gitignore_block()
    if not dest.exists():
        return _scaffold_file(dest, block)
    old = dest.read_text(encoding="utf-8")
    present = {_gitignore_key(line) for line in old.splitlines()}
    missing = [
        line for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and _gitignore_key(line) not in present
    ]
    if not missing:
        return ScaffoldOutcome(dest, "unchanged")
    base = old if not old or old.endswith("\n") else old + "\n"
    appended = "\n# maestro (DESIGN.md §10 scaffolding): entries this file was missing\n"
    return _scaffold_file(dest, base + appended + "\n".join(missing) + "\n")


_DENY_LIST_PLACEHOLDER = "(none yet — see project.yaml's risky_set / deny_list_extra)"


def _repo_project_yaml(repo_root: Path) -> dict:
    """`<repo_root>/project.yaml` read directly, `{}` for anything unusable.

    Not `maestro.config.load_project_yaml`: that binds its root once per process at import
    time, and `init`/`doctor` are handed an explicit root. This is the project layer alone,
    which the per-layer doctor checks need; a reader of a setting uses `_repo_config`.
    """
    import yaml

    try:
        document = yaml.safe_load((repo_root / "project.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return document if isinstance(document, dict) else {}


def _repo_config(repo_root: Path) -> dict:
    """`repo_root`'s effective config: its `project.yaml` laid over the global file
    (DESIGN.md §17.1), which is what the running loop reads."""
    from maestro import config as _config

    return _config.effective_config(repo_root)[0]


def _deny_list_entries(document: dict) -> list[str]:
    """`risky_set` + `deny_list_extra`, merged in that order — the preamble's deny list."""
    entries: list[str] = []
    for key in ("risky_set", "deny_list_extra"):
        value = document.get(key)
        if isinstance(value, list):
            entries.extend(str(item) for item in value if item is not None and str(item).strip())
    return entries


def _deny_list_block(repo_root: Path) -> str:
    """What `operating_preamble.md`'s deny-list block renders: one entry per line, or the
    placeholder while `project.yaml` has none."""
    return "\n".join(_deny_list_entries(_repo_config(repo_root))) or _DENY_LIST_PLACEHOLDER


def _run_telegram_creds(repo_root: Path, *, script_override: Optional[Path] = None) -> str:
    """Use shared bot credentials and register delivery; no project token prompt.

    An explicit test override invokes `scripts/telegram_creds.py` as a subprocess — never read the token file
    ourselves (DESIGN.md's secret-handling rule). Any non-zero exit (no token file, rejected
    token, no inbound message, unwritable target) or the script simply not being present is
    treated the same way: skip Telegram setup, keep going, never fail `init` over it.

    `script_override` exists purely for tests: it lets a test point this at a small stand-in
    script instead of the real `scripts/telegram_creds.py`, so a test can never end up shelling
    out to the real script even indirectly (which would read the operator's real token file at
    `~/.config/maestro/dev_bot_token` — forbidden for a test under any circumstances) regardless
    of what `$MAESTRO_TOKEN_FILE` happens to resolve to in the test's environment.
    """
    if script_override is None:
        from maestro.hitl.shared_bot import credentials, register_project
        if all(credentials()):
            register_project(repo_root)
            return 'ok — using shared ~/.maestro/.env bot'
        return 'skipped (configure the shared bot in ~/.maestro/.env, mode 600)'
    script = script_override
    if not script.is_file():
        return "skipped (scripts/telegram_creds.py not found)"
    env_path = repo_root / ".env"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), str(env_path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"skipped (could not run telegram_creds.py: {exc})"
    if proc.returncode == 0:
        return f"ok — credentials written to {env_path}"
    reason = (proc.stderr or proc.stdout or "").strip().splitlines()
    return f"skipped ({reason[-1] if reason else f'exit {proc.returncode}'})"


#: Same candidate list `scripts/ensure_mermaid.sh` checks — kept in sync by hand, not derived
#: from the shell script (the two run in different processes and neither shells out to the
#: other for this one lookup). Used to fill `.mermaid/puppeteer-config.json`'s
#: `executablePath` so `mmdc` reuses whatever headless-capable browser is already on the box
#: instead of downloading its own.
_CHROMIUM_CANDIDATES = (
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
)


def _detect_chromium_path() -> Optional[str]:
    """First executable in `_CHROMIUM_CANDIDATES`, or `None` if none is installed — in which
    case the scaffolded puppeteer config omits `executablePath` entirely and `mmdc` falls back
    to whatever Chromium puppeteer itself bundled/downloaded."""
    for candidate in _CHROMIUM_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _puppeteer_config_json() -> str:
    """Content for `.mermaid/puppeteer-config.json` — the `-p` config `maestro.docs.depmap.
    render_png()` passes to `mmdc`. `--no-sandbox`/`--disable-setuid-sandbox` are required for
    headless Chromium under a root-less container or CI runner; `--disable-dev-shm-usage`
    avoids `/dev/shm` running out on a small instance. Matches the reference project's own
    `.mermaid/puppeteer-config.json` shape."""
    config: dict = {
        "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    }
    chromium = _detect_chromium_path()
    if chromium:
        config = {"executablePath": chromium, **config}
    return json.dumps(config, indent=2) + "\n"


def _run_ensure_mermaid(repo_root: Path, *, script_override: Optional[Path] = None) -> str:
    """Invoke `scripts/ensure_mermaid.sh` as a subprocess — best-effort, exactly like
    `_run_telegram_creds`: any non-zero exit (no npm, no network, install failure) or the
    script simply not being present is treated the same way — skip, keep going, never fail
    `init` over it. The script itself is check-then-install (a no-op if `mmdc` already
    resolves, globally or from a legacy per-repo `.mermaid/` install) so re-running `init`
    never reinstalls anything that already works.

    `script_override` exists purely for tests, mirroring `_run_telegram_creds`'
    `script_override`: it lets a test point this at a small stand-in script instead of the
    real `scripts/ensure_mermaid.sh`, so a test can never end up shelling out to real `npm`
    (which would hit the network) regardless of what happens to already be on the machine's
    `$PATH`.
    """
    script = script_override or (_MAESTRO_SOURCE_ROOT / "scripts" / "ensure_mermaid.sh")
    if not script.is_file():
        return "skipped (scripts/ensure_mermaid.sh not found)"
    try:
        proc = subprocess.run(
            ["bash", str(script), str(repo_root)],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"skipped (could not run ensure_mermaid.sh: {exc})"
    if proc.returncode == 0:
        last = (proc.stdout or "").strip().splitlines()
        return last[-1] if last else "ok"
    reason = (proc.stderr or proc.stdout or "").strip().splitlines()
    return f"skipped ({reason[-1] if reason else f'exit {proc.returncode}'})"


def _ensure_agents_tmux_session(*, tmux_bin: str = "tmux") -> bool:
    """Ensure the shared `"agents"` tmux session `orchestrator.main()` hard-requires exists.
    Creates it if absent; never kills a pre-existing one (see this module's docstring — other
    projects, and any real production orchestrator loop on this machine, may already depend on
    it). Returns whether the session is present after this call."""
    has = subprocess.run([tmux_bin, "has-session", "-t", _TMUX_SESSION], capture_output=True)
    if has.returncode == 0:
        return True
    created = subprocess.run(
        [tmux_bin, "new-session", "-d", "-s", _TMUX_SESSION], capture_output=True,
    )
    return created.returncode == 0


_LOOP_CHECK_FLAGS = ("paused_by_user", "halted", "proposal_test_notified")


def _reset_control_flags_for_loop_check(repo_root: Path) -> None:
    """Clear `paused_by_user`/`halted`/`proposal_test_notified` in `state.json` before the
    no-op supervised loop check.

    **Why this exists — found while verifying `init` re-runs cleanly (two distinct hangs, both
    reproduced empirically, not assumed).** `main()`'s empty-roadmap idle branch calls
    `_notify_proposal_test_due()`, which sets `paused_by_user = True` as its *own* natural-
    return mechanism (see this module's top docstring) — so after the *first* successful
    `init`, `state.json` is left with `paused_by_user: true` and `proposal_test_notified:
    true`. Two different things then go wrong on a second, unreset `maestro run` against the
    same project:

    1. `main()`'s *very top* has `if state.get("paused_by_user"): ... while True: ...` — a
       poll loop that exits only on an external Telegram `/resume` or `halted`; leftover
       `paused_by_user: true` routes every subsequent run straight into it, before the main
       event loop (and its empty-roadmap natural return) is ever reached. Reproduced: a second
       `init` against an already-initialized scratch project hung past this check's
       `timeout_s` until this flag was reset first.
    2. Even after clearing `paused_by_user` alone, a second hang remains:
       `_notify_proposal_test_due()` itself starts with `if state.get("proposal_test_notified"):
       return` — a leftover `True` from the first run makes it a no-op on the second, so the
       *empty-roadmap* branch's `paused_by_user = True` (the return mechanism from point 1) is
       never set at all, and `main()`'s `while True:` just `sleep(POLL_INTERVAL); continue`s
       through the same "queue exhausted" branch forever. Also reproduced empirically (fixing
       only `paused_by_user` was not sufficient; this flag had to be cleared too).

    Both are the *correct*, intentional real-world behaviour for a genuinely-idle project
    that already pinged the operator once (D5/D6 don't want a repeat ping every poll); they
    are simply not what `init`'s own verification step is trying to measure. Resetting all
    three flags immediately before the check — and only there, never anywhere else in normal
    operation — makes each `init` run's verification exercise the true empty-roadmap
    natural-return path deterministically, regardless of what a previous run left behind.
    """
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro.state import read_state, write_state
    if not (repo_root / ".orchestrator" / "state.json").is_file():
        return
    state = read_state()
    flags = _LOOP_CHECK_FLAGS
    borrowed = {flag: state.get(flag) for flag in flags if state.get(flag)}
    if borrowed:
        for flag in flags:
            state[flag] = False
        write_state(state)
    return borrowed


def _restore_control_flags(repo_root: Path, borrowed: dict) -> None:
    """Put back whatever `_reset_control_flags_for_loop_check` borrowed.

    Without this, `init` is a way to un-pause a project by accident: the reset above is
    scaffolding for the loop check, and a flag the operator set (`paused_by_user`) or the
    watchdog set (`halted`) is state `init` has no business clearing. Restoring rather
    than skipping the reset keeps the check deterministic *and* leaves the project as it
    was found.
    """
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro.state import read_state, write_state
    if not (repo_root / ".orchestrator" / "state.json").is_file():
        return
    state = read_state()
    # The check itself leaves `paused_by_user`/`proposal_test_notified` set (the idle
    # branch's own return mechanism), so a flag that was not active before must be put
    # back to false too, or a fresh project's first controller idles paused (G4, itv).
    wanted = {flag: bool((borrowed or {}).get(flag)) for flag in _LOOP_CHECK_FLAGS}
    if all(bool(state.get(flag)) == value for flag, value in wanted.items()):
        return
    state.update(wanted)
    write_state(state)


def _discover_project_profile(repo_root: Path):
    """Derive and record the project profile from tracked manifests (`D03`, `A06`).

    Reads files; runs nothing. Unresolved facts are printed rather than guessed at —
    they are the agenda for the operator interview, and a wrong guess about a project's
    test command is a verification gate that proves nothing.
    """
    from maestro.workflows import discovery

    profile = discovery.discover(repo_root)
    print(f"\n[init] project profile: {len(profile.facts)} facts derived from tracked "
          f"manifests (nothing executed)")
    for fact in profile.facts:
        rendered = " ".join(map(str, fact.value)) if isinstance(fact.value, list) else fact.value
        print(f"[init]   {fact.key} = {rendered}  "
              f"({fact.confidence} confidence; {fact.source_path})")
    for unresolved in profile.unresolved:
        print(f"[init]   {unresolved.key}: UNRESOLVED — {unresolved.reason}"
              + (f" {list(unresolved.candidates)}" if unresolved.candidates else ""))
    if profile.unresolved:
        print("[init]   unresolved facts fail closed; the setup interview resolves them.")

    destination = repo_root / ".orchestrator" / "project_profile.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {**profile.to_dict(), "profile_hash": profile.profile_hash},
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[init]   recorded: {destination} (profile_hash {profile.profile_hash[:12]})")
    return profile


def _verify_engineering_policy(repo_root: Path) -> Optional[str]:
    """Load the scaffolded policy. `None` when it holds; a reason when it does not.

    Loading it here rather than at first dispatch is the point: a policy that will refuse
    to compile should say so during `init`, not eight hours later when a task reaches the
    front of the queue.
    """
    from maestro.workflows.policy import PolicyError, ProjectPolicy

    try:
        policy = ProjectPolicy.load(repo_root)
    except PolicyError as exc:
        return f"REFUSED — {exc}"
    print(
        f"[init] engineering policy: objective={policy.objective}, "
        f"{len(policy.templates)} templates, {len(policy.agent_definitions)} agent roles "
        f"({', '.join(sorted(policy.agent_definitions))}), "
        f"policy_hash {policy.policy_hash[:12]}"
    )
    return None


def _check_bound(default_s: int) -> int:
    """A self-check's hang bound: `default_s`, or `$MAESTRO_CHECK_TIMEOUT_S` when that is
    larger (a loaded host, the CLI tests). It bounds a hang, never asserts speed, so the
    environment may only raise it."""
    try:
        raised = int(os.environ.get("MAESTRO_CHECK_TIMEOUT_S") or 0)
    except ValueError:
        raised = 0
    return max(default_s, raised)


def _run_noop_supervised_loop_check(
    repo_root: Path, *, timeout_s: int = 120, tmux_bin: str = "tmux",
) -> tuple[bool, str]:
    """Run `maestro run` against `repo_root` (an empty-roadmap project) to completion, as a
    subprocess so it never shares this process's already-imported, `$MAESTRO_REPO`-bound
    modules. See this module's docstring for why this is expected to return on its own, well
    inside `timeout_s`, and why the timeout is a safety net rather than the actual mechanism."""
    if not _ensure_agents_tmux_session(tmux_bin=tmux_bin):
        return False, f"could not create/find tmux session {_TMUX_SESSION!r} (is tmux installed?)"
    timeout_s = _check_bound(timeout_s)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "maestro.cli", "run", "--repo", str(repo_root)],
            cwd=str(repo_root), env=_subprocess_env(repo_root),
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"orchestrator.main() did not return within {timeout_s}s — see this module's "
            f"docstring for the expected natural-return mechanism; something broke it"
        )
    detail = (proc.stdout + proc.stderr)[-2000:]
    return proc.returncode == 0, detail


def _register_shared_models(repo_root: Path) -> str:
    """Join the explicit shared-model registry, so later policy switches reach this project.

    Best-effort, like Telegram setup: a project that cannot be registered still initialises,
    and `maestro models register` can add it later.
    """
    from maestro.model_inventory import register_project
    from maestro.model_policy import PolicyError
    try:
        register_project(repo_root)
    except (PolicyError, OSError) as exc:
        return f"not registered ({exc}); run `maestro models register` later"
    return "registered"


def cmd_init(
    repo_root: Path, *,
    telegram_script_override: Optional[Path] = None,
    mermaid_script_override: Optional[Path] = None,
) -> int:
    """`maestro init [path]` — DESIGN.md §10. Idempotent (see `_scaffold_file`); Telegram setup
    and mermaid-cli installation are both best-effort (see `_run_telegram_creds` and
    `_run_ensure_mermaid` respectively — same shape, same "never fail init over it" contract);
    ends with a dry-run `status` and the no-op supervised loop check before reporting success
    (see this module's docstring)."""
    repo_root = repo_root.resolve()
    from maestro import bootstrap
    print(f"[init] scaffolding from maestro checkout {bootstrap.running_root()}")
    invoked = bootstrap.redirected_from()
    if invoked is not None:
        print(f"[init] WARNING: invoked from {invoked}, but the adopted pin re-exec'd this "
              f"run into the checkout above; its templates may be older. Re-run with "
              f"{bootstrap.SENTINEL}=1 to scaffold from the invoked checkout.")

    if not (repo_root / ".git").exists() and not _git(repo_root, "rev-parse", "--git-dir"):
        print(f"[init] {repo_root} is not a git repository — run `git init` first.")
        return 1

    facts = _derive_repo_facts(repo_root)
    print(f"[init] project={facts['project_name']!r} repo={facts['repo_path']} "
          f"branch={facts['main_branch']!r} remote={facts['remote'] or '(none)'}")
    if facts["test_command"]:
        print(f"[init] detected test command: {facts['test_command']} ({facts['package_manager']})")

    mapping = {
        "project_name": facts["project_name"],
        "repo_path": facts["repo_path"],
        "main_branch": facts["main_branch"],
        # A fresh scaffold has no risky_set/deny_list_extra yet (project.yaml.tmpl renders
        # both empty) — the setup skill (Component C) fills these in from the operator
        # interview. Once it has, a re-run of init renders the real list (F5), and
        # `doctor`'s `preamble_deny_list` check fails while the placeholder outlives it.
        "deny_list": _deny_list_block(repo_root),
        "test_command_argv_json": json.dumps(facts["test_command_argv"]),
    }
    mapping.update(_systemd_service_mapping(facts))

    outcomes: list[ScaffoldOutcome] = []

    outcomes.append(_scaffold_file(
        repo_root / "project.yaml",
        _render((TEMPLATES_DIR / "project.yaml.tmpl").read_text(encoding="utf-8"), mapping),
    ))

    # The *engineering* policy (P5), distinct from the operational `project.yaml` above:
    # which workflow shapes this project allows, and the quality gates a composed graph
    # must satisfy. Scaffolded under the same non-destructive contract — an edited copy
    # gets a `.new` and a diff, never an overwrite. Role definitions are deliberately not
    # copied: they ship with the package so a project picks up corrections, and a project
    # that wants its own drops a file into `workflows/roles/` to shadow one.
    outcomes.append(_scaffold_file(
        repo_root / "workflows" / "project.yaml",
        (TEMPLATES_DIR / "workflows" / "project.yaml").read_text(encoding="utf-8"),
    ))

    for kind_path in sorted((TEMPLATES_DIR / "adapters").iterdir()):
        content = kind_path.read_text(encoding="utf-8")
        if kind_path.name == "test":
            content = _render(content, mapping)
        outcomes.append(_scaffold_file(
            repo_root / "adapters" / kind_path.name,
            content,
            executable=True,
        ))

    outcomes.append(_scaffold_file(
        repo_root / "operating_preamble.md",
        _render((TEMPLATES_DIR / "operating_preamble.md").read_text(encoding="utf-8"), mapping),
    ))

    for profile_path in sorted((TEMPLATES_DIR / "profiles").iterdir()):
        outcomes.append(_scaffold_file(
            repo_root / "profiles" / profile_path.name,
            _render(profile_path.read_text(encoding="utf-8"), mapping),
        ))

    for doc_name in ("ROADMAP.md", "PROJECT.md"):
        outcomes.append(_scaffold_file(
            repo_root / "docs" / doc_name,
            _render((TEMPLATES_DIR / "docs" / doc_name).read_text(encoding="utf-8"), mapping),
        ))

    outcomes.append(_scaffold_file(
        repo_root / ".git" / "hooks" / "pre-commit",
        (TEMPLATES_DIR / "pre-commit").read_text(encoding="utf-8"),
        executable=True,
    ))

    outcomes.append(_scaffold_file(
        repo_root / "launch.sh",
        _render((TEMPLATES_DIR / "launch.sh.tmpl").read_text(encoding="utf-8"), mapping),
        executable=True,
    ))
    outcomes.append(_scaffold_file(
        repo_root / "systemd" / f"{facts['project_name']}-watchdog.service",
        _render(
            (TEMPLATES_DIR / "systemd" / "maestro-watchdog.service.tmpl").read_text(encoding="utf-8"),
            mapping,
        ),
    ))

    outcomes.append(_scaffold_gitignore(repo_root / ".gitignore"))

    # `-p` config `maestro.docs.depmap.render_png()` passes to `mmdc` when it renders
    # docs/dependency_map.md's mermaid block — scaffolded regardless of whether `mmdc` itself
    # is installed yet (see the ensure-mermaid step below), so it's already in place the
    # moment it becomes available.
    outcomes.append(_scaffold_file(repo_root / ".mermaid" / "puppeteer-config.json", _puppeteer_config_json()))

    outcomes.extend(_ensure_orchestrator_dirs(repo_root))
    _ensure_venv_python_symlink(repo_root)

    from maestro import metabranch
    meta_detail = metabranch.reconcile_meta(repo_root)
    print(f"[init] meta branch (process docs): {meta_detail}")

    created = [o for o in outcomes if o.action == "created"]
    unchanged = [o for o in outcomes if o.action == "unchanged"]
    written_new = [o for o in outcomes if o.action == "new_written"]
    print(f"[init] scaffold: {len(created)} created, {len(unchanged)} unchanged, "
          f"{len(written_new)} written as .new (nothing already present was overwritten)")
    for o in written_new:
        print(f"\n[init] {o.dest} already exists and differs — wrote {o.dest}.new instead:")
        print(o.diff)

    print(f"[init] shared model policy: {_register_shared_models(repo_root)}")

    telegram_result = _run_telegram_creds(repo_root, script_override=telegram_script_override)
    print(f"[init] Telegram setup: {telegram_result}")

    mermaid_result = _run_ensure_mermaid(repo_root, script_override=mermaid_script_override)
    print(f"[init] mermaid-cli (dependency-map PNG rendering): {mermaid_result}")

    profile = _discover_project_profile(repo_root)

    policy_detail = _verify_engineering_policy(repo_root)
    if policy_detail is not None:
        print(f"[init] engineering policy: {policy_detail}")
        return 1

    print("\n[init] dry-run `maestro status`:")
    status_rc = cmd_status(repo_root)
    if status_rc != 0:
        print("[init] status check reported a problem (see above) — continuing anyway.")

    print("\n[init] no-op supervised loop check (empty roadmap, zero runnable tasks)...")
    borrowed_flags = _reset_control_flags_for_loop_check(repo_root)
    try:
        loop_ok, loop_detail = _run_noop_supervised_loop_check(repo_root)
    finally:
        # P5 acceptance: "rerunning `maestro init` leaves active flags and existing files
        # untouched". The reset above is the loop check's own fixture, not a policy
        # decision about this project — a project the operator had paused or halted is
        # still paused or halted once the check has run.
        _restore_control_flags(repo_root, borrowed_flags)
    if loop_ok:
        print("[init] no-op supervised loop completed cleanly (returncode 0).")
    else:
        print(f"[init] no-op supervised loop check FAILED:\n{loop_detail}")
        print("[init] scaffolding was written, but the loop verification did not pass.")
        return 1

    unit_path = repo_root / "systemd" / f"{facts['project_name']}-watchdog.service"
    print(
        f"\n[init] done. To install the systemd unit for real (requires root — this tool never "
        f"runs sudo itself):\n"
        f"    sudo cp {unit_path} /etc/systemd/system/\n"
        f"    sudo systemctl daemon-reload\n"
        f"    sudo systemctl enable --now {facts['project_name']}-watchdog.service"
    )
    return 0


# ══════════════════════════════════════════════════════════════════════════
# doctor
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = False


def _check_docs(repo_root: Path) -> Check:
    roadmap = repo_root / "docs" / "ROADMAP.md"
    project_doc = repo_root / "docs" / "PROJECT.md"
    missing = [str(p) for p in (roadmap, project_doc) if not p.is_file()]
    if missing:
        return Check("docs", False, f"missing: {', '.join(missing)}", required=True)
    try:
        from maestro.docs.roadmap import parse_prep_tasks, parse_runnable_tasks
        from maestro.state import read_state_projection
        runnable = parse_runnable_tasks()
        try:
            # The exported projection: doctor runs beside a live controller and must never
            # compete for its lock.
            prep = parse_prep_tasks(read_state_projection())
        except (ValueError, AttributeError) as exc:
            return Check("docs", False, f"state.json did not parse: {exc}", required=True)
        except FileNotFoundError:
            # `.orchestrator/state.json` is gitignored runtime state that exists only in
            # the main checkout, never in a `git worktree` — and every implementer/script
            # task commits from one, with `doctor --pre-commit` (this hook's fast subset)
            # run against that same worktree. `parse_prep_tasks()` needs state.json to
            # cross-reference waiting_on_dan/parked ids; its absence there is structural,
            # not project damage, so degrade to "prep count unknown" instead of failing
            # the whole docs check (M5SCRATCH1 incident, 2026-08-20 — see PROGRESS.md:
            # this crash blocked every worktree commit, i.e. every task, unconditionally).
            prep = []
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a broken project
        return Check("docs", False, f"docs/ROADMAP.md did not parse: {exc}", required=True)
    return Check("docs", True, f"parsed OK — {len(runnable)} runnable, {len(prep)} prep task(s)",
                 required=True)


def _check_preamble_deny_list(repo_root: Path) -> Check:
    """Required: `operating_preamble.md` must not still say the deny list is empty once
    `project.yaml` has one (F5). The preamble is rendered by `init`, which usually runs
    before the setup interview fills in `risky_set` / `deny_list_extra`, so without this an
    agent is told "(none yet)" about exactly the files it must not touch."""
    entries = _deny_list_entries(_repo_config(repo_root))
    preamble = repo_root / "operating_preamble.md"
    try:
        stale = _DENY_LIST_PLACEHOLDER in preamble.read_text(encoding="utf-8")
    except OSError:
        stale = False
    if entries and stale:
        return Check(
            "preamble_deny_list", False,
            f"project.yaml lists {len(entries)} deny-list entr{'y' if len(entries) == 1 else 'ies'} "
            f"but {preamble.name} still shows the placeholder — re-run `maestro init` and merge "
            f"{preamble.name}.new",
            required=True,
        )
    detail = (f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} rendered" if entries
              else "project.yaml has no risky_set / deny_list_extra yet")
    return Check("preamble_deny_list", True, detail, required=True)


def _check_meta_branch(repo_root: Path) -> Check:
    """Not required (WARN, not FAIL): a stale or missing `meta` branch/worktree means
    process docs (handoffs, plans, progress, lessons) aren't syncing anywhere but the
    main tree's disk — a real gap, but not one that should block a project's gate.
    `reconcile_meta` self-heals on the next `init`/`doctor`/orchestrator-startup run,
    so this check's job is just to make drift visible, matching the register the other
    doctor checks use."""
    from maestro import metabranch
    detail = metabranch.reconcile_meta(repo_root)
    ok = "error" not in detail and "failed" not in detail.lower()
    return Check("meta_branch", ok, detail)


def _check_adapters(repo_root: Path) -> list[Check]:
    from maestro import adapters
    checks = []
    for kind in adapters.KNOWN_KINDS:
        result = adapters.run_adapter(kind, repo_root, payload={}, timeout_s=_check_bound(60))
        required = kind in adapters.REQUIRED_KINDS
        if required:
            ok = result.status == "ok"
        else:
            # absent is the normal, honest "not wired up yet" state (D8) for an optional
            # adapter; only a present-but-broken script (bad output, hang, crash) nags.
            ok = result.status in ("ok", "absent")
        detail = f"status={result.status}"
        if result.detail:
            detail += f" — {result.detail[:200]}"
        checks.append(Check(f"adapter:{kind}", ok, detail, required=required))
    return checks


def _role_model_pairs(repo_root: Path) -> set[tuple[str, str]]:
    """Every `(backend, model)` a role would run. A role states no model (DESIGN.md §17.2),
    so these are the derived ones (`model_policy.runtime_roles`). Raises `PolicyError`."""
    from maestro import config as _config
    from maestro import model_policy as _model_policy

    derived = _model_policy.runtime_roles(_config.load_project_yaml(), repo_root)
    return {(backend, model) for role in derived["roles"].values()
            for backend, model in role["models"].items()}


def _check_model_limits(repo_root: Path) -> Check:
    import warnings as _warnings
    from maestro import limits
    from maestro.model_policy import PolicyError
    try:
        # The backend picks the table's row prefix.
        names = {limits.scoped_model_name(model, backend)
                 for backend, model in _role_model_pairs(repo_root)}
    except PolicyError as exc:
        return Check("model_limits", False, f"role models could not be derived: {exc}")
    if not names:
        return Check("model_limits", True, "no role has a model — nothing to resolve")
    # `resolve_all` returns a plain `{name: ModelLimits | None}` dict (unlike `load_limits`,
    # which wraps its result in a `LimitsResult`) and raises a `UserWarning` per unresolved
    # name — caught here so `doctor`'s own output stays one line per check.
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        resolved = limits.resolve_all(sorted(names))
    unresolved = [name for name, entry in resolved.items() if entry is None]
    if unresolved:
        return Check("model_limits", False, f"no context-limit entry for: {', '.join(unresolved)}")
    detail = f"resolved {len(names)} model(s)"
    if caught:
        detail += f"; {len(caught)} warning(s)"
    return Check("model_limits", True, detail)


# ── doctor: model_ids (C7 part 2) ──
#
# `_check_model_limits` above only ever asks `maestro.limits` whether a ceiling-table
# *row* exists for a configured model name — it never asks the backend's own CLI whether
# the model id itself is one it recognises. A typo (`claude-sonnet-5-typo`) has no row
# either, so it would already fail `_check_model_limits` — but a *real*, differently-typo'd
# id that happens to collide with an unrelated table row would sail through it and only
# surface later as `claude -p --model <typo>` crashing an implementer mid-task. This check
# asks the CLI directly, closing that gap independently of the ceiling table.
#
# **Cheap and offline-tolerant by construction.** Both real CLIs (verified by hand while
# building this check, see `docs/plans/2026-08-30-pre-integration-c7-report.md`) print a
# client-side "I don't recognise this model" marker within a couple of seconds, *before*
# attempting any network call —
# offline or online makes no difference to whether that marker appears. A **valid** model,
# by contrast, prints nothing distinguishing before the CLI moves on to its real (network)
# turn, which hangs forever with no network. So the probe is bounded by
# `MODEL_PROBE_TIMEOUT_SEC` and kills the subprocess at that bound no matter what it was
# doing, and the interpretation is asymmetric on purpose: the marker's *presence* is proof
# the model is bad; its *absence* — including a timeout, meaning "offline, or just slow" —
# proves nothing, and is never treated as a failure. A doctor check that fails on every
# unlucky network hiccup would be worse than the typo it exists to catch.

#: How long a model-id probe subprocess gets before it is killed. Comfortably above the
#: ~2-4s both CLIs took (on this machine, online and offline alike) to print their own
#: "unrecognised model" marker; comfortably below what a real turn would take, so an
#: unreachable network — which makes the real CLI hang rather than fail — cannot stall
#: `doctor` for long.
MODEL_PROBE_TIMEOUT_SEC = 8.0

#: Total wall-clock ceiling for `_check_model_ids`' whole loop, across every configured
#: `(backend, model)` pair. `MODEL_PROBE_TIMEOUT_SEC` only bounds one probe — with no
#: aggregate cap, cost still grows linearly and without limit as an operator adds roles
#: or a third backend (review round 1, 2026-09-01: the real `templates/project.yaml.tmpl`
#: roster already carries four distinct pairs, not the two this check was first measured
#: against, so a fully offline `doctor` run cost 4 * 8s = 32s with nothing stopping a
#: fifth or sixth pair from adding another 8s each). Set to exactly two probe timeouts:
#: `_check_model_ids` checks this budget *before* starting each probe, so at most two full
#: `MODEL_PROBE_TIMEOUT_SEC`-length probes ever run in one `doctor` invocation, however
#: many pairs are configured — the remainder are reported unchecked rather than probed.
MODEL_PROBE_BUDGET_SEC = 2 * MODEL_PROBE_TIMEOUT_SEC

#: Per-backend: the flags (everything *after* the binary name) that ask its CLI about one
#: model id as cheaply as possible — a minimal one-word turn, always killed well before it
#: could run for real — and the substring in its output that means "no, I don't know this
#: model", printed locally, before any network attempt, by that backend's own CLI. A
#: backend with no entry here is skipped by `_probe_model_id` rather than guessed at.
#:
#: Deliberately **not** a full `["claude", "-p", ...]` argv: `tests/test_one_shot_agent_
#: calls.py` bans a literal backend name heading an argv list anywhere outside a driver,
#: because that shape is what a *feature* looks like when it bypasses `maestro.agentcall`
#: and hardcodes one backend. This probe is not that — it deliberately targets one named
#: backend's own CLI to validate a model id configured *for that backend specifically* —
#: but the fix is the same either way: the binary name comes from `registry.driver_ref`,
#: not a literal here, exactly like `_check_backends` above already resolves it.
_MODEL_PROBE_FLAGS: dict[str, Callable[[str], list[str]]] = {
    "claude": lambda model: ["-p", "--model", model, "ping"],
    "codex": lambda model: ["exec", "--model", model, "ping"],
}
_MODEL_PROBE_INVALID_MARKER: dict[str, str] = {
    "claude": "claude-code:unrecognized_model",
    "codex": "Model metadata for",
}


def _run_model_probe(argv: list[str], *, timeout: float) -> str:
    """Real subprocess runner for `_probe_model_id`. Stdin is closed (`DEVNULL`) so a CLI
    that would otherwise wait on interactive input fails fast instead of hanging for a
    different reason. Always bounded by `timeout` and never raises: a `TimeoutExpired` is
    caught and whatever text the process had already produced is returned (subprocess
    captures partial output up to the kill, even though `text=True` does not decode it in
    that path — decoded here by hand), and a missing binary or any other `OSError`
    degrades to `""`, same as "no marker seen". The only thing in this module that
    executes an agent CLI; `_probe_model_id`'s `run` parameter lets every test replace it
    (no test may execute a real agent CLI, `maestro/backends/registry.py`'s rule)."""
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out, err = exc.stdout or "", exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return out + err
    except OSError:
        return ""


def _probe_model_id(
    backend: str, model: str, *, run: Optional[Callable[..., str]] = None,
) -> tuple[bool, str]:
    """Ask `backend`'s own CLI whether it recognises `model`. Returns `(invalid, detail)`:
    `invalid=True` only when that CLI's own client-side rejection marker was seen in the
    probe's output — every other outcome (an unsupported backend, a timeout, a clean exit
    with nothing recognisable) reports `invalid=False`, because none of those is proof the
    model is bad; this machine could simply be offline. `run` defaults to the real
    `_run_model_probe` and is the seam every test injects a stub through."""
    from maestro.backends import registry as _registry

    build_flags = _MODEL_PROBE_FLAGS.get(backend)
    marker = _MODEL_PROBE_INVALID_MARKER.get(backend)
    if build_flags is None or marker is None:
        return False, f"{backend}: no probe defined for this backend — skipped"
    try:
        binary = _registry.driver_ref(backend).binary
    except _registry.UnknownBackend:
        return False, f"{backend}: not a registered backend — skipped"
    argv = [binary] + build_flags(model)
    output = (run or _run_model_probe)(argv, timeout=MODEL_PROBE_TIMEOUT_SEC)
    if marker in output:
        return True, f"{backend} does not recognise model id {model!r}"
    return False, ""


def _check_model_ids(repo_root: Path, *, run: Optional[Callable[..., str]] = None) -> Check:
    """`doctor`'s C7 check: probe each `(backend, model)` pair a role would run against
    that backend's own CLI, so an id the CLI rejects fails here instead of reaching
    `claude -p --model <id>` mid-task. A role states no model (DESIGN.md §17.2), so the
    pairs are the derived ones (`model_policy.runtime_roles`), on the backends that have a
    probe. `run` is `_probe_model_id`'s injectable subprocess seam (default: the real
    `_run_model_probe`); tests always pass a stub, per this module's "no test may execute
    a real agent CLI" rule.

    **Not `required`** (`Check.required` defaults to `False`, unset here exactly like the
    pre-existing `_check_model_limits`), which is what makes this check's known blind spot
    tolerable rather than merely disclosed: a false positive from either CLI's own
    allowlist marker firing on a model it simply doesn't have cached metadata for yet
    (observed for Codex; nothing rules out the identical failure mode for Claude's
    `claude-code:unrecognized_model` marker, which is the same class of client-side
    allowlist) prints as `[WARN]`, not `[FAIL]` — it never trips `cmd_doctor`'s
    `if check.required and not check.ok` and never fails the pre-commit fast path (this
    check does not run under `--pre-commit` at all).

    **Bounded, and honest about what "clean" means.** The budget is checked *before* each
    probe starts, not enforced while one runs, so a probe that begins at
    budget-minus-epsilon still runs its full `MODEL_PROBE_TIMEOUT_SEC`: the true
    supremum for one invocation is `MODEL_PROBE_BUDGET_SEC + MODEL_PROBE_TIMEOUT_SEC`
    (16 + 8 = ~24s today), not `MODEL_PROBE_BUDGET_SEC` alone. The aggregate cost is
    still genuinely O(1) in roster size and offline-tolerant — pairs beyond what the
    budget admits are reported unchecked, not probed — only the stated ceiling was
    short by one timeout. And because this probe can only ever *prove*
    a model id is bad (its own CLI's rejection marker appeared) and never prove one is
    good (a valid model gives no distinguishing signal before its real, network-bound turn
    — see `_probe_model_id`), a clean result says exactly that: 0 confirmed invalid among
    however many were actually checked, not "N confirmed working"."""
    from maestro.model_policy import PolicyError
    try:
        pairs = {pair for pair in _role_model_pairs(repo_root) if pair[0] in _MODEL_PROBE_FLAGS}
    except PolicyError as exc:
        return Check("model_ids", False, f"role models could not be derived: {exc}")
    if not pairs:
        return Check("model_ids", True, "no role has a model on a backend with a probe — nothing to probe")

    invalid: list[str] = []
    notes: list[str] = []
    unchecked: list[str] = []
    start = time.monotonic()
    for backend, model in sorted(pairs):
        if time.monotonic() - start >= MODEL_PROBE_BUDGET_SEC:
            unchecked.append(f"{backend}:{model}")
            continue
        bad, detail = _probe_model_id(backend, model, run=run)
        if bad:
            invalid.append(f"{backend}:{model}")
        elif detail:
            notes.append(detail)

    if invalid:
        return Check("model_ids", False, f"rejected by their own CLI: {', '.join(invalid)}")

    checked = len(pairs) - len(unchecked)
    detail = (
        f"0 confirmed invalid among {checked} checked (a clean probe rules out a "
        "known-bad id, it does not confirm a working one)"
    )
    if unchecked:
        detail += f"; {len(unchecked)} not checked (probe time budget exhausted): {', '.join(unchecked)}"
    if notes:
        detail += f"; {len(notes)} note(s): {'; '.join(notes)}"
    return Check("model_ids", True, detail)


def _check_backends(repo_root: Path) -> Check:
    from maestro import config as _config
    from maestro.backends import registry as backend_registry
    from maestro.model_policy import harness_drivers
    # Every harness a role may run on or switch to needs a working binary. A role names no
    # backend (DESIGN.md §17.2): the harnesses are the registry's default chain.
    backend_names = set(harness_drivers(_config.load_project_yaml()))
    missing = []
    for name in sorted(backend_names):
        info = backend_registry.backend_binary(name)
        if info is None:
            missing.append(name)
    if missing:
        return Check("backends", False, f"binary not found on PATH for: {', '.join(missing)}")
    return Check("backends", True, f"resolved: {', '.join(sorted(backend_names))}")


def _check_model_policy(repo_root: Path) -> Check:
    """Shared model policy evidence (`model_inventory.project_report`): adopted revision
    and its state, effective role pairs with provenance, invalid routes and open detection
    proposals. Optional — it warns, never fails doctor — and read-only: it creates no
    shared state and adopts nothing."""
    from maestro.model_inventory import project_report
    from maestro.model_policy import PolicyError
    try:
        report = project_report(repo_root)
    except PolicyError as exc:
        return Check("model_policy", False, f"unreadable: {exc}")
    shared = report["shared_revision"]
    parts = [f"shared {shared[:12]}" if shared else "shipped defaults (no shared policy published)"]
    adopted, adoption = report.get("adopted_revision"), report["adoption"]
    warn = []
    if adoption == "unreadable":
        warn.append(f"adoption unreadable: {report.get('reason')}")
    elif adopted:
        parts.append(f"adopted {adopted[:12]} ({adoption}"
                     + ("; adopts at its next idle boundary)" if adoption == "behind" else ")"))
        if adoption == "behind":
            warn.append("behind")
    elif shared:
        warn.append("not adopted" + ("" if report["registered"] else "; not registered (maestro models register)"))
    if report.get("config") == "invalid":
        warn.append(f"project.yaml invalid: {report.get('reason')}")
    if report["reader"] != "supported":
        warn.append("installed version lacks the shared policy reader (upgrade required)")
    for role, entry in sorted((report.get("roles") or {}).items()):
        if 'models' not in entry:
            requirement = (f'intelligence >= {entry["min_intelligence"]}; effort {entry["effort"]} '
                           f'({entry["effort_rule"]})') if 'min_intelligence' in entry else f'class {entry.get("strength", "default")}'
            parts.append(f'{role} {requirement}')
            continue
        drivers = sorted(entry["models"], key=lambda d: (d != entry["backend"], d))
        parts.append(f"{role} " + ", ".join(
            f"{d}:{entry['models'][d]['model']} [{entry['models'][d]['origin']}]" for d in drivers))
    for role, bad in sorted((report.get("invalid_routes") or {}).items()):
        for driver, reason in sorted(bad.items()):
            warn.append(f"invalid {role}.{driver} ({reason})")
    proposals = report["open_proposals"]
    if proposals:
        warn.append(f"{len(proposals)} open proposal{'s' if len(proposals) != 1 else ''}: " + ", ".join(
            f"{':'.join(p['destination'])} ({p['status']}; "
            + (f"modelctl add {p['destination'][0]} --model {p['destination'][1]})"
               if p['status'] == 'waiting_limits' else f"maestro models review --proposal {p['proposal_id']})")
            for p in proposals))
    for role, choices in report.get('class_choices', {}).items():
        parts.extend(f"class {role}.{driver} {choice['family'] or choice['choice']} -> {choice['model']}"
                     for driver, choice in choices.items())
    for switch in report.get('switches', []):
        text = f"switch {':'.join(switch['destination'])} {switch['status']}"
        if switch['notification_pending']:
            warn.append(text + '; notice pending')
        else:
            parts.append(text + '; notice settled')
    return Check("model_policy", not warn, "; ".join(parts + warn))


def _default_http_get(url: str) -> dict:
    """Real Telegram `getMe` call. Never used in a test — `cmd_doctor`'s `http_get` parameter
    exists specifically so a test can replace this with a stub."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def _check_telegram(repo_root: Path, http_get: Callable[[str], dict]) -> Check:
    import dotenv
    from maestro.hitl.shared_bot import env_path as shared_env_path
    env_path = shared_env_path()
    if not env_path.is_file():
        return Check("telegram", True, "no shared .env — Telegram not configured (optional)")
    values = dotenv.dotenv_values(env_path)
    token = values.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return Check("telegram", True, "no TELEGRAM_BOT_TOKEN in shared .env — skipped (optional)")
    response = http_get(f"https://api.telegram.org/bot{token}/getMe")
    if response.get("ok"):
        return Check("telegram", True, "getMe OK")
    return Check("telegram", False, f"getMe failed: {response}")


def _check_mermaid(repo_root: Path) -> Check:
    """Optional — a missing `mmdc` only means `docs/dependency_map.png` stays stale/absent
    (`maestro.docs.depmap.render_png()` swallows the failure, see `maestro/docs/roadmap.py`'s
    `run_dep_map`), never that orchestration itself is broken. Checks the same two locations
    `scripts/ensure_mermaid.sh` and `maestro.docs.depmap.MMDC` do: global PATH first, then a
    legacy per-repo `.mermaid/node_modules/.bin/mmdc`."""
    if shutil.which("mmdc"):
        return Check("mermaid", True, f"mmdc on PATH: {shutil.which('mmdc')}")
    local = repo_root / ".mermaid" / "node_modules" / ".bin" / "mmdc"
    if local.is_file() and os.access(local, os.X_OK):
        return Check("mermaid", True, f"mmdc installed locally: {local}")
    return Check(
        "mermaid", False,
        "mmdc not found (global or local) — dependency_map.png will not render; "
        "run `scripts/ensure_mermaid.sh` or re-run `maestro init`",
    )


def _check_systemd_unit(repo_root: Path, project_name: str) -> Check:
    systemd_dir = Path(os.environ.get("MAESTRO_SYSTEMD_DIR", "/etc/systemd/system"))
    unit = systemd_dir / f"{project_name}-watchdog.service"
    if unit.is_file():
        return Check("systemd_unit", True, f"installed at {unit}")
    return Check("systemd_unit", False, f"not installed at {unit} (see `maestro init`'s final message)")


def _check_gate(repo_root: Path) -> Check:
    from maestro import config as _config
    cfg = _config.load_project_yaml()
    gate = cfg.get("gate")
    if not isinstance(gate, dict) or "chain" not in gate:
        return Check("gate", False, "no gate.chain configured in project.yaml")
    chain = gate.get("chain")
    if chain == ["test"]:
        return Check("gate", True,
                      "gate.chain == ['test'] — project is running unevaluated; "
                      "eval/smoke are not wired into the gate yet (D8)")
    return Check("gate", True, f"gate.chain={chain}")


def _check_journal_and_version(repo_root: Path) -> Check:
    from maestro.state import JOURNAL, STATE_JSON
    if not STATE_JSON.is_file():
        return Check("journal_version", False, f"no state.json at {STATE_JSON} — run `maestro init`")
    try:
        state = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("journal_version", False, f"state.json did not parse: {exc}")
    if not isinstance(state, dict):
        return Check("journal_version", False, "state.json did not parse: not a mapping")
    version = state.get("maestro_version")
    details = [f"maestro_version={version or '(never self-updated)'}"]
    if JOURNAL.is_file():
        bad = 0
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-20:]
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                bad += 1
        details.append(f"journal: {len(lines)} recent line(s) sampled, {bad} unparseable")
        ok = bad == 0
    else:
        details.append("journal: none yet (project has never run)")
        ok = True
    return Check("journal_version", ok, "; ".join(details))


def _check_retention(repo_root: Path) -> Check:
    """B15: how much `.orchestrator` holds against `retention.warn_bytes`. Optional.

    Task worktrees are left out: they are released on accept/park and are not evidence.
    """
    from maestro import config as _config
    try:
        warn = _config.retention(_repo_config(repo_root))["warn_bytes"]
    except _config.ConfigError as exc:
        return Check("retention", False, str(exc))
    orch = repo_root / ".orchestrator"
    used = 0
    if orch.is_dir():
        for child in orch.iterdir():
            if child.name == "worktrees":
                continue
            paths = [child] if child.is_file() else (p for p in child.rglob("*") if p.is_file())
            for path in paths:
                try:
                    used += path.stat().st_size
                except OSError:
                    pass
    detail = f".orchestrator holds {used} bytes (retention.warn_bytes={warn}; nothing is pruned)"
    return Check("retention", used <= warn, detail)


def _check_config_keys(repo_root: Path) -> Check:
    """P13 W1: no unknown top-level key in `project.yaml` or the global config. Required.

    A typo in either file would otherwise fall back to a default without a word.
    """
    from maestro import config as _config
    global_path = _config.global_config_path()
    problems = []
    problem = _config.global_config_problem()
    if problem:
        return Check("config_keys", False, problem, required=True)
    for name, document in ((str(global_path), _config.load_global_config()),
                           ("project.yaml", _repo_project_yaml(repo_root))):
        unknown = _config.unknown_keys(document)
        if unknown:
            problems.append(f"{name}: unknown keys {unknown}")
    if problems:
        return Check("config_keys", False, "; ".join(problems), required=True)
    layer = str(global_path) if global_path.exists() else "no global config"
    return Check("config_keys", True, f"every top-level key is known (global: {layer})",
                 required=True)


def _check_priority(repo_root: Path) -> Check:
    from maestro import config as _config
    try:
        weight = _config.project_priority(_repo_config(repo_root))
    except _config.ConfigError as exc:
        return Check('priority', False, str(exc), required=True)
    return Check('priority', True, f'effective weight {weight} (1 = normal)', required=True)


def _check_goals(repo_root: Path) -> Check:
    """P13 W11a/GDT-2: the goals contract. A failure (required) when the execution gate
    would hold an open task (R31: an unclassified task, an unknown id, a gate problem on
    what it serves), naming each; otherwise a warning for what drafting still lacks."""
    from maestro import taskgraph
    from maestro.docs import goals as doc_goals

    doc = doc_goals.load(repo_root)
    if doc is None:
        return Check("goals", False, f"no {doc_goals.DOC} (grill-me writes it; "
                     "maestro-setup asks the goal questions when it is missing)")
    roadmap = repo_root / "docs" / "ROADMAP.md"
    tasks = taskgraph.scan_tasks(roadmap.read_text(encoding="utf-8")) if roadmap.is_file() else []
    try:
        completed = json.loads((repo_root / ".orchestrator" / "completed_tasks.json").read_text())
    except (OSError, ValueError):
        completed = []
    completed = [e for e in completed if isinstance(e, dict)] if isinstance(completed, list) else []
    from maestro import goal_roadmap, goal_state

    holds = doc_goals.execution_holds(doc, tasks)
    held = {p.text for problems in holds.values() for p in problems}
    problems = [f"GOALS.md {p.text}" if p in doc.issues else p.text
                for p in doc_goals.diagnose(doc, tasks, completed) if p.text not in held]
    # GDT-9: the baseline overlaid by what the gate and checkpoints observed since, so a
    # criterion its graduated direct task turned GREEN is no false "no open task" FAIL.
    infeasible = goal_roadmap.feasibility(doc, tasks, goal_state.results(repo_root, doc))
    contract = goal_state.contract_problems(repo_root, doc)
    if holds or infeasible or contract:
        parts = [f"blocks at execution: {doc_goals.describe_holds(holds)}"] if holds else []
        if infeasible:
            parts.append(f"not feasible: {'; '.join(p.text for p in infeasible)}")
        if contract:
            parts.append(f"contract: {'; '.join(contract)}")
        also = f"; also: {'; '.join(problems)}" if problems else ""
        return Check("goals", False, "; ".join(parts) + also, required=True)
    if problems:
        return Check("goals", False, "; ".join(problems))
    return Check("goals", True, f"{len(doc.ids)} goals, each served; every task names its goals")


def _check_roadmap_blocks(repo_root: Path) -> Check:
    """A required failure for a malformed `yaml` block in the ROADMAP. Every reader skips one
    silently, so its task would vanish. A block with no `id` is left alone, since a ROADMAP may
    hold examples."""
    from maestro import taskgraph

    roadmap = repo_root / "docs" / "ROADMAP.md"
    if not roadmap.is_file():
        return Check("roadmap blocks", True, "no ROADMAP")
    document = taskgraph.load_document(roadmap)
    malformed = [str(issue) for issue in taskgraph.validate(document)
                 if issue.code == "malformed_yaml"]
    if malformed:
        return Check("roadmap blocks", False, "; ".join(malformed), required=True)
    count = len(taskgraph.iter_yaml_blocks(roadmap.read_text(encoding="utf-8")))
    return Check("roadmap blocks", True, f"{count} blocks parse")


def _check_task_labels(repo_root: Path) -> Check:
    """A warning for open tasks whose class or risk would be read as a default. An unlabelled
    task pollutes the `feature` statistics that route comparison and runaway detection use."""
    from maestro import taskgraph

    roadmap = repo_root / "docs" / "ROADMAP.md"
    tasks = taskgraph.scan_tasks(roadmap.read_text(encoding="utf-8")) if roadmap.is_file() else []
    problems = []
    for task in tasks:
        if str(task.get("status") or "") == "complete":
            continue
        label = task.get("task_class")
        if label is None:
            problems.append(f"{task['id']}: no task_class")
        elif label not in taskgraph.TASK_CLASSES:
            problems.append(f"{task['id']}: unknown task_class {label!r}")
        risk = task.get("risk")
        if risk is not None and risk not in taskgraph.KNOWN_RISKS:
            problems.append(f"{task['id']}: unknown risk {risk!r}")
    if problems:
        return Check("task labels", False, "; ".join(problems))
    return Check("task labels", True, "every open task names a known class")


def _check_role_shape(repo_root: Path) -> Check:
    """P13 W1: a role states only `strength` and `independent_of` (DESIGN.md §17.2). Required.

    The retired keys (`backend`, `model`, `models`, `effort`, top-level `fallback_chain`)
    are no longer read, so a file that still holds them is not saying what it looks like
    it says. Each config layer is checked on its own, so the message names the file.
    """
    from maestro import config as _config
    from maestro.model_policy import role_shape_issues

    problems = []
    for name, document in ((str(_config.global_config_path()), _config.load_global_config()),
                           ("project.yaml", _repo_project_yaml(repo_root))):
        issues = role_shape_issues(document) if isinstance(document, dict) else []
        problems.extend(f"{name}: {issue}" for issue in issues)
    if problems:
        return Check("role_shape", False, "; ".join(problems), required=True)
    return Check("role_shape", True, "roles state only strength and independent_of",
                 required=True)


def cmd_config(repo_root: Path, verb: str, *, effective: bool = False,
               as_json: bool = False) -> int:
    """`maestro config show [--effective] [--json]`: every key, its value and its layer.

    Without `--effective` only the keys `project.yaml` itself sets are shown.
    """
    from maestro import config as _config
    merged, sources = _config.effective_config(repo_root.resolve())
    leaves = _config.config_leaves(merged)
    rows = {path: {"value": leaves[path], "source": source}
            for path, source in sorted(sources.items())
            if effective or source == "project"}
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for path, row in rows.items():
        print(f"{path} = {json.dumps(row['value'])}  [{row['source']}]")
    return 0


def _check_adopted_code(repo_root: Path) -> Check:
    """Is the maestro that is running the maestro that was adopted?

    The gap this closes was invisible for as long as it existed: `adopt()` recorded a version
    and wrote `~/.maestro/current`, nothing read the file, and the code that actually ran was
    whatever `import maestro` resolved to — for an editable install, a working tree, edits and
    all. Every other doctor check would pass while the deployed version was a fiction. So the
    check is deliberately about *this process*, not about the file: it compares where maestro
    was imported from against where `current` says it should have been.

    A machine that has never self-updated is `OK`, not a failure — `current` is absent, there
    is nothing to diverge from, and running the checkout you installed is correct. `WARN` is
    for the two states worth acting on: a pointer that no longer resolves, and a running
    checkout that is not the adopted one.

    `[P11]`: the pointer checked is *this project's* (`<repo>/.orchestrator/current`) when it
    has one, and the machine-global only when it has not — the same order `bootstrap.
    adopted_root` redirects on. Reporting against the global pointer while the process was
    redirected by a project pin would have made the check answer about a file nothing read.
    """
    running = bootstrap.running_root()
    project_pin = bootstrap.project_pin_file(repo_root)
    pointer = project_pin if project_pin and project_pin.exists() else bootstrap.current_file()
    if not pointer.exists():
        return Check("adopted_code", True,
                     f"no {pointer} — never self-updated; running {running}")
    adopted = bootstrap.adopted_root(repo=repo_root)
    if adopted is None:
        return Check("adopted_code", False,
                     f"{pointer} does not name a maestro checkout — "
                     f"running {running} unredirected")
    if adopted != running:
        return Check("adopted_code", False,
                     f"running {running} but {adopted} is adopted — "
                     f"`{bootstrap.SENTINEL}` set, or the re-exec did not happen")
    return Check("adopted_code", True, f"running the adopted checkout: {adopted}")


def _check_follows_latest(repo_root: Path) -> Check:
    """Is this project's pin on the version the maestro checkout last announced?

    A lag is normal for one idle poll after a release; a lag that persists means the
    controller is not reaching its idle adoption (busy, paused, or not running)."""
    from maestro.selfupdate import read_latest

    latest = read_latest()
    pin = bootstrap.project_pin_file(repo_root)
    if latest is None or pin is None or not pin.exists():
        return Check("follows_latest", True, "no announced version or no project pin")
    pinned = Path(pin.read_text().strip()).name
    if pinned != latest:
        return Check("follows_latest", False,
                     f"pinned to {pinned[:12]} but {latest[:12]} is announced — "
                     f"adopted at the controller's next idle poll")
    return Check("follows_latest", True, f"pinned to the announced {latest[:12]}")


def _check_thirdparty(*, required: bool = False) -> Check:
    """Is every third-party thing Maestro uses still the version somebody verified?

    The gate for B06 (backend capabilities) and B17 (free-use guarantees), neither of which
    is answerable in advance: both are facts about *one exact version* and both expire
    silently when that version moves. `maestro/thirdparty.py` holds the ledger; this check
    is where a stale row becomes visible.

    Default `WARN`, like every other optional check here: an agent CLI that auto-updated
    overnight must not fail the whole checklist, it must be *loud*. A backend whose `--help`
    still shows every recorded control after the bump is `reobserved` and passes even the
    required form. `maestro doctor
    --thirdparty` sets `required`, which is the form an implementing agent runs as the last
    step before calling a backend or dependency integration complete — there a non-zero exit
    is the entire point.
    """
    # Imported here rather than at module scope, following the `cmd_*` convention: this
    # module is imported by `maestro doctor --help`, which `templates/pre-commit` probes on
    # every commit, and that path has no reason to read a register.
    from maestro import thirdparty

    try:
        statuses = thirdparty.audit()
    except ValueError as exc:            # a malformed shipped register is a repo defect
        return Check("thirdparty", False, f"register unreadable: {exc}", required=required)

    blocking = thirdparty.blocking(statuses)
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.status] = counts.get(status.status, 0) + 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    if not blocking:
        reobserved = "; ".join(
            f"{status.subject}: {status.detail}" for status in statuses
            if status.status == "reobserved"
        )
        return Check("thirdparty", True,
                     f"{len(statuses)} subject(s): {summary}"
                     + (f" — {reobserved}" if reobserved else ""),
                     required=required)
    detail = "; ".join(f"{status.subject}: {status.detail}" for status in blocking)
    return Check("thirdparty", False,
                 f"{len(blocking)} of {len(statuses)} subject(s) need re-verification — {detail}",
                 required=required)


def cmd_doctor(repo_root: Path, *, pre_commit: bool = False, thirdparty_only: bool = False,
                http_get: Optional[Callable[[str], dict]] = None) -> int:
    """`maestro doctor [--pre-commit | --thirdparty]` — DESIGN.md §10's checklist.

    `--pre-commit` runs only the fast, required subset (docs + the `test` adapter) that
    `templates/pre-commit` probes for via `maestro doctor --help`. `--thirdparty` runs only
    the third-party verification gate (DESIGN.md §15) and makes it *required*, so it exits
    non-zero on an unverified or drifted subject — that is the form the integration gate is
    invoked in. The two flags are mutually exclusive at the parser.

    Exit code is non-zero iff a *required* check failed; optional checks print and nag but
    never affect the exit code."""
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    http_get = http_get or _default_http_get

    if thirdparty_only:
        checks = [_check_thirdparty(required=True)]
        for check in checks:
            print(f"[{'OK  ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        return 0 if all(check.ok for check in checks) else 1

    checks: list[Check] = [_check_docs(repo_root)]
    checks.extend(_check_adapters(repo_root))

    if not pre_commit:
        from maestro import config as _config
        cfg = _config.load_project_yaml()
        project_name = (cfg.get("project") or {}).get("name") or repo_root.name
        checks.append(_check_preamble_deny_list(repo_root))
        checks.append(_check_model_limits(repo_root))
        checks.append(_check_model_ids(repo_root))
        checks.append(_check_backends(repo_root))
        checks.append(_check_mermaid(repo_root))
        checks.append(_check_telegram(repo_root, http_get))
        checks.append(_check_systemd_unit(repo_root, project_name))
        checks.append(_check_gate(repo_root))
        checks.append(_check_adopted_code(repo_root))
        checks.append(_check_follows_latest(repo_root))
        checks.append(_check_model_policy(repo_root))
        checks.append(_check_retention(repo_root))
        checks.append(_check_config_keys(repo_root))
        checks.append(_check_priority(repo_root))
        checks.append(_check_role_shape(repo_root))
        checks.append(_check_goals(repo_root))
        checks.append(_check_roadmap_blocks(repo_root))
        checks.append(_check_task_labels(repo_root))
        checks.append(_check_journal_and_version(repo_root))
        checks.append(_check_thirdparty())
        checks.append(_check_meta_branch(repo_root))

    worst_ok = True
    for check in checks:
        marker = "OK  " if check.ok else ("FAIL" if check.required else "WARN")
        print(f"[{marker}] {check.name}: {check.detail}")
        if check.required and not check.ok:
            worst_ok = False

    return 0 if worst_ok else 1


# ══════════════════════════════════════════════════════════════════════════
# status / run / ctl / watchdog
# ══════════════════════════════════════════════════════════════════════════


def cmd_status(repo_root: Path) -> int:
    """`maestro status` — reads and pretty-prints `state.json` via `maestro.state`."""
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro.state import STATE_JSON, read_state
    if not STATE_JSON.is_file():
        print(f"[status] no state.json at {STATE_JSON} — run `maestro init` first")
        return 1
    state = read_state()
    phase = state.get("phase") or {}
    in_flight = state.get("in_flight", [])
    print(f"Phase: {phase.get('id', '?')} ({phase.get('status', '?')})")
    print(f"In-flight: {len(in_flight)}  "
          f"{', '.join(e.get('task_id', '?') for e in in_flight) or '(none)'}")
    runs = state.get("graph_runs") or []
    if runs:
        print(f"Graph runs: {len(runs)}  "
              f"{', '.join(str(r.get('task_id', '?')) for r in runs if isinstance(r, dict))}")
    print(f"Halted: {state.get('halted', False)}   Paused: {state.get('paused_by_user', False)}")
    parked = state.get("parked_tasks", [])
    if parked:
        print(f"Parked: {', '.join(parked)}")
    return 0


def _load_project_env(repo_root: Path) -> None:
    """Load `<repo>/.env` into this process, over anything inherited.

    The watchdog starts `maestro run` with `tmux new-window`, which inherits the tmux
    *server's* environment, not the watchdog's — so `launch.sh` sourcing `.env` never reached
    the loop, and every Telegram notice was silently skipped (P12 pilot, session 3f). The
    reference `orchestrator_run.py` loaded it itself at import (`override=True`); a stale
    value in the tmux server (another project's bot) must not win over the project's own.
    """
    import dotenv
    env_path = repo_root / ".env"
    if env_path.is_file():
        dotenv.load_dotenv(env_path, override=True)
    from maestro.hitl.shared_bot import load_shared_env, register_project
    load_shared_env()
    if os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_ALERT_CHAT_ID'):
        register_project(repo_root)


def cmd_run(repo_root: Path) -> int:
    """`maestro run` — invokes `maestro.orchestrator.main()` with `MAESTRO_REPO` set to
    `repo_root`. See this module's docstring for the no-op supervised loop behaviour on an
    empty roadmap and the tmux-session precondition `main()` itself enforces."""
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    _load_project_env(repo_root)
    from maestro.orchestrator import main as orchestrator_main
    return orchestrator_main()


def cmd_workflow(repo_root: Path, verb: str, task_id: Optional[str], *, as_html: bool = False,
                 as_json: bool = False, out: Optional[str] = None,
                 run_id: Optional[str] = None, kind: Optional[str] = None) -> int:
    """`maestro workflow show <task_id>` — the operator's view of one compiled graph (`[P11]`).

    `maestro workflow runs <task_id>` lists every run of the task in start order (`[P12A]`
    W11), as text or `--json`.

    Three renderings of *one* read (`status.workflow_view`): text for a terminal, `--json`
    for a script, `--html` for a self-contained page that survives being forwarded. The read
    opens a **client** store — an operator looking at a graph must not be able to move it.
    """
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro import status

    if verb == "runs":
        # P12A W11: every run of the task, oldest first. `--html` / `--run` do not apply.
        try:
            rows = status.list_runs(task_id, repo=repo_root)
        except status.WorkflowViewError as exc:
            print(f"[workflow] {exc}")
            return 1
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            print(status.render_runs_text(rows), end="")
        return 0
    if verb == "failures":
        try:
            rows = status.list_failures(task_id, kind=kind, repo=repo_root)
        except status.WorkflowViewError as exc:
            print(f"[workflow] {exc}")
            return 1
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            print(status.render_failures_text(rows), end="")
        return 0
    if verb != "show":
        print(f"[workflow] unknown verb {verb!r} — the verbs are `show`, `runs` and `failures`")
        return 2
    try:
        view = status.workflow_view(task_id, repo=repo_root, run_id=run_id)
    except status.WorkflowViewError as exc:
        print(f"[workflow] {exc}")
        return 1

    if as_json:
        print(json.dumps(view, indent=2, default=str))
        return 0
    if as_html:
        html = status.render_workflow_html(view)
        target = Path(out) if out else (
            repo_root / ".orchestrator" / "artifacts" / f"workflow-{task_id}.html")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        print(f"[workflow] wrote {target}")
        return 0
    print(status.render_workflow_text(view), end="")
    return 0


def cmd_explain(repo_root: Path, task_id: str, *, as_json: bool = False,
                run_id: Optional[str] = None) -> int:
    """`maestro explain <task_id>` — criteria -> binding -> evidence -> acceptance (`[P11]`).

    Answers "why does this task stand where it stands" from what was *recorded*: it never
    re-runs a check, re-resolves a route or re-decides acceptance. See
    `maestro.status.explain_view`.
    """
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro import status

    # P10B: a task the launch loop is holding back never reaches a workflow run, so the
    # most useful answer for it is the admission verdict, printed whatever else follows.
    waiting = status.admission_wait(task_id)
    if waiting and not as_json:
        print(f"[explain] {task_id} is waiting on host admission — "
              f"{waiting.get('reason')}: {waiting.get('detail')} (since {waiting.get('since')})")
    try:
        view = status.workflow_view(task_id, repo=repo_root, run_id=run_id)
        explained = status.explain_view(view, repo=repo_root)
    except status.WorkflowViewError as exc:
        if waiting and as_json:
            print(json.dumps({"task_id": task_id, "admission_waiting": waiting}, indent=2))
            return 0
        print(f"[explain] {exc}")
        return 0 if waiting else 1
    if waiting:
        explained["admission_waiting"] = waiting
    if as_json:
        print(json.dumps(explained, indent=2, default=str))
        return 0
    print(status.render_explain_text(explained), end="")
    return 0


#: `ctl` verbs whose logic is a trivial, fully-wired state transition (read_state/write_state/
#: append_journal/HALT_FILE) with no dependency on hitl.commands' Telegram-message-shaped
#: handlers. Kept separate from `_CTL_DISPATCH_VERBS` below deliberately: see `cmd_ctl`'s
#: docstring for why the two groups are handled differently.
_CTL_STATE_VERBS = ("pause", "resume", "halt", "hitl", "unpark", "remerge")
#: `ctl` verbs that dispatch straight to the real `maestro.hitl.commands` handler with the same
#: name, since those already exist as callables independent of the Telegram `getUpdates` loop
#: they are normally invoked from.
_CTL_DISPATCH_VERBS = {
    "waiting": "_show_waiting", "manual": "_show_manual", "detail": "_show_detail",
    "approve": "_process_approve", "reject": "_process_reject", "fix": "_process_fix",
    "ask": "_handle_ask", "redo": "_handle_redo", "backend": "_process_backend",
    # `[P11]`: the two operator projections, reachable from `maestro ctl` as well as from
    # Telegram. `maestro workflow show` / `maestro explain` print them; these two *send*
    # them to the operator channel, which is a different thing to want.
    "workflow": "_show_workflow", "explain": "_show_explain",
}
#: P13 W8: `ctl` verbs only a running graph loop can apply, since it holds the live runners.
#: They are checked here and enqueued as the named command (`[INV-01]`), never applied here.
_CTL_GRAPH_VERBS = {"retry": "graph_retry", "cancel": "graph_cancel"}


def _ctl_graph_verb(repo_root: Path, verb: str, args: list[str]) -> int:
    """`maestro ctl retry|cancel <task_id> [<node_id>]`: queue it for the graph loop.

    A request the run does not allow is refused here, at the terminal, and nothing is
    queued. The loop checks again when it applies the command.
    """
    from maestro import command_inbox
    from maestro.control.models import nanoid
    from maestro.control.store import enqueue_external_command, open_active_store

    if not 1 <= len(args) <= 2:
        print(f"[ctl] usage: maestro ctl {verb} <task_id> [<node_id>]")
        return 1
    task_id, node_id = args[0], (args[1] if len(args) == 2 else None)
    command_type = _CTL_GRAPH_VERBS[verb]
    store = open_active_store(repo_root)
    if store is None:
        print(f"[ctl] {verb} refused: not a graph project (no control database).")
        return 1
    try:
        command_inbox.check_graph_request(store, command_type, task_id, node_id)
    except command_inbox.CommandRejected as exc:
        print(f"[ctl] {verb} refused: {exc}")
        return 1
    finally:
        store.close()
    command = enqueue_external_command(
        command_type, {"task_id": task_id, "node_id": node_id},
        idempotency_key=f"ctl:{verb}:{task_id}:{node_id or '*'}:{nanoid()}", repo=repo_root,
    )
    if command is None:
        print(f"[ctl] {verb} could not be queued: the control database refused the write.")
        return 1
    print(f"[ctl] queued {command_type} {command.command_id} for "
          f"{task_id}{' ' + node_id if node_id else ''}. The graph loop applies it on its next "
          f"poll (or when it next starts); the result is journaled (command_applied / "
          f"command_rejected) and sent to Telegram.")
    return 0


def _live_controller_pid(repo_root: Path) -> int | None:
    """The pid of another live process holding this project's controller lease, if any."""
    from maestro.control.store import CONTROLLER_RESOURCE, open_active_store, pid_alive

    store = open_active_store(repo_root)
    if store is None:
        return None
    try:
        row = store.conn.execute(
            "SELECT writer_pid FROM writer_leases WHERE resource_id = ?", (CONTROLLER_RESOURCE,)
        ).fetchone()
    finally:
        store.close()
    if row is None or row["writer_pid"] == os.getpid() or not pid_alive(row["writer_pid"]):
        return None
    return row["writer_pid"]


def cmd_ctl(repo_root: Path, verb: str, args: list[str]) -> int:
    """`maestro ctl <verb> [args...]` — DESIGN.md §10, wrapping `maestro.hitl.commands`'
    existing control-command handling rather than reimplementing it.

    **Two groups, handled differently, on purpose.** `poll_control_commands` (the Telegram
    router this all normally lives behind) answers every command through `notify_telegram`,
    which is a silent no-op unless `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALERT_CHAT_ID` are set in
    the environment — fine for a Telegram-driven flow, useless for an operator watching a
    terminal. `pause`/`resume`/`halt`/
    `hitl`/`unpark` are simple enough (2-4 lines each in the reference `elif` chain) that this
    function reproduces the exact same `read_state`/`write_state`/`append_journal`/`HALT_FILE`
    primitives directly and *also* prints the result — not a reimplementation of logic, just
    the same three-line state transition with a second, stdout-shaped side channel. Every other
    verb (`waiting`, `manual`, `detail`, `approve`, `reject`, `fix`, `ask`, `redo`, `backend`)
    dispatches straight to `maestro.hitl.commands`' real `_show_waiting`/`_process_approve`/...
    functions, unmodified — their output stays Telegram-shaped (this function prints a note
    saying so) because refactoring them to return text instead of calling `notify_telegram`
    would mean editing `maestro/hitl/commands.py`, which is out of this component's scope.

    **A third group (P13 W8): `retry` and `cancel`.** `ctl retry|cancel <task_id> [<node_id>]`
    stop one node of a running graph run, cancel the run, or reopen a settled task. Only the
    running graph loop can do that, so these are checked here, enqueued in the command inbox
    and applied by the loop on its next poll (`_ctl_graph_verb`); unlike the state verbs they
    work while the loop runs.

    **Resolved in M4a (see docs/PROGRESS.md and
    docs/plans/2026-08-16-m4a-pending-placeholders.md).** M4 found that
    `maestro/hitl/commands.py` bound `_finalize_manual_action`, `park_regression`,
    `attempt_self_fix` and `_remove_from_state` to `pending()` placeholders that
    unconditionally raised `NotImplementedError`, so `ctl approve`/`ctl fix` against a real
    matching id crashed. Those four are now `deferred()` late bindings onto the real
    `maestro.parking` / `maestro.selfheal.selffix` / `maestro.orchestrator` implementations,
    verified by `tests/test_no_unresolved_pending.py`. This build's own tests still only
    exercise the "no such pending item" early-return path, because the matching path mutates
    real task state.

    `ctl reject` on a normal HITL-parked task still inherits the pre-existing,
    already-characterised `REPO_ROOT` `NameError` documented in
    `docs/found_bugs_inbox/commands.md`'s C1 — that one is reference behaviour, deliberately
    not fixed.
    """
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro.hitl import commands as hitl_commands
    from maestro.hitl.commands import HALT_FILE
    from maestro.state import append_journal, now_iso, read_state, write_state

    verb = verb.lower()

    if verb == "status":
        return cmd_status(repo_root)

    if verb in _CTL_GRAPH_VERBS:
        return _ctl_graph_verb(repo_root, verb, args)

    if verb in _CTL_STATE_VERBS:
        owner = _live_controller_pid(repo_root)
        if owner is not None:
            # [INV-01]: the running loop is the state document's only writer, so from
            # here only the HALT sentinel (a file it polls) can reach it. P12 finding D5.
            if verb == "halt":
                HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
                HALT_FILE.write_text(now_iso())
                append_journal("control_halt", "halted via `maestro ctl halt`", shadow=False)
                print(f"[ctl] wrote {HALT_FILE} — orchestrator will stop after its current iteration.")
                return 0
            print(f"[ctl] {verb} refused: the loop is running (controller pid {owner}) and owns the "
                  f"state. Use the Telegram /{verb} command, or `maestro ctl halt` first.")
            return 1
        if verb == "pause":
            state = read_state()
            state["paused_by_user"] = True
            write_state(state)
            append_journal("control_pause", "paused via `maestro ctl pause`")
            print("[ctl] paused_by_user=True — no new tasks will launch; `ctl resume` to continue.")
            return 0
        if verb == "resume":
            state = read_state()
            state["paused_by_user"] = False
            write_state(state)
            append_journal("control_resume", "resumed via `maestro ctl resume`")
            print("[ctl] paused_by_user=False — resumed.")
            return 0
        if verb == "halt":
            HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HALT_FILE.write_text(now_iso())
            append_journal("control_halt", "halted via `maestro ctl halt`")
            print(f"[ctl] wrote {HALT_FILE} — orchestrator will stop after its current iteration.")
            return 0
        if verb == "hitl":
            if not args or args[0].lower() not in ("on", "off"):
                print("usage: ctl hitl on|off")
                return 2
            state = read_state()
            state["hitl_mode"] = args[0].lower() == "on"
            write_state(state)
            print(f"[ctl] hitl_mode={state['hitl_mode']}")
            return 0
        if verb == "unpark":
            if not args:
                print("usage: ctl unpark <task_id>")
                return 2
            tid = args[0].upper()
            state = read_state()
            parked = state.get("parked_tasks", [])
            if tid not in parked:
                print(f"[ctl] {tid} is not parked. Currently parked: {', '.join(parked) or 'none'}")
                return 1
            parked.remove(tid)
            state["parked_tasks"] = parked
            state.get("retry_counts", {}).pop(tid, None)
            write_state(state)
            append_journal("control_unpark", f"{tid} unparked via `maestro ctl unpark`")
            print(f"[ctl] {tid} unparked — will be launched on the next `maestro run` poll.")
            return 0
        if verb == "remerge":
            # A failed merge after an approving review, the branch fixed by hand: re-run
            # only merge and accept, no model (`maestro.workflows.remerge`). The loop is
            # stopped (checked above), so this process may take the controller lock.
            if not args:
                print("usage: ctl remerge <task_id>")
                return 2
            from maestro.control.store import ControlStore, SingleWriterViolation, control_db_path
            from maestro.workflows.remerge import RemergeRefused, remerge

            db = control_db_path(repo_root)
            if not db.exists():
                print(f"[ctl] remerge refused: no control database at {db}")
                return 1
            try:
                store = ControlStore.open_controller(db)
            except SingleWriterViolation as exc:
                print(f"[ctl] remerge refused: the loop is running ({exc}). `maestro ctl halt` first.")
                return 1
            try:
                done = remerge(store, args[0])
            except RemergeRefused as exc:
                print(f"[ctl] remerge refused: {exc}")
                return 1
            finally:
                store.close()
            print(f"[ctl] {done.task_id} remerge: {done.old_run_id} (failed) → {done.new_run_id} "
                  f"at revision {done.revision}; carried {', '.join(done.carried)}; "
                  f"{', '.join(done.rerun)} run on the next `maestro run`.")
            return 0

    if verb in _CTL_DISPATCH_VERBS:
        fn = getattr(hitl_commands, _CTL_DISPATCH_VERBS[verb])
        try:
            if verb in ("waiting", "manual"):
                fn()
            elif verb == "detail":
                if not args:
                    print("usage: ctl detail <task_id>")
                    return 2
                fn(args[0])
            elif verb in ("approve", "reject"):
                if not args:
                    print(f"usage: ctl {verb} <id>")
                    return 2
                if verb == "approve":
                    in_flight = read_state().get("in_flight", [])
                    fn(args[0], in_flight)
                else:
                    fn(args[0])
            elif verb == "fix":
                if not args:
                    print("usage: ctl fix <task_id>")
                    return 2
                fn(args[0].upper())
            elif verb in ("ask", "redo"):
                if not args:
                    print(f"usage: ctl {verb} <id> <message>")
                    return 2
                fn(" ".join(args))
            elif verb == "backend":
                fn(" ".join(args))
        # M4a rebound the four formerly-`pending()` names onto real implementations, so
        # neither arm should fire any more. Both are kept as a backstop: NotImplementedError
        # if a `pending()` placeholder is ever reintroduced, ImportError because that is how
        # `maestro.pending.deferred` reports a binding whose owner module cannot supply the
        # name. `tests/test_no_unresolved_pending.py` is the gate that stops either reaching
        # a user; this is only what happens if it somehow does.
        except (NotImplementedError, ImportError) as exc:
            print(f"[ctl] {verb} hit an unresolved late binding in "
                  f"maestro.hitl.commands: {exc} — see this function's docstring and "
                  f"tests/test_no_unresolved_pending.py.")
            return 1
        print(f"[ctl] dispatched to hitl.commands.{fn.__name__}() — its reply goes to Telegram "
              f"if TELEGRAM_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID are set, and to the journal where "
              f"applicable; nothing else is printed here by design (see this function's docstring).")
        return 0

    print(f"[ctl] unknown verb {verb!r}. Known: status, {', '.join(_CTL_GRAPH_VERBS)}, "
          f"{', '.join(_CTL_STATE_VERBS)}, {', '.join(_CTL_DISPATCH_VERBS)}")
    return 2


def cmd_roadmap(repo_root: Path, verb: str, *, expect_hash: Optional[str] = None,
                as_json: bool = False) -> int:
    """`maestro roadmap validate|apply` — the `[P2]` dependency model, from the terminal.

    **validate** reads `docs/ROADMAP.md` strictly: malformed YAML, a duplicate id, a
    dependency on a task nobody declared, a cycle. Each problem is printed with its block
    index and source line, and a problem is an exit code — the roadmap is the project's
    intent, and an intent that does not parse must not be silently half-obeyed. It also
    reports a document-drift conflict, because a roadmap that is *valid* can still have
    moved under a running controller.

    **apply** is the compare-and-swap. It validates, records the document as the applied
    revision in `.orchestrator/roadmap_cas.json`, and — if this project has a control
    database — persists the tasks, their spec revisions and their dependency edges into
    it. `--expect-hash` is the caller's compare operand: pass the hash you read and the
    write is refused if the file moved since. Omit it and the recorded revision is the
    operand instead, so an unnoticed hand edit fails closed rather than being clobbered.

    Exit codes: 0 applied/valid, 1 the roadmap is invalid, 3 the document drifted or the
    control database is held by a live controller. Never 0 on a refusal.
    """
    from maestro import taskgraph
    from maestro.control.store import ControlStore, ControlStoreError, control_db_path

    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    path = taskgraph.roadmap_path(repo_root)
    if not path.exists():
        print(f"[roadmap] missing {path}", file=sys.stderr)
        return 1

    document = taskgraph.load_document(path)
    registry = taskgraph.read_registry_ids(repo_root)
    issues = taskgraph.validate(document, known_ids=registry)
    conflict = taskgraph.drift(repo_root, document)

    if verb == "validate":
        if as_json:
            print(json.dumps({
                "document_hash": document.document_hash,
                "tasks": len(document.blocks),
                "issues": [
                    {"code": i.code, "message": i.message, "block": i.block_index,
                     "line": i.line, "task_id": i.task_id, "cycle": list(i.cycle)}
                    for i in issues
                ],
                "drift": None if conflict is None else {
                    "recorded_hash": conflict.recorded_hash,
                    "current_hash": conflict.current_hash,
                    "changed": list(conflict.changed),
                    "added": list(conflict.added),
                    "removed": list(conflict.removed),
                    "blocked": sorted(conflict.blocked),
                },
            }, indent=2))
        else:
            for issue in issues:
                print(f"[roadmap] {issue}", file=sys.stderr)
            if conflict is not None:
                print(f"[roadmap] {conflict}", file=sys.stderr)
                if conflict.blocked:
                    print("[roadmap] dispatch blocked for: "
                          + ", ".join(sorted(conflict.blocked)), file=sys.stderr)
            if not issues:
                graph = taskgraph.build_graph(document, registry_ids=registry)
                print(f"[roadmap] OK — {len(graph)} tasks, {len(graph.ready())} ready, "
                      f"document {document.document_hash[:12]}")
        return 1 if issues else 0

    if verb != "apply":
        print(f"[roadmap] unknown verb {verb!r} (expected validate or apply)", file=sys.stderr)
        return 2

    try:
        result = taskgraph.apply(repo_root, expected_hash=expect_hash)
    except taskgraph.RoadmapValidationError as exc:
        print(f"[roadmap] refused — {exc}", file=sys.stderr)
        return 1
    except taskgraph.DocumentDriftError as exc:
        # Never resolve a drift by overwriting: the edit is Dan's and the controller's
        # job is to stop, not to win.
        print(f"[roadmap] refused — {exc}", file=sys.stderr)
        if exc.drift is not None and exc.drift.blocked:
            print("[roadmap] dispatch blocked for: "
                  + ", ".join(sorted(exc.drift.blocked)), file=sys.stderr)
        print("[roadmap] re-run with --expect-hash "
              f"{taskgraph.load_document(path).document_hash} to adopt the edited document.",
              file=sys.stderr)
        return 3

    print(f"[roadmap] applied revision {result.record.revision} — "
          f"{len(result.graph)} tasks, document {result.record.document_hash[:12]}")

    if not control_db_path(repo_root).exists():
        print("[roadmap] no control database — roadmap recorded on disk only.")
        return 0
    try:
        store = ControlStore.open_controller(control_db_path(repo_root))
    except ControlStoreError as exc:
        print(f"[roadmap] control store unavailable: {exc}", file=sys.stderr)
        return 3
    try:
        revisions = taskgraph.sync_to_store(store, result.graph)
    finally:
        store.close()
    print(f"[roadmap] persisted {len(revisions)} tasks into control.sqlite3 "
          f"(revisions: {', '.join(f'{k}@{v}' for k, v in sorted(revisions.items()))})")
    return 0


def _goals_baseline(path: Path, *, as_json: bool, timeout: int) -> int:
    """`maestro goals baseline`: run every criterion's bound test once, store the result
    (`acceptance.BASELINE`) and list it. Exit 0 only when every `AC-` is a credible RED or
    GREEN and no fixture or data path leaves the project (GDT-3)."""
    from maestro import acceptance
    from maestro.docs import goals as doc_goals

    if not path.is_file():
        print(f"no {doc_goals.DOC} at {path}", file=sys.stderr)
        return 1
    repo = path.parent.parent if path.parent.name == "docs" else path.parent
    doc = doc_goals.parse(path.read_text(encoding="utf-8"))
    if doc.format != 2:
        print(f"{path}: format {doc.format} has no acceptance criteria "
              "(`maestro goals migrate` drafts format 2)", file=sys.stderr)
        return 1
    results = acceptance.run_baseline(repo, doc, timeout=timeout)
    stored = acceptance.store(repo, doc, results)
    from maestro import goal_state

    # GDT-9: the contract revision this baseline ran at, kept to catch a silent removal.
    # The baseline is already stored: a store that cannot be written is a warning.
    try:
        goal_state.observe_contract(repo, doc, path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: the contract snapshot was not recorded ({type(exc).__name__}: "
              f"{exc}); the baseline is stored", file=sys.stderr)
    safety = [p for c in doc.criteria for p in acceptance.safety_problems(c, repo)]
    credible = (acceptance.RED, acceptance.GREEN, acceptance.OPERATOR)
    failing = [r for r in results if r.state not in credible] or safety
    if as_json:
        print(json.dumps({"path": str(path), "revision": doc.revision, "stored": str(stored),
                          "results": [asdict(r) for r in results], "safety": safety},
                         indent=2))
    else:
        print(f"{path}: revision {doc.revision}, {len(results)} results stored in {stored}")
        for r in results:
            print(f"  - {r.id} {r.state}: {r.reason}")
        for problem in safety:
            print(f"  - [safety] {problem}")
    return 1 if failing else 0


def _goals_tasks(path: Path, *, as_json: bool) -> int:
    """`maestro goals tasks`: proposed ROADMAP blocks for the unmet mandatory criteria in
    the stored baseline (GDT-4), each with its gate checks (`goal_roadmap.verifications`).
    Prints only; nothing writes them into docs/ROADMAP.md."""
    from maestro import acceptance, goal_roadmap, taskgraph
    from maestro.docs import goals as doc_goals

    if not path.is_file():
        print(f"no {doc_goals.DOC} at {path}", file=sys.stderr)
        return 1
    repo = path.parent.parent if path.parent.name == "docs" else path.parent
    doc = doc_goals.parse(path.read_text(encoding="utf-8"))
    roadmap = repo / "docs" / "ROADMAP.md"
    tasks = taskgraph.scan_tasks(roadmap.read_text(encoding="utf-8")) if roadmap.is_file() else []
    try:
        proposed = goal_roadmap.propose_tasks(doc, acceptance.load(repo), tasks)
    except ValueError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    # Each block carries the check the graph runs: a pasted block without one is refused
    # (INV-04) and wedges the run (GDT-10 follow-up M-A).
    proposed = [{**t, "verifications": goal_roadmap.verifications(t, doc, repo)}
                for t in proposed]
    if as_json:
        print(json.dumps(proposed, indent=2))
    elif proposed:
        print(goal_roadmap.render_tasks(proposed, scope_hint=True), end="")
    else:
        print(f"{path}: every mandatory criterion is GREEN or named by an open task")
    return 0


def _goals_status(path: Path, *, as_json: bool) -> int:
    """`maestro goals status`: the goals' durable progress from what is on disk
    (`goal_state.progress`, GDT-9): revision, baseline, each criterion's state and where it
    was observed, what is unmet, decisions, replans, holds, next work, stale evidence and
    how to reproduce. Reads only; never a done verdict (GDT-10's)."""
    from maestro import goal_state, taskgraph
    from maestro.docs import goals as doc_goals

    if not path.is_file():
        print(f"no {doc_goals.DOC} at {path}", file=sys.stderr)
        return 1
    repo = path.parent.parent if path.parent.name == "docs" else path.parent
    doc = doc_goals.parse(path.read_text(encoding="utf-8"))
    roadmap = repo / "docs" / "ROADMAP.md"
    tasks = taskgraph.scan_tasks(roadmap.read_text(encoding="utf-8")) if roadmap.is_file() else []
    shown = goal_state.progress(repo, doc, tasks)
    if as_json:
        print(json.dumps(shown, indent=2))
    else:
        print(goal_state.render(shown), end="")
    return 0


def _goals_finish(path: Path, *, as_json: bool, timeout: int) -> int:
    """`maestro goals finish`: the completion gate's verdict (GDT-10, R53, R56): rerun every
    mandatory criterion on this checkout at (contract revision, HEAD), unless a stored run
    at that key already has them, write the outcome report and print it. Exit 0 only for
    `complete`. Never asks Dan, adds tasks or notifies (the graph loop does)."""
    from maestro import goal_completion
    from maestro.docs import goals as doc_goals

    if not path.is_file():
        print(f"no {doc_goals.DOC} at {path}", file=sys.stderr)
        return 1
    repo = path.parent.parent if path.parent.name == "docs" else path.parent
    doc = doc_goals.parse(path.read_text(encoding="utf-8"))
    if doc.format != 2:
        print(f"{path}: format {doc.format} has no criteria to verify "
              "(`maestro goals migrate` drafts format 2)", file=sys.stderr)
        return 1
    step = goal_completion.evaluate(repo, act=False, budget=float("inf"), timeout=timeout)
    v = step["verdict"]
    if v is None:
        print(f"{path}: {step['text'] or 'no verdict'}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(v, indent=2, default=str))
    elif v.get("items"):
        print(goal_completion.render(v, doc), end="")
    else:
        print(f"Verdict: {v['verdict']} — {step['text']}")
    return 0 if v["verdict"] == "complete" else 1


def cmd_goals(target: Path, *, verb: str = "check", as_json: bool = False,
              gate: bool = False, timeout: Optional[int] = None) -> int:
    """`maestro goals check`: what a GOALS.md lacks, for maestro-setup to ask Dan (W11a);
    each line has its severity (note, draft, gate); `--json` also says per issue whether only
    Dan can answer it (`ask`, GDT-13). Exit 1 on any draft or gate problem,
    or with `--gate` only on a gate problem (GDT-1). `maestro goals migrate` prints a
    format-2 skeleton of a format-1 file and never writes it. `maestro goals baseline`
    runs and stores the acceptance baseline (`_goals_baseline`); `maestro goals status`
    prints the goals' durable progress (`_goals_status`, GDT-9); `maestro goals finish`
    prints the completion gate's verdict (`_goals_finish`, GDT-10)."""
    from maestro.docs import goals as doc_goals

    path = doc_goals.path_for(target)
    if verb == "finish":
        from maestro import acceptance
        return _goals_finish(path, as_json=as_json,
                             timeout=timeout or acceptance.DEFAULT_TIMEOUT)
    if verb == "tasks":
        return _goals_tasks(path, as_json=as_json)
    if verb == "status":
        return _goals_status(path, as_json=as_json)
    if verb == "baseline":
        from maestro import acceptance
        return _goals_baseline(path, as_json=as_json,
                               timeout=timeout or acceptance.DEFAULT_TIMEOUT)
    if verb == "migrate":
        if not path.is_file():
            print(f"no {doc_goals.DOC} at {path}", file=sys.stderr)
            return 1
        try:
            print(doc_goals.migrate(doc_goals.parse(path.read_text(encoding="utf-8"))), end="")
        except ValueError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 1
        return 0
    if not path.is_file():
        # One question, the contract itself: not one line per section an empty file lacks.
        doc = doc_goals.parse("")
        issues = [doc_goals.Problem("gate", f"no {doc_goals.DOC} at {path}", ask=True)]
        ids: tuple[str, ...] = ()
    else:
        doc = doc_goals.parse(path.read_text(encoding="utf-8"))
        issues, ids = list(doc.issues), doc.ids
    problems = [i.text for i in issues if i.severity != "note"]
    failing = [i for i in issues if i.severity == "gate" or (not gate and i.severity == "draft")]
    if as_json:
        print(json.dumps({"path": str(path), "exists": path.is_file(), "format": doc.format,
                          "revision": doc.revision, "goals": list(ids), "problems": problems,
                          "issues": [{"severity": i.severity, "text": i.text, "ids": list(i.ids),
                                      "ask": i.ask} for i in issues]}, indent=2))
    elif problems:
        print(f"{path}: {len(problems)} missing or malformed")
    else:
        print(f"{path}: OK ({len(ids)} goals: {', '.join(ids)})")
    if not as_json:
        for issue in issues:
            print(f"  - [{issue.severity}] {issue.text}")
    return 1 if failing else 0


def cmd_intake(repo_root: Path, *, publish: bool = False, telegram: bool = False) -> int:
    """One bounded preparation tick; optional idle publication, never dispatch."""
    from maestro import intake
    from maestro.control.store import ControlStore, ControlStoreError, control_db_path
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    _load_project_env(repo_root)
    def send(text):
        print(text)
        if telegram:
            from maestro.hitl.telegram import notify_intake
            return notify_intake(text)
        return True
    intake.process(repo_root, None, send)
    if not publish or not intake.publication_ready(repo_root):
        return 0
    db = control_db_path(repo_root)
    if not db.exists():
        print('[intake] publication requires a controller database; the normal controller initializes it at idle.')
        return 3
    try:
        store = ControlStore.open_controller(db)
    except ControlStoreError as exc:
        print(f'[intake] publication deferred: {exc}')
        return 3
    try:
        # Legacy state is also an active-work boundary, independent of graph runs.
        from maestro.state import _authoritative_state
        state = _authoritative_state(store)
        if state is None:
            state_path = repo_root / '.orchestrator/state.json'
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get('in_flight') or state.get('awaiting_dan_verification') or state.get('waiting_on_dan'):
            print('[intake] publication deferred: active work or task approvals remain.')
            return 3
        intake.process(repo_root, store, send, prepare=False)
        return 3 if intake.publication_ready(repo_root) else 0
    finally:
        store.close()


def cmd_watchdog(repo_root: Path) -> int:
    """`maestro watchdog` — invokes `maestro.watchdog.main()` with `MAESTRO_REPO` set to
    `repo_root`, the same shape as `cmd_run` above.

    This is the *supervisor*, not the loop: it polls every `POLL_INTERVAL` (30s), relaunches
    `maestro run` in the shared `agents` tmux session whenever the orchestrator process is
    gone, schedules a resume at the quota reset time, reaps dead implementer windows, and
    HALTs after `MAX_STALL_RESTARTS` (3) consecutive stalls of `STALL_WINDOW_MIN` (20 min)
    journal silence. It is what the generated `launch.sh` runs under the
    `<project>-watchdog` systemd unit; `maestro run` is what *it* launches.

    **This does not return in normal operation.** `main()`'s `while True:` returns `1` when
    repeated stalls make it write the HALT sentinel (`.orchestrator/HALT`) itself. On a HALT
    that `maestro ctl halt` wrote it returns `0` only when no Telegram bot is configured (or
    `MAESTRO_TELEGRAM_LISTENER=0`); with a bot it stays up, answers `/help` and `/status`,
    queues other commands for the loop, and relaunches the loop once HALT is removed. Every
    other error is caught, journalled and slept through. Callers that need it bounded (tests,
    `doctor`) must set `MAESTRO_TELEGRAM_LISTENER=0` and arrange for HALT to exist.

    M4b (`docs/plans/2026-08-17-m4b-watchdog.md`) replaced the M4 honest stub this used to be:
    `maestro/watchdog.py` is now a real extraction of the reference `scripts/watchdog.py`, so
    DESIGN.md §13's "exact stall-detection window / definition of journal progress" open item
    is answered by that extraction (20 minutes; the journal's last line advancing) rather than
    by invention."""
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    _load_project_env(repo_root)
    from maestro.watchdog import main as watchdog_main
    return watchdog_main()


# ══════════════════════════════════════════════════════════════════════════
# install-skills
# ══════════════════════════════════════════════════════════════════════════


def cmd_install_skills(home: Optional[Path] = None) -> int:
    """`maestro install-skills` — copies Component C's two files to the Claude/Codex skill
    directories (DESIGN.md §10). `home` (or `$MAESTRO_HOME_OVERRIDE`, or `Path.home()`) is the
    overridable seam DESIGN.md's scope decision requires: a test must never write into the real
    `~/.codex/` or `~/.claude/`, so it always passes an explicit `home`.

    Unlike `init`'s scaffolding, this always overwrites — the installed skill files are
    maestro-owned generated content an operator is not expected to hand-edit, so re-running
    `install-skills` after an upgrade is meant to bring them current, not fork them into `.new`
    siblings forever.

    A destination reached through a symlink (`~/.claude/skills/maestro-setup` linking into
    `~/agent-skills`, the canonical skill) is left alone and reported: copying the repo's
    mirror through it would silently revert the canonical text (GDT-13)."""
    if home is None:
        override = os.environ.get("MAESTRO_HOME_OVERRIDE")
        home = Path(override) if override else Path.home()
    home = home.resolve()

    claude_src = SKILLS_DIR / "claude" / "SKILL.md"
    claude_dest = home / ".claude" / "skills" / "maestro-setup" / "SKILL.md"
    codex_src = SKILLS_DIR / "codex" / "maestro-setup.md"
    codex_dest = home / ".codex" / "prompts" / "maestro-setup.md"

    for src, dest in ((claude_src, claude_dest), (codex_src, codex_dest)):
        if not src.is_file():
            print(f"[install-skills] source missing: {src}")
            return 1
        linked = next((p for p in (dest, *dest.parents) if p.is_symlink()
                       and (p == home or home in p.parents)), None)
        if linked is not None:
            print(f"[install-skills] {linked} is a symlink (a canonical skill elsewhere); "
                  f"left unchanged, not overwritten with {src}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"[install-skills] {src} -> {dest}")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# argparse / entry point
# ══════════════════════════════════════════════════════════════════════════


def _resolve_repo(explicit: Optional[str]) -> Path:
    """`--repo`/positional `path` if given, else `$MAESTRO_REPO`, else cwd — the same
    resolution order `maestro.paths.Paths.from_env()` already uses, just made overridable
    from the command line too."""
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("MAESTRO_REPO")
    return Path(env).resolve() if env else Path.cwd()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro", description="Autonomous orchestration (DESIGN.md).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser('report', help='Read durable usage without controller ownership.')
    report_sub = p_report.add_subparsers(dest='verb', required=True)
    p_usage = report_sub.add_parser('usage', help='Task, role, route and fault usage as JSON.')
    p_usage.add_argument('--database', required=True, help='Existing saved or controller-owned SQLite database.')
    p_usage.add_argument('--task', default=None, dest='task_id', help='Limit output to this task ID.')

    p_models = sub.add_parser("models", help="Shared model policy, discovery and replacement.")
    p_models.add_argument("verb", nargs="?", default="pick", choices=["pick", "status", "register", "candidates", "adopt", "consume", "proposals",
                                   "review", "rollback", "retire", "asks", "sheet"])
    p_models.add_argument("action", nargs="?", default=None, help="asks: `process` drains the Telegram inbox.")
    p_models.add_argument("--json", action="store_true", dest="as_json")
    p_models.add_argument("--proposal", default=None, help="Proposal id to review (review).")
    p_models.add_argument("--revision", default=None, help="Policy revision to restore (rollback; default: previous).")
    p_models.add_argument("--backend", default=None, help="Backend whose modelctl-removed models to retire (retire).")
    p_models.add_argument("--repo", default=None)
    p_models.add_argument("--root", default=str(Path.home() / 'projects'))

    p_init = sub.add_parser("init", help="Scaffold a project for maestro (idempotent).")
    p_init.add_argument("path", nargs="?", default=".", help="Target repo (default: cwd)")

    p_doctor = sub.add_parser("doctor", help="Conformance checklist for an existing project.")
    p_doctor.add_argument("--repo", default=None, help="Target repo (default: $MAESTRO_REPO or cwd)")
    p_doctor.add_argument("--pre-commit", action="store_true",
                           help="Fast subset: docs + the required test adapter only.")
    # Mutually exclusive with --pre-commit: one asks for the fast pre-commit subset, the
    # other for the third-party gate alone, and a run cannot be both.
    p_doctor.add_argument("--thirdparty", action="store_true",
                           help="Third-party verification gate only (DESIGN.md §15); "
                                "exits non-zero on an unverified or drifted backend or dependency.")

    p_config = sub.add_parser("config", help="Show the config and the layer each key comes from.")
    p_config.add_argument("verb", choices=["show"])
    p_config.add_argument("--repo", default=None, help="Target repo (default: $MAESTRO_REPO or cwd)")
    p_config.add_argument("--effective", action="store_true",
                          help="The merged config: global defaults under project.yaml.")
    p_config.add_argument("--json", action="store_true", dest="as_json")

    p_status = sub.add_parser("status", help="Pretty-print state.json.")
    p_status.add_argument("--repo", default=None)

    p_run = sub.add_parser("run", help="Run the supervised orchestrator loop.")
    p_run.add_argument("--repo", default=None)

    p_ctl = sub.add_parser("ctl", help="Wraps maestro.hitl.commands' control-command handling.")
    p_ctl.add_argument("--repo", default=None)
    p_ctl.add_argument("verb")
    p_ctl.add_argument("args", nargs="*")

    p_roadmap = sub.add_parser(
        "roadmap", help="Validate docs/ROADMAP.md as a dependency model, or apply it.")
    p_roadmap.add_argument("--repo", default=None)
    p_roadmap.add_argument("verb", choices=["validate", "apply"])
    p_roadmap.add_argument("--expect-hash", default=None,
                           help="Compare-and-swap operand: the document hash you read. "
                                "The apply is refused if docs/ROADMAP.md moved since.")
    p_roadmap.add_argument("--json", action="store_true", dest="as_json",
                           help="Machine-readable validate output.")

    p_goals = sub.add_parser("goals", help="Validate docs/GOALS.md (the goals contract, W11a).")
    p_goals.add_argument("verb", choices=["check", "migrate", "baseline", "tasks", "status",
                                          "finish"])
    p_goals.add_argument("path", nargs="?", default=None,
                         help="A GOALS.md file or a project (default: $MAESTRO_REPO or cwd)")
    p_goals.add_argument("--json", action="store_true", dest="as_json")
    p_goals.add_argument("--gate", action="store_true",
                         help="Fail only on gate problems (execution), not drafting gaps.")
    p_goals.add_argument("--timeout", type=int, default=None,
                         help="baseline, finish: seconds allowed per criterion (default 300).")

    p_intake = sub.add_parser("intake", help="Prepare one intake proposal without running tasks.")
    p_intake.add_argument("verb", choices=["tick"])
    p_intake.add_argument("--repo", default=None)
    p_intake.add_argument("--telegram", action="store_true", help="Also deliver tagged receipts/proposals through the shared Maestro bot.")
    p_telegram = sub.add_parser('telegram', help='Shared Telegram bot and command list.')
    telegram_sub = p_telegram.add_subparsers(dest='verb', required=True)
    telegram_commands = telegram_sub.add_parser('commands')
    telegram_commands.add_argument('--botfather', action='store_true')
    telegram_sub.add_parser('poll', help='Run the sole shared bot poller.')
    telegram_register = telegram_sub.add_parser('register', help='Register a project for shared bot delivery.')
    telegram_register.add_argument('--repo', required=True)
    p_intake.add_argument("--publish", action="store_true", help="Also publish approved tasks under the idle controller lock.")

    p_workflow = sub.add_parser(
        "workflow", help="Operator views over a task's compiled execution graph.")
    p_workflow.add_argument("--repo", default=None)
    p_workflow.add_argument("verb", choices=["show", "runs", "failures"])
    p_workflow.add_argument("task_id", nargs="?")
    p_workflow.add_argument("--kind", default=None)
    p_workflow.add_argument("--run", default=None, dest="run_id",
                            help="An earlier run of this task (default: the latest).")
    p_workflow.add_argument("--html", action="store_true", dest="as_html",
                            help="Write a self-contained interactive graph/timeline page.")
    p_workflow.add_argument("--out", default=None,
                            help="Where --html writes (default: "
                                 ".orchestrator/artifacts/workflow-<task_id>.html)")
    p_workflow.add_argument("--json", action="store_true", dest="as_json")

    p_explain = sub.add_parser(
        "explain", help="Why a task stands where it does: criteria, bindings, evidence, "
                        "acceptance.")
    p_explain.add_argument("--repo", default=None)
    p_explain.add_argument("task_id")
    p_explain.add_argument("--run", default=None, dest="run_id")
    p_explain.add_argument("--json", action="store_true", dest="as_json")

    p_watchdog = sub.add_parser(
        "watchdog",
        help="Supervise the loop: relaunch `maestro run`, schedule resumes, detect stalls.")
    p_watchdog.add_argument("--repo", default=None)

    p_release = sub.add_parser(
        "release", help="Test this checkout's HEAD once; if green, announce it to every project.")
    p_release.add_argument("--checkout", default=None,
                           help="The maestro checkout (default: the one running this command)")

    p_install = sub.add_parser("install-skills", help="Install the setup skill for Claude + Codex.")
    p_install.add_argument("--home", default=None, help="Override $HOME (default: real $HOME)")

    return parser


def main(argv: Optional[list] = None) -> int:
    # First, before argparse and before any `cmd_*` binds `$MAESTRO_REPO`: if a version has
    # been adopted and this is not it, hand over to the one that is. Usually a no-op (returns
    # in place); when it acts, it never returns. See `maestro/bootstrap.py` — it imports
    # nothing from `maestro`, which is what makes calling it this early safe.
    bootstrap.reexec_into_adopted_version(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'report':
        import sqlite3
        from maestro.usage_report import build_usage_report, render_usage_report
        try:
            report = build_usage_report(Path(args.database), task_id=args.task_id)
        except (sqlite3.Error, OSError) as exc:
            print(f'usage report: {exc}', file=sys.stderr)
            return 1
        print(render_usage_report(report), end='')
        return 0

    if args.command == "models":
        from maestro.model_commands import run
        return run(args)

    if args.command == "init":
        return cmd_init(Path(args.path))
    if args.command == "doctor":
        if args.pre_commit and args.thirdparty:
            parser.error("--pre-commit and --thirdparty select different subsets; pick one")
        return cmd_doctor(_resolve_repo(args.repo), pre_commit=args.pre_commit,
                          thirdparty_only=args.thirdparty)
    if args.command == "config":
        return cmd_config(_resolve_repo(args.repo), args.verb, effective=args.effective,
                          as_json=args.as_json)
    if args.command == "status":
        return cmd_status(_resolve_repo(args.repo))
    if args.command == "run":
        return cmd_run(_resolve_repo(args.repo))
    if args.command == "ctl":
        return cmd_ctl(_resolve_repo(args.repo), args.verb, args.args)
    if args.command == "roadmap":
        return cmd_roadmap(_resolve_repo(args.repo), args.verb,
                           expect_hash=args.expect_hash, as_json=args.as_json)
    if args.command == "goals":
        return cmd_goals(_resolve_repo(args.path), verb=args.verb, as_json=args.as_json,
                         gate=args.gate, timeout=args.timeout)
    if args.command == "intake":
        return cmd_intake(_resolve_repo(args.repo), publish=args.publish, telegram=args.telegram)
    if args.command == 'telegram':
        from maestro.hitl import shared_bot
        if args.verb == 'poll':
            try:
                return shared_bot.run_polling()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        if args.verb == 'register':
            repo = _resolve_repo(args.repo)
            if not (repo / 'project.yaml').is_file():
                parser.error('telegram register requires project.yaml')
            shared_bot.register_project(repo)
            print(f'Registered [{shared_bot.project_name(repo)}] for shared Telegram delivery.')
            return 0
        from maestro.hitl.commands import COMMANDS, HELP_TEXT
        from maestro.hitl.command_registry import botfather_text
        print(botfather_text(COMMANDS) if args.botfather else HELP_TEXT)
        return 0
    if args.command == "workflow":
        if args.verb in ("show", "runs") and args.task_id is None:
            parser.error(f"workflow {args.verb} requires task_id")
        if args.verb == "show" and args.as_html and args.as_json:
            parser.error("--html and --json are two different renderings; pick one")
        return cmd_workflow(_resolve_repo(args.repo), args.verb, args.task_id,
                            as_html=args.as_html, as_json=args.as_json, out=args.out,
                            run_id=args.run_id, kind=args.kind)
    if args.command == "explain":
        return cmd_explain(_resolve_repo(args.repo), args.task_id, as_json=args.as_json,
                           run_id=args.run_id)
    if args.command == "watchdog":
        return cmd_watchdog(_resolve_repo(args.repo))
    if args.command == "install-skills":
        return cmd_install_skills(Path(args.home) if args.home else None)
    if args.command == "release":
        from maestro.selfupdate import release
        result = release(Path(args.checkout) if args.checkout else None)
        print(f"[release] {result}")
        return 0 if result == "up_to_date" or result.startswith("announced:") else 1

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover — argparse.error already raises SystemExit


if __name__ == "__main__":
    sys.exit(main())
