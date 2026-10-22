"""Liquidation distance and a stable account key on crypto futures rows.

The real FuturesAccounts / Hyperliquid client over mocked HTTP and the real futures_risk
RiskStore on a temporary DB; synthetic numbers and a synthetic address only.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import crypto_futures_accounts as futures  # noqa: E402
import futures_risk  # noqa: E402

ADDRESS = '0x' + 'c' * 36 + '9abc'
FUNDING_TIME = calendar.timegm(datetime(2026, 10, 1, 8).timetuple()) * 1000
POSITIONS = [
    {'coin': 'SYNL', 'szi': '10', 'entryPx': '70', 'positionValue': '776.15', 'liquidationPx': '63.4',
     'leverage': {'type': 'isolated', 'value': 10}},
    {'coin': 'SYNS', 'szi': '-4', 'entryPx': '30', 'positionValue': '121', 'liquidationPx': '40',
     'leverage': {'type': 'cross', 'value': 3}},
    {'coin': 'SYNN', 'szi': '2', 'positionValue': '82', 'liquidationPx': None, 'leverage': {'type': 'cross', 'value': 1}},
]


def _hyperliquid(positions=POSITIONS):
    def handler(request):
        body = json.loads(request.content)
        if body['type'] == 'perpDexs':
            return httpx.Response(200, json=[None])
        if body['type'] == 'userAbstraction':
            return httpx.Response(200, json='default')
        if body['type'] == 'spotClearinghouseState':
            return httpx.Response(200, json={'balances': []})
        if body['type'] == 'userFunding':
            return httpx.Response(200, json=[{'time': FUNDING_TIME, 'hash': '0x' + 'd' * 64,
                                              'delta': {'type': 'funding', 'coin': 'SYNL', 'usdc': '-0.5'}}])
        return httpx.Response(200, json={'assetPositions': [{'position': p} for p in positions],
                                         'marginSummary': {'accountValue': '500', 'totalMarginUsed': '40'},
                                         'withdrawable': '460'})
    return futures.HyperliquidAccountClient([ADDRESS], http=httpx.Client(transport=httpx.MockTransport(handler)),
                                            sleeper=lambda _: None)


def _query(operation, **window):
    return futures.FuturesAccounts(None, _hyperliquid).query(operation, 'hyperliquid', **window)


def _rows(result, field='positions'):
    return {row['symbol']: row for row in result['venues']['hyperliquid'][field]}


def _expected(mark, liquidation, side):
    mark, liquidation = Decimal(mark), Decimal(liquidation)
    move = mark - liquidation if side == 'long' else liquidation - mark
    return str((move / mark * 100).quantize(Decimal('0.0001')))


# ── Liquidation distance ──────────────────────────────────────────────────


def test_distance_to_liquidation_uses_the_derived_mark_with_a_signed_convention():
    result = _query('positions')
    rows = _rows(result)
    long_row, short_row, open_row = rows['SYNL'], rows['SYNS'], rows['SYNN']
    assert long_row['distance_to_liquidation_pct'] == _expected('77.615', '63.4', 'long') == '18.3148'
    assert short_row['distance_to_liquidation_pct'] == _expected('30.25', '40', 'short') == '32.2314'
    assert long_row['field_status']['distance_to_liquidation_pct'] == {
        'value': '18.3148', 'status': 'derived', 'mark_basis': 'derived', 'reason_codes': []}
    assert open_row['distance_to_liquidation_pct'] is None  # no liquidation level: never guessed
    assert open_row['field_status']['distance_to_liquidation_pct']['reason_codes'] == ['LIQUIDATION_PRICE_UNAVAILABLE']
    assert result['liquidation_distance'] == futures.LIQUIDATION_DISTANCE  # convention stated once
    assert 'liquidation_distance' not in _query('funding', start_date='2026-10-01', end_date='2026-10-02')


@pytest.mark.parametrize('raw, expected, reasons, basis', [
    ({'symbol': 'SYNUSDT', 'positionAmt': '-2', 'markPrice': '100', 'liquidationPrice': '125', 'notional': '-200'},
     '25.0000', [], 'venue'),
    ({'symbol': 'SYNUSDT', 'positionAmt': '3', 'markPrice': '100', 'liquidationPrice': '0', 'notional': '300'},
     None, ['LIQUIDATION_PRICE_UNAVAILABLE'], None),
    ({'symbol': 'SYNUSDT', 'positionAmt': '1', 'markPrice': '90', 'liquidationPrice': '95', 'notional': '90'},
     '-5.5556', [], 'venue'),  # mark already past liquidation: negative
])
def test_binance_rows_use_the_venue_mark(raw, expected, reasons, basis):
    row = futures._position(raw, 'binance', {'account_id': 'acct_' + '0' * 32, 'account_last4': None})
    assert row['distance_to_liquidation_pct'] == expected
    status = row['field_status']['distance_to_liquidation_pct']
    assert status['reason_codes'] == reasons and status['mark_basis'] == basis
    risk = futures_risk._position(raw, 'binance', 'acct_x', '', '2026-10-21T00:00:00Z')['liquidation_distance_pct']
    if expected is None:
        assert risk['value'] is None
    else:  # the same formula and sign as the stored risk snapshot
        assert Decimal(risk['value']).quantize(Decimal('0.0001')) == Decimal(expected)


# ── Stable account key ──────────────────────────────────────────────────


def test_account_key_is_the_db_salted_risk_key_shared_by_positions_and_funding(tmp_path, monkeypatch):
    db = tmp_path / 'portfolio.db'
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'factory', lambda: futures_risk.RiskStore(db))
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'store', None)
    positions = _query('positions')
    funding = _query('funding', start_date='2026-10-01', end_date='2026-10-02')
    expected = futures_risk.RiskStore(db).account_id('hyperliquid', ADDRESS)
    assert {row['account_id'] for row in _rows(positions).values()} == {expected}
    assert _rows(funding, 'funding_payments')['SYNL']['account_id'] == expected
    assert positions['account_key'] == {'field': 'account_id', 'scheme': 'futures_risk_hmac_sha256_v1',
                                        'scope': 'stable_db_salt'}
    # A restart (new store object, same DB) keeps the key; the address never appears in it.
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'store', None)
    assert _rows(_query('positions'))['SYNL']['account_id'] == expected
    assert ADDRESS[2:].lower() not in expected and expected.startswith('acct_') and len(expected) == 37
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM futures_risk_state WHERE key='identity_salt'").fetchone()[0] == 1


def test_the_ledger_observer_still_matches_rows_under_the_stable_key(tmp_path, monkeypatch):
    from portfolio_ledger import observe_futures

    db = tmp_path / 'portfolio.db'
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'factory', lambda: futures_risk.RiskStore(db))
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'store', None)
    funding = _query('funding', start_date='2026-10-01', end_date='2026-10-02')['venues']['hyperliquid']
    observe_futures('funding', 'hyperliquid', _hyperliquid(), funding, db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 1


def test_without_a_db_store_the_key_is_process_local_and_labelled(monkeypatch):
    def unavailable():
        raise OSError('no db')

    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'factory', unavailable)
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'store', None)
    result = _query('positions')
    assert result['account_key']['scope'] == 'process_local'
    assert _rows(result)['SYNL']['account_id'] == futures._PRIVACY.opaque('hyperliquid:' + ADDRESS)


# ── read tools derive the key read-only ───────


def _counting(factory):
    calls = []

    def counted():
        calls.append(1)
        return factory()
    return counted, calls


def _snapshot(db):
    # The DB and its WAL; -shm is SQLite's shared-memory index (reader marks), not data.
    return {path.name: path.read_bytes() for path in sorted(db.parent.glob(db.name + '*'))
            if not path.name.endswith('-shm')}


def test_read_keys_never_create_the_db_schema_or_salt_and_a_failure_is_not_retried(tmp_path, monkeypatch):
    fresh = tmp_path / 'fresh.db'
    factory, calls = _counting(lambda: futures_risk.RiskKeys(fresh))
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'factory', factory)
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'store', None)
    for _ in range(3):
        result = _query('positions')
        assert result['account_key']['scope'] == 'process_local'
    assert len(calls) == 1 and not list(tmp_path.iterdir())  # no file created, one attempt
    # An existing portfolio DB without futures_risk state is left byte-identical.
    other = tmp_path / 'portfolio.db'
    with sqlite3.connect(other) as conn:
        conn.execute('CREATE TABLE ledger_state (key TEXT PRIMARY KEY, value BLOB)')
    before = _snapshot(other)
    with pytest.raises(sqlite3.OperationalError):
        futures_risk.RiskKeys(other)
    assert _snapshot(other) == before


def test_read_keys_equal_the_writer_keys_without_a_write_transaction(tmp_path, monkeypatch):
    import server

    db = tmp_path / 'portfolio.db'
    expected = futures_risk.RiskStore(db).account_id('hyperliquid', ADDRESS)  # the writer path creates the salt
    monkeypatch.setattr(server, '_get_nav_store', lambda: type('Nav', (), {'db_path': db})())
    assert isinstance(server._account_key_store(), futures_risk.RiskKeys)
    assert not isinstance(server._account_key_store(), futures_risk.RiskStore)
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'factory', server._account_key_store)
    monkeypatch.setitem(futures._ACCOUNT_KEYS, 'store', None)
    writer = sqlite3.connect(db, timeout=0)
    try:
        writer.execute('BEGIN IMMEDIATE')  # a held write lock: RiskStore() would wait on it
        before = _snapshot(db)
        result = _query('positions')
        assert _snapshot(db) == before
    finally:
        writer.rollback()
        writer.close()
    assert result['account_key']['scope'] == 'stable_db_salt'
    assert {row['account_id'] for row in _rows(result).values()} == {expected}
