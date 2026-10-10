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
from codelens.findings import Category

__all__ = ["Case", "EvalError", "Label", "load_case", "load_cases"]


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
