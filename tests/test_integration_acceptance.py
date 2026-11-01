"""`[P4]`: the 5-stage staging pipeline, against a real git repository.

These tests use real git on a temporary repository rather than a recording double,
because every claim they make is a claim about what git actually did: that a conflict
aborts, that a compare-and-swap refuses a ref that moved, that a failed check leaves
`refs/heads/main` byte-for-byte where it was. A fake `subprocess.run` would let the
implementation be wrong in exactly the ways that matter and still pass.
"""
from __future__ import annotations

import subprocess

import pytest

from maestro import validation
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import ControlStore


# ── a real repository ────────────────────────────────────────────────────────────────


def run(args, cwd):
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path):
    """A repository with one commit on `main` and an identity git will accept."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".orchestrator").mkdir()
    run(["git", "init", "-q", "-b", "main"], root)
    run(["git", "config", "user.email", "test@example.invalid"], root)
    run(["git", "config", "user.name", "P4 test"], root)
    run(["git", "config", "commit.gpgsign", "false"], root)
    (root / "app.py").write_text("VALUE = 1\n")
    (root / "check.sh").write_text("#!/bin/sh\ngrep -q VALUE app.py\n")
    (root / "check.sh").chmod(0o755)
    run(["git", "add", "-A"], root)
    run(["git", "commit", "-q", "-m", "base"], root)
    return root


@pytest.fixture
def store(repo):
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    yield store
    store.close()


@pytest.fixture
def pipeline(repo, store):
    return validation.StagingPipeline(
        repo, target_branch="main", store=store, lifecycle=Lifecycle(store)
    )


def head_of(repo, ref="main"):
    return run(["git", "rev-parse", f"refs/heads/{ref}"], repo).stdout.strip()


def candidate(repo, branch, *, path="feature.py", body="FEATURE = True\n", message=None):
    """Commit `body` to `path` on a fresh branch off main, then return to main."""
    run(["git", "checkout", "-q", "-b", branch, "main"], repo)
    (repo / path).write_text(body)
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", message or f"work on {branch}"], repo)
    run(["git", "checkout", "-q", "main"], repo)
    return run(["git", "rev-parse", branch], repo).stdout.strip()


def passing_spec(check_id="integration"):
    return validation.VerificationSpec(
        check_id=check_id,
        argv_or_adapter_ref=("sh check.sh",),
        shell=True,
        acceptance_criterion_ids=(check_id,),
        cache_policy="never",
    )


def failing_spec(check_id="integration"):
    return validation.VerificationSpec(
        check_id=check_id,
        argv_or_adapter_ref=("exit 3",),
        shell=True,
        acceptance_criterion_ids=(check_id,),
        cache_policy="never",
    )


def integrate(pipeline, branch, specs, **kwargs):
    defaults = dict(
        candidate_ref=branch, specs=specs, attempt_id="att-1", task_id="T-001",
        run_id="run-1",
    )
    defaults.update(kwargs)
    return pipeline.integrate(**defaults)


# ── stage 1: freezing the candidate ──────────────────────────────────────────────────


def test_a_candidate_worktree_with_uncommitted_changes_cannot_be_frozen(repo, pipeline):
    """A tree still being written has a commit SHA that describes something else.

    Every fingerprint taken from such a tree describes a snapshot that was never under
    test, so the integration stops at stage 1 rather than measuring the wrong thing.
    """
    candidate(repo, "impl/T-001")
    (repo / "uncommitted.py").write_text("half a thought\n")
    before = head_of(repo)

    outcome = integrate(
        pipeline, "impl/T-001", [passing_spec()], candidate_worktree=repo
    )
    assert outcome.stage == "freeze" and not outcome.ok
    assert "uncommitted" in outcome.reason
    assert head_of(repo) == before


def test_an_unresolvable_candidate_ref_stops_before_anything_is_staged(repo, pipeline):
    before = head_of(repo)
    outcome = integrate(pipeline, "impl/never-existed", [passing_spec()])
    assert outcome.stage == "freeze" and not outcome.ok
    assert head_of(repo) == before


# ── stage 2: the isolated staging merge ──────────────────────────────────────────────


def test_the_merge_happens_in_a_staging_worktree_not_on_the_target_branch(repo, pipeline):
    candidate(repo, "impl/T-001")
    before = head_of(repo)
    frozen = pipeline.freeze_candidate("impl/T-001")
    staged = pipeline.prepare_staging(frozen, target_head=before)

    assert staged.ok
    assert staged.path == repo / validation.STAGING_REL
    assert (staged.path / "feature.py").exists(), "the candidate's work is in staging"
    assert not (repo / "feature.py").exists(), "and is not in the main worktree"
    assert head_of(repo) == before, "the target branch has not moved"
    pipeline.discard_staging()


def test_a_same_line_merge_conflict_aborts_and_is_never_resolved(repo, pipeline):
    """`§3.1`: a conflict on the same lines is never resolved, and never by a model."""
    candidate(repo, "impl/T-001", path="app.py", body="VALUE = 'from the candidate'\n")
    run(["git", "checkout", "-q", "main"], repo)
    (repo / "app.py").write_text("VALUE = 'from main'\n")
    run(["git", "commit", "-q", "-am", "main moved"], repo)
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])
    assert outcome.stage == "stage" and not outcome.ok
    assert "conflict" in outcome.reason
    assert head_of(repo) == before
    assert (repo / "app.py").read_text() == "VALUE = 'from main'\n"


# ── stage 3: integration checks on the merged snapshot ───────────────────────────────


def test_a_failed_staging_check_leaves_the_target_branch_completely_untouched(repo, pipeline):
    """The P4 acceptance condition. Note there is no revert in the implementation.

    The target branch is not written until stage 4, so a stage-3 failure has nothing to
    undo — which is the difference from `merge_and_eval`, where a failing smoke has to be
    followed by a `git reset --hard` that has to be right about what it is resetting to.
    """
    candidate(repo, "impl/T-001")
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [failing_spec()])

    assert outcome.stage == "verify" and not outcome.ok
    assert head_of(repo) == before
    assert outcome.target_before == outcome.target_after == before
    assert not outcome.target_moved
    assert not (repo / validation.STAGING_REL).exists(), "staging is cleaned up"


def test_integration_evidence_is_tied_to_the_merged_snapshot_not_the_candidate(repo, pipeline):
    """Two trees that each pass alone say nothing about the tree they make together."""
    candidate_sha = candidate(repo, "impl/T-001")
    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])
    assert outcome.ok
    assert outcome.evidence
    snapshots = {record.source_snapshot_id for record in outcome.evidence}
    assert snapshots == {outcome.merged_commit}
    assert candidate_sha not in snapshots


def test_a_semantic_conflict_that_only_appears_after_the_merge_is_caught(repo, pipeline):
    """Both sides commit cleanly; the merged tree fails the check. No git conflict.

    This is the case the candidate's own gate cannot see, and the reason `§3` runs checks
    on the merged snapshot rather than trusting stage 1's verdict.
    """
    run(["git", "checkout", "-q", "-b", "impl/T-001", "main"], repo)
    (repo / "check.sh").write_text("#!/bin/sh\ngrep -q FORBIDDEN main_only.py\n")
    run(["git", "commit", "-q", "-am", "tighten the check"], repo)
    run(["git", "checkout", "-q", "main"], repo)
    (repo / "main_only.py").write_text("ALLOWED = 1\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", "main adds a file the new check rejects"], repo)
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])
    assert not outcome.ok and outcome.stage == "verify"
    assert head_of(repo) == before


# ── stage 4: the target fast-forward (`[INV-05]`) ────────────────────────────────────


def test_a_passing_integration_fast_forwards_the_target_exactly_once(repo, pipeline, store):
    candidate(repo, "impl/T-001")
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])

    assert outcome.ok and outcome.stage == "accepted"
    assert head_of(repo) == outcome.merged_commit != before
    assert outcome.target_moved
    events = [
        event for event in store.events()
        if event.type == "TargetBranchFastForwarded"
    ]
    assert len(events) == 1
    assert events[0].payload["from"] == before


def test_the_checkout_of_the_target_follows_the_fast_forward(repo, pipeline):
    """P12 pilot finding D8: `update-ref` alone left the project checkout of `main` on the
    old tree, its index a staged revert of the merge that the next commit there landed."""
    candidate(repo, "impl/T-001")
    (repo / "notes.txt").write_text("mine\n")               # an unrelated local edit

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])

    assert outcome.ok
    assert (repo / "feature.py").read_text() == "FEATURE = True\n"
    assert run(["git", "diff", "--cached", "--name-only"], repo).stdout == ""
    assert (repo / "notes.txt").read_text() == "mine\n"


def test_a_checkout_with_a_conflicting_edit_is_left_alone_and_reported(repo, pipeline, store):
    candidate(repo, "impl/T-001")
    (repo / "feature.py").write_text("LOCAL = 1\n")        # untracked, in the merge's way

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])

    assert outcome.ok and outcome.target_moved
    assert (repo / "feature.py").read_text() == "LOCAL = 1\n"
    [event] = [e for e in store.events() if e.type == "TargetBranchFastForwarded"]
    assert str(repo) in event.payload["stale_checkouts"][0]


def test_a_target_that_advances_during_verification_forces_reintegration(repo, pipeline):
    """`§3.1` step 4: the ref is compare-and-swapped against the head it was verified on.

    The advance is made to happen *during* stage 3 by running it from inside the check
    itself — which is the real race, not a simulation of it: another controller landing a
    merge while this integration was measuring.
    """
    candidate(repo, "impl/T-001")
    before = head_of(repo)
    intruder = candidate(repo, "impl/other", path="other.py", body="OTHER = 1\n")

    advancing_check = validation.VerificationSpec(
        check_id="integration",
        argv_or_adapter_ref=(f"git -C {repo} update-ref refs/heads/main {intruder}",),
        shell=True,
        acceptance_criterion_ids=("integration",),
        cache_policy="never",
    )
    outcome = integrate(pipeline, "impl/T-001", [advancing_check])

    assert not outcome.ok and outcome.stage == "fast_forward"
    assert "advanced" in outcome.reason
    assert head_of(repo) == intruder != before
    assert head_of(repo) != outcome.merged_commit, "the unverified merge did not land"


def test_two_candidates_cannot_cross_pollinate_patches_or_proofs(repo, pipeline, store):
    """`[INV-06]`, as it shows up at integration time.

    Each candidate is staged separately, against its own snapshot of the target, and its
    evidence is keyed by its own merged commit. The first one's patch is not in the
    second one's staging tree until the first has actually landed.
    """
    candidate(repo, "impl/A", path="a.py", body="A = 1\n")
    candidate(repo, "impl/B", path="b.py", body="B = 1\n")

    first = integrate(pipeline, "impl/A", [passing_spec("a_check")], attempt_id="att-A")
    assert first.ok
    a_snapshot = {record.source_snapshot_id for record in first.evidence}

    second = integrate(
        pipeline, "impl/B", [passing_spec("b_check")], attempt_id="att-B", task_id="T-002",
        run_id="run-2",
    )
    assert second.ok
    b_snapshot = {record.source_snapshot_id for record in second.evidence}

    assert a_snapshot.isdisjoint(b_snapshot)
    assert {row["check_id"] for row in store.evidence_for_attempt("att-A")} == {"a_check"}
    assert {row["check_id"] for row in store.evidence_for_attempt("att-B")} == {"b_check"}


# ── stage 5: external side effects (`[INV-07]`, `A05`) ───────────────────────────────


def test_a_declared_effect_records_an_intent_and_a_receipt(repo, pipeline, store):
    candidate(repo, "impl/T-001")
    landed = []
    outcome = integrate(
        pipeline, "impl/T-001", [passing_spec()],
        effects=[{"name": "push", "effect_class": "external_side_effect",
                  "run": lambda: landed.append("pushed") or 0}],
    )
    assert outcome.ok and landed == ["pushed"]
    assert "push" in outcome.receipts
    intents = store.intents_for_attempt("att-1")
    assert [row["status"] for row in intents] == ["reconciled"]


def test_an_unprobed_effect_that_fails_leaves_the_node_uncertain_not_failed(repo, pipeline):
    """`A05`: it may or may not have happened, and nothing here may guess."""
    candidate(repo, "impl/T-001")
    outcome = integrate(
        pipeline, "impl/T-001", [passing_spec()],
        effects=[{"name": "deploy", "effect_class": "external_side_effect",
                  "run": lambda: 1}],
    )
    assert not outcome.ok and outcome.stage == "side_effects"
    assert "UNCERTAIN" in outcome.reason and "A05" in outcome.reason
    assert head_of(repo) == outcome.merged_commit, "stage 4 already landed; it is not undone"


def test_a_probe_that_confirms_the_effect_completes_the_integration(repo, pipeline, store):
    """The crashed-worker case: the effect landed, the runner did not survive to say so."""
    candidate(repo, "impl/T-001")
    outcome = integrate(
        pipeline, "impl/T-001", [passing_spec()],
        effects=[{
            "name": "deploy", "effect_class": "external_side_effect",
            "run": lambda: (_ for _ in ()).throw(OSError("connection lost")),
            "probe": lambda: True,
        }],
    )
    assert outcome.ok, outcome.reason
    assert store.receipt_for(
        store.intents_for_attempt("att-1")[0]["operation_id"]
    ) is not None


# ── stage 6: the acceptance transaction ──────────────────────────────────────────────


def test_acceptance_is_committed_with_its_evidence_ids_and_moves_the_task(repo, pipeline, store):
    store.register_task("T-001", spec_revision=1, status="planned")
    Lifecycle(store).set_task_status("T-001", "active")
    candidate(repo, "impl/T-001")

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])

    assert outcome.ok and outcome.decision is not None
    rows = store.acceptances_for("T-001", "run-1")
    assert len(rows) == 1 and rows[0]["decision"] == "accepted"
    assert rows[0]["integrated_source_snapshot"] == outcome.merged_commit
    assert rows[0]["required_evidence_ids"] != "[]"
    assert store.task("T-001")["status"] == "accepted"


def test_a_done_sentinel_in_the_candidate_cannot_accept_anything(repo, pipeline, store):
    """The whole point, at integration scale.

    The candidate commits a `DONE` file and a `result.json` swearing everything passed,
    and the declared check fails. The task is not accepted, the branch does not move, and
    no acceptance row is written.
    """
    run(["git", "checkout", "-q", "-b", "impl/T-001", "main"], repo)
    (repo / "DONE").write_text("all verifications complete\n")
    (repo / "result.json").write_text('{"tests": {"pass": true}}')
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", "claim success"], repo)
    run(["git", "checkout", "-q", "main"], repo)
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [failing_spec()])

    assert not outcome.ok
    assert head_of(repo) == before
    assert store.acceptances_for("T-001") == []


def test_a_task_with_no_declared_check_cannot_be_integrated(repo, pipeline):
    """An empty closed world discharges nothing (`[INV-04]`)."""
    candidate(repo, "impl/T-001")
    before = head_of(repo)
    outcome = integrate(pipeline, "impl/T-001", [])
    assert not outcome.ok
    assert "empty closed world" in outcome.reason
    assert head_of(repo) == before


# ── the graph nodes that drive it (`[P3]` compiled them, `[P4]` implements them) ──────


def test_the_merge_node_refuses_to_run_anywhere_but_the_controller(repo):
    """`[INV-05]` again, at the seam where a worker could have reached for it."""
    from maestro.workflows import compiler, handlers
    from maestro.taskgraph import normalize_spec

    revision = compiler.compile_workflow(
        normalize_spec({"id": "T-001", "title": "t", "status": "pending", "deps": []}),
        workflow_id="wf_x",
    )
    node = revision.node("merge")
    ctx = handlers.HandlerContext(
        run_id="r", task_id="T-001", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "a"),
        workspace=repo, params=node.params, controller=None,
    )
    result = handlers.resolve(node.handler_ref)(ctx)
    assert result.status == "failed" and "INV-05" in result.detail


def test_the_controller_executes_the_merge_and_accept_nodes_in_process(repo):
    """`[P4]`: a controller-scoped node is never dispatched into a TMUX worker."""
    from maestro.workflows import runner as runner_module

    assert "controller_target_branch" in runner_module.CONTROLLER_PERMISSIONS
    assert "controller_only" in runner_module.CONTROLLER_PERMISSIONS


def test_the_verification_node_reports_evidence_the_controller_can_ingest(repo, store, tmp_path):
    """A worker runs the checks; only the controller writes them down (`[INV-01]`)."""
    from maestro.workflows import compiler, handlers
    from maestro.taskgraph import normalize_spec

    roadmap = repo / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    roadmap.write_text(
        "```yaml\n"
        "id: T-001\n"
        "verifications:\n"
        "  - id: V1\n"
        "    kind: auto\n"
        "    cmd: sh check.sh\n"
        "```\n"
    )
    revision = compiler.compile_workflow(
        normalize_spec({"id": "T-001", "title": "t", "status": "pending", "deps": []}),
        workflow_id="wf_x",
    )
    node = revision.node("gate")
    from maestro import gates, validation as validation_module

    decision, evidence = gates.run_authoritative_gate(
        "T-001", repo, attempt_id="att-1", store=None, roadmap=roadmap
    )
    assert decision.accepted, decision.reason
    assert [record.check_id for record in evidence] == ["V1"]

    # What the controller does with them on ingest: the rows become durable, under the
    # attempt id the controller believes in rather than the one the report carried.
    for record in evidence:
        row = record.to_row()
        row["attempt_id"] = "att-controller"
        store.record_evidence(row)
    stored = store.evidence_for_attempt("att-controller")
    assert [row["check_id"] for row in stored] == ["V1"]


# ── `kind: dan`: an approval speaks only for the commit it names ─────────────────────


def _dan_spec():
    return validation.VerificationSpec.from_roadmap({"id": "V2", "kind": "dan", "check": "c"})


def test_dans_pre_merge_approval_cannot_replace_the_deployed_observation(repo, pipeline):
    sha = candidate(repo, "impl/T-001")
    approval = validation.review_disposition("V2", "approved", reviewer="dan", subject_sha=sha)

    outcome = integrate(pipeline, "impl/T-001", [passing_spec(), _dan_spec()],
                        review_dispositions=[approval])

    assert outcome.ok, outcome.reason
    assert outcome.stage == 'integrated' and outcome.decision is None
    # Integration is allowed, acceptance still needs an observation of the merged SHA.
    decision = validation.decide(task_id='T-001', run_id='run',
        specs=[passing_spec(), _dan_spec()], evidence=outcome.evidence,
        review_dispositions=[], integrated_source_snapshot=outcome.merged_commit,
        expected_snapshot=outcome.merged_commit)
    assert not decision.accepted and "'V2'" in decision.reason
    deployed = validation.review_disposition('V2', 'approved', reviewer='dan',
                                             subject_sha=outcome.merged_commit)
    assert validation.decide(task_id='T-001', run_id='run',
        specs=[passing_spec(), _dan_spec()], evidence=outcome.evidence,
        review_dispositions=[deployed], integrated_source_snapshot=outcome.merged_commit,
        expected_snapshot=outcome.merged_commit).accepted


def test_dans_approval_of_another_commit_discharges_nothing(repo, pipeline):
    candidate(repo, "impl/T-001")
    before = head_of(repo)
    approval = validation.review_disposition(
        "V2", "approved", reviewer="dan", subject_sha="0" * 40
    )

    outcome = integrate(pipeline, "impl/T-001", [passing_spec(), _dan_spec()],
                        review_dispositions=[approval])

    assert outcome.ok and outcome.stage == 'integrated' and outcome.decision is None
    assert head_of(repo) != before
    decision = validation.decide(task_id='T-001', run_id='run',
        specs=[passing_spec(), _dan_spec()], evidence=outcome.evidence,
        review_dispositions=[approval], integrated_source_snapshot=outcome.merged_commit,
        expected_snapshot=outcome.merged_commit)
    assert not decision.accepted and "'V2'" in decision.reason


# ── P12 pilot finding D20: deterministic resolution of a trivial conflict ─────────────

LIB = "import a\n\n\ndef f():\n    '''Say hi.'''\n    return 1\n"


def _two_sided(repo, *, ours: str, theirs: str, first_via=None):
    """`lib.py` on main, then `impl/T-001` edits it to `theirs` and main to `ours`.

    `first_via`, when given, lands main's edit through the pipeline as `impl/T-000`, so
    the target carries an `integrate(impl/T-000)` merge commit, as a parallel task would.
    """
    (repo / "lib.py").write_text(LIB)
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", "add lib"], repo)
    candidate(repo, "impl/T-001", path="lib.py", body=theirs)
    if first_via is not None:
        candidate(repo, "impl/T-000", path="lib.py", body=ours)
        assert integrate(first_via, "impl/T-000", [passing_spec()], task_id="T-000").ok
    else:
        (repo / "lib.py").write_text(ours)
        run(["git", "commit", "-q", "-am", "main edits lib"], repo)


def test_a_conflict_that_touches_disjoint_lines_is_resolved_deterministically(repo, pipeline):
    """An import edited on main next to an import inserted by the task, plus two blocks
    appended at the same point: git calls both a conflict, and neither competes."""
    ours = LIB.replace("import a\n", "import a, b\n") + "\n\ndef ours():\n    pass\n"
    theirs = LIB.replace("import a\n", "import a\nimport c\n") + "\n\ndef theirs():\n    pass\n"
    _two_sided(repo, ours=ours, theirs=theirs)
    assert run(["git", "merge-tree", "--write-tree", "main", "impl/T-001"], repo).returncode == 1
    before = head_of(repo)

    staged = pipeline.prepare_staging(pipeline.freeze_candidate("impl/T-001"), target_head=before)

    assert staged.ok and staged.resolved == ("lib.py",)
    merged = (staged.path / "lib.py").read_text()
    assert merged.startswith("import a, b\nimport c\n")
    assert merged.index("def ours") < merged.index("def theirs")
    message = run(["git", "log", "-1", "--format=%B", staged.merged_commit], repo).stdout
    assert message.startswith("integrate(impl/T-001): staging verification")
    assert "lib.py" in message
    parents = run(["git", "rev-list", "--parents", "-1", staged.merged_commit], repo).stdout
    assert len(parents.split()) == 3, "a real merge commit"
    pipeline.discard_staging()

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])
    assert outcome.ok and outcome.resolved == ("lib.py",)
    assert head_of(repo) == outcome.merged_commit != before


def test_a_resolved_merge_is_still_verified_and_a_failing_check_moves_nothing(repo, pipeline):
    """Stage 3 re-runs the task's integration checks on the resolved snapshot: that is
    the whole re-check; there is no review and no model."""
    ours = LIB.replace("import a\n", "import a, b\n")
    theirs = LIB.replace("import a\n", "import a\nimport c\n")
    _two_sided(repo, ours=ours, theirs=theirs)
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [failing_spec()])

    assert outcome.stage == "verify" and not outcome.ok
    assert outcome.resolved == ("lib.py",)
    assert head_of(repo) == before


def test_a_same_line_conflict_is_a_planner_fault_naming_the_other_task(repo, pipeline):
    ours = LIB.replace("Say hi.", "Say hello.").replace("import a\n", "import a, b\n")
    theirs = LIB.replace("Say hi.", "Greet the caller.").replace("import a\n", "import a\nimport c\n")
    _two_sided(repo, ours=ours, theirs=theirs, first_via=pipeline)
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])

    assert outcome.stage == "stage" and not outcome.ok
    assert outcome.fault == "planner"
    assert head_of(repo) == before
    assert not (repo / validation.STAGING_REL).exists()
    [conflict] = outcome.conflicts
    assert conflict["path"] == "lib.py" and conflict["lines"] == "5-5"
    assert conflict["other_tasks"] == ["impl/T-000"]
    assert "impl/T-000" in outcome.reason and "lib.py lines 5-5" in outcome.reason
    assert "planner fault" in outcome.reason
    assert "parallel tasks must merge trivially" in outcome.reason


def test_an_add_add_conflict_is_never_resolved(repo, pipeline):
    candidate(repo, "impl/T-001", path="new.py", body="X = 1\n")
    (repo / "new.py").write_text("X = 2\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", "main adds new.py"], repo)
    before = head_of(repo)

    outcome = integrate(pipeline, "impl/T-001", [passing_spec()])

    assert outcome.stage == "stage" and outcome.fault == "planner"
    assert outcome.conflicts[0]["path"] == "new.py"
    assert "not a text edit of a common file" in outcome.conflicts[0]["reason"]
    assert head_of(repo) == before


def test_the_merge_node_journals_a_resolution_and_a_planner_fault(repo, monkeypatch):
    from types import SimpleNamespace

    import maestro.state
    from maestro.workflows import compiler, handlers
    from maestro.taskgraph import normalize_spec

    journal: list[tuple[str, str]] = []
    monkeypatch.setattr(maestro.state, "append_journal",
                        lambda event, detail, *a, **k: journal.append((event, detail)))
    pipeline = validation.StagingPipeline(repo, target_branch="main")
    _two_sided(repo, ours=LIB.replace("Say hi.", "Say hello."),
               theirs=LIB.replace("Say hi.", "Greet."), first_via=pipeline)

    revision = compiler.compile_workflow(
        normalize_spec({"id": "T-001", "title": "t", "status": "pending", "deps": []}),
        workflow_id="wf_x",
    )
    node = revision.node("merge")
    ctx = handlers.HandlerContext(
        run_id="r", task_id="T-001", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(handlers.attempts_root(repo), "a"),
        workspace=repo, params=node.params,
        controller=SimpleNamespace(repo=repo, store=None, lifecycle=None),
        inputs={"specs": [passing_spec()]},
    )
    result = handlers.resolve(node.handler_ref)(ctx)
    assert result.status == "failed"
    assert result.outputs["fault"] == "planner"
    assert result.outputs["conflicts"][0]["other_tasks"] == ["impl/T-000"]
    [(event, detail)] = [j for j in journal if j[0].startswith("merge_")]
    assert event == "merge_conflict_planner_fault"
    assert detail.startswith("T-001 ") and "impl/T-000" in detail

    # The other task's lines were then taken back out of the target (as a fixed plan
    # would have sequenced them); a disjoint edit on main now resolves and is journaled.
    run(["git", "revert", "--no-edit", "-m", "1", "HEAD"], repo)
    (repo / "lib.py").write_text(LIB.replace("import a\n", "import a, b\n"))
    run(["git", "commit", "-q", "-am", "main edits the import"], repo)
    run(["git", "checkout", "-q", "impl/T-001"], repo)
    (repo / "lib.py").write_text(LIB.replace("import a\n", "import a\nimport c\n")
                                 .replace("Say hi.", "Greet."))
    run(["git", "commit", "-q", "-am", "task inserts an import"], repo)
    run(["git", "checkout", "-q", "main"], repo)
    journal.clear()
    result = handlers.resolve(node.handler_ref)(ctx)
    assert result.status == "succeeded", result.detail
    assert result.outputs["resolved"] == ["lib.py"]
    assert journal == [("merge_resolved_deterministic", "T-001 ref=impl/T-001 files=lib.py")]
