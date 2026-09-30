# Handoff: finish graph /ask and correct DuetFlow V-DAN guidance

## Read first
- AGENTS.md (including user-provided global instructions from this session).
- handoffs/2026-09-29-local-model-switch-pending-codex.md and handoffs/2026-09-29-graph-p12-07-run4-vdan-pending-sonnet55-codex.md.
- This handoff and uncommitted diff in /tmp/maestro-graph-ask-fix, branch fix/graph-vdan-ask at baseline 2cac0e7.

## Objective / authorization
Dan explicitly said: "fix it and tell me what to do, should you fix it now and then i approve and then pilot continues or something else?" Finish the fix, verify it, and give him the precise approval/update sequence. Another agent owns model-switch command fundamentals: defer all that work. Do not ask for redundant implementation/design approval.

## Live pilot and user evidence
- Existing controller agents:orchestrator /tmp/run-duetflow-07-run4.sh is still running; do not duplicate it.
- DuetFlow pin is 5c94c9fead187efc05157272da4eec9b36b19fff. Run run_qG3zSNhcUbdHKyDm is paused at dan_confirm; request verify-07-reconciliation-2f3bf5abeb3b, Telegram 117, pending at last read. Snapshot 2f3bf5abeb3b92b34f3a0cc736acbfa3b29ea37a.
- Dan personally performed hand edit and Refresh. Added track 2PnlsTsOTLE5jnBnNe2K0A. He explicitly confirmed the added track stays and removed track stays out.
- Read-only /home/dan/.local/share/duetflow/duetflow.db query found duet_state: origin pin, side a, attributed_to a, added_at 2026-09-30T07:38:45+00:00, pinned_until 2026-10-30T07:38:45+00:00. History has open interval. No manual_pins row (successful bookkeeping clears it).
- Proof_review_03 verdict approved, one nonblocking duet_history follow-up (read actual review.json). Gate 5 previous evidence 297 passed, 1 skipped.
- Do NOT interpret Dan's confirmation of persistence as authorization to write pilot approval. He asked to fix Maestro first; final commentary told him to keep approval pending until guidance correction and final checks complete.

## Root causes (verified)
1. DuetFlow docs/ROADMAP.md 07 V-DAN step 4 claims app explain <track id> shows attribution. Actual cmd_explain in verified worktree app.py:250 queries role B listening plays and scores only; message No role b plays is expected. Wrong instruction is in ROADMAP, rendered by Maestro controller from frozen checks, not authored by an agent when sent. No implementation attribution failure demonstrated.
2. maestro/hitl/commands.py _handle_ask resolves only legacy waiting_on_dan. Graph request uses awaiting_dan_verification, so /ask 07-reconciliation ... currently says no parked task matches. docs/DESIGN.md near line 951 explicitly records this gap.

## Staged implementation (NOT installed, NOT committed)
Isolated worktree /tmp/maestro-graph-ask-fix branch fix/graph-vdan-ask:
- maestro/hitl/graph_ask.py new helper reads exact pending verification/manual request, checks run/snapshot identity and absence of answer, opens client control store, loads latest succeeded implementer/prep manifest via immutable ArtifactStore, includes acceptance criteria, task notes, Dan decisions, entrypoints and rework note. No controller writes or native resume token. Currently only awaiting_dan_verification, deliberately no partial support for spec/permission requests.
- commands.py falls back to helper after legacy resolution, writes graph_request and graph_context into normal ask request, launches same named tmux worker, advertises graph /ask in help.
- ask.py honest fresh operator support prompt carries run/snapshot/action/context, asks to inspect implementation and provide concrete read-only check with expected output. Advice only, no mutations or verdict. Graph footer uses /verify <task> approve/reject (manual uses done/redo), no empty /approve.
- verify.py request advertises /ask <task> <question>.
- Regression tests appended to characterization/test_commands.py and test_ask.py. Red run: 2 failed as intended, one already-answered refusal test passed. Focused suite passed before final cleanup. Latest focused run logs /tmp/maestro-graph-ask-focused.log (inspect summary/exit); command was python -m pytest -o addopts='' -q tests/characterization/test_commands.py tests/characterization/test_ask.py tests/graph_engineering/test_dan_verification.py. Earlier progress indicated 196 tests passing; use latest literal summary.
- git diff --check passed before final cleanup. Full suite NOT run. No live Telegram /ask reply proven.

## Remaining actions (ordered)
1. Inspect staged diff and latest focused summary. Strengthen tests for real immutable manifest extraction, manual request, mismatched snapshot/run and missing worktree/artifact; current tests cover routing request generation, verdict untouched, no answered-request launch, prompt/footer. Review whether preserving current helper's no-native-resume fresh configured implementer call is sufficient (honestly documented). Error handling should be clear if worktree/context missing. Avoid broad new feature design.
2. Correct docs/DESIGN.md stale /ask gap and user help docs. Record D32 wrong V-DAN guidance, D33 graph /ask support in STATE (comment pointers added this session).
3. Correct DuetFlow ROADMAP 07 V-DAN attribution command. Do not modify DuetFlow implementation or broaden pilot task just to add CLI. Use a read-only SQLite query of duet_state origin, attributed_to, pinned_until, joining accounts only for spotify_user_id if needed; never select credential columns. Use app config.DB_PATH to respect DUETFLOW_DB. A human-readable expected output should say origin pin and role a for Dan/role b for partner. Correct command should show supplied track and fail clearly if missing. The wrong explain command belongs to this task guidance, not Maestro generic rendering.
4. Sandbox writes allowed only Maestro and /tmp. For DuetFlow roadmap edits request narrowly scoped escalation, or build one executable remediation script in /tmp and give Dan ONE exact command plus expected output. Do not treat boundary as terminal blocker. Privilege never run directly.
5. Full Maestro suite in named agents tmux window, log + exit file. Fresh worktree lacks gitignored graph specs; previous handoff documented 5 full suite failures when missing. Copy unchanged source docs/graph-engineering/specs into candidate if required; don't confuse missing local docs with regression. Use existing main .venv interpreter without dependency installs. Verify full suite exact counts and diff before commit/integration. Coordinate other agent changes; don't overwrite their work.
6. Safe runtime sequence: selfupdate.adopt docstring explicitly forbids mid-task adoption. Do NOT hotpatch immutable pinned version or adopt now while dan_confirm is active. Finish staged fix and correct guidance; current verified pilot behavior can then be approved explicitly by Dan using /verify 07-reconciliation approve. Let pinned existing controller finish merge/graduation/accept, preserve review follow-up, halt before next task, export exact run/SHA, then materialize/self-test/adopt fixed commit at lease-free task boundary, pinned doctor. Need review actual controller task-boundary mechanism before approval so next task doesn't auto-start before adoption; use established HALT/control path, avoid race or duplicate writer.
7. Tell Dan what to do and expected Telegram reply. After installation he can test /ask <waiting-task-id> <question>; verify actual delivery, not only CLI. This pilot might no longer be waiting after acceptance: use next genuine pending graph request or disposable integration scenario, don't fabricate acceptance or waiting state.
8. Continue pilot D30/D31 only from genuine live proof, preserve existing nonblocking review follow-up and export evidence. Model switch command and Context Gate out of scope.

## State / close-out
Main Maestro remains at 2cac0e7 with original untracked artifacts/graph-engineering/p12-pilots/duetflow.json and docs/GRAPH_ENGINEERING_ARTIFACT.md untouched. Staged worktree has 5 modified tracked files and untracked graph_ask.py. No commit, merge, adoption, Telegram send, pilot answer, DuetFlow file change, full test run, or new background controller occurred. Focused test processes finished; existing pilot controller is intentionally retained. Context warning reported 122936 tokens; closing at verified focused-test boundary. Skills already read: systematic-debugging, test-driven-development + writing-good-tests, verification-before-completion, using-git-worktrees, close-session, handoff. Re-read relevant skills as needed in fresh session.

## Success criteria
Correct attribution instruction with expected output; regression + full-suite evidence; graph ask has exact pending-request context and leaves verdict untouched; safe task-boundary adoption and pinned doctor; actual Telegram support reply verified; clear user action sequence; no interference with other agent/model-switch work or Context Gate.
