# Shared models and class transitions

Model choices name classes, such as Sonnet or Sol, using modelctl family metadata.
A project that chooses Opus keeps that class when shared defaults choose Sonnet.
Its concrete Opus version follows the newest SET member in its adopted policy.
Graph preferences still obey model allowlists, capabilities, context, permissions,
availability and quota checks. A preference never bypasses those constraints.

## Registration and selection

modelctl owns provider tables, discovery, availability smoke tests and registration.
For a detected existing-class successor, `modelctl scan` inherits the predecessor's
exact lifecycle limits and enforced window, retains the predecessor rows, and emits
MODEL_SET after transactional verification. It does not wait for approval of a switch.
Dan reviews or adjusts those values from the Telegram limits review (below); the
underlying writer is still `modelctl add <provider> --model <id>`.

A new class waits for limits. Dan answers its Telegram ask (below), which ends in the
same owner add command; run that command by hand to accept or customize
that provider's actual Model unknown suggestions and enter its routing strength, cost
and verified output capacity. Missing fallback values require explicit lifecycle inputs;
unsafe windows are refused before writes. A same-backend successor inherits its
predecessor's routing profile when no shipped native profile exists. Input capacity
is capped by the enforced window. Profiles never transfer between providers.

Maestro automatically publishes one shared policy revision for a compatible owner's
MODEL_SET event. Existing classes replace their older eligible versions; new classes
add a route without changing role preferences. The argument-free `maestro models`
picker remains available for an explicit shared role/routing change, including a
cross-provider replacement, with a preview and confirmation. Missing rows direct
to modelctl add rather than writing the owner's tables from the picker.

Registration and publication are separate transactions. Registration can succeed
while publication or notice delivery needs retry. A failed manual selection reports
observed registration and publication separately; unavailable evidence is unknown.
A successful owner handoff does not imply every project has already adopted.

## Task lifetime and row retention

Projects adopt at their next proven idle boundary, between tasks. Deferred projects
retain their prior revision. A task freezes its project class choices and concrete
models for retries, bounded calls and controller recovery. Fresh historical task
literals obtain their family from the owner at freeze and resolve against the adopted
policy, rather than the moving owner version. Legacy launch reservation, execution
and recording share one backend decision; later operator/quota edits affect later
launch decisions. Intentional carried backends on retries remain supported.

modelctl keeps old lifecycle and enforced-window rows until Maestro's dependency
inventory proves that no registered adopted policy, unfinished frozen task, active or
paused run, or concrete legacy/attempt binding needs them. Unknown or malformed state
retains rows. The owner holds its mutation lock and Maestro policy lock from inventory
through deletion; adoption and task freeze use that same policy lock. A deterministic
complete script entry without a model/backend requires no LLM binding.

## Telegram approval (Dan, 2026-10-08)

Dan acts on a detected model from Telegram alone: no code edit, release or patch
review. `maestro models consume` (run by modelctl's handoff) opens the asks; Maestro
never edits the owner's tables, it runs `modelctl add ... -y` with the collected values.

- **Use card (Dan, 2026-11-01), which replaces the schema 2 walk below**: on schema 2, Use turns
  the alert message itself into one card: limits, max output, optional native window, and one
  button per effort (intelligence · relative cost). Known values are shown and editable; a model
  without a modelctl row starts empty, with the Model unknown row only as suggestions. Apply needs
  the required values and at least one effort; an effort left ❓ is not routed. agy has no effort
  dial, so its efforts come from each model's own variants. Done and failed arrive as a new message
  so they notify. Decline edits the alert and stops its reminders.
- **Every tap gets an answer**: `process` never leaves a tap in the inbox. A refused tap is reported
  on the ask's message and tapping again is the retry, because a file left in the inbox re-triggers
  `maestro-model-asks.path` until systemd gives up on it. That unit sets `StartLimitIntervalSec=0`
  and a short `ExecStopPost` pause so it cannot die that way. If taps still wait over an hour, `flush`
  warns Dan on Telegram at most once a day.
- **Pull from Drive (Dan, 2026-11-02)**: the alert, the card and a failed apply also offer *Pull from
  Drive*. It reads the model's one row from `ChatGPT/server-exchange/model-reference.csv` with
  `drive-pull`, checks it with the card's own rules, and applies it through the normal `modelctl add`.
  A bad row changes nothing and every problem is written on the message; fix the row and tap again.
  Blank row values count as missing even when modelctl has them. Read-only columns are ignored.
- **The Drive table** (`maestro/model_sheet.py`): one row per model across all providers, for ChatGPT
  to read and update. `maestro models consume`, an applied ask and `maestro models sheet` write it with
  `drive-push`, and only when something changed locally: new models get a row, a model whose values
  changed locally has that row's values replaced, and read-only columns are refreshed. All other
  values, unknown rows and extra columns stay as they are. Drive only suggests: nothing reaches routing
  without a Pull tap. Automatic writes need the `~/.maestro/models/sheet.enabled` marker.
- **A failed access test takes a model out of routing (Dan, 2026-11-02)** (`maestro/model_access.py`):
  each `maestro models consume` compares modelctl's recorded access with the current routes, so a
  missed run corrects itself. `modelctl retest` runs consume hourly while a model is `FAILED` or its
  tests are stopped, without spending a test on it. A
  `FAILED` model's route rows are saved to `~/.maestro/models/access-holds.json`, then a revision
  without them is published (source kind `access_hold`); it is not retired, because a retired route
  cannot return. Once `VERIFIED`, the same rows return at their old positions (`access_restore`). If
  the model already has routes by then, such as after a Pull, those stay and only the saved entry is
  dropped. Tasks keep the revision they froze, and projects adopt between tasks. modelctl tells Dan
  about a pass, and a rate limit stays silent and retries hourly. Maestro also alerts when routing
  refuses, once until the model's state changes: a take-out that would leave a role with no route able to serve it, or the policy
  with no routes, keeps the model routed; a refused restore keeps it out. `validate` does not check
  that roles stay servable, so the take-out checks it itself. Problems in this step appear under
  `access` in the command's output and never fail consume. While a model is out, a new MODEL_SET for
  it (a limits edit, say) cannot route it and stays unacknowledged or asks for a profile until it
  returns.
- **A model its provider removed leaves routing by itself (Dan, 2026-11-03)**
  (`maestro/model_access.py`): modelctl marks a model removed when a complete listing omits it, and
  `maestro models consume` then takes its routes out the same way, whatever its last test said, and
  tells Dan. Consume reads the outbox first, because a same-class switch needs its predecessor's routes.
  If a model left routing, it reads the outbox again, because that model's routes fail its backend's
  events. The hold also keeps the model's profile and class row, which a later `MODEL_SET` drops, so if
  the model is listed again its routes return intact, unless a test has since failed or no profile is
  left, which alerts Dan. If a role needs the model, it stays routed and Dan is alerted once. An
  unreadable state changes nothing. `maestro models retire` remains the deliberate, permanent step, and
  it also reaches a held model: a retired route cannot return, and `rollback` does not undo it.
- **A stopped access test asks Dan what to do (Dan, 2026-11-02)** (`maestro/model_access.py`): any
  failure other than a rate limit stops that model's automatic tests in modelctl. Only `FAILED` takes
  it out of routing; a login, network, timeout or unclassified failure leaves it routed. Each
  consume opens one `access_failure` card per stop, updates it if the reason changes and closes it if
  the stop has gone. A card whose action is running is left alone. While
  `ACCESS_ASKS_VERSION` is declared, modelctl sends no stop notice of its own.
  **Retry** runs `modelctl retest --provider P --model M` (up to 3 minutes). A pass restores any held
  routes, a rate limit returns the model to the hourly tests, and another failure keeps the card open
  with the new reason. **Remove model** drops the model's routes (source kind `access_remove`, refused
  with the reason if it would leave a role without a route), its saved routes and its old approval,
  then marks it `removed_by_dan` in modelctl. It is not retired and its limits row stays. If modelctl
  is busy, tapping again finishes the job. The removed card keeps a **Pull from Drive** button, because
  nothing else offers one for a model with no routes and a closed ask; the tap opens a re-add ask,
  and its `modelctl add` clears the mark. A model already routed is refused with that reason.
- **Dan's approved values beat inheritance (Dan, 2026-11-02):** when a model with no routes has a
  completed approval (Use card or Pull from Drive), its routes use those values even if an older model
  of its class is routed. Inheriting the older model's routes applies only when no approval exists.
- **Brand-new class** (a `waiting_limits` proposal): three steps. *Use it?* (Use /
  Decline), *which roles does it take over on its backend?* (toggle buttons, then Done;
  default none, so the model is only an eligible route), then each missing cell one at a
  time: strength and relative cost (inherited when every replaced model agrees), max
  output (always asked), prepare handoff, start fresh, exception ceiling, and for Codex
  the enforced window with a *Use default* button (ceiling x1.2 rounded to 10K). Lifecycle
  suggestions come from the provider's Model unknown row. The ask waits while modelctl's last
  access check for the model failed; modelctl re-checks each scan, and the ask opens once it
  passes. An inconclusive check asks anyway, with a ⚠️ line (Dan, 2026-10-08). Every value prompt shows a
  compact table of the other models' current values. On completion modelctl registers
  the row, its MODEL_SET publishes the route, and the role choices publish as one more
  revision; projects adopt between tasks. A failed add reports modelctl's own error and
  offers Retry, Re-enter values, or Decline. A newer member of the same class supersedes
  an open ask: by owner version when modelctl knows both, otherwise by detection order.
  A newer member that is already set and routed (under its id or an alias) supersedes
  too, so an old detection never asks about a class Maestro already has. An ask
  whose proposal was settled elsewhere closes itself, with a message only if Dan saw it.
- **Set row without a routing profile** (limits set, but no verified native capacity or
  owner profile): with asks, this is the same new-class ask, worded as "has limits but no
  routing profile". The walk asks only strength, relative cost and max output, because
  modelctl already holds the limits. A same-class successor skips the roles step, since
  it takes over its predecessor's roles anyway. Because this ask can be about an older
  member, it closes another ask only when owner versions prove it is newer. The proposal
  is durable, so the SET event is acknowledged and consume no longer fails on it. A
  decline sticks across replays. Without asks, consume still reports the actionable error.
  A successor whose predecessor Maestro never routed gets this ask too, with the roles step,
  because it has no roles to take over.
- **Same-class successor**: it switches automatically as above, then Dan gets one
  non-blocking *limits review* showing the inherited values: Keep, or Change to walk
  each cell with a *Keep <current>* button. Only changed cells reach `modelctl add`
  (all three profile flags when any profile cell changed); keeping everything calls
  nothing. Owner-entered SET events (manual adds, first-scan reconciliation) need no
  review and settle silently.

- **Model editor (`/models`, Dan 2026-10-08)**: Dan sees and edits any model's values from
  Telegram. `/models` lists every routed Claude and Codex model with its values (one table
  per provider); models without a modelctl limits row are listed as not editable. A tap
  opens that model's card, one button per value: strength, relative cost, max output,
  native window, prepare handoff, start fresh, ceiling and (Codex) window. Tapping a value
  sends the usual value prompt (reply, Keep, Back), and that metric is shown on every other
  model. The prompt checks the value against the whole card, including that the ceiling and
  window do not exceed the native window. Edited cells are marked •. Apply runs one
  `modelctl add` with the lifecycle, plus all profile flags when any profile cell or the
  native window changed. Apply with no change, or Cancel, calls nothing. A failure returns to
  the card with modelctl's or Maestro's error. A second `/models` replaces the list, and a
  second tap on a model brings back its open card, or its ⏳ saving status while it applies.
  Editor asks are never reminded. A card nobody touched for 24 h closes with a note (only if
  Dan saw it); its unsaved changes are dropped.
  **Provider facts (Dan, 2026-10-08)**: each scan stores the provider-verified native window
  and max output on the class row and the MODEL_SET as `provider_limits`
  (`native_window_tokens`, `max_output_tokens`), separate from Dan's `routing_profile`. A
  changed provider value emits a new SET. Claude reports both per model (Anthropic listing
  `max_input_tokens`, `max_tokens`). Codex reports its account listing's `max_context_window`
  as the native window and no max output (Dan, 2026-10-08): the enforced window is the
  exception ceiling x1.2 capped by it, and Maestro routes with that window (GPT-6 Astra: 540K,
  was the 400K backend default). Maestro uses them instead of an inherited or catalog window
  and max output (Haiku 5.5 had inherited Haiku 4.5's 200K and 32K), and a provider native
  window counts as verified native context for an automatic successor. The profile's
  `effective_context_ceiling` (router admission) is the model's own start-fresh lower bound,
  capped by its input window, never a predecessor's (fixed 2026-10-08: Haiku 5.5 120K → 190K, GPT-6 Astra 230K → 300K). The card shows them read-only ("From the provider"), and the Native
  window cell reads `1000K (provider)`.
  The **native window override** is Dan's value for a model the provider reports wrongly
  (modelctl `--native-window`, owner `routing_profile.native_window_tokens`); it and Dan's max
  output win over the provider facts. When either window is known, Maestro bounds the
  profile's input window and lifecycle check by it. The card also shows a profile still
  waiting in routing when modelctl's last change was refused.
- **Applied means published**: a review or edit is ✅ only when Maestro consumed its
  MODEL_SET. A SET left pending (Maestro refused the values) is a failure carrying the
  consume error, and Retry/Apply republishes without another `modelctl add` (2026-10-08:
  Haiku 5.5's review reported done while Maestro refused its 280K ceiling).

Each value is checked on entry (format, whole K, prepare < fresh < ceiling < window);
modelctl remains the final authority. An unanswered ask stays open and never applies
itself. Only open new-class asks are reminded, at most once per 24 h; a review is
non-blocking and never reminded. modelctl leaves its "Run: modelctl add" notice to
Maestro when `maestro/model_asks.py` declares `MODEL_ASKS_VERSION = 1`; it still sends
"announced, not usable yet". Maestro reads modelctl's per-model access check from its state
file (bridge `access`), as it reads `retired`.

Outbound messages use the send-to-me bot (`~/.config/agent-send/telegram.env`, read
like send-to-me; never under pytest, and only once the rollout switch
`~/.maestro/models/asks/telegram.enabled` exists). codex-bridge stays that bot's only listener: its
`mdl:` route writes button taps (`mdl:<ask id>:<action>`) and replies to an ask message
(each carries `mdl:<ask id>` in its footer) to `~/.maestro/models/asks/inbox/`, and
plain non-reply text stays with Codex, so Dan answers a value by replying to the ask (Maestro
ignores a non-reply message);
`maestro models asks process` drains them, idempotent per Telegram `update_id`, applies
finished asks outside the policy lock, then sends what changed. Ask records live under
`~/.maestro/models/asks/`. Without a transport, messages wait (`send_pending`) and are
sent by the next consume or process.

## Notices, status and older installations

With Telegram asks, an automatic switch is announced by its limits review; the plain
notice below remains for direct `consume(notify=...)` callers. One durable logical
switch notice covers both limits/window and routing-profile review. Delivery retries
through send-to-me. A crash after delivery but before persistence can resend; there is
no exactly-once transport contract. `maestro models status --json` shows adopted project
versions, original class choices, waiting limits, pending switch notices and open model
asks. `maestro models consume` reconciles owner events, pending notices and asks.

Older owners that only emit discovery events retain historical approval proposals;
review them with `maestro models review --proposal <id>`. Class-contract owners use
waiting_limits instead and publish automatically after registration. Old proposals
are settled when their destination SET event is consumed. Projects without reader
version 4 report upgrade_required until they adopt a compatible code release through
the separate code-adoption workflow. This document describes the candidate contract;
application, code rollout and live model operations remain separate authorized steps.

On the first compatible scan, existing configured limits reconcile durable SET events
even when their earlier discovery was acknowledged. Only the newest SET row in each
family reconciles; a failed newer registration leaves the retained current row usable.
Registration events keep their original predecessor and acknowledgement identity.
The consumer imports class facts into new immutable revisions, settles old waiting
proposals and adopts only at idle boundaries. Old frozen revisions remain untouched.
Initial reconciliation can publish switches for existing choices; repeated scans do
not repeat those publications or notices. A configured class without verified native
capacity or an explicit owner profile stays pending with an actionable consumer error.

## Automatic reconcile (Dan, 2026-10-08)

A change to Maestro's own derivation rules takes effect without a new MODEL_SET and without a
command. After the outbox, every `consume` re-derives each routed, current (newest set member
of its class) model whose profile came from an owner SET, from the owner's current row. It
uses a synthetic SET (`reconcile:` event id) through `policy_for_set`, and publishes through
the same journal, publish and adopt path as a real switch, so it is crash-safe. A model that
would not change costs one derivation and leaves no record; a second consume publishes nothing.
It never touches unrouted or rejected rows (no asks), shipped routes no SET derived, a model
whose Telegram ask is applying, or a change that would move roles or routes. A reconcile
needs no review or notice. The daily `modelctl scan` runs consume, so this is automatic.
`consume` reports `reconciled`; unreadable owner class rows are a `reconcile_skipped` note.

A SET that differs from the owner's current row (an older value the owner has since changed)
is acknowledged as `superseded`; a SET whose model has no current row stays pending.

Fresh task aliases may enrich adopted identity facts, but cannot replace adopted
versions, limits or profiles. Dynamic inherited capacity remains bounded by the
predecessor's effective native profile even if a later owner limit expands; increasing
that limit alone does not establish a larger verified native capacity.

## Open follow-ups from the live rollout (2026-10-08)

Recorded; fixed items are struck with their date:

- ~~`NEW_MODEL_DISCOVERED` carries no access status~~ Fixed 2026-10-08: Maestro reads
  modelctl's access check, holds the ask while it failed, and warns when it was inconclusive.
- ~~A successor whose predecessor is itself unrouted and profileless raises a plain consume
  error~~ Fixed 2026-10-08: it gets the profile ask, with the roles step.
- ~~The "exactly one waiting ask" fallback in `model_asks._find` can't be reached through
  codex-bridge~~ Fixed 2026-10-08: deleted; a non-reply message is ignored.
- codex-bridge (external owner): the missing `TelegramClient.answer_callback_query`
  method is repaired in current `~/services/codex-bridge/bot.py`; the owner handoff
  records 25 passing tests and a verified restart on 2026-10-08. Dan's phone-toast
  observation remains with that owner; slice6 performs no service change or real tap. Handoff:
  `~/services/codex-bridge/handoffs/2026-10-08-skills-approval-callback-fix.md`.
- Model editor (review of a24c174):
  - ~~`consume` leaves a SET refused for differing from the current owner row pending
    forever~~ Fixed 2026-10-08: acknowledged as superseded by the current row.
  - ~~Abandoned edit asks stay `open` in status~~ Fixed 2026-10-08: closed after 24 h idle.
  - ~~A tap on a model whose edit is `applying` gives no feedback~~ Fixed 2026-10-08: the
    ⏳ status is sent again.
- ~~No way to re-apply a model's current owner row without a new MODEL_SET~~ Fixed 2026-10-08:
  the automatic reconcile above. (Before it, Opus, Haiku and Astra each needed a manual repair;
  backups in `~/.maestro/backups/{provider-facts,haiku-ceiling,astra-ceiling}-2026-10-08/`.)
- Provider facts: Codex max output has no per-model field, so it stays at the 32K backend
  default. modelctl's Codex window never drops below Codex's own `context_window` default
  (272K): GPT-5.6 Terra (ceiling 220K) and GPT-5.5 (210K) get 272K, not ceiling x1.2. Dan, 2026-10-08:
  keep that floor; no override.

### P13 W1 slice6 reconciliation (2026-10-12)

The original list contains nine carried items: seven Maestro bugs already fixed,
one external bridge item, and one accepted provider-facts limitation. Slice6
rechecks the fixes on the released `0f0d2f9` code; it adds no production behavior.
The source/test evidence below supersedes stale live examples in the old handoff.
The execution ledger records fresh test counts and candidate qualification:
`handoffs/2026-10-12-p13-w1-slice6-ledger.md`.

| Original item | Current production seam | Existing regression evidence (`tests/`) |
|---|---|---|
| Access status absent | `model_detection._access`, `_open_asks`; failed access holds the ask, inconclusive warns | `test_model_asks.py::test_a_new_class_ask_waits_while_modelctl_access_check_fails`; `test_an_inconclusive_access_check_asks_with_a_warning`; `test_an_unreadable_access_status_still_asks` |
| Profileless predecessor errors | `policy_for_set`, `_policy_for_set_pairs`, `consume` route profile/pair requirements into durable asks | `test_model_asks.py::test_a_successor_of_an_unrouted_profileless_predecessor_gets_the_profile_ask`; `test_model_management_schema2.py::test_owner_model_profile_cannot_approve_a_new_class_pair` |
| Unreachable one-ask fallback | `model_asks._find` ignores non-reply messages | `test_model_asks.py::test_a_message_that_is_not_a_reply_is_ignored_even_with_one_waiting_ask` |
| Older SET pending forever | `policy_for_set` raises `SupersededSet`; `consume_set` journals it and `consume` acknowledges | `test_model_asks.py::test_a_set_older_than_the_current_owner_row_is_acknowledged_as_superseded`; `test_a_set_without_any_current_owner_row_stays_pending` |
| Abandoned editor card stays open | `model_asks._close_idle_edit`, called by `flush`, closes open edits after 24h, with a note only for seen cards | `test_model_asks.py::test_an_edit_idle_for_a_day_is_closed_with_a_visible_note`; `test_an_unseen_idle_edit_closes_silently` |
| Applying edit tap gives no feedback | `model_asks._on_callback` → `_open_edit` resends saving status without replacing the applying edit | `test_model_asks.py::test_a_tap_on_a_model_whose_edit_is_applying_resends_its_saving_status` |
| Rule changes require SET replay | `model_detection._reconcile`, `consume_set` rederive routed owner-derived current rows through the publication journal | `test_model_asks.py::test_a_rule_change_republishes_a_consumed_model_once_without_a_new_set`; reconcile skip, silent-notice and crash-recovery tests in the same file |

Schema2 refines the historical roles step: new/unrouted classes need explicit
approval of each deliverable effort pair and skip model-to-role assignment;
same-class successors inherit the approved pairs and preserve role requirements.
`test_model_management_schema2.py` exercises both paths. Automatic reconcile
preserves the pair table and never changes grades by itself. The seven closures
do not authorize replaying historical asks, changing live model values/policy,
overriding the accepted Codex floor, or operating the external bridge. Context Gate
remains unbuilt. W2+ retain their existing scope.
