from __future__ import annotations

from yfinance_metrics import dividend_yield_pct, pct, percent_value


def test_info_fractions_convert_even_above_one():
    # NVDA 10/15 live: returnOnEquity 1.1721 next to returnOnAssets 0.53572.
    assert pct(1.1721) == 117.21
    assert pct(0.53572) == 53.572
    assert pct(-2.5) == -250.0
    assert pct(None) is None


def test_debt_to_equity_is_already_percent():
    assert percent_value(12.95) == 12.95
    assert percent_value(0.5) == 0.5
    assert percent_value("n/a") is None


def test_dividend_yield_fallback_units():
    # No rate/price: only the always-fractional trailingAnnualDividendYield is used.
    assert dividend_yield_pct({"dividendYield": 0.0033})[0] is None
    assert dividend_yield_pct({"dividendYield": 0.44, "trailingAnnualDividendYield": 0.0042})[0] == 0.42
    assert dividend_yield_pct({"trailingAnnualDividendYield": 0.0042})[0] == 0.42
    assert dividend_yield_pct({"trailingAnnualDividendYield": 1.2})[0] == 120.0


def test_tools_wire_fraction_fields(monkeypatch):
    import yfinance_api

    info = {"returnOnEquity": 1.1721, "returnOnAssets": 0.53572, "grossMargins": 0.75,
            "operatingMargins": 0.6, "profitMargins": -2.5, "debtToEquity": 0.5}

    class Ticker:
        def __init__(self, symbol):
            self.info = info

    monkeypatch.setattr(yfinance_api.yf, "Ticker", Ticker)
    monkeypatch.setattr(yfinance_api, "growth_fields", lambda symbol, info: {})
    monkeypatch.setattr(yfinance_api, "analyst_consensus", lambda ticker, info: None)
    metrics = yfinance_api.YFinanceClient().get_key_metrics("NVDA")
    assert (metrics["roe_pct"], metrics["roa_pct"], metrics["net_margin_pct"], metrics["debt_to_equity_pct"]) == (
        117.21, 53.572, -250.0, 0.5)
