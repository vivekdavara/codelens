"""Claude through the Anthropic Messages API (``POST /v1/messages``). Opt-in: ``CODELENS_PROVIDER=anthropic``.

Raw HTTP on the standard library instead of the ``anthropic`` SDK, because CodeLens keeps zero runtime
dependencies so the action installs in seconds (DESIGN.md, "Decisions and trade-offs"). The request asks for
structured output (``output_config.format``) so the reply is JSON matching the findings schema, and opts into
server-side refusal fallbacks so a request a safety classifier declines is retried on another model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codelens.providers.base import (
    Completion,
    ProviderError,
    ProviderRefused,
    ProviderTruncated,
    Request,
    Usage,
)
from codelens.providers.http import DEFAULT_POLICY, RetryPolicy, post_json

__all__ = ["DEFAULT_MODEL", "AnthropicProvider", "parse_message"]

DEFAULT_MODEL = "claude-opus-5-5"
API_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_tokens: int = 16000,
        fallbacks: bool = True,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 600.0,
        policy: RetryPolicy = DEFAULT_POLICY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ProviderError("the anthropic provider needs ANTHROPIC_API_KEY")
        if effort not in EFFORTS:
            raise ProviderError(f"effort must be one of {', '.join(EFFORTS)}, not {effort!r}")
        self.api_key = api_key
        self.model = model
        # Code review is reasoning-heavy, so "high" rather than the model's lower default effort.
        self.effort = effort
        # 16K keeps a non-streaming request well inside HTTP timeouts; thinking tokens count toward it.
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.policy = policy
        self.sleep = sleep

    def __repr__(self) -> str:  # never show the key
        return f"AnthropicProvider(model={self.model!r}, effort={self.effort!r})"

    def body(self, request: Request) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": self.effort}
        if request.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
            "output_config": output_config,
        }
        if self.fallbacks:
            body["fallbacks"] = "default"  # Anthropic picks the fallback model by refusal category
        return body

    def headers(self) -> dict[str, str]:
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION}
        if self.fallbacks:
            headers["anthropic-beta"] = FALLBACK_BETA
        return headers

    def complete(self, request: Request) -> Completion:
        data, _ = post_json(
            f"{self.base_url}/v1/messages",
            self.body(request),
            self.headers(),
            timeout=self.timeout,
            policy=self.policy,
            sleep=self.sleep,
        )
        return parse_message(data)


def _int(value: Any) -> int:
    return value if type(value) is int else 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_message(data: Any) -> Completion:
    """Turn a Messages API response into a :class:`Completion`, or raise if it isn't a usable answer.

    The stop reason is checked before the content: a refusal or a ``max_tokens`` cut-off can still carry
    text, and that text is not a complete review.
    """
    message = _dict(data)
    content = message.get("content")
    if message.get("type") != "message" or not isinstance(content, list):
        raise ProviderError("unexpected response from the Messages API (not a message)")
    stop = message.get("stop_reason")
    if stop == "refusal":
        category = _dict(message.get("stop_details")).get("category") or "unspecified"
        raise ProviderRefused(f"the model declined to review this diff (refusal category: {category})")
    if stop == "max_tokens":
        raise ProviderTruncated("the response hit max_tokens before it finished; raise CODELENS_MAX_TOKENS")
    # Only text blocks are the answer; thinking blocks and fallback markers are skipped.
    texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    if not texts or not all(isinstance(t, str) for t in texts):
        raise ProviderError(f"the Messages API response has no text (stop reason {stop!r})")
    usage = _dict(message.get("usage"))
    # input_tokens excludes cached prompt tokens; count everything the model read.
    read = sum(
        _int(usage.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    model = message.get("model")
    return Completion(
        "".join(str(t) for t in texts),
        model if isinstance(model, str) else "unknown",
        Usage(read, _int(usage.get("output_tokens"))),
    )
