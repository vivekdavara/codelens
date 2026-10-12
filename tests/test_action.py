"""Static checks on action.yml that a YAML-unaware test suite can still make."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ACTION = Path(__file__).parent.parent / "action.yml"


def run_scripts(text: str) -> list[tuple[int, str]]:
    """``(line number, line)`` for every line inside a ``run: |`` block."""
    out: list[tuple[int, str]] = []
    block_indent: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip())
        if block_indent is not None and (not line.strip() or indent > block_indent):
            out.append((number, line))
            continue
        block_indent = None
        if re.match(r"\s*run: \|\s*$", line):
            block_indent = indent
    return out


def test_no_expression_is_interpolated_into_a_script() -> None:
    # Inputs reach the shell through env only (a branch name or input can't inject commands), and GitHub
    # evaluates ${{ }} anywhere in a script, comments included: an unknown name there fails the whole action.
    scripts = run_scripts(ACTION.read_text())
    assert len(scripts) > 50  # the parser really found the scripts
    assert [(n, line.strip()) for n, line in scripts if "${{" in line] == []


def test_every_python_in_the_action_runs_isolated() -> None:
    # Steps run in the workspace, the PR's checkout; `python -m pip` or `python -c 'import json'` there
    # would import a planted pip.py or json.py. -I keeps the current directory off sys.path.
    text = ACTION.read_text()
    commands = [line for _, line in run_scripts(text)]
    commands += [line.split("run:", 1)[1] for line in text.splitlines() if re.match(r"\s*run: (?!\|)", line)]
    calls = [c for c in commands if not c.strip().startswith("#") and re.search(r"\bpython3?\b", c)]
    assert len(calls) == 3  # pip install, and the two JSON one-liners
    for call in calls:
        assert re.search(r"\bpython3?\s+-I\s", call), call.strip()


def step_script(text: str, step_id: str) -> str:
    """The ``run: |`` script of the step with ``id: step_id``, dedented as the runner writes it to a file."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"id: {step_id}")
    run = next(i for i in range(start, len(lines)) if re.match(r"\s*run: \|\s*$", lines[i]))
    indent = len(lines[run]) - len(lines[run].lstrip())
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    margin = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return "\n".join(line[margin:] for line in body) + "\n"


STUB = """#!/bin/sh
# Stands in for codelens: records its arguments and the variables the step exports, prints a review.
for arg in "$@"; do echo "$arg"; done > "$RUNNER_TEMP/args"
echo "provider=${CODELENS_PROVIDER-} recordings=${CODELENS_RECORDINGS-unset} key=${ANTHROPIC_API_KEY:+set}" \\
  > "$RUNNER_TEMP/env"
echo '{"findings": [{"source": "static"}, {"source": "llm"}], "rejections": []}'
"""


def run_review_step(tmp_path: Path, **inputs: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the action's Review step under bash as the runner does, with ``codelens`` stubbed out."""
    bin_dir, temp = tmp_path / "bin", tmp_path / "runner"
    bin_dir.mkdir(parents=True)
    temp.mkdir(parents=True)
    (bin_dir / "codelens").write_text(STUB)
    (bin_dir / "codelens").chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    script = tmp_path / "review.sh"
    script.write_text(step_script(ACTION.read_text(), "review"))
    output = tmp_path / "output"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(temp),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_REPOSITORY": "o/r",
        "DIFF_PATH": "pr.diff",
        **inputs,
    }
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    if (temp / "args").exists():
        outputs["args"] = " ".join((temp / "args").read_text().split())
        outputs["env"] = (temp / "env").read_text().strip()
    return done, outputs


def test_the_review_step_skips_when_nothing_is_configured(tmp_path: Path) -> None:
    done, outputs = run_review_step(tmp_path, PROVIDER="recorded")
    assert done.returncode == 0, done.stderr
    assert "review skipped: no provider configured" in done.stdout
    assert outputs == {"findings": "0", "rejected": "0", "static": "0"}  # codelens never ran


def test_the_review_step_runs_static_alone_when_missing_recordings_may_fall_back(tmp_path: Path) -> None:
    done, outputs = run_review_step(tmp_path, PROVIDER="recorded", MISSING_RECORDING="static")
    assert done.returncode == 0, done.stderr
    assert "--missing-recording static" in outputs["args"]
    assert outputs["env"] == "provider=recorded recordings=unset key="  # the CLI's default directory
    assert (outputs["findings"], outputs["rejected"], outputs["static"]) == ("2", "0", "1")


def test_the_review_step_posts_only_a_review_of_the_prs_own_diff(tmp_path: Path) -> None:
    pr = {
        "FROM_PR": "true",
        "PR_NUMBER": "12",
        "HEAD_SHA": "abc123",
        "HEAD_REPO_ID": "41",
        "BASE_REPO_ID": "41",
        "RECORDINGS": "recs",
    }
    done, outputs = run_review_step(tmp_path, PROVIDER="recorded", **pr)
    assert done.returncode == 0, done.stderr
    assert outputs["args"].endswith(
        "--missing-recording fail --source-root " + str(tmp_path) + " --commit abc123 --post --pr 12"
    )
    assert outputs["env"] == "provider=recorded recordings=recs key="
    done, outputs = run_review_step(tmp_path / "x", PROVIDER="recorded", **{**pr, "FROM_PR": "false"})
    assert "--post" not in outputs["args"] and "not posting: the review is of diff-file" in done.stdout


def test_the_review_step_skips_a_moved_pr_and_a_live_provider_without_a_key(tmp_path: Path) -> None:
    done, outputs = run_review_step(tmp_path, PROVIDER="recorded", RECORDINGS="recs", MOVED="true")
    assert "moved on to a newer commit" in done.stdout and "args" not in outputs
    done, outputs = run_review_step(tmp_path / "a", PROVIDER="anthropic")
    assert "no api-key" in done.stdout and "args" not in outputs
    done, outputs = run_review_step(tmp_path / "b", PROVIDER="anthropic", API_KEY="sk-test")
    assert outputs["env"] == "provider=anthropic recordings=unset key=set"


def test_the_review_step_does_not_post_on_a_fork_s_pull_request(tmp_path: Path) -> None:
    # The fork's PR gets a read-only token: posting would answer 403 and fail the job. A deleted fork
    # leaves head.repo null, so its id arrives as "".
    pr = {"FROM_PR": "true", "PR_NUMBER": "12", "HEAD_SHA": "abc123", "MISSING_RECORDING": "static"}
    for n, head_id in enumerate(["99", ""]):
        done, outputs = run_review_step(
            tmp_path / str(n), PROVIDER="recorded", HEAD_REPO_ID=head_id, BASE_REPO_ID="41", **pr
        )
        assert done.returncode == 0, done.stderr
        assert "not posting: a pull request from a fork" in done.stdout
        assert "--post" not in outputs["args"] and outputs["args"].endswith("--commit abc123")
    same = {"HEAD_REPO_ID": "41", "BASE_REPO_ID": "41"}
    done, outputs = run_review_step(tmp_path / "same", PROVIDER="recorded", **same, **pr)
    assert outputs["args"].endswith("--commit abc123 --post --pr 12")
    done, outputs = run_review_step(tmp_path / "off", PROVIDER="recorded", POST="false", **same, **pr)
    assert "--post" not in outputs["args"] and "not posting" not in done.stdout


def test_the_fork_check_reads_the_repository_ids_from_the_event() -> None:
    # The step tests set these variables themselves, so they can't catch a typo in the expressions; a
    # wrong one would silently stop all posting (or never stop it on forks).
    text = ACTION.read_text()
    assert "HEAD_REPO_ID: ${{ github.event.pull_request.head.repo.id }}" in text
    assert "BASE_REPO_ID: ${{ github.event.pull_request.base.repo.id }}" in text


@pytest.mark.parametrize("recordings", ["", "recs"])
def test_an_unknown_missing_recording_value_fails_whatever_else_is_set(
    tmp_path: Path, recordings: str
) -> None:
    # It was a silent skip without recordings and an argparse error with them.
    done, outputs = run_review_step(
        tmp_path, PROVIDER="recorded", MISSING_RECORDING="Static", RECORDINGS=recordings
    )
    assert done.returncode == 1 and "unknown missing-recording 'Static'" in done.stdout
    assert "args" not in outputs


def test_the_review_step_passes_the_source_root_with_the_pre_pass_off(tmp_path: Path) -> None:
    # The source root also locates recordings committed to the PR; without it, a checkout in a
    # subdirectory would review its recordings and never replay one.
    done, outputs = run_review_step(
        tmp_path, PROVIDER="recorded", RECORDINGS="head/recs", STATIC="false", SOURCE_ROOT="head"
    )
    assert done.returncode == 0, done.stderr
    assert "--source-root head --no-static" in outputs["args"]
