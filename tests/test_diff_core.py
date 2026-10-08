import re

import pytest

from codelens.diff import DiffParseError, LineKind, parse_hunk_header, parse_patch

MODIFIED = """\
diff --git a/app/calc.py b/app/calc.py
index 3b18e51..a9c4d2f 100644
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,5 +1,6 @@ import math
 def add(a, b):
-    return a - b
+    return a + b
+
 
 def sub(a, b):
     return a - b
@@ -10,3 +11,3 @@ def mul(a, b):
 def div(a, b):
-    return a / b
+    return a / b if b else 0
 # end
"""


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("@@ -1,5 +1,6 @@", (1, 5, 1, 6, "")),
        ("@@ -1 +1 @@", (1, 1, 1, 1, "")),
        ("@@ -0,0 +1,3 @@", (0, 0, 1, 3, "")),
        ("@@ -7,2 +7 @@ def f(x):", (7, 2, 7, 1, "def f(x):")),
        ("@@ -12,0 +13,2 @@ class A:", (12, 0, 13, 2, "class A:")),
    ],
)
def test_parse_hunk_header(header: str, expected: tuple[int, int, int, int, str]) -> None:
    assert parse_hunk_header(header) == expected


@pytest.mark.parametrize("header", ["@@ -1,5 +1,6", "@@ 1,5 1,6 @@", "@ -1 +1 @", "@@ -a +1 @@"])
def test_parse_hunk_header_rejects_garbage(header: str) -> None:
    with pytest.raises(ValueError):
        parse_hunk_header(header)


def test_modified_file_paths_and_hunks() -> None:
    patch = parse_patch(MODIFIED)
    assert len(patch) == 1
    f = patch.files[0]
    assert (f.old_path, f.new_path, f.path) == ("app/calc.py", "app/calc.py", "app/calc.py")
    assert [(h.old_start, h.old_count, h.new_start, h.new_count) for h in f.hunks] == [
        (1, 5, 1, 6),
        (10, 3, 11, 3),
    ]
    assert f.hunks[0].section == "import math"
    assert f.hunks[1].section == "def mul(a, b):"


def test_line_numbers_follow_both_sides() -> None:
    f = parse_patch(MODIFIED).files[0]
    got = [(ln.kind, ln.old_lineno, ln.new_lineno, ln.content) for ln in f.hunks[0].lines]
    assert got == [
        (LineKind.CONTEXT, 1, 1, "def add(a, b):"),
        (LineKind.REMOVED, 2, None, "    return a - b"),
        (LineKind.ADDED, None, 2, "    return a + b"),
        (LineKind.ADDED, None, 3, ""),
        (LineKind.CONTEXT, 3, 4, ""),
        (LineKind.CONTEXT, 4, 5, "def sub(a, b):"),
        (LineKind.CONTEXT, 5, 6, "    return a - b"),
    ]
    second = [(ln.kind, ln.old_lineno, ln.new_lineno) for ln in f.hunks[1].lines]
    assert second == [
        (LineKind.CONTEXT, 10, 11),
        (LineKind.REMOVED, 11, None),
        (LineKind.ADDED, None, 12),
        (LineKind.CONTEXT, 12, 13),
    ]


def test_github_positions_count_later_hunk_headers() -> None:
    f = parse_patch(MODIFIED).files[0]
    assert [ln.position for ln in f.hunks[0].lines] == [1, 2, 3, 4, 5, 6, 7]
    # Position 8 is the second @@ header itself.
    assert [ln.position for ln in f.hunks[1].lines] == [9, 10, 11, 12]


def test_positions_restart_per_file() -> None:
    second = MODIFIED.replace("app/calc.py", "app/other.py")
    patch = parse_patch(MODIFIED + second)
    assert [f.path for f in patch] == ["app/calc.py", "app/other.py"]
    assert patch.files[1].hunks[0].lines[0].position == 1
    assert patch.get("app/other.py") is patch.files[1]
    assert patch.get("missing.py") is None


def test_no_newline_marker_flags_previous_line() -> None:
    text = """\
--- a/f.txt
+++ b/f.txt
@@ -1,2 +1,2 @@
 keep
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    lines = parse_patch(text).files[0].hunks[0].lines
    assert [(ln.content, ln.no_newline_at_eof) for ln in lines] == [
        ("keep", False),
        ("old", True),
        ("new", True),
    ]
    # Markers occupy positions too.
    assert [ln.position for ln in lines] == [1, 2, 4]


def test_empty_context_line_without_leading_space_is_tolerated() -> None:
    text = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n"
    lines = parse_patch(text).files[0].hunks[0].lines
    assert [(ln.kind, ln.old_lineno, ln.new_lineno) for ln in lines] == [
        (LineKind.CONTEXT, 1, 1),
        (LineKind.CONTEXT, 2, 2),
        (LineKind.REMOVED, 3, None),
        (LineKind.ADDED, None, 3),
    ]


def test_preamble_and_trailing_blank_lines_are_ignored() -> None:
    text = "From abc Mon Sep 17 00:00:00 2001\nSubject: fix\n\n" + MODIFIED + "\n\n"
    assert len(parse_patch(text).files[0].hunks) == 2


def test_format_patch_signature_after_last_hunk() -> None:
    text = "Subject: [PATCH] fix\n---\n" + MODIFIED + "-- \n2.47.0\n"
    assert len(parse_patch(text).files[0].hunks) == 2


def test_empty_input_is_an_empty_patch() -> None:
    assert len(parse_patch("")) == 0


@pytest.mark.parametrize(
    ("text", "message", "lineno"),
    [
        ("@@ -1 +1 @@\n-a\n+b\n", "hunk before any file header", 1),
        ("--- a/f\n@@ -1 +1 @@\n", "expected '+++'", 2),
        ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n", "hunk ended early", 5),
        ("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n a\n-b\n+c\n", "more lines than its header", 5),
        ("--- a/f\n+++ b/f\n@@ -1,2 +1,1 @@\n-a\n+b\n+c\n", "more lines than its header", 6),
        ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n*a\n", "unexpected line in hunk", 4),
        ("--- a/f\n+++ b/f\n@@ -1 +x @@\n", "malformed hunk header", 3),
        ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n\\ No newline at end of file\n", "marker before", 4),
    ],
)
def test_malformed_input_reports_line(text: str, message: str, lineno: int) -> None:
    with pytest.raises(DiffParseError, match=re.escape(message)) as exc:
        parse_patch(text)
    assert exc.value.lineno == lineno
