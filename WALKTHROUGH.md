# CodeLens walkthrough

Questions an interviewer is likely to ask about CodeLens v1.0.0, with short answers grounded in the code:
the diff parser, the review engine, the static pre-pass and the eval set, test generation, and CodeLens
reviewing its own pull requests.

**Reading order**, for someone with an hour: this file's first section; `review.py` (all of it, it's short);
`findings.py` (`check_response`, `_locate`); `diff.py` (`FileDiff.anchor`); `github.py` (`fingerprint`,
`post_review`); `action.yml` (the Review step); then the section of this file for whatever you'll be asked
about. The stories worth telling are under "Tell me about..." questions: the `ruff.py` that ran in the
action (day 3), exit status 0 that wasn't a pass (day 4), the pipe that wasn't the problem (day 4), and the
recording that could never replay (day 5).

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
needed. The old version goes through the same checks in a temporary directory. Each context line in the
diff knows its old line number, so a hit counts as old only if the old version had the same rule and
message on that very line. The first version matched by line text anywhere in the file, and a review agent
built three cases where that blamed the wrong line or missed a new problem.

**How are static and model findings merged?** `review.dedupe`: one finding per line, keeping the one the
ranking puts first (severity, then confidence). The plan said "one per (path, line, category), the more
confident one", and both halves were wrong. The existing cap test showed "more confident" lets a confident
`low` replace a less confident `critical`. And a review agent showed the per-category part disagreed with
posting: fingerprints identify a line, so two findings on one line shared a marker, and the one the cap held
back counted as already posted on the next push.

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

## Test generation (day 4)

**What does `codelens testgen` do?** `cli.run_testgen` → `testgen.generate`:

1. `find_targets` lists the top-level functions the diff adds lines to, in Python files that match the
   checkout (the pre-pass's `static.checked_lines`), each with its import name (`src/` layout aware).
2. `build_prompt` shows each module whole behind a numbered margin, naming its changed functions; one call
   per PR asks for up to 10 standalone pytest files as `{"tests": [{"function", "code"}]}`.
3. `parse_candidates` rejects bad items one by one (unknown function, not Python, no test in it) before any
   costs a run.
4. The existing suite runs once (`--tests`) for a baseline; then each candidate runs alone through
   `sandbox.run_pytest`, and is kept if it passes and runs a changed function's statement nothing ran before.
5. The report gives each verdict and the coverage of the changed functions before and after; `--out`
   writes the kept files.

**Why only top-level functions?** A test of a method first has to build the object, often with
collaborators, and a model guessing a constructor from one module's source mostly writes tests that fail at
setup. Top-level functions are where generated tests pass often enough to be worth the runs. Methods are the
obvious next step (DESIGN.md, "Targets").

**How do you decide a test is worth keeping?** It must pass cleanly (at least one test passed, and none
failed, skipped, xfailed or xpassed: exit status 0 alone also means "expected failure"), and it must run at
least one statement of a changed function's body that neither the existing suite nor an earlier kept test
ran (`generate`, the `covered` set). Statements are coverage.py's own (`Coverage.analysis2`), the `def` line is excluded because
it runs on import, and candidates are judged in the model's order, so of two tests of the same branch the
first is kept. A failing test is never kept, whatever it covered.

**Doesn't "keep only passing tests" just pin the current behaviour, bugs included?** Yes, and the README
says so. A test of what buggy code *should* do fails and is dropped, so kept tests are regression tests,
not bug finders; failing candidates are listed with their output because one may be pointing at a bug. The
sample shows the other limit too: candidates 0 and 3 test behaviour the PR added, on lines the suite already
ran, so line coverage can't tell them from repeats. Branch coverage, or mutation testing (keep a test that
kills a mutant of a changed line), would be the stronger criterion.

**You run code a model wrote, steered by a PR. How is that safe?** It isn't a sandbox, and the design says
so; it's defence in depth (`sandbox.py`). The child's environment comes from an allow-list (no API key, no
`GITHUB_TOKEN`); it runs in a scratch directory with an empty pytest config; the whole process group is
killed at the timeout and after every run; coverage and pytest are imported before the project's
directories go on `sys.path`. What it can't stop: on Linux a process can read the environment of every
other process of the same user (`/proc/<pid>/environ`), and it can open sockets and write anywhere the
user can. So the right deployment splits the work: the model call needs the key and runs no PR code, the
test runs need no key and run PR code, and they belong in separate steps or jobs. That's why the action
doesn't run test generation yet.

**Why does the child write a JSON report instead of using its exit status?** A test can call `os._exit(0)`:
the process exits 0 with no tests reported. The verdict comes from the report the bootstrap writes after
`pytest.main` returns, so no report means "error", never "pass" (`test_an_early_exit_is_an_error_not_a_pass`,
and candidate 9 in the sample).

**Why give a candidate its own empty `pytest.ini`?** pytest finds its config from the tests' common
ancestor, whatever `--rootdir` says, so a `pytest.ini` above the scratch file would apply its `addopts`. `-c`
with an empty file of CodeLens's own rules it out (`test_the_project_pytest_config_does_not_apply...`).

**What did you measure?** On the `stock` sample: 5 of the 10 candidates that ran were kept, and coverage
of the changed functions' bodies went from 8 of 18 statements to 17 of 18. The answer is hand-written
(no API key here), chosen to exercise each decision, so that measures the filter, not a model; one
recorded call would measure a model (`evals/testgen/README.md`).

**Tell me about something you got wrong.** The runner's first version read the child's output through a pipe.
Overnight a test run took over two minutes, and I blamed the pipe: a test leaves a 60 s sleeper running,
the sleeper inherits the pipe, and reading it to the end waits for the sleeper. I switched to a file and
a wait, and the commit message tells that story. When I went to put the number in the README, I rebuilt
the old version (`scripts/measure_runner_stall.py`) and it returned in 0.2 s. pytest's output capture gives the
sleeper a capture file, not the pipe. The two minutes were the laptop going to sleep mid-run. The stall
is real only when the test turns capture off (`capsys.disabled()`): 30.2 s against 0.29 s. So the change
stayed, the regression test now uses that case (before, it passed on both versions), and the docs carry
the measured numbers and the correction. Lesson: measure the mechanism before writing down the cause.

## Self-review and the release (day 5)

**How does CodeLens review its own pull requests without a key?** The `self-review` job in `ci.yml` runs
the PR's own version of the action (`uses: ./`, from the merge commit) on every pull request, with the PR's
head checked out in `.pr-head` as the source root, `provider: recorded`, recordings from the head's
`.codelens/recordings`, and `missing-recording: static`. A new PR has
no recording, because a recording answers one exact prompt, so `review.review` catches that
`RecordingMissing` and the review is the static findings alone, with `Review.unrecorded` naming the
recording that would have answered. The findings are posted like any review. With an `ANTHROPIC_API_KEY`
repository secret, the job's `provider` expression picks `anthropic` instead.

**Isn't falling back the opposite of "loud on a miss"?** It's opt-in, and narrow. The default is still
`fail`, so tests, evals and CI jobs that expect a recording still fail when the prompt changes
(`action-static` checks exactly that). The fallback covers one error, a missing recording; an edited
recording or a live provider's failure still raises (`test_only_a_missing_recording_falls_back`), because
there the review was asked for a model's answer and posting less would hide a broken setup. And the
summary never pretends: its headline says "from static analysis only ... so no model reviewed it".

**Tell me about the recording that could never replay.** To get a model's review replayed on a PR, you record
it locally and commit the recording to the PR. But committing it adds the recording to the PR's diff, the
diff is the prompt, and the prompt's hash is the recording's name, so the recording no longer answers the
new prompt. It could never replay. The fix: `review` never reviews the recordings in the diff (they're
model output, not code); it lists them as skipped, and the CLI works the directory out as a path in the diff
from `--recordings` and `--source-root`. `test_a_recording_committed_to_the_pr_still_replays` commits a
recording into a diff and replays it; it fails without the exclusion. I found this by walking through the
flow before writing it down, not in production, which is the cheap time to find it.

**And the fix had its own bug.** My first version left out every file under the directory, and worked the
directory out with `Path.resolve()`. A review agent showed two ways that went wrong, both from the PR's own
checkout, which the PR controls: a symlink loop at `.codelens/recordings` crashed the review with a
`RuntimeError`, and a symlink from `.codelens/recordings` to `../svc` made `svc/pay.py` "a CodeLens
recording", so its finding was silently dropped. A PR could hide code from its reviewer. Now the path comes
from names alone (`os.path.abspath`, `relpath`; no symlink is followed), and only files shaped like
recordings are left out: `<64 hex>.json` directly in the directory, so a README or code put there is reviewed
like anything else. The same review found that `codelens prompt` and `hand_record.py` didn't apply the
exclusion, so they showed a key the review never looks up. The lesson: an exclusion is a hole in what you
check, so make it as narrow as its purpose.

**What happened the first time it posted to the real GitHub API?** Until day 5 every posting test ran
against a local fake that matched the documented API. On demo PR #1 (seeded bugs, never merged), the first
real run posted one review with three line comments, pinned to the head commit. The second push added a bug
*above* the others, moving them down nine lines: GitHub moved the old comments to their new lines,
CodeLens's fingerprints (file, line text, position among identical lines, not the line number) recognised
all three, and it posted only the new finding with "Not repeated: 3 findings". The seeded off-by-one in
`batches` was missed both times, which is what a review without a model should miss. Then I tried the
recording flow for real: I wrote an answer naming the off-by-one by hand, recorded it against the PR's diff
(the key the second review had named as missing), and committed it to the PR. CI replayed it and posted that
one finding, "hand-written" in the headline, with the recording listed as not reviewed. That proves the
mechanism on GitHub; a hand-written answer says nothing about a model. The jobs took 13–19 s.

**Why two checkouts in one job?** A second review agent caught this. My first version checked out the PR's
head and ran `uses: ./`. But on `pull_request` the workflow file comes from the merge commit, so the job
fed one commit's inputs to another commit's `action.yml`. On a branch cut before `missing-recording`
existed, the old action would ignore that input and fail on the recording miss, and a fork's old branch
would still try to post. Nothing had hit it yet: both open PRs were cut after the change. Now the action
runs from the merge commit, the same commit as the workflow, and the head goes in `.pr-head` for the
pre-pass, which must read the PR's own lines. The same review also had me compare repository ids rather
than names for the fork rule, and reject a bad `missing-recording` value up front: argparse never saw it
when the review skipped first.

**The job runs the PR's own action with a write token. Isn't that dangerous?** On a pull request from a
branch of this repository, the token is the job's (`contents: read`, `pull-requests: write`) and the PR's
author already has write access, so running their version of the action gives them nothing new. On a fork's
PR, `pull_request` gives the job a read-only token and no secrets, so there is nothing to post with and no
key to steal; that's why it's `pull_request` and never `pull_request_target`, which would run with the base
repository's secrets. Inside the job, the PR's code is still never imported by accident: every `python` runs
with `-I` and ruff runs as a binary (day 3).

**And on a fork's PR the job just fails?** It did, in the first version of the job, and I found it while
writing the answer above: the static-only review always has something to post, the post answers 403 with a
read-only token, and every outside contribution would have had a red check. Before day 5 that needed an
unusual setup (the recorded provider with recordings configured), so nobody hit it. The action now compares
the PR's head repository with the base and, on a fork (or a deleted fork, where `head.repo` is null), leaves
out `--post` and says so in a notice; the review is still in the job summary
(`test_the_review_step_does_not_post_on_a_fork_s_pull_request`).

**Did re-running the measurements for the release find anything?** Yes: one measurement had silently
stopped measuring. `scripts/measure_runner_stall.py` emulates the old pipe runner with the sandbox's real
bootstrap. A day-4 review fix made the bootstrap read two more keys from its spec; the script never passed
them, so its child died with a `KeyError` before starting the sleeper, and the "pipe" returned in 0.1 s,
which the script printed as if the stall were gone. It now passes the full spec and reads the child's report
(a child that never ran a test says so), and `tests/test_measure_runner_stall.py` runs it with a 3 s
sleeper. At 30 s it reproduces day 4's numbers: 30.2 s through the pipe, 0.30 s for the sandbox. The lesson
is the same as day 4's, from the other side: a measurement script is code, and it needs a test.

**How do you test the action's shell?** The CI jobs run the whole action, but only on GitHub. So
`tests/test_action.py` also pulls the Review step's script out of `action.yml` and runs it under
`bash -eo pipefail` the way the runner does, with `codelens` replaced by a stub that records its arguments
and the variables the step exported. That covers each skip (no provider, a moved PR, no key), the
arguments for posting, and the new fallback branch; deleting that branch fails a test.

**What is in the release?** The `v1.0.0` tag and GitHub release; `pyproject.toml` and `codelens.__version__`
at 1.0.0, so the action can be pinned (`vivekdavara/codelens@v1.0.0`); and the measurements in the README
re-run at the release commit (the README's "Release check").

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

**How is it tested?** 669 tests, 99% line and branch coverage (`.venv/bin/pytest --cov`). Besides unit tests:
the git differential test (day 1); a scripted local HTTP server standing in for the vendors and GitHub
(`tests/conftest.py`), so retries, timeouts and refused redirects go over real sockets; seeded fuzz tests
(`tests/test_fuzz.py`) that feed random and mutated JSON to every parser of untrusted input (they found
negative token counts passing through); `scripts/check_history.py`, which parses every commit of a real
repository (0 failures on this one's 132 commits at v1.0.0's release branch, and on another clone's 50
Java/SQL/YAML commits); CI jobs that run full reviews through `action.yml` (the sample PR, an eval case with
the static pre-pass, and every PR to this repository); and the eval set, whose static numbers are pinned by
a test. Day 5 added tests that run the action's shell under bash, tests that compare the README's example
output with what the commands print (`tests/test_readme.py`), and one that keeps a measurement script
measuring (`tests/test_measure_runner_stall.py`). Day 4's runner is tested with real child
processes (`tests/test_sandbox.py`: a hang, an early exit, a background child, planted `coverage.py` and
`pytest.py`, secrets in the parent's environment), and the test-generation sample's numbers are pinned by
`tests/test_testgen_sample.py`.

**Did anything find bugs you had missed?** A review of the whole day-2 diff by nine independent agents (line
by line, removed behaviour, call sites, Python pitfalls, wrappers, reuse, simplification, efficiency,
altitude), each candidate then re-run by a verifier against the pre-fix commit. Every fix landed with a test
that reproduces the bug. The worst were: the CI job would have posted the sample fixture's findings onto
real PRs; the finding cap was applied before the already-posted filter, so new findings could be starved
forever; anyone could forge the hidden markers; and a non-UTF-8 byte in a diff crashed fingerprinting and
recording (after the paid model call).

On day 3, three agents reviewed the static pre-pass, the eval harness and the posting changes, and 26
confirmed problems were fixed, each with a regression test. The one I'd tell first: the
pre-pass ran `python -m ruff` inside the PR's checkout, and `-m` imports from the current directory first,
so a PR adding `ruff.py` ran its own code with the job's API key in the environment, and the review just
reported no findings. Checking for the same pattern found it in the action since day 1 (`python -m pip`,
`python -c 'import json'`); everything now runs ruff as a binary and Python with `-I`. Others: a deeply
nested expression crashed the review (RecursionError), `# noqa` comments silenced rules, a file name could
plant a fingerprint marker, and a lone `\r` shifted static findings by a line. After that, two property
tests found two more: `old_version` checked against real git on 120 random edits got the old file's final
newline wrong when a PR changed it (`tests/test_old_version.py`), and fuzzing the rules with mutated code
showed `ast.parse` printing the PR's SyntaxWarnings into CodeLens's output (`tests/test_static_fuzz.py`).

On day 4, two agents reviewed the runner and the generator, and 17 reproduced problems were fixed, each
with a test that fails on the old code. The one I'd tell first: exit status 0 counted as a pass, but pytest
also exits 0 for an expected failure (xfail) or a test that skips itself after running code, so the
generator kept tests whose assertions had failed. A plugin in the child now counts each test's outcome.
The other good one: candidates shared a directory, so one candidate could write a `conftest.py` that made
the next one's failures pass. Each run now gets a fresh directory and `--noconftest`.

## Limitations and what's next

- **The model's review quality is unmeasured.** The eval set and harness exist; the 14 real answers need an
  API key (`codelens eval --provider anthropic --record`). The hand-written recordings prove the pipeline,
  not the model.
- **The model's test writing is unmeasured too.** The sample's answer is hand-written; one recorded call
  would measure a model. Test generation keeps only passing tests that add line coverage, so it can't keep a
  test of new behaviour on old lines, and it never keeps a test that exposes a bug. Methods aren't targets.
  The action doesn't run it yet: it needs the two-step split described above.
- The static pre-pass is Python only, and its eval recall is an upper bound (same author as the rules).
- Comments on removed lines (LEFT side): the prompt numbers only new-file lines, so a removal is flagged on
  the nearest line still in the file.
- Reviewing only what changed since the last review; today every run reviews the whole PR diff and relies on
  fingerprints to avoid repeats. A re-run on a head that was already reviewed still pays for one model call,
  because earlier reviews are read only when posting.
- **The vendor APIs were never called for real.** The Anthropic and OpenAI providers are tested against local
  fakes of the documented request and response shapes. The GitHub side has now run for real (demo PR #1);
  the model side needs a key.
- **Self-review without a key is static analysis alone**, and a recording answers one exact diff, so any
  later push needs a new recording (or the repository secret, for live reviews).
