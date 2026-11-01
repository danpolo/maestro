"""P12B F1: a refusing free-tier endpoint is classified into a failure kind, with its hints.

Every case is a scripted transport — no socket — returning what a provider really sends:
a status, a JSON body, and (optionally) the headers that say when to come back.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from maestro.backends import model_api as api
from maestro.workflows.failures import FAILURE_KINDS


class OneAnswer:
    def __init__(self, answer=None, *, raises=None):
        self.answer = answer
        self.raises = raises

    def post(self, url, *, headers, body, timeout):
        if self.raises is not None:
            raise self.raises
        return self.answer


def _api(transport, wire="openai_chat") -> api.ModelAPI:
    endpoint = api.ModelEndpoint(
        endpoint_id="free_api", base_url="https://api.example.test/v1", model="free-1",
        api_key_env="MAESTRO_FREE_KEY", wire=wire,
    )
    return api.ModelAPI(endpoint, transport=transport, environ={"MAESTRO_FREE_KEY": "k"})


def _refusal(transport, wire="openai_chat") -> api.TransportError:
    with pytest.raises(api.TransportError) as caught:
        _api(transport, wire).complete([api.Message(role="user", content="go")])
    return caught.value


def test_a_429_with_retry_after_is_a_rate_limit_carrying_the_hint():
    error = _refusal(OneAnswer((429, {"error": {"message": "slow down"}}, {"Retry-After": "120"})))
    assert isinstance(error, api.RateLimited)
    assert (error.failure_kind, error.status, error.retry_after_sec) == ("rate_limited", 429, 120)


def test_a_quota_body_is_a_rate_limit_whatever_the_status_and_a_daily_one_is_spent_quota():
    error = _refusal(OneAnswer((403, {"error": {"status": "RESOURCE_EXHAUSTED",
                                                "message": "Quota exceeded for requests per day"}})),
                     wire="gemini_generate")
    assert isinstance(error, api.RateLimited)
    assert error.failure_kind == "quota_exhausted"


@pytest.mark.parametrize("status, cls, kind", [
    (401, api.AuthFailed, "auth_failed"),
    (403, api.AuthFailed, "auth_failed"),
    (500, api.ProviderUnavailable, "provider_unavailable"),
    (503, api.ProviderUnavailable, "provider_unavailable"),
    (529, api.ProviderUnavailable, "provider_unavailable"),
])
def test_status_codes_map_to_their_kinds(status, cls, kind):
    error = _refusal(OneAnswer((status, {"error": "nope"})))
    assert type(error) is cls and error.failure_kind == kind and error.status == status


@pytest.mark.parametrize("wire, body", [
    ("openai_chat", {"error": {"code": "context_length_exceeded",
                               "message": "This model's maximum context length is 8192 tokens"}}),
    ("anthropic_messages", {"type": "error", "error": {
        "type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000"}}),
    ("gemini_generate", {"error": {"code": 400, "message":
                                   "The input token count (1200000) exceeds the maximum number of tokens allowed"}}),
])
def test_a_400_naming_the_context_length_is_a_context_overflow_per_wire(wire, body):
    error = _refusal(OneAnswer((400, body)), wire=wire)
    assert isinstance(error, api.ContextOverflow)
    assert error.failure_kind == "context_overflow"


def test_a_400_for_another_reason_stays_unclassified():
    error = _refusal(OneAnswer((400, {"error": {"message": "unknown field `temperature2`"}})))
    assert type(error) is api.TransportError
    assert error.failure_kind == "endpoint_unavailable"


@pytest.mark.parametrize("raised, cls", [
    (TimeoutError("read timed out"), api.RequestTimeout),
    (ConnectionError("connection refused"), api.ProviderUnavailable),
    (api.RequestTimeout("from the transport"), api.RequestTimeout),
])
def test_transport_exceptions_are_classified(raised, cls):
    error = _refusal(OneAnswer(raises=raised))
    assert type(error) is cls


def test_every_typed_kind_is_in_the_vocabulary():
    for cls in (api.RateLimited, api.AuthFailed, api.ProviderUnavailable,
                api.RequestTimeout, api.ContextOverflow):
        assert cls.failure_kind in FAILURE_KINDS
    assert api.RateLimited("x", failure_kind="quota_exhausted").failure_kind in FAILURE_KINDS


def test_a_two_element_transport_answer_still_works():
    turn = _api(OneAnswer((200, {"choices": [{"message": {"content": "hi"}}]}))).complete(
        [api.Message(role="user", content="go")]
    )
    assert turn.text == "hi"


NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("headers, expected", [
    ({"retry-after": "30"}, (30, None)),
    ({"Retry-After": "Thu, 17 Sep 2026 12:02:00 GMT"}, (120, None)),
    ({"Retry-After": "soon"}, (None, None)),
    ({"anthropic-ratelimit-requests-reset": "2026-09-17T13:00:00Z"}, (None, "2026-09-17T13:00:00Z")),
    ({"x-ratelimit-reset": "1789646400"}, (None, "2026-09-17T12:00:00Z")),
    ({"x-ratelimit-reset-requests": "6m0s"}, (360, "2026-09-17T12:06:00Z")),
    ({"x-ratelimit-reset-tokens": "250ms", "retry-after": "5"}, (5, "2026-09-17T12:00:00Z")),
    ({"x-ratelimit-reset-requests": "later"}, (None, None)),
    ({}, (None, None)),
])
def test_retry_headers_are_parsed_or_left_unknown(headers, expected):
    assert api.parse_retry_headers(headers, now=NOW) == expected


def _openai_call(arguments):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": arguments}}
    ]}}]}


@pytest.mark.parametrize("arguments, malformed", [
    ('{"path": "a.py"}', False),
    ("", False),
    ("{not json", True),
    ('["a.py"]', True),
])
def test_undecodable_tool_arguments_are_flagged(arguments, malformed):
    turn = _api(OneAnswer((200, _openai_call(arguments)))).complete(
        [api.Message(role="user", content="go")]
    )
    [call] = turn.tool_calls
    assert call.malformed is malformed
    assert "malformed" not in call.to_dict()
