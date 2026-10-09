import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeServer, Reply

from codelens.cli import main
from codelens.diff import parse_patch
from codelens.prompts import build_prompt
from codelens.providers import Completion
from codelens.providers.recorded import HAND_WRITTEN, write_recording

DIFF = """\
diff --git a/svc/pay.py b/svc/pay.py
--- a/svc/pay.py
+++ b/svc/pay.py
@@ -1,2 +1,5 @@
 def refund(amount, fee):
-    return amount - fee
+    net = amount - fee
+    if net < 0:
+        net = 0
+    return net
"""
FINDING = {
    "path": "svc/pay.py",
    "line": 3,
    "quote": "    if net < 0:",
    "severity": "high",
    "category": "bug",
    "title": "Clamping hides the error",
    "body": "A refund smaller than the fee becomes 0 instead of failing.",
    "confidence": 0.8,
}
ANSWER = json.dumps({"findings": [FINDING, {**FINDING, "line": 4}]})  # the second one is misquoted


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CODELENS_PROVIDER",
        "CODELENS_MODEL",
        "CODELENS_RECORDINGS",
        "CODELENS_BASE_URL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_API_URL",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def diff_file(tmp_path: Path) -> Path:
    path = tmp_path / "pr.diff"
    path.write_text(DIFF)
    return path


@pytest.fixture
def recordings(tmp_path: Path) -> Path:
    directory = tmp_path / "recordings"
    request = build_prompt(parse_patch(DIFF)).request
    write_recording(directory, request, Completion(ANSWER, HAND_WRITTEN), "recorded")
    return directory


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_review_from_recordings_is_a_dry_run(
    capsys: pytest.CaptureFixture[str], diff_file: Path, recordings: Path
) -> None:
    code, out, err = run(capsys, "review", str(diff_file), "--recordings", str(recordings))
    assert code == 0
    assert out.splitlines() == [
        "svc/pay.py:3  high  bug  Clamping hides the error  (confidence 0.80)",
        "1 finding on 1 reviewed file (recorded: hand-written, 0 input / 0 output tokens)",
        "dropped 1: misquoted 1",
        "  [1] misquoted: svc/pay.py:4 is 'net = 0', not 'if net < 0:'",
    ]
    assert "dry run: nothing posted" in err


def test_review_json(capsys: pytest.CaptureFixture[str], diff_file: Path, recordings: Path) -> None:
    code, out, _ = run(
        capsys, "review", str(diff_file), "--recordings", str(recordings), "--json", "--commit", "c0"
    )
    assert code == 0
    data = json.loads(out)
    assert [f["line"] for f in data["findings"]] == [3]
    assert data["rejections"] == [
        {"index": 1, "kind": "misquoted", "detail": "svc/pay.py:4 is 'net = 0', not 'if net < 0:'"}
    ]
    assert data["payload"]["commit_id"] == "c0"
    assert [(c["path"], c["line"]) for c in data["payload"]["comments"]] == [("svc/pay.py", 3)]


def test_a_missing_recording_fails_loudly(
    capsys: pytest.CaptureFixture[str], diff_file: Path, tmp_path: Path
) -> None:
    code, _, err = run(capsys, "review", str(diff_file), "--recordings", str(tmp_path / "empty"))
    assert code == 1 and "review failed: no recorded response" in err


def claude_says(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 700, "output_tokens": 90},
    }


def test_record_then_replay(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeServer,
    diff_file: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CODELENS_BASE_URL", fake_server.url)
    fake_server.reply(Reply(200, claude_says(ANSWER)))
    recs = tmp_path / "recs"
    code, out, err = run(
        capsys, "review", str(diff_file), "--provider", "anthropic", "--record", "--recordings", str(recs)
    )
    assert code == 0 and "(anthropic: claude-opus-5-5, 700 input / 90 output tokens)" in out
    (recorded,) = recs.iterdir()
    assert f"recorded {recorded}" in err
    assert json.loads(recorded.read_text())["provider"] == "anthropic"
    # The saved answer replays without the vendor: the fake server is not called again.
    code, out_again, _ = run(capsys, "review", str(diff_file), "--recordings", str(recs))
    assert code == 0 and out_again.splitlines()[0] == out.splitlines()[0]
    assert len(fake_server.requests) == 1


def test_post_sends_one_review(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeServer,
    diff_file: Path,
    recordings: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_API_URL", fake_server.url)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    fake_server.reply(Reply(200, []), Reply(200, []))  # no earlier comments or reviews
    fake_server.reply(
        Reply(200, {"id": 81, "html_url": "https://github.com/o/r/pull/3#pullrequestreview-81"})
    )
    args = (
        "review",
        str(diff_file),
        "--recordings",
        str(recordings),
        "--post",
        "--pr",
        "3",
        "--commit",
        "c0",
    )
    code, _, err = run(capsys, *args)
    assert code == 0
    assert "posted review 81 with 1 findings as line comments" in err
    seen = fake_server.requests[-1]
    assert seen.path == "/repos/o/r/pulls/3/reviews" and seen.body["commit_id"] == "c0"
    # The same review again, after another push: the finding is already on the PR.
    fake_server.reply(Reply(200, seen.body["comments"]), Reply(200, []))
    code, _, err = run(capsys, *args)
    assert code == 0 and "all 1 findings were already posted by an earlier review: nothing posted" in err
    assert [r.method for r in fake_server.requests] == ["GET", "GET", "POST", "GET", "GET"]


def test_post_with_no_findings_posts_nothing(
    capsys: pytest.CaptureFixture[str], fake_server: FakeServer, diff_file: Path, tmp_path: Path
) -> None:
    recs = tmp_path / "recs"
    request = build_prompt(parse_patch(DIFF)).request
    write_recording(recs, request, Completion('{"findings": []}', HAND_WRITTEN), "recorded")
    code, _, err = run(
        capsys, "review", str(diff_file), "--recordings", str(recs), "--post", "--repo", "o/r", "--pr", "3"
    )
    assert code == 0 and "no findings: nothing to post" in err
    assert fake_server.requests == []


def test_a_github_failure_is_an_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeServer,
    diff_file: Path,
    recordings: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_API_URL", fake_server.url)
    fake_server.reply(Reply(403, {"message": "Resource not accessible by integration"}))
    args = ("review", str(diff_file), "--recordings", str(recordings), "--post", "--repo", "o/r", "--pr", "3")
    code, _, err = run(capsys, *args)
    assert code == 1 and "Resource not accessible by integration" in err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--post",), "--post needs --repo"),
        (("--post", "--repo", "o/r"), "--post needs --repo"),
        (("--record",), "--record needs a live provider"),
    ],
)
def test_usage_errors(
    capsys: pytest.CaptureFixture[str], diff_file: Path, args: tuple[str, ...], message: str
) -> None:
    code, _, err = run(capsys, "review", str(diff_file), *args)
    assert code == 2 and message in err


def test_configuration_errors_exit_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, diff_file: Path
) -> None:
    code, _, err = run(capsys, "review", str(diff_file), "--provider", "anthropic")
    assert code == 1 and "needs ANTHROPIC_API_KEY" in err
    monkeypatch.setenv("CODELENS_PROVIDER", "gemini")
    code, _, err = run(capsys, "review", str(diff_file))
    assert code == 1 and "unknown provider 'gemini'" in err


def test_prompt_prints_what_the_model_would_see(capsys: pytest.CaptureFixture[str], diff_file: Path) -> None:
    code, out, _ = run(capsys, "prompt", str(diff_file))
    request = build_prompt(parse_patch(DIFF)).request
    assert code == 0
    assert (
        out
        == f"=== system ===\n{request.system}\n=== user ===\n{request.prompt}=== key {request.key()} ===\n"
    )


def test_summary_file_gets_the_findings_in_full(
    capsys: pytest.CaptureFixture[str], diff_file: Path, recordings: Path, tmp_path: Path
) -> None:
    summary = tmp_path / "step-summary.md"
    summary.write_text("### earlier step\n")
    code, _, _ = run(
        capsys, "review", str(diff_file), "--recordings", str(recordings), "--summary-file", str(summary)
    )
    assert code == 0
    text = summary.read_text()
    assert text.startswith("### earlier step\n### CodeLens review\n")  # appended, not overwritten
    assert "#### `svc/pay.py:3`\n\n**Clamping hides the error**" in text
    assert "GitHub did not accept" not in text
    assert "Dropped 1 finding(s) that failed validation or anchoring (misquoted 1)." in text


@pytest.mark.parametrize(
    "args",
    [
        ("--max-findings", "0"),
        ("--max-findings", "-1"),
        ("--max-prompt-chars", "lots"),
        ("--pr", "0"),
    ],
)
def test_limits_must_be_positive(
    capsys: pytest.CaptureFixture[str], diff_file: Path, args: tuple[str, str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["review", str(diff_file), *args])
    assert exc.value.code == 2
    assert "must be a positive integer" in capsys.readouterr().err


def test_max_findings_caps_the_review(
    capsys: pytest.CaptureFixture[str], diff_file: Path, tmp_path: Path
) -> None:
    recs = tmp_path / "recs"
    two = json.dumps(
        {"findings": [FINDING, {**FINDING, "line": 5, "quote": "return net", "severity": "low"}]}
    )
    write_recording(recs, build_prompt(parse_patch(DIFF)).request, Completion(two, HAND_WRITTEN), "recorded")
    code, out, _ = run(capsys, "review", str(diff_file), "--recordings", str(recs), "--max-findings", "1")
    assert code == 0
    assert out.splitlines()[0].startswith("svc/pay.py:3  high")
    assert "over the cap: 1 lower-ranked findings left out" in out


def test_a_diff_with_nothing_to_review_makes_no_call(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    deletion = tmp_path / "deletion.diff"
    deletion.write_text("--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n")
    # No recordings directory exists: a provider call would fail, so success proves none was made.
    code, out, _ = run(capsys, "review", str(deletion), "--recordings", str(tmp_path / "none"))
    assert code == 0
    assert out.splitlines() == [
        "nothing to review: no file in the diff has added lines (no model call made)",
        "not reviewed: gone.py (deleted file)",
    ]


def test_invalid_diffs_fail_both_commands(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.diff"
    bad.write_text("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a\n")
    for command in ("prompt", "review"):
        code, _, err = run(capsys, command, str(bad))
        assert code == 1 and "invalid diff: line 5: hunk ended early" in err


def test_prompt_reports_skipped_files_on_stderr(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    diff = tmp_path / "lock.diff"
    diff.write_text("--- a/uv.lock\n+++ b/uv.lock\n@@ -0,0 +1 @@\n+x\n")
    code, out, err = run(capsys, "prompt", str(diff))
    assert code == 0 and "(0 files shown)" in out
    assert err == "skipped uv.lock: lock file\n"


def test_post_reports_findings_it_did_not_repeat(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeServer,
    diff_file: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_API_URL", fake_server.url)
    recs = tmp_path / "recs"
    two = json.dumps({"findings": [FINDING, {**FINDING, "line": 5, "quote": "return net", "title": "Other"}]})
    write_recording(recs, build_prompt(parse_patch(DIFF)).request, Completion(two, HAND_WRITTEN), "recorded")
    review_args = ("review", str(diff_file), "--recordings", str(recs))
    _, out, _ = run(capsys, *review_args, "--json")
    earlier = [
        {"body": json.loads(out)["payload"]["comments"][0]["body"]}
    ]  # line 3's comment is already there
    fake_server.reply(Reply(200, earlier), Reply(200, []), Reply(200, {"id": 5, "html_url": "u"}))
    code, _, err = run(capsys, *review_args, "--post", "--repo", "o/r", "--pr", "3")
    assert code == 0
    assert "posted review 5 with 1 findings as line comments: u" in err
    assert "not repeated: 1 findings an earlier review already posted" in err
