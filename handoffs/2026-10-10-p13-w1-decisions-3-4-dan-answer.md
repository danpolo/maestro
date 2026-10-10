# Maestro P13 W1 — Decisions 3 and 4: Model Intelligence, Cost and Global Routing

Implement the following decisions as part of Maestro's current policy/schema migration.

The objective is to establish the best initial estimates of **intelligence and relative subscription cost per task for every supported `(harness, model, effort)` pair**, then use them to select the most suitable model across all harnesses.

These estimates should be informed by published benchmarks and subscription-usage research. They will be calibrated using Maestro telemetry later.

**For now, implement the initial scores and routing policy—not new telemetry, adaptive learning, or sophisticated optimization infrastructure.**

## 1. Fundamental principles

Every `(harness, model, effort)` pair has two independent numerical attributes:

- **Intelligence:** estimated capability, initially based on the Artificial Analysis Intelligence Index (0–100).
- **Cost:** estimated relative subscription-quota consumption per task/attempt, rather than API price per token.

Keep the existing `light / balanced / strong` categories as broad capability tiers, but use numerical intelligence scores to distinguish pairs within those categories.

Suggested initial boundaries:

| Class | Intelligence |
|---|---|
| light | Below 40 |
| balanced | 40–47 |
| strong | 48+ |

The long-term objective is minimizing **quota per successfully completed task**, including retries, failures and corrections.

For now, use intelligence requirements to determine eligibility, then estimated quota cost and available quota to select a candidate.

Do not assume that one additional intelligence point represents a meaningful difference in coding-agent performance.

## 2. Decision 3 — Initial intelligence and cost tables

These are the starting values I want.

Scores based directly on Artificial Analysis are stronger evidence than the estimated values. Subscription costs remain provisional, including when derived from measured benchmark task costs.

### A. Claude Code

Cost baseline: **Opus 5.5 medium = 1.00**.

| Model | Effort | Intelligence | Cost |
|---|---|---:|---:|
| Haiku 5.5 | low | 29 | 0.09 |
| Haiku 5.5 | medium | 34 | 0.20 |
| Haiku 5.5 | high | 38 | 0.32 |
| Haiku 5.5 | xhigh | 41 | 0.50 |
| Haiku 5.5 | max | 43 | 0.90 |
| Sonnet 5.5 | low | 36 | 0.36 |
| Sonnet 5.5 | medium | 41 | 0.50 |
| Sonnet 5.5 | high | 47 | 0.90 |
| Sonnet 5.5 | xhigh | 52 | 2.05 |
| Sonnet 5.5 | max | 56 | 5.70 |
| Opus 5.5 | low | 42 | 0.42 |
| Opus 5.5 | medium | 51 | 1.00 |
| Opus 5.5 | high | 54 | 1.36 |
| Opus 5.5 | xhigh | 56 | 2.60 |
| Opus 5.5 | max | 58 | 4.50 |

Important observations:

- Opus low may be a cost-effective alternative to Sonnet medium.
- Haiku high, xhigh and max offer meaningful capability improvements over medium.
- Sonnet xhigh can exceed Opus medium in general benchmark intelligence but is not necessarily cheaper.
- Maximum effort can dramatically increase task cost for relatively small additional intelligence gains.

Don't automatically exclude expensive pairs. They should compete normally when eligible, subject to the task's requirements and available quota.

### B. Codex

Cost baseline: **GPT-6.1 Sol medium = 1.00**.

| Model | Effort | Intelligence | Cost |
|---|---|---:|---:|
| GPT-6 Luna | low | 22 | 0.04 |
| GPT-6 Luna | medium | 30 | 0.12 |
| GPT-6 Luna | high | 33 | 0.18 |
| GPT-6 Luna | xhigh | 35 | 0.29 |
| GPT-6 Luna | max | 38 | 0.46 |
| GPT-6.1 Sol | low | 42 | 0.62 |
| GPT-6.1 Sol | medium | 48 | 1.00 |
| GPT-6.1 Sol | high | 50 | 1.52 |
| GPT-6.1 Sol | xhigh | 51 | 1.86 |
| GPT-6.1 Sol | max | 52 | 3.43 |
| GPT-6 Astra | low | 46 | 2.10 |
| GPT-6 Astra | medium | 50 | 4.00 |
| GPT-6 Astra | high | 51 | 4.50 |
| GPT-6 Astra | xhigh | 52 | 6.00 |
| GPT-6 Astra | max | 53 | 8.50 |

The Astra xhigh score is approximately 52–53 across published benchmark snapshots; use 52 as the initial reference.

Important observations:

- Luna high/max may be useful for inexpensive tasks requiring greater reasoning depth.
- Sol low should compete with other balanced candidates.
- Astra is relatively expensive in Codex subscription usage, despite sometimes having intelligence scores close to Sol.
- Astra should not receive special selection priority because of its model family.
- Very expensive Astra configurations may rarely be optimal, but don't delete them merely because another pair usually wins.

### C. Antigravity — Gemini models

**Correct effort support:**

- Gemini Flash: `low`, `medium`, `high`.
- Gemini Pro: `low`, `high`.

Do not represent Gemini Pro effort as `default/high`, and do not omit Flash low.

Cost baseline: **Gemini 3.8 Flash medium = 1.00**.

| Model | Effort | Intelligence | Cost |
|---|---|---:|---:|
| Gemini 3.8 Flash | low | 33 | 0.65 |
| Gemini 3.8 Flash | medium | 40 | 1.00 |
| Gemini 3.8 Flash | high | 41 | 1.33 |
| Gemini 3.7 Flash | low | 37 | 0.60 |
| Gemini 3.7 Flash | medium | 40 | 0.90 |
| Gemini 3.7 Flash | high | 39 | 1.00 |
| Gemini 3.6 Flash | low | 27* | 0.70 |
| Gemini 3.6 Flash | medium | 31* | 1.15 |
| Gemini 3.6 Flash | high | 34 | 1.70 |
| Gemini 3.1 Pro | low | 27* | 0.95 |
| Gemini 3.1 Pro | high | 30* | 1.40 |

\* Intelligence estimates without sufficiently matched Artificial Analysis results. In particular, Gemini 3.1 Pro's score is based on a Preview benchmark, not a verified pair of Antigravity effort measurements.

The Gemini 3.7 Flash medium/high ordering reflects published aggregate benchmark estimates, including uncertainty. Do not force benchmark scores to increase monotonically with effort merely to make the table look consistent.

All Gemini subscription costs are provisional. Google's actual Antigravity quota accounting is not public enough to derive precise ratios.

### D. Antigravity — Third-party models

**Correct effort support: `low`, `medium`, `high`.**

Do not use `thinking` as the effort value. These are three distinct model-effort pairs.

Initial relative baseline: **Sonnet 5.5 high = 1.00 within the AGY third-party quota pool**.

| Model | Effort | Intelligence | Cost |
|---|---|---:|---:|
| Sonnet 5.5 | low | 36 | 0.40 |
| Sonnet 5.5 | medium | 41 | 0.70 |
| Sonnet 5.5 | high | 47 | 1.00 |
| Opus 5.5 | low | 42 | 0.65 |
| Opus 5.5 | medium | 51 | 1.30 |
| Opus 5.5 | high | 54 | 1.80 |

These are the most uncertain subscription-cost estimates.

The intelligence scores are provisional mappings from corresponding Claude API effort configurations. They are **not independently measured Antigravity harness scores**.

The costs are rough estimates based on the same task-cost and model-weight reasoning used for Claude Code, but AGY's quota accounting may differ.

Do not reuse Claude Code quota costs directly or assume identical consumption on both harnesses.

### E. Completeness and validation

The tables are initial candidate inventories.

- Verify each configuration against the actual installed CLI and model catalog before enabling it.
- The Antigravity effort combinations above reflect the currently observed UI/CLI support; preserve them and verify their invocation syntax.
- API support alone does not establish CLI support.
- Never silently substitute a different effort level.
- Preserve supported manual overrides.
- Exclude unsupported routes from automatic selection.
- Do not add deprecated model generations.
- Do not include GPT-OSS.

If a currently available relevant model-effort combination is missing, add it using a defensible initial estimate. Avoid arbitrary exclusions based on model family.

Do not fabricate measurement data or create an exhaustive model/effort Cartesian product when the harness does not support those combinations.

Preserve the existing rollout boundary: **AGY routes remain inactive until W4.**

## 3. Reasoning behind the cost estimates

We researched the relationship between reasoning effort, task cost and subscription quota.

### Effort does not have a universal cost multiplier

The original proposal used approximate factors such as:

- Low = 0.6 × medium.
- High = 1.5 × medium.

These are inadequate as universal rules.

Published benchmark measurements show effort-dependent task costs vary significantly between models.

Higher effort can produce substantially more reasoning/output tokens, increasing total cost per task even if there is no separate per-token surcharge for selecting that effort.

### Artificial Analysis

Artificial Analysis publishes:

- Intelligence Index.
- API-equivalent cost per benchmark task.
- Token usage and reasoning effort.
- Some coding/agentic performance measurements.

These are useful initial estimates because we care about expenditure for doing actual work, not only price per token.

However, **API benchmark cost per task is not subscription quota cost per task**.

Use it as supporting evidence, not as a direct currency conversion.

For example:

| Pair | Intelligence | API cost/task |
|---|---:|---:|
| Haiku 5.5 high | 38 | $0.08 |
| Sonnet 5.5 high | 47 | $0.88 |
| Opus 5.5 medium | 51 | $1.34 |
| Astra high | 51 | $1.73 |

Sonnet high being cheaper than Opus medium on this benchmark supports separating their estimated costs. It does not prove their subscription quotas follow exactly the same ratio.

### Subscription evidence

Independent studies of Claude subscription quotas suggest model-specific consumption weights differ from API pricing ratios.

For Codex, published credit averages and independent five-hour usage comparisons provide approximate cost anchors.

For Antigravity, quota accounting is less transparent, so initial values are especially uncertain.

Do not perform new measurements now.

### Reference sources

- https://artificialanalysis.ai/
- https://artificialanalysis.ai/models/releases/claude-haiku-5-5
- https://artificialanalysis.ai/models/releases/claude-sonnet-5-5
- https://artificialanalysis.ai/models/releases/gpt-6-astra
- https://artificialanalysis.ai/models/releases/gpt-6-luna
- https://zenn.dev/tksfjt1024/articles/25c0ab111c277c
- https://www.codexusage.dev/research
- https://github.com/Smartandroidtech/codex-usage-benchmark
- https://github.com/openai/codex/issues/43222

These sources explain the initial estimates. There is no need to launch a substantial new research effort.

## 4. Global routing — No fixed harness priority

**Claude must NOT precede Codex.**

Remove the fixed priority where Claude is attempted first and Codex is merely its fallback.

Maestro must select from the **entire pool of available `(harness, model, effort)` pairs**, including Antigravity after W4 activation.

Harness is an execution target, not an implicit quality ranking.

### Different quota pools

The relative costs have independent baselines:

- Claude Code
- Codex
- Antigravity Gemini
- Antigravity third-party

These baseline units are NOT equivalent.

For example, Claude cost 1.0 and Codex cost 1.0 do not mean they consume the same proportion of their respective subscriptions.

Also, verify how existing QuotaPulse usage signals map to actual Antigravity quota pools. Do not invent independent remaining-budget counters if the current subscription information doesn't distinguish them.

### QuotaPulse

We already have QuotaPulse-based quota pacing.

It tracks relative quota consumption and determines which harness has the most favorable remaining usage capacity.

Preserve this existing mechanism. Integrate it into candidate selection rather than creating a separate quota scheduler.

### Selection procedure

1. Determine the task's role, minimum capability, reasoning effort requirements, context requirements and applicable constraints.
2. Gather eligible pairs globally from all currently enabled harnesses.
3. Filter unsupported, unavailable, context-incompatible or insufficiently capable pairs.
4. Compare estimated costs within each quota pool.
5. Use existing QuotaPulse pacing to compare candidates across pools.
6. Select the best-fitting eligible model-effort pair globally.

Do not first select Claude, Codex or AGY and then optimize only inside that harness.

Do not compare raw cost figures across quota pools.

Do not introduce speculative absolute quota-conversion factors.

Use the existing routing architecture and QuotaPulse signals to implement a deterministic, understandable policy.

**Intelligence establishes eligibility. Estimated cost and available quota determine the economical choice among sufficiently capable models.**

Higher intelligence may resolve otherwise comparable candidates, but should not automatically justify substantially higher cost.

Preserve any task-specific model restrictions already shown to be necessary.

## 5. Decision 4 — Separate intelligence from effort

Choose **C: maintain separate intelligence requirements and reasoning effort preferences/constraints**.

Do not introduce an artificial fourth capability class simply to preserve planner/diagnoser effort.

Initial role requirements:

| Role | Minimum intelligence | Normal effort |
|---|---:|---|
| Implementer | 40 | medium |
| Test designer | 40 | medium |
| Reviewer | 48 | medium |
| Planner | 50 | high |
| Diagnoser | 50 | high |

### Effort semantics

For planner and diagnoser, **high is initially a minimum requirement**, preserving their existing reasoning depth.

They may use xhigh/max when otherwise justified by eligibility and cost. Do not silently downgrade them to medium merely because a medium pair meets the intelligence threshold.

For implementation, test design and review, medium represents the normal effort preference, not an absolute exclusion of better alternatives at other effort levels.

A lower-effort pair may be eligible if it meets the required capability. A higher-effort pair may also be eligible when justified.

Keep this distinction explicit in the role policy. Avoid ambiguous behavior where effort sometimes means an exact value and sometimes means a preference.

If no eligible pair can satisfy a hard requirement, use an explicit and controlled fallback.

### Broad classes versus numerical scores

Preserve the three broad classes conceptually, but don't allow them to override more precise numerical requirements.

A numerical score difference of one or two points should not be treated as conclusive evidence of superior coding performance.

Task-specific restrictions remain possible where justified.

## 6. xhigh and max effort

Support `xhigh` and `max` as normal model-effort values for harnesses that actually expose them.

For Claude and Codex, include the verified configurations in the tables.

For Antigravity, use the actual currently supported effort levels:

- Gemini Flash: low/medium/high.
- Gemini Pro: low/high.
- Third-party Claude: low/medium/high.

Do not invent xhigh/max AGY variants.

For supported pairs:

- Don't reserve xhigh/max exclusively for manually selected strong models.
- Don't exclude Haiku or Luna at max.
- Don't make xhigh/max mandatory for any role.
- Allow them to compete under the standard eligibility/cost rules.
- Keep explicit manual overrides.
- Avoid new special-purpose effort escalation systems.

Higher efforts often consume disproportionately more tokens and time for smaller capability improvements.

Their eligibility should therefore be decided by task requirements and estimated cost-effectiveness, not model-family assumptions.

Do not implement a separate latency optimization system now.

## 7. Keep the implementation simple

This phase needs:

1. A single authoritative source for pair-level intelligence and relative cost.
2. Role-level numerical intelligence requirements and effort semantics.
3. Global candidate selection across enabled harnesses.
4. Existing QuotaPulse integration for cross-pool quota pacing.
5. Correct support and validation of model-specific effort values.
6. Updated relevant tests and concise documentation.

Reuse existing Maestro abstractions wherever possible.

Avoid:

- Complex weighted utility formulas.
- Automatic intelligence calibration.
- New telemetry infrastructure.
- Dynamic effort experimentation.
- Additional quota-balancing infrastructure.
- New machine-learning components.
- Excessive confidence/provenance frameworks.
- Duplicate configuration stores.
- Compatibility work for obsolete model generations.

A brief evidence note or provisional marker for uncertain scores is sufficient.

Keep configuration and routing logic straightforward.

## 8. Future ideas — Document only, do not implement

These improvements should be considered after Maestro starts producing real telemetry.

### A. Actual subscription cost calibration

Measure real quota consumption by `(harness, model, effort)`.

Capture five-hour and weekly quota changes where available, along with task duration and token information.

Use these measurements to replace provisional relative costs.

### B. Cost per successful task

Eventually optimize:

`total quota consumed, including retries and corrections / successfully completed tasks`

A stronger, more expensive model can be cheaper overall when it avoids failures.

### C. Role-specific intelligence

General benchmark intelligence scores may not correlate perfectly with coding-agent performance.

Eventually calibrate separate capability estimates for:

- Implementation.
- Planning.
- Diagnosis.
- Review.
- Test design.

### D. Adaptive effort

Adjust effort based on task complexity, context pressure, prior failures and observed success rates.

For example, routine planning might eventually use medium, while complex diagnosis uses high/xhigh.

Do not change the initial planner/diagnoser minimum effort before evidence supports it.

### E. Better cross-pool optimization

Use measured task costs, remaining quota, reset horizons and QuotaPulse pace to improve global selection.

Avoid comparing independently normalized costs as if they were absolute quantities.

### F. Latency and context-aware routing

Eventually account for end-to-end task duration and context-limit pressure when these materially affect throughput or success.

### G. Benchmark calibration

Compare initial Artificial Analysis intelligence estimates with actual Maestro outcomes, including task-specific success rates.

These are roadmap notes, not W1 implementation requirements.

## 9. Scope, coordination and validation

Treat Decisions 3 and 4 as approved according to the specifications above.

Respect the existing P13 rollout structure and all unrelated approved boundaries.

In particular:

- Keep AGY activation under W4.
- Coordinate with any other agents concurrently modifying Maestro.
- Inspect current files before editing shared code or configuration.
- Do not overwrite concurrent changes.
- Do not perform broad unrelated refactoring.
- Do not build new measurement systems.
- Do not add obsolete models or backward-compatibility layers.
- If a required behavior falls outside the authorized implementation slice, record it for the appropriate subsequent slice rather than silently expanding scope.

### Minimum test coverage

Verify that:

1. No harness automatically receives priority over another.
2. All enabled harnesses contribute eligible candidates to global routing.
3. Raw relative costs are not compared directly across quota pools.
4. Existing QuotaPulse pacing participates in cross-harness selection.
5. Numerical intelligence thresholds and broad capability classes behave consistently.
6. Planner and diagnoser preserve their high-effort minimum.
7. Other roles can select suitable lower/higher-effort configurations.
8. Opus low, Haiku high/max, Luna max and Astra high are represented correctly.
9. Antigravity third-party models use `low/medium/high`, not `thinking`.
10. Gemini Pro uses `low/high`; Flash uses `low/medium/high`.
11. Unsupported effort values never silently execute as another effort.
12. Candidates with equivalent scores/costs are selected deterministically.
13. Existing context limits, quota safeguards and rollout boundaries remain intact.
14. The initial model/cost records remain straightforward to update later from telemetry.

## Expected outcome

Maestro should begin with the best evidence-based initial estimates available for each relevant model-effort pair.

It should choose the most suitable eligible candidate from the entire enabled pool of Claude, Codex and Antigravity, with no fixed harness priority.

**Capability controls eligibility; estimated task cost and current quota pressure determine economical selection; effort remains an independent requirement/preference.**

The initial scores are policy estimates, not permanent truths.

Later telemetry will calibrate them against actual Maestro workloads. For now, implement the smallest clean set of changes needed to establish this behavior.
