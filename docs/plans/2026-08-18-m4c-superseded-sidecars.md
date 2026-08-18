# M4c — Give the remaining superseded sidecars a maestro owner (unplanned pre-M5 stage)

**Date:** 2026-08-18
**Status:** plan written before any code, per `docs/EXECUTION.md` "Per-stage loop" step 2.
**Predecessor:** M4b (`a20b485`). **Successor:** M5 (cutover), which this stage unblocks.
**Evidence:** `docs/plans/2026-08-18-m5-cutover-survey-annex.md` — a 3-agent read-only survey
(253K subagent tokens) run before this plan. Read the annex for file:line detail; this plan states
conclusions and the work.

---

## 1. Why this stage exists — M5 cannot be executed safely as specified

M5 deletes the 16 scripts on `docs/DESIGN.md` §11's superseded list. **Eight of them have no maestro
owner, and maestro's own extracted modules still shell out to them by path.** M1's mapping table
covered `scripts/orchestrator_run.py` only; the sidecar scripts it *calls* were never in scope. M4a
caught this for `prepared_actions.py`, M4b for `watchdog.py`. This is the same defect a third time,
and it is the largest instance: **19 dangling callers**, ranked in the annex as R1–R19.

The reason four green stages never noticed is identical to M4a's: characterisation tests
monkeypatch these shell-outs, and every dangerous call site is `.exists()`-guarded, so with the
script deleted the guard is simply False and the branch is skipped **without an error**.

**The three that would do real damage, all silent:**

| # | Site | What happens after M5 as specified |
|---|---|---|
| **R3** | `maestro/gates.py:29,55-56` — `CHECK_VERIFICATIONS = REPO/"scripts"/"check_verifications.py"`, `if not …exists(): return True, "(gate skipped)"` | **The verification gate fails OPEN.** Every task graduates "verified" with no check ever running. Nothing errors, nothing journals a warning. |
| **R4** | `maestro/hitl/telegram.py:35,51-52` — `NOTIFY_SH = REPO/"scripts"/"notify_telegram.sh"`, `.exists()`-guarded | **Every operator notification silently vanishes** — ~20 call sites in `orchestrator.py` plus `quota.py`, `selfheal/*`, `hitl/commands.py`. The operator's only visibility into an unattended loop goes dark with no error. |
| **R2** | `scripts/hooks/pre-commit:19` (survives; `.git/hooks/pre-commit` symlinks to it) calls the deleted `check_roadmap_consistency.py` | **Every `git commit` in the reference project is blocked**, including the ones maestro itself makes when it merges a task. |

Plus, briefly: `/status` returns nothing (R13); `/ask`, `/redo` and self-fix announce work over
Telegram and then hang forever, because `check=True` guards the `tmux new-window`, not the python
that dies inside it (R8–R10); `docs/UPCOMING.md` and `docs/dependency_map.md` stop regenerating and
completed tasks stop graduating out of `ROADMAP.md`, all unguarded and silent (R5–R7); the
`_RESUMABLE_HARD_STOP` auto-merge guard becomes a dead guard naming three paths that can no longer
exist, while the new equivalents `maestro/orchestrator.py` and `maestro/watchdog.py` are **not** in
it, so a self-modifying change to the orchestrator runtime can auto-merge (R14); the installed
**system** unit's `ExecStart` points at a deleted launcher (R1); and 13 of the project's 14 test
files `import orchestrator_run` (R11).

**Conclusion: M5 must not run until these have owners.** The conservative reading of
`docs/EXECUTION.md`'s autonomy rule ("prefer stopping a stage over stopping the programme"; "if
genuinely blocked, pick the more conservative option") is to open this stage, not to cut over with a
gate that passes everything.

---

## 2. Ownership — which of the eight become maestro, decided against DESIGN.md §4

§4 gives maestro "the 3-doc engine", "Telegram layer", "implementer protocol", "decision and
escalation policy", "reporting and proposals"; the project keeps "what good looks like, how to ship,
and configuration" — i.e. the adapters and its own docs' *content*.

| Script | Lines | Owner | Target |
|---|---|---|---|
| `notify_telegram.sh` | 35 | maestro (Telegram layer) | native send in `maestro/hitl/telegram.py` via `requests` (an allowed runtime dep); the shell-out and its `.exists()` guard go away |
| `orchestrator_status.py` | 231 | maestro (reporting) | `maestro status` already exists (`cli.py:682`); `/status` is rewired to it. **Parity matters — see §5.** |
| `maestro_ask.py` | 124 | maestro (HITL) | `maestro/hitl/ask.py`, invoked as `python -m maestro.hitl.ask` |
| `maestro_redo.py` | 265 | maestro (self-heal) | folded into `maestro/selfheal/redo.py` |
| `maestro_selffix.py` | 157 | maestro (self-heal) | folded into `maestro/selfheal/selffix.py` |
| `gen_dependency_map.py` | 270 | maestro (3-doc engine) | `maestro/docs/depmap.py` |
| `gen_upcoming.py` | 661 | maestro (3-doc engine) | `maestro/docs/upcoming.py` |
| `mark_task_complete.py` | 215 | maestro (3-doc engine) | `maestro/docs/complete.py` |
| `check_roadmap_consistency.py` | 271 | maestro (3-doc engine) | `maestro/docs/consistency.py` |
| `check_verifications.py` | 140 | **split** | the *engine* (parse the roadmap's verification stanzas, run each, compare stdout to the declared expectation) is maestro's gate → `maestro/verifications.py`, called directly by `gates.py`. The *commands* stay project data in `docs/ROADMAP.md`. This keeps the seam and closes R3. |

≈2,369 lines. Everything is extracted under **M1 rules** (bodies verbatim; only imports, path globals
via `maestro.paths.Paths`, and project-specific constants that must become configuration change; a
defect found is appended to `docs/FOUND_BUGS.md` and copied wrong anyway).

**Not maestro's, left in the project:** `scripts/canary_deploy.py` and the `check_*.py` verification
commands the roadmap names (`check_pairs_complete.py`, `check_lancedb_migration.py`,
`check_eval_calibration.py`) — those are "how to ship" and "what good looks like". `canary_deploy.py`
keeps its own `notify_telegram.sh` shell-out (R12), so **`notify_telegram.sh` must not be deleted at
M5** unless that call site is rewired too; see §6.

### 2b. The exact inventory — verified by the orchestrating session, not by an agent

`grep -rn 'REPO / "scripts"' maestro/ --include=*.py` → **21 call sites across 12 distinct scripts.**
Ten are on §11's delete list; **two more are maestro-owned by §4 but are *not* on the delete list**,
which the survey did not flag because it scanned the delete list rather than maestro's constants:

| Script | Sites | On §11 delete list? | Exists today | Disposition |
|---|---|---|---|---|
| `check_verifications.py` | `gates.py:29` | yes | yes | extract (R3) |
| `notify_telegram.sh` | `hitl/telegram.py:35`, `merge.py:25`, `docs/roadmap.py:34` | yes | yes | extract (R4) |
| `orchestrator_status.py` | `hitl/commands.py:62`, `parking.py:64` | yes | yes | rewire to `maestro status` (R13) |
| `maestro_ask.py` | `hitl/commands.py:615` | yes | yes | extract (R8) |
| `maestro_redo.py` | `hitl/commands.py:663` | yes | yes | extract (R9) |
| `maestro_selffix.py` | `selfheal/selffix.py:135` | yes | yes | extract (R10) |
| `gen_dependency_map.py` | `docs/roadmap.py:31`, `parking.py:61` | yes | yes | extract (R5) |
| `gen_upcoming.py` | `docs/roadmap.py:32`, `parking.py:62` | yes | yes | extract (R7) |
| `mark_task_complete.py` | `docs/roadmap.py:35` | yes | yes | extract (R6) |
| `check_roadmap_consistency.py` | — (only the project's pre-commit hook) | yes | yes | extract (R2) |
| **`send_dan_request.py`** | `hitl/telegram.py:36`, `implementer.py:55`, `parking.py:65` | **no** | yes, 200 lines | **HITL — maestro's per §4.** Survives M5, so nothing breaks, but the seam stays violated: maestro would keep reaching into the project for its own escalation path. Extract here. |
| **`render_dependency_map.sh`** | `docs/roadmap.py:33`, `parking.py:63` | **no** | yes, 54 lines | **3-doc engine — maestro's per §4.** Same reasoning. Extract here. |
| `canary_deploy.py` | `orchestrator.py:123`, `hitl/commands.py:63` | no | yes, 186 lines | **Correctly project-owned** ("how to ship"). Leave it. It keeps its own `notify_telegram.sh` shell-out (R12) — see §6. |

So M4c extracts **twelve** scripts (≈2,623 lines), not ten. Done-when #4 becomes exact: after this
stage the only `REPO / "scripts"` constant left in `maestro/` is `canary_deploy.py`, twice.

---

## 3. Rules for every agent in this stage

Identical to M1's and M4b's:

- **Copy function bodies verbatim.** Change only the import block, path globals, and project-specific
  constants that must become configuration. Do not reformat, rename, simplify, or fix.
- **If the reference code looks wrong, append to `docs/FOUND_BUGS.md` and copy it wrong anyway.**
- **`/home/dan/projects/AbuAliArchive` is READ-ONLY.** Read its files; never write, never `git` there,
  never touch `.orchestrator/` or the `HALT` sentinel.
- **Run no git commands at all.** The orchestrating session commits.
- **Never read, print or echo `~/.config/maestro/dev_bot_token` or any `.env`.** A test that reaches
  `api.telegram.org` is a defect — monkeypatch it.
- `tests/test_purity.py` must stay green.
- Do not open `scripts/orchestrator_run.py` in full (4,567 lines); targeted `grep`/`sed` only.

## 4. Shape

Three batches, verified and committed between each — do not attempt all ten in one workflow.
Per-batch: Wave A writes `tests/characterization/test_<mod>.py` against the reference; Wave B
extracts and gets both subjects green; then rewire maestro's call sites.

| Batch | Modules | Why grouped |
|---|---|---|
| **1 — the silent-damage three** | `verifications.py` (R3), native Telegram send (R4), `/status` rewire (R13) | Highest severity; smallest surface; unblocks the rest |
| **2 — the 3-doc engine** | `docs/depmap.py`, `docs/upcoming.py`, `docs/complete.py`, `docs/consistency.py` (R5–R7, R2) | 1,417 lines, one coherent subsystem, shared roadmap parsing |
| **3 — HITL + self-heal helpers** | `hitl/ask.py`, `selfheal/redo.py`, `selfheal/selffix.py` (R8–R10) | All three share the "tmux window hides the failure" shape |

Then a final rewiring pass, inline rather than by agent (it is small and cross-cutting):
`_RESUMABLE_HARD_STOP` → maestro paths (R14); `maestro/templates/pre-commit` → call
`python -m maestro.docs.consistency` (R2); delete the now-dead `.exists()` guards.

## 5. `/status` parity — the M5 Done-when depends on it

M5's Done-when is "`/status` output matches the pre-cutover capture". Once `/status` is served by
`maestro status` instead of `orchestrator_status.py`, a byte-for-byte match is the wrong test.
**Define the comparison before the cutover, not during it:** capture the reference
`orchestrator_status.py` output at baseline, and require semantic field-by-field equality — phase id
and title, `in_flight` entries, `parked_tasks`, `waiting_on_dan` ids, quota/usage figures. Any field
the reference prints and maestro does not is a gap to close in this stage, not a difference to
explain away in the morning report.

## 6. Done when

Every command run and its output seen:

1. `python3 -m pytest -q` → exit 0, zero `F`/`E`/`s` markers; collected count recorded against
   M4b's **4530** baseline with the delta explained exactly.
2. Every new module's characterisation file green on **both** subjects, zero skips.
3. **The dangling-caller list is empty**: a new gate test, `tests/test_no_reference_sidecars.py`,
   walks `maestro/` with `pkgutil` and fails on any surviving path constant of the form
   `REPO / "scripts" / …`. It must include anti-vacuity tests, per `test_no_unresolved_pending.py`'s
   precedent — a walk that silently finds nothing must not pass.
4. `grep -rn 'scripts/' maestro/ --include=*.py` returns only comments and the template tree.
5. `/status` parity demonstrated field by field against the captured baseline (§5).
6. `tests/test_purity.py`, `tests/test_no_unresolved_pending.py`, `tests/test_extraction_complete.py`
   green (the last is **17**, not the 12 quoted in M4a's finding — see PROGRESS.md's M4b correction).
7. Reference project unchanged: HEAD `68056b5`, the 4 standing-modified + 5 untracked baseline,
   `stat -c '%s %Y' .orchestrator/state.json` → `888 1786256976`, HALT present.

---

## 7. What M5 then does differently — pre-flight notes the cutover session must read

These were measured during this survey and are not in `docs/DESIGN.md` §11.

- **`maestro init` is not safe to run unsupervised on the reference project.** Two of its writes are
  *not* protected by the `.new` rule (`cli.py:456,457`): it resets `paused_by_user`, `halted` and
  `proposal_test_notified` to `False` in an existing `state.json`, and it then runs a **live**
  `maestro run` against the repo for up to 120 s as its no-op-loop check. Under the M5 protocol the
  loop must not start until the verification step. Scaffold deliberately — file by file, or with the
  loop check understood and accepted — do not just run `init` and watch.
- **`init` ignores `$MAESTRO_REPO`** and takes a positional path only (`cli.py:955`); every other
  subcommand uses `--repo`.
- **`project.yaml` must be merged by hand, v1 → v2.** The project's existing file is `version: "1"`
  and has no `project:`, `roles:`, `gate:`, `docs:` or `thresholds:` block; `init` will write
  `project.yaml.new` beside it and change nothing. `maestro/config.py` **silently returns `{}`** on a
  missing key, a parse error or a missing file (`config.py:23-29`, bare `except`), so a half-merged
  file fails quietly, not loudly. The v1 file's `risky_set`, `deny_list_extra`, `secrets`,
  `prod_stores` and `bot_files` are live safety configuration and must survive the merge —
  `risky_set` also needs its four now-deleted script paths replaced by their maestro equivalents
  (R15), or the Sonnet risky-file reviewer protects nothing.
- **`.gitignore` and `.git/hooks/pre-commit` need manual merges too.** `_scaffold_file` writes a
  whole-file `.gitignore.new` (not an append), and a `pre-commit.new` that git will never execute.
- **maestro must be installed into the project's own venv.** The reference launches everything with
  `/home/dan/projects/AbuAliArchive/.venv/bin/python3`; maestro is **not** installed there. Install it
  (editable, from this checkout) along with `pyyaml`, `python-dotenv`, `requests`, and verify
  `import maestro` under *that* interpreter before cutting over.
- **Telegram env vars.** Nothing in `maestro/` loads `.env`; `hitl/telegram.py:73-74` and
  `hitl/commands.py:136` read `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALERT_CHAT_ID` from `os.environ`. The
  reference got them via `orchestrator_run.py`'s import-time `load_dotenv(REPO/".env", override=True)`
  — which M5 deletes. **Something must export them post-cutover** or every notification is silently
  unauthenticated. Decide where: the launcher, the systemd unit, or a `load_dotenv` in maestro.
- **The installed watchdog unit is a `/etc/systemd/system` unit and needs root to change (R1).**
  It is `enabled` but `inactive (dead)` since 2026-08-10 (clean exit 0), so it does nothing today and
  will not restart on its own — but at the next **boot** it runs the deleted `watchdog-launcher.sh`,
  fails 127, and restart-loops every 60 s until the start limit trips. `maestro init` only prints the
  three privileged commands; it never runs them. Per the operator's standing rule and
  `docs/EXECUTION.md`'s abort condition, **the build must not run them**: package them in one script
  (`scripts/m5-install-unit.sh`), leave it executable, and name it in the morning report. The cutover
  itself does not need root — the loop can be started by running the generated `launch.sh` directly.
- **`tmux` names.** Reference: watchdog in session `abuali-watchdog` window `main`; the orchestrator
  in window `orchestrator` of the **shared** `agents` session; cron resume in window
  `cron-orch-resume` of `agents`. maestro's templates use `{project_name}-watchdog`. The `agents`
  session is shared with the operator's other automation — it exists now and must not be killed.
- **`orchestrator_alive()` is a host-wide `pgrep -f` on script filenames** in both the reference and
  the extracted `maestro/watchdog.py`. After cutover those names change; confirm the extracted
  version greps for something that actually matches the new process, or the watchdog will conclude
  the loop is dead and relaunch it forever.
- **The project's 13 test files import `orchestrator_run` (R11)** and break at collection once it is
  deleted. `adapters/test` is a **required** adapter and does not exist in the project yet — `init`
  scaffolds one. Decide explicitly: the recommendation is to delete those 13 files in the cutover
  commit (they characterise code maestro now owns and tests itself, 4,530 tests deep) and point
  `adapters/test` at the project's real suite. Record it either way.
- **Stale `scripts/__pycache__/*.pyc` for 15 of the 16 deleted scripts** sit in the project. They are
  not on the delete list. Delete them in the same commit; a leftover `.pyc` is a confusing survival
  path when diagnosing an import error.
