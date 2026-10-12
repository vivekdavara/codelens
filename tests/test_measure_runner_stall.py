"""Runs scripts/measure_runner_stall.py with a short sleeper: the measurement must still measure.

The script's pipe runner reuses today's sandbox BOOTSTRAP. When the bootstrap started reading two more
keys, the script's child failed at once with a KeyError, the pipe "returned" in 0.1 s, and the script
printed that as if the stall had gone; this test would have failed.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from measure_runner_stall import measure  # noqa: E402

SECONDS = 3.0


def test_the_pipe_stalls_only_with_capture_off_and_the_sandbox_never_does() -> None:
    on, off = measure(SECONDS)
    pattern = r"pipe ([\d.]+) s \(returned, 1 passed\), codelens.sandbox ([\d.]+) s \(passed\)"
    on_pipe, on_sandbox = map(float, re.search(pattern, on).groups())  # type: ignore[union-attr]
    off_pipe, off_sandbox = map(float, re.search(pattern, off).groups())  # type: ignore[union-attr]
    assert on.startswith("capture on:") and off.startswith("capture off:")
    # The pipe waits for the sleeper only when the test turned capture off; the sandbox never waits.
    assert off_pipe >= SECONDS
    assert on_pipe < SECONDS and on_sandbox < SECONDS and off_sandbox < SECONDS
