# Goals: Ledger

Format: 2
Contract revision: 2 (approved 2026-10-17 by Dan)

## Purpose

Ledger records household spending from bank exports. It exists so Dan can see where the
money went each month without a spreadsheet.

## Users

- **Dan**: imports exports and reads the monthly summary.

## Goals

### G1: Imports every export

- **Outcome:** every bank export Dan drops in the inbox becomes ledger entries.
- **Rationale:** a missing import makes every total wrong.
- **Baseline:** not_applicable
- **Target:** every CSV export from the two banks imports with no manual edits.
- **Verified by:** AC-01, AC-02
- **Constraints:** INV-01

### G2: Monthly summary is right

- **Outcome:** the summary page shows the month's totals by category.
- **Rationale:** the summary is what Dan reads.
- **Baseline:** not_applicable
- **Target:** the monthly totals match the bank statements to the shekel.
- **Verified by:** AC-03, OP-01
- **Constraints:** INV-01
- **Depends on:** G1

### G3: Fast enough

- **Outcome:** a year of exports imports while Dan waits.
- **Rationale:** Dan re-imports a year when a bank changes its format.
- **Baseline:** 42 seconds for a year of exports (2026-10-01).
- **Target:** under 10 seconds for a year of exports.
- **Verified by:** AC-04
- **Depends on:** G1

## Final product

Dan drops the month's exports into the inbox folder, opens the summary page and sees the
month's totals by category. A broken export is named on the page and nothing is imported
from it.

### S-01: Monthly import and summary

- **Goals:** G1, G2
- **Kind:** normal
- **Given:** two valid exports in the inbox
- **When:** Dan opens the summary page
- **Then:** the month's totals by category appear
- **Criteria:** AC-01, AC-03
- **Operator checks:** OP-01

### S-02: A broken export

- **Goals:** G1
- **Kind:** error
- **Given:** an export with a truncated last line
- **When:** the import runs
- **Then:** the page names the file and no entry from it is stored
- **Criteria:** AC-02

### S-03: A year at once

- **Goals:** G3
- **Kind:** boundary
- **Given:** a year of exports in the inbox
- **When:** Dan imports them
- **Then:** the import finishes while he waits
- **Criteria:** AC-04

## Acceptance criteria

### AC-01: Valid exports import

- **Goals:** G1
- **Scenarios:** S-01
- **Given:** the two sample exports from each bank
- **When:** the import runs
- **Then:** every row is a ledger entry
- **Oracle:** entry count equals row count; amounts equal to the agora
- **Layer:** e2e
- **Evidence:** test
- **Fixtures:** tests/acceptance/data (anonymised samples)
- **Test:** `pytest tests/acceptance/test_import.py::test_valid`

### AC-02: Broken export is refused

- **Goals:** G1
- **Scenarios:** S-02
- **Given:** an export with a truncated last line
- **When:** the import runs
- **Then:** an error names the file and nothing is stored
- **Oracle:** zero entries from that file; the error names it
- **Layer:** e2e
- **Evidence:** test

### AC-03: Totals match the statement

- **Goals:** G2
- **Scenarios:** S-01
- **Given:** one month of sample exports and its statement totals
- **When:** the summary is built
- **Then:** each category total equals the statement
- **Oracle:** exact equality per category
- **Layer:** integration
- **Evidence:** test

### AC-04: A year imports in under 10 seconds

- **Goals:** G3
- **Scenarios:** S-03
- **Given:** a year of sample exports on the reference machine
- **When:** the import runs three times
- **Then:** the median wall time is measured
- **Oracle:** median below the threshold
- **Threshold:** 10 seconds
- **Layer:** e2e
- **Evidence:** metric

## Operator checks

### OP-01: Summary reads right

- **Goals:** G2
- **Scenarios:** S-01
- **Instructions:** open the summary for last month next to the bank statement.
- **Expected:** every category total is the same.
- **Sign-off:** Dan

## Non-goals

- No budgeting or alerts.

## Hard invariants

- INV-01: no entry is ever stored twice (checked by AC-01)

## Trade-offs

- TO-01: import may take up to 12 seconds while G1's new bank is added; decision D-7; bound 12 seconds; expires when the bank adapter lands

## Priority

1. G2
2. G1
3. G3

## Authority

- **Autonomous:** implementation, tests, retries, task order within a phase.
- **Ask Dan:** new or changed goals, thresholds, any goal conflict, paid services.
- **Retries:** two, then a stronger diagnoser.
- **Stop a failed approach when:** two attempts miss the same criterion for the same cause.
- **Blocker:** a decision only Dan can make, or a missing credential.
- **Channel:** telegram

## Change history

| Rev | Date | Change | Rationale | Impact | Approved by | Supersedes |
|---|---|---|---|---|---|---|
| 1 | 2026-10-10 | first contract | grill | all | Dan | — |
| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |

## Retired ids

- G4 (rev 2): CSV export of the ledger is a non-goal now.
