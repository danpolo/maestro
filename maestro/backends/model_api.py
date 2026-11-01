"""The free-tier HTTP adapter: one wire shape per provider family, no provider SDK (P7).

`03_BACKENDS_CATALOG_AND_ROUTING.md` §5.4 asks for "standard `requests` or `httpx` over
HTTPS; zero provider-specific SDK dependencies", and the reason is not dependency
hygiene. A vendor SDK carries its own credential discovery — an ambient key file, a
metadata endpoint, a cached session — and `[INV-03]` says maestro never reads a
credential it was not explicitly handed. An SDK that helpfully finds a token in
`~/.config` has crossed that boundary on maestro's behalf, from inside a call site that
looks like `client.messages.create(...)`. A POST built here cannot: the only place an API
key enters this module is `os.environ[endpoint.api_key_env]`, and `ModelEndpoint` refuses
an `api_key_env` that looks like a path so that "read the key from this file" cannot be
spelled at all.

Three provider families are normalised, chosen because they are the shapes a free tier is
actually served over today: OpenAI-compatible chat completions, Anthropic messages, and
Gemini `generateContent`. Each is ~20 lines of request building and ~20 of response
reading, and every one of them produces the same `ModelTurn`. The supervisor loop in
`model_runtime.py` therefore never branches on the provider — which is the property that
makes "identical task spec succeeds across paid, free and hybrid backends" testable at
all.

Everything is injectable: `transport` for the POST, `environ` for the key. The suite runs
the whole runtime against a scripted transport and never opens a socket.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

__all__ = [
    "WIRE_FORMATS",
    "ModelAPIError",
    "CredentialMissing",
    "TransportError",
    "RateLimited",
    "AuthFailed",
    "ProviderUnavailable",
    "RequestTimeout",
    "ContextOverflow",
    "classify_http_failure",
    "parse_retry_headers",
    "ModelEndpoint",
    "ToolSchema",
    "ToolCall",
    "Message",
    "ModelTurn",
    "Transport",
    "RequestsTransport",
    "ModelAPI",
    "capability_entry",
]

#: The provider families this module can speak. Closed: a fourth is a function pair added
#: here, not a `wire` string a project can invent in its configuration.
WIRE_FORMATS: tuple[str, ...] = ("openai_chat", "anthropic_messages", "gemini_generate")

#: Roles the normalised conversation uses. `tool` is the result of a dispatched call
#: coming back; every wire renders it differently and none of them omits it.
_ROLES = frozenset({"system", "user", "assistant", "tool"})


class ModelAPIError(RuntimeError):
    """Any failure to obtain a turn from an endpoint."""


class CredentialMissing(ModelAPIError):
    """The named environment variable is unset (`[INV-03]`: there is no fallback)."""


class TransportError(ModelAPIError):
    """The endpoint answered with a non-2xx status, or nothing parseable.

    The base class is the unclassified case. Its subclasses name a P12B failure kind
    (`maestro.workflows.failures.FAILURE_KINDS`) and carry what the endpoint said about
    when to try again; `None` means it said nothing.
    """

    failure_kind = "endpoint_unavailable"

    def __init__(
        self,
        message: str = "",
        *,
        status: Optional[int] = None,
        retry_after_sec: Optional[int] = None,
        reset_at: Optional[str] = None,
        failure_kind: Optional[str] = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after_sec = retry_after_sec
        self.reset_at = reset_at
        if failure_kind:
            self.failure_kind = failure_kind


class RateLimited(TransportError):
    """429, or a quota body. `failure_kind` is `quota_exhausted` for a spent period quota."""

    failure_kind = "rate_limited"


class AuthFailed(TransportError):
    """401/403: the key was refused."""

    failure_kind = "auth_failed"


class ProviderUnavailable(TransportError):
    """5xx, or no connection at all."""

    failure_kind = "provider_unavailable"


class RequestTimeout(TransportError):
    """The request did not finish inside the endpoint's timeout."""

    failure_kind = "timeout"


class ContextOverflow(TransportError):
    """The request was refused for its length. A routing fact, not a compaction trigger."""

    failure_kind = "context_overflow"


# ── classifying a refusal ────────────────────────────────────────────────────────────

#: A body that says the caller is over a limit, whatever status it came with.
_QUOTA_RE = re.compile(
    r"quota|rate[\s_-]?limit|resource[_\s]exhausted|too many requests", re.I
)
#: …and one that says the limit is a period quota, not a burst limit.
_PERIOD_QUOTA_RE = re.compile(r"daily|per[\s_-]?day|monthly|per[\s_-]?month", re.I)
#: Per wire: the words a provider uses to refuse a request for its length.
_CONTEXT_RE: Mapping[str, "re.Pattern[str]"] = {
    "openai_chat": re.compile(
        r"context_length_exceeded|maximum context length|too many tokens|context window", re.I
    ),
    "anthropic_messages": re.compile(
        r"prompt is too long|context window|input length|too many tokens", re.I
    ),
    "gemini_generate": re.compile(
        r"input token count|exceeds the maximum number of tokens|context window", re.I
    ),
}


def classify_http_failure(
    wire: str,
    status: int,
    payload: Any,
    headers: Optional[Mapping[str, str]],
    *,
    message: str,
) -> TransportError:
    """The typed error for one non-2xx answer (P12B F1). Unrecognised → the base class."""
    body = _excerpt(payload, limit=4000)
    retry_after, reset_at = parse_retry_headers(headers or {})
    extra = {"status": status, "retry_after_sec": retry_after, "reset_at": reset_at}
    if status == 429 or (400 <= status < 500 and _QUOTA_RE.search(body)):
        kind = "quota_exhausted" if _PERIOD_QUOTA_RE.search(body) else "rate_limited"
        return RateLimited(message, failure_kind=kind, **extra)
    context_re = _CONTEXT_RE.get(wire)
    if status in (400, 413) and context_re is not None and context_re.search(body):
        return ContextOverflow(message, **extra)
    if status in (401, 403):
        return AuthFailed(message, **extra)
    if status >= 500:
        return ProviderUnavailable(message, **extra)
    return TransportError(message, **extra)


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def _zulu(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_duration(text: str) -> Optional[float]:
    """A Go-style duration (`6m0s`, `1s`, `250ms`) in seconds, or `None`."""
    text = text.strip()
    if not text or _DURATION_RE.sub("", text):
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * scale[unit] for n, unit in _DURATION_RE.findall(text))


def parse_retry_headers(
    headers: Mapping[str, str], *, now: Optional[datetime] = None
) -> tuple[Optional[int], Optional[str]]:
    """`(retry_after_sec, reset_at)` from an answer's headers; anything unparseable is `None`.

    `Retry-After` is seconds or an HTTP-date. The reset comes from Anthropic's
    `anthropic-ratelimit-*-reset` (RFC 3339), `x-ratelimit-reset` (epoch seconds), or
    OpenAI's `x-ratelimit-reset-*` durations — a duration also fills `retry_after_sec`
    when `Retry-After` is absent.
    """
    now = now or datetime.now(timezone.utc)
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    retry_after: Optional[int] = None
    reset_at: Optional[str] = None

    raw = lowered.get("retry-after", "").strip()
    if raw:
        try:
            retry_after = max(0, int(float(raw)))
        except ValueError:
            try:
                retry_after = max(0, int((parsedate_to_datetime(raw) - now).total_seconds()))
            except (TypeError, ValueError):
                retry_after = None

    for name, value in sorted(lowered.items()):
        if reset_at is not None:
            break
        value = value.strip()
        if name.startswith("anthropic-ratelimit-") and name.endswith("-reset"):
            try:
                reset_at = _zulu(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
        elif name == "x-ratelimit-reset":
            try:
                reset_at = _zulu(datetime.fromtimestamp(float(value), timezone.utc))
            except (ValueError, OverflowError, OSError):
                continue
        elif name.startswith("x-ratelimit-reset-"):
            seconds = _parse_duration(value)
            if seconds is None:
                continue
            reset_at = _zulu(now + timedelta(seconds=seconds))
            if retry_after is None:
                retry_after = int(seconds + 0.999)
    return retry_after, reset_at


# ── the endpoint ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelEndpoint:
    """One addressable model behind an HTTPS API.

    `api_key_env` is a *variable name*, and the validation of it is the credential
    boundary in one line: a value containing a path separator or a dot is refused, so no
    configuration can turn this field into "read `~/.claude/.credentials.json`".
    """

    endpoint_id: str
    base_url: str
    model: str
    api_key_env: str
    wire: str = "openai_chat"
    billing_category: str = "free_tier_api"
    usage_pool_id: str = ""
    max_output_tokens: int = 4096
    timeout_sec: int = 120
    #: Extra headers the provider needs (an API version pin, say). Never credentials.
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.wire not in WIRE_FORMATS:
            raise ModelAPIError(
                f"{self.endpoint_id}: unknown wire format {self.wire!r} "
                f"(expected one of {list(WIRE_FORMATS)})"
            )
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ModelAPIError(
                f"{self.endpoint_id}: base_url must be an absolute https:// URL, "
                f"got {self.base_url!r}"
            )
        name = str(self.api_key_env or "").strip()
        if not name or any(ch in name for ch in "/\\. ~"):
            raise ModelAPIError(
                f"{self.endpoint_id}: api_key_env must be an environment variable name, "
                f"not a path or a literal key ([INV-03])"
            )
        if not self.model:
            raise ModelAPIError(f"{self.endpoint_id}: no model")
        if not self.usage_pool_id:
            object.__setattr__(self, "usage_pool_id", f"{self.endpoint_id}-pool")

    @property
    def url(self) -> str:
        """The full request URL for this wire format."""
        root = self.base_url.rstrip("/")
        if self.wire == "openai_chat":
            return f"{root}/chat/completions"
        if self.wire == "anthropic_messages":
            return f"{root}/messages"
        return f"{root}/models/{self.model}:generateContent"

    def to_dict(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "wire": self.wire,
            "billing_category": self.billing_category,
            "usage_pool_id": self.usage_pool_id,
            "max_output_tokens": self.max_output_tokens,
            "timeout_sec": self.timeout_sec,
        }


# ── the normalised conversation ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolSchema:
    """One tool offered to the model, in JSON-Schema terms every family accepts."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run one tool. `arguments` is already decoded."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""
    #: The arguments arrived but did not decode to an object (P12B F1). Not serialised:
    #: it is a fact about the wire, which the supervisor counts, not part of the call.
    malformed: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "arguments": dict(self.arguments), "call_id": self.call_id}


@dataclass(frozen=True)
class Message:
    """One turn of the normalised conversation."""

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: For `role='tool'`: which call this answers.
    call_id: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ModelAPIError(f"unknown message role {self.role!r}")


@dataclass(frozen=True)
class ModelTurn:
    """What one API call produced, identically shaped whatever answered it."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


# ── transport ────────────────────────────────────────────────────────────────────────


class Transport(Protocol):
    """The whole network surface: one POST, returning `(status, decoded json[, headers])`.

    The headers element is optional so a scripted transport can stay a pair; without it a
    refusal simply carries no retry hint.
    """

    def post(
        self, url: str, *, headers: Mapping[str, str], body: Mapping[str, Any], timeout: int
    ) -> tuple:
        ...


class RequestsTransport:
    """`requests` over HTTPS, and nothing else.

    `requests` is already a declared dependency (`pyproject.toml`), imported lazily so
    that importing this module — which the catalog and the compiler both do — never costs
    the import of an HTTP stack.
    """

    def post(
        self, url: str, *, headers: Mapping[str, str], body: Mapping[str, Any], timeout: int
    ) -> tuple:
        import requests

        if not url.startswith("https://"):
            raise TransportError(f"refusing a non-HTTPS request to {url!r}")
        try:
            response = requests.post(url, headers=dict(headers), json=dict(body), timeout=timeout)
        except requests.Timeout as exc:
            raise RequestTimeout(f"{url}: {exc}") from exc
        except requests.ConnectionError as exc:
            raise ProviderUnavailable(f"{url}: {exc}") from exc
        except Exception as exc:                      # anything else stays unclassified
            raise TransportError(f"{url}: {exc}") from exc
        response_headers = dict(response.headers or {})
        try:
            return response.status_code, response.json(), response_headers
        except ValueError:
            return response.status_code, {"_raw": response.text}, response_headers


# ── the adapter ──────────────────────────────────────────────────────────────────────


class ModelAPI:
    """One endpoint, spoken to in its own wire format and read back in ours."""

    def __init__(
        self,
        endpoint: ModelEndpoint,
        *,
        transport: Optional[Transport] = None,
        environ: Optional[Mapping[str, str]] = None,
    ):
        self.endpoint = endpoint
        self.transport = transport or RequestsTransport()
        self._environ = environ if environ is not None else os.environ

    # -- credentials ------------------------------------------------------------------

    def api_key(self) -> str:
        """The key, from the environment and from nowhere else (`[INV-03]`).

        Absent is a refusal, never an anonymous request: a free tier that answers an
        unauthenticated call answers it as somebody, and maestro should not discover
        whose quota it just spent.
        """
        key = str(self._environ.get(self.endpoint.api_key_env, "") or "").strip()
        if not key:
            raise CredentialMissing(
                f"{self.endpoint.endpoint_id}: ${self.endpoint.api_key_env} is unset — "
                f"maestro does not look for a credential anywhere else ([INV-03])"
            )
        return key

    def headers(self) -> dict:
        key = self.api_key()
        base = {"content-type": "application/json"}
        if self.endpoint.wire == "openai_chat":
            base["authorization"] = f"Bearer {key}"
        elif self.endpoint.wire == "anthropic_messages":
            base["x-api-key"] = key
            base.setdefault("anthropic-version", "2023-06-01")
        else:
            base["x-goog-api-key"] = key
        base.update({str(k): str(v) for k, v in self.endpoint.extra_headers.items()})
        return base

    # -- the call ---------------------------------------------------------------------

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSchema] = (),
        max_output_tokens: Optional[int] = None,
    ) -> ModelTurn:
        """One request, one turn. Raises rather than returning an empty answer.

        Deliberately unlike `AgentBackend.complete`, which swallows every failure into an
        empty `Completion` because its callers are best-effort. This one's caller is a
        supervisor loop that must tell "the model said nothing" from "the endpoint is
        refusing us", and collapsing those would turn a dead credential into a task that
        simply produces no work, forever.
        """
        body = self._body(messages, tools, max_output_tokens or self.endpoint.max_output_tokens)
        try:
            answer = self.transport.post(
                self.endpoint.url,
                headers=self.headers(),
                body=body,
                timeout=self.endpoint.timeout_sec,
            )
        except TransportError:
            raise
        except TimeoutError as exc:
            raise RequestTimeout(f"{self.endpoint.endpoint_id}: {exc}") from exc
        except ConnectionError as exc:
            raise ProviderUnavailable(f"{self.endpoint.endpoint_id}: {exc}") from exc
        status, payload = answer[0], answer[1]
        response_headers = answer[2] if len(answer) > 2 else {}
        if not 200 <= int(status) < 300:
            raise classify_http_failure(
                self.endpoint.wire, int(status), payload, response_headers,
                message=(
                    f"{self.endpoint.endpoint_id}: HTTP {status} from {self.endpoint.url} "
                    f"({_excerpt(payload)})"
                ),
            )
        if not isinstance(payload, Mapping):
            raise TransportError(
                f"{self.endpoint.endpoint_id}: response was not a JSON object "
                f"({_excerpt(payload)})"
            )
        return self._read(payload)

    # -- request building -------------------------------------------------------------

    def _body(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema], max_tokens: int
    ) -> dict:
        if self.endpoint.wire == "openai_chat":
            return _openai_body(self.endpoint, messages, tools, max_tokens)
        if self.endpoint.wire == "anthropic_messages":
            return _anthropic_body(self.endpoint, messages, tools, max_tokens)
        return _gemini_body(self.endpoint, messages, tools, max_tokens)

    def _read(self, payload: Mapping) -> ModelTurn:
        if self.endpoint.wire == "openai_chat":
            return _openai_turn(payload)
        if self.endpoint.wire == "anthropic_messages":
            return _anthropic_turn(payload)
        return _gemini_turn(payload)


def _excerpt(payload: Any, limit: int = 200) -> str:
    try:
        text = json.dumps(payload)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:limit]


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _decode_arguments(raw: Any) -> tuple[dict, bool]:
    """Tool arguments, which arrive as an object on two wires and a JSON string on one.

    Returns `(arguments, decoded)`. A string that does not parse yields `{}` rather than
    raising: the dispatcher below validates arguments against the tool's own schema
    anyway, and a malformed call should be refused there, where the refusal can be handed
    back to the model as a tool result. `decoded=False` lets the supervisor count it
    (P12B F1 `malformed_tool_calls`).
    """
    if isinstance(raw, Mapping):
        return dict(raw), True
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, True
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}, False
        return (dict(parsed), True) if isinstance(parsed, Mapping) else ({}, False)
    return {}, False


# ── OpenAI-compatible chat completions ───────────────────────────────────────────────


def _openai_body(
    endpoint: ModelEndpoint, messages: Sequence[Message], tools: Sequence[ToolSchema], max_tokens: int
) -> dict:
    rendered: list[dict] = []
    for message in messages:
        if message.role == "tool":
            rendered.append(
                {"role": "tool", "tool_call_id": message.call_id, "content": message.content}
            )
            continue
        body: dict = {"role": message.role, "content": message.content}
        if message.tool_calls:
            body["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
                }
                for call in message.tool_calls
            ]
        rendered.append(body)
    out: dict = {"model": endpoint.model, "messages": rendered, "max_tokens": max_tokens}
    if tools:
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in tools
        ]
        out["tool_choice"] = "auto"
    return out


def _openai_turn(payload: Mapping) -> ModelTurn:
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    calls = []
    for raw in message.get("tool_calls") or []:
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), Mapping) else {}
        arguments, decoded = _decode_arguments(function.get("arguments"))
        calls.append(
            ToolCall(
                name=str(function.get("name") or ""),
                arguments=arguments,
                call_id=str(raw.get("id") or ""),
                malformed=not decoded,
            )
        )
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return ModelTurn(
        text=str(message.get("content") or ""),
        tool_calls=tuple(calls),
        finish_reason=str(choice.get("finish_reason") or ""),
        input_tokens=_as_int(usage.get("prompt_tokens")),
        output_tokens=_as_int(usage.get("completion_tokens")),
    )


# ── Anthropic messages ───────────────────────────────────────────────────────────────


def _anthropic_body(
    endpoint: ModelEndpoint, messages: Sequence[Message], tools: Sequence[ToolSchema], max_tokens: int
) -> dict:
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    rendered: list[dict] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            rendered.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.call_id,
                            "content": message.content,
                        }
                    ],
                }
            )
            continue
        blocks: list[dict] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
            )
        rendered.append({"role": message.role, "content": blocks})
    out: dict = {"model": endpoint.model, "messages": rendered, "max_tokens": max_tokens}
    if system:
        out["system"] = system
    if tools:
        out["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.parameters) or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]
    return out


def _anthropic_turn(payload: Mapping) -> ModelTurn:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in payload.get("content") or []:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            arguments, decoded = _decode_arguments(block.get("input"))
            calls.append(
                ToolCall(
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                    call_id=str(block.get("id") or ""),
                    malformed=not decoded,
                )
            )
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return ModelTurn(
        text="\n".join(t for t in texts if t),
        tool_calls=tuple(calls),
        finish_reason=str(payload.get("stop_reason") or ""),
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
    )


# ── Gemini generateContent ───────────────────────────────────────────────────────────


def _gemini_body(
    endpoint: ModelEndpoint, messages: Sequence[Message], tools: Sequence[ToolSchema], max_tokens: int
) -> dict:
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    contents: list[dict] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.name or message.call_id,
                                "response": {"result": message.content},
                            }
                        }
                    ],
                }
            )
            continue
        parts: list[dict] = []
        if message.content:
            parts.append({"text": message.content})
        for call in message.tool_calls:
            parts.append({"functionCall": {"name": call.name, "args": dict(call.arguments)}})
        contents.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
    out: dict = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        out["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        out["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters) or {"type": "object", "properties": {}},
                    }
                    for tool in tools
                ]
            }
        ]
    return out


def _gemini_turn(payload: Mapping) -> ModelTurn:
    candidates = payload.get("candidates") or []
    candidate = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
    content = candidate.get("content") if isinstance(candidate.get("content"), Mapping) else {}
    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in content.get("parts") or []:
        if not isinstance(part, Mapping):
            continue
        if "text" in part:
            texts.append(str(part.get("text") or ""))
        function = part.get("functionCall")
        if isinstance(function, Mapping):
            name = str(function.get("name") or "")
            arguments, decoded = _decode_arguments(function.get("args"))
            calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    malformed=not decoded,
                    #: Gemini names no call id; the tool name is the correlation key its
                    #: own `functionResponse` uses, so it is what we carry.
                    call_id=name,
                )
            )
    usage = payload.get("usageMetadata") if isinstance(payload.get("usageMetadata"), Mapping) else {}
    return ModelTurn(
        text="\n".join(t for t in texts if t),
        tool_calls=tuple(calls),
        finish_reason=str(candidate.get("finishReason") or ""),
        input_tokens=_as_int(usage.get("promptTokenCount")),
        output_tokens=_as_int(usage.get("candidatesTokenCount")),
    )


# ── catalogue interop ────────────────────────────────────────────────────────────────


def capability_entry(
    endpoint: ModelEndpoint,
    *,
    verified_version: str,
    strength: str = "balanced",
    max_input_tokens: int = 1_000_000,
    effective_context_ceiling: int = 200_000,
    concurrency_limit: int = 1,
    modalities: Sequence[str] = ("text",),
) -> Any:
    """This endpoint as a `BackendCapabilityEntry`, for P6's catalog and router.

    `verified_version` is required and has no default, which is `[INV-12]` expressed as a
    signature: a free endpoint is not catalogued because it exists, it is catalogued
    because `maestro doctor --thirdparty` saw it answer at a named contract version. That
    is also why nothing here registers itself at import time — a module that added a
    route to the catalog merely by being imported would be the verification gate's exact
    failure mode.

    Effort is reported unsupported. A bare chat endpoint exposes no reasoning-effort
    control, and `[INV-A07]` forbids inventing a parameter to stand in for one; the
    router compensates the documented way, by preferring the strongest permitted model.
    """
    from maestro.backends.catalog import (
        BackendCapabilityEntry,
        ContextLimits,
        EffortControls,
        ModelProfile,
        PairProfile,
    )

    if not str(verified_version).strip():
        raise ModelAPIError(
            f"{endpoint.endpoint_id}: an endpoint with no verified version is not "
            f"catalogued ([INV-12])"
        )
    return BackendCapabilityEntry(
        backend_id=endpoint.endpoint_id,
        runtime_kind="model_endpoint_harness",
        installed_version=str(verified_version),
        billing_category=endpoint.billing_category,
        usage_pool_id=endpoint.usage_pool_id,
        available_models=(endpoint.model,),
        effort_controls=EffortControls(
            unsupported_reason="a bare chat endpoint exposes no reasoning-effort control"
        ),
        context_limits=ContextLimits(
            max_input_tokens=max_input_tokens,
            max_output_tokens=endpoint.max_output_tokens,
            effective_context_ceiling=effective_context_ceiling,
        ),
        modalities=tuple(modalities),
        #: The supervisor loop dispatches JSON tool calls itself — it has no CLI whose
        #: native tools it could borrow.
        tool_delivery="json_schema_dispatch",
        instruction_delivery="system_flag",
        structured_output_support="tool_call",
        #: `ToolBox` confines every write to the worktree by path; that is directory
        #: isolation, not a kernel sandbox, and the catalog must not overstate it.
        permission_enforcement="directory_isolation",
        concurrency_limit=concurrency_limit,
        telemetry_quality="token_exact",
        runtime_instance_id=endpoint.endpoint_id,
        #: `relative_cost` is a *within-pool* ratio (`PairProfile`), so a one-model
        #: endpoint's is 1.0 by definition. Free-ness lives in `billing_category`, which
        #: is what the free objective actually reads; writing a zero here would be
        #: stating a price, which that field explicitly is not.
        model_profiles={endpoint.model: ModelProfile(endpoint.model)},
        pair_profiles={(endpoint.model, None): PairProfile(endpoint.model, None, strength, 1.0)},
    )
