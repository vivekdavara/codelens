# Changelog

## 1.0.0 (2026-10-12)

The first release. CodeLens reviews a pull request's diff with a model and static analysis, leaves a few
anchored comments as one GitHub review, and proposes pytest tests for changed Python functions.

- **Diff parser** (`codelens diff`): strict unified-diff parsing with old and new line numbers, git's
  extended headers, renames, quoted paths and binary files; checked against real git on random edits.
- **Review** (`codelens review`): one model call per PR (recorded by default; Anthropic or OpenAI when opted
  in), a findings schema with strict validation, and anchoring that drops a finding unless its line is in the
  diff and it quotes that line. One `COMMENT` review per run, never retried, with no repeated comments across
  pushes. Dry run by default in the CLI.
- **Static pre-pass** (`codelens static`): ruff 0.16.10 (isolated, `--ignore-noqa`) and four CodeLens rules
  (CL001 to CL004) on the PR's checkout, reporting only what the PR introduced; the findings are posted and
  shown to the model.
- **Eval set** (`codelens eval`): 14 pull requests with 23 seeded bugs, scored per source by precision and
  recall.
- **Test generation** (`codelens testgen`): pytest tests for the changed top-level functions, each run in a
  scrubbed child process with a timeout, kept only if it passes and runs a changed line nothing else ran.
- **GitHub Action** (`action.yml`): the review on `pull_request`, with every input passed through env and every
  `python` run isolated (`-I`).
- **Self-review**: CodeLens reviews its own pull requests in CI. Without a key, a committed recording is
  replayed or the review is static analysis alone (`missing-recording: static`); with an `ANTHROPIC_API_KEY`
  secret it is live. Recordings are never reviewed, and nothing is posted on a fork's pull request, whose
  token is read-only.

Not measured in this release: the model's review quality and test writing, because no API key was available.
The README's "Not measured: the model" has the commands that record them.
