# Found bugs — parking (waiting_on_dan, manual actions, handle_incomplete)

Behavioural surprises recorded while writing `tests/characterization/test_parking.py`.
Every item below is **pinned by a passing test against the reference**, not repaired.

---

## PK-1 — `park_manual_action` stores a silently truncated action, and the truncation is load-bearing

The state entry gets `"summary": action[:200]` while the Telegram ping interpolates the
whole `action` and `prep_actions.set_action` records the whole `action`. Three different
lengths for the same instruction, with no ellipsis to mark the cut, so a 400-character
action reads as a complete 200-character one in anything that renders `summary`.

That truncation is not merely cosmetic: `/ask` and the manual-action finalizer both do
`action = pa.get("action") or entry.get("summary", "")` (reference lines 2988 and 3050),
so whenever the `prep_actions` sidecar is missing or empty the *truncated* summary becomes
the operative action text handed onward — the tail of Dan's instruction is gone.

`park_for_dan` truncates the stored summary and the ping to the same 200 characters, so
the asymmetry is specific to `park_manual_action`.

Test: `test_park_manual_action_truncates_the_stored_summary_but_not_the_ping`.
