"""Static checks on action.yml that a YAML-unaware test suite can still make."""

import re
from pathlib import Path

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
