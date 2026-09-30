# Next-session work: operator intake and modelctl gap review

Requested by Dan on 2026-09-30 after graph /ask installation. Read AGENTS.md and
`handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md` first. Preserve the
verified installed boundary and unrelated model changes. This plan records scope;
it does not approve a new architecture or a live playlist mutation.

## 1. Establish the operator bug/change/feature intake workflow

Dan reports that some Duet playlist song versions are unavailable to play even though
Spotify has another playable version of the same song. Use this as the concrete intake
example. His primary request is the mechanism for integrating bugs he finds, desired
changes, and new features into Maestro's normal workflow, rather than an isolated repair
of those songs.

- Inspect existing operator commands, graph task intake/ROADMAP authority, follow-up
  tasks, task reopening and controls before proposing another system.
- Determine a clear path from Dan's report to evidence/reproducer, classification and
  scoped acceptance criteria, an authorized queued task, and normal implementation,
  validation, review, Dan verification when needed, and acceptance/graduation.
- Explain how this works when another task is running or waiting on Dan: preserve
  immutable active specifications and pending approvals; establish dependencies or a
  safe follow-up/new task instead of silently rewriting the running task.
- Identify what Dan supplies versus what the agent investigates, and how he requests,
  prioritizes, approves, rejects or changes the scope. Ask only material unresolved
  questions; batch any approval that changes scope/architecture/external effects.
- Investigate the unavailable-version example only enough to ground the intake and
  acceptance checks. Do not silently change the live playlist. Queue its actual repair
  through the resulting mechanism if authorized.

Success: a concrete repeatable operator procedure, integrated with existing Maestro
controls; the reported unavailable-song issue is captured as an actionable normal task,
not just a chat note. Any implementation uses meaningful end-to-end intake/queue tests
and appropriate full-suite verification. Do not claim this mechanism exists before proof.

## 2. Compare the built modelctl tool with Maestro's agreed requirements

Dan reports the model-adding tool is finished at **/home/dan/projects/modelctl**.
Read that repository's instructions, implementation, docs, tests and actual state before
judging it. Compare it with `handoffs/2026-09-29-local-model-switch-pending-codex.md`,
`docs/graph-engineering/STATE.yaml`'s model-catalog/shared-default thread, and actual
Maestro consumers. No inspection or completeness claim was made in the closing session.

Required comparison dimensions:

- Argument-free local interaction: list models currently in use, select one, discover
  fresh available models across supported harnesses, select replacement; explicit
  unsupported/quota/discovery errors rather than stale-cache certainty.
- Context-limit table lookup and missing-row handling using the existing unknown-model
  fallback policy; tell Dan to customize defaults; respect modelctl's ownership of tables.
- Complete persistent configuration/routing updates, including cross-harness changes,
  validation, rollback, and a clear completion/delay report.
- **Maestro shared defaults**, as Dan chose: update inheriting projects while preserving
  deliberate project overrides; identify/report those overrides and active-run delays.
  No mid-task adoption or edits to immutable running versions.
- Actual local disposable-project behavior, doctor/model-limit checks, and meaningful
  tests, rather than relying only on tool documentation or reported completion.
- Reusability for a later Telegram selection flow; Telegram model-switch UI remains
  outside the original task unless newly requested.

Output: concise requirement coverage/gaps with repository evidence, and recommended
remaining work. modelctl need not become part of the Maestro project; reuse its existing
capabilities and recommend the smallest needed integration rather than reimplementing it.
Do not modify global/shared settings, explicit project overrides, or the other agent's
uncommitted changes merely to make the comparison pass. Use Context7 skill for unresolved
current library/CLI/API questions, per AGENTS.md.

## Dependencies and current limits

DuetFlow is accepted and HALTed at an idle boundary on installed Maestro ead3819;
no next task has started. Genuine Telegram /ask delivery remains unproven. Current doctor
warnings include context-limit entries for explicit claude-sonnet-5 and gpt-6-sol overrides;
this is concrete integration evidence for the modelctl comparison, not permission to
silently rewrite them. Preserve the queued duet_history follow-up and D30/D31 evidence
requirements. Context Gate and broad backend permission redesign stay out of scope.

## Next actions

Start a fresh session. Read the installed-boundary handoff, gather bounded evidence for
these two requests, then recommend/implement the authorized intake path and modelctl
integration gaps. Record any unresolved material decisions once, and leave the pilot
HALTed until its continuation is agreed. Verify future /ask on a genuine waiting request.

## Session update and clarified scope (2026-09-30)

Dan chose **Telegram intake: submit a report to the project bot; Maestro prepares a
scoped task for approval**. This authorizes implementation of the intake feature;
no additional staged design approval is needed. He explicitly corrected the scope:
**improve Maestro through the pilot, do not resolve DuetFlow's product issues here**.
The song example is preserved in docs/intake/2026-09-30-duetflow-unavailable-song-versions.md.
Both supplied URLs use track ID 13HD3tYTiz8oSRxWzXEq7g; only si differs. Reported
behavior: all Ella Langley songs greyed/un-clickable in Duet for Dan, playable from
search or artist page. Root cause remains unverified; no replacement assumption.

Core intake implementation is in maestro/intake.py and the Telegram capture routes
in maestro/hitl/commands.py, with tests/test_operator_intake.py. Core 16 tests passed
fresh; controller/tick integration, fuller validation/recovery review, full-suite
verification and live adoption remain. Current operator procedure/status is in
**docs/OPERATOR_INTAKE.md**. No live DuetFlow report/task was queued.

Modelctl comparison completed: **docs/reviews/2026-09-30-modelctl-maestro-coverage.md**.
130 modelctl tests passed; fresh provider metadata discovery and two disposable
public add flows checked. Main Maestro shared-default switch/propagation requirement
is missing. Recommended ownership: provider discovery/registration/tables stay in
modelctl; effective-model selection, routing, project overrides and safe propagation
belong to a small Maestro-owned integration layer. No third model manager. This is a
recommendation; implementing that integration was not requested/approved this turn.
Secondary transaction/access-label defects reproduced and recorded there. Its timer
is already enabled and active; the old install instruction is stale.

Next agent: **handoffs/2026-09-30-telegram-intake-core-modelctl-reviewed.md**.
Finish the approved Telegram intake feature, verify the complete normal workflow,
then arrange safe rollout/delivery evidence without resuming DuetFlow work. Present
any material model-integration design decision together before implementing it.
