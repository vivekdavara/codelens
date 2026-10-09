"""Runs scripts/check_history.py on this repository: every commit's real git diff must parse and render.

In CI's shallow clone that is one commit, shown as adding every file; locally it is the whole history.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_every_commit_of_this_repository_parses() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        [sys.executable, "scripts/check_history.py"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ", 0 parse failures" in result.stdout
