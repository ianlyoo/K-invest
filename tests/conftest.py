"""Tests are synthetic: isolate runtime state and prohibit outgoing sockets."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

# Run before test-module imports; no production credentials or default user cache.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_RUNTIME = tempfile.TemporaryDirectory(prefix="kinvest-tests-", dir=tempfile.gettempdir())
os.environ["KINVEST_CACHE_DIR"] = _RUNTIME.name
os.environ["KINVEST_OAUTH_STATE_DIR"] = _RUNTIME.name + "/oauth"
os.environ["KINVEST_TOSS_INVESTMCP_ACCOUNT_SUFFIX"] = "4321"  # synthetic dedicated-account suffix
for _name in ("MCP_AUTH_TOKEN", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET", "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET",
              "SEC_USER_AGENT",
              "KINVEST_SERVICE_TOKEN", "KINVEST_SERVICE_TOKEN_FILE",
              "TOSS_CREDS_FILE", "TOSS_ACCOUNTS_JSON", "MCP_OAUTH_REFRESH_GRACE_SECONDS", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_CANO", "KIS_ENV_FILE", "KIS_ACCOUNTS_JSON"):
    os.environ.pop(_name, None)

for _name in ("BINANCE_FUTURES_API_KEY_FILE", "BINANCE_FUTURES_SIGNING_KEY_FILE",
              "CREDENTIALS_DIRECTORY", "HYPERLIQUID_ACCOUNT_ADDRESSES"):
    os.environ.pop(_name, None)

# Server import attempts clone-and-run .env loading. Never open real config;
# synthetic *.env fixtures in pytest's temporary directory remain testable.
_path_open = Path.open


def _synthetic_open(path, *args, **kwargs):
    if path.name == ".env" or str(path).startswith("/etc/k-invest"):
        raise FileNotFoundError("Production configuration is unavailable in tests")
    return _path_open(path, *args, **kwargs)


Path.open = _synthetic_open


@pytest.fixture(scope='session')
def require_git_commit():
    """Probe each historical commit once; source-only releases skip its tests."""
    root = Path(__file__).resolve().parents[1]

    @lru_cache(maxsize=None)
    def unavailable(commit):
        # An archive nested below a checkout must not borrow its parent's history.
        if not (root / '.git').exists():
            return 'source tree has no Git repository'
        try:
            probe = subprocess.run(['git', 'cat-file', '-e', commit + '^{commit}'],
                cwd=root, capture_output=True, check=False)
        except OSError:
            return 'Git executable is unavailable'
        if probe.returncode:
            return 'Git repository or commit object is unavailable'
        return None

    def require(commit):
        reason = unavailable(commit)
        if reason:
            pytest.skip(f'Historical Git commit {commit} required: {reason}')

    return require


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Outgoing network is forbidden in synthetic tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def owner_credentials(tmp_path, monkeypatch):
    """Inert owner-gate credential, scoped only to OAuth tests."""
    import owner_gate as gate
    directory = tmp_path / "fake-owner-credentials"
    directory.mkdir()
    passphrase = "fixture-owner-passphrase-distinct-from-client-secret"
    line = gate.hash_passphrase(passphrase)
    for name, value in (("owner-gate", line), ("oauth-client-secret", "fixture-secret")):
        file = directory / name
        file.write_text(value + "\n")
        file.chmod(0o600)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    monkeypatch.setenv("OWNER_GATE_REDIRECT_ORIGINS", '["https://example.invalid", "https://connector.example.invalid", "https://chatgpt.com", "https://claude.ai", "https://claude.com"]')
    monkeypatch.setattr(gate, "_GATE", gate.OwnerGate())
    return passphrase, line, directory


@pytest.fixture(autouse=True)
def isolated_provider_budgets(monkeypatch):
    """Synthetic 429s and pacing debt must not leak between unrelated tests.

    Preserve production intervals and real waits; only reset per-test state.
    """
    import provider_pacing
    import toss_api

    for name in ('SEC_BUDGET', 'BINANCE_BUDGET'):
        current = getattr(provider_pacing, name)
        monkeypatch.setattr(provider_pacing, name, provider_pacing.RequestBudget(current.interval))
    monkeypatch.setattr(toss_api, '_RATE_BUDGETS', {
        group: provider_pacing.RequestBudget(budget.interval)
        for group, budget in toss_api._RATE_BUDGETS.items()
    })
