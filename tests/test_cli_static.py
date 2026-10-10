"""The static pre-pass through the CLI: `codelens static`, and `review`/`prompt` with --source-root."""

import json
from pathlib import Path

import pytest

from codelens.cli import main
from codelens.diff import parse_patch
from codelens.findings import Category, Finding, Severity
from codelens.github import summary_body
from codelens.prompts import build_prompt
from codelens.providers import Completion
from codelens.providers.recorded import HAND_WRITTEN, write_recording
from codelens.review import Review
from codelens.static import StaticResult, analyse, find_ruff

DIFF = """\
diff --git a/svc/pay.py b/svc/pay.py
--- a/svc/pay.py
+++ b/svc/pay.py
@@ -1,2 +1,5 @@
 def refund(amount, fee):
-    return amount - fee
+    net = amount - fee
+    rounded = round(net, 2)
+    if net < 0:
+        net = 0
"""
NEW = "def refund(amount, fee):\n    net = amount - fee\n    rounded = round(net, 2)\n"
NEW += "    if net < 0:\n        net = 0\n"
FINDING = {
    "path": "svc/pay.py",
    "line": 3,
    "quote": "    rounded = round(net, 2)",
    "severity": "high",
    "category": "bug",
    "title": "The rounded amount is never returned",
    "body": "`rounded` is computed and dropped, and the function returns nothing at all.",
    "confidence": 0.9,
}

pytestmark = pytest.mark.skipif(find_ruff() is None, reason="ruff is a dev dependency")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "pay.py").write_text(NEW)
    (tmp_path / "pr.diff").write_text(DIFF)
    return root


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_static_prints_findings_on_added_lines(capsys: pytest.CaptureFixture[str], checkout: Path) -> None:
    code, out, _ = run(capsys, "static", str(checkout.parent / "pr.diff"), "--source-root", str(checkout))
    assert code == 0
    lines = out.splitlines()
    assert (
        lines[0] == "svc/pay.py:3  medium  bug  [F841] Local variable `rounded` is assigned to but never used"
    )
    assert lines[1].startswith("static analysis: ruff ") and lines[1].endswith(
        " + CodeLens rules on 1 Python file, 1 finding on added lines"
    )


def test_static_json(capsys: pytest.CaptureFixture[str], checkout: Path) -> None:
    code, out, _ = run(
        capsys, "static", str(checkout.parent / "pr.diff"), "--source-root", str(checkout), "--json"
    )
    data = json.loads(out)
    assert code == 0 and data["analysed"] == ["svc/pay.py"] and data["skipped"] == []
    assert [(f["line"], f["rule"], f["source"]) for f in data["findings"]] == [(3, "F841", "static")]


def test_static_with_nothing_to_check(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "docs.diff").write_text(
        "diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -1 +1 @@\n-a\n+b\n"
    )
    code, out, _ = run(capsys, "static", str(tmp_path / "docs.diff"))
    assert code == 0 and out == "static analysis: no Python file in the diff adds lines\n"
    (tmp_path / "bad.diff").write_text("@@ nonsense\n")
    assert run(capsys, "static", str(tmp_path / "bad.diff"))[0] == 1


def test_review_merges_static_and_model_findings(
    capsys: pytest.CaptureFixture[str], checkout: Path, tmp_path: Path
) -> None:
    patch = parse_patch(DIFF)
    pre = analyse(patch, checkout, ruff=find_ruff())
    request = build_prompt(patch, static=pre.findings).request  # the key covers the static block
    answer = json.dumps({"findings": [FINDING]})
    write_recording(tmp_path / "rec", request, Completion(answer, HAND_WRITTEN), "recorded")
    args = ["review", str(tmp_path / "pr.diff"), "--recordings", str(tmp_path / "rec")]
    code, out, _ = run(capsys, *args, "--source-root", str(checkout))
    assert code == 0
    lines = out.splitlines()
    # The model's high and ruff's medium are one problem on line 3: the high one stays.
    assert lines[0] == "svc/pay.py:3  high  bug  The rounded amount is never returned  (confidence 0.90)"
    assert "merged 1 duplicate (same line and category)" in lines
    code, out, _ = run(capsys, *args, "--source-root", str(checkout), "--json")
    data = json.loads(out)
    assert data["duplicates"] == 1 and data["static"]["analysed"] == ["svc/pay.py"]
    assert data["static"]["findings"][0]["rule"] == "F841"
    # Without the pre-pass the prompt is a different one, which was never recorded: a loud miss.
    code, _, err = run(capsys, *args, "--source-root", str(checkout), "--no-static")
    assert code == 1 and "no recorded response" in err


def test_prompt_shows_the_static_block(capsys: pytest.CaptureFixture[str], checkout: Path) -> None:
    diff = str(checkout.parent / "pr.diff")
    code, out, err = run(capsys, "prompt", diff, "--source-root", str(checkout))
    assert code == 0
    assert "<static_analysis>" in out and "- svc/pay.py:3 [F841] Local variable `rounded`" in out
    assert "static analysis: ruff" in err
    _, without, _ = run(capsys, "prompt", diff, "--source-root", str(checkout), "--no-static")
    assert "<static_analysis>" not in without
    key = build_prompt(parse_patch(DIFF)).request.key()
    assert f"=== key {key} ===" in without


def test_the_summary_reports_the_pre_pass() -> None:
    static = StaticResult(
        analysed=["svc/pay.py"],
        skipped=[("svc/old.py", "not UTF-8")],
        tool="ruff 0.16.10",
        notes=["ruff failed, so its rules were not checked: boom"],
    )
    review = Review([], reviewed=["svc/pay.py"], model="m", static=static, duplicates=2)
    notes = summary_body(review).split("\n\n")[-1].splitlines()
    assert notes == [
        "Merged 2 duplicate findings (same line and category).",
        "Static analysis: ruff failed, so its rules were not checked: boom.",
        "Static analysis (ruff 0.16.10 and CodeLens rules) checked 1 Python file and found 0 problems on "
        "added lines.",
        "Not checked by static analysis: `svc/old.py` (not UTF-8).",
    ]
    no_ruff = Review([], reviewed=["svc/pay.py"], static=StaticResult(analysed=["svc/pay.py"]))
    assert "(CodeLens rules) checked 1 Python file" in summary_body(no_ruff)


def test_the_summary_of_a_static_only_review() -> None:
    finding = Finding("svc/pay.py", 3, Severity.MEDIUM, Category.BUG, "t", "b", 0.6, source="static")
    headline = summary_body(Review([finding], static=StaticResult(analysed=["svc/pay.py"]))).splitlines()[2]
    assert headline == "1 finding from static analysis; no file in this diff could be shown to the model."


def test_a_long_list_of_unchecked_files_is_cut_short() -> None:
    body = summary_body(Review([], static=StaticResult(skipped=[("x.py", "symlink")] * 25)))
    assert "Not checked by static analysis: " in body and ", and 5 more." in body


def test_review_json_without_the_pre_pass(capsys: pytest.CaptureFixture[str], checkout: Path) -> None:
    patch = parse_patch(DIFF)
    request = build_prompt(patch).request
    recordings = checkout.parent / "rec"
    write_recording(recordings, request, Completion('{"findings": []}', HAND_WRITTEN), "recorded")
    args = [
        "review",
        str(checkout.parent / "pr.diff"),
        "--recordings",
        str(recordings),
        "--no-static",
        "--json",
    ]
    code, out, _ = run(capsys, *args)
    assert code == 0 and json.loads(out)["static"] is None


def test_min_severity_on_the_command_line(capsys: pytest.CaptureFixture[str], checkout: Path) -> None:
    patch = parse_patch(DIFF)
    pre = analyse(patch, checkout, ruff=find_ruff())
    request = build_prompt(patch, static=pre.findings).request
    recordings = checkout.parent / "rec"
    write_recording(recordings, request, Completion(json.dumps({"findings": []}), HAND_WRITTEN), "recorded")
    args = ["review", str(checkout.parent / "pr.diff"), "--recordings", str(recordings), "--source-root"]
    code, out, _ = run(capsys, *args, str(checkout), "--min-severity", "high")
    assert code == 0 and "below high severity: 1 finding left out" in out.splitlines()
    code, out, _ = run(capsys, *args, str(checkout), "--min-severity", "high", "--json")
    data = json.loads(out)
    assert data["findings"] == [] and data["below_min_severity"] == 1
    with pytest.raises(SystemExit):
        main([*args, str(checkout), "--min-severity", "nit"])


def test_the_summary_names_the_threshold() -> None:
    review = Review([], reviewed=["svc/pay.py"], min_severity=Severity.HIGH, below=2)
    assert "Left out 2 findings less severe than high." in summary_body(review)
