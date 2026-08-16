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

## M1 — Telegram HITL transport (`hitl/telegram.py`)

### 89. `_pending_danreqs` sorts `ts` as text, so a UTC-offset spelling lands in the wrong slot

`out.sort(key=lambda r: r.get("ts", ""), reverse=True)` (L1831) compares ISO timestamps as raw
strings. Any record whose `ts` is written with a non-Zulu offset is ordered by its wall-clock
text rather than by the instant it names: `2026-01-01T01:00:00+05:00` (= 2025-12-31T20:00Z) sorts
*above* `2026-01-01T00:00:00Z`, so the older request is reported as "newest first". The same
string comparison mis-orders an unpadded month — `2026-1-01…` outranks every `2026-02-01…`
because `"1" > "0"`. This is not cosmetic: `_route_freetext_answer` (L1885) falls back to
`pend[0]` whenever the inbound message is not a reply, so an untargeted free-text answer from Dan
is recorded against the wrong Dan-request, and `_record_danreq_answer` then flips that request to
`answered` and writes its `.answer` sidecar. The producer side (`send_dan_request.py`) happens to
emit one spelling today, so the bug is latent — the sort is only safe by convention, not by
construction.

### 90. Harness note (not reference code): the suite's own end-to-end callback test was reading the wrong argv element

Not a reference bug, but worth recording because it hid what the test claimed to check:
`_tg_api` (L1805) builds argv as `curl -s --max-time 5 <url>` and *then* appends a
`--data-urlencode k=v` pair per parameter, so the endpoint URL is `argv[4]` and the last element
is the final parameter value. The test asserted on `argv[-1]`, i.e. on
`text=✅ Keep` / `reply_markup={"inline_keyboard": []}` instead of on the method names. The two
Bot API calls that `_handle_danreq_callback` (L1851) issues on a successful tap
(`answerCallbackQuery` then `editMessageReplyMarkup`) were therefore never actually verified by
that test. It now asserts on `argv[4]` and additionally pins the encoded parameter pairs of both
calls, which is what makes it an end-to-end check rather than a re-run of the `_tg_api` unit
tests.

## M1 — Telegram control plane (`hitl/commands.py`)

### 91. `_process_reject` raises `NameError`: `REPO_ROOT` is never defined

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

### 92. One bad update kills the rest of the batch, and the lost updates never come back

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

### 93. `/approve` and `/reject` are dispatched off the lower-cased text, `/fix` and `/unpark` are not

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

### 94. `_show_manual` and `_show_detail` disagree about what "blocked" means

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

### 95. `/progress` is answered with total silence when `docs/ROADMAP.md` is absent

`_build_progress_report` calls `get_task_by_id(tid)` per in-flight task, and
`get_task_by_id` does an unguarded `ROADMAP_FILE.read_text(...)`. With tasks in flight and no
ROADMAP.md the report raises `FileNotFoundError`, which `poll_control_commands` swallows —
so `/progress` produces no reply at all rather than an error. The empty-queue path returns
before the read, so `/progress` "works" until the first task launches.

Pinned by `test_progress_report_raises_when_the_roadmap_is_missing_and_poll_swallows_it`.

### 96. `_process_reject` of a manual-action item also reads ROADMAP.md unguarded

`(get_task_by_id(task_id) or {}).get("dispatch") == "async-job"` is evaluated *after* the
waiting entry has been removed from the in-memory dict but *before* `write_state`. A missing
ROADMAP.md raises there, so the item stays parked on disk (state was never written) while
Dan gets no reply. Same swallowing as entry 92 applies through the router.

Pinned by `test_process_reject_of_a_manual_action_raises_when_the_roadmap_is_missing`.

### 97. `_show_waiting` raises `KeyError` on a waiting entry without `parked_at`

`info['task_id']` and `info['parked_at'][:10]` are subscripted directly, unlike every other
read of a waiting entry in the module (which uses `.get`). A single malformed entry makes
`/waiting` reply with nothing at all — and because `/waiting` is the discovery mechanism for
dan_ids, the operator loses the ability to approve *any* parked item.

Pinned by `test_show_waiting_raises_on_a_waiting_entry_that_never_recorded_parked_at`.

### 98. `_process_approve` drops the waiting entry before it attempts the merge

The sequence is: notify → journal → `del waiting[dan_id]` → `write_state` → `merge_and_eval`.
If the merge raises (or the process dies mid-merge), the item is already gone from
`waiting_on_dan`, so it can never be re-approved; recovery needs a hand edit of `state.json`.

Pinned by `test_process_approve_drops_the_waiting_entry_before_the_merge_is_attempted`.

### 99. `_handle_redo` does not apply the vanished-worktree repair `_handle_ask` does

`_handle_ask` recreates a missing worktree directory when a resumable session uuid exists
(the Claude session JSONL is keyed to the worktree path), and otherwise falls back to `REPO`.
`_handle_redo` copies `entry["worktree"]` verbatim with no existence check and no fallback,
so `maestro_redo.py` can be handed a path that no longer exists — the exact failure mode the
comment in `_handle_ask` says it was written to avoid.

Pinned by `test_handle_redo_does_not_recreate_a_vanished_worktree_the_way_ask_does`.

### 100. `_show_manual`'s "Could not parse ROADMAP" branch is unreachable

`_show_manual` and `_show_detail` both wrap `_load_roadmap_tasks()` in
`try/except Exception` and reply "Could not parse ROADMAP: {exc}". But `_load_roadmap_tasks`
already catches `OSError` (→ `[]`) and `yaml.YAMLError` per block (→ skip), so a missing,
unreadable or malformed roadmap silently produces an empty task list. `/manual` on a repo
with no ROADMAP.md answers "No Dan-must-perform tasks in the queue.", and `/detail X`
answers "No task X in ROADMAP (it may be graduated…)".

Pinned by `test_show_manual_treats_a_missing_roadmap_as_an_empty_one` and
`test_show_detail_treats_a_missing_roadmap_as_a_graduated_task`.

### 101. Unknown commands, bare verbs and `/hitl <junk>` are all answered with silence

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

### 102. `_show_detail` prints a "Verifications:" header for a block with no usable entries

`if verifications:` is truthy for any non-empty list, but the loop `continue`s over every
non-dict element. A `verifications:` list of plain strings therefore renders a bare
`Verifications:` header with nothing under it, instead of the "No structured verifications
in this task block." message.

Pinned by `test_show_detail_keeps_the_verifications_header_even_when_every_entry_is_skipped`.

### 103. `/hitl on|off` mutates state without a journal entry

Every other state-mutating control verb (`/pause`, `/resume`, `/halt`, `/unpark`) appends a
`control_*` journal record. `/hitl` writes `hitl_mode` into `state.json` and sends a
confirmation, but leaves no audit trail, so a run's merge behaviour can change with nothing
in the journal explaining why.

Pinned by `test_hitl_on_and_off_toggle_the_state_flag_without_journalling`.

### 104. `run_status` runs the status script un-captured, un-timed and un-guarded

`subprocess.run([VENV_PYTHON, STATUS_SCRIPT], cwd=REPO)` has no `capture_output`, no
`timeout` and no `try`. The child's stdout/stderr go straight to the orchestrator's own
terminal, a hung status script blocks the main loop forever, and a missing interpreter
raises `FileNotFoundError` into the caller. It is called on both `_process_approve` exit
paths, i.e. from the swallowing router.

Pinned by `test_run_status_runs_the_status_script_in_the_repo_without_capturing_its_output`
and `test_run_status_does_not_shield_the_caller_from_a_launch_failure`.

### 105. `_process_fix` leaves the diagnosis on disk if the self-fix runner throws

`attempt_self_fix(...)` is called before `f.unlink(missing_ok=True)`. The journal already
records `fix_approved` and Dan has already been told the fix is being implemented, so a
crash inside the runner leaves a re-approvable diagnosis and a misleading confirmation. (The
converse ordering would be its own bug; recorded only as observed behaviour.)

Pinned by `test_process_fix_leaves_the_diagnosis_on_disk_when_the_runner_raises`.

### 106. `/approve` reports the phase completion with a task definition it has just deleted

`_process_approve` finalizes an accepted merge in this order (reference L2879-2896):

```python
remove_worktree(worktree)
mark_roadmap_complete(task_id)
...
phase_report(task_id, get_task_by_id(task_id), smoke)
```

`mark_roadmap_complete` graduates the task by *stripping its yaml block out of
ROADMAP.md*, and `get_task_by_id` reads ROADMAP.md. So by the time `phase_report` is
handed a task definition, the lookup returns `None`. `phase_report` guards with
`(task_def or {})`, so nothing raises — it just silently degrades: `short_desc` falls back
to the bare task id and the `decisions_made` / "⏳ Veto window: 24 h" block is never
emitted. The one report Dan gets on an approved task is the one that cannot see the task.
(The autonomous completion path in `main` has the same ordering.)

### 107. A failed canary deploy tells Dan nothing at all

On the accepted path, when the merged branch `_touches_bot_files`, a non-zero canary
deploy exits the finalize with only a journal line (reference L2891-2894):

```python
append_journal("canary_reverted", f"{task_id} canary deploy failed")
run_dep_map(); run_status()
return
```

There is no `notify_telegram`. Dan has already been sent "✅ Approved `<task>` — merging
now…", so from Telegram the approval simply stops: no "merged & accepted", no warning, no
error. The task is also left graduated out of ROADMAP.md and pushed to `main`, i.e. the
revert the event name implies never happens here.

Pinned by `test_process_approve_aborts_the_finalize_when_a_bot_file_canary_deploy_fails`.

### 108. `/ask` and `/redo` build their tmux command by unquoted string interpolation

Both handlers launch their helper as a single shell string (reference L3013-3016 and
L3067-3070):

```python
tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n {window} "
            f"'cd {REPO} && {VENV_PYTHON} {helper} {req_path}'")
subprocess.run(tmux_cmd, shell=True, check=True)
```

`window` is `f"ask-{task_id}"` and `req_path` embeds the same `task_id`, neither quoted nor
validated. A ROADMAP task id containing a space, a quote or a shell metacharacter — or a
repo path with a space — produces a malformed or injected command line. It fails loudly
only because of `check=True`; the resulting `CalledProcessError` is reported to Dan as
"⚠ /ask failed to launch", which does not hint at the cause.

### 109. Control-plane notes (observed, not obviously wrong)

* `_build_progress_report` renders `pct: 0` because it tests `pj.get("pct") is not None`
  rather than truthiness — the one place in this module that gets the falsy-zero case right.
* `_resolve_waiting` resolves a dan_id key before a task_id, so a task literally named `9`
  is shadowed by the waiting entry keyed `"9"`.
* `/detail <id> full` is matched with `raw.upper().endswith(" FULL")`, and the router has
  already upper-cased the argument, so any casing works.
* `poll_control_commands` advances `_getUpdates_offset` for updates from foreign chats and
  for callback queries it then discards — correct for offset hygiene, but it means an update
  is consumed even when nothing acts on it.

## M1 — self-heal: diagnose / self-fix / redo (`selfheal/`)

### 110. `_self_fix_path_ok` / `_redo_path_ok`: `lstrip("./")` lets a parent traversal through the allowlist

`files = [str(f).strip().lstrip("./") for f in ...]` strips a *character set*, not a prefix.
`"../scripts/x.py"` normalises to `"scripts/x.py"` and passes the allowlist; so do
`"../../../scripts/x.py"` and `"/scripts/x.py"`. The gate that is supposed to confine an
unattended self-fix to the orchestrator's own surface therefore cannot see that a target
points outside the repo at all. Both gates share the bug.

Severity: this is the primary safety gate for unattended code edits.

Tests: `test_self_fix_path_gate_lets_a_parent_traversal_in`,
`test_redo_path_gate_lets_a_parent_traversal_in`.

### 111. Both path gates iterate a bare string character by character

`target_files` arriving as a string (an agent that answered `"target_files": "scripts/x.py"`
instead of a list) is not detected. The comprehension iterates the characters, so the gate
decides on the first letter: `_self_fix_path_ok("scripts/x.py")` returns
`(False, "out-of-scope path: s")`. Fails closed here, but the reason is nonsense, and the
same shape would fail *open* if the first characters happened to spell an allowed prefix.

Tests: `test_self_fix_path_gate_iterates_a_bare_string_character_by_character`,
`test_redo_path_gate_iterates_a_bare_string_character_by_character`.

### 112. The deny lists are substring tests

`any(d in fn for d in SELF_FIX_DENY)` matches anywhere in the path, so
`docs/notes-about-main_bot.py.md` (a doc *about* the bot) is vetoed, and
`scripts/requirements_helper.py` is vetoed by the `"requirements"` fragment. A denied
fragment vetoes the whole change set, so one such false positive turns an auto-fix into an
advisory. Same in `_redo_path_ok`.

Test: `test_self_fix_path_gate_matches_a_denied_fragment_anywhere_in_the_path`.

### 113. `_redo_path_ok(None)` raises; its self-fix twin does not

`_self_fix_path_ok` guards with `for f in (files or [])`; `_redo_path_ok` iterates `files`
directly, so a `None` change set raises `TypeError` out of the gate instead of returning
`(False, …)`. The two functions are otherwise the same shape.

Test: `test_redo_path_gate_crashes_on_none`.

### 114. `_self_fix_rate_ok` matches the journal as raw text

The loop-breaker does `if "self_fix_applied" not in ln or failure_class not in ln`. Both
tests are substring tests on the un-parsed line, so:

* a class that is a substring of another class rate-limits it (`"infra"` is blocked by a
  `"transient-infra"` fix);
* any record that merely *mentions* `self_fix_applied` in its detail counts as an applied
  fix;
* `failure_class == ""` matches every line (`"" in ln` is always true), so a class-less
  diagnosis is rate-limited by any self-fix at all. `_self_fix_eligible` happens to reject
  an empty class earlier, so today this is only reachable on a direct call.

Tests: `test_rate_gate_matches_the_class_as_a_substring_of_the_raw_line`,
`test_rate_gate_matches_the_event_name_as_a_substring_too`,
`test_rate_gate_treats_an_empty_class_as_matching_every_self_fix`.

### 115. `_self_fix_rate_ok` fails OPEN on an unreadable journal

The whole read is wrapped in `try: … except Exception: pass` followed by `return True`. If
the journal cannot be opened (permissions, or the path being a directory), the per-class
rate limiter silently disappears and every self-fix is allowed. The only signal is that
nothing is logged.

Test: `test_rate_gate_allows_when_the_journal_cannot_be_opened`.

### 116. `_self_fix_rate_ok` reads timestamps as UTC no matter what they say

`datetime.strptime(json.loads(ln).get("ts","")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=utc)`
truncates to 19 characters, which discards any offset (`+05:00`) and then *asserts* UTC. A
journal written by a differently-configured writer is mis-dated by the offset, which can
put a recent fix outside the window (or a stale one inside it). Any unparseable `ts` — or a
record with no `ts` — is skipped entirely, so that record can never rate-limit anything.

Tests: `test_rate_gate_reads_only_the_first_nineteen_characters_of_the_timestamp`,
`test_rate_gate_ignores_a_record_with_no_usable_timestamp`.

### 117. `_self_fix_eligible` crashes on a non-string `failure_class`

`cls = (diag.get("failure_class") or "").strip()` only defends against a *falsy* value. A
diagnosis whose JSON gave a number or a list for `failure_class` raises `AttributeError`
out of the gate — an agent-controlled input crashing the caller.

Test: `test_eligibility_crashes_on_a_non_string_class`.

### 118. `_diagnose_failure` / `_failure_looks_normal`: greedy `\{.*\}` loses both objects

`re.search(r"\{.*\}", raw, re.DOTALL)` spans first-brace to last-brace. An agent that emits
its answer twice (or emits one object plus an example) produces a span that is not valid
JSON, so the pass returns `{}`. For `_failure_looks_normal` that empty result is the
fail-open value — the skeptic veto is silently skipped and the auto-fix proceeds.

Test: `test_judgment_pass_loses_both_objects_when_the_agent_prints_two`.

### 119. Neither judgment pass validates the shape of what it parsed

`{"totally": "unrelated"}` is returned verbatim; the callers' key expectations
(`failure_class`, `verdict`, `confidence`) are never enforced at the parse boundary.

Test: `test_judgment_pass_returns_whatever_keys_the_agent_invented`.

### 120. `_judge_complete` reports a successful-but-silent agent as `rc=0`

`if r.returncode == 0 and out` — an exit-0 call with blank stdout falls through to the
diagnostic `print(… rc=0 stderr=…)` and returns `""`. The operator-facing line looks like a
success being logged as a failure, and the caller cannot distinguish "agent said nothing"
from "agent crashed".

Test: `test_judge_returns_empty_on_blank_stdout_at_exit_zero`.

### 121. `_persist_diagnosis` keeps an explicit `null`

`diag.get("failure_class", "")` only defaults a *missing* key. An agent that answered
`"failure_class": null` gets `null` written to the persisted diagnosis, which `/fix` later
reads back. Same for `target_files`, which then is not a list.

Test: `test_persist_keeps_an_explicit_none_rather_than_the_default`.

### 122. `_persist_diagnosis` swallows every failure, losing the diagnosis

A non-string `reason` (`reason[:800]` raises), an unserialisable `target_files`, or a
`task_id` that is not a usable filename (`"A/B"` → a path whose parent does not exist) all
land in `except Exception` and only print. The diagnosis is lost, nothing is journalled,
and `/fix <task_id>` will report no diagnosis rather than an error. The `task_id` is
interpolated straight into a path with no validation.

Tests: `test_persist_swallows_an_unserialisable_diagnosis_and_writes_nothing`,
`test_persist_swallows_a_non_string_reason`,
`test_persist_swallows_a_task_id_that_is_not_a_usable_filename`.

### 123. `attempt_self_fix` journals `class=None` while the request file says `""`

The request JSON uses `diag.get("failure_class", "")`, but the journal line and the Telegram
message use `diag.get('failure_class')` with no default. A diagnosis with no class produces
the operator-facing text `class=None` and a request file with an empty class — two different
answers to the same question, in the same function.

Test: `test_attempt_self_fix_journals_the_class_as_none_when_absent`.

### 124. A failed self-fix spawn leaves an orphan request and no record

The request file is written *before* `subprocess.run(tmux …, check=True)`. If the spawn
fails, the `except` only prints `[self-fix] spawn failed: …`: the request file stays on
disk forever, nothing is journalled, and no Telegram message is sent. The operator sees
nothing at all, and the orphan is never retried or cleaned up.

Test: `test_attempt_self_fix_leaves_the_request_behind_when_the_spawn_fails`.

### 125. `apply_ready_self_fixes` ignores the `ORCH_SELF_FIX` kill switch

Only `ORCH_SELF_FIX_MERGE` is consulted in the apply loop. Turning the master kill switch
off stops new self-fixes from being *prepared*, but a branch the runner already gate-passed
is still merged onto main on the next poll.

Test: `test_apply_self_fixes_ignores_the_master_kill_switch`.

### 126. The runner worktree path is derived by chopping eight characters

`fid = branch[len("selffix-"):]` never checks the prefix. A branch not named `selffix-…`
(hand-created, or renamed) yields a worktree path built from the branch minus its first
eight characters, so `git worktree remove` targets a path that does not exist and the real
stale worktree is left behind. The failure is invisible: the cleanup runs with
`capture_output=True` and its exit code is never inspected.

Test: `test_apply_self_fixes_derives_the_worktree_by_chopping_eight_characters`.

### 127. A parked self-fix is renamed with `with_suffix`, so it is never retried

`ready.with_suffix(".notified")` turns `T1.ready.json` into `T1.ready.notified` (only the
last suffix is replaced). That is intentional-looking, but nothing ever reads `*.notified`
again: if Dan does not merge by hand, the gate-passed branch and its worktree stay around
indefinitely with no further reminder.

Test: `test_apply_self_fixes_renames_only_the_last_suffix_when_parking_a_fix`.

### 128. `apply_ready_redo` never notifies on success

The self-fix twin sends `✅ Maestro self-fix applied: …`. The `/redo` loop journals
`redo_applied` and regenerates the dep map, but sends no Telegram message — a landed
deliverable rewrite is invisible unless the detached runner already spoke. The failure path
*does* notify, so the asymmetry is silent-on-success only.

Test: `test_apply_redo_never_notifies_on_success`.

### 129. `apply_ready_redo` has no kill switch and no ask-first mode

There is no `ORCH_*` flag at all on the `/redo` apply path. A gate-passed `/redo` branch is
always auto-merged onto main, with no equivalent of `ORCH_SELF_FIX_MERGE=0`.

Test: `test_apply_redo_has_no_kill_switch_and_no_ask_first_mode`.

### 130. `_redo_path_ok` drops sidecars by basename anywhere in the tree

`_REDO_GATE_IGNORE` is matched against `Path(f).name`, so a file called `redo_result.json`
*anywhere* — including outside every allowed prefix — is removed from the change set before
the gate runs. A branch whose only change is such a file reports "no files changed" rather
than "out of scope".

Test: `test_redo_path_gate_ignores_the_runners_own_sidecars_by_basename`.

### 131. `JUDGE_MODEL` cannot be changed at runtime

`def _judge_complete(system, user, model: str = JUDGE_MODEL, …)` (reference L702) binds the
default at *definition* time. The comment directly above `JUDGE_MODEL` (reference L699)
invites exactly the opposite — "Restore Fable by setting `JUDGE_MODEL = "claude-fable-5"`" —
but rebinding the global afterwards (or monkeypatching it) has no effect on any call that
does not pass `model=` explicitly. Every caller in the reference omits it.

Test: `test_judge_defaults_to_the_module_judge_model`.

### 132. `attempt_self_fix` interpolates the task id into a `shell=True` command line

Reference L2078–L2081: the tmux window name and the single-quoted inner command are built
by f-string interpolation of `task_id`, `REPO`, `VENV_PYTHON` and the request path, then run
with `shell=True, check=True`. A task id (or a repo path) containing a space, a single quote
or a shell metacharacter either breaks the spawn or injects into it. Task ids come from
`docs/ROADMAP.md`, so this is not an untrusted input today, but nothing validates it and the
same pattern is repeated for the `/redo` runner (L3064).

Pinned indirectly by `test_attempt_self_fix_spawns_a_detached_tmux_window_through_a_shell`,
which asserts the string is assembled and passed through `shell=True`.

### 133. A self-fix or `/redo` that fails the merge gate deletes its own sidecar

Reference L2156–L2157: the operator is told "Left for manual review", and then
`ready.unlink(missing_ok=True)` runs unconditionally at the end of the loop body, so the
`*.ready.json` describing the branch is destroyed. Nothing retries the merge on the next
poll — the only surviving record is the Telegram message and the `self_fix_merge_failed`
journal line. `apply_ready_redo` does the same at L2239.

Tests: `test_apply_self_fixes_journals_a_failed_merge_and_cleans_nothing_up`,
`test_apply_redo_journals_and_notifies_a_failed_merge` (both assert the sidecar is gone).

### 134. A future-dated journal entry rate-limits a class until it ages out

Reference L2040: the loop breaker accepts a match when `ts >= cutoff`, so an entry stamped
*ahead* of the current clock is treated as recent. Combined with entry 116 (the offset is
chopped and the remainder read as UTC), a journal line written in a zone ahead of UTC — or
after a clock correction — blocks self-fixes for that class for up to `SELF_FIX_MIN_HOURS`
from a time that has not happened yet.

Test: `test_rate_gate_window_boundary_is_greater_than_or_equal`.

## M1 — parking: waiting_on_dan, manual actions, handle_incomplete (`parking.py`)

### 135. `park_manual_action` stores a silently truncated action, and the truncation is load-bearing

The state entry gets `"summary": action[:200]` while the Telegram ping interpolates the
whole `action` and `prep_actions.set_action` records the whole `action`. Three different
lengths for the same instruction, with no ellipsis to mark the cut, so a 400-character
action reads as a complete 200-character one in anything that renders `summary`.

That truncation is not merely cosmetic: `/ask` and the manual-action finalizer both do
`action = pa.get("action") or entry.get("summary", "")` (reference lines 2988 and 3050),
so whenever the `prep_actions` sidecar is missing or empty the *truncated* summary becomes
the operative action text handed onward — the tail of Dan's instruction is gone.

`park_for_dan` truncates the stored summary and the ping to the same 200 characters, so
the asymmetry is specific to `park_manual_action`.

Test: `test_park_manual_action_truncates_the_stored_summary_but_not_the_ping`.

### 136. `park_failed` reuses Dan's prior answer only to stay silent, never to act on it

The idempotence guard reads `prior = _answer_choice(req_id)` and, when Dan has already
answered an identical escalation, journals `failed_escalation_reused` and returns. Whatever
Dan chose — "Retry with new approach", "Shelve task", "Manual intervention" — is never read
again: the task simply stays in `parked_tasks`. Choosing "Retry with new approach" is
therefore indistinguishable from never replying, except that the second failure is quieter.
The same early return also suppresses the diagnosis/self-fix pass, so a task that keeps
failing the same way is never re-diagnosed.

Test: `test_park_failed_reuse_is_silent_on_telegram`.

### 137. `park_failed`'s journal filter is a bare substring test

`journal_events = [ln.strip() for ln in fh if task_id in ln]` matches any line *containing*
the id, so diagnosing `T1` feeds the judge every `T10`, `T12`, `PT1` … line as well. The
context handed to the diagnosis pass is contaminated by unrelated tasks whenever one task id
is a prefix of another.

Test: `test_park_failed_journal_tail_leaks_lines_of_prefix_sibling_tasks`.

### 138. `park_failed` promises the self-fix, then swallows its failure into a `print`

On the auto-eligible path Dan is told "→ Auto-eligible — implementing the fix for `T1` now."
*before* `attempt_self_fix` runs, and the whole diagnosis block sits inside
`except Exception as exc: print(f"  [judge-diagnose] skipped: {exc}")`. If the self-fix
raises, the only trace is a line on the orchestrator's stdout; Dan's last word on the subject
is the promise. No journal event, no follow-up ping, no `/fix` offer.

Test: `test_park_failed_swallows_an_exception_from_the_self_fix_runner`.

### 139. `park_regression` parks nothing and cannot be answered

Despite the name it touches neither `parked_tasks` nor `waiting_on_dan` — it raises a
`_danreq` and appends `regression_escalated`, and that is all. The `_danreq` is raised
*without* a `req_id`, so the answer file has a content-derived id that nothing correlates back
to the task: whichever of the three options Dan taps ("Revert and shelve", "Revert and retry",
"Keep despite regression"), no code path ever reads it. Contrast `park_failed`, which passes
an explicit `req_id`.

Tests: `test_park_regression_does_not_park_anything`,
`test_park_regression_shells_out_to_the_request_sender`.

### 140. `park_for_dan` burns the dan_id before it can tell Dan anything

The order is: bump `dan_id_counter`, `write_state(state)`, then
`(workspace / "PAUSED").write_text(str(dan_id))`, then `notify_telegram`. The workspace
directory is not created here, so a missing workspace raises `FileNotFoundError` *after* the
counter and the `waiting_on_dan` entry are already persisted. The id is consumed, an
unannounced entry is left in the state document, and Dan is never told the task is waiting on
him. `park_manual_action` has the same write-first ordering.

Test: `test_park_for_dan_without_a_workspace_dies_after_mutating_state`.

### 141. `_graduate_manual_action` marks a task complete on an unknown dan_id

`waiting.pop(dan_id_str, None)` is unguarded, so a stale or wrong id falls straight through to
`mark_roadmap_complete(task_id)`, `git push origin main`, the `task_complete` journal event
and the "verified and graduated" ping. Nothing checks that the id named a real parked entry,
and nothing checks that the entry it named was the one for `task_id`.

Test: `test_graduate_manual_action_graduates_an_unknown_dan_id_anyway`.

### 142. The awaiting-verification transitions lose an unknown id in total silence

Both `_park_awaiting_verification` and `_surface_await_failure` do
`info = waiting.get(dan_id_str)` / `if not info: return`. A vanished entry produces no journal
record and no ping, so a transition that should have been observable — Dan's action succeeded
but the awaited job hasn't landed, or the awaited row landed and failed — disappears with no
trace anywhere.

Tests: `test_park_awaiting_verification_of_an_unknown_id_is_a_silent_no_op`,
`test_surface_await_failure_of_an_unknown_id_is_a_silent_no_op`.

### 143. A hung finalize command escapes `_finalize_manual_action` as an exception

The post-action command runs with `timeout=1800` and no `try`, so a hung command raises
`subprocess.TimeoutExpired` out of the function — 30 minutes after Dan was told
"✅ Finalizing T1 — running post-action + verification gate…". The non-zero-exit path is
handled carefully (journal `manual_action_postcmd_failed`, a ping, item stays parked); the
timeout path has none of that.

Test: `test_finalize_manual_action_does_not_catch_a_post_cmd_timeout`.

### 144. `handle_incomplete` counts productive sittings against the attempt cap

`attempts` is incremented on *every* sitting, including ones that made forward progress, and
the cap is checked before the progress branch: `if attempts >= MAX_RESUME_ATTEMPTS: _park(…)`.
A task that advances steadily for 12 sittings is therefore parked for Dan at the 13th with
"resume attempt cap reached", even though it never stalled once. The separate `stalls` counter
is the one that actually measures being stuck, and it is reset on progress — the attempt cap
duplicates it badly.

Test: `test_handle_incomplete_attempt_cap_counts_productive_sittings_too`.

## M1 — orchestrator loop: retries

### 145. `_do_retry(..., retry_counts, ...)` ignores its `retry_counts` argument entirely

(reference `orchestrator_run.py:3602`). The body never reads or writes the dict; it calls
`read_state()` itself and writes back only `in_flight`. So the retry allowance is
persisted *only* where a caller happens to do it by hand.

### 146. Retry-count increments are lost on restart for the re-brief paths

The timeout/FAILED path persists the bump explicitly (`orchestrator_run.py:4124-4128`,
"B11"), but the gate-failure re-brief (`:4202-4205`) and the Sonnet-proof-review
re-brief (`:4221-4223`) bump `retry_counts[task_id]` in the in-process dict only.
Because `_do_retry` re-reads state from disk, the bump is never written through, and
the in-memory dict dies with the process. After an orchestrator restart the same task
is eligible for another "first" retry through those paths — the `< 1` cap becomes
unbounded across restarts instead of one retry per task.

## M4 — setup: init/doctor acceptance testing

### 147. `main()` shells out to `<project>/.venv/bin/python3` with no existence check

`run_dep_map()`/`run_status()` (reached from inside `main()`'s loop body) invoke
`<REPO>/.venv/bin/python3` unconditionally, with no check that the path exists. On a
project that has never had a virtualenv created at exactly that path — which includes
brand-new projects and, notably, this reference project if its `.venv` were ever
relocated or rebuilt elsewhere — the call raises an uncaught `FileNotFoundError` and
crashes `main()` outright rather than falling back or reporting a clean error. Found
verbatim in the extracted `maestro/orchestrator.py` while building M4's `cli.py init`
acceptance test against a throwaway project with no `.venv/`; `cmd_init` works around it
by scaffolding `.venv/bin/python3` as a symlink to `sys.executable`, but the underlying
crash-on-missing-interpreter is reference behaviour, copied verbatim per the M1 rule, and
is not fixed here.

## M4a — prepared-action sidecar (`prep_actions.py`)

Found while extracting the reference `scripts/prepared_actions.py` into
`maestro/prep_actions.py` and characterising it in
`tests/characterization/test_prep_actions.py`. All three are copied verbatim per the M1
rule and pinned, not fixed.

### 148. A corrupt sidecar is silently emptied by the next `set_action`

`load_actions` catches `FileNotFoundError`, `json.JSONDecodeError` and `OSError` and
returns `{}` for all of them, so a missing file, a truncated file and a directory at that
path are indistinguishable — the same shape as bug #1 in the state layer. The consequence
here is worse than an ambiguous read, because `set_action` is a read-modify-write on top
of it: it calls `load_actions`, gets `{}` from the corrupt file, adds the one new entry
and atomically replaces the file with it. Every other task's prepared action is discarded,
with no error, no journal line and no operator message — the sidecar simply comes back
one-entry-deep and the loop carries on. A half-written file (the process dying between
`mkstemp` and `os.replace` leaves the *old* file intact, but an out-of-space write does
not) is enough to trigger it.

Pinned by `test_load_actions_corrupt_json_returns_empty_dict`,
`test_load_actions_directory_returns_empty_dict`,
`test_set_action_silently_discards_a_corrupt_store`.

### 149. "Never raises" holds only while the JSON happens to be an object

The module docstring promises callers "never raises on missing or corrupt files — callers
can always assume a dict is returned", and `load_actions` is annotated `-> dict[str, dict]`.
But well-formed JSON that is not an object parses fine and is returned unchanged, so a file
containing `["T1"]` comes back as a list. `has_action` then silently mis-answers (`in` on a
list tests the *values*, so it can return `True` for a task id that has no entry, and
`False` for one that does), and `get_action` — one line, `load_actions(path).get(task_id)` —
raises `AttributeError: 'list' object has no attribute 'get'` straight through the guarantee
the docstring makes. Same class as bug #2 in the state layer's `read_json`, but here it is
contradicted by an explicit written contract.

Pinned by `test_load_actions_returns_non_dict_json_unchanged`,
`test_get_action_raises_attribute_error_on_a_json_list_file`.

### 150. `prepared_at` is a second, incompatible timestamp format

Every other timestamp the orchestrator writes goes through `now_iso()`:
`"%Y-%m-%dT%H:%M:%SZ"`, UTC, second resolution, `Z` suffix. `set_action` instead stores
`datetime.now(timezone.utc).isoformat()`, which renders as
`2026-08-16T12:34:56.789012+00:00` — microsecond resolution and a numeric offset. So a
single `.orchestrator/` directory holds two ISO-8601 dialects, and anything that compares a
`prepared_at` to a `state.json` timestamp as a string (the obvious thing to do, given both
are ISO-8601 and lexicographic order is normally date order) gets the wrong answer:
`"…56.789012+00:00" < "…56Z"` is true because `.` sorts below `Z`.

Pinned by `test_set_action_prepared_at_is_offset_isoformat_not_the_zulu_stamp`.
