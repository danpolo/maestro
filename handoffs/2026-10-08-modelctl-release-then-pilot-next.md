# Release the modelctl D-A–D-E work, adopt it for DuetFlow, resume the pilot

> **2026-10-08 (later):** steps 1–3 are committed (Maestro 0c7eb92, modelctl 61c9de0). Continue from
> `handoffs/2026-10-08-modelctl-release-continue-next.md`: 2 owner-suite failures block the release.

## Goal

Dan approved three steps on 2026-10-08. Do them in order:

1. Commit the qualified Maestro D-A–D-E work and integrate it into
   `feat/graph-engineering-foundation`.
2. Make the first commit of the modelctl repo.
3. Materialize a Maestro release from that commit and adopt it for **DuetFlow only**, at its
   idle HALTed boundary.

Then resume the DuetFlow P12 pilot. The release unblocks it because its catalog routes
`gpt-6.1-sol`.

After the pilot is running, the next modelctl task is the **simple Telegram approval flow**
described in the section "Next after the pilot resumes" below. Dan chose release first and
the approval flow second.

These approvals cover only steps 1–3 and the pilot resume. They do **not** cover:
- the live modelctl rollout (first `modelctl scan`/`consume`, installing the scan timer);
- changing `~/.maestro/current`;
- push or merge to master;
- adopting the release for any project other than DuetFlow.

## Read first

- Root `AGENTS.md` and the Maestro auto-memory index. These memories apply especially:
  modelctl-approvals-are-meta-telegram, model-pins-class-level-auto-switch, do-cleanup-yourself,
  agent-scratch-not-in-tmp, context-management-not-built, parallel-tasks-must-merge-trivially.
- `docs/plans/2026-10-07-modelctl-owner-applied.md`: the applied owner patch and the current state.
- `docs/plans/2026-09-30-modelctl-integration.md`, section "Dan's decision changes,
  2026-10-01" (D-A–D-E), and `docs/plans/2026-10-07-modelctl-class-transitions.md`.
- `handoffs/2026-10-07-modelctl-session13-application-ready.md`: gate evidence and the
  evidence directory E.
- `handoffs/2026-10-07-duetflow-pilot-hold-next.md`: the pilot snapshot, why it is on hold,
  the resume steps and what to report.
- `docs/MODEL_TRANSITIONS.md` in the worktree: the operator contract for the new design.
- STATE.yaml `_p12_open_threads` (gitignored, `docs/graph-engineering/STATE.yaml`). The first
  thread is the DuetFlow hold; the modelctl-integration-review thread has an UPDATE dated
  2026-10-08.

## Verified state (2026-10-08)

- **Maestro candidate:** worktree `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`,
  branch `feat/modelctl-integration`, HEAD a40cafc. 62 paths are uncommitted: modified files
  plus new `maestro/model_{classes,commands,detection,inventory,lifecycle,policy}.py`,
  `docs/MODEL_TRANSITIONS.md` and `docs/reviews/`.
  - Session13 serial gate: Maestro 5537 passed / 1 xfailed, exit 0.
  - Its `maestro/backends/catalog.py` codex row lists
    `gpt-6-astra, gpt-6.1-sol, gpt-6-sol, gpt-6-luna, gpt-5.6-luna`, with gpt-6.1-sol strong
    at 1.0 and gpt-6-sol in a superseded set.
  - E = `<worktree>/.superpowers/sdd/2026-09-30-modelctl-integration/`. Check whether the repo
    tracks `.superpowers/`; don't force ignored evidence into Git.
- **modelctl repo** `/home/dan/projects/modelctl`: branch master has **no commits at all**.
  - Many files are staged (`A`/`AM`): the pre-existing staged index, preserved by design.
  - The 19 applied files are working-tree edits or untracked files.
  - Owner suite after applying: 197 passed (`E/session13-applied-final.json`).
- **Main checkout** `feat/graph-engineering-foundation` at **c3f8fd4**, which this pilot line
  committed for the manifest. It holds Dan's five older, unstaged model-switch edits:
  `maestro/backends/catalog.py`, `maestro/limits.py`, `maestro/templates/project.yaml.tmpl`,
  `tests/backends/test_catalog.py` and `tests/test_limits.py`.
  - **All five differ from the worktree's versions.** Example: the main checkout's codex row
    drops gpt-6-sol and **replaces gpt-5.6-luna with gpt-5.6-terra** (272K/140K). The
    worktree keeps both gpt-6-sol and gpt-5.6-luna.
  - Untracked and to be preserved: `artifacts/graph-engineering/p12-pilots/duetflow.json`,
    `docs/GRAPH_ENGINEERING_ARTIFACT.md`, `docs/reviews/`.
- **Versions:** none newer than f13b2c7 under `~/.maestro/versions`. The global pointer is
  still 2f555c28; leave it.
- **DuetFlow:** HALTed, pinned to f13b2c7, idle. The 2026-10-07 snapshot is in the hold
  handoff; re-check it. Pins were committed in 6c0e66c.

## Steps

1. **Reconcile the five main-checkout edits** before integrating. They are an older, partial
   version of the same model switch.
   - Diff each one against the worktree.
   - Where the worktree already covers an edit, the worktree version wins. Keep the
     pre-integration copies under `handoffs/` for audit.
   - **Ask Dan once**, with a recommendation, only where they truly disagree. The known case
     is gpt-5.6-terra vs gpt-5.6-luna in the codex catalog. Bundle every disagreement into
     that one question.
   - After integration, the main checkout must hold no leftover unstaged model-switch edits.
2. **Commit the worktree** on `feat/modelctl-integration` in logical commits. Then integrate
   it into `feat/graph-engineering-foundation`; a fast-forward or merge is fine because both
   sit on a40cafc plus c3f8fd4.
   - Run the full serial gate on the integrated HEAD: the worktree's gate command from
     session13 evidence, ~400 s. It must pass with the same counts or explain any change.
   - Don't push. Don't touch master.
   - Remove the worktree and branch yourself afterwards (do-cleanup-yourself), once
     evidence E has been preserved somewhere that persists.
3. **First commit of the modelctl repo.**
   - Check `.gitignore`, and that no secrets or `.env`/tokens would be committed.
   - Commit the staged baseline plus the applied 19 files. Two commits are clearer: the
     pre-existing staged index first, then "D-A–D-E owner changes".
   - Re-run the owner suite (197) with both Maestro source variables pointing at the
     **integrated** Maestro HEAD.
4. **Release and adopt for DuetFlow only.**
   - Materialize the integrated SHA into `~/.maestro/versions/<sha>` the same way earlier
     releases were made. Pattern: `handoffs/2026-09-30-operator-intake-release/`
     (`install-at-boundary.sh`, `adopt-at-boundary.py`, candidate self-test and integrity
     checks). Copy and adapt those scripts into a new dated release directory; don't rerun
     the old ones.
   - Adopt through `selfupdate.adopt(project_repo=/home/dan/projects/duetflow)` at the idle
     boundary.
   - Check that DuetFlow's `.orchestrator/current` names the new SHA and that the global
     pointer is unchanged.
   - D-A: DuetFlow's exact pins (implementer claude-sonnet-5-5 / gpt-6-luna; judge and
     diagnoser claude-opus-5-5 / gpt-6.1-sol) become class choices. See how the release
     treats an explicit exact pin; `docs/MODEL_TRANSITIONS.md` says fresh literals resolve
     by family. Change DuetFlow's `project.yaml` only if the release requires it, and fold
     that question into the step-1 question if possible.
   - Without spending anything, check with the release's own code what a strong DuetFlow
     node binds to. Expected: gpt-6.1-sol or claude-opus-5-5, **not** gpt-6-sol.
   - Record a `code_under_test` amendment (f13b2c7→new SHA, criteria_or_weights_changed false)
     in `artifacts/graph-engineering/p12-pilots/manifest.json` and commit that file alone.
5. **Resume the pilot.** Follow steps 2 and 4–6 of `handoffs/2026-10-07-duetflow-pilot-hold-next.md`:
   - re-snapshot;
   - pinned doctor exits 0;
   - clear HALT;
   - start exactly one controller via DuetFlow's `launch.sh` workflow, checking host processes
     as well as tmux windows;
   - monitor without idle polling;
   - for D33 `/ask`, give Dan the exact bot, message and expected reply.
   Report everything in that handoff's "Verification to report back" list.
6. Update the STATE.yaml threads (modelctl-integration-review, DuetFlow hold, D33) and the
   status header of `docs/plans/2026-09-30-modelctl-integration.md` with the commits, release
   SHA and adoption.

## Next after the pilot resumes: simple Telegram approval flow (Dan, 2026-10-08)

Dan's requirement for every future modelctl finding replaces the "review the 19-file patch"
style of approval. He must never be asked to read code to approve a model transition. What
modelctl touches, Maestro included, must be **simple, predictable and deterministic**: a
model transition is configuration, never code. His approval is a **meta step** of three
decisions, all answerable from **Telegram**:

1. **Use the new model?** modelctl detected it. Approve or decline using it.
2. **Which models does it replace, in which roles?** For example, "gpt-6.1-sol replaces
   gpt-6-sol for judge and diagnoser". Buttons per role, with a recommended default.
3. **Fill its `model_context_limits.md` row.** This is usually **not** a plain approval: for a
   new model the row is normally empty. modelctl asks Dan **for each missing cell, one by
   one**, and shows the suggested value or a related model's value where one exists. A cell
   that already has a value isn't asked again.

Everything after those three answers happens automatically:
- writing the limits row;
- publishing the shared policy revision;
- adoption at each project's next idle boundary;
- notices;
- old-row retention and pruning.

There must be no code edit, no release and no patch review per new model.

Implications for whoever designs it:
- Check what in the released D-A–D-E design already does this. MODEL_SET publication,
  automatic adoption at the idle boundary and `modelctl add` with lifecycle inputs all exist.
  Build only the missing Telegram interaction and per-cell row filling on top.
- Any model judgement still hardcoded in `catalog.py` (strength, cost, available_models) has
  to move into data that those three answers set. Otherwise a new model would still need a
  code change and a release, which is exactly what happened with gpt-6.1-sol.
- Telegram buttons follow the existing D29/D30 ask-Dan pattern: options, a recommendation,
  free text.
- This is a design task with real latitude. Give it its own session. Use grill or
  brainstorming only where the D-A–D-E docs don't already decide the question. Then use TDD
  with a lane test.
- The live modelctl rollout (first scan, installing the scan timer through a one-script sudo
  step Dan runs) goes with this task, not before it.

**Parallel session:** once steps 1–3 are committed, Dan starts
`handoffs/2026-10-08-modelctl-telegram-approval-flow-next.md` in parallel. That session never
touches DuetFlow. Commit manifest amendments promptly so its rebase onto
`feat/graph-engineering-foundation` stays trivial.

## Out of scope

- Context Gate.
- Adopting the release for other projects, and the global pointer.
- Push or merge to master.
- The post-P12 feature queue beyond what's listed here.
- Spotify writes beyond what accepted tasks do.

## Verification to report back

- How the five-file reconciliation was resolved, and Dan's answer if he was asked.
- The commit SHAs: Maestro worktree commits, the integrated HEAD and the modelctl commits.
- Gate counts and exit codes for both suites.
- Release SHA and path. DuetFlow pin before and after. Global pointer unchanged.
- The manifest amendment commit.
- What a strong DuetFlow node now binds to.
- Then the pilot items: doctor exit, HALT-clear time in Israel local time, the single
  controller's identity, the first run id and outcome, and any D30/D31/D33 evidence with
  file paths.
