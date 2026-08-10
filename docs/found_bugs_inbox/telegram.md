# hitl/telegram — findings from repairing the wave-A characterisation suite

These came out of making `tests/characterization/test_telegram.py` honest against the
reference (`scripts/orchestrator_run.py`, commit 68056b5). Both entries below describe what
the reference *actually* does; the tests now pin that.

### _pending_danreqs sorts `ts` as text, so a UTC-offset spelling lands in the wrong slot

`out.sort(key=lambda r: r.get("ts", ""), reverse=True)` (L1831) compares ISO timestamps as raw
strings. Any record whose `ts` is written with a non-Zulu offset is ordered by its wall-clock
text rather than by the instant it names: `2026-01-01T01:00:00+05:00` (= 2025-12-31T20:00Z) sorts
*above* `2026-01-01T00:00:00Z`, so the older request is reported as "newest first". The same
string comparison mis-orders an unpadded month — `2026-1-01…` outranks every `2026-02-01…`
because `"1" > "0"`. This is not cosmetic: `_route_freetext_answer` (L1885) falls back to
`pend[0]` whenever the inbound message is not a reply, so an untargeted free-text answer from Dan
is recorded against the wrong Dan-request, and `_record_danreq_answer` then flips that request to
`answered` and writes its `.answer` sidecar. The producer side (`send_dan_request.py`) happens to
emit one spelling today, so the bug is latent — the sort is only safe by convention, not by
construction.

### The suite's own end-to-end callback test was reading the wrong argv element

Not a reference bug, but worth recording because it hid what the test claimed to check:
`_tg_api` (L1805) builds argv as `curl -s --max-time 5 <url>` and *then* appends a
`--data-urlencode k=v` pair per parameter, so the endpoint URL is `argv[4]` and the last element
is the final parameter value. The test asserted on `argv[-1]`, i.e. on
`text=✅ Keep` / `reply_markup={"inline_keyboard": []}` instead of on the method names. The two
Bot API calls that `_handle_danreq_callback` (L1851) issues on a successful tap
(`answerCallbackQuery` then `editMessageReplyMarkup`) were therefore never actually verified by
that test. It now asserts on `argv[4]` and additionally pins the encoded parameter pairs of both
calls, which is what makes it an end-to-end check rather than a re-run of the `_tg_api` unit
tests.
