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


def test_summarize_holdings_empty_positions() -> None:
    from server import _summarize_holdings_sector_exposure

    out = _summarize_holdings_sector_exposure([], {"sector_risk": {}})
    assert out["by_sector"] == {}
