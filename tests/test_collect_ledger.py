"""Daily overlap, all-source isolation and private evidence on synthetic clients."""
import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_ledger import LedgerStore
from portfolio_model import toss_transactions
from portfolio_sources import Collection

SPEC = importlib.util.spec_from_file_location('collect_ledger', Path(__file__).parents[1] / 'scripts/collect_ledger.py')
collect_ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect_ledger)
ADDRESS = '0x' + 'a' * 40


class Equity:
    def __init__(self, failed=False):
        self.failed = failed
    def collect_transactions(self, start, end):
        tx = toss_transactions([{'orderId': 'order-1', 'orderedAt': '2026-07-12T09:00:00+09:00',
                                 'side': 'BUY', 'symbol': 'AAPL', 'currency': 'USD', 'execution': {
                                     'filledQuantity': '1', 'averageFilledPrice': '2',
                                     'filledAt': '2026-07-12T10:01:02+09:00'}}], 'toss:primary')
        metadata = {'toss:primary:orders': {'complete': True}}
        if self.failed:
            metadata['kis:domestic'] = {'complete': False, 'error_code': 'KIS_UNAVAILABLE'}
        return Collection(transactions=tx, source_metadata=metadata)


class Binance:
    max_pages = 40
    def request(self, method, path, params=None):
        assert method == 'GET'
        assert ('GET', path) in __import__('crypto_futures_accounts').BINANCE_ALLOWLIST
        if path.startswith('/sapi/'):
            return {'rows': [], 'total': 0} if path.endswith('/transfer') else []
        if path.endswith('/income'):
            assert 'incomeType' not in params
            return [{'tranId': i + 1, 'incomeType': kind, 'income': '-0.1', 'asset': 'USDT',
                     'symbol': 'BTCUSDT', 'time': params['startTime']} for i, kind in enumerate(
                         ('FUNDING_FEE', 'COMMISSION', 'TRANSFER'))]
        if path.endswith('positionRisk'):
            return [{'symbol': 'ETHUSDT'}]
        return [{'symbol': 'BTCUSDT', 'id': 7, 'side': 'BUY', 'time': params['startTime'], 'qty': '1', 'price': '2'}] if params['symbol'] == 'BTCUSDT' else []


class Hyperliquid:
    max_pages = 40
    _addresses = [ADDRESS]
    def info(self, kind, address, **params):
        assert address == ADDRESS
        assert kind in {'userFillsByTime', 'userFunding', 'userNonFundingLedgerUpdates'}
        if kind == 'userNonFundingLedgerUpdates':
            return []
        if kind == 'userFunding':
            return [{'hash': 'funding-1', 'time': params['startTime'], 'delta': {'coin': 'BTC', 'usdc': '0.1'}}]
        return [{'tid': 9, 'coin': 'BTC', 'side': 'B', 'sz': '1', 'px': '2', 'time': params['startTime']}]


def test_weekend_collection_keeps_other_sources_when_kis_fails_and_overlap_dedups(tmp_path):
    store = LedgerStore(tmp_path / 'portfolio.db')
    args = dict(equity=Equity(True), binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    first = collect_ledger.collect(store, '2026-07-12', '2026-07-12', **args)
    assert first['failed'] is True
    assert sum(r['new'] for r in first['streams'].values()) == 7
    second = collect_ledger.collect(store, '2026-07-12', '2026-07-12', **args)
    assert sum(r['new'] for r in second['streams'].values()) == 0
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 7
        assert conn.execute("SELECT execution_time_utc FROM ledger_events WHERE source='toss_transactions'").fetchone()[0] == '2026-07-12T01:01:02.000+00:00'
        assert conn.execute("SELECT source_timezone FROM ledger_events WHERE source='toss_transactions'").fetchone()[0] == 'UTC+09:00'
        assert conn.execute("SELECT fee FROM ledger_events WHERE source='toss_transactions'").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM ledger_coverage WHERE status='partial'").fetchone()[0] >= 1
    for file in (tmp_path / 'ledger-raw').rglob('*.json'):
        assert file.stat().st_mode & 0o777 == 0o600
        assert ADDRESS not in file.read_text()
        assert 'toss:primary' not in file.read_text()
        assert hashlib.sha256(file.read_bytes()).hexdigest()


def test_binance_factory_failure_does_not_block_hyperliquid_or_equity(tmp_path):
    def broken():
        raise ValueError('private exception must never be printed')
    store = LedgerStore(tmp_path / 'portfolio.db')
    result = collect_ledger.collect(store, '2026-07-12', '2026-07-12', equity=Equity(), binance_factory=broken, hyperliquid_factory=Hyperliquid)
    assert result['failed'] is True
    assert sum(r['new'] for r in result['streams'].values()) == 3
    assert 'private exception' not in str(result)


def test_timer_runs_daily_with_explicit_timezone_outside_the_snapshot_window():
    text = (Path(__file__).parents[1] / 'deploy/k-invest-ledger.timer').read_text()
    assert 'OnCalendar=*-*-* 04:10:00 Asia/Seoul' in text
    assert 'Mon..Fri' not in text


def test_collector_main_nonzero_on_source_failure_and_counts_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(collect_ledger.logging, 'disable', lambda _: None)
    monkeypatch.setattr(collect_ledger.os, 'umask', lambda _: None)
    real_collect = collect_ledger.collect
    monkeypatch.setattr(collect_ledger, 'collect', lambda store, start, end, **kwargs:
                        real_collect(store, '2026-07-12', '2026-07-12', equity=Equity(True),
                                     binance_factory=Binance, hyperliquid_factory=Hyperliquid))
    assert collect_ledger.main(['--db', str(tmp_path / 'portfolio.db')]) == 1
    output = capsys.readouterr().out
    assert ADDRESS not in output and 'order-1' not in output and 'AAPL' not in output


@pytest.mark.parametrize('end', ['2026-10-10', '2026-10-02'])
def test_binance_history_caps_future_tail(tmp_path, monkeypatch, end):
    observed = datetime(2026, 10, 9, 19, 12, tzinfo=timezone.utc)  # 04:12 KST
    observation_ms = int(observed.timestamp() * 1000)
    requested_end = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000) + 86400000 - 1
    expected_end = min(requested_end, observation_ms)
    clock = [observation_ms]
    calls = []
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or timezone.utc)
    monkeypatch.setattr(collect_ledger, 'datetime', Clock)
    class Client(Binance):
        def _clock(self):
            return clock[0] / 1000
        def request(self, method, path, params=None):
            if path.endswith('positionRisk'):
                return [{'symbol': 'BTCUSDT'}]
            calls.append((path, dict(params)))
            clock[0] += 1000  # Later requests must keep the original cap.
            if params['endTime'] > observation_ms:
                raise ValueError('synthetic future endTime rejection')
            if path.startswith('/sapi/'):
                return {'rows': [], 'total': 0} if path.endswith('/transfer') else []
            ident = params['startTime']
            if path.endswith('/income'):
                return [dict(tranId=ident, incomeType='FUNDING_FEE', income='1', asset='USDT',
                             symbol='BTCUSDT', time=ident)]
            return [dict(id=ident, symbol='BTCUSDT', side='BUY', time=ident, qty='1', price='2')]
    class HL(Hyperliquid):
        def info(self, kind, address, **params):
            rows = super().info(kind, address, **params)
            for row in rows:
                if 'tid' in row:
                    row['tid'] = params['startTime']  # Distinct fills across windows.
            return rows
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-10-01', end, equity=Equity(),
                                   binance_factory=Client, hyperliquid_factory=HL)
    assert calls and all(params['startTime'] <= params['endTime'] <= expected_end for _, params in calls)
    assert {path for path, _ in calls} == {'/fapi/v1/income', '/fapi/v1/userTrades',
        '/sapi/v1/asset/transfer', '/sapi/v1/capital/deposit/hisrec', '/sapi/v1/capital/withdraw/history'}
    routes = {params['type'] for path, params in calls if path.endswith('/transfer')}
    assert routes == set(__import__('crypto_futures_accounts').BINANCE_WALLET_ROUTES)
    assert not result['failed']
    with store.read() as conn:
        coverage = list(conn.execute("SELECT stream,end_time FROM ledger_coverage WHERE source='binance'"))
        assert len(coverage) == 10
        assert {row['end_time'] for row in coverage} == {str(expected_end)}
        assert conn.execute("SELECT count(*) FROM ledger_events WHERE source='binance_fills'").fetchone()[0] == (2 if end == '2026-10-10' else 1)


@pytest.mark.parametrize('roster', ['complete', 'position_failed', 'empty', 'trade_failed'])
def test_binance_symbol_discovery_reason_is_conditional(tmp_path, roster):
    class Client(Binance):
        def request(self, method, path, params=None):
            if path.endswith('/income') and roster == 'empty':
                return []
            if path.endswith('positionRisk'):
                if roster == 'position_failed':
                    raise ValueError('synthetic roster failure')
                return [] if roster == 'empty' else [{'symbol': 'BTCUSDT'}]
            if path.endswith('/userTrades') and roster == 'trade_failed':
                raise ValueError('synthetic trade failure')
            return super().request(method, path, params)
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-07-12', '2026-07-12', equity=Equity(),
                                   binance_factory=Client, hyperliquid_factory=Hyperliquid)
    with store.read() as conn:
        coverage = list(conn.execute("SELECT stream,status,reason_codes_json FROM ledger_coverage WHERE source='binance'"))
    trades = [row for row in coverage if row['stream'].startswith('binance/userTrades_')]
    if roster == 'empty':
        assert not trades
        empty = next(row for row in coverage if row['stream'] == 'binance/roster')
        assert 'SYMBOL_ROSTER_EMPTY_OR_UNAVAILABLE' in empty['reason_codes_json']
        assert 'SYMBOL_DISCOVERY_INCOMPLETE' in empty['reason_codes_json']
        assert empty['status'] == 'partial'
    else:
        assert trades
        assert all(('SYMBOL_DISCOVERY_INCOMPLETE' in row['reason_codes_json']) == (roster == 'position_failed') for row in trades)
        assert {row['status'] for row in trades} == {dict(complete='complete', position_failed='limited', trade_failed='partial')[roster]}
        assert all(('SOURCE_STREAM_FAILED' in row['reason_codes_json']) == (roster == 'trade_failed') for row in trades)
    assert result['failed'] == (roster != 'complete')


def test_a_preserved_conflict_alone_does_not_fail_the_run(tmp_path):
    """C9: a changed upstream value is kept in ledger_conflicts with partial coverage; the run itself succeeded."""
    class Changed(Binance):
        def request(self, method, path, params=None):
            rows = super().request(method, path, params)
            return [{**r, 'income': '-0.2'} for r in rows] if path.endswith('/income') else rows
    store = LedgerStore(tmp_path / 'ledger.db')
    args = dict(equity=Equity(), hyperliquid_factory=Hyperliquid)
    assert collect_ledger.collect(store, '2026-07-12', '2026-07-12', binance_factory=Binance, **args)['failed'] is False
    result = collect_ledger.collect(store, '2026-07-12', '2026-07-12', binance_factory=Changed, **args)
    assert result['failed'] is False and result['conflicts'] >= 2
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == result['conflicts']
        row = conn.execute("SELECT status,reason_codes_json FROM ledger_coverage WHERE stream='binance/income' ORDER BY rowid DESC").fetchone()
        assert row['status'] == 'partial' and 'LEDGER_CONTENT_CONFLICT' in row['reason_codes_json']


def test_binance_future_only_window_is_partial_without_requests_or_zero_proof(tmp_path, monkeypatch):
    observed = datetime(2026, 10, 9, 19, 12, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed
    monkeypatch.setattr(collect_ledger, 'datetime', Clock)
    calls = []
    class Client(Binance):
        def request(self, *args, **kwargs):
            calls.append(args)
            raise ValueError('future-only history must not be requested')
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-10-10', '2026-10-10', equity=Equity(),
                                   binance_factory=Client, hyperliquid_factory=Hyperliquid)
    assert result['failed'] and not calls
    with store.read() as conn:
        row = conn.execute("SELECT status,end_time,verified_zero FROM ledger_coverage WHERE stream='binance/collection'").fetchone()
        assert row['status'] == 'partial'
        assert row['end_time'] == str(int(observed.timestamp() * 1000))
        assert row['verified_zero'] == 0
