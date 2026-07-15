from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server


def test_main_when_no_auth_token_then_exits(monkeypatch) -> None:
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", "")
    with pytest.raises(SystemExit):
        server.main()


def test_main_when_placeholder_token_then_exits(monkeypatch) -> None:
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", "generate_a_random_token_here")
    with pytest.raises(SystemExit):
        server.main()
