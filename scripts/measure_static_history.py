"""Run the static pre-pass over a repository's own history: every commit's diff, checked against that commit.

For each non-merge commit C (oldest first), `git diff C^ C` is the "PR" and `git archive C` is the checkout
the pre-pass reads, so every number comes from real diffs of real code. Each finding is printed, so you can
judge it; the commits were already reviewed and passed CI, so a finding here is either a bug that slipped
through or noise.

    .venv/bin/python scripts/measure_static_history.py [repo]       # default: this repository

Merges are skipped (their diff against the first parent repeats the branch's commits). The root commit is
diffed against the empty tree.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from codelens.diff import decode_diff, parse_patch
from codelens.static import analyse, find_ruff

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DIFF_FLAGS = ["--no-color", "--no-ext-diff", "-M", "--no-textconv"]


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True).stdout


def main() -> None:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    log = git(repo, "log", "--reverse", "--no-merges", "--format=%H %P").decode().split("\n")
    commits = [line.split() for line in log if line.strip()]
    ruff = find_ruff()
    counts: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    for sha, *parents in commits:
        base = parents[0] if parents else EMPTY_TREE
        patch = parse_patch(decode_diff(git(repo, "diff", *DIFF_FLAGS, base, sha)))
        if not any(f.path.endswith(".py") and f.added_lines() for f in patch):
            continue
        with tempfile.TemporaryDirectory(prefix="codelens-history-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", sha))) as tar:
                tar.extractall(tmp, filter="data")
            result = analyse(patch, Path(tmp), ruff=ruff)
        counts["commits with Python changes"] += 1
        counts["Python files checked"] += len(result.analysed)
        counts["Python files skipped"] += len(result.skipped)
        counts["added Python lines"] += sum(len(f.added_lines()) for f in patch if f.path in result.analysed)
        counts["findings"] += len(result.findings)
        counts["hits on unchanged lines already there"] += result.existing
        for note in result.notes:
            print(f"{sha[:7]} note: {note}")
        for path, reason in result.skipped:
            print(f"{sha[:7]} skipped {path}: {reason}")
        for f in result.findings:
            rules[f.rule] += 1
            print(f"{sha[:7]} {f.path}:{f.line} [{f.rule}] {f.title} | {f.quote.strip()[:80]}")
    print(f"repository: {repo.name}, {len(commits)} non-merge commits, {result.tool if commits else ''}")
    for name, value in counts.items():
        print(f"{name}: {value:,}")
    print("findings by rule: " + (", ".join(f"{r} {n}" for r, n in rules.most_common()) or "none"))


if __name__ == "__main__":
    main()
