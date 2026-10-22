"""Production read/replay budgets on synthetic immutable SQLite evidence only."""
import hashlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from daily_nav import TOSS_CASH_BASIS
from portfolio_ledger import LedgerStore
from portfolio_store import NavStore
from toss_flow_inference import _read_inputs, toss_interval_flows

ACCOUNT = 'toss:primary'
END = datetime(2026, 10, 12, 9, 10, tzinfo=timezone.utc)
SYMBOLS = [f'SYN{i:02}' for i in range(20)]


def nav(stamp, *, open_orders=0, version=1):
    when = stamp.isoformat()
    return {'snapshot_date': when[:10], 'snapshot_id': f'{when}-{version}', 'version': version,
        'valuation_at': when, 'mark_quality': {'quotes': []},
        'account_nav': {ACCOUNT: {'source_status': 'complete',
            'nav_native': {c: {'value': 100, 'reason_codes': []} for c in ('KRW', 'USD')},
            'cash': {c: {'value_decimal': '100', 'basis_reason': TOSS_CASH_BASIS,
                        'observed_at': when} for c in ('KRW', 'USD')},
            'cash_basis_evidence': {'open_order_count': open_orders}}},
        'components': [{'account': ACCOUNT, 'component_type': 'holding', 'symbol': s,
            'currency': 'USD', 'quantity_decimal': '10', 'valuation_price_decimal': '10',
            'priced_at': when} for s in SYMBOLS],
        'fx_times': {'USD': {'rate_decimal': '1400', 'rate_to_krw': 1400, 'priced_at': when,
            'read_at': when, 'valid_until': (stamp + timedelta(minutes=1)).isoformat()}}}


def trade(store, stamp, identity, *, side='buy', symbol=SYMBOLS[0]):
    return {'source': 'toss_transactions', 'account_identity': store.account_identity(ACCOUNT),
        'source_record_id': identity, 'kind': 'fill', 'symbol': symbol, 'currency': 'USD',
        'side': side, 'quantity': '1', 'price_native': '10', 'gross_amount': '10',
        'fee': '0', 'tax': '0', 'execution_time_utc': stamp.isoformat() if stamp else None,
        'source_status': 'FILLED'}


def make_db(path, snapshots, event_factory):
    store = LedgerStore(path)
    events = list(event_factory(store))
    with store.transaction() as conn:
        for event in events:
            store.append(conn, event)
    with NavStore(path)._connect() as conn:
        for snapshot in snapshots:
            NavStore._insert_version(conn, snapshot)
    store.record_coverage('toss', store.account_identity(ACCOUNT), 'toss/fills-v1/synthetic',
        '2020-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00', 'complete', [], 11000, 'synthetic')
    return store


def no_dividends(*args):
    return {'rows': [], 'coverage': [{'symbol': s, 'currency': 'USD', 'start': '2020-01-01',
        'end': '2027-01-01', 'status': 'complete'} for s in SYMBOLS]}


@pytest.fixture(scope='module')
def growing_db(tmp_path_factory):
    path = tmp_path_factory.mktemp('toss-performance') / 'synthetic.db'
    first = END - timedelta(days=1100)
    def events(store):
        for day in range(1, 1101):
            stamp = first + timedelta(days=day, hours=-1)
            for pair in range(5):
                for side in ('buy', 'sell'):
                    yield trade(store, stamp, f'{day}-{pair}-{side}', side=side,
                                symbol=SYMBOLS[(day * 5 + pair) % 20])
    make_db(path, (nav(first + timedelta(days=d)) for d in range(1101)), events)
    return path


@pytest.mark.parametrize(('days', 'ci_budget'), [(90, 4), (365, 8)])
def test_growing_history_budget_and_indexed_bounded_evidence(growing_db, days, ci_budget, request, monkeypatch, tmp_path):
    from test_toss_dividends import bars

    from toss_flow_inference import _decimal, _time
    from yfinance_api import _DIVIDEND_CACHE, _DIVIDEND_CACHE_LOCK, YFinanceClient

    calls = []
    def raw(url, **kwargs):
        calls.append(url)
        symbol = url.rsplit('/', 1)[-1]
        if 'quoteSummary' in url:
            return {'quoteSummary': {'error': None, 'result': [{'price': {'symbol': symbol, 'currency': 'USD'},
                                                               'calendarEvents': {}}]}}
        return bars({'chart': {'error': None, 'result': [{'meta': {'symbol': symbol, 'currency': 'USD'},
            'timestamp': [], 'indicators': {'quote': [{'close': []}]}, 'events': {}}]}}, kwargs['params'])
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: END + timedelta(days=1))
    monkeypatch.setenv('KINVEST_CACHE_DIR', str(tmp_path / 'reference-cache'))
    with _DIVIDEND_CACHE_LOCK:
        _DIVIDEND_CACHE.clear()
    for symbol in SYMBOLS:
        assert YFinanceClient().get_dividend_schedule(symbol, currency='USD', start='2026-01-01',
            end='2026-10-12')['coverage'][0]['status'] == 'estimated'
    _time.cache_clear()
    _decimal.cache_clear()
    start = END - timedelta(days=days)
    before = hashlib.sha256(growing_db.read_bytes()).digest()
    began = time.perf_counter()
    try:
        rows = toss_interval_flows(growing_db, start.isoformat(), END.isoformat(), [ACCOUNT])
    finally:
        with _DIVIDEND_CACHE_LOCK:
            _DIVIDEND_CACHE.clear()
    elapsed = time.perf_counter() - began
    request.node.user_properties.append((f'toss_{days}_days_seconds', f'{elapsed:.6f}'))
    assert len(calls) == 40  # Production provider performs cache projections only.
    assert len(rows) == days * 2
    assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in rows)
    assert elapsed < ci_budget, f'{days} days took {elapsed:.3f}s (local targets 2s/4s; CI allows contention)'
    snaps, fills, windows, _, _ = _read_inputs(growing_db, start.isoformat(), END.isoformat(), [ACCOUNT])
    assert days + 1 < len(snaps) <= days + 308 < 1101
    assert 2000 < len(fills) < 11000  # Must not truncate at Reader's ordinary page limit.
    assert min(datetime.fromisoformat(r['execution_time_utc']) for r in fills) >= datetime.fromisoformat(snaps[0]['time'])
    assert len(windows) == 1
    assert hashlib.sha256(growing_db.read_bytes()).digest() == before


def test_read_queries_use_existing_range_indexes(growing_db, monkeypatch):
    queries = []
    connect = sqlite3.connect
    def observed_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr('toss_flow_inference.sqlite3.connect', observed_connect)
    _read_inputs(growing_db, (END - timedelta(days=90)).isoformat(), END.isoformat(), [ACCOUNT])
    with connect(growing_db) as conn:
        plans = [(q, ' '.join(r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + q)))
                 for q in queries if q.lstrip().upper().startswith(('SELECT', 'WITH'))]
    for table, index in [('ledger_events', 'ledger_flow_window'),
                         ('ledger_conflicts', 'ledger_conflict_window'),
                         ('ledger_enrichments', 'ledger_enrichment_window'),
                         ('ledger_coverage', 'ledger_flow_coverage_window')]:
        matching = [p for q, p in plans if f'INDEXED BY {index}' in q]
        assert matching and all(f'USING INDEX {index}' in p or f'USING COVERING INDEX {index}' in p
                                for p in matching), table
    assert all('ledger_flow_scope' not in p for _, p in plans)
    assert all(not any(f'SCAN {alias} ' in p for alias in ('e', 'o', 'i')) for _, p in plans)
    nav_plans = [p for q, p in plans if 'FROM nav_snapshot_representatives' in q]
    assert nav_plans and all('SEARCH r USING INDEX' in p for p in nav_plans)


def test_open_order_predecessor_keeps_earlier_complete_anchor_and_fills(tmp_path):
    start = END - timedelta(days=1)
    floor = start - timedelta(days=306)
    anchor, filled = floor - timedelta(days=20), floor - timedelta(days=10)
    path = tmp_path / 'open-order.db'
    make_db(path, [nav(anchor), nav(floor - timedelta(days=1), open_orders=1), nav(start), nav(END)],
            lambda store: [trade(store, filled, 'order-created-before-window')])
    snaps, fills, _, _, _ = _read_inputs(path, start.isoformat(), END.isoformat(), [ACCOUNT])
    assert snaps[0]['time'] == anchor.isoformat()
    assert len(fills) == 1 and fills[0]['source_record_id'] == 'order-created-before-window'


@pytest.mark.parametrize('revision', ['conflict', 'unknown-conflict', 'enrichment', 'missing-time'])
def test_old_or_unplaced_fill_evidence_cannot_vanish_from_range(tmp_path, revision):
    path = tmp_path / 'revision.db'
    start = END - timedelta(days=1)
    def events(store):
        original = trade(store, END - timedelta(days=700) if 'conflict' in revision else None, 'same-order')
        yield original
        if revision == 'unknown-conflict':
            yield {**original, 'execution_time_utc': None, 'quantity': '2'}
        elif revision != 'missing-time':
            yield {**original, 'execution_time_utc': (END - timedelta(hours=1)).isoformat()}
    make_db(path, [nav(start), nav(END)], events)
    _, fills, _, _, _ = _read_inputs(path, start.isoformat(), END.isoformat(), [ACCOUNT])
    assert len(fills) == 1
    rows = toss_interval_flows(path, start.isoformat(), END.isoformat(), [ACCOUNT], schedule_provider=no_dividends)
    assert all(r['flow_native'] is None for r in rows)
    expected = {'conflict': 'LEDGER_CONTENT_CONFLICT', 'unknown-conflict': 'LEDGER_CONTENT_CONFLICT',
                'enrichment': 'QUANTITY_UNRECONCILED',
                'missing-time': 'FILL_TIME_MISSING'}[revision]
    assert expected in rows[0]['reasons']
