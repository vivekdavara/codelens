"""CodeLens's own static rules: bug patterns ruff 0.16 doesn't report, or reports only in preview.

Each rule reads one module's AST and returns :class:`Hit` objects (line, code, message). The static pre-pass
(:mod:`codelens.static`) keeps only hits on the PR's added lines and gives each rule its severity, category
and wording. The rules are deliberately narrow: a rule that fires on correct code teaches people to ignore
the reviewer, so each one looks only at the shape that is almost always a mistake.

- ``CL001`` a coroutine function called as a statement without ``await``: the call only creates a
  coroutine object and the body never runs.
- ``CL002`` a statement after ``return``, ``raise``, ``break`` or ``continue`` in the same block.
- ``CL003`` a loop that changes the list, dict or set it is iterating over (ruff's B909 is preview-only).
- ``CL004`` a plain string with ``{name}`` placeholders naming local variables: a missing ``f`` prefix
  (ruff's RUF027 is preview-only).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise

__all__ = ["Hit", "check"]


@dataclass(frozen=True)
class Hit:
    line: int
    code: str
    message: str


def check(source: str) -> list[Hit]:
    """Every rule's hits in ``source``, by line. A file that doesn't parse has none (ruff reports that)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):  # ValueError: source with null bytes
        return []
    hits = [*_unawaited(tree), *_unreachable(tree), *_loop_mutations(tree), *_missing_f_prefix(tree)]
    return sorted(set(hits), key=lambda h: (h.line, h.code, h.message))


_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _bound_names(node: ast.AST) -> set[str]:
    """Every name ``node`` binds anywhere inside it: parameters, assignments, imports, defs, loop targets.

    An over-approximation (nested scopes included), which only ever makes the rules quieter.
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store | ast.Del):
            names.add(sub.id)
        elif isinstance(sub, ast.arg):
            names.add(sub.arg)
        elif isinstance(sub, _Function | ast.ClassDef) and sub is not node:
            names.add(sub.name)
        elif isinstance(sub, ast.alias):
            names.add((sub.asname or sub.name).split(".")[0])
        elif isinstance(sub, ast.Global | ast.Nonlocal):
            names.update(sub.names)
    return names


# CL001 ------------------------------------------------------------------------------------------------------


_TRANSPARENT = frozenset({"staticmethod", "classmethod", "abstractmethod", "override"})


def _transparent(decorator: ast.expr) -> bool:
    """Decorators that leave an ``async def`` a coroutine function."""
    name = decorator.attr if isinstance(decorator, ast.Attribute) else getattr(decorator, "id", None)
    return name in _TRANSPARENT


def _module_coroutines(tree: ast.Module) -> set[str]:
    """Top-level ``async def`` names that nothing else at the top level rebinds."""
    # A decorator can turn a coroutine function into something else (a sync wrapper, a context manager),
    # so only plain ones count.
    coroutines = {
        n.name
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and all(_transparent(d) for d in n.decorator_list)
    }
    rebound: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.ClassDef):
            rebound.add(stmt.name)
        elif not isinstance(stmt, ast.AsyncFunctionDef):
            rebound |= _bound_names(stmt)
    names = [n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    duplicated = {name for name in names if names.count(name) > 1}
    return coroutines - rebound - duplicated


class _Unawaited(ast.NodeVisitor):
    def __init__(self, module: set[str]) -> None:
        self.module = module
        self.methods: list[set[str]] = []  # async methods of each enclosing class, innermost last
        self.local: list[set[str]] = []  # names bound by each enclosing function
        self.hits: list[Hit] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        defs = [n for n in node.body if isinstance(n, _Function)]
        sync = {n.name for n in defs if isinstance(n, ast.FunctionDef)}
        coroutines = {
            n.name
            for n in defs
            if isinstance(n, ast.AsyncFunctionDef) and all(_transparent(d) for d in n.decorator_list)
        }
        self.methods.append(coroutines - sync)
        self.generic_visit(node)
        self.methods.pop()

    def _function(self, node: _Function) -> None:
        self.local.append(_bound_names(node))
        self.generic_visit(node)
        self.local.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def _coroutine(self, func: ast.expr) -> str | None:
        if (
            isinstance(func, ast.Name)
            and func.id in self.module
            and not any(func.id in names for names in self.local)
        ):
            return func.id
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
            and self.methods
            and func.attr in self.methods[-1]
        ):
            return f"{func.value.id}.{func.attr}"
        return None

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Call) and (name := self._coroutine(node.value.func)) is not None:
            self.hits.append(
                Hit(
                    node.lineno,
                    "CL001",
                    f"`{name}()` is an `async def`, so calling it without `await` never runs it",
                )
            )
        self.generic_visit(node)


def _unawaited(tree: ast.Module) -> list[Hit]:
    visitor = _Unawaited(_module_coroutines(tree))
    visitor.visit(tree)
    return visitor.hits


# CL002 ------------------------------------------------------------------------------------------------------

_EXITS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
_EXIT_WORD = {ast.Return: "return", ast.Raise: "raise", ast.Break: "break", ast.Continue: "continue"}


def _blocks(tree: ast.AST) -> Iterator[list[ast.stmt]]:
    """Every statement list in the tree: bodies, else and finally blocks, handlers, match cases."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _unreachable(tree: ast.Module) -> list[Hit]:
    hits = []
    for block in _blocks(tree):
        for stmt, after in pairwise(block):
            if isinstance(stmt, _EXITS):
                word = _EXIT_WORD[type(stmt)]
                hits.append(Hit(after.lineno, "CL002", f"this line comes after a `{word}`, so it never runs"))
                break
    return hits


# CL003 ------------------------------------------------------------------------------------------------------

_MUTATORS = frozenset(
    {"append", "extend", "insert", "remove", "pop", "clear"}  # list
    | {"add", "discard", "update"}  # set (and dict.update)
    | {"popitem", "setdefault"}  # dict
)
_VIEWS = frozenset({"keys", "values", "items"})


def _subject(node: ast.expr) -> str | None:
    """The source of a plain name or attribute chain (``items``, ``self.items``), else ``None``."""
    inner = node
    while isinstance(inner, ast.Attribute):
        inner = inner.value
    return ast.unparse(node) if isinstance(inner, ast.Name) else None


def _iterated(loop: ast.For | ast.AsyncFor) -> str | None:
    """What the loop walks over, live: ``for x in items``, ``d.items()``, ``enumerate(items)``."""
    it = loop.iter
    if isinstance(it, ast.Call) and not it.keywords and len(it.args) == 1:
        if isinstance(it.func, ast.Name) and it.func.id == "enumerate":
            it = it.args[0]
    elif (
        isinstance(it, ast.Call)
        and not it.args
        and isinstance(it.func, ast.Attribute)
        and it.func.attr in _VIEWS
    ):
        it = it.func.value
    return _subject(it)


def _mutates(stmt: ast.stmt, subject: str) -> str | None:
    """How ``stmt`` (not counting nested functions and classes) changes ``subject``, or ``None``."""
    if isinstance(stmt, ast.Delete):
        for target in stmt.targets:
            if isinstance(target, ast.Subscript) and _subject(target.value) == subject:
                return f"del {subject}[...]"
    if isinstance(stmt, ast.AugAssign) and _subject(stmt.target) == subject:
        text = ast.unparse(stmt)
        return text if len(text) <= 60 else text[:59] + "…"
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, _Function | ast.ClassDef | ast.Lambda):
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and _subject(node.func.value) == subject
        ):
            return f"{subject}.{node.func.attr}()"
        stack.extend(ast.iter_child_nodes(node))
    return None


_COMPOUND = (
    ast.If,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Match,
)


def _child_blocks(stmt: ast.stmt) -> Iterator[list[ast.stmt]]:
    for field in ("body", "orelse", "finalbody"):
        block = getattr(stmt, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            yield block
    for handler in getattr(stmt, "handlers", []):  # try / try*
        yield handler.body
    for case in getattr(stmt, "cases", []):  # match
        yield case.body


def _mutations(block: list[ast.stmt], subject: str) -> Iterator[tuple[ast.stmt, str]]:
    for i, stmt in enumerate(block):
        if isinstance(stmt, _Function | ast.ClassDef):
            continue
        if isinstance(stmt, _COMPOUND):
            for inner in _child_blocks(stmt):
                yield from _mutations(inner, subject)
            continue
        how = _mutates(stmt, subject)
        # Changing the collection and then leaving the loop at once is the safe idiom.
        leaves = any(isinstance(later, ast.Break | ast.Return | ast.Raise) for later in block[i + 1 :])
        if how is not None and not leaves:
            yield stmt, how


def _loop_mutations(tree: ast.Module) -> list[Hit]:
    hits = []
    for loop in ast.walk(tree):
        if isinstance(loop, ast.For | ast.AsyncFor) and (subject := _iterated(loop)) is not None:
            for stmt, how in _mutations(loop.body, subject):
                message = f"`{how}` changes `{subject}` while the loop is iterating over it"
                hits.append(Hit(stmt.lineno, "CL003", message))
    return hits


# CL004 ------------------------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*(?:![rsa])?(?::[^{}]*)?\}(?!\})")


class _MissingF(ast.NodeVisitor):
    def __init__(self) -> None:
        self.local: list[set[str]] = []
        self.skip: set[int] = set()  # ids of strings that are fine as they are
        self.hits: list[Hit] = []

    def _docstring(self, node: ast.Module | ast.ClassDef | _Function) -> None:
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            self.skip.add(id(first.value))

    def visit_Module(self, node: ast.Module) -> None:
        self._docstring(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._docstring(node)
        self.generic_visit(node)

    def _function(self, node: _Function) -> None:
        self._docstring(node)
        self.local.append(_bound_names(node))
        self.generic_visit(node)
        self.local.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        return  # an f-string's literal parts are Constant nodes too; they are not plain strings

    def visit_Call(self, node: ast.Call) -> None:
        # "...{x}".format(x=x), _("...{x}").format(x=x) and .format_map(...) fill the placeholders.
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "format_map"):
            receiver = node.func.value
            self.skip.add(id(receiver))
            if isinstance(receiver, ast.Call):
                self.skip.update(id(arg) for arg in receiver.args)
        # So does a call given every placeholder as a keyword: loguru's logger.info("{x}", x=x).
        keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names = {m.group(1) for m in _PLACEHOLDER.finditer(arg.value)}
                if names and names <= keywords:
                    self.skip.add(id(arg))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or id(node) in self.skip or not self.local:
            return  # module-level strings are often templates filled in somewhere else
        local = self.local[-1]
        names = [m.group(1) for m in _PLACEHOLDER.finditer(node.value)]
        if names and all(name in local for name in names):
            shown = ", ".join(f"`{{{name}}}`" for name in dict.fromkeys(names))
            message = f"the string uses {shown} but has no `f` prefix, so it is printed as is"
            self.hits.append(Hit(node.lineno, "CL004", message))


def _missing_f_prefix(tree: ast.Module) -> list[Hit]:
    visitor = _MissingF()
    visitor.visit(tree)
    return visitor.hits
