"""Command-line entry point.

``codelens diff`` parses a unified diff and shows what CodeLens would review. ``codelens review`` reviews it
with a model provider (recorded by default) and prints the review, or posts it with ``--post``.
``codelens prompt`` prints exactly what the model would be sent. ``codelens static`` runs only the static
pre-pass (ruff and CodeLens's rules on the Python lines the diff adds). ``codelens eval`` scores all of it
on the eval set of pull requests with seeded bugs.
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
from codelens.diff import DiffParseError, FileDiff, LineKind, PatchSet, Side, decode_diff, parse_patch
from codelens.evals import EvalError, load_cases
from codelens.evals import run as run_evals
from codelens.findings import FindingsFormatError, Severity
from codelens.github import DEFAULT_AUTHOR, GitHubError, plural, post_review, review_payload, summary_body
from codelens.prompts import DEFAULT_MAX_PROMPT_CHARS, MAX_FINDINGS, build_prompt
from codelens.providers import DEFAULT_RECORDINGS, PROVIDERS, Provider, ProviderError, Recorder, make_provider
from codelens.review import Review, review
from codelens.static import StaticResult, analyse, find_ruff

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
    prompt.add_argument("--max-findings", type=positive_int, default=MAX_FINDINGS)
    add_static_arguments(prompt)

    static = sub.add_parser("static", help="run only the static pre-pass on the Python lines a diff adds")
    static.add_argument("file", nargs="?", default="-", help="diff file to read ('-' or omitted: stdin)")
    static.add_argument("--json", action="store_true", help="print machine-readable JSON")
    add_static_arguments(static, switch=False)

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
    rev.add_argument(
        "--min-severity",
        choices=[s.value for s in Severity],
        default=Severity.LOW.value,
        help="leave out findings less severe than this (default: low, keep all)",
    )
    add_static_arguments(rev)

    ev = sub.add_parser("eval", help="score CodeLens on the eval set: precision and recall per source")
    ev.add_argument(
        "--cases", type=Path, default=Path("evals/cases"), help="eval cases (default: evals/cases)"
    )
    ev.add_argument(
        "--recordings", type=Path, default=Path("evals/recordings"), help="default: evals/recordings"
    )
    ev.add_argument("--provider", choices=PROVIDERS, help="model provider (default: recorded)")
    ev.add_argument("--model", help="model name for a live provider")
    ev.add_argument("--record", action="store_true", help="save the live provider's answers to --recordings")
    ev.add_argument("--static-only", action="store_true", help="score only the static pre-pass")
    ev.add_argument("--json", action="store_true", help="print every finding and score as JSON")
    return parser


def add_static_arguments(parser: argparse.ArgumentParser, *, switch: bool = True) -> None:
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("."),
        help="checkout of the diff's new version, read by the static pre-pass (default: .)",
    )
    if switch:
        parser.add_argument("--no-static", action="store_true", help="skip the static pre-pass")


def run_static_pass(args: argparse.Namespace, patch: PatchSet) -> StaticResult | None:
    if getattr(args, "no_static", False):
        return None
    return analyse(patch, args.source_root, ruff=find_ruff())


def static_json(result: StaticResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "tool": result.tool,
        "analysed": result.analysed,
        "skipped": [{"path": p, "reason": r} for p, r in result.skipped],
        "notes": result.notes,
        "outside": result.outside,
        "existing": result.existing,
        "findings": [f.to_dict() for f in result.findings],
    }


def static_lines(result: StaticResult | None) -> list[str]:
    """What the static pre-pass did, for the terminal: nothing when it didn't run or had nothing to check."""
    if result is None:
        return []
    lines = [f"static analysis: {note}" for note in result.notes]
    if result.analysed:
        tool = f"{result.tool} + CodeLens rules" if result.tool else "CodeLens rules"
        lines.append(
            f"static analysis: {tool} on {plural(len(result.analysed), 'Python file')}, "
            f"{plural(len(result.findings), 'finding')} on added lines"
        )
    lines += [f"not checked by static analysis: {path} ({reason})" for path, reason in result.skipped]
    return lines


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
            stream = getattr(sys.stdin, "buffer", None)  # bytes, so the locale can't fail the decode
            text = decode_diff(stream.read()) if stream is not None else sys.stdin.read()
        else:
            text = decode_diff(Path(path).read_bytes())
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
    pre = run_static_pass(args, patch)
    findings = pre.findings if pre is not None else []
    prompt = build_prompt(patch, args.max_prompt_chars, args.max_findings, static=findings)
    out.write(f"=== system ===\n{prompt.request.system}\n=== user ===\n{prompt.request.prompt}")
    out.write(f"=== key {prompt.request.key()} ===\n")
    for path, reason in prompt.skipped:
        print(f"skipped {path}: {reason}", file=err)
    for line in static_lines(pre):
        print(line, file=err)
    return 0


def run_static(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    patch = read_patch(args.file, err)
    if patch is None:
        return 1
    result = analyse(patch, args.source_root, ruff=find_ruff())
    if args.json:
        json.dump(static_json(result), out, indent=2)
        out.write("\n")
        return 0
    for f in result.findings:
        print(f"{f.path}:{f.line}  {f.severity.value}  {f.category.value}  [{f.rule}] {f.title}", file=out)
    for line in static_lines(result):
        print(line, file=out)
    if not result.analysed and not result.skipped:
        print("static analysis: no Python file in the diff adds lines", file=out)
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
        "duplicates": result.duplicates,
        "below_min_severity": result.below,
        "static": static_json(result.static),
        "payload": payload,
    }


def print_review(result: Review, out: TextIO) -> None:
    for f in result.findings:
        origin = f"{f.rule}, " if f.source == "static" else ""
        print(
            f"{f.path}:{f.line}  {f.severity.value}  {f.category.value}  {f.title}  "
            f"({origin}confidence {f.confidence:.2f})",
            file=out,
        )
    n, files = len(result.findings), len(result.reviewed)
    if not result.reviewed:
        print(
            "nothing reviewed: no file in the diff could be shown to the model (no model call made)", file=out
        )
    else:
        print(
            f"{plural(n, 'finding')} on {plural(files, 'reviewed file')} "
            f"({result.provider}: {result.model}, "
            f"{result.usage.input_tokens:,} input / {result.usage.output_tokens:,} output tokens)",
            file=out,
        )
    if result.rejections:
        counts = ", ".join(f"{kind} {count}" for kind, count in result.rejection_counts().items())
        print(f"dropped {len(result.rejections)}: {counts}", file=out)
        for r in result.rejections:
            print(f"  [{r.index}] {r.kind}: {r.detail}", file=out)
    if result.duplicates:
        print(f"merged {plural(result.duplicates, 'duplicate')} (same line and category)", file=out)
    if result.below:
        print(
            f"below {result.min_severity.value} severity: {plural(result.below, 'finding')} left out",
            file=out,
        )
    if result.over_cap:
        print(f"over the cap: {plural(result.over_cap, 'lower-ranked finding')} left out", file=out)
    for path, reason in result.skipped:
        print(f"not reviewed: {path} ({reason})", file=out)
    for line in static_lines(result.static):
        print(line, file=out)


def recordings_dir(args: argparse.Namespace) -> Path:
    """--recordings, else $CODELENS_RECORDINGS, else the default: one answer for replay and record alike."""
    return args.recordings or Path(os.environ.get("CODELENS_RECORDINGS") or DEFAULT_RECORDINGS)


def run_review(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if args.post and (not repo or args.pr is None):
        print("codelens: --post needs --repo (or $GITHUB_REPOSITORY) and --pr", file=err)
        return 2
    patch = read_patch(args.file, err)
    if patch is None:
        return 1
    provider: Provider | None = None
    try:
        recordings = recordings_dir(args)
        provider = make_provider(args.provider, model=args.model, recordings=recordings)
        if args.record:
            if provider.name == "recorded":
                print("codelens: --record needs a live provider (--provider anthropic|openai)", file=err)
                return 2
            try:  # before the paid call, so a bad path can't throw its answer away
                recordings.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"codelens: cannot use {recordings} for recordings: {exc}", file=err)
                return 1
            provider = Recorder(provider, recordings)
        result = review(
            patch,
            provider,
            static=run_static_pass(args, patch),
            max_findings=args.max_findings,
            max_prompt_chars=args.max_prompt_chars,
            min_severity=Severity(args.min_severity),
        )
    except (ProviderError, FindingsFormatError) as exc:
        print(f"codelens: review failed: {exc}", file=err)
        if isinstance(provider, Recorder) and provider.written:
            # The answer was saved before it was checked; replaying it will fail the same way.
            print(f"recorded the unusable answer in {provider.written[-1]}", file=err)
        return 1
    if isinstance(provider, Recorder):
        for path in provider.written:
            print(f"recorded {path}", file=err)
    payload = review_payload(result, args.commit)
    if args.summary_file is not None:
        try:
            with args.summary_file.open("a", encoding="utf-8") as fh:
                fh.write(summary_body(result, details="") + "\n")
        except OSError as exc:  # the review itself is fine; say so and carry on
            print(f"codelens: warning: cannot write the summary to {args.summary_file}: {exc}", file=err)
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
            author=os.environ.get("CODELENS_GITHUB_LOGIN") or DEFAULT_AUTHOR,
        )
    except GitHubError as exc:
        print(f"codelens: {exc}", file=err)
        return 1
    if posted.id is None:
        print(
            f"nothing new to post: an earlier review already posted {plural(posted.repeated, 'finding')}",
            file=err,
        )
        return 0
    where = "as line comments" if posted.inline else "in the review body (GitHub refused the line comments)"
    print(
        f"posted review {posted.id} with {plural(posted.comments, 'finding')} {where}: {posted.url}", file=err
    )
    if posted.repeated:
        print(
            f"not repeated: {plural(posted.repeated, 'finding')} an earlier review already posted", file=err
        )
    return 0


def run_eval(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        cases = load_cases(args.cases)
        provider: Provider | None = None
        if not args.static_only:
            # The eval's own recordings, whatever CODELENS_PROVIDER says: --provider picks a live one.
            provider = make_provider(
                args.provider or "recorded", model=args.model, recordings=args.recordings
            )
            if args.record:
                if provider.name == "recorded":
                    print("codelens: --record needs a live provider (--provider anthropic|openai)", file=err)
                    return 2
                provider = Recorder(provider, args.recordings)
        report = run_evals(cases, provider, ruff=find_ruff())
    except (EvalError, ProviderError, FindingsFormatError) as exc:
        print(f"codelens: eval failed: {exc}", file=err)
        return 1
    if args.json:
        json.dump(report.to_dict(), out, indent=2)
        out.write("\n")
    else:
        out.write(report.markdown())
    missing = len(cases) - len(report.scored("model"))
    if provider is not None and missing:
        print(
            f"{plural(missing, 'case')} without a recorded answer: scored on the static pre-pass only "
            "(record them with --provider anthropic --record)",
            file=err,
        )
    if report.models():
        print(f"model answers from: {', '.join(report.models())}", file=err)
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
    if args.command == "static":
        return run_static(args, sys.stdout, sys.stderr)
    if args.command == "eval":
        return run_eval(args, sys.stdout, sys.stderr)
    if args.command == "review":
        return run_review(args, sys.stdout, sys.stderr)
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
