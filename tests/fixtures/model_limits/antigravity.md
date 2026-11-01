# Model context limits

These thresholds are **quality / session-lifecycle limits for Antigravity CLI (`agy`)**, not native
model context-window sizes.

The thresholds are derived mechanically from each model's effective Antigravity context-management
boundary:

- **Prepare handoff:** boundary − 25K to boundary − 20K
- **Normally start fresh:** boundary − 15K to boundary − 10K
- **Exception ceiling:** boundary − 5K

The goal is to complete a curated fresh-session handoff before Antigravity performs automatic
checkpoint/compaction or reaches the platform truncation boundary.

Machine-parseable per-model context-ceiling table. Keep the model names and numeric cells stable so
agent tooling can parse this file without depending on prose.

Every model name carries the `agy/` harness prefix. Antigravity also serves third-party models
(Claude, GPT-OSS) through its own pools, so the same model name can appear in another harness's table
with different limits. The prefix keeps these AGY rows from ever answering a Claude Code or Codex
lookup. Maestro resolves them with `limits.resolve(model, backend="antigravity")`.

| Model | Prepare handoff | Normally start fresh | Exception ceiling |
|---:|---:|---:|---:|
| agy/Gemini 3.8 Flash | 115-120K | 125-130K | 135K |
| agy/Gemini 3.7 Flash | 115-120K | 125-130K | 135K |
| agy/Gemini 3.6 Flash | 115-120K | 125-130K | 135K |
| agy/Gemini 3.1 Pro | 103-108K | 113-118K | 123K |
| agy/Claude Sonnet 4.6 (Thinking) | 115-120K | 125-130K | 135K |
| agy/Claude Opus 4.6 (Thinking) | 115-120K | 125-130K | 135K |
| agy/GPT-OSS 120B (Medium) | 55-60K | 65-70K | 75K |

## Boundary basis

| Model family | Effective AGY boundary used |
|---:|---:|
| agy/Gemini 3.8 Flash | 140K checkpoint threshold |
| agy/Gemini 3.7 Flash | 140K checkpoint threshold |
| agy/Gemini 3.6 Flash | 140K checkpoint threshold |
| agy/Gemini 3.1 Pro | 128K AGY platform/checkpointer ceiling |
| agy/Claude Sonnet 4.6 (Thinking) | 140K pre-compaction operating boundary within the 160K AGY profile |
| agy/Claude Opus 4.6 (Thinking) | 140K pre-compaction operating boundary within the 160K AGY profile |
| agy/GPT-OSS 120B (Medium) | 80K AGY platform/checkpointer ceiling |

For Gemini 3.1 Pro and GPT-OSS 120B, a distinct lower live checkpoint trigger is not sufficiently
established, so the AGY platform/checkpointer ceiling is treated as the effective boundary. This keeps
the same −25/−20K / −15/−10K / −5K safety rule without inventing an unsupported compaction trigger.

The Gemini 3.8 Flash, Gemini 3.7 Flash, and Gemini 3.6 Flash rows apply to their High, Medium, and Low
thinking variants unless Antigravity exposes materially different live checkpoint parameters for a
specific tier.

These values are **AGY-specific**. Do not reuse the same model's row for direct Gemini API, Claude API,
Claude Code, Codex, or another agent harness.

For an unknown AGY coding model, first determine its live checkpoint/compaction or platform boundary.
Until that is known, do not infer a threshold solely from the model's native advertised context window.
