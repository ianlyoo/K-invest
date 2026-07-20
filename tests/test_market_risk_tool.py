from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_summarize_market_risk_extracts_holdings_exposure() -> None:
    from server import _summarize_holdings_sector_exposure

    market_risk = {
        "sector_risk": {"XLK": {"score": 72, "level": "high"}, "XLP": {"score": 20, "level": "low"}},
    }
    positions = [
        {"symbol": "NVDA", "currency": "USD", "market_value": 8000.0, "sector_etf": "XLK"},
        {"symbol": "KO", "currency": "USD", "market_value": 2000.0, "sector_etf": "XLP"},
    ]
    out = _summarize_holdings_sector_exposure(positions, market_risk)
    # 비중 가중: XLK 80% × 위험 72 우세
    assert out["by_sector"]["XLK"]["weight_pct"] == 80.0
    assert out["by_sector"]["XLK"]["risk_score"] == 72
    assert "XLK" in out["headline"]
    # by_sector/headline shape unchanged; no usdkrw_rate passed → unnormalized
    assert out["fx_normalized"] is False


def test_summarize_holdings_empty_positions() -> None:
    from server import _summarize_holdings_sector_exposure

    out = _summarize_holdings_sector_exposure([], {"sector_risk": {}})
    assert out["by_sector"] == {}
    assert "headline" in out


def test_summarize_holdings_fx_normalizes_mixed_currency_positions() -> None:
    """Reviewer-flagged gap: raw market_value sums mix KRW (Toss/KIS domestic)
    and USD (KIS overseas) positions, so a ~5,000,000 KRW position (~$3,800)
    drowns out an $8,000 USD position's weight_pct. With usdkrw_rate supplied,
    both should convert to a common USD basis before weighting.
    """
    from server import _summarize_holdings_sector_exposure

    market_risk = {
        "sector_risk": {"XLK": {"score": 72, "level": "high"}, "XLF": {"score": 30, "level": "medium"}},
    }
    positions = [
        # ~5,000,000 KRW at 1,300 KRW/USD ≈ $3,846 — Korean bank stock (XLF proxy)
        {"symbol": "005930", "currency": "KRW", "market_value": 5_000_000.0, "sector_etf": "XLF"},
        # $8,000 USD — KIS overseas holding (XLK proxy)
        {"symbol": "NVDA", "currency": "USD", "market_value": 8_000.0, "sector_etf": "XLK"},
    ]

    out = _summarize_holdings_sector_exposure(positions, market_risk, usdkrw_rate=1300.0)

    assert out["fx_normalized"] is True
    assert "by_sector" in out and "headline" in out
    # USD position (~$8,000) should carry a meaningful share once KRW (~$3,846) is
    # normalized to the same basis — well above the ~0.16% it'd get from a raw sum.
    assert out["by_sector"]["XLK"]["weight_pct"] > 50.0
    # sanity: weights should roughly sum to 100
    total_weight = sum(v["weight_pct"] for v in out["by_sector"].values())
    assert 99.0 <= total_weight <= 101.0


def test_summarize_holdings_no_rate_falls_back_unnormalized() -> None:
    """usdkrw_rate=None (FX lookup unavailable) must not error — falls back to
    the raw-sum behavior with fx_normalized=False so the caller/LLM knows
    weights aren't comparable across currencies."""
    from server import _summarize_holdings_sector_exposure

    market_risk = {"sector_risk": {"XLK": {"score": 72, "level": "high"}}}
    positions = [
        {"symbol": "005930", "currency": "KRW", "market_value": 5_000_000.0, "sector_etf": "XLF"},
        {"symbol": "NVDA", "currency": "USD", "market_value": 8_000.0, "sector_etf": "XLK"},
    ]

    out = _summarize_holdings_sector_exposure(positions, market_risk, usdkrw_rate=None)

    assert out["fx_normalized"] is False
    assert "by_sector" in out and "headline" in out


def test_get_market_risk_keeps_kis_holdings_when_toss_registry_fails(monkeypatch) -> None:
    """Reviewer-flagged gap: _get_toss_registry().account_labels() raising
    NotConfiguredError (KIS-only user, Toss unconfigured) used to blow past the
    already-built KIS probes before the execution loop ran, so holdings_exposure
    came back null even though KIS holdings were fully queryable. The fix wraps
    only the toss-label-appending loop in its own try/except (mirroring
    get_portfolio_risk), so KIS probes still execute.

    All upstreams (margin-ta, KIS, yfinance, toss registry) are monkeypatched so
    this stays a true unit test: no subprocess, no network, no port 8100.
    """
    import server
    from kinvest_common import NotConfiguredError

    class _FakeKIS:
        def get_domestic_balance(self) -> dict:
            return {
                "output": [
                    {"pdno": "005930", "prdt_name": "Samsung Electronics", "evlu_amt": "5000000", "hldg_qty": "10"}
                ]
            }

        def get_overseas_balance(self) -> dict:
            return {"output": []}

    class _FakeYF:
        def get_key_metrics(self, symbol: str) -> dict:
            return {"sector": "Technology"}

    class _FakeMT:
        def market_risk(self) -> dict:
            return {
                "score": 50,
                "regime": "neutral",
                "alerts": [],
                "indicators": {},
                "sector_risk": {"XLK": {"score": 70, "level": "high"}},
                "unavailable": [],
                "as_of": "2026-07-20",
            }

    def _raise_not_configured() -> None:
        raise NotConfiguredError("toss not configured")

    monkeypatch.setattr(server, "_get_mt", lambda: _FakeMT())
    monkeypatch.setattr(server, "_get_kis", lambda: _FakeKIS())
    monkeypatch.setattr(server, "_get_yf", lambda: _FakeYF())
    monkeypatch.setattr(server, "_get_toss_registry", _raise_not_configured)

    result = server.get_market_risk(detail_level="full")

    assert result["ok"] is True
    holdings_exposure = result["data"]["holdings_exposure"]
    assert holdings_exposure is not None, "KIS holdings must still map to sectors when Toss is unconfigured"
    assert "XLK" in holdings_exposure["by_sector"]
    assert holdings_exposure["by_sector"]["XLK"]["weight_pct"] == 100.0
