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
import warnings
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
    """Every rule's hits in ``source``, by line. A file that doesn't parse has none (ruff reports that).

    The source is the PR's, so nothing in it may crash the review: an expression nested a few thousand
    deep makes the parser, or a few hundred deep these recursive visitors, run out of stack, and such a
    file simply gets no hits from these rules.
    """
    try:
        with warnings.catch_warnings():  # the PR's SyntaxWarnings ("invalid escape sequence") aren't ours
            warnings.simplefilter("ignore")
            tree = ast.parse(source.removeprefix("\ufeff"))  # a UTF-8 BOM is fine in a file, not in a str
        hits = [*_unawaited(tree), *_unreachable(tree), *_loop_mutations(tree), *_missing_f_prefix(tree)]
    except (SyntaxError, ValueError, RecursionError, MemoryError):  # ValueError: source with null bytes
        return []
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
        elif isinstance(sub, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and sub.name:
            names.add(sub.name)  # except E as name; case [x, *rest]; case Point() as p
        elif isinstance(sub, ast.MatchMapping) and sub.rest:
            names.add(sub.rest)  # case {"k": v, **rest}
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
        # A class-level assignment can rebind a method: fetch = run_sync(fetch).
        rebound = set().union(
            *(_bound_names(n) for n in node.body if not isinstance(n, _Function | ast.ClassDef))
        )
        self.methods.append(coroutines - sync - rebound)
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
                if isinstance(after, ast.Expr) and isinstance(after.value, ast.Yield | ast.YieldFrom):
                    break  # `return` then `yield`: the idiom for a generator that yields nothing
                word = _EXIT_WORD[type(stmt)]
                hits.append(Hit(after.lineno, "CL002", f"this line comes after a `{word}`, so it never runs"))
                break
    return hits


# CL003 ------------------------------------------------------------------------------------------------------

# The built-in list, set and dict methods that change the collection, with how many positional arguments
# each accepts. A call outside that range is some other class's method (CookieJar.clear(domain, path,
# name)), not a change to a built-in collection.
_MUTATORS: dict[str, range] = {
    "append": range(1, 2),
    "extend": range(1, 2),
    "insert": range(2, 3),
    "remove": range(1, 2),
    "pop": range(0, 3),  # list.pop([i]), dict.pop(k[, default])
    "clear": range(0, 1),
    "add": range(1, 2),
    "discard": range(1, 2),
    "update": range(0, 1000),  # dict.update(other, **kw), set.update(*others)
    "popitem": range(0, 1),
    "setdefault": range(1, 3),
}
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
            and len(node.args) in _MUTATORS[node.func.attr]
            and (not node.keywords or node.func.attr == "update")
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


def _mutations(block: list[ast.stmt], subject: str, nested: bool = False) -> Iterator[tuple[ast.stmt, str]]:
    """Statements in ``block`` that change ``subject``; ``nested`` once inside a loop within the loop, where
    a ``break`` leaves only that inner loop."""
    exits = (ast.Return, ast.Raise) if nested else (ast.Break, ast.Return, ast.Raise)
    for i, stmt in enumerate(block):
        if isinstance(stmt, _Function | ast.ClassDef):
            continue
        if isinstance(stmt, _COMPOUND):
            inner_loop = nested or isinstance(stmt, ast.For | ast.AsyncFor | ast.While)
            for inner in _child_blocks(stmt):
                yield from _mutations(inner, subject, inner_loop)
            continue
        how = _mutates(stmt, subject)
        # Changing the collection and then leaving the loop at once is the safe idiom.
        leaves = any(isinstance(later, exits) for later in block[i + 1 :])
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

_TOKEN_METHODS = frozenset(
    {"replace", "split", "rsplit", "partition", "rpartition", "find", "rfind", "index", "rindex", "count"}
    | {"startswith", "endswith", "removeprefix", "removesuffix", "strip", "lstrip", "rstrip"}
)
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
        # template = "{a}, {b}" ... template.format(a=a, b=b): a template filled in later, through its name.
        formatted = {
            call.func.value.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in ("format", "format_map")
            and isinstance(call.func.value, ast.Name)
        }
        for assign in ast.walk(node):
            targets = assign.targets if isinstance(assign, ast.Assign) else []
            if isinstance(assign, ast.AnnAssign | ast.AugAssign):
                targets = [assign.target]
            if any(isinstance(t, ast.Name) and t.id in formatted for t in targets):
                value = getattr(assign, "value", None)
                if isinstance(value, ast.Constant):
                    self.skip.add(id(value))
        self.generic_visit(node)
        self.local.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_Assign(self, node: ast.Assign) -> None:
        # rest = "... {rest} ...": as an f-string it would read the name before this line gives it a value,
        # so the braces are text (typically code held in a string).
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            names = {m.group(1) for m in _PLACEHOLDER.finditer(value.value)}
            if any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
                self.skip.add(id(value))
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        return  # an f-string's literal parts are Constant nodes too; they are not plain strings

    def visit_Call(self, node: ast.Call) -> None:
        # "...{x}".format(x=x), _("...{x}").format(x=x) and .format_map(...) fill the placeholders.
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "format_map"):
            receiver = node.func.value
            self.skip.add(id(receiver))
            if isinstance(receiver, ast.Call):
                self.skip.update(id(arg) for arg in receiver.args)
        # A string searched for or replaced is a token, not text to show: text.replace("{x}", value).
        if isinstance(node.func, ast.Attribute) and node.func.attr in _TOKEN_METHODS:
            self.skip.update(id(arg) for arg in node.args)
        # So does a call given every placeholder as a keyword: loguru's logger.info("{x}", x=x).
        keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names = {m.group(1) for m in _PLACEHOLDER.finditer(arg.value)}
                if names and names <= keywords:
                    self.skip.add(id(arg))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # if "{x}" in template: the string is a token here too.
        if any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            self.skip.add(id(node.left))
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
