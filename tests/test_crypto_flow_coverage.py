"""History proof through signed/read-only clients with mocked provider examples."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_collect_ledger import collect_ledger

from crypto_futures_accounts import BinanceAccountClient, HyperliquidAccountClient
from crypto_validation import CryptoValidationError, binance_flow_page
from portfolio_ledger import LedgerStore, normalize_row

MS = 1791882600000
ADDRESS = '0x' + 'a' * 40


def transfer(ident=1, stamp=MS, route='MAIN_UMFUTURE'):
    return {'asset': 'USDT', 'amount': '1', 'type': route, 'status': 'CONFIRMED',
            'tranId': ident, 'timestamp': stamp}


def capital(leaf, ident='example'):
    base = {'id': ident, 'amount': '8.91000000', 'coin': 'USDT', 'network': 'ETH',
            'walletType': 0, 'txId': '0x' + 'b' * 64}
    if leaf == 'deposit':
        return {**base, 'insertTime': MS, 'status': 1, 'confirmTimes': '12/12'}
    return {**base, 'applyTime': datetime.fromtimestamp(MS / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'completeTime': datetime.fromtimestamp(MS / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'status': 6, 'transactionFee': '0.004', 'transferType': 0}


def binance(handler, max_pages=40):
    return BinanceAccountClient('synthetic-key', rsa.generate_private_key(public_exponent=65537, key_size=2048),
        http=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: MS / 1000,
        sleeper=lambda _: None, max_pages=max_pages)


def hl(handler, max_pages=40):
    return HyperliquidAccountClient([ADDRESS], http=httpx.Client(transport=httpx.MockTransport(handler)),
                                    sleeper=lambda _: None, max_pages=max_pages)


@pytest.mark.parametrize('leaf', ['deposit', 'withdraw', 'transfer_MAIN_UMFUTURE'])
def test_public_example_history_and_numeric_type_drift(leaf):
    row = transfer() if leaf.startswith('transfer_') else capital(leaf)
    row['amount'] = 1.25  # Explicit string/number drift remains Decimal-safe.
    def handler(request):
        assert request.method == 'GET' and request.url.host == 'api.binance.com'
        return httpx.Response(200, json={'total': 1, 'rows': [row]} if leaf.startswith('transfer_') else [row])
    rows, reasons, failed = collect_ledger.flow_history(binance(handler), leaf, MS, MS + 1000, observed_at_ms=MS + 1000)
    assert len(rows) == 1 and reasons == [] and not failed


# Binance's published "Withdraw History (USER_DATA)" response example for GET
# /sapi/v1/capital/withdraw/history, verbatim from
# https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/capital#withdraw-history
# (accessed 2026-10-17; the page carries no publication date).
BINANCE_WITHDRAW_EXAMPLE = [{
    'id': 'b6ae22b3aa844210a7041aee7589627c', 'amount': '8.91000000', 'transactionFee': '0.004', 'coin': 'USDT',
    'status': 6, 'address': '0x94df8b352de7f46f64b01d3666bf6e936e44ce60',
    'txId': '0xb5ef8c13b968a406cc62a93a8bd80f9e9a906ef1b3fcf20a2e48573c17659268',
    'applyTime': '2019-10-12 11:12:02', 'network': 'ETH', 'transferType': 0, 'withdrawOrderId': 'WITHDRAWtest123',
    'info': 'The address is not valid. Please confirm with the recipient', 'confirmNo': 3, 'walletType': 1,
    'txKey': '', 'completeTime': '2023-03-23 16:52:41'}]
EXAMPLE_APPLY_MS = 1570878722000  # 2019-10-12 11:12:02 UTC


@pytest.mark.parametrize('mutation, outcome', [
    ({}, '8.914'),                                      # the published string fee
    ({'transactionFee': 0.004}, '8.914'),               # number drift stays Decimal-exact
    ({'transactionFee': '0'}, '8.91'),                  # no fee: the debit is the amount
    ({'transactionFee': None}, 'CRYPTO_INVALID_NUMBER'),  # missing
    ({'transactionFee': '-0.004'}, 'CRYPTO_INVALID_RESPONSE'),  # negative
    ({'feeAsset': 'BNB'}, 'CRYPTO_INCOMPLETE_ENVELOPE'),  # another fee asset is no documented field
])
def test_published_withdraw_example_debits_amount_plus_fee(tmp_path, mutation, outcome):
    """The strict validator, then normalization, on Binance's published withdraw-history example (cited above).

    2.12.12: `amount` is net and the fee is debited on top (ledger_flows.BINANCE_WITHDRAWAL_FEE_BASIS), so the
    wallet debit is amount + transactionFee, in `coin`. A missing, negative or foreign-asset fee rejects the page.
    """
    row = {**BINANCE_WITHDRAW_EXAMPLE[0], **mutation}
    if mutation.get('transactionFee', '') is None:
        del row['transactionFee']
    params = {'startTime': EXAMPLE_APPLY_MS - 1000, 'endTime': EXAMPLE_APPLY_MS + 1000, 'limit': 1000}
    if outcome.startswith('CRYPTO_'):
        with pytest.raises(CryptoValidationError) as error:
            binance_flow_page([row], 'withdraw', params)
        assert error.value.code == outcome
        return
    page = binance_flow_page([row], 'withdraw', params)
    store = LedgerStore(tmp_path / 'db')
    with store.read() as conn:
        event = normalize_row(store, conn, 'binance/withdraw', page[0], {})
    assert (event['source'], event['kind'], event['wallet'], event['direction'], event['source_status']) == (
        'binance_withdraw', 'withdrawal', 'FUNDING', 'out', '6')
    assert (event['currency'], event['fee_asset'], event['fee_basis']) == ('USDT', 'USDT', 'separate_debit')
    assert event['execution_time_utc'] == '2019-10-12T11:12:02+00:00' and event['timestamp_precision'] == 'second'
    assert Decimal(str(event['amount'])) + Decimal(str(event['fee'])) == Decimal(outcome)


@pytest.mark.parametrize('mutation', ['short_page', 'duplicate', 'wrong_route', 'missing_amount', 'seconds_time',
                                     'unknown_status', 'total_changed', 'duplicate_next_page', 'truncated_json'])
def test_transfer_bad_page_sets_cannot_be_complete(mutation):
    def handler(request):
        page = int(request.url.params['current'])
        row = transfer(page)
        payload = {'total': 2, 'rows': [row]}
        if mutation == 'short_page':
            payload['rows'] = []
        elif mutation == 'duplicate':
            payload = {'total': 2, 'rows': [row, deepcopy(row)]}
        elif mutation == 'wrong_route':
            row['type'] = 'UMFUTURE_MAIN'
        elif mutation == 'missing_amount':
            del row['amount']
        elif mutation == 'seconds_time':
            row['timestamp'] //= 1000
        elif mutation == 'unknown_status':
            row['status'] = 'newState'
        elif mutation == 'total_changed' and page == 2:
            payload['total'] = 3
        elif mutation == 'duplicate_next_page' and page == 2:
            row['tranId'] = 1
            row['amount'] = '2'
        elif mutation == 'truncated_json':
            return httpx.Response(200, content=b'{"total":2,"rows":[')
        return httpx.Response(200, json=payload)
    _, reasons, failed = collect_ledger.flow_history(binance(handler), 'transfer_MAIN_UMFUTURE', MS, MS + 1000,
        page_size=2 if mutation == 'duplicate' else 1, observed_at_ms=MS + 1000)
    assert failed and reasons


def test_reordered_complete_transfer_pages_are_accepted():
    def handler(request):
        page = int(request.url.params['current'])
        return httpx.Response(200, json={'total': 2, 'rows': [transfer(3 - page)]})
    rows, reasons, failed = collect_ledger.flow_history(binance(handler), 'transfer_MAIN_UMFUTURE', MS, MS + 1000,
                                                       page_size=1, observed_at_ms=MS + 1000)
    assert [row['tranId'] for row in rows] == [2, 1] and not reasons and not failed


@pytest.mark.parametrize('failed_fetch', [False, True])
def test_verified_empty_windows_are_distinct_from_failed_fetch(tmp_path, failed_fetch):
    client = binance(lambda request: httpx.Response(503 if failed_fetch else 200, json={'total': 0}))
    rows, reasons, failed = collect_ledger.flow_history(client, 'transfer_MAIN_UMFUTURE', MS, MS + 1000, observed_at_ms=MS + 1000)
    store = LedgerStore(tmp_path / 'db')
    account = store.account_identity('binance:primary')
    run = collect_ledger.CollectionRun(store)
    run.raw('binance/transfer_MAIN_UMFUTURE', rows, account, MS, MS + 1000, reasons, failed)
    with store.read() as conn:
        proof = conn.execute('SELECT status, verified_zero FROM ledger_coverage').fetchone()
    assert tuple(proof) == ('partial', 0) if failed_fetch else tuple(proof) == ('complete', 1)


def test_retention_advisory_does_not_poison_a_complete_recent_window(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    run = collect_ledger.CollectionRun(store)
    account = store.account_identity('binance:primary')
    client = binance(lambda request: httpx.Response(200, json={'total': 0}))
    lo = MS - 366 * 86400000
    windows = collect_ledger.flow_windows('transfer_MAIN_UMFUTURE', lo, MS, MS)
    assert len(windows) == 2 and windows[0][1] + 1 == windows[1][0]
    for a, b in windows:
        rows, reasons, failed = collect_ledger.flow_history(client, 'transfer_MAIN_UMFUTURE', a, b, observed_at_ms=MS)
        run.raw('binance/transfer_MAIN_UMFUTURE', rows, account, a, b, reasons, failed)
    with store.read() as conn:
        proofs = list(conn.execute('SELECT status,verified_zero,reason_codes_json FROM ledger_coverage ORDER BY start_time'))
    assert proofs[0]['status'] == 'limited'
    assert 'TRANSFER_SIX_MONTH_RETENTION' in json.loads(proofs[0]['reason_codes_json'])
    assert tuple(proofs[1]) == ('complete', 1, '[]')


def test_hl_empty_success_and_failed_read_are_not_conflated():
    good = hl(lambda request: httpx.Response(200, json=[]))
    bad = hl(lambda request: httpx.Response(503, json={'error': 'fixture'}))
    assert collect_ledger.hyperliquid_flow_history(good, ADDRESS, MS, MS + 1000) == ([], [], False)
    rows, reasons, failed = collect_ledger.hyperliquid_flow_history(bad, ADDRESS, MS, MS + 1000)
    assert rows == [] and failed and reasons == ['SOURCE_STREAM_FAILED']


@pytest.mark.parametrize('mutation', ['unknown_delta', 'missing_fee', 'foreign_user', 'duplicate', 'seconds_time', 'mixed_currency'])
def test_hl_bad_ledger_pages_remain_unresolved(mutation):
    row = {'hash': '0x' + 'b' * 64, 'time': MS,
           'delta': {'type': 'withdraw', 'usdc': '2', 'fee': '0', 'nonce': MS}}
    payload = [row]
    if mutation == 'unknown_delta':
        row['delta']['type'] = 'futureDelta'
    elif mutation == 'missing_fee':
        del row['delta']['fee']
    elif mutation == 'foreign_user':
        row['delta'].update(type='internalTransfer', user='0x' + 'c' * 40, destination='0x' + 'd' * 40)
    elif mutation == 'duplicate':
        payload.append(deepcopy(row))
    elif mutation == 'mixed_currency':
        row['delta']['amount'] = '9'
    else:
        row['time'] //= 1000
    _, reasons, failed = collect_ledger.hyperliquid_flow_history(
        hl(lambda request: httpx.Response(200, json=payload)), ADDRESS, MS, MS + 1000)
    assert failed and reasons


def test_hl_full_pages_are_bisected_without_duplicate_legs():
    records = [{'hash': str(i), 'time': MS + i, 'delta': {'type': 'deposit', 'usdc': '1'}} for i in range(500)]
    calls = []
    def handler(request):
        body = json.loads(request.content)
        assert body['user'] == ADDRESS and body['type'] == 'userNonFundingLedgerUpdates'
        calls.append(body)
        return httpx.Response(200, json=[r for r in records if body['startTime'] <= r['time'] <= body['endTime']])
    rows, reasons, failed = collect_ledger.hyperliquid_flow_history(hl(handler), ADDRESS, MS, MS + 1000)
    assert len(rows) == 500 and len({row['hash'] for row in rows}) == 500
    assert not reasons and not failed and len(calls) > 1


def test_hl_dense_same_millisecond_exhaustion_is_explicit():
    payload = [{'hash': str(i), 'time': MS, 'delta': {'type': 'deposit', 'usdc': '1'}} for i in range(500)]
    rows, reasons, failed = collect_ledger.hyperliquid_flow_history(
        hl(lambda request: httpx.Response(200, json=payload)), ADDRESS, MS, MS)
    assert rows == [] and failed and reasons == ['DENSE_TIMESTAMP']
