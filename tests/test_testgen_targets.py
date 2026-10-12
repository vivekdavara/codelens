"""Which functions the test generator writes tests for: real git diffs in temporary repositories."""

from pathlib import Path

import pytest
from test_static import make_pr

from codelens.diff import parse_patch
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
    # Statement first lines: the def line (4) runs on import and is no body statement.
    assert found.targets[0].changed == frozenset({5, 6, 7})
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


def test_a_file_with_only_removals_is_ignored(tmp_path: Path) -> None:
    before = "def f():\n    x = 1\n    return x\n"
    found = find_targets(make_pr(tmp_path, {"m.py": before}, {"m.py": "def f():\n    return 1\n"}), tmp_path)
    assert [t.changed for t in found.targets] == [frozenset({2})]
    (tmp_path / "only").mkdir()
    patch = make_pr(tmp_path / "only", {"m.py": before}, {"m.py": "def f():\n    x = 1\n"})
    assert (
        find_targets(patch, tmp_path / "only").targets == []
        and find_targets(patch, tmp_path / "only").skipped == []
    )


def test_a_body_on_the_def_line_is_not_a_target(tmp_path: Path) -> None:
    after = "def f(x): return x + 1\n\n\ndef g(\n    x,\n): return x\n\n\ndef h(x):\n    return x\n"
    found = find_targets(make_pr(tmp_path, {}, {"m.py": after}), tmp_path)
    assert [t.name for t in found.targets] == ["h"]
    assert found.skipped == [
        ("m.py", "f: its body is on the def line, which import runs"),
        ("m.py", "g: its body is on the def line, which import runs"),
    ]


def test_an_edit_inside_a_multi_line_statement_counts_as_that_statement(tmp_path: Path) -> None:
    before = "def f(a, b):\n    return sum(\n        a,\n        b,\n    )\n"
    found = find_targets(
        make_pr(tmp_path, {"m.py": before}, {"m.py": before.replace("b,", "b * 2,")}), tmp_path
    )
    assert [t.changed for t in found.targets] == [frozenset({2})]  # coverage reports the return on line 2


def test_when_src_is_a_package_its_modules_import_from_the_root(tmp_path: Path) -> None:
    patch = make_pr(tmp_path, {"src/__init__.py": ""}, {"src/utils.py": "def f():\n    return 1\n"})
    assert [(t.import_root, t.qualname) for t in find_targets(patch, tmp_path).targets] == [
        ("", "src.utils.f")
    ]


def test_test_files_and_test_named_functions_are_not_targets(tmp_path: Path) -> None:
    after = {
        "shop/tests.py": "def helper():\n    return 1\n",
        "shop/test.py": "def helper():\n    return 1\n",
        "conftest.py": "def fx():\n    return 1\n",
        "db.py": "def test_connection(host):\n    return host\n\n\ndef connect(host):\n    return host\n",
    }
    found = find_targets(make_pr(tmp_path, {}, after), tmp_path)
    assert [t.qualname for t in found.targets] == ["db.connect"]
    assert sorted(found.skipped) == [
        ("conftest.py", "a test file"),
        ("db.py", "test_connection: named like a test, so pytest would collect it"),
        ("shop/test.py", "a test file"),
        ("shop/tests.py", "a test file"),
    ]


def test_a_byte_order_mark_does_not_stop_a_file_parsing(tmp_path: Path) -> None:
    found = find_targets(make_pr(tmp_path, {}, {"bom.py": "\ufeffdef f():\n    return 1\n"}), tmp_path)
    assert [t.qualname for t in found.targets] == ["bom.f"]
    assert found.sources["bom.py"][0] == "def f():"


def test_a_symlink_loop_in_the_checkout_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "pkg").symlink_to("pkg")
    diff = "diff --git a/pkg/mod.py b/pkg/mod.py\nnew file mode 100644\n--- /dev/null\n+++ b/pkg/mod.py\n"
    found = find_targets(parse_patch(diff + "@@ -0,0 +1,2 @@\n+def f():\n+    return 1\n"), tmp_path)
    assert found.targets == [] and found.skipped == [("pkg/mod.py", "symlink")]
