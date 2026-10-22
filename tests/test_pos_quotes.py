"""compare_quotes: per-provider priced_at/session and a bp difference (synthetic prices)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server  # noqa: E402


class _Toss:
    def __init__(self, rows):
        self.rows = rows

    def get_prices(self, symbol):
        return self.rows


class _Kis:
    def __init__(self, price=None, error=None):
        self.price, self.error = price, error

    def _quote(self, symbol, **kwargs):
        if self.error:
            raise self.error
        return {"symbol": symbol, "market_code": "J", "price": self.price, "raw": {"stck_prpr": self.price}}

    get_domestic_quote = get_overseas_quote = _quote


def _compare(monkeypatch, rows, kis, symbol="000001", market="kr"):
    monkeypatch.setattr(server, "_get_toss", lambda *a, **k: _Toss(rows))
    monkeypatch.setattr(server, "_get_kis", lambda *a, **k: kis)
    return server.compare_quotes(symbol, market=market)


def test_each_quote_carries_its_own_time_and_session(monkeypatch):
    rows = [{"symbol": "000001", "timestamp": "2026-10-21T19:59:59.000+09:00", "lastPrice": "10050", "currency": "KRW"}]
    result = _compare(monkeypatch, rows, _Kis("10000"))
    assert result["ok"]
    toss, kis = result["data"]["quotes"]
    assert (toss["price"], toss["priced_at"], toss["session"], toss["reason_codes"]) == (
        10050.0, "2026-10-21T19:59:59.000+09:00", "nxt_after", [])
    assert (kis["price"], kis["priced_at"], kis["session"]) == (10000.0, None, None)
    assert kis["reason_codes"] == ["UNKNOWN_QUOTE_TIME"]
    assert toss["observed_at"] == kis["observed_at"] and toss["observed_at"].endswith("+00:00")
    comparison = result["data"]["comparison"]
    assert comparison["toss_minus_kis_bp"] == 50.0
    assert comparison["same_priced_at"] is False
    assert comparison["reason_codes"] == ["QUOTE_TIMES_NOT_ALIGNED"]
    assert toss["data"] == rows and kis["data"]["raw"] == {"stck_prpr": "10000"}  # payloads unchanged


def test_untimed_or_missing_toss_row_is_labelled(monkeypatch):
    rows = [{"symbol": "000001", "timestamp": None, "lastPrice": "10050", "currency": "KRW"}]
    toss = _compare(monkeypatch, rows, _Kis("10000"))["data"]["quotes"][0]
    assert toss["priced_at"] is None and toss["reason_codes"] == ["UNKNOWN_QUOTE_TIME"]
    toss = _compare(monkeypatch, [], _Kis("10000"))["data"]["quotes"][0]
    assert toss["price"] is None and toss["reason_codes"] == ["MISSING_TOSS_QUOTE"]


def test_kis_failure_leaves_no_difference(monkeypatch):
    rows = [{"symbol": "SYNA", "timestamp": "2026-10-21T11:30:00-04:00", "lastPrice": "20.2", "currency": "USD"}]
    result = _compare(monkeypatch, rows, _Kis(error=RuntimeError("down")), symbol="SYNA", market="us")
    data = result["data"]
    assert data["fallback_used"] is True
    assert data["quotes"][0]["session"] == "us_regular"
    assert data["quotes"][1]["price"] is None
    assert data["quotes"][1]["reason_codes"] == ["MISSING_PRICE", "UNKNOWN_QUOTE_TIME"]
    assert data["comparison"]["toss_minus_kis_bp"] is None
    assert "MISSING_PRICE" in data["comparison"]["reason_codes"]


def test_currency_mismatch_is_not_compared(monkeypatch):
    rows = [{"symbol": "SYNA", "timestamp": "2026-10-21T11:30:00-04:00", "lastPrice": "20.2", "currency": "KRW"}]
    data = _compare(monkeypatch, rows, _Kis("20"), symbol="SYNA", market="us")["data"]
    assert data["comparison"]["toss_minus_kis_bp"] is None
    assert "CURRENCY_MISMATCH" in data["comparison"]["reason_codes"]
