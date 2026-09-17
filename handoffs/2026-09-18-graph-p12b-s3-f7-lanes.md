# P12B S3 — F7's lane tests, then F8 and the close-out

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-18-graph-p12b-s3-f7-lanes.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). No parallel session is active on this branch.

## Where the previous session stopped

**F7's implementation is written, green and committed** as `faead85`
(`feat(graph): P12B F7 — a third bad output escalates to a stronger diagnoser`), on top of
`b8ef073`. The tree is clean.

- **Gate: tests=1214 failures=0 skipped=0. Full: tests=4704 failures=0 skipped=1.**
  (Baseline at `b8ef073` was gate 1207 / full 4697; the +7 are F7's units.)
- What is covered by tests today: the escalated compile (shapes for ordinary / script /
  resumable / async, the version stamping, the "no edge re-targeted → no node" case) in
  `tests/workflows/test_compiler.py`; the verdict round-trip and its refusals in
  `tests/workflows/test_plan_repair.py`; `resolve_agent(min_strength_floor=)` in
  `tests/backends/test_router.py`.
- **What is owed: F7's seven lane tests under `run_lane`.** Nothing else. That is job 1.

Two pre-existing lane tests were updated in `faead85`, deliberately and with comments:
`test_a_node_larger_than_the_whole_host_fails_to_park_instead_of_waiting`
(`test_dispatch_wiring.py`) and
`test_an_unknown_reset_parks_the_node_once_the_blocked_wait_is_exceeded`
(`test_failure_ladder.py`). Both asserted a refused agent reaching `park` directly; under
F7 the graph loop compiles escalated, so that edge now goes by way of `diagnose`. This is
the intended shape change, not a regression — do not "fix" them back.

## Read first (binding, in this order)

1. `handoffs/2026-09-17-graph-p12b-s3-design.md` §F7 tests (lines 244–258) — the seven
   lanes, **binding**. §F8 (264–289) and §"Close-out after F8" (322–325) follow it.
   §"Invariants" (293–308) applies to all of it.
2. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` §3's F7 acceptance
   bullets (141–147), §4 invariants (158–165).
3. §"What F7 actually built" and §"The lane test that did not land" below, in place of
   re-reading the implementation. `git show faead85` is the whole change if you need it.

`handoffs/2026-09-18-graph-p12b-s3-f7-build.md` is this file's predecessor. Its §"Test
harness facts" (lines 210–250) is **still accurate and still worth reading** before you
write a lane test; its §"Code survey" describes the code *before* `faead85`, so prefer the
section below where the two disagree.

---

## Job 1 — F7's seven lane tests

From §F7 tests of the design contract. In `tests/graph_engineering/test_failure_ladder.py`,
appended as a new `# ── P12B F7` section. **Commit them alone** when green (F7's
implementation is already committed; this is its test commit, like `b8ef073` was F6's).

1. After the 3rd `bad_output`, the next poll dispatches `diagnose` on a route one rung
   stronger than the failed model (balanced implementer → strong diagnoser).
2. `free_quality` with no stronger free route and a hybrid overlay granting paid:
   `diagnose` fails with a `route_` reason, the task parks, no paid route bound.
3. `poor_agent` proposal: revision N+1's implementer carries the enhanced `params`; its
   next `bad_output` → `park` with no retry and no second `diagnose` launch (spy).
4. `bad_graph` proposal: the same through a graph patch.
5. `diagnose` with no proposal → park; and with a proposal `apply_repair` refuses →
   `diagnose` `failed`, park reached.
6. The run does not settle while `diagnose` is live.

(That is six; the contract's seventh is the compile-shape lane, which is already covered by
the units in `test_compiler.py` — `test_escalation_re_points_agent_and_gate_failures_at_diagnose`
and friends. Confirm you agree before writing a seventh.)

**How to give the diagnoser an answer.** Do not run a real diagnoser worker. Use
`_answer_with_a_proposal(node_id, after_answer)` in `tests/graph_engineering/test_dispatch_wiring.py`
(**:1493**): it writes a `NodeResult` with `outputs={"plan_repair": …}` straight into the
live attempt's inbox plus a receipt, which is exactly how P12A's W7 planner lane works
(`test_a_planner_proposal_moves_the_loop_to_revision_n_plus_one`, **:1531**). Pass
`node_id="diagnose"` and build the proposal with a `verdict=`. `_runs_with_revisions()`
(**:1522**) is how tests 3–5 see revision N+1 appear.

**Prove the tests aren't vacuous** — F6's were checked by mutating `QUALITY_ATTEMPTS` to 1
and confirming all nine failed. For F7: compile with `escalation=False` and confirm the
escalation lanes fail; delete the `min_strength_floor` block in `router.resolve_agent` and
confirm test 1 fails. Say so in the commit message.

### The lane test that did not land, and why

Test 1 was written and run. It failed with `LoopRanTooLong: lane 'f7_escalates' ran 13 poll
cycles without finishing (limit 12)` — the halt predicate (`a diagnose attempt row exists`)
never fired within `MAX_POLLS`.

**The most likely cause, unverified:** the implementer bound the *strong* route rather than
the balanced one. The scenario patched `fixture_cli` (strong) with a `_GOOD` driver and
`fixture_cli_b` (balanced) with `_EMPTY`, assuming `paid_efficiency` would pick the cheaper
balanced route when no role demands `strong`. If it picked strong instead, the implementer
answered *well*, went to the gate, and the lane simply ran on to the poll limit — which is
consistent with the symptom (no `diagnose` row, no failure, just polls).

**Check this first, cheaply, before touching the scenario:** resolve the implementer once
against `_escalation_routing()` in a unit-style test (or add a one-line print in the lane)
and see which `(backend_id, model_id)` it binds. If it is `STRONG`, either swap the two
drivers or force the balanced binding (grade `fixture_cli`'s model `strong` *and* give the
implementer role a `balanced` ceiling, or exclude the strong route for the implementer).
`STRONG = ("fixture_cli", "fixture-1")` and `BALANCED = ("fixture_cli_b", "fixture-1")` are
already defined at `test_failure_ladder.py:600–601`.

**Also budget the polls before writing each lane.** Three implementer attempts alone cost
several polls of the twelve. If a lane genuinely cannot fit, prefer seeding durable state
in `before_run` (the way `age_the_first_block` seeds a blocked node at
`test_failure_ladder.py:747`) over raising `MAX_POLLS`.

### The parked work, verbatim

`.scratch/p12b/f7_lane_wip.py` holds it, but `.scratch/` is not durable — the copy below is
authoritative. The three helpers are good and were not the cause of the failure; the
`_relaxed_diagnoser` one in particular is **load-bearing for non-vacuity** (the shipped
`diagnoser` role already declares `min_reasoning_strength: strong`, so a lane that just
asserts "the diagnoser bound a strong model" passes with F7's floor deleted).

```python
# ── P12B F7: the third failure escalates to a stronger diagnoser ─────────────────────
#
# The lane is longer than F6's — three bad answers, then `diagnose`, then possibly a
# repaired run — so every halt below is on a durable condition (an attempt row exists, a
# second run exists) and the polls are counted against `MAX_POLLS` (12) before the
# scenario is written.


def _relaxed_diagnoser(policy):
    """`policy` with the diagnoser's own `strong` floor lowered out of the way.

    The shipped role already demands `strong`, so a lane asserting "the diagnoser bound a
    strong model" would pass with F7's floor deleted. Lowering the declared floor here is
    what leaves the escalation as the only thing that can raise it.
    """
    definitions = dict(policy.agent_definitions)
    role = definitions["diagnoser"]
    definitions["diagnoser"] = dataclasses.replace(
        role, permitted_backend_capabilities={
            **role.permitted_backend_capabilities, "min_reasoning_strength": "light",
        },
    )
    return dataclasses.replace(policy, agent_definitions=definitions)


def _escalation_routing(policy=None, *, billing="subscription"):
    """A `strong` route and a `balanced` one, with no role demanding either."""
    from maestro.backends import catalog as cat

    policy = _relaxed_diagnoser(policy if policy is not None else default_policy())
    return fixture_routing(policy=policy, entries=[
        fixture_cli_entry(
            billing_category=billing,
            model_profiles={"fixture-1": cat.ModelProfile("fixture-1", strength="strong")},
        ),
        fixture_cli_entry(
            backend_id="fixture_cli_b", usage_pool_id="fixture-pool-b",
            billing_category=billing,
        ),
    ])


def _rows_for(node_id: str) -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, status, backend_id, model_id, route_refusal, failure_kind,"
        " binding_json FROM attempts WHERE node_id = ? ORDER BY rowid", (node_id,)
    ).fetchall()


def test_the_third_bad_output_escalates_to_a_model_one_rung_stronger(
    run_lane, monkeypatch, tmp_path
):
    _patch_two_drivers(monkeypatch, _ScriptedCliDriver(_GOOD), _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f7_escalates", routing=_escalation_routing,
        on_poll=_drive_implementer(
            lambda: bool(_attempts_by_node("diagnose")), worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    implementer = _rows_for("implementer")
    assert len(implementer) == 3, "the first run plus F6's two retries"
    assert {(r["backend_id"], r["model_id"]) for r in implementer} == {BALANCED}
    assert _node_phase("implementer") == "failed"

    [diagnose] = _rows_for("diagnose")
    assert (diagnose["backend_id"], diagnose["model_id"]) == STRONG, \
        "the diagnoser is escalated a rung above the model that failed"
    assert "raised to strong" in json.loads(diagnose["binding_json"])["selection_reason"]
```

---

## Job 2 — F8, from §F8 of the design contract

Only after F7's lanes are green and committed. §F8 is small and unchanged:
`_decide_dead_writers(at)` in `tick` right after `reconcile`, under routing only, reading
durable state rather than the reconcile report; candidates go to `failed` + `record_failure`
with no `outcome_ref`, other writers take F6's path. Three lane tests. **Commit F8 alone.**

`candidate_documents` is at `runner.py` (search `def candidate_documents`); F8's candidates
rely on its `WHERE ... outcome_ref IS NOT NULL`.

## Job 3 — the close-out, from §"Close-out after F8"

Close the three `_p12b_open_threads` (disposition `closed` + a note naming the commit),
check `owned_threads(state, "P12B") == 0`, record P12B in STATE `completed_phases` /
`phase_records`, reset `in_progress_details` to null per its comment, and move
`current_phase` on to P12 — `scripts/next_graph_prompt.py` should then print P12.

**Note for the close-out:** thread #2 (P12A `apply_repair`'s known limit) is closed by
`faead85` — the diagnoser override. Thread #1 (dead writers) is closed by F8.

---

## What F7 actually built (`faead85`), so you need not re-read it

### Compiler (`maestro/workflows/compiler.py`)
- `COMPILER_VERSION = "p12b.1"`, `BASELINE_COMPILER_VERSION = "p7.1"`; `compile_workflow`
  stamps `COMPILER_VERSION if escalation else BASELINE_COMPILER_VERSION`. Re-exported from
  `maestro/workflows/__init__.py`.
- `_diagnose_node` (shaped on `_planner_node`: `frozen_snapshot_reader`, no writer lease,
  `inference_slots: 1`, `cache_policy="never"`, `output_schema_ref="PlanRepairProposal.v1"`),
  and `_fragment("diagnose", "1.0.0", _diagnose_node)`.
- `escalate(spec, nodes, edges, *, policy=None)` — a module-level function called from
  `compile_workflow` **after** `compose` and **before** `check_criteria`. Re-points every
  `on_failure → park` edge whose source satisfies `_escalates` (`kind == "agent"` or
  `handler_ref == "maestro.handlers.verification"`), appends `diagnose` and its own
  `on_failure → park` (`required=False`), and returns the inputs unchanged when nothing
  moved. `DIAGNOSE_NODE_ID = "diagnose"`.
- `VERDICTS = frozenset({"bad_graph", "poor_agent"})`; `PlanRepairProposal.verdict:
  str | None = None`, refused in `__post_init__`, emitted by `to_dict` **only when set**,
  read by `from_dict`.
- **Note the real field names**: `EdgeSpec.source_node_id` / `target_node_id`, not
  `source`/`target` (this cost a round trip).
- `maestro/orchestrator.py:~2184` is the only caller passing `escalation=True`.

### Router / routing
- `router.resolve_agent(..., min_strength_floor=None)` — applied right after
  `strength_offset`, and **only raises** (`if floor_rank > demand.strength_rank`), with
  `; raised to <rung>` appended to `demand.reason`.
- `routing.resolve(..., strength_floor=None, billing_only=False)` — both fold into a `base`
  dict applied to *every* pass of the existing `attempt()` helper, so the floor cannot leak
  away on a soft-exclude fallback. `billing_only` uses `router.OBJECTIVE_BILLING[objective]`
  (no hybrid grant). Still one `resolve_agent` per binding decision.

### Runner (`maestro/workflows/runner.py`)
- `Claim.failure_context_artifact_id`; `--failure-context-artifact-id` appended in
  `_default_worker_argv` and read in `worker.py` into `context.inputs["failure_context"]`.
- `_advance_agent` sets `diagnoser = node.agent_definition_ref == wf_failures.ROLE_DIAGNOSER`
  once, then: refuses on `_repair_budget_exhausted()` **before** the manifest; passes
  `soft_exclude=frozenset()`, `strength_floor=self._escalated_strength(node)`,
  `billing_only=True`; and calls `_attach_failure_context` after `session_decision`. One
  path, one `resolve`, one `launch_worker`.
- New helpers, all in one block above `_soft_exclude`: `_failing_node` (durable phases +
  topological order), `_last_binding`, `_escalated_strength` (catalog profile → one rung up,
  clamped at the top), `_failure_context`, `_latest_verification_evidence`,
  `_attach_failure_context`, `_repair_budget_exhausted`. Module-level `_loads` near
  `_git_head`.
- `_commit_result`: `_verdict_refusal(node, proposal)` runs **before** `_plan_repair`, and
  a refusal sets `phase = "failed"` and records `WorkflowRepairRefused`. `_apply_repair`'s
  refusal path now also calls `_fail_refused_diagnoser` (one transaction:
  `override_phase(succeeded → failed)` + `record_failure(garbage_output)`); it is a no-op
  for a planner and without routing. The `_apply_repair` docstring was rewritten.
- `settle`: under routing only, returns `None` when `_still_working(phases)` — any node
  `claimed`/`running` or a non-empty `ready_frontier`. `routing is None` is unchanged.
- `_select_affected_checks` and `_advance_agent` both pass
  `workflow_id=self.revision.workflow_id` to `stepped_down_attempts`.

### Lifecycle
- `stepped_down_attempts(run_id, node_id=None, *, workflow_id=None)` — with `workflow_id`
  it counts across every run of that workflow via a `task_runs` subselect.

## Invariants (binding)

One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`) and one
fresh-vs-resume decision, with no Context Gate. New behaviour only when
`routing is not None`. `tests/workflows/test_compatibility.py` stays unchanged and an
unescalated compile stays byte-identical. Don't edit `NODE_TRANSITIONS`. The diagnoser never
uses the hybrid overlay (Dan, 2026-09-17). Leave `P12A_*`, `AGENTS.md` and `GEMINI.md`
alone. Scratch files go in `.scratch/`. A PreToolUse hook rejects Bash text containing the
privileged word. pytest's summary line is suppressed, so read the counts off the junit root
`testsuite`. Commits end with
`Co-Authored-By: <model actually running> <noreply@anthropic.com>`. STATE: targeted
replacements only. If a step can only be built by bypassing the choke point or adding a
second resume decision, stop and ask Dan (spec §4).

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py tests/control tests/test_confinement.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; run in background, check xml mtime
```

Baseline at `faead85`: **gate tests=1214 failures=0 skipped=0; full tests=4704 failures=0
skipped=1.** Report per commit: hash, gate and full junit counts.

## Budget

F7's lanes are the expensive part of what is left — six `run_lane` scenarios against a
twelve-poll limit, and the first one already resisted. Measure with
`python3 ~/.claude/skills/close-session/scripts/context_usage.py` after each commit; for
Opus 5, write a handoff at 160K and start fresh by 200K. Landing the lanes alone is a fine
session; hand off F8 + the close-out rather than starting them tired.
