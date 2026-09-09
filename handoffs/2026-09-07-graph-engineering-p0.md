# Maestro graph evolution: architecture complete, P0 entry point

## Goal and authorization

The architecture-writing task is complete. The user requested an independent synthesis of the entire local research packet into one concrete architecture and implementation plan, with a separate research-gap file. Runtime implementation, deployment, service changes, commits and pushes were not requested or performed.

If the next session is authorized to begin implementation, start with **P0: isolated baseline fixtures and measurements**. Do not begin by replacing the orchestrator or building a repository index. The main document contains the authoritative proposal and each phase's files, dependencies and acceptance criteria; it is a proposed evolution, not a description of existing functionality.

## Read first

1. [Architecture and implementation plan](../docs/graph-engineering/MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html), especially current-system grounding, P0, migration guardrails and experiments. This is the single main plan; do not create a competing summary or duplicate architecture document.
2. [Research blind spots](../GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md), especially B01/B02/B18 for baseline evidence.
3. [Progress record](../docs/PROGRESS.md), the 2026-09-07 entry and existing readiness caveats; then the applicable parts of [DESIGN](../docs/DESIGN.md) and [EXECUTION](../docs/EXECUTION.md).
4. [Packet index](../docs/graph-engineering/README.md) only if source details are needed. All eight original packet files were read completely during the architecture session. Prior syntheses and candidate plans were treated as input, not authority. No additional external research was conducted.

The HTML and blind-spots file were both delivered to Dan's private inbox with successful `send-to-me` exits. The HTML is intentionally **gitignored** by `.gitignore:13` (`docs/graph-engineering/`). It exists locally and was delivered; do not force-add the research packet or assume it will exist in a remote-only checkout. Establish document availability before starting elsewhere.

## Scope of the next implementation slice

In scope, once implementation is authorized:

- P0's scratch-only lane fixtures and fixed task/acceptance examples, grounded in existing characterization behavior.
- Baseline metrics that distinguish observed outcomes, usage attribution and unknowns; fixed task weights and reproducible inputs.
- Safe second-project setup and real-task rehearsal with the operator-authorized scratch project: **DuetFlow** (`/home/dan/projects/duetflow`, based on `~/projects/project-ideas/DuetFlow-README.md`), recording raw per-task outcomes honestly to close the first half of B01. (Phase P12 qualification will pair DuetFlow with **instagram-to-value** under daemon/queue safeguards).
- A failing behavioral test for each new invariant, focused validation, and the required full-suite phase closure.

Out of scope for P0:

- Runtime authority cutover, live-project experimentation, deployments, service changes, or speculative graph infrastructure.
- Broad new research, automatic paid API fallback, unqualified model rankings or invented subscription cost estimates.
- Direct SymDex indexing or privileged commands. Follow Dan's contained-indexing and single privileged-script rules if those become relevant in a later authorized slice.
- The unrelated watchdog changes and portfolio/watchdog handoffs listed below.

## Decisions to preserve

See D01–D08 and the mode/routing sections in the plan for full contracts. The core direction is a native local durable workflow executor, distinct configured agent roles, deterministic project-policy/task compilation, exact backend/model/effort bindings, and shared ownership/validation rules with genuinely different paid-efficiency and free-quality policies. Repository graphs and affected-check skipping have separate qualification gates. Neither opaque-agent exact replay nor model consensus is an acceptance guarantee.

Supporting technology must be free for normal operation; existing coding-agent subscriptions are the only assumed paid services. Missing evidence stays in the separate blind-spots file. The main plan's future experiments do not authorize a broad research pass.

## Verification already completed

- HTML structure: valid nesting, 30 unique IDs, all 30 links resolved in the checkout, 13 implementation phases, four figures with five diagram views, no placeholder markers, zero external assets.
- Blind-spots file: 18 explicit records, B01–B18.
- Offline browser inspection: desktop 1280×720 and mobile 390×844; the complete primary overview fits in the initial viewport, with no page horizontal overflow. Desktop SVG label bounds passed. All four figures were visually inspected; dark mode, navigation, enlargement, Escape and focus return worked. No browser errors or external resource loads were observed.
- `git diff --check`: exit 0. Existing dirty files retained their original SHA-256 hashes. No runtime tests were run for this documentation-only task; all runtime test commands in the plan are future acceptance requirements.
- `send-to-me` for each deliverable: exit 0. The dedicated browser session was closed.

Temporary verification evidence, if still present: `/tmp/maestro-graph-artifact-audit.json`, `/tmp/maestro-graph-desktop-final.png`, `/tmp/maestro-graph-mobile-final.png`, `/tmp/maestro-graph-policy-expanded.png`, `/tmp/maestro-graph-recovery-expanded.png`, `/tmp/maestro-graph-sequence-expanded.png`. The plan and this record are durable; the temporary images are not required to understand the decisions.

## Next-session verification to report

For P0, report lane coverage, fixture/task counts, actual focused/full-suite pass/fail/skip counts and exit statuses, plus measured calls/outcomes/unknown usage values. Prove fixtures cannot reach a live project, credentials, provider or service. Use the exact commands and regression triggers in P0; do not claim a mock backend establishes real-model quality. Before any eventual cutover, P1's transaction and duplicate-command invariants must pass.

## Workspace and remaining work

Baseline branch: `master`, HEAD `798cc412c814842df8b4a381d615e8177f16038d`. At close, the cached `origin/master` comparison was 0 behind / 0 ahead; no network fetch was performed. This session made no commit or push.

Session-authored files:

- Main HTML plan (ignored), root blind-spots file (untracked), this handoff (untracked), and the appended progress entry.

Pre-existing work, left unchanged:

- `maestro/watchdog.py` — modified; SHA-256 `c6b53997351f8e1c65cfe3a0f893c197e2c0fb5b0ffffc5b3a8f74b9093d3508`.
- `tests/characterization/test_watchdog.py` — modified; SHA-256 `5b7aee82292cdf2298392584bd02884be27ea3dc87b908ad3cde7ddb96e2861e`.
- `handoffs/2026-09-04-github-portfolio-preparation.md` — untracked; SHA-256 `d29879fa2e155d754e4ba77b4400e60c03e18e5f0a2f321d9d777fafa2aaa63f`.
- `handoffs/2026-09-05-abuali-watchdog-duplicate-launch.md` — untracked; SHA-256 `06c11e1dbf04306309d766a738df6720c19a52b167cd7b8e70485edea197ca81`.

No architecture-writing blocker remains. P0–P12 are proposed future implementation work with no invented duration estimate; research uncertainty and existing readiness caveats are recorded in the plan/blind-spots file. Existing unrelated work should be resumed from its own handoff only when requested.

## Session lifecycle and suggested skills

The conversation underwent automatic compaction. Per Dan's lifecycle rule, the bounded verification/delivery work was finished and the session closed at this boundary. No exact active-context counter or session-progress tool was available; no usage measurement is inferred.

Start a fresh Codex session from this handoff for implementation. Apply current instructions and relevant execution-quality skills: writing-plans only to break down the authorized P0 slice, test-driven-development, verification-before-completion, and worktree isolation if needed. Use visual-explainer when changing the architecture artifact. Do not repeat design approvals already supplied by Dan, and do not infer runtime/deployment authorization from the completed architecture-writing request.
