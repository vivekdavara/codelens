"""Runs scripts/measure_static_history.py on a small temporary repository with a known history."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codelens.static import find_ruff

ROOT = Path(__file__).parent.parent
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    subprocess.run([*GIT, "add", "-A"], cwd=repo, env=ENV, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", message], cwd=repo, env=ENV, check=True)


@pytest.mark.skipif(find_ruff() is None, reason="ruff is a dev dependency")
def test_history_run_reports_each_commits_new_findings(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=ENV, check=True)
    commit(tmp_path, {"m.py": "def f():\n    return 1\n"}, "root")  # diffed against the empty tree
    commit(tmp_path, {"m.py": "def f():\n    unused = 2\n    return 1\n"}, "adds a bug")
    commit(tmp_path, {"m.py": "def f():\n    unused = 2\n    return 3\n", "README.md": "x\n"}, "keeps it")
    commit(tmp_path, {"notes.md": "no Python here\n"}, "docs only")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "measure_static_history.py"), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    (finding,) = [line for line in lines if "[F841]" in line]
    assert finding.endswith(
        "m.py:2 [F841] Local variable `unused` is assigned to but never used | unused = 2"
    )
    assert "commits with Python changes: 3" in lines
    assert "findings: 1" in lines and "hits on unchanged lines already there: 1" in lines
    assert lines[-1] == "findings by rule: F841 1"
