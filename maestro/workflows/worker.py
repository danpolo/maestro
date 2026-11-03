"""The supervised worker: steps 4-6 of the controlled-operation protocol (`[P3]`).

What runs inside `tmux new-window -t agents -n maestro-<task>-<node>`. It is a separate
process, and that is its entire reason for existing: the controller must be able to crash,
restart, and find this process still working (`§6` step 2, "if PID is alive: adopt").

It writes, in this exact order, and the order is the protocol:

    1. `envelope.json` — its own PID, immediately, before doing anything (`[INV-02]`)
    2. `heartbeat`     — while it works
    3. `result.json`   — the typed `NodeResult`
    4. `receipt.json`  — last, because its presence is what means "the work is over"

It never opens `control.sqlite3` for writing. `[INV-01]` makes the control database the
controller's alone, and the reason bites here specifically: a worker that could write its
own success would be a worker that could bypass `[INV-04]`.

**A bound agent node** (P12A W1) arrives with `--backend-id`, `--runtime-kind`, `--model`,
its effort in the backend's own vocabulary, and `--manifest-artifact-id`. The brief is
read from that manifest. A `model_endpoint_harness` binding runs `ModelAgentRuntime`
against the configured free-tier endpoint; an `agent_cli` binding runs one bounded agentic
`driver.complete` in the worktree, and the workspace is then adopted through the node's
handler exactly as before. Without binding args the worker is unchanged.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from pathlib import Path

from maestro.control.models import now_iso
from maestro.control.store import ControlStore, control_db_path
from maestro.workflows import dan_questions, handlers, permission_asks, task_tree
from maestro.workflows.compiler import load_revision
from maestro.workflows.handlers import (
    INBOX_PROTOCOL_FILES, PLAN_REPAIR_FILE, REVIEW_FILE, ROLE_DIAGNOSER, ROLE_REVIEWER,
    AttemptInbox, HandlerContext, attempts_root,
)
from maestro.workflows.models import NodeResult


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="maestro.workflows.worker")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--worktree", default="")
    # P12A W1: the resolved binding, rendered by `routing.render_binding_argv`.
    parser.add_argument("--backend-id", default="")
    parser.add_argument("--runtime-kind", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort-arg", action="append", default=[])
    parser.add_argument("--effort-config", action="append", default=[])
    parser.add_argument("--manifest-artifact-id", default="")
    parser.add_argument("--affected-selection-artifact-id", default="")
    # G7: the prep's post_action_cmd, which the gate after a manual step runs first.
    parser.add_argument("--post-action-cmd", default="")
    parser.add_argument('--manual-contract', default='')
    parser.add_argument("--failure-context-artifact-id", default="")
    # D29: the deny-list config (merge target and Dan's rulings) the gate screens with.
    parser.add_argument("--deny-list-config", default="")
    parser.add_argument("--scope-check", default="")
    # GDT-6: the goals guard (anti-tamper + acceptance regression rerun) the gate applies.
    parser.add_argument("--goals-guard", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--session-mode", default="")
    return parser.parse_args(argv)


def _model_api(endpoint):
    """The endpoint client. Module-level so a test can substitute a fake `ModelAPI`."""
    from maestro.backends.model_api import ModelAPI

    return ModelAPI(endpoint)


def _artifacts_root(repo: Path) -> Path:
    return repo / ".orchestrator" / "artifacts"


def _read_manifest(repo: Path, artifact_id: str) -> dict:
    """A stored JSON artifact (the `ContextManifest`, or a W5 affected selection).

    A read, which a client role may do.
    """
    from maestro.control.artifacts import ArtifactStore

    store = ControlStore.open_client(control_db_path(repo))
    try:
        return json.loads(ArtifactStore(_artifacts_root(repo), store).read(artifact_id))
    finally:
        store.close()


def task_header(
    context: HandlerContext, *, title: str = "", reports_by_sentinel: bool = True
) -> str:
    """Which task this is, where the agent works, and how it reports (`_p12_open_threads` #1).

    The context manifest carries the task's goal and criteria but not its identity, and
    nothing in it says where the tree is or how an opaque CLI reports back (`[INV-03]`):
    the agent handler adopts only a sentinel or `result.json` in the workspace. A writer is
    also told to commit, because merge freezes the branch and refuses uncommitted work.
    A model-endpoint harness reports through its own runtime, so it is not told about the
    workspace sentinel (`reports_by_sentinel=False`).
    """
    role = context.node.agent_definition_ref or context.node.node_id
    name = title or str(context.params.get("title") or "")
    lines = [f"You are the {role} for task {context.task_id}"
             + (f": {name}." if name else ".")]
    if context.worktree is not None:
        lines.append(f"WORKTREE: {context.worktree}")
    if reports_by_sentinel:
        lines.append(f"WORKSPACE: {context.workspace}")
    if context.worktree is not None and context.node.permissions_ref == "task_worktree_writer":
        from maestro import confinement

        branch = confinement.branch_name(
            "impl", context.task_id, candidate=str(context.params.get("candidate_id") or "")
        )
        lines.append(
            f"Make every change inside the WORKTREE (branch {branch}) and commit it there:\n"
            f"  git add -A && git commit -m '{context.task_id}: <one-line summary>'\n"
            "Uncommitted changes are not integrated."
        )
    if context.node.agent_definition_ref == ROLE_REVIEWER:
        from maestro.workflows.telemetry import complexity_prompt
        lines.append(complexity_prompt())
        # P12 pilot finding D6: DONE alone only says the review ran. The verdict is what
        # the agent handler adopts, and without one the review fails closed.
        # D10 edge 3: a model-endpoint reviewer has no sentinel and no shell. Its one
        # write tool lands in its workspace, where the same file is adopted.
        lines.append(
            (
                f"Record your verdict in the file {REVIEW_FILE} in the WORKSPACE, as "
                if reports_by_sentinel else
                f"Record your verdict with the write_file tool at the path {REVIEW_FILE} "
                "(the only file of yours that is read; an answer without it is not a review), as "
            )
            + 'JSON: {"complexity": 1 | 2 | 3 | 4 | 5, "verdict": "approved" | "changes_requested" | "rejected", "findings": '
            '[{"finding": "<one defect: what is wrong, where, and why it matters>", '
            '"severity": "blocking" | "follow_up", "rule": "<the general rule or property '
            'it violates>", "scope": "<every place the rule applies>", "verify": "<how to '
            'verify the fix>"}, ...]}. '
            "A finding is blocking only if it must be fixed before this work merges "
            "(it breaks an acceptance criterion, the task's stated behaviour, or safety); "
            "anything that can wait for later work is a follow_up, which is recorded but "
            "does not send the task back. Mark a follow_up "
            '"before_dependents": true only when a task that depends on this one would '
            "build on the defect (an interface, name or schema it uses); it then runs as "
            "its own task before them; every other follow_up waits for the polish stage "
            "at the end of the project. Approve if no finding is blocking; the "
            "implementer reworks the task from your blocking findings alone.\n"
            "Aim for this review to be the last. For each blocking finding state the "
            "general rule or property it violates (your example only demonstrates it), "
            "its scope — every place in the change where that rule applies, not only the "
            "line you tripped on — and how to verify the fix: a test of the property that "
            "the next round can check. Report every blocking finding in this one pass: a "
            "defect you can see now but hold back costs a whole extra round.\n"
            + dan_questions.BRIEF_RULE + " " + dan_questions.REVIEWER_RULE
        )
        round_no = context.inputs.get("review_round")
        if isinstance(round_no, int) and round_no >= 2:
            # Dan, 2026-09-26: where each finding came from, so each role's misses can be
            # counted. The reviewer states what it observed; code maps it to a fault.
            lines.append(
                f"This is review round {round_no}. Earlier findings are listed under PRIOR "
                "REVIEW ROUNDS by id (r<round>-f<n>). Give every blocking finding an "
                '"origin", and for the first three also "prior_finding": "<that id>":\n'
                "- not_fixed: an earlier finding the rework did not address at all;\n"
                "- fixed_incompletely_in_scope: an earlier finding's stated scope covered "
                "this, and only part of it was fixed;\n"
                "- fixed_incompletely_out_of_scope: the same class as an earlier finding, "
                "but outside the scope that finding stated;\n"
                "- missed_earlier: in code an earlier round already reviewed, unrelated to "
                "any earlier finding;\n"
                "- introduced_by_rework: a new defect in the rework's own diff;\n"
                "- exposed_by_rework: an older defect the rework made reachable or visible;\n"
                "- scope_widened: you apply a broader requirement than earlier rounds did.\n"
                "Use the diff since the previous round's commit and git blame (commands "
                "under PRIOR REVIEW ROUNDS) to tell missed_earlier from introduced_by_rework."
            )
    if reports_by_sentinel and context.node.agent_definition_ref == ROLE_DIAGNOSER:
        # P12 pilot finding D17: DONE is the prose diagnosis. The repair is what the
        # controller acts on, and without one the task parks.
        lines.append(
            f"Record the repair you propose in the file {PLAN_REPAIR_FILE} in the WORKSPACE, "
            "as JSON. The controller acts on this file, not on your DONE; without a usable "
            "file the task parks for Dan. Shape: "
            '{"verdict": "poor_agent" | "bad_graph", "reason": "<the cause you found, one '
            'line>", "brief_addenda": {"<node_id>": "<what that node\'s next agent must be '
            'told>"}}.\n'
            '- "poor_agent": the plan is sound; an agent\'s brief or model was not. Give '
            "brief_addenda for the node that failed or for the writer whose work it judged "
            "(named under REPAIR CONTRACT). That node runs once more with your addendum in "
            "its brief, and everything after it re-runs, checks included. This is the one "
            "final run: if it fails too, the task parks. So write the addendum for the "
            "writer to finish in that run: every remaining defect, each as the general rule "
            "it breaks, every place the rule applies, and how to verify the fix with a test.\n"
            '- "bad_graph": the plan itself is wrong. Instead of brief_addenda, give a graph '
            "patch in the fields add_nodes, replace_nodes, remove_nodes, add_edges, "
            "remove_edges, rerun_nodes, with node and edge objects shaped like the CURRENT "
            "GRAPH under REPAIR CONTRACT.\n"
            '- "spec_gap": the rounds failed on a case the task never defined, and each '
            "round guessed a different rule. Instead of a patch, give "
            '"questions": [<question>, ...]. Dan answers on Telegram; the node that failed '
            "then re-runs with his decision, and the repair budget is not spent.\n"
            "A repair never removes or routes around a check (a gate or a review).\n"
            + dan_questions.BRIEF_RULE
        )
    if reports_by_sentinel and context.node.agent_definition_ref not in (
        ROLE_REVIEWER, ROLE_DIAGNOSER
    ):
        # P12 pilot finding D26: a writer (or any other agent) asks through QUESTION.
        lines.append(dan_questions.implementer_instruction())
    if reports_by_sentinel:
        lines.append(
            "When you are finished, write the file DONE into the WORKSPACE (content: a "
            "short summary), or FAILED (content: the reason) if the task cannot be completed."
        )
    return "\n".join(lines)


def agent_brief(
    manifest: dict, *, failure_context: dict | None = None, header: str = "",
    role: str = "",
) -> str:
    """The brief the agent is given.

    The task header (`task_header`), then mandatory instructions, acceptance criteria and
    entrypoints from the context manifest; the task's ROADMAP notes, verbatim, for every
    role (P12 pilot finding D28; `role` is the node's role, which a reviewer's notes
    section addresses); and, for a diagnoser (P12B F7), the failure it is asked to
    explain, rendered with its verification output bounded
    (`failure_context.render_failure_context`).
    """
    parts = [header] if header else []
    parts += [str(line) for line in manifest.get("mandatory_instructions") or ()]
    criteria = manifest.get("acceptance_criteria") or ()
    if criteria:
        parts.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria))
    notes = str(manifest.get("task_notes") or "")
    if notes:
        # P12 pilot finding D28: DuetFlow 06 run 4's notes reached no agent; its last
        # review round found a requirement they stated, and the task parked.
        cut = [m for m in manifest.get("omitted_material") or () if str(m).startswith("task_notes:")]
        parts.append(
            "TASK NOTES (the task's `notes:` in docs/ROADMAP.md, verbatim). Every requirement "
            "in them is part of this task's contract, as binding as the acceptance criteria:\n"
            + notes + "".join(f"\n[{m}]" for m in cut)
        )
        if role == ROLE_REVIEWER:
            parts.append(
                "Check each requirement in the TASK NOTES in this round, the first one "
                "included, against the code and tests: a requirement the work does not meet "
                "is a blocking finding in this round, not one to leave for a later round."
            )
    from maestro.workflows import goal_progress

    goals = goal_progress.render(manifest, role=role)
    if goals:
        # P13 W11a: why the project exists, for every role; a reviewer's goal check.
        parts.append(goals)
    decided = dan_questions.render_decisions(manifest.get("dan_decisions"))
    if decided:
        # P12 pilot finding D26: Dan's answers to earlier spec questions, for every role.
        parts.append(decided)
    ruled = permission_asks.render_rulings(manifest.get("permission_rulings"))
    if ruled:
        # P12 pilot finding D30: Dan's rulings on this workflow's permission requests.
        parts.append(ruled)
    if manifest.get("followup_contract"):
        # A `<id>-followups` task (`maestro.docs.followups.contract`): its criteria are
        # another task's review follow-ups, each done or declined, and nothing else.
        parts.append(str(manifest["followup_contract"]))
    entrypoints = manifest.get("entrypoints") or ()
    if entrypoints:
        parts.append("Entrypoints:\n" + "\n".join(f"- {p}" for p in entrypoints))
    risky = manifest.get("risky_files_touched") or ()
    if risky:
        # P12 finding D10: computed by the controller from the real diff against the run's
        # base, not from the scope the task declared. This review is the only gate on them.
        parts.append(
            "RISKY FILES TOUCHED: this change edits files the project marks risky "
            "(project.yaml risky_set):\n" + "\n".join(f"- {p}" for p in risky) + "\n"
            "Scrutinize the changes to these files specifically: read every hunk, check "
            "that each change is necessary for this task and minimal, and that it cannot "
            "break the behaviour those files guard (credentials, network writes, data "
            "integrity). Your approval is the only gate before these changes merge; if any "
            "risky change is unjustified, unsafe or larger than the task needs, do not "
            "approve — list it as a finding."
        )
    if manifest.get("prior_reviews"):
        # P12 pilot finding D15: a review is one round of several, not a fresh sample.
        parts.append(
            "PRIOR REVIEW ROUNDS in this run (oldest first), and what the implementer "
            "reported after each:\n" + str(manifest["prior_reviews"]) + "\n"
            "Check whether each earlier blocking finding is now fixed. Do not reverse an "
            "earlier round's demand silently: if you disagree with it, say so in your "
            "finding and why. Raise a new blocking finding only for a defect that must be "
            "fixed before this work merges."
        )
        prior = str(manifest.get("prior_reviewed_commit") or "")
        if prior:
            # Dan, 2026-09-26: what the rework changed since the last review, and whether
            # a line predates it — the evidence behind a finding's origin.
            parts.append(
                f"The previous round reviewed commit {prior}. `git diff {prior}..HEAD` in "
                "the WORKTREE is the rework since then; `git blame <file>` shows whether a "
                "line predates it."
            )
    if manifest.get("repair_contract"):
        # P12 pilot finding D17: what a diagnoser's plan_repair.json may name.
        parts.append("REPAIR CONTRACT:\n" + str(manifest["repair_contract"]))
    if manifest.get("runaway_contract"):
        # P13 W13: this diagnosis was opened early, on spend, and may be a false alarm.
        parts.append(str(manifest["runaway_contract"]))
    if manifest.get("forecast_contract"):
        # P13 W13: absent with detection off, so the planner's brief is what it was before.
        parts.append(str(manifest["forecast_contract"]))
    if manifest.get("review_origins"):
        # Dan, 2026-09-26: whose misses the later review rounds were.
        parts.append(
            "REVIEW FINDING ORIGINS (rounds 2+, blocking findings, with the role at fault):"
            "\n" + str(manifest["review_origins"])
        )
    if manifest.get("brief_addendum"):
        # P12 pilot finding D17: a `poor_agent` diagnosis re-briefs this node for its
        # one final run.
        parts.append(
            "DIAGNOSIS OF THE EARLIER ROUNDS (this is the final run; if it fails, the task "
            "parks for Dan):\n" + str(manifest["brief_addendum"])
        )
    if manifest.get("prep_contract"):
        # G7: a manual lane's prep agent does everything but Dan's one step.
        parts.append(str(manifest["prep_contract"]))
    if manifest.get("dan_answers"):
        # B12: Dan's answers to the task's questions, gathered before the run opened.
        parts.append(str(manifest["dan_answers"]))
    if manifest.get("rework_note"):
        # A writer re-run after Dan rejected its work (`kind: dan`): his reason, verbatim.
        parts.append(f"NOTE FROM ORCHESTRATOR: {manifest['rework_note']}")
    if failure_context is not None:
        from maestro.workflows.failure_context import render_failure_context

        parts.append(render_failure_context(failure_context))
    return "\n\n".join(parts)


def _run_model_endpoint(
    args, repo: Path, revision, brief: str, context: HandlerContext | None = None
) -> NodeResult:
    """A `free_tier_api` binding: the bounded model/tool supervisor (P7 §5).

    The model has four tools and no shell, so it cannot commit what it wrote. For a
    writer, the harness commits the tree on a clean finish;
    otherwise merge's freeze refuses the uncommitted tree and no free task can land.
    (`task_tree.commit_all`, the same commit a `kind: script` lane gets.)
    """
    from maestro import config
    from maestro.backends.model_runtime import CheckpointWriter, ModelAgentRuntime, ToolBox
    from maestro.confinement import CommitBoundary
    from maestro.workflows.failures import record_from_runtime
    from maestro.workflows.routing import endpoint_from_config

    rows = [row for row in config.free_tier_endpoints() if row["endpoint_id"] == args.backend_id]
    if not rows:
        return NodeResult(
            run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
            status="failed", exit_status=2,
            detail=f"no engineering.free_tier_endpoints row for {args.backend_id!r}",
        )
    candidate = str((context.params if context else {}).get("candidate_id") or "")
    boundary = CommitBoundary.for_task(repo, revision.task_id, candidate=candidate)
    runtime = ModelAgentRuntime(
        _model_api(endpoint_from_config(rows[0])),
        # Field-notes F1: a reader's `write_file` lands in its workspace, never the tree.
        # That workspace is the attempt inbox, whose protocol files stay the worker's.
        ToolBox(boundary=boundary, writes_to=None if (
            context is None or context.node.permissions_ref == "task_worktree_writer"
        ) else context.workspace, reserved=INBOX_PROTOCOL_FILES),
        CheckpointWriter(_artifacts_root(repo), attempt_id=args.attempt_id),
    )
    outcome = runtime.run(brief)
    outputs: dict = {"runtime": outcome.to_dict()}
    failure = record_from_runtime(outcome)
    if failure is not None:
        outputs["failure"] = failure.to_dict()          # P12B F1
    status, detail = outcome.status, outcome.detail or outcome.stop_reason
    writer = context is not None and context.node.permissions_ref == "task_worktree_writer"
    if status == "succeeded" and writer and task_tree.is_repository(boundary.worktree):
        try:
            outputs["commit"] = task_tree.commit_all(
                boundary.worktree, f"{revision.task_id}: {args.backend_id} harness changes"
            )
        except task_tree.TaskTreeError as exc:
            status, detail = "failed", f"harness_commit_failed: {exc}"
    return NodeResult(
        run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
        status=status, detail=detail,
        outputs=outputs,
        exit_status=0 if status == "succeeded" else 1,
    )


def _run_agent_cli(args, context: HandlerContext, brief: str):
    """An `agent_cli` binding: one bounded agentic call in the worktree (or workspace)."""
    from maestro.backends import registry
    from maestro.backends.base import CompletionSpec

    driver = registry.get_backend(registry.driver_name_for_backend_id(args.backend_id))
    config_pairs = tuple(
        tuple(item.split("=", 1)) for item in args.effort_config if "=" in item
    )
    writer = context.node.permissions_ref == "task_worktree_writer"
    # Field-notes F1: only a writer works in the worktree. A reader (reviewer, planner,
    # diagnoser) runs in its workspace, the one place it may write — its verdict goes
    # there — and reads the worktree by the path its brief names. Codex's
    # `workspace-write` then enforces that at the OS level; Claude's Edit rules and agy's
    # `--add-dir` are scoped to `cwd` + `add_dirs`, neither of which names the tree.
    cwd = (context.worktree if writer else None) or context.workspace
    # The brief tells the agent to report into the workspace, which sits outside the
    # worktree; a sandboxed CLI (Codex `workspace-write`) may write there only if granted.
    add_dirs = (context.workspace,) if context.workspace and context.workspace != cwd else ()
    if writer:
        # P12 pilot finding D13: a linked worktree's index, objects and refs live under
        # the main repository's `.git`, so a sandboxed writer told to commit cannot.
        add_dirs += _git_dirs(context.worktree)
    if getattr(driver, "permission_lists", False):
        # P12 pilot finding D30: the driver runs its agent on the global allow list; a
        # denied call the agent needs becomes a request to Dan (`_adopt_permission_request`).
        brief = brief + "\n\n" + permission_asks.BRIEF_RULE
    return driver, driver.complete(
        CompletionSpec(
            prompt=brief,
            model=args.model,
            timeout=int(context.node.timeout_policy.execution_timeout_sec),
            cwd=cwd,
            writable=True,
            add_dirs=add_dirs,
            log_file=context.inbox.path / "impl.log",
            effort_args=tuple(args.effort_arg),
            effort_config=config_pairs,
            # A fresh session id of its own, so the controller can find this call's
            # transcript at settle and count its tokens. A uuid4, never derived from the
            # attempt id: a relaunched worker must not collide with its predecessor's.
            session_id=str(uuid.uuid4()),
        )
    )


def _git_dirs(worktree: Path | None) -> tuple[Path, ...]:
    """What a sandboxed writer must write to commit in `worktree`; `()` if not a git tree.

    The worktree's own git dir (`.git/worktrees/<name>`: its index and HEAD).
    Native drivers stage objects and refs privately and promote their exact
    task ref after audit; granting host ref/reflog namespace directories would also
    expose sibling tasks ([INV-05], `confinement.CommitBoundary.check_path`).
    """
    import subprocess

    if worktree is None:
        return ()
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--absolute-git-dir",
             "--git-common-dir", "--symbolic-full-name", "HEAD"],
            cwd=worktree, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if done.returncode != 0 or len(lines) < 2:
        return ()
    own, common = Path(lines[0]), Path(lines[1])
    dirs = [own]
    ref = lines[2] if len(lines) > 2 else ""
    from maestro.confinement import is_namespaced
    if own == common or not is_namespaced(ref):
        return ()
    return tuple(d for d in dict.fromkeys(dirs) if d.is_dir())


def _worktree_state(cwd: Path | None) -> tuple[str, str] | None:
    """`(HEAD sha, porcelain status)` for a git worktree, or `None` when `cwd` isn't one."""
    import subprocess

    if cwd is None:
        return None
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or status.returncode != 0:
        return None
    return (head.stdout.strip(), status.stdout)


def _adopt_permission_request(args, workspace: Path) -> NodeResult | None:
    """An agent's `PERMISSION_REQUEST` for Dan (P12 pilot finding D30), or `None`.

    Read before the exit is classified: the agent writes it instead of DONE and stops,
    and a denied call may leave any exit behind it. A valid round is `failed` with the
    requests in `outputs.permission_asks`, which the runner screens and turns into a hold
    on Dan (`WorkflowRunner._screen_permission_asks`); a malformed one is `garbage_output`.
    """
    try:
        text = (Path(workspace) / permission_asks.REQUEST_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    requests, error = permission_asks.parse(text)
    if error:
        evidence = f"unusable {permission_asks.REQUEST_FILE}: {error}"
        outputs = {"failure": {"kind": "garbage_output", "evidence": evidence[:4000]}}
        detail = evidence
    else:
        outputs = {"permission_asks": requests}
        detail = "asked Dan to allow: " + permission_asks.summary(requests)
    return NodeResult(
        run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
        status="failed", exit_status=1, detail=detail[:2000], outputs=outputs,
    )


def _classify_cli(driver, completion, log_path: Path, before, after, *, writer: bool = True):
    """P12B F2: the `agent_cli` completion as a failure, or `None` for a clean run.

    Field-notes F1: a reader whose run left the reviewed tree changed broke its read-only
    boundary (a Claude `Bash` call is not covered by its Edit rules). Its output is not a
    review of the frozen subject, so it fails as bad output and the reviewer runs again.
    """
    from maestro.workflows.failures import FailureRecord, classify_exit

    if completion.timed_out:
        return FailureRecord(kind="timeout", evidence=completion.text[:2000])
    tail = ""
    if log_path.exists():
        tail = log_path.read_bytes()[-65536:].decode("utf-8", errors="replace")
    record = classify_exit(driver.parse_exit(completion.returncode, tail))
    if record is not None:
        return record
    if not writer and before is not None and after is not None and before != after:
        return FailureRecord(
            kind="garbage_output",
            evidence=f"a read-only node changed the worktree it reads: {before} -> {after}",
        )
    if not completion.text and before is not None and after is not None and before == after:
        return FailureRecord(kind="empty_output", evidence="")
    return None


def run(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    inbox = AttemptInbox(attempts_root(repo), args.attempt_id)

    # Step 4 first, before any work: if this process is going to write, the lane must
    # name it before it starts, not after it finishes.
    inbox.bind_pid(node_id=args.node_id, run_id=args.run_id)
    inbox.heartbeat(now_iso())

    # Reading the revision is a read, which a client role may do (`[INV-01]`).
    store = ControlStore.open_client(control_db_path(repo))
    try:
        revision = load_revision(store, args.workflow_id, args.revision)
    finally:
        store.close()
    if revision is None:
        result = NodeResult(
            run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
            status="failed", detail=f"no revision {args.workflow_id}@{args.revision}",
            exit_status=2,
        )
        return _finish(inbox, args, result)

    node = revision.node(args.node_id)
    workspace = Path(args.workspace) if args.workspace else inbox.ensure()
    context = HandlerContext(
        run_id=args.run_id,
        task_id=revision.task_id,
        node=node,
        attempt_id=args.attempt_id,
        inbox=inbox,
        workspace=workspace,
        worktree=Path(args.worktree) if args.worktree else None,
        params=node.params,
    )
    try:
        if args.affected_selection_artifact_id:
            # P12A W5: the selection the controller computed at claim.
            context = dataclasses.replace(context, inputs={
                **context.inputs,
                "affected_selection": _read_manifest(repo, args.affected_selection_artifact_id),
            })
        if getattr(args, "post_action_cmd", ""):
            context = dataclasses.replace(context, inputs={
                **context.inputs, "post_action_cmd": args.post_action_cmd,
            })
        if getattr(args, 'manual_contract', ''):
            context = dataclasses.replace(context, inputs={
                **context.inputs, 'manual_contract': json.loads(args.manual_contract),
            })
        if getattr(args, "deny_list_config", ""):
            context = dataclasses.replace(context, inputs={
                **context.inputs, "deny_list": json.loads(args.deny_list_config),
            })
        if getattr(args, "scope_check", ""):
            context = dataclasses.replace(context, inputs={
                **context.inputs, "scope_check": json.loads(args.scope_check),
            })
        if getattr(args, "goals_guard", ""):
            context = dataclasses.replace(context, inputs={
                **context.inputs, "goals_guard": json.loads(args.goals_guard),
            })
        if args.failure_context_artifact_id:
            # P12B F7: the wreckage the diagnoser is being asked to explain.
            context = dataclasses.replace(context, inputs={
                **context.inputs,
                "failure_context": _read_manifest(repo, args.failure_context_artifact_id),
            })
        if args.backend_id:
            manifest = (
                _read_manifest(repo, args.manifest_artifact_id)
                if args.manifest_artifact_id else {}
            )
            if node.agent_definition_ref == ROLE_REVIEWER:
                # Dan, 2026-09-26: which round this review is, and the ids it may cite,
                # for the header's origin contract and the handler's finding ids.
                context = dataclasses.replace(context, inputs={
                    **context.inputs,
                    **{k: manifest[k] for k in ("review_round", "prior_finding_ids",
                                                "goals_check")
                       if k in manifest},
                })
            brief = agent_brief(
                manifest, failure_context=context.inputs.get("failure_context"),
                role=node.agent_definition_ref or "",
                header=task_header(
                    context,
                    reports_by_sentinel=args.runtime_kind != "model_endpoint_harness",
                ),
            )
            if args.runtime_kind == "model_endpoint_harness":
                result = _run_model_endpoint(args, repo, revision, brief, context)
                if node.agent_definition_ref == ROLE_REVIEWER and result.status == "succeeded":
                    # D10 edge 3: judged by its verdict, like a sentinel-reporting reviewer.
                    result = handlers.adopt_review(context, result)
                return _finish(inbox, args, _with_candidate_proof(context, result))
            cwd = context.worktree or context.workspace
            before = _worktree_state(cwd)
            driver, completion = _run_agent_cli(args, context, brief)
            after = _worktree_state(cwd)
            asked = _adopt_permission_request(args, context.workspace)
            if asked is not None:
                return _finish(inbox, args, asked)
            failure = _classify_cli(
                driver, completion, context.inbox.path / "impl.log", before, after,
                writer=node.permissions_ref == "task_worktree_writer",
            )
            if failure is not None:
                result = NodeResult(
                    run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
                    status="failed", exit_status=1,
                    detail=f"{failure.kind}: {failure.evidence}",
                    outputs={"failure": failure.to_dict()},
                )
                return _finish(inbox, args, result)
        result = _with_candidate_proof(context, handlers.resolve(node.handler_ref)(context))
    except Exception as exc:                        # noqa: BLE001 — a worker never leaks
        # An unhandled exception is not a failure verdict: the handler may have completed
        # half of a side effect. `uncertain` is the honest status, and it is what routes
        # the attempt into reconciliation rather than into a blind retry (`A05`).
        result = NodeResult(
            run_id=args.run_id, node_id=args.node_id, attempt_id=args.attempt_id,
            status="uncertain", detail=f"{type(exc).__name__}: {exc}"[:2000], exit_status=1,
        )
    return _finish(inbox, args, result)


def _with_candidate_proof(context: HandlerContext, result: NodeResult) -> NodeResult:
    """A finished candidate writer's evidence and head commit, for the join (`[INV-06]`).

    The candidate join chooses on controller-ingested evidence and a head commit, and a
    candidate without them is never eligible. Nothing else produces them for a candidate:
    the gate runs once, after the join, on the winner. So a candidate's worker runs the
    task's own checks on the candidate's tree once the agent is done, the same way the
    gate's worker does (`verification_handler`, which also refuses an uncommitted tree),
    and reports the rows for the controller to ingest against this attempt. The node's
    own status is left alone: whether the candidate is conclusive is the join's call.
    """
    from maestro.workflows.candidates import CANDIDATE_PARAM

    if (
        result.status != "succeeded"
        or not context.params.get(CANDIDATE_PARAM)
        or context.node.permissions_ref != "task_worktree_writer"
        or context.worktree is None
    ):
        return result
    proof = handlers.verification_handler(context)
    proven = proof.outputs or {}
    outputs = {
        **dict(result.outputs or {}),
        "evidence": list(proven.get("evidence") or []),
        "check_output": dict(proven.get("check_output") or {}),
        "head_commit": str(proven.get("snapshot") or ""),
        "candidate_checks": proof.detail,
    }
    return dataclasses.replace(result, outputs=outputs)


def _finish(inbox: AttemptInbox, args: argparse.Namespace, result: NodeResult) -> int:
    inbox.heartbeat(now_iso())
    inbox.write_result(result)
    inbox.write_receipt(
        operation_id=args.operation_id,
        exit_status=result.exit_status,
        payload={"status": result.status, "node_id": args.node_id},
    )
    return 0 if result.status == "succeeded" else result.exit_status or 1


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
