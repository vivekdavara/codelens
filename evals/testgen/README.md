# Test-generation samples

Pull requests for measuring `codelens testgen`: what it keeps, and how much coverage of the changed functions
that adds. `tests/test_testgen_sample.py` pins the numbers below.

## What a sample is

```text
cases/<name>/before/...    the project before the PR
cases/<name>/after/...     the project after the PR, with its existing tests (the checkout testgen reads)
cases/<name>/pr.diff       `git diff -M` of the two, made with real git by scripts/build_evals.py
cases/<name>/answer.json   the answer the recording gives: {"tests": [{"function", "code"}]}
recordings/<key>.json      that answer, keyed by the prompt testgen builds for the sample
```

## `stock`

A small warehouse module. The PR adds a safety buffer and input checks to `reorder_point`, makes
`parse_sku` tolerant of case and spaces and strict about the format, and adds `units_to_order`. The existing
suite has one happy-path test each for `reorder_point` and `parse_sku`.

**The answer is hand-written, not model output.** There was no API key on the machine that built it
(2026-10-12). The answer's 12 candidates were chosen to exercise every decision the filter makes, so the
numbers measure CodeLens's filtering and accounting, not a model's test writing:

| # | Candidate | Why it's there | Verdict |
|---|---|---|---|
| 0 | `reorder_point(5, 3, safety_days=2) == 25` | tests the new parameter, but only on lines the suite already runs | dropped: no new line |
| 1 | negative demand raises `ValueError` | the new check | kept |
| 2 | zero demand gives 0 | the new early return | kept |
| 3 | `parse_sku("  ab-12 ")` | new behaviour, old lines again | dropped: no new line |
| 4 | `"AB1234"` raises | the one-dash check | kept |
| 5 | `"12-AB"` raises | the letters-then-digits check | kept |
| 6 | rounds up to whole packs | the new function | kept |
| 7 | at the reorder point, expects one lead time of demand | a wrong expectation (the code orders 0) | dropped: fails |
| 8 | sleeps for 120 s | disobeys the prompt | dropped: timed out |
| 9 | calls `os._exit(0)` | a test that would fake a pass | dropped: no result reported |
| 10 | names `stock.levels.reorder` | not a changed function | rejected before running |
| 11 | doesn't parse | not Python | rejected before running |

Measured (`.venv/bin/codelens testgen evals/testgen/cases/stock/pr.diff --source-root
evals/testgen/cases/stock/after --tests tests --recordings evals/testgen/recordings --timeout 5`, Apple M3,
7.4 s wall time, median of 3 runs, 5 s of it the timed-out candidate):

- 5 of the 10 candidates that ran were kept.
- Statement coverage of the three changed functions' bodies went from 8 of 18 (the existing suite) to 17 of
  18, +50.0 points.
- Counting only statements on lines the diff adds: 7 of 17 before, 16 of 17 after.
- The one statement still not run is `units_to_order`'s `return 0` (stock above the reorder point). No
  candidate tests that case.

Candidates 0 and 3 show what line coverage misses: both check behaviour the PR added, and both are dropped,
because the lines they run were already run. A branch-coverage or mutation-based criterion would keep them.

## Recording a real answer

With a key, a model's answer replaces the hand-written one:

```bash
ANTHROPIC_API_KEY=... .venv/bin/codelens testgen evals/testgen/cases/stock/pr.diff \
    --source-root evals/testgen/cases/stock/after --tests tests \
    --provider anthropic --record --recordings evals/testgen/recordings-live
```

Keep it in a separate directory: the hand-written recording has the same key, because the prompt is the same.

## Changing a sample

Edit `before/` or `after/`, run `.venv/bin/python scripts/build_evals.py evals/testgen/cases`, then
`.venv/bin/python scripts/hand_record_tests.py` (its docstring has the command) to re-key the recording, and
delete the old one.
