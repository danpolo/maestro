# Graph Engineering

**[`MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html`](MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html) is the single
source of truth for the plan** (revision 2, 2026-09-08). Every decision, phase, acceptance criterion and
amendment lives there, and it carries an amendment log recording what changed and on what evidence.

For **autonomous AI agents** implementing the plan, the human-oriented architecture has been compiled and optimized using the principles of **context engineering**:
- **[`MAESTRO_GRAPH_ENGINEERING_AGENT_SPEC.md`](MAESTRO_GRAPH_ENGINEERING_AGENT_SPEC.md)** — Consolidated agent-faced specification (schemas, state machines, invariants, acceptance criteria, P00–P12).
- **[`specs/`](specs/README.md)** — Modular agent-faced specification suite with a **Context Routing Matrix** designed for just-in-time context injection without token bloat.

Two files sit beside it and are *not* plan content:

- `../../GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md` — the open-evidence register (B01–B18). When a blind spot
  changes what Maestro should build, the change goes in the plan, not here.
- `../../artifacts/graph-engineering/` — the machine-readable observations behind the amendments.

Everything below is the input material that produced the plan. It is retained for provenance and is
superseded by the plan wherever the two differ.

## Contents

- `reports/01-deepseek-report.md` — DeepSeek's public-share report.
- `reports/02-grok-report.md` — Grok's public-share report.
- `reports/03-maestro-graph-engineering-research.docx` — the supplied DOCX report, preserved in its original format.
- `reports/04-chatgpt-graph-engineering-report.md` — ChatGPT's plain-text fourth report extracted from the dedicated report link.
- `four-report-synthesis.md` — ChatGPT's synthesis weighing all four reports against Maestro's actual constraints, concluding Maestro should become graph-aware without becoming a graph-framework application.
- `project-workflow-graph.md` — ChatGPT's follow-up on graph-based agent/script workflows and a project-specific workflow graph generated during `init`.
- `REVIEW_RESEARCH_VS_PLAN.md` — **superseded.** The working record of how the external research pass and the local audit were reconciled; its adopted findings are folded into the plan.
- `blind-spots-research-results.md` — the external targeted research summary. Direction only: the deliverable it links to does not exist on this machine and its citations are unresolvable tokens.
- `maestro-grounded-integration-plan.html` — the resulting grounded integration plan (standalone HTML) for making Maestro graph-aware without turning it into a graph framework, Prior grounded integration proposal — not authoritative research.
This file was produced from an earlier synthesis and contains useful repository-specific observations and a candidate architecture. Treat its repository grounding as useful input, but independently reassess its architectural conclusions against the four original reports, the other synthesis/notes, the current Maestro repository, and the new prompt. Do not assume its proposed architecture or implementation order is correct.

Sources are retained verbatim where possible. The ChatGPT responses were extracted from the shared conversation on 2026-09-03.
