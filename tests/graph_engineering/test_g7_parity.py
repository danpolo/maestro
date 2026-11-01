"""G7: the legacy behaviours the graph runner still lacked after dd9a2ff (DESIGN §16).

Driven end to end under the real `orchestrator.main` with fixture agents and a mocked
Telegram, like `test_end_to_end.py`, whose harness this reuses:

- 1a: a manual (`dispatch: manual`) task asks Dan for its one step with the prep's own
  `dan_action`, and only lands after his Done, the gate and the merge (Redo re-runs the
  prep with his reason; Abandon fails the run);
- 1b: an async task's `await_job` waits for its awaited artifact, then the gate decides;
- 1c: a resumable task's failed gate saves its progress and cools down;
- 2: `/unpark` (or a due cooldown) opens a fresh run on the kept branch;
- 3: `/approve`, `/reject`, `/redo`, `/fix`, `/backend` answer truthfully under the graph;
- 4: the failed-run notice carries Retry / Shelve buttons that work.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import maestro.state
from maestro.backends.base import Completion
from maestro.hitl import telegram as _telegram
from maestro.hitl import verify as _verify

from .test_dispatch_wiring import _journal_details, _task
from .test_end_to_end import (
    FEATURE,
    TARGET,
    _CommittingAgent,
    _V1,
    _drive_every_worker,
    _drive_every_worker_of_any_run,
    _git,
    _happy_scenario,
    _report_done,
    _run_status,
)
from .test_failure_ladder import _patch_fixture_cli_driver


# ── 1a: the manual lane ──────────────────────────────────────────────────────────────


class _PrepAgent(_CommittingAgent):
    """A prep agent: commits the prep and names Dan's one action in `result.json`."""

    def __init__(self, post_action_cmd=None):
        super().__init__()
        self.post_action_cmd = post_action_cmd

    def complete(self, spec):
        self.specs.append(spec)
        match = re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE)
        workspace = Path(match.group(1))
        cwd = spec.cwd
        if spec.prompt.startswith("You are the implementer "):
            runs = sum(1 for s in self.specs if s.prompt.startswith("You are the implementer "))
            (cwd / FEATURE).write_text(f"prep {runs}\n", encoding="utf-8")
            _git(cwd, "add", FEATURE)
            _git(cwd, "commit", "-q", "-m", f"prep: the migration script ({runs})")
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "result.json").write_text(json.dumps({
                "status": "DONE",
                "dan_action": "printf 'migration ran\\n'",
                "step_command": "printf 'migration ran\\n'",
                "step_check_cmd": "git cat-file -e HEAD^{commit}",
                "expected_result": "migration ran",
                "post_action_cmd": self.post_action_cmd,
            }), encoding="utf-8")
        _report_done(spec, workspace, "prepared")
        return Completion(text="done", returncode=0, timed_out=False)

    def prep_briefs(self) -> list[str]:
        return [s.prompt for s in self.specs if s.prompt.startswith("You are the implementer ")]


class _ActionDan:
    """Answers each manual-step request with the next scripted reply, and watches main."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent: list[tuple[str, dict | None]] = []
        self.answered: set[str] = set()
        self.main_when_asked: list[str] = []

    def send(self, text, markup=None):
        self.sent.append((text, markup))
        return 7000 + len(self.sent)

    def requests(self) -> list[tuple[str, dict | None]]:
        return [(t, m) for t, m in self.sent if m is not None]

    def on_poll(self, run) -> None:
        self.answer(run)
        _drive_every_worker(run)

    def answer(self, run) -> None:
        state = maestro.state.read_state()
        for task_id, entry in (state.get(_verify.STATE_KEY) or {}).items():
            req = entry["request_id"]
            if req in self.answered or not entry.get("sent") or not self.replies:
                continue
            assert entry.get("kind") == _verify.KIND_ACTION
            self.answered.add(req)
            self.main_when_asked.append(_git(run.repo, "rev-parse", TARGET))
            reply = self.replies.pop(0)
            if reply.startswith('done'):
                record = _verify._load(req)
                action = record.get('action', '')
                if '.orchestrator/manual/' in action:
                    import shlex
                    import subprocess
                    script = shlex.split(action)[1]
                    if not Path(script).with_suffix('.exit.json').exists():
                        completed = subprocess.run(['bash', script], capture_output=True, text=True)
                        assert completed.returncode == 0, completed.stderr
            if reply.endswith("-button"):
                index = {"done-button": 0, "redo-button": 1, "abandon-button": 2}[reply]
                _telegram._handle_danreq_callback(
                    {"id": "cq", "data": f"danreq:{req}:{index}", "message": {}})
            else:
                assert "Recorded" in _verify.handle_verify_command(f"{task_id} {reply}")


def _manual_lane(lane, monkeypatch, dan, **task_fields):
    monkeypatch.setattr(_verify, "send_via_bot", dan.send)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    fields = dict(dispatch="manual", mode="needs-dan", short_desc="prepare the migration",
                  verifications=_V1)
    fields.update(task_fields)
    return _happy_scenario(lane, tasks=[_task("TASK-001", **fields)], on_poll=dan.on_poll)


def _seed(run) -> str:
    return _git(run.repo, "rev-list", "--max-parents=0", TARGET)


def test_a_manual_task_asks_dan_then_gates_merges_and_graduates(run_lane, monkeypatch):
    agent = _PrepAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _ActionDan(["done-button"])

    notes = "Run the migration against a copy first.\nNever drop the old table."
    run = run_lane(_manual_lane("g7_manual_done", monkeypatch, dan, notes=notes))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    # One request, carrying the prep's own action with its tree filled in.
    [(text, markup)] = dan.requests()
    tree = run.repo / ".orchestrator" / "worktrees" / "TASK-001"
    assert '.orchestrator/manual/' in text and "migration ran" in text
    assert [b["text"] for row in markup["inline_keyboard"] for b in row] == \
        ["Done", "Redo", "Abandon"]
    # Nothing landed before Dan acted; the prep commit landed after.
    assert dan.main_when_asked == [_seed(run)]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "prep 1"
    assert any("TASK-001" in d for d in _journal_details("task_complete"))
    # The prep agent was given legacy's B14 contract.
    [brief] = agent.prep_briefs()
    assert "PREP CONTRACT" in brief and "dan_action" in brief
    assert "NEVER perform the human step" in brief
    # D28: the prep agent gets the task's notes verbatim too.
    assert "TASK NOTES (" in brief and notes in brief
    assert maestro.state.read_state().get(_verify.STATE_KEY) == {}


def test_a_redo_reruns_the_prep_with_dans_reason_and_asks_again(run_lane, monkeypatch):
    agent = _PrepAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _ActionDan(["redo The script must be idempotent", "done"])

    run = run_lane(_manual_lane("g7_manual_redo", monkeypatch, dan))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert len(dan.requests()) == 2, "the redo's new attempt was not asked anew"
    ids = [m["inline_keyboard"][0][0]["callback_data"] for _, m in dan.requests()]
    assert ids[0] != ids[1]
    briefs = agent.prep_briefs()
    assert len(briefs) == 2
    assert "The script must be idempotent" not in briefs[0]
    assert "The script must be idempotent" in briefs[1]
    assert dan.main_when_asked == [_seed(run), _seed(run)]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "prep 2"


def test_a_bare_redo_asks_for_the_reason_first(run_lane, monkeypatch):
    agent = _PrepAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _ActionDan(["redo-button", "done"])
    reasons = []

    def on_poll(run):
        for text, markup in dan.sent:
            if markup is None and "Reply to this message" in text and not reasons:
                reasons.append(text)
                pending = [
                    p for p in (_telegram.QUESTIONS_DIR).glob("action-*-reason.json")
                ]
                _telegram._record_danreq_answer(pending[0].stem, "use the staging DSN")
        dan.on_poll(run)

    scenario = _manual_lane("g7_manual_bare_redo", monkeypatch, dan)
    import dataclasses

    run = run_lane(dataclasses.replace(scenario, on_poll=on_poll))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert reasons, "a bare Redo did not ask for the reason"
    assert "use the staging DSN" in agent.prep_briefs()[1]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "prep 2"


def test_abandon_fails_the_run_and_sends_the_failed_notice(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _PrepAgent())
    dan = _ActionDan(["abandon-button"])

    run = run_lane(_manual_lane("g7_manual_abandon", monkeypatch, dan))

    assert _run_status() == "failed"
    assert _git(run.repo, "rev-parse", TARGET) == _seed(run)
    [notified] = _journal_details("graph_run_failed_notified")
    assert "TASK-001" in notified and "node=human_action" in notified


def test_the_gate_runs_the_preps_post_action_cmd_first(run_lane, monkeypatch):
    marker = "../post-action-ran"
    _patch_fixture_cli_driver(monkeypatch, _PrepAgent(post_action_cmd=f"touch {marker}"))
    dan = _ActionDan(["done"])

    run = run_lane(_manual_lane("g7_manual_post_action", monkeypatch, dan))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert (run.repo / ".orchestrator" / "worktrees" / "post-action-ran").exists()
    [(text, _)] = dan.requests()
    assert f"touch {marker}" in text


def test_a_redo_cone_reaches_back_to_the_prep_and_an_abandon_has_none():
    from maestro.taskgraph import normalize_spec
    from maestro.workflows import compiler, failures

    revision = compiler.compile_workflow(normalize_spec(
        {"id": "T-1", "title": "t", "status": "pending", "dispatch": "manual"}))
    assert failures.retry_cone(revision, "human_action", kind="verification_failed") == \
        ("prep", "human_action")
    assert failures.retry_cone(revision, "human_action", kind="unknown") == ()


# ── 1b: the async lane ───────────────────────────────────────────────────────────────


def _async_lane(lane, monkeypatch, artifact: Path, *, lands_with: str, after_polls: int):
    """An async task whose awaited check reads `artifact`, which "the job" writes after
    `after_polls` polls. Telegram is mocked; notices are collected on the returned list."""
    from maestro.workflows import handlers

    monkeypatch.setattr(handlers, "AWAIT_POLL_INTERVAL_SEC", 0.0)
    sent: list[str] = []
    monkeypatch.setattr(_verify, "send_via_bot", lambda text, markup=None: sent.append(text))
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    polls = {"n": 0}

    def on_poll(run):
        polls["n"] += 1
        if polls["n"] == after_polls:
            artifact.write_text(lands_with + "\n", encoding="utf-8")
        _drive_every_worker(run)

    check = {"id": "V-AWAIT", "kind": "auto", "cmd": f"cat {artifact}", "expect": "ok",
             "await": True, "await_artifact": str(artifact)}
    scenario = _happy_scenario(
        lane,
        tasks=[_task("TASK-001", dispatch="async-job", launch_cmd="true",
                     short_desc="evaluate the model", verifications=[check])],
        on_poll=on_poll,
    )
    return scenario, sent, polls


def test_an_awaited_check_that_lands_is_gated_and_accepted(run_lane, monkeypatch, tmp_path):
    artifact = tmp_path / "eval-row.txt"
    scenario, sent, polls = _async_lane(
        "g7_async_lands", monkeypatch, artifact, lands_with="ok", after_polls=6)

    run_lane(scenario)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert polls["n"] > 6, "accepted before the awaited artifact existed"
    launched = [t for t in sent if t.startswith("🚀 TASK-001")]
    assert len(launched) == 1, sent
    assert len(_journal_details("await_verify_passed")) == 1
    assert any("TASK-001" in d for d in _journal_details("task_complete"))


def test_an_awaited_check_that_lands_failing_fails_the_run(run_lane, monkeypatch, tmp_path):
    artifact = tmp_path / "eval-row.txt"
    scenario, _, _ = _async_lane(
        "g7_async_hard_failure", monkeypatch, artifact, lands_with="below threshold",
        after_polls=4)

    run_lane(scenario)

    assert _run_status() == "failed"
    [failed] = _journal_details("await_verify_failed")
    assert "V-AWAIT" in failed
    [notified] = _journal_details("graph_run_failed_notified")
    assert "node=await_job" in notified


def test_an_await_past_its_bound_times_out(monkeypatch, tmp_path):
    """Unit: the 8 h bound, measured from the attempt's first poll."""
    from maestro import gates
    from maestro.workflows import handlers
    from maestro.workflows.models import NodeSpec

    artifact = tmp_path / "never.txt"
    task = {"id": "T-1", "verifications": [
        {"id": "V-AWAIT", "kind": "auto", "cmd": f"cat {artifact}", "expect": "ok",
         "await": True, "await_artifact": str(artifact)}]}
    monkeypatch.setattr(gates, "get_task_by_id", lambda _id: task)
    monkeypatch.setattr(maestro.state, "append_journal", lambda *a, **k: None)
    monkeypatch.setattr(handlers, "AWAIT_POLL_INTERVAL_SEC", 0.0)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(handlers, "_now_epoch", lambda: clock["t"])

    class _Controller:
        repo = tmp_path
        dan_channel = None

    node = NodeSpec(node_id="await_job", kind="wait", handler_ref="maestro.handlers.wait",
                    params={"task_id": "T-1", "kind": "awaiting-verification"})
    inbox = handlers.AttemptInbox(tmp_path / "attempts", "att-1")

    def poll():
        ctx = handlers.HandlerContext(
            run_id="r", task_id="T-1", node=node, attempt_id="att-1", inbox=inbox,
            workspace=inbox.ensure(), params=node.params, controller=_Controller())
        result = handlers.wait_handler(ctx)
        inbox.write_result(result)
        return result

    first = poll()
    assert first.status == "paused" and first.outputs["last_checked_epoch"] == clock["t"]
    # Throttled: a re-poll inside the interval does not run the checks again.
    monkeypatch.setattr(handlers, "AWAIT_POLL_INTERVAL_SEC", 60.0)
    clock["t"] += 10
    assert poll().outputs["last_checked_epoch"] == clock["t"] - 10
    monkeypatch.setattr(handlers, "AWAIT_POLL_INTERVAL_SEC", 0.0)
    clock["t"] += gates.AWAIT_VERIFY_TIMEOUT_SEC - 20
    assert poll().status == "paused"
    clock["t"] += 20
    timed_out = poll()
    assert timed_out.status == "failed" and "timeout" in timed_out.detail


# ── 1c: the resumable lane ───────────────────────────────────────────────────────────


class _SittingAgent(_CommittingAgent):
    """Each sitting processes three more items and commits the running count."""

    def __init__(self, total=10, step=3):
        super().__init__()
        self.total, self.step = total, step

    def complete(self, spec):
        self.specs.append(spec)
        match = re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE)
        cwd = spec.cwd
        if spec.prompt.startswith("You are the implementer "):
            progress = cwd / "progress.txt"
            done = int(progress.read_text().split("=")[1].split("/")[0]) if progress.exists() else 0
            done = min(self.total, done + self.step)
            progress.write_text(f"PROGRESS={done}/{self.total}\n", encoding="utf-8")
            _git(cwd, "add", "progress.txt")
            _git(cwd, "commit", "-q", "-m", f"sitting: {done}/{self.total}")
        _report_done(spec, Path(match.group(1)), "sitting over")
        return Completion(text="done", returncode=0, timed_out=False)


_PROGRESS_CHECK = {"id": "V-PROGRESS", "kind": "auto", "cmd": "cat progress.txt",
                   "expect": "PROGRESS=10/10"}


def _resumable_scenario(lane, **overrides):
    fields = dict(
        tasks=[_task("TASK-001", resumable=True, resume_cooldown_h=5,
                     short_desc="process the backlog", verifications=[_PROGRESS_CHECK])],
    )
    fields.update(overrides)
    return _happy_scenario(lane, **fields)


def test_an_incomplete_sitting_saves_progress_and_cools_down(run_lane, monkeypatch):
    agent = _SittingAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)

    run = run_lane(_resumable_scenario("g7_resumable_sitting"))

    assert _run_status() == "failed"
    record = maestro.state.read_state()["resume_state"]["TASK-001"]
    assert (record["last_count"], record["total"], record["attempts"], record["stalls"]) == \
        (3, 10, 1, 0)
    [notified] = _journal_details("graph_run_cooldown_notified")
    assert "TASK-001" in notified and "3/10" in notified
    assert _journal_details("graph_run_failed_notified") == []
    # One sitting per cooldown: the failing gate did not re-run the writer at once.
    assert len([s for s in agent.specs if s.prompt.startswith("You are the implementer ")]) == 1
    # The progress is kept on the task's branch, not merged.
    assert "sitting: 3/10" in _git(run.repo, "log", "--format=%s", "impl/TASK-001")
    assert _git(run.repo, "rev-parse", TARGET) == _seed(run)


def test_a_sitting_without_progress_twice_parks(monkeypatch, tmp_path):
    """Unit: legacy's stall rule, against `resume_state`."""
    from maestro.workflows import handlers
    from maestro.workflows.models import NodeSpec

    state = {"resume_state": {"T-1": {"last_count": 4, "total": 10, "attempts": 3, "stalls": 1}}}
    monkeypatch.setattr(maestro.state, "read_state", lambda: state)
    monkeypatch.setattr(maestro.state, "write_state", lambda s: state.update(s))
    monkeypatch.setattr(maestro.state, "append_journal", lambda *a, **k: None)

    class _Controller:
        repo = tmp_path
        text = "V-PROGRESS failed\nPROGRESS=4/10"

        def gate_failure_text(self):
            return self.text

    node = NodeSpec(node_id="cooldown", kind="wait", handler_ref="maestro.handlers.wait",
                    params={"task_id": "T-1", "cooldown_hours": 5})
    inbox = handlers.AttemptInbox(tmp_path / "attempts", "att-1")

    def run(controller):
        return handlers.wait_handler(handlers.HandlerContext(
            run_id="r", task_id="T-1", node=node, attempt_id="att-1", inbox=inbox,
            workspace=inbox.ensure(), params=node.params, controller=controller))

    parked = run(_Controller())
    assert parked.status == "failed" and "no progress for 2 sittings" in parked.detail
    assert "T-1" not in state["resume_state"]
    unparseable = _Controller()
    unparseable.text = "something broke"
    assert "unparseable" in run(unparseable).detail


# ── item 2: the operator reopens a task ──────────────────────────────────────────────


def _runs() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT run_id, status, finished_at FROM task_runs ORDER BY started_at, rowid"
    ).fetchall()


def _settled() -> int:
    return sum(1 for r in _runs() if r["status"] != "running")


def test_unpark_opens_a_second_run_on_the_kept_branch(run_lane, monkeypatch):
    from maestro.hitl import commands

    import dataclasses

    _patch_fixture_cli_driver(monkeypatch, _PrepAgent())
    dan = _ActionDan(["abandon", "done"])
    seen = {}

    def on_poll(run):
        if _settled() >= 2:
            run.halt()
            return
        runs = _runs()
        if len(runs) == 1 and runs[0]["status"] == "failed":
            seen.setdefault("first", dict(runs[0]))
            seen["polls_without_marker"] = seen.get("polls_without_marker", 0) + 1
            if seen["polls_without_marker"] == 3:
                # No marker, no reopen: three polls later there is still one run.
                commands._process_unpark("TASK-001")
        dan.answer(run)
        _drive_every_worker_of_any_run(run)

    scenario = _manual_lane("g7_unpark_reopens", monkeypatch, dan)
    run = run_lane(dataclasses.replace(scenario, on_poll=on_poll))

    first, second = _runs()
    assert dict(first) == seen["first"], "the failed run was touched by the reopen"
    assert (first["status"], second["status"]) == ("failed", "succeeded")
    assert len(_journal_details("graph_run_reopened")) == 1
    [unparked] = _journal_details("control_unpark")
    assert "graph reopen requested" in unparked
    assert "TASK-001" not in (maestro.state.read_state().get("graph_reopen") or {})
    # The second prep continued on the kept impl/TASK-001: both prep commits landed.
    log = _git(run.repo, "log", "--format=%s", TARGET)
    assert "prep: the migration script (1)" in log and "prep: the migration script (2)" in log


def test_a_cooled_sitting_reopens_once_resume_after_passes(run_lane, monkeypatch):
    import dataclasses

    agent = _SittingAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    seen = {"held_polls": 0}

    def on_poll(run):
        if _settled() >= 2:
            run.halt()
            return
        runs = _runs()
        if len(runs) == 1 and runs[0]["status"] == "failed":
            seen["held_polls"] += 1
            if seen["held_polls"] == 3:
                # The cooldown elapses.
                state = maestro.state.read_state()
                state["resume_state"]["TASK-001"]["resume_after"] = "2000-01-01T00:00:00+00:00"
                maestro.state.write_state(state)
        _drive_every_worker_of_any_run(run)

    run = run_lane(dataclasses.replace(
        _resumable_scenario("g7_resumable_reopen"), on_poll=on_poll))

    assert seen["held_polls"] >= 3, "the task reopened before resume_after"
    assert [r["status"] for r in _runs()] == ["failed", "failed"]
    record = maestro.state.read_state()["resume_state"]["TASK-001"]
    assert (record["last_count"], record["attempts"]) == (6, 2)
    assert len(_journal_details("graph_run_reopened")) == 1
    log = _git(run.repo, "log", "--format=%s", "impl/TASK-001")
    assert "sitting: 3/10" in log and "sitting: 6/10" in log


class _Commands:
    """`maestro.hitl.commands` with its state, journal and Telegram in memory."""

    def __init__(self, monkeypatch, *, graph: bool, state: dict | None = None):
        from maestro.hitl import commands

        self.module = commands
        self.state = state if state is not None else {}
        self.sent: list[str] = []
        self.journal: list[tuple[str, str]] = []
        monkeypatch.setattr(commands, "read_state", lambda: self.state)
        monkeypatch.setattr(commands, "write_state", lambda s: None)
        monkeypatch.setattr(commands, "notify_telegram", lambda text, *a, **k: self.sent.append(text))
        monkeypatch.setattr(commands, "append_journal",
                            lambda event, detail="", *a, **k: self.journal.append((event, detail)))
        monkeypatch.setattr(commands, "_graph_runner_active", lambda: graph)
        monkeypatch.setattr(_verify, "_default_read_state", lambda: self.state)


def test_unpark_under_the_graph_runner_writes_the_reopen_marker(monkeypatch):
    c = _Commands(monkeypatch, graph=True, state={"parked_tasks": ["T-9"]})
    c.module._process_unpark("T-1")  # a failed graph run parks nothing in parked_tasks
    assert "T-1" in c.state["graph_reopen"]
    assert "fresh graph run" in c.sent[-1] and "impl/T-1" in c.sent[-1]
    c.module._process_unpark("T-9")
    assert c.state["parked_tasks"] == [] and "T-9" in c.state["graph_reopen"]


def test_unpark_under_the_legacy_runner_is_unchanged(monkeypatch):
    c = _Commands(monkeypatch, graph=False, state={"parked_tasks": ["T-9"]})
    c.module._process_unpark("T-1")
    assert "is not parked" in c.sent[-1] and "graph_reopen" not in c.state
    c.module._process_unpark("T-9")
    assert c.sent[-1] == "✅ T-9 unparked — will be launched on next cycle."
    assert "graph_reopen" not in c.state


# ── item 3: the operator verbs under the graph runner ────────────────────────────────


def _waiting(c, tmp_path, monkeypatch, kind: str) -> str:
    """One graph request waiting on T-1, of `kind` (`action` or `verification`)."""
    monkeypatch.setattr(_telegram, "QUESTIONS_DIR", tmp_path / "questions")
    monkeypatch.setattr(_telegram, "append_journal", lambda *a, **k: None)
    req = f"{'action' if kind == 'action' else 'verify'}-T-1-abc"
    c.state[_verify.STATE_KEY] = {"T-1": {
        "run_id": "run_1", "snapshot": "" if kind == "action" else "0123456789abcdef",
        "request_id": req, "title": "", "requested_at": "", "sent": True, "kind": kind,
    }}
    return req


def _answer_file(tmp_path, req) -> str:
    return (tmp_path / "questions" / f"{req}.answer").read_text().strip()


def test_approve_is_done_on_a_manual_step(monkeypatch, tmp_path):
    c = _Commands(monkeypatch, graph=True)
    req = _waiting(c, tmp_path, monkeypatch, "action")
    assert c.module._graph_answer("approve", "T-1") is True
    assert _answer_file(tmp_path, req) == "Done"
    assert "Recorded done" in c.sent[-1]


def test_reject_is_abandon_on_a_manual_step(monkeypatch, tmp_path):
    c = _Commands(monkeypatch, graph=True)
    req = _waiting(c, tmp_path, monkeypatch, "action")
    assert c.module._graph_answer("reject", "T-1") is True
    assert _answer_file(tmp_path, req) == "Abandon"


def test_redo_carries_the_reason_to_a_manual_step(monkeypatch, tmp_path):
    c = _Commands(monkeypatch, graph=True)
    req = _waiting(c, tmp_path, monkeypatch, "action")
    assert c.module._graph_answer("redo", "T-1 the DSN is wrong") is True
    assert _answer_file(tmp_path, req) == "Redo: the DSN is wrong"


def test_approve_and_reject_answer_a_dan_verification(monkeypatch, tmp_path):
    c = _Commands(monkeypatch, graph=True)
    req = _waiting(c, tmp_path, monkeypatch, "verification")
    assert c.module._graph_answer("reject", "T-1") is True
    assert "A reason is required" in c.sent[-1]
    assert c.module._graph_answer("reject", "T-1 it still crashes") is True
    assert _answer_file(tmp_path, req) == "Reject: it still crashes"


def test_approve_with_nothing_waiting_points_to_verify_and_waiting(monkeypatch, tmp_path):
    c = _Commands(monkeypatch, graph=True)
    assert c.module._graph_answer("approve", "T-1") is True
    assert "no merge-review queue" in c.sent[-1] and "/waiting" in c.sent[-1]
    # A legacy numeric id that exists keeps its legacy meaning.
    c.state["waiting_on_dan"] = {"7": {"task_id": "T-1"}}
    assert c.module._graph_answer("approve", "7") is False
    # A redo with nothing graph-side waiting falls back to legacy's /redo.
    assert c.module._graph_answer("redo", "T-1 do it again") is False


def test_the_legacy_runner_never_takes_the_graph_path(monkeypatch, tmp_path):
    c = _Commands(monkeypatch, graph=False)
    _waiting(c, tmp_path, monkeypatch, "action")
    assert c.module._graph_answer("approve", "T-1") is False


def test_fix_and_backend_say_what_the_graph_does_instead(monkeypatch):
    c = _Commands(monkeypatch, graph=True)
    assert "diagnose step" in c.module._graph_fix_reply("T-1")
    assert "/unpark T-1" in c.module._graph_fix_reply("T-1")
    name = c.module.known_backends()[0]
    c.module._process_backend(name)
    assert "would do nothing" in c.sent[-1] and "backend" not in c.state
    c.module._process_backend(f"{name} T-1")
    assert "no in-flight session to move" in c.sent[-1]


def test_the_verbs_are_routed_to_the_graph_answers(monkeypatch, tmp_path):
    """Through `poll_control_commands`, with Telegram's getUpdates faked."""
    import subprocess as _subprocess

    c = _Commands(monkeypatch, graph=True)
    req = _waiting(c, tmp_path, monkeypatch, "action")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "42")
    updates = {"ok": True, "result": [
        {"update_id": 900, "message": {"chat": {"id": 42}, "text": "/fix T-1"}},
        {"update_id": 901, "message": {"chat": {"id": 42}, "text": "/redo T-1 wrong DSN"}},
    ]}
    monkeypatch.setattr(c.module.subprocess, "run", lambda *a, **k: _subprocess.CompletedProcess(
        a, 0, stdout=json.dumps(updates), stderr=""))
    monkeypatch.setattr(c.module, "_ingest_control_command", lambda *a, **k: None)
    monkeypatch.setattr(c.module, '_fetch_updates', lambda *a, **k: updates['result'])
    monkeypatch.setattr(c.module, "_settle_control_command", lambda *a, **k: None)

    c.module.poll_control_commands([])

    assert "diagnose step" in c.sent[0]
    assert _answer_file(tmp_path, req) == "Redo: wrong DSN"


# ── item 4: the failed-run notice's buttons ──────────────────────────────────────────


def _failed_notice_lane(lane, monkeypatch, button: str):
    """A manual task Dan abandons; he then taps `button` on the failed-run notice."""
    import dataclasses

    _patch_fixture_cli_driver(monkeypatch, _PrepAgent())
    dan = _ActionDan(["abandon", "done"])
    tapped = {"polls_after": 0}

    def on_poll(run):
        runs = _runs()
        if _settled() >= 2 or (button == "Shelve" and tapped["polls_after"] >= 4):
            run.halt()
            return
        if "req" in tapped:
            tapped["polls_after"] += 1
        failed = [(t, m) for t, m in dan.sent if t.startswith("✗")]
        if failed and "req" not in tapped:
            [(text, markup)] = failed
            tapped["text"], tapped["markup"] = text, markup
            tapped["req"] = markup["inline_keyboard"][0][0]["callback_data"].split(":")[1]
            index = {"Retry": 0, "Shelve": 1}[button]
            _telegram._handle_danreq_callback(
                {"id": "cq", "data": f"danreq:{tapped['req']}:{index}", "message": {}})
        dan.answer(run)
        _drive_every_worker_of_any_run(run)

    return dataclasses.replace(_manual_lane(lane, monkeypatch, dan), on_poll=on_poll), dan, tapped


def test_the_failed_notice_offers_retry_and_retry_reopens(run_lane, monkeypatch):
    scenario, dan, tapped = _failed_notice_lane("g7_failed_retry", monkeypatch, "Retry")

    run_lane(scenario)

    assert tapped["text"].startswith("✗ TASK-001 failed at human_action — parked (run ")
    assert "Reason: " in tapped["text"] and "/unpark TASK-001 retries" in tapped["text"]
    assert [b["text"] for row in tapped["markup"]["inline_keyboard"] for b in row] == \
        ["Retry", "Shelve"]
    assert [r["status"] for r in _runs()] == ["failed", "succeeded"]
    assert len(_journal_details("graph_failed_retry")) == 1
    assert len(_journal_details("graph_run_reopened")) == 1
    assert len([t for t, _ in dan.sent if t.startswith("✗")]) == 1, "the notice was re-sent"


def test_shelve_parks_the_task_until_unpark(run_lane, monkeypatch):
    scenario, _, _ = _failed_notice_lane("g7_failed_shelve", monkeypatch, "Shelve")

    run_lane(scenario)

    assert [r["status"] for r in _runs()] == ["failed"]
    state = maestro.state.read_state()
    assert "TASK-001" in state["parked_tasks"]
    assert len(_journal_details("graph_failed_shelved")) == 1


def test_a_failing_post_action_cmd_fails_the_gate_with_its_output(tmp_path):
    from maestro.workflows import handlers

    assert handlers._run_post_action_cmd("true", tmp_path) == ""
    failed = handlers._run_post_action_cmd("echo boom; exit 3", tmp_path)
    assert failed.startswith("post_action_cmd exited 3") and "boom" in failed
