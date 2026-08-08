# Maestro M0–M1: Characterisation Harness & Core Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the orchestration core out of `AbuAliArchive/scripts/orchestrator_run.py` (4,567 lines) into a tested, installable `maestro` package, without changing any behaviour and without modifying AbuAliArchive.

**Architecture:** Characterisation tests are written first against the *legacy* module, then re-pointed at the extracted modules via a parametrised `subject` fixture — the same test asserts identical behaviour from both implementations. Extraction proceeds bottom-up in dependency order (state → config → roadmap → worktree → gates → merge → telegram → commands → implementer → quota → selfheal → parking → orchestrator), each module landing green before the next starts.

**Tech Stack:** Python 3.11, pytest 9.1, PyYAML, python-dotenv, requests. No new runtime dependencies.

## Global Constraints

- **Behaviour-identical.** M0–M1 is a pure refactor. No feature changes, no bug fixes, no renames of externally-visible strings (journal event names, sentinel filenames, Telegram command names, `result.json` keys). If a bug is found, record it in `docs/FOUND_BUGS.md` and leave it in place.
- **AbuAliArchive is read-only for the whole of M0–M1.** No file in `/home/dan/projects/AbuAliArchive` may be created, modified, or deleted. The live loop keeps running its own scripts. Cutover is M5, a separate plan.
- **No project-identifying content in maestro.** No tokens, chat IDs, bot names, domain vocabulary, or eval specifics. Grep gate enforced in Task 2.
- **Python 3.11**; target `python_requires = ">=3.11"`.
- **Runtime dependencies limited to** `pyyaml`, `python-dotenv`, `requests`. Anything else needs a note in the commit message explaining why.
- **Legacy repo path comes from the `MAESTRO_LEGACY_REPO` environment variable**, defaulting to `/home/dan/projects/AbuAliArchive`. Never hardcode it in a module.
- **Frequent commits** — one per task minimum, conventional-commit style.
- Design source of truth: `docs/DESIGN.md` in this repo (commit `a6aec10`).

---

## File Structure

Created in this plan:

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, pytest config |
| `maestro/__init__.py` | `__version__` only |
| `maestro/state.py` | `.orchestrator/` runtime state, journal, launch-time persistence |
| `maestro/config.py` | `project.yaml` load + validation |
| `maestro/docs/roadmap.py` | Machine-readable ROADMAP parsing, task model, completion marking |
| `maestro/worktree.py` | git worktree + tmux window lifecycle |
| `maestro/gates.py` | Verification gate + smoke/adapter invocation |
| `maestro/merge.py` | Deny-list guard, risky review, merge, conflict auto-resolve |
| `maestro/hitl/telegram.py` | Telegram transport, human-request records, answer routing |
| `maestro/hitl/commands.py` | Control-command polling and the report/detail renderers |
| `maestro/implementer.py` | Brief construction, launch, sentinel synthesis |
| `maestro/quota.py` | Usage-limit detection, pause bookkeeping, concurrency throttle |
| `maestro/selfheal/` | Failure diagnosis, self-fix, redo |
| `maestro/parking.py` | All park/finalise/graduate transitions |
| `maestro/orchestrator.py` | The event loop |
| `tests/characterization/conftest.py` | `subject` fixture — parametrises legacy vs maestro |
| `tests/characterization/test_*.py` | Behaviour pinned across both implementations |

Deliberately **not** in this plan: `backends/`, `switch.py`, `roles.py`, `limits.py`, `selfupdate.py`, `cli.py`, `templates/`, `skills/`. Those are M2–M4 and get their own plans. `limits.py` in particular is M3 — until then the extracted code keeps the legacy hardcoded context constants verbatim.

---

## Function → Module Mapping

The authoritative mapping for Task 5 onward. Line numbers refer to
`AbuAliArchive/scripts/orchestrator_run.py` at commit `68056b5`.

| Target module | Functions to move |
|---|---|
| `state.py` | `now_iso` L100, `read_json` L104, `read_state` L111, `write_state` L122, `_persist_launch_time` L129, `append_journal` L141 |
| `config.py` | `_load_project_yaml` L273 |
| `docs/roadmap.py` | `get_completed_task_ids` L929, `parse_runnable_tasks` L939, `parse_prep_tasks` L975, `_normalize_task` L1024, `get_task_by_id` L1034, `_load_roadmap_tasks` L3078, `mark_roadmap_complete` L1225, `_roadmap_signature` L178, `run_dep_map` L148, `maybe_push_roadmap_map_change` L231 |
| `worktree.py` | `tmux_window_exists` L1098, `worktree_path_for` L1104, `create_worktree` L1108, `remove_worktree` L1123, `_kill_tmux_window` L3336, `_tail_tmux_pane` L483 |
| `gates.py` | `run_verification_gate` L1131, `sonnet_review_proofs` L1157, `_run_smoke` L1475, `_run_custom_smoke` L1494, `_smoke_for_task` L1509, `_await_absent` L2626, `_classify_verifications` L2639, `_await_timed_out` L2676 |
| `merge.py` | `_diff_files` L280, `deny_list_guard` L303, `sonnet_risky_reviewer` L337, `_touches_bot_files` L390, `_git_head` L1533, `_commit_eval_history` L1542, `merge_and_eval` L1568, `_clean_worktree_for_merge` L3679, `_autoresolve_doc_conflicts` L3716, `_merge_data_only` L3752, `_merge_prep_branch` L2435, `_merge_self_fix_branch` L2091, `_merge_redo_branch` L2182, `_resumable_code_change_escalation` L3788, `_parse_progress` L3745 |
| `hitl/telegram.py` | `notify_telegram` L173, `notify_telegram_with_map` L186, `_tg_api` L1805, `_danreq` L1674, `_pending_danreqs` L1820, `_record_danreq_answer` L1835, `_handle_danreq_callback` L1851, `_route_freetext_answer` L1885, `_escalation_req_id` L1909, `_send_hitl_reminders` L3279, `phase_report` L400 |
| `hitl/commands.py` | `poll_control_commands` L541, `_build_progress_report` L501, `_show_manual` L3100, `_show_detail` L3179, `_show_waiting` L3267, `_handle_ask` L2968, `_handle_redo` L3030, `_resolve_waiting` L2955, `_process_approve` L2841, `_process_reject` L2910, `_process_fix` L2258, `run_status` L169 |
| `implementer.py` | `_make_brief` L1282, `_make_prep_brief` L1363, `launch_implementer` L1430, `_synthesize_sentinel` L3342, `_tail_text` L3402, `_latest_impl_tail` L1927, `_answers_section` L1782, `_task_questions` L1692, `_question_req_id` L1714, `_answer_choice` L1719, `_ask_question` L1741, `_questions_ready` L1766 |
| `quota.py` | `_resolve_limit_reset` L3410, `_scan_impl_log_for_limit` L3441, `_pause_for_usage_limit` L3453, `_paused_until_epoch` L3484, `get_effective_cap` L1085, `_elapsed_min` L453, `_fmt_min` L462, `_elapsed_str` L467, `_parse_est_minutes` L473 |
| `selfheal/diagnose.py` | `_diagnose_failure` L1941, `_failure_looks_normal` L1971, `_persist_diagnosis` L2242, `_judge_complete` L702 |
| `selfheal/selffix.py` | `_self_fix_path_ok` L2011, `_self_fix_rate_ok` L2024, `_self_fix_eligible` L2047, `attempt_self_fix` L2062, `apply_ready_self_fixes` L2117 |
| `selfheal/redo.py` | `_redo_path_ok` L2169, `apply_ready_redo` L2208 |
| `parking.py` | `park_regression` L1901, `park_failed` L2292, `park_for_dan` L2403, `park_manual_action` L2468, `handle_prep_done` L2505, `_graduate_manual_action` L2686, `_park_awaiting_verification` L2708, `_surface_await_failure` L2734, `_finalize_manual_action` L2794, `handle_incomplete` L3873, `_session_uuid_for` L2390, `_retry_phrase` L2281 |
| `orchestrator.py` | `main` L3971, `reconcile_in_flight` L3496, `_timeout_expired` L3363, `_do_retry` L3602, `_remove_from_state` L3639, `_idle_heartbeat_maybe` L3959, `poll_awaiting_verifications` L2769, `launch_async_job` L2533, `generate_proposals` L806, `poll_proposal_answer` L876, `_notify_proposal_test_due` L788, `_resource_set` L1050, `_resources_conflict` L1064 |

**Complexity hotspots** — these need the most characterisation coverage before being touched:
`main` (cc=249), `poll_control_commands` (cc=46), `_show_manual` (cc=40), `park_failed` (cc=37),
`parse_prep_tasks` (cc=33), `_resolve_limit_reset` (cc=32), `parse_runnable_tasks` (cc=30),
`_show_detail` (cc=29), `_send_hitl_reminders` (cc=27).

`main` is not decomposed in this plan beyond moving it. Splitting a 592-line function with cc=249 is a
separate, higher-risk exercise that must happen *after* it is under test — it gets its own plan.

---

## Task 1: Package skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `maestro/__init__.py`
- Create: `tests/test_package.py`

**Interfaces:**
- Consumes: nothing
- Produces: importable package `maestro` exposing `maestro.__version__: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_package.py
import re

import maestro


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", maestro.__version__)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_package.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro'`

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "maestro"
version = "0.1.0"
description = "Project-agnostic autonomous orchestration system"
requires-python = ">=3.11"
dependencies = ["pyyaml>=6.0", "python-dotenv>=1.0", "requests>=2.31"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.setuptools.packages.find]
include = ["maestro*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

```python
# maestro/__init__.py
"""Maestro — project-agnostic autonomous orchestration."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Create the venv and install**

Run:
```bash
cd ~/projects/maestro
python3 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
```
Expected: `Successfully installed maestro-0.1.0`

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_package.py -v`
Expected: PASS, 1 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml maestro/__init__.py tests/test_package.py
git commit -m "feat: package skeleton"
```

---

## Task 2: Purity gate

Guards Global Constraint 3 — no project-identifying content may enter maestro. Runs as a test so it
cannot be forgotten during the extraction tasks.

**Files:**
- Create: `tests/test_purity.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing (a guard test)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_purity.py
"""Maestro must contain nothing that identifies any consuming project."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Case-insensitive. Extend only with genuinely project-identifying terms.
FORBIDDEN = [
    r"abuali",
    r"abu[_ -]?ali",
    r"@\w*DevBot",
    r"\d{9,}:[A-Za-z0-9_-]{30,}",   # Telegram bot token shape
]

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}


def _scanned_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix not in {".py", ".toml", ".md", ".yaml", ".yml", ".sh", ".json"}:
            continue
        # The design and plan docs legitimately name the source project.
        if p.name in {"DESIGN.md"} or "docs/plans" in p.as_posix():
            continue
        yield p


def test_no_project_identifying_strings():
    violations = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in FORBIDDEN:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                line = text[: m.start()].count("\n") + 1
                violations.append(f"{path.relative_to(ROOT)}:{line}: {m.group(0)!r}")
    assert not violations, "project-identifying content found:\n" + "\n".join(violations)


def test_no_hardcoded_legacy_repo_path():
    violations = []
    for path in _scanned_files():
        if path.as_posix().endswith("tests/test_purity.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "/home/dan/projects/AbuAli" in text:
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, f"hardcoded legacy path in: {violations}"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_purity.py -v`
Expected: PASS, 2 passed. (This guard starts green and must *stay* green — it fails the moment an
extraction task copies a project-specific string across.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_purity.py
git commit -m "test: purity gate against project-identifying content"
```

---

## Task 3: Characterisation harness

The mechanism that makes the whole extraction safe: one fixture serving either the legacy module or
the extracted one, so a single test body asserts both behave identically.

**Files:**
- Create: `tests/characterization/__init__.py` (empty)
- Create: `tests/characterization/conftest.py`
- Create: `tests/characterization/test_harness.py`

**Interfaces:**
- Consumes: `maestro.__version__`
- Produces:
  - `subject` fixture → module object, parametrised over `["legacy", "maestro"]`
  - `sandbox(subject, tmp_path)` fixture → `SimpleNamespace(repo, orch_dir, workspaces)` with the
    subject's module globals repointed at a temp tree
  - `pytest.mark.maestro_module(name)` marker → skips the `maestro` param until that module exists

- [ ] **Step 1: Write the failing test**

```python
# tests/characterization/test_harness.py
def test_subject_module_is_importable(subject):
    assert hasattr(subject, "now_iso")


def test_sandbox_isolates_state(subject, sandbox):
    subject.write_state({"version": "1", "in_flight": []})
    assert (sandbox.orch_dir / "state.json").exists()
    assert subject.read_state()["version"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/characterization -v`
Expected: FAIL — `fixture 'subject' not found`

- [ ] **Step 3: Write the harness**

```python
# tests/characterization/conftest.py
"""Runs one test body against both the legacy orchestrator and extracted maestro modules.

The legacy module has import-time side effects: it inserts `scripts/` on sys.path and
calls load_dotenv(REPO/".env", override=True), which loads the consuming project's LIVE
credentials into os.environ. The path argument is explicit, so there is no way to
redirect it — instead we snapshot os.environ around the import and restore it, so no
usable token ever survives into a test. Path globals are then repointed at a temp tree,
the same monkeypatching pattern the reference test suite uses.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

LEGACY_REPO = Path(os.environ.get("MAESTRO_LEGACY_REPO", "/home/dan/projects/AbuAliArchive"))

# module global -> subdirectory it must point at inside the sandbox
_PATH_GLOBALS = {
    "ORCH_DIR": ".orchestrator",
    "STATE": ".orchestrator/state.json",
    "JOURNAL": ".orchestrator/journal.ndjson",
    "WORKSPACES": ".orchestrator/workspaces",
    "USAGE": ".orchestrator/usage.json",
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "maestro_module(name): skip the maestro subject until that module is extracted",
    )


def _load_legacy():
    scripts = LEGACY_REPO / "scripts"
    if not (scripts / "orchestrator_run.py").is_file():
        pytest.skip(f"legacy repo not found at {LEGACY_REPO}")
    env_before = dict(os.environ)
    sys.path.insert(0, str(scripts))
    try:
        mod = importlib.import_module("orchestrator_run")
    finally:
        sys.path.remove(str(scripts))
        # The import loaded the consuming project's real .env. Restore the
        # environment so no live credential is reachable from any test.
        os.environ.clear()
        os.environ.update(env_before)
    return mod


def _load_maestro(request):
    marker = request.node.get_closest_marker("maestro_module")
    if marker is None:
        pytest.skip("test does not declare a maestro_module marker yet")
    name = marker.args[0]
    try:
        return importlib.import_module(f"maestro.{name}")
    except ModuleNotFoundError:
        pytest.skip(f"maestro.{name} not extracted yet")


@pytest.fixture(params=["legacy", "maestro"])
def subject(request):
    """The implementation under test."""
    if request.param == "legacy":
        return _load_legacy()
    return _load_maestro(request)


@pytest.fixture
def sandbox(subject, tmp_path, monkeypatch):
    """Repoint the subject's path globals at a temp tree. Never touches a real repo."""
    repo = tmp_path / "repo"
    orch_dir = repo / ".orchestrator"
    workspaces = orch_dir / "workspaces"
    workspaces.mkdir(parents=True)

    monkeypatch.setattr(subject, "REPO", repo, raising=False)
    for attr, rel in _PATH_GLOBALS.items():
        monkeypatch.setattr(subject, attr, repo / rel, raising=False)

    return SimpleNamespace(repo=repo, orch_dir=orch_dir, workspaces=workspaces)
```

```python
# tests/characterization/__init__.py
```
(empty file)

- [ ] **Step 4: Declare the marker on the harness test**

```python
# tests/characterization/test_harness.py — replace the file with this
import pytest


@pytest.mark.maestro_module("state")
def test_subject_module_is_importable(subject):
    assert hasattr(subject, "now_iso")


@pytest.mark.maestro_module("state")
def test_sandbox_isolates_state(subject, sandbox):
    subject.write_state({"version": "1", "in_flight": []})
    assert (sandbox.orch_dir / "state.json").exists()
    assert subject.read_state()["version"] == "1"


@pytest.mark.maestro_module("state")
def test_no_live_credentials_leak_into_the_test_process(subject):
    """Importing the reference module must not leave a usable token in os.environ."""
    import os
    import re

    for key, value in os.environ.items():
        if key.endswith(("_TOKEN", "_KEY")) and re.fullmatch(r"\d{9,}:[A-Za-z0-9_-]{30,}", value):
            raise AssertionError(f"live bot token leaked via {key}")
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/characterization -v`
Expected: 3 passed (legacy), 3 skipped (`maestro.state not extracted yet`)

- [ ] **Step 6: Verify the sandbox really is isolated**

Run:
```bash
cd /home/dan/projects/AbuAliArchive && git status --porcelain
```
Expected: **no new or modified files** beyond the pre-existing untracked ones
(`data/multivec_output_ft_v2.pkl.gz`, `data/reranker_onnx/`, `data/space_match_result.json`,
`eval/gold_real_v1.json`, `handoffs/2026-06-28_p8-postmortem-diagnosis-and-plan.md`).
If anything else appears, **stop** — the sandbox leaked and no further task may run.

- [ ] **Step 7: Commit**

```bash
git add tests/characterization/
git commit -m "test: characterisation harness parametrised over legacy and maestro"
```

---

## Task 4: Characterise state and journal

**Files:**
- Create: `tests/characterization/test_state.py`

**Interfaces:**
- Consumes: `subject`, `sandbox` fixtures from Task 3
- Produces: pinned behaviour for `read_json`, `read_state`, `write_state`, `append_journal`, `now_iso`

- [ ] **Step 1: Write the tests**

```python
# tests/characterization/test_state.py
import json
import re

import pytest

pytestmark = pytest.mark.maestro_module("state")


def test_now_iso_is_utc_zulu(subject):
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", subject.now_iso())


def test_read_json_missing_returns_empty_dict(subject, sandbox):
    assert subject.read_json(sandbox.repo / "nope.json") == {}


def test_read_json_malformed_returns_empty_dict(subject, sandbox):
    bad = sandbox.repo / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert subject.read_json(bad) == {}


def test_write_state_then_read_state_roundtrips(subject, sandbox):
    subject.write_state({"version": "1", "in_flight": [{"task_id": "T1"}]})
    got = subject.read_state()
    assert got["in_flight"] == [{"task_id": "T1"}]


def test_write_state_stamps_updated_at(subject, sandbox):
    subject.write_state({"version": "1"})
    assert "updated_at" in subject.read_state()


def test_append_journal_writes_one_ndjson_line_per_call(subject, sandbox):
    subject.append_journal("started", "first")
    subject.append_journal("finished", "second", session_id="s1")
    lines = (sandbox.orch_dir / "journal.ndjson").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["event"] == "started" and first["detail"] == "first"
    assert second["session_id"] == "s1"
    assert "ts" in first
```

- [ ] **Step 2: Run against legacy only, and record what it actually does**

Run: `.venv/bin/python -m pytest tests/characterization/test_state.py -v`
Expected: 6 passed (legacy), 6 skipped (maestro).

If any assertion fails, the legacy behaviour differs from what is written above. **Change the test to
match the legacy behaviour, not the other way round** — this is characterisation, not specification.
Note the surprise in `docs/FOUND_BUGS.md` if it looks wrong.

- [ ] **Step 3: Commit**

```bash
git add tests/characterization/test_state.py
git commit -m "test: characterise state and journal behaviour"
```

---

## Task 5: Extract `maestro/state.py`

The first extraction. Establishes the pattern every later module follows.

**Files:**
- Create: `maestro/state.py`
- Create: `maestro/paths.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `maestro.paths.Paths` — dataclass with `repo: Path`, and properties `orch_dir`, `state`, `journal`, `workspaces`, `usage`
  - `maestro.state.now_iso() -> str`
  - `maestro.state.read_json(path: Path) -> dict`
  - `maestro.state.read_state() -> dict`
  - `maestro.state.write_state(state: dict) -> None`
  - `maestro.state.append_journal(event: str, detail: str, session_id: str = "") -> None`
  - `maestro.state._persist_launch_time(sid: str, ts: float) -> None`
  - Module globals `REPO`, `ORCH_DIR`, `STATE`, `JOURNAL`, `WORKSPACES`, `USAGE` — kept as
    module-level names so the `sandbox` fixture can monkeypatch them exactly as it does for legacy

- [ ] **Step 1: Read the legacy source**

Run: `sed -n '95,150p' "$MAESTRO_LEGACY_REPO/scripts/orchestrator_run.py"`

Also read the surrounding global definitions (`REPO`, `ORCH_DIR`, `STATE`, `JOURNAL`, `WORKSPACES`,
`USAGE`) near the top of the file so the copied functions keep referring to the same names.

- [ ] **Step 2: Write `maestro/paths.py`**

```python
# maestro/paths.py
"""Filesystem layout of a maestro-managed project."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    repo: Path

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(Path(os.environ.get("MAESTRO_REPO", Path.cwd())))

    @property
    def orch_dir(self) -> Path:
        return self.repo / ".orchestrator"

    @property
    def state(self) -> Path:
        return self.orch_dir / "state.json"

    @property
    def journal(self) -> Path:
        return self.orch_dir / "journal.ndjson"

    @property
    def workspaces(self) -> Path:
        return self.orch_dir / "workspaces"

    @property
    def usage(self) -> Path:
        return self.orch_dir / "usage.json"
```

- [ ] **Step 3: Write `maestro/state.py`**

Copy the six functions **verbatim** from the legacy file, changing only the import block and keeping
the module-global path names. Do not reformat, rename, or "improve" the bodies.

```python
# maestro/state.py
"""Runtime state: state.json, journal.ndjson, launch-time bookkeeping.

Extracted verbatim from the reference implementation. Path globals are module
level so tests can repoint them without a config object.
"""
from __future__ import annotations

import json
from pathlib import Path

from maestro.paths import Paths

_paths = Paths.from_env()

REPO = _paths.repo
ORCH_DIR = _paths.orch_dir
STATE = _paths.state
JOURNAL = _paths.journal
WORKSPACES = _paths.workspaces
USAGE = _paths.usage

# ... the six function bodies, copied unchanged from the legacy module ...
```

- [ ] **Step 4: Run the characterisation tests against both subjects**

Run: `.venv/bin/python -m pytest tests/characterization/test_state.py -v`
Expected: **12 passed** — 6 legacy, 6 maestro. Zero skips.

Any maestro-side failure means the copy diverged. Fix by making the copy match legacy.

- [ ] **Step 5: Run the whole suite including the purity gate**

Run: `.venv/bin/python -m pytest -v`
Expected: all pass, including `tests/test_purity.py`.

- [ ] **Step 6: Confirm AbuAliArchive is still untouched**

Run: `cd /home/dan/projects/AbuAliArchive && git status --porcelain`
Expected: only the five pre-existing untracked entries listed in Task 3 Step 6.

- [ ] **Step 7: Commit**

```bash
git add maestro/paths.py maestro/state.py
git commit -m "refactor: extract state and journal into maestro.state"
```

---

## Task 6: Characterise and extract `maestro/config.py`

**Files:**
- Create: `tests/characterization/test_config.py`
- Create: `maestro/config.py`

**Interfaces:**
- Consumes: `subject`, `sandbox`
- Produces: `maestro.config.load_project_yaml() -> dict` (legacy name `_load_project_yaml`, re-exported
  under both names so characterisation can address either)

- [ ] **Step 1: Write the tests**

```python
# tests/characterization/test_config.py
import pytest

pytestmark = pytest.mark.maestro_module("config")

SAMPLE = """\
version: "1"
risky_set:
  - scripts/thing.py
deny_list_extra:
  - "DROP TABLE"
secrets:
  - .env
prod_stores: []
bot_files:
  - app.py
"""


def _loader(subject):
    return getattr(subject, "load_project_yaml", None) or subject._load_project_yaml


def test_missing_project_yaml_returns_empty_dict(subject, sandbox):
    assert _loader(subject)() == {}


def test_parses_lists(subject, sandbox):
    (sandbox.repo / "project.yaml").write_text(SAMPLE, encoding="utf-8")
    cfg = _loader(subject)()
    assert cfg["risky_set"] == ["scripts/thing.py"]
    assert cfg["deny_list_extra"] == ["DROP TABLE"]
    assert cfg["bot_files"] == ["app.py"]


def test_malformed_yaml_returns_empty_dict(subject, sandbox):
    (sandbox.repo / "project.yaml").write_text("key: [unclosed", encoding="utf-8")
    assert _loader(subject)() == {}
```

- [ ] **Step 2: Run against legacy and correct the tests to match reality**

Run: `.venv/bin/python -m pytest tests/characterization/test_config.py -v`
Expected: 3 passed (legacy), 3 skipped.

The legacy loader reads `REPO / "project.yaml"`. If it resolves the path differently, or raises on
malformed YAML instead of returning `{}`, **rewrite the test to match** and note it in
`docs/FOUND_BUGS.md`.

- [ ] **Step 3: Write `maestro/config.py`**

Copy `_load_project_yaml` verbatim, then add the public alias:

```python
# maestro/config.py
"""project.yaml loading."""
from __future__ import annotations

import yaml

from maestro.state import REPO

# ... _load_project_yaml copied unchanged ...

load_project_yaml = _load_project_yaml
```

- [ ] **Step 4: Run both subjects**

Run: `.venv/bin/python -m pytest tests/characterization/test_config.py -v`
Expected: 6 passed, zero skips.

- [ ] **Step 5: Commit**

```bash
git add tests/characterization/test_config.py maestro/config.py
git commit -m "refactor: extract project.yaml loading into maestro.config"
```

---

## Tasks 7–17: Remaining module extractions

Every remaining module follows the identical six-step cycle proven in Tasks 4–6. **Do not batch
them.** One module per task, one commit per task, full suite green before the next starts.

The cycle, restated in full so it can be executed without scrolling back:

1. Write `tests/characterization/test_<module>.py` with `pytestmark = pytest.mark.maestro_module("<module>")`,
   covering every function listed for that module in the Function → Module Mapping table, plus each
   error path the legacy code handles explicitly.
2. Run it against legacy only. **Where legacy surprises you, change the test, not the code.** Record
   surprises in `docs/FOUND_BUGS.md`.
3. Create `maestro/<module>.py`, copying the mapped functions **verbatim** — imports rewritten,
   bodies untouched.
4. Run `.venv/bin/python -m pytest tests/characterization/test_<module>.py -v` — expect double the
   count, zero skips.
5. Run the full suite (`.venv/bin/python -m pytest -v`), then
   `cd /home/dan/projects/AbuAliArchive && git status --porcelain` to confirm the source repo is
   untouched.
6. Commit: `refactor: extract <area> into maestro.<module>`.

Order is fixed by dependency — each module may import only from modules extracted before it:

| Task | Module | Depends on | Notes |
|---|---|---|---|
| 7 | `docs/roadmap.py` | state, config | Highest-value early win; `parse_prep_tasks` cc=33 and `parse_runnable_tasks` cc=30 need table-driven tests over every task-block shape in the reference `ROADMAP.md` |
| 8 | `worktree.py` | state | Tests must use a real throwaway `git init` repo in `tmp_path`, never the live one. `tmux_window_exists` / `_kill_tmux_window` are stubbed via monkeypatched `subprocess.run` |
| 9 | `quota.py` | state | `_resolve_limit_reset` cc=32 — feed it the real limit-message strings from `.orchestrator/` impl logs; cover the unparseable case |
| 10 | `gates.py` | state, config | Adapter invocation is JSON stdin→stdout; test with a temp executable shell script adapter, not a mock |
| 11 | `implementer.py` | state, config, worktree | Assert brief *content contracts* (required sections present) rather than exact text, so wording changes don't false-fail |
| 12 | `merge.py` | state, config, worktree | Needs a throwaway git repo with a real conflicting doc commit to exercise `_autoresolve_doc_conflicts` and `_clean_worktree_for_merge` |
| 13 | `hitl/telegram.py` | state, config | **All network calls monkeypatched.** A test that reaches api.telegram.org is a defect. Assert on the request payload |
| 14 | `hitl/commands.py` | all above | `poll_control_commands` cc=46 — one test per command verb (`/status`, `/progress`, `/pause`, `/resume`, `/halt`, `/waiting`, `/manual`, `/detail`, `/ask`, `/redo`, `/approve`, `/reject`, `/fix`), plus unknown-verb handling |
| 15 | `selfheal/` | state, config, merge | Three modules (`diagnose`, `selffix`, `redo`) in one task — they share the rate-limit and path-allowlist helpers. `_judge_complete` shells out to an agent CLI: monkeypatch `subprocess.run` |
| 16 | `parking.py` | all above | `park_failed` cc=37 — cover each park reason and the retry-count escalation text |
| 17 | `orchestrator.py` | everything | Moves `main` (cc=249) **unchanged**. Characterise `reconcile_in_flight` and `_timeout_expired` thoroughly; for `main`, assert only that it is importable and that one poll cycle over an empty queue produces the expected journal events. Splitting `main` is explicitly out of scope |

---

## Task 18: Integration check and M1 close-out

**Files:**
- Create: `docs/FOUND_BUGS.md` (if not already created during earlier tasks)
- Modify: `maestro/__init__.py`

- [ ] **Step 1: Assert every mapped function has a home**

```python
# tests/test_extraction_complete.py
"""Every function in the mapping table must exist in its target maestro module."""
import importlib

import pytest

MAPPING = {
    "state": ["now_iso", "read_json", "read_state", "write_state", "append_journal"],
    "config": ["load_project_yaml"],
    "docs.roadmap": ["parse_runnable_tasks", "parse_prep_tasks", "get_task_by_id",
                     "get_completed_task_ids", "mark_roadmap_complete"],
    "worktree": ["worktree_path_for", "create_worktree", "remove_worktree", "tmux_window_exists"],
    "quota": ["get_effective_cap"],
    "gates": ["run_verification_gate"],
    "implementer": ["launch_implementer"],
    "merge": ["deny_list_guard", "merge_and_eval"],
    "hitl.telegram": ["notify_telegram"],
    "hitl.commands": ["poll_control_commands"],
    "parking": ["park_failed", "park_for_dan", "park_manual_action"],
    "orchestrator": ["main", "reconcile_in_flight"],
}


@pytest.mark.parametrize("module,names", sorted(MAPPING.items()))
def test_module_exports(module, names):
    mod = importlib.import_module(f"maestro.{module}")
    missing = [n for n in names if not hasattr(mod, n)]
    assert not missing, f"maestro.{module} missing {missing}"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_extraction_complete.py -v`
Expected: 12 passed

- [ ] **Step 3: Run the full suite and record the numbers**

Run: `.venv/bin/python -m pytest -v 2>&1 | tail -5`
Expected: all pass, zero skips in `tests/characterization/`. **Any remaining skip means a module was
never extracted** — go back and finish it.

- [ ] **Step 4: Confirm the source repo was never touched across the entire plan**

Run:
```bash
cd /home/dan/projects/AbuAliArchive && git status --porcelain && git log --oneline -1
```
Expected: the same five untracked entries and the same HEAD (`68056b5`) as when the plan started.

- [ ] **Step 5: Bump the version and commit**

```python
# maestro/__init__.py
__version__ = "0.2.0"
```

```bash
git add maestro/__init__.py tests/test_extraction_complete.py docs/FOUND_BUGS.md
git commit -m "feat: complete M1 core extraction

All orchestration functions extracted into maestro modules, each pinned by
characterisation tests running against both the reference implementation and
the extracted code. No behaviour changes. Source project untouched."
```

---

## Definition of Done

- [ ] `.venv/bin/python -m pytest -v` — all green, **zero skips** under `tests/characterization/`
- [ ] Every function in the Function → Module Mapping table lives in its target module
- [ ] `tests/test_purity.py` green — no project-identifying content anywhere in maestro
- [ ] `AbuAliArchive` HEAD still `68056b5` with only its five pre-existing untracked files
- [ ] The live loop was never halted — `pgrep -f orchestrator_run.py` still returns a PID
- [ ] `docs/FOUND_BUGS.md` records every behavioural surprise found, none of them fixed
- [ ] `maestro.__version__ == "0.2.0"`

## Next

M2 (backend drivers and mid-work switching) gets its own plan, written against `docs/DESIGN.md` §6–§7
once this one is green.
