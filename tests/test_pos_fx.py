"""yfinance FX carries the trade time, never the daily-bar label, and no float32 noise.

The real yfinance_fx_quote / build_fx path over a stub yfinance module (synthetic rates).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import portfolio_sources  # noqa: E402
from portfolio_sources import PortfolioCollector, Providers  # noqa: E402

DAY_LABEL = pd.Timestamp("2026-10-21 00:00:00", tz="Europe/London")


def _stub(monkeypatch, close, metadata):
    class Ticker:
        def __init__(self, pair):
            self.pair = pair

        def history(self, period):
            values = np.array([close], dtype=np.float32).astype(np.float64)
            return pd.DataFrame({"Close": values}, index=pd.DatetimeIndex([DAY_LABEL]))

        def get_history_metadata(self):
            if isinstance(metadata, Exception):
                raise metadata
            return metadata

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))


def test_price_time_is_the_last_trade_not_the_day_label(monkeypatch):
    _stub(monkeypatch, 1001.14, {"regularMarketPrice": 1001.06,
                                 "regularMarketTime": pd.Timestamp("2026-10-21 16:52:52", tz="Europe/London")})
    quote = portfolio_sources.yfinance_fx_quote("USD", include_time=True)
    assert quote == {"rate": 1001.06, "priced_at": "2026-10-21T16:52:52+01:00"}


def test_epoch_market_time_is_read_as_utc(monkeypatch):
    _stub(monkeypatch, 1001.14, {"regularMarketPrice": 1001.06, "regularMarketTime": 1792597972})
    quote = portfolio_sources.yfinance_fx_quote("USD", include_time=True)
    assert quote["priced_at"] == "2026-10-21T15:52:52+00:00"


@pytest.mark.parametrize("metadata", [{}, {"regularMarketPrice": 1001.06}, RuntimeError("no metadata"),
                                      {"regularMarketPrice": 1001.06, "regularMarketTime": pd.Timestamp("2026-10-21 16:52:52")}])
def test_without_a_trade_time_the_rate_has_no_time(monkeypatch, metadata):
    _stub(monkeypatch, 1001.14, metadata)
    quote = portfolio_sources.yfinance_fx_quote("USD", include_time=True)
    assert quote == {"rate": 1001.14, "priced_at": None}  # float32 noise removed, day label never used


def test_an_intraday_bar_time_is_kept_when_metadata_has_none(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "DAY_LABEL", pd.Timestamp("2026-10-21 16:52:00", tz="Europe/London"))
    _stub(monkeypatch, 1001.14, {})
    assert portfolio_sources.yfinance_fx_quote("USD", include_time=True)["priced_at"] == "2026-10-21T16:52:00+01:00"


def test_float32_recovery_leaves_real_doubles_alone():
    assert portfolio_sources._float32_decimal(float(np.float32(1001.14))) == 1001.14
    assert portfolio_sources._float32_decimal(1001.1400000001) == 1001.1400000001
    assert portfolio_sources._float32_decimal(float("nan")) is None


def test_build_fx_records_the_trade_time(monkeypatch):
    _stub(monkeypatch, 1001.14, {"regularMarketPrice": 1001.06,
                                 "regularMarketTime": pd.Timestamp("2026-10-21 16:52:52", tz="Europe/London")})
    collector = PortfolioCollector(Providers(toss_labels=list, toss_client=None, kis_accounts=list,
                                             kis_client=None, error_formatter=lambda e: {}))
    fx = collector.build_fx({"USD", "KRW"})
    assert fx.rates == {"USD": 1001.06}
    assert fx.priced_at_by_currency == {"USD": "2026-10-21T16:52:52+01:00"}
    assert fx.nav_source_quality_reasons == {}
