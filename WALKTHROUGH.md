# CodeLens walkthrough

Questions an interviewer is likely to ask about CodeLens, with short answers grounded in the code. Covers days
1 and 2 (the diff parser and the review engine); later days add their own sections.

## The one-minute version

**What is it?** A GitHub Action and CLI that reviews a pull request with an LLM and leaves a few comments on
the changed lines. The hard part isn't calling a model; it's making the output trustworthy enough to leave
on: every comment must land on the right line, the run must be reproducible without an API key, and a
hostile diff must not be able to steer the bot. Most of the code is about those three things.

**Walk me through one review.** `codelens review pr.diff` (`cli.py`, `run_review`):

1. `diff.parse_patch` turns the diff into files, hunks and lines, each with old and new line numbers.
2. `prompts.build_prompt` renders the reviewable files with new-file numbers in the margin, under a fixed
   system prompt, within a 200,000-character budget.
3. `provider.complete(request)` makes one call (recorded by default; Anthropic or OpenAI when opted in) and
   asks for structured JSON matching `findings.FINDINGS_SCHEMA`.
4. `findings.check_response` validates each finding and anchors it: the line must be in the diff and must say
   what the finding's `quote` says. Failures are dropped with a reason, never moved.
5. `review.review` ranks what's left worst first and caps it at 10; `github.review_payload` builds one
   `COMMENT` review; `github.post_review` posts it (or the CLI prints it, the default).

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
posted comment ends with an invisible `<!-- codelens:<fingerprint> -->`, a hash of the file path and the
line's full text. Before posting, `posted_fingerprints` reads the PR's comments and review bodies (paginated,
following `rel="next"` only on the same host because the token goes with it) and drops findings already
there. Line numbers aren't in the fingerprint because they move; titles aren't because the model rewords
them. Trade-off: one comment per line of code per PR.

**And if GitHub rejects the line comments?** A 422 usually means the PR moved on after the diff was fetched.
The review is posted once more with the findings written into its body.

## Large PRs and limits

**What if the PR is huge?** Files are rendered in diff order until a 200,000-character budget (roughly 50K
tokens) is used; a file that doesn't fit is skipped whole, never cut mid-hunk, and reported. Lock files and
generated files (`package-lock.json`, `*.min.js`, protobuf output, snapshots) are skipped first, since their
diffs are often the largest and nobody reviews them. Findings on files the model wasn't shown are rejected.

## Testing

**How is it tested?** 272 tests, 99% line and branch coverage (`.venv/bin/pytest --cov`). Besides unit tests:
the git differential test (day 1); a scripted local HTTP server standing in for the vendors and GitHub
(`tests/conftest.py`), so retries, timeouts and refused redirects go over real sockets; seeded fuzz tests
(`tests/test_fuzz.py`) that feed random and mutated JSON to every parser of untrusted input (they found
negative token counts passing through); and a CI job that runs a full review through `action.yml` on the
sample PR.

## What's not done yet

- **Review quality is unmeasured.** Precision and recall need real model answers on PRs with seeded bugs
  (day 3); the hand-written recording proves the pipeline, not the model.
- Static pre-pass and dedupe within a review (day 3), test generation (day 4).
- Comments on removed lines (LEFT side): the prompt numbers only new-file lines, so a removal is flagged on
  the nearest line still in the file.
- Reviewing only what changed since the last review; today every run reviews the whole PR diff and relies on
  fingerprints to avoid repeats.
