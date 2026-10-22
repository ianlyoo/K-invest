"""Synthetic replay, immutable read adapters and conditional classifications."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from daily_nav import TOSS_CASH_BASIS
from toss_flow_inference import (
    infer_toss_flows,
    toss_interval_flows,
    toss_residual_diagnostics,
)

T0, T1 = '2026-10-10T09:10:00+00:00', '2026-10-11T09:10:00+00:00'
ACCOUNT = 'toss:primary'


def snapshot(stamp=T0, *, usd='100', krw='2000000', qty='10', account=ACCOUNT):
    return {'snapshot_id': stamp + account, 'version': 1, 'account': account, 'time': stamp,
            'complete': True, 'reasons': [], 'open_order_count': 0,
            'cash': {c: {'value': v, 'basis': TOSS_CASH_BASIS, 'observed_at': stamp}
                     for c, v in [('KRW', krw), ('USD', usd)]},
            'holdings': [] if qty == '0' else [{'symbol': 'SYN', 'currency': 'USD', 'quantity': qty,
                                              'price': '10', 'priced_at': stamp}],
            'fx': {'rate': '1400', 'valid_from': stamp, 'observed_at': stamp,
                   'valid_until': (datetime.fromisoformat(stamp) + timedelta(minutes=1)).isoformat()}}


def coverage(account=ACCOUNT, end='2026-10-12T00:00:00+00:00'):
    return [{'account': account, 'start': '2026-10-01T00:00:00+00:00', 'end': end,
             'status': 'complete', 'contract': 'toss-fills-v1', 'coverage_id': 'coverage-1', 'reasons': []}]


def schedule(rows=()):
    return {'rows': list(rows), 'coverage': [{'symbol': 'SYN', 'currency': 'USD',
            'start': '2026-10-01', 'end': '2026-10-15', 'status': 'complete'}]}


def fill(**changes):
    return {'account': ACCOUNT, 'event_id': 'fill-1', 'source_record_id': 'order-1', 'symbol': 'SYN',
            'currency': 'USD', 'side': 'buy', 'quantity': '2', 'gross_amount': '20', 'price_native': '10',
            'fee': '1', 'tax': '0.1', 'execution_time_utc': '2026-10-10T22:00:00+00:00',
            'source_status': 'CANCELED', 'version': 'version-1', **changes}


def run(s0=None, s1=None, fills=(), cal=None, windows=None, **kwargs):
    return infer_toss_flows([s0 or snapshot(), s1 or snapshot(T1)], list(fills), cal or schedule(),
                           coverage=coverage() if windows is None else windows, **kwargs)


def usd(rows):
    return next(r for r in rows if r['currency'] == 'USD')


def test_deposit_and_partial_buy_cancel_once():
    rows = run(s1=snapshot(T1, usd='178.9', qty='12'), fills=[fill(tax='0.1')])
    row = usd(rows)
    assert row['flow_native'] == '100.0'
    assert row['confidence'] == 'conditional' and row['timing'] == 'unknown'
    assert row['diagnostics']['trade_cash_effect_native'] == '-21.1'
    assert row['inputs']['fill_ids'] == ['fill-1']


@pytest.mark.parametrize('side,qty,cash,expected', [('buy', '12', '78.9', '-21.1'), ('sell', '8', '118.9', '18.9')])
def test_trade_cash_signs_fees_taxes(side, qty, cash, expected):
    row = usd(run(s1=snapshot(T1, usd=cash, qty=qty), fills=[fill(side=side, tax='0.1')]))
    assert row['flow_native'] == '0' and row['diagnostics']['trade_cash_effect_native'] == expected


@pytest.mark.parametrize('change,reason', [
    ({'tax': None}, 'FILL_AMOUNT_OR_COST_MISSING'), ({'fee': None}, 'FILL_AMOUNT_OR_COST_MISSING'),
    ({'source_status': 'UNKNOWN_ENUM'}, 'INVALID_FILL_STATUS_OR_ID'),
    ({'gross_amount': '21'}, 'INVALID_FILL_TOTALS'), ({'quantity': 2}, 'FILL_AMOUNT_OR_COST_MISSING'),
    ({'execution_time_utc': '2026-10-10T22:00:00'}, 'FILL_TIME_MISSING'),
    ({'reasons': ['LEDGER_CONTENT_CONFLICT']}, 'LEDGER_CONTENT_CONFLICT'),
])
def test_fill_failures_are_null(change, reason):
    rows = run(s1=snapshot(T1, qty='12'), fills=[fill(tax='0.1', **change)] if 'tax' not in change else [fill(**change)])
    assert all(r['flow_native'] is None for r in rows)
    assert reason in usd(rows)['reasons']


def test_duplicate_fill_rows_are_unresolved():
    row = usd(run(s1=snapshot(T1, qty='12'), fills=[fill(tax='0.1'), fill(tax='0.1')]))
    assert row['flow_native'] is None and 'DUPLICATE_FILL_ORDER' in row['reasons']


def test_quantity_mismatch_is_not_stock_transfer_or_deposit_guess():
    row = usd(run(s1=snapshot(T1, qty='11', usd='200')))
    assert row['flow_native'] is None and 'QUANTITY_UNRECONCILED' in row['reasons']


@pytest.mark.parametrize('index,count', [(0, 1), (1, 1), (0, None), (1, None)])
def test_open_order_endpoint_is_unresolved(index, count):
    snaps = [snapshot(), snapshot(T1)]
    snaps[index]['open_order_count'] = count
    rows = run(*snaps)
    assert rows and all(r['flow_native'] is None for r in rows)
    assert any('TOSS_OPEN_ORDERS_' in reason for reason in rows[0]['reasons'])


def test_multi_day_interval_bridges_missing_or_incomplete_snapshot():
    t2 = '2026-10-12T09:10:00+00:00'
    middle = snapshot(T1)
    middle['open_order_count'] = 1
    for snapshots in ([snapshot(), snapshot(t2)], [snapshot(), middle, snapshot(t2)]):
        rows = infer_toss_flows(snapshots, [], schedule(), coverage=coverage(end=t2))
        assert len(rows) == 2 and rows[0]['t0'] == T0 and rows[0]['t1'] == t2
        assert all(r['flow_native'] == '0' for r in rows)


def test_cutoff_before_t1_and_creation_date_coverage_do_not_certify_fills():
    for windows in (coverage(end=T0), [{**coverage()[0], 'contract': 'legacy-creation-date'}], []):
        rows = run(windows=windows)
        assert all(r['flow_native'] is None for r in rows)
        assert 'FILLS_NOT_COLLECTED_THROUGH_T1' in rows[0]['reasons']


def test_disjoint_coverage_has_gap_even_when_end_reaches_t1():
    windows = [{**coverage()[0], 'end': '2026-10-10T12:00:00+00:00'},
               {**coverage()[0], 'start': '2026-10-10T13:00:00+00:00'}]
    assert 'FILL_COVERAGE_GAP' in usd(run(windows=windows))['reasons']


def dividend(**changes):
    return {'id': 'div-1', 'symbol': 'SYN', 'currency': 'USD', 'market': 'US',
            'ex_date': '2026-10-09', 'pay_date': '2026-10-11', 'gross_per_share': '1', **changes}


def dividend_run(cash, rows=None, history=True):
    snaps = ([snapshot('2026-10-08T09:10:00+00:00')] if history else []) + [snapshot(), snapshot(T1, usd=cash)]
    return infer_toss_flows(snaps, [], schedule([dividend()] if rows is None else rows), coverage=coverage(), start=T0)


def test_dividend_matches_ex_date_holding_net_of_withholding():
    row = usd(dividend_run('108.5'))
    assert row['income_native'] == '8.50' and row['flow_native'] == '0'
    assert row['confidence'] == 'conditional' and row['events'][0]['kind'] == 'dividend'
    assert row['events'][0]['external'] is False and row['events'][0]['time'] is None


@pytest.mark.parametrize('cash,reason', [('108.7', 'DIVIDEND_AMOUNT_UNEXPLAINED'), ('110', 'DIVIDEND_AMOUNT_UNEXPLAINED')])
def test_dividend_near_miss_is_not_a_deposit(cash, reason):
    row = usd(dividend_run(cash))
    assert row['flow_native'] is None and reason in row['reasons']


def test_ambiguous_dividend_subsets_are_null():
    rows = [dividend(id='a', gross_per_share='1'),
            dividend(id='b', gross_per_share='0.4', ex_date='2026-10-10'),
            dividend(id='c', gross_per_share='0.6', pay_date='2026-10-12')]
    row = usd(dividend_run('108.5', rows))
    assert row['flow_native'] is None and 'DIVIDEND_AMBIGUOUS' in row['reasons']


def test_current_holdings_cannot_substitute_for_missing_ex_date_evidence():
    row = usd(dividend_run('108.5', history=False))
    assert row['flow_native'] is None and 'DIVIDEND_ENTITLEMENT_UNKNOWN' in row['reasons']


def test_missing_schedule_is_not_a_zero_dividend():
    row = usd(run(cal={'rows': [], 'coverage': []}))
    assert row['flow_native'] is None and row['income_native'] is None
    assert 'DIVIDEND_COVERAGE_INCOMPLETE' in row['reasons']


@pytest.mark.parametrize('krw,expected', [('600000', 'fx_conversion'), ('586000', 'fx_conversion'), ('500000', 'unresolved')])
def test_fx_band_is_required(krw, expected):
    rows = run(s1=snapshot(T1, usd='1100', krw=krw))
    assert all(r['diagnostics']['classification'] == expected for r in rows)
    if expected == 'fx_conversion':
        assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in rows)
        assert all(r['events'][0]['kind'] == 'fx_leg' and not r['events'][0]['external'] for r in rows)
    else:
        assert all(r['flow_native'] is None and 'FX_RATE_OUTSIDE_BAND' in r['reasons'] for r in rows)


def test_fx_pair_uses_both_valid_endpoint_marks():
    start = snapshot()
    start['fx']['rate'] = '1200'
    rows = run(s0=start, s1=snapshot(T1, usd='1100', krw='600000'))
    assert all(r['flow_native'] is None for r in rows)


def test_rounding_tolerance_and_decimal_precision():
    row = usd(run(s1=snapshot(T1, usd='100.01')))
    assert row['flow_native'] == '0' and row['confidence'] == 'conditional'
    row = usd(run(s1=snapshot(T1, usd='100.010000000000000001')))
    assert row['flow_native'] == '0' and row['income_native'] == '0.010000000000000001'
    assert row['events'][0]['kind'] == 'income_other'
    assert 'SMALL_RESIDUAL_TREATED_AS_INCOME' in row['reasons']


def test_fill_boundary_is_open_left_closed_right():
    at_start = fill(execution_time_utc=T0, tax='0.1')
    assert usd(run(fills=[at_start]))['flow_native'] == '0'
    at_end = fill(execution_time_utc=T1, tax='0.1')
    assert usd(run(s1=snapshot(T1, qty='12', usd='78.9'), fills=[at_end]))['flow_native'] == '0'


def test_inter_account_transfer_requires_independent_two_sided_evidence():
    secondary = 'toss:secondary'
    snaps = [snapshot(), snapshot(T1, usd='50'), snapshot(account=secondary), snapshot(T1, usd='150', account=secondary)]
    windows = coverage() + coverage(account=secondary)
    rows = infer_toss_flows(snaps, [], schedule(), coverage=windows)
    assert {r['events'][0]['kind'] for r in rows if r['currency'] == 'USD'} == {'deposit', 'withdrawal'}
    evidence = {'id': 'paired', 'out_account': ACCOUNT, 'in_account': secondary, 'currency': 'USD',
                'amount_native': '50', 'time': T1, 'basis': 'ledger_transfer_match', 'evidence_ref': 'pair-proof'}
    matched = infer_toss_flows(snaps, [], schedule(), coverage=windows, transfers=[evidence])
    usd_rows = [r for r in matched if r['currency'] == 'USD']
    assert {r['events'][0]['kind'] for r in usd_rows} == {'transfer_in', 'transfer_out'}
    assert {Decimal(r['flow_native']) for r in usd_rows} == {Decimal('-50'), Decimal('50')}
    assert all(r['events'][0]['external'] for r in usd_rows)  # Crosses each account boundary.


def test_core_does_not_mutate_inputs_and_rejects_duplicate_snapshot_versions():
    snaps = [snapshot(), snapshot(T1)]
    original = deepcopy(snaps)
    infer_toss_flows(snaps, [], schedule(), coverage=coverage())
    assert snaps == original
    with pytest.raises(ValueError, match='AMBIGUOUS_SNAPSHOT_VERSIONS'):
        infer_toss_flows(snaps + [deepcopy(snaps[-1])], [], schedule(), coverage=coverage())


def test_missing_db_is_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    rows = toss_interval_flows(path, T0, T1, [ACCOUNT], schedule_provider=lambda *a: schedule())
    assert not path.exists() and all(r['flow_native'] is None for r in rows)
    assert rows[0]['reasons'] == ['LOCAL_TOSS_EVIDENCE_UNAVAILABLE']


def test_capture_and_read_adapter_is_immutable(tmp_path, monkeypatch):
    from test_toss_nav import END, START, capture

    from portfolio_ledger import LedgerStore

    first = capture(tmp_path, monkeypatch, day=START)
    second = capture(tmp_path, monkeypatch, day=END)
    db = tmp_path / 'synthetic-nav.db'
    store = LedgerStore(db)
    for account in ('toss:primary', 'toss:secondary'):
        store.record_coverage('toss', store.account_identity(account), 'toss/fills-v1/synthetic',
                              T0, T1, 'complete', [], 0, 'synthetic-run')
    before = db.read_bytes()
    rows = toss_interval_flows(db, START, END, schedule_provider=lambda *a: schedule())
    assert len(rows) == 4
    assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in rows)
    small = next(r for r in rows if r['account'] == ACCOUNT and r['currency'] == 'KRW')
    assert small['income_native'] == '350' and small['events'][0]['kind'] == 'income_other'
    assert {r['start_snapshot_id'] for r in rows} == {first.row['snapshot_id']}
    assert {r['end_snapshot_id'] for r in rows} == {second.row['snapshot_id']}
    result = toss_residual_diagnostics(db, START, END, schedule_provider=lambda *a: schedule())
    assert result['counts'] == {'intervals_total': 2, 'intervals_classified': 2, 'intervals_unresolved': 0}
    assert db.read_bytes() == before
    cash = second.row['account_nav'][ACCOUNT]['cash']['USD']
    assert cash['value_decimal'] is not None and cash['observed_at'] is not None
    assert second.row['account_nav'][ACCOUNT]['cash_basis_evidence']['open_order_count'] == 0
    assert all(c['quantity_decimal'] is not None for c in second.row['components'] if c['provider'] == 'toss' and c['component_type'] == 'holding')


def test_dividend_uses_ex_date_shares_even_after_sale():
    snaps = [snapshot('2026-10-08T09:10:00+00:00'), snapshot(), snapshot(T1, qty='5', usd='157.5')]
    trade = fill(side='sell', quantity='5', gross_amount='50', tax='0')
    row = usd(infer_toss_flows(snaps, [trade], schedule([dividend()]), coverage=coverage(), start=T0))
    assert row['income_native'] == '8.50' and row['flow_native'] == '0'


def test_korean_dividend_with_explicit_schedule_uses_154_percent_withholding():
    snaps = [snapshot('2026-10-08T09:10:00+00:00'), snapshot(), snapshot(T1, krw='2008460')]
    for snap in snaps:
        snap['holdings'][0]['currency'] = 'KRW'
    cal = {'rows': [dividend(currency='KRW', market='KR', gross_per_share='1000')],
           'coverage': [{'symbol': 'SYN', 'currency': 'KRW', 'start': '2026-10-01', 'end': '2026-10-15', 'status': 'complete'}]}
    rows = infer_toss_flows(snaps, [], cal, coverage=coverage(), start=T0)
    row = next(r for r in rows if r['currency'] == 'KRW')
    assert Decimal(row['income_native']) == Decimal('8460') and row['flow_native'] == '0'


def test_dividend_consumption_is_stable_when_query_start_changes():
    t2 = '2026-10-12T09:10:00+00:00'
    snaps = [snapshot('2026-10-08T09:10:00+00:00'), snapshot(), snapshot(T1, usd='108.5'), snapshot(t2, usd='117')]
    kwargs = {'coverage': coverage(end=t2)}
    whole = infer_toss_flows(snaps, [], schedule([dividend()]), start=T0, **kwargs)
    tail = infer_toss_flows(snaps, [], schedule([dividend()]), start=T1, **kwargs)
    assert tail == [r for r in whole if r['t1'] == t2]
    assert usd(tail)['income_native'] == '0' and usd(tail)['flow_native'] == '8.5'


def test_ambiguous_transfer_references_do_not_pick_first():
    secondary = 'toss:secondary'
    snaps = [snapshot(), snapshot(T1, usd='50'), snapshot(account=secondary), snapshot(T1, usd='150', account=secondary)]
    evidence = {'id': 'paired', 'out_account': ACCOUNT, 'in_account': secondary, 'currency': 'USD',
                'amount_native': '50', 'time': T1, 'basis': 'ledger_transfer_match', 'evidence_ref': 'pair-proof'}
    rows = infer_toss_flows(snaps, [], schedule(), coverage=coverage() + coverage(account=secondary),
                           transfers=[evidence, {**evidence, 'id': 'ambiguous'}])
    assert all(r['flow_native'] is None and 'TRANSFER_EVIDENCE_AMBIGUOUS' in r['reasons'] for r in rows if r['currency'] == 'USD')


def test_ledger_decimal_canonicalization_is_lossless():
    from portfolio_ledger import number

    value = '12345678901234567890.1234567890'
    assert number(value) == '12345678901234567890.123456789'


def test_adjacent_inclusive_microsecond_coverage_and_exclusive_cutoff():
    boundary = '2026-10-11T00:00:00+00:00'
    first = {**coverage()[0], 'end': '2026-10-10T23:59:59.999999+00:00'}
    second = {**coverage()[0], 'start': boundary}
    assert usd(run(windows=[first, second]))['flow_native'] == '0'
    row = usd(run(s1=snapshot(boundary), windows=[first]))
    assert row['flow_native'] is None and 'FILLS_NOT_COLLECTED_THROUGH_T1' in row['reasons']


def test_revision_moving_an_old_aggregate_inside_interval_is_unresolved():
    trade = fill(execution_time_utc='2026-10-01T00:00:00+00:00', conflict_times=[T1], reasons=['LEDGER_CONTENT_CONFLICT'])
    row = usd(run(fills=[trade]))
    assert row['flow_native'] is None and 'LEDGER_CONTENT_CONFLICT' in row['reasons']


def test_unavailable_diagnostics_do_not_invent_an_interval(tmp_path):
    data = toss_residual_diagnostics(tmp_path / 'missing.db', T0, T1, schedule_provider=lambda *a: schedule())
    assert data['counts'] == {'intervals_total': 0, 'intervals_classified': 0, 'intervals_unresolved': 0}
    assert data['reasons'] == ['LOCAL_TOSS_EVIDENCE_UNAVAILABLE']


@pytest.mark.parametrize('other_status', ['limited', 'missing', 'invalid-complete'])
@pytest.mark.parametrize('holding_period', ['current', 'ex-date-only', 'intermediate'])
def test_matching_dividend_cannot_bypass_other_entitled_symbol_coverage(other_status, holding_period):
    snaps = [snapshot('2026-10-08T09:10:00+00:00'), snapshot(), snapshot(T1, usd='108.5')]
    other = {'symbol': 'OTHER', 'currency': 'USD', 'quantity': '1', 'price': '10', 'priced_at': T0}
    selected = snaps if holding_period == 'current' else snaps[:1]
    for snap in selected:
        snap['holdings'].append(dict(other, priced_at=snap['time']))
    trades = []
    if holding_period == 'intermediate':
        snaps[0]['holdings'].pop()
        # Bought and sold between snapshots; absent at both interval endpoints.
        trades = [fill(event_id='other-buy', source_record_id='other-buy', symbol='OTHER',
                       quantity='1', gross_amount='10', fee='0', tax='0',
                       execution_time_utc='2026-10-08T20:00:00+00:00'),
                  fill(event_id='other-sell', source_record_id='other-sell', symbol='OTHER', side='sell',
                       quantity='1', gross_amount='10', fee='0', tax='0',
                       execution_time_utc='2026-10-09T20:00:00+00:00')]
    cal = schedule([dividend()])
    if other_status != 'missing':
        cal['coverage'].append({'symbol': 'OTHER', 'currency': 'USD', 'start': '2026-10-01',
            'end': '2026-10-15', 'status': 'complete' if other_status == 'invalid-complete' else 'limited',
            'reasons': ['INVALID_PUBLIC_DIVIDEND_RESPONSE']})
    row = usd(infer_toss_flows(snaps, trades, cal, coverage=coverage(), start=T0))
    assert row['flow_native'] is None and row['income_native'] is None
    assert row['confidence'] == 'unresolved' and 'DIVIDEND_COVERAGE_INCOMPLETE' in row['reasons']
    assert row['events'] == []


def test_matching_dividend_with_all_held_symbols_covered_is_conditional():
    snaps = [snapshot('2026-10-08T09:10:00+00:00'), snapshot(), snapshot(T1, usd='108.5')]
    for snap in snaps:
        snap['holdings'].append({'symbol': 'OTHER', 'currency': 'USD', 'quantity': '1',
                                 'price': '10', 'priced_at': snap['time']})
    cal = schedule([dividend()])
    cal['coverage'].append(dict(cal['coverage'][0], symbol='OTHER'))
    row = usd(infer_toss_flows(snaps, [], cal, coverage=coverage(), start=T0))
    assert row['flow_native'] == '0' and row['confidence'] == 'conditional'
    assert row['income_native'] == '8.50' and row['reasons'] == []


def test_new_unknown_cost_evidence_blocks_old_complete_window_until_repaired():
    old = {**coverage()[0], 'observed_at': '2026-10-12T01:00:00+00:00'}
    unknown = {**old, 'status': 'limited', 'reasons': ['FILL_AMOUNT_OR_COST_MISSING'],
               'observed_at': '2026-10-12T02:00:00+00:00'}
    row = usd(run(windows=[old, unknown]))
    assert row['flow_native'] is None and 'FILL_AMOUNT_OR_COST_MISSING' in row['reasons']
    repaired = {**old, 'observed_at': '2026-10-12T03:00:00+00:00'}
    assert usd(run(windows=[old, unknown, repaired]))['flow_native'] == '0'


@pytest.mark.parametrize('reason', ['FILL_EVIDENCE_UNAVAILABLE', 'FILL_PAGE_FAILED', 'FILL_ROW_LIMIT',
                                   'FILL_PAGE_LIMIT', 'SYMBOL_FILTERED_FILL_WINDOW'])
def test_transport_refresh_does_not_revoke_complete_strict_coverage(reason):
    old = {**coverage()[0], 'observed_at': '2026-10-12T01:00:00+00:00'}
    failed = {**old, 'status': 'limited', 'reasons': [reason],
              'observed_at': '2026-10-12T02:00:00+00:00'}
    rows = run(windows=[old, failed])
    assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in rows)
    missing = run(windows=[failed])
    assert all(r['flow_native'] is None and 'FILL_COVERAGE_GAP' in r['reasons'] for r in missing)
    contradicted = {**failed, 'reasons': [reason, 'FILL_AMOUNT_OR_COST_MISSING']}
    assert all(r['flow_native'] is None for r in run(windows=[old, contradicted]))


def test_without_schedule_dividend_cash_is_a_flow_or_small_income():
    """Owner decision 10/17: schedule=None models no dividends, so nothing is fetched or matched."""
    paid = {'id': 'div-1', 'symbol': 'SYN', 'currency': 'USD', 'market': 'US', 'ex_date': '2026-10-08',
            'pay_date': '2026-10-10', 'gross_per_share': '1'}
    s0 = snapshot('2026-10-07T09:10:00+00:00')
    rows = infer_toss_flows([s0, snapshot(), snapshot(T1, usd='108.5')], [], schedule([paid]), coverage=coverage())
    assert usd([r for r in rows if r['t1'] == T1])['diagnostics']['classification'] == 'dividend'  # the matcher still works when injected
    rows = infer_toss_flows([s0, snapshot(), snapshot(T1, usd='108.5')], [], None, coverage=coverage())
    row = usd([r for r in rows if r['t1'] == T1])
    assert row['flow_native'] == '8.5' and row['diagnostics']['classification'] == 'deposit'
    assert row['events'][-1] == {'time': None, 'amount_native': '8.5', 'kind': 'deposit', 'external': True}
    assert 'DIVIDENDS_COUNTED_AS_FLOWS' in row['reasons']
    assert not any(r.startswith('DIVIDEND_') for r in row['reasons']) and row['inputs']['dividend_evidence'] == []
    krw = next(r for r in infer_toss_flows([snapshot(), snapshot(T1, krw='2003000')], [], None, coverage=coverage())
               if r['currency'] == 'KRW')
    assert krw['flow_native'] == '0' and krw['income_native'] == '3000'
    assert {'DIVIDENDS_COUNTED_AS_FLOWS', 'SMALL_RESIDUAL_TREATED_AS_INCOME'} <= set(krw['reasons'])
    rows = infer_toss_flows([snapshot(), snapshot(T1, qty='12')], [fill(tax=None)], None, coverage=coverage())
    assert all(r['flow_native'] is None and r['confidence'] == 'unresolved' for r in rows)
    # A US dividend paid gross with its 15% tax debited in KRW fails the FX pair: unresolved, never a flow.
    rows = infer_toss_flows([snapshot(), snapshot(T1, usd='110', krw='1997900')], [], None, coverage=coverage())
    assert all(r['flow_native'] is None and r['confidence'] == 'unresolved' for r in rows)
    assert all({'FX_RATE_OUTSIDE_BAND', 'DIVIDENDS_COUNTED_AS_FLOWS'} <= set(r['reasons']) for r in rows)
