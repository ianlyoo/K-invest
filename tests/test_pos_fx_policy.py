"""Valuation FX is Toss's broker rate, yfinance only as a labelled fallback;
The mark and FX policies are each stated once per response.

The real PortfolioCollector.build_fx and TossClient.get_exchange_rate over a scripted Toss
GET and a stub yfinance module; synthetic rates and values only.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import portfolio_engine as engine  # noqa: E402
from portfolio_model import FX_POLICY, FxRates, Holding  # noqa: E402
from portfolio_sources import Collection, PortfolioCollector, Providers  # noqa: E402
from toss_api import TossClient  # noqa: E402

TOSS_FX = {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1003.5", "midRate": "1001.5",
           "basisPoint": "20", "rateChangeType": "UP", "validFrom": "2026-10-21T18:10:02+09:00",
           "validUntil": "2026-10-21T18:11:02+09:00"}


class FakeToss(TossClient):
    """TossClient with its GET scripted: `fx` is the exchange-rate result or an exception."""

    def __init__(self, fx=None):
        self.fx, self.calls = TOSS_FX if fx is None else fx, []

    def _get(self, path, params=None, account=False):
        self.calls.append((path, dict(params or {})))
        assert path == "/api/v1/exchange-rate"
        if isinstance(self.fx, Exception):
            raise self.fx
        return {"result": dict(self.fx)}


def _yfinance(monkeypatch, rate=1000.25, fail=False):
    calls = []

    class Ticker:
        def __init__(self, pair):
            calls.append(pair)
            self.pair = pair

        def history(self, period):
            import pandas as pd

            if fail:
                raise RuntimeError("yfinance down")
            return pd.DataFrame({"Close": [rate]}, index=pd.DatetimeIndex(
                [pd.Timestamp("2026-10-21 00:00", tz="Europe/London")]))

        def get_history_metadata(self):
            return {"regularMarketPrice": rate, "regularMarketTime": 1792597972}

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    return calls


def _collector(toss, labels=("primary",)):
    return PortfolioCollector(Providers(toss_labels=lambda: list(labels), toss_client=lambda label: toss,
                                        kis_accounts=list, kis_client=None, error_formatter=lambda e: {}))


def test_toss_rate_is_the_valuation_fx_with_its_validity_window(monkeypatch):
    yf_calls = _yfinance(monkeypatch)
    toss = FakeToss()
    fx = _collector(toss).build_fx({"USD", "KRW"})
    assert fx.rates == {"USD": 1003.5} and fx.rate_decimal_by_currency == {"USD": "1003.5"}
    assert fx.priced_at_by_currency == {"USD": TOSS_FX["validFrom"]}
    assert fx.valid_until_by_currency == {"USD": TOSS_FX["validUntil"]}
    assert fx.nav_source_quality_reasons == {} and fx.missing == set()
    assert toss.calls == [("/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"})]
    assert yf_calls == []  # the fallback is not read when Toss is valid
    block = fx.to_dict()
    assert block["policy"] == FX_POLICY and block["policy"]["name"] == "toss_rate_v1"
    assert block["sources"] == {"USD": {"source": "toss", "priced_at": TOSS_FX["validFrom"],
                                        "valid_until": TOSS_FX["validUntil"]}}


@pytest.mark.parametrize("toss_fx, reasons", [
    (RuntimeError("toss down"), ["INVALID_SOURCE_RESPONSE"]),
    ({**TOSS_FX, "baseCurrency": "KRW", "quoteCurrency": "USD"}, ["CURRENCY_BINDING_MISMATCH"]),
    ({**TOSS_FX, "rate": "0"}, ["INVALID_SOURCE_RESPONSE"]),
])
def test_without_a_valid_toss_rate_yfinance_is_the_labelled_fallback(monkeypatch, toss_fx, reasons):
    yf_calls = _yfinance(monkeypatch)
    fx = _collector(FakeToss(toss_fx)).build_fx({"USD"})
    assert fx.rates == {"USD": 1000.25} and yf_calls == ["USDKRW=X"]
    assert fx.nav_source_quality_reasons == {}  # the rate used is valid; the fallback is a label
    assert fx.valid_until_by_currency == {}
    assert fx.to_dict()["sources"]["USD"] == {"source": "yfinance", "priced_at": "2026-10-21T15:52:52+00:00",
                                              "fallback": True, "fallback_reason_codes": reasons}


def test_a_currency_toss_does_not_quote_goes_to_yfinance_without_a_toss_read(monkeypatch):
    _yfinance(monkeypatch, rate=9.125)
    toss = FakeToss()
    fx = _collector(toss).build_fx({"JPY"})
    assert toss.calls == [] and fx.rates == {"JPY": 9.125}
    assert fx.source_by_currency["JPY"]["fallback_reason_codes"] == ["TOSS_FX_CURRENCY_UNSUPPORTED"]


def test_unconfigured_toss_falls_back_and_both_failing_fails_closed(monkeypatch):
    _yfinance(monkeypatch)
    fx = _collector(FakeToss(), labels=()).build_fx({"USD"})
    assert fx.source_by_currency["USD"]["fallback_reason_codes"] == ["TOSS_FX_UNAVAILABLE"]
    _yfinance(monkeypatch, fail=True)
    fx = _collector(FakeToss(RuntimeError("toss down"))).build_fx({"USD"})
    assert fx.rates == {} and fx.missing == {"USD"}
    assert fx.source_by_currency == {"USD": {"source": None, "missing": True, "reason_codes_by_source": {
        "toss": ["INVALID_SOURCE_RESPONSE"], "yfinance": ["INVALID_SOURCE_RESPONSE"]}}}
    assert fx.nav_source_quality_reasons == {"USD": ["INVALID_SOURCE_RESPONSE"]}
    assert fx.to_dict()["missing_currencies"] == ["USD"]


@pytest.mark.parametrize("toss_fx, reasons", [
    ({**TOSS_FX, "baseCurrency": "KRW", "quoteCurrency": "USD", "rate": "0.000752", "midRate": "0.000751"},
     ["CURRENCY_BINDING_MISMATCH"]),
    ({**TOSS_FX, "rate": "-5"}, ["INVALID_SOURCE_RESPONSE"]),
])
def test_a_flagged_toss_rate_with_yfinance_down_is_missing_never_a_valuation_rate(monkeypatch, toss_fx, reasons):
    # A misbound or invalid Toss rate is no rate, even when it is the only one.
    _yfinance(monkeypatch, fail=True)
    fx = _collector(FakeToss(toss_fx)).build_fx({"USD", "KRW"})
    assert fx.rates == {} and fx.missing == {"USD"} and fx.rate("USD") is None
    assert fx.to_dict()["missing_currencies"] == ["USD"]
    assert fx.to_dict()["sources"]["USD"] == {"source": None, "missing": True, "reason_codes_by_source": {
        "toss": reasons, "yfinance": ["INVALID_SOURCE_RESPONSE"]}}
    consolidated = engine.consolidate_holdings(_holdings(), fx)
    usd = next(row for row in consolidated["positions"] if row["symbol"] == "SYNA")
    assert usd["market_value_krw"] is None and usd["weight_pct"] is None
    risk = engine.concentration_risk(consolidated)
    assert risk["fx"]["missing_currencies"] == ["USD"] and risk["valued_position_count"] == 1
    assert risk["concentration"]["top1_weight_pct"] is None and risk["concentration"]["risk_level"] is None


def test_a_non_positive_rate_from_any_source_is_no_rate(monkeypatch):
    _yfinance(monkeypatch, rate=-3.0)
    fx = _collector(FakeToss(RuntimeError("toss down"))).build_fx({"USD"})
    assert fx.rates == {} and fx.missing == {"USD"}
    assert FxRates(rates={"USD": 0.0}).to_krw(10.0, "USD") is None


def test_an_injected_quote_stays_the_only_source(monkeypatch):
    yf_calls = _yfinance(monkeypatch)
    toss = FakeToss()
    collector = PortfolioCollector(Providers(toss_labels=lambda: ["primary"], toss_client=lambda label: toss,
                                             kis_accounts=list, kis_client=None, error_formatter=lambda e: {},
                                             fx_quote=lambda currency: {"rate": 1200.0, "priced_at": None}))
    fx = collector.build_fx({"USD"})
    assert fx.rates == {"USD": 1200.0} and toss.calls == [] and yf_calls == []
    assert fx.source_by_currency["USD"] == {"source": "provider", "priced_at": None}


# ── stated once per response ─────────────────────────────


def _holdings():
    return [Holding("kis", "kis:domestic", "000001", "KRW", 10, market="KR", last_price=1000.0,
                    avg_price=900.0, cost_basis=9000.0, market_value=10000.0),
            Holding("toss", "toss:primary", "SYNA", "USD", 2, market="US", last_price=20.0,
                    avg_price=15.0, cost_basis=30.0, market_value=40.0)]


class _StubCollector:
    def __init__(self, fx_collector):
        self.fx_collector = fx_collector

    def collect_positions(self):
        health = {name: {"account": name, "status": "ok", "last_error": None} for name in ("kis:domestic", "toss:primary")}
        return Collection(holdings=_holdings(), health=health, fetched_at="2026-10-21T09:10:05+00:00")

    def build_fx(self, currencies):
        return self.fx_collector.build_fx(currencies)

    def health_report(self):
        return {}


def _count(payload, needle):
    return json.dumps(payload, ensure_ascii=False).count(needle)


def test_each_tool_states_the_mark_and_fx_policy_once(monkeypatch, tmp_path):
    import server
    from portfolio_store import NavStore

    _yfinance(monkeypatch)
    toss = FakeToss()
    monkeypatch.setattr(server, "_get_collector", lambda: _StubCollector(_collector(toss)))
    monkeypatch.setattr(server, "_get_toss", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no quotes")))
    monkeypatch.setattr(server, "_get_nav_store", lambda: NavStore(tmp_path / "nav.db"))
    outputs = {
        "holdings": server.get_consolidated_holdings(detail_level="summary"),
        "risk_summary": server.get_portfolio_risk(detail_level="summary"),
        "risk_full": server.get_portfolio_risk(detail_level="full"),
        "summary": server.get_portfolio_summary(include_realized=False, snapshot=False),
    }
    for name, envelope in outputs.items():
        assert envelope["ok"], name
        assert _count(envelope, engine.MARK_POLICY["rule"]) == 1, name
        assert _count(envelope, FX_POLICY["rule"]) == 1, name
        assert envelope["data"]["fx"]["sources"]["USD"]["source"] == "toss", name
    for name in ("holdings", "summary"):
        assert outputs[name]["data"]["methodology"]["fx_valuation"].startswith("current spot rate per fx.policy")
    # The Toss validity window reaches the NAV evidence (fx read time basis), unchanged path.
    fx_times = outputs["summary"]["data"]["mark_quality"]["fx"]["USD"]
    assert fx_times["freshness_time_basis"] == "fx_read"
