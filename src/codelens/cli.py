"""Command-line entry point.

``codelens diff`` parses a unified diff and shows what CodeLens would review: each file's status, line counts
and the new-file ranges a comment can target. ``review`` lands with the review engine (day 2).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from codelens import __version__
from codelens.diff import DiffParseError, FileDiff, LineKind, PatchSet, Side, parse_patch

_STATUS_LETTER = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R", "copied": "C"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codelens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the CodeLens version")
    diff = sub.add_parser("diff", help="parse a unified diff and summarise its reviewable lines")
    diff.add_argument("file", nargs="?", default="-", help="diff file to read ('-' or omitted: stdin)")
    diff.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def _ranges_text(ranges: list[tuple[int, int]]) -> str:
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges) or "-"


def file_summary(f: FileDiff) -> dict[str, Any]:
    added = sum(1 for ln in f.lines() if ln.kind is LineKind.ADDED)
    removed = sum(1 for ln in f.lines() if ln.kind is LineKind.REMOVED)
    return {
        "path": f.path,
        "old_path": f.old_path,
        "new_path": f.new_path,
        "status": f.status.value,
        "binary": f.is_binary,
        "hunks": len(f.hunks),
        "added": added,
        "removed": removed,
        "changed_ranges": [list(r) for r in f.changed_ranges()],
        "commentable_right": len(f.commentable_lines(Side.RIGHT)),
    }


def print_summary(patch: PatchSet, out: TextIO) -> None:
    total_added = total_removed = 0
    for f in patch:
        s = file_summary(f)
        total_added += s["added"]
        total_removed += s["removed"]
        name = f"{f.old_path} -> {f.new_path}" if f.status.value in ("renamed", "copied") else f.path
        detail = "binary" if f.is_binary else f"+{s['added']} -{s['removed']}"
        print(
            f"{_STATUS_LETTER[f.status.value]} {name}  {detail}  changed: {_ranges_text(f.changed_ranges())}",
            file=out,
        )
    print(f"{len(patch)} files, +{total_added} -{total_removed}", file=out)


def run_diff(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        if args.file == "-":
            text = sys.stdin.read()
        else:
            with open(args.file, encoding="utf-8", errors="surrogateescape", newline="") as fh:
                text = fh.read()
        patch = parse_patch(text)
    except OSError as exc:
        print(f"codelens: {exc}", file=err)
        return 1
    except DiffParseError as exc:
        print(f"codelens: invalid diff: {exc}", file=err)
        return 1
    if args.json:
        json.dump({"files": [file_summary(f) for f in patch]}, out, indent=2)
        out.write("\n")
    else:
        print_summary(patch, out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "diff":
        return run_diff(args, sys.stdout, sys.stderr)
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
