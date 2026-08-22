# Contributing

Thanks for contributing!

## Quick start
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt  # or pip install -e .[dev]
pytest -q
ruff check .
```

## Branch & PR
- Branch from `master` (or `main`).
- One PR per feature/fix, keep diff focused.
- Fill the PR template, link the issue.

## Code style
- `ruff check .` must pass.
- No secrets in commits.

## Reporting issues
Use the issue template.
