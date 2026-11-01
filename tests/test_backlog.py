"""P13 GDT-11 / W7 S1: review follow-ups wait in a backlog for the end-of-project polish.

`maestro.backlog` keeps every accepted task's review follow-ups in
`.orchestrator/backlog.json`; `followups.record` routes them there (an ordinary task's, a
`-followups` task's, a polish or triage task's, born `deferred`). Only a follow-up the
reviewer marked `before_dependents` still runs early, as `<id>-followups`, and only when
an open task depends on `<id>`: each such dependent waits for it (W7 bullet 1).
"""
from __future__ import annotations

import json

import yaml

from maestro import backlog, taskgraph
from maestro.docs import followups


def _block(**fields) -> str:
    return "```yaml\n" + yaml.safe_dump(fields, sort_keys=False) + "```\n"


T1 = {
    "id": "T1", "status": "open", "mode": "autonomous", "deps": ["T0"],
    "title": "Search", "scope": ["src/search.py"], "criteria": ["AC-1"], "tests": ["AC-2"],
    "verifications": [
        {"id": "V1", "kind": "auto", "cmd": "pytest tests/test_search.py"},
        {"id": "V2", "kind": "dan", "check": "Dan looks at it"},
        {"id": "V3", "kind": "auto", "await": True, "cmd": "wait-for-a-day"},
    ],
}

# T2 spells its deps as a flow list, T3 as a block list; T4 depends on neither.
ROADMAP = (
    "# Roadmap\n\nProse that must survive.\n\n"
    + _block(**T1)
    + "\n### T2 — Rank\n\n```yaml\nid: T2\nstatus: open\ndeps: [T1]\ntitle: Rank\n```\n"
    + "\n```yaml\nid: T3\nstatus: open\ndeps:\n  - T0\n  - T1\nhold: false\n```\n"
    + "\n```yaml\nid: T4\nstatus: open\ndeps: [T0]\n```\n"
    + "\n```yaml\nid: T5\nstatus: complete\ndeps: [T4]\n```\n"
)

PLAIN = followups.FollowUp("SQL outside db.py", 1, "proof_review", "abc1234")
FLAGGED = followups.FollowUp("the result schema field is misnamed", 2, "proof_review",
                             before_dependents=True)


def _project(tmp_path, content: str = ROADMAP):
    roadmap = taskgraph.roadmap_path(tmp_path)
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text(content, encoding="utf-8")
    (tmp_path / ".orchestrator").mkdir()
    taskgraph.registry_path(tmp_path).write_text(json.dumps([{"id": "T0"}]))
    taskgraph.record_applied(tmp_path, taskgraph.load_document(roadmap))
    return roadmap


def _tasks(text: str) -> dict:
    return {str(t["id"]): t for t in taskgraph.scan_tasks(text)}


def test_an_accepted_task_s_follow_ups_go_to_the_backlog_not_a_followups_task(tmp_path):
    roadmap = _project(tmp_path)
    journal: list = []

    recorded = followups.record(
        tmp_path, roadmap, dict(T1),
        [PLAIN, followups.FollowUp("no runs row", 2, "proof_review")],
        journal=lambda *a: journal.append(a))

    assert roadmap.read_text(encoding="utf-8") == ROADMAP           # no task queued
    assert recorded == followups.Recorded(count=2, backlog=2)
    assert recorded.summary() == (
        "2 review follow-up(s): 2 in the backlog for the polish stage")
    stored = backlog.items(tmp_path)
    assert [(i["id"], i["state"], i["text"]) for i in stored] == [
        ("BL-0001", "open", "SQL outside db.py"), ("BL-0002", "open", "no runs row")]
    first = stored[0]
    assert first["source"] == {"task": "T1", "round": 1, "node": "proof_review",
                               "commit": "abc1234"}
    assert first["origin"] == "task"
    assert first["scope"] == ["src/search.py"]
    # Its auto checks, never Dan's or an awaited one; its criteria and product tests.
    assert [c["id"] for c in first["checks"]] == ["V1"]
    assert first["criteria"] == ["AC-1", "AC-2"]
    assert (first["stage"], first["task"], first["of"], first["reason"]) == (
        None, None, None, None)
    assert first["at"] and first["settled_at"] is None
    assert ("backlog_recorded", "T1: BL-0001, BL-0002") in journal


def test_a_retried_graduation_records_each_item_once(tmp_path):
    roadmap = _project(tmp_path)
    for _ in range(2):
        followups.record(tmp_path, roadmap, dict(T1), [PLAIN, FLAGGED])
    # A rewording in whitespace or case is the same item, as `collect` reads it.
    followups.record(tmp_path, roadmap, dict(T1),
                     [followups.FollowUp("SQL   outside DB.py", 3, "proof_review")])

    assert [i["text"] for i in backlog.items(tmp_path)] == ["SQL outside db.py"]
    text = roadmap.read_text(encoding="utf-8")
    assert [t["id"] for t in taskgraph.scan_tasks(text)].count("T1-followups") == 1
    assert _tasks(text)["T2"]["deps"] == ["T1", "T1-followups"]
    assert _tasks(text)["T3"]["deps"] == ["T0", "T1", "T1-followups"]
    # Another task's identical text is its own item.
    backlog.add(tmp_path, {"id": "T9"}, [PLAIN], at="2026-10-24T00:00:00+00:00")
    assert [i["id"] for i in backlog.items(tmp_path)] == ["BL-0001", "BL-0002"]


def test_the_backlog_survives_a_restart_and_a_corrupt_file_is_moved_aside(tmp_path):
    roadmap = _project(tmp_path)
    followups.record(tmp_path, roadmap, dict(T1), [PLAIN])
    path = tmp_path / ".orchestrator" / "backlog.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["version"] == 1 and [i["id"] for i in stored["items"]] == ["BL-0001"]
    # A restart reads the file again: nothing lives only in memory.
    assert [i["text"] for i in backlog.items(tmp_path, "open")] == ["SQL outside db.py"]
    assert backlog.items(tmp_path, "deferred") == []

    path.write_text("{not json", encoding="utf-8")
    assert backlog.items(tmp_path) == []                            # a reader never raises
    followups.record(tmp_path, roadmap, dict(T1),
                     [followups.FollowUp("a later item", 1, "proof_review")])
    [aside] = list(path.parent.glob("backlog.json.corrupt-*"))
    assert aside.read_text(encoding="utf-8") == "{not json"
    assert [(i["id"], i["text"]) for i in backlog.items(tmp_path)] == [
        ("BL-0001", "a later item")]


def test_a_before_dependents_follow_up_runs_as_its_own_task_before_the_dependents(tmp_path):
    roadmap = _project(tmp_path)

    recorded = followups.record(tmp_path, roadmap, dict(T1), [PLAIN, FLAGGED])

    text = roadmap.read_text(encoding="utf-8")
    queued_content, _ = followups.queue_task(ROADMAP, T1, [FLAGGED])
    expected = (queued_content
                .replace("deps: [T1]\n", "deps: [T1, T1-followups]\n")
                .replace("  - T1\nhold", "  - T1\n  - T1-followups\nhold"))
    assert text == expected                                         # every other byte kept
    queued = _tasks(text)["T1-followups"]
    assert queued["acceptance_criteria"] == [FLAGGED.line()]        # the flagged item only
    assert queued["serves"] == [] and queued["deps"] == ["T1"]
    assert _tasks(text)["T2"]["deps"] == ["T1", "T1-followups"]
    assert _tasks(text)["T3"]["deps"] == ["T0", "T1", "T1-followups"]
    assert _tasks(text)["T4"]["deps"] == ["T0"]
    # The CAS records every block maestro wrote: none is withheld as a hand edit.
    assert not {"T1-followups", "T2", "T3"} & taskgraph.blocked_task_ids(tmp_path)
    assert [i["text"] for i in backlog.items(tmp_path)] == ["SQL outside db.py"]
    assert recorded == followups.Recorded(count=2, queued_as="T1-followups", backlog=1,
                                          wired=("T2", "T3"))
    assert recorded.summary() == (
        "2 review follow-up(s): 1 queued as T1-followups (before T2, T3), "
        "1 in the backlog for the polish stage")


def test_a_before_dependents_follow_up_with_no_open_dependent_goes_to_the_backlog(tmp_path):
    roadmap = _project(tmp_path)
    t4 = _tasks(ROADMAP)["T4"]          # only T5 depends on it, and T5 is complete

    recorded = followups.record(tmp_path, roadmap, t4, [FLAGGED])

    assert roadmap.read_text(encoding="utf-8") == ROADMAP
    [item] = backlog.items(tmp_path)
    assert (item["text"], item["state"], item["source"]["task"]) == (
        FLAGGED.text, "open", "T4")
    assert recorded == followups.Recorded(count=1, backlog=1)


def test_a_drifted_dependent_is_not_rewired_and_is_journaled(tmp_path):
    roadmap = _project(tmp_path)
    hand_edited = ROADMAP.replace("title: Rank", "title: Rank!")
    roadmap.write_text(hand_edited, encoding="utf-8")
    journal: list = []

    recorded = followups.record(tmp_path, roadmap, dict(T1), [FLAGGED],
                                journal=lambda *a: journal.append(a))

    tasks = _tasks(roadmap.read_text(encoding="utf-8"))
    assert tasks["T2"]["deps"] == ["T1"] and tasks["T2"]["title"] == "Rank!"
    assert tasks["T3"]["deps"] == ["T0", "T1", "T1-followups"]
    assert recorded.wired == ("T3",)
    assert ("followups_dependents_not_wired",
            "T1-followups: T2 (its block differs from the applied ROADMAP)") in journal
    # Its hand edit is still a conflict, never laundered into the record.
    conflict = taskgraph.drift(tmp_path)
    assert conflict is not None and conflict.changed == ("T2",)


def test_a_followups_task_s_own_follow_ups_go_to_the_backlog_never_another_task(tmp_path):
    content, _ = followups.queue_task(ROADMAP, T1, [PLAIN])
    roadmap = _project(tmp_path, content)
    task = _tasks(content)["T1-followups"]

    recorded = followups.record(tmp_path, roadmap, task, [
        followups.FollowUp("a", 1, "proof_review"),
        followups.FollowUp("b", 1, "proof_review", before_dependents=True)])

    assert roadmap.read_text(encoding="utf-8") == content           # no chained task
    assert [(i["text"], i["state"], i["origin"]) for i in backlog.items(tmp_path)] == [
        ("a", "open", "followups"), ("b", "open", "followups")]
    assert recorded == followups.Recorded(count=2, backlog=2)
    assert not (tmp_path / "docs" / "FOLLOW_UPS.md").exists()


def test_a_polish_or_triage_task_s_own_follow_ups_are_deferred(tmp_path):
    roadmap = _project(tmp_path)
    polish = {"id": "polish-1-a", "polish_stage": 1, "serves": [], "scope": ["src/a.py"]}
    triage = {"id": "polish-triage-1", "polish_stage": 1, "polish_role": "triage"}
    assert backlog.is_polish(polish) and not backlog.is_triage(polish)
    assert backlog.is_triage(triage) and not backlog.is_polish(triage)
    assert not backlog.is_polish(T1) and not backlog.is_triage(T1)

    recorded = followups.record(tmp_path, roadmap, polish, [FLAGGED])
    followups.record(tmp_path, roadmap, triage, [PLAIN])

    assert roadmap.read_text(encoding="utf-8") == ROADMAP
    rows = backlog.items(tmp_path, "deferred")
    assert [(i["origin"], i["source"]["task"]) for i in rows] == [
        ("polish", "polish-1-a"), ("triage", "polish-triage-1")]
    assert {i["reason"] for i in rows} == {
        "raised by a polish/triage review; polish never chains"}
    assert all(i["settled_at"] for i in rows)
    assert backlog.items(tmp_path, "open") == []
    assert recorded.summary() == (
        "1 review follow-up(s): 1 deferred (raised by a polish/triage review)")


def test_existing_queued_followups_tasks_are_left_as_they_are(tmp_path):
    t0 = {"id": "T0", "status": "open", "mode": "autonomous", "deps": [], "title": "Base"}
    old = followups.build_task(t0, [PLAIN])
    old.pop("serves", None)              # queued by an older maestro: no `serves`
    content = ROADMAP + "\n" + followups.render_section(old, heading=False)
    roadmap = _project(tmp_path, content)

    followups.record(tmp_path, roadmap, dict(T1), [PLAIN])
    followups.record(tmp_path, roadmap, _tasks(content)["T0-followups"], [
        followups.FollowUp("its own", 1, "proof_review")])

    text = roadmap.read_text(encoding="utf-8")
    assert text == content                                          # nothing converted
    assert "serves" not in _tasks(text)["T0-followups"]
    assert [(i["source"]["task"], i["text"]) for i in backlog.items(tmp_path)] == [
        ("T1", "SQL outside db.py"), ("T0-followups", "its own")]


# ── S2: triage and the polish stage ─────────────────────────────────────────────────────

AT = "2026-10-24T00:00:00+00:00"
SEARCH = {"id": "T1", "scope": ["src/search.py"], "criteria": ["AC-1"],
          "verifications": [{"id": "V1", "kind": "auto", "cmd": "pytest tests/test_search.py"}]}
RANK = {"id": "T2", "scope": ["src/search.py", "src/rank.py"], "tests": ["AC-2"],
        "verifications": [{"id": "V1", "kind": "auto", "cmd": "pytest tests/test_search.py"},
                          {"id": "V2", "kind": "auto", "cmd": "pytest tests/test_rank.py"}]}
EXPORT = {"id": "T3", "scope": ["src/export.py"]}


def _add(repo, task, *texts, round_=1):
    return backlog.add(repo, task, [followups.FollowUp(t, round_, "proof_review")
                                    for t in texts], at=AT)


def _write(repo, rel, doc):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_polish_keeps_resource_gates_of_its_graduated_source(tmp_path):
    source = dict(SEARCH, resource=['eval-cpu'], resources={'ram_mb': 4096, 'cpu': 2})
    _add(tmp_path, source, 'Preserve the source resource gates')
    [task] = backlog.stage(tmp_path, [], {'T1'}, at=AT)
    assert task.get('resource') == ['eval-cpu']
    assert task.get('resources') == {'ram_mb': 4096, 'cpu': 2}


def _by_id(repo) -> dict:
    return {i["id"]: i for i in backlog.items(repo)}


def test_three_tasks_and_five_follow_ups_two_duplicates_one_moot_end_in_one_polish_task(
        tmp_path):
    """The W7 proof: T1 and T2 share an area, T3 has its own. BL-0002 duplicates BL-0001,
    BL-0004 duplicates BL-0003, and BL-0005, T3's only item, is moot."""
    _add(tmp_path, SEARCH, "search does not fold accents")
    _add(tmp_path, RANK, "accents are not folded when searching",
         "the rank weights are magic numbers")
    _add(tmp_path, SEARCH, "name the rank weights", round_=2)
    _add(tmp_path, EXPORT, "the export header is misspelt")

    [triage] = backlog.stage(tmp_path, [], {"T1", "T2", "T3"}, at=AT)

    ids = ["BL-0001", "BL-0002", "BL-0003", "BL-0004", "BL-0005"]
    assert triage["id"] == "polish-triage-1" and triage["polish_items"] == ids
    assert (triage["polish_stage"], triage["polish_role"]) == (1, "triage")
    assert triage["serves"] == [] and triage["deps"] == []
    assert triage["scope"] == ["docs/polish/triage-1.json"]
    [check] = triage["verifications"]
    assert check["kind"] == "auto" and check["cmd"].endswith(
        "-m maestro.backlog check-triage docs/polish/triage-1.json --items "
        + ",".join(ids))
    assert triage["acceptance_criteria"][0] == (
        "BL-0001 (T1, review round 1, proof_review) search does not fold accents")
    assert {i["state"] for i in backlog.items(tmp_path)} == {"triage"}

    moot = "src/export.py was removed by T3 (its header went with it)"
    _write(tmp_path, "docs/polish/triage-1.json", {"stage": 1, "items": [
        {"id": "BL-0001", "decision": "keep", "reason": "real"},
        {"id": "BL-0002", "decision": "duplicate", "of": "BL-0001", "reason": "same fold"},
        {"id": "BL-0003", "decision": "keep", "reason": "real"},
        {"id": "BL-0004", "decision": "duplicate", "of": "BL-0003", "reason": "same names"},
        {"id": "BL-0005", "decision": "moot", "reason": moot}]})

    blocks, result = backlog.apply_triage(tmp_path, triage, at=AT)

    [polish] = blocks                                   # T3's area gets no task
    assert polish["id"] == "polish-1-a" and polish["polish_items"] == ["BL-0001", "BL-0003"]
    assert polish["polish_stage"] == 1 and "polish_role" not in polish
    assert polish["serves"] == [] and polish["deps"] == []
    assert polish["scope"] == ["src/search.py", "src/rank.py", "docs/polish/polish-1-a.json"]
    assert polish["tests"] == ["AC-1", "AC-2"]
    assert polish["acceptance_criteria"] == [
        "BL-0001 (T1, review round 1, proof_review) search does not fold accents"
        " — also raised as BL-0002 (T2, review round 1, proof_review): "
        '"accents are not folded when searching"',
        "BL-0003 (T2, review round 1, proof_review) the rank weights are magic numbers"
        ' — also raised as BL-0004 (T1, review round 2, proof_review): '
        '"name the rank weights"',
    ]
    rows = _by_id(tmp_path)
    assert [(i, rows[i]["state"], rows[i]["task"]) for i in ids] == [
        ("BL-0001", "assigned", "polish-1-a"), ("BL-0002", "merged", "polish-triage-1"),
        ("BL-0003", "assigned", "polish-1-a"), ("BL-0004", "merged", "polish-triage-1"),
        ("BL-0005", "moot", "polish-triage-1")]
    assert (rows["BL-0002"]["of"], rows["BL-0002"]["reason"]) == ("BL-0001", "same fold")
    assert rows["BL-0005"]["reason"] == moot and rows["BL-0005"]["settled_at"] == AT
    assert result["text"] == (
        "🧹 Triage (polish stage 1): 5 follow-ups → 2 merged as duplicates, 1 declined as "
        f"moot (BL-0005: \"{moot}\"), 1 polish task: polish-1-a (src/search.py, "
        "src/rank.py).")
    # A retried graduation gives the same polish task and changes nothing.
    again, _ = backlog.apply_triage(tmp_path, triage, at=AT)
    assert again == blocks and _by_id(tmp_path) == rows


def test_two_or_more_open_items_open_a_triage_task_and_one_item_goes_straight_to_polish(
        tmp_path):
    # No follow-ups: no stage and no store file (release safety).
    assert backlog.stage(tmp_path, [], set(), at=AT) == []
    assert not backlog.path_for(tmp_path).exists()

    _add(tmp_path, SEARCH, "search does not fold accents")
    [polish] = backlog.stage(tmp_path, [], {"T1"}, at=AT)
    assert polish["id"] == "polish-1-a" and polish["polish_items"] == ["BL-0001"]
    assert "polish_role" not in polish
    assert polish["scope"] == ["src/search.py", "docs/polish/polish-1-a.json"]
    [item] = backlog.items(tmp_path)
    assert (item["state"], item["stage"], item["task"]) == ("assigned", 1, "polish-1-a")
    # Nothing open any more: the next exhaustion opens nothing.
    assert backlog.stage(tmp_path, [polish], {"T1"}, at=AT) == []

    _add(tmp_path, RANK, "a", "b")
    [triage] = backlog.stage(tmp_path, [polish], {"T1", "T2"}, at=AT)
    assert triage["id"] == "polish-triage-2"
    assert triage["polish_items"] == ["BL-0002", "BL-0003"]


def test_check_triage_refuses_a_missing_or_extra_item_a_duplicate_of_a_non_kept_item_and_a_moot_without_reason():
    ids = ["BL-0001", "BL-0002", "BL-0003"]
    clean = {"stage": 1, "items": [
        {"id": "BL-0001", "decision": "keep"},
        {"id": "BL-0002", "decision": "duplicate", "of": "BL-0001", "reason": "same"},
        {"id": "BL-0003", "decision": "moot", "reason": "gone at src/a.py:3"}]}
    assert backlog.check_triage(clean, ids) == []
    # Zero kept items is legal: no polish task follows.
    assert backlog.check_triage({"items": [
        {"id": i, "decision": "moot", "reason": "gone"} for i in ids]}, ids) == []

    def problems(*rows):
        return " | ".join(backlog.check_triage({"items": list(rows)}, ids))

    keep1, keep2 = {"id": "BL-0001", "decision": "keep"}, {"id": "BL-0002", "decision": "keep"}
    assert "BL-0003" in problems(keep1, keep2) and "missing" in problems(keep1, keep2)
    assert "BL-0009" in problems(keep1, keep2, {"id": "BL-0003", "decision": "keep"},
                                 {"id": "BL-0009", "decision": "keep"})
    assert "more than once" in problems(keep1, keep1, keep2,
                                        {"id": "BL-0003", "decision": "keep"})
    assert "BL-0003" in problems(keep1, keep2, {"id": "BL-0003", "decision": "drop"})
    # A duplicate of a non-kept item (a chain), of itself, of nothing, or without a reason.
    for of in ("BL-0002", "BL-0003", "BL-0007", None):
        rows = [keep1, {"id": "BL-0002", "decision": "moot", "reason": "gone"},
                {"id": "BL-0003", "decision": "duplicate", "of": of, "reason": "same"}]
        assert "BL-0003" in problems(*rows), of
    assert "BL-0003" in problems(keep1, keep2,
                                 {"id": "BL-0003", "decision": "duplicate", "of": "BL-0001"})
    assert "BL-0003" in problems(keep1, keep2,
                                 {"id": "BL-0003", "decision": "moot", "reason": "  "})
    assert backlog.check_triage(["not", "an", "object"], ids)
    assert backlog.check_triage({"items": "nope"}, ids)


def test_an_unreadable_triage_file_keeps_every_item(tmp_path):
    _add(tmp_path, SEARCH, "a")
    _add(tmp_path, EXPORT, "b")
    [triage] = backlog.stage(tmp_path, [], {"T1", "T3"}, at=AT)
    (tmp_path / "docs" / "polish").mkdir(parents=True)
    (tmp_path / "docs" / "polish" / "triage-1.json").write_text("{nope", encoding="utf-8")

    blocks, result = backlog.apply_triage(tmp_path, triage, at=AT)

    assert result["unreadable"]
    assert [b["polish_items"] for b in blocks] == [["BL-0001"], ["BL-0002"]]
    assert {i["state"] for i in backlog.items(tmp_path)} == {"assigned"}

    # An invalid file (an item missing) is kept whole the same way.
    other = tmp_path / "other"
    _add(other, SEARCH, "a", "b")
    [triage] = backlog.stage(other, [], {"T1"}, at=AT)
    _write(other, "docs/polish/triage-1.json", {"items": [
        {"id": "BL-0001", "decision": "moot", "reason": "gone"}]})
    blocks, result = backlog.apply_triage(other, triage, at=AT)
    assert "BL-0002" in result["unreadable"]
    assert [b["polish_items"] for b in blocks] == [["BL-0001", "BL-0002"]]
    assert not backlog.items(other, "moot")


def _keep_all(repo, triage):
    _write(repo, f"docs/polish/triage-{triage['polish_stage']}.json", {"items": [
        {"id": i, "decision": "keep"} for i in triage["polish_items"]]})


def test_disjoint_scopes_give_one_polish_task_each_and_an_undeclared_scope_joins_everything(
        tmp_path):
    _add(tmp_path, SEARCH, "a")
    _add(tmp_path, EXPORT, "b")
    _add(tmp_path, RANK, "c")                   # overlaps T1's area, not T3's
    [triage] = backlog.stage(tmp_path, [], {"T1", "T2", "T3"}, at=AT)
    _keep_all(tmp_path, triage)

    blocks, result = backlog.apply_triage(tmp_path, triage, at=AT)

    assert [(b["id"], b["polish_items"], b["scope"]) for b in blocks] == [
        ("polish-1-a", ["BL-0001", "BL-0003"],
         ["src/search.py", "src/rank.py", "docs/polish/polish-1-a.json"]),
        ("polish-1-b", ["BL-0002"], ["src/export.py", "docs/polish/polish-1-b.json"])]
    assert result["text"].endswith(
        "2 polish tasks: polish-1-a (src/search.py, src/rank.py), polish-1-b "
        "(src/export.py).")

    # An undeclared scope overlaps everything (D20): one task, and no `scope:` at all.
    other = tmp_path / "other"
    _add(other, SEARCH, "a")
    _add(other, EXPORT, "b")
    _add(other, {"id": "T9"}, "c")
    [triage] = backlog.stage(other, [], {"T1", "T3", "T9"}, at=AT)
    _keep_all(other, triage)
    [block], _ = backlog.apply_triage(other, triage, at=AT)
    assert block["polish_items"] == ["BL-0001", "BL-0002", "BL-0003"]
    assert "scope" not in block


def test_a_polish_task_carries_its_items_checks_deduplicated_and_the_report_check_last(
        tmp_path):
    _add(tmp_path, SEARCH, "a")
    _add(tmp_path, RANK, "b")
    [triage] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=AT)
    _keep_all(tmp_path, triage)

    [block], _ = backlog.apply_triage(tmp_path, triage, at=AT)

    checks = block["verifications"]
    assert [(c["id"], c["kind"]) for c in checks] == [
        ("V1", "auto"), ("V2", "auto"), ("V3", "auto")]
    assert [c["cmd"] for c in checks[:2]] == [
        "pytest tests/test_search.py", "pytest tests/test_rank.py"]
    assert checks[2]["cmd"].endswith(
        "-m maestro.backlog check-report docs/polish/polish-1-a.json "
        "--items BL-0001,BL-0002")
    # INV-04: a polish task whose items have no checks still has the report check.
    other = tmp_path / "other"
    _add(other, EXPORT, "a")
    [alone] = backlog.stage(other, [], {"T3"}, at=AT)
    [check] = alone["verifications"]
    assert check["id"] == "V1" and "check-report docs/polish/polish-1-a.json" in check["cmd"]


def test_check_report_needs_every_item_done_or_declined_with_a_reason():
    ids = ["BL-0001", "BL-0002"]
    clean = {"items": [{"id": "BL-0001", "outcome": "done", "reason": "folded"},
                       {"id": "BL-0002", "outcome": "declined", "reason": "config, not code"}]}
    assert backlog.check_report(clean, ids) == []
    assert backlog.check_report({"items": [{"id": "BL-0001", "outcome": "done"},
                                           clean["items"][1]]}, ids) == []

    def problems(*rows):
        return " | ".join(backlog.check_report({"items": list(rows)}, ids))

    done = {"id": "BL-0001", "outcome": "done", "reason": "folded"}
    assert "BL-0002" in problems(done)                                   # missing
    assert "BL-0003" in problems(done, clean["items"][1],
                                 {"id": "BL-0003", "outcome": "done"})   # not listed
    assert "BL-0002" in problems(done, {"id": "BL-0002", "outcome": "skipped"})
    assert "BL-0002" in problems(done, {"id": "BL-0002", "outcome": "declined"})
    assert "more than once" in problems(done, done, clean["items"][1])
    assert backlog.check_report("{}", ids)


def test_settle_marks_items_done_or_declined_and_an_unreported_one_deferred(tmp_path):
    _add(tmp_path, SEARCH, "a", "b", "c")
    [triage] = backlog.stage(tmp_path, [], {"T1"}, at=AT)
    _keep_all(tmp_path, triage)
    [polish], _ = backlog.apply_triage(tmp_path, triage, at=AT)
    _write(tmp_path, "docs/polish/polish-1-a.json", {"items": [
        {"id": "BL-0001", "outcome": "done", "reason": "folded"},
        {"id": "BL-0002", "outcome": "declined", "reason": "config, not code"}]})

    result = backlog.settle(tmp_path, polish, at="2026-10-25T00:00:00+00:00")

    rows = _by_id(tmp_path)
    assert [(rows[i]["state"], rows[i]["reason"]) for i in ("BL-0001", "BL-0002", "BL-0003")
            ] == [("done", "folded"), ("declined", "config, not code"),
                  ("deferred", "not reported by polish-1-a")]
    assert {rows[i]["settled_at"] for i in rows} == {"2026-10-25T00:00:00+00:00"}
    assert (result["done"], result["declined"], result["deferred"]) == (
        ["BL-0001"], ["BL-0002"], ["BL-0003"])
    assert result["text"] == "polish-1-a: 1 done, 1 declined, 1 deferred"


def test_a_stage_never_takes_deferred_items_and_a_deleted_stage_task_reopens_its_items(
        tmp_path):
    _add(tmp_path, {"id": "polish-1-a", "polish_stage": 1}, "its own")   # born deferred
    _add(tmp_path, SEARCH, "a")
    _add(tmp_path, RANK, "b")

    [triage] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=AT)
    assert triage["polish_items"] == ["BL-0002", "BL-0003"]
    # The triage task is in the ROADMAP: its items stay with it.
    assert backlog.stage(tmp_path, [triage], {"T1", "T2"}, at=AT) == []

    # Dan deleted it (neither in the ROADMAP nor graduated): its items open again, and the
    # next stage takes a new number.
    [again] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=AT)
    assert again["id"] == "polish-triage-2"
    assert again["polish_items"] == ["BL-0002", "BL-0003"]
    assert _by_id(tmp_path)["BL-0001"]["state"] == "deferred"


def test_the_triage_and_polish_bars_reach_the_writer_and_reviewer_briefs(tmp_path):
    _add(tmp_path, SEARCH, "a")
    _add(tmp_path, RANK, "b")
    [triage] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=AT)
    _keep_all(tmp_path, triage)
    [polish], _ = backlog.apply_triage(tmp_path, triage, at=AT)

    bars = {(name, reviewer): followups.contract(task, reviewer=reviewer)
            for name, task in (("triage", triage), ("polish", polish))
            for reviewer in (False, True)}
    for (name, reviewer), bar in bars.items():
        task = triage if name == "triage" else polish
        assert bar == backlog.contract(task, reviewer=reviewer)
    writer, reviewer = bars["triage", False], bars["triage", True]
    for phrase in ("TRIAGE TASK (polish stage 1)", "Change no code", "docs/polish/triage-1.json",
                   "When unsure, keep", "Never widen, split or add an item (D23)"):
        assert phrase in writer, phrase
    for phrase in ("every item appears once", "true in the code at HEAD",
                   "A wrongly declined item is blocking", "Add no items"):
        assert phrase in reviewer, phrase
    writer, reviewer = bars["polish", False], bars["polish", True]
    for phrase in ("POLISH TASK (stage 1, area src/search.py, src/rank.py)",
                   "still relevant", "no new scope, no new edge cases (D23)",
                   "docs/polish/polish-1-a.json", "rerun on your change"):
        assert phrase in writer, phrase
    for phrase in ("docs/polish/polish-1-a.json", "new scope: blocking",
                   "Do not widen an item", "never polished again"):
        assert phrase in reviewer, phrase
    # An ordinary task carries neither bar.
    assert backlog.contract(T1, reviewer=False) == ""


def test_python_m_maestro_backlog_check_exits_1_on_problems_and_0_when_clean(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    # This checkout's maestro, not whichever one the interpreter has installed.
    env = dict(os.environ, PYTHONPATH=str(Path(backlog.__file__).resolve().parents[1]))

    def run(*argv):
        return subprocess.run([sys.executable, "-m", "maestro.backlog", *argv],
                              cwd=tmp_path, capture_output=True, text=True, env=env)

    _write(tmp_path, "triage.json", {"items": [
        {"id": "BL-0001", "decision": "keep"},
        {"id": "BL-0002", "decision": "moot", "reason": "gone"}]})
    _write(tmp_path, "report.json", {"items": [{"id": "BL-0001", "outcome": "declined"}]})

    clean = run("check-triage", "triage.json", "--items", "BL-0001,BL-0002")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    bad = run("check-triage", "triage.json", "--items", "BL-0001,BL-0002,BL-0003")
    assert bad.returncode == 1 and "BL-0003" in bad.stdout + bad.stderr
    bad = run("check-report", "report.json", "--items", "BL-0001")
    assert bad.returncode == 1 and "BL-0001" in bad.stdout + bad.stderr
    _write(tmp_path, "report.json", {"items": [{"id": "BL-0001", "outcome": "done"}]})
    assert run("check-report", "report.json", "--items", "BL-0001").returncode == 0
    missing = run("check-report", "absent.json", "--items", "BL-0001")
    assert missing.returncode == 1 and "absent.json" in missing.stdout + missing.stderr


# ── GDT-11 review fixes (M1, M2, L1, L2) ──────────────────────────────────────────────


def test_a_lost_or_corrupt_store_never_reuses_a_graduated_stage_id(tmp_path):
    """Review M1: stage numbers also come from the stage ids already in the ROADMAP and the
    completion registry, so a reset store never hands out a graduated `polish-*` id again
    (the dispatch loop would skip it forever and never reach the completion gate)."""
    _add(tmp_path, SEARCH, "a")
    [polish] = backlog.stage(tmp_path, [], {"T1"}, at=AT)
    assert polish["id"] == "polish-1-a"
    backlog.path_for(tmp_path).write_text("{not json", encoding="utf-8")   # store lost
    _add(tmp_path, {"id": "T7", "scope": ["src/t7.py"]}, "a later item")

    [again] = backlog.stage(tmp_path, [], {"T1", "T7", "polish-1-a"}, at=AT)

    assert again["id"] == "polish-2-a"
    # A stage id still in the ROADMAP counts too (triage and a two-letter polish suffix).
    path = backlog.path_for(tmp_path)
    path.unlink()
    _add(tmp_path, SEARCH, "b", "c")
    [triage] = backlog.stage(tmp_path, [{"id": "polish-triage-3"}, {"id": "polish-4-ab"}],
                             {"T1"}, at=AT)
    assert triage["id"] == "polish-triage-5"


def test_more_than_26_disjoint_areas_each_get_a_polish_task_and_every_kept_item_is_assigned(
        tmp_path):
    """Review M2: the polish suffix is unbounded (a..z, aa, ab, ...), so no kept group is
    dropped past `z`."""
    for k in range(28):
        _add(tmp_path, {"id": f"T{k}", "scope": [f"src/m{k}.py"]}, f"item {k}")
    [triage] = backlog.stage(tmp_path, [], {f"T{k}" for k in range(28)}, at=AT)
    _keep_all(tmp_path, triage)

    blocks, result = backlog.apply_triage(tmp_path, triage, at=AT)

    ids = [b["id"] for b in blocks]
    assert len(blocks) == 28 and len(set(ids)) == 28
    assert ids[:2] == ["polish-1-a", "polish-1-b"]
    assert ids[25:] == ["polish-1-z", "polish-1-aa", "polish-1-ab"]
    assert {i["state"] for i in backlog.items(tmp_path)} == {"assigned"}
    assert {i["task"] for i in backlog.items(tmp_path)} == set(ids)
    # A retried graduation rebuilds the same 28 blocks.
    again, _ = backlog.apply_triage(tmp_path, triage, at=AT)
    assert again == blocks


def test_items_left_held_by_a_graduated_stage_task_are_reopened(tmp_path):
    """Review L1: when a stage task graduated but its apply_triage or settle failed (the
    graduation swallows it), its items are not stuck: the next stage reopens them."""
    _add(tmp_path, SEARCH, "a")
    _add(tmp_path, RANK, "b")
    [triage] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=AT)
    # polish-triage-1 graduated without apply_triage: its items are still `triage`.
    [again] = backlog.stage(tmp_path, [], {"T1", "T2", "polish-triage-1"}, at=AT)
    assert again["id"] == "polish-triage-2"
    assert again["polish_items"] == ["BL-0001", "BL-0002"]

    other = tmp_path / "other"
    _add(other, SEARCH, "a")
    [polish] = backlog.stage(other, [], {"T1"}, at=AT)
    # polish-1-a graduated without settle: its item is still `assigned`.
    [later] = backlog.stage(other, [], {"T1", "polish-1-a"}, at=AT)
    assert later["id"] == "polish-2-a" and later["polish_items"] == ["BL-0001"]


def test_a_merged_item_s_text_reaches_its_polish_task(tmp_path):
    """Review L2: a merged duplicate's text joins the kept item's criterion line, so the
    polish writer and reviewer can catch a wrong merge."""
    _add(tmp_path, SEARCH, "search does not fold accents")
    _add(tmp_path, RANK, "rank ignores diacritics too")
    [triage] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=AT)
    _write(tmp_path, "docs/polish/triage-1.json", {"items": [
        {"id": "BL-0001", "decision": "keep"},
        {"id": "BL-0002", "decision": "duplicate", "of": "BL-0001", "reason": "same"}]})

    [polish], _ = backlog.apply_triage(tmp_path, triage, at=AT)

    assert polish["acceptance_criteria"] == [
        "BL-0001 (T1, review round 1, proof_review) search does not fold accents"
        ' — also raised as BL-0002 (T2, review round 1, proof_review): '
        '"rank ignores diacritics too"']
