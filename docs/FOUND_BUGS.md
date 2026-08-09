# Found bugs

Behavioural surprises discovered in the reference implementation while characterising it.

**Nothing here is fixed.** Through M1 the extraction is a pure refactor: every surprise below is
copied across verbatim and pinned by a characterisation test, so the extracted code fails in exactly
the same way. Fixing any of them is separate, later, deliberate work.

Format: one entry per surprise, with the test that pins it.

---

## M0 — state and journal (`state.py`)

### 1. `read_json` and `read_state` have opposite failure contracts

`read_json` wraps everything in `except Exception: return {}` — a missing file, a corrupt file, a
directory and a permission error are all indistinguishable from a legitimately empty document.
`read_state`, three lines below it, has no error handling at all: a missing `state.json` raises
`FileNotFoundError` and a corrupt one raises `JSONDecodeError`, uncaught, into the poll loop.

Two adjacent loaders in the same module, opposite contracts, no comment explaining why.

Pinned by `test_read_json_missing_returns_empty_dict`, `test_read_json_directory_returns_empty_dict`,
`test_read_state_raises_when_the_file_is_missing`, `test_read_state_raises_on_malformed_json`.

### 2. `read_state` defaults six keys but not `version` or `in_flight`

The six bookkeeping keys (`hitl_mode`, `waiting_on_dan`, `dan_id_counter`, `retry_counts`,
`parked_tasks`, `launch_times`) get `setdefault`s. The two keys every consumer actually depends on
do not, so a truncated or half-initialised state document fails with a `KeyError` deep inside a
consumer rather than at the loader.

Pinned by `test_read_state_injects_six_defaults`,
`test_read_state_does_not_default_version_or_in_flight`.

### 3. `write_state` mutates its argument

`state["updated_at"] = now_iso()` is applied to the caller's dict before any I/O, and stays applied
even if the write then raises. A function named `write_*` reads as a pure sink.

Pinned by `test_write_state_mutates_the_callers_dict`.

### 4. `write_state` does not create its parent directory

`tmp.write_text(...)` raises `FileNotFoundError` if `.orchestrator/` does not exist. There is no
`mkdir(parents=True, exist_ok=True)`, so the state layer cannot initialise itself on a fresh
checkout — something else must have created the directory first.

Pinned by `test_write_state_requires_an_existing_parent_directory`.

### 5. `write_state`'s temp file is not collision-free, and is not fsynced

The temp path is `STATE_JSON.with_suffix(".json.tmp")` — derived from the target name, with no pid
or uuid component. Two concurrent `write_state` calls therefore share one temp path and can
interleave. Neither the file nor the directory is fsynced before or after the rename, so the
tmp+rename pattern signals a durability guarantee it does not provide, and a crash between the write
and the rename leaves an orphan `state.json.tmp`. The reference project's `.orchestrator/` currently
contains four orphaned `usage.json.tmp<pid>` files, so this does happen in practice.

Partially pinned by `test_write_state_leaves_no_temp_file_behind` (the happy path).

### 6. `_persist_launch_time` is an unlocked read-modify-write of the whole document

It re-reads `state.json` from disk, sets one key, and writes the entire document back. Any unsaved
in-memory edits the caller was holding are silently discarded, and any concurrent writer's changes
made between its read and its write are clobbered.

Pinned by `test_persist_launch_time_rereads_from_disk_and_discards_unsaved_edits`.

### 7. `write_state` writes in the platform default encoding

`Path.write_text()` is called with no `encoding=` argument, while `append_journal` ten lines later
explicitly passes `encoding="utf-8"`. Both pass `ensure_ascii=False`, so non-ASCII content in
`state.json` round-trips correctly only for as long as the process locale is UTF-8. The journal is
hardened against this; the state file is not.

Not separately pinned — asserting it would require running the suite under a non-UTF-8 locale.

### 8. `append_journal` grows without bound and hardcodes the agent name

No rotation, no size cap; the live journal is already ~7.3 MB, and two call sites read it back by
scanning the whole file, so the cost of those lookups grows without limit. The `agent` field is a
hardcoded string literal naming a specific phase of the consuming project — a purity problem for
M2+, since it is copied verbatim into maestro at M1.

Pinned by `test_append_journal_stamps_a_constant_agent`, which asserts the field is constant and
non-empty without importing the literal into maestro.

### 9. `read_json` is annotated `-> dict` but returns whatever JSON contained

A file holding a top-level list, string or number is returned unchanged with that type.

Pinned by `test_read_json_returns_non_dict_json_unchanged`.

---

## M0 — project.yaml (`config.py`)

Characterised but not yet pinned by tests — `test_config.py` lands with the `config` extraction.

### 10. A corrupt `project.yaml` silently disarms the merge safety gates

`_load_project_yaml` is `try: ... except Exception: return {}`. A missing file, a YAML syntax error,
a permission error and an intentionally empty config all produce the same `{}` with no exception, no
log line, no journal entry and no print.

The callers are security gates. With `{}`: the deny-list guard loses every `deny_list_extra` pattern,
the risky-file reviewer short-circuits with `return True, "no risky_set defined"` and skips its
safety review entirely, and the resumable-merge hard-stop loses its risky list. A one-character YAML
typo downgrades the merge gates to permissive with no signal that config loading failed.

### 11. `_load_project_yaml` does not cache, so one decision can read several configs

Four call sites each re-open and re-parse the file. Within a single merge decision the config is
loaded independently at each gate; if `project.yaml` is edited — or is mid-write and truncated —
between those reads, different gates in the same merge evaluate against different configurations.

### 12. Config defaults live at the call sites and disagree with each other

`secrets` defaults to `[".env", ".env.local"]` at one call site while `risky_set` defaults to `[]` at
another, so a missing config still blocks secret-file edits but fully disables the risky-file review.
`bot_files` is defaulted independently in two places, duplicating the value with no single source of
truth.

---

## M0 — test harness (not reference code)

### 13. The planned monkeypatch list named globals that do not exist

`docs/plans/2026-08-09-m0-m1-core-extraction.md` specified repointing `ORCH_DIR`, `STATE` and
`USAGE`. The reference module has none of those names: the globals are `STATE_JSON`, `USAGE_JSON`,
and there is no directory-level global at all. Combined with `monkeypatch.setattr(..., raising=False)`
the mismatch was silent — three unused attributes were created, `write_state` kept resolving the real
`STATE_JSON`, and the harness test overwrote the **live** `state.json` of the running reference
project. The running loop's own read-modify-write restored it within seconds and no data was lost
(its journal shows a full `in_flight` immediately afterwards), but the harness was one unlucky
interleaving away from destroying production state.

Not a reference-code bug — a plan bug, recorded here because it is the most dangerous thing found so
far. Fixed at source: `sandbox` now discovers path globals by reflection instead of from a list, and
`tests/reference_repo.py` installs a session-wide firewall that makes any mutating filesystem call
inside the reference repo raise. See `tests/test_reference_repo_firewall.py`.

## M1 — merge characterisation-test repair (harness findings)

### 14. _merge_prep_branch merges to main with none of merge_and_eval's safety gates (confirmed, now pinned correctly)

reference orchestrator_run.py:2435-2465. `_merge_prep_branch` runs no `deny_list_guard`, no `sonnet_risky_reviewer` and no smoke — only the dirty-worktree guard. A branch whose diff contains a hard-deny pattern (e.g. `DROP TABLE users;`) is refused outright by `deny_list_guard` before the merge, yet `_merge_prep_branch` merges it onto main and returns (True, 'merged b at <sha>'). Pinned by tests/characterization/test_merge.py::test_merge_prep_branch_runs_no_deny_list_or_risky_check, which now asserts the guard verdict BEFORE the merge (afterwards `main...b` is empty, so the guard misleadingly reads 'ok') and then asserts the denied content is tracked on main.

### 15. RETRACTION: `_merge_prep_branch` does NOT require session_id when a branch is given

The previous test docstring claimed 'the `or` fallback is evaluated eagerly, so session_id is mandatory'. That is not true — reference line 2442 `branch = entry.get("branch") or entry["session_id"].lower()` short-circuits, so with a truthy branch `entry["session_id"]` is never evaluated and no KeyError is raised. If an entry for this was ever queued for docs/FOUND_BUGS.md it should not be recorded. The real behaviour (session_id required only on the fallback path, i.e. branch missing or empty) is now pinned by test_merge_prep_branch_needs_a_session_id_only_when_no_branch_is_given.

### 16. Harness note (not reference code): the sandbox's .orchestrator/ directory made porcelain-status assertions vacuous

The `sandbox` fixture creates `<repo>/.orchestrator/workspaces/` before the `repo` fixture runs `git init`, so `git status --porcelain` in the throwaway repo is never empty. This made `== ""` assertions unconditionally fail and, worse, made the `!= ""` assertion in test_commit_eval_history_reports_success_even_when_the_commit_fails unconditionally pass. Both shapes are now routed through a `_dirty(repo)` helper that drops only that directory.

## M1 — verification gates (`gates.py`)

### 17. A missing check_verifications.py is reported as a PASS

run_verification_gate returns (True, "(check_verifications.py not found — gate skipped)") when the checker script is absent. A deleted, renamed or not-yet-deployed gate script therefore silently disarms every verification check instead of failing closed.

### 18. --auto-only slides into the positional worktree slot when worktree is None

run_verification_gate appends str(worktree) only when worktree is not None, then unconditionally appends "--auto-only". With worktree=None and auto_only=True the argv becomes [py, script, task_id, workspace, "--auto-only"], so the checker receives "--auto-only" as its third positional argument (the worktree).

### 19. Gate output glues stdout onto stderr with no separator

output = (r.stdout + r.stderr).strip(). A checker that writes unterminated stdout produces a run-together message: stdout "out" and stderr "err" become "outerr".

### 20. sonnet_review_proofs crashes on a manual verification with no id

manual_items = [(v["id"], v) for v in verifications if v.get("kind") == "manual"] uses subscript, not .get. A hand-edited ROADMAP block that omits `id:` raises KeyError out of the whole gate rather than failing that one verification.

### 21. The proof-length floor strips whitespace by hand and misses \r

nonws_len is computed with stripped.replace(" ", "").replace("\t", "").replace("\n", ""), so a carriage return counts as content. "a\r" * 7 is seven letters plus six interior CRs = 14 "non-ws" chars and clears the floor of 12, escalating a junk proof to the reviewing agent.

### 22. The reviewer's verdict map is never reconciled with the ids that were sent

failures is built by iterating review.items(). An id the agent invented ("V9") is judged and can fail the gate even though it was never sent; an id the agent dropped is silently treated as passing, so an empty "{}" response passes every manual verification.

### 23. The JSON extraction regex is greedy across objects

re.search(r"\{.*\}", stdout, re.DOTALL) matches first-brace-to-last-brace, not one object. Output containing two JSON objects yields an unparseable span and the gate fails with "Sonnet returned invalid JSON" instead of using the first verdict.

### 24. _run_smoke always evaluates REPO, never the worktree

smoke_input = json.dumps({"worktree": str(REPO), ...}). The key is named worktree but the value is the repository root, so the smoke measures the merged trunk; the task's own unmerged changes are never what is scored.

### 25. _run_smoke turns a silent success into a failure labelled exit=0

The success branch requires sp.returncode == 0 AND sp.stdout.strip(). An adapter that exits 0 printing only whitespace falls through to the error branch and returns reason "smoke.py exit=0: no output" — a failure whose text says the process succeeded.

### 26. _run_smoke is the only subprocess here with no cwd=

Every other subprocess call in this layer pins cwd (REPO or the given cwd). _run_smoke does not, so the adapter inherits whatever working directory the orchestrator happens to be in.

### 27. _run_custom_smoke accepts task_id and never uses it

The parameter is unused — not even in the reason string — so _run_custom_smoke("T1", cmd) and _run_custom_smoke("T2", cmd) are indistinguishable, and a custom-smoke result cannot be attributed to a task from its own return value.

### 28. _smoke_for_task str()'s a non-string smoke value and hands it to a shell

custom is taken straight from the ROADMAP task dict and passed as str(custom). A YAML list `smoke:\n  - true` becomes the literal shell command "['true']".

### 29. _await_absent treats any existing path as the artifact, including a directory

It tests p.exists(), not p.is_file(). An empty directory named by await_artifact — or a zero-byte file with no rows in it — counts as "the awaited artifact has landed" and converts a park into a hard failure.

### 30. A gate that printed nothing reads as present-and-failing

With no await_artifact, absence is decided by substring matching against stdout. Empty output matches no signal, so _await_absent returns False and a crashed checker that printed nothing is classified as a genuine below-threshold failure.

### 31. _classify_verifications reports an id-less verification as an empty string

vid = str(v.get("id", "")). A kind:auto block with no id is hard-failed under the name '' — the failure list contains an unnamed entry that cannot be traced back to a ROADMAP item. The same str() silently renames a numeric id 7 to "7".

### 32. A verification with no `expect` passes only if its command prints nothing

expect defaults to "" and the comparison is actual == expect. "cmd: true" passes; "cmd: echo something" hard-fails. An author who omits expect intending "just run it" gets the opposite of that.

### 33. _classify_verifications ignores the exit code entirely

Only r.stdout.strip() is compared with expect. `echo hi; exit 1` with expect "hi" is classified as passing, so a check whose command dies after printing the right line is recorded as a pass.

### 34. actual is stripped but expect is not

actual = r.stdout.strip() while expect is used raw. A ROADMAP expect carrying trailing whitespace or a newline can never match any command output.

### 35. The interpreter substitution regex splits a versioned interpreter

re.sub(r"^(python3?)\b", str(VENV_PYTHON), cmd) — `.` is a word boundary, so `python3.11 -c …` has only its `python3` prefix replaced and becomes `<VENV_PYTHON>.11 -c …`. The rewritten command runs a `.11` sibling of the venv interpreter if one happens to exist, and is unrunnable otherwise.

### 36. A corrupt awaiting_since timestamp parks a task forever

_await_timed_out swallows the parse error and returns False. An unparseable, empty or "None" started_iso means the timeout can never fire, so the task stays in awaiting-verification indefinitely with no escalation.

### 37. _await_timed_out guards the parse but not the subtraction

datetime.fromisoformat is wrapped in try/except, then (now - started) runs outside it. A naive timestamp (no offset) against the default timezone-aware now raises TypeError straight out of the poller.

## M1 — roadmap parsing (`docs/roadmap.py`)

### 38. get_completed_task_ids is all-or-nothing: one entry without `id` erases every completion

`{str(item['id']) for item in data}` sits inside a single try/except returning set(). A single malformed entry in .orchestrator/completed_tasks.json raises KeyError mid-comprehension, so the function reports that NOTHING is complete. Every dependent task then looks unrunnable and the loop silently goes idle instead of failing loudly. Pinned by test_completed_ids_one_bad_entry_discards_every_good_one. A JSON object instead of a list has the same effect (test_completed_ids_json_object_instead_of_list_is_empty_set).

### 39. The ```yaml fence regex is not line-anchored, so indented documentation blocks parse as live tasks

re.findall(r"```yaml\n(.*?)```", content, re.DOTALL) matches an indented fence inside a bullet/list item. A ROADMAP that documents the task schema by example gets that example parsed as a real, dispatchable task. Pinned by test_blocks_are_found_by_a_non_anchored_regex ('T-INDENTED'). Conversely '``` yaml' (one space) silently hides a real block.

### 40. A falsy task id is silently dropped by three parsers but not by get_task_by_id

parse_runnable_tasks, parse_prep_tasks and _load_roadmap_tasks all guard with `not task.get('id')`, so `id: 0` and `id: ""` vanish from every parse with no warning. get_task_by_id uses `str(task.get('id',''))== str(task_id)` and has no truthiness guard, so it *can* return a task the schedulers can never see. Pinned by test_a_falsy_id_is_treated_as_no_id and test_get_task_by_id_ignores_a_block_with_a_falsy_id.

### 41. Inconsistent missing-ROADMAP contract: _load_roadmap_tasks returns [], the three hot-path parsers raise

_load_roadmap_tasks wraps read_text in `except OSError: return []`. parse_runnable_tasks, parse_prep_tasks and get_task_by_id do not, so a missing or unreadable docs/ROADMAP.md raises FileNotFoundError out of the poll loop's PRIMARY parser while the secondary one quietly reports an empty roadmap. Pinned by test_runnable_raises_when_the_roadmap_is_missing, test_prep_raises_when_the_roadmap_is_missing, test_get_task_by_id_raises_when_the_roadmap_is_missing.

### 42. parse_prep_tasks hard-fails on a missing state.json

It parses the ROADMAP happily and then calls read_state(), which does json.loads(STATE_JSON.read_text()) with no guard. On a fresh checkout a *parser* raises FileNotFoundError. Its sibling parse_runnable_tasks never consults state.json at all. Pinned by test_prep_raises_when_state_json_is_missing and test_runnable_does_not_consult_state_json.

### 43. parse_prep_tasks stringifies the task id but not the parked sets, so numeric ids never dedup

tid = str(task['id']), but parked_ids is built from raw p.get('task_id') values in state['waiting_on_dan'] and failed_parked from raw state['parked_tasks']. A ROADMAP task `id: 7` parked as {'task_id': 7} yields tid '7' not in {7}, so the task is prepared a second time while already awaiting Dan. Pinned by test_prep_parked_ids_are_matched_without_string_coercion.

### 44. No dependency-cycle detection anywhere in the roadmap parsers

A task that lists itself in deps (or any dep cycle) is simply never runnable — `all(str(d) in complete_ids for d in deps)` is false forever. Nothing warns; the task disappears from the runnable set permanently and the loop reports idle. Pinned by test_runnable_a_task_may_depend_on_itself_forever.

### 45. run_dep_map guards the two optional steps with .exists() but not the mandatory one

RENDER_DEP_MAP and GEN_UPCOMING are both `.exists()`-guarded; the mandatory `subprocess.run([VENV_PYTHON, GEN_DEP_MAP], capture_output=True)` is not, and its return code is discarded. A missing or broken gen_dependency_map.py is an invisible non-zero exit, and the subsequent Telegram push ships a stale dependency map. Pinned by test_run_dep_map_does_not_check_that_the_generator_exists and test_run_dep_map_returns_none_and_ignores_exit_codes.

### 46. run_dep_map bounds the renderer with timeout=120 but catches nothing

subprocess.run(['bash', RENDER_DEP_MAP], timeout=120) raises TimeoutExpired on a hung renderer, and there is no try/except. Since run_dep_map is called from maybe_push_roadmap_map_change on the per-poll path, a hung graphviz render propagates straight up into the orchestrator loop. Contrast mark_roadmap_complete, which does wrap its bounded GEN_UPCOMING call. Pinned by test_run_dep_map_lets_a_timeout_escape.

### 47. maybe_push_roadmap_map_change's bare except drops the runnable count it already computed

n_runnable and n_prep are computed inside one try; parse_prep_tasks raising FileNotFoundError (missing state.json) sets summary = '' , discarding the runnable count that succeeded. Dan gets '\U0001f5fa Roadmap updated' with no numbers at all and no hint that anything failed. Pinned by test_maybe_push_drops_the_counts_when_state_json_is_missing.

### 48. mark_roadmap_complete's staged-check is the one subprocess call that is not silenced

subprocess.run(['git','diff','--cached','--quiet'], cwd=REPO) omits capture_output, so it inherits the orchestrator's stdout/stderr while every other call in the function captures. Any git noise (e.g. a warning) lands in the orchestrator log unattributed. Pinned by test_mark_complete_diff_check_is_not_capture_output.

### 49. mark_roadmap_complete journals 'removed from ROADMAP.md' even when nothing was removed

append_journal('roadmap_task_removed', f'{task_id} removed from ROADMAP.md') runs unconditionally at the end. If MARK_TASK_COMPLETE does not exist the helper is skipped, the ROADMAP is untouched, the commit is skipped because nothing staged — and the journal still asserts the task was graduated. The journal is the audit trail used to reconstruct what happened. Pinned by test_mark_complete_journals_even_when_every_step_was_skipped. The helper's return code is also ignored (test_mark_complete_ignores_a_failing_helper).

### 50. parse_runnable_tasks' docstring contradicts its filter

The docstring says 'Return autonomous, non-self-modifying tasks', but the filter is `task.get('mode') not in ('autonomous', 'needs-dan')` — a needs-dan task with an explicit `dispatch: auto` IS returned as runnable. The mode check is also case-sensitive, so `mode: Autonomous` silently never runs. Pinned by test_runnable_status_and_flag_matrix.

### 51. _normalize_task's truthiness guard rewrites an explicitly-falsy dispatch to manual

`not task.get('dispatch')` treats `dispatch: ""`, `dispatch: null` and `dispatch: false` as 'no dispatch declared', so a needs-dan task that explicitly disables dispatch is silently converted to dispatch: manual and routed into the auto-prep pipeline. The function also mutates its argument in place and returns the same object. Pinned by test_normalize_task_overrides_a_falsy_explicit_dispatch and test_normalize_task_mutates_its_argument_in_place.

## M1 — implementer briefs and launch (`implementer.py`)

### 52. _question_req_id is a plain hyphen join, so distinct (task, question) pairs collide

`_question_req_id(task_id, qid)` returns f"q-{task_id}-{qid}" with no separator escaping, so ('a-b','c') and ('a','b-c') produce the identical request id 'q-a-b-c'. Both questions are parked as the same Dan-request: the second is never asked and the first question's answer is silently reused as its answer. Task ids with hyphens are the norm in this codebase (e.g. 'p8b2', 'T1-b'), so this is reachable. The same function also does not sanitise path separators even though the result is used directly as a filename: _question_req_id('a/b','q1') == 'q-a/b-q1', which makes QUESTIONS_DIR/'q-a/b-q1.json' resolve into a subdirectory that does not exist (or, with '..', outside QUESTIONS_DIR). Pinned by test_question_req_id_collides_across_the_hyphen_boundary and test_question_req_id_does_not_sanitise_path_separators.

### 53. A question whose id is the integer 0 is silently discarded

`_task_questions` computes `qid = str(q.get("id") or "").strip()`. For `{'id': 0, 'prompt': 'p'}` the `or` sees a falsy-but-present id and yields '', so the entry is dropped. The task then dispatches without ever asking that question, and `_answers_section` never mentions it. Every other non-string id (12, True) is coerced correctly, so the failure is specific to falsy scalars (0, 0.0, False). Pinned by test_task_questions_drops_a_falsy_numeric_id.

### 54. An empty answer passes the launch gate but renders as 'no answer recorded' in the brief

`_questions_ready` treats a question as answered when `_answer_choice(...) is not None`; `_answers_section` treats it as answered only when the value is truthy (`... or "(no answer recorded)"`). An answer file containing '{"choice": ""}' yields '' from `_answer_choice`, which is not None. Result: the gate opens and the implementer is launched, but the brief tells it Dan never answered. The implementer then guesses at exactly the decision the question block existed to pin down. Pinned by test_answer_choice_empty_choice_is_an_empty_string_not_none and test_answers_section_marks_an_empty_answer_as_unrecorded.

### 55. _answer_choice maps a JSON null to two different results depending on nesting

In the dict branch, `{'choice': null}` returns None (unanswered). In the bare-scalar branch, a file containing just `null` falls through to `return str(data).strip()` and returns the literal text 'None'. So the same 'no value' answer either blocks the launch or is injected into the implementer brief as the word None, purely as a function of how telegram_bot.py happened to serialise it. Pinned by test_answer_choice_dict_with_null_choice_is_none and test_answer_choice_stringifies_bare_json_scalars.

### 56. launch_implementer accepts session_id and never uses it, so retries produce two indistinguishable tmux windows

`launch_implementer(task, session_id, ...)` names the tmux window `impl-{task_id}`, not `impl-{session_id}`. A retry launched while a stale window from the previous attempt is still alive creates a second window with the same name; `_kill_tmux_window` targets `agents:impl-<task_id>` and can only reach one of them, so the other keeps burning the shared Claude quota unmonitored. The single journal record it writes, `implementer_model`, also passes no session_id, so the model choice cannot be correlated with the launch afterwards. Pinned by test_launch_implementer_window_is_named_after_the_task_not_the_session and test_launch_implementer_journal_omits_the_session_id.

### 57. The 'never crash' model fall-safe only covers falsy values; model: 3 raises AttributeError mid-launch

The comment on IMPLEMENTER_MODELS says unknown/missing values 'fall safe to Sonnet (never crash, never silently use a wrong model)'. The implementation is `(task.get("model") or "").lower().strip()`, which handles None/''/0/False but raises AttributeError on any truthy non-string — a ROADMAP yaml line `model: 3` (or a list) crashes the launch. Because the raise happens after brief.txt is written but before the tmux call, the workspace is left half-populated. Pinned by test_launch_implementer_non_string_model_raises.

### 58. _synthesize_sentinel overwrites a real FAILED sentinel with its synthetic reason

The function is documented as the recovery path for 'implementer wrote result.json but left no DONE/FAILED sentinel', yet it has no `exists()` guard on the write. If a FAILED sentinel is already present with the implementer's actual failure text, calling _synthesize_sentinel replaces it with 'synthesized FAILED: implementer left no sentinel (result.json status=...)', destroying the only record of why the task failed before diagnosis reads it. Pinned by test_synthesize_sentinel_overwrites_an_existing_sentinel.

### 59. _tail_text with n_lines <= 0 returns the whole file or a head-skip

`splitlines()[-n_lines:]` is sound only for positive n. n_lines=0 becomes `[0:]`, returning the entire log — an unbounded string that flows straight into _diagnose_failure's prompt and into journal/Telegram text. A negative n_lines becomes a positive slice start, i.e. it drops the *first* |n| lines and returns everything after them, which is the opposite of a tail. Pinned by test_tail_text_zero_lines_returns_the_whole_file and test_tail_text_negative_n_drops_leading_lines.

### 60. _latest_impl_tail's glob leaks across hyphenated sibling task ids and misses the unsuffixed workspace

The glob is `impl-{task_id}-*`. Diagnosing task 'T1' therefore also matches workspaces of task 'T1-b' (e.g. 'impl-T1-b-2026...'), and since candidates are sorted by mtime and the newest non-empty log wins, a failure report for T1 can quote a completely different task's implementer log as evidence. Conversely a workspace named exactly `impl-T1` (no trailing timestamp) never matches, so its log is invisible to diagnosis. Pinned by test_latest_impl_tail_leaks_across_hyphenated_sibling_task_ids and test_latest_impl_tail_requires_the_trailing_separator.

### 61. _ask_question comma-joins options with no escaping and has no error handling

`",".join(q["options"])` is passed as a single --options value, so an option containing a comma ('a,b') is indistinguishable from two options — Dan is shown a button set that does not match what the task declared, and the recorded button index maps to the wrong choice. Separately, the `subprocess.run` call has no try/except: a missing VENV_PYTHON or send_dan_request.py raises FileNotFoundError out of `_questions_ready` and into the launch loop, where it is only caught by the blanket handler further up. Pinned by test_ask_question_options_are_ambiguous_when_an_option_contains_a_comma and test_ask_question_propagates_a_missing_sender_binary.

### 62. A dispatch:manual task is gated on Dan's answers but the prep implementer is never shown them

`_answers_section(task)` is appended only by `_make_brief`. `_make_prep_brief` — which `_make_brief` delegates to for `dispatch: manual` before the answers section is ever reached — omits it entirely. So a manual task that also declares a `questions:` block blocks in `_questions_ready` until Dan answers every question, then launches a prep implementer that is told nothing about those answers. The gate costs Dan round-trips and delivers no information. Pinned by test_prep_brief_drops_dans_answers.

### 63. A non-list verifications: block crashes brief generation

`_make_brief` does `verifs = task.get("verifications") or []` and then `for v in verifs: v.get(...)`. A dict-shaped block (`verifications: {V1: {...}}`, an easy YAML mistake) iterates its string keys and raises AttributeError on `str.get`; a plain string iterates its characters and does the same. The raise happens inside launch_implementer's first statement, so the task fails with an opaque AttributeError rather than a ROADMAP validation error. Pinned by test_make_brief_raises_on_a_non_list_verifications_block.

## M1 — merge, deny-list and conflict resolution (`merge.py`)

### 64. _diff_files never inspects git's exit status

maestro/merge.py::_diff_files returns [] whenever `git diff --name-only main...<branch>` fails — a typo'd branch, a deleted branch, or a cwd that is not a git repo all read as "no files changed". deny_list_guard therefore returns (True, "ok") for a branch that does not exist, and _touches_bot_files returns False.

### 65. _diff_files returns git-escaped quoted names for non-ASCII paths

`git diff --name-only` emits core.quotePath-escaped names, so a file named café.txt comes back as '"caf\\303\\251.txt"'. Every consumer (deny_list_guard's `clean in f` substring test, sonnet_risky_reviewer's risky_set match, _clean_worktree_for_merge's set membership against `git status` output) treats these strings as real paths, so the escaped form silently changes meaning.

### 66. deny_list_guard scans the raw diff, so removing a deny-listed line is itself denied

The guard regex-searches the whole `git diff main...<branch>` text, which includes '-' removal lines. A branch whose only change is deleting `DELETE FROM users;` matches r"DELETE\s+FROM" and is refused — cleaning up a dangerous statement blocks its own merge.

### 67. deny_list_guard's sudo exemption lookahead is line-scoped over the whole diff

r"\bsudo\b(?!.*restart_bot\.sh)(?!.*systemctl\b)" is applied with re.search to the concatenated diff. `.` does not cross a newline, so the exemption is granted purely by what follows on the SAME line: `sudo rm -rf /var # systemctl` is waved through, while a later line mentioning systemctl does not rescue an earlier bare sudo.

### 68. deny_list_guard's secret check is a substring match on the path, not a filename match

`clean = secret_pat.strip("*").strip("/")` then `if clean in f` matches anywhere in the path. A template named docs/example.env.sample is refused as a secret, and conversely a real secret living under a directory whose name does not contain the needle is not caught.

### 69. deny_list_guard accepts task_id and never reads it

The parameter is unused: the reason string never mentions the task, so two different tasks on the same branch produce byte-identical results and the escalation to Dan carries no task attribution from the guard itself.

### 70. sonnet_risky_reviewer approves on any truthy "pass" value

`bool(obj.get("pass", False))` — a judge that replies {"pass": "false"} (a JSON string, which some models emit) yields True and the risky merge is approved. Same defect in _resumable_code_change_escalation.

### 71. risky_set entries are matched as substrings of the changed path

`any(r in f for r in risky_set)` — a short risky_set entry such as "core" captures docs/hardcore-notes.md and sends it to the paid reviewer, while a reviewer approval on that noise still gates the real merge.

### 72. The reviewed diff is truncated at 6000 chars with no marker

`diff_r.stdout[:6000]` cuts mid-hunk and no truncation notice is added to the prompt, so the reviewer confidently judges a fragment and never knows the tail (which may contain the actual regression) was withheld. Same 6000-char cut in _resumable_code_change_escalation.

### 73. _git_head silently returns "" on failure

The returncode is never checked, so outside a git repo (or on any git error) _git_head() is "". merge_and_eval then compares sha_post != sha_pre as "" != "", concludes no new commit, and the revert branch prints "nothing to revert" — a failed git invocation is indistinguishable from a genuine no-op.

### 74. _commit_eval_history reports success when the commit failed

Neither `git add` nor `git commit` has its returncode checked. The return value only means "the file was dirty", and append_journal("eval_history_committed") is written unconditionally — so a failed commit leaves the row staged-but-uncommitted while the journal claims it was committed, re-arming the dirty-worktree merge block the function exists to prevent.

### 75. _clean_worktree_for_merge is blind to untracked files

`git status --porcelain --untracked-files=no` hides untracked files, so an untracked file the incoming merge wants to create is not reported as blocking. The caller proceeds and `git merge` fails with "untracked working tree files would be overwritten", which the caller reports as a merge conflict.

### 76. _clean_worktree_for_merge misses any path containing a space

`git status --porcelain` quotes such a path ("two words.txt") while `git diff --name-only` does not, so `p in dirty` is False, the dirty file is neither cleaned nor reported as blocking, and the caller's `git merge` fails with the misleading "local changes would be overwritten".

### 77. _clean_worktree_for_merge treats a git failure as "clean"

`if st.returncode != 0: return True, []` — outside a repo, or with a corrupted index, the function reports the tree clean and the caller proceeds to merge.

### 78. _autoresolve_doc_conflicts stages ALL regenerable artifacts, aborting if any is absent

`git add -- *_MERGE_DISCARDABLE` fails with "pathspec did not match any files" as soon as one entry of the tuple is missing from the checkout; nothing is staged, `git commit --no-edit` refuses because the path is still unmerged, and the function returns False — leaving a conflicted merge in progress that the caller reads as a genuine content conflict. run_dep_map() has already run at that point, so the failure is not even side-effect free.

### 79. _parse_progress's fallback regex matches any N/M in the text

r"(\d+)\s*/\s*(\d+)" reads "finished on 2026/08" as (2026, 8) and a version string as progress. It also ignores a leading sign, so "PROGRESS=-3/10" parses as (3, 10), and it raises TypeError rather than returning None on a non-string input.

### 80. _merge_data_only allows docs/ but never calls the doc auto-resolver

RESUMABLE_MERGE_PREFIXES includes "docs/", which contains the regenerable artifacts in _MERGE_DISCARDABLE, yet this path returns "conflict" without trying _autoresolve_doc_conflicts() — the same conflict merge_and_eval and _merge_prep_branch recover from automatically. A resumable sitting parks for a human over a file that is regenerated from source.

### 81. _merge_prep_branch runs none of merge_and_eval's safety gates

The prep path goes straight from `git log main..branch` to `git merge --no-ff` onto main with no deny_list_guard and no sonnet_risky_reviewer. A diff that deny_list_guard refuses (e.g. containing DROP TABLE) lands on main unreviewed.

### 82. The gated merges split `git diff --name-only` output on whitespace

_merge_self_fix_branch and _merge_redo_branch use `.stdout.split()`, so "docs/two words.md" arrives at the path gate as two entries, "docs/two" and "words.md". The gate's allow/deny prefixes are then applied to fragments, and _clean_worktree_for_merge is handed paths that do not exist. merge_and_eval, _merge_prep_branch and the approved branch of _resumable_code_change_escalation have the same `.split()`.

### 83. Neither gated merge auto-resolves a regenerable-doc conflict

_merge_self_fix_branch and _merge_redo_branch abort on any non-zero `git merge` without calling _autoresolve_doc_conflicts(), unlike _merge_prep_branch and merge_and_eval — so a self-fix or /redo branch that predates a dependency-map regeneration fails its merge gate over an auto-generated file.

### 84. _resumable_code_change_escalation calls the judge with no model=

Unlike sonnet_risky_reviewer, which pins model="claude-sonnet-4-6", this call passes only system= and user=. The no-eval merge path therefore silently rides whatever _judge_complete's default model is — the expensive one — on every resumable sitting that strays out of the allowlist.

### 85. _resumable_code_change_escalation does not guard the judge call itself

The try/except wraps only the JSON parse; the `_judge_complete(...)` call is outside it. sonnet_risky_reviewer wraps the equivalent call and fails safe with "risky reviewer error: ...", but here a CLI exception propagates out of the merge path instead of parking for Dan.

### 86. merge_and_eval indexes required keys with [] and crashes on a malformed entry

`entry["task_id"]` and `entry["session_id"]` (the latter also inside the branch fallback and in every append_journal call) raise KeyError out of the merge instead of being refused. _merge_prep_branch has the same shape: `entry.get("branch") or entry["session_id"].lower()` short-circuits on a truthy branch, so session_id becomes mandatory only when branch is missing or empty.

### 87. merge_and_eval indexes smoke["pass"] after reading everything else with .get

`if smoke["pass"]:` (and the two print statements above it) require the key. A smoke implementation that returns a dict without "pass" — the same shape the function tolerates for "metrics" and "reason" — raises KeyError after the merge commit has already landed on main, leaving the merge neither accepted nor reverted.

### 88. The resumable merge paths push with both -C REPO and cwd=REPO and ignore the result

`subprocess.run(["git", "-C", str(REPO), "push", "origin", "main"], cwd=str(REPO), capture_output=True)` in both _merge_data_only and _resumable_code_change_escalation: the redundancy is harmless, but the ignored returncode means a rejected or unauthenticated push still yields "ok", so main diverges from origin with no signal.
