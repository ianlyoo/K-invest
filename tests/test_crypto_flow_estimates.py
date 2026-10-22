"""Crypto transfer estimates on a live-shaped NAV interval.

A move is internal only when one record names both in-scope ends; legs that merely
match in amount stay separate external flows at their own times. Stablecoin flows
without an event-time mark are interpolated between the capture marks, and small
unproven fee bases in the flow's own stablecoin are banded. Each estimate stays
visible in reasons and in the KRW range the engine bounds.
"""
import json
from decimal import Decimal

import pytest
from test_crypto_flow_intervals import ADDRESS, OTHER, ms
from test_crypto_live_send import (
    HL,
    RUN_10_16,
    TRANSFER_AT,
    block,
    flows,
    live_send,
    transfer,
    utc,
    world,
)
from test_interval_returns import END, START, crypto_record, event, midpoint

import crypto_flow_evidence as evidence
import interval_returns as ir
from crypto_flow_evidence import FlowEvidence
from daily_nav import instant
from portfolio_ledger import normalize_row

AT, BACK = utc('2026-10-15T08:00:00'), utc('2026-10-15T08:30:00')
INTERPOLATED = 'CRYPTO_EVENT_FX_INTERPOLATED'


def hl_row(typ, amount, fee, **extra):
    delta = {'type': typ, 'usdc': amount, 'fee': fee, **extra}
    delta.update({'nonce': 7} if typ == 'withdraw' else {'user': ADDRESS, 'destination': OTHER})
    return {'time': ms(AT), 'hash': '0x' + 'f' * 64, 'delta': delta}


def legs(row):
    return [(e['kind'], e['external'], e['amount_native']) for e in row['events']]


def interval_nav(navs):
    return [Decimal(nav['crypto_nav'][venue]['value_krw_decimal']) for nav in navs
            for venue in ('binance', 'hyperliquid')]


def test_internal_move_between_scope_wallets_is_no_flow_and_its_fee_is_a_cost(tmp_path):
    """A perp -> spot send inside the unified account: both legs in scope, no FX needed."""
    row = live_send(destination=ADDRESS, destinationDex='spot', amount='40', usdcValue='40', fee='0.5')
    path, _, _, navs = world(tmp_path, {HL: [row]})
    hl = flows(path)['hyperliquid', 'USDC']
    assert legs(hl) == [('transfer_out', False, '0'), ('fee', False, '-0.5')]
    assert all(e['fx'] is None for e in hl['events'])
    assert (hl['flow_native'], hl['flow_krw'], hl['confidence'], hl['reasons']) == ('0', '0', 'observed', [])
    [interval] = block(path, navs)['intervals']
    assert interval['confidence'] == 'observed' and interval['reason_codes'] == [] and interval['flows_krw'] == 0


def test_matching_legs_without_a_linking_record_keep_their_own_dietz_weights(tmp_path):
    """R1 counterexample: 900 out to another address at 10 % of the interval, 900 back into
    futures at 90 %, NAV 1,050 -> 1,150 KRW (marks of 1 KRW). Netting the legs by amount and
    time erased both weights (9.52 %); kept apart, Modified Dietz gives 30.30 %."""
    one = ({'USDT': '1', 'USDC': '1'},) * 2
    (tmp_path / 'probe').mkdir()
    probe = world(tmp_path / 'probe', {}, marks=one)[3]
    t0, t1 = (instant(nav['crypto_nav']['observed_at']) for nav in probe)
    out_at, in_at = ((t0 + (t1 - t0) * share).replace(microsecond=0) for share in (0.1, 0.9))
    send = {**live_send(amount='900', usdcValue='900', fee='0.0', feeToken=''), 'time': ms(out_at)}
    rows = {HL: [send], 'transfer_MAIN_UMFUTURE': [transfer('900', in_at)]}
    path, _, _, navs = world(tmp_path, rows, usdt=('1000', '1100'), marks=one)
    found = flows(path)
    hl, usdt = found['hyperliquid', 'USDC'], found['binance', 'USDT']
    assert legs(hl) == [('transfer_out', True, '-900')] and legs(usdt) == [('transfer_in', True, '900')]
    assert hl['reasons'] == usdt['reasons'] == [INTERPOLATED]
    [interval] = block(path, navs)['intervals']

    def weight(stamp):
        return Decimal(str((t1 - stamp).total_seconds())) / Decimal(str((t1 - t0).total_seconds()))

    expected = Decimal(100) / (1050 - 900 * weight(out_at) + 900 * weight(in_at)) * 100
    assert interval['flows_krw'] == 0 and interval['return_pct'] == round(float(expected), 4)
    assert interval['return_pct'] == pytest.approx(30.30, abs=0.01)
    assert interval['sensitivity_bounds_pct'] == {'low': interval['return_pct'], 'high': interval['return_pct']}


def test_an_out_and_back_through_main_is_two_external_flows(tmp_path):
    rows = {'transfer_UMFUTURE_MAIN': [transfer('300', AT, 1, route='UMFUTURE_MAIN')],
            'transfer_MAIN_UMFUTURE': [transfer('298.5', BACK, 2)]}
    path, _, _, navs = world(tmp_path, rows, usdt='1000')
    usdt = flows(path)['binance', 'USDT']
    assert sorted(legs(usdt)) == [('transfer_in', True, '298.5'), ('transfer_out', True, '-300')]
    assert (usdt['flow_native'], usdt['reasons']) == ('-1.5', [INTERPOLATED])
    assert sorted(e['time'] for e in usdt['events']) == ['2026-10-15T08:00:00Z', '2026-10-15T08:30:00Z']
    assert all(len(e['amount_krw_range']) == 2 for e in usdt['events'])  # USDT moved: each leg banded
    assert block(path, navs)['intervals'][0]['confidence'] == 'conditional'


def test_external_stablecoin_flow_without_an_event_mark_is_interpolated_and_bounded(tmp_path):
    path, _, _, navs = world(tmp_path, {'transfer_MAIN_UMFUTURE': [transfer()]})
    usdt = flows(path)['binance', 'USDT']
    [leg] = usdt['events']
    a, b = (instant(nav['crypto_nav']['fx_times']['USDT']['priced_at']) for nav in navs)
    share = Decimal(str((TRANSFER_AT - a).total_seconds())) / Decimal(str((b - a).total_seconds()))
    assert leg['fx'] == {'rate_to_krw': str((1348 + 3 * share).quantize(Decimal('0.00000001'))),
                         'source': 'linear_interpolation',
                         'between': ['2026-10-14T11:42:49.495000Z', '2026-10-15T09:10:07.964000Z']}
    half = Decimal('75.43219867') * (1351 - 1348)  # |flow_native| x |mark_t1 - mark_t0|
    assert [Decimal(x) - Decimal(leg['amount_krw']) for x in leg['amount_krw_range']] == [-half, half]
    assert (usdt['confidence'], usdt['conversion_status'], usdt['reasons']) == (
        'conditional', 'interpolated', [INTERPOLATED])
    result = block(path, navs)
    [interval] = result['intervals']
    assert result['label'] == 'estimate' and interval['reason_codes'] == [INTERPOLATED]
    # The return bounds are Modified Dietz at the band's corners: to first order the band
    # over the interval NAV, |flow| x |mark_t1 - mark_t0| / NAV.
    t0, t1 = (instant(nav['crypto_nav']['observed_at']) for nav in navs)
    weight = Decimal(str((t1 - TRANSFER_AT).total_seconds())) / Decimal(str((t1 - t0).total_seconds()))
    n0, n1 = sum(interval_nav(navs[:1])), sum(interval_nav(navs[1:]))

    def dietz(flow):
        return float((n1 - n0 - flow) / (n0 + weight * flow) * 100)

    flow = Decimal(leg['amount_krw'])
    assert interval['sensitivity_bounds_pct'] == {'low': pytest.approx(dietz(flow + half), abs=1e-4),
                                                  'high': pytest.approx(dietz(flow - half), abs=1e-4)}


def test_an_event_time_mark_still_wins_over_interpolation(tmp_path):
    path, _, _, navs = world(tmp_path, {'transfer_MAIN_UMFUTURE': [transfer('10', utc('2026-10-14T11:45:00'))]})
    usdt = flows(path)['binance', 'USDT']
    [leg] = usdt['events']
    assert (leg['fx']['source'], leg['amount_krw'], 'amount_krw_range' in leg) == ('upbit_public_ticker', '13480.0', False)
    assert (usdt['confidence'], usdt['conversion_status'], usdt['reasons']) == ('observed', 'observed', [])
    assert block(path, navs)['intervals'][0]['confidence'] == 'observed'


def test_a_non_stablecoin_flow_without_an_event_mark_stays_unresolved(tmp_path):
    path, _, _, navs = world(tmp_path, {'transfer_MAIN_UMFUTURE': [transfer('1', AT, asset='BNB')]})
    bnb = flows(path)['binance', 'BNB']
    assert (bnb['flow_native'], bnb['flow_krw'], bnb['conversion_status'], bnb['reasons']) == (
        '1', None, 'unresolved', ['CRYPTO_EVENT_FX_UNAVAILABLE'])
    [interval] = block(path, navs)['intervals']
    assert interval['confidence'] == 'unresolved' and interval['return_pct'] is None
    assert 'FLOW_KRW_CONVERSION_UNRESOLVED' in interval['reason_codes']


@pytest.mark.parametrize('row, flow, code, band', [
    (hl_row('withdraw', '50', '1'), None, 'CRYPTO_WITHDRAWAL_FEE_BASIS_UNVERIFIED', None),  # 2% of the flow
    (hl_row('withdraw', '500', '1'), '-500', 'CRYPTO_WITHDRAWAL_FEE_BASIS_ESTIMATED', '1'),
    (hl_row('internalTransfer', '1000', '6'), None, 'CRYPTO_TRANSFER_FEE_BASIS_UNVERIFIED', None),  # above 5 USD
    (hl_row('internalTransfer', '400', '5'), None, 'CRYPTO_TRANSFER_FEE_BASIS_UNVERIFIED', None),  # above 1%
    (hl_row('internalTransfer', '1000', '5'), '-1000', 'CRYPTO_TRANSFER_FEE_BASIS_ESTIMATED', '5'),  # at both caps
    (live_send(amount='1000', usdcValue='1000', fee='6'), '-1000', None, None),  # proven basis: fee on top
], ids=['withdraw_2pct', 'withdraw_0.2pct', 'transfer_6usd', 'transfer_1.25pct', 'transfer_at_cap', 'send'])
def test_fee_basis_cap(tmp_path, row, flow, code, band):
    path, _, _, navs = world(tmp_path, {HL: [row]}, usdt='1000')
    hl = flows(path)['hyperliquid', 'USDC']
    [interval] = block(path, navs)['intervals']
    fee = hl['events'][-1]
    assert fee['kind'] == 'fee' and not fee['external']  # the fee itself is a cost, never a flow
    assert hl['flow_native'] == flow
    if flow is None:
        assert hl['confidence'] == 'unresolved' and code in hl['reasons']
        assert interval['confidence'] == 'unresolved' and interval['return_pct'] is None
        return
    [leg] = [e for e in hl['events'] if e['external']]
    assert leg.get('fee_band_native') == band
    assert hl['reasons'] == sorted({INTERPOLATED} | ({code} if code else set()))
    if band:  # USDC 1351 at both marks: the KRW band is the fee basis alone
        assert [Decimal(x) - Decimal(leg['amount_krw']) for x in leg['amount_krw_range']] == [
            -Decimal(band) * 1351, Decimal(band) * 1351]
    else:
        assert 'amount_krw_range' not in leg
    assert interval['confidence'] == 'conditional' and interval['return_pct'] is not None


@pytest.mark.parametrize('row, fee_asset', [
    (hl_row('internalTransfer', '500', '1', feeToken='HYPE'), 'HYPE'),  # R1: was a +-1 USDC band
    (hl_row('withdraw', '500', '1', feeToken='HYPE'), 'HYPE'),
    (live_send(amount='500', usdcValue='500', fee='1', feeToken='HYPE'), 'HYPE'),
    (live_send(amount='500', usdcValue='500', fee='1', feeToken='USDT'), 'USDT'),
    # Samples show an empty feeToken only on a zero fee: it names no asset for a charged one,
    # on any type (R1 round 2: these withdrawals and transfers were -1000 estimates).
    (live_send(amount='500', usdcValue='500', fee='1', feeToken=''), None),
    (hl_row('withdraw', '500', '1', feeToken=''), None),
    (hl_row('internalTransfer', '500', '1', feeToken=''), None),
], ids=['transfer_hype', 'withdraw_hype', 'send_hype', 'send_usdt', 'send_empty', 'withdraw_empty', 'transfer_empty'])
def test_a_fee_outside_the_flow_stablecoin_keeps_its_denomination_and_stays_unresolved(tmp_path, row, fee_asset):
    path, ledger, ids, navs = world(tmp_path, {HL: [row]}, usdt='1000')
    with ledger.read() as conn:
        fact = normalize_row(ledger, conn, 'hyperliquid/' + ids['hyperliquid'] + '/' + HL, row, {})
    assert (fact['currency'], fact['fee'], fact['fee_asset']) == ('USDC', '1', fee_asset)
    hl = flows(path)['hyperliquid', 'USDC']
    # No fee-basis estimate; an unresolved leg has no native amount to convert either.
    assert (hl['flow_native'], hl['flow_krw'], hl['confidence'], hl['reasons']) == (
        None, None, 'unresolved', ['CRYPTO_EVENT_FX_UNAVAILABLE', 'CRYPTO_FEE_ASSET_UNVERIFIED'])
    [interval] = block(path, navs)['intervals']
    assert interval['confidence'] == 'unresolved' and interval['return_pct'] is None


@pytest.mark.parametrize('typ', ['withdraw', 'internalTransfer'])
def test_an_absent_fee_token_is_not_an_empty_one(tmp_path, typ):
    """Withdrawals and internal transfers have no documented feeToken: absent, their fee is
    in USDC (here a capped estimate). Present but empty, it names no asset."""
    found = {}
    for name, extra in (('absent', {}), ('empty', {'feeToken': ''}), ('free', {'feeToken': '', 'fee': '0'})):
        row = hl_row(typ, '1000', '1')
        row['delta'].update(extra)
        (tmp_path / name).mkdir()
        path, _, _, _ = world(tmp_path / name, {HL: [row]}, usdt='1000')
        hl = flows(path)['hyperliquid', 'USDC']
        found[name] = (hl['flow_native'], hl['confidence'])
    assert found == {'absent': ('-1000', 'conditional'), 'empty': (None, 'unresolved'), 'free': ('-1000', 'conditional')}


@pytest.mark.parametrize('fee_token, after', [('HYPE', None), (None, '-1000')], ids=['hype', 'absent'])
def test_evidence_saved_before_fee_denomination_is_not_trusted_for_it(tmp_path, monkeypatch, fee_token, after):
    """R1 round 2: windows of the earlier normalization (evidence v1) kept no feeToken and gave
    every HL fee the transfer currency, so a persisted HYPE fee replayed as a USDC estimate.
    A charged v1 HL fee has no asset until a v2 window observes the same event again."""
    row = hl_row('withdraw', '1000', '1', **({'feeToken': fee_token} if fee_token else {}))
    as_stored = {**row, 'delta': {k: v for k, v in row['delta'].items() if k != 'feeToken'}}
    with monkeypatch.context() as patch:
        patch.setattr(evidence, 'VERSION', 'crypto_flow_windows_v1')
        path, ledger, ids, navs = world(tmp_path, {HL: [as_stored]}, usdt='1000')
    with ledger.read() as conn:
        [stored] = [event for (body,) in conn.execute('SELECT payload_json FROM crypto_flow_windows')
                    for event in json.loads(body)['events']]
    assert stored['fee_asset'] == 'USDC'  # what v1 kept for this fee, HYPE or not
    hl = flows(path)['hyperliquid', 'USDC']
    assert (hl['flow_native'], hl['confidence'], hl['reasons']) == (
        None, 'unresolved', ['CRYPTO_EVENT_FX_UNAVAILABLE', 'CRYPTO_FEE_ASSET_UNVERIFIED'])
    assert block(path, navs)['intervals'][0]['return_pct'] is None

    # The next ledger run's rolling window re-observes the event under v2.
    stream = 'hyperliquid/' + ids['hyperliquid'] + '/' + HL
    FlowEvidence(ledger).raw(stream, [row], ids['hyperliquid'], *map(ms, RUN_10_16))
    hl = flows(path)['hyperliquid', 'USDC']
    [interval] = block(path, navs)['intervals']
    assert hl['flow_native'] == after
    if after is None:
        assert 'CRYPTO_FEE_ASSET_UNVERIFIED' in hl['reasons'] and interval['return_pct'] is None
    else:
        assert hl['reasons'] == [INTERPOLATED, 'CRYPTO_WITHDRAWAL_FEE_BASIS_ESTIMATED']
        assert interval['confidence'] == 'conditional' and interval['return_pct'] is not None


def ranged(span, *, external=True, confidence='conditional', status='interpolated'):
    leg = {**event(midpoint(START, END), '50', 'transfer_in'), 'amount_krw': '70000', 'amount_krw_range': span}
    events = [leg]
    if not external:
        events = [{**event(midpoint(START, END), '50', 'transfer_in'), 'amount_krw': '70000'},
                  {**leg, 'amount_native': '0', 'amount_krw': '0', 'external': False}]
    return crypto_record('50', events, flow_krw='70000', conversion_status=status, confidence=confidence)


def test_engine_reads_a_provider_krw_range_only_on_a_converted_external_estimate():
    valid = ir.validate_record(ranged(['69850', '70150']))
    assert valid['blocked'] == [] and valid['external'][0]['krw_range'] == (Decimal('69850'), Decimal('70150'))
    for record in (ranged(['70001', '70150']), ranged(['69850']), ranged(['69850', '70150'], confidence='observed',
                                                                       status='observed'),
                   ranged(['-1', '1'], external=False)):
        with pytest.raises(ir.InvalidRecord, match='FLOW_EVENT_RANGE_INVALID'):
            ir.validate_record(record)
    with pytest.raises(ir.InvalidRecord, match='FLOW_RECORD_CONFIDENCE_INCONSISTENT'):
        ir.validate_record(ranged(None, confidence='observed'))
