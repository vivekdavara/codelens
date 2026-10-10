"""The eval harness: matching findings to labels, the three sources, and `codelens eval`."""

import json
from pathlib import Path
from typing import Any

import pytest

from codelens.cli import main
from codelens.evals import Label, Report, load_case, load_cases, posted, run, run_case, score
from codelens.findings import Category, Finding, Severity
from codelens.prompts import build_prompt
from codelens.providers import Completion, ProviderError, RecordedProvider, Request
from codelens.providers.recorded import HAND_WRITTEN, write_recording
from codelens.static import analyse, find_ruff

CASES = Path(__file__).parent.parent / "evals" / "cases"
RUFF = find_ruff()
pytestmark = pytest.mark.skipif(RUFF is None, reason="ruff is a dev dependency")


def finding(line: int, category: Category = Category.BUG, path: str = "m.py", **extra: Any) -> Finding:
    fields: dict[str, Any] = {"severity": Severity.MEDIUM, "title": "t", "body": "b", "confidence": 0.5}
    return Finding(path, line, category=category, **{**fields, **extra})


def label(line: int, category: Category = Category.BUG, also: tuple[int, ...] = ()) -> Label:
    return Label("m.py", line, category, "why", frozenset({line, *also}))


def test_findings_match_labels_one_to_one() -> None:
    labels = [label(3), label(7, Category.SECURITY, also=(8,))]
    found, correct = score([finding(3), finding(3), finding(8, Category.SECURITY), finding(9)], labels)
    # The second finding on line 3 has no bug left to find, and line 9 has none at all.
    assert correct == [True, False, True, False]
    assert (found.findings, found.correct, found.labels, found.found) == (4, 2, 2, 2)
    assert (found.precision, found.recall) == (0.5, 1.0)


def test_the_category_must_match_unless_asked_not_to() -> None:
    labels = [label(3, Category.SECURITY)]
    assert score([finding(3)], labels)[1] == [False]
    assert score([finding(3)], labels, categories=False)[1] == [True]
    assert score([finding(3, path="other.py")], labels, categories=False)[1] == [False]


def test_empty_scores_have_no_precision_or_recall() -> None:
    nothing, _ = score([], [])
    assert nothing.precision is None and nothing.recall is None


def test_a_source_is_scored_on_what_it_would_post() -> None:
    many = [finding(n, severity=Severity.LOW) for n in range(1, 13)] + [finding(5, severity=Severity.HIGH)]
    kept = posted(many)
    assert len(kept) == 10 and kept[0].line == 5 and kept[0].severity is Severity.HIGH
    assert [f.line for f in kept[1:]] == [1, 2, 3, 4, 6, 7, 8, 9, 10]  # line 5's low merged into its high


class Counting:
    """Answers from recordings and counts the calls, as a live provider would be billed."""

    name = "counting"

    def __init__(self, inner: RecordedProvider) -> None:
        self.inner = inner
        self.calls = 0

    def complete(self, request: Request) -> Completion:
        self.calls += 1
        return self.inner.complete(request)


def record(directory: Path, case_name: str, items: list[dict[str, Any]]) -> None:
    case = load_case(CASES / case_name)
    pre = analyse(case.patch, case.root, ruff=RUFF)
    request = build_prompt(case.patch, static=pre.findings).request
    write_recording(directory, request, Completion(json.dumps({"findings": items}), HAND_WRITTEN), "recorded")


PAGE = {
    "path": "api/orders.py",
    "line": 22,
    "quote": "    return ordered[start : start + size + 1]",
    "severity": "medium",
    "category": "bug",
    "title": "Each page has one order too many",
    "body": "The slice ends one past the page.",
    "confidence": 0.9,
}


def test_a_recorded_case_is_scored_three_ways_with_one_call(tmp_path: Path) -> None:
    # A hand-written answer: it exercises the harness and says nothing about any model.
    noise = {
        **PAGE,
        "line": 20,
        "quote": "    ordered = list_orders(orders)",
        "title": "Sorted",
        "confidence": 0.4,
    }
    record(tmp_path, "pagination", [PAGE, noise])
    provider = Counting(RecordedProvider(tmp_path))
    result = run_case(load_case(CASES / "pagination"), provider, ruff=RUFF)
    assert provider.calls == 1
    assert result.static == [] and result.model_name == HAND_WRITTEN
    assert result.model is not None and result.combined is not None
    assert [f.line for f in result.model] == [22, 20] and [f.line for f in result.combined] == [22, 20]
    model_score, correct = score(result.model, result.case.labels)
    assert correct == [True, False] and (model_score.found, model_score.labels) == (1, 2)


def test_combined_merges_the_static_finding_on_the_same_line(tmp_path: Path) -> None:
    discount = {
        **PAGE,
        "path": "shop/pricing.py",
        "line": 15,
        "quote": "        discounted = subtotal * (1 - rate)",
        "title": "The discount is never applied",
        "severity": "high",
    }
    record(tmp_path, "discount-codes", [discount])
    result = run_case(load_case(CASES / "discount-codes"), RecordedProvider(tmp_path), ruff=RUFF)
    assert [(f.line, f.source) for f in result.static] == [(15, "static")]
    assert result.combined is not None
    assert [(f.line, f.source, f.severity.value) for f in result.combined] == [(15, "llm", "high")]


def test_an_unrecorded_case_is_scored_on_static_only_and_other_errors_raise(tmp_path: Path) -> None:
    case = load_case(CASES / "pagination")
    result = run_case(case, RecordedProvider(tmp_path), ruff=RUFF)
    assert result.model is None and result.combined is None

    class Broken:
        name = "broken"

        def complete(self, request: Request) -> Completion:
            raise ProviderError("vendor down")

    with pytest.raises(ProviderError, match="vendor down"):
        run_case(case, Broken(), ruff=RUFF)
    assert run_case(case, None, ruff=RUFF).model is None


def test_the_static_pre_pass_on_the_eval_set() -> None:
    # Pinned, so a rule change that loses a bug or adds a false positive fails CI. See README "Results".
    report = run(load_cases(CASES), None, ruff=RUFF)
    static = report.total("static")
    assert (static.findings, static.correct, static.labels, static.found) == (14, 14, 23, 14)
    assert report.total("static", categories=False).found == 14
    assert report.scored("model") == [] and report.total("model").labels == 0


def test_the_report(tmp_path: Path) -> None:
    record(tmp_path, "pagination", [PAGE])
    cases = [load_case(CASES / name) for name in ("pagination", "shipping-email")]
    report = run(cases, RecordedProvider(tmp_path), ruff=RUFF)
    text = report.markdown()
    assert "| static pre-pass | 2 of 2 | 2 | 2 | 100.0% | 2 of 4 | 50.0% | 50.0% |" in text
    assert "| model alone | 1 of 2 | 1 | 1 | 100.0% | 1 of 2 | 50.0% | 50.0% |" in text
    assert "| `pagination` | 2 | 0 / 0 | 1 / 0 |" in text
    assert "| `shipping-email` | 2 | 2 / 0 | not recorded |" in text
    data = report.to_dict()
    assert data["models"] == [HAND_WRITTEN] and data["totals"]["model"]["cases"] == 1
    (pagination, shipping) = data["cases"]
    assert pagination["model"][0]["correct"] is True and shipping["model"] is None
    assert [lb["line"] for lb in shipping["labels"]] == [16, 17]
    assert Report([]).total("combined").findings == 0


def cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["eval", "--cases", str(CASES), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_codelens_eval(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, err = cli(capsys, "--recordings", str(tmp_path))
    assert code == 0 and out.startswith("| Source |")
    assert "14 cases without a recorded answer: scored on the static pre-pass only" in err
    code, out, err = cli(capsys, "--static-only", "--json")
    assert code == 0 and json.loads(out)["totals"]["static"]["found"] == 14 and err == ""
    record(tmp_path, "pagination", [PAGE])
    code, _, err = cli(capsys, "--recordings", str(tmp_path))
    assert "13 cases without a recorded answer" in err and "model answers from: hand-written" in err


def test_codelens_eval_errors(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, _, err = cli(capsys, "--record")
    assert code == 2 and "--record needs a live provider" in err
    code, _, err = main(["eval", "--cases", str(tmp_path / "none")]), *capsys.readouterr()
    assert code == 1 and "eval failed: no eval cases" in err
    code, _, err = cli(capsys, "--provider", "anthropic")
    assert code == 1 and "eval failed:" in err and "ANTHROPIC_API_KEY" in err
