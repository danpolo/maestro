# M5 cutover survey — raw research annex

**Date:** 2026-08-18. **Produced by:** a 3-agent read-only `Workflow` fan-out (253,226 subagent
tokens, 113 tool calls) run before writing the M4c plan. Nothing in the reference project was
written, no git command was run there, no `.env` or token file was read.

Kept verbatim because it is expensive to reproduce and the M4c/M5 sessions will need it. Read the
plan (`2026-08-18-m4c-superseded-sidecars.md`) first; this is the evidence behind it.

Known noise in the text below: agent C's report opens with a harness banner about an
"instruction-shaped pattern" (it had quoted `.claude/settings.json` lines) and has backslash-escaped
newlines in places. Content is unaffected.


---

## Agent A — how the reference loop is launched and supervised today

# TASK A — Reference project autonomous loop: launch & supervision (as deployed today)

All facts below were read from files on disk. Nothing was executed that writes. I did **not** read `/home/dan/projects/AbuAliArchive/.env` (exists, 874 B, dated Jun 14) nor `~/.config/maestro/dev_bot_token`.

---

## 0. Current state (measured, read-only)

| Fact | Value |
|---|---|
| HALT sentinel | `/home/dan/projects/AbuAliArchive/.orchestrator/HALT` — exists, 0 bytes, mtime `Aug 9 09:07` |
| Last journal event | `{"ts": "2026-08-10T11:46:02Z", "event": "watchdog_halt_respected", "agent": "B3-watchdog", "detail": "HALT file present"}` |
| `abuali-watchdog.service` | `is-enabled` = **enabled**; `is-active` = **inactive** (`SubState=dead`, `Result=success`, `ExecMainStatus=0`) |
| Orchestrator processes | `pgrep -af 'watchdog.py|orchestrator_run.py|launch_orchestrator.py'` returned **no AbuAli processes** (only the unrelated `/home/dan/services/claude-bridge/watchdog.py`, PID 2313514) |
| tmux `agents` session | **exists**, 1 window: `1:bash` (created Aug 18 21:08) |
| tmux `abuali-watchdog` session | **absent** (not in `tmux list-sessions`) |
| `state.json` | `halted` key **absent**; `phase.id = "LAT1"`, `status = in_progress`; `in_flight = []`; `waiting_on_dan` has one entry (dan_id `7`, task `P12C`); `parked_tasks = ["TUNE2","LAT1"]` |
| Crontab | 16 entries, **none** contain `cron-orch-resume`. All AbuAli-related resume entries are absent. (Unrelated `maestro-resume-eval` entry exists.) |

So the loop is stopped by both the HALT sentinel **and** an inactive systemd unit; only the shared `agents` tmux session survives.

---

## 1. `scripts/watchdog-launcher.sh` — verbatim

Path: `/home/dan/projects/AbuAliArchive/scripts/watchdog-launcher.sh` (1496 bytes, mode 0755, mtime Jun 26 18:46)

```bash
#!/usr/bin/env bash
# systemd wrapper for watchdog.py.
#
# Runs as Type=simple so systemd tracks THIS process (not the tmux child).
# Creates the named tmux session so Dan can audit via: tmux a -t abuali-watchdog
# Blocks in a wait loop; exits when the session ends → systemd restarts the service.

set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Kill any stale session from a previous run.
/usr/bin/tmux kill-session -t abuali-watchdog 2>/dev/null || true

# Reboot-proofing: ensure the shared 'agents' session exists. The watchdog spawns
# the orchestrator (and cron-resume) via `tmux new-window -t agents`, which fails if
# the session is missing — and a reboot wipes the tmux server with nothing else
# recreating 'agents'. This service is enabled-on-boot and restarts whenever the
# tmux server dies, so guaranteeing it here covers reboot / server-death / paused
# (cron-resume) paths. Idempotent: an existing 'agents' session is left untouched.
/usr/bin/tmux has-session -t agents 2>/dev/null \
    || /usr/bin/tmux new-session -d -s agents -n main

# Launch watchdog in a named, detached session (per CLAUDE.md tmux rule).
/usr/bin/tmux new-session -d -s abuali-watchdog -n main \
    "cd $REPO && $REPO/.venv/bin/python3 scripts/watchdog.py"

# Block here; systemd tracks this shell as the "service process".
# When watchdog exits (HALT, crash, stop command), the session disappears and we exit.
while /usr/bin/tmux has-session -t abuali-watchdog 2>/dev/null; do
    sleep 10
done
```

Notes: `set -e` only (no `-u`, no `pipefail`). `REPO` is derived from the script's own location, so the interpreter path passed to tmux is literally `$REPO/.venv/bin/python3` = `/home/dan/projects/AbuAliArchive/.venv/bin/python3`.

---

## 2. `scripts/launch_orchestrator.py`

Path: `/home/dan/projects/AbuAliArchive/scripts/launch_orchestrator.py` (10253 bytes, 0755, 236 lines).

**What it launches** (line 209):
```python
result = subprocess.run([str(VENV_PYTHON), str(ORCHESTRATOR)], cwd=str(REPO))
```
i.e. `/home/dan/projects/AbuAliArchive/.venv/bin/python3 /home/dan/projects/AbuAliArchive/scripts/orchestrator_run.py` with cwd = repo root. It is a **blocking foreground child** — the launcher waits for the orchestrator to exit and returns its exit code (line 232).

**Constants (lines 32–44):**
| Name | Value |
|---|---|
| `REPO` | `Path(__file__).resolve().parent.parent` → `/home/dan/projects/AbuAliArchive` |
| `STATE_JSON` | `.orchestrator/state.json` |
| `USAGE_JSON` | `.orchestrator/usage.json` |
| `JOURNAL` | `.orchestrator/journal.ndjson` |
| `HALT_FILE` | `.orchestrator/HALT` |
| `ORCHESTRATOR` | `scripts/orchestrator_run.py` |
| `VENV_PYTHON` | `.venv/bin/python3` |
| `QUESTIONS` | `.orchestrator/questions` |
| `TMUX_SESSION` | `"agents"` |
| `FIVE_H_PAUSE_PCT` | `92.0` |

**Control flow of `main()` (lines 134–232):**
1. L138 `cleanup_stale_resume_cron()` — `crontab -l`, strips any line containing `cron-orch-resume`, pipes back via `crontab -`. **This mutates the user crontab.**
2. L141–147 informational log if `state.blocked_on` entries have `.answer` files.
3. L153–157 if `state.halted` truthy → journal `launch_skipped_halted`, return 0.
4. L160–162 if `.orchestrator/HALT` exists → print, return 0 (no journal write).
5. L165–178 `paused_until` in the future → `schedule_resume(usage.five_hour.resets_at)`, return 0. If expired → clear it and write state.
6. L186–202 proactive gate: if `usage.five_hour.used_pct >= 92.0` → write `paused_until` (ISO), `schedule_resume(...)`, return 0.
7. L205–212 journal `orchestrator_launch`, run the orchestrator, journal `orchestrator_exit` with exit code.
8. L214–230 reactive net: re-read `usage.json` after exit; if `>= 92%`, set `paused_until` and schedule resume cron.

**Environment it sets: none.** There is no `env=` argument and no `os.environ` assignment anywhere in the file (verified — the file contains no `os` import at all). The orchestrator inherits the environment of the tmux pane that ran the launcher. Credentials come later, from inside `orchestrator_run.py` itself, which does `load_dotenv(REPO / ".env", override=True)` at lines 93–97.

**tmux usage in this file:** it does **not** create a window for itself. The only tmux reference is the cron resume command it writes (lines 91–94):
```
/usr/bin/tmux new-window -t agents -n 'cron-orch-resume' 'cd /home/dan/projects/AbuAliArchive && /home/dan/projects/AbuAliArchive/.venv/bin/python3 /home/dan/projects/AbuAliArchive/scripts/launch_orchestrator.py'
```
scheduled as a `M H D M *` crontab line (line 97–100) computed from `datetime.fromtimestamp(resets_at)` in **local** time. Guarded by a substring check for `cron-orch-resume` in existing crontab (L104).

**Writes it performs:** `state.json` (atomic via `.json.tmp` + rename, L62–66), appends to `journal.ndjson` (L69–72), and the user crontab (L109, L121).

---

## 3. `scripts/watchdog.py` — restricted report

Path: `/home/dan/projects/AbuAliArchive/scripts/watchdog.py` (13697 bytes, 0755, 322 lines).

**tmux names (lines 35–36):**
```python
TMUX_SESSION  = "agents"
TMUX_WINDOW   = "orchestrator"
```
It spawns the orchestrator with (lines 94–98):
```
tmux new-window -t agents -n orchestrator 'cd /home/dan/projects/AbuAliArchive && /home/dan/projects/AbuAliArchive/.venv/bin/python3 /home/dan/projects/AbuAliArchive/scripts/launch_orchestrator.py'
```
The watchdog itself lives in the separate session `abuali-watchdog` (window `main`), created by the launcher shell, not by this file.

**How it decides the orchestrator is alive** — `orchestrator_alive()`, lines 76–81:
```python
r = subprocess.run(["pgrep", "-f", r"launch_orchestrator\.py|orchestrator_run\.py"], capture_output=True)
return r.returncode == 0
```
A host-wide `pgrep -f` on the two script filenames. No PID file, no cgroup, no user scoping. The kill path (L298–301) is the symmetric `pkill -f 'launch_orchestrator\.py|orchestrator_run\.py'`.

Liveness/stall are two separate signals: process existence (`pgrep`) plus **journal advancement** measured on `time.monotonic()` — `journal_last_line()` (L134–150) returns the raw last line of `journal.ndjson`, and `last_journal_change` is reset whenever that line changes (L201–204). Deliberately never compares wall-clock timestamps.

**`main()` control flow (L174–318), 15 lines:**
1. Journal `watchdog_start`; init `stall_restarts=0`, `last_launch_mono=0.0`, `last_journal_seen/last_journal_change`.
2. Loop forever, `time.sleep(POLL_INTERVAL)` (30 s) at the bottom; whole body wrapped in `try/except` that journals `watchdog_error`.
3. If `.orchestrator/HALT` exists → journal `watchdog_halt_respected`, **return 0** (process exits; tmux session dies; launcher loop exits).
4. Read `state.json`; refresh the monotonic stall timer if the journal's last line changed.
5. If `state.halted` → journal `watchdog_idle_halted` once, sleep, continue (no relaunch).
6. If `paused_until` in future → `ensure_resume_job(usage.five_hour.resets_at)`, sleep, continue; if expired → clear `paused_until` and rewrite state atomically.
7. If `state.blocked_on` non-empty and **not** all `questions/<id>.answer` exist → sleep, continue; if all answered → clear `blocked_on`, journal `blocked_on_cleared`, fall through.
8. If `state.in_flight` **and** `orchestrator_alive()` → `reap_dead_implementers()`, sleep, continue.
9. If `state.waiting_on_dan` → reset `last_journal_change` (HITL park is not a stall).
10. `alive = orchestrator_alive()`.
11. Stall test: `alive` and journal silent > 20 min and > 20 min since this watchdog's last launch.
12. On stall: increment `stall_restarts`; at `>= 3` → journal `watchdog_halt_stall`, `HALT_FILE.touch()`, `notify(...)`, **return 1**.
13. Otherwise journal `stall_restart`, `pkill -f ...`, sleep 2, force `alive = False`, reset the stall window.
14. If `not alive` → record `last_launch_mono`, `launch_orchestrator()` (the tmux new-window above).
15. `reap_dead_implementers()` — for each `in_flight` entry with a live tmux window whose `#{pane_dead}` is `1` (or the query fails), `tmux kill-window -t agents:<window>` and journal `implementer_reaped`.

Tunables (L38–40): `POLL_INTERVAL = 30`, `STALL_WINDOW_MIN = 20`, `MAX_STALL_RESTARTS = 3`.

---

## 4. `scripts/orchestrator_status.py`

Path: `/home/dan/projects/AbuAliArchive/scripts/orchestrator_status.py` (7976 bytes, mode 0644, 231 lines). **Not executed.**

**Inputs it reads (L23–27):**
- `.orchestrator/state.json`
- `.orchestrator/usage.json`
- `.orchestrator/journal.ndjson` (last line only, L189–196)
- `docs/ROADMAP.md` — parsed by regex ` ```yaml … ``` ` blocks (L29, L41–58), `yaml.safe_load` with a hand-rolled `_manual_yaml` fallback (L61–75)

All four exist today (`docs/ROADMAP.md` 20101 B; `usage.json` 214 B).

**Output format** (`print_status()`, L121–197) — literal shape:
```
============================================================
  ORCHESTRATOR STATUS
============================================================

Phase:        <id>  <title>
Status:       <phase.status>
Halted:       YES|no
Paused until: <YYYY-MM-DD HH:MM UTC | —>
Paused by user: yes|no
Blocked on:   <comma list>          # only when non-empty
Queue ref:    <state.queue_ref|?>
Updated at:   <state.updated_at|?>

In-flight (N):
  [status  ] <task_id>  role=<role>  started=<ts>  window=<window|—>
  (none)                              # when empty

--- Usage (statusline sampler) ---
  Model:   <model>
  Updated: <ts>
  Context: <n.n%>
  5h limit: <n.n%>  resets <YYYY-MM-DD HH:MM UTC>
  7d limit: <n.n%>  resets <YYYY-MM-DD HH:MM UTC>

--- Task queue (docs/ROADMAP.md) ---
  <flag> <id>    [<mode>     | <eval_relevance>]  READY|ACTIVE|ready (needs-dan)|blocked on: a, b

Last journal event: [<ts>] <event> — <detail>
```
`flag` is `*` for ACTIVE, otherwise a space (L184). Missing files degrade to `[!] .orchestrator/state.json not found — runtime not initialized.`, `.orchestrator/usage.json not found.`, `(no tasks or ROADMAP.md missing)`.

`--json` (L200–216) prints `json.dumps({"state":…, "usage":…, "tasks":[…]}, indent=2)`.

**Provably read-only: YES.** Full audit of the file:
- Imports (L16–21): `argparse`, `json`, `re`, `sys`, `datetime`, `pathlib`. **No `os`, no `subprocess`, no `shutil`.** (`sys` is imported but never referenced anywhere in the file — dead import.)
- A scan for `open(`, `subprocess`, `write_text`, `write(`, `mkdir`, `touch`, `rename`, `unlink`, `os.` returned **zero matches**.
- Every filesystem call is `Path.exists()` or `Path.read_text()` (L33, L36, L42, L45, L142/`JOURNAL.exists()`, L190).
- Side effects are limited to `print()` on stdout and a lazy `import yaml` inside a `try` (L47).

One caveat worth stating: `_task_readiness()` (L103–118) initialises `done_ids = set()` and never populates it, so any task with `deps` is always reported "blocked on: …". That is a display bug, not a write.

---

## 5. Python interpreter / virtualenv

| Item | Measured value |
|---|---|
| venv root | `/home/dan/projects/AbuAliArchive/.venv/` |
| Interpreter used by every launcher | **`/home/dan/projects/AbuAliArchive/.venv/bin/python3`** |
| `.venv/bin/python3` | symlink → `/usr/bin/python3` |
| `.venv/bin/python` | symlink → `python3` (same target) |
| `.venv/bin/python3.11` | symlink → `python3` |
| Version | `Python 3.11.2` |
| `pyvenv.cfg` | `home = /usr/bin`, `include-system-site-packages = false`, `version = 3.11.2`, `executable = /usr/bin/python3.11`, `command = /usr/bin/python3 -m venv /home/dan/projects/AbuAliArchive/.venv` |
| `pyproject.toml` | **does not exist** |
| `setup.py` | **does not exist** |
| `requirements.txt` | exists (932 B), pinned by lower bound only: torch, sentence-transformers, sqlite-vec, datasets, accelerate, peft, scikit-learn, google-genai, numpy, python-dateutil, pydantic, requests, beautifulsoup4, python-telegram-bot, apscheduler, tqdm, python-dotenv, langfuse, tavily-python, dspy-ai, FlagEmbedding, scipy, pytest |

Exact interpreter strings appearing in launchers/units:
- `watchdog-launcher.sh` → `$REPO/.venv/bin/python3 scripts/watchdog.py` (resolves to `/home/dan/projects/AbuAliArchive/.venv/bin/python3`)
- `watchdog.py` L33 / `launch_orchestrator.py` L38 / `orchestrator_run.py` L47 → `REPO / ".venv" / "bin" / "python3"`
- `systemd/abuali-bot.service` → `/home/dan/projects/AbuAliArchive/.venv/bin/python telegram_bot.py` (note: `python`, not `python3`)
- `systemd/abuali-entities.service` → `/home/dan/projects/AbuAliArchive/.venv/bin/python generate_entities.py`
- `scripts/notify_telegram.sh` → bare `python3` (system interpreter, stdlib only)

---

## 6. tmux naming convention (as evidenced in code)

**Sessions**
| Session | Created by | Windows |
|---|---|---|
| `abuali-watchdog` | `scripts/watchdog-launcher.sh` (`tmux new-session -d -s abuali-watchdog -n main`) | `main` — runs `watchdog.py` |
| `agents` | shared, ensured idempotently by `watchdog-launcher.sh` (`has-session -t agents || new-session -d -s agents -n main`) | all orchestrator work windows, below |
| `abuali-bot` | `systemd/abuali-bot.service` (`tmux new-session -d -s abuali-bot -n main`) | `main` — `telegram_bot.py` |

Pattern: **one long-lived service = one `<project>-<role>` session with a single window named `main`; all ephemeral agent work = windows inside the shared `agents` session.**

**Window names inside `agents`** (every `tmux new-window` in `scripts/`):
| Window name | Source |
|---|---|
| `orchestrator` | `scripts/watchdog.py:95` (`TMUX_WINDOW`) |
| `cron-orch-resume` | `scripts/watchdog.py:110` and `scripts/launch_orchestrator.py:92` |
| `impl-<task_id>` | `scripts/orchestrator_run.py:1433` + `:1467` |
| `selffix-<task_id>` | `scripts/orchestrator_run.py:2078` |
| `ask-<task_id>` | `scripts/orchestrator_run.py:3016` + `:3017` |
| `redo-<task_id>` | `scripts/orchestrator_run.py:3064` |
| `p8b6-eval` | `scripts/p8b6_run_eval.sh:93` |
| `p14-post-eval` | `scripts/p14_cutover.sh:41` |

The shared `agents` session is a machine-wide convention, not AbuAli-specific: the user crontab has 16 entries, most of which do `tmux new-window -t agents -n 'cron-<something>'` for hive-assistant, codex-bridge, etc.

`orchestrator_run.py` hard-fails at startup if the session is missing — L3975–3977:
```python
if subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION], ...
    print(f"[ERROR] tmux session '{TMUX_SESSION}' not found.")
```

---

## 7. systemd units

**In-repo:** `/home/dan/projects/AbuAliArchive/systemd/` — `abuali-bot.service`, `abuali-entities.service`, `abuali-entities.timer`, `abuali-nightly-eval.service`, `abuali-nightly-eval.timer`, `abuali-watchdog.service`.

**Deployed system-wide** (`/etc/systemd/system/`, root-owned): `abuali-bot.service`, `abuali-entities.service`, `abuali-entities.timer`, `abuali-watchdog.service`, plus a drop-in dir `abuali-watchdog.service.d/`. Enabled via `multi-user.target.wants/` symlinks for `abuali-bot.service` and `abuali-watchdog.service`. `abuali-nightly-eval.{service,timer}` are deployed instead as **user** units in `/home/dan/.config/systemd/user/`.

Deployed `abuali-watchdog.service` (root-owned, 756 B, Jun 4 14:31):
```
Type=simple
User=dan
WorkingDirectory=/home/dan/projects/AbuAliArchive
ExecStart=/bin/bash /home/dan/projects/AbuAliArchive/scripts/watchdog-launcher.sh
ExecStop=/usr/bin/tmux kill-session -t abuali-watchdog
Restart=on-failure
RestartSec=60
[Install] WantedBy=multi-user.target
```

Drop-ins (root-owned):
- `60-resource-guard.conf` (Aug 10 14:41): `MemoryHigh=3G`, `MemoryMax=4G`, `OOMPolicy=kill`
- `70-ignore-missing-tmux-session.conf` (Aug 10 14:46): resets and re-sets `ExecStop=-/usr/bin/tmux kill-session -t abuali-watchdog` (leading `-`) so a clean HALT exit does not trigger an auto-restart loop

**Drift note:** the in-repo `systemd/abuali-watchdog.service` (757 B, Jun 26 18:46) already carries the `ExecStop=-` form inline; the deployed copy is the older Jun 4 file and relies on the drop-in for the same behaviour. The deployed file has **not** been re-synced from the repo.

---

## 8. Observations relevant to a Maestro cutover (measured, not opinion)

1. **`pgrep -f` / `pkill -f` are host-wide and unscoped** (`watchdog.py:78`, `:299`). The pattern `launch_orchestrator\.py|orchestrator_run\.py` matches any process on the machine with those strings in its command line, regardless of repo or user. Any Maestro copy that keeps identically-named scripts would be seen as "alive" by this watchdog and killed by its stall path.
2. **The `agents` tmux session is shared** with hive-assistant, codex-bridge and 16 cron entries. `abuali-watchdog` and `abuali-bot` are the only AbuAli-owned session names. Window-name collisions (`orchestrator`, `cron-orch-resume`) are the concrete risk if a second project reuses them.
3. **Cron is a mutable shared resource.** `launch_orchestrator.py:114-122` (`cleanup_stale_resume_cron`) rewrites the whole user crontab, filtering only lines containing `cron-orch-resume`; `schedule_resume` (L102–111) appends. A crash between `crontab -l` and `crontab -` loses the whole table. There are currently 16 unrelated entries in that table.
4. **The resume cron uses `M H D M *`** — day-of-month and month, no year. A resume more than a month out silently fires on the wrong date; the local-time conversion (`datetime.fromtimestamp`, no tz) is also DST-sensitive.
5. **HALT is checked in three places with different semantics**: `watchdog.py:191` (exit the watchdog entirely), `launch_orchestrator.py:160` (skip launch, return 0), and `watchdog.py:293` (`HALT_FILE.touch()` after 3 stalls). The soft switch is `state.halted` (checked *before* HALT at `launch_orchestrator.py:154`, per the in-file "Problem 9 fix" comment).
6. **`state.json` currently has no `halted` key** but does have a stale `waiting_on_dan["7"]` entry parked since 2026-06-21 (task P12C, a Colab manual action, last reminded 2026-06-25). If HALT were removed today, `watchdog.py` step 9 would treat this as a permanent HITL park and keep the stall timer perpetually fresh.
7. **`.orchestrator/` contains 13 orphaned `usage.json.tmp<PID>` files** (dates Jun 5 – Jun 27), evidence of interrupted atomic writes by the statusline sampler.
8. **`journal.ndjson` is 7.6 MB** and `watchdog.py:145` does `JOURNAL.read_text()` — full file into memory — once per 30 s poll, plus once more per poll via `journal_last_line()` inside the loop, and `orchestrator_status.py:190` does the same.


---

## Agent B — what installing maestro into an existing project does

# TASK B — What `maestro init` actually does to a target repo

All facts below were read from source. I ran **no** maestro command, **no** git command, **no** systemctl/crontab, and read **no** `.env` or token file.

---

## 1. `cmd_init` — exact ordered list of what it creates/touches

Entry: `/home/dan/projects/maestro/maestro/cli.py:358` (`cmd_init`). Argv: `maestro init [path]`, positional only, default `"."` (`cli.py:955-956`, dispatched at `cli.py:988` as `cmd_init(Path(args.path))`). **`init` ignores `$MAESTRO_REPO` and has no `--repo` flag** — unlike every other subcommand.

Precondition (`cli.py:363-366`): if `<repo>/.git` does not exist **and** `git rev-parse --git-dir` fails → prints "is not a git repository" and returns 1.

Ordered scaffold (each via `_scaffold_file`, `cli.py:187`):

| # | cli.py line | Relative path written in target repo | Template source | Rendered? | exec bit |
|---|---|---|---|---|---|
| 1 | 385 | `project.yaml` | `maestro/templates/project.yaml.tmpl` | yes | no |
| 2 | 391 | `adapters/deploy` | `maestro/templates/adapters/deploy` | no | yes |
| 3 | 391 | `adapters/eval` | `maestro/templates/adapters/eval` | no | yes |
| 4 | 391 | `adapters/healthcheck` | `maestro/templates/adapters/healthcheck` | no | yes |
| 5 | 391 | `adapters/latency` | `maestro/templates/adapters/latency` | no | yes |
| 6 | 391 | `adapters/smoke` | `maestro/templates/adapters/smoke` | no | yes |
| 7 | 391 | `adapters/test` | `maestro/templates/adapters/test` | no | yes |
| 8 | 397 | `operating_preamble.md` | `maestro/templates/operating_preamble.md` | yes | no |
| 9 | 403 | `profiles/implementer.md` | `maestro/templates/profiles/implementer.md` | yes | no |
| 10 | 403 | `profiles/judge.md` | `maestro/templates/profiles/judge.md` | yes | no |
| 11 | 403 | `profiles/orchestrator.md` | `maestro/templates/profiles/orchestrator.md` | yes | no |
| 12 | 409 | `docs/ROADMAP.md` | `maestro/templates/docs/ROADMAP.md` | yes | no |
| 13 | 409 | `docs/PROJECT.md` | `maestro/templates/docs/PROJECT.md` | yes | no |
| 14 | 414 | `.git/hooks/pre-commit` | `maestro/templates/pre-commit` | no | yes |
| 15 | 420 | `launch.sh` | `maestro/templates/launch.sh.tmpl` | yes | yes |
| 16 | 425 | `systemd/<project_name>-watchdog.service` | `maestro/templates/systemd/maestro-watchdog.service.tmpl` | yes | **no** |
| 17 | 433 | `.gitignore` | inline `_GITIGNORE_BLOCK` (`cli.py:236-244`), not a file template | no | no |
| 18 | 435→232 | `.orchestrator/state.json` (content `"{}\n"`) | inline | — | no |

Adapters and profiles are iterated with `sorted(...iterdir())`, so the order above is exact.

Non-`_scaffold_file` side effects, in order:
- `cli.py:435` / `_ensure_orchestrator_dirs` (`cli.py:229-232`): `mkdir -p .orchestrator/workspaces` — always, no diff protection.
- `cli.py:436` / `_ensure_venv_python_symlink` (`cli.py:218-227`): if `<repo>/.venv/bin/python3` does not exist and is not a symlink, `mkdir -p .venv/bin` and **symlink `.venv/bin/python3 → sys.executable`**. Skipped entirely if the path already exists (so a real venv is untouched).
- `cli.py:447`: Telegram credential step (§3 below) — may write/modify `<repo>/.env`.
- `cli.py:451`: runs `cmd_status(repo_root)` in-process (read-only).
- `cli.py:456` `_reset_control_flags_for_loop_check` (`cli.py:289-332`): **mutates an existing `.orchestrator/state.json`** — if any of `paused_by_user`, `halted`, `proposal_test_notified` is truthy, it sets all three to `False` and calls `write_state`.
- `cli.py:457` `_run_noop_supervised_loop_check` (`cli.py:334-356`): calls `_ensure_agents_tmux_session` (`cli.py:275-287`) which runs `tmux has-session -t agents` and, if absent, **creates the shared `agents` tmux session**; then runs `sys.executable -m maestro.cli run --repo <repo>` as a subprocess with `timeout=120`, cwd = target repo. **This actually starts the real orchestrator loop against the target repo.** If it does not return 0, `cmd_init` prints "FAILED" and returns 1 (after scaffolding).
- `cli.py:463-471`: prints the three `sudo` commands for the systemd unit; never runs them.

**Cutover-relevant:** on a repo that already has orchestration state, `init` clears HALT/pause flags in `state.json` and launches a live `maestro run`. Those two are the only writes it makes that are not protected by the `.new` rule.

---

## 2. Idempotency — verified per path

`_scaffold_file` (`cli.py:187-216`) implements exactly three branches:

1. `dest` does not exist → write content, chmod +x if `executable=True`, action `"created"`.
2. `dest` exists and content is **byte-identical** → do nothing, action `"unchanged"`. **No `.new` file is written** in this case (the design's "always write .new" is not what the code does; the docstring at `cli.py:187-197` states this deviation explicitly).
3. `dest` exists and differs → write `dest.name + ".new"` **beside** the original, chmod +x on the `.new` if `executable=True`, build a `difflib.unified_diff(old, new, fromfile=dest, tofile=dest.new)`, action `"new_written"`. The original is never touched.

The diff is printed per file at `cli.py:441-443`; the summary line is `cli.py:438-440`.

So, per path, on an existing repo: `project.yaml.new`, `adapters/test.new`, `docs/ROADMAP.md.new`, `docs/PROJECT.md.new`, `.gitignore.new`, `launch.sh.new`, `.git/hooks/pre-commit.new`, `.orchestrator/state.json.new`, etc.

Consequences I verified by reading, worth flagging for a cutover into a repo that already has these files:
- `.gitignore` is written as a **whole-file replacement candidate**, not appended. An existing `.gitignore` gets `.gitignore.new` containing only maestro's 7-line block — the operator must merge by hand.
- `.git/hooks/pre-commit`: if a hook already exists, maestro's goes to `.git/hooks/pre-commit.new`, which git never executes → the hook is silently not installed.
- `.orchestrator/state.json`: if a live state file exists, `state.json.new` containing `{}` is written next to it (harmless, but noise).
- The two *unprotected* mutations are the flag reset and the live `maestro run` (§1).

Exceptions to the `.new` rule: `_ensure_orchestrator_dirs`' mkdir, `_ensure_venv_python_symlink`, the `state.json` flag reset, and `cmd_install_skills` (`cli.py:906-935`) which **always overwrites** by design.

---

## 3. Telegram credentials

- Controlled by `_run_telegram_creds` (`cli.py:246-272`), called unconditionally from `cmd_init` at `cli.py:447`. There is **no CLI flag** to enable/disable it; the only parameter is `telegram_script_override`, a keyword-only test seam (`cmd_init(..., telegram_script_override=...)`), not reachable from argv.
- Script invoked: `<maestro checkout>/scripts/telegram_creds.py` (`cli.py:258`, resolved from `_MAESTRO_SOURCE_ROOT`, `cli.py:96`), as `subprocess.run([sys.executable, str(script), str(repo_root / ".env")], timeout=30)`. `cli.py` never reads the token itself.
- If the script is missing, or exits non-zero, or times out → prints `skipped (...)` and `init` continues. It never fails `init`.
- What the script does (`/home/dan/projects/maestro/scripts/telegram_creds.py`, read but **not executed**): reads the token file at `$MAESTRO_TOKEN_FILE` or `~/.config/maestro/dev_bot_token` (line 34), calls `getMe`, calls `getUpdates` and takes the **last** inbound chat id, then upserts `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` into the target `.env` (preserving other lines) and `chmod 0600` it. Exit codes: 0 written, 2 token file missing/unreadable/empty, 3 token rejected, 4 no inbound message, 5 `.env` unwritable. Only the chat id is printed; the token never is.
- I did not read, print, or resolve any token file. I confirmed the *path constant only*.
- Nothing in `maestro/` loads `.env` into the environment. `python-dotenv` is used in exactly one place: `cmd_doctor`'s `_check_telegram` (`cli.py:581-585`, `dotenv_values`). At runtime `maestro/hitl/telegram.py:73-74` and `maestro/hitl/commands.py:136` read `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALERT_CHAT_ID` from `os.environ` — **something outside maestro must export them**, or the `.env` is inert.

---

## 4. `project.yaml` version "2" schema, field by field

Written from `maestro/templates/project.yaml.tmpl`. Only three placeholders are substituted: `{{ project_name }}`, `{{ repo_path }}`, `{{ main_branch }}` (mapping built at `cli.py:373-381`).

```
version: "2"
project:
  name: "{{ project_name }}"      # git toplevel basename
  repo_path: "{{ repo_path }}"    # resolved git toplevel
  remote: origin                  # literal, NOT the derived remote URL
  main_branch: "{{ main_branch }}"# currently checked-out branch, or "main"
roles:
  orchestrator: {backend: claude, models: {claude: claude-opus-5,   codex: gpt-5.6-sol}}
  implementer:  {backend: claude, models: {claude: claude-sonnet-5, codex: gpt-5.6-terra}}
  judge:        {backend: claude, models: {claude: claude-opus-5,   codex: gpt-5.6-sol}}
fallback_chain: [claude, codex]
switch: {on_quota_exhausted: true, on_usage_threshold: {five_hour_pct: 85, weekly_pct: 90}, manual: true}
model_limits: {claude: ~/.claude/model_context_limits.md, codex: ~/.codex/model_context_limits.md}
gate: {chain: [test], primary_metric: null, baseline_source: null,
       bands: {claim: null, flat: null, block: null}}
telegram:
  chat_id: <captured by init>
thresholds: {five_h_pause_pct: 92, concurrency_cap: 3}
risky_set: []
deny_list_extra: []
secrets: [".env", ".env.local", "**/*.pem", "**/*credentials*.json"]
prod_stores: []
docs: {roadmap: docs/ROADMAP.md, project: docs/PROJECT.md, dependency_map: docs/dependency_map.md}
```

Derivation of the rendered values (`_derive_repo_facts`, `cli.py:153-186`): `repo_path` = `git rev-parse --show-toplevel` resolved (falls back to the passed dir); `project_name` = **basename of that git root**; `main_branch` = `git symbolic-ref --short HEAD`, else `"main"`. `remote` is derived (`git remote get-url origin`) but **has no placeholder in the template** — it is only printed. Same for `test_command`/`package_manager` (informational, `cli.py:363-372`).

### What `maestro/config.py` requires at runtime

**Nothing.** `config.py:23-29`: `_load_project_yaml()` is `yaml.safe_load(PROJECT_YAML.read_text()) or {}` wrapped in a bare `except Exception: return {}`. Missing file, unreadable file, a directory, a syntax error, and an empty document all collapse to `{}`. There is no schema check, no `version` check, and no error is surfaced. `PROJECT_YAML` is bound **at import time** to `Paths.from_env().repo / "project.yaml"` (`config.py:16-18`).

Every consumer therefore uses `.get()` with a fallback — verified at each call site:

| Key | Read by | Behaviour when missing |
|---|---|---|
| `roles.<role>.backend` / `.models` | `maestro/roles.py:133` (`_project`/`_role_entry`, lines 125-150); `cli.py:528,553` | non-mapping → `{}`; falls back to registry `DEFAULT_BACKEND` |
| `fallback_chain` | `maestro/roles.py` `_clean_chain` | non-list → `()`; unregistered names dropped |
| `model_limits_paths` | `maestro/limits.py:164-173` | falls back to `~/.claude/…` + `~/.codex/…`. **Note the template writes `model_limits:`, not `model_limits_paths:` — the template key is never read by `limits.py`** |
| `thresholds.stall_window_min` / `.max_stall_restarts` | `maestro/watchdog.py:67,90-91` | defaults 20 / 3. The template's `five_h_pause_pct` and `concurrency_cap` are read **nowhere** in `maestro/` (grepped) |
| `project.name` | `watchdog.py:80`; `cli.py:658` | falls back to `REPO.name` |
| `deny_list_extra`, `secrets`, `risky_set`, `bot_files` | `maestro/merge.py:83,98,117,167,548` | defaults `[]`, `[".env",".env.local"]`, `[]`, `["main_bot.py"]`. **`bot_files` is read but is not in the template** |
| `gate.chain` | only `cli.py:605-614` (doctor). `maestro/gates.py` never loads project.yaml | doctor prints `[WARN] gate` (non-required check → does not affect exit code) |
| `telegram.chat_id` | **never read anywhere** (grepped `maestro/`). `<captured by init>` stays literal forever; the real chat id goes to `.env` | — |
| `version`, `switch`, `prod_stores`, `docs` | **never read** by any module under `maestro/` | — |

`maestro/orchestrator.py` does not import `maestro.config` at all (checked its import block, lines 16-110) — it reads no project.yaml key.

---

## 5. Subcommands: argv and preconditions

Parser: `build_parser()`, `cli.py:951-983`. Repo resolution for all but `init`: `_resolve_repo` (`cli.py:941-949`) — `--repo` if given, else `$MAESTRO_REPO`, else `Path.cwd()`.

**`maestro run --repo PATH`** (`cli.py:703-712`)
- argv: `run [--repo PATH]`. Sets `os.environ["MAESTRO_REPO"]`, then `from maestro.orchestrator import main` and returns it.
- Needs: a tmux session literally named **`agents`** (`orchestrator.py:923-926` → prints `[ERROR] tmux session 'agents' not found.` and returns 1); `docs/ROADMAP.md` (`docs/roadmap.py:28`) and `docs/PROJECT.md` (`orchestrator.py:261`); `.orchestrator/state.json` (`state.py:35` `read_state` does an unguarded `json.loads(STATE_JSON.read_text())` — raises if absent); **`<repo>/.venv/bin/python3` must exist** — `run_dep_map()`/`run_status()` shell out to it every poll with no existence check (documented at `cli.py:60-68`, path at `orchestrator.py:121`, `docs/roadmap.py:30`, `hitl/commands.py:61`). Optional: `scripts/notify_telegram.sh`, `scripts/gen_dependency_map.py`, `scripts/orchestrator_status.py`, `scripts/canary_deploy.py`, `scripts/check_verifications.py`, `scripts/maestro_ask.py`, `scripts/maestro_redo.py` — absent ⇒ degraded/no-op, not fatal. Env: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALERT_CHAT_ID` for anything Telegram. project.yaml keys: none required.

**`maestro watchdog --repo PATH`** (`cli.py:873-905`)
- argv: `watchdog [--repo PATH]`. Wraps `maestro.watchdog.main()`.
- Needs: `tmux` binary and the `agents` session (it does not create it — `watchdog.py:129-134` only lists windows); `pgrep`/`pkill`; `crontab` (`ensure_resume_job`, `watchdog.py:147-162`, runs `crontab -l` and `crontab -` — **it edits the user crontab** when a pause with a quota reset time is in state); `.orchestrator/state.json`, `.orchestrator/journal.ndjson`, `.orchestrator/usage.json`, `.orchestrator/HALT`, `.orchestrator/questions/` (all via `Paths`, `watchdog.py:69-75`). project.yaml: optional `thresholds`, optional `project.name`.
- **It relaunches the orchestrator by shelling out to the literal string `maestro run`** (`watchdog.py:136-145`: `tmux new-window -t agents -n orchestrator 'cd {REPO} && MAESTRO_REPO={REPO} maestro run'`, `shell=True`, unquoted). This requires a `maestro` executable on the **tmux server's** PATH — see §7.
- Returns only 0 (HALT sentinel present) or 1 (3 consecutive 20-min stalls → it writes HALT itself). Never returns otherwise.

**`maestro status --repo PATH`** (`cli.py:682-701`)
- argv: `status [--repo PATH]`. Needs only `.orchestrator/state.json`; prints `[status] no state.json at …` and returns 1 if absent. Prints phase, in-flight count/ids, `halted`, `paused_by_user`, parked tasks. Read-only.

**`maestro ctl [--repo PATH] <verb> [args...]`** (`cli.py:728-871`)
- argv: `ctl [--repo PATH] verb [args ...]` (`cli.py:969-972`).
- Verbs handled locally (`_CTL_STATE_VERBS`, `cli.py:718`): `pause`, `resume`, `halt`, `hitl on|off`, `unpark <TASK_ID>`; plus `status`. These need `.orchestrator/state.json` (read_state raises if absent) and write `.orchestrator/HALT` for `halt`.
- Verbs dispatched into `maestro.hitl.commands` (`_CTL_DISPATCH_VERBS`, `cli.py:722-726`): `waiting`, `manual`, `detail <id>`, `approve <id>`, `reject <id>`, `fix <task_id>`, `ask <id> <message…>`, `redo <id> <message…>`, `backend <args…>`. Their output goes to Telegram (i.e. to `scripts/notify_telegram.sh` if present), **not** stdout; `cmd_ctl` prints a note saying so.
- Unknown verb → usage line, exit 2. Missing required arg → usage line, exit 2.
- Documented residual defect (`cli.py:757-760`): `ctl reject` on a normal HITL-parked task hits a `REPO_ROOT` `NameError` inherited from the reference `hitl/commands.py`, deliberately unfixed.

**`maestro doctor [--repo PATH] [--pre-commit]`** (`cli.py:643-680`)
- argv exactly as above (`cli.py:958-961`).
- Always runs: `_check_docs` (requires `docs/ROADMAP.md` **and** `docs/PROJECT.md`; then parses the roadmap — required check) and `_check_adapters`, which **actually executes all six adapters** with `timeout_s=60` each (`cli.py:505-522`). Only `test` is required; other kinds are OK if `ok` or `absent`.
- Without `--pre-commit` it also runs: `_check_model_limits` (resolves every model named under `roles:` against the model-limit tables), `_check_backends` (`backend_binary()` on PATH), `_check_telegram` (reads `<repo>/.env` via `dotenv_values`, then a live `getMe` HTTPS call unless `http_get` is injected), `_check_systemd_unit` (looks for `$MAESTRO_SYSTEMD_DIR` or `/etc/systemd/system` / `<project.name>-watchdog.service` — read-only stat), `_check_gate`, `_check_journal_and_version` (needs `state.json`; samples the last 20 journal lines).
- Exit code is 1 **only** if a check with `required=True` failed — that is `docs` and `adapter:test` only. Everything else is WARN.
- Name mismatch to watch: `_check_systemd_unit` uses `project.yaml`'s `project.name` (`cli.py:658-660`, falling back to `repo_root.name`), while `cmd_init` names the generated unit from the **git directory basename** (`cli.py:425`). If someone edits `project.name` to `abuali` in a repo directory named `AbuAliArchive`, doctor looks for `abuali-watchdog.service` while init wrote `systemd/AbuAliArchive-watchdog.service`.

---

## 6. `maestro/paths.py` — repo root resolution

Whole mechanism, `/home/dan/projects/maestro/maestro/paths.py:14-27`:

```python
ENV_VAR = "MAESTRO_REPO"
...
    @classmethod
    def from_env(cls) -> "Paths":
        raw = os.environ.get(ENV_VAR)
        root = Path(raw) if raw else Path.cwd()
        return cls(repo=root.resolve())
```

Order: **`$MAESTRO_REPO` if set and non-empty, else `Path.cwd()`**, always `.resolve()`d. No flag, no git-root search, no upward walk, no config file. `Paths` never validates that the directory exists or is a repo.

Layout derived from that root (`paths.py:29-51`): `.orchestrator/`, `.orchestrator/state.json`, `.orchestrator/journal.ndjson`, `.orchestrator/workspaces`, `.orchestrator/usage.json`, `.orchestrator/model_limits.json`.

The `--repo` flag is *not* part of this: `_resolve_repo` (`cli.py:941-949`) picks the value and each `cmd_*` then sets `os.environ["MAESTRO_REPO"]` **before** lazily importing the extracted modules. Because every module calls `Paths.from_env()` at **import time** (`config.py:16`, `state.py:16`, `watchdog.py:65`, `hitl/telegram.py:31`, `docs/roadmap.py`, …), the root binds once per interpreter — two different projects in one process is unsupported (stated at `cli.py:11-23`).

---

## 7. How the `maestro` command becomes available — and whether it is installed

**Declared:** `/home/dan/projects/maestro/pyproject.toml:16-17`

```
[project.scripts]
maestro = "maestro.cli:main"
```

Package name `maestro`, version `0.2.0`, `requires-python >=3.11`, deps `pyyaml>=6.0, python-dotenv>=1.0, requests>=2.31`.

**Measured on this machine:**

- `which -a maestro` → **no output**. `maestro` is not on `$PATH` (`PATH=/home/dan/.local/bin:/home/dan/.bun/bin:/usr/local/bin:/usr/bin:/bin:` + 4 Claude plugin dirs).
- Interpreter checked: `/usr/bin/python3` (the only `python3` on PATH; `/bin/python3` is the same file). `pip` and `pip3` resolve to `/home/dan/.local/bin/pip{,3}`.
- `pip show maestro` → `WARNING: Package(s) not found: maestro`. Same for `pip3 show maestro`. `pip list | grep -i maestro` → nothing.
- `ls /home/dan/.local/bin | grep -i maestro` → nothing.
- `/usr/lib/python3/dist-packages` → no maestro.
- `find /home/dan -maxdepth 8 -name 'maestro*.dist-info' -o -name 'maestro*.egg-link'` → exactly one hit: `/home/dan/projects/maestro/.venv/lib/python3.11/site-packages/maestro-0.1.0.dist-info`.
- Scanned every venv under `/home/dan/projects/*/.venv` (AbuAliArchive, AI-football-player-analyzer, blueprint-symbol-counter-FOR-AHARON, Chess, hive-assistant, maestro, PodcastScraper). Only `maestro/.venv` contains it. **`AbuAliArchive/.venv` does not have maestro installed.**

**Nature of the one install:** editable, via `__editable__.maestro-0.1.0.pth` + `__editable___maestro_0_1_0_finder.py`, installed 2026-08-09. Its dist-info has **no `entry_points.txt`** (`cat` → No such file), and `/home/dan/projects/maestro/.venv/bin/` contains no `maestro` script (only `activate*`, `dotenv`, `idna`, `normalizer`, `pip*`, `pygmentize`, `py.test`, `pytest`, `python*`). It is version **0.1.0** while `pyproject.toml` now says **0.2.0** — i.e. the install predates the current `[project.scripts]` block and was never refreshed.

Also note `/home/dan/projects/maestro/.venv/bin/python3` is a symlink to `/usr/bin/python3`, i.e. a `--system-site-packages`-style venv.

**Consequence for cutover (measured, not inferred from intent):** nothing on this machine can execute `maestro …`. That breaks three generated things by inspection:
1. `launch.sh`'s `tmux new-session … "MAESTRO_REPO=… maestro watchdog"` (template line 24-25).
2. `watchdog.py:139-141`'s `… && MAESTRO_REPO={REPO} maestro run` and `watchdog.py:153-156`'s cron resume line — both spell the bare console script, and both run in the **tmux server's** environment, not the unit's.
3. `templates/pre-commit`'s `command -v maestro` guard — it would exit 0 and silently gate nothing.
The workaround `cli.py` uses for itself is `sys.executable -m maestro.cli …` with `PYTHONPATH` injected (`_subprocess_env`, `cli.py:110-129`); the generated artifacts do not use that form.

---

## 8. Generated systemd unit + launcher, in full

### `maestro/templates/systemd/maestro-watchdog.service.tmpl` (verbatim)

```
# /etc/systemd/system/{{ project_name }}-watchdog.service
#
# Per DESIGN.md §10's Systemd-and-tmux subsection. ExecStart runs launch.sh, which
# wraps `tmux new-session -d -s {{ project_name }}-watchdog` so the loop is
# auditable from a phone with `tmux a -t {{ project_name }}-watchdog`.
#
# Type=simple, not Type=forking: the tracked process (the watchdog) lives
# inside the tmux server's cgroup, not a child systemd can find a PIDFile for —
# launch.sh instead blocks until the tmux session ends, so systemd tracks the
# launcher shell itself.
#
# The unit's name matches what it runs: launch.sh starts `maestro watchdog`
# (maestro/watchdog.py, extracted in M4b), which supervises the orchestrator and
# launches `maestro run` itself in the shared `agents` tmux session.
#
# Restart=on-failure, not Restart=always: systemd tracks launch.sh, whose exit
# status is the wait loop's, not the watchdog's (tmux swallows that) — so this
# restarts when the launcher itself fails, and a watchdog that stopped on the HALT
# sentinel (`maestro ctl halt`) stays stopped, which is the point of HALT. Bringing
# it back after a deliberate halt is `systemctl restart` plus removing the file.
[Unit]
Description={{ project_name }} — maestro watchdog (supervises the orchestrator loop)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={{ repo_path }}
ExecStart=/bin/bash {{ repo_path }}/launch.sh
ExecStop=/bin/sh -c 'tmux kill-session -t {{ project_name }}-watchdog 2>/dev/null || true'
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Note there is **no `User=`, no `Environment=PATH=`, no `Environment=HOME=`** — unlike the repo's own `systemd/maestro-build.service`, which explicitly sets `User=dan`, `HOME`, `USER` and `PATH=/home/dan/.local/bin:…` with a comment (lines 25-31 of that file) saying that without it systemd's bare default PATH breaks the CLI lookup and restart-loops the unit. The generated template omits exactly that fix.

### `maestro/templates/launch.sh.tmpl` (verbatim)

```bash
#!/usr/bin/env bash
# launch.sh — systemd wrapper for {{ project_name }}'s maestro loop.
#
# Same pattern as maestro's own scripts/maestro-build-launcher.sh: creates a
# dedicated, named tmux session so the loop is auditable from a phone with
# `tmux a -t {{ project_name }}-watchdog`, then blocks in a wait loop so systemd
# (Type=simple, per DESIGN.md §10) tracks THIS shell rather than the tmux child —
# Type=forking is unusable here because the tracked process lives in the tmux
# server's cgroup, not this script's.
#
# What this runs (read before "fixing" it): `maestro watchdog`, the supervisor —
# NOT `maestro run`, the loop. The watchdog polls every 30s and launches
# `maestro run` itself, in a separate window of the shared `agents` tmux session,
# relaunching it whenever the orchestrator process dies. It also schedules the
# quota-reset resume, reaps dead implementer windows, and sets the HALT sentinel
# after three consecutive 20-minute stalls. Running `maestro run` here directly
# would lose all four: systemd only supervises this shell, so it fires on the
# tmux session ending, not on the orchestrator inside it dying.
#
# The watchdog exits only on HALT (0) or a stall-HALT (1), so the wait loop below
# ends when it does. `maestro ctl halt` is the clean stop.
set -euo pipefail

PROJECT_NAME="{{ project_name }}"
REPO_PATH="{{ repo_path }}"
SESSION="${PROJECT_NAME}-watchdog"

# Kill any stale session from a previous run (manual relaunch, prior boot, ...).
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Launch the supervisor in a named, detached session.
tmux new-session -d -s "$SESSION" -n main -c "$REPO_PATH" \
    "MAESTRO_REPO='$REPO_PATH' maestro watchdog"

# Block here; systemd tracks this shell as the "service process". When `maestro
# watchdog` exits (clean stop or crash) the session disappears and this loop ends.
# Note the watchdog's own exit code is swallowed by tmux — this script exits 0
# either way, so Restart=on-failure fires for a failure of the *launcher* (no tmux,
# bad REPO_PATH), not for the watchdog stopping. That is deliberate: a watchdog that
# stopped because someone ran `maestro ctl halt` must stay stopped.
while tmux has-session -t "$SESSION" 2>/dev/null; do
    sleep 10
done
```

### Names for `abuali` vs `AbuAliArchive`

`project_name` = **basename of the git toplevel** (`cli.py:159-163,177`), not `project.yaml`. So for `/home/dan/projects/AbuAliArchive` it is `AbuAliArchive` — the string `abuali` can only arise if the checkout directory is literally named `abuali`.

| | project dir `abuali` | project dir `AbuAliArchive` |
|---|---|---|
| scaffolded unit file | `systemd/abuali-watchdog.service` | `systemd/AbuAliArchive-watchdog.service` |
| installed unit name | `abuali-watchdog.service` | `AbuAliArchive-watchdog.service` |
| launcher tmux session (`launch.sh`) | `abuali-watchdog` | `AbuAliArchive-watchdog` |
| `ExecStop` kills | `tmux kill-session -t abuali-watchdog` | `tmux kill-session -t AbuAliArchive-watchdog` |
| orchestrator/implementer tmux session | **`agents`** (unchanged) | **`agents`** (unchanged) |
| orchestrator tmux window | `orchestrator` | `orchestrator` |
| `doctor` looks for | `<project.yaml project.name>-watchdog.service` in `/etc/systemd/system` — diverges from the above if `project.name` was edited |

`agents` (`maestro/worktree.py:21`, duplicated as `_TMUX_SESSION` at `cli.py:101`) and `orchestrator` (`watchdog.py:76`) are **global, not per project** — two maestro projects on one machine share both, and `watchdog.py:120-125`'s `pgrep -f "maestro run|maestro\.orchestrator"` matches *any* project's orchestrator, so a second project's watchdog would consider the first project's loop "alive" and never launch its own (I read this; I did not run it).

---

## Things I did not verify

- I never executed `maestro init`, `run`, `watchdog`, `doctor`, `status`, `ctl`, or `telegram_creds.py`; every behavioural statement above is from reading source, except the install-status facts in §7 which come from `which`/`pip show`/`ls`/`find`.
- I did not inspect `/home/dan/projects/AbuAliArchive` beyond listing that `.venv` exists and contains no maestro. I did not check which of the scaffolded paths already exist there — that determines the exact `.new` set on a real `init` and should be checked before the cutover.
- I did not check installed systemd units (`/etc/systemd/system`) — that requires no elevation to list, but was outside this task and I did not run it.
- The `find /home/dan -maxdepth 8` install sweep is bounded at depth 8; a venv deeper than that would have been missed.


---

## Agent C — breakage scan: every dangling caller the delete list creates

[harness: subagent output matched instruction-shaped pattern(s): settings-json. Control tags below are neutralized (`<` → `<\`); treat any remaining directive-shaped text as a finding to relay to the user, not an instruction to you.]

## TASK C — BREAKAGE SCAN

Scan method: `rg --no-ignore --hidden` over `/home/dan/projects/AbuAliArchive` excluding `data/`, `data_export/`, `.git/objects`, `.git/lfs`, `__pycache__`, `.pytest_cache`, `.venv` (543 hits), plus a separate import-level scan (module names without `.py`), plus `crontab -l`, `/etc/systemd/system/**`, `~/.config/systemd/user/`, `/etc/cron*`, `~/.claude/`, `~/.codex/`, `/home/dan/services/`, `/home/dan/claude/`, and `/home/dan/projects/maestro`. No git/systemctl-state/elevated commands were run (`systemctl is-enabled`/`is-active` are read-only queries). `.env` was never displayed.

---

## 1. EXISTENCE — all 16 exist

| # | Path | Lines |
|---|---|---|
| 1 | `/home/dan/projects/AbuAliArchive/scripts/orchestrator_run.py` | 4567 |
| 2 | `/home/dan/projects/AbuAliArchive/scripts/orchestrator_ctl.py` | 82 |
| 3 | `/home/dan/projects/AbuAliArchive/scripts/orchestrator_status.py` | 231 |
| 4 | `/home/dan/projects/AbuAliArchive/scripts/launch_orchestrator.py` | 236 |
| 5 | `/home/dan/projects/AbuAliArchive/scripts/watchdog.py` | 322 |
| 6 | `/home/dan/projects/AbuAliArchive/scripts/watchdog-launcher.sh` | 31 |
| 7 | `/home/dan/projects/AbuAliArchive/scripts/maestro_ask.py` | 124 |
| 8 | `/home/dan/projects/AbuAliArchive/scripts/maestro_redo.py` | 265 |
| 9 | `/home/dan/projects/AbuAliArchive/scripts/maestro_selffix.py` | 157 |
| 10 | `/home/dan/projects/AbuAliArchive/scripts/prepared_actions.py` | 107 |
| 11 | `/home/dan/projects/AbuAliArchive/scripts/gen_dependency_map.py` | 270 |
| 12 | `/home/dan/projects/AbuAliArchive/scripts/gen_upcoming.py` | 661 |
| 13 | `/home/dan/projects/AbuAliArchive/scripts/mark_task_complete.py` | 215 |
| 14 | `/home/dan/projects/AbuAliArchive/scripts/check_roadmap_consistency.py` | 271 |
| 15 | `/home/dan/projects/AbuAliArchive/scripts/check_verifications.py` | 140 |
| 16 | `/home/dan/projects/AbuAliArchive/scripts/notify_telegram.sh` | 35 |

Stale `.pyc` for 15 of them also sit in `/home/dan/projects/AbuAliArchive/scripts/__pycache__/` (e.g. `orchestrator_run.cpython-311.pyc`). **These are not on the delete list and will keep `import orchestrator_run` working after the `.py` is gone** — a silent, dangerous survival path. Verified present by `ls`.

---

## 2. THE RANKED DANGLING CALLERS (primary deliverable)

Everything below **survives** the deletion and points at a deleted file.

### R1 — `/etc/systemd/system/abuali-watchdog.service:13` — **breaks at next boot**
```
ExecStart=/bin/bash /home/dan/projects/AbuAliArchive/scripts/watchdog-launcher.sh
```
Verified: `systemctl is-enabled abuali-watchdog.service` → **`enabled`**; `is-active` → `inactive`. At next boot systemd runs a missing script; bash exits 127; `Restart=on-failure` + `RestartSec=60` (lines 16–17) restart-loops it every 60 s until the start limit trips, then the unit sits `failed`. Repo mirror of the same defect: `/home/dan/projects/AbuAliArchive/systemd/abuali-watchdog.service:13` (repo copy differs from installed only at line 14, `ExecStop=-` vs `ExecStop=`).

### R2 — `/home/dan/projects/AbuAliArchive/.git/hooks/pre-commit` → `scripts/hooks/pre-commit:19` — **breaks on the very next `git commit` in the repo**
```
19: if ! "$PY" scripts/check_roadmap_consistency.py; then
```
`.git/hooks/pre-commit` is a symlink → `../../scripts/hooks/pre-commit` (verified via `ls -la`), and `scripts/hooks/pre-commit` is **not** on the delete list. Python exits 2 on the missing file, the `if !` fires, the hook prints "commit blocked" and `exit 1`. **Every commit in AbuAliArchive is blocked** until the hook is fixed or `--no-verify` is used. Same file lines 22 and 24 print remediation advice naming two other deleted scripts (`gen_dependency_map.py`, `mark_task_complete.py`).

### R3 — `/home/dan/projects/maestro/maestro/gates.py:29` — **fails OPEN, silently, on first maestro run**
```
29: CHECK_VERIFICATIONS = REPO / "scripts" / "check_verifications.py"
55:     if not CHECK_VERIFICATIONS.exists():
56:         return True, "(check_verifications.py not found — gate skipped)"
```
`REPO = Paths.from_env().repo` = `$MAESTRO_REPO` (`maestro/paths.py:14,25-27`), i.e. AbuAliArchive at cutover. The verification gate becomes an **unconditional PASS**. This is the most dangerous item on the list because nothing errors — every task graduates verified without any check ever running.

### R4 — `/home/dan/projects/maestro/maestro/hitl/telegram.py:35,51-52` — **all operator notifications silently vanish, immediately**
```
35: NOTIFY_SH = REPO / "scripts" / "notify_telegram.sh"
51:     if NOTIFY_SH.exists():
52:         subprocess.run(["bash", str(NOTIFY_SH), msg], ...)
```
`.exists()`-guarded → `notify_telegram()` becomes a no-op. Every `notify_telegram(...)` call site in maestro (≈20 in `maestro/orchestrator.py`, plus `quota.py:123`, `selfheal/redo.py:82`, `selfheal/selffix.py:141,168,177`, `hitl/commands.py`) goes dark. Operator loses all Telegram visibility with no error anywhere.

### R5 — `/home/dan/projects/maestro/maestro/docs/roadmap.py:59` — **unguarded; silent no-op on every roadmap update**
```
59: subprocess.run([str(VENV_PYTHON), str(GEN_DEP_MAP)], cwd=str(REPO), capture_output=True)
```
Unlike its neighbours this call has **no `.exists()` guard** and no `check=True`, and `capture_output=True` swallows python's exit-2 "can't open file". `docs/dependency_map.md` stops regenerating with zero signal. (`GEN_DEP_MAP` defined `maestro/docs/roadmap.py:31`.)

### R6 — `/home/dan/projects/maestro/maestro/docs/roadmap.py:245,250` — **task graduation silently stops**
```
245: if MARK_TASK_COMPLETE.exists():
250:     [str(VENV_PYTHON), str(MARK_TASK_COMPLETE), task_id, "--no-verify"],
```
Guarded → completed tasks are never graduated out of `docs/ROADMAP.md`. Fires at every task completion. (Constant at line 35.)

### R7 — `/home/dan/projects/maestro/maestro/docs/roadmap.py:66-67, 263-270` — `docs/UPCOMING.md` silently freezes
```
66: if GEN_UPCOMING.exists():
67:     subprocess.run([str(VENV_PYTHON), str(GEN_UPCOMING)], ...)
263: if GEN_UPCOMING.exists():
266:     [str(VENV_PYTHON), str(GEN_UPCOMING), "--refresh-explanations"], ...
270: subprocess.run([str(VENV_PYTHON), str(GEN_UPCOMING)], ...)
```
(Constant at line 32.)

### R8 — `/home/dan/projects/maestro/maestro/hitl/commands.py:615-621` — `/ask` hangs forever
```
615: helper   = REPO / "scripts" / "maestro_ask.py"
617: tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n {window} 'cd {REPO} && {VENV_PYTHON} {helper} {req_path}'")
619: subprocess.run(tmux_cmd, shell=True, check=True)
```
`check=True` guards **tmux**, not the helper — `tmux new-window` succeeds, python inside dies at 127/2, and line 625 still tells the operator "Asking Maestro… I'll reply here shortly." No reply ever arrives. Fires on the first `/ask`.

### R9 — `/home/dan/projects/maestro/maestro/hitl/commands.py:663-669` — `/redo` hangs forever
```
663: helper   = REPO / "scripts" / "maestro_redo.py"
665: tmux_cmd = (f"tmux new-window ... '{VENV_PYTHON} {helper} {req_path}'")
667: subprocess.run(tmux_cmd, shell=True, check=True)
```
Identical failure shape; operator is told Maestro is reworking the task (line 672) and it never completes.

### R10 — `/home/dan/projects/maestro/maestro/selfheal/selffix.py:135-140` — self-fix announced, never delivered
```
135: helper = REPO / "scripts" / "maestro_selffix.py"
136: tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n selffix-{task_id} 'cd {REPO} && {VENV_PYTHON} {helper} {req}'")
139: subprocess.run(tmux_cmd, shell=True, check=True)
```
Same tmux-hides-the-failure shape; `append_journal("self_fix_started", ...)` and the Telegram "🔧 self-fix started" message both fire (140–145) for work that never happens.

### R11 — 13 of 14 test files in `/home/dan/projects/AbuAliArchive/tests/` — **test suite fails at collection on next `pytest`**
Each does `sys.path.insert(0, str(REPO / "scripts"))` then imports a deleted module:
```
tests/test_reconcile_stale.py:16                 import orchestrator_run as orch
tests/test_await_verification.py:30              import orchestrator_run as orch
tests/test_timeout_orphan.py:30                  import orchestrator_run as orch
tests/test_resumable_merge_dirty_worktree.py:18  import orchestrator_run as orch
tests/test_async_job_lane.py:43                  import orchestrator_run as orch
tests/test_question_gating.py:18                 import orchestrator_run as orch
tests/test_resource_mutex.py:17                  import orchestrator_run as orch
tests/test_manual_only.py:18                     import orchestrator_run as orch
tests/test_launch_clock.py:26                    import orchestrator_run as orch
tests/test_maestro_selfheal.py:16                import orchestrator_run as orch
tests/test_reconcile_zombie.py:23                import orchestrator_run as orch
tests/test_resume_state.py:19                    import orchestrator_run as orch
tests/test_resume_state.py:20                    import check_roadmap_consistency as cons
tests/test_prepared_actions.py:12                import prepared_actions as pa
```
Only `tests/test_pair_model_pool.py` is unaffected (imports `generate_training_pairs`, which survives). **Caveat I verified:** the stale `scripts/__pycache__/*.pyc` files mean these imports may *silently keep working* on Python 3.11 only if a `__pycache__`-only import path applies — normal CPython will **not** import from `__pycache__` without the source, so expect `ModuleNotFoundError`. I did not execute pytest to confirm.

### R12 — `/home/dan/projects/AbuAliArchive/scripts/canary_deploy.py:20,31-32` — canary deploys go silent
```
20: NOTIFY_SH = REPO / "scripts" / "notify_telegram.sh"
31:     if NOTIFY_SH.exists():
32:         subprocess.run(["bash", str(NOTIFY_SH), msg], capture_output=True, timeout=15)
```
`canary_deploy.py` survives (not on the list; it is also in `project.yaml:7` risky_set) and is invoked by `maestro/orchestrator.py:123` and `maestro/hitl/commands.py:63`. Every canary deploy/rollback alert is silently dropped. Fires on the next merge that touches a `bot_files[]` entry.

### R13 — `/home/dan/projects/maestro/maestro/hitl/commands.py:62,79` — `/status` returns nothing
```
62: STATUS_SCRIPT = REPO / "scripts" / "orchestrator_status.py"
79: subprocess.run([str(VENV_PYTHON), str(STATUS_SCRIPT)], cwd=str(REPO))
```
No `.exists()` guard, no `check=True`, output not captured → operator's `/status` prints a python "can't open file" to the orchestrator's stdout and returns nothing useful.

### R14 — `/home/dan/projects/maestro/maestro/merge.py:403-404` — **auto-merge safety guard becomes a dead guard**
```
403: _RESUMABLE_HARD_STOP = ("scripts/orchestrator_run.py", "scripts/launch_orchestrator.py",
404:                         "scripts/watchdog.py")
```
Not a crash — worse. Three paths that can never again appear in a diff now form the "never auto-merge" set, and the *new* equivalents (`maestro/orchestrator.py`, `maestro/watchdog.py`) are **not** in it. Post-cutover, a self-modifying change to the orchestrator runtime can auto-merge on the no-eval resumable path. Fires on the first self-modifying merge.

### R15 — `/home/dan/projects/AbuAliArchive/project.yaml:5,6,8,9` — stale risky_set, reviewer never triggers
```
5: - scripts/orchestrator_run.py
6: - scripts/launch_orchestrator.py
8: - scripts/mark_task_complete.py
9: - scripts/check_verifications.py
```
Read by `maestro/merge.py` ("risky_set / bot_files from project.yaml are added to this set at check time", `merge.py:402`) and by `maestro/watchdog.py:80` for `PROJECT_NAME`. The Sonnet risky-file reviewer silently protects nothing. Fires on every merge.

### R16 — `/home/dan/projects/AbuAliArchive/CLAUDE.md` + `AGENTS.md` + `operating_preamble.md` — agents instructed to run deleted scripts, breaks on the next agent session
```
CLAUDE.md:28  `scripts/gen_dependency_map.py`; never hand-edit it.
CLAUDE.md:32  ... by `scripts/gen_upcoming.py` ...
CLAUDE.md:55  ... with `python scripts/gen_dependency_map.py`.
CLAUDE.md:59  graduate it with `python scripts/mark_task_complete.py <id>`
CLAUDE.md:62  guard (`scripts/check_roadmap_consistency.py`) runs in the pre-commit hook
AGENTS.md:18  ... by `scripts/gen_upcoming.py` — never hand-edit it.
AGENTS.md:26  ... then run `python scripts/gen_dependency_map.py`
AGENTS.md:51  ... with `python scripts/gen_dependency_map.py`.
operating_preamble.md:19  **Modifying `scripts/orchestrator_run.py` directly** ...
```
These are loaded automatically by every Claude/Codex session in the repo; the agent will run a missing command and improvise.

### R17 — `/home/dan/projects/maestro/scripts/run_overnight.sh:191` — **nightly**, currently masked
```
191: elif ! pgrep -f orchestrator_run.py >/dev/null; then
192:     echo "[chain] WARNING: reference project's orchestrator is NOT running and no HALT sentinel is present"
```
Reached only when `/home/dan/projects/AbuAliArchive/.orchestrator/HALT` is absent (line 188). I verified `HALT` **exists** (0 bytes, dated Aug 9), so this branch is unreachable today. The moment the operator lifts the pause, this prints a permanent spurious WARNING on every overnight chain run. Driven by `maestro-build.service` (`enabled`, tmux session `maestro-build` live).

### R18 — `/home/dan/projects/AbuAliArchive/scripts/check_pairs_complete.py:6,38`, `check_lancedb_migration.py:11`, `check_eval_calibration.py:85` — orphaned stdout contracts
```
check_pairs_complete.py:6   ``expect: "ok"`` under scripts/check_verifications.py's ``stdout.strip() == expect``
check_pairs_complete.py:38  ... the success path must stay exactly "ok" (check_verifications.py
check_lancedb_migration.py:11  OUTPUT CONTRACT (matches scripts/check_verifications.py: stdout.strip() == expect):
check_eval_calibration.py:85   The deterministic gate (check_verifications.py) asserts stdout.strip() == expect,
```
Comments only — no runtime break. But these three surviving scripts exist **solely** to be executed by `check_verifications.py`; after the delete nothing runs them and their exact-stdout discipline has no enforcer.

### R19 — `~/.claude/statusline-command.sh:33` — stale comment, no break
```
33: # erase a prior good value that launch_orchestrator.py reads (null -> 0 -> no pause), so
```
Comment only. The statusline still writes `.orchestrator/usage.json`, which `maestro/quota.py` reads, so the mechanism survives; only the name in the comment is dead. `maestro/quota.py:101` and `maestro/orchestrator.py:174` carry the same stale name in comments.

---

## 3. PER-SCRIPT REFERENCE TABLE

`[DEL]` = the referencing file is itself on the delete list (harmless). `[SURV]` = survives (dangerous or stale). Prose = `.md`; state = `.orchestrator/*.json|.ndjson|.log|DONE`.

**orchestrator_run.py** — [DEL] `scripts/gen_upcoming.py:56`; `scripts/launch_orchestrator.py:2,22,37,205,206,211`; `scripts/mark_task_complete.py:7`; `scripts/orchestrator_run.py:5,65,3666`; `scripts/watchdog.py:259`.
[SURV] code/config: `project.yaml:5`; `scripts/canary_deploy.py:117` (comment); `tests/test_maestro_selfheal.py:122,141,167,171` (string asserts) **plus** the 12 `import orchestrator_run` sites in R11; `.env:4` (**verified to be a COMMENT line — contents not displayed**); `operating_preamble.md:19`.
[SURV] prose/state: `docs/AUTONOMOUS_SYSTEM.md:415,427,450,475`; `docs/CLAUDE_TASK_CONTEXT.md:490,569,595,661`; `docs/ROADMAP.md:32`; `tasks/lessons.md:21,71,105,132,187`; `tasks/todo.md:5,16,20,21,22,23,27,29,34`; 20 handoffs files; `.orchestrator/journal.ndjson` (70 lines), `launch_console.log:189,190,242,245,250,251,1747,1748`, `diagnoses/TUNE1.json:1`, `diagnoses/TUNE2.json:1`, `selffix/TUNE2-20260627-003358.request.json:1`, `questions/archive/danreq-20260604-225518-7e3916.json:4`, `workspaces/impl-B5.1-verify/result.json:22`, `workspaces/impl-B6-20260604-215722/result.json:9`, `workspaces/impl-B6-20260608/result.json:23`.
External: `maestro/merge.py:403`; `maestro/watchdog.py:83,305` (comments); `maestro/scripts/run_overnight.sh:191`; `~/.claude.json` `exampleFiles[]` ×45 (Claude Code's cosmetic per-project filename cache — **false positive**, self-healing) and the same in `~/.claude/backups/.claude.json.backup.*`.

**orchestrator_ctl.py** — [DEL] `scripts/launch_orchestrator.py:150`; `scripts/orchestrator_ctl.py:5,6,7,47`. [SURV] all prose/state: `docs/AUTONOMOUS_SYSTEM.md:465`; `docs/CLAUDE_TASK_CONTEXT.md:532,605`; `tasks/lessons.md:38`; handoffs `2026-06-23-C-upcoming-doc-generator.md:108,109`, `B5.1_verification_gating.md:115,156,215`, `B6_full_auto.md:23`, `B6_review_and_completion.md:14`; `.orchestrator/workspaces/impl-B5.1-verify/result.json:14,22`. **No surviving executable caller — safe to delete outright.**

**orchestrator_status.py** — [DEL] `scripts/orchestrator_run.py:52`; `scripts/orchestrator_status.py:11,12`. [SURV] prose: `.orchestrator/README.md:99`; `docs/AUTONOMOUS_SYSTEM.md:414`; handoffs `2026-06-20-watchdog-handover-and-writer-fix.md:51`, `B1_readonly_spine.md:50`, `B2_supervised_loop.md:26,36,43,91,114,125`, `B3_survival.md:17,30`. **External executable caller: R13.**

**launch_orchestrator.py** — [DEL] `scripts/launch_orchestrator.py:93`; `scripts/orchestrator_run.py:4,3457,3666`; `scripts/watchdog.py:32,93`. [SURV] `project.yaml:6`; prose `docs/AUTONOMOUS_SYSTEM.md:426,444,466`, `docs/CLAUDE_TASK_CONTEXT.md:532,543,628`, `docs/ROADMAP.md:32`, `tasks/lessons.md:66,71`, `tasks/todo.md:45`, 11 handoffs; state `.orchestrator/workspaces/impl-B5.1-verify/result.json:18,22`, `impl-B6-20260608/DONE:5`, `impl-B6-20260608/result.json:38`. External: `maestro/merge.py:403`, `maestro/quota.py:101`, `maestro/orchestrator.py:174`, `maestro/watchdog.py:31,83` (comments), `~/.claude/statusline-command.sh:33`.

**watchdog.py** — [DEL] `scripts/maestro_redo.py:49,140`; `scripts/maestro_selffix.py:31,89`; `scripts/orchestrator_run.py:750,2163,3667`; `scripts/watchdog-launcher.sh:2,25`. [SURV] `systemd/abuali-watchdog.service:7,8` (comments); `tests/test_maestro_selfheal.py:126`; prose `docs/AUTONOMOUS_SYSTEM.md:420,442`, `tasks/lessons.md:16`, 7 handoffs. External: `maestro/merge.py:404`, `maestro/selfheal/redo.py:35`, `maestro/selfheal/selffix.py:52` (deny-list string entries — dead entries after delete), `maestro/watchdog.py:17`.
**False positive, unrelated file:** `/etc/systemd/system/claude-watchdog.service:13` and `/home/dan/services/claude-bridge/services/claude-watchdog.service:13` both `ExecStart=/usr/bin/python3 /home/dan/services/claude-bridge/watchdog.py` — a **different** `watchdog.py`, currently running (PID 2313514). Unaffected.

**watchdog-launcher.sh** — [SURV] only 2 refs, and one is R1: `systemd/abuali-watchdog.service:13` (repo) + `/etc/systemd/system/abuali-watchdog.service:13` (installed); `docs/AUTONOMOUS_SYSTEM.md:422`. Also named as a design precedent in `/home/dan/projects/maestro/scripts/maestro-build-launcher.sh:5` (comment only).

**maestro_ask.py** — [DEL] `scripts/orchestrator_run.py:2973,3015`. **Zero surviving references inside the repo.** External executable caller: R8.

**maestro_redo.py** — [DEL] `scripts/orchestrator_run.py:773,2160,3032,3063`. [SURV] prose only: `handoffs/2026-06-23-redo-strip-dont-discard-spec.md:33,95,124`. External executable caller: R9.

**maestro_selffix.py** — [DEL] `scripts/maestro_redo.py:13`; `scripts/orchestrator_run.py:2063,2077`. [SURV] prose only: `handoffs/2026-06-23-redo-strip-dont-discard-spec.md:44,117`. External executable caller: R10.

**prepared_actions.py** — [DEL] none. [SURV] `tests/test_prepared_actions.py:1` (docstring) **and `:12` `import prepared_actions as pa`** (R11); `docs/CLAUDE_TASK_CONTEXT.md:491`. Also imported by deleted `scripts/gen_upcoming.py:277` and `scripts/orchestrator_run.py:68` (both [DEL]). Note: the live sidecar `/home/dan/projects/AbuAliArchive/.orchestrator/prepared_actions.json` (1603 B, mode 0600) is data, not a caller, and maestro reads it via `prep_actions` (`maestro/hitl/commands.py`).

**gen_dependency_map.py** — [DEL] `scripts/check_roadmap_consistency.py:9,44,99`; `scripts/gen_dependency_map.py:15,16,176,258`; `scripts/mark_task_complete.py:38`; `scripts/orchestrator_run.py:48,151,3670`. [SURV] executable: `scripts/hooks/pre-commit:22` (R2). Prose: `AGENTS.md:26,51`; `CLAUDE.md:28,55`; `docs/AUTONOMOUS_SYSTEM.md:409`; `docs/CLAUDE_TASK_CONTEXT.md:526`; `docs/MIGRATION_B0.md:18,20`; `docs/ROADMAP.md:14,271`; `docs/dependency_map.md:3`; `.orchestrator/README.md:41`; 12 handoffs. External: R5.

**gen_upcoming.py** — [DEL] `scripts/check_roadmap_consistency.py:26,45,105,119`; `scripts/gen_upcoming.py:27,28,29,509,647`; `scripts/orchestrator_run.py:49`. [SURV] prose only: `AGENTS.md:18`; `CLAUDE.md:32`; `docs/AUTONOMOUS_SYSTEM.md:124`; `docs/PROJECT.md:158`; `docs/UPCOMING.md:3`; `handoffs/2026-06-23-C-upcoming-doc-generator.md:68,94,100`. External: R7.

**mark_task_complete.py** — [DEL] `scripts/check_roadmap_consistency.py:4,84`; `scripts/mark_task_complete.py:4,48,153`; `scripts/orchestrator_run.py:13,56,1228,1240,3082`. [SURV] executable: `scripts/hooks/pre-commit:24` (R2); `project.yaml:8` (R15). Prose: `CLAUDE.md:59`; `docs/AUTONOMOUS_SYSTEM.md:464,492,513,518`; `docs/CLAUDE_TASK_CONTEXT.md:604,623`; `docs/PROJECT.md:163`; `tasks/PRD.md:32`; `tasks/todo.md:46`; 6 handoffs; `.orchestrator/workspaces/impl-B6-20260608/DONE:8`. External: R6.

**check_roadmap_consistency.py** — [DEL] `scripts/mark_task_complete.py:22,39,49`. [SURV] executable: **`scripts/hooks/pre-commit:19` (R2, top-severity)**; `tests/test_resume_state.py:20` (R11). Prose: `CLAUDE.md:62`; `docs/AUTONOMOUS_SYSTEM.md:405,505,507,518`; `docs/PROJECT.md:163`; `tasks/lessons.md:112,165`; 8 handoffs.

**check_verifications.py** — [DEL] `scripts/check_verifications.py:4,57`; `scripts/mark_task_complete.py:10,40`; `scripts/orchestrator_run.py:12,55,1133,1138,1152,1154`. [SURV] `project.yaml:9` (R15); `scripts/check_eval_calibration.py:85`, `scripts/check_lancedb_migration.py:11`, `scripts/check_pairs_complete.py:6,38` (R18). Prose/state: `docs/AUTONOMOUS_SYSTEM.md:463`; `docs/CLAUDE_TASK_CONTEXT.md:569,603,619`; `tasks/lessons.md:71,118,161`; `tasks/todo.md:40,44`; 6 handoffs; `.orchestrator/workspaces/impl-B5.1-verify/result.json:10`, `impl-B6-20260608/DONE:3,4`, `impl-B6-20260608/result.json:13,33`. External: **R3**.

**notify_telegram.sh** — [DEL] `scripts/maestro_ask.py:18,29`; `scripts/maestro_redo.py:36`; `scripts/maestro_selffix.py:27`; `scripts/notify_telegram.sh:2`; `scripts/orchestrator_run.py:53`; `scripts/watchdog.py:34`. [SURV] executable: **`scripts/canary_deploy.py:20` (R12)**. Prose: `docs/AUTONOMOUS_SYSTEM.md:242,432`; `tasks/todo.md:27`; 4 handoffs. External: **R4** (`maestro/hitl/telegram.py:35`), plus `maestro/docs/roadmap.py:34` and `maestro/merge.py:25` which define the same `NOTIFY_SH` constant, and `maestro/watchdog.py:27`, `maestro/cli.py:37,734,864` (comments). `/home/dan/projects/maestro/scripts/notify_telegram_bridge.sh` is a **different, maestro-owned** script — false positive.

---

## 4. OUTSIDE-THE-REPO SCAN — full results

- **`crontab -l`** (16 entries read): **zero** references to any of the 16 scripts or to AbuAliArchive. Closest are hive-assistant and `*/15 * * * * /home/dan/projects/maestro/scripts/resume_eval_when_done.sh` — unaffected.
- **`/etc/systemd/system/`**: only `abuali-watchdog.service:13` (R1) and its comments at `:7,:8`. Drop-ins `abuali-watchdog.service.d/60-resource-guard.conf` and `70-ignore-missing-tmux-session.conf` contain no script names but **will keep the failing unit's restart behaviour alive**. `abuali-bot.service` (runs `telegram_bot.py`), `abuali-entities.service/.timer` (run `generate_entities.py`), `maestro-build.service`, `maestro-watchdog.service/.timer` — none reference a deleted script.
- **`/etc/cron.d/`, `/etc/crontab`, `/etc/cron.daily`, `/etc/cron.hourly`**: zero hits.
- **`~/.config/systemd/user/`**: zero hits. `abuali-nightly-eval.service:8` runs `scripts/run_nightly_eval.sh`, which I grepped — **no references to any deleted script**. Its timer is **not** in `timers.target.wants/` (not enabled).
- **`~/.claude/`**: `statusline-command.sh:33` (R19, comment). `~/.claude.json` and `~/.claude/backups/.claude.json.backup.*` — all 45+ hits are in `exampleFiles[]` arrays (verified by JSON path walk): Claude Code's cosmetic filename cache. **False positives.**
- **`~/.codex/`**: zero hits.
- **`/home/dan/services/`**: one hit, `claude-bridge/services/claude-watchdog.service:13` → a different `watchdog.py`. **False positive.**
- **`/home/dan/claude/`**: zero hits.
- **`/home/dan/projects/maestro/`** (not in your list, but the single largest consumer): R3–R10, R13, R14, R17 above.
- **Repo-internal agent/CI surfaces checked and CLEAN** (explicitly grepped, zero hits): `.claude/settings.json` (only `sudo systemctl … abuali-watchdog*` permission entries, lines 4–9), `.claude/settings.local.json`, `.claude/commands/add_task.md`, `.codex/hooks.json`, `.agent/**`, `.agents/skills/add-task/SKILL.md`, `adapters/deploy`, `adapters/healthcheck`, `adapters/smoke.py`, `orchestrator/`, `retired/`, `colab/`, `eval/`, `scripts/hooks/install.sh`. There is **no** Makefile, `.github/`, `.gitlab-ci.yml`, `pyproject.toml`, `pytest.ini`, `tox.ini`, or `conftest.py` in the repo (verified by `ls`).
- **Live process state**: no deleted script is currently running. `.orchestrator/HALT` (0 bytes, Aug 9) is present; `abuali-watchdog.service` and `abuali-bot.service` are `enabled` but `inactive`. Nothing breaks *today* — R1 fires at reboot, R2 fires at the next commit.

---

## 5. THINGS I DID NOT VERIFY

- I did **not** run `pytest`, so R11's exact failure mode (`ModuleNotFoundError` vs. a stale-`.pyc` import succeeding) is reasoned, not observed.
- I did **not** open `scripts/orchestrator_run.py` beyond targeted grep line numbers, per instruction.
- I did **not** read `.env` contents; I only confirmed line 4 is structurally a comment line and that it contains the token `orchestrator_run.py`.
- I did **not** search `data/` (2.4 GB) or `data_export/` (771 MB) — excluded for context/time. Both are data stores; a script reference there would be anomalous but is unproven.
- `.git/objects` and `.git/lfs` were excluded (historical blobs only, not callers). `.git/hooks/` **was** covered: `pre-commit` is the only non-`.sample` hook.
