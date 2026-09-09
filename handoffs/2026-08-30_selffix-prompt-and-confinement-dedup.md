# Fix selffix's stale prompt, and the two code-review follow-ups

**Created:** 2026-08-30, by the session that closed the pre-pilot decoupling queue (items 1-4)
and then fixed a `/code-review high` pass on its own work. Baseline commit: `65479cf`, working
tree clean, suite green (3101 collected, exit 0, 0 failures).

**Goal:** three small, related fixes the prior session found but deliberately left open —
picked because they're quick and low-risk, per the operator's direction, not because they were
originally scoped together. Do all three; they turn out to share one design.

---

## Read first

1. `docs/PROGRESS.md` — search for **"Final review pass, commit `09ead04`"** and the two
   paragraphs above it (the CANARY_DEPLOY ruling, and "Found, not fixed"). That's the authority
   on what these three items are and why they were left open; don't re-derive it.
2. `maestro/confinement.py` — `allow_prefixes()` / `deny_fragments()`. The single source of
   truth every fix below reads from.
3. `maestro/selfheal/diagnose.py:79-111` (`_diagnose_failure`) — **the worked pattern to copy.**
   It already renders `confinement.allow_prefixes(confinement.SELF_FIX)` /
   `confinement.deny_fragments()` into two clean sentences: "A self-fix may only change files
   under: X. It must NEVER change: Y." This was fixed in an earlier session (its own docstring
   says so) and is the model for item 1 below.
4. `maestro/selfheal/redo.py:164-232` (`_redo_rules`, added this session) — the second, more
   recent implementation of "render a confinement surface as agent-prompt prose," for the
   `REDO` kind. Different wording from `diagnose.py`, and doesn't render the deny list. This is
   item 3 (follow-up B).
5. `tests/test_no_reference_sidecars.py` — read this **before** touching item 2. Its own
   docstring explains the gate; the gotcha for item 2 is spelled out below.

## In scope

### 1. `selfheal/selffix.py`'s runner prompt is still hardcoded (the "found, not fixed" item)

Two lines in the `brief` string inside `_run_selffix`'s runner (or whatever it's called now —
grep for `NEVER touch main_bot`):

- `maestro/selfheal/selffix.py:255` — `"Touch ONLY files under scripts/, orchestrator/, docs/."`
  — the reference project's old default allow-list, hardcoded, even though
  `confinement.DEFAULT_ALLOW[SELF_FIX]` and a real project's `confinement.self_fix_allow` may
  say something else entirely.
- `maestro/selfheal/selffix.py:256-257` — `"NEVER touch main_bot.py, .env, data/, models/,
  scripts/watchdog.py, scripts/restart_bot.sh, or any .service file — these are the RAG bot
  runtime."` — same problem as `/redo` had before this session's `1baf726`: a hardcoded deny
  list naming one project's files, while the actual gate (`_self_fix_path_ok` →
  `confinement.path_ok(kind=confinement.SELF_FIX)`) already reads the real configured surface.

Not a live bug (the gate protects regardless of what the prompt says) — but it's the exact
class of mismatch item 1 of the prior queue fixed for `/redo`, and it means a self-fix agent on
a real project is told about a surface that isn't its own.

**Fix:** replace both hardcoded lines with the real surface, the same way `_redo_rules` does it
for `/redo`. `selffix.py`'s confinement kind is `confinement.SELF_FIX` — the *same* kind
`diagnose.py` already renders correctly (see Read-first #3) — so this is a good candidate to
share that rendering rather than write a third independent version. That's follow-up B below;
do them together.

**Before touching the prompt text:** grep `tests/characterization/test_selfheal.py` for
`NEVER touch main_bot` / `Touch ONLY files under scripts` — as of `65479cf` neither string is
pinned by a test, but re-check, since it's the same file that pins `/redo`'s runner and this
session found things shift. If something *is* pinned, rewrite it per this repo's established
method (see PROGRESS.md's "Method" section in the prior handoff, or just mirror how `1baf726`
rewrote redo's tests) — don't delete it silently.

### 2. Follow-up A: the "optional project script, guarded by `.is_file()`" pattern exists 3x

`maestro/implementer.py`'s `CHECK_NB`, `maestro/selfheal/redo.py`'s `CHECK_NB` (same file,
`scripts/check_notebook.py`, checked independently in two modules), and
`maestro/merge.py`'s `CANARY_DEPLOY` (`scripts/canary_deploy.py`, added this session). Each is
its own `REPO / "scripts" / "<name>"` constant plus its own `.is_file()` check plus its own
comment re-explaining the same reasoning.

**The gotcha, read before designing anything:** `tests/test_no_reference_sidecars.py` scans
`maestro/` for the *literal textual shape* `REPO / "scripts" / "<name>"` and fails on any
occurrence not in its `ALLOWED` set (currently `check_notebook.py`, `canary_deploy.py`). Its
own `test_allowed_exceptions_are_still_actually_present` fails if an `ALLOWED` entry stops
being referenced in *that exact shape* anywhere. A naive dedup — extract a
`project_script(repo, name)` helper and call it as `project_script(REPO, "check_notebook.py")`
— makes the literal `REPO / "scripts" / "check_notebook.py"` shape disappear from the codebase
entirely, and that test starts failing. Read that test's own docstring (it explains why the
gate is this narrow) before deciding whether to widen its pattern, keep at least one literal
occurrence per name, or conclude the dedup isn't worth the friction.

**No fix mandated here** — investigate first, then decide. Two honest options, pick whichever
survives contact with the sidecar test:
- Keep each `CHECK_NB = REPO / "scripts" / "..."` constant where it is (satisfies the sidecar
  test unchanged) and only share the *is-it-there* predicate, e.g. a one-line
  `confinement.available(path) -> bool` — thin, but real duplication removed is real.
- Or: extend `test_no_reference_sidecars.py`'s scan to also recognize a parameterized call
  shape, then genuinely centralize the path + check. More invasive; touches a deliberately
  narrow purity gate, so needs its own justification in the commit message.

If neither feels worth it once you're looking at the real diff, it's fine to leave this one
alone again and say so — this item was already ruled "low value, revisit if a fourth instance
shows up" once; only re-open it if you find a cleaner path than the operator and the prior
session did.

### 3. Follow-up B: `redo.py`'s confinement-to-prose duplicates `diagnose.py`'s

Described above (Read-first #3/#4). Fix: extract the "may only touch: X. Must NEVER touch: Y."
rendering into one function — `maestro/confinement.py` is the natural home, next to
`allow_prefixes`/`deny_fragments` — and have `diagnose.py`, `redo.py`'s `_redo_rules`, and (once
item 1 is fixed) `selffix.py`'s runner prompt all call it. Doing items 1 and 3 together is why
they're bundled in this handoff: fixing item 1 from scratch and then extracting a shared
helper for item 3 means touching `selffix.py`'s prompt twice; doing the extraction first (or in
the same commit) means touching it once.

`redo.py`'s current wording differs from `diagnose.py`'s (a RULES-list bullet vs. a system-
prompt sentence) and doesn't mention the deny list explicitly — a `git diff` of the final
`_redo_rules` output is worth eyeballing to confirm the deny list addition doesn't make the
`/redo` prompt read strangely next to its other bullets.

## Out of scope

- Anything from the prior queue's remaining items (`SWITCH_THRESHOLD_PCT` calibration,
  `gate.baseline_source`/`gate.bands` — both still explicitly out of scope per PROGRESS.md).
- Running a pilot, scaffolding a real project, touching `AbuAliArchive` (it still has its own
  standing action item — `gate.chain`/`adapters/smoke` — that's the operator's to apply, not
  yours).
- Any further hunting for reference-project vocabulary beyond these three items. The prior
  session's grep baseline (45 hits, per-file breakdown in PROGRESS.md) stands; don't chase it
  down further unless one of these three fixes naturally touches a counted line.

## Method

Same as the prior two sessions: commit per logically-separate change (item 1+3 together makes
sense as one commit if you extract the shared helper as part of fixing selffix.py; item 2 is
independent — its own commit either way, including a commit that's just "investigated, decided
not to fix, here's why" if that's where it lands), suite green after each, and note real commit
hashes in `docs/PROGRESS.md` when you're done.

## Verification — report these numbers back

```bash
# 1. Suite. Baseline: 3101 collected, exit 0, 0 failures.
.venv/bin/python -m pytest -q ; echo "exit=$?"
.venv/bin/python -m pytest --collect-only -q 2>&1 | tail -3

# 2. The sidecar purity gate specifically (only matters if item 2 touches path constants).
.venv/bin/python -m pytest tests/test_no_reference_sidecars.py -q ; echo "exit=$?"

# 3. Confirm selffix's prompt no longer hardcodes the reference surface.
grep -n "main_bot.py\|RAG bot runtime" maestro/selfheal/selffix.py || echo "clean"
```

Report: final collected/passed count, whether item 2 was fixed or deliberately left (and why),
and whether `_redo_rules`/selffix's prompt/`diagnose.py` ended up sharing one function or stayed
separate.

## Suggested skills

- `superpowers:systematic-debugging` is not needed (nothing here is a bug hunt) — this is
  small, well-scoped refactor-with-a-purpose work. `superpowers:test-driven-development` fits
  the moment you're rewriting or adding characterisation tests for the prompt-text changes.
- `code-review` (default/medium effort is enough — this is a small diff) once all three items
  land, mirroring the prior session's final pass, since two of these three items *are* that
  pass's own findings.
