# Project — `{{ project_name }}`

The **human-facing** companion to `docs/ROADMAP.md`. Where the roadmap is a machine-
parsed task queue, this file is prose: what this project is, what good looks like,
and the vocabulary maestro's reporting uses when it talks about upcoming work. It is
not parsed by `maestro.docs.roadmap` — write most of it however reads best. The one
exception is the "## Glossary" section below: `maestro.docs.upcoming` reads its bullets
to detect jargon in task descriptions and render `docs/UPCOMING.md`'s glossary, so keep
each entry to that section's one-line-per-term format.

## What this project is

*Fill in during setup (the `maestro-setup` skill interviews for this): one or two
paragraphs on what `{{ project_name }}` does and who/what it's for.*

## What good looks like here

*What a passing gate (`project.yaml`'s `gate.chain`) actually certifies, and what it
doesn't yet. If `gate.chain` is still just `[test]`, say so plainly here too — the
same "unevaluated is honest, not hidden" principle `adapters/eval`'s stub follows.*

## What's risky

*Anything an autonomous task should hesitate on even if it's not on the hard deny
list in `operating_preamble.md` — `project.yaml`'s `risky_set` is the machine-
readable version of this list; this section is the reasoning behind it.*

## Glossary

*Terms maestro's phase reports and the upcoming-work summary should use consistently
— domain vocabulary, abbreviations, anything a reader would otherwise have to guess.
One bullet per term, exactly `- **Term** — one-line definition` (spell out an
abbreviation in parentheses, e.g. `RRF (Reciprocal Rank Fusion)`, so both forms are
matched in task descriptions) — `maestro.docs.upcoming` parses this shape.*

## Repo pointers

- Roadmap: `docs/ROADMAP.md`
- Dependency map: `docs/dependency_map.md` (generated; not hand-edited)
- Adapters: `adapters/` (`test` required, `eval`/`smoke`/`deploy`/`healthcheck`/`latency`
  optional — see `DESIGN.md` §4 in the maestro repo for the full contract)
