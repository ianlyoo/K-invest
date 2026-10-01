"""Tests are synthetic: isolate runtime state and prohibit outgoing sockets."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

# Run before test-module imports; no production credentials or default user cache.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_RUNTIME = tempfile.TemporaryDirectory(prefix="kinvest-tests-", dir="/tmp")
os.environ["KINVEST_CACHE_DIR"] = _RUNTIME.name
os.environ["KINVEST_OAUTH_STATE_DIR"] = _RUNTIME.name + "/oauth"
for _name in ("MCP_AUTH_TOKEN", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET", "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET",
              "TOSS_CREDS_FILE", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_CANO", "KIS_ENV_FILE", "KIS_ACCOUNTS_JSON"):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Outgoing network is forbidden in synthetic tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
