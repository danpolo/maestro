# DuetFlow: unavailable song versions

Report: Dan, 2026-09-30. Source:
`handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md`.
Paraphrase: some Duet song versions cannot play although Spotify has playable
alternatives. Requested outcome: make this a normal Maestro bug task and establish
the same intake path for later bugs, changes and features.

- Classification: bug report; symptom reported, root cause and exact tracks unverified.
- Intake example only: no live DuetFlow task was queued or approved in this session.
  Dan explicitly limited this work to improving Maestro's intake mechanism.
- Concrete evidence supplied by Dan during this session:
  - Unplayable from playlist: https://open.spotify.com/track/13HD3tYTiz8oSRxWzXEq7g?si=7fda636b1c1a4ab9
  - Playable from search/artist page: https://open.spotify.com/track/13HD3tYTiz8oSRxWzXEq7g?si=a6904a7cb23a4e70
  - Both links identify **13HD3tYTiz8oSRxWzXEq7g**; only the `si` parameter differs.
  - Dan's exact observation: “its unplayble from the playlist for me (all the ella
    lengley songs in the playlist) but when i search for the songs in the search or
    from the artist page i can play them, they are grayed out and unclikable in the
    playlist for me.”
  - Affects Dan; effect on the other listener is unknown. No claim of live API or
    playback reproduction by the agent.
- Evidence still to investigate: playlist snapshot/items versus direct-track
  metadata, account/client context and relevant local track state. Agent owns this;
  neither a different playable version nor a replacement policy is established.
- Code evidence: `duetflow/spotify.py` exposes playlist/recent/top GETs and playlist
  writes. `duetflow/playlist.py` selects/writes candidate IDs; the inspected code has
  no explicit playability/relink/search handling. This suggests an investigation area,
  not a demonstrated cause or a guaranteed replacement API.
- Dependencies: accepted `07-reconciliation`; existing history follow-ups remain
  untouched. Overlapping scope is handled by the normal scheduler; no urgent order
  has been requested.
- Diagnosis must precede a repair choice. Same-ID playback differing by context
  does not establish an unavailable recording. If the evidenced repair needs a
  substitution or another material scope choice, ask once then; do not infer that
  a cover/live/remaster/explicit variant is acceptable.
- Acceptance: reproduce the playlist-versus-direct same-ID report; fixture coverage
  for the demonstrated cause and both listener contexts; agree any material repair
  policy only if needed;
  preserve attribution, Pins, Vetoes, accounting, order and confirmed-write history;
  full suite and real Dan playback verification in the resulting normal workflow.
- Authorized now: preserve the example and build/verify Maestro Telegram intake.
  Live playlist edits, releasing the task and DuetFlow continuation are not authorized.
- Next agent action: use the supplied example to verify Maestro report capture and
  scoped-proposal behavior in disposable fixtures. Do not diagnose or repair DuetFlow
  here. Keep HALT until continuation is agreed.

Maestro intake verification uses disposable Telegram transport/model fixtures.
The same-ID report must not be portrayed as a diagnosed DuetFlow defect or live repair.
