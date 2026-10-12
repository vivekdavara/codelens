import json
from pathlib import Path
from typing import Any

import pytest

from codelens.diff import parse_patch
from codelens.findings import Category, Finding, FindingsFormatError, Severity
from codelens.prompts import build_prompt
from codelens.providers import (
    Completion,
    ProviderError,
    RecordedProvider,
    RecordingMissing,
    Request,
    Usage,
)
from codelens.providers.recorded import write_recording
from codelens.review import dedupe, review
from codelens.static import StaticResult

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
    critical = item(10, "critical", 0.4)
    items = [item(line, "low", 0.5) for line in (1, 3, 4, 5, 6, 11, 12, 13)] + [critical]
    result = review(PATCH, Scripted(answer(*items)), max_findings=3)
    assert [(f.line, f.severity.value) for f in result.findings] == [(10, "critical"), (1, "low"), (3, "low")]
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


def static_finding(line: int, severity: str = "medium", confidence: float = 0.6, **extra: Any) -> Finding:
    fields: dict[str, Any] = {
        "path": "svc/pay.py",
        "line": line,
        "severity": Severity(severity),
        "category": Category.BUG,
        "title": f"Static problem on line {line}",
        "body": "Found by ruff rule `F841`.",
        "confidence": confidence,
        "quote": "    " + item(line)["quote"],
        "source": "static",
        "rule": "F841",
    }
    return Finding(**{**fields, **extra})


def test_dedupe_keeps_what_the_ranking_puts_first() -> None:
    low, critical = static_finding(4, "low", 0.9), static_finding(4, "critical", 0.4)
    assert dedupe([low, critical]) == ([critical], 1)  # not the more confident low
    sure, unsure = static_finding(4, confidence=0.9), static_finding(4, confidence=0.5)
    assert dedupe([unsure, sure]) == ([sure], 1)
    first, second = static_finding(4), static_finding(4, source="llm", rule="")
    assert dedupe([first, second]) == ([first], 1)  # a full tie goes to the first: static, by convention


def test_dedupe_keeps_one_finding_per_line_whatever_the_category() -> None:
    # Posting identifies a finding by its line; two on one line would share a fingerprint.
    bug, security = static_finding(4), static_finding(4, "high", category=Category.SECURITY)
    other_line, other_file = static_finding(5), static_finding(4, path="svc/other.py")
    assert dedupe([bug, security, other_line, other_file]) == ([security, other_line, other_file], 1)


def test_static_findings_are_listed_in_the_prompt_and_merged_with_the_models() -> None:
    pre = StaticResult(findings=[static_finding(10, "medium", 0.6), static_finding(4, "low", 0.6)])
    provider = Scripted(answer(item(10, "high", 0.9), item(6, "low", 0.5)))
    result = review(PATCH, provider, static=pre)
    (request,) = provider.requests
    assert request.prompt.endswith(
        "</diff>\n\n<static_analysis>\n"
        + request.prompt.split("<static_analysis>\n", 1)[1].split("\n", 1)[0]
        + "\n- svc/pay.py:4 [F841] Static problem on line 4\n"
        "- svc/pay.py:10 [F841] Static problem on line 10\n</static_analysis>\n"
    )
    # Line 10: the model's high replaces ruff's medium; line 4 is static only, line 6 the model's only.
    assert [(f.line, f.source, f.severity.value) for f in result.findings] == [
        (10, "llm", "high"),
        (4, "static", "low"),
        (6, "llm", "low"),
    ]
    assert result.duplicates == 1 and result.static is pre


def test_without_static_findings_the_prompt_is_unchanged() -> None:
    provider = Scripted(answer())
    review(PATCH, provider, static=StaticResult())
    assert provider.requests[0].key() == build_prompt(PATCH).request.key()


def test_static_findings_survive_when_no_file_fits_the_prompt() -> None:
    provider = Scripted(answer())
    result = review(PATCH, provider, static=StaticResult(findings=[static_finding(4)]), max_prompt_chars=10)
    assert provider.requests == [] and result.reviewed == []
    assert [(f.line, f.source) for f in result.findings] == [(4, "static")]


def test_static_findings_count_against_the_cap() -> None:
    pre = StaticResult(findings=[static_finding(line, "critical") for line in (2, 3, 4)])
    result = review(PATCH, Scripted(answer(item(12, "low"))), static=pre, max_findings=2)
    assert [f.line for f in result.findings] == [2, 3] and [f.line for f in result.held] == [4, 12]


def test_min_severity_leaves_out_less_severe_findings_without_changing_the_prompt() -> None:
    pre = StaticResult(findings=[static_finding(10, "low")])
    items = answer(item(4, "high", 0.9), item(5, "medium", 0.9), item(6, "low", 0.9))
    everything, high_only = Scripted(items), Scripted(items)
    full = review(PATCH, everything, static=pre)
    kept = review(PATCH, high_only, static=pre, min_severity=Severity.HIGH)
    assert [f.line for f in full.findings] == [4, 5, 6, 10] and full.below == 0
    assert [f.line for f in kept.findings] == [4] and kept.below == 3
    assert kept.min_severity is Severity.HIGH and kept.held == [] and kept.over_cap == 0
    # The model is never told the threshold: one recording serves every setting.
    assert everything.requests[0].key() == high_only.requests[0].key()


def test_min_severity_applies_after_the_merge() -> None:
    # The model's low and ruff's high are one problem on line 10: the high one survives a medium threshold.
    pre = StaticResult(findings=[static_finding(10, "high")])
    result = review(PATCH, Scripted(answer(item(10, "low", 0.9))), static=pre, min_severity=Severity.MEDIUM)
    assert [(f.line, f.source) for f in result.findings] == [(10, "static")]
    assert result.duplicates == 1 and result.below == 0


def test_a_missing_recording_is_an_error_by_default(tmp_path: Path) -> None:
    with pytest.raises(RecordingMissing):
        review(PATCH, RecordedProvider(tmp_path), static=StaticResult(findings=[static_finding(4)]))


def test_a_missing_recording_can_fall_back_to_static_analysis_alone(tmp_path: Path) -> None:
    pre = StaticResult(findings=[static_finding(4, "high"), static_finding(10, "low")])
    result = review(PATCH, RecordedProvider(tmp_path), static=pre, static_if_unrecorded=True)
    assert [(f.line, f.source) for f in result.findings] == [(4, "static"), (10, "static")]
    # No model saw anything, and the review says which recording would have answered.
    assert result.reviewed == [] and result.model == "" and result.usage == Usage()
    assert result.unrecorded == build_prompt(PATCH, static=pre.findings).request.key()
    assert result.skipped == [("README.md", "deleted file")] and result.rejections == []


def test_a_recording_that_exists_is_replayed_even_with_the_fallback_on(tmp_path: Path) -> None:
    request = build_prompt(PATCH).request
    write_recording(tmp_path, request, Completion(answer(item(4)), "hand-written", Usage()), "recorded")
    result = review(PATCH, RecordedProvider(tmp_path), static_if_unrecorded=True)
    assert [f.line for f in result.findings] == [4] and result.unrecorded is None
    assert result.reviewed == ["svc/pay.py"] and result.model == "hand-written"


def test_only_a_missing_recording_falls_back(tmp_path: Path) -> None:
    # An edited recording, or a live provider's failure, is still an error: the review was asked for a
    # model's answer, and silently posting less would hide that.
    request = build_prompt(PATCH).request
    path = write_recording(tmp_path, request, Completion(answer(), "hand-written", Usage()), "recorded")
    path.write_text(path.read_text().replace('"findings', '"findings '))
    with pytest.raises(ProviderError, match="does not match its key"):
        review(PATCH, RecordedProvider(tmp_path), static_if_unrecorded=True)

    class Down:
        name = "anthropic"

        def complete(self, request: Request) -> Completion:
            raise ProviderError("HTTP 529: overloaded")

    with pytest.raises(ProviderError, match="overloaded"):
        review(PATCH, Down(), static_if_unrecorded=True)


def test_nothing_reviewable_makes_no_call_and_needs_no_recording(tmp_path: Path) -> None:
    deleted = parse_patch("diff --git a/README.md" + DIFF.split("diff --git a/README.md", 1)[1])
    result = review(deleted, RecordedProvider(tmp_path), static_if_unrecorded=True)
    assert result.unrecorded is None and result.findings == [] and result.reviewed == []


RECORDING_FILE = """\
diff --git a/.codelens/recordings/0a1b.json b/.codelens/recordings/0a1b.json
new file mode 100644
--- /dev/null
+++ b/.codelens/recordings/0a1b.json
@@ -0,0 +1,3 @@
+{
+  "response": "{\\"findings\\": []}"
+}
"""


def test_files_under_the_recordings_directory_are_never_reviewed() -> None:
    with_recording = parse_patch(DIFF + RECORDING_FILE)
    provider = Scripted(answer(item(4)))
    result = review(with_recording, provider, recordings=".codelens/recordings")
    # The same prompt as without the recording, so a recording committed to the PR still answers it.
    assert provider.requests[0].key() == build_prompt(PATCH).request.key()
    assert result.skipped == [
        (".codelens/recordings/0a1b.json", "a CodeLens recording"),
        ("README.md", "deleted file"),
    ]
    assert [f.line for f in result.findings] == [4]
    # Only that directory: a sibling whose name starts the same way is reviewed.
    shown = review(with_recording, Scripted(answer()), recordings=".codelens/rec")
    assert ".codelens/recordings/0a1b.json" in shown.reviewed


def test_static_findings_on_recordings_are_dropped_too() -> None:
    pre = StaticResult(findings=[static_finding(4), static_finding(2, path=".codelens/recordings/x.py")])
    result = review(PATCH, Scripted(answer()), static=pre, recordings=".codelens/recordings")
    assert [(f.path, f.line) for f in result.findings] == [("svc/pay.py", 4)]
