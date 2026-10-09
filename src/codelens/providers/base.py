"""The provider interface: one request in, one completion out.

A provider turns a :class:`Request` (system prompt, user prompt, optional JSON schema for structured
output) into a :class:`Completion`. The review engine only sees this interface, so tests run against recorded
responses and live vendors are a configuration choice. See DESIGN.md, "Providers".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "Completion",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRefused",
    "ProviderTruncated",
    "RecordingMissing",
    "Request",
    "Usage",
]


@dataclass(frozen=True)
class Request:
    system: str
    prompt: str
    schema: Mapping[str, Any] | None = None
    """JSON schema the response must follow (structured output), or ``None`` for free text."""

    def key(self) -> str:
        """SHA-256 of a canonical JSON encoding of all three inputs: the recorded provider's lookup key.

        The schema is part of the key because changing it can change the model's answer as much as changing
        the prompt can.
        """
        canonical = json.dumps(
            {"system": self.system, "prompt": self.prompt, "schema": self.schema},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    """The model that produced the text, as the vendor reported it (``hand-written`` for test fixtures)."""
    usage: Usage = field(default_factory=Usage)


class Provider(Protocol):
    name: str

    def complete(self, request: Request) -> Completion: ...


class ProviderError(RuntimeError):
    """A provider could not produce a usable completion."""


class RecordingMissing(ProviderError):
    """No recorded response for this request: the prompt or schema changed, or it was never recorded."""


class ProviderRefused(ProviderError):
    """The model declined the request (a safety refusal); its output, if any, is not a review."""


class ProviderTruncated(ProviderError):
    """The model hit its output limit, so structured output may be cut off mid-JSON."""


class ProviderHTTPError(ProviderError):
    """The vendor API answered with an error status that retries did not fix."""

    def __init__(
        self, status: int, message: str, error_type: str | None = None, request_id: str | None = None
    ) -> None:
        detail = f"HTTP {status}" + (f" {error_type}" if error_type else "") + f": {message}"
        if request_id:
            detail += f" (request id {request_id})"
        super().__init__(detail)
        self.status = status
        self.error_type = error_type
        self.request_id = request_id
