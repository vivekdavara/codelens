"""The committed eval set is consistent: each diff turns before/ into after/, and every label resolves."""

import json
import re
from pathlib import Path

import pytest

from codelens.diff import FileDiff, FileStatus, LineKind
from codelens.evals import EvalError, load_case, load_cases

CASES = Path(__file__).parent.parent / "evals" / "cases"
NAMES = sorted(p.name for p in CASES.iterdir() if p.is_dir())


def apply(old: list[str], file: FileDiff) -> list[str]:
    """``old`` with ``file``'s hunks applied, checking every context and removed line on the way."""
    new: list[str] = []
    i = 0
    for hunk in file.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        new += old[i:start]
        i = start
        for line in hunk.lines:
            if line.kind is LineKind.ADDED:
                new.append(line.content)
                continue
            assert old[i] == line.content, f"{file.path}: old line {i + 1} differs from the diff"
            i += 1
            if line.kind is LineKind.CONTEXT:
                new.append(line.content)
    return new + old[i:]


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("name", NAMES)
def test_the_diff_turns_before_into_after(name: str) -> None:
    case = load_case(CASES / name)
    before, after = tree(case.directory / "before"), tree(case.root)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert {f.path for f in case.patch} == changed
    for file in case.patch:
        old = [] if file.status is FileStatus.ADDED else before[file.old_path or ""].splitlines()
        assert apply(old, file) == after[file.path].splitlines()


@pytest.mark.parametrize("name", NAMES)
def test_labels_resolve_and_cannot_leak_into_the_prompt(name: str) -> None:
    case = load_case(CASES / name)
    for label in case.labels:
        assert label.line in label.lines
    diff = (case.directory / "pr.diff").read_text().lower()
    assert "bug" not in diff and "seeded" not in diff  # the model sees the diff: no hints in it


def test_the_set_has_seeded_bugs_and_clean_prs() -> None:
    cases = load_cases(CASES)
    assert len(cases) == 14 and sum(len(c.labels) for c in cases) == 23
    assert [c.name for c in cases if not c.labels] == ["clean-median", "clean-slugify"]


def write_case(root: Path, labels: object, diff: str | None = None) -> Path:
    case = root / "case"
    (case / "after").mkdir(parents=True)
    (case / "after" / "m.py").write_text("x = 1\ny = 2\nx = 1\n")
    default = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,3 @@\n x = 1\n+y = 2\n x = 1\n"
    (case / "pr.diff").write_text(diff if diff is not None else default)
    (case / "labels.json").write_text(json.dumps(labels))
    return case


@pytest.mark.parametrize(
    ("bugs", "message"),
    [
        ([{"path": "m.py"}], "a label needs path, quote, category and why"),
        ([{"path": "x.py", "quote": "y", "category": "bug", "why": "w"}], "'x.py' is not in pr.diff"),
        ([{"path": "m.py", "quote": "y", "category": "style", "why": "w"}], "unknown category 'style'"),
        ([{"path": "m.py", "quote": "x = 1", "category": "bug", "why": "w"}], "is on 2 lines of m.py"),
        ([{"path": "m.py", "quote": "z", "category": "bug", "why": "w"}], "is on 0 lines of m.py"),
        ([{"path": "m.py", "quote": "y", "category": "bug", "why": "w", "also": ["nope"]}], "is on 0 lines"),
    ],
)
def test_malformed_labels_are_refused(tmp_path: Path, bugs: list[object], message: str) -> None:
    with pytest.raises(EvalError, match=message):
        load_case(write_case(tmp_path, {"bugs": bugs}))


def test_a_label_must_be_on_a_line_the_diff_shows(tmp_path: Path) -> None:
    diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,3 @@\n a = 1\n+y = 2\n b = 1\n"
    label = {"path": "m.py", "quote": "a = 1", "category": "bug", "why": "w"}
    case = write_case(tmp_path, {"bugs": [label]}, diff)
    (case / "after" / "m.py").write_text("a = 1\ny = 2\nb = 1\nq = 0\n")
    # A shown context line is fine: a change can make an unchanged line wrong.
    assert load_case(case).labels[0].lines == {1}
    (case / "labels.json").write_text(json.dumps({"bugs": [{**label, "quote": "q = 0"}]}))
    with pytest.raises(EvalError, match=re.escape("m.py:4 is not a line the diff shows")):
        load_case(case)
    (case / "labels.json").write_text(json.dumps({"bugs": [{**label, "quote": "y = 2", "also": ["q = 0"]}]}))
    with pytest.raises(EvalError, match=re.escape("an `also` line of m.py:2 is not in the diff")):
        load_case(case)


def test_unreadable_cases_are_refused(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="must be an object"):
        load_case(write_case(tmp_path, ["not", "an", "object"]))
    with pytest.raises(EvalError, match="no eval cases"):
        load_cases(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(EvalError, match="empty: "):
        load_case(tmp_path / "empty")
    (tmp_path / "gone").mkdir()
    label = {"path": "m.py", "quote": "y", "category": "bug", "why": "w"}
    case = write_case(tmp_path / "gone", {"bugs": [label]})
    (case / "after" / "m.py").unlink()
    with pytest.raises(EvalError, match=re.escape("label path 'm.py'")):
        load_case(case)
