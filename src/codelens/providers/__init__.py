"""Model providers: recorded (the default) and opt-in live vendors. See DESIGN.md, "Providers"."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def _overrides(env: Mapping[str, str], model: str | None) -> dict[str, Any]:
    """Only the settings the environment actually gives; everything else keeps the provider's own default,
    so a default changed in a provider class reaches CLI and action users too."""
    overrides: dict[str, Any] = {}
    if model:
        overrides["model"] = model
    if base_url := env.get("CODELENS_BASE_URL"):
        overrides["base_url"] = base_url
    if raw := env.get("CODELENS_MAX_TOKENS"):
        if not raw.isdigit() or int(raw) < 1:
            raise ProviderError(f"CODELENS_MAX_TOKENS must be a positive integer, not {raw!r}")
        overrides["max_tokens"] = int(raw)
    return overrides


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
    if name == "recorded":
        return RecordedProvider(recordings or Path(env.get("CODELENS_RECORDINGS") or DEFAULT_RECORDINGS))
    overrides = _overrides(env, model or env.get("CODELENS_MODEL"))
    if name == "anthropic":
        if effort := env.get("CODELENS_EFFORT"):
            overrides["effort"] = effort
        if env.get("CODELENS_FALLBACKS") == "0":
            overrides["fallbacks"] = False
        return AnthropicProvider(env.get("ANTHROPIC_API_KEY", ""), **overrides)
    if name == "openai":
        return OpenAIProvider(env.get("OPENAI_API_KEY", ""), **overrides)
    raise ProviderError(f"unknown provider {name!r}: choose one of {', '.join(PROVIDERS)}")
