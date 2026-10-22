"""Review findings 4–7: review counterexamples through real, mocked clients."""
from datetime import timedelta

import httpx
import pytest
from test_collect_ledger import collect_ledger
from test_crypto_flow_coverage import binance, capital, hl, transfer
from test_crypto_flow_intervals import (
    ADDRESS,
    OTHER,
    T0,
    T1,
    bn_transfer,
    hl_flow,
    ms,
    result,
    setup,
)
from test_crypto_nav_capture import (
    capture,
    examples,
    nav,
    quote_client,
    tickers,
    zero_asset,
)

from crypto_flow_evidence import FlowEvidence
from crypto_validation import CryptoValidationError


def persist(store, leaf, rows, reasons=(), failed=False):
    venue = 'hyperliquid' if leaf == 'userNonFundingLedgerUpdates' else 'binance'
    account = store.hl_identity(ADDRESS) if venue == 'hyperliquid' else store.account_identity('binance:primary')
    run = FlowEvidence(store)
    stream = venue + '/' + (account + '/' if venue == 'hyperliquid' else '') + leaf
    run.raw(stream, rows, account, ms(T0), ms(T1), reasons, failed)
    return run


def collect_transfers(payload, *, page_size=None):
    return collect_ledger.flow_history(binance(lambda req: httpx.Response(200, json=payload)),
        'transfer_MAIN_UMFUTURE', ms(T0), ms(T1), page_size=page_size, observed_at_ms=ms(T1))


def test_finding4_empty_positions_cannot_drop_reported_cross_pnl(tmp_path):
    data = examples()
    data['account']['positions'] = []
    path = tmp_path / 'db'
    capture(path, data)
    value = nav(path)['binance']
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_BINANCE_ACCOUNT_POSITION_MISMATCH' in value['reason_codes']


@pytest.mark.parametrize('response', ['spot', 'state'])
@pytest.mark.parametrize('flag', ['error', 'partial', 'hasNext', 'nextCursor', 'complete'])
@pytest.mark.parametrize('empty', [True, False])
def test_finding4_incomplete_hl_envelope_never_proves_nav(tmp_path, response, flag, empty):
    data = examples()
    if response == 'state':
        data['mode'] = 'default'
        if empty:
            data['state']['assetPositions'] = []
            data['state']['marginSummary']['totalNtlPos'] = '0'
    elif empty:
        data['spot']['balances'] = []
    data[response][flag] = False if flag == 'complete' else {'code': 'fixture'} if flag == 'error' else True
    path = tmp_path / 'db'
    capture(path, data)
    value = nav(path)['hyperliquid']
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_INCOMPLETE_ENVELOPE' in value['reason_codes']


@pytest.mark.parametrize('mutation', ['cross_pnl', 'missing_isolated_position', 'missing_isolated_wallet',
                                     'missing_isolated_margin', 'wrong_collateral', 'unknown_account_field'])
def test_finding4_binance_reconciles_each_collateral_and_margin_scope(tmp_path, mutation):
    data = examples()
    account = data['account']
    if mutation == 'cross_pnl':
        account['assets'][0]['unrealizedProfit'] = '9'
    elif mutation == 'missing_isolated_position':
        account['assets'][0]['walletBalance'] = '120'
    elif mutation == 'missing_isolated_wallet':
        del account['positions'][0]['isolatedWallet']
    elif mutation == 'missing_isolated_margin':
        del account['positions'][0]['isolatedMargin']
    elif mutation == 'wrong_collateral':
        account['positions'][0]['symbol'] = 'ADAUSDC'
    else:
        account['assets'][0]['accountAlias'] = 'other'
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['binance']['value_krw_decimal'] is None


def test_finding4_reconciled_cross_and_isolated_equity_count_wallet_once(tmp_path):
    data = examples()
    account = data['account']
    account.update(totalWalletBalance='120', totalUnrealizedProfit='13', totalMarginBalance='133',
                   totalInitialMargin='2', totalPositionInitialMargin='2', totalMaintMargin='0.1')
    account['assets'][0].update(walletBalance='120', unrealizedProfit='13', marginBalance='133',
                               initialMargin='2', positionInitialMargin='2', maintMargin='0.1')
    account['positions'].append(dict(account['positions'][0], symbol='ETHUSDT', positionAmt='1',
        notional='30', unrealizedProfit='3', isolatedWallet='20', isolatedMargin='23'))
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['binance']['by_currency']['USDT']['decimal_value'] == '133'


@pytest.mark.parametrize('response', ['balance', 'positions', 'spot_row', 'state_summary', 'state_position', 'state_leverage'])
def test_finding4_nested_incomplete_rows_never_prove_nav(tmp_path, response):
    data = examples()
    venue = 'binance' if response in ('balance', 'positions') else 'hyperliquid'
    if response in ('balance', 'positions'):
        target = data['account']['assets' if response == 'balance' else 'positions'][0]
    elif response == 'spot_row':
        target = data['spot']['balances'][0]
    else:
        data['mode'] = 'default'
        target = data['state']['marginSummary'] if response == 'state_summary' else data['state']['assetPositions'][0]['position']
        if response == 'state_leverage':
            target = target['leverage']
    target['partial'] = True
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)[venue]['value_krw_decimal'] is None


def test_finding4_valid_empty_wallets_still_prove_zero(tmp_path):
    data = examples()
    data['account'] = {k: '0' for k in data['account']}
    data['account'].update(assets=[zero_asset('USDT')], positions=[])
    data['spot'] = {'balances': []}
    path = tmp_path / 'db'
    capture(path, data)
    assert all(nav(path)[venue]['value_krw_decimal'] == '0' for venue in ('binance', 'hyperliquid'))


def test_finding4_quote_row_with_partial_metadata_is_not_a_mark():
    payload = tickers()
    payload[0]['partial'] = True
    with pytest.raises(CryptoValidationError, match='CRYPTO_INCOMPLETE_ENVELOPE'):
        quote_client(payload).read()


@pytest.mark.parametrize('leaf', ['deposit', 'withdraw', 'transfer_MAIN_UMFUTURE'])
def test_finding4_capital_and_transfer_rows_reject_partial_metadata(leaf):
    row = transfer(1, ms(T0)) if leaf.startswith('transfer_') else capital(leaf)
    row['partial'] = True
    payload = {'total': 1, 'rows': [row]} if leaf.startswith('transfer_') else [row]
    rows, reasons, failed = collect_ledger.flow_history(binance(lambda req: httpx.Response(200, json=payload)),
        leaf, ms(T0 - timedelta(days=1)), ms(T1), observed_at_ms=ms(T1))
    assert failed and not rows and 'CRYPTO_INCOMPLETE_ENVELOPE' in reasons


@pytest.mark.parametrize('location', ['row', 'delta'])
def test_finding4_hl_ledger_rejects_partial_metadata(location):
    row = {'time': ms(T0), 'hash': 'fixture', 'delta': {'type': 'deposit', 'usdc': '1'}}
    (row if location == 'row' else row['delta'])['partial'] = True
    rows, reasons, failed = collect_ledger.hyperliquid_flow_history(
        hl(lambda req: httpx.Response(200, json=[row])), ADDRESS, ms(T0), ms(T1))
    assert failed and not rows and 'CRYPTO_INCOMPLETE_ENVELOPE' in reasons


def hl_delta(typ):
    delta = {'type': typ, 'user': OTHER, 'destination': ADDRESS, 'fee': '0'}
    if typ == 'spotTransfer':
        delta.update(amount='7', token='USDC', usdcValue='7')
    else:
        delta['usdc'] = '7'
    return delta


@pytest.mark.parametrize('typ', ['internalTransfer', 'spotTransfer'])
@pytest.mark.parametrize('fee', ['missing', None, True, '-1', 'NaN'])
def test_finding5_missing_or_invalid_transfer_fee_is_unresolved(tmp_path, typ, fee):
    store = setup(tmp_path)
    delta = hl_delta(typ)
    if fee == 'missing':
        del delta['fee']
    else:
        delta['fee'] = fee
    payload = [{'time': ms(T0 + timedelta(minutes=1)), 'hash': 'fixture-transfer', 'delta': delta}]
    rows, reasons, failed = collect_ledger.hyperliquid_flow_history(
        hl(lambda req: httpx.Response(200, json=payload)), ADDRESS, ms(T0), ms(T1))
    assert failed and not rows and reasons
    persist(store, 'userNonFundingLedgerUpdates', rows, reasons, failed)
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] is None and value['confidence'] == 'unresolved'


@pytest.mark.parametrize('typ', ['internalTransfer', 'spotTransfer'])
def test_finding5_legacy_transfer_without_fee_is_also_unresolved(tmp_path, typ):
    store = setup(tmp_path)
    delta = hl_delta(typ)
    del delta['type'], delta['fee']
    hl_flow(store, typ, **delta)
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] is None
    assert 'CRYPTO_TRANSFER_FEE_UNAVAILABLE' in value['reasons']


@pytest.mark.parametrize('typ', ['internalTransfer', 'spotTransfer'])
def test_finding5_explicit_zero_transfer_fee_preserves_observed_flow(tmp_path, typ):
    store = setup(tmp_path)
    payload = [{'time': ms(T0 + timedelta(minutes=1)), 'hash': 'fixture-transfer', 'delta': hl_delta(typ)}]
    rows, reasons, failed = collect_ledger.hyperliquid_flow_history(
        hl(lambda req: httpx.Response(200, json=payload)), ADDRESS, ms(T0), ms(T1))
    assert not failed and not reasons
    persist(store, 'userNonFundingLedgerUpdates', rows)
    assert result(store, 'hyperliquid')[0]['flow_native'] == '7'


@pytest.mark.parametrize('delta,missing', [
    ({'type': 'withdraw', 'usdc': '7', 'fee': '0', 'nonce': 1}, 'nonce'),
    (hl_delta('spotTransfer'), 'usdcValue'),
    (hl_delta('internalTransfer'), 'user'),
    ({'type': 'subAccountTransfer', 'usdc': '7', 'user': OTHER, 'destination': ADDRESS}, 'destination'),
    ({'type': 'accountClassTransfer', 'usdc': '7', 'toPerp': True}, 'toPerp'),
    ({'type': 'send', 'amount': '7', 'token': 'USDC', 'user': OTHER, 'destination': ADDRESS,
      'sourceDex': 'spot', 'destinationDex': '', 'fee': '0'}, 'fee'),
])
def test_finding5_delta_contract_requires_each_direction_and_fee_input(delta, missing):
    stamp = ms(T0 + timedelta(minutes=1))
    def read(delta):
        page = [{'time': stamp, 'hash': 'fixture', 'delta': delta}]
        return collect_ledger.hyperliquid_flow_history(
            hl(lambda req: httpx.Response(200, json=page)), ADDRESS, ms(T0), ms(T1))
    assert read(delta)[1:] == ([], False)
    assert read({k: v for k, v in delta.items() if k != missing})[2] is True


def test_finding6_duplicate_numeric_identity_drift_rejects_whole_page(tmp_path):
    store = setup(tmp_path)
    row = transfer(1, ms(T0 + timedelta(minutes=1)))
    rows, reasons, failed = collect_transfers({'total': 2, 'rows': [row, dict(row, tranId='01')]})
    assert failed and not rows and 'CRYPTO_DUPLICATE_IDENTITY' in reasons
    persist(store, 'transfer_MAIN_UMFUTURE', rows, reasons, failed)
    assert result(store)[0]['flow_native'] is None


def test_finding6_numeric_identity_drift_is_idempotent_across_captures(tmp_path):
    store = setup(tmp_path)
    for ident in (1, '01', '1'):
        row = transfer(ident, ms(T0 + timedelta(minutes=1)))
        rows, reasons, failed = collect_transfers({'total': 1, 'rows': [row]})
        assert not failed and not reasons and rows[0]['tranId'] == 1
        persist(store, 'transfer_MAIN_UMFUTURE', rows)
    with store.read() as conn:
        from crypto_flow_evidence import read_view
        events, _ = read_view(conn, store.db_path, {})
        assert len([e for e in events if e['source'] == 'binance_transfers']) == 2
        assert conn.execute("SELECT count(*) FROM ledger_events WHERE source='binance_transfers'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == 0
    assert result(store)[0]['flow_native'] == '1'


def test_finding6_duplicate_canonical_identity_across_pages_is_unresolved(tmp_path):
    store = setup(tmp_path)
    def response(req):
        ident = 1 if req.url.params['current'] == '1' else '01'
        return httpx.Response(200, json={'total': 2, 'rows': [transfer(ident, ms(T0 + timedelta(minutes=1)))]})
    rows, reasons, failed = collect_ledger.flow_history(binance(response), 'transfer_MAIN_UMFUTURE',
        ms(T0), ms(T1), page_size=1, observed_at_ms=ms(T1))
    assert failed and reasons and len(rows) == 1
    persist(store, 'transfer_MAIN_UMFUTURE', rows, reasons, failed)
    assert result(store)[0]['flow_native'] is None


def test_finding6_legacy_noncanonical_rows_cannot_double_observed_flow(tmp_path):
    store = setup(tmp_path)
    bn_transfer(store, ident=1, amount='1')
    bn_transfer(store, ident='01', amount='1')
    value = result(store)[0]
    assert value['flow_native'] is None
    assert 'CRYPTO_DUPLICATE_OR_CONFLICTING_LEG' in value['reasons']


@pytest.mark.parametrize('ident', [True, 1.0, '1.0', '+1', ' 1', '-1', '1e0', '', 0, -1])
def test_finding6_unsupported_transfer_id_forms_never_reach_persistence(ident):
    row = transfer(ident, ms(T0 + timedelta(minutes=1)))
    rows, reasons, failed = collect_transfers({'total': 1, 'rows': [row]})
    assert failed and not rows and 'CRYPTO_INVALID_IDENTITY' in reasons


@pytest.mark.parametrize('failure', ['PAGINATION_TOTAL_CHANGED', 'PAGINATION_WINDOW_GAP'])
def test_finding7_later_semantic_failure_invalidates_old_proof_until_repaired(tmp_path, failure):
    store = setup(tmp_path)
    def response(req):
        page = int(req.url.params['current'])
        row = transfer(page, ms(T0 + timedelta(minutes=page)))
        payload = {'total': 2 if page == 1 else 3, 'rows': [row]}
        if failure == 'PAGINATION_WINDOW_GAP' and page == 2:
            payload = {'total': 2, 'rows': []}
        return httpx.Response(200, json=payload)
    rows, reasons, failed = collect_ledger.flow_history(binance(response), 'transfer_MAIN_UMFUTURE',
        ms(T0), ms(T1), page_size=1, observed_at_ms=ms(T1))
    assert failed and reasons == [failure] and len(rows) == 1
    persist(store, 'transfer_MAIN_UMFUTURE', rows, reasons, failed)
    value = result(store)[0]
    assert value['flow_native'] is None and value['confidence'] == 'unresolved'
    assert failure in value['reasons']
    repaired = [transfer(i, ms(T0 + timedelta(minutes=i))) for i in (1, 2, 3)]
    rows, reasons, failed = collect_transfers({'total': 3, 'rows': repaired})
    assert not failed and not reasons
    persist(store, 'transfer_MAIN_UMFUTURE', rows)
    assert result(store)[0]['flow_native'] == '3'


@pytest.mark.parametrize('reason,invalidates', [
    ('PAGINATION_STALLED', True), ('DENSE_TIMESTAMP', True),
    ('HISTORY_SHAPE_UNAVAILABLE', True), ('CRYPTO_INVALID_RESPONSE', True),
    ('SOURCE_STREAM_FAILED', False), ('PAGE_BUDGET', False),
])
def test_finding7_semantic_errors_and_transport_gaps_have_distinct_effects(tmp_path, reason, invalidates):
    store = setup(tmp_path)
    persist(store, 'transfer_MAIN_UMFUTURE', [], [reason], True)
    value = result(store)[0]
    assert (value['flow_native'] is None) == invalidates


@pytest.mark.parametrize('venue', ['binance', 'hyperliquid'])
@pytest.mark.parametrize('malformed', [True, False])
def test_finding7_malformed_success_body_is_semantic_but_http_outage_is_transport(tmp_path, venue, malformed):
    store = setup(tmp_path)
    def handler(req):
        return httpx.Response(200, content=b'[{') if malformed else httpx.Response(503)
    if venue == 'binance':
        leaf = 'transfer_MAIN_UMFUTURE'
        rows, reasons, failed = collect_ledger.flow_history(binance(handler), leaf,
            ms(T0), ms(T1), observed_at_ms=ms(T1))
    else:
        leaf = 'userNonFundingLedgerUpdates'
        rows, reasons, failed = collect_ledger.hyperliquid_flow_history(hl(handler), ADDRESS, ms(T0), ms(T1))
    assert failed
    persist(store, leaf, rows, reasons, failed)
    value = result(store, venue)[0]
    assert (value['flow_native'] is None) == malformed
    assert ('CRYPTO_INVALID_RESPONSE' in value['reasons']) == malformed
