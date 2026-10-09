"""Command-line entry point.

``codelens diff`` parses a unified diff and shows what CodeLens would review. ``codelens review`` reviews it
with a model provider (recorded by default) and prints the review, or posts it with ``--post``.
``codelens prompt`` prints exactly what the model would be sent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from codelens import __version__
from codelens.diff import DiffParseError, FileDiff, LineKind, PatchSet, Side, parse_patch
from codelens.findings import FindingsFormatError
from codelens.github import GitHubError, post_review, review_payload, summary_body
from codelens.prompts import DEFAULT_MAX_PROMPT_CHARS, MAX_FINDINGS, build_prompt
from codelens.providers import DEFAULT_RECORDINGS, PROVIDERS, ProviderError, Recorder, make_provider
from codelens.review import Review, review

_STATUS_LETTER = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R", "copied": "C"}


def positive_int(text: str) -> int:
    """argparse type for limits: a slice by a negative cap would silently keep all but the last findings."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, not {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codelens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the CodeLens version")
    diff = sub.add_parser("diff", help="parse a unified diff and summarise its reviewable lines")
    diff.add_argument("file", nargs="?", default="-", help="diff file to read ('-' or omitted: stdin)")
    diff.add_argument("--json", action="store_true", help="print machine-readable JSON")

    prompt = sub.add_parser("prompt", help="print the system prompt and user prompt a review would send")
    prompt.add_argument("file", nargs="?", default="-", help="diff file to read ('-' or omitted: stdin)")
    prompt.add_argument("--max-prompt-chars", type=positive_int, default=DEFAULT_MAX_PROMPT_CHARS)

    rev = sub.add_parser("review", help="review a diff and print the review (or post it with --post)")
    rev.add_argument("file", nargs="?", default="-", help="diff file to read ('-' or omitted: stdin)")
    rev.add_argument(
        "--provider", choices=PROVIDERS, help="model provider (default: $CODELENS_PROVIDER or recorded)"
    )
    rev.add_argument(
        "--model", help="model name for a live provider (default: $CODELENS_MODEL or the vendor's)"
    )
    rev.add_argument("--recordings", type=Path, help="recordings directory (default: .codelens/recordings)")
    rev.add_argument("--record", action="store_true", help="save the live provider's answer to --recordings")
    rev.add_argument("--max-findings", type=positive_int, default=MAX_FINDINGS)
    rev.add_argument("--max-prompt-chars", type=positive_int, default=DEFAULT_MAX_PROMPT_CHARS)
    rev.add_argument("--json", action="store_true", help="print the review and its payload as JSON")
    rev.add_argument(
        "--summary-file", type=Path, help="append the review as Markdown to this file ($GITHUB_STEP_SUMMARY)"
    )
    rev.add_argument("--post", action="store_true", help="post the review to GitHub (default: dry run)")
    rev.add_argument(
        "--repo", help="owner/name of the pull request's repository (default: $GITHUB_REPOSITORY)"
    )
    rev.add_argument("--pr", type=positive_int, help="pull request number to post to")
    rev.add_argument("--commit", help="head commit SHA the review is for")
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


def read_patch(path: str, err: TextIO) -> PatchSet | None:
    """Parse the diff at ``path`` (``-`` for stdin); print the problem and return ``None`` on failure."""
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
                text = fh.read()
        return parse_patch(text)
    except OSError as exc:
        print(f"codelens: {exc}", file=err)
    except DiffParseError as exc:
        print(f"codelens: invalid diff: {exc}", file=err)
    return None


def run_diff(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    patch = read_patch(args.file, err)
    if patch is None:
        return 1
    if args.json:
        json.dump({"files": [file_summary(f) for f in patch]}, out, indent=2)
        out.write("\n")
    else:
        print_summary(patch, out)
    return 0


def run_prompt(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    patch = read_patch(args.file, err)
    if patch is None:
        return 1
    prompt = build_prompt(patch, args.max_prompt_chars)
    out.write(f"=== system ===\n{prompt.request.system}\n=== user ===\n{prompt.request.prompt}")
    out.write(f"=== key {prompt.request.key()} ===\n")
    for path, reason in prompt.skipped:
        print(f"skipped {path}: {reason}", file=err)
    return 0


def review_json(result: Review, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": result.provider,
        "model": result.model,
        "usage": {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens},
        "reviewed": result.reviewed,
        "skipped": [{"path": p, "reason": r} for p, r in result.skipped],
        "findings": [f.to_dict() for f in result.findings],
        "rejections": [{"index": r.index, "kind": r.kind, "detail": r.detail} for r in result.rejections],
        "over_cap": result.over_cap,
        "payload": payload,
    }


def print_review(result: Review, out: TextIO) -> None:
    for f in result.findings:
        print(
            f"{f.path}:{f.line}  {f.severity.value}  {f.category.value}  {f.title}  "
            f"(confidence {f.confidence:.2f})",
            file=out,
        )
    n, files = len(result.findings), len(result.reviewed)
    model = f"{result.provider}: {result.model}" if result.model else result.provider
    print(
        f"{n} finding{'s' if n != 1 else ''} on {files} reviewed file{'s' if files != 1 else ''} ({model}, "
        f"{result.usage.input_tokens:,} input / {result.usage.output_tokens:,} output tokens)",
        file=out,
    )
    if result.rejections:
        counts = ", ".join(f"{kind} {count}" for kind, count in result.rejection_counts().items())
        print(f"dropped {len(result.rejections)}: {counts}", file=out)
        for r in result.rejections:
            print(f"  [{r.index}] {r.kind}: {r.detail}", file=out)
    if result.over_cap:
        print(f"over the cap: {result.over_cap} lower-ranked findings left out", file=out)
    for path, reason in result.skipped:
        print(f"not reviewed: {path} ({reason})", file=out)


def recordings_dir(args: argparse.Namespace) -> Path:
    return args.recordings or Path(os.environ.get("CODELENS_RECORDINGS") or DEFAULT_RECORDINGS)


def run_review(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if args.post and (not repo or args.pr is None):
        print("codelens: --post needs --repo (or $GITHUB_REPOSITORY) and --pr", file=err)
        return 2
    patch = read_patch(args.file, err)
    if patch is None:
        return 1
    try:
        provider = make_provider(args.provider, model=args.model, recordings=args.recordings)
        if args.record:
            if provider.name == "recorded":
                print("codelens: --record needs a live provider (--provider anthropic|openai)", file=err)
                return 2
            provider = Recorder(provider, recordings_dir(args))
        result = review(
            patch, provider, max_findings=args.max_findings, max_prompt_chars=args.max_prompt_chars
        )
    except (ProviderError, FindingsFormatError) as exc:
        print(f"codelens: review failed: {exc}", file=err)
        return 1
    if isinstance(provider, Recorder):
        for path in provider.written:
            print(f"recorded {path}", file=err)
    payload = review_payload(result, args.commit)
    if args.summary_file is not None:
        with args.summary_file.open("a", encoding="utf-8") as fh:
            fh.write(summary_body(result, details="") + "\n")
    if args.json:
        json.dump(review_json(result, payload), out, indent=2)
        out.write("\n")
    else:
        print_review(result, out)
    if not args.post:
        print("dry run: nothing posted (pass --post to post the review)", file=err)
        return 0
    if not result.findings:
        print("no findings: nothing to post", file=err)
        return 0
    try:
        posted = post_review(
            result,
            repo or "",
            args.pr,
            os.environ.get("GITHUB_TOKEN", ""),
            commit_id=args.commit,
            api_url=os.environ.get("GITHUB_API_URL") or "https://api.github.com",
        )
    except GitHubError as exc:
        print(f"codelens: {exc}", file=err)
        return 1
    where = "as line comments" if posted.inline else "in the review body (GitHub refused the line comments)"
    print(f"posted review {posted.id} with {len(result.findings)} findings {where}: {posted.url}", file=err)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "diff":
        return run_diff(args, sys.stdout, sys.stderr)
    if args.command == "prompt":
        return run_prompt(args, sys.stdout, sys.stderr)
    if args.command == "review":
        return run_review(args, sys.stdout, sys.stderr)
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
