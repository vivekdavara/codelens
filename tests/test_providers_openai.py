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
from codelens.providers.openai import OpenAIProvider, parse_chat_completion

REQUEST = Request("You review code.", "Review this diff.", FINDINGS_SCHEMA)
KEY = "sk-test-not-a-real-key"


def chat(content: Any = '{"findings": []}', finish: str = "stop", refusal: Any = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-5-2025-08-07",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 64, "total_tokens": 1264},
    }


def provider(server: FakeServer, **kwargs: Any) -> OpenAIProvider:
    return OpenAIProvider(KEY, base_url=server.url, sleep=lambda _: None, **kwargs)


def test_request_shape(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, chat()))
    provider(fake_server).complete(REQUEST)
    (seen,) = fake_server.requests
    assert seen.path == "/v1/chat/completions"
    assert seen.headers["authorization"] == f"Bearer {KEY}"
    assert seen.body == {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "You review code."},
            {"role": "user", "content": "Review this diff."},
        ],
        "max_completion_tokens": 16000,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "codelens_findings", "strict": True, "schema": FINDINGS_SCHEMA},
        },
    }


def test_free_text_requests_have_no_response_format(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, chat("hello")))
    assert provider(fake_server, model="gpt-5-mini").complete(Request("s", "p")).text == "hello"
    (seen,) = fake_server.requests
    assert "response_format" not in seen.body and seen.body["model"] == "gpt-5-mini"


def test_returns_text_model_and_usage(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, chat()))
    completion = provider(fake_server).complete(REQUEST)
    assert (completion.text, completion.model) == ('{"findings": []}', "gpt-5-2025-08-07")
    assert completion.usage == Usage(1200, 64)


def test_refusals_and_filtered_answers_raise() -> None:
    with pytest.raises(ProviderRefused, match="I can't help with that"):
        parse_chat_completion(chat(content=None, refusal="I can't help with that."))
    with pytest.raises(ProviderRefused, match="content filter"):
        parse_chat_completion(chat(finish="content_filter"))


def test_a_truncated_answer_is_not_used() -> None:
    with pytest.raises(ProviderTruncated):
        parse_chat_completion(chat(content='{"findings": [', finish="length"))


@pytest.mark.parametrize(
    "data", [{}, {"choices": []}, {"choices": ["x"]}, chat(content=None), chat(content="")]
)
def test_malformed_responses_raise(data: Any) -> None:
    with pytest.raises(ProviderError):
        parse_chat_completion(data)


def test_rate_limits_are_retried_and_errors_never_contain_the_key(fake_server: FakeServer) -> None:
    limited = {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}}
    fake_server.reply(Reply(429, limited, {"retry-after-ms": "10"}), Reply(200, chat()))
    assert provider(fake_server).complete(REQUEST).text == '{"findings": []}'
    bad_key = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}
    fake_server.reply(Reply(401, bad_key, {"x-request-id": "req_abc"}))
    p = provider(fake_server)
    with pytest.raises(ProviderHTTPError) as exc:
        p.complete(REQUEST)
    assert "req_abc" in str(exc.value)
    assert KEY not in str(exc.value) and KEY not in repr(p)


def test_a_key_is_required() -> None:
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAIProvider("")


def test_nonsense_token_counts_count_as_zero() -> None:
    data = chat()
    data["usage"] = {"prompt_tokens": -1, "completion_tokens": 2.5}
    assert parse_chat_completion(data).usage == Usage(0, 0)
