"""Review findings: the schema the model must follow, validation, and anchoring to the diff.

A finding is a claim about one line of the new file. Everything a model returns is untrusted data: it is
parsed, validated field by field, and anchored through the diff parser. Anything that fails is dropped with a
reason, never repaired or moved to a nearby line. See DESIGN.md, "Findings".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from codelens.diff import DiffLine, FileDiff, PatchSet, Side

__all__ = [
    "FINDINGS_SCHEMA",
    "Category",
    "Finding",
    "FindingsFormatError",
    "Rejection",
    "Severity",
    "anchor_finding",
    "check_response",
    "parse_findings",
]


class Severity(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """0 for critical up to 3 for low, so sorting by rank puts the worst first."""
        return list(Severity).index(self)


class Category(Enum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    TEST = "test"


MAX_TITLE = 200
MAX_BODY = 4000


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    severity: Severity
    category: Category
    title: str
    body: str
    confidence: float
    quote: str = ""
    """The cited line's text as the model copied it; once anchored, the line's full text from the diff."""
    side: Side = Side.RIGHT
    source: str = "llm"
    occurrence: int = 0
    """Once anchored: how many lines above this one in the file's diff have the same text. With the path
    and the quote it identifies the line across runs, where its number would move (github.fingerprint)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "side": self.side.value,
            "severity": self.severity.value,
            "category": self.category.value,
            "title": self.title,
            "body": self.body,
            "confidence": self.confidence,
            "quote": self.quote,
            "source": self.source,
            "occurrence": self.occurrence,
        }


@dataclass(frozen=True)
class Rejection:
    """A finding that was dropped, and why. ``index`` is its position in the model's ``findings`` array."""

    index: int
    kind: str
    """``invalid`` (fails the schema), ``unanchored`` (not a diff line) or ``misquoted`` (wrong line)."""
    detail: str


class FindingsFormatError(ValueError):
    """The response as a whole is unusable: not JSON, or not an object with a ``findings`` array."""


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


# The schema sent to the model (structured outputs). Structured-output APIs accept only a subset of JSON
# Schema: no numeric ranges or string lengths, and every object closed with additionalProperties: false. The
# limits they can't express (confidence in [0, 1], line >= 1, length caps) are checked by parse_findings.
FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": _string("File path exactly as it appears in the diff."),
                    "line": {
                        "type": "integer",
                        "description": "New-file line number, read from the numbered diff's left margin.",
                    },
                    "quote": _string(
                        "The full text of that line, copied exactly, without the margin or the +/space mark."
                    ),
                    "severity": {"type": "string", "enum": [s.value for s in Severity]},
                    "category": {"type": "string", "enum": [c.value for c in Category]},
                    "title": _string("One-line summary of the problem."),
                    "body": _string("What is wrong, why it matters, and how to fix it. Markdown."),
                    "confidence": {"type": "number", "description": "How sure you are, from 0 to 1."},
                },
                "required": ["path", "line", "quote", "severity", "category", "title", "body", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

_ITEM_KEYS = frozenset(FINDINGS_SCHEMA["properties"]["findings"]["items"]["properties"])
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not valid JSON")


def _load(text: str) -> Any:
    stripped = text.strip()
    # Accept one surrounding Markdown fence (models without structured outputs add them); nothing else.
    if (fenced := _FENCE.match(stripped)) is not None:
        stripped = fenced.group(1)
    try:
        return json.loads(stripped, parse_constant=_reject_constant)
    except ValueError as exc:
        raise FindingsFormatError(f"response is not valid JSON: {exc}") from None


def _validate(index: int, item: Any) -> Finding | Rejection:
    def invalid(detail: str) -> Rejection:
        return Rejection(index, "invalid", detail)

    if not isinstance(item, dict):
        return invalid(f"expected an object, got {type(item).__name__}")
    missing = sorted(_ITEM_KEYS - item.keys())
    if missing:
        return invalid(f"missing {', '.join(missing)}")
    extra = sorted(item.keys() - _ITEM_KEYS)
    if extra:
        return invalid(f"unexpected {', '.join(extra)}")
    for key in ("path", "quote", "title", "body", "severity", "category"):
        if not isinstance(item[key], str):
            return invalid(f"{key} must be a string")
    line, confidence = item["line"], item["confidence"]
    # bool is a subclass of int in Python; JSON true is not a line number.
    if type(line) is not int or line < 1:
        return invalid(f"line must be a positive integer, got {line!r}")
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        return invalid(f"confidence must be a number from 0 to 1, got {confidence!r}")
    try:
        severity, category = Severity(item["severity"]), Category(item["category"])
    except ValueError as exc:
        return invalid(str(exc))
    # A title is one line (bold text and a table cell when posted); the body is Markdown and keeps its own.
    title, body = " ".join(item["title"].split()), item["body"].strip()
    if not item["path"] or not title or not body:
        return invalid("path, title and body must not be empty")
    if len(title) > MAX_TITLE or len(body) > MAX_BODY:
        return invalid(f"title is limited to {MAX_TITLE} characters and body to {MAX_BODY}")
    return Finding(
        path=item["path"],
        line=line,
        severity=severity,
        category=category,
        title=title,
        body=body,
        confidence=float(confidence),
        quote=item["quote"],
    )


def _items(text: str) -> list[Any]:
    data = _load(text)
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise FindingsFormatError('expected a JSON object with a "findings" array')
    return items


def parse_findings(text: str) -> tuple[list[Finding], list[Rejection]]:
    """Parse a model response into valid findings plus the items that failed validation.

    Raises :class:`FindingsFormatError` when the response itself is unusable; a single bad item only rejects
    that item.
    """
    findings: list[Finding] = []
    rejections: list[Rejection] = []
    for index, item in enumerate(_items(text)):
        result = _validate(index, item)
        if isinstance(result, Finding):
            findings.append(result)
        else:
            rejections.append(result)
    return findings, rejections


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _locate(finding: Finding, patch: PatchSet, index: int) -> tuple[FileDiff, DiffLine] | Rejection:
    file = patch.get(finding.path)
    if file is None:
        return Rejection(index, "unanchored", f"{finding.path} is not in the diff")
    if file.is_binary:
        return Rejection(index, "unanchored", f"{finding.path} is a binary file")
    diff_line = file.anchor(finding.line, finding.side)
    if diff_line is None:
        return Rejection(index, "unanchored", f"{finding.path}:{finding.line} is not a line in the diff")
    quote, actual = _normalise(finding.quote), _normalise(diff_line.content)
    if quote != actual and not (quote and quote in actual):
        return Rejection(
            index,
            "misquoted",
            f"{finding.path}:{finding.line} is {diff_line.content.strip()!r}, not {finding.quote.strip()!r}",
        )
    return file, diff_line


def anchor_finding(finding: Finding, patch: PatchSet, index: int = -1) -> Rejection | None:
    """``None`` if the finding lands on a diff line whose text matches its quote; otherwise why not.

    The finding must name a file in the diff and a line :meth:`FileDiff.anchor` accepts, and its ``quote``
    must appear in that line (whitespace-insensitive; a blank line needs a blank quote). The quote check
    catches the commonest model error, an off-by-some line number that still lands inside a hunk, which
    anchoring alone would accept.
    """
    located = _locate(finding, patch, index)
    return located if isinstance(located, Rejection) else None


def _occurrence(file: FileDiff, target: DiffLine, side: Side) -> int:
    """How many lines of ``file``'s diff, on ``side`` and above ``target``, have the same text as it."""

    def number(line: DiffLine) -> int | None:
        return line.new_lineno if side is Side.RIGHT else line.old_lineno

    limit, text = number(target), _normalise(target.content)
    assert limit is not None  # target was anchored on this side
    return sum(
        1
        for line in file.lines()
        if (n := number(line)) is not None and n < limit and _normalise(line.content) == text
    )


def check_response(text: str, patch: PatchSet) -> tuple[list[Finding], list[Rejection]]:
    """Validate and anchor every finding in a model response, in response order.

    Returns the findings that pass both checks, each with ``quote`` set to the full text of its line (a model
    may quote part of it) and ``occurrence`` to the number of identical lines above it in the diff, and a
    :class:`Rejection` for each one that doesn't; a rejection's ``index`` is the item's position in the
    response's ``findings`` array.
    """
    findings: list[Finding] = []
    rejections: list[Rejection] = []
    for index, item in enumerate(_items(text)):
        result = _validate(index, item)
        located = _locate(result, patch, index) if isinstance(result, Finding) else result
        if isinstance(located, Rejection):
            rejections.append(located)
            continue
        assert isinstance(result, Finding)
        file, line = located
        findings.append(replace(result, quote=line.content, occurrence=_occurrence(file, line, result.side)))
    return findings, rejections
