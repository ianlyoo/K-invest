"""Reproducible NAV history: synthetic prices, calendars and legacy databases."""
import asyncio
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

import portfolio_engine as pe
from portfolio_model import FxRates
from portfolio_store import NavStore


def summary(day="2026-10-02", total=100):
    return {
        "total_assets_krw": total, "result_status": "complete",
        "valuation_at": f"{day}T16:00:00+09:00",
        "observed_at": f"{day}T16:01:00+09:00",
        "components": [{"symbol": "SYN", "currency": "KRW", "priced_at": f"{day}T15:30:00+09:00"}],
        "fx_times": {},
        "nav_bridge": {"settlement_adjusted_nav_krw": total - 5,
                       "reconciliation": {"status": "complete", "difference_reasons": []}},
    }


@pytest.fixture
def store(tmp_path):
    return NavStore(tmp_path / "nav.db")


def test_versions_remain_immutable_when_daily_representative_changes(store):
    first = store.upsert_snapshot(summary(), "2026-10-02")
    second = store.upsert_snapshot(summary(total=120), "2026-10-02")
    assert first["snapshot_id"] != second["snapshot_id"]
    assert (first["version"], second["version"]) == (1, 2)
    assert store.get_snapshot("2026-10-02")["snapshot_id"] == second["snapshot_id"]
    assert store.select_snapshots([first["snapshot_id"]]) == [first]
    assert store.count() == 1
    with sqlite3.connect(store.db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE nav_snapshot_versions SET payload_json = '{}' WHERE snapshot_id = ?", (first["snapshot_id"],))


def test_existing_schema_migrates_without_losing_rows_or_inventing_provenance(tmp_path):
    path = tmp_path / "legacy.db"
    # Copy of the deployed date-keyed schema; do not import the new schema here.
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE nav_snapshots (
            snapshot_date TEXT PRIMARY KEY, total_value_krw REAL NOT NULL,
            stock_value_krw REAL, cash_value_krw REAL, cost_basis_krw REAL,
            unrealized_pnl_krw REAL, position_count INTEGER, fx_usdkrw REAL,
            breakdown_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO nav_snapshots VALUES ('2026-10-02', 100, 90, 10, 80, 10, 1, 1300,
            '{"by_currency":{"KRW":100}}', 'old-created', 'old-updated');
        """)
    store = NavStore(path)
    migrated = store.get_snapshot("2026-10-02")
    assert migrated["snapshot_id"]
    assert migrated["version"] == 1
    assert migrated["total_value_krw"] == 100
    assert migrated["created_at"] == "old-created"
    assert migrated["breakdown"]["by_currency"]["KRW"] == 100
    assert migrated["valuation_at"] is None and migrated["observed_at"] is None
    assert migrated["calculation_version"] == "legacy_unknown"
    assert migrated["result_status"] == "partial"
    assert migrated["settlement_adjusted_nav_krw"] is None
    newer = store.upsert_snapshot(summary(total=120), "2026-10-02")
    assert newer["version"] == 2
    assert NavStore(path).select_snapshots([migrated["snapshot_id"]]) == [migrated]


def test_legacy_writer_after_migration_cannot_silently_desynchronize_representative(store):
    first = store.upsert_snapshot(summary(), "2026-10-02")
    # The old checkout's timer can still update the compatibility table until ops
    # applies the proposed release unit. Retain its changes as unknown provenance.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE nav_snapshots SET total_value_krw=200, updated_at='old-timer' WHERE snapshot_date='2026-10-02'")
    second = store.get_snapshot("2026-10-02")
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["version"] == 2
    assert second["total_value_krw"] == 200
    assert second["calculation_version"] == "legacy_unknown"
    assert store.select_snapshots([first["snapshot_id"]]) == [first]


def test_concurrent_recollections_allocate_unique_versions_atomically(store):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda total: store.upsert_snapshot(summary(total=total), "2026-10-02"), range(100, 108)))
    assert sorted(row["version"] for row in rows) == list(range(1, 9))
    assert len({row["snapshot_id"] for row in rows}) == 8
    assert store.get_snapshot("2026-10-02")["version"] == 8


def test_snapshot_stores_bridge_and_price_time_separately_from_observation(store):
    row = store.upsert_snapshot(summary(), "2026-10-02", fx_usdkrw=1300)
    assert row["settlement_adjusted_nav_krw"] == 95
    assert row["total_value_krw"] == 100
    assert row["nav_bridge"] == summary()["nav_bridge"]
    assert row["result_status"] == "complete"
    assert row["observed_at"] != row["components"][0]["priced_at"]
    assert row["calculation_version"]


def test_current_prices_under_past_date_are_explicitly_labelled(store):
    row = store.upsert_snapshot(summary(day=date.today().isoformat()), "2020-01-02")
    assert row["result_status"] == "partial"
    assert "SNAPSHOT_DATE_MISMATCH" in row["reason_codes"]
    assert row["valuation_at"].startswith(date.today().isoformat())


def test_future_component_price_cannot_be_disguised_by_past_valuation_at(store):
    value = summary()
    value["components"][0]["priced_at"] = "2026-10-04T15:30:00+09:00"
    row = store.upsert_snapshot(value, "2026-10-02")
    assert "SNAPSHOT_DATE_MISMATCH" in row["reason_codes"]


def test_calendar_gaps_are_unknown_without_explicit_calendar_and_ignore_holidays(store):
    store.upsert_snapshot(summary(), "2026-10-02")
    store.upsert_snapshot(summary("2026-10-06", 110), "2026-10-06")
    unknown = store.collection_health("2026-10-02", "2026-10-06")
    assert unknown["expected_days"] is None and unknown["expected_day_gaps"] is None
    calendar = ["2026-10-02", "2026-10-06"]  # Oct 5 excluded explicitly, not guessed.
    covered = store.collection_health("2026-10-02", "2026-10-06", calendar)
    assert covered["expected_day_gaps"] == []
    missing = store.collection_health("2026-10-02", "2026-10-06", calendar + ["2026-10-05"])
    assert missing["expected_day_gaps"] == ["2026-10-05"]


def test_health_counts_failed_attempts_and_success_is_not_price_freshness(store):
    store.upsert_snapshot(summary(), "2026-10-02")
    store.record_attempt("2026-10-06", "unavailable", ["FAILED_SOURCES"])
    health = store.collection_health("2026-10-02", "2026-10-06", ["2026-10-02", "2026-10-06"])
    assert health["success_rate"] == 0.5
    assert health["attempt_count"] == 2
    assert health["last_success_at"]
    assert health["expected_day_gaps"] == ["2026-10-06"]
    assert health["success_definition"] == "complete_source_collection_and_snapshot_write"


@pytest.mark.parametrize("bad_ids", [["missing"], []])
def test_pinned_selection_rejects_missing_or_empty_ids(store, bad_ids):
    with pytest.raises(ValueError):
        store.select_snapshots(bad_ids)


def test_pinned_selection_rejects_two_versions_for_one_day(store):
    first = store.upsert_snapshot(summary(), "2026-10-02")
    second = store.upsert_snapshot(summary(total=120), "2026-10-02")
    with pytest.raises(ValueError):
        store.select_snapshots([first["snapshot_id"], second["snapshot_id"]])


def test_history_replay_preserves_values_after_representative_changes(monkeypatch, store):
    import server
    store.upsert_snapshot(summary(), "2026-10-02")
    store.upsert_snapshot(summary("2026-10-03", 110), "2026-10-03")
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    first = server.get_portfolio_history(period="ALL", detail_level="full", external_cashflows_verified_zero=True)["data"]
    ids = first["input_snapshot_ids"]
    store.upsert_snapshot(summary("2026-10-03", 900), "2026-10-03")
    replay = server.get_portfolio_history(period="ALL", detail_level="full", external_cashflows_verified_zero=True, input_snapshot_ids=ids)["data"]
    assert replay["snapshots"] == first["snapshots"]
    assert replay["statistics"] == first["statistics"]
    assert replay["statistics"]["metrics"]["max_drawdown_pct"]["input_snapshot_ids"] == ids


def test_history_replay_parameters_pin_calendar_bounds_and_risk_free_rate(monkeypatch, store):
    import server
    store.upsert_snapshot(summary(), "2026-10-02")
    store.upsert_snapshot(summary("2026-10-03", 110), "2026-10-03")
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    first = server.get_portfolio_history(period="ALL", expected_snapshot_dates=["2026-10-01", "2026-10-02", "2026-10-03"], external_cashflows_verified_zero=True)["data"]
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0.05)
    replay = server.get_portfolio_history(**first["replay_parameters"])["data"]
    assert replay["statistics"] == first["statistics"]
    assert replay["collection_health"]["expected_day_gaps"] == ["2026-10-01"]


def test_pinned_history_never_fetches_a_live_risk_free_quote(monkeypatch, store):
    import server
    first = store.upsert_snapshot(summary(), "2026-10-02")
    last = store.upsert_snapshot(summary("2026-10-03", 110), "2026-10-03")
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: pytest.fail("replay cannot fetch current Treasury yield"))
    result = server.get_portfolio_history(input_snapshot_ids=[first["snapshot_id"], last["snapshot_id"]])
    assert result["ok"] is True
    assert result["data"]["statistics"]["risk_free_rate_used"] == 0


def test_pinned_performance_does_not_recollect_live_prices_or_ledger(monkeypatch, store):
    import server
    first = store.upsert_snapshot(summary(), "2026-10-02")
    last = store.upsert_snapshot(summary("2026-10-03", 110), "2026-10-03")
    ids = [first["snapshot_id"], last["snapshot_id"]]
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    def forbidden():
        raise AssertionError("pinned snapshot replay must not collect live inputs")
    monkeypatch.setattr(server, "_get_collector", forbidden)
    before = server.get_portfolio_performance(input_snapshot_ids=ids, external_cashflows_verified_zero=True, detail_level="full")
    assert before["ok"]
    store.upsert_snapshot(summary("2026-10-03", 900), "2026-10-03")
    after = server.get_portfolio_performance(input_snapshot_ids=ids, external_cashflows_verified_zero=True, detail_level="full")
    assert before["data"] == after["data"]
    assert after["data"]["raw_asset_change_pct"] == 10
    assert after["data"]["metrics"]["twr_pct"]["input_snapshot_ids"] == ids
    assert after["data"]["pnl"]["realized_krw"] is None
    assert after["data"]["replay_scope"] == "snapshot_nav_only"


def test_snapshot_quality_and_expected_gaps_feed_existing_metric_reasons():
    rows = [dict(summary(), snapshot_id="a", snapshot_date="2026-10-02", total_value_krw=100,
                 reason_codes=["SNAPSHOT_DATE_MISMATCH"]),
            dict(summary("2026-10-03"), snapshot_id="b", snapshot_date="2026-10-03", total_value_krw=110)]
    result = pe.nav_statistics(rows, flows_by_date={}, input_quality={"snapshot_reason_codes": ["MISSING_EXPECTED_SNAPSHOT"]})
    metric = result["metrics"]["max_drawdown_pct"]
    assert metric["value"] is None
    assert set(metric["reason_codes"]) == {"SNAPSHOT_DATE_MISMATCH", "MISSING_EXPECTED_SNAPSHOT"}
    assert metric["input_snapshot_ids"] == ["a", "b"]
    performance = pe.compute_performance([], rows, 110, FxRates(), "2026-10-02", "2026-10-03",
        external_cashflows_coverage={"status": "verified_zero", "start": "2026-10-02", "end": "2026-10-03", "source": "test"})
    assert "SNAPSHOT_DATE_MISMATCH" in performance["metrics"]["twr_pct"]["reason_codes"]


def test_explicit_valuation_calendar_does_not_treat_a_holiday_as_a_missing_day():
    calendar = ["2026-10-01", "2026-10-02", "2026-10-06"]
    rows = [dict(snapshot_date=day, total_value_krw=value, snapshot_id=day)
            for day, value in zip(calendar, [100, 110, 105])]
    result = pe.nav_statistics(rows, flows_by_date={}, input_quality={"expected_snapshot_dates": calendar})
    assert result["metrics"]["volatility_annual_pct"]["status"] == "available"


def test_snapshot_write_tool_annotations_are_honest():
    import server
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["get_portfolio_summary"].annotations.readOnlyHint is True  # no snapshot argument
    for name in ("snapshot_portfolio_nav",):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
        assert tools[name].annotations.idempotentHint is False


def test_past_date_snapshot_tool_refuses_before_collecting(monkeypatch, store):
    import server
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_get_collector", lambda: pytest.fail("must validate date before collection"))
    result = server.snapshot_portfolio_nav("2020-01-02")
    assert result["ok"] is False
    assert "current prices" in result["error"]["message"]
    assert store.count() == 0


def test_provenance_never_substitutes_fx_fetch_time_for_underlying_quote_time():
    from portfolio_store import snapshot_summary
    component = SimpleNamespace(symbol="SYN", account="test", currency="USD", priced_at=None,
                                to_dict=lambda: {"symbol": "SYN", "currency": "USD"})
    collection = SimpleNamespace(holdings=[component], cash=[], fetched_at="2026-10-02T12:00:00Z",
                                 failed_sources=[], truncated_sources=[])
    fx = FxRates({"USD": 1300}, as_of="2026-10-02T12:00:01Z")
    captured = snapshot_summary(summary(), collection, fx)
    assert captured["components"][0]["priced_at"] is None
    assert captured["fx_times"]["USD"]["priced_at"] is None
    assert captured["fx_times"]["USD"]["observed_at"] == fx.as_of


def test_cli_force_records_partial_quality_and_failed_attempt(monkeypatch, store):
    import server
    from portfolio_sources import Collection
    from scripts import snapshot_nav
    collection = Collection(health={"test": {"status": "error"}}, fetched_at="2026-10-02T12:00:00Z")
    collector = SimpleNamespace(collect_positions=lambda: collection, build_fx=lambda currencies: FxRates())
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(snapshot_nav, "apply_env_file", lambda *args: None)
    monkeypatch.setattr(snapshot_nav, "prepare_crypto_snapshot", lambda *args: None)
    monkeypatch.setattr(pe, "summarize_portfolio", lambda *args: summary(day=date.today().isoformat()))
    monkeypatch.setattr("sys.argv", ["snapshot_nav.py", "--force"])
    assert snapshot_nav.main() == 0
    row = store.get_snapshot(date.today().isoformat())
    assert row["result_status"] == "partial"
    assert "FAILED_SOURCES" in row["reason_codes"]
    assert store.collection_health("2020-01-01", "2099-01-01")["success_rate"] == 0


def test_cli_past_date_is_rejected_even_with_force_before_loading_environment(monkeypatch):
    from scripts import snapshot_nav
    monkeypatch.setattr(snapshot_nav, "apply_env_file", lambda *args: pytest.fail("past date must fail before loading environment"))
    monkeypatch.setattr("sys.argv", ["snapshot_nav.py", "--date", "2020-01-02", "--force"])
    assert snapshot_nav.main() == 1


def test_failed_tool_collection_is_persisted_in_snapshot_health(monkeypatch, store):
    import server
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_get_collector", lambda: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    assert server.snapshot_portfolio_nav()["ok"] is False
    health = store.collection_health("2020-01-01", "2099-01-01")
    assert health["attempt_count"] == 1 and health["success_rate"] == 0


def test_summary_write_failure_is_counted_without_failing_the_summary(monkeypatch, store):
    import server
    from portfolio_model import CashBalance
    from portfolio_sources import Collection
    collection = Collection(cash=[CashBalance("test", "test", "KRW", 100)], health={"test": {"status": "ok"}})
    collector = SimpleNamespace(collect_positions=lambda: collection, build_fx=lambda currencies: FxRates())
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(store, "upsert_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic write failure")))
    result = server.get_portfolio_summary(include_realized=False, snapshot=True)
    assert result["ok"] is True
    assert result["data"]["snapshot"]["stored"] is False
    assert store.collection_health("2020-01-01", "2099-01-01")["attempt_count"] == 1


def test_tool_write_without_crypto_fx_keeps_the_scheduled_representative(store):
    """2.12.11: a summary call after 18:10 must not replace the scheduled capture's crypto evidence."""
    scheduled = store.upsert_snapshot(dict(summary(), crypto_fx_times={'USDT': {'rate_to_krw': 1400}}), "2026-10-02")
    assert 'fx_times' in scheduled['crypto_nav']
    tool = store.upsert_snapshot(summary(total=120), "2026-10-02", keep_crypto_representative=True)
    assert tool['representative'] is False and tool['version'] == 2 and 'fx_times' not in tool['crypto_nav']
    assert store.get_snapshot("2026-10-02")["snapshot_id"] == scheduled["snapshot_id"]
    assert store.select_snapshots([tool["snapshot_id"]])[0]["version"] == 2  # stored, immutable, not representative
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT total_value_krw FROM nav_snapshots WHERE snapshot_date='2026-10-02'").fetchone()[0] == 100
        assert conn.execute("SELECT COUNT(*) FROM nav_collection_attempts").fetchone()[0] == 2
    # A tool write that carries crypto FX evidence, or one on a day whose representative has none, still moves it.
    moved = store.upsert_snapshot(dict(summary(total=130), crypto_fx_times={}), "2026-10-02", keep_crypto_representative=True)
    assert 'representative' not in moved and store.get_snapshot("2026-10-02")["snapshot_id"] == moved["snapshot_id"]
    first = store.upsert_snapshot(summary("2026-10-03", 110), "2026-10-03")
    second = store.upsert_snapshot(summary("2026-10-03", 120), "2026-10-03", keep_crypto_representative=True)
    assert 'fx_times' not in first['crypto_nav'] and store.get_snapshot("2026-10-03")["snapshot_id"] == second["snapshot_id"]
    # Without the flag (the scheduled job) the latest version always becomes the representative.
    last = store.upsert_snapshot(summary(total=140), "2026-10-02")
    assert store.get_snapshot("2026-10-02")["snapshot_id"] == last["snapshot_id"]


def test_summary_and_snapshot_tools_keep_the_scheduled_crypto_representative(monkeypatch, store):
    import server
    from portfolio_model import CashBalance
    from portfolio_sources import Collection
    today = date.today().isoformat()
    scheduled = store.upsert_snapshot(dict(summary(today), crypto_fx_times={'USDT': {'rate_to_krw': 1400}}), today)
    collection = Collection(cash=[CashBalance("test", "test", "KRW", 100)], health={"test": {"status": "ok"}})
    collector = SimpleNamespace(collect_positions=lambda: collection, build_fx=lambda currencies: FxRates())
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    result = server.get_portfolio_summary(include_realized=False, snapshot=True)
    assert result["data"]["snapshot"]["stored"] is True and result["data"]["snapshot"]["daily_representative"] is False
    assert server.snapshot_portfolio_nav()["data"]["snapshot"]["representative"] is False
    assert store.get_snapshot(today)["snapshot_id"] == scheduled["snapshot_id"]
