"""Turn a :class:`~codelens.review.Review` into one GitHub pull request review, and post it.

One review per run (``POST /repos/{owner}/{repo}/pulls/{number}/reviews``, ``event: COMMENT``) with every
finding attached as a line comment, so the PR author gets one notification instead of one per finding.
Posting is never retried: GitHub has no idempotency key, so a retry after a timeout could post the review
twice. Every comment carries an invisible fingerprint, and findings an earlier run already posted are not
posted again, so pushing more commits to a PR doesn't repeat old comments. See DESIGN.md, "Posting to
GitHub".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any

from codelens import __version__
from codelens.findings import Finding
from codelens.providers import ProviderError, ProviderHTTPError
from codelens.providers.http import RetryPolicy, get_json, post_json
from codelens.review import Review

__all__ = [
    "GitHubError",
    "Posted",
    "comment_body",
    "fingerprint",
    "post_review",
    "posted_fingerprints",
    "review_payload",
    "summary_body",
]

API_VERSION = "2022-11-28"
_REPO = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_NO_RETRY = RetryPolicy(attempts=1)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
_MARKER = re.compile(r"<!-- codelens:([0-9a-f]{16}) -->")
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
MAX_PAGES = 10
"""Pages of 100 read per listing when looking for earlier comments: 1,000 comments is plenty for one PR."""


class GitHubError(RuntimeError):
    """GitHub refused the review, or could not be reached."""


@dataclass(frozen=True)
class Posted:
    id: int | None
    """The review's id, or ``None`` when nothing was posted because every finding was already on the PR."""
    url: str
    inline: bool
    """False when GitHub rejected the line comments and the findings went into the review body instead."""
    comments: int = 0
    """Findings posted in this review."""
    repeated: int = 0
    """Findings not posted because an earlier CodeLens review had already posted them."""


def defang(text: str) -> str:
    """Break ``@mentions`` outside code, so model-written text (steerable by the diff) can't ping people.

    A word joiner after the ``@`` stops GitHub from linking the mention; code spans and fences are left
    alone because GitHub never links mentions there and a pasted ``@decorator`` must stay valid code.
    """
    parts = _CODE.split(text)
    return "".join(part if i % 2 else _MENTION.sub("@\u2060", part) for i, part in enumerate(parts))


def _location(finding: Finding) -> str:
    return f"`{finding.path}:{finding.line}`"


def _cell(text: str) -> str:
    """Text safe inside one Markdown table cell: no pipes or line breaks."""
    return " ".join(text.split()).replace("|", "\\|")


def fingerprint(finding: Finding) -> str:
    """Identifies a finding across runs by its file and its line's text, not the line number.

    Line numbers move when lines are added above, and a model words the same issue differently from run to
    run, so neither identifies it. The rule this gives: CodeLens comments on a given line of code once per PR.
    """
    key = f"{finding.path}\0{' '.join(finding.quote.split())}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def comment_body(finding: Finding) -> str:
    meta = f"{finding.severity.value} · {finding.category.value} · confidence {finding.confidence:.2f}"
    return (
        f"**{defang(finding.title)}**\n\n{defang(finding.body)}\n\n<sub>CodeLens · {meta}</sub>\n"
        f"<!-- codelens:{fingerprint(finding)} -->"  # invisible on GitHub; read back by posted_fingerprints
    )


_FALLBACK_LEAD = "GitHub did not accept these as line comments, so they are listed here:"
_HINTS = {
    401: "the token is missing or invalid",
    403: "the token needs pull-requests: write (on a fork's PR the token is read-only)",
    404: "check the repository and PR number, and that the token can see the repository",
}


def summary_body(review: Review, *, details: str | None = None) -> str:
    """The review's top-level text: what was reviewed, the findings, and everything that was dropped.

    Findings are a table by default (they are also line comments). With ``details`` set, each one is written
    out in full instead, after ``details`` as a lead sentence when it is not empty: for a review whose line
    comments GitHub refused, and for the Actions job summary.
    """
    n = len(review.findings)
    files = len(review.reviewed)
    usage = review.usage
    if not review.reviewed:
        headline = "Nothing to review: no file in this diff has added lines CodeLens can comment on."
    else:
        headline = (
            f"{n} finding{'s' if n != 1 else ''} on {files} reviewed file{'s' if files != 1 else ''} "
            f"({review.model}, {usage.input_tokens:,} input / {usage.output_tokens:,} output tokens)."
        )
    lines = ["### CodeLens review", "", headline]
    if review.findings and details is None:
        lines += ["", "| Where | Severity | Finding |", "|---|---|---|"]
        lines += [
            f"| {_location(f)} | {f.severity.value} | {_cell(defang(f.title))} |" for f in review.findings
        ]
    elif review.findings:
        if details:
            lines += ["", details]
        for f in review.findings:
            lines += ["", f"#### {_location(f)}", "", comment_body(f)]
    notes = []
    if review.rejections:
        counts = ", ".join(f"{kind} {count}" for kind, count in review.rejection_counts().items())
        notes.append(
            f"Dropped {len(review.rejections)} finding(s) that failed validation or anchoring ({counts})."
        )
    if review.over_cap:
        notes.append(f"Left out {review.over_cap} lower-ranked finding(s) over the cap.")
    if review.repeated:
        notes.append(f"Not repeated: {review.repeated} finding(s) an earlier CodeLens review already posted.")
    if review.skipped:
        skipped = ", ".join(f"`{path}` ({reason})" for path, reason in review.skipped)
        notes.append(f"Not reviewed: {skipped}.")
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


def review_payload(review: Review, commit_id: str | None = None, *, inline: bool = True) -> dict[str, Any]:
    """The JSON body for the Reviews API: a ``COMMENT`` review, line comments unless ``inline`` is off."""
    body = summary_body(review) if inline else summary_body(review, details=_FALLBACK_LEAD)
    payload: dict[str, Any] = {"event": "COMMENT", "body": body}
    if commit_id:
        payload["commit_id"] = commit_id  # pin the comments to the commit that was reviewed
    payload["comments"] = (
        [
            {"path": f.path, "line": f.line, "side": f.side.value, "body": comment_body(f)}
            for f in review.findings
        ]
        if inline
        else []
    )
    return payload


def _hint(exc: ProviderHTTPError) -> str:
    return f" ({_HINTS[exc.status]})" if exc.status in _HINTS else ""


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": f"codelens/{__version__}",
    }


def _post(url: str, payload: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    data, _ = post_json(url, payload, _headers(token), timeout=timeout, policy=_NO_RETRY)
    return data if isinstance(data, dict) else {}


def _next_page(link: str | None, base: str) -> str | None:
    """The ``rel="next"`` URL of a Link header, but only on the same API host: the token goes with it."""
    match = _NEXT.search(link or "")
    if match is None or not match.group(1).startswith(base + "/"):
        return None
    return match.group(1)


def posted_fingerprints(repo: str, number: int, token: str, *, api_url: str, timeout: float) -> set[str]:
    """Fingerprints of the findings CodeLens already posted on the PR: in line comments and review bodies.

    Reading is a GET, so unlike posting it keeps the default retries.
    """
    base = api_url.rstrip("/")
    found: set[str] = set()
    for listing in ("comments", "reviews"):
        url: str | None = f"{base}/repos/{repo}/pulls/{number}/{listing}?per_page=100"
        for _ in range(MAX_PAGES):
            if url is None:
                break
            data, headers = get_json(url, _headers(token), timeout=timeout)
            for item in data if isinstance(data, list) else []:
                body = item.get("body") if isinstance(item, dict) else None
                if isinstance(body, str):
                    found.update(_MARKER.findall(body))
            url = _next_page(headers.get("link"), base)
    return found


def post_review(
    review: Review,
    repo: str,
    number: int,
    token: str,
    *,
    commit_id: str | None = None,
    api_url: str = "https://api.github.com",
    timeout: float = 30.0,
) -> Posted:
    """Post ``review`` to pull request ``repo#number``, leaving out findings already posted there.

    If GitHub answers 422 (typically "line must be part of the diff", when the PR moved on after the diff was
    fetched), the review is posted once more with the findings in its body instead of as line comments. When
    every finding was already posted, nothing is posted and the result's ``id`` is ``None``.
    """
    if not _REPO.match(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise GitHubError(f"repository must look like owner/name, not {repo!r}")
    if not token:
        raise GitHubError("posting a review needs a GitHub token (GITHUB_TOKEN)")
    url = f"{api_url.rstrip('/')}/repos/{repo}/pulls/{number}/reviews"
    try:
        already = posted_fingerprints(repo, number, token, api_url=api_url, timeout=timeout)
    except ProviderHTTPError as exc:
        raise GitHubError(f"could not read the PR's earlier comments: {exc}{_hint(exc)}") from None
    except ProviderError as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from None
    new = [f for f in review.findings if fingerprint(f) not in already]
    review = replace(review, findings=new, repeated=review.repeated + len(review.findings) - len(new))
    if not new:
        return Posted(None, "", inline=False, repeated=review.repeated)
    inline = True
    try:
        try:
            data = _post(url, review_payload(review, commit_id), token, timeout)
        except ProviderHTTPError as exc:
            if exc.status != 422:
                raise
            inline = False
            data = _post(url, review_payload(review, commit_id, inline=False), token, timeout)
    except ProviderHTTPError as exc:
        raise GitHubError(f"GitHub did not accept the review: {exc}{_hint(exc)}") from None
    except ProviderError as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from None
    review_id = data.get("id")
    return Posted(
        review_id if isinstance(review_id, int) else 0,
        str(data.get("html_url", "")),
        inline,
        comments=len(review.findings),
        repeated=review.repeated,
    )
