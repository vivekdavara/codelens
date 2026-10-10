"""Runs scripts/measure_static_noise.py on CodeLens's own sources: a smoke test of the measurement, and a
guard that CodeLens's code doesn't trip its own rules (if one fires here, the rule is noisy or the code is
wrong)."""

import subprocess
import sys
from pathlib import Path

import pytest

from codelens.static import find_ruff

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from measure_static_noise import added_diff  # noqa: E402

from codelens.diff import parse_patch  # noqa: E402


@pytest.mark.skipif(find_ruff() is None, reason="ruff is a dev dependency")
def test_codelens_does_not_trip_its_own_rules() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/measure_static_noise.py", "--corpus", "src", "--files", "100"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0].startswith("corpus: src, ") and "every line treated as added" in lines[0]
    assert "hits: 0 (0.00 per 1,000 lines)" in lines


@pytest.mark.parametrize("text", ["x = 1\ny = 2\n", "x = 1\ny = 2", "single line no newline", "\n"])
def test_the_added_file_diff_parses_with_every_line_added(text: str) -> None:
    (file,) = parse_patch(added_diff("pkg/m.py", text)).files
    expected = text.split("\n")
    if expected[-1] == "":
        expected.pop()
    assert [line.content for line in file.lines()] == expected
    assert file.added_lines() == list(range(1, len(expected) + 1))
