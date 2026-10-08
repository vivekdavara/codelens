"""Command-line entry point.

Day 1 ships a single subcommand, ``version``; ``diff`` (parse a unified diff) lands with
the parser and ``review`` with the review engine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from codelens import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codelens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the CodeLens version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
