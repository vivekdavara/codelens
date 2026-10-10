"""Seeded fuzzing of the static pre-pass: the code is the PR's, so no input may crash it, and whatever it
reports must be placed exactly where the diff can carry it."""

from __future__ import annotations

import ast
import random
import shutil
import warnings
from pathlib import Path

import pytest
from test_diff_against_git import git

from codelens.diff import Side, decode_diff, parse_patch
from codelens.rules import check
from codelens.static import RULES, analyse, find_ruff

SOURCES = sorted((Path(__file__).parent.parent / "src" / "codelens").rglob("*.py"))
TOKENS = [
    "(",
    ")",
    "[",
    "]",
    ":",
    "async ",
    "await ",
    "for x in items:",
    "items.remove(x)",
    "return",
    "\n",
    "    ",
    "'{x}'",
    "yield",
    "lambda: ",
    "del d[k]",
    "\\r",
    "﻿",
    "match x:",
    "case _:",
    "*",
]


def mutate(rng: random.Random, text: str) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, 12)):
        i = rng.randrange(len(chars) + 1)
        op = rng.random()
        if op < 0.4:
            chars[i:i] = list(rng.choice(TOKENS))
        elif op < 0.7 and chars:
            del chars[i : i + rng.randint(1, 20)]
        elif chars:
            j = rng.randrange(len(chars))
            chars[i:i] = chars[j : j + rng.randint(1, 80)]  # copy a stretch of real code elsewhere
    return "".join(chars)


def parses(text: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


def test_the_codelens_rules_never_raise_on_mutated_code() -> None:
    rng = random.Random(20261010)
    parsed = 0
    for _ in range(2000):
        text = mutate(rng, rng.choice(SOURCES).read_text())
        hits = check(text)
        assert all(h.code in RULES and h.line >= 1 for h in hits)
        parsed += parses(text)
    # Most mutations break the syntax; enough still parse (143 at this seed) for the visitors to run too.
    assert parsed > 100


@pytest.mark.skipif(shutil.which("git") is None or find_ruff() is None, reason="needs git and ruff")
def test_every_static_finding_sits_on_a_shown_line_and_quotes_it(tmp_path: Path) -> None:
    rng = random.Random(20261011)
    git(tmp_path, "init", "-q")
    files = rng.sample(SOURCES, 6)
    for n, source in enumerate(files):
        (tmp_path / f"m{n}.py").write_text(source.read_text())
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    checked = 0
    for _ in range(4):
        for n, source in enumerate(files):
            lines = source.read_text().split("\n")
            for _ in range(rng.randint(1, 6)):  # line-level edits: mutated copies of lines, deletions
                i = rng.randrange(len(lines) + 1)
                if rng.random() < 0.5 or not lines:
                    lines[i:i] = [mutate(rng, rng.choice(lines or ["pass"]))]
                else:
                    del lines[i : i + rng.randint(1, 3)]
            (tmp_path / f"m{n}.py").write_text("\n".join(lines))
        patch = parse_patch(decode_diff(git(tmp_path, "diff", "--no-color").encode()))
        result = analyse(patch, tmp_path, ruff=find_ruff())
        assert result.notes == [] and sorted(result.analysed) == sorted(
            f.path for f in patch if f.added_lines()
        )
        for finding in result.findings:
            file = patch.get(finding.path)
            assert file is not None and finding.line in file.commentable_lines(Side.RIGHT)
            on_disk = (tmp_path / finding.path).read_text().split("\n")[finding.line - 1]
            assert finding.quote == on_disk and finding.rule in RULES
            checked += 1
        git(tmp_path, "checkout", "-q", "--", ".")
    assert checked > 0
