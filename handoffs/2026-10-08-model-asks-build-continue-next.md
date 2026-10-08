# Continue building the Telegram model-ask flow (design settled, step 1 of the build done)

> **2026-10-08 progress:** the state machine, consume wiring, bridge `add` and CLI are committed (d3683ef). Continue from `handoffs/2026-10-08-model-asks-build-continue-2-next.md`.

## Goal

Finish implementing `handoffs/2026-10-08-modelctl-telegram-approval-build-next.md` (the **build
handoff**). That file still governs. Its Goal, Dan's decisions, Facts, Build plan, Parallel-session
coordination, Out of scope and Verification sections all apply unchanged. Don't re-ask Dan anything
it settles. This file records progress so far plus the detailed design the previous session worked
out, so you don't have to derive it again.

The previous session stopped at its context-handoff threshold, not because it was blocked.

## Read first

- The build handoff (above), then the files its "Read first" section lists. The previous session
  read them all; the design below is grounded in them.
- Root `AGENTS.md` and the memory index (the build handoff names the important memories).
- The worktree commit `23dd9e0` (see Progress).

## Progress

- Worktree: `/home/dan/projects/maestro/.scratch/worktrees/model-asks`, branch `feat/model-asks`,
  branched from `feat/graph-engineering-foundation` at **322dc7e**. Note that 322dc7e is newer than
  the 0c7eb92 the build handoff names: the release session has since added a DuetFlow adoption
  record. modelctl master is now at **ccc0b2f**, also newer.
- **Done, at commit 23dd9e0:**
  - `assign_roles(policy, pair, roles)` in `maestro/model_policy.py`, placed next to `replace_route`.
  - `tests/test_model_new_class_routing.py`, 5 tests, green. They prove gap 3: the catalog-unknown
    `gpt-7-comet` routes from the owner's SET row (with its `routing_profile`) plus `assign_roles`,
    through `effective_roles`, `graph_catalog_defaults`, `check_task_pin`, `invalid_routes` and
    `provider_lifecycle`. **No `SHIPPED_*` lookup tripped**, so no catalog change was needed.
- **Not started:** build-plan steps 1–10, except the `assign_roles` part of step 4.

## Design settled by the previous session (implement this; it fits the build handoff)

### `maestro/model_asks.py`

**Declarations and storage**
- Declare `MODEL_ASKS_VERSION = 1` at module top level. modelctl reads it with `ast`, the same way
  `maestro_owns_switch_notice` reads `CLASS_TRANSITION_NOTICE_VERSION`.
- Records live at `store.home/'asks'/<ask_id>.json`.
- `ask_id = sha256(f'{kind}:{subject}')[:8]`. The subject is the proposal_id for a new class or the
  switch_id for a review. This makes creation idempotent.

**Record fields**
- `kind`, `subject`, `backend`, `model_id`, `family`
- `status`: open | applying | done | declined | kept | superseded
- `step`: use | roles | cell | review | failed | closed
- `cells` (the remaining queue), `pending_cell`, `answers`, `roles`, `role_options` (sorted snapshot),
  `current` (review values), `suggestions`
- `message_ids`, `current_message`, `send_pending`, `last_sent`, `applied_updates` (bounded, about
  100), `error`, `registered`

**Callbacks**
- Format: `mdl:<id>:<action>`, at most 64 bytes.
- Actions: `use`, `decline`, `r<i>` (toggle a role), `done`, `keep`, `change`, `retry`, and
  `set:<cell>:<value>`.
- Every value button uses `set:<cell>:<value>`: strength choices, "Use suggestion", "Keep current"
  and the window "Use default". The cell name is in the data, so a tap on an older message can't
  fill the wrong cell. Ignore it if `cell != pending_cell`.

**Cells**
- New class: `use` → `roles` → [strength] → [relative_cost] → max_output → handoff → fresh → ceiling
  → [window, Codex only].
  - Strength and cost are inferred, each separately, when every model currently preferred for
    `roles[R].models[driver]` in the chosen roles agrees on it.
  - Suggestions for handoff, fresh and ceiling come from `bridge.limits(backend)['unknown']`.
  - The window default is ceiling × 1.2, rounded to 10K.
  - If the owner already has a SET row or profile for the model, prefill those cells and skip them.
- Review: `review` (Keep / Change), then for each cell handoff, fresh, ceiling, [window], strength,
  relative_cost, max_output, a "Keep <current>" button or a reply.
  - If nothing changed, the status becomes `kept` and modelctl isn't called.
  - Pass the profile flags only when a profile cell changed, and then pass all three (modelctl
    requires them together).

**Validation**
- Mirror modelctl `tokens.py`: K = 1000, a whole number of K, `220-250K` shorthand allowed, and an
  ordering check against cells already entered:
  - `prepare_high < fresh_low`
  - `fresh_high < ceiling < window`
- An invalid reply sends a "❗ <error>, reply again" message and adds it to `message_ids`, so a reply
  to the error message also works.

**Inbound updates**
- A callback finds its record by the id in its data.
- A reply finds its record through `reply_to_message_id` in `message_ids`. The fallback is the
  `mdl:[0-9a-f]{8}` token in `reply_to_text`; every ask message carries that ref in its footer.
- A non-reply text is accepted only when exactly one open ask has `step == 'cell'`.
- Skip any `update_id` already in `applied_updates`.
- Drop an update when its `chat_id` doesn't match `transport.chat_id`.

**`process(store, bridge, transport, now=None)`**
1. Drain `asks/inbox/*.json` in `update_id` order. Handle each update under `store.locked()`.
   - If context computation raises `PolicyError` (bridge down), leave the file for a retry and
     report the error.
   - Otherwise delete the file.
2. Run every record whose status is `applying`, **without** holding the lock. The `modelctl add`
   nested `deliver_to_maestro` runs `maestro models consume`, which takes `policy.lock`, so holding
   the lock here deadlocks.
   - If `registered` is not yet set: call `bridge.add(provider, argv)`, and on success set
     `registered=True`.
   - Then run `consume(..., asks=True)`.
   - If roles were chosen, run `assign_roles` through `preview_base` + `publish_and_adopt`, and
     skip it if the roles are already assigned.
   - Then set the status to `done`.
   - On failure, set the step to `failed`, keep the ask open, and offer a *Retry* button. The error
     text is modelctl's own `FAILED:` lines (or its tail), redacted and capped.
3. Run `flush`.

**`flush`**
- Send each record with `send_pending`. Before sending, best-effort remove the keyboard from the
  previous message.
- Every state change sends a **new** message, so that replies thread. The exception is a role
  toggle, which edits the keyboard in place (`editMessageReplyMarkup`).
- Terminal outcomes also send a short closing message: declined, kept, superseded, done.
- Reminders: one per 24 h, for **open `new_class` asks only**. The `limits_review` ask is
  non-blocking, so reminding about it every day would be spam. Mention this judgement in the final
  report (gaps-are-not-decisions).
- Close an open `new_class` ask whose proposal is no longer `waiting_limits` while its step is still
  use, roles or cell, for example because Dan ran `modelctl add` by hand.

**Rendering**
- Telegram HTML through `redact_secrets`, with `html.escape` on every interpolated value.
- Every cell or profile ask includes a `<pre>` compact table of the other routes on the same
  backend. Columns: model, strength initial, cost, max output, handoff, fresh, ceiling, and window
  for Codex.
  - The profile comes from `catalog_defaults(policy, {})`.
  - Lifecycle and window come from the owner's `configured_models` SET rows, matched by model_id or
    alias. If the bridge fails, show `?`.

**Transport**
- `TelegramTransport(token, chat_id)` with `send(text, markup) -> message_id` and
  `edit(message_id, text=None, markup=None)`, using `requests`, the same way `dan_request` does.
- `TelegramTransport.from_config(path=None)` parses `~/.config/agent-send/telegram.env` the way
  send-to-me does: `KEY=value` lines, a numeric chat id, and a file with private permissions.
- `from_config` returns **None** when the config is missing, **or when `PYTEST_CURRENT_TEST` is set**.
  The second rule is a safety net: subprocesses spawned from a test inherit the real HOME (see the
  risk below).

### Wiring into `model_detection.consume`

- Change the signature to `consume(store, bridge, *, notify=None, asks=False, transport=None)`.
  The CLI `consume` verb passes `asks=True, transport=TelegramTransport.from_config()`. Existing
  tests keep the old behaviour, because `asks` defaults to False.
- With `asks=True`, create a `new_class` ask, idempotently, for each proposal with status
  `waiting_limits`.
  - Supersession: an open `new_class` ask on the same backend and family for a different model
    becomes `superseded`, because the newest detection wins.
  - Decline sets the proposal to `rejected`, through `model_detection._save`.
- Applied switches with `notification_pending` stop calling `notify`.
  - If a `new_class` ask exists for that destination, settle the notice: it is covered by that ask.
  - Else, if `event.predecessor_id` is not None (an automatic inheritance), create a
    `limits_review` ask with `current` values. Then set `notification_pending=False` and
    `notification='model ask <id>'`.
  - Otherwise (owner-entered values, or the configured-installation reconciliation, both with
    predecessor None), settle silently. That prevents a burst of asks at rollout.
  - Checked in modelctl: `predecessor_id` is set only by `inherited_plan`. Manual add plans don't
    set it (`providers/codex.py:505`, `claude.py:361`).
- After those steps, call `model_asks.flush`.
- Also: `maestro models status` lists open asks (add them to the view and the `_adoption` lines).
  `maestro models asks process` is a new verb: add `"asks"` to the verb choices plus an optional
  positional action `process`.

### `ModelctlBridge.add(provider, argv) -> (ok, output)`

- Run a subprocess: `[sys.executable, '-I', '-c', "import sys; sys.path.insert(0,sys.argv[1]); from modelctl.cli import main; raise SystemExit(main(sys.argv[2:]))", source, 'add', provider, *argv]`.
  - Set `cwd` to `source` and `stdin` to DEVNULL.
  - Set `MODELCTL_HOME` when `self.home` is set.
  - Use a 900 s timeout.
- Arguments: `--model M --handoff A-B --fresh C-D --ceiling E [--window W] [--strength S --relative-cost R --max-output N] -y`.
- Tests inject a fake bridge. The modelctl lane test subclasses it to call `modelctl.cli.main`
  in-process with the fake provider.

### modelctl change (branch in `/home/dan/projects/modelctl`)

- Add `outbox.maestro_owns_model_asks(env)`, using the same AST check against
  `maestro/model_asks.py` for `MODEL_ASKS_VERSION == 1`.
- In `scan_provider`, skip only the `available:` (VERIFIED, "Run: modelctl add") notice when it
  returns true. Keep the "announced, not usable yet" notice.
  - A known limitation: `NEW_MODEL_DISCOVERED` carries no access status. Maestro may therefore ask
    about a model that isn't usable yet, and the final add smoke then fails with modelctl's error
    and *Retry*. Note it as a follow-up.
- Append "Telegram approval revision, Dan 2026-10-08" to `docs/SPEC.md`.

### codex-bridge route (`/home/dan/services/codex-bridge`, not a git repo)

- Before editing, back it up to `~/.maestro/backups/codex-bridge-2026-10-08/`.
- Add a small `model_asks_route.py`, modelled on `inbox.py`, and call it from
  `bot.py:_handle_update` (around lines 1276–1281).
  - **Callbacks:** for data starting `mdl:` from an allowed chat, call `answerCallbackQuery` and
    write `{update_id, kind:'callback', data, chat_id, message_id}`.
  - **Messages:** route a message whose `reply_to_message` text contains `mdl:<8hex>` **before**
    the `inbox.extract_file`/codex enqueue path. Write
    `{update_id, kind:'message', text, chat_id, message_id, reply_to_message_id, reply_to_text}`.
  - Write each file atomically (tmp + `os.replace`) to `~/.maestro/models/asks/inbox/<update_id>.json`.
  - Pass everything else through unchanged.
- Test it in the existing pytest style, as in `tests/test_skills_approval.py`, using a DummyTG.

### Lane tests

- Put them in **modelctl** `tests/test_maestro_class_integration.py`, next to
  `test_public_add_new_class_publishes_with_supplied_profile`. Run them with
  `MODELCTL_TEST_MAESTRO_SOURCE=<worktree>`.
- Use `new_provider(env)` from `tests/test_new_class_input.py` (a fake Codex with `gpt-7-comet`) and
  a fake transport.
- **Set `HOME` to a tmp dir with monkeypatch in these tests.** Consider an autouse HOME isolation in
  the modelctl conftest; see the risk below.
- The second lane is a same-class successor (`provider(env,*IDS,'claude-sonnet-6')`): auto-adopt,
  then the `limits_review` ask, then *Change* one cell, then MODEL_SET, then a new revision.
- Maestro unit tests for `model_asks` belong in `tests/test_model_asks.py`, using fake bridge,
  transport and runner objects.

## Risks found

- **Live Telegram from tests.** `deliver_to_maestro` runs `maestro models consume` in a subprocess
  with the real `HOME`. Once the CLI sends asks, every modelctl test that reaches it, such as
  `test_public_scan_*`, would read the real send-to-me config and **send live**. Guard against this
  with both the `PYTEST_CURRENT_TEST` rule in `from_config` and HOME isolation in the tests.
  Existing modelctl test `test_public_scan_delivers_one_combined_limits_and_switch_notice` expects
  the old send-to-me notice. Update it to the new ask behaviour: one `limits_review` ask, held in
  `send_pending` because no transport is available.
- Before rollout, check the live `~/.maestro/models/proposals` for stale `waiting_limits`
  proposals. Each one would become a `new_class` ask on the first consume. List them for Dan in the
  rollout ask.

## In scope / Out of scope

As in the build handoff, build-plan steps 1–10. **Out of scope:**
- the DuetFlow pilot;
- the Context Gate;
- agy;
- paid probes;
- any Telegram router beyond the single `mdl:` route;
- live sends or rollout before Dan's go-ahead.

## Parallel-session coordination

Unchanged from the build handoff:
- The pilot/release session owns DuetFlow, its `.orchestrator/`, its pin and HALT.
- Don't touch `~/.maestro/current`, any pin, or the live limits and catalog files before rollout.
- Before integrating, `git pull`/rebase on the latest `feat/graph-engineering-foundation`. The merge
  must be trivial. Never push or merge to master.
- Remove the worktree and branch yourself.

## Verification to report back

From the build handoff:
- the SHAs in Maestro, in modelctl, and the codex-bridge backup path;
- both full serial gates, with counts and exit codes;
- the lane test names and what each one proves;
- the evidence for gap 3 (already the 5 tests in `tests/test_model_new_class_routing.py`, at commit
  23dd9e0);
- rollout status: done, or pending with the exact commands.

Also state the reminder-scope judgement and the access-status limitation.

## Suggested skills

`test-driven-development`, `verification-before-completion`, `using-git-worktrees` (the worktree
already exists), `spec-code-review` before integrating, `finishing-a-development-branch`, and
`close-session` at the end.
