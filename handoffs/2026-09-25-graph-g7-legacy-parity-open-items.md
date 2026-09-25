# G7 — close the legacy-vs-graph parity gaps left open after dd9a2ff

Status: **investigation done, no code written.** A previous agent read the code and made the
design calls below, then stopped at its context budget. Start from here and don't survey the code again.

## Goal

Dan's rule: every legacy behaviour is carried into the graph runner (`orchestrator._graph_main`)
unless it is irrelevant under the graph *and* the graph already has something better. Read
`docs/DESIGN.md` §16 "Parity with the legacy loop" (lines ~840-897). Its "Still open" paragraph
is this task. Close the four items below with one commit each (`fix(graph): …`, ending
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`). Then move them from "Still open"
to carried/replaced in §16. That doc change can go in the last commit or in its own `docs(design):` commit.

## Setup

- Branch `feat/graph-engineering-foundation` at or after `dd9a2ff` (`git log --oneline -12`
  must show D9/D10/G1-G6). In a worktree: `git -C <wt> reset --hard dd9a2ff` if behind.
- `docs/graph-engineering/` is gitignored: `ln -s /home/dan/projects/maestro/docs/graph-engineering <wt>/docs/graph-engineering`
  (else 5 failures in `tests/test_next_graph_prompt.py`). A worktree also needs
  `ln -s /home/dan/projects/maestro/.venv <wt>/.venv`.
- Full suite: `.venv/bin/python -m pytest --junitxml=/home/dan/projects/maestro/.junit-g7.xml`
  (addopts has -q; don't pass -q; read counts from the xml). Baseline at dd9a2ff ≈ 4833 tests,
  0 failures.
- Constraints: don't touch `maestro/watchdog.py`. Don't build any Context Gate piece (AGENTS.md).
  Always mock Telegram. Use the lane harnesses in
  `tests/graph_engineering/test_legacy_parity.py` (`run_lane`, `_happy_scenario`, `_Spy`,
  `_drive_every_worker`) and the kind:dan lane pattern in `test_end_to_end.py` (`_Dan`,
  `_dan_lane`: monkeypatch `_verify.send_via_bot` and `_verify.telegram_configured`).

## Facts established (file:line at dd9a2ff)

Compiled lanes (`compiler.BASELINE_TEMPLATES`, ~line 470; verified by compiling specs):

- **manual** (`dispatch: manual`; `mode: needs-dan` normalises to it):
  `prep`(implementer, writer) → `human_action`(kind human, handler `maestro.handlers.human`,
  permissions `operator`) → `gate` → [`dan_confirm` if kind:dan checks] → `accept`.
  Terminals `("human_action","accept")`. **There is no merge node.** The required criteria are
  `human_receipt, verification_gate, acceptance`. So under the graph a manual task's prep
  commits never reach main, yet `accept` graduates it. DuetFlow `08-timers-notify`
  (needs-dan, a kind:dan V-DAN check) is exactly this lane. Legacy merged the prep
  (`parking.handle_prep_done` → `_merge_prep_branch`).
- **async** (`dispatch: async-job`): `async_launch`(script handler, runs `launch_cmd`, no
  writer lease, no worktree) → `await_job`(wait, controller_only, params
  `kind: awaiting-verification`) → `gate` → `accept`. No merge (legacy had no branch either).
- **resumable** (`resumable: true`): `implementer` → `gate` (on_failure → `cooldown`) →
  `merge` → `accept`. `cooldown` is a wait node, controller_only, with param `cooldown_hours`
  (`resume_cooldown_h`, default 20).
- The `human` and `wait` handlers (`handlers.py:630-659`) always return `paused`. A paused result
  moves no node (`models.RESULT_DISPOSITION`), so the attempt stays claimed forever.
  `runner._repoll_operator_nodes` (`runner.py:1903`) re-runs a paused inline node **only for
  `DAN_CONFIRM_HANDLER`**. `wait` nodes run inline (controller_only ∈
  `CONTROLLER_PERMISSIONS`, runner.py:83). `human_action` (`operator`) is dispatched to a tmux
  worker that writes `paused`. The repoll can still re-run it inline on the same attempt,
  because it only needs the paused result and the receipt in the inbox. That also rescues a live pilot run
  whose human_action is already parked.
- `_still_working` (runner.py:2390) ignores claimed inline nodes. `run_outcome` needs every
  terminal to be `succeeded`, so a paused terminal keeps the run open.
- `failures.retry_cone` (failures.py:87): verification handlers (`VERIFICATION_HANDLERS`) get a
  cone that reaches back to the nearest upstream agent writer. A reviewer gets one only for
  `kind == "verification_failed"`. Anything else gets `()` (the plain failed path).
  `runner.rework_note` (runner.py:1971) feeds Dan's dan_confirm reason and the reviewer findings
  into the writer's brief (`manifest_doc["rework_note"]`, runner.py:766-775,
  rendered by `worker.agent_brief`).
- The graph prep node's brief is the generic manifest brief (`worker.task_header` +
  `agent_brief`). **It never gives the prep agent the B14 contract**: do all prep, never the
  human step, write `dan_action` / `post_action_cmd` into `result.json`. Legacy's contract is
  `implementer._make_prep_brief` (implementer.py:355). The agent handler already carries
  `result.json` verbatim in `outputs["result"]` (`handlers.result_from_workspace`).
- `task_tree.ensure` reuses an existing `impl/<task>` branch. A failed run's `release` keeps
  the branch; only a merged branch is deleted. So a fresh run of a task continues on the old
  branch's commits.
- `_graph_main` (orchestrator.py:2356) builds `opened` from every task with a task_runs row and runs
  `if task_id in … opened …: continue`, so no task is ever reopened. It already honours
  `state.resume_state[task].resume_after` and `parked_tasks`. The failed-settle notice is
  `_graph_notify_failed` (orchestrator.py:2144), a plain `notify_telegram`.
- Control verbs are in `maestro/hitl/commands.py` `poll_control_commands` (~line 203). `/unpark`
  (~line 266) only edits `parked_tasks`/`retry_counts` and replies "will be launched on next
  cycle". `/approve` and `/reject` act on `waiting_on_dan[dan_id]`. `/redo` and `/ask` resolve via
  `_resolve_waiting`. `/fix` reads `.orchestrator/diagnoses/<task>.json`, which only
  legacy `park_failed` writes. `/backend <name> <task>` uses `state.in_flight` + `switch.switch_task`.
  The graph never writes `waiting_on_dan` or `in_flight` rows, and the runner switch refuses
  while legacy waiting entries exist (`_runner_switch_refusal`).
- Dan-request machinery to reuse: `maestro/hitl/verify.py` (`DanVerificationChannel`). It keeps a durable
  record in `.orchestrator/questions/<id>.json`, sends once per id, and uses `danreq:<id>:<i>` buttons answered
  by `telegram._handle_danreq_callback`. Free text goes through `_route_freetext_answer` into the `.answer` file.
  `state["awaiting_dan_verification"]` is what `/waiting`, the reminders and the watchdog read, and
  `prune_awaiting` runs each poll.
- Legacy references:
  - Manual: `parking.handle_prep_done`, `park_manual_action`, `_finalize_manual_action`
    (post_action_cmd, auto-only gate, `_park_awaiting_verification`) and `_graduate_manual_action`.
  - Async: `orchestrator.launch_async_job` and `poll_awaiting_verifications`. The poll uses
    `gates._classify_verifications` to split pending `await: true` checks from hard failures, plus
    `gates._await_timed_out` with `AWAIT_VERIFY_TIMEOUT_SEC` (8 h).
  - Resumable: `parking.handle_incomplete` (progress parse, data-only merge, park after 2 stalls
    or 12 sittings, `resume_state` cooldown).
  - Failure escalation: `parking.park_failed`, with buttons "Retry with new approach" / "Shelve task" /
    "Manual intervention", idempotent per `_escalation_req_id`.

## Design decided (implement this)

### Item 1: make something release the pause nodes

Common: extend `_repoll_operator_nodes` to every paused inline node whose handler is
`maestro.handlers.dan_confirm`, `maestro.handlers.human` or `maestro.handlers.wait`, **except
`park`**, which stays a sink. Make `human` nodes run inline by adding `"operator"` to
`CONTROLLER_PERMISSIONS`. Check `policy.py` and the permission tables for other readers of
`operator` first. The runner gets the channel through the existing `dan_channel` argument.
Extend the channel object rather than adding a second kwarg, so `_graph_dan_channel()` stays
the one swap point.

1a. **manual / human_action.**
- Compiler: add a `merge` stage to the manual template after the gate, and after
  `dan_confirm` when there is one (`insert_dan_confirmation` already anchors on merge when one exists). Add
  `controller_merge` to its required criteria and bump the template version. The graph merges after
  Dan's action and the gate, where legacy merged before. Record this in §16 as a deliberate difference:
  nothing lands on main before the gate, and Dan acts on the worktree, which DuetFlow 08's
  `{worktree}` instructions already assume.
- Prep brief: in `runner._advance_agent`, for a writer node in a revision that has a
  `human` node (that is, `prep`), add `manifest_doc["prep_contract"]` and render it in
  `worker.agent_brief`. It is a short B14 contract lifted from `_make_prep_brief`: do all prep,
  never the human step, commit, and write `result.json` with `dan_action` plus an optional
  `post_action_cmd`. Include the ROADMAP `dan_action`/`post_action_cmd` hints.
- Handler: when it has the controller, `human_handler` asks through the channel.
  - Request id: `action-<task>-<short hash of the human attempt_id>`. A new attempt after a redo
    gets a new request, and repolls of the same attempt reuse it.
  - Action text, first found: the prep's committed `outputs["result"]["dan_action"]`, the task's
    `dan_action`, then the node param `action`. The message mirrors `park_manual_action`
    (worktree path, the one action).
  - Buttons: **Done** / **Redo** / **Abandon**. By text: `/verify <task> done|redo <what's wrong>|abandon`
    (extend `handle_verify_command`).
  - `/waiting` lists it through the same `awaiting_dan_verification` state key. Add a `kind` field.
  - Done: `succeeded`. The outputs carry a receipt `{acted_at, request_id, answer, dan_action,
    post_action_cmd}`. Calling `prep_actions.record_human_receipt` is optional, and only applies when a
    sidecar entry exists.
  - Redo <reason>: a bare Redo asks for the reason, like Reject does in verify. The node returns `failed` with
    `outputs.failure = {"kind": "verification_failed", evidence}`. Make `retry_cone` treat the
    human handler like the reviewer (cone only for `verification_failed`) so it reaches back to
    `prep`, and add the reason to `rework_note`. This is the graph's `/redo`.
  - Abandon: `failed` with kind `unknown` and no cone. The run settles failed and sends the item-4 notice.
- `post_action_cmd`: pass it to the gate through `node_inputs` (for a gate after a human node) and run it
  in the worktree at the top of `verification_handler`. Use timeout 1800 and map `python3?` to the venv
  as legacy does. A non-zero exit fails the gate with the output. Don't commit its output. If it
  dirties the tree, the existing `_uncommitted_refusal` fires, and that is the right message.

1b. **async / await_job.** Change `wait_handler` for nodes with the controller and param
`kind == "awaiting-verification"`:
- Run `gates._classify_verifications` on the task's `await: true` auto checks only. Use the task
  tree as cwd if present, else the repo.
- Throttle it: store the last check time in the node outputs and wait at least 60 s between runs.
- No pending and no hard failures: `succeeded`, and the gate then runs every check authoritatively.
- Hard failures: `failed` with the check ids.
- Still pending past `AWAIT_VERIFY_TIMEOUT_SEC` since the attempt was claimed: `failed` with "timeout".
- Otherwise: `paused`.

Tell Dan once when the job launches (legacy's 🚀 message). That can live in the handler's first poll,
journaled. A failed await settles the run failed and sends the item-4 notice. Retry then means reopen,
which re-launches the job. Note the difference: legacy's "/approve to retry" re-ran only the check.

1c. **resumable / cooldown.** Change `wait_handler` for nodes with `cooldown_hours`, reusing legacy `resume_state`:
- On the first run, read the gate's latest failed detail and `_parse_progress` it.
- Apply legacy's rules. Unparseable progress, the 12-sitting cap, or 2 no-progress sittings: `failed`
  (that is, park).
- Otherwise write `resume_state[task] = {last_count,total,attempts+1,stalls,resume_after}`
  and return `succeeded`. The run then settles failed because accept is skipped.

In `_graph_main`:
- A settled run whose `cooldown` node succeeded sends legacy's "progress saved, resuming in ~Nh"
  instead of the ✗ notice.
- The task can be reopened (item 2) once `resume_after` passes. The existing check already holds it
  until then.
- Clear `resume_state[task]` when the task graduates, as legacy did.

Progress rides on the kept `impl/<task>` branch. Document that legacy's per-sitting data-only merge
to main is *replaced* by one gated merge at completion.

### Item 2: operator reopen

Reopen only on an explicit marker, never by guessing:
- Under the graph runner, `/unpark <task>` writes `state["graph_reopen"][task] = now`, in addition
  to today's `parked_tasks` edit, and replies truthfully.
- `_graph_main` removes from `opened` any task whose latest run is not `running` and that has a
  reopen marker or a due cooldown from 1c. It opens a fresh `start_run` with a new run_id, leaves the
  old failed run untouched, and pops the marker once the run opens.
- No marker means no reopen, which keeps today's behaviour.
- Also clear `compile_refused` for the task, so a fixed ROADMAP entry can compile.
- `task_tree.ensure` reuses the kept branch. Say so in the reply.

### Item 3: operator verbs under the graph runner

Detect the graph runner through config (`engineering.runner == "graph"`; see how
`_runner_switch_refusal` and `main` read it) and reply honestly:
- `/approve <task>` and `/reject <task>`: if the task has a pending graph request
  (an `awaiting_dan_verification` entry), map the verb to its Approve/Done or Reject/Abandon answer. Rejecting
  a kind:dan verification still needs a reason. Otherwise, say the graph has no
  merge-review queue by dan_id and point to `/verify` and `/waiting`. Keep legacy behaviour for
  numeric `waiting_on_dan` ids that exist.
- `/redo <task> <msg>`: if a graph manual task is waiting on Dan, send the Redo answer (1a). Otherwise
  keep legacy behaviour.
- `/fix <task>`: replaced. Under the graph the self-heal is the `diagnose` node, plan repair (W7)
  and the failure ladder, and no diagnosis file is written. Reply saying so and
  offer `/unpark <task>` to retry.
- `/backend <name> <task>`: there is no in-flight session to switch, because routing binds per attempt
  (`routing.refresh_pauses`, fallback/step-down).
- `/backend <name>` alone: check whether graph routing reads `state["backend"]`. If it doesn't, reply
  that under the graph routing makes the choice (project.yaml `routing`/catalog) and the pin does
  nothing, instead of claiming it applies.
- Update the `/help` text for the changed verbs.

### Item 4: failed-run notice with working options

- `_graph_notify_failed` sends a Dan request (a verify.py-style record with id
  `failed-<task>-<run_id>`, sent once per run, restart-safe) with buttons **Retry** / **Shelve**.
- A per-poll handler in `_graph_main` reads the answers:
  - Retry: set item 2's reopen marker and remove the task from `parked_tasks`.
  - Shelve: add the task to `parked_tasks` permanently and journal it.
- Keep today's notice text, because tests assert `✗ <id> failed at <node> — parked (run …)` and `Reason: …`.
  The notice should also say that `/unpark <task>` retries.

## Verification to report back

- New lane tests, with Telegram mocked:
  - Manual: the task sends a Dan request with the prep's `dan_action`. Done → gate → merge → main has
    the prep commit and the task graduates. Redo → prep re-runs with the reason in its brief and a new
    request goes out. Abandon → failed notice.
  - Async: an await check that is pending and then lands → accepted. A hard failure → failed.
  - Resumable: the gate fails with `PROGRESS=n/m` → resume_state is written and "resuming in ~Nh" is
    sent. A second run opens after `resume_after`, on the same branch.
  - `/unpark` → a second run for the task, with the first run untouched.
  - Failed notice: the Retry button reopens and the Shelve button parks.
  - Each verb's reply under the graph runner.
- Full-suite junit counts (tests / failures / errors / skipped) against the ~4833 baseline.
- A table of item → fixed (sha) / replaced (by what, why) / blocked (why), plus the worktree and branch.
- The new commands and buttons a live pilot operator must know. DuetFlow 08-timers-notify will hit 1a.
