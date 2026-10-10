"""The static pre-pass: ruff and CodeLens's own rules on the Python lines a pull request adds.

Static findings are posted like the model's (``source: "static"``) and listed in the prompt, so the model
starts from verified facts instead of rediscovering them. Only problems the PR introduces count: hits on
added lines, and hits on unchanged lines that the old version of the file didn't have. A PR is not the place
to report what was already there, but a change can make an unchanged line wrong.

Files are read from a checkout of the PR's head (``root``) and used only if every line the diff shows is the
same on disk. A checkout of another commit (``actions/checkout`` defaults to the PR's merge commit, which
differs when the base branch changed the same file) would put findings on the wrong lines, so such a file is
skipped with the reason. Ruff runs with ``--isolated``: the PR's own ruff configuration is the PR author's to
edit, and must not decide what CodeLens checks. See DESIGN.md, "Static pre-pass".
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from codelens import rules
from codelens.diff import FileDiff, FileStatus, LineKind, PatchSet, Side
from codelens.findings import Category, Finding, Severity, anchored
from codelens.prompts import unsafe_path

__all__ = ["RULES", "Rule", "StaticResult", "analyse", "find_ruff", "old_version", "ruff_codes"]

MAX_FILE_BYTES = 2_000_000
RUFF_TIMEOUT = 120.0
TARGET_VERSION = "py314"
"""The newest grammar ruff knows: version-gated syntax (``match``, ``except*``) is never reported as an
error, since CodeLens can't know which Python the project targets."""
_CHUNK = 200  # paths per ruff invocation, far below any command-line length limit
_LONE_CR = re.compile(r"\r(?!\n)")

B, S, P, M, T = Category.BUG, Category.SECURITY, Category.PERFORMANCE, Category.MAINTAINABILITY, Category.TEST
CRIT, HIGH, MED, LOW = Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW


@dataclass(frozen=True)
class Rule:
    severity: Severity
    category: Category
    confidence: float
    """A prior set by hand: how often a hit on new code is a real problem. Not measured; the eval set
    measures the pre-pass as a whole."""
    why: str
    """The consequence, in one or two sentences: the body of the posted comment."""


_SYNTAX = "The file no longer parses, so importing it raises SyntaxError."
_COMPILE = "This is a SyntaxError when the module is compiled, so importing it fails."

RULES: dict[str, Rule] = {
    # Code that can't run.
    "invalid-syntax": Rule(CRIT, B, 0.95, _SYNTAX),
    "F701": Rule(CRIT, B, 0.95, _COMPILE),
    "F702": Rule(CRIT, B, 0.95, _COMPILE),
    "F704": Rule(CRIT, B, 0.95, _COMPILE),
    "F706": Rule(CRIT, B, 0.95, _COMPILE),
    "PLE1142": Rule(CRIT, B, 0.95, _COMPILE),
    # Crashes and wrong results.
    "F821": Rule(HIGH, B, 0.85, "Running this line raises NameError: nothing in scope defines this name."),
    "F823": Rule(
        HIGH,
        B,
        0.9,
        "The name is assigned later in this function, which makes it local everywhere in the function, so "
        "reading it here raises UnboundLocalError.",
    ),
    "F507": Rule(
        HIGH, B, 0.9, "The number of `%` placeholders and arguments differ, so this raises TypeError."
    ),
    "F524": Rule(
        HIGH, B, 0.9, "A placeholder has no argument, so `.format()` raises KeyError or IndexError."
    ),
    "F632": Rule(
        HIGH,
        B,
        0.9,
        "`is` compares identity, not value: with a str, bytes or int literal the result depends on interning "
        "and can be False for equal values. Use `==`.",
    ),
    "F631": Rule(HIGH, B, 0.95, "An assert on a non-empty tuple is always true, so this assert never fails."),
    "B012": Rule(
        HIGH,
        B,
        0.85,
        "A `return`, `break` or `continue` in `finally` discards any exception raised in the `try`, so "
        "errors vanish silently.",
    ),
    "B016": Rule(HIGH, B, 0.95, "Raising something that isn't an exception raises TypeError instead."),
    "B020": Rule(
        HIGH,
        B,
        0.85,
        "The loop variable has the name of the collection being looped over, so after the loop that name "
        "holds the last item, not the collection.",
    ),
    "PLW0133": Rule(
        HIGH, B, 0.9, "The exception is created and thrown away: without `raise`, nothing stops."
    ),
    "PLW0711": Rule(
        HIGH,
        B,
        0.9,
        "`except A or B` means `except A`, so B is never caught here. Catch a tuple: `except (A, B)`.",
    ),
    "PLW0177": Rule(
        HIGH,
        B,
        0.9,
        "Every comparison with NaN is false, even NaN == NaN, so this condition never holds. Use "
        "`math.isnan`.",
    ),
    "SIM222": Rule(
        HIGH,
        B,
        0.8,
        'One side of `or` is always true, so the whole condition is: `x == "a" or "b"` means '
        '`(x == "a") or "b"`, and a non-empty string is true. Write `x in ("a", "b")`.',
    ),
    "SIM223": Rule(HIGH, B, 0.8, "One side of `and` is always false, so the whole condition is too."),
    "F811": Rule(
        MED,
        B,
        0.7,
        "A second definition replaces the first before anything used it, so the first is dead; with two "
        "tests of the same name only the last one runs.",
    ),
    "F841": Rule(
        MED,
        B,
        0.6,
        "The value is computed and never read. If it was meant to be used, the code that should use it is "
        "using something else.",
    ),
    "F601": Rule(
        MED, B, 0.8, "The same key appears twice in this dict literal; the last value silently wins."
    ),
    "F822": Rule(
        MED,
        B,
        0.85,
        "`__all__` names something the module doesn't define, so `import *` raises AttributeError.",
    ),
    "B006": Rule(
        MED,
        B,
        0.8,
        "The default is created once, when the function is defined, and shared by every call that doesn't "
        "pass the argument, so a change made in one call shows up in the next. Default to `None`.",
    ),
    "B015": Rule(
        MED, B, 0.8, "The comparison's result is thrown away: an `assert` or `=` was probably meant."
    ),
    "B023": Rule(
        MED,
        B,
        0.7,
        "The function reads the loop variable when it is called, not when it is defined, so every function "
        "made in the loop sees its last value. Bind it as a default argument.",
    ),
    "B031": Rule(
        MED, B, 0.8, "A `groupby` group is a one-shot iterator; using it a second time sees nothing."
    ),
    "B032": Rule(MED, B, 0.85, "`x: value` is an annotation, not an assignment, so nothing is assigned."),
    "B034": Rule(
        MED,
        B,
        0.85,
        "The fourth positional argument of `re.sub` is `count` (`maxsplit` for `re.split`), not `flags`, so "
        "a flag passed there is silently used as a number. Pass `flags=` by keyword.",
    ),
    "B035": Rule(MED, B, 0.9, "The key doesn't depend on the loop, so this builds a dict with one entry."),
    "PLW0127": Rule(MED, B, 0.8, "Assigning a name to itself does nothing; `self.x = x` was probably meant."),
    "PLR0124": Rule(
        MED,
        B,
        0.85,
        "Comparing a value with itself always gives the same answer; one side is the wrong name.",
    ),
    "RUF006": Rule(
        MED,
        B,
        0.75,
        "The event loop keeps only a weak reference to a task, so a task nobody stores can be garbage "
        "collected before it finishes. Keep a reference until it is done.",
    ),
    "RUF018": Rule(
        MED, B, 0.85, "Asserts are removed under `python -O`, and the assignment inside goes too."
    ),
    "RUF024": Rule(
        MED,
        B,
        0.9,
        "`dict.fromkeys` gives every key the same mutable value, so appending to one key's list appends to "
        "all of them. Use a dict comprehension.",
    ),
    "RUF034": Rule(MED, B, 0.7, "Both branches give the same value, so the condition does nothing."),
    "PIE794": Rule(MED, B, 0.8, "The class field is defined twice; the second silently replaces the first."),
    "PIE796": Rule(MED, B, 0.85, "Two members have the same value, so the second is an alias, not a member."),
    "S113": Rule(LOW, B, 0.6, "Without a timeout the request can wait forever on a stalled server."),
    # Async code that blocks the event loop.
    "ASYNC251": Rule(
        HIGH,
        P,
        0.85,
        "`time.sleep` in an `async def` blocks the event loop, stalling every other task for that long. "
        "Use `await asyncio.sleep`.",
    ),
    "ASYNC210": Rule(
        MED, P, 0.8, "A blocking HTTP call in an `async def` stalls the event loop while it runs."
    ),
    "B019": Rule(
        MED,
        P,
        0.7,
        "`lru_cache` on a method keeps every `self` it saw alive for the life of the process (a memory "
        "leak), and the cache is shared by all instances.",
    ),
    # Security.
    "S608": Rule(
        HIGH,
        S,
        0.75,
        "The SQL is built by string formatting, so a value with a quote in it can change the query (SQL "
        "injection). Pass values as query parameters.",
    ),
    "S307": Rule(HIGH, S, 0.7, "`eval` runs any expression; on user input that is code execution."),
    "S102": Rule(HIGH, S, 0.6, "`exec` runs any code it is given; on user input that is code execution."),
    "S301": Rule(
        HIGH, S, 0.6, "Unpickling runs code chosen by whoever wrote the data: never load untrusted pickles."
    ),
    "S506": Rule(
        HIGH, S, 0.85, "`yaml.load` without a safe loader can build arbitrary objects. Use `yaml.safe_load`."
    ),
    "S602": Rule(
        HIGH,
        S,
        0.6,
        "With `shell=True` the command goes through a shell, so any user-controlled part of it can inject "
        "commands. Pass a list of arguments instead.",
    ),
    "S605": Rule(
        HIGH, S, 0.6, "This runs the command through a shell, so user input in it can inject commands."
    ),
    "S501": Rule(
        HIGH,
        S,
        0.85,
        "`verify=False` turns off certificate checks, so anyone on the network path can pose as the server.",
    ),
    "PLE2502": Rule(
        HIGH,
        S,
        0.9,
        "A bidirectional control character can make code read differently from how it runs (Trojan Source, "
        "CVE-2021-42574).",
    ),
    "PLE2515": Rule(MED, S, 0.8, "A zero-width space is invisible: two strings that look the same differ."),
    "S105": Rule(
        MED,
        S,
        0.5,
        "This looks like a secret in the source, where everyone who can read the repository sees it.",
    ),
    "S324": Rule(
        MED,
        S,
        0.5,
        "MD5 and SHA-1 are broken for security uses. Use SHA-256, or pass `usedforsecurity=False` if this "
        "isn't one.",
    ),
    # Tests and maintainability.
    "B017": Rule(
        MED,
        T,
        0.7,
        "Expecting any `Exception` makes the test pass on unrelated errors too, a typo's NameError included. "
        "Expect the specific exception.",
    ),
    "E722": Rule(
        LOW,
        M,
        0.6,
        "A bare `except:` also catches KeyboardInterrupt and SystemExit. Catch `Exception`, or narrower.",
    ),
    # CodeLens's own rules (codelens.rules).
    "CL001": Rule(
        HIGH,
        B,
        0.85,
        "Calling a coroutine function only creates a coroutine object: without `await` (or a task) its body "
        "never runs, and Python only warns that it was never awaited.",
    ),
    "CL002": Rule(MED, B, 0.8, "Nothing can reach this line, so whatever it was meant to do never happens."),
    "CL003": Rule(
        HIGH,
        B,
        0.75,
        "Changing a list while looping over it skips or repeats items, and changing a dict or set raises "
        "RuntimeError. Loop over a copy (`list(items)`) or build a new collection.",
    ),
    "CL004": Rule(
        MED,
        B,
        0.6,
        "Without the `f` prefix the braces are not filled in: the text is used exactly as written.",
    ),
}


def ruff_codes() -> list[str]:
    """The ruff rules selected: every table entry that isn't a CodeLens rule or a syntax error."""
    return sorted(code for code in RULES if code != "invalid-syntax" and not code.startswith("CL"))


@dataclass
class StaticResult:
    findings: list[Finding] = field(default_factory=list)
    """Hits on added lines, anchored, in diff order."""
    analysed: list[str] = field(default_factory=list)
    """Paths of the Python files checked."""
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """Python files with added lines that were not checked, with the reason."""
    tool: str = ""
    """The ruff that ran (``ruff 0.16.10``), or empty when it didn't."""
    notes: list[str] = field(default_factory=list)
    """Problems with the pre-pass itself, such as ruff missing or failing; the review goes on without it."""
    outside: int = 0
    """Hits on lines the diff doesn't show, which no comment can be attached to."""
    existing: int = 0
    """Hits on unchanged lines that the old version had too: not this PR's doing, so not reported."""


def find_ruff() -> list[str] | None:
    """The ruff installed next to CodeLens (``python -m ruff``), else one on ``PATH``, else ``None``.

    The first is the version ``codelens[static]`` pins, which matters: the prompt lists its findings, so a
    different ruff can change the prompt and with it every recording key.
    """
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    found = shutil.which("ruff")
    return [found] if found else None


def _skip_reason(file: FileDiff, root: Path) -> str | None:
    if file.new_mode == "120000":
        return "symlink"
    parts = PurePosixPath(file.path).parts
    if unsafe_path(file.path) or file.path.startswith("/") or ".." in parts:
        return "unsafe path"
    full = root / file.path
    try:
        if full.is_symlink() or not full.resolve().is_relative_to(root.resolve()):
            return "symlink"
        if not full.is_file():
            return f"not found in {root}"
        if full.stat().st_size > MAX_FILE_BYTES:
            return f"over {MAX_FILE_BYTES:,} bytes"
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    return None


def _matches(file: FileDiff, lines: list[str]) -> bool:
    """Whether every line the diff shows on the new side is the same in ``lines`` (the file on disk)."""
    for diff_line in file.lines():
        n = diff_line.new_lineno
        if n is not None and (n > len(lines) or lines[n - 1].removesuffix("\r") != diff_line.content):
            return False
    return True


def _read(file: FileDiff, root: Path) -> list[str] | str:
    """The file's lines if it can be checked, else why not."""
    try:
        text = (root / file.path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    # git numbers lines by "\n" only; Python and ruff also end a line at a lone "\r", so after one their
    # line numbers run ahead of the diff's and a finding would land on a later line than its problem.
    if _LONE_CR.search(text):
        return "a carriage return without a line feed (git and Python would number its lines differently)"
    lines = text.split("\n")
    if not _matches(file, lines):
        return f"the file in {root} is not the diff's new version (check out the PR's head commit)"
    return lines


def _run_ruff(
    ruff: Sequence[str], root: Path, paths: list[str], result: StaticResult
) -> dict[int, list[rules.Hit]]:
    """Ruff's hits on ``paths`` (relative to ``root``), keyed by each path's index in ``paths``."""
    try:
        version = subprocess.run(
            [*ruff, "--version"], capture_output=True, text=True, timeout=RUFF_TIMEOUT, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.notes.append(f"ruff could not run: {exc}")
        return {}
    result.tool = version.stdout.strip()
    index = {(root / p).resolve(): i for i, p in enumerate(paths)}
    command = [*ruff, "check", "--isolated", "--no-cache", "--exit-zero", "--output-format", "json"]
    command += ["--target-version", TARGET_VERSION, "--select", ",".join(ruff_codes()), "--"]
    found: dict[int, list[rules.Hit]] = {}
    for start in range(0, len(paths), _CHUNK):
        try:
            proc = subprocess.run(
                [*command, *paths[start : start + _CHUNK]],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=RUFF_TIMEOUT,
            )
            if proc.returncode != 0:
                raise subprocess.SubprocessError(
                    f"exit status {proc.returncode}: {proc.stderr.strip()[:300]}"
                )
            diagnostics = json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            result.notes.append(f"ruff failed, so its rules were not checked: {exc}")
            return {}
        for item in diagnostics if isinstance(diagnostics, list) else []:
            parsed = _ruff_hit(item, index)
            if parsed is not None:
                found.setdefault(parsed[0], []).append(parsed[1])
    return {i: _one_syntax_error(hits) for i, hits in found.items()}


def _ruff_hit(item: Any, index: dict[Path, int]) -> tuple[int, rules.Hit] | None:
    """One diagnostic from ruff's JSON as ``(path index, hit)``, or ``None`` if it isn't one we report."""
    if not isinstance(item, dict):
        return None
    code, message, location, filename = (item.get(k) for k in ("code", "message", "location", "filename"))
    row = location.get("row") if isinstance(location, dict) else None
    if not (isinstance(code, str) and isinstance(message, str) and isinstance(filename, str)):
        return None
    if type(row) is not int or code not in RULES:
        return None
    i = index.get(Path(filename).resolve())
    return None if i is None else (i, rules.Hit(row, code, message))


def _one_syntax_error(hits: list[rules.Hit]) -> list[rules.Hit]:
    """Ruff can report one typo as several syntax errors; one "doesn't parse" comment per file is enough."""
    hits = sorted(hits, key=lambda h: (h.line, h.code))
    first = next((h for h in hits if h.code == "invalid-syntax"), None)
    return [h for h in hits if h.code != "invalid-syntax" or h is first]


def _title(code: str, message: str) -> str:
    text = " ".join(message.split())
    if code == "invalid-syntax":
        text = f"Syntax error: {text}"
    text = text[:1].upper() + text[1:]
    return text if len(text) <= 200 else text[:199] + "…"


def _finding(file: FileDiff, lines: list[str], hit: rules.Hit, patch: PatchSet) -> Finding | None:
    rule = RULES[hit.code]
    tool = "CodeLens" if hit.code.startswith("CL") else "ruff"
    finding = Finding(
        path=file.path,
        line=hit.line,
        severity=rule.severity,
        category=rule.category,
        title=_title(hit.code, hit.message),
        body=f"{rule.why}\n\nFound by {tool} rule `{hit.code}`.",
        confidence=rule.confidence,
        quote=lines[hit.line - 1].removesuffix("\r"),
        source="static",
        rule=hit.code,
    )
    placed = anchored(finding, patch)
    return placed if isinstance(placed, Finding) else None


def old_version(file: FileDiff, new: list[str]) -> list[str]:
    """The file before the change, rebuilt from its new version and the diff: each hunk's new side (context
    and added lines) is swapped for its old side (context and removed lines). Exact when ``new`` matches
    the diff, which :func:`analyse` checks first."""
    old: list[str] = []
    i = 0  # index into new
    for hunk in file.hunks:
        start = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        old += new[i:start]
        i = start
        for line in hunk.lines:
            if line.kind is not LineKind.ADDED:
                old.append(line.content)
            if line.kind is not LineKind.REMOVED:
                i += 1
    return old + new[i:]


def _hits(
    root: Path, items: list[tuple[str, list[str]]], ruff: Sequence[str] | None, result: StaticResult
) -> list[list[rules.Hit]]:
    """Every rule's hits on each ``(path, lines)`` of ``items``; ruff reads the files from ``root``."""
    hits = [rules.check("\n".join(lines)) for _, lines in items]
    if ruff is not None:
        for i, ruff_hits in _run_ruff(ruff, root, [path for path, _ in items], result).items():
            hits[i].extend(ruff_hits)
    return hits


def _old_hits(
    files: list[tuple[FileDiff, list[str]]], ruff: Sequence[str] | None
) -> tuple[list[list[rules.Hit]], list[list[str]]] | None:
    """The same checks on every file's old version (empty for added files), or ``None`` if they failed."""
    olds = [old_version(f, lines) if f.status is not FileStatus.ADDED else [] for f, lines in files]
    scratch = StaticResult()
    with tempfile.TemporaryDirectory(prefix="codelens-old-") as tmp:
        root = Path(tmp)
        for (file, _), old in zip(files, olds, strict=True):
            (root / file.path).parent.mkdir(parents=True, exist_ok=True)
            (root / file.path).write_text("\n".join(old), encoding="utf-8")
        hits = _hits(root, [(f.path, old) for (f, _), old in zip(files, olds, strict=True)], ruff, scratch)
    return None if scratch.notes else (hits, olds)


def _signature(code: str, lines: list[str], line: int) -> tuple[str, str]:
    return code, " ".join(lines[line - 1].split()) if 0 < line <= len(lines) else ""


def analyse(patch: PatchSet, root: Path, *, ruff: Sequence[str] | None) -> StaticResult:
    """Check the Python files ``patch`` adds lines to, as they are in ``root``, with ruff and CodeLens's
    rules, and keep the problems the change introduces.

    That is every hit on an added line, and every hit on an unchanged line shown in the diff that the old
    version didn't have: making a function ``async`` makes its unchanged ``time.sleep`` block the event loop.
    The old version is rebuilt from the new one and the diff (:func:`old_version`) and checked the same way.
    Hits on lines the diff doesn't show can't be commented on and are only counted.

    ``ruff`` is the command to run (see :func:`find_ruff`); with ``None`` only CodeLens's own rules run and a
    note says so. Never raises for a problem with a file or with ruff: those become skips and notes.
    """
    result = StaticResult()
    files: list[tuple[FileDiff, list[str]]] = []
    for file in patch:
        if file.is_binary or file.status is FileStatus.DELETED or not file.path.endswith(".py"):
            continue
        if not file.added_lines():
            continue
        reason = _skip_reason(file, root)
        lines = _read(file, root) if reason is None else reason
        if isinstance(lines, str):
            result.skipped.append((file.path, lines))
        else:
            files.append((file, lines))
            result.analysed.append(file.path)
    if not files:
        return result
    if ruff is None:
        result.notes.append(
            "ruff is not installed, so only CodeLens's own rules ran (pip install 'codelens[static]')"
        )
    hits = _hits(root, [(f.path, lines) for f, lines in files], ruff, result)
    # Without a clean check of the old versions there is no telling new from old on unchanged lines, so
    # only added lines count then.
    old = _old_hits(files, ruff) if not result.notes or ruff is None else None
    for i, (file, lines) in enumerate(files):
        added, shown = set(file.added_lines()), file.commentable_lines(Side.RIGHT)
        before = Counter(_signature(h.code, old[1][i], h.line) for h in old[0][i]) if old else Counter()
        for hit in sorted(hits[i], key=lambda h: (h.line, h.code)):
            if hit.line not in shown or hit.line > len(lines):
                result.outside += 1
                continue
            if hit.line not in added:
                signature = _signature(hit.code, lines, hit.line)
                if old is None or before[signature] > 0:
                    before[signature] -= 1
                    result.existing += 1
                    continue
            finding = _finding(file, lines, hit, patch)
            if finding is not None:
                result.findings.append(finding)
    return result
