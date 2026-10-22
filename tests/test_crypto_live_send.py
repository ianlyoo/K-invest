"""Live-shaped 10/14 -> 10/15 crypto interval flows (timestamps as observed, amounts synthetic).

At 07:23:59.716 UTC on 10/15 the Hyperliquid account sent 75.68 USDC (fee 0.871204)
to another address. 17 s later Binance MAIN received a 75.48 USDC Arbitrum deposit,
and at 07:52:02 75.43219867 USDT moved MAIN -> UM futures. The 10/15 ledger run kept
the send in the legacy ledger, but the strict page validator rejected the send delta
(CRYPTO_INCOMPLETE_ENVELOPE): it carries its action nonce, which the allowlist only
admitted on withdrawals. That one partial window produced three of the interval's reasons.

Scope: crypto_nav values Binance UM futures collateral and the Hyperliquid unified
account only; Binance MAIN/FUNDING and other addresses are outside it. No record links
the send to the futures credit (the deposit's txId is not the send's hash, and a USDC ->
USDT conversion in MAIN that no collector reads sits between them), so each is its own
external flow with its own time.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from test_collect_ledger import collect_ledger
from test_crypto_flow_coverage import hl
from test_crypto_flow_intervals import ADDRESS, OTHER, ms
from test_crypto_scope_live_shape import live_10_15, live_nav

import interval_returns
from crypto_flow_evidence import FlowEvidence
from crypto_flow_intervals import REQUIRED_STREAMS, crypto_interval_flows
from daily_nav import instant
from portfolio_ledger import LedgerStore
from portfolio_store import NavStore


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


SEND_AT, DEPOSIT_AT, TRANSFER_AT = utc('2026-10-15T07:23:59.716'), utc('2026-10-15T07:24:16'), utc('2026-10-15T07:52:02')
# Strict windows of the 10/14 and 10/15 ledger runs, and the first run after the fix.
RUN_10_14 = (utc('2026-10-06T00:00'), utc('2026-10-14T19:10:48.268'))
RUN_10_15 = (utc('2026-10-07T00:00'), utc('2026-10-15T19:10:53.090'))
RUN_10_16 = (utc('2026-10-08T00:00'), utc('2026-10-16T19:10:50'))
HL = 'userNonFundingLedgerUpdates'


def live_send(**changes):
    # The ledger stores no feeToken (only fee_asset, normalized from the transfer currency
    # before window format v4), so the live send's feeToken is unknown: 'USDC' is an assumption.
    delta = {'type': 'send', 'user': ADDRESS, 'destination': OTHER, 'sourceDex': '', 'destinationDex': '',
             'token': 'USDC', 'amount': '75.68', 'usdcValue': '75.68', 'fee': '0.871204',
             'nativeTokenFee': '0.0', 'nonce': ms(SEND_AT) - 412, 'feeToken': 'USDC'}
    delta.update(changes)
    return {'time': ms(SEND_AT), 'hash': '0x' + 'c' * 64, 'delta': {k: v for k, v in delta.items() if v is not ...}}


def read(page, window=RUN_10_15):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=page if body['type'] == HL else [])
    return collect_ledger.hyperliquid_flow_history(hl(handler), ADDRESS, *map(ms, window))


@pytest.mark.parametrize('row', [
    live_send(),
    # Public dataset samples: an int nonce with an empty feeToken on a free send,
    # and a null nonce on a spot transfer.
    live_send(fee='0.0', feeToken='', sourceDex='spot'),
    live_send(type='spotTransfer', sourceDex=..., destinationDex=..., nonce=None, feeToken='', fee='0.0',
              user=OTHER, destination=ADDRESS),
], ids=['live_send', 'free_send', 'spot_null_nonce'])
def test_signed_transfer_nonce_is_part_of_the_documented_delta(row):
    assert read([row]) == ([row], [], False)


@pytest.mark.parametrize('typ, changes, code', [
    ('send', {'nonce': '1792049039304'}, 'CRYPTO_INVALID_IDENTITY'),
    ('send', {'nonce': True}, 'CRYPTO_INVALID_IDENTITY'),
    ('send', {'nonce': -1}, 'CRYPTO_INVALID_IDENTITY'),
    ('send', {'nonce': 2 ** 64}, 'CRYPTO_INVALID_IDENTITY'),
    ('send', {'feeToken': 'US DC'}, 'CRYPTO_INVALID_IDENTITY'),
    ('send', {'feeToken': None}, 'CRYPTO_INVALID_IDENTITY'),
    ('send', {'partial': True}, 'CRYPTO_INCOMPLETE_ENVELOPE'),
    ('internalTransfer', {'nonce': 1}, 'CRYPTO_INCOMPLETE_ENVELOPE'),
    ('withdraw', {'nonce': ...}, 'CRYPTO_INVALID_RESPONSE'),
    ('withdraw', {'nonce': None}, 'CRYPTO_INVALID_IDENTITY'),
])
def test_other_nonce_and_fee_token_shapes_still_fail_closed(typ, changes, code):
    row = live_send(**changes)
    if typ == 'internalTransfer':
        row['delta'] = {'type': typ, 'usdc': '75.68', 'user': ADDRESS, 'destination': OTHER, 'fee': '1', **changes}
    elif typ == 'withdraw':
        row['delta'] = {k: v for k, v in {'type': typ, 'usdc': '75.68', 'fee': '1', 'nonce': 7, **changes}.items()
                        if v is not ...}
    assert read([row]) == ([], [code], True)


# Upbit marks of the frozen 10/14 and 10/15 captures (public quotes).
FROZEN_MARKS = ({'USDT': '1348.0', 'USDC': '1351.0'}, {'USDT': '1351.0', 'USDC': '1351.0'})


def priced(nav, rates):
    """The capture at its frozen marks, venue KRW values re-priced at them."""
    crypto = nav['crypto_nav']
    for coin, rate in rates.items():
        crypto['fx_times'][coin]['rate_to_krw'] = rate
    for venue, coin in (('binance', 'USDT'), ('hyperliquid', 'USDC')):
        krw = Decimal(crypto[venue]['by_currency'][coin]['decimal_value']) * Decimal(rates[coin])
        crypto[venue].update(value_krw=float(krw), value_krw_decimal=str(krw))
    return nav


def world(tmp_path, rows, *, usdt='100', hl_partial=False, marks=FROZEN_MARKS):
    """The two live NAV points and the 10/14 + 10/15 legacy and strict ledger runs of rows.

    ``usdt``: the futures USDT of both points, or a (start, end) pair.
    """
    path = tmp_path / 'crypto.db'
    ledger = LedgerStore(path)
    ids = {'binance': ledger.account_identity('binance:primary'), 'hyperliquid': ledger.hl_identity(ADDRESS)}
    first, last = (usdt, usdt) if isinstance(usdt, str) else usdt
    navs = [priced(live_nav(0, usdt=first, ids=ids), marks[0]), priced(live_10_15(usdt=last, ids=ids), marks[1])]
    with NavStore(path)._connect() as conn:
        for nav in navs:
            NavStore._insert_version(conn, nav)
    legacy = collect_ledger.CollectionRun(ledger)
    for leaf, found in rows.items():
        stream = 'binance/' + leaf if leaf != HL else 'hyperliquid/' + ids['hyperliquid'] + '/' + leaf
        legacy.raw(stream, found, ids[stream.split('/')[0]], *map(ms, RUN_10_15),
                   ['HISTORY_CAP_UNVERIFIED'] if leaf == HL else [])
    strict = FlowEvidence(ledger)
    for venue, streams in REQUIRED_STREAMS.items():
        for leaf in sorted(streams):
            stream = venue + '/' + ids[venue] + '/' + leaf
            strict.raw(stream, [], ids[venue], *map(ms, RUN_10_14))
            if leaf == HL and hl_partial:  # as stored on 10/15: no facts, partial, the validator's code
                strict.raw(stream, [], ids[venue], *map(ms, RUN_10_15), ['CRYPTO_INCOMPLETE_ENVELOPE'], True)
            else:
                strict.raw(stream, rows.get(leaf, []), ids[venue], *map(ms, RUN_10_15))
    return path, ledger, ids, navs


def deposit(amount='75.48', at=DEPOSIT_AT):
    return {'id': 'live-deposit', 'amount': amount, 'coin': 'USDC', 'network': 'ARBITRUM', 'status': 1,
            'address': '0x' + 'd' * 40, 'txId': '0x' + 'e' * 64, 'insertTime': ms(at), 'walletType': 0}


def transfer(amount='75.43219867', at=TRANSFER_AT, ident=9001, asset='USDT', route='MAIN_UMFUTURE'):
    return {'tranId': ident, 'timestamp': ms(at), 'amount': amount, 'asset': asset, 'type': route,
            'status': 'CONFIRMED'}


def interval(tmp_path):
    """The live 10/15 chain as the frozen copy stores it."""
    rows = {'deposit': [deposit()], 'transfer_MAIN_UMFUTURE': [transfer()], HL: [live_send()]}
    return world(tmp_path, rows, hl_partial=True)


def flows(path):
    return {(r['source'], r['currency']): r for r in crypto_interval_flows(path, '2026-10-14', '2026-10-15')}


def block(path, navs):
    return interval_returns.ledgerless_returns(str(path), navs, 'crypto', '2026-10-14', '2026-10-15',
                                               detail_level='full')


def test_frozen_10_15_interval_and_the_first_ledger_run_after_the_fix(tmp_path):
    path, ledger, ids, navs = interval(tmp_path)
    before = flows(path)
    # The frozen copy's HL record, exactly.
    assert before['hyperliquid', 'USDC']['reasons'] == [
        'CRYPTO_FLOW_COVERAGE_INCOMPLETE', 'CRYPTO_INCOMPLETE_ENVELOPE', 'CRYPTO_LEGACY_FLOW_NOT_RECONCILED']
    usdt = before['binance', 'USDT']
    assert usdt['flow_native'] == '75.43219867' and usdt['reasons'] == ['CRYPTO_EVENT_FX_INTERPOLATED']
    assert [e['amount_native'] for e in before['binance', 'USDC']['events']] == ['0']  # MAIN deposit: outside the cut
    [row] = block(path, navs)['intervals']
    assert row['confidence'] == 'unresolved' and row['reason_codes'] == [
        'UNRESOLVED_FLOW', 'CRYPTO_FLOW_COVERAGE_INCOMPLETE', 'CRYPTO_INCOMPLETE_ENVELOPE',
        'CRYPTO_LEGACY_FLOW_NOT_RECONCILED', 'CRYPTO_EVENT_FX_INTERPOLATED']

    # The next 04:10 KST ledger run re-reads the rolling window, which still holds the send.
    found, reasons, failed = read([live_send()], RUN_10_16)
    stream = 'hyperliquid/' + ids['hyperliquid'] + '/' + HL
    assert FlowEvidence(ledger).raw(stream, found, ids['hyperliquid'], *map(ms, RUN_10_16), reasons, failed) == {
        'status': 'complete', 'reason_codes': [], 'row_count': 1}
    after = flows(path)
    hl_row, usdt = after['hyperliquid', 'USDC'], after['binance', 'USDT']
    # No record links the send to the futures credit: two external flows, each at its own
    # time (Modified Dietz weights them apart); the send's fee, on top of it, is a cost.
    assert [(e['kind'], e['external'], e['amount_native'], e['time']) for e in hl_row['events']] == [
        ('transfer_out', True, '-75.68', '2026-10-15T07:23:59.716000Z'),
        ('fee', False, '-0.871204', '2026-10-15T07:23:59.716000Z')]
    assert [(e['kind'], e['external'], e['amount_native'], e['time']) for e in usdt['events']] == [
        ('transfer_in', True, '75.43219867', '2026-10-15T07:52:02Z')]
    # USDC was 1351.0 at both captures: its interpolated rate is exact and has no band.
    send = hl_row['events'][0]
    assert (send['fx']['rate_to_krw'], send['amount_krw'], 'amount_krw_range' in send) == ('1351.0', '-102243.680', False)
    credit = usdt['events'][0]
    half = Decimal('75.43219867') * 3  # USDT moved 1348 -> 1351 between the captures
    assert [Decimal(x) - Decimal(credit['amount_krw']) for x in credit['amount_krw_range']] == [-half, half]
    for row, flow in ((hl_row, '-75.68'), (usdt, '75.43219867')):
        assert (row['flow_native'], row['confidence'], row['conversion_status'], row['reasons']) == (
            flow, 'conditional', 'interpolated', ['CRYPTO_EVENT_FX_INTERPOLATED'])
    result = block(path, navs)
    [row] = result['intervals']
    assert result['label'] == 'estimate' and result['period']['reason_codes'] == []
    assert row['confidence'] == 'conditional' and row['reason_codes'] == ['CRYPTO_EVENT_FX_INTERPOLATED']
    t0, t1 = (instant(nav['crypto_nav']['observed_at']) for nav in navs)

    def weight(stamp):
        return Decimal(str((t1 - stamp).total_seconds())) / Decimal(str((t1 - t0).total_seconds()))

    def dietz(credit_krw):
        flows = ((Decimal('-102243.680'), weight(SEND_AT)), (credit_krw, weight(TRANSFER_AT)))
        start, end = Decimal(100 * 1348 + 50 * 1351), Decimal(100 * 1351 + 50 * 1351)
        return float((end - start - sum(f for f, _ in flows)) / (start + sum(f * w for f, w in flows)) * 100)

    krw = Decimal(credit['amount_krw'])
    assert row['flows_krw'] == round(float(krw - Decimal('102243.680')), 2)
    assert row['return_pct'] == round(dietz(krw), 4)
    assert row['sensitivity_bounds_pct'] == {'low': round(dietz(krw + half), 4), 'high': round(dietz(krw - half), 4)}


def test_the_live_send_with_an_empty_fee_token_stays_unresolved_after_the_re_read(tmp_path):
    """The other possible live shape: a charged fee under an empty feeToken names no asset."""
    path, ledger, ids, navs = interval(tmp_path)
    found, reasons, failed = read([live_send(feeToken='')], RUN_10_16)
    stream = 'hyperliquid/' + ids['hyperliquid'] + '/' + HL
    FlowEvidence(ledger).raw(stream, found, ids['hyperliquid'], *map(ms, RUN_10_16), reasons, failed)
    hl_row = flows(path)['hyperliquid', 'USDC']
    assert (hl_row['flow_native'], hl_row['reasons']) == (None, ['CRYPTO_EVENT_FX_UNAVAILABLE', 'CRYPTO_FEE_ASSET_UNVERIFIED'])
    assert block(path, navs)['intervals'][0]['confidence'] == 'unresolved'
