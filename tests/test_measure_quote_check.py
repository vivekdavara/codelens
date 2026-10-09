"""Runs scripts/measure_quote_check.py on this repo's own sources: a smoke test of the measurement, a check
of its invariant that a correct citation (true line, exact quote) is never rejected, and a floor on how many
near-miss citations the quote check catches."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_measurement_on_our_own_sources() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/measure_quote_check.py", "--corpus", "src", "--files", "8"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    out = result.stdout
    assert "true citations rejected: 0" in out

    def number(pattern: str) -> int:
        match = re.search(pattern, out)
        assert match is not None, pattern
        return int(match.group(1))

    inside = number(r"inside a hunk \(anchoring alone would accept\): (\d+)")
    rejected = number(r"rejected by the quote check: (\d+)")
    accepted = number(r"    accepted: (\d+)")
    assert inside > 0 and rejected + accepted == inside
    # A regression guard on the rule itself: on these files it rejects about 95%, and loosening it (dropping
    # the quote check, or matching any word of the line) would fall far below 90%.
    assert rejected / inside >= 0.9
