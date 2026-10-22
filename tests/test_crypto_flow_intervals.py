"""Synthetic immutable NAV boundaries, direct flows and coverage evidence."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from crypto_flow_evidence import FlowEvidence, normalize
from crypto_flow_intervals import EVIDENCE_PAD, REQUIRED_STREAMS, crypto_interval_flows
from portfolio_ledger import LedgerStore, normalize_rows
from portfolio_store import NavStore

T0 = datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
ADDRESS = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40


def ms(stamp):
    return int(stamp.timestamp() * 1000)


def identities(store):
    return {'binance': store.account_identity('binance:primary'),
            'hyperliquid': store.hl_identity(ADDRESS)}


def snapshot(store, stamp, ident, *, complete=True, mode='unifiedAccount', fx=True, version=1,
             hl_accounts=None, unbound=False):
    accounts = identities(store)
    navs = {}
    for venue, currency in (('binance', 'USDT'), ('hyperliquid', 'USDC')):
        rows = [accounts[venue]] if venue == 'binance' or hl_accounts is None else hl_accounts
        navs[venue] = {'status': 'derived' if complete else 'unknown', 'value_krw': 140000 if complete else None,
            'reason_codes': [] if complete else ['MISSING_CRYPTO_FX_RATE'],
            'by_currency': {currency: {'value': 100, 'decimal_value': '100'}},
            'accounts': [{'account_id': 'risk_' + str(i), 'ledger_account_identity': None if unbound else account,
                          'asset': currency, 'scope': 'futures_collateral' if venue == 'binance' else (
                              'unified_shared_wallet' if mode == 'unifiedAccount' else 'perp_dex'),
                          'dex': '', 'abstraction': None if venue == 'binance' else mode}
                         for i, account in enumerate(rows)]}
    payload = {'snapshot_id': ident, 'snapshot_date': stamp.date().isoformat(), 'version': version,
               'valuation_at': stamp.isoformat(), 'crypto_nav': {'risk_snapshot_id': 'risk-' + ident, **navs},
               'fx_times': {c: {'rate_to_krw': '1400', 'priced_at': stamp.isoformat(),
                   'observed_at': stamp.isoformat(), 'source': 'upbit_public_ticker', 'reason_codes': []}
                   for c in ('USDT', 'USDC')} if fx else {}}
    nav_store = NavStore(store.db_path)
    with nav_store._connect() as conn:
        nav_store._insert_version(conn, payload)
    return payload


def coverage(store, start=T0 - EVIDENCE_PAD, end=T1 + EVIDENCE_PAD, *, omit=None, status='complete', reasons=(), hl_accounts=None, run='proof'):
    accounts = identities(store)
    writer = FlowEvidence(store)
    for venue, streams in REQUIRED_STREAMS.items():
        roster = [accounts[venue]] if venue == 'binance' or hl_accounts is None else hl_accounts
        for account in roster:
            for leaf in streams:
                if leaf == omit:
                    continue
                writer.save(venue, leaf, [], account, ms(start), ms(end), reasons, status == 'partial')


def append(store, stream, row):
    with store.read() as conn:
        events = normalize_rows(store, conn, stream, row, {})
        strict = normalize(store, conn, stream, row)
    assert all(events)
    if strict:
        FlowEvidence(store).save(stream.split('/')[0], stream.split('/')[-1], strict,
                                strict[0]['account_identity'], ms(T0), ms(T1))
    else:
        store.append_many([(e, None) for e in events])
    return strict or events


def bn_transfer(store, ident=1, stamp=T0 + timedelta(minutes=1), route='MAIN_UMFUTURE', amount='50'):
    return append(store, 'binance/transfer_' + route, {'tranId': ident, 'timestamp': ms(stamp), 'amount': amount,
                                                   'asset': 'USDT', 'type': route, 'status': 'CONFIRMED'})


def hl_flow(store, typ, *, stamp=T0 + timedelta(minutes=1), address=ADDRESS, ident='1', **delta):
    account = store.hl_identity(address)
    return append(store, 'hyperliquid/' + account + '/userNonFundingLedgerUpdates',
                  {'time': ms(stamp), 'hash': ident, 'delta': {'type': typ, **delta}})


def result(store, venue='binance', end='2026-10-14'):
    return [r for r in crypto_interval_flows(store.db_path, '2026-10-13', end) if r['source'] == venue]


def setup(tmp_path, **kwargs):
    store = LedgerStore(tmp_path / 'db')
    snapshot(store, T0, 'start', **kwargs)
    snapshot(store, T1, 'end', **kwargs)
    coverage(store, hl_accounts=kwargs.get('hl_accounts'))
    return store


def test_deposit_then_futures_transfer_and_trade_counts_contribution_once(tmp_path):
    store = setup(tmp_path)
    append(store, 'binance/deposit', {'id': 'chain-deposit', 'insertTime': ms(T0 + timedelta(seconds=10)),
        'amount': '50', 'coin': 'USDT', 'walletType': 0, 'status': 1})
    bn_transfer(store)
    append(store, 'binance/userTrades_ADAUSDT', {'symbol': 'ADAUSDT', 'id': 11,
        'time': ms(T0 + timedelta(minutes=2)), 'side': 'BUY', 'qty': '10', 'price': '1',
        'commission': '1', 'commissionAsset': 'USDT', 'realizedPnl': '5'})
    for ident, kind, amount in ((1, 'FUNDING_FEE', '0.5'), (2, 'COMMISSION', '-1'), (3, 'REALIZED_PNL', '5')):
        append(store, 'binance/income', {'tranId': ident, 'incomeType': kind, 'symbol': 'ADAUSDT',
            'time': ms(T0 + timedelta(minutes=2)), 'income': amount, 'asset': 'USDT'})
    row = result(store)[0]
    assert row['flow_native'] == '50' and row['flow_krw'] == '70000'
    assert row['confidence'] == 'observed' and row['timing'] == 'known'
    assert len([e for e in row['events'] if e['external']]) == 1
    assert row['start_snapshot_id'] == 'start' and row['end_snapshot_id'] == 'end'
    assert row['inputs']['start_snapshot_version'] == 1


def test_exact_snapshot_interval_is_open_at_start_closed_at_end(tmp_path):
    store = setup(tmp_path)
    bn_transfer(store, 1, T0, amount='10')
    bn_transfer(store, 2, T1, amount='20')
    bn_transfer(store, 3, T1 + timedelta(milliseconds=1), amount='30')
    row = result(store)[0]
    assert row['flow_native'] == '20'
    assert row['events'][0]['time'] == '2026-10-14T09:10:00Z'
    assert row['flow_krw'] == '28000'


@pytest.mark.parametrize('route,expected', [('FUNDING_UMFUTURE', '50'), ('UMFUTURE_FUNDING', '-50'),
                                         ('UMFUTURE_MAIN', '-50'), ('MAIN_FUNDING', '0')])
def test_only_futures_wallet_boundary_crossings_are_flows(tmp_path, route, expected):
    store = setup(tmp_path)
    bn_transfer(store, route=route)
    assert result(store)[0]['flow_native'] == expected


def test_missing_coverage_through_actual_t1_is_null_not_zero(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    snapshot(store, T0, 'start')
    snapshot(store, T1, 'end')
    coverage(store, end=T1 - timedelta(milliseconds=1))
    row = result(store)[0]
    assert row['flow_native'] is None and row['confidence'] == 'unresolved'
    assert 'CRYPTO_FLOW_COVERAGE_INCOMPLETE' in row['reasons']


def test_verified_zero_and_retention_warning_on_old_window(tmp_path):
    store = setup(tmp_path)
    coverage(store, start=T0 - timedelta(days=365), end=T0 - timedelta(days=90), status='limited',
             reasons=['TRANSFER_SIX_MONTH_RETENTION'], run='old')
    row = result(store)[0]
    assert row['flow_native'] == row['flow_krw'] == '0'
    assert row['reasons'] == [] and row['events'] == []


def test_incomplete_nav_day_bridges_only_with_complete_multiday_flow_proof(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    snapshot(store, T0, 'start')
    snapshot(store, T1, 'missing', complete=False)
    snapshot(store, T1 + timedelta(days=1), 'end')
    coverage(store, end=T1 + timedelta(days=1))
    rows = result(store, end='2026-10-15')
    assert len(rows) == 1 and rows[0]['start_snapshot_id'] == 'start' and rows[0]['end_snapshot_id'] == 'end'


def test_missing_event_time_fx_keeps_observed_native_flow_but_null_krw(tmp_path):
    store = setup(tmp_path, fx=False)  # no capture marks either: nothing to interpolate between
    bn_transfer(store, stamp=T0 + timedelta(hours=2))
    row = result(store)[0]
    assert row['flow_native'] == '50' and row['flow_krw'] is None
    assert row['conversion_status'] == 'unresolved'
    assert row['reasons'] == ['CRYPTO_EVENT_FX_UNAVAILABLE']


@pytest.mark.parametrize('mode,expected', [('unifiedAccount', '0'), ('default', '12')])
def test_hl_account_class_transfer_uses_snapshot_wallet_cut(tmp_path, mode, expected):
    store = setup(tmp_path, mode=mode)
    hl_flow(store, 'accountClassTransfer', usdc='12', toPerp=True)
    row = result(store, 'hyperliquid')[0]
    assert row['flow_native'] == expected and row['reasons'] == []


@pytest.mark.parametrize('typ', ['internalTransfer', 'spotTransfer', 'subAccountTransfer', 'send'])
@pytest.mark.parametrize('incoming', [True, False])
def test_hl_direction_and_wallet_are_bound_to_observed_account(tmp_path, typ, incoming):
    store = setup(tmp_path)
    delta = {'user': OTHER if incoming else ADDRESS, 'destination': ADDRESS if incoming else OTHER}
    if typ in ('internalTransfer', 'spotTransfer', 'send'):
        delta['fee'] = '0'
    if typ in ('spotTransfer', 'send'):
        delta.update(amount='7', token='USDC')
    else:
        delta['usdc'] = '7'
    if typ == 'send':
        delta.update(sourceDex='spot', destinationDex='')
    hl_flow(store, typ, **delta)
    row = result(store, 'hyperliquid')[0]
    assert row['flow_native'] == ('7' if incoming else '-7')


def test_hl_transfer_between_included_accounts_is_not_external(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    roster = [store.hl_identity(ADDRESS), store.hl_identity(OTHER)]
    snapshot(store, T0, 'start', hl_accounts=roster)
    snapshot(store, T1, 'end', hl_accounts=roster)
    coverage(store, hl_accounts=roster)
    for address in (ADDRESS, OTHER):
        hl_flow(store, 'internalTransfer', address=address, user=ADDRESS, destination=OTHER, usdc='7', fee='0')
    row = result(store, 'hyperliquid')[0]
    assert row['flow_native'] == '0'
    assert len(row['events']) == 1 and row['events'][0]['external'] is False


def test_hl_deposit_and_zero_fee_withdrawal_are_direct_flows(tmp_path):
    store = setup(tmp_path)
    hl_flow(store, 'deposit', usdc='10', ident='deposit')
    hl_flow(store, 'withdraw', usdc='3', fee='0', ident='withdraw')
    assert result(store, 'hyperliquid')[0]['flow_native'] == '7'


def test_hl_nonzero_withdrawal_fee_never_masquerades_as_capital(tmp_path):
    store = setup(tmp_path)
    hl_flow(store, 'withdraw', usdc='3', fee='1')
    row = result(store, 'hyperliquid')[0]
    assert row['flow_native'] is None
    assert 'CRYPTO_WITHDRAWAL_FEE_BASIS_UNVERIFIED' in row['reasons']
    assert [e for e in row['events'] if e['kind'] == 'fee'][0]['external'] is False


def test_binance_withdrawal_debits_amount_plus_fee_outside_the_futures_cut(tmp_path):
    # Synthetic: 100 USDT leaves futures, then 98.5 + fee 1.5 is withdrawn on chain from MAIN. The
    # transfer is the exact crossing; the withdrawal's amount + fee debit is not a second one.
    store = setup(tmp_path)
    bn_transfer(store, route='UMFUTURE_MAIN', amount='100')
    strict = append(store, 'binance/withdraw', {'id': 'chain-withdraw', 'amount': '98.5', 'transactionFee': '1.5',
        'coin': 'USDT', 'walletType': 0, 'status': 6,
        'applyTime': (T0 + timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')})
    assert (strict[0]['fee'], strict[0]['fee_asset'], strict[0]['fee_basis']) == ('1.5', 'USDT', 'separate_debit')
    row = result(store)[0]
    assert row['flow_native'] == '-100' and row['flow_krw'] == '-140000'
    assert row['confidence'] == 'observed' and row['reasons'] == []
    withdrawal = [e for e in row['events'] if e['kind'] == 'withdrawal']
    assert [(e['amount_native'], e['external']) for e in withdrawal] == [('0', False)]


def test_hl_withdrawal_fee_basis_is_unchanged_by_the_binance_basis(tmp_path):
    # The same numbers on Hyperliquid stay a fee-banded estimate of -amount, never -(amount + fee).
    store = setup(tmp_path)
    hl_flow(store, 'withdraw', usdc='99.19808', fee='0.2')
    row = result(store, 'hyperliquid')[0]
    assert row['flow_native'] == '-99.19808' and row['confidence'] == 'conditional'
    assert row['reasons'] == ['CRYPTO_WITHDRAWAL_FEE_BASIS_ESTIMATED']
    assert [e['fee_band_native'] for e in row['events'] if e['kind'] == 'withdrawal'] == ['0.2']


def test_unknown_delta_evidence_and_later_complete_fetch(tmp_path):
    store = setup(tmp_path)
    coverage(store, status='partial', reasons=['UNKNOWN_HYPERLIQUID_DELTA_TYPE'], run='bad')
    row = result(store, 'hyperliquid')[0]
    assert row['flow_native'] is None and 'UNKNOWN_HYPERLIQUID_DELTA_TYPE' in row['reasons']
    coverage(store, run='repaired')
    assert result(store, 'hyperliquid')[0]['flow_native'] == '0'


def test_scope_change_or_missing_identity_never_looks_like_empty_flow(tmp_path):
    store = setup(tmp_path, unbound=True)
    assert result(store)[0]['flow_native'] is None
    assert 'CRYPTO_ACCOUNT_SCOPE_UNBOUND' in result(store)[0]['reasons']


def test_duplicate_economic_leg_and_content_conflicts_are_unresolved(tmp_path):
    store = setup(tmp_path)
    legs = bn_transfer(store)
    duplicate = {**legs[0], 'source_record_id': 'another-source-id'}
    FlowEvidence(store).save('binance', 'transfer_MAIN_UMFUTURE', [duplicate],
                            duplicate['account_identity'], ms(T0), ms(T1))
    assert 'CRYPTO_DUPLICATE_OR_CONFLICTING_LEG' in result(store)[0]['reasons']
    assert result(store)[0]['flow_native'] is None


def test_query_does_not_modify_snapshot_or_ledger_and_pins_versions(tmp_path):
    store = setup(tmp_path)
    bn_transfer(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    first, second = result(store), result(store)
    assert first == second
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    snapshot(store, T1, 'replacement', version=2)
    after = result(store)[0]
    assert after['end_snapshot_id'] == 'replacement' and after['inputs']['end_snapshot_version'] == 2
    with sqlite3.connect(store.db_path) as conn:
        original = json.loads(conn.execute("SELECT payload_json FROM nav_snapshot_versions WHERE snapshot_id='end'").fetchone()[0])
    assert original['version'] == 1


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'absent'
    assert crypto_interval_flows(path, '2026-10-13', '2026-10-14') == []
    assert not path.exists()
