# Handoff: Graph P12, session 3p. Archive and rerun 05, apply Dan's 03-followups decision, continue the pilot

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3o-pilot-continue.md`. Still apply: s3m §3.4 (the then-steps), §4 (traps),
§5 (report format); s3j §2, §3 item 3 (archive procedure), §4, §5; s3l §1, §3, §4. `docs/graph-engineering/` and
`handoffs/` are gitignored. **Talk to Dan in local time (IDT = UTC+3); journals and the DB are UTC.**

## 1. What s3o did (2026-09-27, 00:00–11:50 local)
- **Model refresh (Dan's order):**
  - The swaps: claude-opus-5 → **claude-opus-5-5**; codex gpt-5.6-sol → **gpt-6-sol**, gpt-5.6-terra → **gpt-6-luna**.
    gpt-6-astra and gpt-5.6-luna are unchanged.
  - maestro **6dc102e** covers: the catalog rows, the `opus` keyword, `JUDGE_MODEL`, the diagnoser default, the
    template and DESIGN.md.
  - Each catalog ceiling is the low end of the model's "normally start fresh" range in the live limits tables.
  - `claude-opus-5` stays as a keyword alias of `opus`.
  - The limits parser now reads a single-value cell as a range of one. The live `~/.claude/model_context_limits.md`
    was rewritten on 09-26 23:30: its Opus 5 row is gone, and it has a new `Model unknown | 100K | 120K | 150K` row.
  - DuetFlow `project.yaml` roles: **2e4cde1**. Manifest amendment: **ba1c3e2**.
  - gpt-6-sol was probed live and answered "OK".
- **D22 (found and fixed):** every agy run in the graph loop was classified `worker_crashed`, successes included.
  - Cause: the worker calls `complete()` (`--output-format json`, which logs ONE document), but
    `AntigravityBackend.parse_exit` read only the stream-json `result` event.
  - Fix: maestro **a9156ec** (`_completion_document` fallback, 2 tests). Replaying the real log gives `ok`.
  - Known limit: the worker passes only the last 64 KiB of `impl.log`.
  - Adopted into DuetFlow (current = a9156ec). Manifest amendment **14dbdb4**. Doctor: OK + the 2 known WARNs.
    Claude CLI 2.1.283 is `reobserved`.
- **Suite at a9156ec:** 5064 passed, 1 failed.
  - The failure: `test_cli.py::test_doctor_healthy_project_passes_once_a_real_test_adapter_exists` hits its 60 s
    subprocess timeout under host load. Load avg is ~6.5 from an AbuAliArchive eval run, and the suite took 18 min.
  - It failed 2 of 3 single reruns and passed once. `parse_exit` is not on the doctor path, so the failure is load,
    not the fix.
  - **Small gap:** re-run the test when the host is quiet. If it is still near 60 s, raise the timeout or find what
    slowed doctor.
- **Post-P12 feature queue documented** (Dan: "when the time comes tell me it's next and I'll elaborate"):
  - `specs/README.md` §9, and a closing note in the P12 spec;
  - STATE.yaml `_p12_open_threads` #1 (owner P12);
  - memory `post-p12-feature-queue`.
  - The queue, in order: (1) agy as a real harness, then (2) choose models by usage left.
  - **When closing P12, tell Dan it is next and ask him to elaborate on each item before any planning.**
- s3m branch commits re-verified in the s3n handoff §2 (all 5 match).
- `/help`: Dan confirms it works both halted and unhalted. The halted reply still writes no journal line (small gap,
  still open).

## 2. Current state (11:49 local): DuetFlow HALTED (`.orchestrator/HALT` present), nothing in flight
- **05-selector**: `run_S9DhgAMQyVtn0nqt` is failed; its park node is `claimed`.
  - Run 1 (`run_57eSjz3y3U2TPt0o`): 3 review rounds, then the diagnoser repaired it.
  - Revision 2: implementer_04 hit the Claude 5h cap. implementer_05 fell back to **agy gemini-3.8-flash-high**, which
    **succeeded**: commit `509ec9b` on `impl/05-selector`, 221 tests passing.
  - D22 recorded it as crashed. The repair budget (1) was already spent, so the run parked.
- **03-source-playlist-reader-followups**: `run_1pLN6DKpeFdWejHZ` is failed at proof_review (park `claimed`).
  - There were 5 review rounds (gpt-5.6-sol ×2, the opus-5 rate limit, then gpt-6-sol ×2). Every one asked for changes
    to `duetflow/config.py`'s text-level YAML editing (`set_source_playlist` / `_edited_text` / `_inline`).
  - The rounds escalate through rarer YAML shapes: quoted keys → flow mappings/aliases → anchors with block scalars →
    the `...` document-end marker. The task is "do or decline each follow-up; **no new scope**".
- 04-scoring-followups: queued, waits on the write overlap (app.py).
- `dep_map_render_failed` (mmdc CalledProcessError) appears twice in the journal. It is new; take a quick look
  (small gap).

## 3. In scope, in order
1. **03-followups: DECIDED by Dan (2026-09-27).**
   - Keep it on `hold: true` until the end-of-project polish stage. Commit that as a ROADMAP-only change in DuetFlow
     before relaunching.
   - When it runs, narrow it. `config.yaml` is DuetFlow's own file, and only `use-source` writes it. DuetFlow owns
     the file: it supports only the layout it writes, and anything else refuses with "edit config.yaml by hand"
     (a decline with a reason).
   - Add a safety check: re-load the result and verify that only the one value changed, or refuse. Record it as a
     manifest amendment (a criterion clarification) when it runs.
   - Record **D23 (maestro)**: a follow-up reviewer has no bound on edge-case scope. Do not build anything in P12.
   - The post-P12 queue grew: follow-ups move to a polish stage, and the `project.yaml` redesign is part of the
     model-choice item (specs/README.md §9).
2. **Archive and rerun 05** (s3j §3 item 3):
   1. Export first: `scripts/graph_pilot_export.py --project /home/dan/projects/duetflow --out
      artifacts/graph-engineering/p12-pilots/duetflow-run9-archived.json --since <run 8 archive time>`. Check how
      runs 7 and 8 chose `--since`.
   2. `git branch -m impl/05-selector archive/05-selector-run1` (it holds agy's 509ec9b). Also rename
      `impl/03-source-playlist-reader-followups`.
   3. Move `control.sqlite3*`, `attempts`, `artifacts` and a HALT copy into
      `.orchestrator/archive/2026-09-27-run9`. Keep a HALT in place.
   4. Commit the export.
   5. Then remove HALT: the watchdog relaunches the loop within ~30 s.
   - Note that the archive moves the whole control DB, so 03-followups' parked run is archived too. That is fine
     under every option above.
3. Continue per s3m §3.4: 04-followups, then 05+ follow-ups; 06 collect + the first live V-DAN (numbered steps);
   the final export with contamination; retention re-measure, qualification re-run, verdict, close P12 (+ tell Dan
   about the §9 queue).
4. Small gaps: halted `/help` should journal; the worktree-only project.yaml test-isolation bug; the mmdc render
   failure; the doctor-test timeout.

## 4. Traps
- s3m §4 still applies. `maestro status`/`ctl` take the controller lock: while the loop runs, read the journal and
  the DB read-only (`sqlite3 file:...?mode=ro`) instead.
- The watchdog stays up through HALT (a bot is configured), so removing HALT relaunches the loop.
- To halt at an accept, `$CLAUDE_JOB_DIR/tmp/watch_accept.sh` was used: it tails the journal for `task_complete`
  or a failed settle, then touches HALT. There is a ~35 s window before the next launch. Recreate it if the job dir
  is gone.
- The host is loaded, so the full suite takes ~18 min. Run it in the background with a long timeout.
- Hand off again at ~180K context (Opus 5.5).

## 5. Report back
As s3m §5.
