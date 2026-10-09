"""OpenAI models through the Chat Completions API (``POST /v1/chat/completions``). Opt-in:
``CODELENS_PROVIDER=openai``.

Same shape as the Anthropic provider: stdlib HTTP, structured output (``response_format`` with a strict JSON
schema), and a refusal or a length cut-off raised instead of returned as text.
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

__all__ = ["DEFAULT_MODEL", "OpenAIProvider", "parse_chat_completion"]

DEFAULT_MODEL = "gpt-5"
SCHEMA_NAME = "codelens_findings"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 16000,
        base_url: str = "https://api.openai.com",
        timeout: float = 600.0,
        policy: RetryPolicy = DEFAULT_POLICY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ProviderError("the openai provider needs OPENAI_API_KEY")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.policy = policy
        self.sleep = sleep

    def __repr__(self) -> str:  # never show the key
        return f"OpenAIProvider(model={self.model!r})"

    def body(self, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            # Reasoning models count hidden reasoning tokens against this limit too.
            "max_completion_tokens": self.max_tokens,
        }
        if request.schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": request.schema},
            }
        return body

    def complete(self, request: Request) -> Completion:
        data, _ = post_json(
            f"{self.base_url}/v1/chat/completions",
            self.body(request),
            {"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            policy=self.policy,
            sleep=self.sleep,
        )
        return parse_chat_completion(data)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    return value if type(value) is int else 0


def parse_chat_completion(data: Any) -> Completion:
    """Turn a Chat Completions response into a :class:`Completion`, or raise if it isn't a usable answer."""
    completion = _dict(data)
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("unexpected response from the Chat Completions API (no choices)")
    choice = _dict(choices[0])
    message = _dict(choice.get("message"))
    if isinstance(message.get("refusal"), str) and message["refusal"]:
        raise ProviderRefused(f"the model declined to review this diff: {message['refusal']}")
    finish = choice.get("finish_reason")
    if finish == "content_filter":
        raise ProviderRefused("the response was withheld by the content filter")
    if finish == "length":
        raise ProviderTruncated("the response hit max_completion_tokens before it finished")
    text = message.get("content")
    if not isinstance(text, str) or not text:
        raise ProviderError(f"the Chat Completions response has no text (finish reason {finish!r})")
    usage = _dict(completion.get("usage"))
    model = completion.get("model")
    return Completion(
        text,
        model if isinstance(model, str) else "unknown",
        Usage(_int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))),
    )
