# M4b — Extract `maestro/watchdog.py` (unplanned pre-M5 stage)

**Date:** 2026-08-17
**Status:** plan written before any code, per `docs/EXECUTION.md` "Per-stage loop" step 2.
**Predecessor:** M4a (`de7d1e6`). **Successor:** M5 (cutover), which this stage unblocks.

---

## 1. Why this stage exists

M5 cuts `AbuAliArchive` over to maestro and **deletes the superseded scripts listed in DESIGN.md
§11**. That list includes `scripts/watchdog.py` (322 lines) and `scripts/watchdog-launcher.sh`.

`maestro/watchdog.py` does not exist. M4 left it out deliberately and honestly (see PROGRESS.md's M4
judgement calls: it was never in the M0–M1 mapping table, and DESIGN.md §13 still lists watchdog
stall-detection semantics as an open item), so `maestro/cli.py`'s `watchdog` subcommand is a stub
that prints a message and exits 1, and the generated `launch.sh` runs `maestro run` directly.

**Running M5 as specified in that state would regress the operator's production system.** The
reference watchdog is not a nicety; it is the supervision layer DESIGN.md §3 assigns to maestro
("Liveness, resume scheduling, stall detection, HALT, control plane"). Deleting it and running
`maestro run` under systemd loses, concretely:

| Reference behaviour | Lost if M5 runs today |
|---|---|
| `orchestrator_alive()` + `launch_orchestrator()` — relaunch the loop if it dies inside tmux | systemd only supervises the *launcher*; `Restart=on-failure` fires when the tmux session ends, not when the orchestrator process inside it dies |
| `ensure_resume_job()` — schedule a resume at the quota reset time | The orchestrator exits on quota exhaustion expecting the watchdog to bring it back (`orchestrator_run.py` says so in three separate comments). Nothing would. The loop stays down until a human notices. |
| Journal-silence stall detection (`STALL_WINDOW_MIN = 20`, `MAX_STALL_RESTARTS = 3` → HALT) | A wedged orchestrator sits wedged indefinitely |
| `reap_dead_implementers()` | `in_flight` accumulates entries for processes that no longer exist, and the concurrency cap silently strangles the loop |

There is no way to keep the reference watchdog *and* cut over: its `launch_orchestrator()` launches
`scripts/orchestrator_run.py`, which M5 deletes. Editing the reference watchdog to launch maestro
instead would leave project-specific orchestration code inside the consuming project, which
DESIGN.md §3 ("No orchestration code is copied into a project") and §4's seam forbid.

So: extract it, exactly as M1 extracted everything else, then cut over.

## 2. Scope

**In scope**

1. `maestro/watchdog.py` — the reference `scripts/watchdog.py` extracted under M1 rules.
2. `tests/characterization/test_watchdog.py` — dual-subject, reference vs extracted.
3. `maestro/cli.py` — `cmd_watchdog()` becomes real; its stub text and `--help` go away.
4. `maestro/templates/launch.sh.tmpl` and `templates/systemd/maestro-watchdog.service.tmpl` — the
   honesty notes about a nonexistent module are replaced by a real `maestro watchdog` invocation.
5. The `limits.py` slug↔display-name mismatch carried from M4 (§6 below). Small, independent, and
   `doctor` runs during M5 — worth closing in the same session, not a separate stage.

**Out of scope** (recorded so it is a decision, not an omission)

- Any change to `AbuAliArchive`. It stays read-only until M5. This stage reads its scripts; it
  writes nothing.
- Inventing supervision behaviour the reference does not have. DESIGN.md §13's open item ("exact
  stall-detection window and the definition of journal progress") is **answered by extraction, not
  by design**: the window is 20 minutes, the strike count is 3, and journal progress is the mtime
  /last line of the journal — because that is what the reference does and what the orchestrator's
  `IDLE_HEARTBEAT_S = 600` comment is calibrated against. Those become defaults, overridable in
  `project.yaml`; no new semantics are introduced.
- The M4 `pyproject.toml` package-data issue (wheel installs). M5 runs from this checkout.

## 3. Rules for every agent in this stage

Identical to M1's, because this is an M1-shaped extraction arriving late:

- **Copy function bodies verbatim.** Change only the import block, the path globals (derive from
  `maestro.paths.Paths`, per the `maestro/prep_actions.py` precedent), and project-specific
  constants that must become configuration. Do not reformat, rename, simplify, or fix.
- **If the reference code looks wrong, append an entry to `docs/FOUND_BUGS.md` and copy it wrong
  anyway.**
- **`/home/dan/projects/AbuAliArchive` is READ-ONLY.** Read its files; never write, never `git` in
  it, never touch `.orchestrator/` or the `HALT` sentinel.
- **Run no git commands at all.** The orchestrating session commits.
- **Never read, print, `cat`, `grep` or echo `~/.config/maestro/dev_bot_token` or any `.env`.**
- A test that reaches the network is a defect. Monkeypatch.
- `tests/test_purity.py` must stay green: no project-identifying content in `maestro/`.

## 4. Shape

One `Workflow`, three waves, 5 agents (guideline for this machine: under 15).

**Wave A — parallel, different files, no conflicts**

| Agent | Writes | Verifies with |
|---|---|---|
| `chars:watchdog` | `tests/characterization/test_watchdog.py` | tests pass against the `legacy` subject; the `maestro` half skips with `maestro.watchdog not extracted yet` |
| `fix:limits-slugs` | `maestro/limits.py`, `tests/test_limits.py` | `pytest tests/test_limits.py` green; slugs and display names both resolve |

Wave A's test author works against the reference only. The `maestro` half skipping is the expected
M1 Wave-A state; Wave B closes it.

**Wave B — serial, depends on Wave A**

| Agent | Writes | Verifies with |
|---|---|---|
| `extract:watchdog` | `maestro/watchdog.py`, `docs/FOUND_BUGS.md` (append only) | `pytest tests/characterization/test_watchdog.py` green on **both** subjects, zero skips |
| `wire:cli` | `maestro/cli.py`, `maestro/templates/launch.sh.tmpl`, `maestro/templates/systemd/maestro-watchdog.service.tmpl`, `tests/test_cli.py`, `tests/test_templates.py` | `pytest tests/test_cli.py tests/test_templates.py` green |

**Wave C — adversarial verification**

| Agent | Checks |
|---|---|
| `verify:fidelity` | Every extracted function body is byte-identical to the reference's modulo the declared import/path changes; no path global can resolve inside a live repo; `test_purity.py` green; no network call reachable; `test_no_unresolved_pending.py` green |

## 5. `maestro/watchdog.py` — the generalisations that are unavoidable

The reference watchdog names its project in four places. Each needs a generic source. Nothing else
about it changes.

| Reference | maestro |
|---|---|
| `REPO` and every `.orchestrator/*` path derived from it | `Paths.from_env()`, as `maestro/prep_actions.py` does |
| `NOTIFY_SCRIPT = REPO/"scripts"/"notify_telegram.sh"` (itself on the §11 delete list) | `maestro.hitl.telegram`'s notifier — already extracted in M1 |
| The tmux session/window names (`abuali-watchdog`, and the orchestrator's window) | `project.name` from `project.yaml`, matching `cli.py`'s existing `{project_name}-watchdog` convention |
| `launch_orchestrator()` shelling to `scripts/launch_orchestrator.py` (delete list) | `maestro run`, i.e. the console script `cli.py` already installs |
| `STALL_WINDOW_MIN = 20`, `MAX_STALL_RESTARTS = 3` | Same values as defaults, overridable under `project.yaml`'s `thresholds:` |

`ensure_resume_job()`'s scheduling mechanism is whatever the reference uses; it is extracted as-is
and must not acquire a new one. **If it requires `sudo`, it is not extracted and this plan is
wrong** — flag it and stop, per EXECUTION.md's abort conditions.

## 6. The `limits.py` slug fix

M4 finding #2: the tables key by display name (`Claude Opus 5`), `project.yaml`'s `roles:` block
uses slugs (`claude-opus-5`), so `doctor`'s `model_limits` check resolves nothing on every real
project. Verified against both live tables, a single normalisation closes it exactly — no mapping
table, no guessing:

```
"Claude Sonnet 5" → claude-sonnet-5      "GPT-5.6 Luna"  → gpt-5.6-luna
"Claude Opus 5"   → claude-opus-5        "GPT-5.6 Terra" → gpt-5.6-terra
                                          "GPT-5.6 Sol"   → gpt-5.6-sol
```

i.e. casefold, then spaces → hyphens. **Lookup becomes tolerant of both spellings; the tables keep
their human-readable keys and `ModelLimits.model` keeps reporting the display name** (M3's
`resolve_all()` Done-when asserts against display names and must stay green). Exact-match wins over
normalised match, so a table that ever keys by slug directly still behaves.

## 7. Done when

Every command run and its output seen — never a reported number that was not observed:

1. `python3 -m pytest -q` → exit 0, **zero `F`/`E`/`s` markers**. `--collect-only` count recorded
   against M4a's 4270 baseline, and the delta explained exactly by the new files.
2. `pytest tests/characterization/test_watchdog.py` → green on both subjects, **zero skips**.
3. `maestro watchdog --help` no longer says "not yet implemented"; the stub exit-1 path is gone.
4. `python3 -c "import maestro.watchdog"` in a fresh interpreter — no import cycle.
5. `tests/test_purity.py`, `tests/test_no_unresolved_pending.py`, `tests/test_extraction_complete.py`
   all green (the last still 12 passed — `watchdog` was never in the mapping table, and that test
   pins the plan's contract; it is not edited to match new work, same call M4a made for
   `prep_actions`).
6. Slugs resolve: `limits.resolve("claude-opus-5")` and `limits.resolve("Claude Opus 5")` both
   return an entry.
7. **The reference-project check, before and after:** HEAD `68056b5`, `git status --porcelain`
   exactly the 4 standing-modified + 5 untracked baseline, `stat -c '%s %Y' .orchestrator/state.json`
   → `888 1786256976`, HALT sentinel present. Any difference aborts the programme.

## 8. What M5 then does differently

Nothing in the M5 safety protocol changes. Two notes for the session that runs it:

- The superseded-script deletion is now genuinely safe for `watchdog.py`, `watchdog-launcher.sh`,
  `launch_orchestrator.py` and `notify_telegram.sh` — every one has a maestro owner. (M4a did the
  same for `prepared_actions.py`.)
- The installed systemd unit must be swapped to the maestro-generated one **in the same step** as
  the deletion, or the operator is left with a unit whose `ExecStart` points at a deleted file.
  Reverting that unit file is part of the rollback script M5 writes first.
- EXECUTION.md's M5 step 8 says the last action is confirming `pgrep -f orchestrator_run.py` returns
  a PID. **After a successful cutover that process no longer exists by that name** — the check has
  to become "the loop is running again" under its maestro name. Flagged here rather than silently
  reinterpreted at 3am; the *rolled-back* path still uses the literal check.
