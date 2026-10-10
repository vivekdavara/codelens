"""scripts/hand_record.py must key a recording exactly as `codelens review` will look it up."""

import subprocess
import sys
from pathlib import Path

from codelens.diff import decode_diff, parse_patch
from codelens.prompts import build_prompt

ROOT = Path(__file__).parent.parent


def test_the_recording_key_matches_the_cli_for_awkward_bytes(tmp_path: Path) -> None:
    # A lone CR inside a content line, a CRLF line and a Latin-1 byte: any newline translation or a
    # different decoding would change the prompt, and with it the key.
    diff = tmp_path / "pr.diff"
    diff.write_bytes(b"--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1,3 @@\n+one\rtwo\n+three\r\n+caf\xe9\n")
    answer = tmp_path / "answer.json"
    answer.write_text('{"findings": []}')
    out = tmp_path / "recs"
    subprocess.run(
        [sys.executable, "scripts/hand_record.py", str(diff), str(answer), "--out", str(out)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    expected = build_prompt(parse_patch(decode_diff(diff.read_bytes()))).request.key()
    assert [p.name for p in out.iterdir()] == [f"{expected}.json"]


def test_by_default_it_keys_the_prompt_review_builds_in_the_same_directory(tmp_path: Path) -> None:
    # Like `codelens review`, it runs the static pre-pass on the current directory unless told not to.
    from codelens.static import analyse, find_ruff

    case = ROOT / "evals" / "cases" / "discount-codes"
    answer = tmp_path / "answer.json"
    answer.write_text('{"findings": []}')
    for flags, static in (([], True), (["--no-static"], False)):
        out = tmp_path / f"recs-{static}"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "hand_record.py"),
                "../pr.diff",
                str(answer),
                "--out",
                str(out),
                *flags,
            ],
            cwd=case / "after",
            check=True,
            capture_output=True,
        )
        patch = parse_patch(decode_diff((case / "pr.diff").read_bytes()))
        findings = analyse(patch, case / "after", ruff=find_ruff()).findings if static else []
        assert findings or not static
        expected = build_prompt(patch, static=findings).request.key()
        assert [p.name for p in out.iterdir()] == [f"{expected}.json"]
