"""Extended git headers. The fixtures were produced by real ``git diff --cached -M`` (see each test)."""

from pathlib import Path

from codelens.diff import FileStatus, parse_patch

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_every_file_in_the_git_fixture_is_found() -> None:
    patch = parse_patch(load("git_extended_headers.diff"))
    assert [(f.path, f.status) for f in patch] == [
        ("added.txt", FileStatus.ADDED),
        ("dir with space/a file.txt", FileStatus.ADDED),
        ("empty.txt", FileStatus.ADDED),
        ("gone.txt", FileStatus.DELETED),
        ("img.bin", FileStatus.MODIFIED),
        ("keep.txt", FileStatus.MODIFIED),
        ("new_name.py", FileStatus.RENAMED),
        ("run.sh", FileStatus.MODIFIED),
    ]


def test_added_file() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("added.txt")
    assert f is not None
    assert (f.old_path, f.new_path, f.new_mode) == (None, "added.txt", "100644")
    assert [(ln.new_lineno, ln.content) for ln in f.lines()] == [(1, "new")]


def test_added_empty_file_has_no_hunks_or_dashes() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("empty.txt")
    assert f is not None
    assert (f.old_path, f.new_path, f.hunks) == (None, "empty.txt", [])


def test_path_with_spaces_and_git_trailing_tab() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("dir with space/a file.txt")
    assert f is not None
    assert f.new_path == "dir with space/a file.txt"


def test_deleted_file() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("gone.txt")
    assert f is not None
    assert (f.old_path, f.new_path, f.old_mode) == ("gone.txt", None, "100644")
    assert [(ln.old_lineno, ln.new_lineno) for ln in f.lines()] == [(1, None)]


def test_binary_file_has_no_hunks() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("img.bin")
    assert f is not None
    assert f.is_binary and f.hunks == []


def test_rename_with_edits() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("new_name.py")
    assert f is not None
    assert (f.old_path, f.new_path, f.similarity) == ("old_name.py", "new_name.py", 73)
    assert [ln.content for ln in f.lines()][-2:] == ["delta", "DELTA"]


def test_mode_change_only() -> None:
    f = parse_patch(load("git_extended_headers.diff")).get("run.sh")
    assert f is not None
    assert (f.old_mode, f.new_mode, f.hunks) == ("100644", "100755", [])
    assert f.old_path == f.new_path == "run.sh"


def test_pure_rename_takes_paths_from_rename_lines() -> None:
    # 100% renames have no ---/+++ lines; paths with spaces make the diff --git line ambiguous.
    text = (
        "diff --git a/old dir/x.py b/new dir/x.py\n"
        "similarity index 100%\n"
        "rename from old dir/x.py\n"
        "rename to new dir/x.py\n"
    )
    f = parse_patch(text).files[0]
    assert (f.status, f.old_path, f.new_path, f.similarity) == (
        FileStatus.RENAMED,
        "old dir/x.py",
        "new dir/x.py",
        100,
    )


def test_copy() -> None:
    text = "diff --git a/a.py b/b.py\nsimilarity index 100%\ncopy from a.py\ncopy to b.py\n"
    f = parse_patch(text).files[0]
    assert (f.status, f.old_path, f.new_path) == (FileStatus.COPIED, "a.py", "b.py")


def test_git_binary_patch_body_is_skipped() -> None:
    text = load("git_binary_patch.diff") + load("git_extended_headers.diff")
    patch = parse_patch(text)
    assert patch.files[0].path == "img.bin" and patch.files[0].is_binary
    assert len(patch) == 9  # the binary file + the 8 files after it


def test_plain_diff_u_with_timestamps() -> None:
    text = (
        "--- old/f.txt\t2026-10-08 12:00:00.000000000 -0400\n"
        "+++ new/f.txt\t2026-10-08 12:01:00.000000000 -0400\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "--- /dev/null\t1970-01-01 00:00:00.000000000 +0000\n"
        "+++ new/g.txt\t2026-10-08 12:01:00.000000000 -0400\n"
        "@@ -0,0 +1 @@\n+g\n"
    )
    patch = parse_patch(text)
    assert [(f.old_path, f.new_path, f.status) for f in patch] == [
        ("old/f.txt", "new/f.txt", FileStatus.MODIFIED),
        (None, "new/g.txt", FileStatus.ADDED),
    ]
