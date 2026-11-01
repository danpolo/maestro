# GDT-1: `docs/GOALS.md` contract format v2 (implementation design)

Internal design within the approved contract (`01` §2, §3.1, §8, §11). Markdown stays the
source format (R02, `01` §2.3). Labels are matched loosely like v1 (bold, `—` for `:`, `*`
lists, numbered headings) so ChatGPT-written files parse (R12).

```md
# Goals
Format: 2
Contract revision: 3 (approved 2026-10-17 by Dan)

## Purpose                      (<= 2 sentences: a guidance note above 2, not a problem)
## Users                        (who, and what each does)
## Goals                        (3-7 preferred; any count valid, R03)
### G1: <name>
- Outcome: <human-readable outcome>
- Rationale: <why>
- Baseline: <value> | not_applicable
- Target: <threshold/target>     (absent → open question, never invented, R05)
- Verified by: AC-01, AC-02, OP-01
- Constraints: INV-01
- Depends on: G2
## Final product                (walkthrough prose, then scenarios)
### S-01: <name>
- Goals: G1, G2
- Kind: normal | error | boundary
- Given: … / When: … / Then: …
- Criteria: AC-01 · Operator checks: OP-01
## Acceptance criteria
### AC-01: <name>
- Goals: G1 · Scenarios: S-01
- Given/When/Then (behaviour only, no internal paths unless contracted, R15)
- Oracle: <observable success condition> · Threshold: <if any>
- Layer: e2e | integration | unit · Evidence: test | metric | trace
- Fixtures / Environment / Data: <safe, deterministic, R19>
- Test: `<command or test id>`  (filled by setup; absent before setup)
- Open question: <only when the oracle cannot be derived, R05/C10>
## Operator checks
### OP-01: <name>
- Goals · Instructions · Expected observation · Sign-off: Dan
## Non-goals
## Hard invariants
- INV-01: <property> (checked by AC-xx)       (never traded, R51)
## Trade-offs
- TO-01: <bounded, approved degradation>; decision <ref>; bound <limit>; expires <cond>
## Priority                     (input to recommendations, never a resolution, R08)
## Authority
- Autonomous: … · Ask Dan: … · Retries: … · Stop a failed approach when: … · Blocker: … · Channel: telegram
## Change history
| Rev | Date | Change | Rationale | Impact | Approved by | Supersedes |
## Retired ids
- G4 (rev 2): <reason>          (never reused, R04)
```

Every field above, with whether its absence is a `gate` or `draft` problem and what the parser
takes instead, is `goals.CONTRACT_FIELDS`; the Fields table on every grill surface is
generated from it (GDT-13, DESIGN §19.11).

Parser rules:

- No `Format:` line → format 1 (the six-section W11a file). It parses with the W11a result
  unchanged plus one `note` pointing at migration; the missing v2 parts are never gate
  failures until the project migrates. `maestro goals migrate <path>` prints (never writes) a
  v2 skeleton keeping every id and text, marking unknowns `Open question: ask Dan`, never
  inventing values, with a change-history row that has no approver, so the gate stays shut
  until Dan approves (R05, R62).
- Problems carry a severity: `note` (guidance, e.g. goal count), `draft` (warn while drafting),
  `gate` (blocks execution of the affected work, R31) and the ids they affect.
  `maestro goals check <path> [--json] [--gate]` prints `[severity] text` and exits 1 on any
  draft or gate problem (keeps W11a/setup behaviour: setup asks Dan, R62); with `--gate` it
  exits 1 only on a gate problem. Options follow the path.
  *(Deviation from this doc's first draft, which had check exit 0 on drafts.)*
- Id rules (R04): `G\d+`, `S-\d+`, `AC-\d+`, `OP-\d+`, `INV-\d+`, `TO-\d+`; duplicates, an id
  listed in Retired ids reappearing, and references to unknown ids are `gate` problems. Ids
  seen at earlier revisions are also kept in durable state (GDT-9) to catch silent removal.
- Links are bidirectional (R06, R69): every goal has ≥1 AC or OP; every scenario names goals
  and ≥1 AC/OP; every AC names ≥1 goal or scenario; orphans are `gate` problems at execution.
  A link named on one side only still counts and is a `draft` problem.
- Added while implementing: a goal appearing in no scenario is `draft`; a contract with no
  `Kind: error` scenario gets a `note`; a trade-off without a bound is `gate` (an approved
  trade-off is bounded, R50); `Mandatory: no` on an AC or OP marks it optional (W11b's
  "mandatory criteria", R53); a missing target or oracle and every open question are `gate`
  problems scoped to the ids they name, so they block only that work (R05, R66).
- Revision (R11, R67): `Contract revision: N`; a change to a goal's target/verification or an
  AC's oracle/threshold without a history row for rev N is a `gate` problem. Evidence and
  baselines record the revision they were taken at; GDT-3/GDT-10 treat older-revision results
  for changed criteria as stale.
