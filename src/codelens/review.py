"""The review engine: diff in, ranked and anchored findings out.

``review`` builds the prompt, asks the provider once, then validates and anchors every finding against the
files the model was shown. Nothing here talks to GitHub; posting is :mod:`codelens.github`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from codelens.diff import PatchSet
from codelens.findings import Finding, Rejection, check_response
from codelens.prompts import DEFAULT_MAX_PROMPT_CHARS, MAX_FINDINGS, build_prompt
from codelens.providers import Provider, Usage

__all__ = ["Review", "rank", "review"]


@dataclass
class Review:
    findings: list[Finding]
    """Anchored findings, worst first, at most ``max_findings`` of them."""
    rejections: list[Rejection] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """Files not shown to the model, with the reason."""
    reviewed: list[str] = field(default_factory=list)
    """Paths of the files the model was shown."""
    over_cap: int = 0
    """Valid findings dropped because the review already had ``max_findings``."""
    provider: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)

    def rejection_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(r.kind for r in self.rejections).items()))


def rank(findings: list[Finding]) -> list[Finding]:
    """Worst first: severity, then confidence, then position, so equal findings keep a stable order."""
    return sorted(findings, key=lambda f: (f.severity.rank, -f.confidence, f.path, f.line))


def review(
    patch: PatchSet,
    provider: Provider,
    *,
    max_findings: int = MAX_FINDINGS,
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
) -> Review:
    """Review ``patch`` with one provider call.

    No call is made when no file is reviewable (only deletions, renames or binaries): such PRs cost nothing.
    Raises :class:`~codelens.providers.ProviderError` if the provider fails and
    :class:`~codelens.findings.FindingsFormatError` if its answer is not a findings object at all.
    """
    prompt = build_prompt(patch, max_prompt_chars)
    result = Review(
        [], skipped=prompt.skipped, reviewed=[f.path for f in prompt.files], provider=provider.name
    )
    if not prompt.files:
        return result
    completion = provider.complete(prompt.request)
    # Anchor against the files that were shown: a finding on a file the model never saw is a guess.
    findings, result.rejections = check_response(completion.text, PatchSet(prompt.files))
    ranked = rank(findings)
    result.findings = ranked[:max_findings]
    result.over_cap = len(ranked) - len(result.findings)
    result.model = completion.model
    result.usage = completion.usage
    return result
