# P13 goal-driven execution + project-level TDD: completion report (R01-R70, GDT-0..GDT-14)

Date 2026-10-22. Branch `feat/p13-w11a`. Root (`feat/graph-engineering-foundation`) was fast-forwarded
to `c047d20` and released (`release.log` `announced:c047d20…`, `~/.maestro/latest` written 18:00 IDT; the
release hook's first run had 1 failure and its serial retry passed). The branch carries the close-out
commits on top of `c047d20` (section 3): the `goal_guard` diff-failure fix and the doc fixes.

**Outcome.** 65 of 70 requirements are built and green; 57 of them have nothing left over. 8 are
green only for the part this work owns, and the rest belongs to W7 or W12 (each row says so). 5 are not
built (W7 and W12, planned, owned). GDT-1..GDT-14 and the GDT-13 lows are **released**, so the
goal-driven TDD work this chain owns is complete. What remains is GDT-11 (W7) and GDT-12 (W12) only, plus
the named limits in section 5. No decision awaits Dan. The Context Gate is not built and is out of scope.
(Superseded by §7 and §8: GDT-11 and GDT-12 are built; all 70 requirements are green with no remainder.)

---

## 1. Reconciliation

- **Baseline observed.** Root was `dc1e560` (W2 merge) on 2026-10-17. `feat/p13-w11a` held the
  five original W11a commits `b9e3488` (parser and `maestro goals check`), `c6f50c1` (`serves`
  survives graduation, doctor coverage), `2709a60` (graph excerpts, reviewer `goal_progress`),
  `08d059b` (legacy implementer excerpt) and `09d2ad0` (ledger row, planner fault). `03-ORIGINAL-W11A-BASELINE.md`
  was used as a snapshot only; each row was re-checked against the live code.
- **Concurrent changes.** Codex's W4 (`fb646ca`, merged here as `044227e`) and W6 (`70d5077`,
  merged as `3b1815a`) landed on root during the work and were merged into the branch. They are
  the source of the full-suite failures listed in section 4.
- **Preserved.** D23 (goals never widen scope), "the reviewer observes, rules decide", stable
  goal ids, "ask only what is missing", the W2 history (`CAUSE_FAULT["task_did_not_serve_goal"]`
  still readable), and both the legacy and graph paths (the characterization tests were extended, not replaced).
- **Superseded, on purpose** (TRACEABILITY §3, Dan's decisions of 2026-10-17):
  3-7 goals required (now guidance, any count valid, R03);
  `serves: []` meaning "no check" (now only the expected-advancement check is skipped; regression and invariant checks stay, R28/R41);
  a missing `serves` as a warning (now unclassified: the draft warns, the execution gate holds that task, R29/R31);
  doctor warn-only (now a required FAIL when the gate would hold an open task, R31);
  one diff line as evidence (now outcome-appropriate evidence with an `unknown` option, R40);
  `advanced/no_progress/regressed` (now six impacts; old `no_progress` reads as a legacy value, R38/R62);
  automatic planner fault (now an `unattributed` observation with provenance, R58);
  priority deciding the winner (now priority informs a recommendation and every real conflict goes to Dan, R08/R48);
  Q1 "two in a row" (now modified B: repeated unexpected direct misses, a proven infeasibility, or a milestone gap, R44-R46);
  goal check only at the end (now milestone checkpoints plus an authoritative W11b gate, R43/R53).
- **Spec-internal choices** (no Dan decision needed) are in `docs/DESIGN.md` §19.9 (store layout, red-on-base rule),
  §19.10 (completion-gate internals and deviations) and §19.12 (the product fixes the demonstrator surfaced).

## 2. Plan

- **Single current plan.** `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md` (root-only, gitignored)
  carries B16, W11, W7 and W12 pointers to the register. Its W11a and W11b checkboxes are ticked for what is
  done (grill-me, ChatGPT web, setup and Maestro, the goal check stage); the goal check stage says what remains
  (the polish stage and loop-back, W7), and the W7 and W12 boxes stay unticked. The register is
  `docs/agent-plans/p13-goal-driven-tdd/TRACEABILITY.md` on this branch. §1 lists GDT-0..GDT-14 with
  dependencies and owners, §2 maps all 70 R-IDs (kind, item, workstream, tests, status, evidence), §4 is the
  verification log, §5 maps C1-C33 to tests. The normative spec (`00`-`03`) is in the same directory.
- **Stage mapping** (spec `02` §B against what exists; R-IDs approximate, by the register's workstream column, cross-cutting R62 and R65 omitted):

| Stage / workstream | GDT items | R-IDs | State |
|---|---|---|---|
| W11a, stage 2 (contract, tests, ROADMAP, briefs, review, conflicts, checkpoints, state, grills) | GDT-1..9, GDT-13 | R01-R13, R15-R20, R22-R23, R26-R27, R29-R51, R61, R63, R66, R69 | built, released |
| W11a to W11b, stage 3 (completion gate, demonstrator) | GDT-10, GDT-14 | R14, R21, R24-R25, R53-R56, R64, R67 | built, released (GDT-14 at `c047d20`) |
| W7 | GDT-11 | R52, rest of R28, R41, R55 | not started (superseded by §7: built 2026-10-24) |
| W12, stage 4 | GDT-12 | R57, R59, R60, R68, rest of R38, R58, R62, R65, R70 | not started (superseded by §8: built 2026-10-25) |

- **Release points** (`~/.maestro/release.log`, `announced:`): `13c9add` (GDT-0..6 and stage-1 fixes, 10-18 00:08 IDT),
  `adda0cb` (GDT-7), `74aaf95` (GDT-8), `b400a47` (GDT-9), `ef74d2b` (GDT-10),
  `3b1815a` (GDT-13 and the GDT-10 follow-ups, with the W6 merge, 10-22 12:10 IDT),
  `c047d20` (GDT-14, its review fixes and the GDT-13 lows, 10-22 18:00 IDT).
- **Sizes.** Since GDT-0 (`d5c621c`) the branch has 104 non-merge commits to `c047d20`. Against `dc1e560`, `maestro/` is +15,985/-479
  lines over 74 files; with tests and docs it is 218 files. New modules: `acceptance.py`, `goal_roadmap.py`, `goal_conflicts.py`,
  `goal_authority.py`, `goal_store.py`, `goal_milestones.py`, `goal_replan.py`, `goal_state.py`, `goal_completion.py`,
  `workflows/goal_guard.py`, `workflows/goal_progress.py`.

## 3. Code and docs

| Item | What it is | Commits (red, then green) |
|---|---|---|
| GDT-0 | requirements and register in repo | `d5c621c`, `3f7c49e` |
| GDT-1 | contract v2, revisions, ids, migration from v1 | `23b3503` (design), `1c3b7d9` |
| GDT-2 | draft view vs execution gate, coverage | `c8635e9` |
| GDT-3 | acceptance binding, five-state baseline, safety | `66d6972`, `b8b1015` |
| GDT-4 | contribution, ROADMAP proposal, feasibility | `8f389e4` |
| GDT-5 | role-specific goals excerpt, critical lines never cut | `eabac1e` |
| GDT-6 | six impacts, evidence rules, goals guard, R58 | `72bb0f7`, `c3c45f7` |
| Stage-1 fixes | review rounds on 1-6 (env/option parsing) | `d44d617`..`0c7cdac` |
| GDT-7 | conflicts, decisions, authority matrix | `750c05d`, `2a2cca9`, `b15966c`; review `9f883bb`; lows `50345b2` |
| GDT-8 | modified-B replan, milestone checkpoints, holds | `63f9c5b`, `a4f8bce`, `d9a6466`; review `e1d4e6f`; lows `66a8c3c` |
| GDT-9 | durable goal state (store v2) | `b99edc3`, `d33e6ff`; review `f9c2236`, `fedff9c`; lows `afa9845` |
| GDT-10 | completion gate and outcome report | `da68347`, `8e5f100`; review `6f6501b`; follow-ups `3de2c8e` |
| GDT-13 | grill surfaces and parity fixture | `858736c`, `88f8e2d`, `751be59`; review `46445dd`; lows `f73df36`, `f7f1f63` |
| GDT-14 | Recipe Box demonstrator (S1 `d29e5ab`/`d327dc4`, S2 `f52cfea`/`bb2770d`, S3 `4942be8`/`7133261`) | review fixes `a1f7fd4`..`c047d20` (released) |
| Close-out | the verify Low and the doc fixes (on the branch, after `c047d20`) | fix `1c59a47`; TRACEABILITY `223fca9`; the register row and this report follow |

- **Close-out fix (verify Low, `gdt14-verify.md`).** `goal_guard._added_files` and `_added_lines` never read git's exit code, so a failed
  diff was an empty result: a skip added to a bound test passed unseen when the per-file diff failed. Both now go through one `_diff` helper that raises
  on a non-zero exit, and `tamper_findings` turns that into `goals_guard_diff_unavailable`, the same finding as for `changed_paths` None.
  Red `gdtclose-red.txt`: `tests/test_goal_guard.py::test_a_failed_added_files_or_added_lines_diff_is_a_finding_not_an_empty_result` 2 failed (one case per diff, 60 passed);
  green `gdtclose-green.txt`: 62 passed.
- **Close-out doc fixes (`223fca9`).** Every `green (… part)` row that has a remainder says so and names its owner (R28, R41, R55 -> W7/GDT-11; R38, R58, R62, R65, R70 -> W12/GDT-12);
  R20 names both its parts and says there is no remainder; R65's items list now names GDT-6 and GDT-8, whose parts the row cites; the legend drops the unused
  `qualified`/`merged` values (§4 records both per item); the S3 row's old "6 reuse, 3 limits" tally is marked superseded by 24/4/4/1.
- **Docs.** `docs/DESIGN.md` §19.1-§19.12 describes what is built, one subsection per item. Each landed with its code.
- **Grill surfaces (GDT-13).**
  - Canonical Drive folder `gdrive:ChatGPT/grill-me-framework/` (also the ChatGPT paste-ready path): `SKILL.md`, `PLAN-FORMAT.md` and `GOALS-FORMAT.md` were pushed in place with the same ids.
    The first two went 2026-10-21 and `GOALS-FORMAT.md` again 2026-10-22 for the lows.
    Live ids and times are in `tests/fixtures/grill_surfaces/SOURCES.json`.
  - `~/agent-skills` commits: `504b232` (contract first), `5591e43` (review fixes), `a588a24` (verify lows), `253cdf9` (setup step 4 gives each kept task a `scope:`; re-vendored into `maestro/skills/{claude,codex}`).
  - Parity: `scripts/grill_surfaces.py check` reported "clean, 8 files" after each push (`gdt13lows-check.txt`, `gdt14fix-check.txt`).
    `tests/test_grill_surfaces.py` checks that the Drive-style and skill-style contracts parse equal, that every contract field is listed with its default, and that each grill settles the contract before design.
- **Demonstrator (GDT-14).** `tests/fixtures/goals/demo/` (Recipe Box), `tests/goals_demo.py`,
  `tests/test_goals_demo.py` and `tests/graph_engineering/test_goals_demo_lanes.py`.
  Lane A runs a RED baseline to `complete` and writes a provenance bundle (`maestro-goals-demo-provenance/1`).
  Lane B runs a maintenance regression that is reworked, plus a G1-vs-G3 conflict that goes to Dan while independent work merges.
  Product fixes it surfaced: `acceptance._direction` (negated oracles), `goal_state._label`, fork-point scope and guard diffs, `goal_roadmap.SCOPE_HINT`.

## 4. Verification

All commands used `.venv/bin/python -m pytest -n 8` with temp dirs under `~/.cache/maestro-gdt`. Narrative evidence notes are archived in
`~/maestro-evidence/gdt-evidence/` (outside the repo).

- **RED then GREEN.** Every item has a red run before code, logged in TRACEABILITY §4. Examples:
  GDT-1 34 failed then 76 passed; GDT-3 `test_acceptance.py` 2 failed then 326 passed focused;
  GDT-7 wiring 18 failed then 310 passed; GDT-10 45 red then 1650 root and 216 graph passed;
  GDT-14 review fixes 27 red then 1871 root and 372 graph passed; the close-out fix 2 red then `test_goal_guard.py` 62 passed. GDT-1..6 were reviewed together in the stage-1 rounds; GDT-7..10, GDT-13 and GDT-14 each had a review and a verification pass, with findings fixed test-first.
- **Full suite history.** Baseline `dc1e560` 6516 passed. GDT-2 6607, GDT-4 6649, GDT-6 6706, stage-1 6776, GDT-7 6877, GDT-8 6928, GDT-9 6972, GDT-10 7644 (`a702323`).
  With W4 merged: GDT-13 `f99fa38` 7785 passed.
- **Latest full suite, at `c047d20`** (`gdt14-full2.txt`, 44m55s): **7888 passed**, 20 failed, 14 errors, 25 skipped, 1 xfailed.
  Every failure is accounted for by a retained rerun or a known class:
  - W4 classes, 18 failures and 2 errors: `test_codex_driver` 5, `test_native_ownership` 7, `test_agy_runtime` 2, `test_codex_auth_freshness` 2,
    `test_codex_auth_lifetime` 1, `test_codex_backend_app_server` 1, `test_native_legacy_timeout` 2 errors.
    Earlier logs show the same classes failing on root `fb646ca` without GDT code (`root-fb646ca-w4.txt`).
  - `tests/test_cli.py`, 1 failure and 12 errors: `orchestrator.main() did not return within 120s` under load.
  - `graph_engineering/test_audit_script.py`, 1 failure: the documented 30 s subprocess load flake; it passes alone (earlier alone-runs: `gdt3-audit-rerun.txt`, `gdt13-full2-rerun-audit.txt`, `gdt13-full3-rerun-audit.txt`).
  - **Isolated rerun, retained** (`gdt14-full2-rerun.txt`: `tests/test_cli.py` plus the codex app-server and codex auth lifetime tests): 124 passed, 2 failed.
    `test_cli.py` and the app-server tests pass alone, so the `test_cli.py` failures and the app-server failure were load. The only failures are the two
    `test_codex_auth_lifetime::test_killed_controller_queues_successor_until_native_pid_namespace_is_gone` cases (`[complete]`, `[generated]`), a known `test_codex_auth_*`
    W4 class that fails on root without GDT code; the handoff lists it.
- **Focused runs at the close-out tip.** Root `gdt13-root-files.txt` + `test_goals_demo.py` + `test_affected_validation.py`: 1873 passed (`gdtclose-root.txt`; 1871 at `c047d20` plus the 2 new cases).
  Graph `test_goals_demo_lanes.py` + `test_safety_gate.py`: 26 passed (`gdtclose-graph.txt`). The full suite was not rerun for the close-out.
- **Demonstrator.**
  - Verify (`gdt14-verify.md`, 89f4cd7..c047d20): verdict merge. Demo lanes 26 passed (120 s under load).
  - **Provenance in its final form, rerun at the clean tip `223fca9`** (lane A twice with `MAESTRO_DEMO_EVIDENCE` set, `gdt14-demo/final-run-{1,2}`, `final-diff.txt`,
    `gdtclose-prov-{1,2}.txt`, call 30.3 s and 29.2 s): the two `demo_provenance.json` bundles are identical after `goals_demo.without_volatile`
    (it drops `generated_at`, `ran_at`, `at`, `commit`, `introduced`, `merge_commit`, `finish_id`, `question_id`). The bundle carries the review fields:
    `fixture.tree` `3f15ce9…` with `clean: true`, `maestro.gate` (commit `c047d20`, clean, the root checkout's interpreter), `decisions`, and `independence` with two overridden claims.
    The two derived outputs, `goals_status.json` and `goal_outcome.md`, differ raw (run ids, timestamps, commit hashes inside text, the temp path) and are identical once those are masked.
    This replaces the S2 pair (`run-{1,2}`, `bb2770d`), which predated the review fixes.
  - In a linked worktree the venv's editable install resolves `maestro.acceptance` to the root checkout, so gate checks there never exercise
    a worktree-only change to it (DESIGN §19.12). `maestro.gate` in the bundle records which Maestro the gate imported.
- **Regression and conflict cases.** C-map (TRACEABILITY §5, checked against `goals_demo.C_MAP` by `test_every_c_scenario_maps_to_a_test_or_a_named_limit` and `test_traceability_s5_says_what_the_c_map_proves`):
  24 C-ids with demo tests (C1-C9, C11-C16, C18, C19, C21-C25, C28, C30), 4 reuse-only (C10, C17, C26, C27), 4 partial (C20, C29, C31, C32), 1 limit (C33)
  (superseded by §7 and §8: 29 demo, 4 reuse, 0 partial, 0 limit).
  Covered: conflict escalation in both priority orders (R08/R48), bounded-accept decisions not re-asked (R50), hard invariant never traded (C23),
  tamper blocked by the guard alone (C13), `serves: []` regression (C18), empty queue with a RED criterion is `incomplete` (C30), and `02` §D bullet 9 on Recipe Box (Q1 modified-B; the bounded final repeat gate asks Dan at the cap).
- **Resource and prompt effects.** R65: the goals excerpt per role grew from 418 characters to 2045-2402, under the 4000 bound, no new role (`gdt5-size-{before,after}.txt`).
  The gate reruns only GREEN criteria plus the task-named ones, not every test. Demo lane runs are 17-30 s each depending on load.
  W12's measured before/after effects (R59) are not built, so there is no cost or time-to-outcome measurement
  (superseded by §8: built, `maestro/improvement_metrics.py`).

## 5. Remaining

**Tally (TRACEABILITY §2):** 70 R-IDs. 57 green with no remainder, 8 green with a named remainder, 5 planned
(superseded by §7: 61 / 5 / 4 after GDT-11; by §8: 70 / 0 / 0 after GDT-12).
Nothing else is open: the work this chain owns (GDT-0..GDT-10, GDT-13, GDT-14) is released. What remains is GDT-11 (W7) and GDT-12 (W12), plus the named limits below
(superseded by §7 and §8: both are built; only the named limits remain).

| R-ID | Status | Owner / stage | What is missing | Depends on |
|---|---|---|---|---|
| R52 | planned (superseded by §7: green) | GDT-11, W7 | duplicate findings become one polish task; moot ones declined (C29) | W7 core, not started |
| R28, R41 | green for GDT-6 (the gate guard blocks regressions on every task, `serves: []` included; superseded by §7: green, no remainder) | GDT-11, W7 | the polish side keeps invariant and global checks | W7 core |
| R55 | green for GDT-10 (corrective loop and cap; superseded by §7: green, no remainder) | GDT-11, W7 | polish loop-back from W11b; C31 partial | W7 core |
| R57 | planned (superseded by §8: green) | GDT-12, W12 | proposal set and weekly or project-end schedule | W2 ledger (merged), W10 digest (not checked) |
| R59 | planned (superseded by §8: green; C33 demo) | GDT-12, W12 | before/after digest: effects, false holds, cost, time-to-outcome; C33 limit | W10 digest |
| R60 | planned (superseded by §8: green) | GDT-12, W12 | decline memory, resubmit only on new evidence, measurement and rollback fields | W12 analyst |
| R68 | planned (superseded by §8: green; C20 demo) | GDT-12, W12 | a ledger action never changes a past verdict; C20 partial | W2 ledger is append-only today |
| R58 | green for the minimal part (GDT-6; superseded by §8: green, no remainder; C32 demo) | GDT-12, W12 | evidence-based attribution; C32 partial | R57..R60 |
| R38, R62, R70 | green for GDT-6/GDT-8 (superseded by §8: green, no remainder) | GDT-12, W12 | W12's use of the six impacts; W12 ledger migration; attribution for no bogus Q1 | W12 |
| R65 | green for prompt size and incremental selection (GDT-5, GDT-6, GDT-8; superseded by §8: green, no remainder) | GDT-12, W12 | measured effects | W12 |

Order from the plan: W12 goes last (Dan, 2026-10-17); GDT-11 only if the Claude chain reaches W7 before Codex does and quota allows,
otherwise W7's builder takes it. Neither is started and neither is claimed (superseded by §7: GDT-11 is
built; only GDT-12 remains; by §8: GDT-12 is built).

**Named limits (none block anything released):**
- Replan-appended tasks carry no `scope:`, so they run alone (owner GDT-8; DESIGN §19.12, R66).
  Narrowed 2026-10-24: diagnosis tasks declare their cause file and run beside other work; only
  a feasibility replan's missing tasks and the completion gate's corrective tasks still run alone.
  Closed 2026-10-25 as accepted: a declared scope is enforced, so it must cover every file the
  task writes, and nothing names those files for either kind. Corrective tasks run on an empty
  queue, so they serialize only among themselves. Running alone costs throughput, never
  correctness (DESIGN §19.12).
- The live-LLM grill interview cannot be automated; the parity contract stands in for it (R10).
- C20 and C32 (W12) and C29 and C31 (W7) are partial and C33 (W12) is a limit, each with its owner in TRACEABILITY §5
  (superseded by §7: C29 and C31 are demo; C20 and C32 stay partial; by §8: C20, C32 and C33 are demo, so no partial or limit is left).
- Gate subprocesses in a linked worktree resolve `maestro.acceptance` through the venv's editable install to the root checkout (test-environment note; DESIGN §19.12).
- The two `test_codex_auth_lifetime` cases fail on root without GDT code (W4, Codex's owner).
- Recorded 2026-10-27, not fixed (W4, Codex's owner; each needs its own reviewed product change; DESIGN §19.14):
  `codex_runtime._omission_inventory` walks the masked `~/.claude`/`~/.codex` surfaces recursively and raises at
  20,000 entries or on a directory that changes mid-walk; about 8,000 today and growing, so every Codex prepare will
  eventually fail, and each prepare spends 3-9 s on it (production keeps walking the real-home roots; suggested fix:
  a names-only or depth-limited record). The 60 s queued-reservation deadline still counts worker interpreter startup
  (80-130 s seen at load 25).

**Decisions awaiting Dan:** none. The close-out commits on the branch (the `goal_guard` fix `1c59a47` changes live gate code, so merging it is an
outward change that the post-merge hook releases to every adopted project) go to root under the standing root-merge authorization in `AGENTS.md`
and "GDT releases without asking", by the coordinator.

## 6. Handoff hygiene

- Drive `ChatGPT/agent-handoffs/maestro-goal-driven-tdd-2026-10-17/`: removed. The `rclone lsf` of `agent-handoffs/` on 2026-10-22 shows only `podcast-scraper-ep74-audit-2026-10-20/`, which is not ours and was left alone.
- Drive `ChatGPT/server-exchange/gdt13-trial`: purged (not in the 2026-10-22 listing).
- Drive `ChatGPT/grill-me-framework-backup-2026-10-20/`: kept (7 `.docx`, the pre-push copy; `GOALS-FORMAT` did not exist then).
  **Recommendation:** keep it until Dan has run one real grill from the new Drive text in ChatGPT, then list it for cleanup.
  It is the only pre-change copy on Drive. The old skill text is in `~/agent-skills` history, so deleting it later loses nothing. I have not deleted it.
- Only the narrative notes (reviews, designs) are kept, archived outside the repo in `~/maestro-evidence/gdt-evidence/`; the raw run outputs were deleted. TRACEABILITY §4 cites them by file name.
- The W11a worktree inside the checkout: remove once the close-out commits are on root (done by the coordinator, not handed to Dan).

## 7. Addendum 2026-10-24: GDT-11 (W7) built

The Claude chain built W7 core and its goal-driven parts on the branch, in three test-first slices
(design and builder notes `~/maestro-evidence/gdt-evidence/gdt11-design.md`; DESIGN §19.13):
S1 the record side (`7580013`/`be5574d`: the backlog, `before_dependents`, routing), S2 triage and
the polish stage end to end (`5f2d3e6`/`4ee94d5`), S3 the goal-driven parts and the demonstrator
(`b0ef887`/`f4402ae`: R41 `named: []` for `serves: []`, leftover items deferred in the outcome,
demo lane A's polish stage, new lane C). Evidence in TRACEABILITY §4.

- **Requirements.** R52 is green; R28, R41 and R55 have no remainder. The tally is now 70 R-IDs:
  61 green with no remainder, 5 green with a named remainder (all W12/GDT-12: R38, R58, R62, R65,
  R70), 4 planned (R57, R59, R60, R68, W12). What remains is GDT-12 (W12) only (superseded by §8:
  70 green, none remaining).
- **Scenarios.** C29 and C31 move from `partial` to `demo` (lane A: 3 tasks, 5 follow-ups with 2
  duplicates and 1 moot → 1 polish task that breaks a protected criterion and reworks; lane C: polish,
  the finish round, polish again, Dan at the cap). C18 also names lane A. Tally: 26 demo, 4 reuse,
  2 partial (C20, C32), 1 limit (C33) (superseded by §8: 29 demo, 4 reuse, 0 partial, 0 limit).
- **Live change on release.** Every adopted graph project stops getting an immediate `<id>-followups`
  task per accepted task; follow-ups wait for the end-of-project polish stage (only a
  `before_dependents` one runs early). Already-queued `-followups` tasks run unchanged. The release
  message must say so in one sentence.
- **Limits (DESIGN §19.13).** A project whose queue never empties never polishes (nothing is lost); a
  wrong "moot" drops a fix (keep when unsure, the reviewer checks every moot reason, Dan sees each);
  no item cap per polish task; already-queued `-followups` tasks in a format-2 project stay held and
  are listed as deferred; graph loop only. In a linked worktree the gate's `python -m maestro.backlog`
  imports the root checkout's install, so the polish lanes set `PYTHONPATH` (harmless after the root
  merge, which the W7 completion needs before any release).
- **Root merge.** S1-S3 merge to root together, after review, under the standing root-merge
  authorization (by the coordinator). The P13 spec's §W7 boxes (root, gitignored) are the
  coordinator's to tick.

## 8. Addendum 2026-10-25: GDT-12 (W12) built

The Claude chain built W12 core and its goal-driven parts on the branch, in four test-first slices
(design and builder notes `~/maestro-evidence/gdt-evidence/gdt12-design.md`; DESIGN §19.14): S1 the ledger
side (`abdb432`/`ff4c396`: mapping version 2, attribution by evidence as appended amendments, the
evidence pack), S2 measurement (`eb181cd`/`1329e14`: `maestro/improvement_metrics.py`), S3 the
analyst and its proposals (`f425c95`/`3f70eab`: `maestro/improvement.py`, decline memory, the
question), S4a Dan's decisions applied, rolled back and measured, and the graph wiring
(`1fc5cb0`/`7e6cb03`, neighbour lane `883af53`), S4b the demonstrator and an empty run that keeps
its window (`d789f6e`/`7199ba6`). Evidence in TRACEABILITY §4.

- **Requirements.** R57, R59, R60 and R68 are green; R38, R58, R62, R65 and R70 have no
  remainder. The tally is now 70 R-IDs green, none with a remainder, none planned.
- **Scenarios.** C20, C32 and C33 move to `demo` (Recipe Box with a real ledger: W12's actions
  leave every accepted verdict, the registry, ROADMAP, goals store and outcome report
  byte-identical; a W11a row and AC-1's real miss read `unattributed`, and a declined proposal
  returns only on new evidence; an approved lever's effect measured before and after, cheaper
  with an escaped defect `worse`). Demo lane A runs one project-end analysis and its bundle names
  it. Tally: 29 demo, 4 reuse (C10, C17, C26, C27), 0 partial, 0 limit.
- **Live change on release.** In every adopted graph project the first idle tick runs a weekly
  analysis over the whole history, and each queue exhaustion a project-end one. A project with no
  pattern sees no call and no message. With a pattern: one strong-route analyst call and up to 3
  proposals to Dan, which hold no work; approving a config lever edits `~/.maestro/config.yaml`
  (reversible, backed up), approving a code lever writes an intake report into Maestro's checkout
  (a mailbox file, not a commit, so not a release). The release message must say so in two
  sentences.
- **Review fixes (2026-10-27, DESIGN §19.14 "Review fixes").** The weekly analysis waits while
  work is runnable; a failed rollback is recorded and told; a stale approval never lowers a
  strength (refused under the config lock); the rollback restore holds the same lock; an
  outranked unanswered proposal question is withdrawn (at most 3 open) and holds-False questions
  never take unthreaded text; the diagnosis gate reads only the first line; intake ids carry a
  path hash (`I-<slug>-<8 hex>-IP-n`). Evidence in TRACEABILITY §4.
- **Limits (DESIGN §19.14).** The analyst can block the poll loop for up to 3 × 300 s, only at an
  idle moment with nothing runnable; nothing processes Maestro's intake automatically (a queued code lever waits for a
  Maestro session and is measured from the first release naming it); effects are measured in the
  origin project only while a config lever applies everywhere; five-task samples are noisy and no
  verdict acts by itself; graph loop only.
- **The W10 seam.** W10's digest is not built. W12 sends its own per-run message and prints
  read-only JSON (`python -m maestro.improvement report|evidence`) whose numbers equal `maestro
  report usage`; W10 embeds it later.
- **Root merge.** S1-S4 change live behaviour only together, so they merge to root as one W12
  completion, after review, under the standing root-merge authorization (by the coordinator).
  The P13 spec's §W12 boxes (root, gitignored) are the coordinator's to tick.
