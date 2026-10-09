"""Measure how many off-by-N line citations the quote check catches, on real code diffed by real git.

A model that means line L but writes L+N (a common LLM error) still lands inside a hunk most of the time, and
anchoring by line number alone would post the comment on the wrong line. This script simulates exactly that:

1. Take real Python files (by default a seeded sample of this interpreter's standard library; read locally,
   never copied into the repo) and make seeded random edits to each.
2. Diff each edit with `git diff` (default 3 lines of context) and parse it with codelens.diff.
3. For every commentable new-file line L with text T, build the finding a model would give if it quoted T but
   cited L+N, and run it through codelens.findings.anchor_finding.

    .venv/bin/python scripts/measure_quote_check.py                    # 300 stdlib files, offsets +-1 and +-2
    .venv/bin/python scripts/measure_quote_check.py --corpus src --files 20

The edits are synthetic; the code they are applied to is real.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import sysconfig
import tempfile
from collections import Counter
from pathlib import Path

from codelens.diff import Side, parse_patch
from codelens.findings import Category, Finding, Severity, anchor_finding

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "m",
    "GIT_AUTHOR_EMAIL": "m@example.com",
    "GIT_COMMITTER_NAME": "m",
    "GIT_COMMITTER_EMAIL": "m@example.com",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout


def edit(rng: random.Random, lines: list[str]) -> list[str]:
    """1-5 edits of the kinds a PR makes: change a line, insert lines copied from elsewhere, delete a few."""
    out = list(lines)
    for _ in range(rng.randint(1, 5)):
        i = rng.randrange(len(out) + 1)
        op = rng.choice(["change", "insert", "delete"])
        if op == "insert" or not out:
            out[i:i] = [rng.choice(lines) for _ in range(rng.randint(1, 4))]
        elif op == "delete":
            del out[min(i, len(out) - 1) : i + rng.randint(1, 3)]
        else:
            j = min(i, len(out) - 1)
            out[j] = out[j] + "  # edited" if out[j].strip() else "pass  # added"
    return out


def corpus(root: Path | None) -> list[Path]:
    base = root if root is not None else Path(sysconfig.get_paths()["stdlib"])
    return sorted(
        p for p in base.rglob("*.py") if "site-packages" not in p.parts and p.stat().st_size < 200_000
    )


def measure(paths: list[Path], offsets: list[int]) -> Counter[str]:
    counts: Counter[str] = Counter()
    rng = random.Random(20261009)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git(repo, "init", "-q")
        for n, path in enumerate(paths):
            try:
                lines = path.read_text(encoding="utf-8").split("\n")
            except (UnicodeDecodeError, OSError):
                counts["files unreadable"] += 1
                continue
            target = repo / f"f{n}.py"
            target.write_text("\n".join(lines), encoding="utf-8")
            git(repo, "add", target.name)
            git(repo, "commit", "-qm", "base", "--allow-empty")
            target.write_text("\n".join(edit(rng, lines)), encoding="utf-8")
            diff = git(repo, "diff", "--no-color", "--", target.name)
            git(repo, "checkout", "-q", "--", target.name)
            if not diff:
                continue
            patch = parse_patch(diff)
            (file,) = patch.files
            counts["files"] += 1
            for line in file.lines():
                if line.new_lineno is None:
                    continue
                counts["commentable lines"] += 1
                true = Finding(
                    file.path, line.new_lineno, Severity.LOW, Category.BUG, "t", "b", 0.5, line.content
                )
                if anchor_finding(true, patch) is not None:
                    counts["true citations rejected"] += 1  # must stay 0
                for offset in offsets:
                    cited = line.new_lineno + offset
                    finding = Finding(
                        file.path, cited, Severity.LOW, Category.BUG, "t", "b", 0.5, line.content
                    )
                    counts["off-by-N citations"] += 1
                    neighbour = file.anchor(cited, Side.RIGHT)
                    if neighbour is None:
                        counts["outside every hunk"] += 1
                        continue
                    counts["inside a hunk"] += 1
                    rejection = anchor_finding(finding, patch)
                    quote, actual = " ".join(line.content.split()), " ".join(neighbour.content.split())
                    if quote != actual:
                        counts["exact rule would reject"] += 1
                    if rejection is not None:
                        counts["quote check rejects"] += 1
                    else:
                        counts["quote check accepts"] += 1
                        kind = "blank" if not quote else "same text" if quote == actual else "quote inside"
                        counts[f"accepted: {kind}"] += 1
    return counts


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", type=Path, help="directory of .py files (default: the stdlib)")
    parser.add_argument("--files", type=int, default=300, help="number of files to sample")
    parser.add_argument("--offsets", default="-2,-1,1,2", help="comma-separated citation errors to simulate")
    args = parser.parse_args()
    paths = corpus(args.corpus)
    sample = random.Random(20261009).sample(paths, min(args.files, len(paths)))
    offsets = [int(x) for x in args.offsets.split(",")]
    c = measure(sample, offsets)
    source = args.corpus or f"Python {sys.version.split()[0]} stdlib"
    inside = c["inside a hunk"]
    print(f"corpus: {source}, {c['files']} edited files, {c['commentable lines']} commentable lines")
    print(f"true citations rejected: {c['true citations rejected']}")
    print(f"off-by-N citations (N in {offsets}): {c['off-by-N citations']}")
    print(f"  outside every hunk (rejected by anchoring alone): {c['outside every hunk']}")
    print(f"  inside a hunk (anchoring alone would accept): {inside}")
    rejects, accepts, exact = c["quote check rejects"], c["quote check accepts"], c["exact rule would reject"]
    print(f"    rejected by the quote check: {rejects} ({pct(rejects, inside)})")
    print(f"    accepted: {accepts} ({pct(accepts, inside)})")
    for kind in ("blank", "same text", "quote inside"):
        print(f"      {kind}: {c[f'accepted: {kind}']}")
    print(f"    an exact-match rule would reject: {exact} ({pct(exact, inside)})")


if __name__ == "__main__":
    main()
