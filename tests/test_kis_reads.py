"""KIS requests per chat call and per burst (EGW00215 on 2.15.0).

Synthetic numbers only. The real KISClient runs over httpx.MockTransport that counts every
request; the collector and the server's own call_tool are the code under test.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import kis_api  # noqa: E402
import portfolio_sources  # noqa: E402
from kinvest_common import NotConfiguredError  # noqa: E402
from kis_accounts import KISAccount, KISAccountRegistry  # noqa: E402
from kis_api import KISClient  # noqa: E402
from portfolio_sources import (  # noqa: E402
    PortfolioCollector,
    Providers,
    shared_position_reads,
)

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kis_published"
US_ROW = {"ovrs_pdno": "SYNU", "ovrs_item_name": "SYN", "ovrs_cblc_qty": "10", "ord_psbl_qty": "10",
          "ovrs_stck_evlu_amt": "200", "now_pric2": "20", "pchs_avg_pric": "15", "frcr_pchs_amt1": "150",
          "frcr_evlu_pfls_amt": "50", "evlu_pfls_rt": "33.3", "tr_crcy_cd": "USD", "ovrs_excg_cd": "NASD"}


class Broker:
    """Scripted KIS: a held USD position without a USD cash row (live shape), every request logged."""

    def __init__(self, latency: float = 0.0):
        self.log: list[tuple[str, str]] = []
        self.latency = latency
        self.jpy_deposit = None
        self.fail_krw = False
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        tr_id, params = request.headers.get("tr_id", ""), dict(request.url.params)
        with self.lock:
            self.log.append((tr_id, params.get("WCRC_FRCR_DVSN_CD") or params.get("OVRS_EXCG_CD") or ""))
        if self.latency:
            time.sleep(self.latency)
        if tr_id == "TTTC8434R":
            body = json.loads((FIXTURES / "TTTC8434R.json").read_text(encoding="utf-8"))
        elif tr_id == "TTTS3012R":
            rows = [dict(US_ROW)] if params.get("OVRS_EXCG_CD") == "NASD" else []
            body = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output1": rows, "output2": {}}
        elif tr_id == "CTRP6504R":
            body = json.loads((FIXTURES / "CTRP6504R.json").read_text(encoding="utf-8"))
            body.pop("_provenance", None)
            if params.get("WCRC_FRCR_DVSN_CD") == "02":
                body["output2"] = [row for row in body["output2"] if row.get("crcy_cd") != "USD"]
                if self.jpy_deposit is not None:
                    body["output2"][0]["frcr_dncl_amt_2"] = self.jpy_deposit
            elif self.fail_krw:
                return httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00500", "msg1": "down"})
        else:
            body = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output": [], "output1": [], "output2": []}
        return httpx.Response(200, json=body, headers={"tr_cont": "D"})

    def count(self, tr_id: str, arg: str | None = None) -> int:
        return sum(1 for tr, a in self.log if tr == tr_id and (arg is None or a == arg))


@pytest.fixture
def broker(monkeypatch, tmp_path):
    fake = Broker()
    init = KISClient.__init__

    def patched(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self._http = httpx.Client(transport=httpx.MockTransport(fake), timeout=5)

    monkeypatch.setenv("KIS_APP_KEY", "synthetic-key")
    monkeypatch.setenv("KIS_APP_SECRET", "synthetic-secret")
    monkeypatch.setenv("KIS_TOKEN_CACHE", str(tmp_path / "token.json"))
    monkeypatch.setattr(KISClient, "__init__", patched)
    monkeypatch.setattr(KISClient, "_ensure_token", lambda self: "synthetic-token")
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    fake.registry = KISAccountRegistry([KISAccount("primary", "00000001", "01"),
                                        KISAccount("pension", "00000001", "22")], cache_dir=tmp_path)
    return fake


def _collector(registry):
    def no_toss():
        raise NotConfiguredError("toss", "not configured in this test")
    return PortfolioCollector(Providers(toss_labels=no_toss, toss_client=lambda label: None,
                                        kis_accounts=registry.accounts, kis_client=registry.client_for,
                                        error_formatter=lambda exc: {"code": "ERR", "message": str(exc)}))


# ── KRW cash check: reused while the balance read it was made for is unchanged ──


def test_cash_check_read_once_for_repeated_balance_reads(broker):
    client = broker.registry.client_for("primary")
    first = client.get_overseas_balance()
    second = client.get_overseas_balance()
    assert broker.count("CTRP6504R", "02") == 2 and broker.count("CTRP6504R", "01") == 1
    assert first["cash_check_krw"] == second["cash_check_krw"] and first["cash_check_krw"]["basis"] == "01"
    assert "error" not in first["cash_check_krw"]


def test_cash_check_read_again_when_its_inputs_change_or_expire(broker, monkeypatch):
    client = broker.registry.client_for("primary")
    client.get_overseas_balance()
    broker.jpy_deposit = "123.00"  # the listed cash rows changed: the proof needs a fresh KRW total
    client.get_overseas_balance()
    assert broker.count("CTRP6504R", "01") == 2
    monkeypatch.setattr(kis_api, "CASH_CHECK_REUSE_SECONDS", 0.0)
    client.get_overseas_balance()
    assert broker.count("CTRP6504R", "01") == 3


def test_failed_cash_check_is_not_reused(broker):
    client = broker.registry.client_for("primary")
    broker.fail_krw = True
    assert client.get_overseas_balance()["cash_check_krw"] == {"basis": "01", "error": "KIS source unavailable"}
    broker.fail_krw = False
    assert "error" not in client.get_overseas_balance()["cash_check_krw"]
    assert broker.count("CTRP6504R", "01") == 2


def test_cash_check_is_per_account(broker):
    a = broker.registry.client_for("primary")
    other = KISAccountRegistry([KISAccount("second", "00000002", "01")], cache_dir=broker.registry._cache_dir)
    a.get_overseas_balance()
    other.client_for("second").get_overseas_balance()
    assert broker.count("CTRP6504R", "01") == 2


# ── collector: parallel tool calls share one positions read ──


def _parallel(fn, n=2):
    results, threads = [], []
    for _ in range(n):
        threads.append(threading.Thread(target=lambda: results.append(fn())))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_parallel_positions_reads_share_one_fan_out(broker):
    broker.latency = 0.05
    collector = _collector(broker.registry)

    def call():
        with shared_position_reads():
            return collector.collect_positions()

    first, second = _parallel(call)
    assert len(broker.log) == 9  # 2 domestic + 5 overseas markets + present balance + one cash check
    assert first is not second and [h.symbol for h in first.holdings] == [h.symbol for h in second.holdings]
    second.holdings.clear()  # each caller owns its copy
    with shared_position_reads():
        assert collector.collect_positions().holdings  # within SHARED_POSITIONS_SECONDS: no new read
    assert len(broker.log) == 9


def test_without_sharing_or_after_the_window_each_call_reads(broker, monkeypatch):
    collector = _collector(broker.registry)
    collector.collect_positions()
    collector.collect_positions()  # scripts and the NAV snapshot: their own read every time
    assert broker.count("TTTC8434R") == 4
    monkeypatch.setattr(portfolio_sources, "SHARED_POSITIONS_SECONDS", 0.0)
    with shared_position_reads():
        collector.collect_positions()
        time.sleep(0.01)
        collector.collect_positions()
    assert broker.count("TTTC8434R") == 8


def test_a_failed_shared_read_is_not_handed_out(broker, monkeypatch):
    collector = _collector(broker.registry)
    calls = Counter()
    original = collector._collect_positions

    def flaky(accounts=None):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.05)
            raise RuntimeError("synthetic collector failure")
        return original(accounts)

    monkeypatch.setattr(collector, "_collect_positions", flaky)
    errors, results = [], []

    def call():
        try:
            with shared_position_reads():
                results.append(collector.collect_positions())
        except RuntimeError as exc:
            errors.append(exc)

    _parallel(call)
    assert len(errors) == 1 and len(results) == 1 and calls["n"] == 2


def test_kis_balance_fan_outs_on_one_account_ledger_never_overlap(broker, monkeypatch):
    collector = _collector(broker.registry)
    active, peak = Counter(), Counter()
    lock = threading.Lock()
    real = KISClient.get_overseas_balance

    def watched(self, markets=None):
        with lock:
            active["n"] += 1
            peak["n"] = max(peak["n"], active["n"])
        try:
            time.sleep(0.05)
            return real(self, markets)
        finally:
            with lock:
                active["n"] -= 1

    monkeypatch.setattr(KISClient, "get_overseas_balance", watched)
    # different account filters: no shared read, so only the ledger gate keeps them apart
    _parallel(lambda: collector.collect_positions(["kis:overseas"]))
    assert peak["n"] == 1 and broker.count("TTTS3012R") == 10


# ── the server: ChatGPT I01 burst (get_holdings x2 with include_cash, in parallel) ──


def test_i01_burst_through_call_tool_reads_kis_once(broker, monkeypatch, tmp_path):
    import server

    monkeypatch.setenv("KINVEST_PORTFOLIO_DB", str(tmp_path / "portfolio.db"))
    monkeypatch.setattr(server, "_nav_store", None)
    monkeypatch.setattr(server, "_collector", None)
    monkeypatch.setattr(server, "_kis_accounts", broker.registry)

    def no_toss():
        raise NotConfiguredError("toss", "not configured in this test")

    monkeypatch.setattr(server, "_get_toss_registry", no_toss)
    broker.latency = 0.05

    async def burst():
        return await asyncio.gather(*(server.mcp.call_tool("get_holdings", {"include_cash": True})
                                      for _ in range(2)))

    results = asyncio.run(burst())
    assert len(results) == 2
    # 2.14.1 made 8 per call (16 for the burst), 2.15.0 9 (18); now one shared collection.
    assert len(broker.log) == 9, Counter(tr for tr, _ in broker.log)
    monkeypatch.setattr(portfolio_sources, "SHARED_POSITIONS_SECONDS", 0.0)
    asyncio.run(burst())  # past the share window, inside the cash-check reuse: 8 per call, as 2.14.1
    assert len(broker.log) == 9 + 8, Counter(tr for tr, _ in broker.log)
