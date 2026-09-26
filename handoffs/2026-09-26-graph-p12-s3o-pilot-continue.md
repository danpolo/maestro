# Handoff: Graph P12, session 3o. Watch 04 graduate, then continue the pilot to the P12 verdict

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3n-integrate.md`. Still apply: s3m §3.4 (the then-steps), §4 (traps),
§5 (report format); s3j §2, §4, §5; s3l §1, §3, §4. `docs/graph-engineering/STATE.yaml` and `handoffs/` are
gitignored. **Talk to Dan in local time (IDT = UTC+3); journals and the DB are UTC.**

## 1. What s3n did (2026-09-26, ~23:00–23:50 local)
- **Merged the five s3m branches** into `feat/graph-engineering-foundation`:
  - d20 → 6c0169a, d21 → 80d88fe, contam → d5bfabb, remerge → ef266c1, tghelp → 7223126. All merged without a
    textual conflict, and a review of the overlaps (orchestrator.py launch loop vs loop-start reduction;
    cli.py/commands.py) found no semantic clash.
  - Full suite: **5063 passed, 0 failed**, 1 skipped.
  - 9a0ea5a fixes the stale "watchdog exits on HALT" docs (the `cmd_watchdog` docstring and `templates/launch.sh.tmpl`).
    DuetFlow's `launch.sh` has the same fix in 625dec9.
  - STATE.yaml: D20 and D21 are marked FIXED.
- **The project.yaml test-isolation bug s3m saw did not reproduce** in the main checkout (`git status` was clean
  after the suite). It showed up only in a worktree. It is still open, and it is small.
- **Recovered 04 (D20 live):**
  - `archive/04-scoring-run1` keeps the original approved tip 5f63b5c.
  - D20's resolver (`mergeresolve.merge_text`) was run over the three index stages of `duetflow/app.py` (main
    145a5b5 merged into `impl/04-scoring`). Per hunk:
    1. **Docstring** "Implemented so far": a true same-line conflict (base l.3–5), so the resolver refused it. It
       was combined by hand, main's items first.
    2. **Imports**: modify vs an adjacent insert. **Resolved by rule.**
    3. **`cmd_explain`** vs 03's `_owner_playlists`/`cmd_playlists`/`cmd_use_source` (base l.191): two insertions
       at one point. **Unioned by rule**, main's first.
    4. **Subcommand registration** (base l.223): two insertions. **Unioned by rule**, main's first.
  - Then, in the worktree: V1 1/1, V2 7/7, the full DuetFlow suite 197 passed. Merge commit **fe5059a** is on
    `impl/04-scoring`, and the worktree is removed.
  - Against main, the branch touches only 04's six files; none is in `risky_set`.
- **DuetFlow `scope:`** was added to every open task (including 04) in **550cd5a**, a ROADMAP-only commit.
  - 02-followups and 03-followups overlap (spotify/collector/db/app), so they serialise. 05 is disjoint from both.
  - DuetFlow has no ROADMAP CAS (`roadmap_cas.json` absent), so the hand edit does not drift-block anything.
- **Adopted maestro 9a0ea5a** into DuetFlow. Doctor: OK + the 2 known WARNs (systemd_unit, meta_branch push).
  Manifest amendment **f442245**.
- **`maestro ctl remerge 04-scoring`**: `run_hN6LPQuShbwkOIEn` (stays failed) → **`run_R43Kpp154fyR6z1w`**,
  revision 2. It carries gate, implementer and proof_review; only merge and accept re-run.
- **Installed maestro-setup skill updated.** It gained the scope field, the scope paragraph, "`mode`, `deps` and
  `scope`" and item 5 (list only direct deps). Committed in the candidate worktree and `~/agent-skills` (4d40abe).
  The Claude and Codex runtimes both see it. There is no `agent-skills` CLI on PATH, and it was not needed.
- **Telegram `/help`**: the new watchdog came up with HALT present ("answering Telegram until it is removed").
  **Dan sent `/help` while halted and got a reply (live-verified).** The halted reply writes no journal line.
- **Relaunch** at 23:41 local (HALT removed; Dan approved "removing HALT relaunches the loop"). Order of events:
  1. D21 commit **c28111c** removed 02-followups→02, 07→03 and 08→06.
  2. 04 resumed on run_R43Kpp154fyR6z1w. Only **merge + accept** ran (attempts 29→31, **no model usage**), and the
     merge was trivial (no `merge_resolved_deterministic`). Integrated as 4f4585a at 23:42.
  3. Graduation **f11bdfb** (docs only) queued **04-scoring-followups (3 follow-ups)**, then `push_skipped_no_remote`.
  4. **D20 live at 23:45:** **05-selector and 02-collector-schema-followups opened in parallel** (disjoint scopes).
     03-followups and 04-followups got `launch_skipped_write_overlap` on duetflow/app.py and wait. The queued
     04-followups task already carries a scope.

## 2. In scope, in order
1. Watch 05 and 02-followups (running since 23:45 local), then 03-/04-followups. Per accept: graduation is docs
   only, `-followups` queued, the map pushed, `review_origins` recorded, fresh `quota` rows, contamination.
2. **New small gaps (fix in-session):**
   - The halted `/help` answer should journal (`telegram_answered_halted`) so it is observable.
   - The worktree-only project.yaml test-isolation bug (s3m: `test_confinement.py` / `characterization/test_selfheal.py`).
3. Continue per s3m §3.4:
   - follow-ups of 02/03/04, then 05+;
   - 06: collect, plus the first live V-DAN with numbered steps;
   - archive before any rerun;
   - final export with contamination (untracked `p12-pilots/duetflow.json`);
   - retention re-measure, qualification re-run, verdict, close P12.

## 3. Traps
- s3m §4 still applies. Also: the `ctl` verbs open the controller lock. Run them only while halted.
- **The watchdog no longer exits on a ctl HALT** (a bot is configured). Removing HALT relaunches the loop within
  ~30 s. To stop everything, stop the watchdog tmux session / unit.
- A task without `scope:` now blocks every other launch while it runs (D20 is conservative).
- Hand off again at ~120K context.

## 4. Report back
As s3m §5.
