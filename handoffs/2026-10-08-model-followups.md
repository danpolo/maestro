# Model-transition follow-ups: automatic reconcile first, then the review backlog

Run this session from `/home/dan/projects/maestro` (it also edits `/home/dan/projects/modelctl`).

## Sequencing and parallel sessions

- Start **after** the codex-bridge session
  (`~/services/codex-bridge/handoffs/2026-10-08-skills-approval-callback-fix.md`) has closed,
  as Dan asked. There is no file overlap (that session touches only `~/services/codex-bridge`),
  so if it is still running, just don't touch codex-bridge files.
- A DuetFlow pilot session may be live on this branch. It owns `maestro/hitl/`, the graph
  state and `tests/test_next_graph_prompt.py`. Leave those alone, and `git pull`/check
  `git status` before committing.

## Goal

1. **Automatic reconcile (do first, Dan 2026-10-08).** A change to Maestro's own derivation
   rules must take effect without a new MODEL_SET and without a manual command. Today Maestro
   recomputes a model's routing profile only when modelctl emits a new SET. modelctl dedupes SETs
   by content (`modelctl/core/transitions.py` `record_set`), and a replayed SET stops at its switch
   record (`maestro/model_detection.py` `consume_set`). So after the provider-facts and
   effective-ceiling rules changed, Opus, Haiku and Astra each needed a manual repair: move the
   switch record aside, un-ack the event under both locks, then consume. Backups are in
   `~/.maestro/backups/{provider-facts,haiku-ceiling,astra-ceiling}-2026-10-08/`.
   Wanted design: `consume` also re-derives every **routed, current set** model
   from the owner rows. That means running `policy_for_set` with a synthetic or current SET,
   reusing `_profile_current` / the stored-facts comparison as the cheap "nothing to do"
   check, and publishing one new revision only when the result differs. It goes through the
   same journal/publish/adopt path as a real switch, so it is crash-safe and idempotent. The
   daily `modelctl scan` already triggers consume, so this is automatic. **No new CLI command.**
   - It must not open Telegram asks for unrouted or rejected models (GPT-5.6 Terra and GPT-5.5
     are `rejected` proposals). It must not re-route roles, and it must not touch a model whose
     edit ask is `applying`.
   - Second consume run: 0 switches, same revision.
2. Then the review backlog, in this order. Each is recorded in
   `docs/MODEL_TRANSITIONS.md` § "Open follow-ups from the live rollout (2026-10-08)". Read the
   wording there; don't re-derive it.
   1. `consume` leaves a SET refused for "set event differs from current owner limits" pending
      forever. Acknowledge it as superseded once a newer SET or the current row exists. The
      reconcile in (1) may make this nearly free.
   2. Abandoned `/models` edit asks stay `open` forever. Prune them (e.g. after the menu is
      replaced, or after N hours idle) and close them with a visible note. One is live now:
      ask `ddc2b813` (Opus 5.5, open since 10:57 IDT).
   3. A tap on a model whose edit is `applying` gives no feedback. Reply with the ⏳ status.
   4. `NEW_MODEL_DISCOVERED` carries no access status.
   5. A successor whose predecessor is itself unrouted and profileless raises a plain consume
      error ("predecessor lacks a verified routing profile"). It should become the
      profile ask that a profileless set row already gets.
   6. The "exactly one waiting ask" fallback in `model_asks._find` can't be reached through
      codex-bridge. Decide whether to delete it or keep it with a comment; prefer deleting
      dead code if no test or contract needs it.
   For each item: fix it if it's small; if it turns out to be a design question, ask Dan on
   Telegram (options / recommended / free text) and record the answer in the doc.

## Read first

- `docs/MODEL_TRANSITIONS.md` (whole), `tasks/lessons.md` § 2026-10-08 provider facts
- `maestro/model_detection.py`: `consume`, `consume_set`, `policy_for_set`,
  `_profile_current`, `_admission_ceiling`
- `maestro/model_asks.py` (for items 2.2 and 2.3)
- `modelctl/core/transitions.py` (`configured_models`, `record_set`), `modelctl/core/scan.py`
- Commits for context: Maestro `4f90dbe`..`420a607`, modelctl `50c540b`..`7b4d0e6`

## In scope / out of scope

- In: the items above, with tests in both repos, plus doc updates (strike each fixed item in the
  follow-ups list with a date).
- Out: the Context Gate (AGENTS.md); agy/antigravity; changing any model's values; the
  codex-bridge bug (its own handoff); the 272K Codex window floor (Dan: keep it).

## Verification (report these numbers)

- Maestro: `.venv/bin/python -m pytest -n auto -p no:cacheprovider` → pass count (last run:
  5615 passed, 1 xfailed, 0 failed). modelctl:
  `MODELCTL_TEST_MAESTRO_SOURCE=/home/dan/projects/maestro python3 -m pytest -q` (last: 215).
  Without that env var, 35 integration tests skip silently.
- Reconcile test: a rule change (e.g. monkeypatch the ceiling rule) on an already-consumed model
  publishes exactly one new revision on the next consume with no new SET, and 0 on the one after.
- Live, read-only first: dry-run the reconcile against `~/.maestro/models` (pattern: compute
  `policy_for_set` against the live snapshot without writing) and report which models would
  change. Expect **none**, since all were repaired by hand on 2026-10-08. Then run
  `modelctl scan` and `maestro models consume` (with `MODELCTL_SOURCE=/home/dan/projects/modelctl`):
  0 errors, 0 switches, unchanged revision, and no new asks in `maestro models status`.
- Live profiles must stay: Haiku 5.5 335K/128K/ceiling 190K, Sonnet 5.5 385K/128K/220K,
  Opus 5.5 420K/128K/240K, Sol 420K/32K/240K, Luna 340K/32K/180K, Astra 540K/32K/300K.

## Suggested skills

- `test-driven-development`, `systematic-debugging` (if a live dry-run disagrees),
  `verification-before-completion`, `spec-code-review` before closing, `close-session`.
