# Build the Telegram model editor (`/models`): see and edit every model's values from Telegram

## Goal

Dan wants to **see the current values of every model and edit them from Telegram, not the CLI,
with good UX** (Dan, 2026-10-08). He chose the **model-first** design below. Build it, test it,
have it reviewed, deploy it (one sudo bridge restart), and verify it live.

His motivation: he found wrong values on other models (likely, among others, every model
showing **max output 32K**, which is the shipped catalog default and not a verified per-model
value). He fixes them himself through this editor. Don't change values on his behalf.

## Read first

- Root `AGENTS.md` and the memory index. These memories apply especially:
  - talk-in-local-time
  - do-cleanup-yourself
  - codex-bridge-is-system-service
  - live-scan-reads-maestro-checkout
  - modelctl-approvals-are-meta-telegram
  - no-idle-subagent-polling
- `docs/MODEL_TRANSITIONS.md`, section "Telegram approval (Dan, 2026-10-08)": the contract the
  editor extends.
- `maestro/model_asks.py`: the ask state machine. The editor is a new ask kind on top of it.
- `/home/dan/services/codex-bridge/model_asks_route.py` and `bot.py`:
  - ~1284 routes `mdl:` callbacks;
  - ~1302 routes replies to ask messages;
  - ~1318 dispatches `/commands` to `_handle_command`.

## Dan's decision: model-first UX

```
/models  ->  "Models (tap one to edit)"  [claude-opus-5-5] [claude-sonnet-5-5] [claude-haiku-5-5]
                                         [gpt-6-astra] [gpt-6-luna] [gpt-6.1-sol]
tap gpt-6-luna  ->  card "gpt-6-luna (codex)", one button per value:
     [Strength: balanced] [Relative cost: 0.5] [Max output: 32K]
     [Prepare handoff: 120K-140K] [Start fresh: 180K-220K] [Ceiling: 280K] [Window: 340K]
     [Apply] [Cancel]          (edited cells marked •; Apply shows how many changed)
tap a value  ->  the usual value prompt:
     - reply with a value, or tap Keep <current>;
     - strength gets its three buttons;
     - shows THAT metric on every other Claude+Codex model (see "Already done" below);
     - checked on entry, with prepare < fresh < ceiling < window;
     - then back to the card.
Apply  ->  modelctl add <backend> --model <id> <lifecycle flags> [profile flags if any changed] -y
       ->  Maestro publishes and projects adopt between tasks  ->  ✅ message.
```

## Design notes (my recommendation; settle small gaps yourself)

- **New ask kind `edit`.**
  - Opened on demand by the command, not by consume.
  - Initial values: build them like `open_limits_review`:
    - lifecycle and window from the owner row (`_owner_row`);
    - profile via `_profile_values` on the current revision.
  - Reuse the rest of the machinery:
    - `_accept`, `_check_order`, `parse_cell`, `_cell_buttons`;
    - `_argv`, where the lifecycle is always sent and profile flags only if a profile cell changed vs `baseline`;
    - `_apply` (non-`new_class` path) and `flush`.
  - New steps:
    - `pick`: the card. After a cell is accepted, go back to `pick`, not `_advance`'s walk.
    - Apply → `applying`. With no change, Apply closes as `kept` without calling modelctl.
    - Cancel → `kept`.
  - Card buttons edit the message in place (`edit_pending`), like role toggles do.
    The value prompt is a new message, so replies thread.
- **The list.** It covers every model routed on a modelctl-managed backend (`BACKENDS[*].modelctl_provider`:
  claude and codex; antigravity is not managed). Use `_reference()` in `model_asks.py`, which
  already collects exactly these rows and values. A model with no owner limits row can't be applied
  through `modelctl add`: show it, but mark it read-only, or offer the lifecycle cells as a first
  registration. Your call.
- **One open edit ask per model.** `/models` again reuses or supersedes it; don't pile them up.
  Editor asks are never reminded.
- **Entry: the command `/models`.**
  - `bot.py` `_handle_command` must route `/models` (and maybe `/model <id>`) to the Maestro inbox,
    as `kind: command`. Today plain text goes to Codex, and commands go to the bridge's own handler.
  - Extend `model_asks_route.py` and its tests (`tests/test_model_asks_route.py`).
  - Maestro side: `_valid_update` and `_handle` accept `kind == 'command'`. The chat check
    stays as it is.
  - The path unit `maestro-model-asks.path` already fires `maestro models asks process`
    whenever the inbox is non-empty.
- **Tap ids.** List taps need a model reference. Telegram callback data is limited to 64 bytes,
  and `_find` allows 52 characters of action. Use the index into the list snapshot stored on the
  ask record, not the model id.

## State (verified 2026-10-08 ~05:30 IDT)

**Maestro** `feat/graph-engineering-foundation`:
- 594f59e — docs: rollout follow-ups.
- cd45cb5 — fix: close asks about superseded class members; set rows without a profile ask on
  Telegram. Includes the review fixes: version-aware supersession, route aliases, and successors
  skip the roles step.
- 74b133c — committed (see `git log`): per-metric reference table, gate 5593 passed + 1 xfailed.

**Already done: per-metric context (Dan's request 1).** A value prompt shows only the metric
being asked, on every other Claude and Codex model:

```
Normally start fresh on the other models:
claude  claude-opus-5-5    240K-270K
codex   gpt-6-astra        300K-350K ...
```

The window is Codex-only. Use and review messages keep this backend's full table. Reuse
`_table` and `_reference` for the editor's value prompts.

**Live rollout.** Done and verified:
- the bridge is active, with the `mdl:` route loaded;
- `maestro-model-asks.path` is enabled;
- the switch `~/.maestro/models/asks/telegram.enabled` exists.

**What happened to each ask:**
- Haiku 4.5's stale ask: closed automatically, after the fix.
- Haiku 5.5 limits review: Dan changed it, and it applied ("done").
- Codex gpt-5.5 and gpt-5.6-terra profile asks: **Dan declined both**. They stay declined. With
  the editor he can still set their profiles if they become routed. Today they're declined and
  unrouted, so they aren't in the list.

## Gates

- **Maestro**: run with **`.venv/bin/python -m pytest -q -p no:cacheprovider`**. The system
  `python3` has older pytest, pyyaml and requests and fails two third-party checks.
  - Expected: all pass, 1 xfailed. 5592 passed before the table test, so about 5593 now.
- **modelctl**: from `/home/dan/projects/modelctl`, run
  `MODELCTL_TEST_MAESTRO_SOURCE=/home/dan/projects/maestro python3 -m pytest -q`. Expected: 203 passed.
- **codex-bridge**: from `/home/dan/services/codex-bridge`, run `python3 -m pytest -q`. Expected: 16 passed.
  - It isn't in git, so back up the tree before editing.
  - The backup from the earlier change is `~/.maestro/backups/codex-bridge-2026-10-08/`; make a new one.
- An independent review (subagent) of the diff before deploying. It caught a real bug last time.

## Deploy (live effects; Dan asked for this feature, so building it is approved; restarts still need his sudo)

1. Maestro: the live services run code from `~/projects/maestro` (the path unit and modelctl's
   handoff). Committing there is live. Run the gate first.
2. codex-bridge: write `/tmp/model-editor-bridge-restart.sh`, containing
   `systemctl restart codex-telegram-isolated` plus a status line. Dan runs it with
   `sudo bash /tmp/model-editor-bridge-restart.sh`. Then verify yourself:
   - `systemctl is-active codex-telegram-isolated`;
   - `tmux -L codex-telegram-bridge capture-pane -p -t codex-telegram` shows a clean start.
3. Ask Dan to send `/models` and walk one edit. Confirm afterwards:
   - the change reached the limits file (`~/.claude|~/.codex/model_context_limits.md`), or
     modelctl state for profile cells;
   - `maestro models status` shows a new revision adopted.

## Open follow-ups (recorded in docs/MODEL_TRANSITIONS.md; don't build unasked)

- `NEW_MODEL_DISCOVERED` carries no access status.
- A successor whose predecessor is itself unrouted and profileless still fails consume with no ask.
- The "exactly one waiting ask" fallback in `_find` can't be reached through the bridge.
- Unrelated codex-bridge bug: `skills_approval.handle_callback` calls `tg.answer_callback_query`,
  which `bot.TelegramClient` doesn't define. Tell Dan; don't fix it unasked.

## Out of scope

- The DuetFlow pilot (owned by its own session).
- The Context Gate.
- agy and antigravity values.
- Changing any model's values yourself.
