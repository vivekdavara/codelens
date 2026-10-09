import json
from typing import Any

import pytest

from codelens.diff import parse_patch
from codelens.findings import (
    FINDINGS_SCHEMA,
    MAX_BODY,
    Category,
    Finding,
    FindingsFormatError,
    Severity,
    anchor_finding,
    check_response,
    parse_findings,
)

DIFF = """\
diff --git a/svc/pay.py b/svc/pay.py
--- a/svc/pay.py
+++ b/svc/pay.py
@@ -3,6 +3,8 @@ def refund(amount, fee):
     if amount <= 0:
         raise ValueError("amount")
-    net = amount - fee
+    net = amount - fee
+    if net < 0:
+        net = 0
     log(net)
     return net

diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ
"""
PATCH = parse_patch(DIFF)


def item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "path": "svc/pay.py",
        "line": 6,
        "quote": "    if net < 0:",
        "severity": "high",
        "category": "bug",
        "title": "Clamping hides the error",
        "body": "A refund smaller than the fee is silently turned into 0.",
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


def response(*items: Any) -> str:
    return json.dumps({"findings": list(items)})


def test_valid_item_becomes_a_finding() -> None:
    (finding,), rejections = parse_findings(response(item(confidence=1)))
    assert rejections == []
    assert finding == Finding(
        path="svc/pay.py",
        line=6,
        severity=Severity.HIGH,
        category=Category.BUG,
        title="Clamping hides the error",
        body="A refund smaller than the fee is silently turned into 0.",
        confidence=1.0,
        quote="    if net < 0:",
    )
    assert finding.to_dict()["side"] == "RIGHT" and finding.to_dict()["source"] == "llm"


def test_empty_findings_is_a_valid_answer() -> None:
    assert parse_findings('{"findings": []}') == ([], [])


def test_one_markdown_fence_is_accepted() -> None:
    findings, _ = parse_findings("```json\n" + response(item()) + "\n```")
    assert len(findings) == 1


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "Here you go: " + response(item()),
        "[]",
        '{"issues": []}',
        '{"findings": {}}',
        '{"findings": [], "x": NaN}',
    ],
)
def test_unusable_responses_raise(text: str) -> None:
    with pytest.raises(FindingsFormatError):
        parse_findings(text)


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("a string", "expected an object"),
        ({k: v for k, v in item().items() if k != "quote"}, "missing quote"),
        (item(extra=1), "unexpected extra"),
        (item(path=7), "path must be a string"),
        (item(line="6"), "line must be a positive integer"),
        (item(line=True), "line must be a positive integer"),
        (item(line=0), "line must be a positive integer"),
        (item(line=6.0), "line must be a positive integer"),
        (item(confidence=1.5), "confidence must be a number from 0 to 1"),
        (item(confidence=-0.1), "confidence must be a number from 0 to 1"),
        (item(confidence=True), "confidence must be a number from 0 to 1"),
        (item(severity="blocker"), "'blocker' is not a valid Severity"),
        (item(category="style"), "'style' is not a valid Category"),
        (item(title="   "), "must not be empty"),
        (item(path=""), "must not be empty"),
        (item(body="x" * (MAX_BODY + 1)), "body to 4000"),
    ],
)
def test_invalid_items_are_rejected_with_a_reason(bad: Any, message: str) -> None:
    findings, (rejection,) = parse_findings(response(bad))
    assert findings == []
    assert rejection.kind == "invalid" and message in rejection.detail


def test_a_bad_item_rejects_only_itself() -> None:
    findings, rejections = parse_findings(response(item(), item(line=-1), item(line=7, quote="net = 0")))
    assert [f.line for f in findings] == [6, 7]
    assert [r.index for r in rejections] == [1]


def test_titles_and_bodies_are_stripped() -> None:
    (finding,), _ = parse_findings(response(item(title="  Title \n", body="\nBody  ")))
    assert (finding.title, finding.body) == ("Title", "Body")


def test_titles_are_kept_on_one_line_but_bodies_keep_their_markdown() -> None:
    body = "First paragraph.\n\n```python\nx = 1\n```"
    (finding,), _ = parse_findings(response(item(title="Refund\n  can go\tnegative", body=body)))
    assert (finding.title, finding.body) == ("Refund can go negative", body)


def finding(**overrides: Any) -> Finding:
    (f,), _ = parse_findings(response(item(**overrides)))
    return f


def test_anchors_on_added_and_context_lines() -> None:
    assert anchor_finding(finding(), PATCH) is None
    assert anchor_finding(finding(line=3, quote="if amount <= 0:"), PATCH) is None


@pytest.mark.parametrize(
    ("overrides", "kind", "detail"),
    [
        ({"path": "svc/other.py"}, "unanchored", "svc/other.py is not in the diff"),
        ({"path": "logo.png"}, "unanchored", "binary file"),
        ({"line": 40}, "unanchored", "svc/pay.py:40 is not a line in the diff"),
        ({"line": 7}, "misquoted", "svc/pay.py:7 is 'net = 0', not 'if net < 0:'"),
        ({"quote": "if net <= 0:"}, "misquoted", "not 'if net <= 0:'"),
    ],
)
def test_findings_off_the_diff_are_rejected(overrides: dict[str, Any], kind: str, detail: str) -> None:
    rejection = anchor_finding(finding(**overrides), PATCH, index=4)
    assert rejection is not None
    assert (rejection.index, rejection.kind) == (4, kind)
    assert detail in rejection.detail


def test_quote_check_ignores_whitespace_and_accepts_part_of_the_line() -> None:
    assert anchor_finding(finding(quote="if   net  <  0:"), PATCH) is None
    assert anchor_finding(finding(quote="net < 0"), PATCH) is None


def test_blank_lines_need_a_blank_quote() -> None:
    assert anchor_finding(finding(line=10, quote=""), PATCH) is None
    rejection = anchor_finding(finding(line=10, quote="return net"), PATCH)
    assert rejection is not None and rejection.kind == "misquoted"
    # An empty quote is a substring of everything; it must not anchor a non-blank line.
    rejection = anchor_finding(finding(quote=" "), PATCH)
    assert rejection is not None and rejection.kind == "misquoted"


def test_check_response_validates_then_anchors_and_keeps_response_indices() -> None:
    text = response(item(line=7), item(line=0), item(), item(path="nope.py"))
    findings, rejections = check_response(text, PATCH)
    assert [f.line for f in findings] == [6]
    assert [(r.index, r.kind) for r in rejections] == [(0, "misquoted"), (1, "invalid"), (3, "unanchored")]


def test_severity_rank_orders_worst_first() -> None:
    assert sorted(Severity, key=lambda s: s.rank) == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
    ]


def _objects(schema: Any) -> list[dict[str, Any]]:
    if isinstance(schema, dict):
        found = [schema] if schema.get("type") == "object" else []
        return found + [o for value in schema.values() for o in _objects(value)]
    if isinstance(schema, list):
        return [o for value in schema for o in _objects(value)]
    return []


def test_schema_stays_inside_the_structured_output_subset() -> None:
    # Every object closed and fully required (both vendors' strict modes need this), and none of the keywords
    # structured outputs reject: those limits are enforced in parse_findings instead.
    objects = _objects(FINDINGS_SCHEMA)
    assert len(objects) == 2
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    text = json.dumps(FINDINGS_SCHEMA)
    for keyword in ("minimum", "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems"):
        assert f'"{keyword}"' not in text


def test_kept_findings_carry_the_full_text_of_their_line() -> None:
    (kept,), _ = check_response(response(item(quote="net < 0")), PATCH)
    assert kept.quote == "    if net < 0:"
