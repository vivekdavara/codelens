from pathlib import Path

import pytest

from codelens.providers import DEFAULT_RECORDINGS, ProviderError, RecordedProvider, make_provider
from codelens.providers.anthropic import AnthropicProvider
from codelens.providers.openai import OpenAIProvider


def test_recorded_is_the_default() -> None:
    provider = make_provider(env={})
    assert isinstance(provider, RecordedProvider) and provider.directory == DEFAULT_RECORDINGS
    from_env = make_provider(env={"CODELENS_RECORDINGS": "recs"})
    assert isinstance(from_env, RecordedProvider) and from_env.directory == Path("recs")
    explicit = make_provider("recorded", recordings=Path("mine"), env={"CODELENS_RECORDINGS": "recs"})
    assert isinstance(explicit, RecordedProvider) and explicit.directory == Path("mine")


def test_anthropic_from_the_environment() -> None:
    provider = make_provider(env={"CODELENS_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"})
    assert isinstance(provider, AnthropicProvider)
    assert (provider.model, provider.effort, provider.max_tokens, provider.fallbacks) == (
        "claude-opus-5-5",
        "high",
        16000,
        True,
    )
    assert provider.base_url == "https://api.anthropic.com"
    tuned = make_provider(
        "anthropic",
        env={
            "ANTHROPIC_API_KEY": "k",
            "CODELENS_MODEL": "claude-sonnet-5-5",
            "CODELENS_EFFORT": "xhigh",
            "CODELENS_MAX_TOKENS": "32000",
            "CODELENS_FALLBACKS": "0",
            "CODELENS_BASE_URL": "http://127.0.0.1:9/",
            # Not read on purpose: other tools (Claude Code among them) set it for their own use.
            "ANTHROPIC_BASE_URL": "http://elsewhere.invalid",
        },
    )
    assert isinstance(tuned, AnthropicProvider)
    assert (tuned.model, tuned.effort, tuned.max_tokens, tuned.fallbacks) == (
        "claude-sonnet-5-5",
        "xhigh",
        32000,
        False,
    )
    assert tuned.base_url == "http://127.0.0.1:9"


def test_openai_from_the_environment() -> None:
    provider = make_provider("openai", model="gpt-5-mini", env={"OPENAI_API_KEY": "k"})
    assert isinstance(provider, OpenAIProvider)
    assert (provider.model, provider.base_url) == ("gpt-5-mini", "https://api.openai.com")


@pytest.mark.parametrize(
    ("name", "env", "message"),
    [
        ("anthropic", {}, "ANTHROPIC_API_KEY"),
        ("openai", {"ANTHROPIC_API_KEY": "k"}, "OPENAI_API_KEY"),
        ("anthropic", {"ANTHROPIC_API_KEY": "k", "CODELENS_MAX_TOKENS": "lots"}, "CODELENS_MAX_TOKENS"),
        ("anthropic", {"ANTHROPIC_API_KEY": "k", "CODELENS_MAX_TOKENS": "0"}, "CODELENS_MAX_TOKENS"),
        ("anthropic", {"ANTHROPIC_API_KEY": "k", "CODELENS_EFFORT": "huge"}, "effort must be one of"),
        ("gemini", {}, "unknown provider 'gemini'"),
    ],
)
def test_configuration_errors_fail_before_any_request(name: str, env: dict[str, str], message: str) -> None:
    with pytest.raises(ProviderError, match=message):
        make_provider(name, env=env)
