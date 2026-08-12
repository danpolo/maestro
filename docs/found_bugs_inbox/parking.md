# Found bugs — parking (waiting_on_dan, manual actions, handle_incomplete)

Behavioural surprises recorded while writing `tests/characterization/test_parking.py`.
Every item below is **pinned by a passing test against the reference**, not repaired.

---

## PK-1 — `park_manual_action` stores a silently truncated action, and the truncation is load-bearing

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

---

The items below were recorded while extracting `maestro/parking.py`. Each was read out of
the reference body and is pinned by a passing characterisation test against *both* subjects;
none is repaired — the extracted bodies are byte-identical to the reference.

## PK-2 — `park_failed` reuses Dan's prior answer only to stay silent, never to act on it

The idempotence guard reads `prior = _answer_choice(req_id)` and, when Dan has already
answered an identical escalation, journals `failed_escalation_reused` and returns. Whatever
Dan chose — "Retry with new approach", "Shelve task", "Manual intervention" — is never read
again: the task simply stays in `parked_tasks`. Choosing "Retry with new approach" is
therefore indistinguishable from never replying, except that the second failure is quieter.
The same early return also suppresses the diagnosis/self-fix pass, so a task that keeps
failing the same way is never re-diagnosed.

Test: `test_park_failed_reuse_is_silent_on_telegram`.

## PK-3 — `park_failed`'s journal filter is a bare substring test

`journal_events = [ln.strip() for ln in fh if task_id in ln]` matches any line *containing*
the id, so diagnosing `T1` feeds the judge every `T10`, `T12`, `PT1` … line as well. The
context handed to the diagnosis pass is contaminated by unrelated tasks whenever one task id
is a prefix of another.

Test: `test_park_failed_journal_tail_leaks_lines_of_prefix_sibling_tasks`.

## PK-4 — `park_failed` promises the self-fix, then swallows its failure into a `print`

On the auto-eligible path Dan is told "→ Auto-eligible — implementing the fix for `T1` now."
*before* `attempt_self_fix` runs, and the whole diagnosis block sits inside
`except Exception as exc: print(f"  [judge-diagnose] skipped: {exc}")`. If the self-fix
raises, the only trace is a line on the orchestrator's stdout; Dan's last word on the subject
is the promise. No journal event, no follow-up ping, no `/fix` offer.

Test: `test_park_failed_swallows_an_exception_from_the_self_fix_runner`.

## PK-5 — `park_regression` parks nothing and cannot be answered

Despite the name it touches neither `parked_tasks` nor `waiting_on_dan` — it raises a
`_danreq` and appends `regression_escalated`, and that is all. The `_danreq` is raised
*without* a `req_id`, so the answer file has a content-derived id that nothing correlates back
to the task: whichever of the three options Dan taps ("Revert and shelve", "Revert and retry",
"Keep despite regression"), no code path ever reads it. Contrast `park_failed`, which passes
an explicit `req_id`.

Tests: `test_park_regression_does_not_park_anything`,
`test_park_regression_shells_out_to_the_request_sender`.

## PK-6 — `park_for_dan` burns the dan_id before it can tell Dan anything

The order is: bump `dan_id_counter`, `write_state(state)`, then
`(workspace / "PAUSED").write_text(str(dan_id))`, then `notify_telegram`. The workspace
directory is not created here, so a missing workspace raises `FileNotFoundError` *after* the
counter and the `waiting_on_dan` entry are already persisted. The id is consumed, an
unannounced entry is left in the state document, and Dan is never told the task is waiting on
him. `park_manual_action` has the same write-first ordering.

Test: `test_park_for_dan_without_a_workspace_dies_after_mutating_state`.

## PK-7 — `_graduate_manual_action` marks a task complete on an unknown dan_id

`waiting.pop(dan_id_str, None)` is unguarded, so a stale or wrong id falls straight through to
`mark_roadmap_complete(task_id)`, `git push origin main`, the `task_complete` journal event
and the "verified and graduated" ping. Nothing checks that the id named a real parked entry,
and nothing checks that the entry it named was the one for `task_id`.

Test: `test_graduate_manual_action_graduates_an_unknown_dan_id_anyway`.

## PK-8 — the awaiting-verification transitions lose an unknown id in total silence

Both `_park_awaiting_verification` and `_surface_await_failure` do
`info = waiting.get(dan_id_str)` / `if not info: return`. A vanished entry produces no journal
record and no ping, so a transition that should have been observable — Dan's action succeeded
but the awaited job hasn't landed, or the awaited row landed and failed — disappears with no
trace anywhere.

Tests: `test_park_awaiting_verification_of_an_unknown_id_is_a_silent_no_op`,
`test_surface_await_failure_of_an_unknown_id_is_a_silent_no_op`.

## PK-9 — a hung finalize command escapes `_finalize_manual_action` as an exception

The post-action command runs with `timeout=1800` and no `try`, so a hung command raises
`subprocess.TimeoutExpired` out of the function — 30 minutes after Dan was told
"✅ Finalizing T1 — running post-action + verification gate…". The non-zero-exit path is
handled carefully (journal `manual_action_postcmd_failed`, a ping, item stays parked); the
timeout path has none of that.

Test: `test_finalize_manual_action_does_not_catch_a_post_cmd_timeout`.

## PK-10 — `handle_incomplete` counts productive sittings against the attempt cap

`attempts` is incremented on *every* sitting, including ones that made forward progress, and
the cap is checked before the progress branch: `if attempts >= MAX_RESUME_ATTEMPTS: _park(…)`.
A task that advances steadily for 12 sittings is therefore parked for Dan at the 13th with
"resume attempt cap reached", even though it never stalled once. The separate `stalls` counter
is the one that actually measures being stuck, and it is reset on progress — the attempt cap
duplicates it badly.

Test: `test_handle_incomplete_attempt_cap_counts_productive_sittings_too`.
