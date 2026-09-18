# P12B S4 — F7's missing consumer, F8, then the close-out

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-18-graph-p12b-s4-f8-closeout.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). No parallel session is active on this branch.

## Where the previous session stopped

**F7's ladder is finished and proven; one half of F7's *inputs* is not built.** Tree clean at
`a329ddf`. See §"Job 0" — it must land before the close-out records P12B as complete.

- `faead85` — F7's implementation (previous session).
- `903ff60` — `fix(graph): the failure context reads a column `attempts` has`.
- `a329ddf` — `test(graph): the escalation proven under six real lanes`.

**Baseline at `a329ddf`: gate tests=1221 failures=0 skipped=0; full tests=4711 failures=0
skipped=1, exit 0.** (Was gate 1214 / full 4704 at `faead85`; the +7 are F7's six lanes plus
the failure-context assertion split out of lane 1.)

Jobs 2 and 3 of the previous handoff are jobs 1 and 2 here, behind a new job 0.

### The bug F7's first real lane found, so you don't rediscover its shape

`_failure_context` selected `attempt_index` from `attempts`, a column that table has never
had. No existing test reached it: the shipped `diagnoser` role declares
`min_reasoning_strength: strong`, so in every lane before F7's the diagnoser was refused a
route *before* the claim, and `_attach_failure_context` never ran. The first lane that
actually bound a diagnoser hit `sqlite3.OperationalError` inside `advance`, which the event
loop's `except Exception` swallowed — leaving the attempt `claimed` with no binding and the
lane polling to `MAX_POLLS`. The symptom was `LoopRanTooLong` with no failure anywhere.

**The lesson worth carrying into F8:** a lane that stalls at the poll limit with nothing
failed is very often an exception swallowed inside `advance` or `ingest_results`. Wrap the
method under suspicion in a `monkeypatch.setattr` that prints `traceback.print_exc()` and
re-raises; it costs one run and names the line. Guessing at the scenario's routing cost the
previous session a whole session.

## Read first (binding, in this order)

1. `handoffs/2026-09-17-graph-p12b-s3-design.md` §F8 (264–289) — **binding**; §"Invariants"
   (293–308) and §"Close-out after F8" (322–325) apply.
2. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` §"F8" (115–117) and
   its F8 acceptance bullet (148); §4 invariants (158–165).
3. §"Test harness facts" of `handoffs/2026-09-18-graph-p12b-s3-f7-build.md` (lines 210–250) —
   **still accurate**, still the fastest way to write a lane test that works first time.
4. §"F7's lane helpers, and which of them F8 wants" below.

---

## Job 0 — the failure context reaches the model (do this first)

**Found at the end of the F7 session, by asking what else the diagnoser's unexercised paths
hid. Verified, not suspected.**

The failure context is assembled, stored, flagged onto the worker argv and parsed by the
worker — and then nothing reads it:

```
runner._failure_context          → builds the document          ✔ (lane-tested)
runner._attach_failure_context   → artifact + _attempt_inputs   ✔ (lane-tested)
runner._default_worker_argv      → --failure-context-artifact-id ✔
worker.py:238–242                → context.inputs["failure_context"] ✔
         ...and no consumer anywhere.                            ✘
```

`grep -rn 'failure_context' maestro/` returns only the producers. The brief the agent actually
sees is `worker.brief_from_manifest(manifest)` — `mandatory_instructions`,
`acceptance_criteria`, `entrypoints`, all from the **context manifest**. `ctx.inputs` is read
by handlers for `affected_selection` (`handlers.py:631`), `specs`, `branch` and `candidates`;
`failure_context` is never among them, and nothing writes it into the attempt workspace
either.

**So F7 today escalates to a stronger model and asks it to explain a failure without telling
it what failed.** Every test passes, because no test ran a diagnoser worker: F7's lane tests
write the diagnoser's answer straight into the attempt inbox (the P12A W7 pattern), which is
right for testing the controller's decision and blind to this.

This is spec §3 line 106, unmet: *"**Inputs.** `diagnose` is an agent node with role
`diagnoser`. Its inputs are the failing node's spec, its brief, every attempt's failure record
and evidence, and the verification output."*

### Where it goes — decided, don't re-open

**Into the brief the agent is given**, i.e. the text `worker.brief_from_manifest` produces,
extended with a rendered failure-context section when `context.inputs["failure_context"]` is
present. The two alternatives are ruled out on evidence, so that they are not re-litigated:

- **Not the context manifest.** `routing.build_manifest` is a pure function of
  `(task, role, code_base_sha, policy)`. Its content feeds `router.RoutingContext.from_manifest`
  and the statistics `shape_key`, so run-specific failure text in it would move the routing
  demand and pollute per-route success statistics. F7 was right to keep the failure context as
  its own artifact.
- **Not a file in the attempt workspace.** The diagnoser is `frozen_snapshot_reader` with
  `writer_lease: False`, so it has no worktree, and `model_runtime.ToolBox` confines every read
  to a `CommitBoundary`. The file would be unreachable on the `model_endpoint_harness` path and
  merely *optional* on the `agent_cli` one — optional delivery is the same failure mode as the
  present one, one notch softer.

**Rename `brief_from_manifest` as part of this — decided by Dan, 2026-09-18.** Once it also
renders the failure context it is no longer a function of the manifest, and a name that lies
about its inputs is exactly the kind of thing that hides the next gap like this one. Call it
`agent_brief(manifest, *, failure_context=None)`, or another name that says "the brief the
agent is given" rather than "the brief derived from the manifest".

It is a three-site rename — `maestro/workflows/worker.py:91` (definition),
`worker.py:249` (the one caller) and `tests/graph_engineering/test_dispatch_wiring.py:407`
(which asserts the brief the driver saw equals this function's output, and is worth keeping
exactly that way). Do the rename in the same commit as the consumer; a rename commit on its
own would leave a name mid-migration for no benefit.

### What gets bounded — Dan's decision, 2026-09-18

**Verbose verification evidence only.** Everything else goes in **full**:

- the failing node's spec — full;
- its latest brief — full;
- every attempt's **binding** — full;
- every attempt's **structured failure record** — full.

These are small and structured, and they are the part a diagnosis actually reasons from.

**The bound on verification evidence:**

- an **aggregate** cap of **~16k tokens**, not per-row;
- denominated with **`maestro.repository.context.estimate_tokens`** — the same estimator the
  context manifest's own `context_estimate_tokens` budget uses, so the two budgets are in one
  unit and there is no chars-per-token guess to maintain;
- **failed checks first**: spend the budget on the checks that failed before any that passed;
- **head *and* tail excerpts** of a long row, not a head-only cut — the useful lines cluster at
  both ends of check output;
- **explicit truncation markers** in the text, so the model knows it is reading an excerpt and
  does not reason from a tail as if it were the whole;
- the cap is **an initial value to be tuned**. Put it where it can be changed without touching
  logic — a named module constant at minimum, `project.yaml engineering.*` if that is cheap —
  and state it in the spec.

**Telemetry Dan asked for explicitly, and the reason for it:** record, per diagnosis, **the
character count of the full failure context and of the rendered/truncated one**. He wants real
numbers before deciding whether ~16k is right, so the cap can be re-evaluated against usage
rather than argued about. Both numbers must be **durable and queryable per diagnosis** — the
`failure_context` artifact document already exists and is attributable to the attempt, so a
small `sizes` block in it is the obvious home; a journal line or event payload is fine as well
if it is easier to query. Whatever the mechanism, the requirement is that after a few real
diagnoses Dan can read off "full vs delivered" without re-deriving it.

### What the test must be

A lane that runs a **real diagnoser worker** against a scripted `agent_cli` driver and asserts
the failing node's id, its `empty_output` records and the verification evidence are in **the
text the driver was handed**. Nothing weaker proves it: the whole gap is that the plumbing is
green and the model is blind, so the assertion has to be on the prompt, not on a row.

- `_escalation_routing` in `test_failure_ladder.py` is the routing to reuse — it is the only
  routing in the suite under which a diagnoser actually binds.
- `_ScriptedCliDriver.complete(spec)` receives the spec, so record it there and assert on it.
- The lane needs a **gate that really ran**, or `_latest_verification_evidence` returns `[]`
  and the evidence half of the assertion is vacuous. `test_a_failing_gate_re_runs_the_writer_and_then_the_check`
  (F6) and `_run_the_gate_worker` in `test_dispatch_wiring.py` show how to get a real gate
  verdict into a lane; budget the polls, since that lane plus three bad answers plus a
  diagnosis will not fit in `MAX_POLLS` without `_one_bad_output`.
- Add a second, cheap test for the bound itself: a synthetic failure context with evidence far
  over the cap, rendered directly (no lane), asserting the failed check survives, a passed
  check is dropped or cut first, head and tail are both present, the marker is there, and the
  estimate is under the cap. A unit is the right shape for that — it is arithmetic, not
  orchestration.

**Also unexercised, and the reason the lane above needs a real gate:**
`runner._latest_verification_evidence`'s non-empty branch. Every F7 lane leaves the gate
`skipped`, so it has only ever returned `[]`. Its SQL is `SELECT *`, so it cannot carry the
bug `903ff60` fixed — but "the verification output" is half of what spec line 106 promises the
diagnoser, it is the *only* part being truncated, and no test has yet seen a single evidence
row arrive.

### One thread to open, not to fix here

An unexpected exception inside `advance` is swallowed by the event loop's `except Exception`
and leaves the node `claimed` with no durable record and no failure — which is exactly how a
one-line SQL typo became a lane that polled to `MAX_POLLS` with nothing failed. That is
pre-existing loop behaviour and outside P12B's scope; record it as an open thread in the
close-out rather than fixing it inside F8.

### A sweep already done, so you need not repeat it

Every SQL statement F7 added was prepared against a real control schema (`EXPLAIN`, all
columns): the `attempt_index` typo was the only bad one. F8's likely queries
(`node_states.last_attempt_id`, `attempts.outcome_ref`) were checked at the same time and are
valid.

---

## Job 1 — F8: dead writers reach a decision

From §F8 of the design contract, unchanged:

`WorkflowRunner._decide_dead_writers(at)`, called in `tick` **right after `reconcile`**,
**under routing only**. It reads durable state, not the reconcile report, so a second
controller process decides the same thing: `node_states` of this run in `claimed`/`running`
whose `last_attempt_id`'s attempt is `uncertain` or `failed` and has **no receipt on disk**.
For each, `record = FailureRecord(kind="worker_crashed", evidence=<reconcile detail or
"writer died with no receipt">)`, then:

- **candidate** (`params.candidate_id` set and the node has no `on_failure` edge, i.e. it is
  reaped by the join) → `set_node_phase(→ failed)` + `record_failure`, **one transaction**,
  **no `outcome_ref`** — so `candidate_documents` gives it no document and the join reports it
  `abandoned`. The attempt keeps `uncertain` (A05's record stays truthful).
- **non-candidate writer** → F6's path (`requeue_cone` or `fail_quality`) with that record;
  the retry restores `start_sha` at claim.

`reconcile.py` is **not** changed. Routing-None runs never call the new method.

`candidate_documents` is in `runner.py` (`grep -n "def candidate_documents"`); F8's candidate
half relies on its `WHERE ... outcome_ref IS NOT NULL`.

### F8's three lane tests

1. 3-candidate run; c3's writer "killed" (fixture `is_alive` false for its PID, no receipt) →
   `candidate_select` runs, exactly one winner, c3 reported `abandoned`; **no failed launch
   stands in for the kill**. Reuse the P12A W4 candidate lane test's harness.
2. Killed non-candidate implementer → retried once from `start_sha`
   (`quality_failures == 1`, `failure_kind == "worker_crashed"`).
3. `routing is None`: the same kill leaves the attempt `uncertain` and the node `running`.

**Commit F8 alone** when green, and say in the message how you proved the lanes aren't
vacuous (F6 mutated `QUALITY_ATTEMPTS` to 1; F7 compiled with `escalation=False` and disabled
the `min_strength_floor` block — for F8 the obvious mutation is to make `is_alive` return
`True` and confirm the lanes fail).

### F7's lane helpers, and which of them F8 wants

All at the end of `tests/graph_engineering/test_failure_ladder.py`, under
`# ── P12B F7`. Reuse rather than rebuild:

- **`_one_bad_output(monkeypatch)`** — sets `wf_failures.QUALITY_ATTEMPTS = 1`, so the whole
  quality budget goes on the first bad answer. This is what makes a lane with a repaired run
  fit inside `MAX_POLLS` (12). F8 lane 2 wants it if the lane runs long.
- **`_run_newest_implementer(run, worktree)`** — runs the newest live implementer worker of
  *whichever run owns it* (survives a repair). The piece `_drive_implementer` hides.
- **`_rows_for(node_id)`** / **`_bound(row)`** — attempt rows of any node, and its
  `(backend_id, model_id)`.
- **`_failure_context(run, attempt_id)`** — reads a `failure_context` artifact back through
  the `ArtifactCommitted` event (the `artifacts` table has **no** `producer_attempt_id`
  column; the event carries the attempt id).
- `_escalation_routing` / `_relaxed_diagnoser` / `_balanced_implementer` /
  `_with_capabilities` — F7's routing shape. `_with_capabilities(policy, role, **caps)` is
  general and worth reusing for any role floor.
- `_answer_the_diagnoser(build, halt_when, *, worktree)` — writes a diagnoser's result into
  the live attempt's inbox. F8 does not need it, but it is the pattern for answering *any*
  node without running its worker.

Two facts these cost:
- **`available_models=`, not just `model_profiles=`.** Holding a role to one route means
  giving the two entries **different model ids** (`fixture-strong` vs `fixture-1`) and naming
  the permitted one on the role (`permitted_backend_capabilities["models"]`, read by the
  router as `permitted.get("models")`). Grading strength alone is not enough, because F6's
  second retry will happily move a writer onto the other route.
- **`_attempts_by_node(node_id)` selects only `attempt_id, status`.** Indexing anything else
  off its rows raises `IndexError: No item with that key`. Use `_rows_for`.

## Job 2 — the close-out, from §"Close-out after F8"

**Do not start this until job 0 has landed** — P12B is not complete while spec line 106 is
unmet, and recording it as complete is the one step here that is awkward to walk back.

Close the three `_p12b_open_threads` (disposition `closed` + a note naming the commit), check
`owned_threads(state, "P12B") == 0`, record P12B in STATE `completed_phases` /
`phase_records`, reset `in_progress_details` to null per its comment, and move
`current_phase` on to P12 — `scripts/next_graph_prompt.py` should then print P12.

- Thread #1 (dead writers) is closed by F8.
- Thread #2 (P12A `apply_repair`'s known limit) is closed by `faead85`, and
  `test_a_repair_refused_after_the_fact_still_fails_the_diagnoser_and_parks`
  (`test_failure_ladder.py`) is its proof.
- Thread #3: read it and decide; the previous handoffs do not name what closes it.

STATE: **targeted replacements only.**

## Invariants (binding)

One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`) and one
fresh-vs-resume decision, with no Context Gate. New behaviour only when
`routing is not None`. `tests/workflows/test_compatibility.py` stays unchanged and an
unescalated compile stays byte-identical. Don't edit `NODE_TRANSITIONS`. The diagnoser never
uses the hybrid overlay (Dan, 2026-09-17). Leave `P12A_*`, `AGENTS.md` and `GEMINI.md` alone.
Scratch files go in `.scratch/`. A PreToolUse hook rejects Bash text containing the
privileged word. pytest's summary line is suppressed, so read the counts off the junit root
`testsuite`. Commits end with
`Co-Authored-By: <model actually running> <noreply@anthropic.com>`. If a step can only be
built by bypassing the choke point or adding a second resume decision, stop and ask Dan
(spec §4).

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py tests/control tests/test_confinement.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; ~235s, run in background
```
```bash
python3 -c "import xml.etree.ElementTree as ET; print([s.attrib for s in ET.parse('.scratch/p12b/gate.xml').getroot()])"
```

Baseline at `a329ddf`: **gate tests=1221 failures=0 skipped=0; full tests=4711 failures=0
skipped=1.** Report per commit: hash, gate and full junit counts.

## Budget

Job 0 is one consumer, a bounded renderer with its own unit test, size telemetry, a spec
update, and one lane that runs a real diagnoser worker against a real gate. Treat it as the
session's main piece rather than a warm-up. F8 is three lanes and one method — smaller than F7 by a good margin. Job 0 plus F8 in one session is realistic; the
close-out is small and can follow in either. Measure with
`python3 ~/.claude/skills/close-session/scripts/context_usage.py` after each commit; for
Opus 5, write a handoff at 160K and start fresh by 200K.
