"""Synthetic recovery and reconciliation; no broker or credential access."""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from importlib import import_module
from threading import Lock
from types import SimpleNamespace

import pytest
from test_kis_portfolio import FakeResponse, HttpKIS
from test_portfolio_sources import FakeToss, build

import portfolio_engine as engine
import toss_api
from portfolio_model import FxRates, Holding, Transaction, TxType, toss_transactions
from portfolio_sources import Collection
from toss_api import TossAPIError, TossClient


def order(i, day="2026-01-01", status="FILLED", qty=1):
    return {"orderId": str(i), "symbol": "SYN", "currency": "KRW", "side": "BUY",
            "status": status, "orderedAt": day + "T09:00:00+09:00",
            "execution": {"filledAt": day + "T09:01:00+09:00", "filledQuantity": qty,
                          "averageFilledPrice": 10, "filledAmount": qty * 10,
                          "commission": 0, "tax": 0}}


class Pages(TossClient):
    def __init__(self, size, fail=False):
        self.size, self.fail = size, fail

    def get_orders(self, cursor=None, **kwargs):
        if cursor and self.fail:
            raise TossAPIError(429, {}, retry_after=120)
        start = int(cursor or 0)
        end = min(start + self.size, 100)
        return {"result": {"orders": [order(i) for i in range(start, end)],
                           "hasNext": end < 100, "nextCursor": str(end - 1)}}


@pytest.mark.parametrize("size", [50, 100])
def test_overlap_pages_deduplicate_and_rerun_totals(size):
    client = Pages(size)
    first = client.iter_closed_orders(page_size=size)
    second = client.iter_closed_orders(page_size=size)
    assert len(first) == len(second) == 100
    assert sum(t.quantity for t in toss_transactions(first, "toss:primary")) == 100


class Windows(FakeToss):
    def __init__(self, fail_start=None):
        self.fail_start, self.calls = fail_start, []

    def iter_closed_orders(self, date_from=None, date_to=None, **kwargs):
        self.calls.append((date_from, date_to))
        if date_from == self.fail_start:
            raise TossAPIError(429, {}, retry_after=120)
        # A broad broker request silently misses older buys. Small windows recover them.
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        if (end - start).days >= 90:
            return []
        rows = [order(i, (date(2025, 1, 1) + timedelta(days=i * 10)).isoformat())
                for i in range(22)]
        return [r for r in rows if date_from <= r["orderedAt"][:10] <= date_to]


def test_22_historical_buys_recovered_with_cost_and_provenance():
    client = Windows()
    collector = build(toss_clients={"primary": client}, kis_accounts=[])
    before = toss_transactions(client.iter_closed_orders("2025-01-01", "2025-12-31"), "toss:primary")
    collection = collector.collect_transactions("2025-01-01", "2025-12-31")
    assert len(before) == 0
    assert len(collection.transactions) == 22
    basis = engine.compute_realized_pnl(collection.transactions, FxRates())
    assert basis["open_positions_from_ledger"]["SYN:KRW"]["quantity"] == 22
    assert basis["open_positions_from_ledger"]["SYN:KRW"]["cost_basis"] == 220
    assert all(t.execution_id and t.source for t in collection.transactions)
    assert collection.fetch_complete is True
    assert collection.historical_coverage == "unknown"
    assert collection.observed_window == {"start": "2025-01-01", "end": "2025-07-30"}


@pytest.mark.parametrize("limit", [150, 200, 500])
def test_failed_chunk_resume_preserves_other_accounts_and_only_retries_failed(limit, monkeypatch):
    import portfolio_sources
    now = [1000.0]
    monkeypatch.setattr(portfolio_sources.time, "monotonic", lambda: now[0])
    # Legacy wide row requests can fail; recovery has an independent small-page
    # path. No cause is inferred from the requested limit.
    with pytest.raises(TossAPIError):
        Pages(50, fail=True).get_trade_history(limit=limit)
    bad, good = Windows("2025-12-27"), Windows()
    collector = build(toss_clients={"primary": bad, "secondary": good}, kis_accounts=[])
    first = collector.collect_transactions("2025-01-01", "2025-12-31", page_size=min(limit, 100))
    assert first.resume_cursor and not first.fetch_complete
    assert any(t.account == "toss:secondary" for t in first.transactions)
    assert first.failed_chunks[0]["retry_after_seconds"] == 120
    bad_before, good_before = len(bad.calls), len(good.calls)
    bad.fail_start = None
    now[0] += 120
    resumed = collector.collect_transactions("2025-01-01", "2025-12-31",
                                             page_size=min(limit, 100), resume_cursor=first.resume_cursor)
    assert len(bad.calls) == bad_before + 1
    assert len(good.calls) == good_before
    assert len(resumed.transactions) == 44
    assert resumed.fetch_complete and resumed.resume_cursor is None
    assert resumed.historical_coverage == "unknown"


def test_page_failure_preserves_successful_rows():
    result = Pages(50, fail=True).iter_closed_orders()
    assert len(result) == 50
    assert result.truncated and result.fetch_error.status == 429


class HttpToss(TossClient):
    def __init__(self, responses):
        self.client_id, self.sent = "synthetic-connection", 0
        self._responses, self._http = list(responses), self

    def _ensure_token(self):
        return "synthetic"

    def get(self, *args, **kwargs):
        self.sent += 1
        return self._responses.pop(0)


def test_toss_429_retry_after_and_two_retry_budget(monkeypatch):
    sleeps = []
    monkeypatch.setattr(toss_api.time, "sleep", sleeps.append)
    limited = FakeResponse({}, status_code=429)
    limited.headers["Retry-After"] = "7"
    client = HttpToss([limited, limited, FakeResponse({"result": []})])
    assert client._get("/synthetic") == {"result": []}
    assert client.sent == 3 and sleeps == [7, 7]
    client = HttpToss([limited] * 4)
    with pytest.raises(TossAPIError):
        client._get("/synthetic")
    assert client.sent == 3
    limited.headers["Retry-After"] = "120"
    client = HttpToss([limited])
    with pytest.raises(TossAPIError) as exc:
        client._get("/synthetic")
    assert client.sent == 1 and exc.value.retry_after_seconds == 120


def test_kis_rate_limit_does_not_replay_exchange_fallback(monkeypatch):
    monkeypatch.setattr("kis_api.time.sleep", lambda _: None)
    limited = FakeResponse({"rt_cd": "1", "msg_cd": "EGW00201"}, status_code=429)
    client = HttpKIS([limited] * 10)
    with pytest.raises(Exception):
        client.get_domestic_trade_history("20260101", "20260103")
    assert len(client.sent) == 3


@pytest.mark.parametrize("provider", ["toss", "kis"])
def test_requests_serialized_per_connection(provider):
    guard = Lock()
    active = peak = 0
    def response(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(.01)
        with guard:
            active -= 1
        return FakeResponse({"rt_cd": "0", "result": []})
    clients = [HttpToss([]), HttpToss([])] if provider == "toss" else [HttpKIS([]), HttpKIS([])]
    for c in clients:
        c._http = SimpleNamespace(get=response)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda c: c._get("/synthetic", {}), clients))
    assert peak == 1


def reconcile(txs, holdings, **kwargs):
    return import_module("portfolio_engine").reconcile_quantities(txs, holdings, **kwargs)


def test_ram_471_break_invalidates_realized_and_performance():
    txs = toss_transactions([order("ram", qty=471)], "toss:primary")
    result = reconcile(txs, [], known_accounts=["toss:primary"])
    assert result["status"] == "unreconciled"
    row = result["reconciliation_breaks"][0]
    assert row["status"] == "unexplained" and row["quantity_difference"] == -471
    assert row["opening_qty"] is None
    quality = engine.ledger_input_quality(Collection(transactions=txs,
        health={"toss:primary:orders": {"status": "ok"}}, reconciliation=result))
    pnl = engine.compute_realized_pnl(txs, FxRates(), input_quality=quality)
    assert pnl["totals"]["status"] == "partial"
    assert "RECONCILIATION_BREAK" in pnl["totals"]["reason_codes"]
    performance = engine.compute_performance(txs, [], 0, FxRates(), "2026-01-01", "2026-10-04",
                                             realized=pnl, input_quality=quality)
    assert performance["pnl"]["realized_krw"] is None
    assert "RECONCILIATION_BREAK" in performance["metrics"]["simple_return_pct"]["reason_codes"]


def test_matching_quantity_never_proves_historical_coverage():
    txs = toss_transactions([order("ram", qty=471)], "toss:primary")
    result = reconcile(txs, [Holding("toss", "toss:primary", "SYN", "KRW", 471)],
                       known_accounts=["toss:primary"])
    assert not result["reconciliation_breaks"]
    assert result["historical_coverage"] == "unknown"
    assert result["positions"][0]["opening_qty"] is None


def test_verified_opening_and_explicit_adjustments_reconcile_without_fake_trades():
    txs = [Transaction("toss", "toss:primary", "sell", TxType.SELL, "2026-01-02", "KRW",
                       symbol="SYN", quantity=3, price=10, gross_amount=30)]
    positions = [Holding("toss", "toss:primary", "SYN", "KRW", 10)]
    result = reconcile(txs, positions, known_accounts=["toss:primary"], opening_positions=[{
        "account": "toss:primary", "symbol": "SYN", "currency": "KRW", "quantity": 10,
        "cost_basis": 100, "status": "verified", "source": "synthetic-opening"}],
        adjustments=[{"account": "toss:primary", "symbol": "SYN", "currency": "KRW",
                      "quantity_delta": 3, "kind": "transfer", "source": "synthetic-transfer"}])
    assert not result["reconciliation_breaks"]
    assert result["positions"][0]["opening_cost_basis"] == 100
    assert len(txs) == 1


def test_failed_holdings_are_not_assumed_zero():
    result = reconcile(toss_transactions([order("ram", qty=471)], "toss:primary"), [],
                       known_accounts=[])
    assert result["status"] == "unavailable" and not result["reconciliation_breaks"]


def test_six_canceled_replaced_positive_fills_kept():
    rows = [order(i, status="CANCELED" if i % 2 else "REPLACED") for i in range(6)]
    rows.append(order("unfilled", status="CANCELED", qty=0))
    assert len(toss_transactions(rows, "toss:primary")) == 6


def test_all_transactions_distinguishes_failed_from_empty(monkeypatch):
    import server
    monkeypatch.setattr(server, "_resolve_accounts", lambda _: None)
    collector = build(toss_clients={"primary": FakeToss(fail=TossAPIError(429, {}))}, kis_accounts=[])
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    failed = server.get_all_transactions("2026-01-01", "2026-01-02")
    assert failed["result_status"] == "unavailable"
    collector = build(toss_clients={"primary": FakeToss(orders=[])}, kis_accounts=[])
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    empty = server.get_all_transactions("2026-01-01", "2026-01-02")
    assert empty["result_status"] == "empty"
    assert empty["data"]["historical_coverage"] == "unknown"


def test_cursor_rejects_changed_filters_and_expired_sessions(monkeypatch):
    import portfolio_sources
    collector = build(toss_clients={"primary": Windows("2025-04-01")}, kis_accounts=[])
    initial = collector.collect_transactions("2025-01-01", "2025-12-31")
    with pytest.raises(ValueError, match="mismatched"):
        collector.collect_transactions("2025-01-01", "2025-12-31", symbol="SYN",
                                       resume_cursor=initial.resume_cursor)
    now = portfolio_sources.time.monotonic()
    monkeypatch.setattr(portfolio_sources.time, "monotonic", lambda: now + 1801)
    with pytest.raises(ValueError, match="expired"):
        collector.collect_transactions("2025-01-01", "2025-12-31", resume_cursor=initial.resume_cursor)


def test_overlap_chunks_deduplicate_stable_ids_but_keep_unidentified_rows():
    known = order("known")
    unknown = order("unknown")
    unknown.pop("orderId")
    collector = build(toss_clients={"primary": FakeToss(orders=[known, unknown])}, kis_accounts=[])
    result = collector.collect_transactions("2026-01-01", "2026-05-01")
    assert len(result.transactions) == 3
    assert sum(t.execution_id is not None for t in result.transactions) == 1
    assert len(collector.collect_transactions("2026-01-01", "2026-05-01").transactions) == 3


def test_retry_budget_and_exponential_jitter(monkeypatch):
    import portfolio_sources
    monkeypatch.setattr(portfolio_sources.random, "uniform", lambda *args: .125)
    assert portfolio_sources.retry_delay(0, None, 60) == .625
    assert portfolio_sources.retry_delay(1, None, 60) == 1.125
    assert portfolio_sources.retry_delay(1, 40, 20) is None
    sleeps = []
    monkeypatch.setattr(toss_api.time, "sleep", sleeps.append)
    limited = FakeResponse({}, status_code=429)
    limited.headers["Retry-After"] = "40"
    client = HttpToss([limited] * 4)
    with pytest.raises(TossAPIError):
        client._get("/synthetic")
    assert sleeps == [40] and client.sent == 2


def test_retry_after_defers_same_connection_chunks_and_early_resume(monkeypatch):
    import portfolio_sources
    now = [1000.0]
    monkeypatch.setattr(portfolio_sources.time, "monotonic", lambda: now[0])
    bad, good = Windows("2025-04-01"), Windows()
    collector = build(toss_clients={"primary": bad, "secondary": good}, kis_accounts=[])
    result = collector.collect_transactions("2025-01-01", "2025-12-31")
    assert len(bad.calls) == 2 and len(good.calls) == 5
    assert len(result.failed_chunks) == 4
    bad.fail_start = None
    early = collector.collect_transactions("2025-01-01", "2025-12-31", resume_cursor=result.resume_cursor)
    assert len(bad.calls) == 2 and early.resume_cursor == result.resume_cursor
    now[0] += 120
    recovered = collector.collect_transactions("2025-01-01", "2025-12-31", resume_cursor=result.resume_cursor)
    assert len(bad.calls) == 6 and len(good.calls) == 5
    assert recovered.fetch_complete and len(recovered.transactions) == 44


def test_reconciliation_as_of_and_symbol_scope_are_conservative():
    txs = toss_transactions([order("ram", qty=471)], "toss:primary")
    ledger = Collection(transactions=txs, requested_window={"end": "2026-01-01"})
    positions = Collection(fetched_at="2026-10-04T00:00:00Z",
                           health={"toss:primary": {"status": "ok"}})
    result = engine.reconcile_collections(ledger, positions)
    assert result["status"] == "unavailable" and not result["reconciliation_breaks"]
    assert result["reason_codes"] == ["HOLDINGS_AS_OF_MISMATCH"]
    ledger.requested_window["end"] = "2026-10-04"
    positions.holdings = [Holding("toss", "toss:primary", "SYN", "KRW", 471),
                          Holding("toss", "toss:primary", "OTHER", "KRW", 100)]
    positions.health["toss:primary:usd"] = {"status": "error"}  # cash ≠ holdings
    assert not engine.reconcile_collections(ledger, positions, "SYN")["reconciliation_breaks"]
    assert engine.reconcile_collections(ledger, positions)["reconciliation_breaks"][0]["symbol"] == "OTHER"


def test_opening_and_corporate_actions_preserve_equation_and_uncertainty():
    opening = [{"account": "toss:primary", "symbol": "SYN", "currency": "KRW",
                "quantity": 10, "status": "verified", "cost_basis": 100, "source": "synthetic"}]
    buy = Transaction("toss", "toss:primary", "buy", TxType.BUY, "2026-01-01", "KRW",
                      symbol="SYN", quantity=2)
    split = replace(buy, tx_type=TxType.SPLIT, tx_id="split", trade_date="2026-01-02", quantity=24)
    holdings = [Holding("toss", "toss:primary", "SYN", "KRW", 24)]
    result = reconcile([buy, split], holdings, opening_positions=opening)
    assert result["positions"][0]["corporate_actions"] == 12
    assert result["positions"][0]["expected_ending_qty"] == 24
    assert not result["reconciliation_breaks"]
    unknown = reconcile([buy, split], holdings)
    assert "UNRESOLVED_QUANTITY_EVENT" in unknown["reconciliation_breaks"][0]["reason_codes"]
    # Unverified opening evidence cannot silently establish a position.
    unverified = reconcile([], holdings, opening_positions=[dict(opening[0], status="unknown")])
    assert unverified["positions"][0]["opening_qty"] is None


def test_unmodeled_quantity_events_are_unknown_not_zero():
    transfer = Transaction("toss", "toss:primary", "transfer", TxType.TRANSFER,
                           "2026-01-01", "KRW", symbol="SYN", quantity=3)
    split = replace(transfer, tx_type=TxType.SPLIT, tx_id="split", quantity=20)
    result = reconcile([transfer, split], [Holding("toss", "toss:primary", "SYN", "KRW", 20)])
    row = result["positions"][0]
    assert row["transfers"] is None and row["corporate_actions"] is None
    assert row["expected_ending_qty"] is None
    assert row["status"] == "unexplained"


def test_configuration_recovery_discovers_bounded_windows_without_refetching_other_provider():
    from test_portfolio_sources import FakeKIS, FakeKISAccount
    collector = build(toss_clients={"primary": Windows()}, kis=FakeKIS(),
                      kis_accounts=RuntimeError("synthetic unavailable"))
    first = collector.collect_transactions("2025-01-01", "2025-12-31")
    toss_calls = len(collector._providers.toss_client("primary").calls)
    collector._providers.kis_accounts = lambda: [FakeKISAccount(supports_overseas=False)]
    kis = FakeKIS()
    collector._providers.kis_client = lambda _: kis
    resumed = collector.collect_transactions("2025-01-01", "2025-12-31", resume_cursor=first.resume_cursor)
    assert resumed.fetch_complete and len(resumed.transactions) == 22
    assert len(collector._providers.toss_client("primary").calls) == toss_calls
    assert len(kis.calls) == 5


def test_ram_break_reaches_all_three_tools_and_performance(monkeypatch):
    import server
    day = date.today().isoformat()
    txs = toss_transactions([order("ram", day, qty=471)], "toss:primary")
    ledger = Collection(transactions=txs, requested_window={"start": "2000-01-01", "end": day},
                        health={"toss:primary:orders": {"status": "ok"}}, fetch_complete=True)
    positions = Collection(health={"toss:primary": {"status": "ok"}}, fetched_at=day + "T00:00:00Z")
    collector = SimpleNamespace(collect_transactions=lambda *a, **kw: ledger,
                                collect_positions=lambda *a: positions, build_fx=lambda *a: FxRates())
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: SimpleNamespace(range=lambda *a: []))
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    monkeypatch.setattr(server, "_resolve_accounts", lambda _: None)
    for response in [server.get_all_transactions("2000-01-01", day), server.get_realized_pnl(),
                     server.get_consolidated_holdings(reconcile=True)]:
        assert response["ok"]
        assert response["data"]["reconciliation"]["unexplained_quantity_breaks"] == 1
    realized = server.get_realized_pnl()
    assert realized["result_status"] == "partial"
    for subset in ["complete_basis", "incomplete_basis"]:
        assert "RECONCILIATION_BREAK" in realized["data"]["totals"][subset]["reason_codes"]
    performance = server.get_portfolio_performance(external_cashflows_verified_zero=True)
    assert performance["ok"]
    assert performance["data"]["pnl"]["realized_krw"] is None
    assert "RECONCILIATION_BREAK" in performance["data"]["metrics"]["realized_krw"]["reason_codes"]
    assert "FAILED_SOURCES" not in performance["data"]["metrics"]["realized_krw"]["reason_codes"]


def test_raw_history_default_payloads_are_unchanged_and_recovery_is_opt_in(monkeypatch):
    import ast
    import hashlib
    import json
    import subprocess
    from pathlib import Path

    import server
    def no_git(*args, **kwargs):
        raise AssertionError("Release validation has no Git repository")
    monkeypatch.setattr(subprocess, "check_output", no_git)
    # Compile just the two baseline tool bodies; no baseline module import,
    # configuration or authentication. Compare serialized envelopes byte for byte.
    baseline = (Path(__file__).parent / "fixtures/legacy_raw_history_tools.py.txt").read_text()
    assert hashlib.sha256(baseline.encode()).hexdigest() == "c4ac96a4a1b0286a89717cea9fa744faba798ed9f29a060149f25b8bb27d7fe6"
    functions = [n for n in ast.parse(baseline).body if isinstance(n, ast.FunctionDef)
                 and n.name in {"get_toss_trade_history", "get_kis_trade_history"}]
    for function in functions:
        function.decorator_list = []
    namespace = dict(vars(server))
    client = SimpleNamespace(get_trade_history=lambda **kw: [order("raw")], supports_overseas=False,
                             get_domestic_trade_history=lambda *a: {"trades": [], "truncated": False},
                             get_overseas_trade_history=lambda *a: pytest.fail("unsupported"))
    registry = SimpleNamespace(client_for=lambda _: client, default_label=lambda: "primary",
                               account_labels=lambda: ["primary"])
    monkeypatch.setattr(server, "_get_toss_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_kis_registry", lambda: registry)
    namespace.update(_get_toss_registry=lambda: registry, _get_kis_registry=lambda: registry)
    exec(compile(ast.Module(body=functions, type_ignores=[]), "baseline-tool-bodies", "exec"), namespace)
    for name, args in [("get_toss_trade_history", ()), ("get_kis_trade_history", ("20260101", "20260102")),
                       ("get_toss_trade_history", (50, "all")), ("get_kis_trade_history", ("20260101", "20260102", "all"))]:
        expected = namespace[name](*args)
        assert json.dumps(getattr(server, name)(*args), sort_keys=True) == json.dumps(expected, sort_keys=True)
    calls = []
    monkeypatch.setattr(server, "get_all_transactions", lambda *a, **kw: calls.append((a, kw)) or {"recovery": True})
    assert server.get_toss_trade_history(recovery=True) == {"recovery": True}
    assert server.get_kis_trade_history("20260101", "20260102", recovery=True) == {"recovery": True}
    assert calls[0][0][2] == "toss:primary"
    assert calls[1][0][2] == "kis:primary"


def test_summary_and_realized_tool_agree_on_quantity_break_without_extra_fetches(monkeypatch):
    import server
    day = date.today().isoformat()
    ram = toss_transactions([order("ram", day, qty=471)], "toss:primary")
    buy = Transaction("toss", "toss:primary", "other-buy", TxType.BUY, day, "KRW",
                      symbol="OTHER", quantity=1, price=100, gross_amount=100,
                      fee_status="verified_zero", tax_status="verified_zero")
    sell = replace(buy, tx_id="other-sell", tx_type=TxType.SELL, price=120, gross_amount=120)
    calls = []
    def transactions(start, end, *args, **kwargs):
        calls.append("transactions")
        return Collection(transactions=[*ram, buy, sell], health={"toss:primary:orders": {"status": "ok"}},
                          fetch_complete=True, requested_window={"start": start, "end": end})
    def positions(*args, **kwargs):
        calls.append("positions")
        return Collection(health={"toss:primary": {"status": "ok"}}, fetched_at=day + "T00:00:00Z")
    collector = SimpleNamespace(collect_transactions=transactions, collect_positions=positions,
                                build_fx=lambda *a: FxRates())
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_resolve_accounts", lambda _: None)
    summary = server.get_portfolio_summary(snapshot=False)
    assert calls == ["positions", "transactions"]
    realized = server.get_realized_pnl()
    assert summary["ok"] and realized["ok"]
    assert summary["data"]["realized_pnl_krw"] is None
    assert summary["data"]["realized_pnl_krw"] == realized["data"]["totals"]["realized_pnl_krw"]
    assert summary["data"]["realized_pnl_status"] == realized["data"]["totals"]["status"] == "partial"
    assert summary["data"]["realized_pnl_reason_codes"] == realized["data"]["totals"]["reason_codes"] == ["RECONCILIATION_BREAK"]
    for subset in ("complete_basis", "incomplete_basis"):
        assert summary["data"]["realized_pnl_" + subset] == realized["data"]["totals"][subset]
        assert "RECONCILIATION_BREAK" in summary["data"]["realized_pnl_" + subset]["reason_codes"]
    assert summary["result_status"] == realized["result_status"] == "partial"
    assert summary["data"]["total_assets_krw"] == 0
