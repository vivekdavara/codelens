"""Run pytest under coverage in a child process: scrubbed environment, scratch directory, timeout.

Generated tests are model output steered by the diff, and they import the pull request's code, so running
them runs code the PR author chose. This module is defence in depth, not a sandbox: the child can do
anything the user running CodeLens can. What it rules out is the accidental and the cheap: the child gets an
environment built from an allow-list (no API keys, no ``GITHUB_TOKEN``), runs in a fresh temporary
directory (so files it writes don't land in the checkout, and nothing one run leaves affects the next), and
is killed with its whole process group when the run ends, however it ends. POSIX only (process groups).
See DESIGN.md, "Running a candidate".

The child reports through a JSON file it writes after pytest returns, never through its exit status: a test
can call ``os._exit(0)``, and a missing report then reads as an error, not a pass. The report counts each
test's outcome, because pytest's exit status 0 also means "everything skipped" or "failed as expected".
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
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

__all__ = ["COUNTS", "DEFAULT_TIMEOUT", "Outcome", "RunResult", "run_pytest", "scrubbed_env"]

DEFAULT_TIMEOUT = 60.0
MAX_OUTPUT = 4_000
"""Characters of the child's output kept for the report (its tail, where pytest puts the failures)."""

# Variables a test run may see. Everything else (API keys, GITHUB_TOKEN, ACTIONS_* tokens, cloud
# credentials) stays out: an allow-list, because a deny-list misses the secret nobody thought of.
_PASSED_THROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")

COUNTS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
"""What the child counts, from pytest's reports: ``errors`` are failures outside a test's call (setup,
teardown, collection)."""

# Runs in the child as `python -I -B -X utf8 -c BOOTSTRAP`, reading its instructions from stdin. coverage and
# pytest are imported before the project's directories go on sys.path, so files in the project can't stand
# in for them. After the report is written the process ends at once: a thread or an atexit hook a test
# left behind would otherwise keep it alive until the timeout, and the finished report would be thrown away.
BOOTSTRAP = r"""
import json, os, sys
spec = json.loads(sys.stdin.read())
sys.stdin.close()
import coverage, pytest


class Count:
    def __init__(self):
        self.n = dict.fromkeys(spec["counts"], 0)

    def pytest_collectreport(self, report):
        if report.failed:
            self.n["errors"] += 1

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.n["failed" if report.when == "call" else "errors"] += 1
        elif hasattr(report, "wasxfail"):
            self.n["xfailed" if report.skipped else "xpassed"] += 1
        elif report.skipped:
            self.n["skipped"] += 1
        elif report.when == "call":
            self.n["passed"] += 1


count = Count()
sys.path[:0] = spec["path"]
cov = coverage.Coverage(data_file=None, source=spec["source"], config_file=False)
cov.start()
try:
    code = int(pytest.main(spec["args"], plugins=[count]))
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
    json.dump({"exit": code, "counts": count.n, "files": files}, fh)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
"""


class Outcome(Enum):
    PASSED = "passed"
    """pytest exited 0 and every test it ran passed: at least one, none skipped, none xfailed or xpassed."""
    FAILED = "failed"
    """Anything else pytest reported: a failure, nothing collected, or nothing that passed cleanly."""
    TIMEOUT = "timeout"
    ERROR = "error"
    """No report: the interpreter couldn't start, crashed, exited early, or lacks pytest or coverage."""


@dataclass
class RunResult:
    outcome: Outcome
    executed: dict[str, set[int]] = field(default_factory=dict)
    """Lines run, per measured file (absolute paths as given in ``include``)."""
    statements: dict[str, set[int]] = field(default_factory=dict)
    """Executable lines, per measured file, as coverage counts them."""
    exit_code: int | None = None
    """pytest's exit status, when it reported one."""
    counts: dict[str, int] = field(default_factory=dict)
    """How many tests ended each way (:data:`COUNTS`), when pytest reported."""
    output: str = ""
    """The tail of the child's stdout and stderr."""
    seconds: float = 0.0


def scrubbed_env(
    home: Path, *, base: Mapping[str, str] | None = None, plugins: bool = False
) -> dict[str, str]:
    """The child's whole environment: a few harmless variables from ``base`` and a scratch ``HOME``.

    ``plugins=False`` stops pytest loading every installed plugin: generated tests are told to need only
    pytest and the standard library, and an installed plugin (pytest-cov, say) would change the run.
    (``PYTHON*`` variables would be ignored anyway: the child runs with ``-I``.)
    """
    base = os.environ if base is None else base
    env = {name: base[name] for name in _PASSED_THROUGH if name in base}
    env.setdefault("PATH", os.defpath)
    env.update(HOME=str(home), TMPDIR=str(home), NO_COLOR="1")
    if not plugins:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def interpreter(python: str | None) -> str:
    """An absolute path for ``python`` (default: this interpreter), since the child runs in another directory.

    A bare name is looked up on ``PATH``; a path is made absolute without resolving symlinks, which would
    turn a virtualenv's ``bin/python`` into the base interpreter and lose the virtualenv.
    """
    python = python or sys.executable
    if os.sep in python or (os.altsep and os.altsep in python):
        return os.path.abspath(python)
    return shutil.which(python) or python


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
    project: Path | None = None,
    python: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    plugins: bool = False,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run pytest on ``tests`` in a child process and measure line coverage of the files in ``include``.

    ``path`` goes in front of the child's ``sys.path`` (the project's import roots).

    - With ``project`` (the checkout), ``tests`` are the project's own suite: they run in place, with the
      project's pytest config and conftest files.
    - Without it, ``tests`` are copied into a fresh temporary directory and run there with an empty pytest
      config of their own and no conftest files, so no ``pytest.ini`` above them applies and nothing an
      earlier run wrote can change this one.

    The child's working directory is also its ``HOME`` and ``TMPDIR``. Either way the project's
    ``addopts`` are dropped, pytest-cov is blocked (its tracer would fight this one), the cache plugin is
    off and no bytecode is written. ``python`` is the interpreter (default: this one), which must have
    pytest and coverage installed. ``env`` is the environment the allow-list is drawn from (default: this
    process's). Never raises for a problem with the child: those become ``ERROR`` or ``TIMEOUT``.
    """
    started = time.monotonic()
    measured = [os.path.abspath(p) for p in include]
    exe = interpreter(python)
    # The runner's own files live apart from the child's directory, so a test that tidies its temporary
    # directory can't delete the output or the instructions.
    with (
        tempfile.TemporaryDirectory(prefix="codelens-run-") as own_dir,
        tempfile.TemporaryDirectory(prefix="codelens-work-") as work_dir,
    ):
        own, work = Path(own_dir), Path(work_dir)
        workdir = project or work
        targets = [str(t) for t in tests]
        args = ["-q", "-p", "no:cacheprovider", "-p", "no:cov", "-o", "addopts=", "--rootdir", str(workdir)]
        if project is None:
            targets = []
            for test in tests:
                copy = work / Path(test).name
                if copy.exists():
                    raise ValueError(f"two tests named {copy.name} in one scratch run")
                shutil.copyfile(test, copy)
                targets.append(copy.name)
            # pytest finds its config from the tests' common ancestor, whatever --rootdir says: an empty
            # one of our own keeps any pytest.ini or pyproject.toml above the scratch directory out.
            (own / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            args += ["-c", str(own / "pytest.ini"), "--noconftest"]
        spec: dict[str, Any] = {
            "path": [str(p) for p in path],
            "include": measured,
            # Directories, not the files as include patterns: coverage reads those as globs, and a checkout
            # under "repo[1]" would then measure nothing.
            "source": sorted({os.path.dirname(p) for p in measured}),
            "report": str(own / "report.json"),
            "args": [*targets, *args],
            "counts": list(COUNTS),
        }
        (own / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        timed_out = False
        # Output goes to a file, not a pipe, and the runner waits for the child itself: with pytest's
        # capture off, a process a test starts in the background inherits the output, and a pipe would end
        # only when that process did (scripts/measure_runner_stall.py).
        with (own / "spec.json").open("rb") as stdin, (own / "output.txt").open("w+b") as out:
            try:
                proc = subprocess.Popen(
                    [exe, "-I", "-B", "-X", "utf8", "-c", BOOTSTRAP],
                    cwd=workdir,
                    env=scrubbed_env(work, base=env, plugins=plugins),
                    stdin=stdin,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                return RunResult(Outcome.ERROR, output=f"could not start {exe}: {exc}", seconds=0.0)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:  # a timeout, an interrupt, or anything the tests left running in the background
                _kill_group(proc)
                proc.wait()
            out.seek(0)
            output = out.read().decode("utf-8", "replace")
        seconds = time.monotonic() - started
        if timed_out:
            return RunResult(Outcome.TIMEOUT, output=_tail(output), seconds=seconds)
        try:
            data = json.loads((own / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return RunResult(Outcome.ERROR, output=_tail(output), seconds=seconds)
    return _parse(data, output, seconds)


def _lines(value: Any) -> set[int]:
    return {n for n in value if type(n) is int} if isinstance(value, list) else set()


def _counts(value: Any) -> dict[str, int]:
    given = value if isinstance(value, dict) else {}
    return {k: given[k] if type(given.get(k)) is int and given[k] >= 0 else 0 for k in COUNTS}


def _parse(data: Any, output: str, seconds: float) -> RunResult:
    if not isinstance(data, dict) or type(data.get("exit")) is not int:
        return RunResult(Outcome.ERROR, output=_tail(output), seconds=seconds)
    code: int = data["exit"]
    counts = _counts(data.get("counts"))
    clean = counts["passed"] > 0 and not any(counts[k] for k in COUNTS if k != "passed")
    outcome = Outcome.PASSED if code == 0 and clean else Outcome.FAILED
    result = RunResult(outcome, exit_code=code, counts=counts, output=_tail(output), seconds=seconds)
    files = data.get("files")
    for name, info in (files if isinstance(files, dict) else {}).items():
        if isinstance(name, str) and isinstance(info, dict):
            statements = _lines(info.get("statements"))
            result.statements[name] = statements
            result.executed[name] = statements - _lines(info.get("missing"))
    return result
