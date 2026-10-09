"""Parse and render every commit of a git repository: a smoke test of the parser on real diffs.

    .venv/bin/python scripts/check_history.py              # this repository
    .venv/bin/python scripts/check_history.py ../other     # any local clone

For each commit, `git show -M` output goes through codelens.diff.parse_patch and
codelens.prompts.build_prompt. Prints the totals and every commit whose diff fails to parse, and exits 1 if
any did.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from codelens.diff import DiffParseError, parse_patch
from codelens.prompts import build_prompt


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return result.stdout.decode("utf-8", errors="surrogateescape")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args()
    commits = git(args.repo, "rev-list", "--reverse", "HEAD").split()
    failures: list[tuple[str, str]] = []
    files = shown = 0
    skipped: Counter[str] = Counter()
    started = time.perf_counter()
    for commit in commits:
        text = git(args.repo, "show", "--format=", "-M", commit)
        try:
            patch = parse_patch(text)
        except DiffParseError as exc:
            failures.append((commit[:12], str(exc)))
            continue
        prompt = build_prompt(patch)
        files += len(patch)
        shown += len(prompt.files)
        skipped.update(reason.split(" (")[0] for _, reason in prompt.skipped)
    elapsed_ms = (time.perf_counter() - started) * 1000
    summary = f"{len(commits)} commits, {files} file diffs, {shown} shown to the model"
    print(f"{summary}, {len(failures)} parse failures")
    print(f"skipped: {dict(skipped) or 'none'}; {elapsed_ms:.0f} ms including git")
    for commit, error in failures:
        print(f"  {commit}: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
