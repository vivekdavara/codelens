import json
from typing import Any

import pytest
from conftest import FakeServer, Reply

from codelens import __version__
from codelens.findings import Category, Finding, Rejection, Severity
from codelens.github import (
    GitHubError,
    comment_body,
    defang,
    fingerprint,
    post_review,
    posted_fingerprints,
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
    assert defang("cc @alice and @org/team") == "cc @\u2060alice and @\u2060org/team"
    assert defang("use `@property` here") == "use `@property` here"
    assert defang("```python\n@pytest.fixture\ndef f(): ...\n```\nthanks @bob") == (
        "```python\n@pytest.fixture\ndef f(): ...\n```\nthanks @\u2060bob"
    )
    assert defang("a lone @ sign") == "a lone @ sign"


def test_comment_body() -> None:
    assert comment_body(finding()) == (
        "**Clamping hides the error**\n\n"
        "A refund smaller than the fee becomes 0. Raise instead.\n\n"
        "<sub>CodeLens · high · bug · confidence 0.80</sub>\n"
        f"<!-- codelens:{fingerprint(finding())} -->"
    )


def bot(body: str, login: str = "github-actions[bot]") -> dict[str, Any]:
    """A PR comment as the GitHub API lists it, written by ``login``."""
    return {"user": {"login": login, "type": "Bot"}, "body": body}


def no_earlier_comments(server: FakeServer) -> None:
    server.reply(Reply(200, []), Reply(200, []))  # GET .../comments, then GET .../reviews


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
        "Dropped 3 findings that failed validation or anchoring (invalid 1, misquoted 2).",
        "Left out 1 lower-ranked finding over the cap.",
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
    no_earlier_comments(fake_server)
    fake_server.reply(
        Reply(200, {"id": 81, "html_url": "https://github.com/o/r/pull/3#pullrequestreview-81"})
    )
    posted = post_review(a_review(finding()), "o/r", 3, TOKEN, commit_id="abc", api_url=fake_server.url)
    assert (posted.id, posted.inline, posted.comments, posted.repeated) == (81, True, 1, 0)
    assert posted.url.endswith("pullrequestreview-81")
    assert [(r.method, r.path) for r in fake_server.requests] == [
        ("GET", "/repos/o/r/pulls/3/comments?per_page=100"),
        ("GET", "/repos/o/r/pulls/3/reviews?per_page=100"),
        ("POST", "/repos/o/r/pulls/3/reviews"),
    ]
    seen = fake_server.requests[-1]
    assert seen.headers["authorization"] == f"Bearer {TOKEN}"
    assert seen.headers["accept"] == "application/vnd.github+json"
    assert seen.headers["x-github-api-version"] == "2022-11-28"
    assert seen.headers["user-agent"] == f"codelens/{__version__}"
    assert seen.body == review_payload(a_review(finding()), "abc")


def test_a_422_falls_back_to_findings_in_the_body(fake_server: FakeServer) -> None:
    unprocessable = {"message": "Unprocessable Entity", "errors": ["Line could not be resolved"]}
    no_earlier_comments(fake_server)
    fake_server.reply(Reply(422, unprocessable), Reply(200, {"id": 82, "html_url": "u"}))
    posted = post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert (posted.id, posted.inline) == (82, False)
    first, second = fake_server.requests[2:]
    assert len(first.body["comments"]) == 1 and second.body["comments"] == []
    assert "#### `svc/pay.py:6`" in second.body["body"]
    assert "Clamping hides the error" in second.body["body"]


def test_server_errors_are_not_retried(fake_server: FakeServer) -> None:
    no_earlier_comments(fake_server)
    fake_server.reply(Reply(502, {"message": "Bad gateway"}))
    with pytest.raises(GitHubError, match="HTTP 502") as exc:
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert [r.method for r in fake_server.requests] == ["GET", "GET", "POST"]
    assert TOKEN not in str(exc.value)


def test_a_second_422_is_reported(fake_server: FakeServer) -> None:
    no_earlier_comments(fake_server)
    fake_server.reply(*[Reply(422, {"message": "Validation Failed"})] * 2)
    with pytest.raises(GitHubError, match="Validation Failed"):
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert [r.method for r in fake_server.requests] == ["GET", "GET", "POST", "POST"]


@pytest.mark.parametrize("repo", ["o", "o/r/x", "../o/r", "../x", "o/..", "o/r?x=1", "o r/x"])
def test_repository_names_are_checked(repo: str) -> None:
    with pytest.raises(GitHubError, match="owner/name"):
        post_review(a_review(finding()), repo, 3, TOKEN, api_url="http://127.0.0.1:9")


def test_a_token_is_required() -> None:
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        post_review(a_review(finding()), "o/r", 3, "", api_url="http://127.0.0.1:9")


def test_summary_with_details_writes_each_finding_out() -> None:
    lines = summary_body(a_review(finding()), details="").splitlines()
    assert lines[3:] == ["", "#### `svc/pay.py:6`", "", *comment_body(finding()).splitlines()]
    with_lead = summary_body(a_review(finding()), details="Findings:").splitlines()
    assert with_lead[3:6] == ["", "Findings:", ""]


def test_summary_when_nothing_was_reviewed() -> None:
    review = a_review(reviewed=[], model="", usage=Usage(), skipped=[("gone.txt", "deleted file")])
    assert summary_body(review).splitlines() == [
        "### CodeLens review",
        "",
        "Nothing reviewed: no file in this diff could be shown to the model.",
        "",
        "Not reviewed: `gone.txt` (deleted file).",
    ]


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "missing or invalid"), (403, "pull-requests: write"), (404, "check the repository and PR number")],
)
def test_permission_errors_say_what_to_fix(fake_server: FakeServer, status: int, hint: str) -> None:
    fake_server.reply(Reply(status, {"message": "Resource not accessible by integration"}))
    with pytest.raises(GitHubError, match=hint):
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)


def test_an_unreachable_github_is_reported_as_such() -> None:
    with pytest.raises(GitHubError, match="could not reach GitHub"):
        post_review(
            a_review(finding()),
            "o/r",
            3,
            TOKEN,
            api_url="http://127.0.0.1:9",
            timeout=2,
            sleep=lambda _: None,
        )


def test_fingerprints_ignore_line_numbers_and_whitespace_but_not_path_or_text() -> None:
    base = fingerprint(finding())
    assert (
        fingerprint(finding(line=40, quote="  if   net < 0:", title="Reworded", severity=Severity.LOW))
        == base
    )
    assert fingerprint(finding(path="svc/other.py")) != base
    assert fingerprint(finding(quote="if net <= 0:")) != base


def test_earlier_fingerprints_come_from_comments_and_review_bodies_across_pages(
    fake_server: FakeServer,
) -> None:
    a, b, c = (fingerprint(finding(quote=q)) for q in ("one", "two", "three"))
    page2 = f"{fake_server.url}/repositories/1/pulls/3/comments?per_page=100&page=2"
    fake_server.reply(
        Reply(
            200,
            [bot(f"x\n<!-- codelens:{a} -->"), {"body": None}, "junk"],
            {"Link": f'<{page2}>; rel="next"'},
        ),
        Reply(200, [bot(f"<!-- codelens:{b} -->")]),
        Reply(200, [bot(f"#### `x:1`\n<!-- codelens:{c} -->"), bot("<!-- codelens:nothex -->")]),
    )
    assert posted_fingerprints("o/r", 3, TOKEN, api_url=fake_server.url, timeout=5) == {a, b, c}
    assert [r.path for r in fake_server.requests] == [
        "/repos/o/r/pulls/3/comments?per_page=100",
        "/repositories/1/pulls/3/comments?per_page=100&page=2",
        "/repos/o/r/pulls/3/reviews?per_page=100",
    ]


def test_a_next_page_on_another_host_is_not_followed(fake_server: FakeServer) -> None:
    fake_server.reply(
        Reply(200, [], {"Link": '<https://evil.example/steal?page=2>; rel="next"'}), Reply(200, [])
    )
    assert posted_fingerprints("o/r", 3, TOKEN, api_url=fake_server.url, timeout=5) == set()
    assert len(fake_server.requests) == 2  # one page of comments, one of reviews; the link was ignored


def test_findings_already_on_the_pr_are_not_posted_again(fake_server: FakeServer) -> None:
    old, new = finding(), finding(7, quote="net = 0", title="Silent clamp")
    fake_server.reply(
        Reply(200, [bot(comment_body(old))]), Reply(200, []), Reply(200, {"id": 90, "html_url": "u"})
    )
    posted = post_review(a_review(old, new), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert (posted.id, posted.comments, posted.repeated) == (90, 1, 1)
    sent = fake_server.requests[-1].body
    assert [c["line"] for c in sent["comments"]] == [7]
    assert "Not repeated: 1 finding an earlier CodeLens review already posted." in sent["body"]


def test_nothing_is_posted_when_every_finding_is_already_there(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, [bot(comment_body(finding()))]), Reply(200, []))
    posted = post_review(a_review(finding(line=9)), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert (posted.id, posted.repeated) == (None, 1)
    assert [r.method for r in fake_server.requests] == ["GET", "GET"]


def test_reading_earlier_comments_can_fail_with_a_hint(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(404, {"message": "Not Found"}))
    with pytest.raises(GitHubError, match=r"could not read the PR's earlier comments.*check the repository"):
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)


def test_reading_stops_after_max_pages(fake_server: FakeServer) -> None:
    def page(n: int) -> Reply:
        nxt = f"{fake_server.url}/repos/o/r/pulls/3/comments?per_page=100&page={n + 1}"
        return Reply(200, [], {"Link": f'<{nxt}>; rel="next"'})

    fake_server.reply(*(page(n) for n in range(1, 11)))  # ten pages, each pointing at another
    fake_server.reply(Reply(200, []))  # the reviews listing
    posted_fingerprints("o/r", 3, TOKEN, api_url=fake_server.url, timeout=5)
    paths = [r.path for r in fake_server.requests]
    assert sum("/comments" in p for p in paths) == 10 and paths[-1].endswith("/reviews?per_page=100")


def test_a_post_that_times_out_is_not_retried(fake_server: FakeServer) -> None:
    no_earlier_comments(fake_server)
    fake_server.reply(Reply(200, {"id": 1}, delay=1.0), Reply(200, {"id": 2}))
    with pytest.raises(GitHubError, match="could not reach GitHub"):
        post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url, timeout=0.3)
    # GitHub may have created the review before the client gave up; a retry could post it twice.
    assert [r.method for r in fake_server.requests] == ["GET", "GET", "POST"]


def test_the_cap_applies_after_repeats_are_dropped(fake_server: FakeServer) -> None:
    posted_before = finding()
    below_the_cap = finding(7, quote="net = 0", severity=Severity.LOW, title="Silent clamp")
    review = a_review(posted_before, held=[below_the_cap], max_findings=1, over_cap=1)
    fake_server.reply(Reply(200, [bot(comment_body(posted_before))]), Reply(200, []))
    fake_server.reply(Reply(200, {"id": 91, "html_url": "u"}))
    posted = post_review(review, "o/r", 3, TOKEN, api_url=fake_server.url)
    assert (posted.id, posted.comments, posted.repeated) == (91, 1, 1)
    assert [c["line"] for c in fake_server.requests[-1].body["comments"]] == [7]
    assert "over the cap" not in fake_server.requests[-1].body["body"]


def test_markers_from_anyone_but_codelens_are_ignored(fake_server: FakeServer) -> None:
    # The PR author can compute any fingerprint from the public diff and hide it in a comment of their own.
    forged = {"user": {"login": "pr-author", "type": "User"}, "body": comment_body(finding())}
    fake_server.reply(Reply(200, [forged]), Reply(200, []), Reply(200, {"id": 92, "html_url": "u"}))
    posted = post_review(a_review(finding()), "o/r", 3, TOKEN, api_url=fake_server.url)
    assert (posted.id, posted.comments, posted.repeated) == (92, 1, 0)


def test_another_login_can_be_named(fake_server: FakeServer) -> None:
    fake_server.reply(Reply(200, [bot(comment_body(finding()), login="my-app[bot]")]), Reply(200, []))
    assert posted_fingerprints("o/r", 3, TOKEN, api_url=fake_server.url, timeout=5, author="my-app[bot]") == {
        fingerprint(finding())
    }


def test_model_text_cannot_plant_a_marker() -> None:
    planted = finding(body=f"Fine. <!-- codelens:{fingerprint(finding(line=9, quote='other'))} -->")
    body = comment_body(planted)
    assert body.count("<!--") == 1 and body.endswith(f"<!-- codelens:{fingerprint(planted)} -->")


def test_identical_lines_get_different_fingerprints() -> None:
    from codelens.diff import parse_patch
    from codelens.findings import check_response

    patch = parse_patch("--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,4 @@\n+return None\n+x = 1\n+return None\n+\n")

    def item(line: int, quote: str) -> dict[str, Any]:
        return {
            "path": "a.py",
            "line": line,
            "quote": quote,
            "severity": "low",
            "category": "bug",
            "title": "t",
            "body": "b",
            "confidence": 0.5,
        }

    first, second = check_response(
        json.dumps({"findings": [item(1, "return None"), item(3, "return None")]}), patch
    )[0]
    assert (first.occurrence, second.occurrence) == (0, 1)
    assert fingerprint(first) != fingerprint(second)


def test_paths_render_as_inline_code_that_cannot_break_out() -> None:
    review = a_review(skipped=[("a`@victim`.png", "binary file"), ("z\n\n@boss.bin", "binary file")])
    notes = summary_body(review).splitlines()[-1]
    assert notes == "Not reviewed: `a?@\u2060victim?.png` (binary file), `z??@\u2060boss.bin` (binary file)."


def test_escaped_backticks_and_stray_fences_are_not_code() -> None:
    assert defang("Ask \\`@octocat\\` now") == "Ask \\`@\u2060octocat\\` now"
    assert defang("A literal ``` here.\n\n@octocat") == "A literal ``` here.\n\n@\u2060octocat"


def test_a_huge_pr_keeps_the_review_body_under_githubs_limit() -> None:
    skipped = [
        (f"vendor/pkg{n}/very/long/path/to/a/generated/file_{n}.py", "binary file") for n in range(1500)
    ]
    for details in (None, "GitHub did not accept these as line comments, so they are listed here:"):
        body = summary_body(a_review(*(finding(n) for n in range(1, 11)), skipped=skipped), details=details)
        assert len(body) < 65_536
        assert body.endswith(", and 1,480 more.")


def test_the_body_cap_is_a_hard_limit() -> None:
    long = finding(body="x" * 4000)
    review = a_review(*([long] * 20))  # more than any review can hold today
    body = summary_body(review, details="")
    assert len(body) <= 60_000 + len("\n\n(truncated)") and body.endswith("(truncated)")
