"""Model providers: recorded (the default) and opt-in live vendors. See DESIGN.md, "Providers"."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from codelens.providers.anthropic import DEFAULT_MODEL as ANTHROPIC_MODEL
from codelens.providers.anthropic import AnthropicProvider
from codelens.providers.base import (
    Completion,
    Provider,
    ProviderError,
    ProviderHTTPError,
    ProviderRefused,
    ProviderTruncated,
    RecordingMissing,
    Request,
    Usage,
)
from codelens.providers.openai import DEFAULT_MODEL as OPENAI_MODEL
from codelens.providers.openai import OpenAIProvider
from codelens.providers.recorded import RecordedProvider, Recorder

__all__ = [
    "DEFAULT_RECORDINGS",
    "PROVIDERS",
    "Completion",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRefused",
    "ProviderTruncated",
    "RecordedProvider",
    "Recorder",
    "RecordingMissing",
    "Request",
    "Usage",
    "make_provider",
]

PROVIDERS = ("recorded", "anthropic", "openai")
DEFAULT_RECORDINGS = Path(".codelens/recordings")


def _max_tokens(env: Mapping[str, str]) -> int:
    raw = env.get("CODELENS_MAX_TOKENS") or "16000"
    if not raw.isdigit() or int(raw) < 1:
        raise ProviderError(f"CODELENS_MAX_TOKENS must be a positive integer, not {raw!r}")
    return int(raw)


def make_provider(
    name: str | None = None,
    *,
    model: str | None = None,
    recordings: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Provider:
    """The provider named by ``name`` or ``CODELENS_PROVIDER``; ``recorded`` when neither is set.

    Live providers read their key from the vendor's usual variable (``ANTHROPIC_API_KEY``,
    ``OPENAI_API_KEY``) and fail here, before any request, when it is missing. ``CODELENS_MODEL``,
    ``CODELENS_MAX_TOKENS``, ``CODELENS_EFFORT`` (Anthropic), ``CODELENS_FALLBACKS=0`` (Anthropic) and
    ``CODELENS_BASE_URL`` tune them.
    """
    env = os.environ if env is None else env
    name = name or env.get("CODELENS_PROVIDER") or "recorded"
    model = model or env.get("CODELENS_MODEL") or None
    if name == "recorded":
        return RecordedProvider(recordings or Path(env.get("CODELENS_RECORDINGS") or DEFAULT_RECORDINGS))
    base_url = env.get("CODELENS_BASE_URL") or None
    if name == "anthropic":
        return AnthropicProvider(
            env.get("ANTHROPIC_API_KEY", ""),
            model=model or ANTHROPIC_MODEL,
            effort=env.get("CODELENS_EFFORT") or "high",
            max_tokens=_max_tokens(env),
            fallbacks=env.get("CODELENS_FALLBACKS", "1") != "0",
            base_url=base_url or "https://api.anthropic.com",
        )
    if name == "openai":
        return OpenAIProvider(
            env.get("OPENAI_API_KEY", ""),
            model=model or OPENAI_MODEL,
            max_tokens=_max_tokens(env),
            base_url=base_url or "https://api.openai.com",
        )
    raise ProviderError(f"unknown provider {name!r}: choose one of {', '.join(PROVIDERS)}")
