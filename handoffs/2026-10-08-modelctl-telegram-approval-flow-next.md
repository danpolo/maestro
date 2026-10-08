# Build the simple Telegram approval flow for new models (modelctl + Maestro)

> **2026-10-08 update:** the design is settled; Dan answered every question. Continue from
> `handoffs/2026-10-08-modelctl-telegram-approval-build-next.md`, which replaces this file's Approach section.

## Goal

Dan must never again approve a model transition by reading a code patch. The 2026-10-07
19-file owner patch is the example of what to avoid. When modelctl finds a new model, Dan
answers **three meta decisions from Telegram**. Everything else happens automatically and
deterministically, with **no code edit, release or patch review for each new model**.

1. **Use the new model?** Approve or decline using the model modelctl detected.
2. **Which models does it replace, in which roles?** Buttons per role (implementer, judge,
   diagnoser, …) with a recommended default, e.g. "gpt-6.1-sol replaces gpt-6-sol for judge
   and diagnoser".
3. **Fill its `model_context_limits.md` row.** This is usually not a plain approval. For a new
   model the row is normally empty, so modelctl asks Dan **for each missing cell, one cell at
   a time**, and shows a suggested value or a related model's value where one exists. A cell
   that already has a value isn't asked again.

After the three answers, the system does the rest by itself:
- writes the limits row;
- publishes the shared policy revision;
- adopts at each project's next idle boundary;
- sends notices;
- retains and later prunes old rows.

Dan's words (2026-10-08): the changes modelctl touches, Maestro included, "should be simple
predictable and deterministic so my approval should be a meta step to make the transition
easier for me". Memory: `modelctl-approvals-are-meta-telegram`.

## Precondition: start only after the release session has committed

This session runs **in parallel with** the session working from
`handoffs/2026-10-08-modelctl-release-then-pilot-next.md`. That session commits the D-A–D-E
work, integrates it into `feat/graph-engineering-foundation`, makes modelctl's first commit,
releases, adopts for DuetFlow and resumes the pilot.

- Before anything else, confirm its steps 1–3 are done: commits exist on
  `feat/graph-engineering-foundation`, `/home/dan/projects/modelctl` has commits, and the
  modelctl-integration worktree has been removed. The STATE.yaml modelctl-integration-review
  thread and the integration plan's status header should record the SHAs.
- If they aren't done, stop and tell Dan this session is waiting on the release session. Do
  not build on the uncommitted worktree.

## Read first

- Root `AGENTS.md` and the auto-memory index. These memories apply especially:
  modelctl-approvals-are-meta-telegram, model-pins-class-level-auto-switch,
  per-agent-model-context-limits, deny-list-hits-ask-dan-with-recommendation,
  agent-permission-lists-not-classifier, agents-ask-dan-on-spec-gaps, agent-scratch-not-in-tmp,
  harness-auto-updates-must-not-break-gates, context-management-not-built.
- `docs/MODEL_TRANSITIONS.md`: the D-A–D-E operator contract as released.
- `docs/plans/2026-09-30-modelctl-integration.md` (D-A–D-E) and
  `docs/plans/2026-10-07-modelctl-class-transitions.md`.
- `docs/plans/2026-10-07-modelctl-owner-applied.md` and the modelctl repo's `docs/SPEC.md`.
- The D29/D30 Telegram ask-Dan pattern (options, recommendation, free text, buttons). Find it
  in Maestro's Telegram/HITL code, `maestro/hitl/`, before designing anything new.

## What already exists (verify against the committed code)

D-A–D-E already provides:
- class-level choices;
- MODEL_SET publication through a durable outbox;
- automatic shared-policy publication;
- adoption at the idle boundary;
- task freezing;
- dependency-inventory retention;
- `modelctl add <provider> --model <id>` with lifecycle inputs;
- `maestro models` (local picker) and `maestro models status|consume`;
- switch notices through send-to-me.

Build **only the gap**:
- The three decisions as Telegram interactions, persisted durably and resumable after a
  restart.
- Per-cell filling of a missing limits row, which then writes through modelctl's own
  transactional table code.
- Removing any per-model judgement that still needs a code change. Today `catalog.py`
  `SHIPPED_CATALOG_DEFAULTS` hardcodes `available_models`, strength and relative cost. A new
  model must become routable from the three answers plus the owner data, without editing
  `catalog.py`. This is exactly what forced a release for gpt-6.1-sol.

## Approach

- This is a design task with real latitude. Settle the open design questions first and
  bundle them into **one** question to Dan with recommendations. Use grill or brainstorming
  only where the D-A–D-E docs don't already decide. Likely questions:
  - which bot carries the asks (each project's bot, or one global bot);
  - where strength and cost come from for a brand-new class;
  - what happens if Dan never answers.
- Then use TDD with a lane test covering: detection → three Telegram answers (fake transport)
  → row written → policy published → an idle project adopts → a running task stays frozen.
- Tests use fixture limits tables, never the live `~/.claude`/`~/.codex` files.

## Parallel-session coordination

- Work in a new worktree under `/home/dan/projects/maestro/.scratch/worktrees/`, not under
  `/tmp`, branched from the integrated `feat/graph-engineering-foundation` HEAD. Commit early.
  If modelctl changes are needed, use a branch in `/home/dan/projects/modelctl`.
- **The pilot session owns** `/home/dan/projects/duetflow`, its `.orchestrator/`, the DuetFlow
  pin and HALT. Never touch them, and never run a DuetFlow controller or doctor.
- Don't change `~/.maestro/current`, any project pin, or the live
  `~/.claude`/`~/.codex` `model_context_limits.md` / `model_catalog.json`.
- **Live rollout is the last step and needs Dan's go-ahead first.** Rollout means a live
  `modelctl scan`/`consume`, installing the scan timer through one sudo script that Dan runs,
  and Telegram asks to Dan.
  - A live scan can publish a policy revision that DuetFlow would adopt automatically at its
    next idle boundary. That would change the pilot's models mid-pilot without a manifest
    amendment.
  - Before rollout, ask Dan once: hold DuetFlow's adoption until the pilot ends, or allow it
    with an amendment that the pilot session records.
- When integrating back into `feat/graph-engineering-foundation`, `git pull`/rebase on the
  pilot session's latest commits there first; it may have committed manifest amendments. The
  merge must be trivial (parallel-tasks-must-merge-trivially). Never push or merge to master.

## Out of scope

- The DuetFlow pilot itself.
- Context Gate.
- Antigravity (agy) adapter work.
- Paid discovery probes, unless Dan approves them.
- Releasing or adopting this work into DuetFlow during the pilot.

## Verification to report back

- The design decisions and Dan's answers.
- Commit SHAs in Maestro and modelctl.
- Full serial gate counts and exit codes for both suites.
- The lane test name and what it proves.
- Evidence that adding a new model needs no code change.
- Rollout status: done with Dan's go-ahead, or pending with the exact steps.
