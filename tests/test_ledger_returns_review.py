"""Four review reproductions, with synthetic durable identity evidence."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tests.test_ledger_returns import (
    ACCOUNTS,
    END,
    START,
    blocked,
    coverage,
    flow,
    performance,
)
from tests.test_ledger_returns import (
    wired as base_wired,
)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    return base_wired.__wrapped__(tmp_path, monkeypatch)


def roster(w, *, second_toss=False):
    for nav in w.navs:
        nav['account_nav'] = {'toss:fixture': {}, 'kis:fixture': {}}
        if second_toss:
            nav['account_nav']['toss:missing'] = {}
        nav['crypto_nav'] = {v: {'accounts': [{'account_id': ACCOUNTS[v], 'asset': 'KRW',
            'scope': 'futures_collateral' if v == 'binance' else 'perp_dex', 'dex': ''}]}
            for v in ('binance', 'hyperliquid')}
        nav['consolidated_nav_krw']['basis'] = 'legacy_broker_assets_plus_verified_futures_equity_before_settlement_adjustments'
    with w.ledger.transaction() as conn:
        for v in ('toss', 'kis', 'binance', 'hyperliquid'):
            reference = v + ':fixture' if v in ('toss', 'kis') else ACCOUNTS[v]
            conn.execute('INSERT OR IGNORE INTO ledger_account_aliases VALUES (?,?,?)',
                (w.ledger._privacy.opaque(reference), ACCOUNTS[v], datetime.now(timezone.utc).isoformat()))


def wallet_pair(w, out_wallet='MAIN', in_wallet='UMFUTURE'):
    events = []
    with w.ledger.transaction() as conn:
        for direction, wallet in (('out', out_wallet), ('in', in_wallet)):
            event = dict(source='binance_transfers', account_identity=ACCOUNTS['binance'],
                source_record_id='wallet-' + direction, native_record_id='wallet', kind='transfer_internal',
                amount='50', currency='KRW', wallet=wallet, direction=direction,
                transfer_route=out_wallet + '_' + (in_wallet or 'UNKNOWN'), amount_precision='1',
                execution_time_utc='2025-12-31T16:00:00+00:00', source_status='CONFIRMED',
                timestamp_precision='second', source_timezone='UTC', identity_quality='native_record')
            assert w.ledger.append(conn, event) == 'new'
            event_id = w.ledger.event_id(event)
            w.ledger.classify_transfer(conn, event_id, 'internal', 'MATCHED_INTERNAL_TRANSFER')
            events.append(event_id)
        conn.execute('INSERT INTO ledger_transfer_matches VALUES (?,?,?,?,?,?,?,?)',
            ('wallet-pair', 1, *events, 'synthetic', '{}', 'internal', datetime.now(timezone.utc).isoformat()))


def test_fix1_wallet_boundary_main_to_futures_is_not_gain(wired):
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    roster(wired)
    wallet_pair(wired)
    wired.navs[0]['consolidated_nav_krw']['value'] = 200
    wired.navs[-1]['consolidated_nav_krw']['value'] = 250
    data = performance(wired, portfolio_scope='consolidated')
    assert data['returns']['twr_pct'] == 0
    assert data['returns']['simple_return_pct'] == 0
    assert data['returns']['xirr_pct'] == 0
    assert data['flows']['flows_by_date'] == {END: 50}


def test_fix1_nav_account_missing_ledger_mapping_blocks(wired):
    coverage(wired)
    roster(wired, second_toss=True)
    flow(wired)
    blocked(performance(wired), 'ACCOUNT_FLOW_COVERAGE_MISSING')


def test_fix1_conflicting_timestamp_inside_window_blocks(wired):
    coverage(wired)
    roster(wired)
    event, _ = flow(wired, stamp='2026-01-02T00:00:00+00:00')
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'execution_time_utc': '2026-01-01T00:00:00+00:00'}) == 'conflict'
    blocked(performance(wired), 'CONFLICTED_FLOWS')


def test_fix1_ten_thousand_old_flows_have_constant_statement_count(wired, monkeypatch):
    from ledger_returns import read_flows
    coverage(wired)
    roster(wired)
    event, _ = flow(wired, stamp='2024-01-01T00:00:00+00:00', classification='unknown')
    # Bulk synthetic immutable rows isolate reader scaling from ingestion costs.
    with wired.ledger.transaction() as conn:
        original = dict(conn.execute('SELECT * FROM ledger_events LIMIT 1').fetchone())
        columns = list(original)
        for index in range(10_000):
            row = {**original, 'event_id': f'old-{index}', 'source_record_id': f'old-{index}',
                'payload_json': json.dumps({**event, 'source_record_id': f'old-{index}'})}
            conn.execute('INSERT INTO ledger_events (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                         tuple(row[c] for c in columns))
    statements = []
    connect = sqlite3.connect
    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, 'connect', traced)
    evidence = read_flows(wired.ledger.db_path, START, END, 'equity', wired.navs)
    assert evidence['coverage']['reason_codes'] == []
    assert evidence['flows_by_date'] == {}
    assert len(statements) <= 50, len(statements)
    query = next(sql for sql in statements if 'SELECT event_id,payload_json FROM ledger_events' in sql)
    with connect(wired.ledger.db_path) as conn:
        plan = conn.execute('EXPLAIN QUERY PLAN ' + query).fetchall()
    assert any('ledger_flow_window' in row[3] for row in plan), plan


@pytest.mark.parametrize('wallet', ['FUNDING', None])
def test_fix1_futures_to_other_wallet_respects_nav_cut(wired, wallet):
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    roster(wired)
    wallet_pair(wired, 'UMFUTURE', wallet)
    if wallet is None:
        blocked(performance(wired, portfolio_scope='consolidated'), 'FLOW_NAV_BOUNDARY_UNKNOWN')
        return
    data = performance(wired, portfolio_scope='consolidated')
    assert data['flows']['flows_by_date'] == ({END: -50} if wallet == 'FUNDING' else {})


def test_fix1_roster_mapping_without_flow_coverage_blocks(wired):
    coverage(wired)
    roster(wired, second_toss=True)
    with wired.ledger.transaction() as conn:
        conn.execute('INSERT INTO ledger_account_aliases VALUES (?,?,?)',
            (wired.ledger._privacy.opaque('toss:missing'), 'acct_' + 'e' * 32, datetime.now(timezone.utc).isoformat()))
    blocked(performance(wired), 'ACCOUNT_FLOW_COVERAGE_MISSING')


def test_fix1_crypto_risk_identity_requires_durable_mapping(wired):
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    roster(wired)
    wired.navs[-1]['crypto_nav']['hyperliquid']['accounts'].append(
        {'account_id': 'acct_' + 'f' * 32, 'asset': 'KRW', 'scope': 'perp_dex', 'dex': ''})
    blocked(performance(wired, portfolio_scope='consolidated'), 'ACCOUNT_FLOW_COVERAGE_MISSING')


def test_fix1_read_budget_exhaustion_is_null(wired):
    from ledger_returns import read_flows
    coverage(wired)
    evidence = read_flows(wired.ledger.db_path, START, END, 'equity', wired.navs, budget_seconds=0)
    assert evidence['flows_by_date'] is None
    assert evidence['observed_deposits_krw'] is None
    assert 'FLOW_READ_BUDGET_EXCEEDED' in evidence['coverage']['reason_codes']
    result = wired.server.portfolio_engine.compute_performance([], wired.navs, None,
        wired.server.portfolio_engine.FxRates(), START, END,
        external_cashflows_coverage=evidence['coverage'], ledger_flow_input=evidence)
    blocked(result, 'FLOW_READ_BUDGET_EXCEEDED')


def test_fix1_live_flow_returns_use_same_stored_nav_boundary(wired):
    coverage(wired)
    flow(wired)
    result = wired.server.get_portfolio_performance()
    assert result['ok']
    assert result['data']['end_nav_krw'] == 160
    assert result['data']['returns']['twr_pct'] == 10


def test_fix1_microsecond_end_boundary_is_not_dropped_by_sql_rounding(wired):
    coverage(wired)
    flow(wired, stamp='2026-01-01T14:59:59.999999+00:00')
    assert performance(wired)['flows']['flows_by_date'] == {END: 50}


def test_fix1_effective_timestamp_enrichment_brings_event_into_window(wired):
    coverage(wired)
    event, event_id = flow(wired, stamp='2026-01-02T00:00:00+00:00')
    # The date-only first observation gets a compatible precise timestamp.
    with wired.ledger.transaction() as conn:
        row = dict(conn.execute('SELECT * FROM ledger_events WHERE event_id=?', (event_id,)).fetchone())
        payload = {**event, 'source_record_id': 'date-only', 'execution_time_utc': None, 'source_timezone': 'Asia/Seoul'}
        row.update(event_id='date-only', source_record_id='date-only', execution_time_utc=None, payload_json=json.dumps(payload))
        columns = list(row)
        conn.execute('INSERT INTO ledger_events (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(row[c] for c in columns))
        conn.execute('INSERT INTO ledger_enrichments VALUES (?,?,?,?)', ('date-only', 'synthetic-enrichment',
            json.dumps({'execution_time_utc': '2026-01-01T00:00:00+00:00'}), datetime.now(timezone.utc).isoformat()))
        wired.ledger.classify_transfer(conn, 'date-only', 'external_in', 'SYNTHETIC_CLASSIFICATION')
    assert performance(wired)['flows']['flows_by_date'] == {END: 50}


def test_fix1_additive_indexes_upgrade_existing_immutable_schema(wired):
    from portfolio_ledger import LedgerStore
    with wired.ledger.read() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name IN ('ledger_flow_window','ledger_enrichment_window','ledger_conflict_window','ledger_flow_coverage_window','ledger_first_flow_coverage')")]
        for name in names:
            conn.execute('DROP INDEX ' + name)
        before = conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0]
    with LedgerStore(wired.ledger.db_path).read() as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN ('ledger_flow_window','ledger_enrichment_window','ledger_conflict_window','ledger_flow_coverage_window','ledger_first_flow_coverage')").fetchone()[0] == 5
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == before


def test_fix1_window_ending_before_first_coverage_keeps_reason(wired):
    from ledger_returns import read_flows
    coverage(wired, lo='2025-11-01')
    evidence = read_flows(wired.ledger.db_path, START, '2025-10-01', 'equity', wired.navs)
    assert evidence['flows_by_date'] is None
    assert 'PERIOD_BEFORE_FLOW_COVERAGE' in evidence['coverage']['reason_codes']
