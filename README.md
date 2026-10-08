# CodeLens

An LLM pull-request reviewer and test generator that runs as a GitHub Action.

**Status: day 1 of 5** — design, package scaffold, `action.yml`, and the unified-diff parser. The review engine,
static pre-pass, eval set and test generation arrive on days 2–4. See [DESIGN.md](DESIGN.md).

## Develop

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

## License

MIT
