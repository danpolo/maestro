# Wave 2: finish the pre-integration readiness queue (A4 onward)

**Created:** 2026-08-31, by the session that executed wave 1 of
`handoffs/2026-08-30_pre-integration-readiness-queue.md`. Baseline commit: `d7dd51d`, working tree
clean, suite **3138 collected / exit 0**.

**What this session is:** the second half of a queue that is already half done. Five of the sixteen
items are merged and reviewed. Eleven remain. Every design decision is still the one recorded in the
original queue's decisions table — **do not re-open any of them**, including the ones this session's
predecessor ruled on (they are listed below and are equally closed).

---

## Read first

1. `handoffs/2026-08-30_pre-integration-readiness-queue.md` — **the plan.** It is still the
   authority for every remaining item (A4, B1, B2, C1, C2, C3, C4, C7, D1, D2, D3), its "Decisions
   already made by Dan" table, its Method section, and its Verification section. Read the item text
   for what you are about to do; do not re-derive intent from the code.
2. `/home/dan/.maestro-sdd/2026-08-30-pre-integration/progress.md` — **the wave 1 ledger.** It holds
   the pre-flight conflict scan, all eight rulings (R1–R8) carried forward, the per-item review
   outcomes, the deferred minors, and the scratchpad-wipe incident. The rulings bind you.
3. `/home/dan/.maestro-sdd/2026-08-30-pre-integration/wave1-close.md` — the verification snapshot at
   wave 1 close, including the one item still owed (C5's re-review, below).
4. `docs/DESIGN.md` §8 (model context limits, D4) — A4 is the item that makes §8's claim true.
   §5/§6/§7 remain the design authority for the rest.
5. `maestro/roles.py` — the pure resolution layer; C1, C2, C4 and C7 all touch it or read it.
6. `tests/test_purity.py` and `tests/test_no_reference_sidecars.py` — the existing gates, and the
   model for the new ones D1/D2 must add.

---

## State at handoff

- `master`, clean, `d7dd51d`. Suite **3138 collected, exit 0**.
  (This repo's pytest config prints no summary line. Get the count with
  `.venv/bin/python -m pytest -q --collect-only 2>/dev/null | awk -F': ' '/^tests\/.*: [0-9]+$/ {s+=$2} END {print s}'`
  and the exit code from a separate plain run.)
- Wave 1 branches merged and deleted; wave 1 worktrees removed. No stale registrations.
- The two model-limit tables outside the repo already carry C6's rows
  (`Claude Haiku 4.5` in `~/.claude/`, `GPT-5.5` in `~/.codex/`), each with a prose caveat marking
  the numbers as conservative judgements. Verified present, correctly formatted, `fable` absent.

### Done, reviewed clean
**A1** `10ccadd` · **A2** `999b063` · **A3** `0065b1b` · **B3** `24062c0` · **C6** (out-of-repo)

### Remaining, in execution order
**A4 → B1 → B2 → C1 → C2 → C3 → C4 → C7 → D1 → D2 → D3**

---

## Owed from wave 1 — do this first

**C5's scoped re-review was never dispatched.** Its fix commit `bda4bdb` was authored by the C5
implementer, which then died on a rate limit before committing. The controller read the whole fix
diff, confirmed both findings addressed, ran the covering tests (67 passed), and committed and
merged it. That is controller verification, not the independent review seat the process requires.

**Dispatch a scoped re-review of `865f4f2..bda4bdb`** (findings: the `_clean_backend_binary_cache`
fixture must clear before *and* after, mirroring `tests/backends/test_registry.py`'s `_clean_cache`;
and the removal of the unreachable `if not backend_names` fallback in `_check_backends`) before
anything relies on C5. C7 extends the same doctor check list, so this is on C7's critical path.

---

## Rulings carried forward — closed, do not re-open

Full text in the ledger. Summarised so you do not have to guess at their scope:

- **R2 (C1)** — a task's `model:` resolves through the backend's size map if the keyword matches;
  otherwise it is passed through as a literal model id; a keyword with no entry for the resolved
  backend is ignored, the role-table default applies, and a warning is logged. Never pass a bogus id
  to a CLI. Document it in code and in `project.yaml.tmpl`.
- **R3 (A3×B1)** — B1 **must preserve** the SWITCH-sentinel contract A3 made structurally
  unconditional. A3 restructured `_make_brief` so a single trailing `return` appends
  `SWITCH_SENTINEL_CONTRACT` after both the ordinary and the `dispatch: manual` branches. B1 rewrites
  wording in that same function. If B1's diff moves that append back inside a branch, or drops the
  test pinning it, that is a blocking finding — A4's rotation is carried by that sentinel.
- **R4 (B2)** — do **not** add a `description:` key to `project.yaml`. The system prompt becomes
  "…for the project described below"; `docs/PROJECT.md` is already passed as user content.
- **R5 (D2)** — the D2 gate must cover the whole `AgentBackend` capability surface (every public
  method drivers implement — `parse_exit`, `usage`, `exhaustion_signal`, …) **and** every
  `Capabilities` field, with a commented allow-list for genuinely driver-internal ones. As the plan
  words it, the gate would not have caught `parse_exit`, the very bug it cites as motivation.
- **R7** — worktree fan-out replaces the plan's "one session at a time" note (see Method).
- **R8 (→ C7)** — `limits.resolve()` matches `Claude Haiku 4.5` and `claude-haiku-4.5` but **not**
  the actual configured id `claude-haiku-4-5-20251001`, nor `claude-haiku-4-5`. Two gaps:
  period-vs-hyphen folding and date-suffix stripping. **Fix the normaliser, do not rename the table
  row** — the tables use display names by convention and the plan states the parser tolerates both
  spellings. This is load-bearing twice: verification item 7 requires every configured model to
  resolve, and A4 keys its rotation trigger on the running model id, so a haiku implementer would
  otherwise never rotate.

### Deferred minors to hand the final review
- `orchestrator.py` reconcile prints `verdict.reset_at` unguarded while the paired
  `_pause_for_usage_limit` uses `verdict.reset_at or ""`. Unreachable today; `ExitVerdict` permits
  `None` for `kind=quota_exhausted`.
- `backends/claude.py:24` docstring now misleads — says the reactive net is
  `quota._scan_impl_log_for_limit`, but production goes through `parse_exit`, which never calls it.
  **Fold into D3.**
- `docs/DESIGN.md:118` still documents the stale `orchestrator/profiles/` layout. **Fold into D3.**
- `selfheal/redo.py`'s `main()` success notice duplicates leading/trailing scaffolding in both
  branches while the merge-failure site extracts only the differing fragment (`nb_note`).

---

## In scope

The eleven remaining items, per the plan's own text, in the order listed above. Note the
dependencies the plan states and wave 1 confirmed: **A3 → B1/C1** (all touch `_make_brief` /
the launch site), **C2 → C7**, **C6 → C7**, **B2+B3 → D1**, **A1 → D2**, **A4 → D3**.

A4 is the largest remaining item and the only one with real design surface. Its four constraints in
the plan (`REASON_CONTEXT` rather than a degenerate self-switch, five journal fields,
`switch_task` must never call `create_worktree`, and an anti-loop guard keyed on a sample newer than
the relaunch) are all still binding.

## Out of scope

- Everything the original plan lists as out of scope, unchanged — in particular: onboarding any new
  project, touching `/home/dan/projects/AbuAliArchive`, re-opening any decision, calibrating
  `SWITCH_THRESHOLD_PCT` to a new value, and adding Fable 5 or any non-subscription model.
- Re-doing wave 1. A1/A2/A3/B3/C6 are merged and reviewed; treat them as fixed ground.

---

## Method — what worked, and the two traps that cost the most

Use `superpowers:subagent-driven-development`, with these adaptations proven over wave 1:

**Worktree fan-out (R7).** The plan's "one session at a time" note exists because of file
collision, but the real constraint is a single git index. Items with disjoint file sets and no
stated dependency run **concurrently**, each in its own worktree on its own branch, merged back to
master in plan order:

```
git worktree add /home/dan/.maestro-sdd/wt-<item> -b sdd/<item> <master head>
```

Agents run `/home/dan/projects/maestro/.venv/bin/python` with the worktree as cwd — verified that
cwd shadows the venv's editable install, so each agent tests **its own** source. Never put two items
that touch the same file in the same wave. Wave 1 ran four implementers plus reviewers concurrently
and merged with zero conflicts; the merged suite count composed exactly
(3110 + 12 + 10 + 4 + 2 = 3138), which is the check that the fan-out did not lose work.

**Trap 1 — `/tmp` is not durable.** The workspace originally lived in the session scratchpad. `/tmp`
was cleared mid-run and destroyed four worktrees of uncommitted work. Everything now lives in
`/home/dan/.maestro-sdd/` — persistent, and outside the repo so `tests/test_purity.py` (which scans
the *working tree*, not the index) stays green. **Do not put the workspace inside the repo**; its
plan excerpts name the reference project and will fail two purity gates.

**Trap 2 — subagents strand themselves on background runs.** Three agents ended their turn waiting
on a background pytest or a Monitor notification that cannot arrive once the turn ends. **Tell every
implementer to run the suite in the foreground with a 600000 ms tool timeout, and to commit as soon
as it has something green** rather than accumulating uncommitted state. The wipe cost four items
precisely because nothing had committed.

**Reviews earn their seat.** Every item that reached review produced at least one finding a passing
test suite did not. The highest-value example: A3 looked complete with 8 green tests, but
`_make_brief` early-returned for `dispatch: manual` tasks *before* the SWITCH-contract append, and
the new test used a task with no `dispatch` key so it never exercised that branch. Give reviewers
the binding constraints verbatim and the stake ("A4's rotation is carried by this sentinel"), and
tell re-reviewers which of two offered fixes was ruled for, so they can catch a fix that closes the
symptom the rejected way.

**Model selection.** Sonnet handled every implementation and review seat in wave 1 without an
escalation. Haiku was adequate for read-only research and a narrow data-only review. Reserve the
most capable model for A4 and for the final whole-branch review.

**Rate limits.** Wave 1 lost a full round to a 429 wave that killed five agents at once; the current
window resets **3am Asia/Jerusalem**. The message wording says "monthly spend limit" but also fires
for ordinary 5h/weekly caps. If several agents die at once on 429, stop dispatching rather than
burning the queue against it.

---

## Verification — report these numbers back

The original plan's Verification section is unchanged and still the target. Items 1–4 have moved;
report the current values, not wave 1's:

1. `.venv/bin/python -m pytest -q` → collected count and exit code. **Now 3138 / 0** at `d7dd51d`;
   it should rise again. A fall means a pin was deleted — explain it.
2. `grep -rn "parse_exit" maestro/ | grep -v backends/` → **already satisfied**: a real core call
   site at `maestro/orchestrator.py:612`. Confirm it survives.
3. `grep -rniE "abualiarchive|main_bot|restart_bot|Arabic/Hebrew|RAG archive" maestro/ --include=*.py`
   → currently **6 hits: 1 live string + 5 provenance comments**. The live one is
   `maestro/orchestrator.py:276` — B2's target. **Target after B2: zero live strings.**
4. `grep -rn 'REPO / "orchestrator"' maestro/` → **already 0**. Confirm it stays 0.
5. On a throwaway scratch repo: `maestro init`, then `maestro doctor` → full check list; confirm
   (a) `backends` probes both chain members (C5, merged — verify end to end), (b) the new model-id
   probe appears (C7), (c) no check references a path `init` did not create.
6. Same scratch repo: dump a generated implementer brief — **not** by launching an agent — and
   confirm it carries both the SWITCH-sentinel contract (A3, merged) and the resolved model name
   (B1/C1). Check a `dispatch: manual` task too; that branch was the wave 1 defect.
7. `limits.resolve_all` over the post-C6/C7 roster → every configured model resolves **including
   `claude-haiku-4-5-20251001`** (see R8), and `claude-fable-5` is absent.
8. State plainly which of G1–G9, A1–A4 and B1–B3 are done, and for anything left open, why.

---

## Suggested skills

`superpowers:subagent-driven-development` to run the queue · `superpowers:test-driven-development`
for the item work · `superpowers:systematic-debugging` if A4's rotation or the characterisation
suite goes sideways · `superpowers:verification-before-completion` before reporting any number
above · `superpowers:requesting-code-review` for the final whole-branch review.
