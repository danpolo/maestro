# Found bugs — Telegram control plane (`hitl.commands`)

Behavioural surprises discovered while writing `tests/characterization/test_commands.py`
against the reference implementation of `poll_control_commands`, `_build_progress_report`,
`_show_manual`, `_show_detail`, `_show_waiting`, `_handle_ask`, `_handle_redo`,
`_resolve_waiting`, `_process_approve`, `_process_reject`, `_process_fix` and `run_status`.

Nothing here is fixed. Every item is pinned by a test that asserts the *current* behaviour.

---

## C1 — `_process_reject` raises `NameError`: `REPO_ROOT` is never defined

`_process_reject` finishes a non-manual-action rejection with:

```python
subprocess.run(["git", "worktree", "remove", "--force", worktree],
               capture_output=True, cwd=REPO_ROOT)
subprocess.run(["git", "branch", "-D", branch], capture_output=True, cwd=REPO_ROOT)
```

`REPO_ROOT` is referenced on exactly those two lines and assigned nowhere in the 4,567-line
module (the real global is `REPO`). So **every `/reject` of a normal HITL-parked task raises
`NameError`**, and it raises *after* the function has already:

1. sent "❌ Rejected `<task>` — discarding branch `<branch>`" to Dan,
2. appended `hitl_rejected` to the journal,
3. deleted the waiting entry and persisted the new state,
4. removed the workspace `PAUSED` sentinel.

The branch and the worktree are therefore never deleted, but Dan is told they were. Because
`poll_control_commands` swallows every exception, the operator sees the success message and
no error at all — the leak is completely silent.

Pinned by `test_process_reject_of_a_normal_parked_task_raises_a_name_error_on_repo_root`
and `test_reject_of_a_normal_parked_task_is_swallowed_by_the_router`.

---

## C2 — one bad update kills the rest of the batch, and the lost updates never come back

`poll_control_commands` has a single `try:` around the whole `for upd in data.get("result")`
loop. There is no per-update guard, so any handler exception aborts the batch. The offset,
however, is advanced at the *top* of each iteration:

```python
for upd in data.get("result", []):
    _getUpdates_offset = upd["update_id"] + 1
```

So after a failure the offset already points past the failing update, and the following
updates in the same batch are acknowledged-by-omission: the next `getUpdates` asks for
`offset = failing_id + 1` and Telegram drops everything at or below it. Commands Dan sent
are silently discarded with no reply of any kind.

Trivially reachable: on a repo whose `state.json` does not exist yet, `/pause` raises inside
`read_state()` and a `/halt` sent in the same batch is never executed.

Pinned by `test_poll_swallows_a_handler_exception_and_abandons_every_later_update`.

---

## C3 — `/approve` and `/reject` are dispatched off the lower-cased text, `/fix` and `/unpark` are not

The router lower-cases the message (`text = text_raw.lower()`) and then:

| verb | argument transform |
|---|---|
| `/approve <id>` | `text.split(" ", 1)[1].strip()` — **stays lower case** |
| `/reject <id>` | `text.split(" ", 1)[1].strip()` — **stays lower case** |
| `/fix <id>` | `.upper()` |
| `/unpark <id>` | `.upper()` |
| `/detail <id>` | `.upper()` |
| `/ask` / `/redo` | taken from `text_raw`, original case preserved |

`_process_approve`/`_process_reject` look their argument up in `waiting_on_dan` by exact key
and never fall back to a task-id match (unlike `_resolve_waiting`, which `/ask` and `/redo`
use). The numeric dan_id is unaffected, but the affordance lines the bot itself prints in
`_show_manual` / `_show_detail` are the only way a user learns the id — anyone who types
`/approve P12C` instead gets "No pending item with ID p12c."

Pinned by `test_approve_forwards_the_lowercased_id_and_the_live_in_flight_list`,
`test_reject_forwards_the_lowercased_id_unchanged`,
`test_fix_uppercases_its_argument_unlike_approve_and_reject`.

---

## C4 — `_show_manual` and `_show_detail` disagree about what "blocked" means

* `_show_manual`: `blocked = [d for d in deps if d in pending_ids and d not in complete_ids]`
  — a dep is blocking only if it is **still in ROADMAP.md**.
* `_show_detail` (one-action view): `blocked = [d for d in deps if d not in complete]` —
  completion is checked alone.

`mark_task_complete.py` strips graduated tasks out of ROADMAP.md, and `completed_tasks.json`
is a separate file. A dep that graduated before `completed_tasks.json` existed (or that was
completed out-of-band) therefore reads as *ready* in `/manual` and *blocked* in `/detail`
for the same task, in the same poll.

Pinned by `test_show_manual_reports_blocked_deps_only_for_deps_still_present_in_the_roadmap`
and `test_show_detail_blocks_on_any_dep_absent_from_completed_tasks_even_if_graduated`.

---

## C5 — `/progress` is answered with total silence when `docs/ROADMAP.md` is absent

`_build_progress_report` calls `get_task_by_id(tid)` per in-flight task, and
`get_task_by_id` does an unguarded `ROADMAP_FILE.read_text(...)`. With tasks in flight and no
ROADMAP.md the report raises `FileNotFoundError`, which `poll_control_commands` swallows —
so `/progress` produces no reply at all rather than an error. The empty-queue path returns
before the read, so `/progress` "works" until the first task launches.

Pinned by `test_progress_report_raises_when_the_roadmap_is_missing_and_poll_swallows_it`.

---

## C6 — `_process_reject` of a manual-action item also reads ROADMAP.md unguarded

`(get_task_by_id(task_id) or {}).get("dispatch") == "async-job"` is evaluated *after* the
waiting entry has been removed from the in-memory dict but *before* `write_state`. A missing
ROADMAP.md raises there, so the item stays parked on disk (state was never written) while
Dan gets no reply. Same swallowing as C2 applies through the router.

Pinned by `test_process_reject_of_a_manual_action_raises_when_the_roadmap_is_missing`.

---

## C7 — `_show_waiting` raises `KeyError` on a waiting entry without `parked_at`

`info['task_id']` and `info['parked_at'][:10]` are subscripted directly, unlike every other
read of a waiting entry in the module (which uses `.get`). A single malformed entry makes
`/waiting` reply with nothing at all — and because `/waiting` is the discovery mechanism for
dan_ids, the operator loses the ability to approve *any* parked item.

Pinned by `test_show_waiting_raises_on_a_waiting_entry_that_never_recorded_parked_at`.

---

## C8 — `_process_approve` drops the waiting entry before it attempts the merge

The sequence is: notify → journal → `del waiting[dan_id]` → `write_state` → `merge_and_eval`.
If the merge raises (or the process dies mid-merge), the item is already gone from
`waiting_on_dan`, so it can never be re-approved; recovery needs a hand edit of `state.json`.

Pinned by `test_process_approve_drops_the_waiting_entry_before_the_merge_is_attempted`.

---

## C9 — `_handle_redo` does not apply the vanished-worktree repair `_handle_ask` does

`_handle_ask` recreates a missing worktree directory when a resumable session uuid exists
(the Claude session JSONL is keyed to the worktree path), and otherwise falls back to `REPO`.
`_handle_redo` copies `entry["worktree"]` verbatim with no existence check and no fallback,
so `maestro_redo.py` can be handed a path that no longer exists — the exact failure mode the
comment in `_handle_ask` says it was written to avoid.

Pinned by `test_handle_redo_does_not_recreate_a_vanished_worktree_the_way_ask_does`.

---

## C10 — `_show_manual`'s "Could not parse ROADMAP" branch is unreachable

`_show_manual` and `_show_detail` both wrap `_load_roadmap_tasks()` in
`try/except Exception` and reply "Could not parse ROADMAP: {exc}". But `_load_roadmap_tasks`
already catches `OSError` (→ `[]`) and `yaml.YAMLError` per block (→ skip), so a missing,
unreadable or malformed roadmap silently produces an empty task list. `/manual` on a repo
with no ROADMAP.md answers "No Dan-must-perform tasks in the queue.", and `/detail X`
answers "No task X in ROADMAP (it may be graduated…)".

Pinned by `test_show_manual_treats_a_missing_roadmap_as_an_empty_one` and
`test_show_detail_treats_a_missing_roadmap_as_a_graduated_task`.

---

## C11 — unknown commands, bare verbs and `/hitl <junk>` are all answered with silence

* `/frobnicate` — falls through every branch; the final `elif text_raw and not
  text_raw.startswith("/")` excludes it, so nothing is sent.
* `/approve`, `/reject`, `/fix`, `/unpark`, `/hitl` with no argument — the branches match on
  a trailing space (`text.startswith("/approve ")`), so the bare verb is also silent. There
  is no usage hint, unlike `/detail`, `/ask` and `/redo` which do print one.
* `/hitl maybe` — the `if arg == "on" / elif arg == "off"` chain has no `else`, so state is
  read, nothing is changed, and nothing is sent.

Pinned by `test_poll_ignores_an_unknown_slash_command_without_replying`,
`test_poll_ignores_a_bare_verb_that_the_router_only_accepts_with_an_argument`,
`test_hitl_with_any_other_argument_is_read_but_silently_ignored`.

---

## C12 — `_show_detail` prints a "Verifications:" header for a block with no usable entries

`if verifications:` is truthy for any non-empty list, but the loop `continue`s over every
non-dict element. A `verifications:` list of plain strings therefore renders a bare
`Verifications:` header with nothing under it, instead of the "No structured verifications
in this task block." message.

Pinned by `test_show_detail_keeps_the_verifications_header_even_when_every_entry_is_skipped`.

---

## C13 — `/hitl on|off` mutates state without a journal entry

Every other state-mutating control verb (`/pause`, `/resume`, `/halt`, `/unpark`) appends a
`control_*` journal record. `/hitl` writes `hitl_mode` into `state.json` and sends a
confirmation, but leaves no audit trail, so a run's merge behaviour can change with nothing
in the journal explaining why.

Pinned by `test_hitl_on_and_off_toggle_the_state_flag_without_journalling`.

---

## C14 — `run_status` runs the status script un-captured, un-timed and un-guarded

`subprocess.run([VENV_PYTHON, STATUS_SCRIPT], cwd=REPO)` has no `capture_output`, no
`timeout` and no `try`. The child's stdout/stderr go straight to the orchestrator's own
terminal, a hung status script blocks the main loop forever, and a missing interpreter
raises `FileNotFoundError` into the caller. It is called on both `_process_approve` exit
paths, i.e. from the swallowing router.

Pinned by `test_run_status_runs_the_status_script_in_the_repo_without_capturing_its_output`
and `test_run_status_does_not_shield_the_caller_from_a_launch_failure`.

---

## C15 — `_process_fix` leaves the diagnosis on disk if the self-fix runner throws

`attempt_self_fix(...)` is called before `f.unlink(missing_ok=True)`. The journal already
records `fix_approved` and Dan has already been told the fix is being implemented, so a
crash inside the runner leaves a re-approvable diagnosis and a misleading confirmation. (The
converse ordering would be its own bug; recorded only as observed behaviour.)

Pinned by `test_process_fix_leaves_the_diagnosis_on_disk_when_the_runner_raises`.

---

## Notes (observed, not obviously wrong)

* `_build_progress_report` renders `pct: 0` because it tests `pj.get("pct") is not None`
  rather than truthiness — the one place in this module that gets the falsy-zero case right.
* `_resolve_waiting` resolves a dan_id key before a task_id, so a task literally named `9`
  is shadowed by the waiting entry keyed `"9"`.
* `/detail <id> full` is matched with `raw.upper().endswith(" FULL")`, and the router has
  already upper-cased the argument, so any casing works.
* `poll_control_commands` advances `_getUpdates_offset` for updates from foreign chats and
  for callback queries it then discards — correct for offset hygiene, but it means an update
  is consumed even when nothing acts on it.
