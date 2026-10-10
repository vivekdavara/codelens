"""Regenerate every eval case's pr.diff from its before/ and after/ trees with real git.

    .venv/bin/python scripts/build_evals.py [evals/cases]

Each case is committed as a base snapshot in a temporary repository, the after/ tree is written over it, and
`git diff --cached -M` of the two is the case's diff, exactly as `git diff` or GitHub would show the PR. The
diff is committed with the case, so a different git version can't change the prompts (and with them the
recording keys); tests/test_evals.py checks that each diff still turns before/ into after/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GIT = [
    "git",
    "-c",
    "user.name=CodeLens evals",
    "-c",
    "user.email=evals@example.com",
    "-c",
    "core.quotePath=true",
]


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run([*GIT, *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def build(case: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git(repo, "init", "-q")
        if (case / "before").is_dir():
            shutil.copytree(case / "before", repo, dirs_exist_ok=True)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        for child in repo.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(case / "after", repo, dirs_exist_ok=True)
        git(repo, "add", "-A")
        return git(repo, "diff", "--cached", "-M", "--no-color", "--no-ext-diff")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "evals/cases")
    for case in sorted(p for p in root.iterdir() if p.is_dir()):
        diff = build(case)
        (case / "pr.diff").write_bytes(diff.encode("utf-8"))
        print(f"{case.name}: {diff.count(chr(10))} diff lines")


if __name__ == "__main__":
    main()
