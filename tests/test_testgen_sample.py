"""The test-generation sample, end to end: the numbers the README reports.

The candidates are hand-written (no API key here), chosen to exercise every way the filter can decide, so
these numbers measure CodeLens's filtering and accounting, not a model. See evals/testgen/README.md.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codelens.cli import main

SAMPLE = Path("evals/testgen/cases/stock")


def test_the_sample_keeps_five_tests_and_reaches_17_of_18_statements(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["testgen", str(SAMPLE / "pr.diff"), "--source-root", str(SAMPLE / "after"), "--tests", "tests"]
    args += ["--recordings", "evals/testgen/recordings", "--timeout", "2", "--json"]
    assert main(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["model"] == "hand-written"
    verdicts = [(c["index"], c["outcome"], c["kept"]) for c in data["candidates"]]
    assert verdicts == [
        (0, "passed", False),  # a real test of the new parameter, but the suite already runs those lines
        (1, "passed", True),
        (2, "passed", True),
        (3, "passed", False),  # same: a new behaviour on old lines
        (4, "passed", True),
        (5, "passed", True),
        (6, "passed", True),
        (7, "failed", False),  # a wrong expectation
        (8, "timeout", False),  # sleeps, which the prompt forbids
        (9, "error", False),  # exits the process before pytest reports
    ]
    assert [(r["index"], r["kind"]) for r in data["rejections"]] == [
        (10, "unknown-function"),
        (11, "not-python"),
    ]
    assert data["totals"] == {
        "statements": 18,
        "before": 8,
        "after": 17,
        "changed": 17,
        "changed_before": 7,
        "changed_after": 16,
    }
    assert data["baseline"] == {"outcome": "passed", "exit": 0}


def test_the_committed_recording_is_what_the_script_writes(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "scripts/hand_record_tests.py",
        str(SAMPLE / "pr.diff"),
        str(SAMPLE / "answer.json"),
    ]
    command += ["--source-root", str(SAMPLE / "after"), "--out", str(tmp_path)]
    written = Path(subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip())
    committed = Path("evals/testgen/recordings") / written.name
    assert json.loads(written.read_text()) == json.loads(committed.read_text())
