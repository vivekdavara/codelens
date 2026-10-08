"""Differential tests: random edits, diffed by real git, must map back to the exact file contents.

If any line number or count were off by one, the checks below would fail: with full context the hunks rebuild
both files byte for byte, and with git's default 3 lines of context every shown line must match the file at
the line number the parser gave it.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from codelens.diff import LineKind, parse_patch

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

WORDS = ["alpha", "beta", "gamma", "delta", "", "    pass", "return x", "# note", "x = 1", "}"]
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True, text=True)
    return result.stdout


def random_file(rng: random.Random) -> list[str]:
    return [rng.choice(WORDS) for _ in range(rng.randint(0, 60))]


def mutate(rng: random.Random, lines: list[str]) -> list[str]:
    out = list(lines)
    for _ in range(rng.randint(1, 8)):
        op = rng.choice(["insert", "delete", "replace"])
        i = rng.randint(0, len(out))
        if op == "insert":
            out[i:i] = [rng.choice(WORDS) for _ in range(rng.randint(1, 4))]
        elif out and op == "delete":
            del out[min(i, len(out) - 1) : i + rng.randint(1, 3)]
        elif out:
            out[min(i, len(out) - 1)] = f"changed {rng.random():.6f}"
    return out


def write(path: Path, lines: list[str], trailing_newline: bool) -> None:
    text = "\n".join(lines)
    if lines and trailing_newline:
        text += "\n"
    path.write_text(text)


def as_text(lines: list[str], trailing_newline: bool) -> str:
    return "\n".join(lines) + ("\n" if lines and trailing_newline else "")


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str, str, str]]:
    """(old_text, new_text, full_context_diff, default_diff) for 150 seeded random edits."""
    repo = tmp_path_factory.mktemp("repo")
    git(repo, "init", "-q")
    rng = random.Random(20261008)
    out = []
    for n in range(150):
        old = random_file(rng)
        new = mutate(rng, old)
        old_nl, new_nl = rng.random() > 0.2, rng.random() > 0.2
        f = repo / f"f{n}.txt"
        write(f, old, old_nl)
        git(repo, "add", f.name)
        git(repo, "commit", "-qm", f"base {n}", "--allow-empty")
        write(f, new, new_nl)
        full = git(repo, "diff", "--no-color", "-U100000", "--", f.name)
        default = git(repo, "diff", "--no-color", "--", f.name)
        git(repo, "checkout", "-q", "--", f.name)
        out.append((as_text(old, old_nl), as_text(new, new_nl), full, default))
    return out


def rebuild(diff_text: str) -> tuple[str, str]:
    """Rebuild old and new file text from a full-context diff."""
    (f,) = parse_patch(diff_text).files
    old: list[str] = []
    new: list[str] = []
    old_eof_nl = new_eof_nl = True
    for line in f.lines():
        if line.kind is not LineKind.ADDED:
            old.append(line.content)
            if line.no_newline_at_eof:
                old_eof_nl = False
        if line.kind is not LineKind.REMOVED:
            new.append(line.content)
            if line.no_newline_at_eof:
                new_eof_nl = False
    return as_text(old, old_eof_nl), as_text(new, new_eof_nl)


def test_full_context_hunks_rebuild_both_files(cases: list[tuple[str, str, str, str]]) -> None:
    checked = 0
    for old, new, full, _ in cases:
        if not full:  # the mutation happened to produce identical content
            continue
        assert rebuild(full) == (old, new)
        checked += 1
    assert checked > 100


def test_default_context_line_numbers_match_the_files(cases: list[tuple[str, str, str, str]]) -> None:
    checked = 0
    for old, new, _, default in cases:
        if not default:
            continue
        old_lines, new_lines = old.split("\n"), new.split("\n")
        (f,) = parse_patch(default).files
        for line in f.lines():
            if line.old_lineno is not None:
                assert old_lines[line.old_lineno - 1] == line.content
            if line.new_lineno is not None:
                assert new_lines[line.new_lineno - 1] == line.content
            checked += 1
        added = f.added_lines()
        assert added == sorted(set(added))
    assert checked > 1000
