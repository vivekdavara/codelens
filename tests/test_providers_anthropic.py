from typing import Any

import pytest
from conftest import FakeServer, Reply

from codelens.findings import FINDINGS_SCHEMA
from codelens.providers import (
    ProviderError,
    ProviderHTTPError,
    ProviderRefused,
    ProviderTruncated,
    Request,
    Usage,
)
from codelens.providers.anthropic import AnthropicProvider, parse_message

REQUEST = Request("You review code.", "Review this diff.", FINDINGS_SCHEMA)
KEY = "sk-ant-test-not-a-real-key"


def message(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": '{"findings": []}'},
        ],
        "stop_reason": "end_turn",
        "stop_details": None,
        "usage": {
            "input_tokens": 900,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
            "output_tokens": 42,
        },
    }
    base.update(overrides)
    return base


def provider(server: FakeServer, **kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(KEY, base_url=server.url, sleep=lambda _: None, **kwargs)


def test_request_shape(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, message()))
    provider(fake_server).complete(REQUEST)
    (seen,) = fake_server.requests
    assert seen.path == "/v1/messages"
    assert seen.headers["x-api-key"] == KEY
    assert seen.headers["anthropic-version"] == "2023-06-01"
    assert seen.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert seen.body == {
        "model": "claude-opus-5-5",
        "max_tokens": 16000,
        "system": "You review code.",
        "messages": [{"role": "user", "content": "Review this diff."}],
        "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": FINDINGS_SCHEMA}},
        "fallbacks": "default",
    }


def test_options_change_the_request(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, message()))
    p = provider(fake_server, model="claude-sonnet-5-5", effort="medium", max_tokens=8000, fallbacks=False)
    p.complete(Request("s", "p"))
    (seen,) = fake_server.requests
    assert "anthropic-beta" not in seen.headers
    assert "fallbacks" not in seen.body
    assert seen.body["model"] == "claude-sonnet-5-5" and seen.body["max_tokens"] == 8000
    assert seen.body["output_config"] == {"effort": "medium"}  # no schema, no format


def test_returns_text_model_and_usage(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, message()))
    completion = provider(fake_server).complete(REQUEST)
    assert completion.text == '{"findings": []}'
    assert completion.model == "claude-opus-5-5"
    assert completion.usage == Usage(input_tokens=1000, output_tokens=42)


def test_a_fallback_answer_reports_the_model_that_wrote_it() -> None:
    content = [
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}},
        {"type": "text", "text": '{"findings": '},
        {"type": "text", "text": "[]}"},
    ]
    completion = parse_message(message(model="claude-opus-4-8", content=content))
    assert (completion.text, completion.model) == ('{"findings": []}', "claude-opus-4-8")


def test_refusals_raise_with_the_category() -> None:
    refused = message(
        content=[],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": ""},
    )
    with pytest.raises(ProviderRefused, match="cyber"):
        parse_message(refused)
    with pytest.raises(ProviderRefused, match="unspecified"):
        parse_message(message(content=[], stop_reason="refusal"))


def test_a_truncated_answer_is_not_used() -> None:
    with pytest.raises(ProviderTruncated, match="max_tokens"):
        parse_message(message(stop_reason="max_tokens"))


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"type": "error"},
        message(content="text"),
        message(content=[{"type": "thinking", "thinking": ""}]),
        message(content=[{"type": "text", "text": 5}]),
    ],
)
def test_malformed_responses_raise(data: Any) -> None:
    with pytest.raises(ProviderError):
        parse_message(data)


def test_missing_usage_counts_as_zero() -> None:
    assert parse_message(message(usage=None)).usage == Usage(0, 0)


def test_overloaded_is_retried(fake_server: FakeServer) -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    fake_server.reply(Reply(529, overloaded), Reply(200, message()))
    assert provider(fake_server).complete(REQUEST).text == '{"findings": []}'
    assert len(fake_server.requests) == 2


def test_errors_never_contain_the_key(fake_server: FakeServer) -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    fake_server.reply(Reply(401, error, {"request-id": "req_011"}))
    p = provider(fake_server)
    with pytest.raises(ProviderHTTPError) as exc:
        p.complete(REQUEST)
    assert "authentication_error" in str(exc.value) and "req_011" in str(exc.value)
    assert KEY not in str(exc.value) and KEY not in repr(p)


def test_configuration_errors() -> None:
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider("")
    with pytest.raises(ProviderError, match="effort must be one of"):
        AnthropicProvider(KEY, effort="extreme")


def test_nonsense_token_counts_count_as_zero() -> None:
    usage = {"input_tokens": -5, "cache_read_input_tokens": "9", "output_tokens": True}
    assert parse_message(message(usage=usage)).usage == Usage(0, 0)
