"""pyproject.toml: the static pre-pass's ruff is pinned the same way everywhere it is installed."""

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_the_action_and_ci_install_the_same_pinned_ruff() -> None:
    # The action installs the `static` extra and CI the `dev` one; the static block is part of the prompt,
    # so a different ruff in either would change prompts and miss recordings in only one of them.
    extras = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]
    (static,) = [d for d in extras["static"] if d.startswith("ruff")]
    (dev,) = [d for d in extras["dev"] if d.startswith("ruff")]
    assert static == dev and re.fullmatch(r"ruff==\d+\.\d+\.\d+", static)
