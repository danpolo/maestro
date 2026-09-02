<!-- Provenance note, added 2026-09-02 by the pre-integration queue's final
     whole-branch review (Important 5). -->

> **Provenance.** This is the working report for item C7 — the limits tables as the single source of truth, and `doctor`'s model-id probe, written during the
> 2026-08-30 pre-integration queue and copied verbatim into `docs/plans/` when that
> queue's scratch directory (`/home/dan/.maestro-sdd/2026-08-30-pre-integration/`)
> was deleted on completion.
>
> `maestro/limits.py` (`model_slug`'s R8 note), `maestro/cli.py` and `tests/test_cli.py`
> cite this file for the hand-verified, offline evidence behind `_probe_model_id`'s
> asymmetric interpretation: both real CLIs print a client-side rejection marker for an
> unknown model *before* any network call, while a valid model prints nothing
> distinguishing. That observation cannot be re-derived from the tree — it is the record
> of running the two binaries by hand — so it lives here rather than in a directory that
> was deleted when the queue closed.
>
> Paths and sibling-file references inside the report below are **historical** and
> name files that no longer exist. They are left unedited because the report is
> evidence, not documentation: rewriting it would defeat the point of keeping it.

---

# Task C7 report — the tables are the single source of truth + doctor model-id probe

Worktree: `/home/dan/.maestro-sdd/wt-C7`, branch `sdd/C7`, base `b398c3f`.
Verified early: `python -c "import maestro; print(maestro.__file__)"` printed
`/home/dan/.maestro-sdd/wt-C7/maestro/__init__.py` — testing this worktree's own source.

Three commits, one per part, all green before moving to the next:

1. `c829f5b` — fix(sdd/C7 R8): fold hyphen/period as one separator and strip date suffixes
   in the model-key normaliser
2. `2b7da8a` — feat(sdd/C7 part 1): derive DEFAULT_MODELS and _SHIPPED_DEFAULTS from three
   shared constants
3. `b8940dd` — feat(sdd/C7 part 2): doctor probes every configured model id against its own
   CLI

Order taken: **Part 3 (R8) → Part 1 → Part 2**, per the brief's own note that R8 is
load-bearing for A4's rotation and for Part 1/2's own tests to exercise the real Haiku
row meaningfully.

---

## Part 3 (R8) — fix the normaliser

**Defect confirmed exactly as reported.** Before the fix:

```
limits.resolve('Claude Haiku 4.5')        -> resolves (already worked)
limits.resolve('claude-haiku-4.5')        -> resolves (already worked)
limits.resolve('claude-haiku-4-5')        -> None      (BROKEN)
limits.resolve('claude-haiku-4-5-20251001') -> None    (BROKEN — the actual configured id)
```

**Fix.** `_normalise_model_key` (`maestro/limits.py`) now:
1. Casefolds, then collapses any run of whitespace, `.` or `-` to a single `-`
   (`_SEPARATOR_RUN_RE = re.compile(r"[\s.-]+")`) — periods and hyphens must fold to the
   *same* character, not just to each other, because `project.yaml`'s own
   `gpt-5.6-terra` slug already mixes both spellings in one string (DESIGN.md §5).
2. Strips a trailing 8-digit release-date suffix (`_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")`),
   applied after step 1 so `claude-haiku-4-5-20251001` and `claude-haiku-4-5` fold to the
   identical `claude-haiku-4-5`, which is also what `"Claude Haiku 4.5"` folds to.

**TDD evidence.**
- RED: `test_normalise_model_key_strips_a_trailing_release_date_suffix` and
  `test_resolve_finds_the_actual_configured_haiku_id_against_the_real_claude_table` failed
  pre-fix —
  ```
  AssertionError: assert 'claude-haiku-4-5-20251001' == 'claude-haiku-4.5'
  AssertionError: 'claude-haiku-4-5' did not resolve
  ```
- GREEN after the fix; `tests/test_limits.py` 37/37 (then 39, then 41 after later parts).

**Deliberate re-baseline (called out, not silent).**
`test_normalise_model_key_folds_display_names_to_project_yaml_slugs` used to assert
`_normalise_model_key("GPT-5.6 Luna") == "gpt-5.6-luna"` (period kept). Because `.` now
folds the same as `-`, the literal folded form is now `gpt-5-6-luna`. **This changes no
resolution outcome** — `project.yaml`'s own `gpt-5.6-terra` slug folds to the identical new
value on the *other* side of every real lookup, proven directly by the new
`test_gpt_5_6_slugs_still_resolve_against_the_real_codex_table_after_the_fold_change`
against the real `~/.codex/model_context_limits.md`. The test's docstring documents the
re-baseline and why.

**(a) Resolution of every configured model id, after the fix, against the real tables:**

```
'claude-sonnet-5'            -> Claude Sonnet 5
'claude-opus-5'              -> Claude Opus 5
'gpt-5.6-terra'               -> GPT-5.6 Terra
'gpt-5.6-sol'                 -> GPT-5.6 Sol
'claude-haiku-4-5-20251001'  -> Claude Haiku 4.5   (the actual configured id — now resolves)
'claude-haiku-4-5'           -> Claude Haiku 4.5
'claude-haiku-4.5'           -> Claude Haiku 4.5
'Claude Haiku 4.5'           -> Claude Haiku 4.5
'gpt-5.5' / 'GPT-5.5'         -> GPT-5.5
'claude-fable-5'             -> None (UserWarning raised)
'Claude Fable 5'             -> None (UserWarning raised)
'fable'                      -> None (UserWarning raised)
```

`claude-fable-5` (every spelling tried) is confirmed **absent** from both tables — it
resolves to `None` with a warning, exactly as it should per Dan's subscription-only rule.
Nothing in this change adds it, and nothing needed to: it was never in `_SHIPPED_DEFAULTS`
before or after, and the two real table files (edited by C6, outside this item) don't carry
it.

**(d) Bare display names — deliberate decision, and A4's slug path.**
`_normalise_model_key("Opus 5")` still folds to `"opus-5"`, **not** `"claude-opus-5"` — the
missing `"Claude "` prefix is a different defect (found by A4's review) that this fix
deliberately does **not** close. Reasoning: the two gaps R8 names (separator folding,
date-suffix stripping) are both *lossless* — they only unify spellings that already name
the *same* model unambiguously. Inventing a `"Claude "` prefix for an arbitrary bare name
is a *guess* — `"Opus 5"` could as easily be some other vendor's "Opus 5" once other
backends grow display names of their own, and it would also newly collide with a
same-named-but-different codex/gpt display name path this scan never had to consider
before. A4 already has the correct, narrower fix in production (key the ceiling lookup on
the *launch model slug*, not the runtime sample's display name), so there is no live path
that actually needs the guess. I did not touch that workaround — nothing in `switch.py` was
edited (not mine to touch this wave, and not needed). Confirmed by direct check:
`_normalise_model_key("Opus 5") == "opus-5"` still holds, and `tests/test_switch.py::test_the_samples_display_name_is_not_a_table_key`
(the pin asserting `"Opus 5"` does **not** resolve while `"claude-opus-5"` does) is
**unmodified** and still passes.

---

## Part 1 — derive shipped defaults from the tables

**Root duplication, confirmed by re-deriving the brief's own arithmetic.** Renaming the
implementer/judge's default model used to require: the table row (1), `roles.DEFAULT_MODELS`'s
two entries that both hardcoded `"claude-sonnet-5"` (2), and `limits._SHIPPED_DEFAULTS`'s key
(1) — four edits across three places (table, `roles.py`, `limits.py`).

**Fix.** `maestro/limits.py` now carries three constants — `DEFAULT_IMPLEMENTER_MODEL_NAME`,
`DEFAULT_JUDGE_MODEL_NAME`, `DEFAULT_DIAGNOSER_MODEL_NAME` — spelled exactly as the tables
spell their rows (`"Claude Sonnet 5"` / `"Claude Opus 5"`), plus a public `model_slug()`
(a thin wrapper on `_normalise_model_key` — pure string folding, no IO). `maestro/roles.py`'s
`DEFAULT_MODELS` now computes its slugs via `limits.model_slug(limits.DEFAULT_..._MODEL_NAME)`
instead of typing `"claude-sonnet-5"`/`"claude-opus-5"` a second time. Renaming one of the
three constants is now the *only* edit both `roles.DEFAULT_MODELS` and `_SHIPPED_DEFAULTS`
need.

**`roles.py` stays pure.** `model_slug` touches no file and starts no subprocess — it is
`"-".join`/regex string folding over a Python literal, computed once at import time. The
module docstring's "reads no file beyond the project configuration" claim is unchanged
in substance; I added a short paragraph explaining why the new import doesn't contradict it.
Confirmed by source scan: `test_roles_module_holds_no_model_id_string_literal_of_its_own`
walks `roles.py`'s AST and asserts no `"claude-...-N"`-shaped string literal remains.

**(b) What `_SHIPPED_DEFAULTS` shrank to, and why.**

Before (5 entries, a stale mirror of both real tables — the exact defect: it wasn't updated
when C6 added Haiku 4.5/GPT-5.5 rows, so it was *already* out of sync the moment I opened
the file):
```
"Claude Sonnet 5", "Claude Opus 5", "GPT-5.6 Luna", "GPT-5.6 Terra", "GPT-5.6 Sol"
```

After (2 entries — `DEFAULT_JUDGE_MODEL_NAME == DEFAULT_IMPLEMENTER_MODEL_NAME`, so the set
collapses to two, not three):
```python
>>> sorted(limits._SHIPPED_DEFAULTS)
['Claude Opus 5', 'Claude Sonnet 5']
>>> limits._SHIPPED_DEFAULTS["Claude Sonnet 5"]
ModelLimits(model='Claude Sonnet 5', prepare_handoff_low=90000, prepare_handoff_high=100000,
            normally_fresh_low=100000, normally_fresh_high=120000, exception_ceiling=150000)
```

**Why this residue is right.** `_SHIPPED_DEFAULTS`'s only documented job (its own comment,
both before and after) is "a machine with neither table file" — the minimal set that
satisfies that job is exactly the models `roles.DEFAULT_MODELS` can ever hand back with zero
`project.yaml` and neither table present. It does not need Haiku 4.5 or the GPT-5.6 family:
those are only ever reached by an *explicit* `project.yaml` `models:` entry, and if neither
table exists to confirm them, `resolve()` correctly returns `None` with a warning rather than
inventing a number for a model this fallback was never asked to cover. Shrinking it also
means C6-style table additions (a new row in either real file) never again require a
matching edit here — the exact defect this item exists to close.

**The numbers are also deliberately different, and this is a real (scoped) behaviour
change.** Instead of Sonnet's and Opus's own distinct historical fallback numbers, both
entries now use the tables' own documented **generic "unknown model"** figures — both real
files end with the same guidance: "Model unknown → 100K warn / 120K evaluate / 150K normal
max" (Claude side) / "warn at 100K, evaluate a reset at 120K, and normally stop by 150K"
(Codex side). Mapped: `prepare_handoff=(90K,100K)`, `normally_fresh=(100K,120K)`,
`exception_ceiling=150K`. This is a conscious choice: on a machine with **neither** table,
maestro cannot actually confirm any model's real ceiling (including Opus's, which used to
get a distinct, more generous number here) — claiming otherwise would be a stale, unverifiable
number frozen at authoring time, the same defect class this item removes. The tables'
own documented fallback is the more honest answer. **Scope of the change:** this only
affects the already-rare "neither table file is readable" degrade path
(`load_limits()`'s `if not models: models = dict(_SHIPPED_DEFAULTS)"`); it never fires when
either real table is present, which is the normal case on every machine that has run
`maestro init`.

**TDD evidence.**
- RED: `test_default_models_are_derived_from_limits_canonical_names_not_a_second_literal`,
  `test_roles_module_holds_no_model_id_string_literal_of_its_own` (roles.py side) and
  `test_shipped_defaults_covers_exactly_the_role_default_models_not_a_full_table_mirror`
  (limits.py side) all failed with `AttributeError: module 'maestro.limits' has no attribute
  'DEFAULT_IMPLEMENTER_MODEL_NAME'` / literal-scan assertion errors before the change.
- GREEN after. The **pre-existing** pin
  `test_default_models_reproduce_the_call_sites_they_replace` (asserting
  `roles.model_for(ROLE_IMPLEMENTER) == implementer._DEFAULT_IMPLEMENTER_MODEL` and
  `roles.model_for(ROLE_DIAGNOSER) == diagnose.JUDGE_MODEL`) is **untouched and still
  green** — the derived slugs are byte-identical to the old literals
  (`claude-sonnet-5`/`claude-opus-5`).

---

## Part 2 — the doctor model-id probe

**New check:** `_check_model_ids` in `maestro/cli.py`, added to `cmd_doctor`'s
non-pre-commit checklist right after `_check_model_limits` (same list `_check_backends`
lives in — it does **not** run under `--pre-commit`, so it adds no cost to the fast
commit-hook path). It collects every distinct `(backend, model)` pair out of
`project.yaml`'s `roles:` block and asks that backend's own CLI about it.

**How I derived the design (empirical, not assumed).** Before writing any code I manually
probed both real CLIs on this machine, online and under `unshare --net` (unprivileged
network-namespace isolation, no sudo):

- **Invalid model, online or offline:** both `claude -p --model <bogus> "hi"` and
  `codex exec --model <bogus> "hi"` print a **client-side** rejection marker within ~2-4s,
  *before* any network attempt — confirmed identical under `unshare --net` (no network at
  all). Claude's marker: `[claude-code:unrecognized_model]`. Codex's:
  `Model metadata for \`<id>\` not found`.
- **Valid model, offline:** *no* distinguishing output appears at all — the process just
  hangs (confirmed: 15s+ with zero output under `unshare --net`, would hang indefinitely
  with no external timeout). This is exactly the "doctor that hangs on a flaky network"
  the brief warned against, and is why the probe **must** be time-bounded and must **not**
  treat a timeout as proof of anything.

**Resulting contract** (`_probe_model_id`): the marker's *presence* is proof the model is
invalid; its *absence* — including a timeout — proves nothing and is never a failure.
Bounded by `MODEL_PROBE_TIMEOUT_SEC = 8.0` (comfortably above the ~2-4s observed marker
latency, comfortably below a real turn).

**(c) Injection — no test executes a real agent CLI.**
- `_run_model_probe(argv, *, timeout)` is the **one** function that calls
  `subprocess.run` for real. It always returns a string (kills on `TimeoutExpired`,
  decodes whatever partial output the process had already produced — Python's
  `TimeoutExpired.stdout/stderr` carry that even though `text=True` doesn't decode it on
  that path, handled explicitly); a missing binary or any other `OSError` degrades to
  `""`. It never raises.
- `_probe_model_id(backend, model, *, run=None)` and `_check_model_ids(repo_root, *,
  run=None)` both take the same `run` parameter, defaulting to `_run_model_probe`. Every
  test in `tests/test_cli.py`'s new `# ── doctor: model_ids ──` section passes a stub —
  this is the exact `_probe_version`-style seam the plan asked for.
- **Offline behaviour, tested directly:**
  `test_probe_model_id_is_offline_tolerant_a_timeout_is_not_proof_of_an_invalid_model`
  injects a `run` returning `""` (standing in for a timed-out, marker-less probe) and
  asserts `invalid is False` — a `doctor` run against a genuinely unreachable network
  reports the model **unverified**, never **rejected**.
- `_run_model_probe` itself is exercised directly with real (harmless) subprocesses —
  `sleep 5` under a `0.2s` timeout (proves the kill-and-return-`""` path) and
  `bash -c "echo marker; sleep 5"` under `0.5s` (proves partial-output capture survives
  the kill). Neither is an agent CLI.
- **The argv is built through the registry, not a hardcoded literal.** First draft used
  `["claude", "-p", ...]`/`["codex", "exec", ...]` literals and broke
  `tests/test_one_shot_agent_calls.py::test_no_module_shells_out_to_an_agent_cli_directly`
  (an AST scanner banning exactly that shape outside a driver, because it's what a
  *feature* looks like when it bypasses `maestro.agentcall`). Fixed by building only the
  *flags* as a literal and resolving the binary name via
  `registry.driver_ref(backend).binary` at call time — strictly more correct anyway
  (matches how `_check_backends` already resolves its binary), and the AST scanner no
  longer matches since the backend name isn't a literal list head anymore.

**A real gap this surfaced and fixed:** `test_doctor_healthy_project_fails_only_on_the_
unwired_test_adapter` runs `doctor` (non-`--pre-commit`) against a genuinely scaffolded
project — `templates/project.yaml.tmpl`'s real `roles:` example, with real model ids —
through an actual `subprocess.run([sys.executable, "-m", "maestro.cli", ...])`. With no
guard, my new check would (and, measured, **did**) probe the real, installed
`claude`/`codex` CLIs for real: the test's runtime went from its prior ~30s (the
documented one-time `maestro init` cost) to **~60s** — confirmed by timing before and
after the fix. Fixed with `_path_shadowing_agent_clis`, prepending a directory of harmless
`claude`/`codex` stub scripts (`exit 1`, no output) onto `PATH` for that one test — the
same PATH-shadowing technique `test_run_requires_the_shared_agents_tmux_session` already
uses for `tmux`, not a new pattern. (A cruder first attempt — strip any `PATH` entry that
resolves either name — broke `python3` resolution: this machine has an *unrelated* `codex`
binary on `/usr/bin`/`/bin`, confirmed by hand, not the AI CLI, that a name-only filter
can't distinguish from the real one without also losing the directories that carry
`python3`.) After the fix: the doctor call itself takes ~0.6s once the one-time fixture
cost is set aside.

**TDD evidence.** RED: all 8 new tests failed with `AttributeError: module 'maestro.cli'
has no attribute '_check_model_ids'` / `'_probe_model_id'` / `'_run_model_probe'` before
this part's code existed. GREEN after, including the two regressions found and fixed along
the way (`test_no_module_shells_out_to_an_agent_cli_directly`, the healthy-project doctor
test).

---

## Full-suite verification (final, after all three commits)

```
$ .venv/bin/python -m pytest -q --collect-only 2>/dev/null | awk -F': ' '/^tests\/.*: [0-9]+$/ {s+=$2} END {print s}'
3207
$ .venv/bin/python -m pytest -q
....................................................................... [100%]
PYTEST_EXIT=0
```

3207 collected (baseline 3191, **+16**: 4 from R8, 4 from Part 1, 8 from Part 2). Exit 0.
Output pristine (all dots, no `F`/`E`, no warnings printed by default `-q`). Also verified
individually at each commit boundary (37 → 39 → 41 in `tests/test_limits.py`; full
`tests/test_cli.py`, `tests/test_roles.py`, `tests/test_switch.py`,
`tests/test_orchestrator_switch_hooks.py`, `tests/test_one_shot_agent_calls.py`,
`tests/test_no_name_branching.py`, `tests/characterization/` all green throughout).

## Files changed

- `maestro/limits.py` — R8 normaliser fix; `DEFAULT_*_MODEL_NAME` constants; shrunk
  `_SHIPPED_DEFAULTS`; new public `model_slug()`.
- `maestro/roles.py` — `DEFAULT_MODELS` now derives its slugs via `limits.model_slug`
  instead of hardcoding them; docstring updated to explain the import without contradicting
  its "pure" claim.
- `maestro/cli.py` — new `_check_model_ids`, `_probe_model_id`, `_run_model_probe`,
  `MODEL_PROBE_TIMEOUT_SEC`, `_MODEL_PROBE_FLAGS`, `_MODEL_PROBE_INVALID_MARKER`; wired
  into `cmd_doctor`'s non-pre-commit checklist.
- `tests/test_limits.py`, `tests/test_roles.py`, `tests/test_cli.py` — new tests per part
  above, plus the one deliberate re-baseline (GPT-5.6 literal fold) and the two test-only
  fixes needed to keep `test_cli.py`'s existing end-to-end doctor test hermetic
  (`_path_shadowing_agent_clis`).

## Self-review findings (fixed before this report)

1. Raw `["claude", ...]`/`["codex", ...]` argv literals tripped
   `test_one_shot_agent_calls.py`'s AST scanner — fixed by resolving the binary name
   through `registry.driver_ref` instead of a literal.
2. The same feature made an existing end-to-end doctor test invoke real agent CLIs
   (measured: ~28s of added real subprocess time) — fixed with PATH-shadowing, following
   an existing precedent in the same file rather than inventing a new technique.
3. A stray commit-message backtick got shell-interpreted on the first R8 commit (cosmetic
   only, `git commit --amend`d before pushing forward).
4. Minor test cleanup: removed an unused `monkeypatch` fixture parameter and two clumsy
   manual-monkeypatch/dead-code drafts in the model_ids tests before finalizing.

## Concerns / things worth a second look

- `_check_model_ids` costs real wall-clock time on a genuine `doctor` run when a backend's
  binary exists but the network is unreachable — up to `MODEL_PROBE_TIMEOUT_SEC` (8s) per
  distinct `(backend, model)` pair, since a *valid* model never short-circuits early. For
  the template's default 2-pair roster that's at most ~16s added to a manual `doctor`
  invocation when fully offline; never added to `--pre-commit`. I judged this an acceptable
  cost for catching a typo before it reaches an implementer, per the brief's own framing,
  but flagging it explicitly since "cheap" is a judgement call.
- Codex's rejection marker (`"Model metadata for"`) is also the message Codex prints for
  any model id its *local* CLI install simply doesn't have cached metadata for yet —
  including a brand-new, genuinely valid model the operator's Codex CLI predates. This
  would report a false "rejected" for that narrow window (new model, stale local Codex
  install) rather than "unverified". I didn't find a more specific marker in Codex's output
  to distinguish the two; noting it rather than silently accepting the risk.

---

# Fix round 1 (review response, commit `77d12c0`)

Base: `b8940dd`, baseline 3207 collected / exit 0. Two Important findings fixed, one Minor
folded in, per the reviewer's instructions. Design not reworked.

## Important 1 — corrected cost figure + aggregate budget

**The reviewer was right and my original report's cost estimate was wrong.** I said "the
template's default 2-pair roster" at "~16s" offline. Hand-tracing
`templates/project.yaml.tmpl`'s actual `roles:` block shows each of implementer/judge/
diagnoser's `models:` maps carries **two** entries (`claude:` and `codex:`), and
`_check_model_ids` collects a pair from *every key*, not just the role's active
`backend:`. The real, deduplicated roster is **four** pairs:
`(claude, claude-sonnet-5)`, `(claude, claude-opus-5)`, `(codex, gpt-5.6-terra)`,
`(codex, gpt-5.6-sol)`. Fully offline, with no cap, that is `4 × 8s = 32s` — and nothing
stopped a fifth or sixth pair (a third backend, an extra role) from adding another 8s
each, unbounded.

**Fix chosen:** a total probe-time budget, `MODEL_PROBE_BUDGET_SEC = 2 *
MODEL_PROBE_TIMEOUT_SEC` (16s) — the smaller change the reviewer suggested, preserving
offline-tolerance rather than adding concurrency. `_check_model_ids` now checks elapsed
wall-clock time (`time.monotonic()`) before starting each probe; once the budget is spent,
every remaining pair is reported as `"not checked (probe time budget exhausted)"` rather
than probed. Per-probe behaviour (`MODEL_PROBE_TIMEOUT_SEC`, the offline-tolerant
marker/no-marker interpretation) is unchanged.

**New realistic worst case, computed against the actual 4-pair roster** (verified
directly, not estimated — a fake `run` that sleeps for the full per-probe timeout,
standing in for a genuinely offline network):

```python
>>> elapsed  # measured, 4-pair roster, budget=16s, every probe times out
16.0
>>> check.detail
'0 confirmed invalid among 2 checked (a clean probe rules out a known-bad id, it does
 not confirm a working one); 2 not checked (probe time budget exhausted):
 codex:gpt-5.6-sol, codex:gpt-5.6-terra'
```

**Corrected worst case: ~16s, not ~32s (unbounded before this fix, not merely "smaller
than before" — bounded at exactly `MODEL_PROBE_BUDGET_SEC` regardless of how many pairs
a project configures).** Adding a third backend or more roles no longer changes this
number at all: the budget caps total probe time to at most two full-length probes per
`doctor` invocation, and every pair beyond that is honestly reported as unchecked rather
than silently skipped or silently slow.

## Important 2 — symmetric risk statement (report-only, no code change)

My original report disclosed the stale-cached-metadata false-positive risk only for
Codex's marker (`"Model metadata for"`). **The identical risk applies to Claude's marker
too:** `"claude-code:unrecognized_model"` is the same class of client-side allowlist —
if a locally-installed `claude` CLI predates a brand-new, genuinely valid model id, there
is nothing ruling out the same CLI printing its own "I don't recognise this" marker for a
model that is, in fact, fine. I had not connected this for Claude in the first report;
stating it symmetrically now.

**Why this is tolerable, not just disclosed (the mitigation was already in the code, I
just hadn't named it):** `_check_model_ids` returns its `Check` without `required=True`
— `Check.required` defaults to `False`, matching the pre-existing `_check_model_limits`.
`cmd_doctor`'s exit-code logic is `if check.required and not check.ok: worst_ok = False`
— a false positive from *either* backend's marker prints as `[WARN]`, never `[FAIL]`,
never flips `doctor`'s exit code, and never runs at all under `--pre-commit` (this check
is only in the non-`pre_commit` list). So the blind spot — for both backends
symmetrically — is bounded to a warning an operator can see and dismiss, not a build- or
commit-blocking failure. `_check_model_ids`'s docstring now states this explicitly.

## Minor, folded in — honest "checked" vs "confirmed" wording

A valid model that times out (the normal offline outcome, and — per the module's own
design note — also the expected outcome even *online*, since the probe is deliberately
killed well before a real turn completes) returned `(False, "")` from `_probe_model_id`
and was counted into `"probed N model id(s)"` exactly like every other non-invalid
outcome. That phrasing reads as "N confirmations," which this probe structurally cannot
produce — it can only ever prove a model id is *bad* (the marker appeared); the marker's
*absence* is never proof a model works.

**Fix:** `_check_model_ids`'s success detail now reads:
```
"0 confirmed invalid among {checked} checked (a clean probe rules out a known-bad id,
 it does not confirm a working one)"
```
plus, when applicable, the budget-exhausted and structural-note suffixes. A `doctor` run
can no longer claim more confirmation than it actually performed.

## TDD evidence

- RED: `test_check_model_ids_stops_probing_once_the_aggregate_time_budget_is_exhausted`
  failed with `AttributeError: module 'maestro.cli' has no attribute
  'MODEL_PROBE_BUDGET_SEC'`; `test_check_model_ids_never_claims_a_clean_result_confirms_a_model_works`
  failed with `assert 'confirmed invalid' in 'probed 1 model id(s)'` — both before this
  commit's code existed.
- GREEN after: `.venv/bin/python -m pytest -q tests/test_cli.py -k "model_ids or
  probe_model_id or run_model_probe"` → 10 passed.
- Full covering sweep: `tests/test_cli.py`, `tests/test_one_shot_agent_calls.py`,
  `tests/test_no_name_branching.py`, `tests/test_roles.py`, `tests/test_limits.py`,
  `tests/test_switch.py` all green together.

## Exact commands and output (final verification)

```
$ .venv/bin/python -m pytest -q --collect-only 2>/dev/null | awk -F': ' '/^tests\/.*: [0-9]+$/ {s+=$2} END {print s}'
3209
$ .venv/bin/python -m pytest -q
....................................................................... [100%]
PYTEST_EXIT=0
```

Run in the foreground with a 600000ms tool timeout (no background). 3209 collected
(baseline at `b8940dd`: 3207, +2 — the two new tests above). Exit 0. Output pristine.

## Files changed (this round)

- `maestro/cli.py` — `import time`; new `MODEL_PROBE_BUDGET_SEC`; `_check_model_ids`
  rewritten to enforce the budget and to report "confirmed invalid" honestly instead of
  "probed N"; docstring states the `required=False` mitigation and the symmetric
  stale-marker risk for both backends.
- `tests/test_cli.py` — two new tests (budget enforcement measured via a real-sleeping
  fake `run`; honest wording).

## Deferred (per instruction, not addressed)

Marker-substring matching being brittle to a future CLI wording change — accepted
trade-off, carried to final review as instructed.
