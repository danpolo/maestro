# Found bugs — self-heal (diagnose / selffix / redo)

Behavioural surprises recorded while writing `tests/characterization/test_selfheal.py`.
Every item below is **pinned by a passing test against the reference**, not repaired.

---

## SH-1 — `_self_fix_path_ok` / `_redo_path_ok`: `lstrip("./")` lets a parent traversal through the allowlist

`files = [str(f).strip().lstrip("./") for f in ...]` strips a *character set*, not a prefix.
`"../scripts/x.py"` normalises to `"scripts/x.py"` and passes the allowlist; so do
`"../../../scripts/x.py"` and `"/scripts/x.py"`. The gate that is supposed to confine an
unattended self-fix to the orchestrator's own surface therefore cannot see that a target
points outside the repo at all. Both gates share the bug.

Severity: this is the primary safety gate for unattended code edits.

Tests: `test_self_fix_path_gate_lets_a_parent_traversal_in`,
`test_redo_path_gate_lets_a_parent_traversal_in`.

## SH-2 — Both path gates iterate a bare string character by character

`target_files` arriving as a string (an agent that answered `"target_files": "scripts/x.py"`
instead of a list) is not detected. The comprehension iterates the characters, so the gate
decides on the first letter: `_self_fix_path_ok("scripts/x.py")` returns
`(False, "out-of-scope path: s")`. Fails closed here, but the reason is nonsense, and the
same shape would fail *open* if the first characters happened to spell an allowed prefix.

Tests: `test_self_fix_path_gate_iterates_a_bare_string_character_by_character`,
`test_redo_path_gate_iterates_a_bare_string_character_by_character`.

## SH-3 — The deny lists are substring tests

`any(d in fn for d in SELF_FIX_DENY)` matches anywhere in the path, so
`docs/notes-about-main_bot.py.md` (a doc *about* the bot) is vetoed, and
`scripts/requirements_helper.py` is vetoed by the `"requirements"` fragment. A denied
fragment vetoes the whole change set, so one such false positive turns an auto-fix into an
advisory. Same in `_redo_path_ok`.

Test: `test_self_fix_path_gate_matches_a_denied_fragment_anywhere_in_the_path`.

## SH-4 — `_redo_path_ok(None)` raises; its self-fix twin does not

`_self_fix_path_ok` guards with `for f in (files or [])`; `_redo_path_ok` iterates `files`
directly, so a `None` change set raises `TypeError` out of the gate instead of returning
`(False, …)`. The two functions are otherwise the same shape.

Test: `test_redo_path_gate_crashes_on_none`.

## SH-5 — `_self_fix_rate_ok` matches the journal as raw text

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

## SH-6 — `_self_fix_rate_ok` fails OPEN on an unreadable journal

The whole read is wrapped in `try: … except Exception: pass` followed by `return True`. If
the journal cannot be opened (permissions, or the path being a directory), the per-class
rate limiter silently disappears and every self-fix is allowed. The only signal is that
nothing is logged.

Test: `test_rate_gate_allows_when_the_journal_cannot_be_opened`.

## SH-7 — `_self_fix_rate_ok` reads timestamps as UTC no matter what they say

`datetime.strptime(json.loads(ln).get("ts","")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=utc)`
truncates to 19 characters, which discards any offset (`+05:00`) and then *asserts* UTC. A
journal written by a differently-configured writer is mis-dated by the offset, which can
put a recent fix outside the window (or a stale one inside it). Any unparseable `ts` — or a
record with no `ts` — is skipped entirely, so that record can never rate-limit anything.

Tests: `test_rate_gate_reads_only_the_first_nineteen_characters_of_the_timestamp`,
`test_rate_gate_ignores_a_record_with_no_usable_timestamp`.

## SH-8 — `_self_fix_eligible` crashes on a non-string `failure_class`

`cls = (diag.get("failure_class") or "").strip()` only defends against a *falsy* value. A
diagnosis whose JSON gave a number or a list for `failure_class` raises `AttributeError`
out of the gate — an agent-controlled input crashing the caller.

Test: `test_eligibility_crashes_on_a_non_string_class`.

## SH-9 — `_diagnose_failure` / `_failure_looks_normal`: greedy `\{.*\}` loses both objects

`re.search(r"\{.*\}", raw, re.DOTALL)` spans first-brace to last-brace. An agent that emits
its answer twice (or emits one object plus an example) produces a span that is not valid
JSON, so the pass returns `{}`. For `_failure_looks_normal` that empty result is the
fail-open value — the skeptic veto is silently skipped and the auto-fix proceeds.

Test: `test_judgment_pass_loses_both_objects_when_the_agent_prints_two`.

## SH-10 — Neither judgment pass validates the shape of what it parsed

`{"totally": "unrelated"}` is returned verbatim; the callers' key expectations
(`failure_class`, `verdict`, `confidence`) are never enforced at the parse boundary.

Test: `test_judgment_pass_returns_whatever_keys_the_agent_invented`.

## SH-11 — `_judge_complete` reports a successful-but-silent agent as `rc=0`

`if r.returncode == 0 and out` — an exit-0 call with blank stdout falls through to the
diagnostic `print(… rc=0 stderr=…)` and returns `""`. The operator-facing line looks like a
success being logged as a failure, and the caller cannot distinguish "agent said nothing"
from "agent crashed".

Test: `test_judge_returns_empty_on_blank_stdout_at_exit_zero`.

## SH-12 — `_persist_diagnosis` keeps an explicit `null`

`diag.get("failure_class", "")` only defaults a *missing* key. An agent that answered
`"failure_class": null` gets `null` written to the persisted diagnosis, which `/fix` later
reads back. Same for `target_files`, which then is not a list.

Test: `test_persist_keeps_an_explicit_none_rather_than_the_default`.

## SH-13 — `_persist_diagnosis` swallows every failure, losing the diagnosis

A non-string `reason` (`reason[:800]` raises), an unserialisable `target_files`, or a
`task_id` that is not a usable filename (`"A/B"` → a path whose parent does not exist) all
land in `except Exception` and only print. The diagnosis is lost, nothing is journalled,
and `/fix <task_id>` will report no diagnosis rather than an error. The `task_id` is
interpolated straight into a path with no validation.

Tests: `test_persist_swallows_an_unserialisable_diagnosis_and_writes_nothing`,
`test_persist_swallows_a_non_string_reason`,
`test_persist_swallows_a_task_id_that_is_not_a_usable_filename`.

## SH-14 — `attempt_self_fix` journals `class=None` while the request file says `""`

The request JSON uses `diag.get("failure_class", "")`, but the journal line and the Telegram
message use `diag.get('failure_class')` with no default. A diagnosis with no class produces
the operator-facing text `class=None` and a request file with an empty class — two different
answers to the same question, in the same function.

Test: `test_attempt_self_fix_journals_the_class_as_none_when_absent`.

## SH-15 — A failed self-fix spawn leaves an orphan request and no record

The request file is written *before* `subprocess.run(tmux …, check=True)`. If the spawn
fails, the `except` only prints `[self-fix] spawn failed: …`: the request file stays on
disk forever, nothing is journalled, and no Telegram message is sent. The operator sees
nothing at all, and the orphan is never retried or cleaned up.

Test: `test_attempt_self_fix_leaves_the_request_behind_when_the_spawn_fails`.

## SH-16 — `apply_ready_self_fixes` ignores the `ORCH_SELF_FIX` kill switch

Only `ORCH_SELF_FIX_MERGE` is consulted in the apply loop. Turning the master kill switch
off stops new self-fixes from being *prepared*, but a branch the runner already gate-passed
is still merged onto main on the next poll.

Test: `test_apply_self_fixes_ignores_the_master_kill_switch`.

## SH-17 — The runner worktree path is derived by chopping eight characters

`fid = branch[len("selffix-"):]` never checks the prefix. A branch not named `selffix-…`
(hand-created, or renamed) yields a worktree path built from the branch minus its first
eight characters, so `git worktree remove` targets a path that does not exist and the real
stale worktree is left behind. The failure is invisible: the cleanup runs with
`capture_output=True` and its exit code is never inspected.

Test: `test_apply_self_fixes_derives_the_worktree_by_chopping_eight_characters`.

## SH-18 — A parked self-fix is renamed with `with_suffix`, so it is never retried

`ready.with_suffix(".notified")` turns `T1.ready.json` into `T1.ready.notified` (only the
last suffix is replaced). That is intentional-looking, but nothing ever reads `*.notified`
again: if Dan does not merge by hand, the gate-passed branch and its worktree stay around
indefinitely with no further reminder.

Test: `test_apply_self_fixes_renames_only_the_last_suffix_when_parking_a_fix`.

## SH-19 — `apply_ready_redo` never notifies on success

The self-fix twin sends `✅ Maestro self-fix applied: …`. The `/redo` loop journals
`redo_applied` and regenerates the dep map, but sends no Telegram message — a landed
deliverable rewrite is invisible unless the detached runner already spoke. The failure path
*does* notify, so the asymmetry is silent-on-success only.

Test: `test_apply_redo_never_notifies_on_success`.

## SH-20 — `apply_ready_redo` has no kill switch and no ask-first mode

There is no `ORCH_*` flag at all on the `/redo` apply path. A gate-passed `/redo` branch is
always auto-merged onto main, with no equivalent of `ORCH_SELF_FIX_MERGE=0`.

Test: `test_apply_redo_has_no_kill_switch_and_no_ask_first_mode`.

## SH-21 — `_redo_path_ok` drops sidecars by basename anywhere in the tree

`_REDO_GATE_IGNORE` is matched against `Path(f).name`, so a file called `redo_result.json`
*anywhere* — including outside every allowed prefix — is removed from the change set before
the gate runs. A branch whose only change is such a file reports "no files changed" rather
than "out of scope".

Test: `test_redo_path_gate_ignores_the_runners_own_sidecars_by_basename`.

## SH-22 — `JUDGE_MODEL` cannot be changed at runtime

`def _judge_complete(system, user, model: str = JUDGE_MODEL, …)` (reference L702) binds the
default at *definition* time. The comment directly above `JUDGE_MODEL` (reference L699)
invites exactly the opposite — "Restore Fable by setting `JUDGE_MODEL = "claude-fable-5"`" —
but rebinding the global afterwards (or monkeypatching it) has no effect on any call that
does not pass `model=` explicitly. Every caller in the reference omits it.

Test: `test_judge_defaults_to_the_module_judge_model`.

## SH-23 — `attempt_self_fix` interpolates the task id into a `shell=True` command line

Reference L2078–L2081: the tmux window name and the single-quoted inner command are built
by f-string interpolation of `task_id`, `REPO`, `VENV_PYTHON` and the request path, then run
with `shell=True, check=True`. A task id (or a repo path) containing a space, a single quote
or a shell metacharacter either breaks the spawn or injects into it. Task ids come from
`docs/ROADMAP.md`, so this is not an untrusted input today, but nothing validates it and the
same pattern is repeated for the `/redo` runner (L3064).

Pinned indirectly by `test_attempt_self_fix_spawns_a_detached_tmux_window_through_a_shell`,
which asserts the string is assembled and passed through `shell=True`.

## SH-24 — A self-fix or `/redo` that fails the merge gate deletes its own sidecar

Reference L2156–L2157: the operator is told "Left for manual review", and then
`ready.unlink(missing_ok=True)` runs unconditionally at the end of the loop body, so the
`*.ready.json` describing the branch is destroyed. Nothing retries the merge on the next
poll — the only surviving record is the Telegram message and the `self_fix_merge_failed`
journal line. `apply_ready_redo` does the same at L2239.

Tests: `test_apply_self_fixes_journals_a_failed_merge_and_cleans_nothing_up`,
`test_apply_redo_journals_and_notifies_a_failed_merge` (both assert the sidecar is gone).

## SH-25 — A future-dated journal entry rate-limits a class until it ages out

Reference L2040: the loop breaker accepts a match when `ts >= cutoff`, so an entry stamped
*ahead* of the current clock is treated as recent. Combined with SH-7 (the offset is chopped
and the remainder read as UTC), a journal line written in a zone ahead of UTC — or after a
clock correction — blocks self-fixes for that class for up to `SELF_FIX_MIN_HOURS` from a
time that has not happened yet.

Test: `test_rate_gate_window_boundary_is_greater_than_or_equal`.
