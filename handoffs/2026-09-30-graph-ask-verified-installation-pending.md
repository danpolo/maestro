> STATUS SUPERSEDED: Dan approved; pilot succeeded; ead3819 installed; pinned doctor exit 0.
> Read `handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md` for current state.
> The staging detail below is retained as evidence, not a request to repeat installation.

# Handoff: graph /ask fix verified; finish guarded installation after Dan's verdict

Read AGENTS.md, this handoff, and the current live state first. Prior root-cause evidence:
`handoffs/2026-09-30-graph-vdan-ask-fix-pending.md`. Success criteria remain safe task-boundary
adoption, pinned doctor, and actual Telegram support delivery from a genuine waiting request.

## Verified work

- Maestro fix **ead3819ce9110debec25f12dc1fee66174b1367b** on `fix/graph-vdan-ask` in
  `/tmp/maestro-graph-ask-fix`, fast-forward integrated into the existing
  `feat/graph-engineering-foundation` branch. Commit has 8 files; inspect its diff.
- Exact pending graph verification/manual-action support. Read-only SQLite connection
  (regular ControlStore clients migrate schemas); immutable artifact hash checks; refusal
  for missing implementation tree/database/attempt/artifact or wrong identity; saved
  `dan_answers`, `relevant_decisions`, and `dan_decisions` included. Fresh configured
  implementer call, no native resume token, graph /verify footer. Handler never answers
  the original verdict. Backend filesystem enforcement follows existing capabilities;
  writable=False is advisory for unsandboxed drivers (known D30 limitation, out of scope).
- Focused checks: **205 passed**. Red-green for missing context: 3 failed / 6 passed before
  fix; missing Dan answers test: 1 failed / 8 passed before review fix.
- Final full suite `/tmp/maestro-graph-ask-full-v2.log` + `.exit`: **5239 passed,
  1 xfailed, exit 0**, 382.98 s. Independent code review says ready to merge.
- Materialized at `/home/dan/.maestro/versions/ead3819ce9110debec25f12dc1fee66174b1367b`.
  Actual selfupdate.self_test there: **5239 passed, 1 xfailed, exit 0**, 371.67 s.
  `/tmp/maestro-graph-ask-candidate-selftest.json` + `.exit`; JSON archived alongside
  scripts in `handoffs/2026-09-30-graph-ask-installation-support/`.
  Untracked specs and STATE were copied unchanged into the candidate to avoid fresh-tree
  missing-document failures. Candidate git status is clean.
- DuetFlow guidance fixed and committed **1d135ff** on its main, only ROADMAP edited.
  Uses duetflow.config.DB_PATH (respects DUETFLOW_DB), SQLite mode=ro, four noncredential
  duet_state columns. Query parsed from YAML and executed successfully. Fixture role-b
  output works; missing/invalid track each exits 1.
- Live track **2PnlsTsOTLE5jnBnNe2K0A** has origin=pin, attributed_to=role a (Dan),
  pinned_until=2026-10-30T07:38:45+00:00. Dan previously confirmed the addition stays and
  removal stays out. This is NOT authorization to approve the V-DAN request.
- Candidate helper successfully read the genuine live pending request and 6617 chars of
  immutable context without changing the request or creating an answer. Evidence:
  `/tmp/maestro-graph-ask-live-context.json` (older projection before review additions).
- docs/DESIGN.md stale /ask gap corrected in fix commit. D32/D33 now recorded in
  `docs/graph-engineering/STATE.yaml`. D30/D31 remain open on their original criteria.

## Current controller — deliberately retained

**Do not launch another controller.** Original `/tmp/run-duetflow-07-run4.sh` gracefully
stopped via HALT. Exactly one replacement was verified: named window
`agents:codex-duetflow-07-install-drain`, shell `/tmp/run-duetflow-07-install-drain.sh`,
Python `/tmp/drain-duetflow-07-before-update.py`. Last verified PID 513316 (requery).
The guarded controller runs the unchanged pin **5c94c9fead187efc05157272da4eec9b36b19fff**.
Startup log `/tmp/duetflow-07-install-drain.log` confirms 1 original run resumed.
It holds the same project orchestrator.lock, clears HALT only after obtaining it, uses the
normal graph loop, accepts no queue tasks, disables auto-update/proposals, and sets HALT
when the existing run settles. Unexpected successor or unrelated run refuses with HALT.

The guard was rehearsed against the real graph loop: existing run succeeded, HALT set,
ready NEXT task did not get a run (**1 passed**, 1.14 s). Archived script + result text
are in the support directory. Production pinned files were never hotpatched.

Live run **run_qG3zSNhcUbdHKyDm**, task **07-reconciliation**, paused at dan_confirm.
Request **verify-07-reconciliation-2f3bf5abeb3b**, snapshot
**2f3bf5abeb3b92b34f3a0cc736acbfa3b29ea37a**, Telegram **117**, was still pending with
no .answer at the last read. Request location: DuetFlow `.orchestrator/questions/`.
Current pin remains old. Guard startup produces known meta-worktree origin warning;
no new failure demonstrated. The server/daemon recovery did not kill the host controller:
ordinary sandbox pgrep cannot see host processes; use escalated host pgrep or tmux.

## Dan's decision — approved, installation in progress

A native async question was sent: does he approve live 07-reconciliation verification,
including his removal/addition persistence observations? Options: approve and finish
installation / reject / keep pending. It explains that the handoff reserves this verdict
for him and that the fix request alone is not permission to answer. Dan explicitly chose
"Approve the verification and finish installation". The pinned verify helper recorded it:
`✅ Recorded your approval of 07-reconciliation at 2f3bf5abeb3b.` Script
`/tmp/record-duetflow-07-approved-verdict.py` passes the current JSON projection explicitly
and uses the documented append_journal(shadow=False) seam for external text journaling,
avoiding a second SQLite writer. Verify the answer and terminal state before repeating it.
No approval is pending now; proceed to settlement, adoption, and pinned doctor.

## Next actions, in order

1. If Dan explicitly approves in chat, record ONLY that verdict through the pinned
   `maestro.hitl.verify.handle_verify_command("07-reconciliation approve")` protocol.
   Use a new process with MAESTRO_REPO=/home/dan/projects/duetflow and PYTHONPATH pointing
   to the OLD pin. Do not directly edit control.sqlite3/state or fabricate the result.
   The helper writes the same question answer as Telegram; no control-store writer.
   If he uses Telegram `/verify 07-reconciliation approve`, verify the real answer instead.
   Expected reply: verification answer recorded/Approve for this task; inspect exact helper
   output rather than guessing. Rejection must record his actual reason and follow ordinary
   rework/halt/export rules; never install mid-run. Keep pending means leave pin unchanged.
2. Wait for guarded controller terminal `/tmp/duetflow-07-install-drain.exit` and
   `/tmp/duetflow-07-install-drain-terminal.json`. Verify task_runs succeeded, tasks accepted,
   durable acceptance decision, graduation/main SHA, retained follow-up, controller exited,
   HALT retained, zero active graph runs/claimed or running workers/live writer leases.
   Guard runs no next task. Ordinary graph graduation may push target; it is the existing
   authorized controller path. Do not duplicate or race it.
3. Export terminal pilot with `scripts/graph_pilot_export.py --project
   /home/dan/projects/duetflow --out <new artifact path>` and report SHA-256. Preserve the
   preexisting untracked duetflow.json. Proof review 3 approved, one nonblocking follow-up:
   duet_history should record a confirmed PUT admission later hand-removed before failed
   bookkeeping retries. Ensure the accepted task's follow-up is preserved/queued. Gate 5
   previously had 297 passed, 1 skipped. Report exact acceptance/main/export SHAs and quality
   failure count from durable data, not inferred numbers.
4. Run **bash /tmp/install-maestro-graph-ask-at-boundary.sh** only after step 2. Narrow
   escalation was approved for that script. It refuses active controller/run/worker/lease,
   requires exact successful candidate self-test and clean candidate SHA, invokes supported
   selfupdate.adopt(project_repo=DuetFlow), verifies project pin, global pointer unchanged,
   runs the pinned doctor, and restores prior pin/version on failure. Expected success:
   `Installed ead3819ce9110debec25f12dc1fee66174b1367b; pinned doctor exit 0; HALT retained`.
   Evidence `/tmp/maestro-graph-ask-installation.json`,
   `/tmp/maestro-graph-ask-pinned-doctor.log`, rollback metadata
   `/tmp/maestro-graph-ask-install-before.json`. Refusal was verified against actual active
   pilot without mutation. Isolated boundary checks also verified running pilot refusal,
   accepted-idle allowance, and active-worker refusal. Check doctor warnings individually.
   Installer currently preserves project.yaml; another agent owns model-switch fundamentals
   and main's model edits. Prior handoff authorized DuetFlow Sonnet 5->5.5 adoption, but
   latest shared-default rule preserves deliberate overrides; do not silently broaden this
   ask fix into model-switch work. If doctor specifically fails on the old project model,
   reconcile that evidence/authorization with the other agent before changing config.
5. Amend artifacts/graph-engineering/p12-pilots/manifest.json code_under_test only on real
   adoption, recording old/new pin, why, exact selftest/doctor evidence. No criteria/weights
   changed. Register the guidance correction separately if needed; immutable request 117
   remains the originally issued text, and support/context plus chat correct its advice.
6. Verify an actual Telegram /ask reply from the installed version against the next genuine
   waiting graph request. This accepted pilot may no longer be waiting. Do not fabricate
   waiting state, substitute a unit test for bot delivery, or approve an unrelated task.
   Leave HALT until the agreed continuation can be monitored safely. Model-switch command,
   Context Gate, and broad permission redesign remain out of scope.
7. Update D33 activation/delivery status and D30/D31 only on their required live evidence.

## Worktree / permission state

Main Maestro branch `feat/graph-engineering-foundation` has integrated fix ead3819 plus
this handoff/evidence recording (query newest HEAD). Preexisting model-switch edits remain
in catalog.py, limits.py, project.yaml.tmpl, test_catalog.py, test_limits.py; byte hashes
of these five and the two original untracked artifacts were verified unchanged after
integration (`/tmp/maestro-graph-ask-preserved-files.json`). Do not include or overwrite them.
DuetFlow main is 1d135ff until pilot settlement; generated UPCOMING/dependency_map md/png
modifications were preserved. /tmp fix worktree is clean at ead3819. No push/adoption was
performed by this session. The guard's normal meta startup attempted its existing push,
with the recorded known warning; no separate Telegram send was performed by the agent.
No stale background test or monitor loop remains; only the deliberately retained guarded
pilot controller. Use host-level process checks, anchored patterns avoiding self-match.

Scripts are copied losslessly in the support directory; if /tmp files disappear, restore
those same named files to /tmp before executing. Do not replace the running guard with a
second copy. Full candidate self-test is also archived there. First scripts had transient
fixture setup/heredoc errors, corrected before live actions; final operational scripts
passed syntax/rehearsal checks.

Suggested skills: verification-before-completion; monitor-long-running-tasks for terminal
waits; systematic-debugging only if an actual failure appears. At session close the runtime
reported 181525 context tokens, entering the configured awareness window; this is the
verified installation-ready boundary. Fresh session should finish the bounded steps above.
