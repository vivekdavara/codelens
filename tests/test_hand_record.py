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
