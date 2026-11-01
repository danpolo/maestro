# P13 W2 qualified source and completion contract

Scope: ALL authorized W2 telemetry, complexity, extra-step capture, per-fault
reporting and read-only CLI. Authority: root P13 W2, B10/B20, AGENTS.md and
`p13-w2-remainder.md`. Executable requirement coverage is in
`p13-w2-proof-inventory.md`; runtime/report contracts are in `../W2_USAGE_REPORT.md`.

## Verified source

Isolated branch `feat/p13-w2-usage` includes the completed W3/W8/W9 base `33a170d`.
Final source: `89e0817cff72bb9aa2daa190dd781d585d4dc7ad`, parent
`38cc3a4167dffda9a0d9221481fafeacbb22ec55`, tree
`d9c1c38041b3d81ff2ca25c79d5873f6c11341fa`.
Tasks 1–3 are verified complete through the canonical execution ledger.

Qualification results (the raw logs were deleted; narrative notes are archived outside the repo in `~/maestro-evidence/w2-remainder-evidence/`):

| Gate | Result | Time | Exit |
|---|---|---|---|
| `final-source-candidate` (frozen working snapshot) | 6510 passed, 6 skipped, 1 xfailed | 229.51s | 0 |
| `final-source-committed` (clean exact source) | 6510 passed, 6 skipped, 1 xfailed | 230.36s | 0 |

Both have no warnings and test/hash/status/diff/index exits 0. Read-only
`verify-final-source-candidate.log` and `verify-final-source-committed.log` establish
exact fingerprints, parent, 11-path source commit, inventory content, unchanged saved
P12 fixture, root `62f12a0` clean, other-owner refs preserved and W2 outside root.

## Independent review resolution

`final-independent-review.md` covers the complete `33a170d..38cc3a4` range plus its
frozen final working snapshot, including original W2 files. It found 0 Critical,
2 Important, 0 Minor and no decision gap. The original failed review is retained;
`final-review-resolution.md` records both closures and the independent review's limits.
There was no repeated broad review.

- F1: real CLI empty output and three-turn malformed endpoint tool calls now retain
  own-role `garbage_output`; worker failure kinds/verdicts remain unchanged.
  RED 2 failed; GREEN 107 passed, 22.89s, exit 0.
- F2: normal ROADMAP shell and staged checks retain observed missing executable and
  pytest-no-tests causes, while ambiguous commands remain unknown. RED 11 failed,
  2 safeguards passed; combined GREEN 350 passed, 37.86s, exit 0.
  Absolute shell-wrapper safeguard RED 1 failed; GREEN 14 passed, 5.26s, exit 0.
  Wider regression: 1075 passed, 105.34s, exit 0. No warnings in these green gates.

## Final docs-only completion

Task 4 owns this documentation, DESIGN/EXECUTION, the remainder plan and proof
inventory; ignored root planning updates are restricted to W2 spec/progress.
No W2 STATE thread exists, so STATE and every other owner's rows remain unchanged.
The final docs commit must have exact source parent `89e0817`, an explicit docs-only
allowlist, frozen `completion-candidate` and clean exact `completion-committed`
full suites, and linked-hook/source/owner preservation proofs. Its SHA and measured
results belong in root `handoffs/2026-10-15-p13-w2-complete.md` after they exist;
they are not self-referential edits to this candidate. Archive the completed execution
workspace and its review packages before removing only that plan's workspace.

## Retained limits

The saved P12 fixture proves one accepted DuetFlow run, not whole-P12/ITV history.
Historical complexity, fault, price and acceptance/goal evidence remain unavailable.
Quota deltas are account observations, never summed consumption; costs stay within
quota pools and partial measurements retain coverage. W5's 5h admission is unbuilt,
so production quota/rate/endpoint allocation retains unknown admission.
W6/W11/W13 have capture seams only, with no product behavior or planner forecast.
Fixture/runtime qualification is not a live W2 pilot. Root integration, release,
push, live changes and Context Gate are outside this completion.
