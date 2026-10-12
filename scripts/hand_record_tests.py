"""Write a hand-written recording for `codelens testgen`: the answer a sample wants the provider to give.

    .venv/bin/python scripts/hand_record_tests.py evals/testgen/cases/stock/pr.diff \
        evals/testgen/cases/stock/answer.json --source-root evals/testgen/cases/stock/after \
        --out evals/testgen/recordings

The recording is keyed by the prompt `codelens testgen` builds for the diff and checkout today, so run this
again after changing the prompt, the schema or the sample (and delete the old file). It is marked
"model": "hand-written" so it is never mistaken for model output; real recordings come from
`codelens testgen --provider anthropic --record`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codelens.diff import decode_diff, parse_patch
from codelens.providers import Completion
from codelens.providers.recorded import HAND_WRITTEN, write_recording
from codelens.testgen import MAX_TESTS, build_prompt, find_targets


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("diff", type=Path)
    parser.add_argument("response", type=Path, help='JSON file holding the answer: {"tests": [...]}')
    parser.add_argument(
        "--source-root", type=Path, default=Path("."), help="checkout of the diff's new version"
    )
    parser.add_argument("--out", type=Path, default=Path("evals/testgen/recordings"))
    parser.add_argument("--max-tests", type=int, default=MAX_TESTS, help="the cap the run will use")
    args = parser.parse_args()
    response = args.response.read_text(encoding="utf-8").strip()
    json.loads(response)  # refuse to record something that isn't JSON
    # Read exactly as `codelens testgen` does (bytes, no newline translation), or the key would differ.
    patch = parse_patch(decode_diff(args.diff.read_bytes()))
    request = build_prompt(find_targets(patch, args.source_root), max_tests=args.max_tests).request
    print(write_recording(args.out, request, Completion(response, HAND_WRITTEN), "recorded"))


if __name__ == "__main__":
    main()
