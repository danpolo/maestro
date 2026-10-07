# Modelctl class transitions — after session 11 legacy/pruning milestone

## Goal, authority, and resume

Finish accepted D-A–D-E, candidate qualification, and one concrete reviewed owner
application package. Read root AGENTS.md, the complete D-A–D-E section in
`docs/plans/2026-09-30-modelctl-integration.md`,
`docs/plans/2026-10-07-modelctl-class-transitions.md`, this handoff, then
`handoffs/2026-10-07-modelctl-session10-next.md` for the remaining contract chain.
Session9/session8/session7 remain historical evidence and preservation authority.
Do not reinterpret their historical gates as acceptance of the revised design.

Worktree: `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`.
Branch: `feat/modelctl-integration`; HEAD `a40cafc44ea00051a1e609833d15fd710c61cf30`.
All feature changes remain uncommitted. E = worktree
`.superpowers/sdd/2026-09-30-modelctl-integration/`.
Python: `/home/dan/projects/maestro/.venv/bin/python`.
Run the execution-workspace resolver FROM THIS WORKTREE with the MAIN absolute plan
path `/home/dan/projects/maestro/docs/plans/2026-09-30-modelctl-integration.md`.
Running from main resolves an irrelevant main ledger; do not use it or create a
worktree-relative collision. Read E/progress.md Session11.

**Candidate remains incomplete, unreviewed and unapplied.** No new application approval
was requested or granted. Never reapply the previously approved outbox application
script. No live registration, switch, prune, inference, deployment/restart, owner index
edit, installed pointer change, DuetFlow mutation, Context Gate, commit/push/merge.

## Resume fingerprints and preservation

- Resume verified all **293** session10-close-source inputs, both **47-file** archived
  and editable owner copies. Earlier handoff said 284 for session9; session10's actual
  close manifest is 293. No mismatch.
- Editable owner stays `/tmp/modelctl-class-candidate-session9` deliberately.
- New durable copy: E/session11-owner-candidate/ and session11-owner-candidate.json,
  47 source/test/doc/script inputs. Verify hashes and restore /tmp from this copy if
  missing. Never modify old archives. .gitignore and systemd units remain included.
- E/session11-baseline.json captures main status, operating byte hashes/file set and
  live models-home absence. External/host preservation continues from the unchanged
  E/session10-baseline.json `external` map; DuetFlow mutable state stays excluded.
- Serial bounded close verification and its exact results are recorded below.
- E/session11-close-source.json is the final resume fingerprint after documentation,
  NOT a full acceptance qualification manifest.
- Last developer context measurement: **182799 active tokens**, entering the
  gpt-6.1-sol awareness window. Native context/dashboard tools unavailable. The
  close-session then handoff workflow applies at this verified milestone; no compaction.

## Session 11 changes and evidence

### Actual legacy launch and record agreement

- Whole main-loop regression uses the real policy freeze, role/model resolution and
  implementer.launch_implementer, stubbing only paid driver.launch and external
  controller/git/Telegram boundaries. Project backend changes after freeze and before
  launch. RED launched Claude Opus but recorded Codex Luna.
- Real _do_retry regression with an older entry lacking a backend: config edit changed
  the retry to Codex despite its frozen Claude choice. RED reproduced this.
- Main launch plus entry recording now share task_model_context. Retry binds first,
  then enters task context for _entry_backend/fallback, launch, recording and state write.
  Context errors inside retry are still caught by its existing park path.
- Existing carried-backend/backends_tried and scheduling behavior passes. No explicit
  main launch backend pin was added; _launch_backend still reads live operator/quota
  inputs. An operator/quota update DURING driver.launch could still change a subsequent
  recorded backend; this session tested project-config drift, not that interleaving.
  Host slot reservation still precedes freeze and is outside task context. Inspect and
  prove those boundaries before claiming all launch/record synchronization complete.
- E/session11-legacy-red.log: 2 intended failures, 4 passes.
  session11-legacy-green.log: 119 pass, exit 0.

### Detached task calls and recommendation

- Self-fix, redo and ask entry points enter task_model_context around agentcall.ask.
  Deny-list recommend is decorated with with_task_models, so graph/legacy task
  recommendation uses frozen diagnoser choices too.
- Real public runner main tests stub git/transport/execution and inspect concrete
  CompletionSpec models after project edits; all four paths now preserve Opus.
- Tests initially had argparse argv and deny-list reply-shape fixture mistakes. These
  were corrected before the intended RED. Self-fix's fake worktree initially lacked its
  log parent; its production scope was shelved, the fixture corrected, intended RED
  observed (session11-selffix-red.log), then scope restored. Do not present the earlier
  fixture errors as behavioral RED.
- session11-detached-final-red.log covers redo/ask/recommend intended failures;
  session11-selffix-red.log covers self-fix. Final combined GREEN:
  session11-detached-complete-green.log. Earlier missing test filenames caused two
  pytest collection exits 4; these are recorded failed invocations, not green suites.
- No model execution or real notifications were sent.

### Inventory filename/script shape and real prune interleavings

- Inventory rejects a snapshot whose filename is not _task_file(repo, task_id), before
  borrowing terminal-release proof from a different canonical file. Requirement RED
  proved a duplicate misnamed terminal snapshot was previously ignored as complete.
- A complete legacy entry matching main's deterministic script shape (role script,
  status running, all identity fields, window script-<task>, no backend/model keys)
  needs no LLM binding. Anything else retains the prior conservative refusal/binding.
  Requirement RED proved a legitimate script previously made the inventory incomplete.
  Add real subprocess script/misnamed coverage and malformed lookalikes before final gate.
- Conservative frozen_possible_pin guard is still present; no exact-choice cleanup
  optimization or historical guard removal was attempted.
- Owner test_real_maestro_retention uses actual subprocess inventory and real tables.
  At first Transaction.write AFTER inventory and BEFORE deletion, a separate policy
  writer tries freeze/adopt/publish: each blocks until deletion finishes, then resumes.
  Reverse case pauses task atomic_write while freeze holds policy.lock: owner reaches
  shared lock, blocks, and after reservation persists real inventory refuses deletion.
- These tests characterize the existing session10 locking; no invented behavioral RED
  or extra lock fix is claimed. Initial reverse test instrumented a file object as if
  fcntl received an integer fd; corrected using fileno(). Final interleavings pass.
- E/session11-inventory-{red,green}.log; session11-prune-interleavings-green.log.
  Source reader remains version **4**.

### Shipped profile edits and recovery/shape guards

- RED proved a current shipped gpt-6.1-sol limits/profile edit published strength/cost,
  but catalog still used 420K input/32K output instead of owner 380K/28K.
- policy_for_set now serializes the effective profile even for shipped targets,
  caps native input by enforced window, and applies explicit owner strength/cost/output.
  model_classes.profile prefers this immutable serialized record over shipped defaults.
  Shipped new-class targets get a serialized capped verified profile too.
- E/session11-shipped-profile-{red,green}.log covers policy/catalog/lifecycle/graph.
- Concurrent consume characterization synchronizes TWO publish attempts, proves one
  publication and notice, and clean replay. Ack failure characterization proves one
  revision/notice, acknowledgement retry. Owner metadata changed after preparation
  is refused by under-lock revalidation. All passed before additional code changes.
- Three intended shape REDs: prepared journal cannot have notice_pending false;
  unchanged/superseded cannot have it true. _read_switch now refuses these combinations.
- Class facts reject identities/aliases that map to different families on the same
  backend, including casefold identity collisions. Same-family version aliases remain
  legitimate; cross-backend names remain qualified.
- E/session11-recovery-shapes-{red,green}.log. No exactly-once transport guarantee:
  crash after send-to-me delivery but before persistence can still resend.

### Reports, owner removed rows and new-class fallback inputs

- Status/doctor include waiting_limits entries and instruct `modelctl add <provider>
  --model <id>`, without asking for Maestro switch approval. Historical owner proposals
  keep their review instruction. Status exposes switch journals/notice pending state,
  immutable class metadata and per-project effective model reports.
- project_report adds class_choices: original choice, family from adopted owner facts,
  effective version. Existing roles/provenance remains intact. Preview now reports
  class_choices_preserved with ORIGINAL config choices, replacing stale
  overrides_preserved. Sol replacement fixture expectations were updated accordingly.
- RED: session11-reporting-red.log. GREEN: session11-reporting-verified-green.log.
  First broader invocation included CLI tests inside the filesystem sandbox and failed
  because init could not reach TMUX, plus a test fixture used a predecessor not in its
  pre-change policy. Corrected predecessor/table fixture; final serial verifier runs in
  a host agents TMUX window. Do not label those first CLI errors as product regressions.
- Owner inheritance now ignores rows marked set=False/removed. Prune requires a newer
  SET family member; a removed newer row cannot authorize pruning the current model.
  Two intended REDs observed. Custom predecessor thresholds/window inheritance and
  unsafe successor-window rejection characterized as already passing.
- Owner registration_defaults supplies exact newest SET family limits; for new classes
  it shows provider Model unknown values as reviewable/customizable suggestions.
  Missing fallback refuses implicit limits, while complete explicit limits remain
  permitted. Both providers call it. Accepted/custom Codex values, missing fallback and
  existing add flows pass; additional Claude new-class/unsafe fallback UX proof remains.
- E/session11-owner-inheritance-{red,green}.log, session11-prune-removed-red.log,
  session11-owner-fallback-requirement-red.log and owner-fallback-green.log (56 passed).
- No registration bridge was enabled. No durable ruling yet mapped whether D-B/D-D
  supersede the old Maestro picker missing-row path. Do not silently count that complete.
  Current preview still inaccurately says owner transaction fixes are unverified and
  _review says Registered no/Selected no; reconcile this with actual registration state.
  Owner CLI/docs still retain stale proposal/never-activate wording. This session made
  reporting changes but DID NOT finish the planned documentation/UX update.

## Next task — finish registration/reporting contract, then qualify/review

Continue implementation without another planning approval. Main substantial next task:
finish missing-row requirement mapping and owner/public reporting/registration UX,
then remaining frozen-launch safety checks and final qualification.

1. Read D-B/D-D against old item 6/8 missing-row picker authority. Record a precise
   mapping: automatic existing-class owner registration, new-class owner limits/profile
   flow, and any still-required manual Maestro registration-versus-selection workflow.
   Do not infer completion from owner add tests. Accepted/custom fallback is implemented
   in owner; missing/unsafe values, manual successor/profile editing, new-class operator
   UX and truthful registration-versus-selection partial success still need qualification.
   No live application or registration bridge yet.
2. Finish owner CLI/docstrings/docs and Maestro docs. Explain class following, immutable
   task choices, between-task adoption, retained rows/prune locking, logical combined
   notice with transport crash ambiguity, and old-owner discovery proposals/migration.
   Update misleading disabled-registration text from the chosen requirement mapping.
3. Remaining launch safety: main host reservation is outside freeze scope and main
   recorded backend is resolved AFTER launch. Prove live quota/operator drift behavior
   and choose one resolution for reservation/launch/record without breaking legitimate
   carried vendor/quota fallback. Broaden review of task-associated calls. Task-specific
   historical literals absent from policy/project/shipped identity requests still need
   owner family coverage. Hard graph allowlists/preferences/capability/quota fallback
   must remain hard; run original graph recovery test.
4. Add actual owner subprocess inventory cases for new script identity and misnamed
   snapshot; malformed script lookalikes must remain unknown. Validate frozen choice
   shape/integrity, especially task_models entries/hash-recomputed malformed inputs,
   and all malformed filename/task-id paths. Keep historical conservative retention.
   Legacy terminal release without control-store proof remains conservative.
5. Remaining metadata/profile cases: serialized/shipped current edits now work, but
   expanded limits after earlier capped dynamic profile, exact/custom/removed predecessor
   behavior across both providers and unsafe successor windows need requirement review.
   Concurrent/ack/lock-wait/alias/journal cases now have tests; do not repeat broad
   investigation without a new gap. Check fresh combined owner/consumer notice tests.
6. Fresh independent spec-code-review agent explicitly authorized by plan/handoff;
   no implementer delegation. Handle valid findings, prepare guarded owner patch/application
   script preserving staged index. Ask Dan ONE application approval only once reviewed,
   qualified package is concrete. Never reuse old already-applied outbox script.
7. Fresh SERIAL full Maestro gate in named agents TMUX with all required ignored docs
   present, disposable runtime/models state and recording send-to-me. Root AGENTS.md
   remains absent in worktree; old helpers silently skip missing docs. Required docs
   preparation, frozen manifests, exact counts/exit/duration, unchanged source/owner/main/
   host/operating bytes AND file set, live models-home absence, and current review are
   still required. This session's expanded related verification is NOT the full gate.

Suggested skills: executing-plans inline, TDD, systematic-debugging as needed,
verification-before-completion, spec-code-review and bounded TMUX monitoring.
Native Astra capacity stays an owner-data follow-up; never copy another native profile.

## Serial close verification

- Expanded related Maestro suite: **4274 passed**, exit 0, 315.74s. Includes previous model/controller/backend/workflow/control/graph selection plus public CLI, one-shot calls and characterization. E/session11-close-related-result.json, session11-close-related-final.log/.exit.
- First serial owner run: 1 failed / 180 passed, exit 1, 20.94s. Its old_reader fixture directory lacked maestro/__init__.py, so bootstrap correctly rejected that installed pointer and used fallback. Earlier global fallback happened to be unsupported, masking the fixture gap. This run used isolated MAESTRO_HOME, exposing it. Added __init__.py to make the fake reader-3 checkout an actual accepted pointer; no production change.
- Owner FULL repeat: **181 passed, no skips**, exit 0, 20.24s. E/session11-owner-repeat-owner-result.json, session11-owner-repeat-owner-final.log/.exit and session11-owner-repeat-verify-terminal.json. Both source environment variables target this worktree; send-to-me is a recording stub.
- All **335 frozen inputs unchanged DURING the original serial run**, including its failing owner fixture. All **335 inputs unchanged DURING the owner repeat**, after the one fixture correction. Maestro source unchanged between the passing 4274 run and owner repeat. No unnecessary Maestro repeat. Original terminal exit 1 is retained; repeat exit 0 is separate evidence, never overwritten.
- Runner /tmp/modelctl-session11-close-verify.py, named agents window codex-modelctl-s11-close. Local-only terminal monitor codex-modelctl-s11-monitor wrote session11-close-monitor.json. Owner repeat runner /tmp/modelctl-session11-owner-repeat.py in codex-modelctl-s11-owner-repeat. All completed; no ongoing task processes/windows.
- E/session11-preservation.json: external owner/host inputs and main status unchanged, all three worktree operating byte hashes AND file set unchanged, live models home absent, 47 archived/editable owner hashes match. git diff --check passes. Documentation-only close-out updates occurred AFTER verification; final resume manifest captures those.
- These are **bounded related verification**, not the full Maestro acceptance gate. New candidate has no independent review or application approval. Continue from this handoff in a fresh Codex session.
