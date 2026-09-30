# Handoff: graph /ask safely installed; next genuine Telegram smoke

Read AGENTS.md, this file, and `docs/graph-engineering/STATE.yaml` D32/D33/D30/D31.
Detailed implementation/staging evidence (status now superseded):
`handoffs/2026-09-30-graph-ask-verified-installation-pending.md`.

## Dan's exact next action

Open a fresh Codex session in `/home/dan/projects/maestro` and paste:

> Read AGENTS.md, handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md, and docs/plans/2026-09-30-operator-intake-and-modelctl-review.md. Continue the two requests: establish how my bug reports, changes, and feature requests enter Maestro's workflow, using the unavailable-song versions as the example; compare /home/dan/projects/modelctl against the agreed Maestro requirements and recommend what remains. Keep DuetFlow halted until its continuation is agreed. When the Telegram /ask check becomes possible, tell me the exact bot, exact message to send, and expected reply.

**No Telegram action is needed from Dan now.** The installed fix has automated test
coverage, but no real support answer from the bot has been observed yet. That remaining
check means sending a question about a genuinely waiting task and confirming that the
bot replies, rather than saying it cannot find the task. The next agent owns arranging
the check; it must give Dan the actual task ID, bot identity, complete message to copy,
expected reply, and what to do if no reply arrives. Do not leave placeholders as user
instructions or ask Dan to invent a waiting task.

Dan's close-out instruction (2026-09-30): whenever ending a session and recommending
a new session from a handoff, always use close-session and provide exact user steps
and a paste-ready next-session prompt. Explain any remaining check in plain language,
and distinguish work owned by the next agent from actions Dan must take now.

## Completed and verified

- Maestro graph /ask fix **ead3819ce9110debec25f12dc1fee66174b1367b** is committed,
  reviewed, integrated on feat/graph-engineering-foundation, materialized and **adopted
  by DuetFlow**. Project pin and state version both match this SHA. Global pointer
  was verified unchanged. Installed worktree has no uncommitted changes.
- Exact pending verification/manual-action context, immutable manifest, real Dan answers
  and decision references; fresh configured advisory call; no native session token; correct
  graph /verify footer; no handler verdict write; missing context fails clearly.
  Source diff: `git show ead3819`. Unsandboxed-backend writable=False remains advisory,
  accurately documented; broad permissions redesign is out of scope.
- Focused tests **205 passed**. Final full suite **5239 passed, 1 xfailed, exit 0**
  (382.98 s). Actual materialized selfupdate.self_test **5239 passed, 1 xfailed,
  exit 0** (371.67 s). Evidence `/tmp/maestro-graph-ask-full-v2.log` + `.exit` and
  `/tmp/maestro-graph-ask-candidate-selftest.json` + `.exit`; selftest JSON archived in
  `handoffs/2026-09-30-graph-ask-installation-support/`.
- DuetFlow attribution instruction corrected in **1d135ff**, without implementation edits.
  config.DB_PATH + mode=ro query verified on live added track and controlled role-b,
  missing-track and invalid-track fixtures. Live track 2PnlsTsOTLE5jnBnNe2K0A:
  origin=pin, attributed_to=role a (Dan), expiry 2026-10-30T07:38:45+00:00.
- Dan explicitly chose **Approve the verification and finish installation** in the native
  question. Pinned verify protocol recorded approval at snapshot
  2f3bf5abeb3b92b34f3a0cc736acbfa3b29ea37a (Telegram request 117). Answer is Approve.
  Do not ask him again or repeat the answer submission.
- Run **run_qG3zSNhcUbdHKyDm**, 07-reconciliation, **succeeded**, 14 attempts,
  **2 quality failures** on implementer node, 2 rework attempts. dan_confirm, merge,
  accept all succeeded. Task status **accepted**, durable decisions accepted.
  Integrated source **3b19da86d83661691d6c524eafdf93b1cbd950c4**.
  Graduated DuetFlow main **4da33d228022dcf546f04211c06e8450be1add0d**.
  Gate 5 evidence previously 297 passed, 1 skipped. Proof review 3 approved, one
  nonblocking history issue. Both round-2 and round-3 records of that issue survive in
  the queued **07-reconciliation-followups** task (2 exported follow-up records).
  The issue is a confirmed PUT admission later hand-removed before failed bookkeeping
  retries, leaving no closed duet_history interval. Do not lose this follow-up.
- Pilot export **artifacts/graph-engineering/p12-pilots/duetflow-07-run4-accepted-20260930.json**.
  SHA-256 **a932b488df8929602e76e5531d99e511a90ead43e4aaac4d335b54cdbfada0e3**.
  Summary: 1 attempted/accepted task, 1 run, 14 attempts, 2 reworks, 17 human interventions
  (export scope is broader than this session's one approval). Original duetflow.json untouched.
- Same-pin installation drain safely finished only the existing run, then HALT. Actual
  graph-loop rehearsal: 1 passed, existing run succeeded, ready NEXT not opened.
  Drain exited **0**; terminal succeeded. Original controller and replacement have exited;
  final host pgrep anchored patterns found none. No stale test/monitor shells remain.
- Installer refused an active controller in a real negative check, then after settlement
  invoked selfupdate.adopt at idle boundary. **Pinned doctor exit 0**; report and logs
  archived alongside scripts. **HALT remains set**. Zero active runs/workers/live leases.
  Doctor left one stale controller lease row (PID 558897), verified absent with host ps;
  do not delete it by direct SQL. Standard controller acquisition handles it.
- STATE D32 closed, D33 fixed/adopted but live Telegram delivery still open. Pilot manifest
  has operator-guidance and code_under_test amendments; initial pinned baseline preserved,
  no criteria/weights changed. STATE parser checks **13 passed** before final recording;
  recheck after this final recording if needed.

## Current boundary / material limits

No controller is running and the project is deliberately HALTed before any next task.
The next queue includes 07-reconciliation-followups and manual prep 08-timers-notify.
Do not clear HALT or start extra tasks merely to manufacture a /ask test without the
agreed continuation. No actual Telegram /ask SUPPORT reply has been observed yet;
unit tests, the actual context extraction, and closing request 117 are different evidence.

Pinned doctor warnings (exit remains 0):
1. Missing context-limit entries **claude-sonnet-5** and **gpt-6-sol** in project role overrides.
   Doctor's model-id probe found 0 confirmed invalid among 4, which does not establish
   successful paid calls. The modelctl/shared-default work belongs to another agent;
   explicit project.yaml overrides were preserved. Do not silently change them as part
   of this ask fix. Reconcile with that owner before next paid calls if necessary.
2. DuetFlow watchdog systemd unit not installed (known; privileged commands only through
   the user-run one-script procedure).
3. Existing duetflow-meta worktree has no usable origin for push (known). Fix only if
   requested or necessary for the agreed continuation; primary acceptance was verified.

D30 quota-available Claude dontAsk/no-classifier live proof and D31 automatic durable-pause
recovery proof remain open. The deliberate installation restart is not D31 automatic proof.
Context Gate is not built and must remain out of scope.

## New requests from Dan — next session priority

Dan added two requests after installation:
1. Some Duet song versions are unavailable despite playable alternatives on Spotify.
   The main task is **establishing a bug/change/feature intake mechanism integrated into
   Maestro**, using this report as the concrete case, rather than isolated playlist edits.
2. The finished model-adding tool is at **/home/dan/projects/modelctl**. Compare it with
   Maestro's agreed model-switch/shared-default requirements and recommend remaining work;
   it can be reused without being part of the Maestro project.

Agent-facing scope, dependencies, success criteria and next actions are in
**docs/plans/2026-09-30-operator-intake-and-modelctl-review.md**. Neither task was investigated
in the closing session; do not invent findings or claim completion. Begin these in a fresh
session rather than starting a broad investigation in the context close-out window.

## Genuine support delivery verification

At the next authorized genuine graph verification or manual-action wait, verify `/ask
<waiting-task-id> <question>` on the installed version through the project bot. Check:
actual support reply delivered, correct pending request/run/worktree/context, correct graph
/verify command footer, and original .answer/verdict still untouched by asking. Do not create
fake waiting state or an approval to make the test pass. A disposable integration scenario
can be used only with a real workflow-produced request and agreed external Telegram effects.
If no such continuation is authorized, keep the project HALTed and report the smoke as open.

For further pilot continuation: inspect real state first, coordinate model-limit/override
work, preserve the history follow-up, and resume exactly one normal pinned controller using
existing project launch/lock workflow. Installation scripts are preserved for audit, not to
be rerun unnecessarily. Complete D33 delivery and D30/D31 only on their specific evidence.

## Branch and uncommitted state

Maestro main checkout `feat/graph-engineering-foundation` is **c711dd2** (evidence-only
commit after runtime fix ead3819; query live HEAD in case modelctl owner advances it). Five preexisting model-switch edits are preserved:
catalog.py, limits.py, project.yaml.tmpl, test_catalog.py, test_limits.py. Original untracked
artifacts/graph-engineering/p12-pilots/duetflow.json and docs/GRAPH_ENGINEERING_ARTIFACT.md
are untouched. Check `/tmp/maestro-graph-ask-preserved-files.json` hashes if needed.
Fix worktree /tmp/maestro-graph-ask-fix is clean at ead3819. Materialized version is clean.
DuetFlow main is 4da33d2; docs/dependency_map.md remains generated and uncommitted, preserved.
Do not clear it reflexively. Graduation followed the existing push path; there was no separate
agent git push. Handoffs and STATE are ignored under existing repository conventions;
leave them durable on disk rather than forcing them into git. Manifest amendment + new export are committed in **c711dd2**, without unrelated model
edits. Final STATE parser recheck: **13 passed**, 0.26 s.

Operational scripts, selftest report, installation report and doctor log are archived in
`handoffs/2026-09-30-graph-ask-installation-support/`; /tmp live evidence also remains.
Suggested skills: verification-before-completion; monitor-long-running-tasks on actual
continuation; systematic-debugging only for observed failures. Session context entered
its awareness window at **181525 tokens**; this is the verified installed boundary.
Start fresh for any further investigation or pilot branch.
