"""The eval set: small pull requests with seeded bugs, and the labels that say where the bugs are.

A case is a directory::

    <name>/before/...   the files the PR changes, as they were (absent for a PR that only adds files)
    <name>/after/...    the same files after the PR
    <name>/pr.diff      `git diff -M` of the two (scripts/build_evals.py)
    <name>/labels.json  {"summary": "...", "bugs": [{"path", "quote", "category", "why", "also"?}]}

A label names its line by quoting it, so labels can't drift when a case is edited: the quote must appear on
exactly one line of ``after/<path>``, and that line must be one the diff shows (usually an added line, but a
change can make an unchanged line wrong). ``also`` lists other lines a reviewer could reasonably cite for the
same bug (the line that computes a value the bug then misuses, say). See evals/README.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codelens.diff import PatchSet, Side, decode_diff, parse_patch
from codelens.findings import Category, Finding, check_response
from codelens.prompts import MAX_FINDINGS, build_prompt
from codelens.providers import Completion, Provider, RecordingMissing, Request
from codelens.review import dedupe, rank, review
from codelens.static import analyse

__all__ = [
    "Case",
    "CaseResult",
    "EvalError",
    "Label",
    "Report",
    "Score",
    "load_case",
    "load_cases",
    "run",
    "run_case",
    "score",
]


class EvalError(ValueError):
    """A case directory or its labels are malformed."""


@dataclass(frozen=True)
class Label:
    path: str
    line: int
    """The line the quote names (new-file numbering)."""
    category: Category
    why: str
    lines: frozenset[int] = field(default_factory=frozenset)
    """Every line a finding may cite to find this bug: ``line`` and the ``also`` lines."""


@dataclass
class Case:
    name: str
    summary: str
    directory: Path
    patch: PatchSet
    labels: list[Label]

    @property
    def root(self) -> Path:
        """The checkout of the PR's new version that the static pre-pass reads."""
        return self.directory / "after"


def _resolve(case: str, after: Path, path: str, quote: str) -> int:
    try:
        lines = (after / path).read_text(encoding="utf-8").split("\n")
    except OSError as exc:
        raise EvalError(f"{case}: label path {path!r}: {exc.strerror or exc}") from None
    matches = [n for n, text in enumerate(lines, 1) if quote.strip() and quote.strip() in text]
    if len(matches) != 1:
        raise EvalError(f"{case}: {quote!r} is on {len(matches)} lines of {path}, not exactly one")
    return matches[0]


def _label(case: str, after: Path, patch: PatchSet, item: Any) -> Label:
    if not isinstance(item, dict) or not {"path", "quote", "category", "why"} <= item.keys():
        raise EvalError(f"{case}: a label needs path, quote, category and why: {item!r}")
    path, also = item["path"], item.get("also", [])
    file = patch.get(path)
    if file is None:
        raise EvalError(f"{case}: label path {path!r} is not in pr.diff")
    try:
        category = Category(item["category"])
    except ValueError:
        raise EvalError(f"{case}: unknown category {item['category']!r}") from None
    line = _resolve(case, after, path, item["quote"])
    shown = file.commentable_lines(Side.RIGHT)
    if line not in shown:  # a bug a reviewer can't comment on can't be found
        raise EvalError(f"{case}: {path}:{line} is not a line the diff shows")
    lines = {line} | {_resolve(case, after, path, quote) for quote in also}
    if not lines <= shown:
        raise EvalError(f"{case}: an `also` line of {path}:{line} is not in the diff")
    return Label(path, line, category, str(item["why"]), frozenset(lines))


def load_case(directory: Path) -> Case:
    """Read one case and resolve its labels; :class:`EvalError` if anything doesn't check out."""
    name = directory.name
    try:
        patch = parse_patch(decode_diff((directory / "pr.diff").read_bytes()))
        data = json.loads((directory / "labels.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalError(f"{name}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("bugs"), list):
        raise EvalError(f'{name}: labels.json must be an object with a "bugs" list')
    labels = [_label(name, directory / "after", patch, item) for item in data["bugs"]]
    return Case(name, str(data.get("summary", "")), directory, patch, labels)


def load_cases(root: Path) -> list[Case]:
    """Every case under ``root``, by name."""
    if not root.is_dir():
        raise EvalError(f"no eval cases at {root}")
    return [load_case(d) for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")]


@dataclass
class Score:
    findings: int = 0
    correct: int = 0
    """Findings that matched a label."""
    labels: int = 0
    found: int = 0
    """Labels that some finding matched."""

    def __add__(self, other: Score) -> Score:
        return Score(
            self.findings + other.findings,
            self.correct + other.correct,
            self.labels + other.labels,
            self.found + other.found,
        )

    @property
    def precision(self) -> float | None:
        return self.correct / self.findings if self.findings else None

    @property
    def recall(self) -> float | None:
        return self.found / self.labels if self.labels else None


def matches(finding: Finding, label: Label, *, categories: bool = True) -> bool:
    return (
        finding.path == label.path
        and finding.line in label.lines
        and (not categories or finding.category is label.category)
    )


def score(
    findings: list[Finding], labels: list[Label], *, categories: bool = True
) -> tuple[Score, list[bool]]:
    """Match ``findings`` to ``labels`` one to one, in the findings' order: each finding takes the first label
    it fits that no earlier finding took. A second finding on a bug already found counts against precision.

    Returns the score and, for each finding, whether it matched.
    """
    taken = [False] * len(labels)
    correct: list[bool] = []
    for finding in findings:
        i = next(
            (
                i
                for i, label in enumerate(labels)
                if not taken[i] and matches(finding, label, categories=categories)
            ),
            None,
        )
        if i is not None:
            taken[i] = True
        correct.append(i is not None)
    return Score(len(findings), sum(correct), len(labels), sum(taken)), correct


def posted(findings: list[Finding], cap: int = MAX_FINDINGS) -> list[Finding]:
    """What a review with only these findings would post: deduped, ranked, capped."""
    return rank(dedupe(findings)[0])[:cap]


class _Once:
    """Asks the wrapped provider once per request, so scoring the model alone and the merged review costs
    one call per case, even with a live provider."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name
        self.answers: dict[str, Completion] = {}

    def complete(self, request: Request) -> Completion:
        key = request.key()
        if key not in self.answers:
            self.answers[key] = self.inner.complete(request)
        return self.answers[key]


@dataclass
class CaseResult:
    case: Case
    static: list[Finding]
    """What the static pre-pass alone would post."""
    model: list[Finding] | None = None
    """What the model alone would post, or ``None`` when the case has no recording."""
    combined: list[Finding] | None = None
    """What the review posts: static and model findings merged, ranked and capped."""
    model_name: str = ""

    def findings(self, source: str) -> list[Finding] | None:
        return {"static": self.static, "model": self.model, "combined": self.combined}[source]


def run_case(case: Case, provider: Provider | None, *, ruff: list[str] | None) -> CaseResult:
    """Review one case the way ``codelens review`` would, and keep each source's findings apart.

    A case without a recording (``RecordingMissing``) is scored on the static pre-pass only; any other
    provider error is raised, since a broken provider would make every number wrong.
    """
    pre = analyse(case.patch, case.root, ruff=ruff)
    result = CaseResult(case, posted(pre.findings))
    if provider is None:
        return result
    once = _Once(provider)
    try:
        merged = review(case.patch, once, static=pre)
    except RecordingMissing:
        return result
    prompt = build_prompt(case.patch, static=pre.findings)
    completion = once.complete(prompt.request)  # the review's own answer, from the cache
    found, _ = check_response(completion.text, PatchSet(prompt.files))
    result.model, result.combined, result.model_name = posted(found), merged.findings, completion.model
    return result


SOURCES = ("static", "model", "combined")
_ROW_NAMES = {"static": "static pre-pass", "model": "model alone", "combined": "static + model (posted)"}


@dataclass
class Report:
    results: list[CaseResult]

    def scored(self, source: str) -> list[CaseResult]:
        return [r for r in self.results if r.findings(source) is not None]

    def total(self, source: str, *, categories: bool = True) -> Score:
        total = Score()
        for result in self.scored(source):
            findings = result.findings(source)
            assert findings is not None
            total += score(findings, result.case.labels, categories=categories)[0]
        return total

    def models(self) -> list[str]:
        return sorted({r.model_name for r in self.results if r.model_name})

    def markdown(self) -> str:
        def pct(value: float | None) -> str:
            return "—" if value is None else f"{value:.1%}"

        n = len(self.results)
        lines = [
            "| Source | Cases scored | Findings | Correct | Precision | Bugs found | Recall "
            "| Recall, any category |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for source in SOURCES:
            strict, loose = self.total(source), self.total(source, categories=False)
            lines.append(
                f"| {_ROW_NAMES[source]} | {len(self.scored(source))} of {n} | {strict.findings} | "
                f"{strict.correct} | {pct(strict.precision)} | {strict.found} of {strict.labels} | "
                f"{pct(strict.recall)} | {pct(loose.recall)} |"
            )
        lines += ["", "| Case | Bugs | Static: found / false positives | Model: found / false positives |"]
        lines.append("|---|---|---|---|")
        for result in self.results:
            cells = []
            for source in ("static", "model"):
                findings = result.findings(source)
                if findings is None:
                    cells.append("not recorded")
                    continue
                case_score, _ = score(findings, result.case.labels)
                cells.append(f"{case_score.found} / {case_score.findings - case_score.correct}")
            lines.append(f"| `{result.case.name}` | {len(result.case.labels)} | {cells[0]} | {cells[1]} |")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        def entry(result: CaseResult, source: str) -> list[dict[str, Any]] | None:
            findings = result.findings(source)
            if findings is None:
                return None
            _, correct = score(findings, result.case.labels)
            return [{**f.to_dict(), "correct": ok} for f, ok in zip(findings, correct, strict=True)]

        totals = {}
        for source in SOURCES:
            strict, loose = self.total(source), self.total(source, categories=False)
            totals[source] = {
                "cases": len(self.scored(source)),
                "findings": strict.findings,
                "correct": strict.correct,
                "labels": strict.labels,
                "found": strict.found,
                "precision": strict.precision,
                "recall": strict.recall,
                "recall_any_category": loose.recall,
            }
        return {
            "models": self.models(),
            "totals": totals,
            "cases": [
                {
                    "name": r.case.name,
                    "labels": [
                        {"path": lb.path, "line": lb.line, "category": lb.category.value}
                        for lb in r.case.labels
                    ],
                    **{source: entry(r, source) for source in SOURCES},
                }
                for r in self.results
            ],
        }


def run(cases: list[Case], provider: Provider | None, *, ruff: list[str] | None) -> Report:
    return Report([run_case(case, provider, ruff=ruff) for case in cases])
