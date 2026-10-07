# Continue modelctl integration — after session 6 (notice transport, item 10 gaps, real interactive run)

> **2026-10-01 — Dan changed the design.** Read `docs/plans/2026-09-30-modelctl-integration.md`
> § "Dan's decision changes, 2026-10-01" (D-A … D-E) first. It supersedes conflicting text
> here, including the "only partly" answer below. Re-plan the remaining work around it before
> running the item 11 final gate.

## Goal and read first

Finish the approved shared model policy/routing/adoption feature: item 11 (fresh independent
whole-branch review, then the full candidate gate in a named TMUX window). Read, in order:
root `AGENTS.md`, `docs/plans/2026-09-30-modelctl-integration.md`,
`handoffs/2026-09-30-modelctl-implementation-milestone-next.md` (the "Built so far",
"Modelctl owner seam" and "Scope and execution guidance" sections still apply verbatim),
`handoffs/2026-09-30-modelctl-session2-next.md`, `handoffs/2026-09-30-modelctl-session3-next.md`,
`handoffs/2026-10-01-modelctl-session4-next.md`, `handoffs/2026-10-01-modelctl-session5-next.md`,
then this file. Read the tail of E/progress.md: the three "Session 6" entries describe this
session in full.

Worktree (do not recreate it): `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`,
branch `feat/modelctl-integration`, base/HEAD `a40cafc`. All work is **uncommitted**.
No commit, push or merge is authorized. Evidence and ledger: E =
`.superpowers/sdd/2026-09-30-modelctl-integration/` in the worktree.
Python: `/home/dan/projects/maestro/.venv/bin/python`. The worktree has no `.venv`.
Always `cd` into the worktree with an absolute path. A `cd` into the main checkout
changes the session's working directory.

Standing constraints (unchanged from session 5):
- No Context Gate work.
- No live model switch, deployment, daemon restart, paid inference, pilot continuation or HALT clearing.
- The modelctl owner patch is approved and applied. Do not rerun its apply script or ask again.
- Missing-row registration stays disabled pending owner hardening (item 8).
- The main checkout's five uncommitted owner files and three untracked paths belong to
  someone else. Leave them alone and edit only the worktree copies.
- No live `maestro models` switch on this host. Do not edit DuetFlow's `project.yaml`
  (a parallel pilot session owns DuetFlow).

## Done in session 6 (details and logs in E/progress.md)

1. **Item 4 leftover: proposal notice transport (done).**
   - Dan chose the host's `send-to-me` over Maestro's per-project Telegram on 2026-10-01.
     The reason: `maestro models consume` runs from modelctl's scan timer, where no project
     `.env` is loaded.
   - `model_detection.send_to_me` is wired into `maestro models consume`.
   - Retry-safe: a failure or missing binary keeps `notification_pending`, the next consume
     retries, and the proposal is never recreated.
   - A proposal decided before delivery is not announced.
   - Tests: `tests/test_model_proposal_notice.py`.
2. **Item 10: gaps closed.**
   - Bug fixed: `reconcile_proposals` trusted a revision file. It now trusts only the
     current → `previous_policy` chain.
   - Bug fixed: `project_report` reported a malformed `project.yaml` as an unreadable
     adoption. It now has a separate `config` field.
   - Bug fixed: doctor printed "not adopted" twice.
   - New evidence: a malformed project does not block a switch.
   - New evidence: a contended publication blocks on the policy lock, then refuses its
     stale preview by CAS.
3. **Genuine interactive run (E/interactive/).**
   - The real `maestro models` picker ran in tmux with real modelctl Codex discovery
     (read-only listing, no inference) and the real host provider tables.
   - It used a disposable policy home and disposable projects, now removed; the evidence
     was archived first.
   - Outcomes: the inheritor and pinned projects were updated (pins preserved); the active
     project reported `upgrade_required`, which is truthful because the installed
     `~/.maestro/current` lacks the reader.

Session 6 gate:
- `-n auto` full suite in the worktree: **5422 passed, 1 xfailed, exit 0, 161 s**
  (E/session6-full.log/.exit). That is 5410 plus the 12 new tests.
- Count only the progress lines:
  `grep -E '^[.sxFE]+( +\[ *[0-9]+%\])?$' log | tr -cd '.' | wc -c`.
- The fingerprint was unchanged after the gate.
- `git diff --check` was clean.
- The preservation script exited 0.
- The main checkout status was unchanged.
- The worktree `.orchestrator/` still holds only its 3 files, and `~/.maestro/models` does not exist.

## Findings to report to Dan (recorded, not fixed)

1. The live `~/.codex/model_context_limits.md` no longer has a GPT-6 Sol row. Under this
   branch, DuetFlow's explicit `gpt-6-sol` pins report as invalid, and a pinned launch
   would be refused.
   **Resolved 2026-10-01:** with Dan's approval, DuetFlow's pins now name `gpt-6.1-sol`
   (see `handoffs/2026-10-01-duetflow-pilot-resume-next.md`). The pilot runs in a parallel
   session that owns `/home/dan/projects/duetflow`; do not touch it from here.
2. Under an adopted policy, graph routing drops `codex:gpt-6-astra`. The catalog has no
   verified native Astra limits, so the Codex base window (400K) is below its provider
   ceiling (450K). The fix needs verified Astra `ContextLimits`, which is an owner/data
   decision. Never clone values.
3. **Owner follow-up:** two handoff tests in modelctl's `tests/test_maestro_outbox.py` inherit
   the real PATH. Run against a Maestro source that has the transport, they would send a real
   `send-to-me` notice. **Until the owner stubs it, always run the owner suite with a recording
   `send-to-me` stub first on PATH** (session 6 did this: 135 passed, see E/notice-owner-*.log).

## Dan's question (2026-10-01): will this branch make model transitions automatic?

Answer: **only partly**. Today's DuetFlow problem would recur in four places after
rollout unless the following are addressed. Verified against code; nothing here is built.

1. **Explicit pins never follow a switch.** This is by approved design: overrides stay
   explicit and are reported as invalid, never rewritten (the session 6 interactive run
   shows it). DuetFlow pins every role, so it gets nothing automatically.
   - Fix: give DuetFlow inheritance (`implementer: {}` and so on), with Dan's OK, when it
     adopts the release.
2. **A row disappears before Maestro has switched.** modelctl keeps one table row per
   tier. Registering a newer model drops the older model's row at once, but sets no
   `removed` signal; `core/scan.py` sets `removed` only when the account listing stops
   showing the model. From that moment until Dan approves the proposal:
   - shared routes and pins on the old model are invalid;
   - graph routing drops the model;
   - pinned launches are refused.
   This is exactly the item 8 owner hardening: refuse to remove rows that registered
   projects or active runs still reference, using Maestro's dependency inventory. It is
   not built, and it must be built (or the window accepted) for transitions to be seamless.
3. **A brand-new model needs a Maestro release.** A destination without a verified native
   profile in `backends/catalog.py` is refused ("verified native profile unavailable",
   `model_policy.py` around line 444). Today's `gpt-6.1-sol` and the Astra window gap are
   examples. Detection creates the proposal, but it cannot be approved until a release adds
   the profile. Removing that dependency means taking native limits from modelctl
   metadata (deferred item 7).
4. **A project's pinned release must be upgraded to read shared policy.** Until it adopts
   a release that has the reader, it reports `upgrade_required` and keeps its own catalog.
   DuetFlow on f13b2c7 is in this state.

Ask Dan whether 2 and 3 become follow-up work after item 11. They change scope, so the
decision is his.

## In scope next

- **Item 11:** a fresh independent whole-branch review. A reviewer agent is allowed by
  executing-plans. Address its findings with TDD.
- Then the full candidate gate in a named TMUX window:
  - freeze the source and inputs (hash manifest);
  - isolate the operating pointer;
  - capture the required doc inputs, as in the milestone gate.
- Then re-run `.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`
  from the main checkout.
- Update `docs/plans/2026-09-30-modelctl-integration.md`'s status paragraph to point at
  this handoff chain.

## Out of scope

- Item 8: missing-row registration stays disabled.
- Fixing findings 1 and 2: they are Dan's or the owner's call.
- Editing modelctl's checkout.
- The merge with the owner's main-checkout edits. See the merge note in the session 5
  handoff: keep inheritance, keep the Codex column with GPT-6.1 Sol, keep `gpt-6-sol`
  pin-only, and leave the terra swap to the owner.
- Live rollout, commit, push and merge.

## Verification to report back

- The reviewer's findings and how each was resolved, with RED/GREEN logs for any fix.
- The TMUX gate: counts (progress-line method), exit code, duration, and the frozen-manifest check.
- The preservation script's exit code, `git diff --check`, whether the worktree `.orchestrator/`
  is unchanged, and whether `~/.maestro/models` exists (it must not).

## Suggested skills

executing-plans, spec-code-review (whole-branch review against the plan),
receiving-code-review, test-driven-development, monitor-long-running-tasks (TMUX gate),
verification-before-completion, close-session, handoff.
