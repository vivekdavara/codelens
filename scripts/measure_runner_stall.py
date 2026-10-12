"""Time two ways of running a test that leaves a background process behind: reading the child's output
through a pipe (the first, uncommitted version of codelens.sandbox) and the committed one (output to a
file, wait on the child).

    .venv/bin/python scripts/measure_runner_stall.py [seconds]

Two tests start a sleeper of `seconds` (default 30) and return: one with pytest's output capture on (the
default), one inside `capsys.disabled()`. With capture on, the sleeper inherits pytest's capture file, so
the pipe reaches its end when pytest exits. With capture off, the sleeper inherits the pipe, and reading
it to the end waits for the sleeper.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from codelens.sandbox import BOOTSTRAP, run_pytest, scrubbed_env

TESTS = {
    "capture on": "import subprocess, sys\n\n\ndef test_a():\n    subprocess.Popen({cmd})\n",
    "capture off": (
        "import subprocess, sys\n\n\ndef test_a(capsys):\n    with capsys.disabled():\n"
        "        subprocess.Popen({cmd})\n"
    ),
}


def through_a_pipe(test: Path, timeout: float) -> tuple[float, str]:
    """The first version's I/O: the spec on a stdin pipe, the output read with communicate()."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch)
        (home / "pytest.ini").write_text("[pytest]\n")
        args = [str(test), "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", scratch]
        spec = {
            "path": [],
            "include": [],
            "report": str(home / "report.json"),
            "args": [*args, "-c", str(home / "pytest.ini")],
        }
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", BOOTSTRAP],
            cwd=home,
            env=scrubbed_env(home),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            proc.communicate(json.dumps(spec), timeout=timeout)
            how = "returned"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            how = "timed out"
    return time.monotonic() - started, how


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    cmd = repr([sys.executable, "-c", f"import time; time.sleep({seconds})"])
    with tempfile.TemporaryDirectory() as tmp:
        for name, template in TESTS.items():
            test = Path(tmp) / f"test_{name.replace(' ', '_')}.py"
            test.write_text(template.format(cmd=cmd))
            piped, how = through_a_pipe(test, timeout=seconds * 2)
            fixed = run_pytest([test], include=[])
            print(
                f"{name}: pipe {piped:.1f} s ({how}), codelens.sandbox {fixed.seconds:.2f} s "
                f"({fixed.outcome.value}), sleeper {seconds:g} s"
            )


if __name__ == "__main__":
    main()
