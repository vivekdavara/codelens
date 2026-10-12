"""Which functions the test generator writes tests for: real git diffs in temporary repositories."""

from pathlib import Path

import pytest
from test_static import make_pr

from codelens.testgen import find_targets, module_name

BEFORE = """\
import functools


def total(prices):
    return sum(prices)


def unchanged(x):
    return x


class Cart:
    def size(self):
        return 0
"""

AFTER = '''\
import functools


def total(prices, discount=0):
    """The sum, less a discount."""
    gross = sum(prices)
    return gross - discount


def unchanged(x):
    return x


@functools.cache
def rate(code):
    return {"A": 1}.get(code, 0)


class Cart:
    def size(self):
        return 1
'''


def test_changed_top_level_functions_are_targets(tmp_path: Path) -> None:
    found = find_targets(make_pr(tmp_path, {"shop/cart.py": BEFORE}, {"shop/cart.py": AFTER}), tmp_path)
    assert [(t.qualname, t.start, t.body_start, t.end) for t in found.targets] == [
        ("shop.cart.total", 4, 5, 7),
        ("shop.cart.rate", 14, 16, 16),  # the decorator starts it; the body is what a test runs
    ]
    assert found.targets[0].changed == frozenset({4, 5, 6, 7})
    assert found.targets[0].import_root == "" and found.targets[0].path == "shop/cart.py"
    assert found.sources["shop/cart.py"][3] == "def total(prices, discount=0):"
    assert found.skipped == []  # the method's change is not a target, and says nothing


def test_src_layout_imports_from_src(tmp_path: Path) -> None:
    found = find_targets(make_pr(tmp_path, {}, {"src/pkg/mod.py": "def f():\n    return 1\n"}), tmp_path)
    assert [(t.import_root, t.qualname) for t in found.targets] == [("src", "pkg.mod.f")]


def test_files_that_give_no_target_say_why(tmp_path: Path) -> None:
    after = {
        "tests/test_x.py": "def test_a():\n    pass\n",
        "scripts/build-docs.py": "def main():\n    pass\n",
        "constants.py": "LIMIT = 10\n",
        "broken.py": "def f(:\n",
        "notes.md": "# hi\n",
    }
    found = find_targets(make_pr(tmp_path, {}, after), tmp_path)
    assert found.targets == []
    assert sorted(found.skipped) == [
        ("broken.py", "does not parse"),
        ("constants.py", "no changed top-level function"),
        ("scripts/build-docs.py", "not importable as a module"),
        ("tests/test_x.py", "a test file"),
    ]


def test_a_checkout_that_is_not_the_diffs_new_version_is_skipped(tmp_path: Path) -> None:
    patch = make_pr(tmp_path, {}, {"m.py": "def f():\n    return 1\n"})
    (tmp_path / "m.py").write_text("def f():\n    return 2\n")
    found = find_targets(patch, tmp_path)
    assert found.targets == [] and "not the diff's new version" in found.skipped[0][1]


def test_a_redefined_function_is_the_last_definition(tmp_path: Path) -> None:
    after = "def f():\n    return 1\n\n\ndef f():\n    return 2\n"
    found = find_targets(make_pr(tmp_path, {}, {"m.py": after}), tmp_path)
    assert [(t.name, t.start) for t in found.targets] == [("f", 5)]


def test_deleted_and_unchanged_files_are_ignored(tmp_path: Path) -> None:
    before = {"gone.py": "def f():\n    pass\n", "same.py": "def g():\n    pass\n\n\nX = 1\n"}
    found = find_targets(
        make_pr(tmp_path, before, {"gone.py": None, "same.py": before["same.py"] + "Y = 2\n"}), tmp_path
    )
    assert found.targets == [] and found.skipped == [("same.py", "no changed top-level function")]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("shop/pricing.py", ("", "shop.pricing")),
        ("shop/__init__.py", ("", "shop")),
        ("src/codelens/diff.py", ("src", "codelens.diff")),
        ("src/__init__.py", None),
        ("src.py", ("", "src")),
        ("class/x.py", None),
        ("a/b-c.py", None),
        ("a/b.txt", None),
        ("__init__.py", None),
    ],
)
def test_module_name(path: str, expected: tuple[str, str] | None) -> None:
    assert module_name(path) == expected
