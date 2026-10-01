# Deployed versus GitHub code comparison

Deployed tracked tree: clean at fbe224e939f377bf77d64b7038a577a37628a94a.
Fresh clone: 9845c4f43c49d9110955f5b398450f8f0213350d.
Compared tracked Python sources, tests, build/dependency manifests and deployment templates only; no environment, credential, cache or live proxy files.

Differences:
- `deploy/k-invest-snapshot.service`: deployed-only
- `deploy/k-invest-snapshot.timer`: deployed-only
- `kis_accounts.py`: deployed-only
- `kis_api.py`: different
- `oauth_provider.py`: different
- `portfolio_engine.py`: deployed-only
- `portfolio_model.py`: deployed-only
- `portfolio_sources.py`: deployed-only
- `portfolio_store.py`: deployed-only
- `pyproject.toml`: different
- `requirements.txt`: different
- `scripts/probe_kis.py`: deployed-only
- `scripts/snapshot_nav.py`: deployed-only
- `server.py`: different
- `tests/test_kis_accounts.py`: deployed-only
- `tests/test_kis_portfolio.py`: deployed-only
- `tests/test_portfolio_engine.py`: deployed-only
- `tests/test_portfolio_integration.py`: deployed-only
- `tests/test_portfolio_model.py`: deployed-only
- `tests/test_portfolio_sources.py`: deployed-only
- `tests/test_portfolio_store.py`: deployed-only
- `tests/test_portfolio_tools.py`: deployed-only
- `tests/test_server_main.py`: different
- `toss_api.py`: different

Candidate preserves deployed portfolio/multi-account behavior atop the fresh clone. No uncommitted host differences were found.

Original comparison retained by the lead in investmcp/DEPLOYED_DIFF.md.
