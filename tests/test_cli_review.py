import io
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
    assert "posted review 81 with 1 finding as line comments" in err
    seen = fake_server.requests[-1]
    assert seen.path == "/repos/o/r/pulls/3/reviews" and seen.body["commit_id"] == "c0"
    # The same review again, after another push: the finding is already on the PR.
    earlier = [{"user": {"login": "github-actions[bot]"}, **comment} for comment in seen.body["comments"]]
    fake_server.reply(Reply(200, earlier), Reply(200, []))
    code, _, err = run(capsys, *args)
    assert code == 0 and "nothing new to post: an earlier review already posted 1 finding" in err
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
    assert "Dropped 1 finding that failed validation or anchoring (misquoted 1)." in text


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
    request = build_prompt(parse_patch(DIFF), max_findings=1).request  # the prompt asks for the cap too
    write_recording(recs, request, Completion(two, HAND_WRITTEN), "recorded")
    code, out, _ = run(capsys, "review", str(diff_file), "--recordings", str(recs), "--max-findings", "1")
    assert code == 0
    assert out.splitlines()[0].startswith("svc/pay.py:3  high")
    assert "over the cap: 1 lower-ranked finding left out" in out


def test_a_diff_with_nothing_to_review_makes_no_call(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    deletion = tmp_path / "deletion.diff"
    deletion.write_text("--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n")
    # No recordings directory exists: a provider call would fail, so success proves none was made.
    code, out, _ = run(capsys, "review", str(deletion), "--recordings", str(tmp_path / "none"))
    assert code == 0
    assert out.splitlines() == [
        "nothing reviewed: no file in the diff could be shown to the model (no model call made)",
        "not reviewed: gone.py (deleted file)",
    ]


def test_invalid_diffs_fail_both_commands(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.diff"
    bad.write_text("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a\n")
    for command in ("prompt", "review"):
        code, _, err = run(capsys, command, str(bad))
        assert code == 1 and "invalid diff: line 5: hunk ended early" in err


def test_prompt_reports_skipped_files_on_stderr(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    diff = tmp_path / "gone.diff"
    diff.write_text("--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    code, out, err = run(capsys, "prompt", str(diff))
    assert code == 0 and "(0 files shown)" in out
    assert err == "skipped gone.py: deleted file\n"


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
        {"user": {"login": "github-actions[bot]"}, "body": json.loads(out)["payload"]["comments"][0]["body"]}
    ]  # line 3's comment is already there
    fake_server.reply(Reply(200, earlier), Reply(200, []), Reply(200, {"id": 5, "html_url": "u"}))
    code, _, err = run(capsys, *review_args, "--post", "--repo", "o/r", "--pr", "3")
    assert code == 0
    assert "posted review 5 with 1 finding as line comments: u" in err
    assert "not repeated: 1 finding an earlier review already posted" in err


LATIN1 = (
    b"--- a/conf.properties\n+++ b/conf.properties\n@@ -0,0 +1,2 @@\n+title=Caf\xe9\n+ratio = total / count\n"
)


def latin1_recording(tmp_path: Path, quote: str, line: int) -> Path:
    from codelens.diff import decode_diff

    finding = {**FINDING, "path": "conf.properties", "line": line, "quote": quote}
    recs = tmp_path / "latin1-recs"
    request = build_prompt(parse_patch(decode_diff(LATIN1))).request
    write_recording(recs, request, Completion(json.dumps({"findings": [finding]}), HAND_WRITTEN), "recorded")
    return recs


def test_a_diff_that_is_not_utf8_is_reviewed_with_replacement_characters(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    diff = tmp_path / "latin1.diff"
    diff.write_bytes(LATIN1)
    recs = latin1_recording(tmp_path, "title=Caf�", 1)
    code, out, _ = run(capsys, "review", str(diff), "--recordings", str(recs), "--json")
    assert code == 0
    (comment,) = json.loads(out)["payload"]["comments"]  # building it fingerprints the line: no crash
    assert comment["line"] == 1 and "<!-- codelens:" in comment["body"]


def test_a_diff_on_stdin_is_read_as_bytes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stdin:
        buffer = io.BytesIO(LATIN1)

    monkeypatch.setattr("sys.stdin", Stdin())
    recs = latin1_recording(tmp_path, "ratio = total / count", 2)
    code, out, _ = run(capsys, "review", "-", "--recordings", str(recs))
    assert code == 0 and out.startswith("conf.properties:2  high  bug")


def test_record_checks_the_recordings_path_before_the_paid_call(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeServer,
    diff_file: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CODELENS_BASE_URL", fake_server.url)
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    args = ("review", str(diff_file), "--provider", "anthropic", "--record", "--recordings", str(not_a_dir))
    code, _, err = run(capsys, *args)
    assert code == 1 and "cannot use" in err and "for recordings" in err
    assert fake_server.requests == []  # no vendor call was made, so none was paid for


def test_an_unusable_recorded_answer_is_reported(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeServer,
    diff_file: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CODELENS_BASE_URL", fake_server.url)
    fake_server.reply(Reply(200, claude_says("Looks good to me!")))
    recs = tmp_path / "recs"
    code, _, err = run(
        capsys, "review", str(diff_file), "--provider", "anthropic", "--record", "--recordings", str(recs)
    )
    (saved,) = recs.iterdir()
    assert code == 1 and "review failed: response is not valid JSON" in err
    assert f"recorded the unusable answer in {saved}" in err


def test_an_unwritable_summary_file_is_a_warning(
    capsys: pytest.CaptureFixture[str], diff_file: Path, recordings: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-dir" / "summary.md"
    code, out, err = run(
        capsys, "review", str(diff_file), "--recordings", str(recordings), "--summary-file", str(missing)
    )
    assert code == 0 and out.startswith("svc/pay.py:3")
    assert "warning: cannot write the summary" in err


def test_files_skipped_for_the_budget_are_not_called_files_without_added_lines(
    capsys: pytest.CaptureFixture[str], diff_file: Path
) -> None:
    code, out, _ = run(capsys, "review", str(diff_file), "--max-prompt-chars", "10")
    assert code == 0
    assert out.splitlines() == [
        "nothing reviewed: no file in the diff could be shown to the model (no model call made)",
        "not reviewed: svc/pay.py (over the 10-character prompt budget)",
    ]
