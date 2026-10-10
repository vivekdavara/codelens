# CodeLens walkthrough

Questions an interviewer is likely to ask about CodeLens, with short answers grounded in the code. Covers days
1 to 3 (the diff parser, the review engine, the static pre-pass and the eval set); later days add their own
sections.

## The one-minute version

**What is it?** A GitHub Action and CLI that reviews a pull request with an LLM and leaves a few comments on
the changed lines. The hard part isn't calling a model; it's making the output trustworthy enough to leave
on: every comment must land on the right line, the run must be reproducible without an API key, and a
hostile diff must not be able to steer the bot. Most of the code is about those three things.

**Walk me through one review.** `codelens review pr.diff` (`cli.py`, `run_review`):

1. `diff.parse_patch` turns the diff into files, hunks and lines, each with old and new line numbers.
2. `static.analyse` runs ruff and CodeLens's rules on the changed Python files, read from the checkout, and
   keeps what the PR introduced.
3. `prompts.build_prompt` renders the reviewable files with new-file numbers in the margin, under a fixed
   system prompt, within a 200,000-character budget, then lists the static findings.
4. `provider.complete(request)` makes one call (recorded by default; Anthropic or OpenAI when opted in) and
   asks for structured JSON matching `findings.FINDINGS_SCHEMA`.
5. `findings.check_response` validates each finding and anchors it: the line must be in the diff and must say
   what the finding's `quote` says. Failures are dropped with a reason, never moved.
6. `review.review` merges them with the static findings (`dedupe`), ranks worst first and caps at 10;
   `github.review_payload` builds one `COMMENT` review; `github.post_review` posts it (or the CLI prints it,
   the default).

## The diff parser (day 1)

**Why is the parser the foundation?** GitHub only accepts a review comment on a line that appears in the PR's
diff, addressed by file line number and side. If the parser is off by one, every comment lands on the wrong
line, and a confident comment on the wrong code is worse than none. So `diff.py` is strict: a hunk whose body
disagrees with its header raises `DiffParseError` with the input line number instead of guessing.

**How do you know it's right?** `tests/test_diff_against_git.py` makes 150 seeded random edits in a temporary
repository, diffs them with real git, and parses the output. With full context the hunks must rebuild both
files byte for byte; with default context every line must match its file at the number the parser gave it.
Any off-by-one in counting would break the rebuild.

**What's tricky about paths?** `diff --git a/x b/y` is ambiguous when paths contain spaces, and pure renames
and binary files have no `---`/`+++` lines. The parser trusts `rename from/to` first, then `---`/`+++`, then
the `diff --git` line (split at the midpoint only when both halves agree). It also decodes git's C-quoted
paths (`"caf\303\251.txt"`).

## Findings and the quote check (day 2)

**Why does every finding quote its line?** Because models often cite a line one or two away from the one they
mean, and that line usually still sits inside the hunk, so line-number anchoring alone would accept it.
`anchor_finding` also requires the quote to appear in the cited line. `scripts/measure_quote_check.py`
measures it on real code (seeded edits to 300 Python stdlib files, diffed by git): of 17,366 off-by-one/two
citations that land inside a hunk, the quote check rejects 96.4%. What gets through lands on a line with the
same text (mostly blank lines), and a correct citation is never rejected.

**Why drop instead of moving the comment to the nearest valid line?** Moving it is a guess presented as a
claim. Every drop is recorded as a `Rejection` (`invalid`, `unanchored` or `misquoted`) and shown in the
review summary, so loss is visible rather than silent.

**Why is the schema an object with a `findings` array?** Structured-output modes want an object at the root
(OpenAI's strict mode requires one). The schema also stays inside the subset both vendors' strict modes
accept: every object closed, every field required, no numeric or length constraints. Those limits
(`0 <= confidence <= 1`, `line >= 1`, title and body lengths) are checked in `parse_findings`, and
`test_schema_stays_inside_the_structured_output_subset` keeps the schema honest.

**Any validation details you're proud of?** `type(line) is int`, because `True` is an `int` in Python;
`parse_constant` refuses `NaN` and `Infinity`, which Python's `json` would otherwise accept; one bad item
rejects only itself.

## Providers and determinism (day 2)

**How do tests run without an API key?** The default `RecordedProvider` replays answers stored as
`<sha256 of the request>.json`. The key covers the system prompt, the prompt and the schema, and its encoding
is pinned by a test. A miss raises `RecordingMissing` rather than calling a model or skipping the review, so a
prompt change can't silently replay a stale answer, and a recording whose stored request no longer hashes to
its name is refused. The only recording in the repo is hand-written and labelled `"model": "hand-written"`;
real ones come from `codelens review --provider anthropic --record`.

**What does the Anthropic request look like?** `providers/anthropic.py`, `AnthropicProvider.body`:
`claude-opus-5-5`, `output_config` with effort `high` and `format` (the JSON schema), `max_tokens` 16,000, and
`"fallbacks": "default"` behind the `server-side-fallback-2026-07-01` beta header so a safety-classifier
decline is retried server-side on another model. `parse_message` checks `stop_reason` before reading content:
`refusal` raises `ProviderRefused`, `max_tokens` raises `ProviderTruncated`, because half a JSON object is not
a review.

**Why raw HTTP instead of the SDKs?** Zero runtime dependencies keeps the action's install to seconds and the
attack surface small, and every header and retry rule is visible and tested against a local fake server. The
cost is keeping two small request builders current as the APIs change.

**How do retries work?** `providers/http.py`: retry 408, 409, 429, 5xx (including Anthropic's 529) and
connection errors, three attempts; use the server's `retry-after-ms`/`retry-after` when it asks for at most
60 s, otherwise back off exponentially with up to 25% jitter; `x-should-retry` overrides. Redirects are
refused because urllib would forward the API key to the new host. Bodies are capped at 10 MB.

**Why `CODELENS_BASE_URL` rather than `ANTHROPIC_BASE_URL`?** Other tools set `ANTHROPIC_BASE_URL` for their
own use (it is set inside Claude Code sessions), and an API key should only go where CodeLens was explicitly
pointed.

## Prompt injection (day 2)

**The diff is written by whoever opened the PR. How do you stop it steering the model?** In layers:

- The system prompt says the diff is untrusted input to review, not instructions.
- Structure backs that up: every diff-derived line sits behind a line-number margin, so diff text can't start
  a line with `</diff>` or `File:`. That holds under Unicode's definition of a line too: `\r`, `U+2028` and
  the other characters `str.splitlines` breaks on are shown as spaces (a bug found and fixed on day 2,
  `test_unicode_line_breaks_in_content_cannot_start_a_line`), and paths with control characters are skipped.
- Output is data: schema-validated, anchored, never executed; `@mentions` in model text are broken
  (`github.defang`) so a steered model can't ping people.
- The action runs on `pull_request`, never `pull_request_target`, so fork code never runs with secrets.

**What can an attacker still do?** Make the model say wrong or unhelpful things about their own PR on lines
their PR changed. They can't make it comment elsewhere, post more than 10 comments a run, mention people, or
execute anything.

## Posting to GitHub (day 2)

**Why one review instead of one comment per finding?** One notification for the author, and the summary can
list everything that was dropped, capped or skipped.

**Why is posting never retried?** The Reviews API has no idempotency key: if a POST times out after GitHub
created the review, a retry would post it twice. Reads (GETs) do retry.

**What happens when the author pushes again?** The action runs again and would repeat every finding. Each
posted comment ends with an invisible `<!-- codelens:<fingerprint> -->`, a hash of the file path, the
line's full text and its `occurrence` (how many identical lines sit above it in the diff, so two
`return None` lines don't collide). Before posting, `posted_fingerprints` reads the PR's comments and review
bodies (paginated, following `rel="next"` only on the same host because the token goes with it), drops the
findings already there, and only then applies the cap. Line numbers aren't in the fingerprint because they
move; titles aren't because the model rewords them. Trade-off: one comment per line of code per PR.

**Couldn't the PR author fake those markers?** They could compute any fingerprint from the public diff, and
an early version trusted markers in anyone's comment: a review agent found that one comment from the author
could silence CodeLens on every line they chose. Now only markers in comments by CodeLens's own login
(`github-actions[bot]`) count, and `<!--` in model text is escaped so a steered model can't plant one.

**And if GitHub rejects the line comments?** A 422 usually means the PR moved on after the diff was fetched.
The review is posted once more with the findings written into its body.

## The static pre-pass (day 3)

**Why run static analysis if you have an LLM?** Because some bugs are facts, not opinions: an undefined
name, an `except A or B`, a coroutine called without `await`. A rule finds those every time for free, and a
model finds them sometimes. The pre-pass posts them as they are and lists them in the prompt
(`prompts.render_static`), so the model starts from checked facts and spends its attention on what rules
can't see.

**Which rules, and why not all of ruff?** 56 ruff rules picked one by one for bugs (`static.RULES`), each
with a severity, a category, a confidence prior and a sentence on the consequence, which becomes the comment
body. Whole families would be noise: B008 flags every FastAPI `Depends()`, S101 every `assert` in tests. A
test asks ruff for its own rule list and fails if a selected code is missing or preview-only.

**Why `--isolated`?** The PR's `pyproject.toml` is written by the PR's author. Without `--isolated`, a PR could
add `ignore = ["S608"]` next to its SQL injection. A test commits exactly that kind of config and checks the
finding still appears.

**Why your own rules?** For bugs ruff 0.16 doesn't report, or reports only in preview (whose behaviour can
change between releases): CL001 a coroutine called without `await`, CL002 code after `return`/`raise`/
`break`/`continue`, CL003 changing the collection a loop iterates, CL004 a missing `f` prefix. Each is a
small AST pass in `rules.py`, and each is narrow on purpose: CL003 stays quiet on a loop over a copy or a
change followed by `break`; CL004 only fires when every placeholder names a local, and skips `.format`
receivers, loguru-style keyword templates and search tokens.

**How does it read the code, and what if the checkout is wrong?** From a checkout of the PR's new version
(`--source-root`, the workspace in the action). A file is used only if every line the diff shows matches the
file on disk (`static._matches`); otherwise it is skipped with a reason. That matters because
`actions/checkout` checks out the PR's merge commit by default, where a file the base branch also changed has
different line numbers. Symlinks and paths that escape the root are skipped too, since the PR controls them.

**Why not just report hits on added lines?** That was the first version, and the eval's `async-jobs` case
broke it: the PR changes `def run` to `async def run`, which makes its unchanged `time.sleep()` block the
event loop and its unchanged `self.flush()` a coroutine that never runs. Both bugs are on context lines. Now
the pre-pass also reports a hit on a shown unchanged line if the old version of the file didn't have it.

**Where does the old version come from?** It's rebuilt from the new file and the diff (`static.old_version`):
for each hunk, swap its new side (context and added lines) for its old side (context and removed lines). The
new file was already verified against the diff, so this is exact, and no git history or base checkout is
needed. The old version goes through the same checks in a temporary directory, and a hit counts as old when
the old version has the same rule on a line with the same text, counted per occurrence.

**How are static and model findings merged?** `review.dedupe`: one finding per (path, line, side,
category), keeping the one the ranking puts first (severity, then confidence). The plan said "keep the more
confident one", and the existing cap test caught why that's wrong: a confident `low` would replace a less
confident `critical` on the same line. Different categories on one line are different problems and both
stay.

**Why is ruff pinned?** The static block is part of the prompt, and recordings are keyed by a hash of the
prompt. A different ruff could word a message differently and orphan every recording. CI's `action-static`
job replays, on a Linux runner, a recording made on a Mac, which only works if the pinned ruff says exactly
the same thing on both.

## Evals (day 3)

**What's in the eval set?** 14 small PRs in `evals/cases/` (12 with 23 seeded bugs, 2 clean), each a
`before/` and `after/` tree, the real `git diff -M` of the two, and `labels.json`. A label names its bug's line
by quoting it (`evals._resolve`), so labels can't drift when a case is edited, and `also` lists other lines
a reviewer could fairly cite. A test checks that every committed diff still turns `before/` into `after/`.

**How is a finding matched to a bug?** Same path, a line among the label's lines, and the same category;
one to one, so a second comment on a found bug counts as a false positive (`evals.score`). Each source is
scored on what it would post: deduped, ranked and capped (`evals.posted`).

**What are the numbers, and are they honest?** The static pre-pass posts 14 findings on the 14 cases, all
correct, and finds 14 of the 23 bugs (`codelens eval`). The precision on this set is real, but the recall is
an upper bound: I wrote the rules and the cases on the same day, and about half the bugs are of a kind a
rule targets. The 9 misses are the semantic ones, such as off-by-one pagination, path traversal and a timing-unsafe
token compare: exactly the model's job.

**Why are there no model numbers?** They need real model answers, and there was no API key where this was
built. Hand-written answers would only measure my own guesses about a model, so the model rows say "not
recorded", and the harness never scores a missing recording as zero. One command records the 14 answers
(`codelens eval --provider anthropic --record`), and after that CI replays them.

**How do you know the rules aren't noisy on real code?** `scripts/measure_static_noise.py` reviews 296
files of the Python 3.11.12 standard library as if a PR had added each one whole: 3.14 hits per 1,000 lines
outside tests, 14.69 in tests, and most of those are deliberate (the standard library calls `eval` on
purpose). It also found two real bugs in the standard library: two error messages in
`test/support/__init__.py` missing their `f` prefix.

**Did the measurements find bugs in your own code?** Yes, three. The eval found the added-lines-only gap
(above). The stdlib run found two false-positive classes: CL003 fired on CookieJar's
`for cookie in self: self.clear(domain, path, name)`, which is CookieJar's own `clear` over a snapshot, so
CL003 now checks the call's arity against the built-in methods (`list.clear` takes no arguments); and CL004
fired on CodeLens's own `prompts.py`, on the `"{max_findings}"` token passed to `.replace`, so search tokens
are skipped. A test now requires 0 hits on CodeLens's own sources.

## Large PRs and limits

**What if the PR is huge?** The diff shown to the model is capped at 200,000 characters (roughly 50K tokens;
`max-prompt-chars` changes it). If the diff doesn't fit, `prompts.budget_rank` gives the budget to code and
config first, then tests, then docs, then lock and generated files (`package-lock.json`, `*.min.js`,
protobuf output, snapshots), whose diffs are often the largest and rarely written by hand. They are ranked
last, not skipped, because the PR author chooses file names: hand-written code in `x_pb2.py` must still be
read when there is room. A file that doesn't fit is skipped whole, never cut mid-hunk, and reported. That ranking came
from running CodeLens on its own day-2 diff (35 files): in plain diff order it cut 7 test files while the
Markdown docs took about 40,000 characters; ranked, it cuts the 3 docs and 1 test file. Findings on files
the model wasn't shown are rejected.

## Testing

**How is it tested?** 492 tests, 99% line and branch coverage (`.venv/bin/pytest --cov`). Besides unit tests:
the git differential test (day 1); a scripted local HTTP server standing in for the vendors and GitHub
(`tests/conftest.py`), so retries, timeouts and refused redirects go over real sockets; seeded fuzz tests
(`tests/test_fuzz.py`) that feed random and mutated JSON to every parser of untrusted input (they found
negative token counts passing through); `scripts/check_history.py`, which parses every commit of a real
repository (0 failures on this one's 48 commits at `8646d07` and on another clone's 50 Java/SQL/YAML commits); two CI
jobs that run full reviews through `action.yml` (the sample PR, and an eval case with the static pre-pass);
and the eval set, whose static numbers are pinned by a test.

**Did anything find bugs you had missed?** A review of the whole day-2 diff by nine independent agents (line
by line, removed behaviour, call sites, Python pitfalls, wrappers, reuse, simplification, efficiency,
altitude), each candidate then re-run by a verifier against the pre-fix commit. Every fix landed with a test
that reproduces the bug. The worst were: the CI job would have posted the sample fixture's findings onto
real PRs; the finding cap was applied before the already-posted filter, so new findings could be starved
forever; anyone could forge the hidden markers; and a non-UTF-8 byte in a diff crashed fingerprinting and
recording (after the paid model call).

## What's not done yet

- **The model's review quality is unmeasured.** The eval set and harness exist; the 14 real answers need an
  API key (`codelens eval --provider anthropic --record`). The hand-written recordings prove the pipeline,
  not the model.
- Test generation (day 4).
- The static pre-pass is Python only, and its eval recall is an upper bound (same author as the rules).
- Comments on removed lines (LEFT side): the prompt numbers only new-file lines, so a removal is flagged on
  the nearest line still in the file.
- Reviewing only what changed since the last review; today every run reviews the whole PR diff and relies on
  fingerprints to avoid repeats.
