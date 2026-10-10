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
import time
from collections.abc import Callable
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
    "plural",
    "post_review",
    "posted_fingerprints",
    "review_payload",
    "static_notes",
    "summary_body",
]

API_VERSION = "2022-11-28"
_REPO = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_NO_RETRY = RetryPolicy(attempts=1)
# A fence opens and closes at the start of a line; an inline code span starts at a backtick that isn't
# escaped. Anything else that looks like code to a naive scan (an escaped backtick, a stray ```) is text to
# GitHub, so mentions in it would be live.
_CODE = re.compile(r"(^```.*?^```|(?<![\\`])`[^`\n]+`)", re.DOTALL | re.MULTILINE)
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
DEFAULT_AUTHOR = "github-actions[bot]"
"""The login CodeLens posts as with the Actions token; only its comments' markers count as already posted."""
_MARKER = re.compile(r"<!-- codelens:([0-9a-f]{16}) -->")
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
MAX_PAGES = 10
MAX_BODY_CHARS = 60_000
"""GitHub refuses review and comment bodies over 65,536 characters; stay clear of it."""
MAX_LISTED = 20
"""Skipped files named in a summary; a huge PR can skip thousands, and the rest are counted instead."""
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
    """Make model-written text (steerable by the diff) safe to post.

    ``@mentions`` outside code get a word joiner after the ``@``, which stops GitHub from linking them; code
    spans and fences are left alone because GitHub never links mentions there and a pasted ``@decorator``
    must stay valid code. ``<!--`` is escaped everywhere, so the only HTML comments in a posted body are
    CodeLens's own fingerprint markers.
    """
    parts = _CODE.split(text.replace("<!--", "&lt;!--"))
    return "".join(part if i % 2 else _MENTION.sub("@\u2060", part) for i, part in enumerate(parts))


def _path(path: str) -> str:
    """A file path as inline code that can't break out of its span or ping anyone (paths come from the PR)."""
    clean = "".join("?" if ch < " " or ch in "`\x7f\u2028\u2029" else ch for ch in path)
    return f"`{_MENTION.sub('@' + chr(0x2060), clean)}`"


def _location(finding: Finding) -> str:
    return _path(f"{finding.path}:{finding.line}")


def _cell(text: str) -> str:
    """Text safe inside one Markdown table cell: no pipes or line breaks."""
    return " ".join(text.split()).replace("|", "\\|")


def fingerprint(finding: Finding) -> str:
    """Identifies a finding across runs by its file, its line's text and which of the identical lines it is.

    Line numbers move when lines are added above, and a model words the same issue differently from run to
    run, so neither identifies it. ``occurrence`` keeps two identical lines (two ``return None``, two blank
    lines) apart. The rule this gives: CodeLens comments on a given line of code once per PR.
    """
    key = f"{finding.path}\0{' '.join(finding.quote.split())}\0{finding.occurrence}"
    return hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


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
    if not review.reviewed and review.findings:
        headline = (
            f"{plural(n, 'finding')} from static analysis; no file in this diff could be shown to the model."
        )
    elif not review.reviewed:
        headline = "Nothing reviewed: no file in this diff could be shown to the model."
    else:
        headline = (
            f"{plural(n, 'finding')} on {plural(files, 'reviewed file')} "
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
        dropped = plural(len(review.rejections), "finding")
        notes.append(f"Dropped {dropped} that failed validation or anchoring ({counts}).")
    if review.duplicates:
        notes.append(f"Merged {plural(review.duplicates, 'duplicate finding')} (same line and category).")
    if review.over_cap:
        notes.append(f"Left out {plural(review.over_cap, 'lower-ranked finding')} over the cap.")
    if review.repeated:
        notes.append(
            f"Not repeated: {plural(review.repeated, 'finding')} an earlier CodeLens review already posted."
        )
    if review.skipped:
        skipped = ", ".join(f"{_path(path)} ({reason})" for path, reason in review.skipped[:MAX_LISTED])
        rest = len(review.skipped) - MAX_LISTED
        notes.append(f"Not reviewed: {skipped}" + (f", and {rest:,} more." if rest > 0 else "."))
    notes += static_notes(review)
    if notes:
        lines += ["", *notes]
    body = "\n".join(lines)
    if len(body) > MAX_BODY_CHARS:  # e.g. a high max-findings, written out in full: avoid GitHub's 422
        body = body[:MAX_BODY_CHARS] + "\n\n(truncated)"
    return body


def static_notes(review: Review) -> list[str]:
    """What the static pre-pass checked, skipped and ran into, as sentences for the summary."""
    static = review.static
    if static is None:
        return []
    notes = [f"Static analysis: {note}." for note in static.notes]
    if static.analysed:
        tool = f"{static.tool} and CodeLens rules" if static.tool else "CodeLens rules"
        notes.append(
            f"Static analysis ({tool}) checked {plural(len(static.analysed), 'Python file')} and found "
            f"{plural(len(static.findings), 'problem')} on added lines."
        )
    if static.skipped:
        listed = ", ".join(f"{_path(path)} ({reason})" for path, reason in static.skipped[:MAX_LISTED])
        rest = len(static.skipped) - MAX_LISTED
        notes.append(
            f"Not checked by static analysis: {listed}" + (f", and {rest:,} more." if rest > 0 else ".")
        )
    return notes


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


def plural(n: int, noun: str) -> str:
    """``1 finding``, ``3 findings``: counts in messages, written once."""
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


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


def posted_fingerprints(
    repo: str,
    number: int,
    token: str,
    *,
    api_url: str,
    timeout: float,
    author: str = DEFAULT_AUTHOR,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    """Fingerprints of the findings ``author`` already posted on the PR, in line comments and review bodies.

    Only ``author``'s markers count: anyone can compute a fingerprint from the public diff, so a marker in
    the PR author's own comment would otherwise silence CodeLens on any line they chose. Reading is a GET,
    so unlike posting it keeps the default retries.
    """
    base = api_url.rstrip("/")
    found: set[str] = set()
    for listing in ("comments", "reviews"):
        url: str | None = f"{base}/repos/{repo}/pulls/{number}/{listing}?per_page=100"
        for _ in range(MAX_PAGES):
            if url is None:
                break
            data, headers = get_json(url, _headers(token), timeout=timeout, sleep=sleep)
            for item in data if isinstance(data, list) else []:
                if not isinstance(item, dict):
                    continue
                user, body = item.get("user"), item.get("body")
                login = user.get("login") if isinstance(user, dict) else None
                if login == author and isinstance(body, str):
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
    author: str = DEFAULT_AUTHOR,
    sleep: Callable[[float], None] = time.sleep,
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
        already = posted_fingerprints(
            repo, number, token, api_url=api_url, timeout=timeout, author=author, sleep=sleep
        )
    except ProviderHTTPError as exc:
        raise GitHubError(f"could not read the PR's earlier comments: {exc}{_hint(exc)}") from None
    except ProviderError as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from None
    # Drop repeats from the whole ranked pool before capping: otherwise findings an earlier run posted would
    # keep using up the cap, and the ones ranked below them would never be posted.
    pool = review.findings + review.held
    fresh = [f for f in pool if fingerprint(f) not in already]
    new, held = fresh[: review.max_findings], fresh[review.max_findings :]
    review = replace(
        review, findings=new, held=held, over_cap=len(held), repeated=review.repeated + len(pool) - len(fresh)
    )
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
