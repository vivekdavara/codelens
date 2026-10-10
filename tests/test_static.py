"""The static pre-pass against real git diffs and the real ruff, in temporary repositories."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codelens import static
from codelens.diff import PatchSet, decode_diff, parse_patch
from codelens.static import RULES, analyse, find_ruff, ruff_codes

RUFF = find_ruff()
pytestmark = pytest.mark.skipif(RUFF is None, reason="ruff is a dev dependency")


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_pr(root: Path, before: dict[str, str], after: dict[str, str | None]) -> PatchSet:
    """Commit ``before``, write ``after`` over it (``None`` deletes), and parse ``git diff`` of the two."""
    git(root, "init", "-q")
    for path, text in before.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    for path, new in after.items():
        if new is None:
            (root / path).unlink()
        else:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(new)
    git(root, "add", "-A")
    return parse_patch(decode_diff(git(root, "diff", "--cached", "-M").encode()))


def run(patch: PatchSet, root: Path, ruff: list[str] | None = RUFF) -> static.StaticResult:
    return analyse(patch, root, ruff=ruff)


def test_only_lines_the_pr_adds_are_reported(tmp_path: Path) -> None:
    before = "def old(x):\n    unused = 1\n    return x\n"
    after = before + "\n\ndef new(y):\n    also_unused = 2\n    return y\n"
    result = run(make_pr(tmp_path, {"m.py": before}, {"m.py": after}), tmp_path)
    assert [(f.line, f.rule) for f in result.findings] == [(7, "F841")]
    assert result.existing == 1  # the old unused variable, shown as context, is not this PR's doing
    assert result.analysed == ["m.py"] and result.tool.startswith("ruff ")


def test_a_static_finding_is_anchored_like_a_model_finding(tmp_path: Path) -> None:
    after = "def f(x):\n    v = 1\n    return x\n\n\ndef g(x):\n    v = 1\n    return x\n"
    (first, second) = run(make_pr(tmp_path, {}, {"m.py": after}), tmp_path).findings
    assert (first.line, second.line) == (2, 7)
    assert first.quote == second.quote == "    v = 1"
    assert (first.occurrence, second.occurrence) == (0, 1)  # two identical lines stay apart
    assert first.source == "static" and first.rule == "F841"
    assert first.severity is RULES["F841"].severity and first.confidence == RULES["F841"].confidence
    assert first.title == "Local variable `v` is assigned to but never used"
    assert first.body.startswith(RULES["F841"].why) and first.body.endswith("Found by ruff rule `F841`.")


def test_codelens_rules_run_alongside_ruff(tmp_path: Path) -> None:
    after = "async def flush():\n    pass\n\n\ndef close():\n    flush()\n"
    (finding,) = run(make_pr(tmp_path, {}, {"m.py": after}), tmp_path).findings
    assert (finding.line, finding.rule) == (6, "CL001")
    assert finding.body.endswith("Found by CodeLens rule `CL001`.")
    assert finding.title.startswith("`flush()` is an `async def`")


def test_without_ruff_only_codelens_rules_run(tmp_path: Path) -> None:
    after = "def f(items):\n    unused = 1\n    for x in items:\n        items.remove(x)\n"
    result = run(make_pr(tmp_path, {}, {"m.py": after}), tmp_path, ruff=None)
    assert [f.rule for f in result.findings] == ["CL003"]
    assert result.tool == "" and "ruff is not installed" in result.notes[0]


@pytest.mark.parametrize(
    ("ruff", "note"),
    [
        (["/nonexistent/ruff"], "ruff could not run"),
        ([sys.executable, "-c", "import sys; sys.exit(3)"], "ruff could not run"),  # --version fails
    ],
)
def test_a_ruff_that_cannot_run_becomes_a_note(tmp_path: Path, ruff: list[str], note: str) -> None:
    after = "def f(items):\n    for x in items:\n        items.remove(x)\n"
    result = run(make_pr(tmp_path, {}, {"m.py": after}), tmp_path, ruff=ruff)
    assert [f.rule for f in result.findings] == ["CL003"]  # the review goes on without ruff
    assert result.notes and result.notes[0].startswith(note)


@pytest.mark.parametrize(
    ("script", "note"),
    [
        ("import sys; print('ruff 9'); sys.exit(0 if '--version' in sys.argv else 2)", "exit status 2"),
        ("print('ruff 9' if __import__('sys').argv[1:] == ['--version'] else 'not json')", "Expecting value"),
    ],
)
def test_a_ruff_that_fails_or_prints_garbage_becomes_a_note(tmp_path: Path, script: str, note: str) -> None:
    fake = tmp_path / "fake_ruff.py"
    fake.write_text(script)
    repo = tmp_path / "repo"
    repo.mkdir()
    after = "def f(items):\n    unused = 1\n"
    result = run(make_pr(repo, {}, {"m.py": after}), repo, ruff=[sys.executable, str(fake)])
    assert result.findings == [] and result.tool == "ruff 9"
    assert note in result.notes[0] and "its rules were not checked" in result.notes[0]


def test_the_prs_own_ruff_config_cannot_switch_rules_off(tmp_path: Path) -> None:
    config = '[tool.ruff]\nexclude = ["*.py"]\n[tool.ruff.lint]\nignore = ["F841"]\n'
    after = "def f():\n    unused = 1\n"
    patch = make_pr(
        tmp_path, {}, {"pyproject.toml": config, "ruff.toml": "lint.select = []\n", "m.py": after}
    )
    assert [f.rule for f in run(patch, tmp_path).findings] == ["F841"]


def test_a_file_on_disk_that_is_not_the_diffs_new_version_is_skipped(tmp_path: Path) -> None:
    patch = make_pr(tmp_path, {"m.py": "x = 1\n"}, {"m.py": "x = 1\nunused = [y for y in []]\n"})
    (tmp_path / "m.py").write_text("x = 1\nsomething = 'else'\n")  # say, the merge commit
    result = run(patch, tmp_path)
    assert result.findings == [] and result.analysed == []
    assert result.skipped == [
        ("m.py", f"the file in {tmp_path} is not the diff's new version (check out the PR's head commit)")
    ]


def test_a_file_shorter_than_the_diff_is_skipped(tmp_path: Path) -> None:
    patch = make_pr(tmp_path, {}, {"m.py": "a = 1\nb = 2\n"})
    (tmp_path / "m.py").write_text("a = 1\n")
    assert run(patch, tmp_path).skipped[0][1].startswith("the file in")


def test_a_crlf_file_matches_its_diff(tmp_path: Path) -> None:
    patch = make_pr(tmp_path, {}, {"m.py": "def f():\r\n    unused = 1\r\n"})
    assert [(f.line, f.rule, f.quote) for f in run(patch, tmp_path).findings] == [
        (2, "F841", "    unused = 1")
    ]


def test_files_that_are_not_checked(tmp_path: Path) -> None:
    before = {"gone.py": "x = 1\n", "same.py": "y = 2\n"}
    after: dict[str, str | None] = {
        "gone.py": None,  # deleted
        "notes.md": "unused = 1\n",  # not Python
        "renamed.py": "y = 2\n",  # a pure rename adds no lines
        "same.py": None,
    }
    result = run(make_pr(tmp_path, before, after), tmp_path)
    assert result.analysed == [] and result.skipped == [] and result.findings == []


def test_unsafe_paths_and_symlinks_are_skipped(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = make_pr(repo, {}, {"link.py": "x = 1\n"})
    (repo / "link.py").unlink()
    (repo / "link.py").symlink_to(outside)  # on disk a symlink, whatever the diff said
    escape = (
        "diff --git a/../outside.py b/../outside.py\n--- a/../outside.py\n+++ b/../outside.py\n"
        "@@ -1 +1 @@\n-x\n+secret = 1\n"
    )
    patch.files += parse_patch(escape).files
    result = run(patch, repo)
    assert result.skipped == [("link.py", "symlink"), ("../outside.py", "unsafe path")]


def test_a_symlink_in_the_diff_is_skipped(tmp_path: Path) -> None:
    diff = (
        "diff --git a/l.py b/l.py\nnew file mode 120000\n--- /dev/null\n+++ b/l.py\n"
        "@@ -0,0 +1 @@\n+target.py\n\\ No newline at end of file\n"
    )
    assert run(parse_patch(diff), tmp_path).skipped == [("l.py", "symlink")]


def test_missing_non_utf8_and_oversized_files_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = make_pr(tmp_path, {}, {"a.py": "x = 1\n", "b.py": "y = 2\n", "c.py": "z = 3\n"})
    (tmp_path / "a.py").unlink()
    (tmp_path / "b.py").write_bytes(b"y = '\xff'\n")
    monkeypatch.setattr(static, "MAX_FILE_BYTES", 3)
    reasons = dict(run(patch, tmp_path).skipped)
    assert reasons == {"a.py": f"not found in {tmp_path}", "b.py": "over 3 bytes", "c.py": "over 3 bytes"}
    monkeypatch.setattr(static, "MAX_FILE_BYTES", 2_000_000)
    assert dict(run(patch, tmp_path).skipped)["b.py"] == "not UTF-8"


def test_one_syntax_error_per_file_is_reported(tmp_path: Path) -> None:
    after = "def f(:\n    pass\n\n\ndef g(:\n    pass\n"
    (finding,) = run(make_pr(tmp_path, {}, {"m.py": after}), tmp_path).findings
    assert finding.rule == "invalid-syntax" and finding.line == 1 and finding.severity.value == "critical"
    assert finding.title.startswith("Syntax error: ")


def test_ruff_runs_in_chunks_and_every_file_gets_its_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(static, "_CHUNK", 2)
    after = {f"pkg/m{i}.py": "def f():\n    unused = 1\n" for i in range(5)}
    result = run(make_pr(tmp_path, {}, dict(after)), tmp_path)
    assert sorted(f.path for f in result.findings) == sorted(after)


def test_modern_syntax_is_not_a_syntax_error(tmp_path: Path) -> None:
    after = (
        "def f(x):\n    match x:\n        case [a, *rest]:\n            return a, rest\n"
        "    type T = int\n    return T\n"
    )
    assert run(make_pr(tmp_path, {}, {"m.py": after}), tmp_path).findings == []


def test_every_selected_ruff_rule_exists_and_is_stable() -> None:
    assert RUFF is not None
    listing = subprocess.run(
        [*RUFF, "rule", "--all", "--output-format", "json"], capture_output=True, check=True
    )
    known = {rule["code"]: rule for rule in json.loads(listing.stdout)}
    for code in ruff_codes():
        assert code in known, f"{code} is not a ruff rule"
        assert not known[code].get("preview"), f"{code} is preview-only: its behaviour can change"


def test_the_rule_table_is_well_formed() -> None:
    for code, rule in RULES.items():
        assert 0 < rule.confidence <= 1, code
        assert rule.why.endswith((".", ")")) and len(rule.why) < 400, code
    assert {"CL001", "CL002", "CL003", "CL004"} <= RULES.keys()


def test_find_ruff_prefers_the_installed_package_then_path(monkeypatch: pytest.MonkeyPatch) -> None:
    assert find_ruff() == [sys.executable, "-m", "ruff"]
    monkeypatch.setattr(static.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(static.shutil, "which", lambda name: "/usr/local/bin/ruff")
    assert find_ruff() == ["/usr/local/bin/ruff"]
    monkeypatch.setattr(static.shutil, "which", lambda name: None)
    assert find_ruff() is None


def test_diagnostics_that_are_not_ours_are_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = make_pr(repo, {}, {"m.py": "a = 1\nb = 2\n"})
    target = str((repo / "m.py").resolve())
    good = {"code": "F841", "message": "kept", "filename": target, "location": {"row": 2}}
    items = [
        1,
        {**good, "code": 5},
        {**good, "location": {"row": "2"}},
        {**good, "location": {"row": True}},
        {**good, "code": "E501"},  # not selected by CodeLens: never reported
        {**good, "filename": str(tmp_path / "elsewhere.py")},
        good,
    ]
    fake = tmp_path / "fake_ruff.py"
    fake.write_text(
        "import json, sys\n"
        "print('ruff 9') if sys.argv[1:] == ['--version'] else print(json.dumps(" + repr(items) + "))\n"
    )
    result = run(patch, repo, ruff=[sys.executable, str(fake)])
    assert [(f.line, f.title) for f in result.findings] == [(2, "Kept")]


def test_unreadable_files_are_skipped_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = make_pr(tmp_path, {}, {"a.py": "x = 1\n"})

    def denied(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(static.Path, "read_bytes", denied)
    assert run(patch, tmp_path).skipped == [("a.py", "unreadable: Permission denied")]

    def broken(self: Path, strict: bool = False) -> Path:
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(static.Path, "resolve", broken)
    assert run(patch, tmp_path).skipped == [("a.py", "unreadable: Too many levels of symbolic links")]


def test_a_hit_that_does_not_anchor_is_dropped(tmp_path: Path) -> None:
    # The same path twice: findings anchor on the first entry, whose text is not the file's.
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1,2 @@\n x = 1\n+other = 2\n"
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1,2 @@\n x = 1\n+def f():\n"
    )
    (tmp_path / "m.py").write_text("x = 1\ndef f():\n")
    result = run(parse_patch(diff), tmp_path)
    assert result.findings == [] and result.analysed == ["m.py"]


def test_a_change_that_makes_an_unchanged_line_wrong_is_reported(tmp_path: Path) -> None:
    before = "import time\n\n\ndef run(job):\n    time.sleep(1)\n    job.done()\n"
    after = "import time\n\n\nasync def run(job):\n    time.sleep(1)\n    job.done()\n"
    result = run(make_pr(tmp_path, {"w.py": before}, {"w.py": after}), tmp_path)
    # Line 5 is unchanged context, but only the new version is a coroutine that blocks the event loop.
    assert [(f.line, f.rule) for f in result.findings] == [(5, "ASYNC251")]
    assert result.existing == 0


def test_a_problem_on_an_unchanged_line_that_was_already_there_is_not(tmp_path: Path) -> None:
    before = "import time\n\n\nasync def run(job):\n    time.sleep(1)\n    job.done()\n"
    after = before.replace("job.done()", "job.finish()")
    result = run(make_pr(tmp_path, {"w.py": before}, {"w.py": after}), tmp_path)
    assert result.findings == [] and result.existing == 1


def test_the_same_problem_twice_counts_each_occurrence(tmp_path: Path) -> None:
    # Both sleeps are unchanged lines; the old version had one ASYNC251, the new one has two.
    before = "import time\n\n\nasync def a():\n    time.sleep(1)\n\n\ndef b():\n    time.sleep(1)\n"
    after = before.replace("def b", "async def b")
    result = run(make_pr(tmp_path, {"m.py": before}, {"m.py": after}), tmp_path)
    assert [(f.line, f.rule) for f in result.findings] == [(9, "ASYNC251")] and result.existing == 1


def test_hits_on_lines_the_diff_does_not_show_are_only_counted(tmp_path: Path) -> None:
    before = "def f():\n    x = 1\n" + "\n" * 10 + "y = 1\n"
    after = before.replace("y = 1", "y = 2")
    result = run(make_pr(tmp_path, {"m.py": before}, {"m.py": after}), tmp_path)
    assert result.findings == [] and result.outside == 1 and result.existing == 0


def test_when_the_old_version_cannot_be_checked_only_added_lines_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = "import time\n\n\ndef run(job):\n    time.sleep(1)\n    unused = 1\n"
    after = before.replace("def run", "async def run").replace("unused = 1", "unused = 2")
    patch = make_pr(tmp_path, {"w.py": before}, {"w.py": after})
    monkeypatch.setattr(static, "_old_hits", lambda files, ruff: None)
    result = run(patch, tmp_path)
    assert [(f.line, f.rule) for f in result.findings] == [(6, "F841")] and result.existing == 1


def test_old_version_rebuilds_the_file_before_the_change(tmp_path: Path) -> None:
    before = "".join(f"line {n}\n" for n in range(1, 30))
    after = (
        before.replace("line 3\n", "")
        .replace("line 15\n", "line 15\nnew a\nnew b\n")
        .replace("line 28\n", "changed 28\n")
    )
    patch = make_pr(tmp_path, {"t.py": before}, {"t.py": after})
    assert static.old_version(patch.files[0], after.split("\n")) == before.split("\n")
    (tmp_path / "added").mkdir()
    added = make_pr(tmp_path / "added", {}, {"t.py": after})
    assert static.old_version(added.files[0], after.split("\n")) == [""]
