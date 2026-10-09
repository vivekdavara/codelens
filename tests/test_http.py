import socket
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from conftest import FakeServer, Reply

from codelens.providers import ProviderError, ProviderHTTPError
from codelens.providers.http import MAX_RESPONSE_BYTES, RetryPolicy, _backoff, post_json

ANTHROPIC_429 = {
    "type": "error",
    "error": {"type": "rate_limit_error", "message": "Number of request tokens has exceeded your limit."},
}


def call(server: FakeServer, sleeps: list[float], **kwargs: object) -> object:
    data, _ = post_json(
        server.url + "/v1/messages",
        {"hello": "world"},
        {"x-api-key": "test-key"},
        sleep=sleeps.append,
        rng=lambda: 0.0,
        **kwargs,  # type: ignore[arg-type]
    )
    return data


def test_posts_json_and_returns_json_and_headers(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, {"ok": True}, {"Request-Id": "req_1"}))
    data, headers = post_json(fake_server.url + "/v1/messages", {"hello": "world"}, {"x-api-key": "k"})
    assert data == {"ok": True}
    assert headers["request-id"] == "req_1"
    (seen,) = fake_server.requests
    assert (seen.method, seen.path, seen.body) == ("POST", "/v1/messages", {"hello": "world"})
    assert seen.headers["content-type"] == "application/json"
    assert seen.headers["x-api-key"] == "k"


def test_retries_a_429_after_the_server_requested_wait(fake_server: FakeServer) -> None:
    sleeps: list[float] = []
    fake_server.reply(Reply(429, ANTHROPIC_429, {"retry-after": "2"}), Reply(200, {"ok": 1}))
    assert call(fake_server, sleeps) == {"ok": 1}
    assert sleeps == [2.0]
    assert len(fake_server.requests) == 2


def test_retry_after_ms_wins_over_retry_after(fake_server: FakeServer) -> None:
    sleeps: list[float] = []
    fake_server.reply(
        Reply(429, ANTHROPIC_429, {"retry-after-ms": "250", "retry-after": "9"}), Reply(200, {})
    )
    call(fake_server, sleeps)
    assert sleeps == [0.25]


def test_retry_after_as_an_http_date(fake_server: FakeServer) -> None:
    sleeps: list[float] = []
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    fake_server.reply(Reply(503, {"message": "busy"}, {"retry-after": when}), Reply(200, {}))
    call(fake_server, sleeps)
    (slept,) = sleeps
    assert 3 < slept <= 5


@pytest.mark.parametrize("value", ["120", "-1", "soon"])
def test_unusable_retry_after_falls_back_to_backoff(fake_server: FakeServer, value: str) -> None:
    sleeps: list[float] = []
    fake_server.reply(Reply(429, ANTHROPIC_429, {"retry-after": value}), Reply(200, {}))
    call(fake_server, sleeps)
    assert sleeps == [1.0]


def test_gives_up_after_the_last_attempt_with_vendor_details(fake_server: FakeServer) -> None:
    sleeps: list[float] = []
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    fake_server.reply(*[Reply(529, overloaded, {"request-id": "req_9"})] * 3)
    with pytest.raises(ProviderHTTPError) as exc:
        call(fake_server, sleeps)
    assert (exc.value.status, exc.value.error_type, exc.value.request_id) == (
        529,
        "overloaded_error",
        "req_9",
    )
    assert "Overloaded" in str(exc.value)
    assert sleeps == [1.0, 2.0]  # exponential backoff, no jitter with rng=0
    assert len(fake_server.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_are_not_retried(fake_server: FakeServer, status: int) -> None:
    sleeps: list[float] = []
    fake_server.reply(Reply(status, {"error": {"message": "bad", "type": "invalid_request_error"}}))
    with pytest.raises(ProviderHTTPError) as exc:
        call(fake_server, sleeps)
    assert exc.value.status == status and sleeps == [] and len(fake_server.requests) == 1


def test_x_should_retry_overrides_the_status(fake_server: FakeServer) -> None:
    sleeps: list[float] = []
    fake_server.reply(Reply(500, {"message": "no"}, {"x-should-retry": "false"}))
    with pytest.raises(ProviderHTTPError):
        call(fake_server, sleeps)
    assert len(fake_server.requests) == 1
    fake_server.reply(Reply(400, {"message": "try again"}, {"x-should-retry": "true"}), Reply(200, {}))
    call(fake_server, sleeps)
    assert len(fake_server.requests) == 3


def test_redirects_are_refused_so_headers_never_leave(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(307, b"", {"Location": fake_server.url + "/elsewhere"}))
    with pytest.raises(ProviderHTTPError) as exc:
        call(fake_server, [])
    assert exc.value.status == 307
    assert [r.path for r in fake_server.requests] == ["/v1/messages"]


def test_a_read_timeout_is_retried(fake_server: FakeServer) -> None:
    sleeps: list[float] = []
    fake_server.reply(Reply(200, {"late": True}, delay=1.0), Reply(200, {"ok": True}))
    assert call(fake_server, sleeps, timeout=0.2) == {"ok": True}
    assert sleeps == [1.0]


def test_connection_errors_are_retried_then_reported() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed again before the call: nothing listens there
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match=f"cannot reach 127.0.0.1:{port} after 3 attempts"):
        post_json(f"http://127.0.0.1:{port}/", {}, {}, sleep=sleeps.append, rng=lambda: 0.0)
    assert sleeps == [1.0, 2.0]


def test_a_non_json_success_body_is_an_error(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, b"<html>gateway</html>"))
    with pytest.raises(ProviderError, match="non-JSON"):
        call(fake_server, [])


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}},
            "Incorrect API",
        ),
        ({"message": "Validation Failed", "errors": []}, "Validation Failed"),
        (b"upstream connect error", "upstream connect error"),
        (b"", "(empty body)"),
    ],
)
def test_error_messages_from_each_vendor_shape(fake_server: FakeServer, body: object, message: str) -> None:
    fake_server.reply(Reply(400, body))
    with pytest.raises(ProviderHTTPError, match=message.replace("(", r"\(").replace(")", r"\)")):
        call(fake_server, [])


def test_backoff_doubles_up_to_the_cap_with_bounded_jitter() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=5.0)
    assert [_backoff(n, policy, lambda: 0.0) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert _backoff(2, policy, lambda: 1.0) == 1.5  # at most 25% shorter


def test_an_oversized_response_is_refused(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, b"[" + b"0," * (MAX_RESPONSE_BYTES // 2) + b"0]"))
    with pytest.raises(ProviderError, match="over 10,485,760 bytes"):
        call(fake_server, [])


def test_an_oversized_error_body_is_cut_short(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(400, b"x" * (MAX_RESPONSE_BYTES + 100)))
    with pytest.raises(ProviderHTTPError) as exc:
        call(fake_server, [])
    assert len(str(exc.value)) < 400  # a non-JSON body is quoted only up to 300 characters
