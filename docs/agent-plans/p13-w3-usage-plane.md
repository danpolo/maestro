# P13 W3 isolated usage plane

Authorized by Dan 2026-10-13; base f9e207b, branch feat/p13-w3. Governing
root-only sources: AGENTS.md, P13 B5/B8/B20, W3, acceptance and §4;
handoffs/2026-10-13-p13-w3-start.md. No W3-owned STATE thread currently exists.

## Scope and decisions

- Add independent UsageSource and ResetSignalSource adapters, using current source
  contracts from QuotaPulse/bridge/bridge.py and codex-reset-monitor/{normalizer,server}.py.
- Bridge first per pool, native fallback on missing/malformed/stale/unavailable
  bridge data. Preserve sample time and declared freshness bounds; journal source
  changes without payloads, errors or credentials. Preserve Opus-scoped weekly as
  a separate constraint, and agy Gemini/3P as distinct quota pools.
- Wire workflows/routing.py: attempt reads, pace refresh, metadata serialization.
  Review exposed the blocked-attempt settle recovery path; Dan approved a narrow
  native-source guard in isolated workflows/runner.py, preserving bridge rollover
  telemetry. Paused pools keep their existing direct-native recovery reader.
  There is no maestro/routing.py or separate legacy pace reader in this base.
- Runtime bridge URL/token use explicit environment configuration; no live config
  changes. Dan approved narrow existing-token use for bounded GET comparisons;
  comparison evidence contains normalized fields only. The monitor remains a
  read-only adapter for future W5; no sprint, budgets or agy activation here.
- Review showed aggregate fetched_at can renew older Codex cache/agy dump values.
  Dan approved native fallback unless those providers supply a trustworthy original
  observed_at. Current bridge omits it; external contract repair stays outside W3.
- Dan additionally approved timestamp-only fixture corrections in isolated
  tests/graph_engineering/test_end_to_end.py after the full suite exposed stale and
  future-dated measurements. W8's changes occupy a separate section and are intact.
- Leave W8/W9 source/test ownership and six root tracked edits untouched. No root
  integration, release, push, publication, Context Gate or service changes.

## Work and verification

1. Verify fresh pane/thread, continuation logs, clean worktree and baseline suite.
2. Failure-first bridge/native pool normalization and freshness/failover lanes.
3. Failure-first reset status/events lanes: healthy/fresh only; announcements,
   banked credits/expiry and informational forecast kept distinct; missing = none.
4. Failure-first safe callback/pace serialization wiring; retain recovery-proof lanes.
5. Compare normalized bridge values/pace to the same widget source (within 1%).
6. Freeze candidate; full suite in named agents TMUX with private MAESTRO_HOME.
7. Fresh independent whole-range spec review; fix blocking findings with regressions.
8. Full suite before worktree-only commit, then qualify clean commit. Record W3-only
   spec status and handoff with documentation text, integration seams and evidence.

Commands run from this worktree with root .venv/bin/python; verify maestro.__file__.
Full suite: -m pytest -o addopts='' -q. Evidence archived outside the repo (narrative notes only);
private home: a scratch directory inside the checkout. Source timestamps never renewed on
fallback or caching. Test fixtures avoid live credentials and agent inference.
