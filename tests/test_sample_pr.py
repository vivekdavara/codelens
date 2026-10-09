"""The sample PR and its hand-written recording, which the action job in CI replays.

If a prompt or schema change breaks these tests, regenerate the recording (and delete the old file):
    .venv/bin/python scripts/hand_record.py tests/fixtures/sample_pr.diff \
        tests/fixtures/sample_pr.response.json --out tests/fixtures/recordings
"""

from pathlib import Path

from codelens.diff import parse_patch
from codelens.github import review_payload
from codelens.prompts import build_prompt
from codelens.providers import RecordedProvider
from codelens.providers.recorded import HAND_WRITTEN, load_recording
from codelens.review import review

FIXTURES = Path(__file__).parent / "fixtures"
RECORDINGS = FIXTURES / "recordings"
PATCH = parse_patch((FIXTURES / "sample_pr.diff").read_text())


def test_the_recording_matches_todays_prompt_and_nothing_is_stale() -> None:
    key = build_prompt(PATCH).request.key()
    assert sorted(p.name for p in RECORDINGS.iterdir()) == [f"{key}.json"]
    _, completion = load_recording(RECORDINGS / f"{key}.json")
    assert completion.model == HAND_WRITTEN
    assert completion.text == (FIXTURES / "sample_pr.response.json").read_text().strip()


def test_reviewing_the_sample_pr() -> None:
    result = review(PATCH, RecordedProvider(RECORDINGS))
    assert [(f.path, f.line, f.severity.value, f.category.value) for f in result.findings] == [
        ("shop/orders.py", 15, "high", "bug"),
        ("shop/orders.py", 22, "medium", "bug"),
        ("tests/test_orders.py", 7, "low", "test"),
    ]
    # The third answer cites line 16 with line 15's text, the off-by-one the quote check exists for.
    assert [(r.index, r.kind) for r in result.rejections] == [(2, "misquoted")]
    assert len(review_payload(result)["comments"]) == 3
