"""Run pytest under coverage in a child process: scrubbed environment, scratch directory, timeout.

Generated tests are model output steered by the diff, and they import the pull request's code, so running
them runs code the PR author chose. This module is defence in depth, not a sandbox: the child can do
anything the user running CodeLens can. What it rules out is the accidental and the cheap: the child gets an
environment built from an allow-list (no API keys, no ``GITHUB_TOKEN``), runs in a fresh temporary
directory (so files it writes don't land in the checkout), and is killed with its whole process group when
the timeout runs out. See DESIGN.md, "Test generation (day 4)".

The child reports through a JSON file it writes after pytest returns, never through its exit status: a test
can call ``os._exit(0)``, and a missing report then reads as an error, not a pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_TIMEOUT", "Outcome", "RunResult", "run_pytest", "scrubbed_env"]

DEFAULT_TIMEOUT = 60.0
MAX_OUTPUT = 4_000
"""Characters of the child's output kept for the report (its tail, where pytest puts the failures)."""

# Variables a test run may see. Everything else (API keys, GITHUB_TOKEN, ACTIONS_* tokens, cloud
# credentials) stays out: an allow-list, because a deny-list misses the secret nobody thought of.
_PASSED_THROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")

# Runs in the child as `python -I -c BOOTSTRAP`, reading its instructions from stdin. coverage and pytest
# are imported before the project's directories go on sys.path, so files in the project can't stand in
# for them.
BOOTSTRAP = r"""
import json, sys
spec = json.loads(sys.stdin.read())
sys.stdin.close()
import coverage, pytest
sys.path[:0] = spec["path"]
cov = coverage.Coverage(data_file=None, include=spec["include"], config_file=False)
cov.start()
try:
    code = int(pytest.main(spec["args"]))
finally:
    cov.stop()
files = {}
for path in spec["include"]:
    try:
        _, statements, _, missing, _ = cov.analysis2(path)
    except Exception:
        continue
    files[path] = {"statements": sorted(statements), "missing": sorted(missing)}
with open(spec["report"], "w", encoding="utf-8") as fh:
    json.dump({"exit": code, "files": files}, fh)
"""


class Outcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    """pytest ran and a test failed, or nothing was collected."""
    TIMEOUT = "timeout"
    ERROR = "error"
    """The child ended without a report: a crash, an early exit, or pytest itself missing."""


@dataclass
class RunResult:
    outcome: Outcome
    executed: dict[str, set[int]] = field(default_factory=dict)
    """Lines run, per measured file (absolute paths as given in ``include``)."""
    statements: dict[str, set[int]] = field(default_factory=dict)
    """Executable lines, per measured file, as coverage counts them."""
    exit_code: int | None = None
    """pytest's exit status, when it reported one."""
    output: str = ""
    """The tail of the child's stdout and stderr."""
    seconds: float = 0.0


def scrubbed_env(
    home: Path, *, base: Mapping[str, str] | None = None, plugins: bool = False
) -> dict[str, str]:
    """The child's whole environment: a few harmless variables from ``base`` and a scratch ``HOME``.

    ``plugins=False`` stops pytest loading every installed plugin: generated tests are told to need only
    pytest and the standard library, and an installed plugin (pytest-cov, say) would change the run.
    """
    base = os.environ if base is None else base
    env = {name: base[name] for name in _PASSED_THROUGH if name in base}
    env.setdefault("PATH", os.defpath)
    env.update(HOME=str(home), TMPDIR=str(home), PYTHONUTF8="1", NO_COLOR="1")
    if not plugins:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "…" + text[-MAX_OUTPUT:]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    # The test's own children too, not just the interpreter; the group may already be gone.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def run_pytest(
    tests: Sequence[Path],
    *,
    include: Sequence[Path],
    path: Sequence[Path] = (),
    cwd: Path | None = None,
    python: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    plugins: bool = False,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run pytest on ``tests`` in a child process and measure line coverage of the files in ``include``.

    ``path`` goes in front of the child's ``sys.path`` (the project's import roots). The child runs in
    ``cwd``, a fresh temporary directory when ``None``, which is also its ``HOME`` and ``TMPDIR``. A scratch
    run gets an empty pytest config of its own, so no ``pytest.ini`` or ``pyproject.toml`` of the project
    applies; with ``cwd`` (the project's own suite) the project's config is used. Either way the project's
    ``addopts`` are dropped, pytest-cov is blocked (its tracer would fight this one) and the cache plugin is
    off, so nothing is written beside the tests. ``python`` is the
    interpreter (default: this one), which must have pytest and coverage installed.
    ``env`` is the environment the allow-list is drawn from (default: this process's).
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codelens-run-") as scratch:
        home = Path(scratch)
        report = home / "codelens-report.json"
        workdir = cwd or home
        args = [*(str(t) for t in tests), "-q", "-p", "no:cacheprovider", "-p", "no:cov", "-o", "addopts="]
        args += ["--rootdir", str(workdir)]
        if cwd is None:
            # pytest finds its config from the tests' common ancestor, whatever --rootdir says: an empty
            # one of our own keeps any pytest.ini or pyproject.toml above the tests out of a scratch run.
            (home / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            args += ["-c", str(home / "pytest.ini")]
        spec: dict[str, Any] = {
            "path": [str(p) for p in path],
            "include": [str(p) for p in include],
            "report": str(report),
            "args": args,
        }
        (home / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        # Output goes to a file, not a pipe: a process the tests leave running in the background would hold
        # a pipe open, and reading it to the end would wait for that process instead of for pytest.
        with (home / "spec.json").open("rb") as stdin, (home / "output.txt").open("wb") as out:
            proc = subprocess.Popen(
                [python or sys.executable, "-I", "-c", BOOTSTRAP],
                cwd=workdir,
                env=scrubbed_env(home, base=env, plugins=plugins),
                stdin=stdin,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
            _kill_group(proc)  # on a timeout, and anything the tests started in the background
            proc.wait()
        output = (home / "output.txt").read_bytes().decode("utf-8", "replace")
        seconds = time.monotonic() - started
        if timed_out:
            return RunResult(Outcome.TIMEOUT, output=_tail(output), seconds=seconds)
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return RunResult(Outcome.ERROR, exit_code=None, output=_tail(output), seconds=seconds)
    return _parse(data, output, seconds)


def _lines(value: Any) -> set[int]:
    return {n for n in value if type(n) is int} if isinstance(value, list) else set()


def _parse(data: Any, output: str, seconds: float) -> RunResult:
    if not isinstance(data, dict) or type(data.get("exit")) is not int:
        return RunResult(Outcome.ERROR, output=_tail(output), seconds=seconds)
    code: int = data["exit"]
    result = RunResult(
        Outcome.PASSED if code == 0 else Outcome.FAILED, exit_code=code, output=_tail(output), seconds=seconds
    )
    files = data.get("files")
    for name, info in (files if isinstance(files, dict) else {}).items():
        if isinstance(name, str) and isinstance(info, dict):
            statements = _lines(info.get("statements"))
            result.statements[name] = statements
            result.executed[name] = statements - _lines(info.get("missing"))
    return result
