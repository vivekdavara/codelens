"""static.old_version against real git: seeded random edits, diffed with 0, 1 and 3 lines of context, must
rebuild the old file exactly from the new file and the diff (the old file is never read)."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest
from test_diff_against_git import as_text, git, mutate, random_file, write

from codelens.diff import parse_patch
from codelens.static import old_version

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_old_version_rebuilds_the_old_file_exactly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    rng = random.Random(20261010)
    checked = 0
    for n in range(120):
        old, new = random_file(rng), []
        new = mutate(rng, old)
        old_nl, new_nl = rng.random() > 0.2, rng.random() > 0.2
        f = repo / f"f{n}.txt"
        write(f, old, old_nl)
        git(repo, "add", f.name)
        git(repo, "commit", "-qm", f"base {n}", "--allow-empty")
        write(f, new, new_nl)
        for context in ("-U0", "-U1", "-U3"):
            diff = git(repo, "diff", "--no-color", context, "--", f.name)
            if not diff:
                continue
            (file,) = parse_patch(diff).files
            rebuilt = old_version(file, as_text(new, new_nl).split("\n"))
            assert "\n".join(rebuilt) == as_text(old, old_nl), (n, context)
            checked += 1
        git(repo, "checkout", "-q", "--", f.name)
    assert checked > 300
