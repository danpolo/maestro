# GitHub portfolio preparation handoff

## Goal

Prepare Maestro as a credible public portfolio repository for a data-engineering job search, while preserving all existing work and keeping research drafts out of the publication commit. Stop after producing a reviewed, tested local commit; do not create a remote, change visibility, or pin it.

## Read first

- `/home/dan/.codex/AGENTS.md`, especially the SymDex safety rule; never run `symdex index` directly.
- The repository's governing instruction files, if any.
- `/home/dan/.agent/diagrams/github-data-engineering-portfolio-audit.html` for portfolio context.
- Existing source, tests, and documentation; the repository currently has no root README.

## In scope

- Explain the data/agent orchestration problem, architecture, reliability controls, storage/state model, and measurable tests in a recruiter-facing root `README.md`.
- Add a safe `.gitignore` entry or another non-destructive exclusion for untracked research material under `docs/DEEP_RESEARCH_PROMPT.md` and `docs/graph-engineering/` unless those files are intentionally product documentation.
- Audit tracked history and current files for credential, PII, runtime-data, and oversized-file risks without printing sensitive values.
- Run the largest reliable test subset available in this TMUX-based environment and document any environment-only limitation accurately.
- Prepare one clean portfolio commit on the existing branch without rewriting history or discarding local files.

## Out of scope

- Do not publish, create a GitHub remote, change visibility, or pin the repository.
- Do not delete or rewrite the untracked research material.
- Do not run SymDex directly or modify production/server services.
- Do not invent benchmark claims, employers, users, or production scale.

## Verification and report-back

- Report the number of tracked files scanned, secret-risk findings, and files larger than 10 MiB.
- Report exact test totals: passed, failed, skipped, and environment-blocked.
- Confirm `git status --short` contains only deliberately preserved pre-existing work, or enumerate every remaining path.
- Report the prepared commit SHA and the final README's architecture/test sections.

## Suggested skills

- `superpowers:brainstorming` before writing the recruiter-facing narrative.
- `superpowers:systematic-debugging` if the TMUX-dependent test failure needs investigation.
- `superpowers:verification-before-completion` before claiming the repo is publication-ready.

