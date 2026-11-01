# Model context limits

Machine-parseable per-model context-ceiling table, extracted out of `CLAUDE.md` per Maestro
DESIGN.md §8 (D4). See `CLAUDE.md`'s "Session triage" section for what these thresholds mean and
how to use them — this file holds only the numbers, kept here so a tool (`maestro`'s `limits.py`)
can parse them without touching prose.

| Model | Prepare handoff | Normally start fresh | Exception ceiling |
|---|---:|---:|---:|
| Claude Haiku 4.5 | 90-100K | 120–140K | 180K |
| Claude Sonnet 5 | 120-130K | 150-170K | 250K |
| Claude Opus 5 | 160-180K | 200-220K | 300K |

Haiku 4.5's row is a conservative judgement, not a transcribed window: no local file states
its context window, and it shares Sonnet 5's ~200K window so the window itself isn't the
constraint — these thresholds track reasoning reliability under load, scaled to roughly
60–65% of Sonnet 5's row as the smallest tier.

Model unknown → 100K warn / 120K evaluate / 150K normal max.

## Enforced Claude Code auto-compact window

These are **derived**, not a second policy. `~/.claude/hooks/context-governor.py` computes
them as `exception ceiling x 1.2`, rounded to 5K, and writes the result to
`autoCompactWindow` in `settings.json` whenever the session's model changes. Claude Code
re-resolves the window on model switch, settings change and at each turn start, and
compacts at `window - 13000` (its summary buffer).

The point of the 1.2 headroom is layering: native compaction must sit *above* the
exception ceiling so it stays an emergency capacity net, never the mechanism that carries
a session past its quality ceiling. A fresh session comes first; compaction is what happens only if
that instruction was ignored. A handoff document is only written when work is unfinished —
a finished task just needs a fresh session.

| Model | Enforced `autoCompactWindow` | Compaction fires at |
|---|---:|---:|
| Claude Haiku 4.5 | 145,000 | ~132,000 |
| Claude Sonnet 5 | 215,000 | ~202,000 |
| Claude Opus 5 | 290,000 | ~277,000 |
| Model unknown | 180,000 | ~167,000 |

Claude Code clamps the window to the model's real context window, so these values can never
exceed what the model actually supports. On this account Opus 5 and Sonnet 5 default to their
1M windows, so no clamp binds and each row lands where the arithmetic says: compaction sits
above the exception ceiling for every model, which is the whole point of the layering.

This table has three columns on purpose: `maestro/limits.py` only parses four-cell rows,
so adding a column to the policy table above would have silently dropped every model from
Maestro's view.
