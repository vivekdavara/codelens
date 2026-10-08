import io
import json
from pathlib import Path

import pytest

from codelens import __version__
from codelens.cli import main


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_missing_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


FIXTURES = Path(__file__).parent / "fixtures"


def test_diff_summary(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["diff", str(FIXTURES / "git_extended_headers.diff")]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "A added.txt  +1 -0  changed: 1",
        "A dir with space/a file.txt  +1 -0  changed: 1",
        "A empty.txt  +0 -0  changed: -",
        "D gone.txt  +0 -1  changed: -",
        "M img.bin  binary  changed: -",
        "M keep.txt  +1 -1  changed: 3",
        "R old_name.py -> new_name.py  +1 -1  changed: 4",
        "M run.sh  +0 -0  changed: -",
        "8 files, +4 -3",
    ]


def test_diff_json_from_stdin(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,5 @@\n a\n+b\n+c\n d\n-e\n+f\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert main(["diff", "--json"]) == 0
    (summary,) = json.loads(capsys.readouterr().out)["files"]
    assert summary == {
        "path": "x.py",
        "old_path": "x.py",
        "new_path": "x.py",
        "status": "modified",
        "binary": False,
        "hunks": 1,
        "added": 3,
        "removed": 1,
        "changed_ranges": [[2, 3], [5, 5]],
        "commentable_right": 5,
    }


def test_diff_reports_parse_errors(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.diff"
    bad.write_text("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n")
    assert main(["diff", str(bad)]) == 1
    assert capsys.readouterr().err.strip() == (
        "codelens: invalid diff: line 5: hunk ended early: expected 1 more old and 1 more new lines"
    )


def test_diff_reports_missing_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["diff", str(tmp_path / "nope.diff")]) == 1
    assert "No such file" in capsys.readouterr().err
