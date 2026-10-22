"""Look-through exposure per underlying.

The real consolidate_holdings / summarize_portfolio / concentration_risk over synthetic
holdings and a synthetic product map (fixtures/product_map.synthetic.json), with perps from the real FuturesAccounts + Hyperliquid client over mocked HTTP
and stablecoin quotes from the real StablecoinQuotes over a mocked Upbit ticker.
Synthetic numbers and a synthetic address only.
"""

from __future__ import annotations

import json
import re
import sys
from functools import partial
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import crypto_futures_accounts as futures  # noqa: E402
import crypto_nav_capture  # noqa: E402
import portfolio_engine as engine  # noqa: E402
import portfolio_model  # noqa: E402
from portfolio_model import FxRates, Holding  # noqa: E402
from portfolio_sources import Collection  # noqa: E402

MAP_FILE = PROJECT_ROOT / 'tests' / 'fixtures' / 'product_map.synthetic.json'
MAP_VERSION, PRODUCT_MAP, PERP_MAP, UNDERLYING_NAMES = portfolio_model.load_product_map(str(MAP_FILE))
ADDRESS = '0x' + 'e' * 36 + '5d1f'
MS = 1792600000000
MAIN = [{'coin': 'BTC', 'szi': '-0.01', 'positionValue': '1000', 'liquidationPx': '130000',
         'leverage': {'type': 'cross', 'value': 5}}]
XYZ = [{'coin': 'xyz:ACME', 'szi': '6', 'positionValue': '300', 'liquidationPx': '45',
        'leverage': {'type': 'isolated', 'value': 10}},
       {'coin': 'xyz:SYNX', 'szi': '1', 'positionValue': '20', 'liquidationPx': '10',
        'leverage': {'type': 'isolated', 'value': 3}}]


def _holding(account, symbol, currency, quantity, price, market):
    value = quantity * price
    return Holding("toss" if account.startswith("toss") else "kis", account, symbol, currency, quantity,
                   market=market, last_price=price, avg_price=price, cost_basis=value, market_value=value)


HOLDINGS = [
    _holding("toss:primary", "ACME", "USD", 10, 50.0, "US"),
    _holding("toss:primary", "ACMU", "USD", 5, 20.0, "US"),
    _holding("toss:primary", "KOR3", "USD", 1, 100.0, "US"),
    _holding("kis:domestic", "900100", "KRW", 2, 100000.0, "KR"),
    _holding("kis:domestic", "9001A0", "KRW", 10, 10000.0, "KR"),
    _holding("toss:primary", "9001B0", "KRW", 10, 5000.0, "KR"),
    _holding("kis:domestic", "390390", "KRW", 10, 10000.0, "KR"),  # a 1x index ETF the map does not cover
]
HOLDINGS_VALUE_KRW = 1150000.0


@pytest.fixture(autouse=True)
def _synthetic_product_map(monkeypatch):
    for name, value in (("PRODUCT_MAP_VERSION", MAP_VERSION), ("PRODUCT_MAP", PRODUCT_MAP),
                        ("PERP_MAP", PERP_MAP), ("UNDERLYING_NAMES", UNDERLYING_NAMES)):
        monkeypatch.setattr(engine, name, value)


def test_without_a_map_file_every_product_is_unmapped():
    assert portfolio_model.load_product_map(None) == ("product_map_empty", {}, {}, {})


def test_a_malformed_map_entry_is_refused(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"products": {"US:X": {"kind": "leveraged_etf", "underlying": "US:Y"}}}))
    with pytest.raises(ValueError, match="US:X"):
        portfolio_model.load_product_map(str(path))


def _fx():
    return FxRates(rates={"USD": 1000.0}, as_of="2026-10-21T09:10:05+00:00")


def _hyperliquid(sent=None, hold=None):
    def handler(request):
        body = json.loads(request.content)
        if sent is not None:
            sent.append(body['type'])
        if body['type'] == 'userFunding' and hold is not None:  # a slow paced history page
            hold[0].set()
            hold[1].wait(10)
            return httpx.Response(200, json=[])
        if body['type'] == 'perpDexs':
            return httpx.Response(200, json=[None, {'name': 'xyz'}])
        if body['type'] == 'userAbstraction':
            return httpx.Response(200, json='default')
        if body['type'] == 'spotClearinghouseState':
            return httpx.Response(200, json={'balances': []})
        rows = XYZ if body.get('dex') == 'xyz' else MAIN
        return httpx.Response(200, json={'assetPositions': [{'position': p} for p in rows],
                                         'marginSummary': {'accountValue': '500', 'totalMarginUsed': '40'},
                                         'withdrawable': '460'})
    return futures.HyperliquidAccountClient([ADDRESS], http=httpx.Client(transport=httpx.MockTransport(handler)),
                                            sleeper=lambda _: None)


def _perps():
    return futures.FuturesAccounts(None, _hyperliquid).query('positions', 'all')


def _quotes(prices=(('USDT', 1400), ('USDC', 1390))):
    def handler(request):
        assert request.url.host == 'api.upbit.com' and request.url.path == '/v1/ticker'
        return httpx.Response(200, json=[{'market': 'KRW-' + coin, 'trade_price': price, 'trade_timestamp': MS,
                                          'timestamp': MS, 'trade_date': '20261021', 'trade_time': '091000'}
                                         for coin, price in prices])
    return partial(crypto_nav_capture.StablecoinQuotes, http=httpx.Client(transport=httpx.MockTransport(handler)),
                   clock=lambda: MS / 1000, sleeper=lambda _: None)


def _by_underlying(view):
    return {row['underlying']: row for row in view['by_underlying']}


def test_map_products_are_loaded_with_a_cited_source():
    expected = {"US:ACMU": ("US:ACME", 2), "US:KOR3": ("index:SYN_KOREA", 3),
                "KR:900200": ("index:SYN_SEMI", 2), "KR:9001A0": ("KR:900100", 2),
                "KR:9001B0": ("KR:900100", 2), "US:MEM2": ("US:MEMF", 2), "US:MEMF": ("US:MEMF", 1)}
    for key, (underlying, leverage) in expected.items():
        entry = PRODUCT_MAP[key]
        assert (entry["underlying"], entry["leverage_target"]) == (underlying, leverage), key
    for key, entry in [*PRODUCT_MAP.items(), *PERP_MAP.items()]:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["source"]["date"]), key
        if entry["kind"] != "common_stock":
            assert entry["source"]["url"].startswith("https://") and entry["source"]["evidence"], key
        assert entry["reset"] in (None, "daily") if "reset" in entry else entry["kind"] == "equity_perp"
    assert all(entry["reset"] == "daily" for entry in PRODUCT_MAP.values() if entry["kind"] == "leveraged_etf")


def test_fund_sources_are_at_most_six_months_older_than_the_map():
    from datetime import date

    built = date.fromisoformat(MAP_VERSION.rsplit("_", 1)[1])
    for key, entry in PRODUCT_MAP.items():
        if entry["kind"] != "common_stock":
            assert 0 <= (built - date.fromisoformat(entry["source"]["date"])).days <= 183, key


def test_exposure_sums_value_times_leverage_and_perp_notional_per_underlying(monkeypatch):
    perp_fx = _quotes()().read()
    view = engine.consolidate_holdings(HOLDINGS, _fx(), perps=_perps(), perp_fx=perp_fx)["underlying_exposure"]
    rows = _by_underlying(view)
    # ACME: shares 500k + ACMU 100k x 2 + xyz:ACME 300 USDC x 1390
    assert rows["US:ACME"]["exposure_krw"] == 500000 + 200000 + 417000
    assert rows["US:ACME"]["exposure_pct"] == round(1117000 / HOLDINGS_VALUE_KRW * 100, 4)
    assert [leg["symbol"] for leg in rows["US:ACME"]["legs"]] == ["ACME", "ACMU", "xyz:ACME"]
    perp_leg = rows["US:ACME"]["legs"][2]
    assert perp_leg["kind"] == "equity_perp" and perp_leg["notional"] == "300" and perp_leg["rate_to_krw"] == "1390"
    assert perp_leg["account_id"].startswith("acct_") and ADDRESS[2:] not in json.dumps(view)
    # 합성전자: shares + two single-stock 2x ETFs on it
    assert rows["KR:900100"]["exposure_krw"] == 200000 + 2 * 100000 + 2 * 50000
    assert rows["index:SYN_KOREA"]["exposure_krw"] == 3 * 100000
    assert {leg["reset"] for leg in rows["KR:900100"]["legs"] if leg["kind"] == "leveraged_etf"} == {"daily"}
    # A short main-dex perp is negative exposure to its coin.
    assert rows["crypto:BTC"]["exposure_krw"] == -1390000 and rows["crypto:BTC"]["legs"][0]["side"] == "short"
    assert view["by_underlying"][0]["underlying"] == "crypto:BTC"  # ordered by absolute exposure
    # Unknown products are listed, never guessed.
    assert [(row["symbol"], row["kind"], row["reason_codes"]) for row in view["unmapped"]] == [
        ("390390", "holding", ["NOT_IN_PRODUCT_MAP"]), ("xyz:SYNX", "perp", ["NOT_IN_PRODUCT_MAP"])]
    assert view["coverage"] == {"mapped_holding_weight_pct": round(1050000 / HOLDINGS_VALUE_KRW * 100, 4),
                                "unmapped_holding_weight_pct": round(100000 / HOLDINGS_VALUE_KRW * 100, 4),
                                "unmapped_count": 2}
    assert view["perps"]["status"] == "complete" and view["perps"]["leg_count"] == 2
    assert view["perps"]["conversion"]["USDC"]["source"] == "upbit_public_ticker"
    assert view["perps"]["venues"]["binance"]["source_status"] == "not_configured"
    assert set(view["products"]) == {"US:ACMU", "US:KOR3", "KR:9001A0", "KR:9001B0", "hyperliquid:xyz:ACME"}
    assert "daily target" in view["method"]["leverage"]


def test_perps_never_reach_nav_totals_or_weights():
    plain = engine.consolidate_holdings(HOLDINGS, _fx())
    with_perps = engine.consolidate_holdings(HOLDINGS, _fx(), perps=_perps(), perp_fx=_quotes()().read())
    for field in ("totals", "by_currency", "by_market", "position_count"):
        assert with_perps[field] == plain[field], field
    assert [p["weight_pct"] for p in with_perps["positions"]] == [p["weight_pct"] for p in plain["positions"]]
    assert plain["totals"]["market_value_krw"] == HOLDINGS_VALUE_KRW
    assert plain["underlying_exposure"]["perps"] == {"status": "not_read", "reason_codes": ["PERPS_NOT_READ"]}
    summary = engine.summarize_portfolio(HOLDINGS, [], _fx(), perps=_perps(), perp_fx=_quotes()().read())
    assert summary["total_assets_krw"] == summary["stock_value_krw"] == HOLDINGS_VALUE_KRW
    assert summary["underlying_exposure"]["perps"]["leg_count"] == 2
    risk = engine.concentration_risk(with_perps)
    assert risk["underlying_exposure"] == with_perps["underlying_exposure"]
    assert risk["concentration"] == engine.concentration_risk(plain)["concentration"]


def test_an_unconverted_leg_nulls_its_underlying_instead_of_a_short_sum():
    view = engine.consolidate_holdings(HOLDINGS, _fx(), perps=_perps(), perp_fx={})["underlying_exposure"]
    rows = _by_underlying(view)
    assert rows["US:ACME"]["exposure_krw"] is None and rows["US:ACME"]["exposure_pct"] is None
    assert rows["US:ACME"]["reason_codes"] == ["LEG_EXPOSURE_UNAVAILABLE"]
    assert rows["US:ACME"]["legs"][2]["reason_codes"] == ["STABLECOIN_KRW_RATE_UNAVAILABLE"]
    assert rows["KR:900100"]["exposure_krw"] == 500000  # holdings-only underlyings are unaffected
    assert view["perps"]["status"] == "partial" and "PERP_LEG_UNCONVERTED" in view["perps"]["reason_codes"]
    missing_fx = engine.consolidate_holdings(HOLDINGS, FxRates())["underlying_exposure"]
    assert _by_underlying(missing_fx)["US:ACME"]["legs"][0]["reason_codes"] == ["MARKET_VALUE_KRW_UNAVAILABLE"]


@pytest.mark.parametrize("raw, underlying", [
    ({'symbol': 'BTCUSDT', 'positionAmt': '0.01', 'markPrice': '100000', 'liquidationPrice': '80000',
      'notional': '1000', 'marginAsset': 'USDT'}, 'crypto:BTC'),
    ({'symbol': 'BTCUSDT_261225', 'positionAmt': '0.01', 'markPrice': '100000', 'liquidationPrice': '80000',
      'notional': '1000', 'marginAsset': 'USDT'}, None),  # a delivery contract is not guessed
])
def test_binance_usdm_perps_map_to_their_base_asset(raw, underlying):
    row = futures._position(raw, 'binance', {'account_id': 'acct_' + '0' * 32, 'account_last4': None})
    perps = {'venues': {'binance': {'positions': [row], 'source_status': 'ok', 'reason_codes': []}}}
    view = engine.underlying_exposure([], None, perps, {'USDT': {'rate_to_krw': '1400', 'source': 'upbit_public_ticker'}})
    if underlying is None:
        assert view['by_underlying'] == [] and view['unmapped'][0]['reason_codes'] == ['NOT_IN_PRODUCT_MAP']
    else:
        assert view['by_underlying'][0]['underlying'] == underlying
        assert view['by_underlying'][0]['exposure_krw'] == 1400000 and view['by_underlying'][0]['exposure_pct'] is None


def test_a_failed_venue_marks_the_perp_view_partial():
    perps = {'venues': {'hyperliquid': {'positions': [], 'source_status': 'failed',
                                        'reason_codes': ['FUTURES_INVALID_RESPONSE']}}}
    view = engine.underlying_exposure([], None, perps, {})
    assert view['perps']['status'] == 'partial'
    assert view['perps']['reason_codes'] == ['FUTURES_INVALID_RESPONSE', 'PERPS_SOURCE_INCOMPLETE']


# ── tool wiring: get_portfolio_risk reads perps alongside the brokers ──


class _StubCollector:
    def collect_positions(self):
        health = {name: {"account": name, "status": "ok", "last_error": None} for name in ("kis:domestic", "toss:primary")}
        return Collection(holdings=list(HOLDINGS), health=health, fetched_at="2026-10-21T09:10:05+00:00")

    def build_fx(self, currencies):
        return _fx()


def _server(monkeypatch, accounts, quotes):
    import server

    monkeypatch.setattr(server, "_get_collector", lambda: _StubCollector())
    monkeypatch.setattr(server, "_get_toss", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no quotes")))
    monkeypatch.setattr(server, "_crypto_futures_accounts", accounts)
    monkeypatch.setattr(crypto_nav_capture, "StablecoinQuotes", quotes)
    return server


@pytest.mark.parametrize("detail_level", ["summary", "full"])
def test_risk_tool_includes_perps_in_exposure_only(monkeypatch, detail_level):
    server = _server(monkeypatch, futures.FuturesAccounts(None, _hyperliquid), _quotes())
    envelope = server.get_portfolio_risk(detail_level=detail_level)
    assert envelope["ok"]
    data = envelope["data"]
    view = data["underlying_exposure"]
    assert _by_underlying(view)["US:ACME"]["exposure_krw"] == 1117000 and view["perps"]["status"] == "complete"
    assert data["total_market_value_krw"] == HOLDINGS_VALUE_KRW  # perps stay out of the valued total
    assert json.dumps(envelope, ensure_ascii=False).count(engine.UNDERLYING_EXPOSURE_METHOD["perps"]) == 1


def test_risk_tool_without_open_perps_skips_the_stablecoin_read(monkeypatch):
    def no_quotes(**_):
        raise AssertionError("no stablecoin read without an open perp")

    server = _server(monkeypatch, futures.FuturesAccounts(None, None), no_quotes)
    view = server.get_portfolio_risk()["data"]["underlying_exposure"]
    assert view["perps"]["status"] == "complete" and view["perps"]["leg_count"] == 0
    assert view["perps"]["reason_codes"] == ["BINANCE_FUTURES_NOT_CONFIGURED", "HYPERLIQUID_NOT_CONFIGURED"]
    assert _by_underlying(view)["US:ACME"]["exposure_krw"] == 700000


def test_holdings_state_perps_not_read_and_the_summary_reads_them_like_risk(monkeypatch, tmp_path):
    from portfolio_store import NavStore

    server = _server(monkeypatch, futures.FuturesAccounts(None, _hyperliquid), _quotes())
    monkeypatch.setattr(server, "_get_nav_store", lambda: NavStore(tmp_path / "nav.db"))
    holdings = server.get_consolidated_holdings(detail_level="summary")
    assert holdings["ok"]
    view = holdings["data"]["underlying_exposure"]
    assert view["perps"]["status"] == "not_read" and _by_underlying(view)["US:ACME"]["exposure_krw"] == 700000
    # Round 3: the summary shares the risk tool's perps read (and its positions cache).
    summary, risk = (server.get_portfolio_summary(include_realized=False, snapshot=False),
                     server.get_portfolio_risk(detail_level="full"))
    assert summary["ok"] and risk["ok"]
    view = summary["data"]["underlying_exposure"]
    assert view["perps"]["status"] == "complete" and view["by_underlying"] == risk["data"]["underlying_exposure"]["by_underlying"]
    assert summary["data"]["total_assets_krw"] == HOLDINGS_VALUE_KRW  # perps stay out of NAV


def test_positions_risk_and_summary_share_one_hyperliquid_read(monkeypatch, tmp_path):
    from portfolio_store import NavStore

    sent = []
    server = _server(monkeypatch, futures.FuturesAccounts(None, lambda: _hyperliquid(sent)), _quotes())
    monkeypatch.setattr(server, "_get_nav_store", lambda: NavStore(tmp_path / "nav.db"))
    positions = server.get_crypto_futures_positions()
    risk = server.get_portfolio_risk(detail_level="full")
    summary = server.get_portfolio_summary(include_realized=False, snapshot=False)
    assert positions["ok"] and risk["ok"] and summary["ok"]
    assert sent.count("perpDexs") == 1 and sent.count("clearinghouseState") == 2  # main + xyz, read once
    assert positions["data"]["venues"]["hyperliquid"]["cache"]["hit"] is False
    for envelope in (risk, summary):
        cache = envelope["data"]["underlying_exposure"]["perps"]["venues"]["hyperliquid"]["cache"]
        assert cache["hit"] is True and cache["ttl_seconds"] == futures.POSITIONS_CACHE_SECONDS
    assert risk["data"]["underlying_exposure"]["by_underlying"] == summary["data"]["underlying_exposure"]["by_underlying"]


def test_the_summary_does_not_wait_out_a_crypto_history_read(monkeypatch, tmp_path):
    import threading
    import time

    from portfolio_store import NavStore

    entered, release = threading.Event(), threading.Event()
    accounts = futures.FuturesAccounts(None, lambda: _hyperliquid(hold=(entered, release)))
    server = _server(monkeypatch, accounts, _quotes())
    monkeypatch.setattr(server, "_get_nav_store", lambda: NavStore(tmp_path / "nav.db"))
    history = threading.Thread(target=accounts.query, args=("funding", "hyperliquid"),
                               kwargs={"start_date": "2026-10-15", "end_date": "2026-10-21"})
    history.start()
    assert entered.wait(5)
    try:
        started = time.monotonic()
        summary = server.get_portfolio_summary(include_realized=False, snapshot=False)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        history.join(10)
    assert summary["ok"] and elapsed < 3
    perps = summary["data"]["underlying_exposure"]["perps"]
    assert perps["status"] == "partial" and perps["venues"]["hyperliquid"]["reason_codes"] == ["PERPS_READ_BUSY"]
    assert {"PERPS_READ_BUSY", "PERPS_SOURCE_INCOMPLETE"} <= set(perps["reason_codes"])
    assert summary["data"]["total_assets_krw"] == HOLDINGS_VALUE_KRW  # perps never reach NAV


def test_a_failing_perp_read_leaves_the_risk_tool_ok_and_the_view_partial(monkeypatch):
    class Broken:
        def query(self, *args):
            raise RuntimeError("boom")

    server = _server(monkeypatch, Broken(), _quotes())
    envelope = server.get_portfolio_risk()
    assert envelope["ok"]
    perps = envelope["data"]["underlying_exposure"]["perps"]
    assert perps["status"] == "partial" and perps["reason_codes"] == ["PERPS_READ_FAILED", "PERPS_SOURCE_INCOMPLETE"]
