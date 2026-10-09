import json
from typing import Any

import pytest

from codelens.diff import parse_patch
from codelens.findings import FindingsFormatError, Severity
from codelens.providers import Completion, ProviderError, Request, Usage
from codelens.review import review

DIFF = """\
diff --git a/svc/pay.py b/svc/pay.py
--- a/svc/pay.py
+++ b/svc/pay.py
@@ -1,2 +1,13 @@
 def refund(amount, fee):
-    return amount - fee
+    net = amount - fee
+    if net < 0:
+        net = 0
+    log(net)
+    return net
+
+
+def charge(card, amount):
+    token = card.token
+    send(token, amount)
+    audit(token)
+    return True
diff --git a/README.md b/README.md
deleted file mode 100644
--- a/README.md
+++ /dev/null
@@ -1 +0,0 @@
-# old
"""
PATCH = parse_patch(DIFF)


class Scripted:
    """A provider that returns a fixed answer and remembers what it was asked."""

    name = "scripted"

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[Request] = []

    def complete(self, request: Request) -> Completion:
        self.requests.append(request)
        return Completion(self.text, "scripted-1", Usage(500, 50))


def item(line: int, severity: str = "medium", confidence: float = 0.5, **extra: Any) -> dict[str, Any]:
    quote = {
        1: "def refund(amount, fee):",
        2: "net = amount - fee",
        3: "if net < 0:",
        4: "net = 0",
        5: "log(net)",
        6: "return net",
        9: "def charge(card, amount):",
        10: "token = card.token",
        11: "send(token, amount)",
        12: "audit(token)",
        13: "return True",
    }.get(line, "")
    return {
        "path": "svc/pay.py",
        "line": line,
        "quote": quote,
        "severity": severity,
        "category": "bug",
        "title": f"Problem on line {line}",
        "body": "Explanation.",
        "confidence": confidence,
        **extra,
    }


def answer(*items: dict[str, Any]) -> str:
    return json.dumps({"findings": list(items)})


def test_review_ranks_findings_and_reports_usage() -> None:
    provider = Scripted(answer(item(6, "low", 0.9), item(12, "high", 0.6), item(4, "high", 0.9)))
    result = review(PATCH, provider)
    assert [(f.line, f.severity) for f in result.findings] == [
        (4, Severity.HIGH),
        (12, Severity.HIGH),
        (6, Severity.LOW),
    ]
    assert (result.provider, result.model, result.usage) == ("scripted", "scripted-1", Usage(500, 50))
    assert result.reviewed == ["svc/pay.py"]
    assert result.skipped == [("README.md", "deleted file")]
    (request,) = provider.requests
    assert "File: svc/pay.py (modified)" in request.prompt and "README.md" not in request.prompt


def test_bad_findings_are_dropped_and_counted() -> None:
    provider = Scripted(answer(item(4), item(5, quote="if net < 0:"), item(99), item(4, confidence=7)))
    result = review(PATCH, provider)
    assert [f.line for f in result.findings] == [4]
    assert result.rejection_counts() == {"invalid": 1, "misquoted": 1, "unanchored": 1}
    assert [r.index for r in result.rejections] == [1, 2, 3]


def test_findings_on_files_the_model_was_not_shown_are_rejected() -> None:
    deleted = item(1, path="README.md", quote="# old")
    result = review(PATCH, Scripted(answer(deleted)))
    assert result.findings == []
    (rejection,) = result.rejections
    assert rejection.detail == "README.md is not in the diff"


def test_the_cap_keeps_the_worst_findings() -> None:
    items = [item(line, "low", 0.5) for line in (1, 3, 4, 5, 6, 11, 12, 13)] + [item(12, "critical", 0.4)]
    result = review(PATCH, Scripted(answer(*items)), max_findings=3)
    assert [(f.line, f.severity.value) for f in result.findings] == [(12, "critical"), (1, "low"), (3, "low")]
    assert result.over_cap == 6
    # The rest are held, in rank order, for posting to reach into when some of the top three were posted.
    assert [f.line for f in result.held] == [4, 5, 6, 11, 12, 13] and result.max_findings == 3


def test_no_reviewable_files_means_no_provider_call() -> None:
    only_deletion = parse_patch(DIFF[DIFF.index("diff --git a/README.md") :])
    provider = Scripted(answer())
    result = review(only_deletion, provider)
    assert provider.requests == [] and result.findings == [] and result.model == ""
    assert result.skipped == [("README.md", "deleted file")]


def test_an_unusable_answer_raises() -> None:
    with pytest.raises(FindingsFormatError):
        review(PATCH, Scripted("I found no problems!"))


def test_provider_errors_propagate() -> None:
    class Down:
        name = "down"

        def complete(self, request: Request) -> Completion:
            raise ProviderError("vendor unavailable")

    with pytest.raises(ProviderError, match="vendor unavailable"):
        review(PATCH, Down())


@pytest.mark.parametrize(("cap", "budget"), [(0, 1000), (-1, 1000), (5, 0)])
def test_limits_must_be_positive(cap: int, budget: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        review(PATCH, Scripted(answer()), max_findings=cap, max_prompt_chars=budget)
