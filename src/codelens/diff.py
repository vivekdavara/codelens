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
    "FileStatus",
    "Hunk",
    "LineKind",
    "PatchSet",
    "Side",
    "parse_hunk_header",
    "parse_patch",
]


class DiffParseError(ValueError):
    """The input is not a well-formed unified diff. ``lineno`` is 1-based."""

    def __init__(self, message: str, lineno: int) -> None:
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


class FileStatus(Enum):
    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"


class Side(Enum):
    """Which file version a review comment refers to, as GitHub's Reviews API names them."""

    LEFT = "LEFT"  # old file: removed and context lines
    RIGHT = "RIGHT"  # new file: added and context lines


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

    @property
    def header(self) -> str:
        section = f" {self.section}" if self.section else ""
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@{section}"

    def render_numbered(self) -> str:
        """The hunk as diff text with new-file line numbers in a left margin, for prompts.

        Removed lines have a blank margin, so a model citing a number always cites a new-file (RIGHT) line.
        """
        width = len(str(self.new_start + self.new_count))
        out = [self.header]
        for line in self.lines:
            margin = str(line.new_lineno).rjust(width) if line.new_lineno is not None else " " * width
            out.append(f"{margin} {line.kind.value}{line.content}")
        return "\n".join(out)


@dataclass
class FileDiff:
    old_path: str | None
    """``None`` when the file is added (``--- /dev/null``)."""
    new_path: str | None
    """``None`` when the file is deleted (``+++ /dev/null``)."""
    status: FileStatus = FileStatus.MODIFIED
    old_mode: str | None = None
    new_mode: str | None = None
    similarity: int | None = None
    """Percent from ``similarity index`` (renames/copies) or ``dissimilarity index`` (rewrites)."""
    is_binary: bool = False
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

    def anchor(self, line: int, side: Side = Side.RIGHT) -> DiffLine | None:
        """The diff line a review comment at ``(line, side)`` would attach to, or ``None``.

        GitHub only accepts comments on lines shown in a hunk (context included). CodeLens drops findings that
        don't anchor rather than moving them; see DESIGN.md, "Line mapping".
        """
        for diff_line in self.lines():
            number = diff_line.new_lineno if side is Side.RIGHT else diff_line.old_lineno
            if number == line:
                return diff_line
        return None

    def commentable_lines(self, side: Side = Side.RIGHT) -> set[int]:
        """Every line number on ``side`` that :meth:`anchor` accepts."""
        attr = "new_lineno" if side is Side.RIGHT else "old_lineno"
        return {n for ln in self.lines() if (n := getattr(ln, attr)) is not None}

    def added_lines(self) -> list[int]:
        """New-file line numbers of added lines, ascending."""
        return [ln.new_lineno for ln in self.lines() if ln.kind is LineKind.ADDED and ln.new_lineno]

    def changed_ranges(self) -> list[tuple[int, int]]:
        """Added lines collapsed into inclusive ``(first, last)`` new-file ranges."""
        ranges: list[tuple[int, int]] = []
        for n in self.added_lines():
            if ranges and ranges[-1][1] == n - 1:
                ranges[-1] = (ranges[-1][0], n)
            else:
                ranges.append((n, n))
        return ranges


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


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _read_quoted(text: str) -> tuple[str, str]:
    """Decode a C-style quoted path at the start of ``text`` (git's ``core.quotePath`` form).

    Returns the decoded path and the rest of ``text`` after the closing quote. Octal escapes are bytes, so
    ``"caf\\303\\251"`` decodes as UTF-8 to ``café``.
    """
    out = bytearray()
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return out.decode("utf-8", errors="replace"), text[i + 1 :]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if text[i + 1 : i + 4].isdigit() and len(text[i + 1 : i + 4]) == 3:
                out.append(int(text[i + 1 : i + 4], 8) & 0xFF)
                i += 4
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    raise ValueError(f"unterminated quoted path: {text!r}")


def _maybe_unquote(path: str) -> str:
    return _read_quoted(path)[0] if path.startswith('"') else path


def _file_header_path(rest: str, prefix: str) -> str | None:
    """The path from a ``---``/``+++`` line (``rest`` excludes the marker); ``None`` for /dev/null."""
    if rest.startswith('"'):
        return _strip_prefix(_read_quoted(rest)[0], prefix)
    path = rest.split("\t", 1)[0]  # plain diff -u appends a tab and a timestamp
    if path == "/dev/null":
        return None
    return _strip_prefix(path, prefix)


def _split_git_header(rest: str) -> tuple[str | None, str | None]:
    """Split ``a/<old> b/<new>`` from a ``diff --git`` line; ``(None, None)`` if ambiguous.

    With unquoted paths containing spaces the split is only certain when both halves name the same path, so
    other sources (rename lines, ``---``/``+++``) take precedence; see DESIGN.md, "Path resolution".
    """
    if rest.startswith('"'):
        old, tail = _read_quoted(rest)
        new = _maybe_unquote(tail.lstrip(" "))
        return _strip_prefix(old, "a/"), _strip_prefix(new, "b/")
    if rest.endswith('"') and ' "' in rest:
        old, quoted_new = rest.rsplit(' "', 1)
        return _strip_prefix(old, "a/"), _strip_prefix(_read_quoted('"' + quoted_new)[0], "b/")
    if len(rest) % 2 == 1:
        mid = len(rest) // 2
        old, new = rest[:mid], rest[mid + 1 :]
        if old.startswith("a/") and new.startswith("b/") and old[2:] == new[2:]:
            return old[2:], new[2:]
    if rest.startswith("a/") and rest.count(" b/") == 1:
        old, new = rest.split(" b/")
        return old[2:], new
    return None, None


class _Parser:
    def __init__(self, text: str) -> None:
        # Split on "\n" only: str.splitlines() would also break on form feeds and other characters that
        # legitimately appear inside source lines. A CRLF diff keeps "\r" out of headers and paths.
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        if self.lines and all(line.endswith("\r") for line in self.lines):
            self.lines = [line[:-1] for line in self.lines]
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
                self.parse_plain_file()
            elif line.startswith("@@"):
                raise self.error("hunk before any file header")
            else:
                # Preamble (commit message, email headers) and blank lines between files.
                self.i += 1
        return self.patch

    def parse_plain_file(self) -> None:
        file = FileDiff(None, None)
        self.read_file_headers(file)
        file.status = (
            FileStatus.ADDED
            if file.old_path is None
            else FileStatus.DELETED
            if file.new_path is None
            else FileStatus.MODIFIED
        )
        self.patch.files.append(file)
        self.parse_hunks(file)

    def parse_git_file(self) -> None:
        try:
            header_old, header_new = _split_git_header(self.lines[self.i][len("diff --git ") :])
        except ValueError as exc:
            raise self.error(str(exc)) from None
        self.i += 1
        file = FileDiff(header_old, header_new)
        rename_from = rename_to = None
        while (line := self.peek()) is not None:
            if line.startswith(("old mode ", "deleted file mode ")):
                file.old_mode = line.rsplit(" ", 1)[1]
                if line.startswith("deleted"):
                    file.status = FileStatus.DELETED
            elif line.startswith(("new mode ", "new file mode ")):
                file.new_mode = line.rsplit(" ", 1)[1]
                if line.startswith("new file"):
                    file.status = FileStatus.ADDED
            elif line.startswith(("similarity index ", "dissimilarity index ")):
                file.similarity = int(line.rsplit(" ", 1)[1].rstrip("%"))
            elif line.startswith(("rename from ", "copy from ")):
                rename_from = _maybe_unquote(line.split(" ", 2)[2])
                file.status = FileStatus.RENAMED if line.startswith("rename") else FileStatus.COPIED
            elif line.startswith(("rename to ", "copy to ")):
                rename_to = _maybe_unquote(line.split(" ", 2)[2])
            elif line.startswith("index "):
                pass
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                file.is_binary = True
                if line == "GIT binary patch":
                    # Base85 literal/delta blocks run until the next file.
                    while (nxt := self.peek()) is not None and not nxt.startswith("diff --git "):
                        self.i += 1
                    continue
            elif line.startswith("@@"):
                raise self.error("hunk without ---/+++ file headers")
            else:
                break  # '---', the next file, or trailing noise
            self.i += 1

        if (line := self.peek()) is not None and line.startswith("--- "):
            self.read_file_headers(file)
        # Path precedence: rename/copy lines, then ---/+++ (already applied), then the diff --git line.
        if rename_from is not None:
            file.old_path = rename_from
        if rename_to is not None:
            file.new_path = rename_to
        if file.status is FileStatus.ADDED:
            file.old_path = None
        elif file.status is FileStatus.DELETED:
            file.new_path = None
        if file.old_path is None and file.new_path is None:
            raise self.error("cannot determine the file path from 'diff --git' header")
        self.patch.files.append(file)
        self.parse_hunks(file)

    def read_file_headers(self, file: FileDiff) -> None:
        file.old_path = self.header_path(self.lines[self.i][4:], "a/")
        self.i += 1
        new_line = self.peek()
        if new_line is None or not new_line.startswith("+++ "):
            raise self.error("expected '+++' after '---'")
        file.new_path = self.header_path(new_line[4:], "b/")
        self.i += 1

    def header_path(self, rest: str, prefix: str) -> str | None:
        try:
            return _file_header_path(rest, prefix)
        except ValueError as exc:
            raise self.error(str(exc)) from None

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
