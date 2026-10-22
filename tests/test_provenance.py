"""Every section of a merged tool names its real data provider and the data's as-of.

2.15.0 labelled get_market_info's FX section only with the old tool's name (Claude I11 could not
say the rate was Toss's). Synthetic providers (the merge stub); synthetic numbers only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from test_merge_equality import FakeBinance, FakeToss, install_stub  # noqa: E402

import server  # noqa: E402
import tools_v2  # noqa: E402

VALID_FROM, VALID_UNTIL = "2026-10-22T09:00:00+09:00", "2026-10-22T09:10:00+09:00"
MARK_MS = 1792627200000  # 2026-10-22T00:00:00Z


@pytest.fixture
def stub(monkeypatch, tmp_path):
    monkeypatch.setattr(FakeToss, "get_exchange_rate", lambda self, base, quote: {
        "base": base, "quote": quote, "rate": "1000", "validFrom": VALID_FROM, "validUntil": VALID_UNTIL})
    monkeypatch.setattr(server, "risk_free_rate",
                        lambda ticker: {"ten_year_treasury_yield_pct": 4.1, "date": "2026-10-21"})
    monkeypatch.setattr(server, "yf", SimpleNamespace(Ticker=lambda symbol: None))
    return install_stub(monkeypatch, tmp_path)


def test_market_info_fx_names_toss_and_its_valid_from(stub):
    env = tools_v2.get_market_info(market="KR")
    fx = env["meta"]["sections"]["fx"]
    assert fx["provider"] == "toss" and "토스증권" in fx["provider_name"]
    assert fx["as_of"] == VALID_FROM and fx["as_of_basis"] == "validFrom" and fx["valid_until"] == VALID_UNTIL
    assert fx["tool"] == "get_exchange_rate" and "source" not in fx and fx["observed_at"]
    assert env["meta"]["providers"] == {"hours": "toss", "fx": "toss", "risk_free_rate": "yfinance"}
    assert env["data"]["fx"]["rate"] == "1000"  # data unchanged


def test_market_info_hours_and_risk_free_rate(stub):
    sections = tools_v2.get_market_info(market="US")["meta"]["sections"]
    assert sections["hours"]["provider"] == "toss"
    assert sections["hours"]["as_of"] is None and sections["hours"]["as_of_reason"] == "SOURCE_TIME_ABSENT"
    rf = sections["risk_free_rate"]
    assert rf["provider"] == "yfinance" and rf["as_of"] == "2026-10-21" and rf["as_of_basis"] == "close_date"


def test_a_failed_section_keeps_its_provider_and_no_time(stub, monkeypatch):
    def down(self, base, quote):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr(FakeToss, "get_exchange_rate", down)
    fx = tools_v2.get_market_info(include=["fx", "hours"])["meta"]["sections"]["fx"]
    assert fx["provider"] == "toss" and fx["as_of"] is None and fx["as_of_reason"] == "SECTION_UNAVAILABLE"


def test_quote_sources_name_the_provider_per_symbol(stub):
    env = tools_v2.get_quote_v2(["AAPL"], provider="auto", market="us")
    row = env["data"]["quotes"][0]
    assert row["provider"] == "toss" and row["sources"]["quote"]["provider"] == "toss"
    assert "providers" not in env["meta"] and "sections" not in env["meta"]
    kis = tools_v2.get_quote_v2(["005930"], provider="kis", market="kr")["data"]["quotes"][0]["sources"]["quote"]
    assert kis["provider"] == "kis" and "한국투자증권" in kis["provider_name"]
    assert kis["as_of"] is None and kis["as_of_reason"] == "UNKNOWN_QUOTE_TIME"  # KIS price TRs carry no time


def test_toss_quote_as_of_is_its_row_timestamp(stub, monkeypatch):
    monkeypatch.setattr(FakeToss, "get_prices", lambda self, symbol: [
        {"symbol": symbol, "lastPrice": "12.5", "timestamp": "2026-10-21T20:00:00Z"}])
    quote = tools_v2.get_quote_v2(["AAPL"], provider="toss", market="us")["data"]["quotes"][0]["sources"]["quote"]
    assert quote["as_of"] == "2026-10-21T20:00:00Z" and quote["as_of_basis"] == "timestamp"


def test_crypto_market_names_venue_and_exchange_time(stub, monkeypatch):
    class Timed(FakeBinance):
        def snapshot(self, symbol):
            return {**super().snapshot(symbol), "mark": {"markPrice": "100.1", "time": MARK_MS}}

    monkeypatch.setattr(server, "_get_binance", Timed)
    env = tools_v2.get_crypto_market("BTCUSDT", include=list(tools_v2.CRYPTO_INCLUDE), limit=3)
    snap = env["meta"]["sections"]["snapshot"]
    assert snap["provider"] == "binance" and snap["venue"] == "binance_usd_m_futures"
    assert snap["as_of"] == "2026-10-22T00:00:00+00:00" and snap["as_of_basis"] == "exchange_time"
    assert all(info["venue"] == "binance_usd_m_futures" for info in env["meta"]["sections"].values())


def test_old_tools_keep_their_2_14_1_envelope(stub):
    env = server.get_exchange_rate(base="USD", quote="KRW")
    assert env["meta"].get("provider") in (None, "") and "providers" not in env["meta"]
    assert "sections" not in env["meta"] and env["data"]["validFrom"] == VALID_FROM
