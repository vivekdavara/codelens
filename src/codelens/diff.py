"""Unified-diff parser that maps every hunk line to its old and new file line numbers.

Everything CodeLens posts is anchored through this module: a review comment is only placed on a line that
:meth:`FileDiff.anchor` can find. See DESIGN.md, "The diff parser".
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "DiffLine",
    "DiffParseError",
    "FileDiff",
    "Hunk",
    "LineKind",
    "PatchSet",
    "parse_hunk_header",
    "parse_patch",
]


class DiffParseError(ValueError):
    """The input is not a well-formed unified diff. ``lineno`` is 1-based."""

    def __init__(self, message: str, lineno: int) -> None:
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


class LineKind(Enum):
    CONTEXT = " "
    ADDED = "+"
    REMOVED = "-"


@dataclass
class DiffLine:
    kind: LineKind
    content: str
    old_lineno: int | None
    new_lineno: int | None
    position: int
    """GitHub's legacy diff position: lines below the file's first ``@@``, counting later ``@@`` headers."""
    no_newline_at_eof: bool = False


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    old_path: str | None
    """``None`` when the file is added (``--- /dev/null``)."""
    new_path: str | None
    """``None`` when the file is deleted (``+++ /dev/null``)."""
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        """The path a reviewer refers to: the new path, or the old one for a deleted file."""
        path = self.new_path if self.new_path is not None else self.old_path
        assert path is not None
        return path

    def lines(self) -> Iterator[DiffLine]:
        for hunk in self.hunks:
            yield from hunk.lines


@dataclass
class PatchSet:
    files: list[FileDiff] = field(default_factory=list)

    def __iter__(self) -> Iterator[FileDiff]:
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def get(self, path: str) -> FileDiff | None:
        """The file diff whose new path (or old path, for deletions) is ``path``."""
        return next((f for f in self.files if f.path == path), None)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_NO_NEWLINE = "\\ No newline at end of file"
_SIGNATURE = "-- "  # git format-patch's signature separator


def parse_hunk_header(line: str) -> tuple[int, int, int, int, str]:
    """Parse ``@@ -a[,b] +c[,d] @@ section`` into ``(a, b, c, d, section)``; omitted counts are 1."""
    match = _HUNK_HEADER.match(line)
    if match is None:
        raise ValueError(f"not a hunk header: {line!r}")
    old_start, old_count, new_start, new_count, section = match.groups()
    return (
        int(old_start),
        1 if old_count is None else int(old_count),
        int(new_start),
        1 if new_count is None else int(new_count),
        section,
    )


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix) :] if path.startswith(prefix) else path


def _file_header_path(rest: str, prefix: str) -> str | None:
    """The path from a ``---``/``+++`` line (``rest`` excludes the marker); ``None`` for /dev/null."""
    path = rest.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    return _strip_prefix(path, prefix)


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.i = 0
        self.patch = PatchSet()

    def error(self, message: str) -> DiffParseError:
        return DiffParseError(message, self.i + 1)

    def peek(self) -> str | None:
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse(self) -> PatchSet:
        while (line := self.peek()) is not None:
            if line.startswith("diff --git "):
                self.parse_git_file()
            elif line.startswith("--- "):
                self.parse_file_headers(FileDiff(None, None))
            elif line.startswith("@@"):
                raise self.error("hunk before any file header")
            else:
                # Preamble (commit message, email headers) and blank lines between files.
                self.i += 1
        return self.patch

    def parse_git_file(self) -> None:
        self.i += 1
        file = FileDiff(None, None)
        while (line := self.peek()) is not None and not line.startswith(("--- ", "diff --git ")):
            if line.startswith("@@"):
                raise self.error("hunk without ---/+++ file headers")
            self.i += 1
        if line is not None and line.startswith("--- "):
            self.parse_file_headers(file)
        else:
            self.patch.files.append(file)

    def parse_file_headers(self, file: FileDiff) -> None:
        old_line = self.lines[self.i]
        self.i += 1
        new_line = self.peek()
        if new_line is None or not new_line.startswith("+++ "):
            raise self.error("expected '+++' after '---'")
        self.i += 1
        file.old_path = _file_header_path(old_line[4:], "a/")
        file.new_path = _file_header_path(new_line[4:], "b/")
        self.patch.files.append(file)
        self.parse_hunks(file)

    def parse_hunks(self, file: FileDiff) -> None:
        position = 0
        while (line := self.peek()) is not None and line.startswith("@@"):
            try:
                old_start, old_count, new_start, new_count, section = parse_hunk_header(line)
            except ValueError:
                raise self.error(f"malformed hunk header {line!r}") from None
            if file.hunks:
                position += 1  # later @@ headers occupy a position
            self.i += 1
            hunk = Hunk(old_start, old_count, new_start, new_count, section)
            position = self.parse_hunk_body(hunk, position)
            file.hunks.append(hunk)
        line = self.peek()
        if (
            file.hunks
            and line is not None
            and line.startswith((" ", "+", "-"))
            and not line.startswith("--- ")
            and line != _SIGNATURE
        ):
            # A body line right after a complete hunk means the header's counts are wrong.
            raise self.error("hunk has more lines than its header declares")

    def parse_hunk_body(self, hunk: Hunk, position: int) -> int:
        old_no, new_no = hunk.old_start, hunk.new_start
        old_left, new_left = hunk.old_count, hunk.new_count
        while old_left > 0 or new_left > 0:
            line = self.peek()
            if line is None:
                raise self.error(
                    f"hunk ended early: expected {old_left} more old and {new_left} more new lines"
                )
            position += 1
            if line == _NO_NEWLINE:
                self.mark_no_newline(hunk)
                self.i += 1
                continue
            prefix, content = line[:1], line[1:]
            if line == "":
                # Some tools strip the single space from empty context lines.
                prefix, content = " ", ""
            if prefix == " ":
                diff_line = DiffLine(LineKind.CONTEXT, content, old_no, new_no, position)
                old_no, new_no, old_left, new_left = old_no + 1, new_no + 1, old_left - 1, new_left - 1
            elif prefix == "-":
                diff_line = DiffLine(LineKind.REMOVED, content, old_no, None, position)
                old_no, old_left = old_no + 1, old_left - 1
            elif prefix == "+":
                diff_line = DiffLine(LineKind.ADDED, content, None, new_no, position)
                new_no, new_left = new_no + 1, new_left - 1
            else:
                raise self.error(f"unexpected line in hunk: {line!r}")
            if old_left < 0 or new_left < 0:
                raise self.error("hunk has more lines than its header declares")
            hunk.lines.append(diff_line)
            self.i += 1
        # The no-newline marker may follow the hunk's last counted line.
        if self.peek() == _NO_NEWLINE:
            self.mark_no_newline(hunk)
            self.i += 1
            position += 1
        return position

    def mark_no_newline(self, hunk: Hunk) -> None:
        if not hunk.lines:
            raise self.error("'No newline at end of file' marker before any hunk line")
        hunk.lines[-1].no_newline_at_eof = True


def parse_patch(text: str) -> PatchSet:
    """Parse ``git diff`` / GitHub ``.diff`` / ``diff -u`` output.

    Raises :class:`DiffParseError` on malformed input rather than guessing line numbers.
    """
    return _Parser(text).parse()
