# Roll out the Telegram model asks (built, reviewed, integrated; waiting on Dan's go-ahead)

## Goal

The Telegram model-approval flow is **built, integrated and gated**. What remains is the live
rollout, and **only with Dan's explicit go-ahead**.

- If Dan's message starting this session already says go: do the rollout below, then verify it.
- Otherwise: ask him once with the "Rollout ask" text below, and wait.

Never send live Telegram messages or change live units before that go-ahead.

## Read first

- Root `AGENTS.md` and the memory index. These memories apply especially:
  - talk-in-local-time
  - do-cleanup-yourself
  - manual-step-done-means-ran-it
  - no-idle-subagent-polling
- `docs/MODEL_TRANSITIONS.md`, section "Telegram approval (Dan, 2026-10-08)": the contract as built.
- `handoffs/2026-10-08-modelctl-telegram-approval-build-next.md`: Dan's decisions, and build-plan step 10 (rollout).

## State (verified 2026-10-08 ~04:20 IDT)

**Maestro** `feat/graph-engineering-foundation`: fast-forwarded 322dc7e → **45bcbc9**. Commits:
- 23dd9e0 — `assign_roles` + gap-3 proof (`tests/test_model_new_class_routing.py`, 5 tests)
- d3683ef — `maestro/model_asks.py` state machine, consume wiring, `ModelctlBridge.add`, CLI `models asks process`
- 00b6798 — docs
- df1f9c3 — review fixes:
  - redact each value before escaping;
  - no stale close after an apply attempt;
  - `asks/process.lock`;
  - the inbox waits when there is no transport;
  - exact relative cost;
  - no closing message for an ask Dan never saw
- 45bcbc9 — **rollout switch**: sends happen only once `~/.maestro/models/asks/telegram.enabled` exists.

**modelctl** master → **225780f**. Commits:
- a7808a1 — defers the `available:` "Run: modelctl add" notice when Maestro declares `MODEL_ASKS_VERSION = 1`
- 2e72a96 — tests: the deferral, the held review ask, both lane tests, and the autouse HOME/MAESTRO_MODELS_HOME isolation
- 225780f — the SPEC revision

**codex-bridge** (`/home/dan/services/codex-bridge`, not git). Backup of the pre-change tree: `~/.maestro/backups/codex-bridge-2026-10-08/`.
- New: `model_asks_route.py` and `tests/test_model_asks_route.py`.
- `bot.py` routes `mdl:` callbacks, and replies to an ask message, to `~/.maestro/models/asks/inbox/`.
- It's on disk but **not loaded**: the running bot predates it.

**Gates** (serial, after integration; logs are in `/home/dan/projects/maestro/.scratch/gates/*-final.log`):

| Suite | Result | Exit |
|---|---|---|
| Maestro | 5582 passed, 1 xfailed, purity included | 0 |
| modelctl (with `MODELCTL_TEST_MAESTRO_SOURCE=/home/dan/projects/maestro`) | 203 passed | 0 |
| codex-bridge | 16 passed | 0 |

**Lane tests** are in modelctl `tests/test_maestro_class_integration.py`:
- `test_lane_new_class_approved_on_telegram_is_written_by_modelctl_and_adopted_between_tasks`:
  - a catalog-unknown `gpt-7-comet` goes through Use → judge role → cells, as inbox files;
  - the real `modelctl add` (in-process, fake provider) writes the row and window;
  - the route and the judge role publish;
  - the idle project adopts, and the busy project's frozen task keeps its old binding.
- `test_lane_same_class_successor_adopts_then_review_change_republishes`:
  - claude-sonnet-6 inherits limits and adopts with no ask;
  - the review ask's Change edits one cell;
  - `modelctl add` emits MODEL_SET and a new revision is adopted.

**Live state**:
- `~/.maestro/models/` holds only `policy.lock`: no proposals, so no stale `waiting_limits` asks will appear.
- The user timer `modelctl-scan.timer` runs `~/.local/bin/modelctl scan --quiet` daily (about 06:00). Its handoff runs `maestro models consume` from `~/projects/maestro`.
- Until the switch exists, asks are created and held (`send_pending`), never sent.

## Facts the earlier handoffs got wrong

- The bridge is **not** a plain tmux user process. It is the **system** service
  `codex-telegram-isolated` (User=dan; it starts tmux on socket `codex-telegram-bridge`), so
  restarting it needs sudo. The script is ready: `/tmp/model-asks-bridge-restart.sh`. If `/tmp`
  was cleared, recreate it: `systemctl restart codex-telegram-isolated` plus a status line.
- The scan timer already exists as a **user** unit. No sudo is needed for it, or for a user
  `.path` unit.

## Rollout ask (send once if no go-ahead yet)

> The Telegram model asks are built and gated. Rollout needs one sudo command from you plus my user-level steps:
> 1. You: `sudo bash /tmp/model-asks-bridge-restart.sh` (restarts codex-bridge so it routes `mdl:` taps/replies).
> 2. Me: install user units `maestro-model-asks.path` (DirectoryNotEmpty on `~/.maestro/models/asks/inbox`) + `.service` (`maestro models asks process`).
> 3. Me: `touch ~/.maestro/models/asks/telegram.enabled` (the send switch).
> 4. Me: one live `modelctl scan`, then `maestro models consume` — expected result: no proposals exist today, so no ask is sent unless a new model is detected; `maestro models status` lists open asks.
> DuetFlow may adopt; the pilot session records the manifest amendment (already told via its handoff). Go?

## Rollout steps (after go-ahead)

1. Dan runs `sudo bash /tmp/model-asks-bridge-restart.sh`. Then verify yourself:
   - `systemctl is-active codex-telegram-isolated`;
   - `tmux -L codex-telegram-bridge ls`;
   - the end of `codex-telegram.log` shows no import error.
2. Write user units in `~/.config/systemd/user/`:
   - **`maestro-model-asks.path`**: `[Path] DirectoryNotEmpty=%h/.maestro/models/asks/inbox`,
     `MakeDirectory=yes`; `[Install] WantedBy=default.target`.
   - **`maestro-model-asks.service`**, `Type=oneshot`:
     - `Environment=MODELCTL_SOURCE=/home/dan/projects/modelctl`
     - `ExecStart=/usr/bin/python3 -I -c "import sys; sys.path.insert(0,'/home/dan/projects/maestro'); from maestro.cli import main; raise SystemExit(main(['models','asks','process']))"`
     - This matches how modelctl's `deliver_to_maestro` runs Maestro: `/usr/bin/python3` with the checkout on `sys.path`.
     - Check `ModelctlBridge`'s source resolution (`maestro/modelctl_bridge.py`) before relying on `MODELCTL_SOURCE`.
   - Then: `systemctl --user daemon-reload && systemctl --user enable --now maestro-model-asks.path`.
3. Run `touch ~/.maestro/models/asks/telegram.enabled`.
4. Run `modelctl scan`, then `maestro models consume` and `maestro models status`. Report what was sent.
5. Optional live smoke, only if Dan wants it. Write a fake inbox file for a non-existent ask id;
   `process` should report it ignored and delete it.

## Open follow-ups (record; don't build now)

- `NEW_MODEL_DISCOVERED` carries no access status. An ask can reach a model that isn't usable yet;
  the final add smoke then fails, with Retry. The fix is to carry `access` on the event (modelctl
  SPEC "Known limitation").
- Reminder scope is a judgement call: only open `new_class` asks are reminded, at most once per 24 h.
  `limits_review` is non-blocking and never reminded.
- Plain non-reply text goes to Codex. Dan answers a value by replying to the ask; this is documented.
  The "exactly one waiting ask" fallback in `_find` is therefore unreachable through the bridge.
- An existing codex-bridge bug, unrelated to this work: `skills_approval.handle_callback` calls
  `tg.answer_callback_query`, which `bot.TelegramClient` doesn't define, so a live
  `skill_upd:` tap would raise. Tell Dan; don't fix it unasked.
- Run the Maestro worktree gate from the main checkout. `tests/test_next_graph_prompt.py` needs
  the gitignored `docs/graph-engineering/` directory, which a worktree doesn't have.

## Out of scope

The DuetFlow pilot (owned by the pilot session, `handoffs/2026-10-08-duetflow-pilot-running-next.md`,
which already has a note about this release), the Context Gate, agy, paid probes, and any Telegram
route beyond `mdl:`.

## Verification to report back

- The bridge is active with the route loaded, and the path unit is enabled.
- The switch file exists.
- The live scan/consume/status output, with what was sent.
- Every item done is stated plainly; anything skipped is said.
