# Handoff: Graph P12, session 3. The real pilots (paid subscription), then close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). All of P12's maestro-side code is done
(sessions 1 to 2f). This session runs **real** tasks through the graph runner on two scratch projects, retains raw
per-attempt outcomes, re-measures retention and qualifies **paid subscription only**. This file is the whole prompt.
`docs/graph-engineering/STATE.yaml` and `handoffs/` are gitignored, so edits to them live on disk only.

## 0. Read first (and only these)
1. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (short). §2 "Scratch Project Pilot"
   and §4 are this session's spec.
2. `docs/DESIGN.md` §16 (the graph runner as P12 left it: worktree lifecycle, gate refuses an uncommitted tree,
   parking, prep, controller errors, interrupted merge, retention) and `docs/EXECUTION.md` "Graph
   qualification (P12)".
3. `docs/graph-engineering/STATE.yaml`: `in_progress_details` and `_p12_open_threads` (search; do not read the
   whole file).
4. `artifacts/graph-engineering/p12-qualification.json`: `summary`, `modes`, `retention`, `experiments` with a
   jq/python one-liner (never `cat`: most of it is raw `lanes` telemetry).

## 1. Dan's decisions (2026-09-18). Do not re-ask
- **Paid subscription only.** Free, hybrid and the script lane stay labelled *unqualified*. A fixture pass never
  claims production readiness (P12 §3).
- **instagram-to-value (ITV) waits for Dan's own `grill-me` run.** At hand-off it had no `CONTEXT.md` (not done).
  Do not onboard it, do not run `maestro-setup` on it, until Dan says in this session that grill-me is done.
  Then onboard it from `CONTEXT.md` + `docs/adr/` (memory: grill-me precedes maestro init).
- **ITV daemons keep running** (`itv-bot`, `itv-worker` tmux sessions), protected by config guards in its
  `project.yaml`: `bot_files: ["scripts/bot.py", "scripts/worker.py"]`; `deny_list_extra` adds
  `jobs/ staging/ media/ out/ logs/ config/`; `gate.chain: [test]`. Never stop or restart them.

## 2. Pre-flight blockers found while writing this handoff (resolve before any run)
> **Update 2026-09-19 (session 3a): ALL pre-pilot work is DONE. Do not redo §2 or §3 step 1.**
> - DuetFlow `628d51d`: 01-auth complete + auto verifications 02-08 (absolute venv python; fail closed on today's tree).
> - DuetFlow `0dece04`: `engineering.runner: graph`. DuetFlow `e56a9b4`: `kind: dan` V-DAN checks on 06/07/08.
> - NEW maestro feature (Dan's request): `kind: dan` verifications. The task waits BEFORE merge for Dan's Approve/Reject
>   on Telegram (`/verify <id> approve|reject <reason>`, listed in `/waiting`); watchdog does not count it as a stall.
>   Commits 9bca264..dacb69d; DESIGN §16. Suite 4777 passed, 1 xfailed, rc 0 (re-run by the parent session).
> - DuetFlow pinned to maestro `dacb69d` via `selfupdate.adopt` (`duetflow/.orchestrator/current`); maestro installed
>   editable into `duetflow/.venv` (launch.sh needs `.venv/bin/maestro`); python-dotenv held at 1.2.2.
> - Manifest committed: maestro `f32321f`, `artifacts/graph-engineering/p12-pilots/manifest.json` (28 checks, equal weights).
> - `doctor` (with MAESTRO_REPO): only WARNs are systemd_unit not installed (start via `bash launch.sh`) and
>   meta_branch push (DuetFlow has no `origin`; the graph merge path never pushes).
> - DuetFlow state has `paused_by_user: true`: after launching, send `/resume` (Telegram or `maestro ctl`).
> **Session 3a addendum (2026-09-24) — do these first, they are small:**
> 1. **Telegram: SETTLED, nothing left to do.** Dan chose the maestro dev bot
>    (`~/.config/maestro/dev_bot_token` = `@MaestroGenericTestingBot`). `duetflow/.env` holds it plus
>    `TELEGRAM_ALERT_CHAT_ID=1101436848` (0600, gitignored). `doctor` reports `telegram: getMe OK`, and a test
>    message was delivered to Dan (`ok: true`), so the chat is open. No other config on this machine names that
>    bot, so nothing else polls it; the earlier send-to-me/codex-bridge clash does not apply. AbuAliArchive's
>    running bot is a different token and needs no action.
> 2. **The Duet playlist creates itself; do not ask Dan for an id.** docs/PLAN.md §5 already specifies
>    `POST /users/{id}/playlists` with `public: false, collaborative: true` as one-off setup, and the owner
>    token already carries `playlist-modify-private` (duetflow/auth.py), so no re-consent. Edit 06's notes:
>    when `duet.playlist_id` is unset, create the Duet, persist the id (config.yaml), and print the share link.
> 3. **`duetflow playlists` — Dan's decision 2026-09-24: it also REGISTERS the choice, and it is the pilot's
>    first `kind: dan` check (deliberately, to exercise the Telegram wait early).** Add to task 03's notes and
>    give 03 a `kind: dan` V-DAN whose instructions are the command. Behaviour: list the owner's playlists by
>    name with a selection number (scope `playlist-read-private` is already granted), let Dan choose **by name
>    or number**, then write the chosen id to `person_a.source_playlist_id` in config.yaml itself and echo back
>    the name + id it registered. Dan must never copy an id by hand. Keep the selection step usable over a
>    terminal Dan is reading from his phone (numbered list, one command to choose, no long-lived prompt is
>    required — a two-step `duetflow playlists` then `duetflow use-source <n|name>` is acceptable and is the
>    safer shape). Auto checks for 03 must stay offline: cover the listing/registration against a fake
>    transport (e.g. test name `use_source_registers`), not the live pick.
> 4. Re-pin the manifest (`artifacts/graph-engineering/p12-pilots/manifest.json`) after 2 and 3 change the
>    ROADMAP, since nothing has run yet; after the first run those become amendments instead.

**Waiting on Dan before starting:** (a) `duetflow/.env` with TELEGRAM_BOT_TOKEN (dev bot) + TELEGRAM_ALERT_CHAT_ID and
> a /start to the bot; (b) `duetflow/config.yaml` with `duet.playlist_id` and `person_a.source_playlist_id` before
> 06's live check. Then §3 step 2 = launch.
1. **DuetFlow `01-auth` is `status: open` and `mode: needs-dan`, but its code is committed** (`979727d`, `fd977d2`,
   2026-09-11). Its done-condition is Dan walking the Spotify consent for two accounts. Every other task depends
   on it transitively (`02-collector-schema` deps `[01-auth]`). **Ask Dan** (one AskUserQuestion) whether the
   consent is done. If yes, mark `01-auth` complete the way DuetFlow's roadmap schema says. If no, the pilot
   cannot start on DuetFlow; say so and stop there, since it is the only unblocked project.
2. **No DuetFlow task has `verifications:`.** Under `engineering.runner: graph` the gate and accept fail closed
   on an empty closed world (`validation.decide`: "no required verification is declared"), so every task would
   park at the gate. Before any run, add a `verifications:` list to each pilot task
   (format: `[{id: V1, kind: auto, cmd: "..."}]`, see
   `tests/test_integration_acceptance.py::test_the_verification_node_reports_evidence_the_controller_can_ingest`),
   derived from that task's own "Done when" text in `docs/ROADMAP.md` and `docs/PLAN.md` §7. Checks must be
   offline and deterministic (no Spotify network call). Commit that to DuetFlow as one commit before the first
   run. This is "pin the criteria" (below).
3. DuetFlow's `project.yaml` predates the `retention:` block. `config.RETENTION_DEFAULTS` applies without it;
   do not add pruning.

## 3. In scope, in order
1. **Pin before any run.** Write `artifacts/graph-engineering/p12-pilots/manifest.json` in maestro and commit it:
   maestro HEAD SHA; for each project its HEAD SHA *after* the verifications commit; the pilot task ids in order;
   each task's verification ids and cmds with equal weight (unless Dan gives weights); the claude CLI version and
   model ids per role (from `project.yaml` `roles:`). No criterion or weight changes once a run starts. If one
   must change, record it as an amendment in the manifest with the reason, and re-run that task.
2. **DuetFlow on the graph runner.** Set `engineering.runner: graph` in `/home/dan/projects/duetflow/project.yaml`
   (commit). Check `maestro doctor` is clean. Run the loop the way DuetFlow is launched (`launch.sh` / its
   systemd unit; read them first) against the 8 tasks after `01-auth` in dependency order. Do not hand-edit the
   control DB or `.orchestrator`; a stuck task is a finding (next point), not something to fix by hand.
   Operate with the memories on idle polling (dispatch, wait for completion, check only if far too long).
3. **Every failure is a finding.** A defect in maestro found by a real task: fix it TDD in maestro with a lane in
   `tests/graph_engineering/test_end_to_end.py`, commit, then resume. A big one: record it in `_p12_open_threads`
   and park that task (memory: gaps are not decisions). A DuetFlow product bug the agent introduced is a task
   outcome (rework), not a maestro fix.
4. **Retain raw per-attempt outcomes** (P12 §2): for every attempt, the `attempts` row telemetry (model id, agent
   version, session id, fresh/resumed, token split, quota windows at start and settle, wall time, reset crossing,
   failure kind/class), rework and reopen counts, verification pass/fail, human interventions, and the
   transcript/`impl.log` path. Export them from DuetFlow's control DB to
   `artifacts/graph-engineering/p12-pilots/duetflow.json` with a small script (`scripts/graph_pilot_export.py`,
   read-only on the DB). Leave unmeasurable values `null`. Do not convert to the Context Gate's JSONL schema.
5. **Re-measure retention** (the `owner: P12-pilots` thread): `.orchestrator` bytes per accepted task (task
   worktrees excluded), split into control DB + WAL, artifacts, attempts, transcripts. If real transcripts change
   the picture, revise `config.RETENTION_DEFAULTS` / `RETENTION_BASIS` and `project.yaml.tmpl` (TDD for the
   config change), then close the thread.
6. **ITV**, only after Dan reports grill-me done: onboard per §1 (guards in `project.yaml`, `runner: graph`),
   pick 3 to 5 representative tasks from its `PLAN.md` backlog with Dan's agreement (one AskUserQuestion,
   bundled), pin them in the manifest (step 1), run, export to `p12-pilots/instagram-to-value.json`. Confirm
   afterwards that `itv-bot` / `itv-worker` are still running and no file under the deny-listed dirs changed.
7. **Re-run qualification**: `.venv/bin/python -m pytest` (full suite) and
   `.venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends`
   (check `$?` directly). Read its `lanes` array alongside the pilot exports.
8. **Qualification verdict.** Paid subscription is qualified only if the pilot tasks reached accepted runs with
   raw outcomes retained and no unresolved maestro defect. Update the report's `modes.subscription.label`
   accordingly (the other modes stay unqualified). Every E1 to E5 row keeps its "stays off / as shipped,
   unqualified" decision unless the pilots produced a real measurement for it.
9. **Close P12 if and only if** steps 2 to 8 are done (ITV included, or Dan explicitly drops it): move P12 to
   `completed_phases`, `in_progress_details: null`, and add a `docs/PROGRESS.md` entry. Otherwise keep
   `in_progress` with `whats_left` naming what remains. Close with `close-session`.

## 4. Out of scope
The Context Gate (never in a graph phase). The failure-handling-phase threads (uncertain inline nodes; the
unimplemented `worktree_probe` / `human_receipt_probe` / `async_job_probe`): record only. Enabling candidates or
any E1 to E5 optimisation. Free, hybrid or script real runs.

## 5. Coordination and traps
- No other session is known to be writing maestro. DuetFlow has no running maestro loop at hand-off; check
  `tmux ls` and its systemd unit before starting one, and never run two loops on one project.
- The `agents` tmux session belongs to other work; do not kill windows you did not create.
- Pytest `addopts` already has `-q` (adding it silences the run). The qualification script's stdout ends in a
  pytest line, so read `$?` directly.
- Agent worktrees and uncommitted work go on persistent storage, not /tmp (memory). Commit early.
- Edit STATE.yaml with targeted replacements only (thread text is hard-wrapped).
- Budget: context-governor thresholds (handoff at 160K). Real runs take wall time; do not poll.

## 6. Report back (with numbers)
- Per project: tasks attempted / accepted / parked, rework attempts, human interventions, total wall time,
  tokens per accepted task (where measured), and every maestro defect found (one line + commit).
- Retention: bytes per accepted task (split), and the default kept or changed.
- Full suite `junit tests=… failures=0` + rc; qualification script rc.
- The subscription verdict, and P12's final STATE.
