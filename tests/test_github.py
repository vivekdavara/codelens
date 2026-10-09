from typing import Any

import pytest
from conftest import FakeServer, Reply

from codelens import __version__
from codelens.findings import Category, Finding, Rejection, Severity
from codelens.github import (
    GitHubError,
    comment_body,
    defang,
    post_review,
    review_payload,
    summary_body,
)
from codelens.providers import Usage
from codelens.review import Review

TOKEN = "ghs_test_not_a_real_token"


def finding(line: int = 6, **overrides: Any) -> Finding:
    values: dict[str, Any] = {
        "path": "svc/pay.py",
        "line": line,
        "severity": Severity.HIGH,
        "category": Category.BUG,
        "title": "Clamping hides the error",
        "body": "A refund smaller than the fee becomes 0. Raise instead.",
        "confidence": 0.8,
        "quote": "if net < 0:",
    }
    values.update(overrides)
    return Finding(**values)


def a_review(*findings: Finding, **overrides: Any) -> Review:
    values: dict[str, Any] = {
        "findings": list(findings),
        "reviewed": ["svc/pay.py"],
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "usage": Usage(12_345, 678),
    }
    values.update(overrides)
    return Review(**values)


def test_defang_breaks_mentions_outside_code_only() -> None:
    assert defang("cc @alice and @org/team") == "cc @⁠alice and @⁠org/team"
    assert defang("use `@property` here") == "use `@property` here"
    assert defang("```python\n@pytest.fixture\ndef f(): ...\n```\nthanks @bob") == (
        "```python\n@pytest.fixture\ndef f(): ...\n```\nthanks @⁠bob"
    )
    assert defang("a lone @ sign") == "a lone @ sign"


def test_comment_body() -> None:
    assert comment_body(finding()) == (
        "**Clamping hides the error**\n\n"
        "A refund smaller than the fee becomes 0. Raise instead.\n\n"
        "<sub>CodeLens · high · bug · confidence 0.80</sub>"
    )


def test_summary_lists_findings_and_everything_dropped() -> None:
    review = a_review(
        finding(),
        finding(9, severity=Severity.LOW, title="Pipe | in\ntitle"),
        rejections=[
            Rejection(2, "misquoted", "x"),
            Rejection(3, "invalid", "y"),
            Rejection(4, "misquoted", "z"),
        ],
        skipped=[("logo.png", "binary file")],
        over_cap=1,
    )
    assert summary_body(review).splitlines() == [
        "### CodeLens review",
        "",
        "2 findings on 1 reviewed file (claude-opus-5-5, 12,345 input / 678 output tokens).",
        "",
        "| Where | Severity | Finding |",
        "|---|---|---|",
        "| `svc/pay.py:6` | high | Clamping hides the error |",
        "| `svc/pay.py:9` | low | Pipe \\| in title |",
        "",
        "Dropped 3 finding(s) that failed validation or anchoring (invalid 1, misquoted 2).",
        "Left out 1 lower-ranked finding(s) over the cap.",
        "Not reviewed: `logo.png` (binary file).",
    ]


def test_summary_without_findings() -> None:
    assert summary_body(a_review()).splitlines()[2].startswith("0 findings on 1 reviewed file")


def test_payload_has_one_comment_per_finding_on_the_right_side() -> None:
    payload = review_payload(a_review(finding(), finding(7, quote="net = 0")), "abc123")
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "abc123"
    assert [(c["path"], c["line"], c["side"]) for c in payload["comments"]] == [
        ("svc/pay.py", 6, "RIGHT"),
        ("svc/pay.py", 7, "RIGHT"),
    ]
    assert payload["comments"][0]["body"] == comment_body(finding())
    assert "commit_id" not in review_payload(a_review(finding()))


def test_posts_one_review(fake_server: FakeServer) -> None:
    fake_server.reply(
        Reply(200, {"id": 81, "html_url": "https://github.com/o/r/pull/3#pullrequestreview-81"})
    )
    posted = post_review(a_review(finding()), "o/r", 3, TOKEN, commit_id="abc", api_url=fake_server.url)
    assert (posted.id, posted.inline) == (81, True)
    assert posted.url.endswith("pullrequestreview-81")
    (seen,) = fake_server.requests
    assert seen.path == "/repos/o/r/pulls/3/reviews"
    assert seen.headers["authorization"] == f"Bearer {TOKEN}"
    assert seen.headers["accept"] == "application/vnd.github+json"
    assert seen.headers["x-github-api-version"] == "2022-11-28"
    assert seen.headers["user-agent"] == f"codelens/{__version__}"
    assert seen.body == review_payload(a_review(finding()), "abc")


def test_a_422_falls_back_to_findings_in_the_body(fake_server: FakeServer) -> None:
    unprocessable = {"message": "Unprocessable Entity", "errors": ["Line could not be resolved"]}
    fake_server.reply(Reply(422, unprocessable), Reply(200, {"id": 82, "html_url": "u"}))
    posted = post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert (posted.id, posted.inline) == (82, False)
    first, second = fake_server.requests
    assert len(first.body["comments"]) == 1 and second.body["comments"] == []
    assert "#### `svc/pay.py:6`" in second.body["body"]
    assert "Clamping hides the error" in second.body["body"]


def test_server_errors_are_not_retried(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(502, {"message": "Bad gateway"}))
    with pytest.raises(GitHubError, match="HTTP 502") as exc:
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert len(fake_server.requests) == 1
    assert TOKEN not in str(exc.value)


def test_a_second_422_is_reported(fake_server: FakeServer) -> None:
    fake_server.reply(*[Reply(422, {"message": "Validation Failed"})] * 2)
    with pytest.raises(GitHubError, match="Validation Failed"):
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert len(fake_server.requests) == 2


@pytest.mark.parametrize("repo", ["o", "o/r/x", "../o/r", "../x", "o/..", "o/r?x=1", "o r/x"])
def test_repository_names_are_checked(repo: str) -> None:
    with pytest.raises(GitHubError, match="owner/name"):
        post_review(a_review(finding()), repo, 3, TOKEN, api_url="http://127.0.0.1:9")


def test_a_token_is_required() -> None:
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        post_review(a_review(finding()), "o/r", 3, "", api_url="http://127.0.0.1:9")
