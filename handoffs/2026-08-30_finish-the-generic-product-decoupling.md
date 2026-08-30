# Finish the generic-product decoupling (pre-pilot)

**Created:** 2026-08-30, by the session that closed the M4 one-shot-call gap and fixed the
smoke-gate pilot blocker. Baseline commit: `7ab3904`, working tree clean, suite green.

**Goal:** finish removing the reference project (AbuAliArchive) from the generic package, so
the first pilot on a different repo behaves the way the docs say it does. Dan runs the pilot
himself — this handoff is the work that must land before it, not the pilot.

---

## Read first

1. `docs/PROGRESS.md` → the **"Post-programme session, 2026-08-30"** entry. Its numbered
   *"Still open — the next session's queue"* list is your backlog, in priority order, and is
   the authority if it and this file ever disagree.
2. `maestro/confinement.py` and `maestro/metrics.py` — the two modules added last session.
   They are the worked pattern for *"a value that differs per project is read from
   `project.yaml`, has a documented default, degrades rather than raising, and has a test
   that proves the knob changes behaviour."* Copy that shape.
3. `tests/test_templates.py::test_project_yaml_declares_no_key_that_nothing_reads` — the
   scoreboard. Its `known_dead` set is down to two entries; removing one is the acceptance
   test for wiring that knob up.

## State at handoff

| | |
|---|---|
| Branch / commit | `master` @ `7ab3904`, clean tree |
| Suite | `3085` collected, exit 0, 0 failures |
| `known_dead` template keys | 2 (`gate.baseline_source`, `gate.bands`) |
| Reference-vocabulary hits in `maestro/*.py` | **42** (see the verification command below) |
| Skills | installed and current at `~/.claude/skills/maestro-setup/SKILL.md` and `~/.codex/prompts/maestro-setup.md` |
| Venv | repaired — `.venv/bin/maestro` exists again (was missing entirely) |

## Parallel-session coordination

No other session is known to own any of these files, and nothing needs pulling — this is a
single-worktree repo on `master` and the last session left it clean. **Do not touch
`~/projects/AbuAliArchive`**: it has one outstanding action item (below) that is Dan's to
apply, and maestro must not grow a path dependency on it again (that coupling was
deliberately removed on 2026-08-21, commit `dca84a9`).

## Action item for Dan, not for you

AbuAliArchive's `gate.chain` is `[test]`, so after commit `9473a56` its smoke **skips**
instead of running `adapters/smoke.py`. To keep it: point `adapters/smoke` at that script and
add `smoke` to the chain. It had been running a gate its own config said was off. Mention it
if he asks; do not do it for him.

---

## In scope

Queue items 1–4 from `docs/PROGRESS.md`. Items 1 and 2 are **one question asked in two
places** — how does a project declare what its deliverables are and how they ship? — so
design them together even if they land as separate commits.

1. **`selfheal/redo.py`'s deliverable pipeline.** The path gate was decoupled last session;
   the pipeline was not. The `/redo` prompt instructs the agent about
   `scripts/check_notebook.py`, a `gdrive:` rclone remote and Colab links unconditionally,
   and `CHECK_NB` points at a script no scaffolded project has. 17 hits.
2. **`implementer.py`'s prep brief.** Larger blast radius than item 1: the brief handed to an
   implementer preparing *any* `needs-dan` task instructs it about free-tier Colab
   checkpoint-resumability, the `gdrive:` remote, `scripts/check_notebook.py` and
   `docs/CLAUDE_TASK_CONTEXT.md`. All unconditional. 12 hits. This affects every manual task
   on every project.
3. **`docs/upcoming.py`'s `GLOSSARY`** — 18 hardcoded retrieval terms (dense/sparse/RRF/
   Recall@5) in a project-agnostic package. The setup skill already collects a glossary into
   `docs/PROJECT.md`; read it from there rather than inventing a new key.
4. **`merge.py` leftovers.** Four separate things:
   - `bot_files` defaults to `["main_bot.py"]` **and is not declared in
     `project.yaml.tmpl`** — a live reader with no documented key, the mirror image of a
     dead knob. Declaring it needs a matching test, the same way the dead-knob list has one.
   - `CANARY_DEPLOY` points at `REPO/scripts/canary_deploy.py`; the `deploy` adapter
     (`adapters.KNOWN_KINDS`) is the proper seam and already exists.
   - `_HARD_DENY_PATTERNS` carries a `restart_bot\.sh` exception inside a regex.
   - `RESUMABLE_MERGE_PREFIXES` is `("data/", "docs/")`.

## Out of scope

- **`SWITCH_THRESHOLD_PCT = 70.0`.** Still uncalibrated on one real switch. If it is ever made
  configurable it becomes **one window-agnostic knob**, never the per-window
  `on_usage_threshold` block that was removed — honouring two per-window keys would mean
  reverting the G5 fix. Leave it.
- **`gate.baseline_source` / `gate.bands`.** These describe how to *score* a run against a
  baseline. That is the D8 eval story and a stage of its own, not a cleanup. Leave both on the
  `known_dead` list.
- Running a pilot, scaffolding a real project, or touching AbuAliArchive.
- Renaming or restructuring anything that is merely *ugly*. Every change here should remove a
  place where maestro assumes a project it is not looking at.

## Method

Follow the existing process, which last session used throughout:

- **Commit per change**, suite green after each. Message style: what was wrong, why it
  mattered, what the fix settles — see `9473a56` and `6c66514` for the shape.
- When a characterisation test pins reference-project behaviour that is now wrong, **rewrite
  it to pin the new behaviour** and say in its docstring what it used to assert and why that
  changed. Do not delete it silently and do not leave it pinning a fiction.
- When you find a pinned FOUND_BUG that is a real defect in a path an *unattended agent*
  chooses (last session found three: a `../` path escape, a character-by-character string
  iteration, a glob that matched nothing), fix it rather than carrying it, and say so.
- Update `docs/PROGRESS.md`'s queue as items close. Record real commit hashes — amending a
  commit changes them.

## Verification — report these numbers back

```bash
# 1. Suite. Baseline: 3085 collected, exit 0.
.venv/bin/python -m pytest -q ; echo "exit=$?"
.venv/bin/python -m pytest --collect-only 2>&1 | grep collected

# 2. Dead-knob scoreboard. Baseline: 2 entries (gate.baseline_source, gate.bands).
#    Report the length AND whether `bot_files` became a declared key.
.venv/bin/python -m pytest tests/test_templates.py -q ; echo "exit=$?"

# 3. Reference-project vocabulary still in the package. Baseline: 42 hits.
#    Report the new total and the per-file breakdown.
grep -rniE "recall@5|main_bot|colab|rclone|check_notebook" maestro/ --include="*.py" \
  | grep -v __pycache__ | wc -l
grep -rniE "recall@5|main_bot|colab|rclone|check_notebook" maestro/ --include="*.py" \
  | grep -v __pycache__ | sed 's/:.*//' | sort | uniq -c | sort -rn
```

Note on (3): a handful of the 42 are *deliberate* — `metrics.py` and `confinement.py` mention
`recall@5` / `main_bot.py` in docstrings explaining what they replaced, and that is worth
keeping. Report which files still hold hits, not just the count, so the residue is legible.
