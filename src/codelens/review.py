"""The review engine: diff in, ranked and anchored findings out.

``review`` builds the prompt, asks the provider once, then validates and anchors every finding against the
files the model was shown. Nothing here talks to GitHub; posting is :mod:`codelens.github`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from codelens.diff import PatchSet
from codelens.findings import Finding, Rejection, Severity, check_response
from codelens.prompts import DEFAULT_MAX_PROMPT_CHARS, MAX_FINDINGS, build_prompt
from codelens.providers import Provider, RecordingMissing, Usage
from codelens.static import StaticResult

__all__ = ["RECORDING", "Review", "dedupe", "rank", "review"]


@dataclass
class Review:
    findings: list[Finding]
    """Anchored findings, worst first, at most ``max_findings`` of them."""
    held: list[Finding] = field(default_factory=list)
    """The valid findings ranked below the cap, worst first: posting reaches into these when some of
    ``findings`` were already posted by an earlier run."""
    max_findings: int = MAX_FINDINGS
    rejections: list[Rejection] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """Files not shown to the model, with the reason."""
    reviewed: list[str] = field(default_factory=list)
    """Paths of the files the model was shown."""
    over_cap: int = 0
    """Valid findings left out because the review already had ``max_findings`` (``len(held)``)."""
    repeated: int = 0
    """Findings left out when posting because an earlier CodeLens review on the PR already posted them."""
    provider: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    static: StaticResult | None = None
    """What the static pre-pass checked and found, or ``None`` when it didn't run."""
    duplicates: int = 0
    """Findings merged into a more severe (or more confident) one on the same line (see :func:`dedupe`)."""
    min_severity: Severity = Severity.LOW
    below: int = 0
    """Findings left out because they are less severe than ``min_severity``."""
    unrecorded: str | None = None
    """The key of the recording that would have answered, when the recorded provider had none and the review
    fell back to static analysis alone (``static_if_unrecorded``); ``None`` otherwise."""

    def rejection_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(r.kind for r in self.rejections).items()))


def rank(findings: list[Finding]) -> list[Finding]:
    """Worst first: severity, then confidence, then position, so equal findings keep a stable order."""
    return sorted(findings, key=lambda f: (*_order(f), f.path, f.line))


def _order(finding: Finding) -> tuple[int, float]:
    return finding.severity.rank, -finding.confidence


def dedupe(findings: Iterable[Finding]) -> tuple[list[Finding], int]:
    """One finding per (path, line, side): the one :func:`rank` puts first, and on a tie the first.

    One per line, whatever the categories, because posting identifies a finding by its line (the
    fingerprint: path, line text, occurrence), so two findings on one line would share a marker and the
    one not posted would count as posted on the next push. Keeping simply the most confident one would
    let a confident ``low`` replace a less confident ``critical``. Callers put static findings first, so a
    full tie goes to the reproducible one. Returns the kept findings in first-seen order and how many were
    merged away.
    """
    best: dict[tuple[str, int, str], Finding] = {}
    total = 0
    for finding in findings:
        total += 1
        key = (finding.path, finding.line, finding.side.value)
        if key not in best or _order(finding) < _order(best[key]):
            best[key] = finding
    return list(best.values()), total - len(best)


RECORDING = "a CodeLens recording"
"""Why a file under the recordings directory is not reviewed."""


def _under(path: str, directory: str | None) -> bool:
    return directory is not None and (path == directory or path.startswith(directory + "/"))


def review(
    patch: PatchSet,
    provider: Provider,
    *,
    static: StaticResult | None = None,
    max_findings: int = MAX_FINDINGS,
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    min_severity: Severity = Severity.LOW,
    static_if_unrecorded: bool = False,
    recordings: str | None = None,
) -> Review:
    """Review ``patch`` with one provider call, adding the static pre-pass's findings if it ran.

    Static findings are listed in the prompt and merged with the model's (:func:`dedupe`) before ranking
    and the cap. Findings less severe than ``min_severity`` are left out after the merge; the model is not
    told about the threshold, so changing it doesn't change the prompt (or any recording key).

    No call is made when no file is reviewable (only deletions, renames or binaries): such PRs cost nothing,
    and static findings, which don't depend on the prompt, are still returned.

    With ``static_if_unrecorded``, a recorded provider that has no answer for this prompt makes the review
    static analysis alone instead of an error: no model reviewed anything, ``reviewed`` is empty and
    ``unrecorded`` names the missing recording. This is for running on new pull requests (CodeLens's own,
    in CI) without a key; only a missing recording falls back, never an edited one or a live provider's
    failure.

    Files under ``recordings`` (the recordings directory as a path in the diff) are never reviewed: they are
    model answers, not code, and leaving them out means a recording committed to a pull request doesn't
    change the prompt it answers.
    Raises :class:`~codelens.providers.ProviderError` if the provider fails and
    :class:`~codelens.findings.FindingsFormatError` if its answer is not a findings object at all.
    """
    if max_findings < 1 or max_prompt_chars < 1:
        raise ValueError("max_findings and max_prompt_chars must be positive")
    own = [f.path for f in patch if _under(f.path, recordings)]
    if own:
        patch = PatchSet([f for f in patch if not _under(f.path, recordings)])
    pre = [f for f in static.findings if not _under(f.path, recordings)] if static is not None else []
    # The model is asked for the same cap the review applies.
    prompt = build_prompt(patch, max_prompt_chars, max_findings, static=pre)
    result = Review(
        [],
        skipped=[(path, RECORDING) for path in own] + prompt.skipped,
        reviewed=[f.path for f in prompt.files],
        provider=provider.name,
        static=static,
    )
    found: list[Finding] = []
    if prompt.files:
        try:
            completion = provider.complete(prompt.request)
        except RecordingMissing:
            if not static_if_unrecorded:
                raise
            result.unrecorded, result.reviewed = prompt.request.key(), []
        else:
            # Anchor against the files that were shown: a finding on a file the model never saw is a guess.
            found, result.rejections = check_response(completion.text, PatchSet(prompt.files))
            result.model, result.usage = completion.model, completion.usage
    merged, result.duplicates = dedupe([*pre, *found])
    kept = [f for f in merged if f.severity.rank <= min_severity.rank]
    result.min_severity, result.below = min_severity, len(merged) - len(kept)
    ranked = rank(kept)
    result.findings, result.held = ranked[:max_findings], ranked[max_findings:]
    result.max_findings = max_findings
    result.over_cap = len(result.held)
    return result
