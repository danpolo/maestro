# Telegram operator intake

Maestro captures bug reports, changes and feature requests, prepares a scoped proposal,
and publishes only the exact revision the operator approves. Publication enters the
existing ROADMAP and controller workflow: implementation, declared checks, proof review,
Dan verification when specified, merge, acceptance and graduation. Intake has no task runner.

Implementation, independent review, the final exact-version gate and pin-only rollout
are verified. DuetFlow is pinned to f13b2c7eb8498edf39c1e72e439131e0635b024a and remains
HALTed. The candidate passed 5297 tests with 1 expected failure; pinned doctor exited 0.
Live receipt was confirmed for I-211702189. The first proposal attempt failed safely
on a comment-only ROADMAP example; that Maestro compatibility fix is now reviewed,
gated and installed without editing DuetFlow. Live retry, revision-1 proposal delivery
and operator rejection are verified. No task was published or started; original report
and proposal evidence are retained. The temporary capture-only listener was stopped
after the smoke; no ongoing controller/watchdog is running. The stale old-code watchdog
was also stopped. DuetFlow continuation remains a separate operator decision.

## Operator commands

- `/report bug <description>`, `/report change <description>` or
  `/report feature <description>` saves the original text and returns its actual `I-…`
  report ID. Include expected/actual behavior, the desired result and an example.
- `/reports` lists reports and progress. `/intake <report-id>` shows the original report,
  errors and full proposed task, including scope, constraints, dependencies and checks.
- `/intake approve <report-id> <revision>` approves the displayed revision. Material
  unanswered questions block approval. Copy the actual command from the proposal.
- `/intake amend <report-id> <message>` changes scope/priority or supplies answers;
  the old approval becomes invalid and a fresh proposal receives a new revision.
- `/intake reject <report-id> <reason>` rejects unpublished work.
- `/intake retry <report-id>` retries failed proposal preparation. Network/quota/model
  failure preserves the report and does not produce a guessed task.

These angle-bracket forms describe the interface; the receipt and proposal supply
copy-ready commands with real IDs and revisions. Once publication starts, submit a new
report for additional scope. Intake approval never answers a task's pending verification.
Use the existing `/verify` flow when normal implementation later needs Dan's observation.

## Preparation, publication and pause

An active graph or legacy controller prepares at most one new proposal at each control
poll. A paused startup controller can prepare without resuming task execution. Receiving
a report in the halted watchdog only captures it: no paid model call or worker starts.

While deliberately halted, an operator can request one bounded preparation tick:

```bash
maestro intake tick --repo /absolute/project/path --telegram
```

Expected output: the scoped proposal and its actual approval/amend/reject commands,
or a durable preparation error with the actual retry command. `--telegram` also delivers
through the configured project bot; failed delivery remains pending for a later tick.
Omit `--telegram` for terminal delivery only. A tick may make one configured judge call,
bounded to 60 seconds, and starts no task. It preserves HALT and paused state.

Approved additions publish automatically under the existing controller lock only when
there are no active legacy jobs, pending task approvals, running/paused graph runs, or
pending/claimed/running attempts. For publication while deliberately halted:

```bash
maestro intake tick --repo /absolute/project/path --publish --telegram
```

Expected output: `queued as a normal ROADMAP task` for approved work, or a clear deferral.
Exit 3 means publication remains deferred (for example, another controller holds the
lock). This requires an existing controller database; an unmigrated legacy controller
initializes the normal store at an idle approved-publication boundary. Publication does
not clear HALT, resume tasks, or change any existing task specification.

Receipts/proposals/progress are durable before delivery. A later processor tick retries
unconfirmed delivery without regenerating the proposal. Delivery is at least once: a
crash after Telegram receives a message but before the local acknowledgement is persisted
can duplicate that message. Report and action replays preserve the scope and revision.

## Progress and recovery

A queued report is not resolved. Reports follow normal task state: queued, running,
paused/waiting/blocked, failed/canceled, accepted, then graduated when the completion
registry records it. `/workflow <report-id>` and `/explain <report-id>` show normal task
execution and evidence. Reviewer follow-ups continue through the existing mechanism.

Per-report JSON and locks live in `.orchestrator/intake/`. Original reports and amendments
remain intact. Corrupt records are identified and isolated, allowing later reports to
proceed; preserve the corrupt file for recovery. Failed preparation requires explicit
retry. Failed notifications retry automatically on processor ticks.

Task publication uses the existing ROADMAP compare-and-swap path. It refuses existing
IDs, unapplied operator edits, or conflicts with the stored task. A durable publication
intent supports retry after partial ROADMAP/database/report persistence. If an operator
edit happened before any addition, use the normal ROADMAP review/adoption procedure;
intake then revalidates and rebases the same approved scope. Conflicting edits after a
partial addition require operator investigation rather than adopting another task.

Proposal validation enforces relative project scope (including existing symlink paths),
known dependencies, explicit acceptance criteria, automatic commands and actionable Dan
steps. The configured model's `writable=False` request remains advisory for unsandboxed
backends; intake does not claim stronger process confinement.

## Pilot and model ownership boundary

Dan's DuetFlow playback example is recorded in
`docs/intake/2026-09-30-duetflow-unavailable-song-versions.md`. Both supplied URLs identify
track `13HD3tYTiz8oSRxWzXEq7g`; the root cause and repair policy remain unverified. No live
report, bug task, playlist edit, product worker or continuation is authorized by this
feature's implementation. Keep DuetFlow HALTed until Dan agrees to continuation.
The 07 history follow-up and D30/D31/D33 evidence requirements remain intact.

Preserve the recommendation in `docs/reviews/2026-09-30-modelctl-maestro-coverage.md`:
model discovery, registration and context tables remain owned by external modelctl;
effective Maestro defaults, routing, override provenance and safe cross-project
propagation belong to a small Maestro-owned integration layer. That integration and
a Telegram model-selection UI remain outside this feature. Context Gate is not built.
