#!/usr/bin/env bash
# Re-run every measurement the README reports, one after another so the timings don't overlap.
# About two minutes on an Apple M3; run it from the repository root with the dev virtualenv installed:
#
#     scripts/release_check.sh | tee release-check.txt
#
# It prints; it doesn't compare. Check the output against the README's Results by eye, and say in the
# README what moved.
set -euo pipefail
cd "$(dirname "$0")/.."
py=.venv/bin/python
codelens=.venv/bin/codelens

section() { printf '\n=== %s\n' "$*"; }

echo "commit $(git rev-parse --short HEAD), $(date)"
section "tests and coverage"
.venv/bin/pytest --cov | tail -3
section "eval set, static pre-pass"
$codelens eval --static-only | head -5
section "quote check"
$py scripts/measure_quote_check.py
$py scripts/measure_quote_check.py --offsets=-1,1
section "static rules on the standard library (three runs, for the median time)"
for _ in 1 2 3; do $py scripts/measure_static_noise.py | head -6; done
section "static pre-pass on this repository's history"
$py scripts/measure_static_history.py | tail -8
section "every commit of this repository parses"
$py scripts/check_history.py
section "test generation sample (three runs)"
for _ in 1 2 3; do
  start=$($py -c 'import time; print(time.monotonic())')
  $codelens testgen evals/testgen/cases/stock/pr.diff --source-root evals/testgen/cases/stock/after \
    --tests tests --recordings evals/testgen/recordings --timeout 5 | tail -6
  $py -c "import time; print(f'wall time {time.monotonic() - $start:.2f} s')"
done
section "a test that leaves a 30 s background process"
$py scripts/measure_runner_stall.py
