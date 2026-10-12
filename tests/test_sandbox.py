"""The test runner: real child processes, real pytest and coverage, on small projects in tmp_path."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codelens.sandbox import MAX_OUTPUT, Outcome, _parse, run_pytest, scrubbed_env

MODULE = """\
def grade(score):
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    return "C"
"""


IMPORT = "from school.grades import grade\n\n"


def project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "proj" / "src" / "school"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "grades.py").write_text(MODULE)
    return tmp_path / "proj"


def run(tmp_path: Path, test: str, **kwargs: object):  # type: ignore[no-untyped-def]
    root = project(tmp_path)
    file = tmp_path / "test_candidate.py"
    file.write_text(test)
    module = root / "src" / "school" / "grades.py"
    return run_pytest([file], include=[module], path=[root / "src"], **kwargs), str(module)  # type: ignore[arg-type]


def test_a_passing_test_reports_the_lines_it_ran(tmp_path: Path) -> None:
    result, module = run(tmp_path, f"{IMPORT}def test_a():\n    assert grade(95) == 'A'\n")
    assert result.outcome is Outcome.PASSED and result.exit_code == 0
    assert result.statements[module] == {1, 2, 3, 4, 5, 6}
    assert result.executed[module] == {1, 2, 3}


def test_a_failing_test_is_failed_and_says_why(tmp_path: Path) -> None:
    result, _ = run(tmp_path, f"{IMPORT}def test_a():\n    assert grade(50) == 'A'\n")
    assert result.outcome is Outcome.FAILED and result.exit_code == 1
    assert "assert 'C' == 'A'" in result.output


def test_no_tests_collected_is_a_failure(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "import school.grades\n")
    assert result.outcome is Outcome.FAILED and result.exit_code == 5


def test_an_import_error_is_a_failure_with_the_traceback(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "from school.grades import missing\n\ndef test_a():\n    pass\n")
    assert result.outcome is Outcome.FAILED
    assert "ImportError" in result.output


def test_a_hanging_test_is_killed_at_the_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    result, _ = run(tmp_path, "import time\n\ndef test_a():\n    time.sleep(60)\n", timeout=3)
    assert result.outcome is Outcome.TIMEOUT
    assert time.monotonic() - started < 20


def test_children_the_tests_start_are_killed_too(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    # With pytest's capture off, the sleeper inherits the run's own stdout: a runner that read the output
    # through a pipe to its end would wait for the sleeper (scripts/measure_runner_stall.py).
    test = (
        "import subprocess, sys\n\ndef test_a(capsys):\n"
        "    with capsys.disabled():\n"
        "        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"    open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
    )
    result, _ = run(tmp_path, test)
    assert result.outcome is Outcome.PASSED
    assert result.seconds < 30
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # Killed but not yet reaped by init shows up as a zombie, which kill(0) still finds.
        status = os.popen(f"ps -o stat= -p {pid}").read().strip()
        if not status or status.startswith("Z"):
            return
        time.sleep(0.1)
    raise AssertionError(f"background child {pid} survived the run")


def test_an_early_exit_is_an_error_not_a_pass(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "import os\n\ndef test_a():\n    os._exit(0)\n")
    assert result.outcome is Outcome.ERROR and result.exit_code is None


def test_the_child_sees_no_secrets(tmp_path: Path) -> None:
    base = {"PATH": os.environ["PATH"], "LANG": "C", "ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t"}
    test = (
        "import os\n\ndef test_a():\n"
        "    assert 'ANTHROPIC_API_KEY' not in os.environ and 'GITHUB_TOKEN' not in os.environ\n"
        "    assert os.environ['LANG'] == 'C'\n"
    )
    result, _ = run(tmp_path, test, env=base)
    assert result.outcome is Outcome.PASSED, result.output


def test_files_the_tests_write_stay_in_the_scratch_directory(tmp_path: Path) -> None:
    test = (
        "import os\nfrom pathlib import Path\n\ndef test_a():\n"
        "    Path('written.txt').write_text('x')\n"
        "    assert Path.home().resolve() == Path.cwd().resolve()\n"
    )
    result, _ = run(tmp_path, test)
    assert result.outcome is Outcome.PASSED, result.output
    assert not list(tmp_path.rglob("written.txt"))  # the scratch directory is gone, and nothing else has it


def test_project_files_cannot_stand_in_for_coverage_or_pytest(tmp_path: Path) -> None:
    root = project(tmp_path)
    for name in ("coverage.py", "pytest.py", "json.py"):
        (root / "src" / name).write_text("raise SystemExit('planted module ran')\n")
    file = tmp_path / "test_candidate.py"
    file.write_text("from school.grades import grade\n\ndef test_a():\n    assert grade(85) == 'B'\n")
    result = run_pytest([file], include=[root / "src/school/grades.py"], path=[root / "src"])
    assert result.outcome is Outcome.PASSED, result.output
    assert "planted" not in result.output


def test_the_project_pytest_config_does_not_apply_to_scratch_runs(tmp_path: Path) -> None:
    root = project(tmp_path)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --definitely-not-an-option\n")
    file = tmp_path / "test_candidate.py"
    file.write_text("def test_a():\n    pass\n")
    result = run_pytest([file], include=[root / "src/school/grades.py"], path=[root / "src"])
    assert result.outcome is Outcome.PASSED, result.output


def test_output_keeps_the_tail(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "def test_a():\n    print('x' * 10000)\n    assert False\n")
    assert len(result.output) <= MAX_OUTPUT + 1
    assert result.output.startswith("…") and "1 failed" in result.output  # pytest's summary is last


def test_a_missing_interpreter_module_is_an_error(tmp_path: Path) -> None:
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    result, _ = run(tmp_path, "def test_a():\n    pass\n", python=str(fake))
    assert result.outcome is Outcome.ERROR


def test_scrubbed_env_is_an_allow_list(tmp_path: Path) -> None:
    env = scrubbed_env(tmp_path, base={"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x", "TZ": "UTC"})
    assert env["PATH"] == "/bin" and env["TZ"] == "UTC" and env["HOME"] == str(tmp_path)
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in scrubbed_env(tmp_path, base={}, plugins=True)
    assert scrubbed_env(tmp_path, base={})["PATH"] == os.defpath


def test_a_malformed_report_is_an_error() -> None:
    assert _parse([], "", 0.0).outcome is Outcome.ERROR
    assert _parse({"exit": True}, "", 0.0).outcome is Outcome.ERROR
    files = {"a.py": {"statements": [1, 2, "3", True], "missing": [2]}, 7: {}}
    parsed = _parse({"exit": 0, "files": files}, "", 0)
    assert parsed.statements == {"a.py": {1, 2}} and parsed.executed == {"a.py": {1}}
    assert _parse({"exit": 1, "files": []}, "", 0).statements == {}


@pytest.mark.parametrize(
    ("test", "counts"),
    [
        (
            "import pytest\n\n@pytest.mark.xfail\ndef test_a():\n    assert grade(100) == 'B'\n",
            {"xfailed": 1},
        ),
        ("import pytest\n\ndef test_a():\n    grade(85)\n    pytest.skip('later')\n", {"skipped": 1}),
        ("import pytest\n\ndef test_a():\n    pytest.exit('bye', returncode=0)\n", {}),
        (
            "import pytest\n\n@pytest.mark.xfail\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n",
            {"passed": 1, "xpassed": 1},
        ),
        (
            "import pytest\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError\n\n"
            "def test_a(broken):\n    pass\n",
            {"errors": 1},
        ),
    ],
)
def test_exit_status_zero_is_not_a_pass_unless_a_test_passed_cleanly(
    tmp_path: Path, test: str, counts: dict[str, int]
) -> None:
    result, _ = run(tmp_path, IMPORT + test)
    assert result.outcome is Outcome.FAILED
    assert {k: v for k, v in result.counts.items() if v} == counts


def test_counts_of_a_clean_pass(tmp_path: Path) -> None:
    result, _ = run(
        tmp_path, f"{IMPORT}def test_a():\n    assert grade(95) == 'A'\n\ndef test_b():\n    pass\n"
    )
    assert result.outcome is Outcome.PASSED and result.counts["passed"] == 2


def test_a_thread_left_running_does_not_turn_a_pass_into_a_timeout(tmp_path: Path) -> None:
    test = "import threading, time\n\ndef test_a():\n"
    test += "    threading.Thread(target=time.sleep, args=(60,)).start()\n"
    result, _ = run(tmp_path, test, timeout=10)
    assert result.outcome is Outcome.PASSED and result.seconds < 10


def test_a_conftest_beside_the_tests_does_not_apply(tmp_path: Path) -> None:
    # A candidate could write one beside itself for the next; scratch runs copy the tests away and
    # load no conftest at all.
    (tmp_path / "conftest.py").write_text(
        "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n    outcome.get_result().outcome = 'passed'\n"
    )
    result, _ = run(tmp_path, "def test_a():\n    assert False\n")
    assert result.outcome is Outcome.FAILED


def test_two_tests_with_one_name_are_refused(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for d in ("a", "b"):
        (tmp_path / d / "test_x.py").write_text("def test_a():\n    pass\n")
    with pytest.raises(ValueError, match=r"two tests named test_x\.py"):
        run_pytest([tmp_path / "a" / "test_x.py", tmp_path / "b" / "test_x.py"], include=[])


def test_the_child_runs_in_utf8_mode_and_writes_no_bytecode(tmp_path: Path) -> None:
    test = f"import sys\n{IMPORT}def test_a():\n"
    test += "    assert sys.flags.utf8_mode == 1 and sys.dont_write_bytecode\n"
    result, _ = run(tmp_path, test, env={"PATH": os.environ["PATH"], "LANG": "en_US.ISO8859-1"})
    assert result.outcome is Outcome.PASSED, result.output
    assert not list((tmp_path / "proj").rglob("__pycache__"))


def test_a_relative_interpreter_path_works_and_a_missing_one_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = Path(sys.executable).parent.parent
    monkeypatch.chdir(venv.parent)
    result, _ = run(
        tmp_path, "def test_a():\n    pass\n", python=str(Path(venv.name) / "bin" / Path(sys.executable).name)
    )
    assert result.outcome is Outcome.PASSED, result.output
    missing, _ = run(tmp_path / "again", "def test_a():\n    pass\n", python="/no/such/python")
    assert missing.outcome is Outcome.ERROR and "could not start /no/such/python" in missing.output


def test_an_interrupt_kills_the_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pidfile = tmp_path / "pid"
    test = f"import os, time\n\ndef test_a():\n    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
    test += "    time.sleep(60)\n"
    real_wait = subprocess.Popen.wait

    def interrupted(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if timeout is None:
            return real_wait(self)
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, test)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed and reaped: run_pytest waited for it after the kill


def test_a_checkout_path_with_glob_characters_is_measured(tmp_path: Path) -> None:
    result, module = run(tmp_path / "repo[1]", f"{IMPORT}def test_a():\n    assert grade(95) == 'A'\n")
    assert result.outcome is Outcome.PASSED and result.executed[module] == {1, 2, 3}


def test_interpreter_paths_are_absolute() -> None:
    from codelens.sandbox import interpreter

    assert interpreter(None) == os.path.abspath(sys.executable)
    assert interpreter("python3") == shutil.which("python3")
    assert interpreter("no-such-python-anywhere") == "no-such-python-anywhere"  # Popen then fails: an ERROR
    assert interpreter("venv/bin/python") == os.path.join(os.getcwd(), "venv", "bin", "python")
