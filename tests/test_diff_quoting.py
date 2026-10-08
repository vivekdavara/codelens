"""Quoted paths and line splitting. ``git_quoted_paths.diff`` is real ``git diff --cached -M`` output."""

from pathlib import Path

import pytest

from codelens.diff import DiffParseError, FileStatus, parse_patch

FIXTURE = (Path(__file__).parent / "fixtures" / "git_quoted_paths.diff").read_text()


def test_quoted_paths_decode_octal_utf8_quotes_and_tabs() -> None:
    patch = parse_patch(FIXTURE)
    assert [f.path for f in patch] == ["café.txt", "ff.txt", 'said "hi".txt', "tab\tname.txt"]


def test_quoted_rename_lines() -> None:
    f = parse_patch(FIXTURE).get('said "hi".txt')
    assert f is not None
    assert (f.status, f.old_path, f.new_path) == (FileStatus.RENAMED, 'say "hi".txt', 'said "hi".txt')


def test_form_feed_inside_a_line_does_not_split_it() -> None:
    f = parse_patch(FIXTURE).get("ff.txt")
    assert f is not None
    assert [(ln.old_lineno, ln.new_lineno, ln.content) for ln in f.lines()] == [
        (1, None, "page1\fstill line1"),
        (None, 1, "page1\fstill LINE1"),
        (2, 2, "line2"),
    ]


def test_crlf_diff_is_normalised() -> None:
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@ def f():\n-a\n+b\n"
    f = parse_patch(text.replace("\n", "\r\n")).files[0]
    assert f.path == "x.py"
    assert f.hunks[0].section == "def f():"
    assert [ln.content for ln in f.lines()] == ["a", "b"]


def test_crlf_content_lines_in_an_lf_diff_keep_their_carriage_return() -> None:
    text = "--- a/w.bat\n+++ b/w.bat\n@@ -1 +1 @@\n-echo a\r\n+echo b\r\n"
    assert [ln.content for ln in parse_patch(text).files[0].lines()] == ["echo a\r", "echo b\r"]


def test_mixed_quoted_and_unquoted_halves() -> None:
    text = 'diff --git a/plain.txt "b/caf\\303\\251.txt"\nsimilarity index 100%\n'
    text += 'rename from plain.txt\nrename to "caf\\303\\251.txt"\n'
    f = parse_patch(text).files[0]
    assert (f.old_path, f.new_path) == ("plain.txt", "café.txt")


def test_unterminated_quoted_path_is_an_error() -> None:
    with pytest.raises(DiffParseError, match="unterminated quoted path") as exc:
        parse_patch('--- "a/oops\n+++ b/oops\n')
    assert exc.value.lineno == 1
