# Pre-integration readiness queue: nine gaps + finish the AbuAliArchive decoupling

**Created:** 2026-08-30, by a read-only audit session that answered Dan's question *"is maestro
finished — can I start integrating it into other projects?"*. Baseline commit: `7e29e3b`,
working tree clean, suite green (**3109 collected, exit 0, 0 failures**).

**Answer that produced this queue:** no, not yet. Three things are still wired to the reference
project (AbuAliArchive) rather than to what `maestro init` actually scaffolds, one headline
feature (the D4 context ceiling) is parsed but never enforced, and the automatic quota-fallback
path is Claude-shaped end to end. A second project would hit at least three of these on day one.

**Dan's ruling:** fix **everything** — G1–G9 *and* a complete AbuAliArchive decoupling — before
onboarding any new project. He wants "full generalized and no connection to it."

---

## Read first

1. `docs/DESIGN.md` §5 (`project.yaml`), §6 (backend abstraction, D1), §7 (mid-work switching,
   D2/D3), §8 (model context limits, D4). These are the design authority for almost everything
   below; do not re-derive the intent from the code.
2. `maestro/roles.py` — the whole module. It is the resolution layer every phase touches, it is
   pure, and its docstring explains why. Read it before changing any call site.
3. `maestro/backends/base.py:81-88` (`Capabilities`) and `:172-190` (`ExitKind` / `ExitVerdict`).
   Phase A is entirely about routing core decisions through these instead of around them.
4. `maestro/switch.py:1-45` (module docstring — three triggers, one path). A4 adds a
   *fourth* trigger onto the same path; read why the existing three are shaped the way they are
   before adding to it.
5. `tests/test_no_reference_sidecars.py` — the existing gate against reference-project coupling,
   and the model for the new gate Phase D asks for.
6. `docs/PROGRESS.md`, last ~200 lines — the M5/M6 cutover writeups. They explain why
   AbuAliArchive-shaped code is still present and which parts were deliberate.

---

## State at handoff

- `master`, clean, `7e29e3b`. Suite 3109 collected / exit 0.
- Both CLIs present: `claude` 2.1.251, `codex-cli` 0.151.0.
- `~/.claude/model_context_limits.md` holds Claude Sonnet 5 / Claude Opus 5;
  `~/.codex/model_context_limits.md` holds GPT-5.6 Luna / Terra / Sol. Both parse cleanly.
- The live loop for this repo (`maestro-build.service`, watched by `maestro-watchdog.timer`) is a
  separate concern; nothing here requires stopping it, but see coordination below.

## Parallel-session coordination

- Phases A–D below are **sequential, not parallel**, and so are the items inside Phase A. A3 is a
  hard prerequisite for A4 — the rotation A4 adds is delivered through the `SWITCH` sentinel that
  A3 is what makes reachable — so do not start A4 until A3 is green. C changes `roles.py`, which A
  and B both build on; D gates all of it. One session at a time. If a session's context fills, hand
  off at an item boundary, never inside one.
- Nothing here touches `/home/dan/projects/AbuAliArchive`. If a change appears to require editing
  that repo, stop and ask Dan — the decoupling direction is "maestro stops assuming AbuAliArchive's
  layout", never "AbuAliArchive adopts maestro's".
- `~/.codex/` is edited **only by invoking the Codex CLI** (Dan's delegate-to-agent rule), never by
  writing into it directly. This matters in Phase C, which adds a table row there.

## Decisions already made by Dan — do not re-open these

| Gap | Decision |
|---|---|
| G4 context ceiling | **Same-backend session rotation** using the existing switch machinery — not warn-only, not doctor-only |
| G1/G2 quota | **Full fix**: route exit classification through `driver.parse_exit`, and have the loop sample `driver.usage()` itself and write `usage.json` — removing the dependency on an interactive Claude Code session |
| G3 profiles | **Repoint `SYS_PROMPT` at the scaffolded `profiles/<role>.md`**, and fold the profile into the brief when `capabilities().system_prompt_file` is `False`. No new `project.yaml` knob. |
| G5 model | **Task-level `model:` overrides the role table** (role table becomes the default) |
| G6–G9 | **All four**, plus make `SWITCH_THRESHOLD_PCT` configurable via `thresholds:` |
| Model roster | **Add Haiku 4.5 and gpt-5.5. Do NOT add Fable 5** |
| New-model path | **The two `model_context_limits.md` tables become the single source**, and `doctor` validates each configured model id against its CLI |
| Sequencing | Fix everything, including G6–G9 and the decoupling, **before** onboarding any project |

### The model rule, in Dan's words

> "on fable note that its a model that uses per token api, its not in the subscription thats why i
> left it out, as a rule i want only models that are in the subscription."

**Only subscription-covered models may enter the roster.** Fable 5 bills per token against the API,
so an unattended overnight loop could spend real money outside the flat subscription the whole
quota/fallback machinery is built around. If you find yourself adding a model "for completeness",
stop and check whether the subscription covers it; ask Dan if unsure. Note `orchestrator.py:271`
currently carries the comment *"Opus now, Fable when CLI access returns"* — that comment is now
wrong; striking it is part of B3 below.

---

## In scope

**Execution order is A1 → A2 → A3 → A4 → B1 → B2 → B3 → C → D, and it is not
arbitrary.** A3 (deliver the profile, and with it the `SWITCH` sentinel contract) comes before
A4 (context-ceiling rotation) because A4's rotation is *carried* by that sentinel: run A4 first
and every rotation degrades to a 120-second wait and a hard window kill, losing the uncommitted
work the rotation exists to preserve. A3 was originally filed under Phase B as a decoupling
item; it is here because A4 depends on it, not because it stopped being one.

### Phase A — make the unattended loop correct on any backend and any project

**A1. `driver.parse_exit` is dead code.** Both drivers implement it (`claude.py:523`,
`codex.py:1081`) and **no module under `maestro/` calls it** — verify with a grep before you start,
so you see it for yourself. The reconcile path instead calls `quota._scan_impl_log_for_limit`
(`orchestrator.py:808`), whose `_LIMIT_RE` (`quota.py:47`) is the Claude CLI's wording. Codex's own
`exhaustion_signal` (`codex.py:517`) is therefore never reached.

*Consequence:* a codex implementer that dies on quota falls through to
`"no window, no sentinel — stale entry; marking FAILED"` (`orchestrator.py:827`) → retry → straight
back into the same wall it just hit. The switch machinery that exists to prevent exactly this never
runs, because nothing classified the exit as `quota_exhausted`.

*Fix:* the reconcile branch should resolve the entry's driver (`_entry_backend` already exists) and
call `parse_exit(rc, log_tail)`, using `ExitVerdict.kind` / `.reset_at` / `.evidence` where the scan
dict's `limit` / `reset_iso` / `evidence` are used today. Keep `_scan_impl_log_for_limit` as the
`ClaudeBackend.parse_exit` implementation detail it already effectively is; do not delete it, and do
not let core code call it directly again. The characterisation suite pins the current shape — expect
to update `tests/characterization/` and say so explicitly in the commit message.

**A2. `usage.json` is written by Dan's interactive statusline hook, not by maestro.**
`~/.claude/statusline-command.sh:27-51` writes `$cwd/.orchestrator/usage.json`, and only when a
Claude Code session's cwd is that project. `maestro init` never sets this up and `doctor` never
checks it. On a project with no interactive session open, `quota.get_effective_cap()`
(`quota.py:153-161`) reads a missing file → `five_pct = 0` → the concurrency throttle and the
`PAUSE_PCT` (92%) pause **never fire**. Combined with A1, an unattended loop has no quota protection
at all.

*Fix:* the main loop samples usage itself. Both drivers already implement `usage()` and declare
`usage_telemetry=True`; `base.to_usage_json` already renders the exact document shape `quota.py`
reads. So: once per poll, for each backend currently in use, take a sample through the driver and
write `.orchestrator/usage.json` via `to_usage_json`. Notes:

- `orchestrator._under_usage_pressure` (`:629-650`) already does driver-based sampling for the
  switch decision. Reuse that reading rather than sampling twice per poll.
- `CodexBackend.usage_from_app_server` spawns a short-lived `codex app-server` subprocess
  (`codex.py:1202-1250`). It is bounded by `APP_SERVER_TIMEOUT_SEC`, but do not call it more than
  once per poll per backend.
- **`None` is not zero.** `Usage.context_used_pct` and a missing sample both mean *not measurable*
  (finding G6, documented in `base.py:13-21`). Never write a `0` where you had no reading — that is
  the exact bug the statusline script guards against in its own comment at line 33.
- Leave the statusline hook working. This makes maestro independent of it, not incompatible with it;
  a project where both write the file should still behave.

**A3. The implementer profile is never delivered on any `init`'d project.** `implementer.py:54`
sets `SYS_PROMPT = REPO / "orchestrator" / "profiles" / "implementer_sys.md"` — AbuAliArchive's
layout, and that file does exist there (confirmed). `maestro init` scaffolds
`<repo>/profiles/implementer.md` (`cli.py:403-406`) plus `operating_preamble.md` (`cli.py:398-401`),
and **nothing in the package reads either one**. `claude.py:171-181` silently omits
`--system-prompt-file` when the file is missing, so this fails as "the feature is quietly absent",
never as an error.

Three consequences, in increasing severity:

1. Implementers on every freshly-onboarded project run with no profile at all.
2. The `SWITCH` sentinel contract lives **only** in that profile
   (`templates/profiles/implementer.md:9-12`) and `_make_brief` never mentions it
   (`implementer.py:233-256`). So the cooperative-checkpoint half of the D2 threshold switch is
   undelivered on any new project: every threshold switch degrades to a 120-second wait followed by
   a hard `tmux kill-window`, losing everything since the last commit. **It is also the failure mode A4's
   rotation would inherit**, which is why A3 is ordered ahead of it.
3. `CodexBackend` declares `system_prompt_file=False` (`codex.py:707-713`) and nothing folds the
   profile into the brief, so codex never receives it — on *any* project, AbuAliArchive included.

*Fix (Dan's choice):* point `SYS_PROMPT` at the scaffolded `profiles/implementer.md`, and where
`capabilities().system_prompt_file` is `False`, prepend the profile text to the brief instead of
dropping it. Both launch sites need it: `implementer.py:490-499` and `switch.py:857-865`. Do **not**
add a `project.yaml` profiles block — Dan explicitly rejected more config surface here, and this
repo has a track record of declared-but-unread knobs (see G6).

Belt and braces: regardless of profiles, move the SWITCH-sentinel/checkpoint contract into
`_make_brief` itself so it is delivered unconditionally. A safety mechanism whose only carrier is an
optional file is not a safety mechanism.

**A4. The D4 context ceiling is measured, reported and ignored.** `maestro/limits.py` parses the two
`model_context_limits.md` tables — the same files Dan's `CLAUDE.md` session-triage section points at,
so the numbers are already the right source of truth — merges them, tolerates display-name vs. slug
spelling, and caches to `.orchestrator/model_limits.json`. It has **exactly one consumer in the
package**: `cli.py:552`, the `doctor` check. `usage.json` carries `context_used_pct`, whose only
consumer is a print at `status.py:176`. DESIGN §8 claims this "replaces the hardcoded 110K/120K
constants with a lookup keyed on the running model id"; the lookup exists, the replacement never
happened.

*Fix (Dan chose same-backend session rotation):* when an in-flight implementer's sampled context
crosses its model's `prepare_handoff_high`, put it through `switch.switch_task` **targeting its own
current backend** — SWITCH sentinel, grace period, checkpoint, relaunch in the same worktree with a
`handoff_brief`. This is precisely what Dan does by hand per his own session-triage rule, and
`switch.py` already implements every step. What is new is only the trigger and the same-backend
target.

Design constraints, all of which `switch.py` will fight you on if you ignore them:

- `switch.target_backend` (`:668-693`) returns `None` when the target equals the current backend,
  and `SwitchOutcome.switched` is defined as `backend != preferred`. A same-backend rotation must
  not be expressed as a degenerate "switch to yourself" through those functions — decide whether to
  add an explicit rotation reason (`REASON_CONTEXT`, alongside `REASON_QUOTA` / `REASON_THRESHOLD` /
  `REASON_MANUAL`) that bypasses target selection, and journal it distinctly.
- The journal record keeps its **five fields** (`switch.py:36-40`). Flatten into `detail`.
- `switch_task` must still never call `create_worktree` — `tests/test_switch.py` makes that
  explosive. The uncommitted work is the entire point.
- Rotation needs its own anti-loop guard. A task that rotates, immediately re-reads a stale
  `context_used_pct` from the pre-rotation sample and rotates again will churn forever. `Usage`
  carries `updated_at`; require a sample newer than the relaunch, or count rotations per task the
  way `backends_tried` counts backends.
- Use `prepare_handoff_high` as the trigger, not `exception_ceiling`. The tables' own semantics
  (and `CLAUDE.md`) are that "prepare handoff" is where you act and the ceiling is where it is
  already too late.

### Phase B — remove reference-project assumptions from model- and operator-facing text

**B1. Reference-project text in the briefs.** `implementer.py:234` opens every brief with
`"You are a Sonnet implementer"` regardless of the model or backend actually resolved (and after
Phase C's G5 fix, the model is genuinely variable). `implementer.py:250` says
`"Do NOT restart the bot"` — AbuAliArchive's RAG bot. `_make_prep_brief` (`:315`) repeats
`"You are a Sonnet PREP implementer"`. Replace with the resolved model/backend, and with a generic
"do not restart or deploy any running service" guardrail.

**B2. The proposal generator describes AbuAliArchive to the model.** `orchestrator.py:275`:

```
"You are the autonomous orchestrator for an Arabic/Hebrew Telegram RAG archive bot. "
```

This is sent verbatim, on every project, whenever the queue empties. The same call already passes
`docs/PROJECT.md` as user content (`:263`, `:284`), which is where a project describes itself — so
the system prompt should say "for the project described below" and stop asserting a domain. If a
one-line description is genuinely wanted in the system prompt, take it from `project.yaml`'s
`project:` block; adding a `description:` key there is acceptable **only if you also wire it**,
otherwise it becomes another G6.

Also strike the now-false comment at `orchestrator.py:271` (*"Opus now, Fable when CLI access
returns"*) — per Dan's rule, Fable is never coming into this roster.

**B3. `/redo` still ships a Colab/Google-Drive notebook pipeline.** `maestro/selfheal/redo.py`
carries an end-to-end notebook workflow: `_redo_rules` (`:181-209`) tells the agent to run
`scripts/check_notebook.py`, re-upload to a `gdrive:` rclone remote, and report
`notebook_path` / `gdrive_dest` / `colab_link`; the runner then re-runs the syntax gate (`:352-359`)
and does a belt-and-suspenders `rclone copy` (`:364-375`).

Partially mitigated already — the *prompt* is gated on `confinement.available(CHECK_NB)` and the
upload on `nb_rel and gdrive_dest`, so a project without notebooks degrades to no-ops. But the
operator-facing text is **not** gated and fires on every project:

- `:121-123` — "The notebook was re-uploaded but the commit needs manual review."
- `:386-390` — "reworked X and reshipped it (syntax-gated, re-uploaded)", "Re-run the Colab, then
  /approve…"

*Fix:* gate every notebook/Colab/Drive-specific message on the same signal that gates the
behaviour, so a project with no notebook deliverable gets neutral wording ("reworked X and
reshipped it… review it, then /approve"). Keep the notebook path working when `CHECK_NB` is
present — it is a legitimate project-owned capability (`tests/test_no_reference_sidecars.py`'s
`ALLOWED` set documents why), and Dan still uses it on AbuAliArchive. The goal is *no unconditional
assumption*, not *removal*.

`implementer._make_prep_brief` (`:290-307`) is the same pattern and is **already** correctly gated
on `CHECK_NB` — use it as the worked example rather than inventing a second approach.

### Phase C — configuration and model surface

**C1 (G5). Per-task model selection is silently discarded.** `implementer.py:479-480`:

```python
model_id = resolve_implementer_model(task)          # honours the task's `model:` field
model_id = backend_models.get(backend) or model_id  # ...and then throws it away
```

The role's per-backend table wins. Since `project.yaml.tmpl` always declares `models:`, a ROADMAP
task's `model: opus` has never done anything on a scaffolded project. Dan's ruling: **the task
overrides the role**, the role table is the default. Mirror how `agentcall.resolve_call`
(`agentcall.py:46-65`) already treats an explicit `model` argument as winning, so the two agree.

Note `IMPLEMENTER_MODELS` (`implementer.py:345-349`) is a claude-only `sonnet`/`opus` keyword map,
so `model: opus` has no meaning on codex. Decide and document: either the keyword resolves per
backend (needs a size→model mapping per backend) or a task pins a literal model id and is
implicitly backend-specific. Do not leave it ambiguous.

**C2 (G6). Dead knobs.** `project.yaml.tmpl:36-39` declares `model_limits: {claude: …, codex: …}`
(a mapping); `limits.py:167` reads `model_limits_paths: [...]` (a flat list). The template's block
is unread — the exact class of bug the `switch.on_usage_threshold` block was struck for on
2026-08-21, and the template even carries that removal's tombstone comment eight lines above.
Reconcile them (pick one spelling, wire it, delete the other). Likewise `roles.orchestrator` in the
template names a role no code resolves — `roles.KNOWN_ROLES` is `implementer`/`judge`/`diagnoser`
(`roles.py:86`) — while `diagnoser`, which *is* real, is absent from the template. Fix both
directions.

**C3 (G6b). `SWITCH_THRESHOLD_PCT` becomes configurable.** `switch.py:145` hardcodes `70.0` with a
comment saying calibration waits until switching has run in anger. It has now run in anger (M6).
Move it into `thresholds:` alongside `five_h_pause_pct` and `concurrency_cap`, using
`config.threshold()` exactly as `quota.py:31-38` does — including that module's reasoning about why
a malformed value degrades to the default rather than raising. Keep the default at 70.0.

**C4 (G7). The `/backend` pin is a trap.** `implementer._implementer_backend` (`:440-441`) is
`operator_backend() or settings.backend` — **no fallback-chain walk at all**. So `/backend codex`,
set during a Claude outage, pins every future launch to codex forever, and there is no
`/backend auto` to clear it (`hitl/commands.py:323-366`). Add a clear verb, and make the pin the
*preferred head* of `roles.resolve` rather than a bypass of it, so a pinned-but-exhausted backend
still falls through the chain. `roles.resolve` already takes `preferred=` for exactly this
(`roles.py:271-330`); the launch path just isn't using it.

**C5 (G8). `doctor` ignores the fallback chain.** `cli.py:561-579` probes binaries only for
backends named in `roles:`. With the template's all-claude roles and `fallback_chain: [claude,
codex]`, a missing `codex` binary passes `doctor` cleanly and only surfaces later as a switch that
silently doesn't happen. Probe every backend in the resolved chain.

**C6. Model roster: add Haiku 4.5 and gpt-5.5. Do not add Fable 5.**

- `claude-haiku-4-5-20251001` — a genuinely cheaper tier, useful for `judge`/`diagnoser` on
  low-stakes checks.
- `gpt-5.5` — `~/.codex/config.toml:502` still references it, so a project pinning it today
  resolves no ceiling row and silently falls back to shipped defaults.
- **Fable 5 is excluded** — per-token API billing, outside the subscription. See the model rule
  above.

Each needs a row in the relevant `model_context_limits.md`. The `~/.codex/` edit goes through the
Codex CLI, not a direct write. Pick ceiling numbers deliberately and say what they are based on;
do not copy Opus 5's row for a model with a different window.

**C7. Make the tables the single source of truth (Dan's choice).** Adding a model currently takes
four edits in three places, two of them inside maestro's source: `roles.DEFAULT_MODELS`
(`roles.py:92-96`) and `limits._SHIPPED_DEFAULTS` (`limits.py:71-77`) both duplicate what the
tables already say. Derive the shipped defaults from the tables where they exist, keeping a minimal
in-code fallback only for a machine with neither file — which is the documented reason
`_SHIPPED_DEFAULTS` exists at all, so shrink it to that job rather than deleting it.

Then add the `doctor` check Dan asked for: **probe each configured model id against its CLI**, so a
typo fails at setup instead of reaching `claude -p --model <typo>` and dying mid-task as an opaque
crash. Today `_check_model_limits` (`cli.py:536-559`) only verifies a ceiling-table row exists.
Keep the probe cheap and offline-tolerant — a `doctor` that hangs or fails on a flaky network is
worse than the gap.

### Phase D — gates, so none of this comes back

**D1.** Extend `tests/test_no_reference_sidecars.py` (or add a sibling with the same anti-vacuity
structure — see its `test_the_gate_actually_bites`) to fail on AbuAliArchive-specific *strings* in
`maestro/`, not just `REPO / "scripts" / "…"` path constants. Candidate terms, drawn from the audit
that produced this handoff: `main_bot`, `restart_bot`, `Arabic/Hebrew`, `RAG archive`,
`AbuAliArchive`, `gdrive:`, `Colab` outside a `CHECK_NB`-gated branch. Historical provenance
comments ("extracted from `orchestrator_run.py`") are legitimate and must not trip it — scope the
gate to string literals that reach a model or an operator, not to docstrings.

**D2.** A gate that fails when a `capabilities()` field exists that no core module reads, mirroring
the spirit of `tests/test_no_unresolved_pending.py`. `parse_exit` was fully implemented, fully
tested at the driver level, and never called by anything for two milestones; the driver-level tests
all passed the whole time. That is the failure mode worth institutionalising against.

**D3.** Update `docs/DESIGN.md` §8 once A4 lands, so the claim about replacing the hardcoded
constants becomes true rather than aspirational, and `docs/PROGRESS.md` with a session writeup in
the existing style.

---

## Out of scope

- Onboarding any new project. That is the *next* session, after this queue is green — it is the
  whole reason the queue exists.
- Touching `/home/dan/projects/AbuAliArchive` in any way.
- Re-opening any decision in the table above.
- Calibrating `SWITCH_THRESHOLD_PCT` to a new value. C3 makes it configurable and keeps the default
  at 70.0; choosing a different number is Dan's call with data, not this session's.
- Adding Fable 5, or any other model not covered by the subscription.
- `maestro/watchdog.py`'s `launch_orchestrator` naming and the `scripts/m5-*.sh` cutover scripts.
  Those are maestro's own dev/ops history, not code shipped into a consuming project; leave them
  unless Dan asks. (`scripts/run_overnight.sh:188` reads AbuAliArchive's HALT sentinel as a
  deliberate interlock for maestro's *own* build loop — that is not product coupling.)

## Method

- **TDD.** Every item above is a behaviour change with an observable symptom; write the failing
  test first. Phase A especially: the characterisation suite currently pins the *broken* behaviour
  in places, so be explicit in each commit message about which pin you are deliberately re-baselining
  and why.
- **No test may execute a real agent CLI.** `registry.py`'s docstring states this as a rule and
  every seam is already injectable (`SwitchDeps`, driver constructor kwargs, `_probe_version`). The
  C7 doctor probe is the one new place tempted to break it — inject it.
- Commit per item, not per phase. Each item above is independently revertable and should stay that
  way.
- Run the full suite at every phase boundary, not just at the end.

## Verification — report these numbers back

1. `.venv/bin/python -m pytest -q` → **collected count and exit code.** Baseline is 3109 / 0; the
   count should rise. A *fall* means you deleted a pin — explain it.
2. `grep -rn "parse_exit" maestro/ | grep -v backends/` → must now return **at least one core call
   site**. It returns zero today.
3. `grep -rniE "abualiarchive|main_bot|restart_bot|Arabic/Hebrew|RAG archive" maestro/ --include=*.py`
   → report every surviving hit and classify each as *provenance comment* (fine) or *live string*
   (not fine). Target: zero live strings.
4. `grep -rn 'REPO / "orchestrator"' maestro/` → must be **zero**. It is 1 today
   (`implementer.py:54`).
5. On a throwaway scratch repo: `maestro init`, then `maestro doctor` → report the full check list
   and confirm (a) `backends` probes both chain members, (b) the new model-id probe appears,
   (c) no check references a path `init` did not create.
6. Same scratch repo: confirm the implementer brief now contains the SWITCH-sentinel contract and
   the resolved model name, by dumping a generated brief — **not** by launching an agent.
7. `maestro limits`-equivalent (`limits.resolve_all`) over the post-C6 roster → every configured
   model resolves, **and `claude-fable-5` is absent**.
8. State plainly which of G1–G9, A1–A4 and B1–B3 are done, and for anything left open, why.

## Suggested skills

`superpowers:test-driven-development` for the phase work; `superpowers:systematic-debugging` if
Phase A's characterisation re-baselining goes sideways; `superpowers:verification-before-completion`
before reporting any of the numbers above.
