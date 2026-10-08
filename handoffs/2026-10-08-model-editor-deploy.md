# Native window and max output come from the provider automatically; then deploy `/models`

## Goal

Dan asked "why do I even need the native window setting?" and then said **yes** to this plan
(2026-10-08): **modelctl reports each model's provider-verified native window, and for Claude its
max output, to Maestro automatically**. Dan should not type facts the provider already knows.

1. Build it: modelctl → class rows and MODEL_SET; Maestro uses the values instead of inherited
   or catalog guesses.
2. The `/models` card shows native window (and Claude max output when provider-sourced) as
   **read-only provider values**. The manual `--native-window` (already built, see below) stays
   **only as an override** for a model the provider reports wrongly. Keep it, but don't feature it.
3. Then deploy `/models` (bridge restart) and verify live. Haiku 5.5 should fix itself on the
   first consume after the change, with nothing for Dan to enter.

Don't change any model's values yourself.

## Facts verified 2026-10-08 (live, read-only)

- **Claude**: `ClaudeProvider(Env.default()).listing()` (Anthropic models API) gives `max_input`
  per model:
  - `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`: **1000000**;
  - `claude-haiku-4-5-20251001`: 200000.
  - Parsed at `modelctl/providers/claude.py:91`. The API item also has `max_tokens` (max
    output); it is hashed into the fingerprint at :92 but **not kept on `ClaudeModel`**. Add it,
    and check it is present and per-model before using it.
- **Codex: unresolved, needs Dan or evidence.** The bundled catalog (`CodexProvider(env).cli.bundled_models()`)
  gives `max_context_window` **872000 for all of** gpt-6-astra, gpt-6.1-sol and gpt-6-luna, and
  `context_window` 272000 for all three. Maestro's shipped catalog says luna 340K and sol 420K
  native. Neither catalog field matches.
  - So for Codex, don't switch automatically yet. Keep Maestro's catalog value, and ask Dan on
    Telegram (options / recommended / free text) which value is the true native window.
  - Recommendation: keep Maestro's catalog for Codex until verified. Claude goes automatic now.
- Codex max output: no per-model field seen. Keep the current value.

## Design notes (settle small gaps yourself)

- **modelctl:**
  - add `native_window_tokens` (and Claude `max_output_tokens` if verified) as **provider facts**
    on the class rows from `configured_models` and on MODEL_SET events.
  - Keep them separate from Dan's `routing_profile`, e.g. a `provider_limits` dict. Dan's
    `routing_profile.native_window_tokens` override wins when present.
  - A scan must emit a new SET when the provider value changes, so Maestro picks it up.
  - For Haiku 5.5, the SET still pending in the live outbox (event `90c18510…`) is re-checked
    against the current owner row, so once rows carry 1M it passes.
- **Maestro:**
  - `model_classes.validate_rows` accepts the new optional dict.
  - `model_detection.policy_for_set`: the native-window override block (it now reads
    `grade.get('native_window_tokens')`) falls back to the provider fact. The same goes for
    max_output when the owner profile has none.
  - `model_asks._edit_values` / `_reference`: show the provider value with a "from provider" mark.
  - The card's Native window cell stays editable only as an override, labelled so.
  - The `assumed_native` note on the card should then mostly disappear.
- **Effective context ceiling:** an inherited profile keeps its predecessor's
  `effective_context_ceiling` (Haiku 5.5: 120K, below its 190K fresh range). With a real native
  window, decide whether it should follow the lifecycle (the no-original branch uses
  `fresh[0]`). That's a follow-up; settle it if it's small, otherwise record it.
- Tests: modelctl (provider facts in rows/SET, change → new SET) and Maestro (provider window
  lets the 280K ceiling pass, the override wins, Codex is unchanged).

## What was built (all committed)

- **Maestro** `feat/graph-engineering-foundation`:
  - a24c174 `maestro/model_asks.py`: new kinds `menu` and `edit`; `policy_for_set` override in
    `model_detection.py`; `validate_rows` in `model_classes.py`; tests; docs.
  - bad0f6a: follow-ups doc.
- **modelctl** `/home/dan/projects/modelctl`:
  - 27e7134 `modelctl add --native-window` (an optional 4th profile field, `native_window_tokens`;
    it bounds the ceiling and Codex's window).
  - fe9d0b8: a stored native window survives profile edits and bounds lifecycle-only adds.
- **codex-bridge** `/home/dan/services/codex-bridge` (not git):
  - `model_asks_route.handle_command` writes `/models` (also `/models@bot`, and also when sent
    as a reply to an ask) to the inbox as `kind: command`;
  - `bot.py` checks it before the ask-reply route, and `/help` lists it;
  - tests added.
  - Pre-change backup: `~/.maestro/backups/codex-bridge-2026-10-08-models/`.
  - **Not live until the restart.**
- **Gates** (last run):
  - Maestro: 5605 passed, 1 xfailed, 1 failed. The failure is
    `tests/test_next_graph_prompt.py::test_live_state_threads_are_all_structured`. It reads live
    graph state owned by the DuetFlow session and fails identically with this work stashed.
    Not ours; leave it.
  - modelctl: 210 passed.
  - codex-bridge: 19 passed.

### UX as built

- `/models`: one table per provider of every routed model, then a button per editable model.
  Models with no modelctl limits row are named as not editable.
- Tap a model to get a card with one button per value. Edited values show `•`; Apply shows the
  change count, next to Cancel.
- Tap a value to get the value prompt:
  - reply with a value, Keep, Back to saved, or Back;
  - strength has its three buttons;
  - the prompt shows that metric on every other model.
- Values are checked on entry against the whole card: prepare < fresh < ceiling < window, and
  the ceiling and window must not exceed the native window.
- Apply runs `modelctl add <backend> --model <id> <lifecycle> [--window] [all profile flags +
  --native-window if any profile cell or the native window changed] -y`, then `consume`. It shows
  ✅ only if the SET it produced was consumed. Otherwise the card comes back with the error.

## Live finding behind the native window (first answer: "Editable cell"; superseded by automatic, above)

Dan's Haiku 5.5 limits review at 05:08 IDT (ceiling 280K, cost 0.05, max output 128K) reached
modelctl and `~/.claude/model_context_limits.md`, but **Maestro refused it**. Its policy still
uses Haiku 4.5's 200K native window for Haiku 5.5, and the old `_apply` ignored consume errors,
so the review said ✅. That is now fixed: applied means published.

**Haiku 5.5 state now:** modelctl's outbox still holds the unacknowledged Haiku SET (event
`90c18510…`, window 335000). Maestro routes Haiku 5.5 with cost 0.25, max output 32K, and input
window 200K. Every consume run reports that SET as an error until it is fixed.

**Superseded:** this was going to be Dan's manual entry (`/models` → `claude-haiku-5-5` →
*Native window* → Apply). With provider facts it fixes itself; the manual path remains as a fallback. The card already shows Dan's saved 0.05 and 128K, a ⚠️ line saying
Maestro still routes with 0.25 and 32K, and "Maestro currently allows up to 200K".

Expected result:
- modelctl add with all profile flags and `--native-window`;
- consume publishes both the old pending SET (it now passes, since `policy_for_set` reads the
  owner row's native window) and the new one;
- ✅.

## Deploy

1. Ask Dan to run `sudo bash /tmp/model-editor-bridge-restart.sh`. The script already exists:
   restart `codex-telegram-isolated` plus a status line. If `/tmp` was cleared, recreate it with
   those two commands.
2. Verify:
   - `systemctl is-active codex-telegram-isolated`;
   - `tmux -L codex-telegram-bridge capture-pane -p -t codex-telegram` shows a clean start.
3. Run consume once (or let the timer do it). Haiku 5.5's pending SET should publish with
   max_input 335K (min(1M, its 335K enforced window)), cost 0.05 and max output 128K. Then ask Dan
   to send `/models` and walk any one edit.
4. Afterwards confirm:
   - `MODELCTL_SOURCE=/home/dan/projects/modelctl .venv/bin/python -c "from maestro.modelctl_bridge import ModelctlBridge; print(ModelctlBridge().pending_events())"`
     no longer lists a Haiku SET;
   - the owner row `routing_profile` has `native_window_tokens`;
   - `maestro models status` (with `MODELCTL_SOURCE` set) shows a new revision adopted;
   - Haiku 5.5's profile in `catalog_defaults` has relative_cost 0.05, max_output 128000, and
     max_input = min(native, 335000);
   - the ask record under `~/.maestro/models/asks/` is `done`;
   - `journalctl --user -u maestro-model-asks.service` shows the process run.
5. If something fails, the card shows the error. Diagnose before retrying. The live service runs
   `/usr/bin/python3` (system python), not the venv.

## Open follow-ups (recorded in docs/MODEL_TRANSITIONS.md; don't build unasked)

- `consume` leaves a SET that differs from the current owner row pending forever.
- Abandoned edit asks are never pruned and stay open in status.
- A tap on a model whose edit is `applying` gives no feedback.
- An inherited profile keeps its predecessor's `effective_context_ceiling` (Haiku 5.5: 120K).
- The earlier ones: `NEW_MODEL_DISCOVERED` access status; unrouted profileless predecessor;
  unreachable `_find` fallback.
- Tell Dan, but don't fix unasked: codex-bridge `skills_approval.handle_callback` calls
  `tg.answer_callback_query`, which `bot.TelegramClient` lacks.

## Out of scope

- The DuetFlow pilot, and its failing live-state test.
- The Context Gate.
- agy and antigravity values.
- Changing model values yourself.
