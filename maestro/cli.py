"""The `maestro` console-script entry point (DESIGN.md §10, `docs/plans/2026-08-16-m4-setup.md`).

Six subcommands: `init`, `doctor`, `status`, `run`, `ctl`, `watchdog`, plus `install-skills`.
`init`/`doctor`/`status`/`run`/`ctl` are Component D wiring `run_adapter` (Component A),
the templates (Component B) and the setup skill (Component C) together; none of the actual
orchestration logic lives here.

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
   a fresh scaffold has no `scripts/notify_telegram.sh`), which sets `state["paused_by_user"] =
   True`, then `time.sleep(POLL_INTERVAL)` (30s by default) and `continue`s.
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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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

    `test_command`/`package_manager` are heuristic, informational-only (there is no template
    placeholder for either — `templates/adapters/test` is a fixed pytest-first stub per
    Component B; this is a signal for the operator's console output, not a rendered value).
    """
    git_root_raw = _git(repo_root, "rev-parse", "--show-toplevel")
    git_root = Path(git_root_raw).resolve() if git_root_raw else repo_root.resolve()
    remote = _git(git_root, "remote", "get-url", "origin")
    branch = _git(git_root, "symbolic-ref", "--short", "HEAD") or "main"

    if (git_root / "package.json").is_file():
        test_command, package_manager = "npm test", "npm"
    elif (git_root / "pyproject.toml").is_file() or (git_root / "requirements.txt").is_file():
        test_command, package_manager = "pytest", "pip"
    elif (git_root / "go.mod").is_file():
        test_command, package_manager = "go test ./...", "go"
    else:
        test_command, package_manager = "", ""

    return {
        "repo_path": str(git_root),
        "project_name": git_root.name,
        "remote": remote,
        "main_branch": branch,
        "test_command": test_command,
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
    orch = repo_root / ".orchestrator"
    (orch / "workspaces").mkdir(parents=True, exist_ok=True)
    return [_scaffold_file(orch / "state.json", "{}\n")]


_GITIGNORE_BLOCK = """\
# maestro (DESIGN.md §10 scaffolding)
.orchestrator/
.venv/
__pycache__/
*.pyc
.env
.env.local
"""


def _run_telegram_creds(repo_root: Path, *, script_override: Optional[Path] = None) -> str:
    """Invoke `scripts/telegram_creds.py` as a subprocess — never read the token file
    ourselves (DESIGN.md's secret-handling rule). Any non-zero exit (no token file, rejected
    token, no inbound message, unwritable target) or the script simply not being present is
    treated the same way: skip Telegram setup, keep going, never fail `init` over it.

    `script_override` exists purely for tests: it lets a test point this at a small stand-in
    script instead of the real `scripts/telegram_creds.py`, so a test can never end up shelling
    out to the real script even indirectly (which would read the operator's real token file at
    `~/.config/maestro/dev_bot_token` — forbidden for a test under any circumstances) regardless
    of what `$MAESTRO_TOKEN_FILE` happens to resolve to in the test's environment.
    """
    script = script_override or (_MAESTRO_SOURCE_ROOT / "scripts" / "telegram_creds.py")
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
    flags = ("paused_by_user", "halted", "proposal_test_notified")
    if any(state.get(flag) for flag in flags):
        for flag in flags:
            state[flag] = False
        write_state(state)


def _run_noop_supervised_loop_check(
    repo_root: Path, *, timeout_s: int = 120, tmux_bin: str = "tmux",
) -> tuple[bool, str]:
    """Run `maestro run` against `repo_root` (an empty-roadmap project) to completion, as a
    subprocess so it never shares this process's already-imported, `$MAESTRO_REPO`-bound
    modules. See this module's docstring for why this is expected to return on its own, well
    inside `timeout_s`, and why the timeout is a safety net rather than the actual mechanism."""
    if not _ensure_agents_tmux_session(tmux_bin=tmux_bin):
        return False, f"could not create/find tmux session {_TMUX_SESSION!r} (is tmux installed?)"
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


def cmd_init(repo_root: Path, *, telegram_script_override: Optional[Path] = None) -> int:
    """`maestro init [path]` — DESIGN.md §10. Idempotent (see `_scaffold_file`); Telegram setup
    is best-effort (see `_run_telegram_creds`); ends with a dry-run `status` and the no-op
    supervised loop check before reporting success (see this module's docstring)."""
    repo_root = repo_root.resolve()
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
        # both empty) — the setup skill (Component C) is what fills these in from the
        # operator interview; init only renders the mechanical placeholder honestly.
        "deny_list": "(none yet — see project.yaml's risky_set / deny_list_extra)",
    }

    outcomes: list[ScaffoldOutcome] = []

    outcomes.append(_scaffold_file(
        repo_root / "project.yaml",
        _render((TEMPLATES_DIR / "project.yaml.tmpl").read_text(encoding="utf-8"), mapping),
    ))

    for kind_path in sorted((TEMPLATES_DIR / "adapters").iterdir()):
        outcomes.append(_scaffold_file(
            repo_root / "adapters" / kind_path.name,
            kind_path.read_text(encoding="utf-8"),
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

    outcomes.append(_scaffold_file(repo_root / ".gitignore", _GITIGNORE_BLOCK))

    outcomes.extend(_ensure_orchestrator_dirs(repo_root))
    _ensure_venv_python_symlink(repo_root)

    created = [o for o in outcomes if o.action == "created"]
    unchanged = [o for o in outcomes if o.action == "unchanged"]
    written_new = [o for o in outcomes if o.action == "new_written"]
    print(f"[init] scaffold: {len(created)} created, {len(unchanged)} unchanged, "
          f"{len(written_new)} written as .new (nothing already present was overwritten)")
    for o in written_new:
        print(f"\n[init] {o.dest} already exists and differs — wrote {o.dest}.new instead:")
        print(o.diff)

    telegram_result = _run_telegram_creds(repo_root, script_override=telegram_script_override)
    print(f"[init] Telegram setup: {telegram_result}")

    print("\n[init] dry-run `maestro status`:")
    status_rc = cmd_status(repo_root)
    if status_rc != 0:
        print("[init] status check reported a problem (see above) — continuing anyway.")

    print("\n[init] no-op supervised loop check (empty roadmap, zero runnable tasks)...")
    _reset_control_flags_for_loop_check(repo_root)
    loop_ok, loop_detail = _run_noop_supervised_loop_check(repo_root)
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
        runnable = parse_runnable_tasks()
        prep = parse_prep_tasks()
    except Exception as exc:  # noqa: BLE001 — doctor must never crash on a broken project
        return Check("docs", False, f"docs/ROADMAP.md did not parse: {exc}", required=True)
    return Check("docs", True, f"parsed OK — {len(runnable)} runnable, {len(prep)} prep task(s)",
                 required=True)


def _check_adapters(repo_root: Path) -> list[Check]:
    from maestro.adapters import KNOWN_KINDS, REQUIRED_KINDS, run_adapter
    checks = []
    for kind in KNOWN_KINDS:
        result = run_adapter(kind, repo_root, payload={}, timeout_s=60)
        required = kind in REQUIRED_KINDS
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


def _check_model_limits(repo_root: Path) -> Check:
    import warnings as _warnings
    from maestro import config as _config
    from maestro import limits
    cfg = _config.load_project_yaml()
    names: set[str] = set()
    for role in (cfg.get("roles") or {}).values():
        if isinstance(role, dict):
            names.update((role.get("models") or {}).values())
    if not names:
        return Check("model_limits", True, "no models declared in project.yaml roles — nothing to resolve")
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


def _check_backends(repo_root: Path) -> Check:
    from maestro import config as _config
    from maestro.backends import registry as backend_registry
    cfg = _config.load_project_yaml()
    backend_names: set[str] = set()
    for role in (cfg.get("roles") or {}).values():
        if isinstance(role, dict) and role.get("backend"):
            backend_names.add(backend_registry.normalise_name(role["backend"]))
    if not backend_names:
        backend_names = {backend_registry.DEFAULT_BACKEND}
    missing = []
    for name in sorted(backend_names):
        info = backend_registry.backend_binary(name)
        if info is None:
            missing.append(name)
    if missing:
        return Check("backends", False, f"binary not found on PATH for: {', '.join(missing)}")
    return Check("backends", True, f"resolved: {', '.join(sorted(backend_names))}")


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
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return Check("telegram", True, "no .env — Telegram not configured (optional)")
    values = dotenv.dotenv_values(env_path)
    token = values.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return Check("telegram", True, "no TELEGRAM_BOT_TOKEN in .env — skipped (optional)")
    response = http_get(f"https://api.telegram.org/bot{token}/getMe")
    if response.get("ok"):
        return Check("telegram", True, "getMe OK")
    return Check("telegram", False, f"getMe failed: {response}")


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


def cmd_doctor(repo_root: Path, *, pre_commit: bool = False,
                http_get: Optional[Callable[[str], dict]] = None) -> int:
    """`maestro doctor [--pre-commit]` — DESIGN.md §10's checklist. `--pre-commit` runs only the
    fast, required subset (docs + the `test` adapter) that `templates/pre-commit` probes for via
    `maestro doctor --help`. Exit code is non-zero iff a *required* check failed; optional checks
    print and nag but never affect the exit code."""
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    http_get = http_get or _default_http_get

    checks: list[Check] = [_check_docs(repo_root)]
    checks.extend(_check_adapters(repo_root))

    if not pre_commit:
        from maestro import config as _config
        cfg = _config.load_project_yaml()
        project_name = (cfg.get("project") or {}).get("name") or repo_root.name
        checks.append(_check_model_limits(repo_root))
        checks.append(_check_backends(repo_root))
        checks.append(_check_telegram(repo_root, http_get))
        checks.append(_check_systemd_unit(repo_root, project_name))
        checks.append(_check_gate(repo_root))
        checks.append(_check_journal_and_version(repo_root))

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
    print(f"Halted: {state.get('halted', False)}   Paused: {state.get('paused_by_user', False)}")
    parked = state.get("parked_tasks", [])
    if parked:
        print(f"Parked: {', '.join(parked)}")
    return 0


def cmd_run(repo_root: Path) -> int:
    """`maestro run` — invokes `maestro.orchestrator.main()` with `MAESTRO_REPO` set to
    `repo_root`. See this module's docstring for the no-op supervised loop behaviour on an
    empty roadmap and the tmux-session precondition `main()` itself enforces."""
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro.orchestrator import main as orchestrator_main
    return orchestrator_main()


#: `ctl` verbs whose logic is a trivial, fully-wired state transition (read_state/write_state/
#: append_journal/HALT_FILE) with no dependency on hitl.commands' Telegram-message-shaped
#: handlers. Kept separate from `_CTL_DISPATCH_VERBS` below deliberately: see `cmd_ctl`'s
#: docstring for why the two groups are handled differently.
_CTL_STATE_VERBS = ("pause", "resume", "halt", "hitl", "unpark")
#: `ctl` verbs that dispatch straight to the real `maestro.hitl.commands` handler with the same
#: name, since those already exist as callables independent of the Telegram `getUpdates` loop
#: they are normally invoked from.
_CTL_DISPATCH_VERBS = {
    "waiting": "_show_waiting", "manual": "_show_manual", "detail": "_show_detail",
    "approve": "_process_approve", "reject": "_process_reject", "fix": "_process_fix",
    "ask": "_handle_ask", "redo": "_handle_redo", "backend": "_process_backend",
}


def cmd_ctl(repo_root: Path, verb: str, args: list[str]) -> int:
    """`maestro ctl <verb> [args...]` — DESIGN.md §10, wrapping `maestro.hitl.commands`'
    existing control-command handling rather than reimplementing it.

    **Two groups, handled differently, on purpose.** `poll_control_commands` (the Telegram
    router this all normally lives behind) answers every command through `notify_telegram`,
    which is a silent no-op unless the project has `scripts/notify_telegram.sh` — fine for a
    Telegram-driven flow, useless for an operator watching a terminal. `pause`/`resume`/`halt`/
    `hitl`/`unpark` are simple enough (2-4 lines each in the reference `elif` chain) that this
    function reproduces the exact same `read_state`/`write_state`/`append_journal`/`HALT_FILE`
    primitives directly and *also* prints the result — not a reimplementation of logic, just
    the same three-line state transition with a second, stdout-shaped side channel. Every other
    verb (`waiting`, `manual`, `detail`, `approve`, `reject`, `fix`, `ask`, `redo`, `backend`)
    dispatches straight to `maestro.hitl.commands`' real `_show_waiting`/`_process_approve`/...
    functions, unmodified — their output stays Telegram-shaped (this function prints a note
    saying so) because refactoring them to return text instead of calling `notify_telegram`
    would mean editing `maestro/hitl/commands.py`, which is out of this component's scope.

    **A caution for real use, found while wiring this up (see docs/FOUND_BUGS.md /
    docs/PROGRESS.md's M4 entry):** `maestro/hitl/commands.py` still binds `_finalize_manual_
    action`, `park_regression`, `attempt_self_fix` and `_remove_from_state` to `pending()`
    placeholders (`maestro.pending`) that unconditionally raise `NotImplementedError`, even
    though the modules that now own them (`maestro.parking`, `maestro.selfheal.selffix`,
    `maestro.orchestrator`) have since been extracted for real — commands.py was never updated
    to import the real names. So `ctl approve <a real match>` and `ctl fix <a real match>`
    will raise `NotImplementedError` reaching those lines (an *existing* defect in already-
    extracted code, not something this file introduces or fixes); only the "no such pending
    item" early-return path is exercised by this build's own tests. `ctl reject` on a normal
    HITL-parked task also inherits the pre-existing, already-characterised `REPO_ROOT`
    `NameError` documented in `docs/found_bugs_inbox/commands.md`'s C1.
    """
    repo_root = repo_root.resolve()
    os.environ["MAESTRO_REPO"] = str(repo_root)
    from maestro.hitl import commands as hitl_commands
    from maestro.hitl.commands import HALT_FILE
    from maestro.state import append_journal, now_iso, read_state, write_state

    verb = verb.lower()

    if verb == "status":
        return cmd_status(repo_root)

    if verb in _CTL_STATE_VERBS:
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
        except NotImplementedError as exc:
            print(f"[ctl] {verb} hit an unresolved `pending()` placeholder in "
                  f"maestro.hitl.commands: {exc} — see this function's docstring.")
            return 1
        print(f"[ctl] dispatched to hitl.commands.{fn.__name__}() — its reply goes to Telegram "
              f"if this project has scripts/notify_telegram.sh, and to the journal where "
              f"applicable; nothing else is printed here by design (see this function's docstring).")
        return 0

    print(f"[ctl] unknown verb {verb!r}. Known: status, "
          f"{', '.join(_CTL_STATE_VERBS)}, {', '.join(_CTL_DISPATCH_VERBS)}")
    return 2


def cmd_watchdog() -> int:
    """`maestro watchdog` — HONEST STUB. `maestro/watchdog.py` does not exist; DESIGN.md §13
    still lists the stall-detection window and the definition of journal progress as open
    items, and building it now would mean inventing behaviour with no spec to extract from
    (see `docs/plans/2026-08-16-m4-setup.md`'s scope decision). `run`/`ctl`/`status` are real
    wrappers; this is not."""
    print("maestro watchdog: not yet implemented (maestro/watchdog.py does not exist — "
          "see docs/plans/2026-08-16-m4-setup.md's scope decision). Use `maestro run` under "
          "the systemd unit + tmux launcher `maestro init` scaffolds instead.")
    return 1


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
    siblings forever."""
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

    p_init = sub.add_parser("init", help="Scaffold a project for maestro (idempotent).")
    p_init.add_argument("path", nargs="?", default=".", help="Target repo (default: cwd)")

    p_doctor = sub.add_parser("doctor", help="Conformance checklist for an existing project.")
    p_doctor.add_argument("--repo", default=None, help="Target repo (default: $MAESTRO_REPO or cwd)")
    p_doctor.add_argument("--pre-commit", action="store_true",
                           help="Fast subset: docs + the required test adapter only.")

    p_status = sub.add_parser("status", help="Pretty-print state.json.")
    p_status.add_argument("--repo", default=None)

    p_run = sub.add_parser("run", help="Run the supervised orchestrator loop.")
    p_run.add_argument("--repo", default=None)

    p_ctl = sub.add_parser("ctl", help="Wraps maestro.hitl.commands' control-command handling.")
    p_ctl.add_argument("--repo", default=None)
    p_ctl.add_argument("verb")
    p_ctl.add_argument("args", nargs="*")

    sub.add_parser("watchdog", help="NOT YET IMPLEMENTED — honest stub, exits 1.")

    p_install = sub.add_parser("install-skills", help="Install the setup skill for Claude + Codex.")
    p_install.add_argument("--home", default=None, help="Override $HOME (default: real $HOME)")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return cmd_init(Path(args.path))
    if args.command == "doctor":
        return cmd_doctor(_resolve_repo(args.repo), pre_commit=args.pre_commit)
    if args.command == "status":
        return cmd_status(_resolve_repo(args.repo))
    if args.command == "run":
        return cmd_run(_resolve_repo(args.repo))
    if args.command == "ctl":
        return cmd_ctl(_resolve_repo(args.repo), args.verb, args.args)
    if args.command == "watchdog":
        return cmd_watchdog()
    if args.command == "install-skills":
        return cmd_install_skills(Path(args.home) if args.home else None)

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover — argparse.error already raises SystemExit


if __name__ == "__main__":
    sys.exit(main())
