# Continue the Telegram model-ask build (state machine done; lanes, bridge, docs, gates, integrate left)

## Goal

Finish `handoffs/2026-10-08-modelctl-telegram-approval-build-next.md` (the **build handoff**; it
still governs: Dan's decisions, facts, build plan, coordination, out of scope, verification).
The detailed design is in `handoffs/2026-10-08-model-asks-build-continue-next.md` (the **design
handoff**). Don't re-ask Dan anything either settles. The previous session stopped at its
context-handoff threshold, not because it was blocked.

## Read first

- The two handoffs above, root `AGENTS.md`, the memory index.
- The commits below (`git show`), especially `maestro/model_asks.py` — read it whole before
  changing anything; it is the implementation of the design handoff.

## Progress

Maestro worktree `/home/dan/projects/maestro/.scratch/worktrees/model-asks`, branch `feat/model-asks`
(base 322dc7e; `feat/graph-engineering-foundation` was still at 322dc7e when this was written):
- `23dd9e0` — `assign_roles` + `tests/test_model_new_class_routing.py` (gap 3 evidence, 5 tests).
- `d3683ef` — build-plan steps 1–4 on the Maestro side:
  - `maestro/model_asks.py`: records, cells/validation, inbound `process`, `_apply` (bridge.add →
    `consume(asks=True)` → `assign_roles` via `publish_and_adopt`, all outside the lock), `flush`
    (new message per state change, keyboard edit for role toggles, 24 h reminders for open
    `new_class` only, stale close), `render`, `TelegramTransport` (+ `from_config`, None under pytest).
  - `model_detection.consume(..., asks=False, transport=None)`; `_open_asks` creates new-class asks
    (sorted by `first_seen`, newest supersedes, old proposal → `superseded`) and settles applied
    switches (covered by new-class ask / `limits_review` when `predecessor_id` set / silent otherwise).
    With `asks=True` the switch `notify` path is skipped.
  - `ModelctlBridge.add(backend, argv)` (subprocess `modelctl.cli.main`, 900 s, MODELCTL_HOME).
  - CLI: `maestro models consume` passes `asks=True` + `TelegramTransport.from_config()`;
    new `maestro models asks [process]`; `status` lists open asks.
  - `tests/test_model_asks.py`: 33 tests, green. `tests/test_model_*.py tests/test_cli*.py` green with
    `/home/dan/projects/maestro/.venv/bin/python` (system `python3` fails an unrelated thirdparty
    version check — always use the venv).
- Small design deviations made (keep or revisit): the *failed* step offers Retry / **Re-enter values**
  (re-walks the asked cells with entered values as "Keep") / Decline (or Keep as is for a review);
  an invalid reply re-renders the cell prompt with a "❗ … — reply again" prefix (one message, not two).

modelctl `/home/dan/projects/modelctl`, branch `feat/model-asks` (from master ccc0b2f):
- `a7808a1` — `outbox.maestro_owns_model_asks(env)` (shared `_declares` AST helper) and `scan.py`
  skips only the two `available:` notices when it returns true. `tests/test_scan.py` +
  `tests/test_maestro_outbox.py` pass (19). **No new test yet for the deferral.**

## Remaining work (build-plan steps)

1. **modelctl tests** (in `tests/test_maestro_outbox.py`): a provider with `env` (use the conftest
   `env` fixture so `inherited_plan` finds tables) and an unconfigured `gpt-7-comet` (family comet);
   tmp maestro source declaring `MODEL_ASKS_VERSION = 1` → no "Run: modelctl add" notice; `= 2` → notice;
   smoke FAILED → "announced, not usable yet" still sent. Provider needs `access_word()`.
   Consider an autouse `MAESTRO_MODELS_HOME` → tmp fixture in modelctl `tests/conftest.py`: `env`
   uses `maestro_src=REAL_MAESTRO`, and `deliver_to_maestro` runs the real consume.
2. **Update** `test_public_scan_delivers_one_combined_limits_and_switch_notice` and check
   `test_scan_set_consumes_and_follows_class` / `test_public_scan_retries_switch_notice_after_event_ack`
   against the worktree source: through the CLI, a successor now yields one `limits_review` ask held in
   `send_pending` (no transport under pytest) instead of the send-to-me notice. Direct `consume(...,
   notify=...)` calls without `asks` keep the old behaviour.
3. **Lane tests** in modelctl `tests/test_maestro_class_integration.py`, run with
   `MODELCTL_TEST_MAESTRO_SOURCE=<worktree>` (design handoff "Lane tests" section). Bridge subclass:
   `add()` calls `modelctl.cli.main(['add', provider, *argv, '--no-restart'], env=env, providers={'codex': prov})`
   in-process, capturing stdout. Pattern for registered project + frozen running task:
   `test_configured_installation_bootstraps_class_choices_without_registration_or_inference`.
   Fake transport + inbox files as in Maestro `tests/test_model_asks.py` (`tap`/`reply` helpers).
4. **codex-bridge route** (`/home/dan/services/codex-bridge`, not git): back up to
   `~/.maestro/backups/codex-bridge-2026-10-08/` first; `model_asks_route.py` + call in
   `bot.py:_handle_update`; test like `tests/test_skills_approval.py`. Inbox record fields must match
   `model_asks._valid_update`/`_find`: `update_id` (int), `kind`, `data` | `text`, `chat_id`,
   `message_id`, `reply_to_message_id`, `reply_to_text`. Not live until rollout.
5. **Docs**: `docs/MODEL_TRANSITIONS.md` (Telegram approval surface, review ask, `models asks
   process`), modelctl `docs/SPEC.md` ("Telegram approval revision, Dan 2026-10-08"; note the
   access-status limitation as a follow-up).
6. `spec-code-review` of both branches against the build handoff.
7. **Full serial gates** both suites (counts + exit codes). Maestro: the venv python;
   `tests/test_purity.py` scans `.scratch/` worktrees (3 expected failures until the worktree is
   removed — rerun purity after).
8. **Integrate**: rebase on the latest `feat/graph-engineering-foundation` (trivial merge required),
   fast-forward it; modelctl branch into master locally. Never push. Remove the worktree and both
   `feat/model-asks` branches yourself.
9. **Rollout ask** to Dan (one message, exact steps; see build plan step 10). Before it, list any live
   `waiting_limits` proposals under `~/.maestro/models/proposals` (each becomes a new-class ask).

## In scope / Out of scope

- In: steps 1–9 above; the rollout *ask*.
- Out: DuetFlow pilot, the Context Gate, agy, paid probes, any Telegram route beyond `mdl:`, live sends
  or rollout before Dan's go-ahead.

## Parallel-session coordination

Unchanged from the build handoff: the pilot/release session owns DuetFlow (`.orchestrator/`, pin,
HALT). Don't touch `~/.maestro/current`, pins, or live limits/catalog files before rollout. `git pull`/
rebase before integrating; tell the pilot session about the DuetFlow manifest amendment via its handoff
or STATE note, never touch DuetFlow yourself.

## Verification to report back

- SHAs: Maestro (after integration), modelctl, codex-bridge backup path.
- Both full serial gates: pass/fail counts and exit codes.
- Lane test names and what each proves; the gap-3 evidence (`tests/test_model_new_class_routing.py`, 23dd9e0).
- Rollout: pending with exact commands, or done with Dan's go-ahead.
- State the reminder-scope judgement (only open `new_class` asks are reminded; `limits_review` is
  non-blocking) and the access-status limitation (`NEW_MODEL_DISCOVERED` carries no access status).

## Suggested skills

`test-driven-development`, `verification-before-completion`, `spec-code-review`,
`finishing-a-development-branch`, `close-session` at the end.
