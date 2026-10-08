# Build the Telegram model-approval flow (design settled, implement now)

> **2026-10-08 progress:** step 1 of the build has started. Continue from `handoffs/2026-10-08-model-asks-build-continue-next.md`, which records progress and the detailed design.

## Goal

Implement the design below. Every decision is already settled by Dan (2026-10-08). Do not re-ask
any of them. The parent brief is `handoffs/2026-10-08-modelctl-telegram-approval-flow-next.md`:
read its Goal, "Parallel-session coordination", "Out of scope" and "Verification to report back"
sections. They still apply. This file replaces its "Approach" section.

Outcome: when modelctl finds a model, Dan acts only from Telegram, with no code edit, release or
patch review.
- **Same-class successor:** it adopts automatically. Dan then gets a non-blocking ask to change its
  limits.
- **Brand-new class:** it waits for a three-step ask.

## Precondition (verified 2026-10-08 by the design session)

Release steps 1–3 are committed:
- Maestro `feat/graph-engineering-foundation` at 072d399, c4fc6b7, c0f484b and **0c7eb92** (HEAD).
- modelctl master at f39bbc8 and 61c9de0.
- The modelctl-integration worktree has been removed.

The release session continues in parallel from `handoffs/2026-10-08-modelctl-release-continue-next.md`.
Its job is the owner-suite fix, staging, DuetFlow adoption and the pilot. Before integrating,
`git pull`/rebase on its commits.

## Read first

- Root `AGENTS.md` and the memory index. Especially: **modelctl-approvals-are-meta-telegram**,
  which carries Dan's refined answers, model-pins-class-level-auto-switch, per-agent-model-context-limits,
  agent-scratch-not-in-tmp, do-cleanup-yourself, parallel-tasks-must-merge-trivially and
  context-management-not-built.
- `docs/MODEL_TRANSITIONS.md`, the D-A–D-E contract as released.
- modelctl: `docs/SPEC.md`, `modelctl/cli.py`, `core/scan.py`, `core/transitions.py`
  (`inherited_plan`, `record_set`, `registration_defaults`, `registration_profile`), `core/addflow.py`,
  `core/outbox.py` (`deliver_to_maestro`, `maestro_owns_switch_notice`) and `core/notify.py`.
- Maestro:
  - `maestro/model_detection.py`: `consume`, `policy_for_set`, `consume_set`, the switch journal and
    `CLASS_TRANSITION_NOTICE_VERSION`.
  - `maestro/model_policy.py`: `validate`, `replace_route`, `catalog_defaults`, `check_task_pin`,
    `provider_lifecycle`, `PolicyStore`.
  - `maestro/model_classes.py`, `maestro/model_replacement.py`, `maestro/model_lifecycle.py`,
    `maestro/model_commands.py`, `maestro/modelctl_bridge.py`.
  - The `SHIPPED_CATALOG_DEFAULTS` / `PIN_ONLY_MODELS` section of `maestro/backends/catalog.py`.
- Telegram patterns to reuse (rendering only; their transport is per-project):
  - `maestro/hitl/verify.py`, whose `render_permission` shows the Allow/Decline buttons;
  - `maestro/hitl/dan_request.py`: `build_message_and_markup` and the HTML `sendMessage` payload;
  - `maestro/status.redact_secrets`.

## Dan's decisions (2026-10-08) — settled

1. **Same-class successor** (for example gpt-6.1-sol after gpt-6-sol):
   - It stays automatic, as D-B already does: modelctl scan registers it with the predecessor's exact
     limits and window, then Maestro publishes and adopts.
   - **New:** after the switch is applied, send Dan a **non-blocking** ask that shows the inherited
     values. It offers *Keep* or *Change*. *Change* walks the cells one at a time.
   - There is no "use?" ask and no "replace?" ask for the same class. Nothing waits on his answer.
2. **Brand-new class:** the three-step ask, in this order.
   1. **Use it?** Use / Decline.
   2. **Which roles does it replace?**
   3. **Fill the missing cells one at a time.**
3. **Strength, relative cost and max output of a new class:** infer deterministically where
   possible and ask for the rest. **Every such ask also shows the other models' current values**
   (a compact table) for context.
4. **Carrier:**
   - Outbound uses the **send-to-me bot**. Its config is `~/.config/agent-send/telegram.env`, with
     keys `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; read them at runtime the way the send-to-me
     script parses them.
   - Inbound goes **through codex-bridge**, which stays the single listener on that bot.
     - Add a small deterministic route there: a callback whose data starts `mdl:`, and a message that
       replies to a model-ask message, are written as JSON files into `~/.maestro/models/asks/inbox/`.
       Maestro processes those files.
     - This is the first route of the deterministic "one listener routes to projects" idea Dan
       mentioned. Don't build more than this route.
5. **Rollout and DuetFlow:** allow DuetFlow to adopt **with a manifest amendment, recorded by the
   pilot session**. The live rollout itself still needs Dan's explicit go-ahead first (parent brief).

Not asked, so a default applies (gaps-are-not-decisions): an unanswered ask stays open with no
auto-apply. At most one reminder per 24 h re-sends the current step. `maestro models status` lists
open asks. A newer same-family detection supersedes a pending new-class ask.

## Facts established during design

- **The policy path already routes a new model without code.**
  - `policy_for_set` builds an existing-class successor from the predecessor's profile and an
    owner-set window.
  - A new class's route and profile come from the owner `routing_profile` (`--strength`,
    `--relative-cost`, `--max-output`), stored in `source.inherited_profiles`.
  - `catalog_defaults`, `check_task_pin` and `provider_lifecycle` accept adopted-profile models.
  - `SHIPPED_CATALOG_DEFAULTS` matters only as the **bootstrap seed** (`shipped_policy`), used when
    no shared policy exists yet.
  - gpt-6.1-sol needed a release because DuetFlow ran pre-D-A–D-E code (f13b2c7) with no shared
    policy. The light-route change (gpt-6-luna over gpt-5.6-luna) was a judgement call about the
    role and route. **Step 2 now covers that judgement.**
  - Gap 3 therefore means: **prove** with a test that a model unknown to `catalog.py` becomes
    routable from the answers plus owner data. Remove any remaining `SHIPPED_*` lookup the test
    trips over. Don't restructure the catalog unless the test needs it.
- **Existing building blocks to reuse:**
  - A new class already produces a `waiting_limits` proposal in `consume`, from
    `NEW_MODEL_DISCOVERED` against a contract-1 owner. **Hang the three-step ask on that proposal.**
  - The same-class switch already creates an `applied` switch record with
    `notification_pending`, delivered via `send_to_me`. **Replace that plain notice with the Keep /
    Change ask**, sent once per switch record.
- **The writer for limits and profile is `modelctl add <provider> --model <id> --handoff A-B --fresh C-D --ceiling E [--window W] [--strength S --relative-cost R --max-output N] -y`.**
  - It validates before writing, writes transactionally, runs a smoke test inside the rollback
    boundary and then hands MODEL_SET to Maestro via `deliver_to_maestro`.
  - It works both for a new row and for editing an existing row. Call it with all collected values
    at the end; never edit tables from Maestro.
  - If it fails, report modelctl's own error on Telegram, keep the ask open and offer *Retry*.
- **Limits table cells** (Markdown, `| Model | Prepare handoff | …`, marker `LIFECYCLE_MARKER`):
  - Prepare handoff is a range like `220K-250K`.
  - Normally start fresh is a range.
  - Exception ceiling is a single value like `450K`.
  - Codex also has an enforced-window table (`WINDOW_MARKER`). Its default is ceiling × 1.2 rounded
    to 10K, so ask for it only as optional, with a *Use default* button.
  - Validate each cell's format and its ordering against cells already entered as soon as it is
    entered. modelctl's `stage_check` remains the final authority.
- **Deterministic inference for a new class:**
  - Strength and relative cost: if step 2 picked roles, inherit from the replaced model(s) when they
    all agree; otherwise ask. Suggestions for lifecycle cells come from the provider's `Model unknown`
    row (`registration_defaults`).
  - Max output: Codex bundled metadata has `context_window`/`max_context_window` but **no max
    output**, so always ask for it, showing the other models' values.
  - The native input window is the provider metadata where it exists (Codex `max_context_window`).
- **Step 2 semantics:**
  - Replacing in role R sets R's preference on the new model's backend (`roles[R].models[driver]`)
    to the new model.
  - It is per role. The old route stays eligible; don't use `replace_route`, which replaces it
    everywhere.
  - Add a small `assign_roles(policy, (backend, model), roles)` revision builder in
    `model_policy.py`, validated like the others.
  - The default is no role changes: the model is added as a route only. Buttons toggle each role;
    the button text shows the role's current model on that backend. A *Done* button finishes.
- **codex-bridge:**
  - It is `/home/dan/services/codex-bridge/bot.py`, which dispatches callbacks at roughly lines
    1279–1281 (`self.inbox.handle_callback`, `skills_approval.handle_callback`) and handles messages
    around line 1298.
  - **It is not a git repo.** Back it up (copy to `~/.maestro/backups/codex-bridge-<date>/`) before
    editing. Model the route on `inbox.py`.
  - It must ack the callback (`answerCallbackQuery`), write the file atomically and drop everything
    else unchanged.
  - Restarting the bridge is a rollout step. It's a user process in tmux `codex-telegram-bridge`; no
    sudo is needed for it.
- The other send-to-me config reader, `codex-reset-monitor/dispatcher.py`, only sends. It doesn't
  conflict.

## Build plan

Use TDD throughout, in a worktree under `/home/dan/projects/maestro/.scratch/worktrees/model-asks`
branched from `feat/graph-engineering-foundation` HEAD. Commit early. If modelctl changes, use a
branch in `/home/dan/projects/modelctl`. Make the codex-bridge change as a reviewed, backed-up edit;
it isn't live until rollout.

1. **The `maestro/model_asks.py` state machine**, durable under `PolicyStore.home / 'asks'`:
   - Write records atomically under the store lock. Each record holds kind (`new_class` /
     `limits_review`), its proposal or switch id, step, answers, pending cell, Telegram message ids,
     the last sent time and the status.
   - It must be resumable after a restart: reprocessing an inbox file is idempotent, keyed by
     Telegram `update_id`.
   - Callback data is `mdl:<8-char ask id>:<action>`, which must fit in 64 bytes.
   - Free text: a reply to an ask message goes to that ask's pending cell. A non-reply text is
     accepted only when exactly one ask is waiting for text.
2. **Rendering**: Telegram HTML, redacted. Every cell or profile ask includes the compact table of
   the other models' current values.
3. **Transport**:
   - Outbound: Bot API `sendMessage`/`editMessageReplyMarkup` with the send-to-me config, behind an
     injectable transport so tests use a fake.
   - Inbound: `maestro models asks process`, which drains `asks/inbox/*.json`.
4. **Wiring**:
   - In `consume`: a new `waiting_limits` proposal creates a `new_class` ask (sends step 1).
   - An applied same-class switch creates a `limits_review` ask in place of the plain notice.
   - Completing a `new_class` ask runs `modelctl add … -y` through `ModelctlBridge`, as a
     subprocess, never by importing modelctl into Maestro. It then runs `consume`, then publishes the
     `assign_roles` revision through the existing `publish_and_adopt` path.
   - *Change* on a `limits_review` ask runs `modelctl add` with the edited cells.
   - Make modelctl's own owner notification for a new class ("Run: modelctl add") defer to Maestro
     when Maestro declares an ask-capability constant, the same way `maestro_owns_switch_notice`
     works today, so Dan isn't told twice.
5. **The codex-bridge route** plus its own small test, in the bridge's existing test style if it
   has one; otherwise add a focused unit test next to it.
6. **The lane test** (the parent brief requires one), with fixture limits tables and a fake
   transport:
   - modelctl detection of a brand-new class unknown to `catalog.py` → three Telegram answers through
     inbox files → row written by modelctl's transactional add → policy published with the route and
     role assignment → an idle registered project adopts → a running task stays frozen on its old
     binding.
   - A second lane: a same-class successor adopts automatically with inherited limits → the
     `limits_review` ask → *Change* one cell → MODEL_SET → a new revision.
7. **Docs**: update `docs/MODEL_TRANSITIONS.md` (the approval surface is now Telegram, plus the
   same-class review ask) and modelctl `docs/SPEC.md` (append a short "Telegram approval revision,
   Dan 2026-10-08").
8. **Full serial gates** for both suites, with counts and exit codes. Run Maestro's from the main
   checkout or the worktree, but remember that `tests/test_purity.py` scans nested worktrees under
   `.scratch/`. Run the final gate after the worktree is merged and removed, or expect those 3
   purity failures and rerun purity separately, as the release session did.
9. **Integrate**: rebase on the latest `feat/graph-engineering-foundation`. The merge must be
   trivial. Never push or merge to master. Remove the worktree and branch yourself.
10. **Rollout** needs Dan's go-ahead first. Ask him once, with the exact steps:
    - install the codex-bridge route and restart the bridge;
    - a live `modelctl scan`, plus `maestro models consume` if needed;
    - the scan timer, through one sudo script if it needs one (`modelctl/scripts/install-scan-timer.sh`);
    - a timer or path unit for `maestro models asks process`. A systemd `.path` unit on the inbox dir
      is the simplest choice.

    DuetFlow adoption is allowed, but the **pilot session** must record the manifest amendment.
    Tell that session through its handoff or STATE note; never touch DuetFlow yourself.

## Parallel-session coordination (unchanged from the parent)

- The pilot/release session owns `/home/dan/projects/duetflow`, its `.orchestrator/`, its pin and its
  HALT. Never run a DuetFlow controller or doctor.
- Don't change `~/.maestro/current`, any project pin, or the live `~/.claude`/`~/.codex`
  `model_context_limits.md`/`model_catalog.json` before rollout.
- Don't send live Telegram messages before rollout. Tests use the fake transport only.

## Out of scope

The DuetFlow pilot itself, the Context Gate, agy, paid discovery probes, and a general multi-project
Telegram router beyond the single `mdl:` route.

## Verification to report back

- Commit SHAs in Maestro, modelctl and the codex-bridge backup path.
- Both full serial gates: counts and exit codes.
- The lane test names and what each one proves.
- Evidence that a model unknown to `catalog.py` routes with no code change.
- Rollout status: done with Dan's go-ahead, or pending with the exact steps and commands.
