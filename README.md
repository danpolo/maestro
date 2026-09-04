# Maestro

Maestro is a self-hosted control plane for moving a software project forward with
autonomous coding agents while retaining explicit human control over consequential
decisions. It turns a project roadmap into bounded, auditable work: an orchestrator
plans and delegates, implementers work in isolated Git worktrees, and a watchdog
keeps the loop observable and recoverable.

This is orchestration infrastructure, not a general-purpose agent framework. The
project being managed supplies its own tests, evaluation, smoke, deployment, and
health-check adapters; Maestro owns the execution protocol, state, gates, and
operator controls.

## Architecture

```text
                         project.yaml + roadmap + adapters
                                      |
                                      v
  operator <---- Telegram / CLI ---- orchestrator ----> backend drivers
     ^                                  |                 (Claude, Codex)
     |                                  v
     +------------------------- verification gates
                                      |
                                      v
                           Git worktree implementers
                                      |
                                      v
                  .orchestrator/state.json + journal.ndjson
                                      ^
                                      |
                                watchdog (TMUX)
```

The backend layer presents one capability-oriented protocol to the orchestration
logic. Roles select their backend and model from `project.yaml`; a quota event or
manual command can checkpoint work and switch to the configured fallback without
discarding the worktree.

## Reliability controls

- **Isolated change execution.** Implementers receive a scoped brief and work in Git
  worktrees, keeping unmerged changes separate from the project branch.
- **Gated completion.** A task must clear project-provided verification checks. Optional
  evaluation and smoke adapters are reported truthfully when absent rather than treated
  as evidence that did not run.
- **Durable state and recovery.** State is atomically written; an append-only journal,
  persisted retry/launch data, and a watchdog support restart, stall detection, and
  explicit halt/resume control.
- **Human-in-the-loop.** Telegram and CLI controls surface manual work, decisions,
  approvals, rejects, and backend selection instead of silently assuming them.
- **Confinement.** Project-defined allow/deny rules constrain automated self-fix and
  redo operations, while secrets and runtime state stay outside version control.

## State and storage model

Each managed project keeps operational state in its ignored `.orchestrator/` directory:

- `state.json` records the current task, in-flight work, retries, parking, and operator
  control state.
- `journal.ndjson` is an append-only event trail for decisions, task outcomes, and
  recovery activity.
- Workspaces hold the Git worktrees used by implementers; the project repository remains
  the source of truth for accepted changes.

Configuration (`project.yaml`), roadmap documents, and executable adapters are committed
with the managed project. Credentials belong in that project's ignored `.env`, never in
Maestro's tracked configuration.

## Quick start

Maestro requires Python 3.11+, Git, TMUX, and at least one supported agent CLI. Create a
development environment, install the package, then scaffold a managed project:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

maestro init /path/to/project
maestro doctor --repo /path/to/project
maestro status --repo /path/to/project
```

`maestro init` is designed to be idempotent: existing files are preserved and proposed
updates are emitted as `.new` files for review. See [the design](docs/DESIGN.md) and
[execution protocol](docs/EXECUTION.md) for the adapter contract and operating model.

## Tests

The repository has a pytest suite covering backend protocol behavior, orchestration and
switching logic, state/journal handling, CLI scaffolding, safety gates, templates, and
operational shell helpers. Run the full suite with:

```bash
.venv/bin/python -m pytest -q
```

Some end-to-end checks deliberately require access to the shared `agents` TMUX session
and a loopback HTTP fixture. In restricted sandboxes those checks cannot establish their
required OS resources; this is reported as an environment limitation, not silently
skipped. The normal development and CI environment should run the complete command.

## Repository hygiene

Runtime state, environments, and credentials are ignored. Research material under
`docs/DEEP_RESEARCH_PROMPT.md` and `docs/graph-engineering/` is deliberately kept local
to this checkout and excluded from the portfolio commit.
