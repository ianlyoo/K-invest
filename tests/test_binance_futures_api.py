from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from binance_futures_api import BinanceFuturesClient


class FakeBinanceFuturesClient(BinanceFuturesClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str | int]]] = []

    def _get_json(self, path: str, params: dict[str, str | int]) -> Any:
        self.calls.append((path, params))
        if path == "/fapi/v2/ticker/price":
            return {"symbol": "BTCUSDT", "price": "60000.50", "time": 1589437530011}
        if path == "/fapi/v1/premiumIndex":
            return {
                "symbol": "BTCUSDT",
                "markPrice": "60010.25",
                "indexPrice": "60005.10",
                "lastFundingRate": "0.00010000",
                "interestRate": "0.00010000",
                "nextFundingTime": 1597392000000,
                "time": 1597370495002,
            }
        if path == "/fapi/v1/fundingRate":
            return [
                {
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.00010000",
                    "fundingTime": 1570636800000,
                    "markPrice": "60010.25",
                }
            ]
        if path == "/fapi/v1/openInterest":
            return {"openInterest": "123.45", "symbol": "BTCUSDT", "time": 1589437530011}
        if path == "/fapi/v1/klines":
            return [
                [
                    1499040000000,
                    "1.0",
                    "2.0",
                    "0.5",
                    "1.5",
                    "100.0",
                    1499644799999,
                    "150.0",
                    9,
                    "60.0",
                    "90.0",
                    "0",
                ]
            ]
        raise AssertionError(path)


def test_snapshot_when_public_payloads_then_normalized_fields() -> None:
    client = FakeBinanceFuturesClient()

    result = client.snapshot("btc/usdt")

    assert result["symbol"] == "BTCUSDT"
    assert result["quote"]["price"] == 60000.5
    assert result["mark"]["mark_price"] == 60010.25
    assert result["mark"]["last_funding_rate_pct"] == 0.01
    assert result["funding_rates"][0]["funding_rate_pct"] == 0.01
    assert result["open_interest"]["open_interest"] == 123.45
    assert result["open_interest_notional"] == 7408265.3625
    assert result["auth_required"] is False


def test_klines_when_binance_array_payload_then_named_ohlcv_rows() -> None:
    client = FakeBinanceFuturesClient()

    rows = client.klines("BTCUSDT", interval="1h", limit=1, price_type="last")

    assert rows == [
        {
            "open_time": 1499040000000,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100.0,
            "close_time": 1499644799999,
            "quote_volume": 150.0,
            "trade_count": 9,
            "taker_buy_base_volume": 60.0,
            "taker_buy_quote_volume": 90.0,
        }
    ]
