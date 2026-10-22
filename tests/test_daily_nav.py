"""Daily NAV acceptance, using only synthetic normalized balances and SQLite."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from futures_risk import RiskStore
from portfolio_engine import summarize_portfolio
from portfolio_model import CashBalance, FxRates, Holding
from portfolio_sources import Collection
from portfolio_store import NavStore, snapshot_summary

STAMP = '2026-10-10T09:10:00Z'  # Saturday 18:10 KST


def fixture_summary():
    collection = Collection(
        holdings=[Holding('kis', 'kis:test:overseas', 'SYN', 'USD', 1, market_value=100)],
        cash=[CashBalance('kis', 'kis:test:overseas', 'USD', 20, settled_cash=20,
                          unsettled_receivables=5, unsettled_payables=2,
                          source_fields={'settled_cash': 'synthetic.deposit'})],
        health={'kis:test:overseas': {'status': 'ok'}}, fetched_at=STAMP)
    fx = FxRates({'USD': 1300}, as_of=STAMP)
    legacy = summarize_portfolio(collection.holdings, collection.cash, fx)
    return legacy, snapshot_summary(legacy, collection, fx)


def risk_capture(path, stamp=STAMP, *, unified=False, duplicate=False, snapshot_id='risk-test'):
    def metric(amount):
        return {'value': str(amount), 'status': 'reported', 'reason_codes': []}
    accounts = [dict(venue='binance', account_id='acct_b', scope='futures_collateral', dex='',
                     asset='USDT', observed_at=stamp, source_status='complete', equity=metric(10))]
    accounts.append(dict(venue='hyperliquid', account_id='acct_h', dex='', asset='USDC',
                         scope='unified_shared_wallet' if unified else 'perp_dex',
                         abstraction='unifiedAccount' if unified else 'default', collateral_status='known',
                         observed_at=stamp, source_status='complete', equity=metric(30)))
    if unified:
        accounts.extend([dict(accounts[-1], scope='perp_dex', dex=dex, equity=metric(100))
                         for dex in ('', 'builder')])
    if duplicate:
        accounts.append(dict(accounts[0]))
    # These fixtures exercise persisted pre-NAV captures, whose version must
    # remain explicit when RiskStore's current writer version advances.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('futures_risk.CALCULATION_VERSION', 'futures_risk_v1')
        RiskStore(path).append(dict(snapshot_id=snapshot_id, started_at=stamp, completed_at=stamp,
            source_status='complete', accounts=accounts, positions=[],
            venues=[dict(venue=v, source_status='complete') for v in ('binance', 'hyperliquid')]))


def test_weekend_timer_runs_at_1810_in_seoul():
    timer = Path('deploy/k-invest-snapshot.timer').read_text()
    assert 'OnCalendar=*-*-* 18:10:00 Asia/Seoul' in timer
    assert 'Mon..Fri' not in timer
    assert 'RandomizedDelaySec=0' in timer


def test_native_nav_cash_unsettled_and_exposure_persist_additively(tmp_path):
    legacy, summary = fixture_summary()
    account = summary['account_nav']['kis:test:overseas']
    assert account['nav_native']['USD']['value'] == 120
    assert account['cash']['USD']['basis'] == 'verified'
    assert account['unsettled']['USD']['receivables']['value'] == 5
    assert account['unsettled']['USD']['payables']['value'] == 2
    assert account['currency_exposure']['USD']['value'] == 120
    assert account['currency_exposure']['KRW']['value'] is None
    assert summary['valuation_timezone'] == 'Asia/Seoul'
    assert summary['mark_quality']['quotes'][0]['priced_at'] is None
    assert 'UNKNOWN_QUOTE_TIME' in summary['mark_quality']['quotes'][0]['reason_codes']
    assert all(summary[k] == v for k, v in legacy.items())
    store = NavStore(tmp_path / 'nav.db')
    row = store.upsert_snapshot(summary, '2026-10-10', 1300)
    assert row['account_nav'] == summary['account_nav']
    assert row['total_value_krw'] == legacy['total_assets_krw']
    assert store.get_snapshot('2026-10-10') == row
    assert store.range('2026-10-10', '2026-10-10') == [row]


def test_missing_source_is_null_with_reason_and_buying_power_is_not_cash():
    c = Collection(cash=[CashBalance('toss', 'toss:test', 'USD', 999, kind='buying_power')],
        health={'toss:test': {'status': 'ok'}, 'toss:test:usd': {'status': 'error'}}, fetched_at=STAMP)
    data = snapshot_summary(summarize_portfolio(c.holdings, c.cash, FxRates()), c, FxRates())
    account = data['account_nav']['toss:test']
    assert account['nav_native']['USD']['value'] is None
    assert account['nav_native']['USD']['reason_codes']
    assert account['cash']['USD']['value'] is None
    assert account['cash']['USD']['basis'] == 'unknown'
    assert account['unsettled']['USD']['receivables']['value'] is None
    c = Collection(health={'kis:missing:domestic': {'status': 'error'}}, fetched_at=STAMP)
    data = snapshot_summary({'total_assets_krw': 0}, c, FxRates())
    assert data['account_nav']['kis:missing:domestic']['nav_native']['KRW']['value'] is None


def test_stale_crypto_snapshot_is_null_and_keeps_snapshot_age(tmp_path):
    path = tmp_path / 'nav.db'
    risk_capture(path, '2026-10-10T07:09:59Z')
    _, summary = fixture_summary()
    row = NavStore(path).upsert_snapshot(summary, '2026-10-10')
    crypto = row['crypto_nav']
    assert crypto['risk_snapshot_id'] == 'risk-test'
    assert crypto['age_seconds'] == 7201
    assert crypto['binance']['value_krw'] is None
    assert row['consolidated_nav_krw']['value'] is None
    assert 'STALE_CRYPTO_SNAPSHOT' in row['consolidated_nav_krw']['reason_codes']


def test_verified_crypto_collateral_is_added_once_with_direct_fx(tmp_path):
    path = tmp_path / 'nav.db'
    risk_capture(path)
    legacy, summary = fixture_summary()
    summary['fx_times'].update({c: {'rate_to_krw': 1300, 'priced_at': STAMP, 'observed_at': STAMP}
                                for c in ('USDT', 'USDC')})
    row = NavStore(path).upsert_snapshot(summary, '2026-10-10')
    assert row['crypto_nav']['binance']['by_currency']['USDT']['value'] == 10
    assert row['crypto_nav']['hyperliquid']['by_currency']['USDC']['value'] == 30
    assert row['consolidated_nav_krw']['value'] == legacy['total_assets_krw'] + 40 * 1300


@pytest.mark.parametrize('unified,duplicate', [(True, False), (False, True)])
def test_shared_wallet_dexes_and_duplicate_collateral_never_double_count(tmp_path, unified, duplicate):
    path = tmp_path / 'nav.db'
    risk_capture(path, unified=unified, duplicate=duplicate)
    _, summary = fixture_summary()
    row = NavStore(path).upsert_snapshot(summary, '2026-10-10')
    assert row['consolidated_nav_krw']['value'] is None
    venue = row['crypto_nav']['hyperliquid' if unified else 'binance']
    assert venue['value_krw'] is None
    assert ('UNIFIED_EQUITY_UNVERIFIED' if unified else 'DUPLICATE_COLLATERAL_SCOPE') in venue['reason_codes']


def test_missing_crypto_source_remains_null(tmp_path):
    _, summary = fixture_summary()
    row = NavStore(tmp_path / 'nav.db').upsert_snapshot(summary, '2026-10-10')
    assert row['crypto_nav']['risk_snapshot_id'] is None
    assert row['consolidated_nav_krw']['value'] is None
    assert 'MISSING_CRYPTO_SNAPSHOT' in row['consolidated_nav_krw']['reason_codes']


def test_summary_read_adds_fields_without_changing_legacy_values(monkeypatch, tmp_path):
    import server
    c = Collection(cash=[CashBalance('kis', 'kis:test:domestic', 'KRW', 100, settled_cash=100)],
                   health={'kis:test:domestic': {'status': 'ok'}}, fetched_at=STAMP)
    fx = FxRates()
    store = NavStore(tmp_path / 'nav.db')
    monkeypatch.setattr(server, '_get_collector', lambda: SimpleNamespace(
        collect_positions=lambda: c, build_fx=lambda currencies: fx))
    monkeypatch.setattr(server, '_get_nav_store', lambda: store)
    response = server.get_portfolio_summary(include_realized=False, snapshot=False)['data']
    assert response['account_nav']['kis:test:domestic']['nav_native']['KRW']['value'] == 100
    assert response['consolidated_nav_krw']['value'] is None
    baseline = summarize_portfolio(c.holdings, c.cash, fx)
    for key, value in baseline.items():
        # The tool also reads perps into underlying_exposure (exposure only, never NAV).
        if key not in ('methodology', 'underlying_exposure'):
            assert response[key] == value
    assert not store.db_path.exists()


def test_quote_and_fx_staleness_use_price_time_not_fetch_time():
    legacy, summary = fixture_summary()
    from daily_nav import mark_quality
    summary['components'][0]['priced_at'] = '2026-10-09T09:09:59Z'
    summary['fx_times']['USD']['priced_at'] = '2026-10-09T09:09:59Z'
    quality = mark_quality(summary)
    assert 'STALE_QUOTE' in quality['quotes'][0]['reason_codes']
    assert 'STALE_FX' in quality['fx']['USD']['reason_codes']
    assert quality['fx']['USD']['observed_at'] == STAMP
    assert datetime.fromisoformat(STAMP.replace('Z', '+00:00')).weekday() == 5


def test_existing_271_snapshot_fields_are_byte_identical():
    import json
    import runpy

    from daily_nav import FIELDS

    old = runpy.run_path('tests/fixtures/legacy_snapshot_summary.py')['snapshot_summary']
    c = Collection(cash=[CashBalance('kis', 'kis:test:domestic', 'KRW', 100, settled_cash=100)],
                   health={'kis:test:domestic': {'status': 'ok'}}, fetched_at=STAMP)
    fx = FxRates({'USD': 1300}, as_of=STAMP)
    summary = summarize_portfolio(c.holdings, c.cash, fx)
    before = old(summary, c, fx)
    after = {k: v for k, v in snapshot_summary(summary, c, fx).items() if k not in FIELDS}
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    assert 'account_nav' in snapshot_summary(summary, c, fx)


def test_nearest_crypto_snapshot_is_pinned_and_future_observations_are_disclosed(tmp_path):
    path = tmp_path / 'nav.db'
    risk_capture(path, '2026-10-10T09:11:00Z')
    _, summary = fixture_summary()
    store = NavStore(path)
    first = store.upsert_snapshot(summary, '2026-10-10')
    assert first['crypto_nav']['age_seconds'] == 60
    assert first['crypto_nav']['observed_at'] == '2026-10-10T09:11:00Z'
    risk_capture(path, snapshot_id='risk-closer')
    second = store.upsert_snapshot(summary, '2026-10-10')
    assert second['crypto_nav']['risk_snapshot_id'] == 'risk-closer'
    assert second['crypto_nav']['age_seconds'] == 0
    assert store.select_snapshots([first['snapshot_id']]) == [first]


def test_external_flows_are_frozen_separately_and_internal_transfers_excluded(tmp_path):
    from portfolio_ledger import LedgerStore, normalize_rows
    path = tmp_path / 'nav.db'
    ledger = LedgerStore(path)
    with ledger.transaction() as conn:
        ids = []
        for i, kind in enumerate(('deposit', 'withdrawal')):
            event = dict(source='synthetic', account_identity='acct_' + 'a' * 32, source_record_id=str(i),
                         kind=kind, execution_time_utc=STAMP, currency='USD', amount='7')
            ledger.append(conn, event)
            ids.append(ledger.event_id(ledger.prepare(conn, event)['candidate']))
        ledger.classify_transfer(conn, ids[0], 'external_in', 'SYNTHETIC_VERIFIED_BOUNDARY')
        legs = normalize_rows(ledger, conn, 'binance/transfer_MAIN_UMFUTURE',
                              dict(tranId=42, timestamp=int(datetime.fromisoformat(STAMP.replace('Z', '+00:00')).timestamp() * 1000),
                                   amount='7', asset='USD', status='CONFIRMED'), {})
        for leg in legs:
            ledger.append(conn, leg)
    assert ledger.match_transfers() == 1
    _, summary = fixture_summary()
    row = NavStore(path).upsert_snapshot(summary, '2026-10-10')
    flows = row['external_flows']
    assert sorted(event['classification'] for event in flows['observed_events']) == ['external_in', 'unknown']
    assert flows['net_by_currency'] is None
    assert flows['reason_codes'] == ['EXTERNAL_FLOW_COVERAGE_UNVERIFIED']
    assert row['unrealized_pnl_krw'] == summary['unrealized_pnl_krw']
    assert row['nav_bridge'] == summary['nav_bridge']


def test_loopback_service_eight_raw_tools_unchanged_except_ledger_disclosure():
    import ast
    import hashlib
    import json

    expected = json.loads(Path('tests/fixtures/legacy_raw_tool_hashes.json').read_text())
    source = Path('server.py').read_text()
    functions = {n.name: ast.get_source_segment(source, n).replace(
        '\n    Fetched broker rows are recorded in the local append-only ledger (no broker writes).', '') for n in ast.parse(source).body
                 if isinstance(n, ast.FunctionDef)}
    assert {name: hashlib.sha256(functions[name].encode()).hexdigest() for name in expected} == expected
    # Couple invariance to the new behavior so this acceptance test fails on 2.7.1.
    assert 'account_nav' in fixture_summary()[1]


def test_unreadable_optional_nav_evidence_preserves_broker_summary(monkeypatch, tmp_path):
    import server
    c = Collection(cash=[CashBalance('kis', 'kis:test:domestic', 'KRW', 100, settled_cash=100)],
                   health={'kis:test:domestic': {'status': 'ok'}}, fetched_at=STAMP)
    store = NavStore(tmp_path / 'broken.db')
    store.db_path.write_text('synthetic invalid SQLite content')
    monkeypatch.setattr(server, '_get_collector', lambda: SimpleNamespace(
        collect_positions=lambda: c, build_fx=lambda currencies: FxRates()))
    monkeypatch.setattr(server, '_get_nav_store', lambda: store)
    response = server.get_portfolio_summary(include_realized=False, snapshot=False)
    assert response['ok'] is True
    assert response['data']['total_assets_krw'] == 100
    assert response['data']['consolidated_nav_krw']['value'] is None
    assert 'LOCAL_NAV_EVIDENCE_UNAVAILABLE' in response['data']['consolidated_nav_krw']['reason_codes']
