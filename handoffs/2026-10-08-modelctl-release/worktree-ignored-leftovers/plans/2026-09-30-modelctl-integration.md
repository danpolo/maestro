# Maestro shared models: integration proposal

Status: **D-A–D-E below are accepted and pending replanning/implementation.** Session 7's
earlier registered-replacement candidate passed independent review and its corrected
gate (5433 passed, 1 xfailed, exit 0); that evidence qualifies the earlier design only,
not the newly accepted automatic class-transition behavior. The current next-session
scope and carry-forward checklist are in `handoffs/2026-10-01-modelctl-session7-next.md`.
Historical trigger semantics were detection → replacement proposal for approval;
the D-B/D-D decisions below supersede that approval requirement.
No live switch, code deployment, pilot continuation or paid inference was authorized.

Execution worktree:
`/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`, branch
`feat/modelctl-integration`, base `a40cafc44ea00051a1e609833d15fd710c61cf30`.
Implementation and tests are uncommitted. Read the current execution handoff:
`handoffs/2026-10-01-modelctl-session7-next.md` (which builds on sessions 2–6 and
`handoffs/2026-09-30-modelctl-implementation-milestone-next.md`).
Evidence and ledger are under the worktree's
`.superpowers/sdd/2026-09-30-modelctl-integration/`.

Verified slices include immutable policy/CAS, project override provenance,
registered disposable same/cross-harness switching, conservative event-backed idle
adoption, task/run binding and refusal, graph role preference ranking, provider-table
reads, retirement/rollback, future-init inheritance, durable proposal consumption and
send-to-me notices. The final independent review identified four correctness issues;
all have test-first fixes. A genuine disposable interactive picker run is recorded.
The full feature remains unfinished: missing-row registration stays disabled pending
owner transaction/retention hardening and dependency-inventory contract proof. Native
Astra capacity still needs owner-verified data. Consult the handoff for exact evidence,
gate status and remaining work. A parallel session owns the DuetFlow pilot; its mutable
records are outside this candidate's preservation baseline.
The modelctl outbox candidate passed 135 tests; Dan explicitly approved its five-file
patch, which was applied with the staged index unchanged. The applied owner suite
also passed 135 tests. No code deployment or live model switch occurred.

## Current execution — 2026-10-07

Session 8 began D-A–D-E in the existing worktree; it is **not acceptance-complete**.
Bounded evidence: 271 Maestro tests and 146 isolated owner tests pass (exit 0).
The owner candidate is archived and remains unapplied; no live behavior changed.
Revised executable plan: `docs/plans/2026-10-07-modelctl-class-transitions.md`.
Continue from `handoffs/2026-10-07-modelctl-session8-next.md`, which records the exact
contract/recovery gaps before independent review, the new full gate and one owner
patch-application approval. Session 7's gate remains historical baseline evidence.

Session 9 expanded the owner/consumer contract and recovery coverage: 1374 related
Maestro tests and 160 isolated owner tests pass, exit 0. Candidate remains unreviewed
and incomplete; no full D-A–D-E gate, owner application or live transition occurred.
Continue from `handoffs/2026-10-07-modelctl-session9-next.md` for controller/pruning
race qualification, total notice behavior, reporting, missing-row workflow and review.

Session 10 controller milestone: 1802 related Maestro tests and 170 isolated owner
tests pass, exit 0, with 335 frozen inputs unchanged. Task reservations/choices,
controller restart/retry, real owner subprocess inventory and combined switch notices
are covered. The revised feature remains incomplete and unreviewed; no full gate or
owner application. Current next-session scope:
`handoffs/2026-10-07-modelctl-session10-next.md`.

## Dan's decision changes, 2026-10-01 (they supersede conflicting text below)

Dan changed the model-transition design on 2026-10-01. Where any section below or any
handoff disagrees, this section wins. Re-plan the remaining work around it before running the
item 11 final review/gate, because a gate on the superseded design proves the wrong thing.

**D-A. Pins are class-level, never version-level.**
- A model choice in `project.yaml` roles (or a task) names a *class* (family): Opus, Sonnet,
  Sol, Luna and so on. It always resolves to that class's current model.
  - `sonnet-5` means "Sonnet": when Sonnet 5.5 is set, the project switches automatically.
  - A project that chose Opus where the shared policy says Sonnet keeps Opus and follows
    new Opus versions.
- Class comes from modelctl metadata (`family`), never a second name parser.
- This supersedes "explicit overrides remain explicit and are never rewritten" for
  versions only: the *class* choice is preserved, the version is not.

**D-B. A switch happens exactly when a model has context limits (modelctl).**
- New model in an *existing* class:
  - modelctl registers it automatically with its predecessor's limits rows (lifecycle
    thresholds and enforced window);
  - modelctl notifies Dan to review or adjust;
  - it is live immediately, before Dan responds.
- New model in a *new* class: modelctl waits. Until Dan enters its limits, Maestro behaves
  as if it does not exist.
- This supersedes "detection creates a replacement proposal for approval; never
  auto-activates" and "unknown limits require explicit confirmation", for existing classes.
- modelctl's availability smoke and the item 8 transaction hardening still apply
  (state, smoke and rollback inside one boundary).

**D-C. Rows are added, never replaced, until every Maestro instance has moved.**
- modelctl adds the new model's row beside the old one.
- The old row is deleted only when two things are true:
  - every registered project has adopted a policy that no longer references the old model;
  - no frozen task or run still uses it.
- Maestro supplies that dependency inventory (item 8 contract, now required). modelctl
  never infers orchestration state.
- This replaces modelctl's current "only the current model of each tier has a row".

**D-D. Maestro pushes a newly set model to every project automatically.**
- When modelctl reports a model as set (it has limits), Maestro publishes one shared
  revision without an approval step:
  - an existing-class model replaces its predecessor in routes and every class
    reference;
  - a new-class model is added as an eligible route, and role preferences are unchanged
    unless Dan sets them.
- Each registered project adopts at its closest checkpoint, **between tasks**. This is
  today's idle-boundary rule: a running task keeps its frozen model, including its retries.
- Dan gets one notice per switch, through the existing send-to-me transport.

**D-E. New-model routing profile inherits from its predecessor.**
- An existing-class model with no shipped verified native profile takes its predecessor's
  Maestro profile on the *same backend*:
  - strength, relative cost and effort vocabulary are copied;
  - the native window is capped by modelctl's enforced window.
- No Maestro release is needed, and Dan's limits notice also covers reviewing this profile.
- Copying a profile across backends remains forbidden.
- This supersedes "never clone the old model's grade/window" for same-backend, same-class
  successors only.

**Still unchanged:**
- Adoption is between tasks.
- Projects on releases without the reader report `upgrade_required` until they adopt a
  release that has it.
- No Context Gate work.

**DuetFlow:** its pins stay as set on 2026-10-01 (Sonnet 5.5 / Opus 5.5 / GPT-6.1 Sol /
GPT-6 Luna) while it runs f13b2c7. After it adopts a release with D-A, they act as class
choices.

**Ownership:**
- Maestro (this branch) implements D-A, D-D, D-E and the inventory side of D-C.
- modelctl (external owner; Dan has asked for these features) implements D-B and the row
  lifecycle of D-C.
- Apply modelctl changes with the same reviewed-patch discipline as the outbox patch, and
  ask Dan once for application approval.

## Detection-trigger integration requirement

Source inspection confirms modelctl returns `NEW_MODEL_DISCOVERED` and
`MODEL_NOW_AVAILABLE` events from `core/scan.py`, with per-model persistent state in
`core/state.py`. Detection is separate from registration and selection. Use these
existing events as the entry point; do not create another Maestro discovery timer or
require Dan to manually launch the picker as the only entry point.

The current `last_scan.events` is overwritten on every scan and stores rendered text;
it is not a reliable event outbox. Configured new rows take an early branch before
`NEW_MODEL_DISCOVERED` is emitted. The integration needs a durable, acknowledged,
idempotent event handoff (including models newly detected already configured), with
replay and notification/consumer failure proof. Settle this seam explicitly with
modelctl's owner; do not silently change the staged external checkout. Persist one
pending Maestro workflow per detection identity, retain it across retries and scans,
and never equate discovery/configuration with verified account inference.

Implement the approved policy/routing layer independently of this remaining trigger
answer; detection-trigger delivery remains unfinished until the external owner patch and
installed consumer seam are verified; proposal semantics are now approved. The full feature cannot be marked
complete merely because an argument-free local picker works.

## Outcome and already approved decisions

Provide `maestro models` with no required arguments: choose a current shared
backend/model binding, discover replacements, preview affected projects, then
apply an explicitly confirmed change. Reuse the same use case later for Telegram.
Dan already chose shared defaults: inheritors change; explicit project/task
overrides remain explicit. modelctl owns discovery, registration and provider
context tables. Maestro owns routing and adoption. Context Gate, pilot continuation,
Telegram selection, Antigravity adapter development and paid discovery probes are
outside this proposal. Preserve the five model-owner edits; do not force ignored
plans/handoffs into Git.

## Approved combined decision

Dan approved the following contract together; implement without staged re-approvals:

1. **One shared routing policy**, versioned under `~/.maestro/models/`, containing
   eligible backend/model pairs, strength, relative cost and role preferences.
   Lifecycle numbers remain solely authoritative in modelctl's provider tables.
   Bootstrap from the approved executing source defaults; retain source provenance
   and avoid treating pending owner edits as an adopted release.
2. **Roles apply to both execution paths.** Legacy `roles:` remains supported;
   graph reads explicit project role entries as preferred routes after hard
   constraints, ahead of ordinary objective ranking. `implementer` maps to the
   graph implementer, `judge` to reviewer/proof review, and `diagnoser` to diagnoser.
   Graph planner/test designer keep their existing capability-based selection
   unless given a new explicit preference. Keep graph model allowlists and task
   constraints hard; a preference never overrides capability, context, billing,
   permission, availability or quota checks. Explain an ineligible preference and
   choose the next eligible route. Explicit literal task pins that cannot be served
   are refused rather than reinterpreted across vendors.
3. **Unknown limits require explicit confirmation.** Show the selected provider's
   actual `Model unknown` values as suggested values, with a customization notice;
   Dan accepts or edits them before modelctl registration. This reconciles the
   earlier fallback-row request with modelctl's no-unapproved-activation rule.
   No usable fallback row or unsafe window means refusal before routing writes.
   Registration remains a separate provider transaction; a successful registration
   followed by a failed routing change is reported truthfully as registered but
   not selected. It is not an all-or-nothing cross-tool transaction.

The external owner must harden modelctl registration before the automatic missing-row
path is enabled: include smoke exceptions and state recording in its rollback
boundary, snapshot state as well as planned provider/cache files, and refuse removal
of rows/windows still referenced by projects or active runs. Maestro must provide the
dependency inventory; modelctl must not infer orchestration state. Existing registered
replacements can be implemented and proven independently. Do not silently patch the
owner's staged checkout. False `VERIFIED` scan labels are also an owner finding;
Maestro must not use them as proof of inference. Missing registration remains visibly
unavailable until owner changes and contract checks are verified, never counted done.

## Effective resolution and replacement

Identify models by `(backend_id, model_id)`, not model text alone. Precedence:
immutable task/attempt binding and deliberate task constraints; project configuration
and operator choice; adopted shared policy revision; shipped defaults when no shared
policy exists. Track provenance independently of equality with a default. A project
with explicit `models:` entries is overridden even when its model matches the shared
model. Do not infer old template entries were inherited.

A switch removes the selected old pair from shared eligibility, adds the replacement,
and updates only shared role preferences/defaults referencing that pair. It transfers
a legacy preferred backend when that role's selected shared binding moves harnesses.
Other fallback routes stay available; do not force every graph node onto one model.
Validate the destination against its own verified backend capabilities and effort
mapping; never copy another backend's pool, permission grade, physical context window
or native effort vocabulary. Known strength/cost may be reused from an existing
destination profile. A genuinely new profile requires explicit strength/cost values
in the same switch preview; do not silently assign the old model's grade.

Explicit old project models stay available only while actually supported and valid.
Retired routes are excluded, reported and cannot be resurrected by a project
preference. A metadata failure is unknown availability, not retirement; it cannot
publish a replacement or mass retirement decision. Latest-per-family filtering uses
modelctl metadata, not a second provider parser. Antigravity routes remain visible
and retain existing verified metadata, but the picker reports discovery/registration
unsupported until its modelctl adapter exists.

## Adoption, inventory and transaction boundary

Use immutable shared revisions plus a single atomic current pointer; never rewrite
immutable installed source. Each supported project records its adopted policy revision
under `.orchestrator/`. Adopt under its existing controller lock at the boundary before
a new task starts. Freeze the revision across the complete task, retries, bounded
calls and controller recovery. Graph catalog rebuilding and legacy role reads must
use that same task revision; a restart is not permission to change it mid-task.

Running/paused graph runs, pending approvals, claimed/pending/running attempts, live
writer leases and legacy in-flight jobs defer adoption. Unreadable state or uncertain
activity defers it too. HALT remains unchanged. Idle stopped projects can adopt only
through the same lock/idle proof. No daemon or controller restart to force adoption.
Installed versions without the reader report **upgrade required**, not updated.
Use existing code-version adoption separately at its approved idle boundary.

Maintain an explicit project registry under the shared policy home. Initially preview
immediate `/home/dan/projects` candidates and require validation of project identity;
init/register adds future or outside-root projects. Do not claim an arbitrary directory
scan covers every host project. Exclude Maestro's own evidence `.orchestrator` unless
it becomes an explicitly registered operating project.

Before publishing, check policy revision and configuration hashes against the preview.
Validate schema, limits, destination support and effective bindings on scratch state.
Publish one shared revision transactionally. Each project adopts independently;
report updated, deferred, override-preserved, invalid and upgrade-required outcomes.
Crash/retry reconciles the actual adopted revision; no fabricated atomic host-wide
success. Rollback publishes a validated prior policy as a new revision and follows
the same idle-boundary rule. Active tasks keep their existing revision.

## Concrete current preview (read-only)

Evidence: `handoffs/2026-09-30-modelctl-integration-preview/inventory.json` and
its reproducible `inventory.py`. Inspection covered immediate children of
`/home/dan/projects`, not the whole host. Pinned sources were imported in isolated
read-only subprocesses; no cache resolver, discovery, inference or doctor ran.

- **AbuAliArchive:** legacy; no role/fallback overrides. Effective source is global
  `2f555c28bd7bf0142979ee130c63e5b422a4c690`, which predates the graph catalog.
  Effective implementer/judge are Claude Sonnet 5; diagnoser is Claude Opus 5.
  Codex fallback models are unspecified. Shared-default inheritance candidate;
  upgrade required. State says P10 in progress; zero in-flight records alone does
  not prove an idle boundary. Adoption would be deferred pending full idle proof.
- **DuetFlow:** graph; pin `f13b2c7eb8498edf39c1e72e439131e0635b024a`, HALTed,
  idle projection; database has one succeeded run, 10 succeeded/4 failed attempts,
  no active status rows. Explicit implementer: Claude Sonnet 5 / GPT-6 Luna;
  judge and diagnoser: Claude Opus 5.5 / GPT-6 Sol. These remain explicit.
  Installed graph catalog contains GPT-6 Sol and GPT-5.6 Luna, whereas pending
  source edits contain newer identifiers. This is real version drift, not adoption.
  No project graph role-model allowlists found; current graph ignores `roles:`.
  On a future integration upgrade its explicit entries become graph preferences
  under the proposed contract; missing/retired preferences must be reported.
  No upgrade/switch/resume is authorized by this preview.
- **Maestro checkout:** `.orchestrator` evidence directory; no project.yaml or
  operating state.json. Exclude from managed-project adoption inventory.

No concrete replacement has been selected, so these are provenance/adoption
classifications, not a count of projects already switched. For example, replacing
a shared Sonnet 5 route would affect an upgraded inheriting AbuAliArchive; DuetFlow's
explicit Sonnet 5 entry would be preserved/reported. Graph's current shared catalog
already contains Sonnet 5.5, so show the legacy and graph bindings separately in
the picker rather than pretending there is one universal current model.

## Implementation sequence and verification

1. After the combined decision, isolate Maestro implementation from pending owner
   edits. Record their hashes and the modelctl source/API fingerprint. Add policy
   parsing, provenance and read-only project preview in `maestro/model_policy.py`
   and `maestro/model_inventory.py`; CLI wiring in `maestro/cli.py`. Use public
   CLI/integration tests that first fail for absent shared replacement behavior.
2. Add a bounded subprocess bridge (`maestro/modelctl_bridge.py`) consuming the
   external checkout's `Env`, `ClaudeProvider`/`CodexProvider.discover` and
   `latest_per_family`. Configure its source root explicitly; fail clearly for
   missing/incompatible code. No import-path pollution, credential output, scan
   side effects, default paid inference, daemon restart or catalog ownership copy.
3. Wire shared policy through `roles.py`, `agentcall.py`, `implementer.py`,
   `selfheal/diagnose.py`, `orchestrator._graph_routing`, `workflows/routing.py`
   and the router's qualified preference ranking. Check all bounded-call defaults.
   Extend `Paths` and task/run records only as required to freeze/recover revisions.
   Keep the single dispatch/fresh-versus-resume seam and nullable telemetry intact.
4. Implement safe per-project adoption/retry and transactional policy publication.
   Update future init templates to inherit explicitly without altering existing
   project files. Doctor/status report real effective pairs, provenance, invalid
   models and deferred/unsupported-version adoption, including graph catalog IDs.
5. Prove registered same-harness and cross-harness replacement on disposable
   inheriting, overridden, active and older-version projects. Exercise graph
   preference eligibility/fallback, malformed config, stale preview, unavailable
   provider, duplicated IDs across harnesses, retirement, crash/recovery, task
   revision preservation, rollback and lock contention. Run the focused suite,
   independent review and full candidate gate with required ignored docs present.
6. Enable missing-row registration only after modelctl owner fixes and source
   contract checks: accepted fallback values, customized values, missing fallback,
   unsafe native window, dependent-row retention, state-save/smoke failures, quota
   inconclusive versus explicit unsupported outcomes. Prove provider registration
   versus selection partial-success reporting. If those dependencies remain open,
   report that path unfinished and give the exact owner follow-up.
7. Run a real interactive local command on disposable projects and retain selected
   backend/model, policy revision, resolver/doctor evidence, actual graph binding,
   project adoption outcomes, exact test counts and exit statuses. Re-run preservation
   checks before claiming completion. Live rollout and pilot continuation remain
   separately scoped; do not repoint the global installed version here.

Human review copy: `/tmp/maestro-modelctl-integration.html`, ephemeral outside Git,
delivered to Dan's private inbox after browser verification and explicitly approved.
The private delivered copy is the human-facing record; the Markdown is durable agent
context. Next execution entry point:
`handoffs/2026-09-30-modelctl-approved-implementation-next.md`.

Session 11 bounded legacy/pruning/reporting milestone: 4274 expanded related Maestro tests
pass, exit 0; corrected full isolated owner suite 181 pass, no skips, exit 0. Both
335-input freezes unchanged during their runs. Legacy record/launch and detached task
calls preserve frozen choices; real pruning interleavings, shipped profile edits,
recovery/shape/identity guards, waiting-limits/class/switch reporting and owner fallback
limits are covered. Registration requirement mapping/documentation, remaining launch
interleavings/shape qualification, independent review, owner application package and
full acceptance gate remain pending. Candidate remains unreviewed and unapplied.
Next: `handoffs/2026-10-07-modelctl-session11-next.md`.
