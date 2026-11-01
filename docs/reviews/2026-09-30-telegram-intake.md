# Maestro Telegram intake review and verification

Final runtime candidate: `f13b2c7eb8498edf39c1e72e439131e0635b024a` (comment-only ROADMAP compatibility).
Previous f46ffd4 (legacy progress) passed and was installed before the live finding.
Base intake candidate `9b4585683eb21d6dda0a12f4206254a8826828cf` passed its full gate.
Scope: durable bug/change/feature report capture, bounded scoped proposal preparation,
exact revision approval, safe new-task publication, and progress through the existing
engineering workflow. Modelctl integration, Context Gate and DuetFlow product repair
or continuation are excluded.

## Independent code review

Read-only reviewer inspected the new intake/tests and CLI, command, notifier,
controller and ROADMAP diffs against `c711dd2`. Final verdict: **ready to merge within
the reviewed scope; no remaining Important or Critical findings**. Final reviewer
checks: 52 core/integration tests passed, 4.93 s, exit 0; diff whitespace check passed.

Important findings fixed with failing then passing regressions:

- A pre-addition CAS failure retained an unusable publication intent. Recovery now
  requires the addition to be absent from ROADMAP, completion registry and store,
  and the operator's changed baseline to have been adopted. The unchanged approved
  task is revalidated before rebasing.
- Corrupt root/optional/nested report fields could starve later reports or crash
  detail commands. Consumed field types are checked before membership/chunking;
  errors isolate the record and preserve it for recovery.
- Failed capture receipts were discarded. Receipts persist before delivery and
  redeliver without model calls or workers. Replaying a receipt also preserves any
  undelivered proposal. Conflicting reuse of an update ID is explicitly refused.

Minor optional error-field validation was also fixed and regression-tested.

Reviewer declined to judge unrelated preexisting changes, parent-owned full gates,
live rollout/bot delivery, DuetFlow execution and modelctl implementation. Those
boundaries are explicitly retained rather than silently counted as verified.

## Automated verification

- Final focused paths: **484 passed**, 14.41 s, exit 0.
  `/tmp/maestro-intake-final-focused.log` and `.exit`.
- Source full suite: **5281 passed, 1 xfailed**, 406.33 s, exit 0.
  `/tmp/maestro-intake-full.log` and `.exit`. Final regressions were added during
  this source run, so this alone is not the final candidate gate.
- Base immutable candidate: **5290 passed, 1 xfailed**, 390.99 s, exit 0, after
  supplying 27 ignored specs/STATE test inputs byte-identically, as the prior release
  procedure documents. Its initial missing-input failure (5 failed, 5284 passed,
  1 skipped, 1 xfailed) is preserved in the release support folder. The phase-prompt
  checks passed 13/13 after staging those inputs.
- Final exact candidate (f46ffd4): **5291 passed, 1 xfailed**, 389.79 s, exit 0.
  `handoffs/2026-09-30-operator-intake-release/candidate-selftest.json` records
  passed:true, the exact SHA and immutable worktree. Candidate integrity check verified
  clean HEAD and all 27 unchanged document hashes before pin-only adoption.
- Core/public integration: durable transport receipts, approval/replay/stale revision,
  model/notification failure, scope/symlink/verification validation, locked/active-work
  deferral, collision/drift/CAS and partial persistence recovery, and progress.
  A git-backed disposable report traverses normal implementation, declared validation,
  proof review, durable acceptance, merge and graduation. Paused/legacy lanes are covered.
- Rollout-boundary rehearsal: idle accepted fixture permits adoption; active run,
  pending attempt, live writer and missing HALT all refuse. This rehearsal executes
  no adoption. Installer/listener source compiled successfully.

## Preservation and rollout evidence

Eight preexisting model-owner/evidence files were hash-checked unchanged, including
the modelctl ownership recommendation. Before rollout DuetFlow has HALT, pin ead3819,
zero active runs/attempts or in-flight work, accepted 07, and zero intake files.
Its history follow-up and D30/D31/D33 obligations are unchanged.

Read-only Bot API identity: **@MaestroGenericTestingBot**, bot ID 8764245904,
no webhook. No updates were fetched and no messages sent by that identity check.

Pin-only adoption uses the existing selfupdate path under the orchestration and
controller locks. It verifies the exact candidate gate and old pin, preserves HALT
and the global pointer, checks pinned doctor, and rolls the pin/version back on failure.
A separate ten-minute intake-only listener permits a genuine operator smoke test
without a watchdog, controller, model preparation, task publication or product worker.
The explicit intake tick may prepare one proposal when requested; smoke scope must
be rejected and never approved/implemented.

Release scripts and evidence are kept under
`handoffs/2026-09-30-operator-intake-release/` using the repository's ignored handoff
convention. Installation and actual bot receipt/proposal/rejection observations are
reported separately; automatic checks do not constitute real Telegram delivery proof.

Legacy progress also follows the authoritative existing state document through a
client connection when there is no graph run. The final listener rehearsal trapped
controller opening, model generation, publication and worker launch; receipt/list/detail/
rejection and foreign-chat refusal passed with zero tasks, journal events and leases.
Pin-only installation and temporary listener startup are now verified; actual operator
receipt/proposal/rejection proof remains pending.

## Final gate and installation (2026-09-30 continuation)

Installation adopted only DuetFlow's pin to f46ffd4; pinned doctor exited 0 and HALT
remained set. Global pointer and all eight preserved model-owner/evidence files are
unchanged. ROADMAP/project/HALT/dependency files and task/run/attempt/acceptance records
match the pre-install baselines. Product state is unchanged apart from the installation
metadata: maestro_version, updated_at and control_sequence. The initial preservation
check excluded only version and therefore failed; the exact pre-install document was
recovered from event 249 by its saved hash, proving those three differences only.
The verification script now accounts explicitly for that established metadata delta.

Doctor retains existing warnings for explicit model context rows and missing systemd
unit. Its meta-worktree check attempted a push and warned that origin is unavailable;
no remote push succeeded. These warnings were not repaired. Capture-only listener
readiness was verified after restarting it; the first listener was stopped while
the state delta was investigated, before any operator message was requested.
Evidence: installation.json, pinned-doctor.log, candidate-integrity.json,
state-install-delta.json, live-before.json and preservation-latest.json in release support.

## Live report exposed compatibility defects

Dan sent the exact smoke report as Telegram update 211702189. The first temporary
listener had reached its ten-minute bound before that message; a preexisting host-side
DuetFlow watchdog still loaded from 9a0ea5a queued the command using the old handler.
Host /proc inspection established its exact repo/argv/PYTHONPATH and PID 1367429.
Only that stale halted watchdog received SIGTERM, and its exit was verified. It was
not restarted. The installed capture-only listener then received the same genuine
Telegram update, persisted I-211702189 and confirmed receipt delivery. No invented
update or report was submitted. Its original command-inbox record is retained.

One configured, bounded intake tick ran (exit 0), but durable report status is
needs_retry, revision 0: proposal validation rejected the existing comment-only
ROADMAP example as empty_block. This is failed live proposal proof, despite CLI exit 0.
Error/raw report evidence is preserved in live-tick.log and live-report-first-failed.json.
No model override, guessed proposal, approval, publication or product task occurred.

Maestro correction f13b2c7eb8498edf39c1e72e439131e0635b024a ignores only nonempty
comment-only YAML fences. Two regressions failed on the actual empty_block error,
then passed; empty/whitespace/null/document-marker bodies remain refused. Focused
taskgraph/intake/public integration/legacy characterization checks passed. Actual
unchanged pilot ROADMAP validates with this correction. Full immutable gate, independent
review and pin-only follow-up installation are pending in comment-example-fix/.
The live listener was stopped while those gates run. DuetFlow remains unchanged/HALTed.

Compatibility fix independent review: ready to merge, no Critical/Important/Minor
findings. Reviewer verified 435 focused taskgraph/intake/integration/characterization
tests plus 14 additional refusal probes, and clean whitespace. Full candidate gate,
adoption and live proof remain parent-owned. Reviewer excluded unrelated model edits,
operator documentation, DuetFlow repair/resume, modelctl and Context Gate.

## Compatibility candidate gated and installed

Exact f13b2c7 immutable gate: **5297 passed, 1 xfailed**, 390.58 s, exit 0.
Clean candidate HEAD, 27 unchanged document hashes and release-script syntax verified.
Pin-only follow-up adoption succeeded; pinned doctor exit 0, HALT retained. Original
product and model-owner baselines still pass, including global pointer and state
except the three established installation metadata fields. No DuetFlow controller
or watchdog was running before corrected capture-only listener startup. Readiness
verified and actual `/intake retry I-211702189` given to Dan. Retry/proposal/rejection
proof remains pending; no additional configured model call has run yet.
Evidence is in release support's comment-example-fix/ directory.

## Live Telegram sequence completed

Actual operator retry update 211702190 returned I-211702189: captured; Dan supplied
the actual bot reply. One additional configured installed tick (no override/publication)
exited 0 and prepared revision 1, awaiting_approval. Its full proposal and two material
questions persisted; Telegram confirmed all four delivery chunks. Those unresolved
questions would block approval. No approval was requested or given.

Actual operator rejection update 211702191 persisted rejected with reason Smoke complete;
Dan supplied the actual I-211702189: rejected reply. Raw report text equals the original
captured text; revision/proposal evidence retained. No approved_at or publication intent.
Final baseline checks pass: no new task/run/attempt, unchanged ROADMAP/config/HALT,
product state except installation metadata, global pointer and model-owner files.
The original old-handler inbox record remains preserved; this release does not drain
or resume the general command queue. The smoke report is durably rejected, so later
idempotent recapture cannot approve or implement it.

The temporary listener received its graceful stop request after rejection. Shutdown
terminal record and host process/window check are the final cleanup evidence.

Final cleanup verified: listener-terminal status stopped, HALT true, no release
processes or named windows remain, stale watchdog absent. Installed pin/state version
match f13b2c7; candidate is clean. Final diff check passed. release-complete.json records
full gate, installation and actual bot proof distinctly. Temporary smoke complete;
there is no ongoing receiver. DuetFlow was not repaired/resumed. Modelctl ownership,
its eight preserved files and the earlier history/D30/D31/D33 boundaries remain intact.
