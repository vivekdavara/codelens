"""POST JSON with retries, on the standard library HTTP client. Shared by the live providers and GitHub.

Retries follow what the vendor SDKs do: retry 408, 409, 429, 5xx (including Anthropic's 529 "overloaded") and
connection errors, honour ``retry-after`` / ``retry-after-ms`` when the server sends a short one, otherwise
back off exponentially with jitter. A server's ``x-should-retry`` header overrides the status code.

Redirects are refused rather than followed: urllib would forward every header, API key included, to wherever a
redirect points.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from codelens.providers.base import ProviderError, ProviderHTTPError

__all__ = ["DEFAULT_POLICY", "MAX_RESPONSE_BYTES", "RETRY_STATUSES", "RetryPolicy", "post_json"]

RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
"""A review response is a few kilobytes; anything near this is a misbehaving endpoint, not an answer."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    """Total tries, the first one included."""
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_retry_after: float = 60.0
    """A server-requested wait longer than this is ignored in favour of the backoff."""


DEFAULT_POLICY = RetryPolicy()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises the 3xx as an HTTPError


_OPENER = urllib.request.build_opener(_NoRedirect)


def _server_delay(headers: Mapping[str, str], policy: RetryPolicy) -> float | None:
    """The wait the server asked for, if it asked for a usable one."""
    if (ms := headers.get("retry-after-ms")) is not None:
        try:
            seconds = float(ms) / 1000
        except ValueError:
            seconds = -1.0
    elif (value := headers.get("retry-after")) is not None:
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            seconds = (when - datetime.now(UTC)).total_seconds()
    else:
        return None
    return seconds if 0 <= seconds <= policy.max_retry_after else None


def _backoff(attempt: int, policy: RetryPolicy, rng: Callable[[], float]) -> float:
    delay = min(policy.base_delay * 2.0 ** (attempt - 1), policy.max_delay)
    return delay * (1 - 0.25 * rng())  # up to 25% jitter, so parallel clients don't retry in lockstep


def _should_retry(status: int, headers: Mapping[str, str]) -> bool:
    override = headers.get("x-should-retry")
    if override in ("true", "false"):
        return override == "true"
    return status in RETRY_STATUSES


def _error_details(body: bytes) -> tuple[str, str | None]:
    """``(message, error type)`` from a vendor error body: Anthropic, OpenAI and GitHub shapes."""
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:300] or "(empty body)", None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            kind = error.get("type")
            return error["message"], kind if isinstance(kind, str) else None
        if isinstance(data.get("message"), str):
            return data["message"], None
    return text.strip()[:300], None


def _request_id(headers: Mapping[str, str]) -> str | None:
    for name in ("request-id", "x-request-id", "x-github-request-id"):
        if value := headers.get(name):
            return value
    return None


def post_json(
    url: str,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    timeout: float = 600.0,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> tuple[Any, dict[str, str]]:
    """POST ``body`` as JSON and return the decoded JSON response and its headers (names lower-cased).

    ``timeout`` bounds each socket operation, not the whole call: a long non-streaming completion can keep the
    connection silent for minutes. Raises :class:`ProviderHTTPError` for an error status that is not retried
    or still fails on the last attempt, and :class:`ProviderError` for network failures and non-JSON bodies.
    """
    data = json.dumps(body).encode("utf-8")
    all_headers = {"Content-Type": "application/json", "Accept": "application/json", **headers}
    host = urlsplit(url).netloc
    for attempt in range(1, policy.attempts + 1):
        request = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            error_headers = {k.lower(): v for k, v in exc.headers.items()}
            with exc:
                error_body = exc.read(MAX_RESPONSE_BYTES)
            if attempt == policy.attempts or not _should_retry(exc.code, error_headers):
                message, error_type = _error_details(error_body)
                raise ProviderHTTPError(exc.code, message, error_type, _request_id(error_headers)) from None
            delay = _server_delay(error_headers, policy)
            sleep(delay if delay is not None else _backoff(attempt, policy, rng))
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            if attempt == policy.attempts:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                raise ProviderError(f"cannot reach {host} after {attempt} attempts: {reason}") from None
            sleep(_backoff(attempt, policy, rng))
            continue
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError(
                f"{host} sent a response over {MAX_RESPONSE_BYTES:,} bytes; refusing to read it"
            )
        try:
            return json.loads(raw), response_headers
        except ValueError:
            raise ProviderError(f"{host} returned a non-JSON response: {raw[:200]!r}") from None
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises
