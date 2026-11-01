# Maestro — Goal-Driven Autonomous Execution: Agent Handoff

**Date:** 2026-10-17  
**Authority:** User-approved requirements from ChatGPT conversation on 2026-10-17.  
**Scope:** Maestro's current development plan + actual implementation; canonical `ChatGPT/grill-me-framework` + setup skill + portable ChatGPT grill/paste-ready workflow.  
**Status:** Requirements approved by user; not a claim they have been implemented.  
**Source baseline:** `03-ORIGINAL-W11A-BASELINE.md` is the exact original W11a proposal (Decided/Open/Later status as of 2026-10-17).

## Read ALL four files, in order

1. `00-START-HERE.md` — execution procedure and definitions.
2. `01-FULL-BEHAVIOR-CONTRACT.md` — **normative comprehensive behavior specification**: original behavior retained, all audit fixes and optimizations, project-level TDD, and mandatory user adjudication of goal conflicts.
3. `02-REQUIREMENT-TRACEABILITY-AND-VERIFICATION.md` — **mandatory** exhaustive traceability register and test/acceptance matrix. A requirement cannot be omitted merely because it is P2 or a later workstream.
4. `03-ORIGINAL-W11A-BASELINE.md` — original proposed behavior and current status. Read it to preserve existing work and capture intentional changes.

The revised user requirements take precedence over superseded original rules. Preserve the original behaviors that are not explicitly modified. Do not interpret priorities (P0/P1/P2) as permission to omit P1 or P2: **all requirements are approved and required**. Priorities only determine delivery order and workstream ownership.

## Task to execute in the CURRENT Maestro development process

1. Inspect the live Maestro repository, branch, git working tree, active workstreams W11a/W7/W11b/W12, ROADMAP, decisions, implementation, regression tests, and current agent/session coordination rules. Do not assume the snapshot in `03` is current; reconcile it with reality. Protect concurrent work and the user's unrelated changes; do not overwrite. Avoid paid or destructive operations without approval.
2. Read the actual canonical Google Drive `ChatGPT/grill-me-framework/SKILL.md` and **only its relevant format/support files**. Read the current Maestro setup skill and ChatGPT paste-ready grill version. The folder is canonical; neither this handoff nor a repo copy replaces it.
3. Turn each requirement ID in `02` into a concrete plan entry: existing/completed, modify, add, depends on, verification, and workstream/stage. Update the **existing** Maestro development plan and milestones rather than create a competing plan. Account for W11a being in progress and W7/W11b/W12 originally later. Address all requirements; do not silently defer any indefinitely.
4. Implement in dependency-safe stages using the repository's current TDD practices. Write failing tests before code changes where appropriate. Add project-level outside-in TDD test generation as a feature of Maestro/grill, and test the feature itself. Prefer deterministic validation, scripting, and targeted tests to extra model calls. Preserve both graph and legacy paths while supported by the repository.
5. Revise both **grill** interfaces (canonical Drive skill and ChatGPT portable/paste-ready equivalent), the persisted GOALS/PLAN contract, setup conversion into ROADMAP tasks, all applicable agent role briefs, review/decision machinery, milestone checks, goal gate/completion, and W12 telemetry/improvement.
6. Record explicit design choices only when necessary. The user's decisions in these files are already approved. You may autonomously decide implementation internals within scope, but must ask the user for material scope changes, changed goal acceptance criteria, goal-versus-goal conflicts, hard-invariant exceptions, paid/destructive actions, or other user-reserved decisions. When asking, show evidence, trade-offs, alternative options, and your recommendation.
7. Maintain an evidence-backed check-off matrix from `02`. For each requirement, record implementation location, evidence/test IDs, pass/fail status, and remaining blockers. Do not claim complete until every entry is resolved according to its explicit acceptance rule. For future workstream items not yet executable, include them as explicit, scheduled, owned tasks with verifiable acceptance criteria—never quietly exclude them. User requested implementation of ALL, including later and optimization elements.
8. Verify code, fixtures, migrations, backwards compatibility, failure modes, prompt-size impact, goal-ID traceability, evidence fidelity, security/approval boundaries, and the RED→GREEN acceptance-test lifecycle. Return a concise report of completed changes, staged work, tests, outstanding user decisions, and links/paths. Do not report test success without test output.
9. After consuming these temporary Drive handoff files **and preserving the durable requirements in the repository/plan and canonical grill docs**, remove temporary Drive handoff copies via your Drive integration if permitted (the user's one-time handoff hygiene preference). Do not delete the canonical grill files, user files, or copies still needed for audit before verification. If deletion unavailable, report exact paths for cleanup. Do not delete the originating local repository copy.

## Non-negotiable principles

- Goal intent becomes an approved **verifiable product contract**, not simply prose.
- **Outside-in project TDD:** executable high-level acceptance tests from grill/setup lead Maestro's gap-filling plan. Local RED is a meaningful product behavior failure, never a broken harness or a fabricated failure. Existing passing behavior stays GREEN.
- **Inner TDD:** agents still use normal unit/integration TDD while progressing outer acceptance tests.
- **Goal conflicts ALWAYS go to the user for decision** with evidence, alternatives, and recommendation. Goal priority informs advice, never grants automatic authority to sacrifice another goal.
- Scope bound **D23**: goals explain task purpose but never individually authorize task scope expansion. Maestro may create/adjust tasks through its authorized planning procedure, not via an implementer's spontaneous feature creep.
- Review findings are observations; deterministic policy decides. Evaluation evidence and telemetry do not silently change verdicts.
- A correct task is not reworked solely due to a bad plan. Verified unacceptable regression is protected against even in `serves: []` tasks.
- Maintain durable per-goal, per-scenario, per-test, per-task traceability through revisions and session compactions.
- Minimal user interruptions; only genuine product-level choices, trade-offs, privilege/cost boundaries, and material uncertainty. Independent work should continue during holds.
- Efficient models, tokens, CPU and external resources: reuse existing gates/roles when possible; no gratuitous extra LLM loops or blanket E2E after every tiny task.

## Definitive user decisions and superseded original behaviors

- The user APPROVED every recommendation made in the audit, including additional optimizations; P2 is mandatory, not optional.
- The user ADDED project-level initially-failing behavioral acceptance tests derived from every automatable goal and final-product scenario, starting during grill and materialized/validated during setup; those tests guide Maestro's work.
- The user ADDED mandatory human adjudication of ANY real conflict between approved goals or progress on one requiring deterioration of another, with an agent recommendation.
- Original Q1 is RESOLVED as **modified option B**, evidence-based replanning, not a rigid "two tasks" rule.
- Original `no_progress => always fault: planner` is REPLACED with evidence-sensitive contribution classification and attribution.
- Original `serves: [] => no check at all` is REPLACED: skip direct advancement expectation only; preserve invariant/global regression checks.
- Original `doctor always warns, never fails` is REPLACED with draft-time advisory / execution-time strict validation for required contract integrity.
- Original `one diff line proves goal movement` is REPLACED with outcome-appropriate evidence and an `unknown` option.
- Original priority deciding which goal "wins" is REPLACED with priority as input to a user-approved conflict decision. Already-agreed explicit trade-offs need not be re-asked unless new facts exceed approval.
- Original late-only goal verification is EXTENDED with milestone checkpoints, and W11b stays authoritative at finish.

## External references for engineering judgment (secondary, not normative)

- OpenAI, *Using Goals in Codex*: https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex
- OpenAI, *Rethinking skills and prompts for GPT-6 Astra*: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- Anthropic, *Effective harnesses for long-running agents*: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Anthropic, *Demystifying evals for AI agents*: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents

These links contextualize the design; this handoff's user-approved requirements and the live repository are the operative source of truth. If a reference has moved or is inaccessible, proceed with the requirements without fabricating source claims.
