# Model context limits

These thresholds are **quality / session-lifecycle limits**, not physical model context-window sizes
and not Codex auto-compaction thresholds. They define when Maestro should prepare a curated handoff,
prefer a fresh session, or stop extending the current session even if the model can technically accept
more context.

Machine-parseable per-model context-ceiling table, extracted out of `AGENTS.md` per Maestro
DESIGN.md §8 (D4). See `AGENTS.md`'s "Context lifecycle" section for what these thresholds mean and
how to use them — this file holds only the numbers, kept here so a tool (`maestro`'s `limits.py`)
can parse them without touching prose.

| Model | Prepare handoff | Normally start fresh | Exception ceiling |
|---:|---:|---:|---:|
| GPT-6 Astra | 160K–200K | 230K–270K | 350K |
| GPT-5.6 Sol | 120K–140K | 180K–220K | 280K |
| GPT-5.6 Terra | 100K–120K | 140K–170K | 220K |
| GPT-5.6 Luna | 70K–90K | 100K–120K | 150K |
| GPT-Reserve | 70K–90K | 100K–120K | 150K |
| GPT-5.5 | 90K–110K | 130K–160K | 210K |
| GPT-5.4 Mini | 40K–60K | 70K–90K | 110K |

`gpt-reserve` is treated as an alias of GPT-5.6 Luna for context-lifecycle purposes.

`codex-auto-review` is intentionally excluded from this table because it is a hidden automatic
approval/review model rather than a normal long-running coding-agent session. Maestro should not
apply ordinary session-lifecycle thresholds to it.

For an unknown normal coding model: warn at 100K, evaluate a reset at 120K, and normally stop by 150K.

Codex physical context-window configuration and native auto-compaction should remain separate from
these thresholds. Maestro should normally hand off before native compaction; Codex compaction is a
capacity safety net, not a replacement for a curated fresh-session handoff.

## Enforced Codex windows

`model_catalog.json` is regenerated from `models_cache.json`. Only the Astra and Sol
`context_window` values are changed; every override is clamped to the model's own
`max_context_window`. With no explicit compaction limit, Codex derives native compaction at about
90% of the resolved window.

| Model | Enforced window | Native compaction (~90%) |
|---|---:|---:|
| GPT-6 Astra | 420K | ~378K |
| GPT-5.6 Sol | 360K | ~324K |
| GPT-5.6 Terra | 272K | ~244.8K |
| GPT-5.6 Luna | 272K | ~244.8K |
| GPT-Reserve | 272K | ~244.8K |
| GPT-5.5 | 272K | ~244.8K |
| GPT-5.4 Mini | 272K | ~244.8K |
| codex-auto-review (lifecycle-exempt) | unchanged at 272K | unchanged at ~244.8K |

Regenerate the pinned catalog after Codex updates or whenever `models_cache.json` changes:

```bash
/home/dan/.codex/scripts/regenerate-model-catalog.py
```
