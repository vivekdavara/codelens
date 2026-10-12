"""The README's example output is what the command prints: an example that drifts misleads every reader.

The review example went stale once already: it lost two lines when the static pre-pass (day 3) started
reporting files it couldn't check.
"""

import re
from pathlib import Path

import pytest

from codelens.cli import main

ROOT = Path(__file__).parent.parent


def example_after(heading: str) -> tuple[str, list[str]]:
    """The command in backticks and the ```text block that follow ``heading`` in the README."""
    text = (ROOT / "README.md").read_text()
    rest = text[text.index(heading) :]
    command = re.search(r"\(`(codelens [^`]+)`\)", rest)
    block = re.search(r"```text\n(.*?)```", rest, re.DOTALL)
    assert command and block
    return command.group(1), block.group(1).splitlines()


def test_the_review_example_is_what_the_command_prints(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    command, shown = example_after("Output on the sample PR in this repo")
    assert command.startswith("codelens review ")
    monkeypatch.chdir(ROOT)
    for name in ("CODELENS_PROVIDER", "CODELENS_RECORDINGS"):
        monkeypatch.delenv(name, raising=False)
    assert main(command.split()[1:]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() + captured.err.splitlines() == shown


def test_the_testgen_example_is_what_the_command_prints(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (ROOT / "README.md").read_text()
    rest = text[text.index("Output on the sample in this repo") :]
    block = re.search(r"```text\n(.*?)```", rest, re.DOTALL)
    command = re.search(r"`(\.venv/bin/codelens testgen [^`]+)`", text)
    assert block and command
    # The README's own command, from its results table, with a shorter timeout: only the candidate that
    # sleeps waits for it, and it prints "timed out" either way.
    args = command.group(1).split()[1:]
    assert args[-2:] == ["--timeout", "5"]
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("CODELENS_PROVIDER", raising=False)
    assert main([*args[:-1], "2"]) == 0
    printed = capsys.readouterr().out.splitlines()
    # The README elides the middle of the candidate list with "...".
    head, tail = block.group(1).split("...\n")
    assert printed[: len(head.splitlines())] == head.splitlines()
    assert printed[-len(tail.splitlines()) :] == tail.splitlines()
