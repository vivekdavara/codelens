"""Measure how often each static rule fires on mature code: a ceiling on the pre-pass's noise.

Every file of the sample is reviewed as if a PR had added it whole, so every line counts as changed, and
the script counts the findings per rule. The code is real and has been reviewed and shipped, so most hits on
it are intentional or harmless (the standard library calls `eval` on purpose); a rule that fires often here
would fire often on real PRs too.

    .venv/bin/python scripts/measure_static_noise.py                  # the 300-file stdlib sample
    .venv/bin/python scripts/measure_static_noise.py --corpus src --files 50

The sample is the one scripts/measure_quote_check.py uses (same seed), read locally, never copied into the
repo.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import sysconfig
import tempfile
from collections import Counter
from pathlib import Path

from codelens.diff import parse_patch
from codelens.prompts import budget_rank
from codelens.static import RULES, analyse, find_ruff


def corpus(root: Path | None) -> list[Path]:
    base = root if root is not None else Path(sysconfig.get_paths()["stdlib"])
    return sorted(
        p for p in base.rglob("*.py") if "site-packages" not in p.parts and p.stat().st_size < 200_000
    )


def added_diff(path: str, text: str) -> str:
    """The diff of a PR that adds ``text`` as a new file at ``path``."""
    lines = text.split("\n")
    ends_with_newline = lines[-1] == ""
    if ends_with_newline:
        lines.pop()
    body = "".join(f"+{line}\n" for line in lines)
    marker = "" if ends_with_newline else "\\ No newline at end of file\n"
    header = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
    return f"{header}@@ -0,0 +1,{len(lines)} @@\n{body}{marker}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", type=Path, help="directory of .py files (default: the stdlib)")
    parser.add_argument("--files", type=int, default=300, help="number of files to sample")
    args = parser.parse_args()
    paths = corpus(args.corpus)
    sample = random.Random(20261009).sample(paths, min(args.files, len(paths)))
    base = args.corpus or Path(sysconfig.get_paths()["stdlib"])
    diffs, lines, unreadable = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="codelens-noise-") as tmp:
        root = Path(tmp)
        for path in sample:
            relative = path.relative_to(base).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                unreadable += 1
                continue
            if not text:
                continue
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, root / relative)
            diffs.append(added_diff(relative, text))
            lines += text.count("\n") + (not text.endswith("\n"))
        patch = parse_patch("".join(diffs))
        result = analyse(patch, root, ruff=find_ruff())
    hits = Counter(f.rule for f in result.findings)
    tests = {f.path for f in patch if budget_rank(f) == 1}  # test files, by the prompt budget's rule
    test_lines = sum(len(f.added_lines()) for f in patch if f.path in tests)
    test_hits = sum(1 for f in result.findings if f.path in tests)
    files_hit = Counter(rule for rule, _ in {(f.rule, f.path) for f in result.findings})
    source = args.corpus or f"Python {sys.version.split()[0]} stdlib"
    print(f"corpus: {source}, {len(result.analysed)} files, {lines:,} lines, every line treated as added")
    print(f"static pre-pass: {result.tool or 'ruff not run'} + CodeLens rules ({len(RULES)} rules)")
    print(f"skipped: {len(result.skipped)}, not UTF-8 or unreadable: {unreadable}, notes: {result.notes}")
    total = sum(hits.values())
    print(f"hits: {total:,} ({total / lines * 1000:.2f} per 1,000 lines)")
    for label, n, size in (
        ("in test files", test_hits, test_lines),
        ("in other files", total - test_hits, lines - test_lines),
    ):
        rate = f"{n / size * 1000:.2f} per 1,000 lines" if size else "no lines"
        print(f"  {label}: {n:,} hits in {size:,} lines ({rate})")
    print("rule      hits  per 1,000 lines  files  severity  category")
    for rule, count in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])):
        meta = RULES[rule]
        print(
            f"{rule:<9} {count:>4}  {count / lines * 1000:>15.3f}  {files_hit[rule]:>5}  "
            f"{meta.severity.value:<8}  {meta.category.value}"
        )
    silent = sorted(set(RULES) - set(hits))
    print(f"rules that never fired ({len(silent)}): {', '.join(silent)}")


if __name__ == "__main__":
    main()
