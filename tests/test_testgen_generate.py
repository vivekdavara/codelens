"""The whole test generator on a small project: a real diff, a real suite, real pytest runs."""

import json
from pathlib import Path

import pytest
from test_static import make_pr

from codelens.diff import PatchSet
from codelens.providers import Completion, Request, Usage
from codelens.sandbox import Outcome
from codelens.testgen import generate, kept_files

BEFORE = """\
def grade(score):
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    return "C"
"""

AFTER = """\
def grade(score):
    if score < 0 or score > 100:
        raise ValueError(f"score out of range: {score}")
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    return "C"


def curve(scores, points):
    return [min(100, s + points) for s in scores]
"""

SUITE = 'from school.grades import grade\n\n\ndef test_a():\n    assert grade(95) == "A"\n'
IMPORTS = "import pytest\n\nfrom school.grades import curve, grade\n\n\n"


class Scripted:
    """A provider with one hand-written answer, which records what it was asked."""

    name = "scripted"

    def __init__(self, *tests: tuple[str, str]) -> None:
        self.text = json.dumps({"tests": [{"function": f, "code": IMPORTS + c} for f, c in tests]})
        self.requests: list[Request] = []

    def complete(self, request: Request) -> Completion:
        self.requests.append(request)
        return Completion(self.text, "hand-written", Usage(100, 200))


def project(tmp_path: Path) -> PatchSet:
    return make_pr(
        tmp_path,
        {"school/grades.py": BEFORE, "tests/test_grades.py": SUITE},
        {"school/grades.py": AFTER},
    )


GRADE, CURVE = "school.grades.grade", "school.grades.curve"
B_AND_C = (GRADE, 'def test_b_and_c():\n    assert grade(85) == "B"\n    assert grade(50) == "C"\n')
AGAIN = (GRADE, 'def test_a_again():\n    assert grade(91) == "A"\n')
WRONG = (GRADE, "def test_hundred():\n    with pytest.raises(ValueError):\n        grade(100)\n")
NEGATIVE = (GRADE, "def test_negative():\n    with pytest.raises(ValueError):\n        grade(-1)\n")
CAPPED = (CURVE, "def test_capped():\n    assert curve([95, 50], 10) == [100, 60]\n")
HANGS = (CURVE, "def test_hangs():\n    while True:\n        pass\n")


def test_only_passing_tests_that_run_new_lines_are_kept(tmp_path: Path) -> None:
    provider = Scripted(B_AND_C, AGAIN, WRONG, NEGATIVE, CAPPED, HANGS)
    result = generate(project(tmp_path), tmp_path, provider, tests=[Path("tests")], timeout=3)
    assert len(provider.requests) == 1 and result.model == "hand-written"
    assert result.baseline is not None and result.baseline.outcome is Outcome.PASSED
    summary = [(v.candidate.index, v.outcome.value, v.kept, len(v.new_lines)) for v in result.verdicts]
    assert summary == [
        (0, "passed", True, 3),  # lines 6-8: the B and C branches the suite never took
        (1, "passed", False, 0),  # the suite already runs the A branch
        (2, "failed", False, 0),  # 100 is in range: the test is wrong, so it goes, whatever it ran
        (3, "passed", True, 1),  # the new raise on line 3
        (4, "passed", True, 1),  # curve's body
        (5, "timeout", False, 0),
    ]
    assert result.verdicts[0].new_lines == [
        ("school/grades.py", 6),
        ("school/grades.py", 7),
        ("school/grades.py", 8),
    ]
    assert (
        "passes, but runs no new line" in result.verdicts[1].reason and result.verdicts[2].reason == "fails"
    )
    assert result.verdicts[5].reason == "timed out"
    coverage = {
        c.target.name: (c.statements, c.before, c.after, c.changed, c.changed_before, c.changed_after)
        for c in result.coverage
    }
    # grade's body is lines 2-8; the suite ran 2, 4 and 5. Lines 2 and 3 are the PR's (the def line, 1, runs
    # on import and is not counted). curve's body is line 12.
    assert coverage == {"grade": (7, 3, 7, 2, 1, 2), "curve": (1, 0, 1, 1, 0, 1)}
    assert result.totals() == {
        "statements": 8,
        "before": 3,
        "after": 8,
        "changed": 3,
        "changed_before": 1,
        "changed_after": 3,
    }
    assert list(kept_files(result)) == [
        "test_codelens_school_grades_grade_0.py",
        "test_codelens_school_grades_grade_3.py",
        "test_codelens_school_grades_curve_4.py",
    ]


def test_without_a_suite_nothing_counts_as_covered_before(tmp_path: Path) -> None:
    result = generate(project(tmp_path), tmp_path, Scripted(AGAIN, CAPPED))
    assert result.baseline is None
    assert [v.kept for v in result.verdicts] == [True, True]
    assert result.totals()["before"] == 0 and result.totals()["after"] == 4  # lines 2, 4, 5 and 12


def test_a_failing_suite_still_counts_what_it_ran(tmp_path: Path) -> None:
    patch = project(tmp_path)
    (tmp_path / "tests" / "test_broken.py").write_text("def test_x():\n    assert False\n")
    result = generate(patch, tmp_path, Scripted(AGAIN), tests=[Path("tests")])
    assert result.baseline is not None and result.baseline.outcome is Outcome.FAILED
    assert [v.kept for v in result.verdicts] == [False] and result.totals()["before"] == 3


def test_no_changed_function_means_no_call(tmp_path: Path) -> None:
    patch = make_pr(tmp_path, {}, {"README.md": "# hi\n", "tests/test_x.py": "def test_x():\n    pass\n"})

    class Refuses:
        name = "refuses"

        def complete(self, request: Request) -> Completion:
            raise AssertionError("no call should be made")

    result = generate(patch, tmp_path, Refuses())
    assert result.targets == [] and result.skipped == [("tests/test_x.py", "a test file")]


def test_candidates_over_the_cap_are_counted_not_run(tmp_path: Path) -> None:
    result = generate(project(tmp_path), tmp_path, Scripted(B_AND_C, AGAIN, CAPPED), max_tests=2)
    assert len(result.verdicts) == 2 and result.over_cap == 1
    with pytest.raises(ValueError):
        generate(PatchSet([]), tmp_path, Scripted(), max_tests=0)


def test_rejected_items_never_run(tmp_path: Path) -> None:
    provider = Scripted(("school.grades.nope", "def test_x():\n    pass\n"), (GRADE, "import os\n"))
    result = generate(project(tmp_path), tmp_path, provider)
    assert result.verdicts == [] and [r.kind for r in result.rejections] == ["unknown-function", "no-tests"]


def test_a_file_no_run_could_measure_is_reported_as_unmeasured(tmp_path: Path) -> None:
    result = generate(project(tmp_path), tmp_path, Scripted(HANGS), timeout=1)
    assert result.coverage == [] and [t.name for t in result.unmeasured] == ["grade", "curve"]
