"""Pinned behaviour of the ROADMAP document layer.

Characterisation, not specification. Every assertion here was derived by running the
reference implementation; where it does something surprising the test pins the surprise.

Everything that shells out (``run_dep_map``, ``maybe_push_roadmap_map_change``,
``mark_roadmap_complete``) runs against a fake ``subprocess`` module and is asserted on
the argv it *would* have run. No test may execute a real command, and no test may touch
a real repository: all document state lives under the ``sandbox`` temp tree.
"""
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.maestro_module("docs.roadmap")


# --- fixtures builders --------------------------------------------------------------
#
# The shapes below are the ones the reference project's docs/ROADMAP.md actually
# contains, transcribed with neutral content: a manual-dispatch task with a
# post-action command, a bare `mode: needs-dan` task with no dispatch, two autonomous
# tasks holding a `resource` mutex, and a manual task with an empty dep list.

SHAPE_MANUAL_WITH_POST_ACTION = """
id: T-MANUAL
title: A task only a human can perform
short_desc: Long prose describing the deliverable.
est_time: ~30 min
mode: needs-dan
dispatch: manual  # a human must perform this step
dan_action: "Open the notebook and run all cells, then report back."
post_action_cmd: "python3 scripts/import_results.py"
eval_relevance: retrieval
deps: [T-DONE]
verifications:
  - id: V1
    kind: auto
    check: "table is populated"
    cmd: "python3 scripts/check.py"
    expect: "exists"
  - id: V2
    kind: manual
    check: "qualitative human check"
"""

SHAPE_NEEDS_DAN_NO_DISPATCH = """
id: T-NEEDSDAN
title: Needs a human but declares no dispatch
short_desc: Prose.
est_time: ~2h
mode: needs-dan
eval_relevance: retrieval
deps: [T-DONE]
verifications:
  - id: V1
    kind: auto
    await: true
    check: "metric does not drop"
    cmd: "python3 scripts/check_eval_gate.py"
    expect: "ok"
"""

SHAPE_AUTONOMOUS_RESOURCE_TWO_DEPS = """
id: T-AUTO2
title: Autonomous task holding a resource mutex
short_desc: Prose.
est_time: ~6-9h CPU
mode: autonomous
eval_relevance: retrieval
resource: eval-cpu
deps: [T-DONE, T-ALSO-DONE]
verifications:
  - id: V1
    kind: auto
    check: "metric does not regress"
    cmd: "python3 scripts/check_eval_gate.py"
    expect: "ok"
"""

SHAPE_AUTONOMOUS_RESOURCE_ONE_DEP = """
id: T-AUTO1
title: Autonomous task with one dep
short_desc: Prose.
est_time: ~6-9h CPU
mode: autonomous
eval_relevance: retrieval
resource: eval-cpu
deps: [T-DONE]
verifications:
  - id: V1
    kind: auto
    check: "metric does not regress"
    cmd: "python3 scripts/check_eval_gate.py"
    expect: "ok"
  - id: V2
    kind: manual
    check: "latency reduced"
"""

SHAPE_MANUAL_EMPTY_DEPS = """
id: T-MANUAL0
title: Manual task with no deps at all
short_desc: Prose.
est_time: ~1 day
mode: needs-dan
dispatch: manual  # the deliverable requires human judgement
dan_action: "Build and run the curation pipeline."
eval_relevance: non-retrieval  # trailing comment on a scalar
deps: []
verifications:
  - id: V1
    kind: auto
    check: "file exists with >=80 entries"
    cmd: "python3 scripts/validate.py"
    expect: "ok"
"""

REAL_SHAPES = [
    SHAPE_MANUAL_WITH_POST_ACTION,
    SHAPE_NEEDS_DAN_NO_DISPATCH,
    SHAPE_AUTONOMOUS_RESOURCE_TWO_DEPS,
    SHAPE_AUTONOMOUS_RESOURCE_ONE_DEP,
    SHAPE_MANUAL_EMPTY_DEPS,
]


def write_roadmap(subject, *blocks, preamble="# Roadmap\n\nSome prose.\n\n"):
    """Write a ROADMAP.md fixture made of ```yaml fenced blocks."""
    path = subject.ROADMAP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"```yaml\n{b.strip()}\n```\n\n" for b in blocks)
    path.write_text(preamble + body, encoding="utf-8")
    return path


def write_raw_roadmap(subject, text):
    path = subject.ROADMAP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_completed(subject, *entries):
    """entries are either bare ids or full dicts."""
    payload = [e if isinstance(e, dict) else {"id": e} for e in entries]
    subject.COMPLETED_TASKS.parent.mkdir(parents=True, exist_ok=True)
    subject.COMPLETED_TASKS.write_text(json.dumps(payload), encoding="utf-8")


def stub_prep_actions(subject, monkeypatch, prepared=()):
    """Replace the prepared-actions sidecar so no test reads the live JSON store."""
    prepared = {str(t) for t in prepared}
    monkeypatch.setattr(
        subject,
        "prep_actions",
        SimpleNamespace(has_action=lambda tid, *a, **k: str(tid) in prepared),
    )


class FakeSubprocess:
    """Records argv instead of running anything."""

    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError
    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL

    def __init__(self, rc=None, raises=None, stdout=""):
        self.calls = []
        self._rc = rc or (lambda argv: 0)
        self._raises = raises or (lambda argv: None)
        self._stdout = stdout

    def run(self, argv, **kwargs):
        argv = list(argv) if not isinstance(argv, str) else argv
        self.calls.append(SimpleNamespace(argv=argv, kwargs=kwargs))
        exc = self._raises(argv)
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=self._rc(argv), stdout=self._stdout, stderr="")

    @property
    def argvs(self):
        return [c.argv for c in self.calls]


def no_subprocess(subject, monkeypatch, **kw):
    fake = FakeSubprocess(**kw)
    monkeypatch.setattr(subject, "subprocess", fake)
    # `maybe_push_roadmap_map_change` calls `notify_telegram_with_map`, which in the
    # extracted package lives in `maestro.hitl.telegram` and therefore shells out through
    # *that* module's `subprocess` — stubbing only the subject's would let a real `bash`
    # and a real `curl` to api.telegram.org out of the sandbox. The legacy subject is one
    # flat namespace that never imports the module, so the lookup is guarded.
    telegram = sys.modules.get("maestro.hitl.telegram")
    if telegram is not None and telegram is not subject:
        monkeypatch.setattr(telegram, "subprocess", fake)
    return fake


# --- get_completed_task_ids ---------------------------------------------------------


def test_completed_ids_missing_file_is_empty_set(subject, sandbox):
    assert subject.get_completed_task_ids() == set()


def test_completed_ids_reads_the_id_of_each_entry(subject, sandbox):
    write_completed(
        subject,
        {"id": "B4", "title": "x", "completed_at": "2026-06-04T00:00:00Z"},
        {"id": "B5", "title": "y", "graduated_to": "docs/PROJECT.md"},
    )
    assert subject.get_completed_task_ids() == {"B4", "B5"}


def test_completed_ids_are_stringified(subject, sandbox):
    write_completed(subject, {"id": 12}, {"id": 3.5}, {"id": True})
    assert subject.get_completed_task_ids() == {"12", "3.5", "True"}


def test_completed_ids_malformed_json_is_empty_set(subject, sandbox):
    subject.COMPLETED_TASKS.parent.mkdir(parents=True, exist_ok=True)
    subject.COMPLETED_TASKS.write_text("{not json", encoding="utf-8")
    assert subject.get_completed_task_ids() == set()


def test_completed_ids_one_bad_entry_discards_every_good_one(subject, sandbox):
    """FOUND_BUGS: the set comprehension is all-or-nothing — a single entry missing
    `id` silently reports that *nothing* is complete, which makes every dependent task
    look unrunnable rather than failing loudly."""
    subject.COMPLETED_TASKS.parent.mkdir(parents=True, exist_ok=True)
    subject.COMPLETED_TASKS.write_text(
        json.dumps([{"id": "B4"}, {"title": "no id here"}]), encoding="utf-8"
    )
    assert subject.get_completed_task_ids() == set()


def test_completed_ids_json_object_instead_of_list_is_empty_set(subject, sandbox):
    subject.COMPLETED_TASKS.parent.mkdir(parents=True, exist_ok=True)
    subject.COMPLETED_TASKS.write_text('{"B4": {"id": "B4"}}', encoding="utf-8")
    assert subject.get_completed_task_ids() == set()


def test_completed_ids_empty_list_is_empty_set(subject, sandbox):
    write_completed(subject)
    assert subject.get_completed_task_ids() == set()


# --- _normalize_task ----------------------------------------------------------------


def test_normalize_task_promotes_needs_dan_to_manual_dispatch(subject):
    task = {"id": "X", "mode": "needs-dan"}
    assert subject._normalize_task(task)["dispatch"] == "manual"


def test_normalize_task_mutates_its_argument_in_place(subject):
    """The return value and the argument are the same object."""
    task = {"id": "X", "mode": "needs-dan"}
    assert subject._normalize_task(task) is task
    assert task["dispatch"] == "manual"


def test_normalize_task_respects_an_explicit_dispatch(subject):
    task = {"id": "X", "mode": "needs-dan", "dispatch": "auto"}
    assert subject._normalize_task(task)["dispatch"] == "auto"


def test_normalize_task_overrides_a_falsy_explicit_dispatch(subject):
    """FOUND_BUGS: `dispatch: ""` (or null, or false) is not "explicit" — the guard is
    truthiness, so an empty dispatch is silently rewritten to manual."""
    assert subject._normalize_task({"id": "X", "mode": "needs-dan", "dispatch": ""})[
        "dispatch"
    ] == "manual"


def test_normalize_task_leaves_other_modes_alone(subject):
    assert "dispatch" not in subject._normalize_task({"id": "X", "mode": "autonomous"})


def test_normalize_task_is_none_safe(subject):
    assert subject._normalize_task(None) is None


def test_normalize_task_passes_non_dicts_through(subject):
    assert subject._normalize_task(["a", "b"]) == ["a", "b"]
    assert subject._normalize_task("scalar") == "scalar"


# --- block extraction: the fence regex ----------------------------------------------


def test_blocks_are_found_by_a_non_anchored_regex(subject, sandbox):
    """FOUND_BUGS: the fence regex is not line-anchored, so an *indented* fence (a code
    block inside a list item, i.e. documentation rather than a live task) parses as a
    real task."""
    write_raw_roadmap(
        subject,
        "- example:\n\n    ```yaml\n    id: T-INDENTED\n    mode: autonomous\n    ```\n",
    )
    assert [t["id"] for t in subject._load_roadmap_tasks()] == ["T-INDENTED"]


def test_a_space_after_the_backticks_hides_the_block(subject, sandbox):
    write_raw_roadmap(subject, "``` yaml\nid: T-X\nmode: autonomous\n```\n")
    assert subject._load_roadmap_tasks() == []


def test_other_fence_languages_are_ignored(subject, sandbox):
    write_raw_roadmap(subject, "```json\n{\"id\": \"T-X\"}\n```\n")
    assert subject._load_roadmap_tasks() == []


def test_empty_yaml_block_is_skipped(subject, sandbox):
    write_raw_roadmap(subject, "```yaml\n```\n")
    assert subject._load_roadmap_tasks() == []


def test_non_mapping_yaml_block_is_skipped(subject, sandbox):
    write_raw_roadmap(subject, "```yaml\n- one\n- two\n```\n")
    assert subject._load_roadmap_tasks() == []


def test_malformed_yaml_block_is_skipped_not_raised(subject, sandbox):
    bad = "id: T-BAD\nmode: autonomous\n\tdeps: [x]\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(bad)
    write_raw_roadmap(subject, f"```yaml\n{bad}```\n")
    assert subject._load_roadmap_tasks() == []


def test_a_malformed_block_does_not_hide_later_blocks(subject, sandbox):
    write_raw_roadmap(
        subject,
        "```yaml\nid: T-BAD\n\tbroken: 1\n```\n\n```yaml\nid: T-GOOD\nmode: autonomous\n```\n",
    )
    assert [t["id"] for t in subject._load_roadmap_tasks()] == ["T-GOOD"]


def test_a_block_without_an_id_is_skipped(subject, sandbox):
    write_raw_roadmap(subject, "```yaml\ntitle: no id\nmode: autonomous\n```\n")
    assert subject._load_roadmap_tasks() == []


def test_a_falsy_id_is_treated_as_no_id(subject, sandbox):
    """FOUND_BUGS: `id: 0` and `id: ""` are dropped by the truthiness guard — a task
    numbered zero silently vanishes from every parse."""
    write_raw_roadmap(
        subject,
        '```yaml\nid: 0\nmode: autonomous\n```\n\n```yaml\nid: ""\nmode: autonomous\n```\n',
    )
    assert subject._load_roadmap_tasks() == []


# --- _load_roadmap_tasks ------------------------------------------------------------


def test_load_roadmap_tasks_returns_every_block_in_document_order(subject, sandbox):
    write_roadmap(subject, *REAL_SHAPES)
    assert [t["id"] for t in subject._load_roadmap_tasks()] == [
        "T-MANUAL",
        "T-NEEDSDAN",
        "T-AUTO2",
        "T-AUTO1",
        "T-MANUAL0",
    ]


def test_load_roadmap_tasks_normalizes_needs_dan(subject, sandbox):
    write_roadmap(subject, SHAPE_NEEDS_DAN_NO_DISPATCH)
    assert subject._load_roadmap_tasks()[0]["dispatch"] == "manual"


def test_load_roadmap_tasks_does_not_filter_completed(subject, sandbox):
    write_roadmap(subject, "id: T-C\nmode: autonomous\nstatus: complete\n")
    assert [t["id"] for t in subject._load_roadmap_tasks()] == ["T-C"]


def test_load_roadmap_tasks_missing_file_returns_empty_list(subject, sandbox):
    assert subject._load_roadmap_tasks() == []


def test_load_roadmap_tasks_keeps_duplicate_ids(subject, sandbox):
    write_roadmap(
        subject, "id: T-DUP\nmode: autonomous\ntitle: first\n",
        "id: T-DUP\nmode: autonomous\ntitle: second\n",
    )
    assert [t["title"] for t in subject._load_roadmap_tasks()] == ["first", "second"]


def test_load_roadmap_tasks_preserves_nested_verification_structure(subject, sandbox):
    write_roadmap(subject, SHAPE_NEEDS_DAN_NO_DISPATCH)
    verifs = subject._load_roadmap_tasks()[0]["verifications"]
    assert verifs[0] == {
        "id": "V1",
        "kind": "auto",
        "await": True,
        "check": "metric does not drop",
        "cmd": "python3 scripts/check_eval_gate.py",
        "expect": "ok",
    }


# --- get_task_by_id -----------------------------------------------------------------


def test_get_task_by_id_returns_the_full_dict(subject, sandbox):
    write_roadmap(subject, *REAL_SHAPES)
    task = subject.get_task_by_id("T-AUTO2")
    assert task["resource"] == "eval-cpu"
    assert task["deps"] == ["T-DONE", "T-ALSO-DONE"]


def test_get_task_by_id_returns_none_when_absent(subject, sandbox):
    write_roadmap(subject, *REAL_SHAPES)
    assert subject.get_task_by_id("NOPE") is None


def test_get_task_by_id_normalizes_the_result(subject, sandbox):
    write_roadmap(subject, SHAPE_NEEDS_DAN_NO_DISPATCH)
    assert subject.get_task_by_id("T-NEEDSDAN")["dispatch"] == "manual"


def test_get_task_by_id_compares_ids_as_strings(subject, sandbox):
    write_roadmap(subject, "id: 42\nmode: autonomous\n")
    assert subject.get_task_by_id("42")["id"] == 42
    assert subject.get_task_by_id(42)["id"] == 42


def test_get_task_by_id_returns_the_first_of_duplicate_ids(subject, sandbox):
    write_roadmap(
        subject, "id: T-DUP\nmode: autonomous\ntitle: first\n",
        "id: T-DUP\nmode: autonomous\ntitle: second\n",
    )
    assert subject.get_task_by_id("T-DUP")["title"] == "first"


def test_get_task_by_id_finds_a_completed_task(subject, sandbox):
    """No status filtering here — unlike parse_runnable_tasks."""
    write_roadmap(subject, "id: T-C\nmode: autonomous\nstatus: complete\n")
    assert subject.get_task_by_id("T-C") is not None


def test_get_task_by_id_raises_when_the_roadmap_is_missing(subject, sandbox):
    """FOUND_BUGS: _load_roadmap_tasks swallows OSError, get_task_by_id does not."""
    with pytest.raises(FileNotFoundError):
        subject.get_task_by_id("T-X")


def test_get_task_by_id_ignores_a_block_with_a_falsy_id(subject, sandbox):
    write_roadmap(subject, "id: 0\nmode: autonomous\n")
    assert subject.get_task_by_id("0") is not None  # no truthiness guard in this one


# --- parse_runnable_tasks -----------------------------------------------------------


def test_runnable_real_shapes_with_no_deps_complete(subject, sandbox):
    write_roadmap(subject, *REAL_SHAPES)
    assert subject.parse_runnable_tasks() == []


def test_runnable_real_shapes_once_deps_are_complete(subject, sandbox):
    write_roadmap(subject, *REAL_SHAPES)
    write_completed(subject, "T-DONE", "T-ALSO-DONE")
    # T-MANUAL / T-MANUAL0 are dispatch:manual, T-NEEDSDAN is normalized to manual.
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T-AUTO2", "T-AUTO1"]


def test_runnable_partial_deps_block_the_task(subject, sandbox):
    write_roadmap(subject, *REAL_SHAPES)
    write_completed(subject, "T-DONE")
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T-AUTO1"]


def test_runnable_preserves_document_order(subject, sandbox):
    write_roadmap(
        subject,
        "id: B\nmode: autonomous\n",
        "id: A\nmode: autonomous\n",
        "id: C\nmode: autonomous\n",
    )
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["B", "A", "C"]


@pytest.mark.parametrize(
    "block, runnable",
    [
        ("id: T\nmode: autonomous\n", True),
        ("id: T\nmode: needs-dan\ndispatch: auto\n", True),
        ("id: T\nmode: needs-dan\n", False),                      # normalized to manual
        ("id: T\nmode: needs-dan\ndispatch: manual\n", False),
        ("id: T\nmode: autonomous\ndispatch: manual\n", False),
        ("id: T\nmode: interactive\n", False),
        ("id: T\n", False),                                        # no mode at all
        ("id: T\nmode: Autonomous\n", False),                      # case sensitive
        ("id: T\nmode: autonomous\nstatus: complete\n", False),
        ("id: T\nmode: autonomous\nstatus: in_progress\n", True),
        ("id: T\nmode: autonomous\nself_modifying: true\n", False),
        ("id: T\nmode: autonomous\nself_modifying: false\n", True),
        ("id: T\nmode: autonomous\nhold: true\n", False),
        ("id: T\nmode: autonomous\nhold: false\n", True),
        ("id: T\nmode: autonomous\nhold: 'no'\n", False),          # truthy string
        ("id: T\nmode: autonomous\ndispatch: manual-ish\n", True),  # only exact 'manual'
        ("id: T\nmode: autonomous\ndeps:\n", True),                 # null deps
        ("id: T\nmode: autonomous\ndeps: []\n", True),
        ("id: T\nmode: autonomous\ndeps: ''\n", True),              # empty string deps
    ],
)
def test_runnable_status_and_flag_matrix(subject, sandbox, block, runnable):
    write_roadmap(subject, block)
    assert bool(subject.parse_runnable_tasks()) is runnable


def test_runnable_deps_given_as_a_bare_string(subject, sandbox):
    write_roadmap(subject, "id: T\nmode: autonomous\ndeps: DEP1\n")
    assert subject.parse_runnable_tasks() == []
    write_completed(subject, "DEP1")
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


def test_runnable_deps_are_compared_as_strings(subject, sandbox):
    write_roadmap(subject, "id: T\nmode: autonomous\ndeps: [12]\n")
    write_completed(subject, {"id": 12})
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


def test_runnable_a_dep_string_is_not_split_into_characters(subject, sandbox):
    """A bare string dep is wrapped, not iterated — `deps: AB` needs task `AB`."""
    write_roadmap(subject, "id: T\nmode: autonomous\ndeps: AB\n")
    write_completed(subject, "A", "B")
    assert subject.parse_runnable_tasks() == []


def test_runnable_treats_in_roadmap_completions_as_satisfied_deps(subject, sandbox):
    """A `status: complete` block still in the ROADMAP counts towards dep satisfaction
    even though completed_tasks.json has never heard of it."""
    write_roadmap(
        subject,
        "id: DEP\nmode: autonomous\nstatus: complete\n",
        "id: T\nmode: autonomous\ndeps: [DEP]\n",
    )
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


def test_runnable_in_roadmap_completion_counts_regardless_of_block_order(subject, sandbox):
    write_roadmap(
        subject,
        "id: T\nmode: autonomous\ndeps: [DEP]\n",
        "id: DEP\nmode: autonomous\nstatus: complete\n",
    )
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


def test_runnable_a_manual_completed_task_also_satisfies_deps(subject, sandbox):
    write_roadmap(
        subject,
        "id: DEP\nmode: needs-dan\nstatus: complete\n",
        "id: T\nmode: autonomous\ndeps: [DEP]\n",
    )
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


def test_runnable_unknown_dep_is_never_satisfied(subject, sandbox):
    write_roadmap(subject, "id: T\nmode: autonomous\ndeps: [GHOST]\n")
    assert subject.parse_runnable_tasks() == []


def test_runnable_a_task_may_depend_on_itself_forever(subject, sandbox):
    """FOUND_BUGS: no cycle detection — a self-dep is simply never runnable, silently."""
    write_roadmap(subject, "id: T\nmode: autonomous\ndeps: [T]\n")
    assert subject.parse_runnable_tasks() == []


def test_runnable_returns_the_same_dict_objects_it_parsed(subject, sandbox):
    write_roadmap(subject, "id: T\nmode: autonomous\ntitle: hello\n")
    got = subject.parse_runnable_tasks()[0]
    assert got["title"] == "hello"
    got["title"] = "mutated"
    assert subject.parse_runnable_tasks()[0]["title"] == "hello"  # re-read from disk


def test_runnable_ignores_malformed_and_idless_blocks(subject, sandbox):
    write_raw_roadmap(
        subject,
        "```yaml\n\tbad\n```\n\n```yaml\nmode: autonomous\n```\n\n"
        "```yaml\nid: T\nmode: autonomous\n```\n",
    )
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


def test_runnable_raises_when_the_roadmap_is_missing(subject, sandbox):
    """FOUND_BUGS: the poll loop's primary parser has no OSError guard, unlike
    _load_roadmap_tasks which returns []."""
    with pytest.raises(FileNotFoundError):
        subject.parse_runnable_tasks()


def test_runnable_empty_roadmap_is_empty_list(subject, sandbox):
    write_raw_roadmap(subject, "# Roadmap\n\nNothing here yet.\n")
    assert subject.parse_runnable_tasks() == []


def test_runnable_does_not_consult_state_json(subject, sandbox):
    """parse_runnable_tasks works with no state.json at all; parse_prep_tasks does not."""
    write_roadmap(subject, "id: T\nmode: autonomous\n")
    assert not subject.STATE_JSON.exists()
    assert [t["id"] for t in subject.parse_runnable_tasks()] == ["T"]


# --- parse_prep_tasks ---------------------------------------------------------------


def prep_state(subject, waiting_on_dan=None, parked_tasks=None):
    subject.write_state(
        {
            "version": "1",
            "in_flight": [],
            "waiting_on_dan": waiting_on_dan or {},
            "parked_tasks": parked_tasks or [],
        }
    )


def test_prep_real_shapes_once_deps_are_complete(subject, sandbox, monkeypatch):
    write_roadmap(subject, *REAL_SHAPES)
    write_completed(subject, "T-DONE", "T-ALSO-DONE")
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == [
        "T-MANUAL",
        "T-NEEDSDAN",
        "T-MANUAL0",
    ]


def test_prep_is_the_exact_complement_of_runnable_for_the_real_shapes(subject, sandbox, monkeypatch):
    write_roadmap(subject, *REAL_SHAPES)
    write_completed(subject, "T-DONE", "T-ALSO-DONE")
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    runnable = {t["id"] for t in subject.parse_runnable_tasks()}
    prep = {t["id"] for t in subject.parse_prep_tasks()}
    assert runnable & prep == set()
    assert runnable | prep == {"T-MANUAL", "T-NEEDSDAN", "T-AUTO2", "T-AUTO1", "T-MANUAL0"}


def test_prep_blocked_until_deps_complete(subject, sandbox, monkeypatch):
    write_roadmap(subject, *REAL_SHAPES)
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    # only the empty-deps manual task is ready
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["T-MANUAL0"]


@pytest.mark.parametrize(
    "block, prepped",
    [
        ("id: T\nmode: needs-dan\n", True),                        # normalized to manual
        ("id: T\nmode: needs-dan\ndispatch: manual\n", True),
        ("id: T\nmode: autonomous\ndispatch: manual\n", True),     # mode is not filtered
        ("id: T\ndispatch: manual\n", True),                       # no mode at all
        ("id: T\nmode: autonomous\n", False),
        ("id: T\nmode: needs-dan\ndispatch: auto\n", False),
        ("id: T\nmode: needs-dan\nstatus: complete\n", False),
        ("id: T\nmode: needs-dan\nhold: true\n", False),
        ("id: T\nmode: needs-dan\nhold: false\n", True),
        ("id: T\nmode: needs-dan\nself_modifying: true\n", False),
        ("id: T\nmode: needs-dan\nself_modifying: false\n", True),
        ("id: T\nmode: needs-dan\ndeps: []\n", True),
        ("id: T\nmode: needs-dan\ndeps:\n", True),
    ],
)
def test_prep_flag_matrix(subject, sandbox, monkeypatch, block, prepped):
    write_roadmap(subject, block)
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    assert bool(subject.parse_prep_tasks()) is prepped


def test_prep_skips_a_task_that_already_has_a_prepared_action(subject, sandbox, monkeypatch):
    write_roadmap(subject, "id: T\nmode: needs-dan\n", "id: U\nmode: needs-dan\n")
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch, prepared=["T"])
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["U"]


def test_prep_skips_a_task_already_parked_awaiting_dan(subject, sandbox, monkeypatch):
    write_roadmap(subject, "id: T\nmode: needs-dan\n", "id: U\nmode: needs-dan\n")
    prep_state(subject, waiting_on_dan={"7": {"task_id": "T", "kind": "manual-action"}})
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["U"]


def test_prep_skips_a_task_parked_after_failure(subject, sandbox, monkeypatch):
    write_roadmap(subject, "id: T\nmode: needs-dan\n", "id: U\nmode: needs-dan\n")
    prep_state(subject, parked_tasks=["T"])
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["U"]


def test_prep_parked_ids_are_matched_without_string_coercion(subject, sandbox, monkeypatch):
    """FOUND_BUGS: task ids are stringified before the lookup but the parked sets are
    not, so a numeric `task_id` in waiting_on_dan never matches and the task gets
    prepared a second time."""
    write_roadmap(subject, "id: 7\nmode: needs-dan\n")
    prep_state(subject, waiting_on_dan={"1": {"task_id": 7}})
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == [7]


def test_prep_waiting_on_dan_entry_without_task_id_is_harmless(subject, sandbox, monkeypatch):
    write_roadmap(subject, "id: T\nmode: needs-dan\n")
    prep_state(subject, waiting_on_dan={"1": {"kind": "question"}})
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["T"]


def test_prep_counts_in_roadmap_completions_as_satisfied_deps(subject, sandbox, monkeypatch):
    write_roadmap(
        subject,
        "id: DEP\nmode: autonomous\nstatus: complete\n",
        "id: T\nmode: needs-dan\ndeps: [DEP]\n",
    )
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["T"]


def test_prep_deps_given_as_a_bare_string(subject, sandbox, monkeypatch):
    write_roadmap(subject, "id: T\nmode: needs-dan\ndeps: DEP1\n")
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    assert subject.parse_prep_tasks() == []
    write_completed(subject, "DEP1")
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["T"]


def test_prep_preserves_document_order(subject, sandbox, monkeypatch):
    write_roadmap(
        subject,
        "id: B\nmode: needs-dan\n",
        "id: A\nmode: needs-dan\n",
    )
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["B", "A"]


def test_prep_raises_when_state_json_is_missing(subject, sandbox, monkeypatch):
    """FOUND_BUGS: parse_prep_tasks reads the ROADMAP happily but then hard-fails on a
    missing state.json — a fresh checkout raises FileNotFoundError from a *parser*."""
    write_roadmap(subject, "id: T\nmode: needs-dan\n")
    stub_prep_actions(subject, monkeypatch)
    with pytest.raises(FileNotFoundError):
        subject.parse_prep_tasks()


def test_prep_raises_when_the_roadmap_is_missing(subject, sandbox, monkeypatch):
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    with pytest.raises(FileNotFoundError):
        subject.parse_prep_tasks()


def test_prep_ignores_malformed_blocks(subject, sandbox, monkeypatch):
    write_raw_roadmap(
        subject,
        "```yaml\n\tbad\n```\n\n```yaml\nid: T\nmode: needs-dan\n```\n",
    )
    prep_state(subject)
    stub_prep_actions(subject, monkeypatch)
    assert [t["id"] for t in subject.parse_prep_tasks()] == ["T"]


# --- _roadmap_signature -------------------------------------------------------------


def test_roadmap_signature_is_the_sha256_of_the_file(subject, sandbox):
    path = write_roadmap(subject, *REAL_SHAPES)
    assert subject._roadmap_signature() == hashlib.sha256(path.read_bytes()).hexdigest()


def test_roadmap_signature_is_empty_when_the_file_is_missing(subject, sandbox):
    assert subject._roadmap_signature() == ""


def test_roadmap_signature_of_an_empty_file_is_not_empty(subject, sandbox):
    """An empty ROADMAP hashes to the sha256 of b"" — only an unreadable file
    produces the "" sentinel that callers treat as "skip"."""
    write_raw_roadmap(subject, "")
    assert subject._roadmap_signature() == hashlib.sha256(b"").hexdigest()


def test_roadmap_signature_is_empty_when_the_path_is_a_directory(subject, sandbox):
    subject.ROADMAP_FILE.mkdir(parents=True)
    assert subject._roadmap_signature() == ""


def test_roadmap_signature_changes_with_any_byte(subject, sandbox):
    write_raw_roadmap(subject, "a")
    first = subject._roadmap_signature()
    write_raw_roadmap(subject, "a ")
    assert subject._roadmap_signature() != first


# --- run_dep_map --------------------------------------------------------------------


def test_run_dep_map_always_runs_the_generator(subject, sandbox, monkeypatch):
    fake = no_subprocess(subject, monkeypatch)
    subject.run_dep_map()
    assert fake.argvs == [[str(subject.VENV_PYTHON), str(subject.GEN_DEP_MAP)]]
    assert fake.calls[0].kwargs == {"cwd": str(subject.REPO), "capture_output": True}


def test_run_dep_map_does_not_check_that_the_generator_exists(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the two optional steps are `.exists()`-guarded, the mandatory one is
    not — a missing gen_dependency_map.py is a swallowed non-zero exit, not an error."""
    fake = no_subprocess(subject, monkeypatch)
    assert not subject.GEN_DEP_MAP.exists()
    subject.run_dep_map()
    assert len(fake.calls) == 1


def test_run_dep_map_renders_the_png_when_the_renderer_exists(subject, sandbox, monkeypatch):
    subject.RENDER_DEP_MAP.parent.mkdir(parents=True, exist_ok=True)
    subject.RENDER_DEP_MAP.write_text("#!/bin/bash\n", encoding="utf-8")
    fake = no_subprocess(subject, monkeypatch)
    subject.run_dep_map()
    assert fake.argvs[1] == ["bash", str(subject.RENDER_DEP_MAP)]
    assert fake.calls[1].kwargs["timeout"] == 120


def test_run_dep_map_regenerates_upcoming_when_present(subject, sandbox, monkeypatch):
    subject.GEN_UPCOMING.parent.mkdir(parents=True, exist_ok=True)
    subject.GEN_UPCOMING.write_text("x", encoding="utf-8")
    fake = no_subprocess(subject, monkeypatch)
    subject.run_dep_map()
    assert fake.argvs[-1] == [str(subject.VENV_PYTHON), str(subject.GEN_UPCOMING)]
    assert fake.calls[-1].kwargs["timeout"] == 60


def test_run_dep_map_runs_all_three_steps_in_order(subject, sandbox, monkeypatch):
    for path in (subject.RENDER_DEP_MAP, subject.GEN_UPCOMING):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    fake = no_subprocess(subject, monkeypatch)
    subject.run_dep_map()
    assert fake.argvs == [
        [str(subject.VENV_PYTHON), str(subject.GEN_DEP_MAP)],
        ["bash", str(subject.RENDER_DEP_MAP)],
        [str(subject.VENV_PYTHON), str(subject.GEN_UPCOMING)],
    ]


def test_run_dep_map_lets_a_timeout_escape(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the render step is bounded by a timeout but nothing catches
    TimeoutExpired, so a hung renderer takes the caller down with it."""
    subject.RENDER_DEP_MAP.parent.mkdir(parents=True, exist_ok=True)
    subject.RENDER_DEP_MAP.write_text("x", encoding="utf-8")
    no_subprocess(
        subject,
        monkeypatch,
        raises=lambda argv: subprocess.TimeoutExpired(argv, 120) if argv[0] == "bash" else None,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        subject.run_dep_map()


def test_run_dep_map_returns_none_and_ignores_exit_codes(subject, sandbox, monkeypatch):
    no_subprocess(subject, monkeypatch, rc=lambda argv: 3)
    assert subject.run_dep_map() is None


# --- maybe_push_roadmap_map_change --------------------------------------------------


@pytest.fixture
def no_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)


def test_maybe_push_no_roadmap_is_a_silent_noop(subject, sandbox, monkeypatch, no_telegram):
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    assert fake.calls == []
    assert not subject.LAST_MAP_SIG.exists()


def test_maybe_push_first_observation_records_a_silent_baseline(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    assert fake.calls == []
    assert subject.LAST_MAP_SIG.read_text() == subject._roadmap_signature()
    assert not subject.JOURNAL.exists()


def test_maybe_push_unchanged_roadmap_is_a_noop(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    subject.LAST_MAP_SIG.write_text(subject._roadmap_signature())
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    assert fake.calls == []


def test_maybe_push_tolerates_trailing_whitespace_in_the_baseline(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    subject.LAST_MAP_SIG.write_text(subject._roadmap_signature() + "\n")
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    assert fake.calls == []


def test_maybe_push_on_change_regenerates_and_journals(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    # the dep-map generator ran; nothing else could (no renderer, no notifier on disk)
    assert fake.argvs == [[str(subject.VENV_PYTHON), str(subject.GEN_DEP_MAP)]]
    record = json.loads(subject.JOURNAL.read_text(encoding="utf-8").strip())
    assert record["event"] == "roadmap_map_pushed"
    assert record["detail"] == "sig=" + subject._roadmap_signature()[:12]


def test_maybe_push_updates_the_baseline_so_the_next_poll_is_quiet(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    assert subject.LAST_MAP_SIG.read_text() == subject._roadmap_signature()
    before = len(fake.calls)
    subject.maybe_push_roadmap_map_change()
    assert len(fake.calls) == before


def test_maybe_push_sends_the_notifier_the_task_counts(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    write_completed(subject, "T-DONE", "T-ALSO-DONE")
    subject.write_state({"version": "1", "in_flight": []})
    stub_prep_actions(subject, monkeypatch)
    subject.NOTIFY_SH.parent.mkdir(parents=True, exist_ok=True)
    subject.NOTIFY_SH.write_text("#!/bin/bash\n", encoding="utf-8")
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    notify = [c for c in fake.calls if c.argv[0] == "bash" and c.argv[1] == str(subject.NOTIFY_SH)]
    assert len(notify) == 1
    assert "2 runnable" in notify[0].argv[2]
    assert "3 awaiting-you" in notify[0].argv[2]


def test_maybe_push_drops_the_counts_when_state_json_is_missing(subject, sandbox, monkeypatch, no_telegram):
    """FOUND_BUGS: parse_prep_tasks raising FileNotFoundError is swallowed by the bare
    `except`, so the Telegram message silently loses *both* counts — including the
    runnable count, which was computed successfully."""
    write_roadmap(subject, *REAL_SHAPES)
    stub_prep_actions(subject, monkeypatch)
    subject.NOTIFY_SH.parent.mkdir(parents=True, exist_ok=True)
    subject.NOTIFY_SH.write_text("#!/bin/bash\n", encoding="utf-8")
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    notify = [c for c in fake.calls if c.argv[0] == "bash" and c.argv[1] == str(subject.NOTIFY_SH)]
    assert len(notify) == 1
    assert "runnable" not in notify[0].argv[2]


def test_maybe_push_never_calls_curl_without_credentials(subject, sandbox, monkeypatch, no_telegram):
    write_roadmap(subject, *REAL_SHAPES)
    subject.DEP_MAP_PNG.parent.mkdir(parents=True, exist_ok=True)
    subject.DEP_MAP_PNG.write_bytes(b"\x89PNG")
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch)
    subject.maybe_push_roadmap_map_change()
    assert not any(c.argv[0] == "curl" for c in fake.calls)


def test_maybe_push_uploads_the_png_when_credentials_are_present(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100")
    write_roadmap(subject, *REAL_SHAPES)
    subject.DEP_MAP_PNG.parent.mkdir(parents=True, exist_ok=True)
    subject.DEP_MAP_PNG.write_bytes(b"\x89PNG")
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch, stdout='{"ok":true}')
    subject.maybe_push_roadmap_map_change()
    curls = [c.argv for c in fake.calls if c.argv[0] == "curl"]
    assert len(curls) == 1
    assert curls[0][-1].endswith("/bottest-token/sendPhoto")
    assert f"photo=@{subject.DEP_MAP_PNG}" in curls[0]


def test_maybe_push_falls_back_to_send_document_when_send_photo_is_rejected(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100")
    write_roadmap(subject, *REAL_SHAPES)
    subject.DEP_MAP_PNG.parent.mkdir(parents=True, exist_ok=True)
    subject.DEP_MAP_PNG.write_bytes(b"\x89PNG")
    subject.LAST_MAP_SIG.write_text("0" * 64)
    fake = no_subprocess(subject, monkeypatch, stdout='{"ok":false}')
    subject.maybe_push_roadmap_map_change()
    curls = [c.argv for c in fake.calls if c.argv[0] == "curl"]
    assert [c[-1].rsplit("/", 1)[-1] for c in curls] == ["sendPhoto", "sendDocument"]


# --- mark_roadmap_complete ----------------------------------------------------------


def test_mark_complete_runs_the_helper_with_no_verify(subject, sandbox, monkeypatch):
    subject.MARK_TASK_COMPLETE.parent.mkdir(parents=True, exist_ok=True)
    subject.MARK_TASK_COMPLETE.write_text("x", encoding="utf-8")
    fake = no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-1")
    assert fake.argvs[0] == [
        str(subject.VENV_PYTHON), str(subject.MARK_TASK_COMPLETE), "T-1", "--no-verify",
    ]
    assert fake.calls[0].kwargs == {"cwd": str(subject.REPO), "capture_output": True}


def test_mark_complete_skips_the_helper_when_it_is_absent(subject, sandbox, monkeypatch):
    fake = no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-1")
    assert not any(str(subject.MARK_TASK_COMPLETE) in a for c in fake.calls for a in c.argv)


def test_mark_complete_never_edits_the_roadmap_itself(subject, sandbox, monkeypatch):
    """All ROADMAP mutation is delegated to the external helper script."""
    path = write_roadmap(subject, *REAL_SHAPES)
    before = path.read_bytes()
    no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-MANUAL")
    assert path.read_bytes() == before


def test_mark_complete_refreshes_explanations_with_a_long_timeout(subject, sandbox, monkeypatch):
    subject.GEN_UPCOMING.parent.mkdir(parents=True, exist_ok=True)
    subject.GEN_UPCOMING.write_text("x", encoding="utf-8")
    fake = no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-1")
    call = [c for c in fake.calls if str(subject.GEN_UPCOMING) in c.argv][0]
    assert call.argv == [str(subject.VENV_PYTHON), str(subject.GEN_UPCOMING), "--refresh-explanations"]
    assert call.kwargs["timeout"] == 900


def test_mark_complete_falls_back_to_the_cheap_upcoming_regen(subject, sandbox, monkeypatch):
    subject.GEN_UPCOMING.parent.mkdir(parents=True, exist_ok=True)
    subject.GEN_UPCOMING.write_text("x", encoding="utf-8")

    def raises(argv):
        if "--refresh-explanations" in argv:
            return subprocess.TimeoutExpired(argv, 900)
        return None

    fake = no_subprocess(subject, monkeypatch, raises=raises)
    subject.mark_roadmap_complete("T-1")
    upcoming = [c for c in fake.calls if str(subject.GEN_UPCOMING) in c.argv]
    assert upcoming[1].argv == [str(subject.VENV_PYTHON), str(subject.GEN_UPCOMING)]
    assert upcoming[1].kwargs["timeout"] == 60


def test_mark_complete_stages_only_the_graduation_artifacts(subject, sandbox, monkeypatch):
    fake = no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-1")
    add = [c for c in fake.calls if c.argv[:2] == ["git", "add"]][0]
    assert add.argv[2:] == [
        ".orchestrator/completed_tasks.json",
        "docs/ROADMAP.md",
        "docs/PROJECT.md",
        "docs/AUTONOMOUS_SYSTEM.md",
        "docs/dependency_map.md",
        "docs/dependency_map.png",
        "docs/UPCOMING.md",
        ".orchestrator/upcoming_explanations.json",
    ]
    assert add.kwargs["cwd"] == str(subject.REPO)


def test_mark_complete_does_not_commit_when_nothing_was_staged(subject, sandbox, monkeypatch):
    fake = no_subprocess(subject, monkeypatch, rc=lambda argv: 0)
    subject.mark_roadmap_complete("T-1")
    assert not any(c.argv[:2] == ["git", "commit"] for c in fake.calls)


def test_mark_complete_commits_when_something_was_staged(subject, sandbox, monkeypatch):
    fake = no_subprocess(
        subject, monkeypatch, rc=lambda argv: 1 if argv[:2] == ["git", "diff"] else 0
    )
    subject.mark_roadmap_complete("T-1")
    commit = [c for c in fake.calls if c.argv[:2] == ["git", "commit"]][0]
    assert commit.argv == [
        "git", "commit", "--no-verify", "-m",
        "roadmap(T-1): graduate to project docs + registry",
    ]


def test_mark_complete_diff_check_is_not_capture_output(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the staged-check inherits the parent's stdout/stderr — the only
    subprocess call in this function that is not silenced."""
    fake = no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-1")
    diff = [c for c in fake.calls if c.argv[:2] == ["git", "diff"]][0]
    assert diff.argv == ["git", "diff", "--cached", "--quiet"]
    assert "capture_output" not in diff.kwargs


def test_mark_complete_journals_the_removal(subject, sandbox, monkeypatch):
    no_subprocess(subject, monkeypatch)
    subject.mark_roadmap_complete("T-1")
    record = json.loads(subject.JOURNAL.read_text(encoding="utf-8").strip())
    assert record["event"] == "roadmap_task_removed"
    assert record["detail"] == "T-1 removed from ROADMAP.md"


def test_mark_complete_journals_even_when_every_step_was_skipped(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the journal records "removed from ROADMAP.md" unconditionally — the
    helper script may be absent and nothing at all may have happened."""
    fake = no_subprocess(subject, monkeypatch)
    assert not subject.MARK_TASK_COMPLETE.exists()
    subject.mark_roadmap_complete("T-1")
    assert subject.JOURNAL.exists()
    assert not any(str(subject.MARK_TASK_COMPLETE) in a for c in fake.calls for a in c.argv)


def test_mark_complete_ignores_a_failing_helper(subject, sandbox, monkeypatch):
    subject.MARK_TASK_COMPLETE.parent.mkdir(parents=True, exist_ok=True)
    subject.MARK_TASK_COMPLETE.write_text("x", encoding="utf-8")
    no_subprocess(subject, monkeypatch, rc=lambda argv: 2)
    assert subject.mark_roadmap_complete("T-1") is None
    assert subject.JOURNAL.exists()
