"""Parse and render every commit of a git repository: a smoke test of the parser on real diffs.

    .venv/bin/python scripts/check_history.py              # this repository
    .venv/bin/python scripts/check_history.py ../other     # any local clone

One `git log -p` over the whole history feeds each commit's diff through codelens.diff.parse_patch and
codelens.prompts.build_prompt. Git runs with the user's configuration ignored (colour, prefixes and the like
would change the output), and a merge shows its diff against its first parent, as a pull request does,
instead of git's combined diff. Prints the totals and every commit whose diff fails to parse, and exits 1 if
any did.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from codelens.diff import DiffParseError, decode_diff, parse_patch
from codelens.prompts import build_prompt

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def history(repo: Path) -> list[tuple[str, str]]:
    """``(commit, diff text)`` for every commit, oldest first."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "--reverse",
            "--no-color",
            "-p",
            "-M",
            "--diff-merges=first-parent",
            "--format=%x00%H",
        ],
        env=GIT_ENV,
        check=True,
        capture_output=True,
    ).stdout
    commits = []
    for chunk in out.split(b"\0")[1:]:
        sha, _, diff = chunk.partition(b"\n")
        commits.append((sha.decode(), decode_diff(diff)))
    return commits


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args()
    started = time.perf_counter()
    commits = history(args.repo)
    failures: list[tuple[str, str]] = []
    files = shown = 0
    skipped: Counter[str] = Counter()
    for commit, text in commits:
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
