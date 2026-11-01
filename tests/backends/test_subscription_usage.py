"""Per-step subscription telemetry: a fresh Claude quota read and per-attempt token counts.

Pilot finding (DuetFlow 02 run 9): every Claude attempt's `usage_start_json` and
`usage_settle_json` were the same `.orchestrator/usage.json` snapshot — written by the
*interactive* session's statusline sampler, carrying that session's model, and not
re-read per attempt — so a claude step's quota delta was always 0 and meaningless, and
no attempt had a token count. These pin the replacements:

* `claude_subscription.read_subscription_usage` asks the account's OAuth usage endpoint
  (the one the `usage` skill and Claude Code's `/usage` read), at most once per
  `max_age_sec` across every process sharing one cache file, and answers a failure with
  empty windows plus a reason — never an exception, never a stale number dressed as new.
* `attempt_token_usage(workspace)` on both CLI drivers reads the attempt's own session
  record (Claude transcript / Codex rollout) into cumulative token totals.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from maestro.backends import claude_subscription as sub
from maestro.backends.base import CompletionSpec
from maestro.backends.claude import ClaudeBackend, SESSION_UUID_FILE, completion_argv
from maestro.backends.codex import CodexBackend

_API = {
    "five_hour": {"utilization": 23.0, "resets_at": "2026-09-26T19:50:00.282229+00:00"},
    "seven_day": {"utilization": 44.0, "resets_at": "2026-09-29T13:00:00.282252+00:00"},
    "seven_day_opus": None,
}


def _creds(tmp_path: Path, *, expires_in_sec: float = 3600) -> Path:
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-test-not-a-real-token",
        "expiresAt": int((time.time() + expires_in_sec) * 1000),
    }}), encoding="utf-8")
    return path


class _Fetch:
    def __init__(self, answer=_API, exc=None):
        self.calls = 0
        self.answer, self.exc = answer, exc

    def __call__(self, token, timeout):
        self.calls += 1
        assert token == "sk-test-not-a-real-token"
        if self.exc is not None:
            raise self.exc
        return self.answer


def test_a_fresh_read_reports_both_windows_keyed_by_minutes(tmp_path):
    fetch = _Fetch()
    usage = sub.read_subscription_usage(
        cache_path=tmp_path / "cache.json", credentials_path=_creds(tmp_path),
        fetch=fetch, now=1_790_000_000.0,
    )
    assert fetch.calls == 1
    assert usage.error is None
    assert usage.source == sub.SOURCE
    assert usage.windows[300].used_pct == 23.0
    assert usage.windows[300].resets_at == 1790452200  # 19:50:00 UTC, fraction dropped
    assert usage.windows[10080].used_pct == 44.0
    assert usage.updated_at == "2026-09-21T14:13:20Z", "sampled_at, not a file's stamp"
    assert usage.model is None, "an account read names no model — least of all Dan's"


def test_parallel_attempts_share_one_fetch_within_the_throttle(tmp_path):
    fetch = _Fetch()
    kwargs = dict(cache_path=tmp_path / "cache.json", credentials_path=_creds(tmp_path),
                  fetch=fetch, max_age_sec=60)
    first = sub.read_subscription_usage(now=1000.0, **kwargs)
    second = sub.read_subscription_usage(now=1059.0, **kwargs)
    assert fetch.calls == 1, "a second read inside the window reuses the cached sample"
    assert second.updated_at == first.updated_at, "the reuse keeps the real sample time"
    sub.read_subscription_usage(now=1061.0, **kwargs)
    assert fetch.calls == 2


@pytest.mark.parametrize("exc,reason", [
    (OSError("connection refused"), "fetch failed"),
    (ValueError("not json"), "fetch failed"),
])
def test_a_failed_fetch_is_empty_windows_and_a_reason_never_an_exception(
    tmp_path, exc, reason
):
    fetch = _Fetch(exc=exc)
    kwargs = dict(cache_path=tmp_path / "cache.json", credentials_path=_creds(tmp_path),
                  fetch=fetch)
    usage = sub.read_subscription_usage(now=1000.0, **kwargs)
    assert usage.windows == {}
    assert reason in usage.error
    # The failure is cached too: a dead endpoint is not hammered by every claim.
    sub.read_subscription_usage(now=1010.0, **kwargs)
    assert fetch.calls == 1


def test_an_expired_token_is_not_used_and_not_refreshed(tmp_path):
    """Refreshing would rotate the refresh token under the CLI's feet; the CLI refreshes it
    itself the next time any claude process runs, so this read simply reports why not."""
    fetch = _Fetch()
    usage = sub.read_subscription_usage(
        cache_path=tmp_path / "cache.json",
        credentials_path=_creds(tmp_path, expires_in_sec=-5), fetch=fetch, now=time.time(),
    )
    assert fetch.calls == 0
    assert usage.windows == {} and "expired" in usage.error


def test_missing_credentials_and_an_unwritable_cache_still_answer(tmp_path):
    usage = sub.read_subscription_usage(
        cache_path=tmp_path / "no" / "such" / "dir" / "cache.json",
        credentials_path=tmp_path / "absent.json", fetch=_Fetch(), now=1000.0,
    )
    assert usage.windows == {} and "credentials" in usage.error


def test_the_token_never_reaches_the_cache_or_the_reason(tmp_path):
    cache = tmp_path / "cache.json"
    fetch = _Fetch(exc=OSError("401 for Bearer sk-test-not-a-real-token"))
    usage = sub.read_subscription_usage(
        cache_path=cache, credentials_path=_creds(tmp_path), fetch=fetch, now=1000.0,
    )
    assert "sk-test" not in (usage.error or "")
    assert "sk-test" not in cache.read_text(encoding="utf-8")


def test_the_claude_driver_serves_it_from_beside_usage_json(tmp_path, monkeypatch):
    monkeypatch.setattr(sub, "read_subscription_usage",
                        lambda **kw: kw["cache_path"])
    driver = ClaudeBackend(usage_path=tmp_path / ".orchestrator" / "usage.json")
    assert driver.subscription_usage() == (
        tmp_path / ".orchestrator" / sub.CACHE_FILE
    )


def test_the_statusline_reading_says_where_it_came_from(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"five_hour": {"used_pct": 5, "resets_at": 9}}),
                    encoding="utf-8")
    assert ClaudeBackend(usage_path=path).usage().source == "statusline_usage_json"


# ── per-attempt tokens ──


@pytest.mark.parametrize('scoped', [
    {'opus': {'not-minutes': {'used_pct': 25}}},
    {'opus': {"1.5": {'used_pct': 25}}},
    ['malformed'],
    {'opus': {'10080': None}},
])
def test_malformed_optional_scoped_cache_keeps_valid_account(tmp_path, scoped):
    cache = tmp_path / 'cache.json'
    cache.write_text(json.dumps({'fetched_epoch': 1000, 'fetched_at': '1970-01-01T00:16:40Z',
        'windows': {'300': {'used_pct': 10, 'resets_at': 2000}},
        'scoped_windows': scoped}))
    usage = sub.read_subscription_usage(cache_path=cache, now=1010,
        credentials_path=tmp_path / 'absent.json',
        fetch=lambda *_: pytest.fail('fresh cache must not fetch'))
    assert usage.error is None
    assert usage.windows[300].used_pct == 10
    assert usage.updated_at == '1970-01-01T00:16:40Z'
    assert not usage.scoped_windows.get('opus')


def test_a_completion_names_its_session_only_when_asked():
    plain = completion_argv(CompletionSpec(prompt="hi", model="m"))
    assert "--session-id" not in plain
    named = completion_argv(CompletionSpec(prompt="hi", model="m", session_id="u-1"))
    assert named[named.index("--session-id") + 1] == "u-1"
    assert named[-1] == "hi", "the prompt stays positional and last"
    resumed = completion_argv(
        CompletionSpec(prompt="hi", resume_id="r-1", session_id="u-1")
    )
    assert "--session-id" not in resumed, "--resume replaces --session-id (F4)"


def test_a_named_completion_leaves_its_session_uuid_beside_the_log(tmp_path):
    class _Done:
        stdout, stderr, returncode = "ok", "", 0

    driver = ClaudeBackend(runner=lambda *a, **k: _Done())
    driver.complete(CompletionSpec(prompt="hi", session_id="u-42",
                                   log_file=tmp_path / "impl.log"))
    assert (tmp_path / SESSION_UUID_FILE).read_text(encoding="utf-8").strip() == "u-42"


def _assistant(mid, model, **usage):
    return {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}}


def _claude_home(tmp_path, uuid, rows, *, sub_rows=()):
    home = tmp_path / "claude-home"
    project = home / "projects" / "-some-cwd"
    project.mkdir(parents=True)
    (project / f"{uuid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n{half a line", encoding="utf-8"
    )
    if sub_rows:
        agents = project / uuid / "subagents"
        agents.mkdir(parents=True)
        (agents / "agent-1.jsonl").write_text(
            "\n".join(json.dumps(r) for r in sub_rows), encoding="utf-8"
        )
    return home


def test_claude_attempt_tokens_prefer_the_cli_cost_state(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / SESSION_UUID_FILE).write_text("u-1\n", encoding="utf-8")
    home = _claude_home(tmp_path, "u-1", [
        _assistant("m1", "claude-sonnet-5", input_tokens=1, output_tokens=2,
                   cache_read_input_tokens=3, cache_creation_input_tokens=4),
        {"type": "cost-state", "totalCostUSD": 0.25, "modelUsage": {
            "claude-sonnet-5": {"inputTokens": 10, "outputTokens": 20,
                                "cacheReadInputTokens": 30, "cacheCreationInputTokens": 40},
            "claude-haiku-5": {"inputTokens": 1, "outputTokens": 2,
                               "cacheReadInputTokens": 3, "cacheCreationInputTokens": 4},
        }},
    ])
    tokens = ClaudeBackend(claude_home=home).attempt_token_usage(workspace)
    assert tokens == {
        "fresh_input": 11, "cache_read": 33, "cache_write": 44, "output": 22,
        "cost_usd": 0.25, "models": ["claude-haiku-5", "claude-sonnet-5"],
        "source": "claude_cost_state",
    }


def test_claude_attempt_tokens_fall_back_to_one_count_per_message_id(tmp_path):
    """A streamed message is written as several records repeating one usage; summing
    them would double count. A subagent's own file counts too."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / SESSION_UUID_FILE).write_text("u-2", encoding="utf-8")
    turn = dict(input_tokens=1, output_tokens=10, cache_read_input_tokens=100,
                cache_creation_input_tokens=1000)
    home = _claude_home(
        tmp_path, "u-2",
        [_assistant("m1", "opus", **turn), _assistant("m1", "opus", **turn),
         _assistant("m2", "opus", **turn)],
        sub_rows=[_assistant("s1", "haiku", **turn)],
    )
    tokens = ClaudeBackend(claude_home=home).attempt_token_usage(workspace)
    assert tokens["source"] == "claude_transcript"
    assert (tokens["fresh_input"], tokens["output"], tokens["cache_read"],
            tokens["cache_write"]) == (3, 30, 300, 3000)
    assert tokens["cost_usd"] is None


def test_claude_attempt_tokens_are_none_without_a_session(tmp_path):
    assert ClaudeBackend(claude_home=tmp_path).attempt_token_usage(tmp_path) is None


def test_codex_attempt_tokens_come_from_the_rollout_totals(tmp_path):
    thread = "01a0dd01-3551-7db1-8974-57d483291290"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "impl.log").write_text(f"model: x\nsession id: {thread}\n--------\n",
                                        encoding="utf-8")
    home = tmp_path / "codex-home"
    day = home / "sessions" / "2026" / "09" / "26"
    day.mkdir(parents=True)

    def count(total):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": total, "last_token_usage": total}}}

    (day / f"rollout-2026-09-26T12-17-19-{thread}.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
        count({"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 1}),
        count({"input_tokens": 994294, "cached_input_tokens": 826496,
               "cache_write_input_tokens": 0, "output_tokens": 10905,
               "reasoning_output_tokens": 5207, "total_tokens": 1005199}),
    ]), encoding="utf-8")
    driver = CodexBackend(codex_home=home)
    tokens = driver.attempt_token_usage(workspace)
    assert tokens == {
        "fresh_input": 994294 - 826496, "cache_read": 826496, "cache_write": 0,
        "output": 10905, "reasoning_output": 5207, "cost_usd": None,
        "models": ["gpt-5.6-sol"], "source": "codex_rollout",
    }


def test_codex_attempt_tokens_are_none_without_a_thread(tmp_path):
    assert CodexBackend(codex_home=tmp_path).attempt_token_usage(tmp_path) is None
