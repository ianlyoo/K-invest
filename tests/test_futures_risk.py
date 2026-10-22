"""Prospective risk captures, using public-response-shaped synthetic data only."""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone

import pytest

STAMP = datetime(2026, 10, 6, 15, 7, tzinfo=timezone.utc)
ADDRESS = '0x' + 'a' * 40


class Binance:
    _api_key = 'synthetic-private-key'

    def __init__(self, positions=None, fail=False):
        self.positions = positions if positions is not None else [
            {'symbol': 'BTCUSDT', 'marginAsset': 'USDT', 'positionAmt': '2',
             'positionSide': 'LONG', 'markPrice': '100', 'notional': '200',
             'unRealizedProfit': '20', 'liquidationPrice': '70', 'marginType': 'cross',
             'positionInitialMargin': '40', 'maintMargin': '1', 'leverage': '5'},
            {'symbol': 'ETHUSDT', 'marginAsset': 'USDT', 'positionAmt': '-1',
             'positionSide': 'SHORT', 'markPrice': '50', 'notional': '-50',
             'unRealizedProfit': '-5', 'liquidationPrice': '80', 'marginType': 'isolated',
             'positionInitialMargin': '10', 'maintMargin': '0.25', 'leverage': '5'}]
        self.fail, self.calls = fail, []

    def request(self, method, path):
        self.calls.append((method, path))
        if self.fail:
            raise RuntimeError('private API key must never be persisted')
        return deepcopy(self.positions if path.endswith('positionRisk') else
                        [{'asset': 'USDT', 'balance': '100', 'crossUnPnl': '20'}])


def state(value, positions):
    return {'marginSummary': {'accountValue': value, 'totalRawUsd': '100',
                             'totalMarginUsed': '30' if positions else '0', 'totalNtlPos': '150' if positions else '0'},
            'crossMarginSummary': {'accountValue': value}, 'crossMaintenanceMarginUsed': '2' if positions else '0',
            'assetPositions': [{'type': 'oneWay', 'position': row} for row in positions],
            'time': 1791299220000}


class Hyperliquid:
    _addresses = [ADDRESS]
    max_pages = 40

    def __init__(self, mode='default', fail_dex=None):
        self.mode, self.fail_dex, self.calls = mode, fail_dex, []
        self.states = {'': state('100', []), 'xyz': state('115', [
            {'coin': 'xyz:SYNB', 'szi': '3', 'positionValue': '150',
             'unrealizedPnl': '15', 'liquidationPx': '30', 'marginUsed': '30',
             'leverage': {'type': 'cross', 'value': 5}}])}

    def info(self, kind, address, **params):
        self.calls.append((kind, params))
        assert address == ADDRESS
        if kind == 'clearinghouseState':
            if params['dex'] == self.fail_dex:
                raise RuntimeError(ADDRESS)
            return deepcopy(self.states[params['dex']])
        return deepcopy({'userAbstraction': self.mode, 'perpDexs': [None, {'name': 'xyz'}],
                         'spotClearinghouseState': {'balances': [
                             {'coin': 'USDC', 'token': 0, 'total': '2150.37', 'hold': '1604.29'},
                             {'coin': 'HYPE', 'token': 150, 'total': '99', 'hold': '0'}]}}[kind])


def collect(tmp_path, binance=None, hyperliquid=None):
    from futures_risk import RiskCollector, RiskStore
    store = RiskStore(tmp_path / 'portfolio.db')
    collector = RiskCollector(store, binance_factory=lambda: binance or Binance(),
                              hyperliquid_factory=lambda: hyperliquid or Hyperliquid(), clock=lambda: STAMP)
    return store, collector.capture()


def value(row, key):
    return row[key]['value']


def test_cross_isolated_inputs_and_leverage_are_persisted(tmp_path):
    store, result = collect(tmp_path)
    account = next(row for row in result['accounts'] if row['venue'] == 'binance')
    assert value(account, 'equity') == '115'
    assert value(account, 'wallet_balance') == '100'
    assert value(account, 'gross_notional') == '250'
    assert value(account, 'net_notional') == '150'
    assert float(value(account, 'effective_leverage')) == pytest.approx(250 / 115)
    assert account['equity']['inputs'] == {'wallet_balance': '100', 'position_unrealized_pnl': ['20', '-5']}
    positions = [row for row in result['positions'] if row['venue'] == 'binance']
    assert [row['margin_mode'] for row in positions] == ['cross', 'isolated']
    assert [row['position_side'] for row in positions] == ['LONG', 'SHORT']
    assert [float(value(row, 'liquidation_distance_pct')) for row in positions] == [30, 60]
    assert value(account, 'margin_used') is None
    assert account['margin_used']['reason_codes'] == ['BINANCE_ACCOUNT_SUMMARY_NOT_ALLOWED']
    with sqlite3.connect(store.path) as connection:
        payload = json.loads(connection.execute('SELECT payload_json FROM futures_risk_accounts WHERE venue=?',
                                                ('binance',)).fetchone()[0])
    assert payload == account


def test_hip3_preserves_scope_and_derives_mark_with_inputs(tmp_path):
    _, result = collect(tmp_path)
    row = next(row for row in result['positions'] if row['venue'] == 'hyperliquid')
    assert row['dex'] == 'xyz' and row['symbol'] == 'xyz:SYNB'
    assert value(row, 'mark_price') == '50'
    assert row['mark_price']['inputs'] == {'positionValue': '150', 'szi': '3'}
    assert float(value(row, 'liquidation_distance_pct')) == 40
    assert {row['dex'] for row in result['accounts'] if row['venue'] == 'hyperliquid'} == {'', 'xyz'}


def test_zero_positions_are_known_zero_but_missing_values_are_null(tmp_path):
    bn = Binance(positions=[])
    _, result = collect(tmp_path, binance=bn)
    row = next(row for row in result['accounts'] if row['venue'] == 'binance')
    assert value(row, 'gross_notional') == value(row, 'net_notional') == '0'
    assert value(row, 'effective_leverage') == '0'
    assert value(row, 'equity') == '100'
    assert value(row, 'margin_used') is None
    assert bn.calls == [('GET', '/fapi/v3/positionRisk'), ('GET', '/fapi/v3/balance')]


@pytest.mark.parametrize('missing', [None, '0', 'bad'])
def test_missing_liquidation_price_is_never_a_zero_distance(tmp_path, missing):
    hl = Hyperliquid()
    hl.states['xyz']['assetPositions'][0]['position']['liquidationPx'] = missing
    _, result = collect(tmp_path, hyperliquid=hl)
    row = next(row for row in result['positions'] if row['venue'] == 'hyperliquid')
    assert value(row, 'liquidation_price') is None
    assert value(row, 'liquidation_distance_pct') is None
    assert row['liquidation_distance_pct']['reason_codes']


def test_unified_wallet_and_dex_equity_are_never_summed_or_relabelled(tmp_path):
    _, result = collect(tmp_path, hyperliquid=Hyperliquid(mode='unifiedAccount'))
    accounts = [row for row in result['accounts'] if row['venue'] == 'hyperliquid']
    shared = next(row for row in accounts if row['scope'] == 'unified_shared_wallet')
    assert value(shared, 'wallet_balance') == '2150.37'
    assert shared['wallet_balance']['inputs']['hold'] == '1604.29'
    assert value(shared, 'equity') is None
    assert value(shared, 'effective_leverage') is None
    assert 'UNIFIED_EQUITY_UNVERIFIED' in shared['equity']['reason_codes']
    assert all(value(row, 'equity') is None for row in accounts)


@pytest.mark.parametrize('down', ['binance', 'hyperliquid_dex'])
def test_failed_venue_or_dex_is_recorded_without_blocking_other_venue(tmp_path, down):
    store, result = collect(tmp_path, binance=Binance(fail=down == 'binance'),
                            hyperliquid=Hyperliquid(fail_dex='xyz' if down == 'hyperliquid_dex' else None))
    assert result['source_status'] == 'partial'
    assert result['exit_code'] == 1
    assert any(row['venue'] != ('binance' if down == 'binance' else 'hyperliquid') for row in result['accounts'])
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT count(*) FROM futures_risk_snapshots').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM futures_risk_venues').fetchone()[0] == 2
        assert 'private API key' not in str(connection.execute('SELECT payload_json FROM futures_risk_venues').fetchall())


def test_only_risk_schema_is_created_and_all_risk_rows_are_append_only(tmp_path):
    from futures_risk import RiskStore
    path = tmp_path / 'portfolio.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sentinel (value TEXT)')
        connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
    store, result = collect(tmp_path)
    second = RiskStore(path)
    assert second.account_id('hyperliquid', ADDRESS) == store.account_id('hyperliquid', ADDRESS)
    assert second.account_id('binance', Binance._api_key) != second.account_id('hyperliquid', Binance._api_key)
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert all(table == 'sentinel' or table.startswith('futures_risk_') for table in tables)
        assert connection.execute('SELECT value FROM sentinel').fetchone()[0] == 'unchanged'
        for table in tables:
            if not table.startswith('futures_risk_'):
                continue
            for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET rowid=rowid'):
                with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                    connection.execute(statement)
    assert ADDRESS not in json.dumps(result) and Binance._api_key not in json.dumps(result)
    assert b'0xaaaaaaaa' not in path.read_bytes() and Binance._api_key.encode() not in path.read_bytes()


def test_cli_failure_persists_attempt_and_emits_only_fixed_metadata(tmp_path, capsys):
    from futures_risk import RiskCollector, RiskStore
    from scripts.snapshot_futures_risk import main
    store = RiskStore(tmp_path / 'portfolio.db')
    collector = RiskCollector(store, binance_factory=lambda: Binance(fail=True),
                              hyperliquid_factory=Hyperliquid, clock=lambda: STAMP)
    assert main([], collector=collector) == 1
    output = capsys.readouterr().out
    assert ADDRESS not in output and Binance._api_key not in output
    assert json.loads(output)['source_status'] == 'partial'
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT count(*) FROM futures_risk_positions').fetchone()[0] == 1


def test_cli_db_failure_is_nonzero_and_masks_exception(capsys):
    from scripts.snapshot_futures_risk import main
    class Broken:
        def capture(self):
            raise RuntimeError(ADDRESS)
    assert main([], collector=Broken()) == 1
    assert json.loads(capsys.readouterr().out) == {'source_status': 'failed', 'reason_codes': ['RISK_SNAPSHOT_NOT_PERSISTED']}


@pytest.mark.parametrize('balance', ['0', '-1', None, 'NaN'])
def test_nonpositive_or_missing_equity_never_produces_infinite_leverage(tmp_path, balance):
    bn = Binance(positions=[])
    request = bn.request
    def changed(method, path):
        data = request(method, path)
        if path.endswith('balance'):
            data[0]['balance'] = balance
        return data
    bn.request = changed
    _, result = collect(tmp_path, binance=bn)
    row = next(row for row in result['accounts'] if row['venue'] == 'binance')
    assert value(row, 'effective_leverage') is None
    assert row['effective_leverage']['reason_codes']


def test_isolated_hl_total_maintenance_and_missing_margin_mode_are_unknown(tmp_path):
    hl = Hyperliquid()
    hl.states['xyz']['assetPositions'][0]['position']['leverage']['type'] = 'isolated'
    bn = Binance()
    del bn.positions[0]['marginType']
    _, result = collect(tmp_path, binance=bn, hyperliquid=hl)
    account = next(row for row in result['accounts'] if row['venue'] == 'hyperliquid' and row['dex'] == 'xyz')
    assert value(account, 'maintenance_margin') is None
    assert value(account, 'cross_maintenance_margin') == '2'
    position = next(row for row in result['positions'] if row['venue'] == 'binance')
    assert position['margin_mode'] is None and 'MARGIN_MODE_UNAVAILABLE' in position['reason_codes']


def test_incomplete_discovery_keeps_default_dex_but_nulls_unified_aggregates(tmp_path):
    hl = Hyperliquid(mode='unifiedAccount')
    read = hl.info
    hl.info = lambda kind, address, **params: None if kind == 'perpDexs' else read(kind, address, **params)
    _, result = collect(tmp_path, hyperliquid=hl)
    assert result['exit_code'] == 1
    shared = next(row for row in result['accounts'] if row['scope'] == 'unified_shared_wallet')
    assert value(shared, 'wallet_balance') == '2150.37'
    assert value(shared, 'gross_notional') is None
    assert any(row['venue'] == 'hyperliquid' and row['scope'] == 'perp_dex' for row in result['accounts'])


def test_hl_budget_records_uncollected_scopes_without_new_endpoints(tmp_path):
    from crypto_futures_accounts import HYPERLIQUID_ALLOWLIST
    hl = Hyperliquid(mode='unifiedAccount')
    hl.max_pages = 3
    _, result = collect(tmp_path, hyperliquid=hl)
    assert result['exit_code'] == 1 and 'PAGE_BUDGET' in result['reason_codes']
    assert len(hl.calls) == 3
    assert {call[0] for call in hl.calls} <= HYPERLIQUID_ALLOWLIST


def test_repeated_capture_appends_instead_of_overwriting_and_stable_ids_survive_reopen(tmp_path):
    store, first = collect(tmp_path)
    _, second = collect(tmp_path)
    assert first['snapshot_id'] != second['snapshot_id']
    assert first['accounts'][0]['account_id'] == second['accounts'][0]['account_id']
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT count(*) FROM futures_risk_snapshots').fetchone()[0] == 2
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_all_factories_down_produces_failed_persisted_attempt_without_exception_text(tmp_path):
    from futures_risk import RiskCollector, RiskStore
    def fail():
        raise RuntimeError(ADDRESS)
    store = RiskStore(tmp_path / 'portfolio.db')
    result = RiskCollector(store, binance_factory=fail, hyperliquid_factory=fail, clock=lambda: STAMP).capture()
    assert result['source_status'] == 'failed' and result['exit_code'] == 1
    assert result['positions'] == result['accounts'] == []
    assert all(row['source_status'] == 'failed' for row in result['venues'])
    assert ADDRESS not in json.dumps(result)


def test_risk_timer_is_hourly_on_every_day_with_bounded_jitter():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    timer = (root / 'deploy/k-invest-risk.timer').read_text()
    assert 'OnCalendar=*-*-* *:00:00 UTC' in timer
    assert 'RandomizedDelaySec=120' in timer and 'Persistent=true' in timer
    service = (root / 'deploy/k-invest-risk.service').read_text()
    assert 'snapshot_futures_risk.py' in service and 'UMask=0077' in service
    assert 'TimeoutStartSec=600' in service
