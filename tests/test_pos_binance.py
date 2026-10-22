"""The Binance client asks for HTTP/2 only when the optional h2 package is importable.

Live 2.14.1 (no h2 in the venv): every Binance read failed with ModuleNotFoundError once TLS
negotiated h2, so health_check showed binance_futures degraded and the snapshot tool
PROVIDER_ERROR. The real _create_client path; sockets stay blocked (conftest).
"""

from __future__ import annotations

import importlib.machinery
import sys
import types
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import binance_futures_api  # noqa: E402


def _requested_http2(monkeypatch, h2_module):
    seen = []
    real = httpx.HTTPTransport

    def recording(**kwargs):
        seen.append(kwargs["http2"])
        return real(**kwargs)

    monkeypatch.setattr(binance_futures_api.httpx, "HTTPTransport", recording)
    monkeypatch.setitem(sys.modules, "h2", h2_module)
    client = binance_futures_api._create_client()
    client.close()
    return seen


def test_without_h2_the_client_uses_http1(monkeypatch):
    assert _requested_http2(monkeypatch, None) == [False]  # None in sys.modules: h2 not importable


def test_with_h2_the_client_keeps_http2(monkeypatch):
    h2 = types.ModuleType("h2")
    h2.__spec__ = importlib.machinery.ModuleSpec("h2", None)
    assert _requested_http2(monkeypatch, h2) == [True]
