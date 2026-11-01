# P13 W3 usage sources

`maestro.usage_sources.UsageSource` supplies account quota for existing graph
attempt/pace callbacks. It tries QuotaPulse `/usage` first for each pool and falls
back to the registered driver's subscription reader (or account `usage()` reader).
No source read launches an agent turn. Unknown measurements stay `None`, not zero.

The default freshness bound is 300 seconds and can be supplied to either adapter's
constructor. Missing, malformed, future-dated, stale or explicitly failed provider
data falls back. A native sample that is also unreadable/stale returns `None`.
`PoolUsage.observed_at` and `updated_at` preserve the source timestamp; `written_at`
belongs to the derived `QuotaObservation`. A bridge HTTP cache lasts at most 60
seconds and never restamps the document. Claude's bridge reader performs a direct
account fetch, so its aggregate `fetched_at` is a conservative observation time.
Codex/agy require a provider-level `observed_at` ISO timestamp: their lockout caches
and dump reads can be older than the aggregate refresh. Provider timestamps are
preserved and checked independently of the envelope; stale flags also reject data.
The installed bridge currently omits these timestamps, so Codex/agy use native
fallback (or remain unknown if native data is unavailable). Dan approved this
conservative boundary during review. Restoring those pools' bridge support requires
an external contract change exposing original measurement/dump times on every
cache path; W3 does not modify QuotaPulse or its live configuration.
Each source change emits `usage_source_changed` with pool and previous/current
source, without error bodies, quota payloads or credentials. Journal failure cannot
fail dispatch. Failed refresh leaves the previous pace observation to age out.

## Pool and constraint identity

| Catalog pool | Bridge provider / view | Native registry driver |
|---|---|---|
| claude-subscription | claude account windows | claude |
| codex-subscription | codex account windows | codex |
| antigravity-gemini | antigravity model_pools.gemini | agy |
| antigravity-3p | antigravity model_pools.3p | agy |

The Opus weekly constraint lives in `scoped_windows['opus']`, alongside the Claude
account weekly window. Neither overwrites the other. Bridge `weekly_model` and
native OAuth `limits[].kind=weekly_scoped` are recognized; historical native
`seven_day_opus` is supported too. A missing scoped reading is unknown. agy 3P
never borrows Gemini windows. The callback serializes selected pool/scoped windows
and observation metadata into attempt telemetry. Bridge-declared window durations
are preserved. Pace uses the existing weekly formula and strength selection.

No route is enabled by reading a pool. Model-scoped admission, budget, evidence and
sprint behavior belong to W5. Paused-pool reconciliation is unchanged: direct fresh
native account readings, source allowlist and existing recovery evidence checks.
Bridge data cannot recover a paused pool, including the blocked-attempt settle path:
the runner accepts only the same native source identities as pause reconciliation.
Bridge reset-crossing telemetry is still recorded. Historical callback callers lacking a
pool id retain their prior native account-only contract.

## Runtime configuration

- `MAESTRO_QUOTAPULSE_URL`: bridge base URL, default `http://127.0.0.1:8787`.
- `USAGE_BRIDGE_TOKEN`: bearer token supplied explicitly to the runtime; alternatively
  `MAESTRO_QUOTAPULSE_TOKEN_FILE` names an explicitly configured bridge token file.
  With neither configured, the bridge is unavailable and native readers continue.
  Maestro never creates, rotates or discovers credentials. No token is journalled.
- `MAESTRO_RESET_MONITOR_URL`: monitor base URL, default `http://127.0.0.1:8080`.

HTTP uses only GET, a 2-second socket timeout and a 1 MiB response limit, with no
redirects, retries or inherited proxy. Configuration/HTTP errors become source
unavailability. The installed bridge currently binds to its NetBird address rather
than loopback, so deployment must supply its URL and auth through the above seam.
No live configuration was changed by W3 qualification.

## Reset signals

`maestro.reset_signals.ResetSignalSource.read()` consumes `/status` and `/events`
only. The snapshot's observation time/bound applies to its three distinct current
signals: `active_signal` preserves an announcement's deadline/window;
`banked_signal` preserves credit lifecycle/expiry; `ResetForecast` preserves
`p24`, `p48`, `confidence` and always has `is_announcement=False`.
An absent, unhealthy, stale, future-dated or malformed monitor returns no snapshot.
Its history retains normalized event timing and banked lifecycle, plus known expiry
fields from `raw_data`, without retaining that raw payload. Events are historical
evidence; they never create a current signal or replace current monitor state.

The current normalizer assigns no `banked_signal` in either provider path. W3 does
not infer an available credit from an old banked event or a pending reset from an
old announcement: the captured monitor history contains announcements followed by
completion. Synthetic status fixtures verify banked credits and expiry independently.
W5 must honor this current-state/history distinction. W3 performs no sprint, credit
activation, monitor polling mutation or scheduling action.

## Evidence and boundaries

Failure-first source, reset and transport fixtures plus callback/routing lanes live
in `tests/test_{usage_sources,reset_signals,source_http,usage_source_wiring}.py`.
Native Opus cache coverage uses synthetic credentials. The inert killed-bridge lane
resolves through native readers; no service is stopped. Redacted live GET comparisons
(evidence archived outside the repo) compare the widget's same normalized source. Initial
Claude/Codex percentage/pace differences were zero, but review invalidated Codex's
freshness claim because the source omits provider timestamps. Final comparison
accepts Claude only, rejects timestamp-free Codex and stale agy, and preserves the
monitor forecast/history distinction. Opus and timestamped Codex/agy have synthetic
fixtures; those fixtures do not establish current live bridge support.

Native scoped constraints receive the same validation as account windows: positive
integer durations, finite percentages between 0 and 100 (or unknown), and finite
nonnegative numeric reset timestamps (or unknown). Malformed optional scopes are
omitted independently; valid account telemetry and other valid scopes survive.
The cached native reader isolates malformed scoped containers and duration keys.
W5 must keep these validated constraints separate when applying model admission.

Work is isolated on feat/p13-w3 from f9e207b. W8/W9 files, root tracked edits,
release/push/live state and the unbuilt Context Gate remain outside this change.
