"""Test generation: pytest tests for the Python functions a pull request changes, kept only if they help.

The model is asked once per pull request for tests of every changed top-level function. Each candidate runs
on its own (:mod:`codelens.sandbox`) and is kept only if it passes and runs lines of a changed function that
neither the existing suite nor an earlier kept candidate ran. See DESIGN.md, "Test generation (day 4)".
"""

from __future__ import annotations

import ast
import keyword
import warnings
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from codelens.diff import FileStatus, PatchSet
from codelens.prompts import budget_rank
from codelens.static import checked_lines

__all__ = ["Target", "Targets", "find_targets", "module_name"]


@dataclass(frozen=True)
class Target:
    """A changed top-level function, and where to import it from."""

    path: str
    """The file, relative to the checkout, as the diff names it."""
    module: str
    """Its dotted import name (``shop.pricing``)."""
    import_root: str
    """The directory, relative to the checkout, that must be on ``sys.path`` to import it: "" or "src"."""
    name: str
    start: int
    """First line of the definition, decorators included."""
    end: int
    body_start: int
    """First line of the body: the lines a test can make run (the ``def`` line runs on import)."""
    changed: frozenset[int]
    """Lines the pull request added inside the definition."""

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"


@dataclass
class Targets:
    targets: list[Target] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """Python files with added lines that give no target, with the reason."""
    sources: dict[str, list[str]] = field(default_factory=dict)
    """The checked new version of each file that has targets, by path."""


def module_name(path: str) -> tuple[str, str] | None:
    """``(import root, dotted module name)`` for a Python file, or ``None`` if it can't be imported by name.

    Files under a top-level ``src/`` import from there (the src layout); everything else from the checkout's
    root. ``__init__.py`` is its package. Every part must be an identifier and not a keyword:
    ``scripts/build-docs.py`` or ``class/x.py`` can't be imported with an ``import`` statement.
    """
    parts = list(PurePosixPath(path).parts)
    if not parts or not parts[-1].endswith(".py"):
        return None
    root = ""
    if parts[0] == "src" and len(parts) > 1:
        root, parts = "src", parts[1:]
    parts[-1] = parts[-1].removesuffix(".py")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() and not keyword.iskeyword(p) for p in parts):
        return None
    return root, ".".join(parts)


def _parse(source: str) -> ast.Module | str:
    with warnings.catch_warnings():  # the PR's invalid escapes and the like are not CodeLens's to report
        warnings.simplefilter("ignore")
        try:
            return ast.parse(source)
        except (SyntaxError, ValueError):
            return "does not parse"
        except (RecursionError, MemoryError):
            return "nested too deeply to parse"


def find_targets(patch: PatchSet, root: Path) -> Targets:
    """The top-level functions ``patch`` adds lines to, in Python files that match the checkout ``root``.

    Test files are left out (tests for tests are not the point), and so is any file the static pre-pass
    would refuse (:func:`codelens.static.checked_lines`): the functions' line numbers must be the diff's.
    Methods and nested functions are not targets: a test would first have to build the object or reach
    the closure, which the model can't do reliably from one module's source.
    """
    result = Targets()
    for file in patch:
        if file.is_binary or file.status is FileStatus.DELETED or not file.path.endswith(".py"):
            continue
        added = set(file.added_lines())
        if not added:
            continue
        if budget_rank(file) == 1:
            result.skipped.append((file.path, "a test file"))
            continue
        named = module_name(file.path)
        if named is None:
            result.skipped.append((file.path, "not importable as a module"))
            continue
        lines = checked_lines(file, root)
        if isinstance(lines, str):
            result.skipped.append((file.path, lines))
            continue
        tree = _parse("\n".join(lines))
        if isinstance(tree, str):
            result.skipped.append((file.path, tree))
            continue
        found: dict[str, Target] = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            changed = frozenset(n for n in added if start <= n <= end)
            # A second definition of a name replaces the first, and an import gets the last one.
            found.pop(node.name, None)
            if changed:
                found[node.name] = Target(
                    file.path, named[1], named[0], node.name, start, end, node.body[0].lineno, changed
                )
        if not found:
            result.skipped.append((file.path, "no changed top-level function"))
            continue
        result.targets += sorted(found.values(), key=lambda t: t.start)
        result.sources[file.path] = lines
    return result
