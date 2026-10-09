"""Runs scripts/check_history.py: every commit's real git diff must parse and render.

On this repository (the whole history locally, one commit in CI's shallow clone), and on a scratch repository
with a merge that resolved a conflict, where `git show` would print a combined diff the parser can't read.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def check(repo: Path, env: dict[str, str] | None = None) -> tuple[int, int]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_history.py"), str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(
        r"(\d+) commits, (\d+) file diffs, \d+ shown to the model, 0 parse failures", result.stdout
    )
    assert match is not None, result.stdout
    return int(match.group(1)), int(match.group(2))


def test_every_commit_of_this_repository_parses() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    commits, files = check(ROOT)
    assert commits >= 1 and files >= 1  # an empty parse would pass vacuously


def test_merges_and_user_git_config_dont_break_it(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, env=env)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[color]\n\tui = always\n[diff]\n\tnoprefix = true\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    env.pop("GIT_CONFIG_GLOBAL", None)
    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    git("add", "a.txt")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "side")
    (tmp_path / "a.txt").write_text("one\nSIDE\nthree\n")
    git("commit", "-qam", "side")
    git("checkout", "-q", "main")
    (tmp_path / "a.txt").write_text("one\nMAIN\nthree\n")
    git("commit", "-qam", "main")
    subprocess.run(
        ["git", "-C", str(tmp_path), "merge", "-q", "side"], capture_output=True, env=env
    )  # conflicts
    (tmp_path / "a.txt").write_text("one\nBOTH\nthree\n")
    git("commit", "-qam", "merge")
    commits, files = check(tmp_path, env)
    assert (commits, files) == (4, 4)
