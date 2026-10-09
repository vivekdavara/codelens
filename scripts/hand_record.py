"""Write a hand-written recording: the answer a test wants the recorded provider to give for a diff.

    .venv/bin/python scripts/hand_record.py tests/fixtures/sample_pr.diff \
        tests/fixtures/sample_pr.response.json --out tests/fixtures/recordings

The recording is keyed by the prompt CodeLens builds for the diff today, so run this again after changing the
prompt or the schema (and delete the old file). It is marked "model": "hand-written" so it is never mistaken
for model output; real recordings come from `codelens review --record` with a live provider.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codelens.diff import parse_patch
from codelens.prompts import build_prompt
from codelens.providers import Completion
from codelens.providers.recorded import HAND_WRITTEN, write_recording


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("diff", type=Path)
    parser.add_argument("response", type=Path, help="JSON file holding the answer, as a model would give it")
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/recordings"))
    args = parser.parse_args()
    response = args.response.read_text(encoding="utf-8").strip()
    json.loads(response)  # refuse to record something that isn't JSON
    request = build_prompt(parse_patch(args.diff.read_text(encoding="utf-8"))).request
    print(write_recording(args.out, request, Completion(response, HAND_WRITTEN), "recorded"))


if __name__ == "__main__":
    main()
