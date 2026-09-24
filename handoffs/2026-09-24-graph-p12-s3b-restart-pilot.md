# Handoff: Graph P12, session 3c. Archive the stuck DuetFlow run, restart the pilot on fixed maestro, then close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-18-graph-p12-s3-pilots.md`, but that file's §1 (Dan's decisions), §3 steps 3–9
(the process from "every failure is a finding" to "close P12"), §4 (out of scope) and §6 (report back) still
apply unchanged. Read them there; they are not repeated here. `docs/graph-engineering/STATE.yaml` and `handoffs/`
are gitignored (edits live on disk only).

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1, §3 (steps 3–9), §4, §5, §6.
2. `docs/graph-engineering/STATE.yaml`: search `_p12_open_threads` (D1–D4 findings were added in s3b).
3. `artifacts/graph-engineering/p12-pilots/manifest.json` (use jq for `.amendments`, `.maestro` and `.projects.duetflow | del(.tasks)`).

## 1. What session 3b did (2026-09-24)
- **Pre-pilot addendum: done.** DuetFlow `98033e5`: 03 gains `duetflow playlists` + `duetflow use-source <n|name>`
  (V3 `use_source_registers`, offline) and a `kind: dan` V-DAN (Dan picks on Telegram, the first Dan check);
  06 creates the Duet itself when `duet.playlist_id` is unset (V4 `creates_duet_once`). Manifest re-pinned before
  any run: maestro `625bd4f`, DuetFlow `98033e5`, 31 checks (4 `kind: dan`), claude CLI 2.1.281.
- **Launched.** `setsid nohup bash launch.sh` in DuetFlow (tmux `duetflow-watchdog`; orchestrator in `agents:orchestrator`),
  then `maestro ctl resume`. Doctor was clean except the two known WARNs. Telegram is settled, so don't touch it.
- **Run 02 (`run_Qk52s47aQCwcU4Hm`)**: implementer on claude-sonnet-5, fresh, 61 s wall, tokens
  14 fresh / 247,367 cache-read / 34,507 cache-write / 5,194 output. The gate passed V1, V2, V3 and V-SUITE (76 tests).
  Proof review was routed to **Codex gpt-5.6-sol** and **rejected** the work with 4 real findings: the `collect` CLI is still a stub
  (06's V-DAN needs it), no 401/429/5xx recovery in `recently_played`, no `max_collection_gap_minutes` warning, and no `after`
  cursor. Its sandbox could not write DONE (D3 below), so the node settled `uncertain`, and the run is **stuck for good** (D4).
  Raw data so far: `artifacts/graph-engineering/p12-pilots/duetflow.json` (not committed yet; re-export later).
- **Maestro defects found and fixed (TDD, with a lane in `tests/graph_engineering/test_end_to_end.py`):**
  - `9b21e3e` D1/D2: real attempts had NULL `usage_start/settle` (production never wired `usage_reader`), and
    `agent_version` came from the register's *verified* version (2.1.270 while 2.1.281 ran). Now `_graph_routing` wires
    `routing.driver_usage` and `routing.observed_agent_version`.
  - `396e2cc` D3: the worker grants the attempt workspace to the agent via `CompletionSpec.add_dirs`. Codex's
    `workspace-write` sandbox could not write DONE outside the worktree.
  - D4 (recorded only, owned by the failure-handling phase): an `uncertain` agent node never settles, and no operator verb
    reaches graph nodes.
- `2e657ea`: `scripts/graph_pilot_export.py` (read-only; token split summed from the Claude transcript whose opening lines name
  the attempt workspace; unmeasurable values stay null). Codex transcripts are not parsed yet: only a total `tokens used N`
  appears at the tail of `impl.log`. Add that as `codex_tokens_total` if cheap, otherwise leave it null.
- Full suite at `9b21e3e`: junit tests=4780 failures=0 errors=0 skipped=1, rc 0. After `396e2cc`, only
  `tests/graph_engineering` was re-run (234 passed), so **re-run the full suite first**.

## 2. Dan's decision (2026-09-24). Do not re-ask
**Archive and restart** the stuck run. Don't build a new ctl verb.

## 3. In scope, in order
1. Full suite on maestro HEAD (`.venv/bin/python -m pytest --junitxml=...`, check rc). It must be green before adoption.
2. **Stop the loop cleanly:** `cd /home/dan/projects/duetflow && MAESTRO_REPO=$PWD .venv/bin/maestro ctl halt`. Confirm the
   `duetflow-watchdog` tmux session and the `agents:orchestrator` window are gone. Kill only windows this pilot created
   (the proof-review/implementer windows for 02, if any linger). Never kill other `agents` windows.
3. **Re-export first**, then archive: run `scripts/graph_pilot_export.py --project /home/dan/projects/duetflow --out
   artifacts/graph-engineering/p12-pilots/duetflow-run1-archived.json --since 2026-09-24T11:50:00Z`. Then move (not delete)
   `duetflow/.orchestrator/{control.sqlite3,-wal,-shm,attempts,artifacts,worktrees}` into
   `duetflow/.orchestrator/archive/2026-09-24-run1/`. Use `git worktree remove --force` for the 02 worktree first and
   then `git worktree prune`. Rename branch `impl/02-collector-schema` to `archive/02-run1`. Keep `journal.ndjson`
   and `state.json`. Check that state.json has no `in_flight` or graph-run references.
4. **Adopt the fixed maestro** the way session 3a did: `selfupdate.materialize_worktree(maestro_repo, HEAD)` then
   `selfupdate.adopt(sha, project_repo=/home/dan/projects/duetflow)`. Verify `duetflow/.orchestrator/current` names it, then
   run `doctor` (look for `adopted_code` at the new sha).
5. **Manifest amendment** (append to `.amendments`, commit): the code under test moves from dacb69d to <new sha> for D1–D3.
   02 is re-run from scratch because run 1 hung on D4 through no fault of the criteria. Criteria and weights are unchanged.
6. **Relaunch** (`setsid nohup bash launch.sh`, then `maestro ctl resume` if paused) and continue with step 2 of the old
   handoff's §3 onwards. Idle-polling memory applies: wait with a background `until` loop on the control DB (for example
   `task_runs.status != 'running'` or a new `dan_verify_requested` journal line), never with repeated sleeps.
7. Watch these on the restarted run: (a) whether proof_review now reaches accept or rework on Codex; (b) whether
   usage_start/settle and agent_version are populated (D1/D2 verified live); (c) routing ignores `project.yaml roles:`
   (judge=claude, but proof_review went to Codex under `objective=paid_efficiency`). Codex is also a paid subscription, so this is
   in-mode. Record it as a manifest note, not a defect, unless DESIGN says roles bind graph routing (check §16 quickly).
8. 03's V-DAN asks Dan to pick his playlist on Telegram. 06's V-DAN needs `duetflow collect`, which 02's rework must add
   (the reviewer already flagged it).

9. **Queued after the pilot. Do NOT do it in P12:** the `owner: model-catalog-config` thread in `_p12_open_threads`
   (Dan, 2026-09-24): make graph routing's model catalog editable config instead of hardcoded `catalog.py`, and handle new
   and retired models. When closing P12 (old handoff §3 step 9, `close-session`), name it as a next-task candidate
   alongside the failure-handling phase, and carry the thread into whatever records the next phase.

## 4. Coordination and traps
- No other session writes maestro. DuetFlow is at `98033e5` on `main` and nothing else touches it.
- The `grep -c` trick fails as a wait: `journal.ndjson` already has more than 6 lines. Wait on a specific new event.
- Budget: this session hit 160K on reading and diagnosis. Keep the next one lean: use the export script instead of hand SQL.
