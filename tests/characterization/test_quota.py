"""Pinned behaviour of the quota / usage-limit layer.

Characterisation, not specification. Nine functions live here and they split cleanly in
two: four pure-ish time formatters (``_elapsed_min``, ``_fmt_min``, ``_elapsed_str``,
``_parse_est_minutes``) and five that decide, from a JSON usage file and from free text
scraped out of an implementer's log, whether the orchestrator may launch work at all
(``get_effective_cap``, ``_resolve_limit_reset``, ``_scan_impl_log_for_limit``,
``_pause_for_usage_limit``, ``_paused_until_epoch``).

``_resolve_limit_reset`` is the interesting one — a three-tier fallback (authoritative
epoch, then a regex over English prose, then a flat +1h) with five distinct silent
``except`` paths. It is table-driven below over every message shape the regex can and
cannot parse, with the clock and the local timezone both frozen so every expected string
is exact rather than approximate.

Where the reference surprises, the test pins the surprise and ``docs/FOUND_BUGS.md``
records it. Nothing here asserts what the code *should* do.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.maestro_module("quota")


# --- frozen clock + frozen local timezone -------------------------------------------

#: 2026-03-10 is inside US DST and outside EU DST, so America/New_York is a stable
#: UTC-04:00 all day: no fold, no gap, no ambiguity for any hour a test picks.
FIXED = datetime(2026, 3, 10, 12, 34, 56, tzinfo=timezone.utc)
LOCAL_TZ = "America/New_York"  # UTC-4 on FIXED's date


class _FrozenDatetime(datetime):
    """`datetime` with `now()` pinned to FIXED; everything else is inherited."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            # Real `datetime.now()` returns a naive local time.
            return FIXED.astimezone().replace(tzinfo=None)
        return FIXED.astimezone(tz)


@pytest.fixture
def local_tz():
    """Pin the process-local timezone so `astimezone()` is deterministic."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = LOCAL_TZ
    time.tzset()
    yield LOCAL_TZ
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture
def clock(subject, monkeypatch, local_tz):
    """Freeze the subject's clock at FIXED and the local zone at LOCAL_TZ."""
    monkeypatch.setattr(subject, "datetime", _FrozenDatetime)
    return FIXED


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


PLUS_1H = "2026-03-10T13:34:56Z"  # the unconditional final fallback, at FIXED


# --- helpers ------------------------------------------------------------------------


def _write_usage(subject, payload) -> None:
    subject.USAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    subject.USAGE_JSON.write_text(json.dumps(payload), encoding="utf-8")


def _write_state(sandbox, payload) -> None:
    (sandbox.orch_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def _read_state_file(sandbox) -> dict:
    return json.loads((sandbox.orch_dir / "state.json").read_text(encoding="utf-8"))


def _journal_records(sandbox) -> list:
    path = sandbox.orch_dir / "journal.ndjson"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ====================================================================================
# _elapsed_min
# ====================================================================================


@pytest.mark.parametrize(
    "started_at, expected",
    [
        ("2026-03-10T12:34:56Z", 0),        # exactly now
        ("2026-03-10T12:34:00Z", 0),        # 56s -> 0 whole minutes
        ("2026-03-10T12:33:56Z", 1),
        ("2026-03-10T11:34:56Z", 60),
        ("2026-03-10T12:00:00Z", 34),       # 2096s // 60
        ("2026-03-09T12:34:56Z", 1440),
        ("2026-3-10T11:34:56Z", 60),        # strptime accepts unpadded fields
        ("2026-03-10T11:34:56z", 60),       # the literal 'Z' matches case-insensitively
    ],
)
def test_elapsed_min_parses_and_floors_to_whole_minutes(subject, clock, started_at, expected):
    assert subject._elapsed_min(started_at) == expected


@pytest.mark.parametrize(
    "started_at",
    ["2026-03-10T12:34:57Z", "2026-03-10T13:34:56Z", "2027-01-01T00:00:00Z"],
)
def test_elapsed_min_clamps_a_future_start_to_zero(subject, clock, started_at):
    assert subject._elapsed_min(started_at) == 0


@pytest.mark.parametrize(
    "started_at",
    [
        "",
        "nope",
        "2026-03-10T12:34:56",          # no trailing Z
        "2026-03-10T12:34:56+00:00",    # a real ISO offset is *not* accepted
        "2026-03-10T12:34:56.500Z",     # fractional seconds are not accepted
        " 2026-03-10T12:34:56Z",        # leading space
        "2026-03-10T12:34:56Z ",        # trailing space
        "2026-03-10 12:34:56Z",         # space instead of T
        None,
        12345,
        object(),
    ],
)
def test_elapsed_min_returns_none_for_anything_it_cannot_parse(subject, clock, started_at):
    assert subject._elapsed_min(started_at) is None


def test_elapsed_min_ignores_any_timezone_the_caller_meant(subject, clock):
    """FOUND_BUGS: the `Z` is matched as a literal, not honoured as an offset.

    There is no way to feed this function a non-UTC instant: the format has no offset
    field, so a local-time string is silently read as UTC.
    """
    assert subject._elapsed_min("2026-03-10T11:34:56Z") == 60


def test_elapsed_min_returns_a_plain_int(subject, clock):
    result = subject._elapsed_min("2026-03-10T11:34:56Z")
    assert isinstance(result, int) and not isinstance(result, bool)


# ====================================================================================
# _fmt_min
# ====================================================================================


@pytest.mark.parametrize(
    "mins, expected",
    [
        (0, "0m"),
        (1, "1m"),
        (9, "9m"),
        (59, "59m"),
        (60, "1h00m"),
        (61, "1h01m"),
        (125, "2h05m"),
        (600, "10h00m"),
        (1440, "24h00m"),
        (100000, "1666h40m"),
    ],
)
def test_fmt_min_formats_hours_and_zero_padded_minutes(subject, mins, expected):
    assert subject._fmt_min(mins) == expected


@pytest.mark.parametrize("mins", [-1, -59, -60, -100000])
def test_fmt_min_clamps_negatives_to_zero_minutes(subject, mins):
    assert subject._fmt_min(mins) == "0m"


def test_fmt_min_accepts_bool_because_bool_is_an_int(subject):
    assert subject._fmt_min(True) == "1m"
    assert subject._fmt_min(False) == "0m"


def test_fmt_min_leaks_the_float_repr_on_the_minutes_only_branch(subject):
    """FOUND_BUGS: the sub-hour branch has no format spec, so a float is not formatted."""
    assert subject._fmt_min(30.0) == "30.0m"
    assert subject._fmt_min(0.5) == "0.5m"


@pytest.mark.parametrize("mins", [60.0, 90.5, 3600.0])
def test_fmt_min_raises_on_a_float_once_it_reaches_an_hour(subject, mins):
    """FOUND_BUGS: the same input is formatted below 60 and crashes at 60."""
    with pytest.raises(ValueError):
        subject._fmt_min(mins)


def test_fmt_min_raises_on_a_string(subject):
    with pytest.raises(TypeError):
        subject._fmt_min("5")


# ====================================================================================
# _elapsed_str
# ====================================================================================


@pytest.mark.parametrize(
    "started_at, expected",
    [
        ("2026-03-10T12:34:56Z", "0m"),
        ("2026-03-10T12:04:56Z", "30m"),
        ("2026-03-10T10:04:56Z", "2h30m"),
        ("2026-03-09T12:34:56Z", "24h00m"),
        ("2026-03-10T13:34:56Z", "0m"),   # future -> clamped, not "?"
    ],
)
def test_elapsed_str_formats_a_parseable_timestamp(subject, clock, started_at, expected):
    assert subject._elapsed_str(started_at) == expected


@pytest.mark.parametrize("started_at", ["", "nope", None, 12345, "2026-03-10T12:34:56"])
def test_elapsed_str_renders_an_unparseable_timestamp_as_a_question_mark(
    subject, clock, started_at
):
    assert subject._elapsed_str(started_at) == "?"


# ====================================================================================
# _parse_est_minutes
# ====================================================================================


@pytest.mark.parametrize(
    "est_time, expected",
    [
        ("~2h", 120),
        ("2h", 120),
        ("2H", 120),
        ("~ 2 h", 120),
        ("2 hour", 120),
        ("3hr", 180),
        ("~1.5h", 90),
        ("0.05h", 3),
        ("~0h", 0),
        ("~90 min", 90),
        ("90min", 90),
        ("5m", 5),
        ("~30m", 30),
        ("about 2h of work", 120),
        ("~2.5 hour, maybe more", 150),
    ],
)
def test_parse_est_minutes_reads_the_first_number_unit_pair(subject, est_time, expected):
    assert subject._parse_est_minutes(est_time) == expected


@pytest.mark.parametrize("est_time", ["", None, "10", "soon", "half a day", "h", "~"])
def test_parse_est_minutes_returns_none_when_there_is_no_number_unit_pair(subject, est_time):
    assert subject._parse_est_minutes(est_time) is None


def test_parse_est_minutes_discards_a_minus_sign(subject):
    """FOUND_BUGS: the sign is outside the capture group, so `-3h` is three hours."""
    assert subject._parse_est_minutes("-3h") == 180


@pytest.mark.parametrize("est_time", ["2 hours", "30 mins", "~45 minutes", "3 hrs"])
def test_parse_est_minutes_rejects_the_plural_units_humans_actually_write(subject, est_time):
    """FOUND_BUGS: `(h|hour|hr|min|m)\\b` never matches `hours`, `mins`, `minutes`, `hrs`.

    The alternation is leftmost-first and every branch is followed by a word boundary, so
    the trailing `s` kills the match outright. Free-text estimates written the natural way
    parse as "unknown".
    """
    assert subject._parse_est_minutes(est_time) is None


def test_parse_est_minutes_reads_the_minutes_of_a_compound_estimate_as_the_whole_estimate(
    subject,
):
    """FOUND_BUGS: `~2h30m` is 30 minutes, not 150.

    `2h` fails the `\\b` after `h` (the next character is `3`), so the search slides
    forward and matches `30m` instead — a five-fold under-estimate, silently.
    """
    assert subject._parse_est_minutes("~2h30m") == 30
    assert subject._parse_est_minutes("1h45m") == 45


def test_parse_est_minutes_truncates_rather_than_rounds(subject):
    assert subject._parse_est_minutes("1.9h") == 114
    assert subject._parse_est_minutes("1.1h") == 66   # 66.00000000000001 -> 66
    assert subject._parse_est_minutes("0.9m") == 0
    assert subject._parse_est_minutes("0.9h") == 54


def test_parse_est_minutes_conflates_zero_with_unparseable_for_any_truthiness_check(subject):
    """FOUND_BUGS: `~0h` returns 0, which is falsy — indistinguishable from None."""
    assert subject._parse_est_minutes("~0h") == 0
    assert subject._parse_est_minutes("0 min") == 0


def test_parse_est_minutes_treats_falsy_non_strings_as_empty(subject):
    assert subject._parse_est_minutes(0) is None
    assert subject._parse_est_minutes([]) is None


def test_parse_est_minutes_raises_on_a_truthy_non_string(subject):
    """FOUND_BUGS: `est_time or ""` guards falsy input only; `5` reaches `re.search`."""
    with pytest.raises(TypeError):
        subject._parse_est_minutes(5)


# ====================================================================================
# get_effective_cap
# ====================================================================================


def test_get_effective_cap_thresholds_are_the_documented_constants(subject):
    assert subject.CONCURRENCY_CAP == 3
    assert subject.THROTTLE_75_CAP == 1
    assert subject.THROTTLE_75_PCT == 75.0
    assert subject.PAUSE_PCT == 92.0


@pytest.mark.parametrize(
    "used_pct, expected_cap",
    [
        (0, 3),
        (1, 3),
        (74.9, 3),
        (75.0, 1),      # >= is inclusive
        (75.1, 1),
        (91.9, 1),
        (92.0, 0),      # >= is inclusive
        (92.1, 0),
        (100, 0),
        (1000, 0),
    ],
)
def test_get_effective_cap_steps_down_at_75_and_92(subject, sandbox, used_pct, expected_cap):
    _write_usage(subject, {"five_hour": {"used_pct": used_pct}})
    cap, pct = subject.get_effective_cap()
    assert cap == expected_cap
    assert pct == float(used_pct)


def test_get_effective_cap_returns_the_percentage_as_a_float(subject, sandbox):
    _write_usage(subject, {"five_hour": {"used_pct": 50}})
    cap, pct = subject.get_effective_cap()
    assert isinstance(cap, int)
    assert isinstance(pct, float)


def test_get_effective_cap_coerces_a_string_percentage(subject, sandbox):
    """No validation: a JSON string is accepted wherever a number was meant."""
    _write_usage(subject, {"five_hour": {"used_pct": "80"}})
    assert subject.get_effective_cap() == (1, 80.0)


def test_get_effective_cap_lets_a_negative_percentage_through(subject, sandbox):
    _write_usage(subject, {"five_hour": {"used_pct": -5}})
    assert subject.get_effective_cap() == (3, -5.0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"five_hour": None},
        {"five_hour": {}},
        {"five_hour": []},                       # falsy -> `or {}`
        {"five_hour": {"used_pct": None}},
        {"five_hour": {"used_pct": 0}},
        {"five_hour": {"used_pct": ""}},         # falsy -> `or 0`
        {"seven_day": {"used_pct": 99}},         # only the five-hour window is consulted
    ],
)
def test_get_effective_cap_falls_back_to_the_full_cap_at_zero_percent(
    subject, sandbox, payload
):
    _write_usage(subject, payload)
    assert subject.get_effective_cap() == (3, 0.0)


def test_get_effective_cap_treats_a_missing_usage_file_as_zero_percent(subject, sandbox):
    """FOUND_BUGS: `read_json` swallows the missing file, so "no data" reads as "no usage".

    The safety net fails open — if the usage tracker has never run, or its file is
    corrupt, the orchestrator launches at full concurrency.
    """
    assert not subject.USAGE_JSON.exists()
    assert subject.get_effective_cap() == (3, 0.0)


def test_get_effective_cap_treats_a_corrupt_usage_file_as_zero_percent(subject, sandbox):
    subject.USAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    subject.USAGE_JSON.write_text("{not json", encoding="utf-8")
    assert subject.get_effective_cap() == (3, 0.0)


def test_get_effective_cap_raises_when_five_hour_is_a_truthy_non_mapping(subject, sandbox):
    """The `or {}` guard only covers falsy values; a string reaches `.get`."""
    _write_usage(subject, {"five_hour": "92"})
    with pytest.raises(AttributeError):
        subject.get_effective_cap()


def test_get_effective_cap_raises_on_a_top_level_json_list(subject, sandbox):
    """`read_json` is annotated `-> dict` but hands back the list, which has no `.get`."""
    _write_usage(subject, [{"five_hour": {"used_pct": 99}}])
    with pytest.raises(AttributeError):
        subject.get_effective_cap()


def test_get_effective_cap_raises_on_a_non_numeric_percentage(subject, sandbox):
    _write_usage(subject, {"five_hour": {"used_pct": "abc"}})
    with pytest.raises(ValueError):
        subject.get_effective_cap()


def test_get_effective_cap_takes_no_arguments(subject, sandbox):
    _write_usage(subject, {"five_hour": {"used_pct": 10}})
    with pytest.raises(TypeError):
        subject.get_effective_cap({"five_hour": {"used_pct": 99}})


def test_get_effective_cap_rereads_the_file_on_every_call(subject, sandbox):
    _write_usage(subject, {"five_hour": {"used_pct": 10}})
    assert subject.get_effective_cap()[0] == 3
    _write_usage(subject, {"five_hour": {"used_pct": 95}})
    assert subject.get_effective_cap()[0] == 0


# ====================================================================================
# _resolve_limit_reset — tier 1: usage.json's authoritative epoch
# ====================================================================================


def test_resolve_limit_reset_prefers_a_future_epoch_from_usage_json(subject, sandbox, clock):
    _write_usage(subject, {"five_hour": {"resets_at": (FIXED + timedelta(hours=3)).timestamp()}})
    assert subject._resolve_limit_reset("") == "2026-03-10T15:34:56Z"


def test_resolve_limit_reset_epoch_beats_a_parseable_message(subject, sandbox, clock):
    _write_usage(subject, {"five_hour": {"resets_at": (FIXED + timedelta(hours=3)).timestamp()}})
    assert subject._resolve_limit_reset("resets 11pm (UTC)") == "2026-03-10T15:34:56Z"


def test_resolve_limit_reset_accepts_a_string_epoch(subject, sandbox, clock):
    _write_usage(subject, {"five_hour": {"resets_at": str(int((FIXED + timedelta(hours=2)).timestamp()))}})
    assert subject._resolve_limit_reset("") == "2026-03-10T14:34:56Z"


def test_resolve_limit_reset_truncates_a_fractional_epoch_to_whole_seconds(
    subject, sandbox, clock
):
    _write_usage(subject, {"five_hour": {"resets_at": FIXED.timestamp() + 7200.75}})
    assert subject._resolve_limit_reset("") == "2026-03-10T14:34:56Z"


@pytest.mark.parametrize(
    "payload",
    [
        {},                                                  # no five_hour
        {"five_hour": None},
        {"five_hour": {}},                                   # no resets_at
        {"five_hour": {"resets_at": None}},
        {"five_hour": {"resets_at": 0}},                     # falsy -> skipped
        {"five_hour": {"resets_at": ""}},
        {"five_hour": {"resets_at": 1000.0}},                # long past
        {"five_hour": {"resets_at": FIXED.timestamp()}},     # exactly now: `>` is strict
        {"five_hour": {"resets_at": "soon"}},                # float() raises -> swallowed
        {"five_hour": "not-a-mapping"},                      # .get raises -> swallowed
        [{"five_hour": {"resets_at": 9e9}}],                 # list -> .get raises -> swallowed
    ],
)
def test_resolve_limit_reset_falls_through_when_the_epoch_is_unusable(
    subject, sandbox, clock, payload
):
    _write_usage(subject, payload)
    assert subject._resolve_limit_reset("") == PLUS_1H


def test_resolve_limit_reset_swallows_a_broken_usage_file(subject, sandbox, clock):
    """FOUND_BUGS: the whole tier-1 lookup is one bare `except Exception: pass`.

    A malformed file, a wrong-typed document and a genuinely absent reset time are
    indistinguishable, and none of them is logged.
    """
    subject.USAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    subject.USAGE_JSON.write_text("{not json", encoding="utf-8")
    assert subject._resolve_limit_reset("") == PLUS_1H


# ====================================================================================
# _resolve_limit_reset — tier 2: the prose regex
# ====================================================================================


# Frozen clock: 2026-03-10 12:34:56 UTC == 08:34:56 in America/New_York (UTC-4).
RESET_MESSAGES = [
    # (tail, expected UTC ISO, why)
    ("resets 4:30pm (America/New_York)", "2026-03-10T20:30:00Z", "hh:mm, pm, named zone"),
    ("resets 4pm (America/New_York)", "2026-03-10T20:00:00Z", "no minutes -> :00"),
    ("resets 11am (America/New_York)", "2026-03-10T15:00:00Z", "am, later today"),
    ("resets 4:30am (America/New_York)", "2026-03-11T08:30:00Z", "already past -> tomorrow"),
    ("resets 8:34am (America/New_York)", "2026-03-11T12:34:00Z", "same minute counts as past"),
    ("resets 8:35am (America/New_York)", "2026-03-10T12:35:00Z", "one minute ahead is today"),
    ("resets 12am (UTC)", "2026-03-11T00:00:00Z", "12am -> hour 0"),
    ("resets 12pm (UTC)", "2026-03-11T12:00:00Z", "12pm stays 12, and 12:00 is past"),
    ("resets 12:35pm (UTC)", "2026-03-10T12:35:00Z", "12:35pm is still ahead of 12:34:56"),
    ("resets 11:59pm (UTC)", "2026-03-10T23:59:00Z", "pm adds 12"),
    ("resets 1am (UTC)", "2026-03-11T01:00:00Z", "am before now -> tomorrow"),
    ("resets 22:15 (UTC)", "2026-03-10T22:15:00Z", "24h clock, no meridiem"),
    ("resets 4 (UTC)", "2026-03-11T04:00:00Z", "bare hour is read as 04:00, not 4pm"),
    ("reset 4pm (UTC)", "2026-03-10T16:00:00Z", "singular `reset` also matches"),
    ("RESETS 4PM (UTC)", "2026-03-10T16:00:00Z", "case-insensitive"),
    ("resets\t4pm (UTC)", "2026-03-10T16:00:00Z", "any whitespace after the verb"),
    ("resets 4pm(UTC)", "2026-03-10T16:00:00Z", "no space before the zone"),
    ("resets 4:30 pm (UTC)", "2026-03-10T16:30:00Z", "space before the meridiem"),
    ("quota exceeded, resets 4pm (UTC), sorry", "2026-03-10T16:00:00Z", "embedded in prose"),
    ("presets 4pm (UTC)", "2026-03-10T16:00:00Z", "matches inside an unrelated word"),
    ("resets 3pm (UTC) or maybe resets 5pm (UTC)", "2026-03-10T15:00:00Z", "first match wins"),
    ("resets 4:30pm (Europe/London)", "2026-03-10T16:30:00Z", "zone is honoured, London is UTC+0"),
]


@pytest.mark.parametrize(
    "tail, expected",
    [(t, e) for t, e, _ in RESET_MESSAGES],
    ids=[why for _, _, why in RESET_MESSAGES],
)
def test_resolve_limit_reset_parses_every_message_shape(subject, sandbox, clock, tail, expected):
    assert subject._resolve_limit_reset(tail) == expected


UNPARSEABLE_MESSAGES = [
    ("", "empty tail"),
    ("nothing to see here", "no match at all"),
    ("resets soon", "no digits"),
    ("resets4pm (UTC)", "no whitespace after the verb"),
    ("reset at 4pm (UTC)", "a word between the verb and the hour"),
    ("resets 4:30pm (Nowhere/Fake)", "ZoneInfo raises on an unknown zone"),
    ("resets 4:30pm (PDT)", "abbreviations are not IANA zone keys"),
    ("resets 25 (UTC)", "hour 25 -> replace() raises ValueError"),
    ("resets 4:99 (UTC)", "minute 99 -> replace() raises ValueError"),
    ("resets 99 (UTC)", "hour 99 -> replace() raises ValueError"),
]


@pytest.mark.parametrize(
    "tail",
    [t for t, _ in UNPARSEABLE_MESSAGES],
    ids=[why for _, why in UNPARSEABLE_MESSAGES],
)
def test_resolve_limit_reset_falls_back_to_one_hour_when_it_cannot_parse(
    subject, sandbox, clock, tail
):
    assert subject._resolve_limit_reset(tail) == PLUS_1H


def test_resolve_limit_reset_reads_a_bare_hour_as_am_and_can_sleep_16_extra_hours(
    subject, sandbox, clock
):
    """FOUND_BUGS: `resets 4` with no meridiem is 04:00, so a 4pm reset is read as 4am.

    Because 04:00 is already past at the frozen 08:34 local, it rolls to tomorrow — the
    orchestrator would sit out an extra ~20 hours rather than resuming at 16:00.
    """
    assert subject._resolve_limit_reset("resets 4 (UTC)") == "2026-03-11T04:00:00Z"
    assert subject._resolve_limit_reset("resets 4pm (UTC)") == "2026-03-10T16:00:00Z"


def test_resolve_limit_reset_matches_the_regex_inside_an_unrelated_word(
    subject, sandbox, clock
):
    """FOUND_BUGS: `resets?` is unanchored, so `presets`, `resetsomething` and any other
    word containing the substring drives the backoff time."""
    assert subject._resolve_limit_reset("presets 9am (UTC)") == "2026-03-11T09:00:00Z"


def test_resolve_limit_reset_uses_the_local_zone_when_the_message_names_none(
    subject, sandbox, clock
):
    """No zone in the message means *the orchestrator's* zone, not the API's."""
    assert subject._resolve_limit_reset("resets 4pm") == "2026-03-10T20:00:00Z"
    assert subject._resolve_limit_reset("resets 9am") == "2026-03-10T13:00:00Z"


def test_resolve_limit_reset_always_returns_a_zulu_iso_string(subject, sandbox, clock):
    for tail in ("", "resets 4pm (UTC)", "resets 4:99"):
        value = subject._resolve_limit_reset(tail)
        assert isinstance(value, str)
        assert datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def test_resolve_limit_reset_raises_on_a_non_string_tail(subject, sandbox, clock):
    """The regex search is outside every `try`, so a None tail is not survivable."""
    with pytest.raises(TypeError):
        subject._resolve_limit_reset(None)


# ====================================================================================
# _scan_impl_log_for_limit
# ====================================================================================


def _workspace(sandbox, name="impl-T1-1"):
    ws = sandbox.workspaces / name
    ws.mkdir(parents=True, exist_ok=True)
    return ws


NO_LIMIT = {"limit": False, "reset_iso": None, "evidence": ""}


def test_scan_impl_log_returns_the_negative_shape_when_the_workspace_is_empty(
    subject, sandbox, clock
):
    assert subject._scan_impl_log_for_limit(_workspace(sandbox)) == NO_LIMIT


def test_scan_impl_log_returns_the_negative_shape_when_the_workspace_does_not_exist(
    subject, sandbox, clock
):
    assert subject._scan_impl_log_for_limit(sandbox.workspaces / "nope") == NO_LIMIT


def test_scan_impl_log_returns_the_negative_shape_when_impl_log_is_a_directory(
    subject, sandbox, clock
):
    ws = _workspace(sandbox)
    (ws / "impl.log").mkdir()
    assert subject._scan_impl_log_for_limit(ws) == NO_LIMIT


def test_scan_impl_log_returns_the_negative_shape_for_an_empty_file(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text("", encoding="utf-8")
    assert subject._scan_impl_log_for_limit(ws) == NO_LIMIT


def test_scan_impl_log_returns_the_negative_shape_for_an_ordinary_log(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text("running tests\nall green\n", encoding="utf-8")
    assert subject._scan_impl_log_for_limit(ws) == NO_LIMIT


LIMIT_PHRASES = [
    "you hit your limit",
    "you hit the limit",
    "you hit your usage limit",
    "you hit the usage limit",
    "usage limit reached",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "overloaded_error",
    "RATE LIMIT",
    "Too Many Requests",
]


@pytest.mark.parametrize("phrase", LIMIT_PHRASES)
def test_scan_impl_log_detects_every_limit_phrase(subject, sandbox, clock, phrase):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text(f"work\n{phrase}\n", encoding="utf-8")
    result = subject._scan_impl_log_for_limit(ws)
    assert result["limit"] is True
    assert result["evidence"] == phrase
    assert result["reset_iso"] == PLUS_1H


@pytest.mark.parametrize("phrase", ["limit", "limited", "rate  limit", "429", "quota exceeded"])
def test_scan_impl_log_ignores_near_misses(subject, sandbox, clock, phrase):
    """FOUND_BUGS: a bare HTTP 429 and the word `quota` are not recognised.

    `rate[\\s-]?limit` also permits at most one separator, so `rate  limit` (two spaces,
    a common log-wrapping artefact) is missed.
    """
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text(f"work\n{phrase}\n", encoding="utf-8")
    assert subject._scan_impl_log_for_limit(ws)["limit"] is False


def test_scan_impl_log_only_reads_the_last_25_lines(subject, sandbox, clock):
    """FOUND_BUGS: a limit hit early in a chatty run scrolls out of the window."""
    ws = _workspace(sandbox)
    lines = ["rate limit"] + [f"line {i}" for i in range(25)]
    (ws / "impl.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert subject._scan_impl_log_for_limit(ws)["limit"] is False

    lines = ["rate limit"] + [f"line {i}" for i in range(24)]
    (ws / "impl.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert subject._scan_impl_log_for_limit(ws)["limit"] is True


def test_scan_impl_log_reports_the_first_matching_line_as_evidence(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text(
        "boot\nfirst: rate limit\nnoise\nsecond: too many requests\n", encoding="utf-8"
    )
    assert subject._scan_impl_log_for_limit(ws)["evidence"] == "first: rate limit"


def test_scan_impl_log_strips_and_truncates_the_evidence_to_200_chars(subject, sandbox, clock):
    ws = _workspace(sandbox)
    line = "   rate limit " + "x" * 400 + "   "
    (ws / "impl.log").write_text(line + "\n", encoding="utf-8")
    evidence = subject._scan_impl_log_for_limit(ws)["evidence"]
    assert len(evidence) == 200
    assert evidence.startswith("rate limit x")


def test_scan_impl_log_falls_back_to_the_tail_when_the_match_spans_a_newline(
    subject, sandbox, clock
):
    """FOUND_BUGS: `rate[\\s-]?limit` lets `\\s` match a newline.

    The whole-tail search then succeeds while the per-line search finds nothing, so the
    `next(..., tail[-200:])` default fires and the "evidence" is an arbitrary 200-char
    window rather than the offending line.
    """
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text("rate\nlimit\n", encoding="utf-8")
    result = subject._scan_impl_log_for_limit(ws)
    assert result["limit"] is True
    assert result["evidence"] == "rate\nlimit"


def test_scan_impl_log_resolves_the_reset_time_from_the_same_tail(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text(
        "usage limit reached, resets 4pm (UTC)\n", encoding="utf-8"
    )
    result = subject._scan_impl_log_for_limit(ws)
    assert result["reset_iso"] == "2026-03-10T16:00:00Z"
    assert result["evidence"] == "usage limit reached, resets 4pm (UTC)"


def test_scan_impl_log_prefers_the_usage_json_epoch_over_the_log(subject, sandbox, clock):
    _write_usage(subject, {"five_hour": {"resets_at": (FIXED + timedelta(hours=2)).timestamp()}})
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text("rate limit, resets 4pm (UTC)\n", encoding="utf-8")
    assert subject._scan_impl_log_for_limit(ws)["reset_iso"] == "2026-03-10T14:34:56Z"


def test_scan_impl_log_survives_undecodable_bytes(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_bytes(b"\xff\xfe rate limit\n")
    assert subject._scan_impl_log_for_limit(ws)["limit"] is True


def test_scan_impl_log_returns_exactly_three_keys(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text("rate limit\n", encoding="utf-8")
    assert set(subject._scan_impl_log_for_limit(ws)) == {"limit", "reset_iso", "evidence"}


def test_scan_impl_log_writes_nothing(subject, sandbox, clock):
    ws = _workspace(sandbox)
    (ws / "impl.log").write_text("rate limit\n", encoding="utf-8")
    subject._scan_impl_log_for_limit(ws)
    assert sorted(p.name for p in ws.iterdir()) == ["impl.log"]


# ====================================================================================
# _pause_for_usage_limit
# ====================================================================================


@pytest.fixture
def notifications(subject, monkeypatch):
    """Capture Telegram notifications; nothing may reach the network."""
    sent = []
    monkeypatch.setattr(subject, "notify_telegram", lambda *a, **k: sent.append((a, k)))
    return sent


RESET = "2026-03-10T20:00:00Z"


def test_pause_writes_the_sentinel_sets_paused_until_journals_and_notifies(
    subject, sandbox, clock, notifications
):
    _write_state(sandbox, {"version": "1"})
    ws = _workspace(sandbox)

    subject._pause_for_usage_limit("T1", RESET, "rate limit", ws)

    assert (ws / "USAGE_LIMIT_PARKED").read_text() == RESET
    assert _read_state_file(sandbox)["paused_until"] == RESET

    record = _journal_records(sandbox)[-1]
    assert record["event"] == "usage_limit_backoff"
    assert record["detail"] == f"T1 paused_until={RESET} ev=rate limit"
    assert record["session_id"] == ws.name

    assert len(notifications) == 1


def test_pause_returns_none(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1"})
    assert subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox)) is None


def test_pause_notifies_only_once_per_workspace(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1"})
    ws = _workspace(sandbox)
    subject._pause_for_usage_limit("T1", RESET, "e", ws)
    subject._pause_for_usage_limit("T1", "2026-03-10T21:00:00Z", "e", ws)
    subject._pause_for_usage_limit("T1", "2026-03-10T22:00:00Z", "e", ws)
    assert len(notifications) == 1
    assert len(_journal_records(sandbox)) == 3
    assert (ws / "USAGE_LIMIT_PARKED").read_text() == "2026-03-10T22:00:00Z"


def test_pause_dedup_is_per_workspace_not_per_task(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1"})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox, "impl-T1-1"))
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox, "impl-T1-2"))
    assert len(notifications) == 2


def test_pause_never_shortens_a_later_existing_pause(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1", "paused_until": "2026-03-10T23:00:00Z"})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["paused_until"] == "2026-03-10T23:00:00Z"


def test_pause_extends_an_earlier_existing_pause(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1", "paused_until": "2026-03-10T14:00:00Z"})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["paused_until"] == RESET


@pytest.mark.parametrize("existing", [None, "", 0, False])
def test_pause_overwrites_a_falsy_existing_pause(
    subject, sandbox, clock, notifications, existing
):
    _write_state(sandbox, {"version": "1", "paused_until": existing})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["paused_until"] == RESET


def test_pause_compares_strings_so_a_numeric_epoch_wedges_the_pause_forever(
    subject, sandbox, clock, notifications
):
    """FOUND_BUGS: `str(cur) < reset_iso` is a lexicographic compare, not a time compare.

    An epoch-shaped `paused_until` (anything the digits of which sort above `"2"`) is never
    replaced, so every subsequent usage-limit backoff is silently dropped and the pause
    stays at whatever that value decoded to.
    """
    _write_state(sandbox, {"version": "1", "paused_until": 9999999999})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["paused_until"] == 9999999999


def test_pause_replaces_a_small_numeric_epoch_because_1_sorts_below_2(
    subject, sandbox, clock, notifications
):
    _write_state(sandbox, {"version": "1", "paused_until": 1000000000})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["paused_until"] == RESET


def test_pause_truncates_the_evidence_to_80_chars_in_the_journal(
    subject, sandbox, clock, notifications
):
    _write_state(sandbox, {"version": "1"})
    evidence = "y" * 200
    subject._pause_for_usage_limit("T1", RESET, evidence, _workspace(sandbox))
    detail = _journal_records(sandbox)[-1]["detail"]
    assert detail == f"T1 paused_until={RESET} ev=" + "y" * 80


def test_pause_renders_the_reset_time_in_the_local_zone_for_the_notification(
    subject, sandbox, clock, notifications
):
    _write_state(sandbox, {"version": "1"})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    message = notifications[0][0][0]
    assert "16:00" in message   # 20:00Z is 16:00 in America/New_York
    assert "T1" in message


def test_pause_falls_back_to_the_raw_string_when_the_reset_time_is_unparseable(
    subject, sandbox, clock, notifications
):
    """A garbage `reset_iso` is stored and shown verbatim; nothing validates it."""
    _write_state(sandbox, {"version": "1"})
    subject._pause_for_usage_limit("T1", "whenever", "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["paused_until"] == "whenever"
    assert "whenever" in notifications[0][0][0]


def test_pause_stamps_updated_at_via_write_state(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1"})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert "updated_at" in _read_state_file(sandbox)


def test_pause_preserves_the_rest_of_the_state_document(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1", "in_flight": [{"task_id": "T9"}]})
    subject._pause_for_usage_limit("T1", RESET, "e", _workspace(sandbox))
    assert _read_state_file(sandbox)["in_flight"] == [{"task_id": "T9"}]


def test_pause_notifies_every_time_when_the_sentinel_cannot_be_written(
    subject, sandbox, clock, notifications
):
    """FOUND_BUGS: the dedup sentinel write is wrapped in `except Exception: pass`.

    If the workspace directory is gone the sentinel never lands, `first` stays True, and
    the "notify once" guarantee degrades to "notify on every poll cycle".
    """
    _write_state(sandbox, {"version": "1"})
    missing = sandbox.workspaces / "gone"
    assert not missing.exists()

    subject._pause_for_usage_limit("T1", RESET, "e", missing)
    subject._pause_for_usage_limit("T1", RESET, "e", missing)

    assert not missing.exists()
    assert len(notifications) == 2
    assert _read_state_file(sandbox)["paused_until"] == RESET


def test_pause_writes_the_sentinel_before_it_reads_state_so_a_crash_leaves_it_behind(
    subject, sandbox, clock, notifications
):
    """FOUND_BUGS: no state.json means `read_state` raises *after* the sentinel is written.

    The workspace is then permanently marked as notified even though no notification was
    ever sent and no pause was recorded.
    """
    ws = _workspace(sandbox)
    assert not (sandbox.orch_dir / "state.json").exists()
    with pytest.raises(FileNotFoundError):
        subject._pause_for_usage_limit("T1", RESET, "e", ws)
    assert (ws / "USAGE_LIMIT_PARKED").read_text() == RESET
    assert notifications == []


def test_pause_sentinel_is_overwritten_not_appended(subject, sandbox, clock, notifications):
    _write_state(sandbox, {"version": "1"})
    ws = _workspace(sandbox)
    subject._pause_for_usage_limit("T1", RESET, "e", ws)
    subject._pause_for_usage_limit("T1", "2026-03-11T05:00:00Z", "e", ws)
    assert (ws / "USAGE_LIMIT_PARKED").read_text() == "2026-03-11T05:00:00Z"


# ====================================================================================
# _paused_until_epoch
# ====================================================================================


def test_paused_until_epoch_parses_a_zulu_iso_string(subject):
    expected = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp()
    assert subject._paused_until_epoch({"paused_until": "2026-03-10T20:00:00Z"}) == expected


def test_paused_until_epoch_returns_a_float(subject):
    assert isinstance(subject._paused_until_epoch({"paused_until": "2026-03-10T20:00:00Z"}), float)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"paused_until": None},
        {"paused_until": ""},
        {"paused_until": 0},
        {"paused_until": False},
        {"paused_until": []},
    ],
)
def test_paused_until_epoch_returns_zero_for_a_falsy_value(subject, state):
    assert subject._paused_until_epoch(state) == 0.0


@pytest.mark.parametrize(
    "value",
    [
        "whenever",
        "2026-03-10T20:00:00",          # no Z
        "2026-03-10T20:00:00+00:00",    # a real offset
        "2026-03-10 20:00:00Z",
        1773172800.0,                   # an epoch float, stringified and then unparseable
        ["2026-03-10T20:00:00Z"],
        datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc),
    ],
)
def test_paused_until_epoch_returns_zero_for_anything_it_cannot_parse(subject, value):
    """FOUND_BUGS: "unset" and "corrupt" both return 0.0, i.e. "not paused".

    A `paused_until` the loop cannot read is treated as no pause at all, so the failure
    mode of this guard is to resume immediately.
    """
    assert subject._paused_until_epoch({"paused_until": value}) == 0.0


def test_paused_until_epoch_stringifies_before_parsing(subject):
    """`str(val)` means a value that merely *renders* as the format is accepted."""

    class Rendered:
        def __str__(self):
            return "2026-03-10T20:00:00Z"

    expected = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp()
    assert subject._paused_until_epoch({"paused_until": Rendered()}) == expected


def test_paused_until_epoch_reads_the_value_as_utc(subject, local_tz):
    """The local zone is irrelevant: the string is stamped UTC before `.timestamp()`."""
    assert subject._paused_until_epoch({"paused_until": "1970-01-01T00:00:01Z"}) == 1.0


@pytest.mark.parametrize("state", [None, [], "paused_until", 7])
def test_paused_until_epoch_raises_on_a_non_mapping_state(subject, state):
    """`state.get(...)` sits outside the `try`, so a wrong-typed state is not survivable."""
    with pytest.raises(AttributeError):
        subject._paused_until_epoch(state)


def test_paused_until_epoch_does_not_mutate_the_state(subject):
    state = {"paused_until": "2026-03-10T20:00:00Z"}
    subject._paused_until_epoch(state)
    assert state == {"paused_until": "2026-03-10T20:00:00Z"}
