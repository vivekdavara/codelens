# Recordings of CodeLens reviewing its own pull requests

The `self-review` job in `.github/workflows/ci.yml` reviews every pull request to this repository with the
recorded provider and this directory. A recording here is a model's answer to one exact prompt, named by the
prompt's SHA-256 (`<key>.json`), so it answers one version of one pull request's diff.

- **No recording for the diff** (the usual case): the review is static analysis alone, and its summary says
  that no model reviewed the diff and which recording would have answered.
- **To have a model's review replayed**, record it with a key and commit the recording to the pull request:

  ```bash
  gh api repos/vivekdavara/codelens/pulls/<number> -H "Accept: application/vnd.github.diff" > pr.diff
  ANTHROPIC_API_KEY=... codelens review pr.diff --provider anthropic --record --recordings .codelens/recordings
  git add .codelens/recordings && git commit -m "Record CodeLens's review" && git push
  ```

  CodeLens never reviews files in this directory, so committing a recording doesn't change the prompt it
  answers. Any later change to the pull request's code does, and needs a new recording.
- **With an `ANTHROPIC_API_KEY` repository secret**, the job reviews live instead and this directory is not
  used.

Run the commands from the repository root, on a checkout of the pull request's head commit: the static
pre-pass reads the files there, and its findings are part of the prompt.
