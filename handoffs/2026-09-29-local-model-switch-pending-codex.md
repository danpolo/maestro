# Handoff: local model switching and DuetFlow pilot continuation

Read `AGENTS.md`, this file, and
`handoffs/2026-09-29-graph-p12-07-run4-vdan-pending-sonnet55-codex.md` first.
This is the entry point for **both** workstreams. The pilot handoff holds its run
IDs, verification evidence, and recovery procedure; query live state before acting
because its recorded V-DAN status may have changed.

## Goals

1. Continue the DuetFlow `07-reconciliation` pilot through Dan's real V-DAN,
   merge and acceptance, or evidenced halt/export/archive. Preserve its D30/D31
   follow-ups and use the pilot handoff for exact steps.
2. Build an **argument-free interactive local Maestro command** that lists models
currently in use, lets Dan select a current model, obtains fresh available-model
lists across supported harnesses, lets him select a replacement, checks the
relevant `model_context_limits.md`, registers a missing row with the existing
unknown-model defaults, reports that Dan should customize those limits, performs
the remaining safe configuration work, and reports completion. The selection
logic should be reusable for a later Telegram button flow, but **no Telegram
command in this task**. In the terminal, numbered choices are a reasonable
button equivalent; no UI choice has yet been committed.

## Pilot continuation

Check Telegram request **117** and the live controller/control DB first. At the
last verified point, run `run_qG3zSNhcUbdHKyDm` had passed gate 5 and proof
review 3, and awaited Dan's Spotify hand-edit V-DAN on snapshot `2f3bf5a`.
Only Dan can approve or reject that observation. If approved, verify merge,
graduation, acceptance, follow-up preservation, final export and SHA. If rejected,
failed, or uncertain, follow the pilot handoff's halt/export/archive path. At a
lease-free task boundary, adopt the already tested Sonnet 5.5 Maestro commit
`2cac0e7` and verify the pinned doctor; do not adopt it mid-task. Continue D30
and D31 only on their specified live evidence. The pilot handoff is authoritative
for these details and must be reconciled with current state before reporting.

## Dan's scope decision

Dan answered **"Maestro's shared default"** on 2026-09-29. Implement the switch
at Maestro's shared-default layer, so projects inheriting that default receive the
new model. Preserve deliberate project-specific overrides; do not silently edit
an explicit project pin merely because its model happens to match the old shared
default. The command should report such overrides and any active-run delay. This
is a concrete interpretation of Dan's wording; only clarify if repository evidence
shows that a project cannot inherit the shared default as described.

## Scope

- In: local command, live discovery for supported harnesses, selection, context
  row lookup/insertion, runtime configuration updates, clear result and failure
  reporting, tests, and documentation for the command; DuetFlow pilot settlement,
  safe Sonnet adoption, and the pilot's existing D30/D31 evidence goals.
- Out: Telegram model-switch command or callbacks, Context Gate, direct DuetFlow
  code repairs outside its graph worker, unrelated pilot code fixes.

## Verified reconnaissance

- No feature code or tests for this new command were written. Maestro repo remains
  on `feat/graph-engineering-foundation`; two preexisting untracked files
  `artifacts/graph-engineering/p12-pilots/duetflow.json` and
  `docs/GRAPH_ENGINEERING_ARTIFACT.md` were left untouched.
- CLI dispatch lives in `maestro/cli.py` (`build_parser` near line 2013 and `main`
  near 2085). Current runtime model sources include `maestro/backends/catalog.py`
  (`CLAUDE_CATALOG_DEFAULTS`, `CODEX_CATALOG_DEFAULTS`, AGY profile defaults),
  `maestro/limits.py` (`default_table_paths`, `resolve`, display/slug folding),
  `maestro/roles.py` (defaults derived from limits names), and each project's
  `project.yaml` role bindings. Assess the routing and backend consumers before
  deciding which persisted values the command must change; hardcoded code edits
  on every switch would defeat Dan's goal.
- `agy models` is a documented available-model listing command in the
  [Antigravity headless CLI docs](https://www.antigravity.google/docs/cli/headless/);
  installed `agy models --help` confirms the subcommand. Its actual output and
  machine-readable flags were not yet tested.
- Installed `claude --help` exposes `--model` and an interactive model picker but
  did not show a model-list subcommand. Context7's official Claude Code package
  did not return a model-list API for the query; do not invent one. Investigate a
  supported read-only discovery path or report that harness as unavailable.
- Installed `codex --help` exposes `--model` and `app-server` but did not show a
  top-level model-list command. The host has `~/.codex/models_cache.json`; check
  whether app-server offers a fresh model-list request before relying on cache.
- Context7 was invoked as required by `AGENTS.md`. For implementation, use the
  `context7-cli` skill on each unresolved current CLI/API question; if it cannot
  establish a capability, report that explicitly and use primary vendor docs only
  as permitted. Avoid a paid model call just to enumerate models.
- Existing `maestro/cli.py` doctor checks model context entries and probes IDs;
  reuse their rules where appropriate. `~/.claude/model_context_limits.md` already
  has Sonnet 5.5, and `maestro/limits.py` has `_SHIPPED_DEFAULTS` for missing
  tables. Inspect the live unknown-model row per harness before writing one.

## Next actions and verification

1. Check the live DuetFlow V-DAN question, run status, and Maestro pin. Handle
   Dan's actual verdict and continue the pilot as described above and in the
   pilot handoff. Keep the controller single-owned.
2. Apply Dan's shared-default scope. Check the live DuetFlow gate/pin before edits
   affecting its active runtime; `selfupdate.adopt` forbids mid-task adoption.
3. Verify read-only fresh model discovery for each supported harness on this host,
   with bounded timeouts and explicit errors. Distinguish account availability
   from a stale local cache or generic vendor catalog.
4. Specify the exact persistent records and transaction/rollback behavior for
   swapping one selected old model to one new model. Handle a cross-harness
   replacement, active runs, duplicate model IDs, missing limits, and discovery
   failure explicitly. The command must not silently rewrite unrelated projects.
5. Follow `test-driven-development`: write a failing public-CLI/vertical-slice
   test before production code, observe the expected red result, implement, and
   verify green. Read `writing-good-tests.md` before changing tests.
6. Run focused command tests and the repository's full suite. Verify an actual
   local interactive invocation against a disposable project, not the active
   DuetFlow config. Confirm the selected model, harness, context row, routing,
   doctor result, rollback/failure result, and completion report. Report concrete
   test counts and exit codes.

Suggested skills: `context7-cli`, `test-driven-development`,
`verification-before-completion`, `systematic-debugging` for failures, and
`close-session` at the next safe stop.
