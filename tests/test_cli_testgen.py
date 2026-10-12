"""`codelens testgen` end to end: a real diff file, a hand-written recording, real pytest runs."""

import json
from pathlib import Path

import pytest
from test_static import git
from test_testgen_generate import AGAIN, B_AND_C, CAPPED, IMPORTS, WRONG, project

from codelens.cli import main
from codelens.diff import decode_diff, parse_patch
from codelens.providers import Completion
from codelens.providers.recorded import HAND_WRITTEN, write_recording
from codelens.testgen import build_prompt, find_targets


def setup(tmp_path: Path, *tests: tuple[str, str]) -> tuple[Path, Path, Path]:
    """A checkout with a staged PR, its diff as a file, and a recording answering it with ``tests``."""
    root = tmp_path / "repo"
    root.mkdir()
    project(root)
    diff = tmp_path / "pr.diff"
    diff.write_bytes(git(root, "diff", "--cached", "-M").encode())
    request = build_prompt(find_targets(parse_patch(decode_diff(diff.read_bytes())), root)).request
    text = json.dumps({"tests": [{"function": f, "code": IMPORTS + c} for f, c in tests]})
    recordings = tmp_path / "recordings"
    write_recording(recordings, request, Completion(text, HAND_WRITTEN), "recorded")
    return root, diff, recordings


def test_testgen_reports_what_it_kept_and_the_coverage_it_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, diff, recordings = setup(tmp_path, B_AND_C, AGAIN, WRONG, CAPPED)
    args = [
        "testgen",
        str(diff),
        "--source-root",
        str(root),
        "--tests",
        "tests",
        "--recordings",
        str(recordings),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "kept    [0] school.grades.grade: passes, and runs 3 new lines of changed functions",
        "dropped [1] school.grades.grade: passes, but runs no new line of a changed function",
        "dropped [2] school.grades.grade: fails",
        "kept    [3] school.grades.curve: passes, and runs 1 new line of changed functions",
        "2 changed functions in 1 file: kept 2 of 4 tests run",
        "tests from recorded: hand-written, 0 input / 0 output tokens",
        "  school.grades.grade: statements run 3 of 7 before, 6 of 7 after",
        "  school.grades.curve: statements run 0 of 1 before, 1 of 1 after",
        "coverage of the changed functions: 3 of 8 statements before, 7 of 8 after (+50.0 points)",
        "  of them on lines the diff adds: 1 of 3 before, 2 of 3 after",
        "existing suite: passed",
    ]


def test_testgen_json_has_every_candidate_with_its_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, diff, recordings = setup(tmp_path, B_AND_C, ("school.grades.nope", "def test_x():\n    pass\n"))
    assert (
        main(["testgen", str(diff), "--source-root", str(root), "--recordings", str(recordings), "--json"])
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert [(c["index"], c["outcome"], c["kept"]) for c in data["candidates"]] == [(0, "passed", True)]
    assert data["candidates"][0]["code"].startswith("import pytest")
    assert data["rejections"] == [
        {
            "index": 1,
            "kind": "unknown-function",
            "detail": "'school.grades.nope' is not a changed function shown",
        }
    ]
    assert data["baseline"] is None and data["totals"]["before"] == 0
    assert [t["function"] for t in data["targets"]] == ["school.grades.grade", "school.grades.curve"]


def test_testgen_writes_the_kept_tests(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, diff, recordings = setup(tmp_path, B_AND_C, WRONG)
    out = tmp_path / "generated"
    args = [
        "testgen",
        str(diff),
        "--source-root",
        str(root),
        "--recordings",
        str(recordings),
        "--out",
        str(out),
    ]
    assert main(args) == 0
    assert [p.name for p in out.iterdir()] == ["test_codelens_school_grades_grade_0.py"]
    assert "def test_b_and_c" in (out / "test_codelens_school_grades_grade_0.py").read_text()
    assert "wrote " in capsys.readouterr().err


def test_a_missing_recording_fails_loudly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, diff, _ = setup(tmp_path)
    assert (
        main(["testgen", str(diff), "--source-root", str(root), "--recordings", str(tmp_path / "none")]) == 1
    )
    assert "test generation failed: no recorded response" in capsys.readouterr().err


def test_record_needs_a_live_provider(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, diff, recordings = setup(tmp_path)
    assert (
        main(["testgen", str(diff), "--source-root", str(root), "--recordings", str(recordings), "--record"])
        == 2
    )
    assert "--record needs a live provider" in capsys.readouterr().err


def test_no_changed_function_means_no_call(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    diff = tmp_path / "docs.diff"
    diff.write_text("diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -1 +1 @@\n-x\n+y\n")
    assert main(["testgen", str(diff), "--recordings", str(tmp_path / "none")]) == 0
    assert "no changed top-level function to test (no model call made)" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_the_timeout_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["testgen", "x.diff", "--timeout", value])
    assert exit_info.value.code == 2
