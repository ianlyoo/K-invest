"""Toss flow resolution rules, using synthetic cash/entitlement/FX evidence only."""
from copy import deepcopy
from decimal import Decimal

import pytest
from test_toss_flow_inference import (
    T0,
    T1,
    coverage,
    dividend,
    dividend_run,
    run,
    schedule,
    snapshot,
    usd,
)

from toss_flow_inference import infer_toss_flows

ANCHOR = '2026-10-08T09:10:00+00:00'
T2 = '2026-10-12T09:10:00+00:00'


def replay(cash='100', krw='2000000', *, rows=None, history=True, fx='1400'):
    snaps = ([snapshot(ANCHOR)] if history else []) + [snapshot(), snapshot(T1, usd=cash, krw=krw)]
    for snap in snaps:
        snap['fx']['rate'] = fx
    return infer_toss_flows(snaps, [], schedule([dividend()] if rows is None else rows),
                            coverage=coverage(), start=T0)


@pytest.mark.parametrize('cash', ['100', '100.01', '99.99'])
def test_r1_zero_keeps_dividend_pending_and_later_payment_matches(cash):
    snaps = [snapshot(ANCHOR), snapshot(), snapshot(T1, usd=cash),
             snapshot(T2, usd=str(Decimal(cash) + Decimal('8.5')))]
    results = infer_toss_flows(snaps, [], schedule([dividend()]), coverage=coverage(end=T2), start=T0)
    early = usd([r for r in results if r['t1'] == T1])
    paid = usd([r for r in results if r['t1'] == T2])
    assert early['flow_native'] == '0' and early['income_native'] == '0' and early['events'] == []
    assert 'DIVIDEND_PENDING_NOT_PAID' in early['reasons']
    assert paid['flow_native'] == '0' and Decimal(paid['income_native']) == Decimal('8.5')
    assert paid['events'][0]['schedule_id'] == 'div-1'
    assert all(r['confidence'] == 'conditional' for r in results)
    # Warm-up history must consume the same payment regardless of query start.
    tail = infer_toss_flows(snaps, [], schedule([dividend()]), coverage=coverage(end=T2), start=T1)
    assert tail == [r for r in results if r['t1'] == T2]


def test_r1_rounding_zero_does_not_consume_a_tiny_dividend():
    result = usd(replay(rows=[dividend(gross_per_share='0.001')]))
    assert result['events'] == [] and result['income_native'] == '0'
    assert 'DIVIDEND_PENDING_NOT_PAID' in result['reasons']


@pytest.mark.parametrize('cash', ['100', '100.01', '99.99'])
def test_r4_zero_retains_unknown_entitlement_reason(cash):
    row = usd(replay(cash, history=False))
    assert row['flow_native'] == '0' and row['income_native'] == '0'
    assert row['confidence'] == 'conditional' and row['events'] == []
    assert {'DIVIDEND_ENTITLEMENT_UNKNOWN', 'DIVIDEND_ENTITLEMENT_UNKNOWN_ZERO_RESIDUAL'} <= set(row['reasons'])


@pytest.mark.parametrize('cash', ['100.0100001', '101', '108.5', '150', '50'])
def test_r4_nonzero_unknown_entitlement_is_not_income_or_flow(cash):
    row = usd(replay(cash, history=False))
    assert row['flow_native'] is None and row['income_native'] is None
    assert row['confidence'] == 'unresolved' and 'DIVIDEND_ENTITLEMENT_UNKNOWN' in row['reasons']
    assert 'DIVIDEND_ENTITLEMENT_UNKNOWN_ZERO_RESIDUAL' not in row['reasons']


@pytest.mark.parametrize('cash', ['100', '108.5'])
def test_unknown_second_symbol_allows_only_zero_not_matching_known_subset(cash):
    snaps = [snapshot(ANCHOR), snapshot(), snapshot(T1, usd=cash)]
    for snap in snaps:
        snap['holdings'].append({**snap['holdings'][0], 'symbol': 'OTHER'})
    cal = schedule([dividend(), dividend(id='other', symbol='OTHER', ex_date='2026-10-01')])
    cal['coverage'].append({**cal['coverage'][0], 'symbol': 'OTHER'})
    row = usd(infer_toss_flows(snaps, [], cal, coverage=coverage(), start=T0))
    assert 'DIVIDEND_ENTITLEMENT_UNKNOWN' in row['reasons']
    assert row['flow_native'] == ('0' if cash == '100' else None)
    assert row['events'] == []


@pytest.mark.parametrize('damage', ['coverage', 'bad-row', 'fill-gap', 'quantity', 'open-orders'])
def test_zero_exceptions_do_not_bypass_other_safety_gates(damage):
    snaps = [snapshot(), snapshot(T1)]
    cal, windows = schedule([dividend()]), coverage()
    if damage == 'coverage':
        cal['coverage'] = []
    elif damage == 'bad-row':
        cal['rows'][0]['gross_per_share'] = '-1'
    elif damage == 'fill-gap':
        windows = []
    elif damage == 'quantity':
        snaps[1]['holdings'][0]['quantity'] = '11'
    else:
        snaps[1]['open_order_count'] = 1
    values = infer_toss_flows(snaps, [], cal, coverage=windows)
    assert values and all(r['flow_native'] is None and r['confidence'] == 'unresolved' for r in values)


@pytest.mark.parametrize(('residual', 'allowed'), [('4.24', True), ('4.25', False), ('8.7', False),
                                                ('12.5', False), ('12.51', True), ('-20', True)])
def test_r2_non_dividend_cash_vs_inclusive_dividend_like_bounds(residual, allowed):
    row = usd(replay(str(Decimal('100') + Decimal(residual))))
    if allowed:
        assert Decimal(row['flow_native']) == Decimal(residual) and row['income_native'] == '0'
        assert row['confidence'] == 'conditional' and 'DIVIDEND_POSSIBLY_IN_FLOW' in row['reasons']
    else:
        assert row['flow_native'] is None and 'DIVIDEND_AMOUNT_UNEXPLAINED' in row['reasons']


def test_r2_dividend_like_guard_checks_combined_subsets():
    rows = [dividend(), dividend(id='b', ex_date='2026-10-10', gross_per_share='2')]
    row = usd(replay('134', rows=rows))  # Above either single window, inside the combined 37.5 upper bound.
    assert row['flow_native'] is None and 'DIVIDEND_AMOUNT_UNEXPLAINED' in row['reasons']


def test_r2_flow_does_not_consume_a_later_dividend():
    snaps = [snapshot(ANCHOR), snapshot(), snapshot(T1, usd='150'), snapshot(T2, usd='158.5')]
    values = infer_toss_flows(snaps, [], schedule([dividend()]), coverage=coverage(end=T2), start=T0)
    assert usd([r for r in values if r['t1'] == T1])['flow_native'] == '50'
    paid = usd([r for r in values if r['t1'] == T2])
    assert paid['flow_native'] == '0' and Decimal(paid['income_native']) == Decimal('8.5')


@pytest.mark.parametrize('tax', ['2100', '2152.5', '2047.5'])
def test_r5_us_gross_requires_and_books_the_actual_krw_tax_leg(tax):
    values = replay('110', str(Decimal('2000000') - Decimal(tax)))
    dollar, won = usd(values), next(r for r in values if r['currency'] == 'KRW')
    assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in values)
    assert Decimal(dollar['income_native']) == 10 and Decimal(won['income_native']) == -Decimal(tax)
    assert dollar['events'][0]['withholding'] == 'gross'
    assert won['events'][0]['kind'] == 'fee' and won['events'][0]['fee_type'] == 'dividend_withholding'
    assert won['events'][0]['dividend_schedule_ids'] == ['div-1']
    assert won['diagnostics']['matched_income_native'] == won['income_native']
    assert all('DIVIDEND_WITHHOLDING_IN_KRW' in r['reasons'] for r in values)


@pytest.mark.parametrize('krw', ['2000000', '2002100', '1997800'])
def test_r5_missing_offset_or_mismatched_tax_is_not_gross_income(krw):
    # 2,000,000 includes the rejected tax debit + equal transfer (net zero) case.
    values = replay('110', krw)
    row = usd(values)
    assert all(r['flow_native'] is None and r['confidence'] == 'unresolved' for r in values)
    assert row['income_native'] is None and row['events'] == []
    assert 'DIVIDEND_WITHHOLDING_LEG_UNPROVEN' in row['reasons']


def test_r5_net_match_has_priority_over_a_different_gross_subset():
    rows = [dividend(), dividend(id='b', ex_date='2026-10-10', gross_per_share='0.85')]
    row = usd(replay('108.5', rows=rows))
    assert row['flow_native'] == '0' and row['events'][0]['schedule_id'] == 'div-1'
    assert all(e.get('withholding') != 'gross' for e in row['events'])


def test_r5_ambiguous_gross_subsets_remain_unresolved():
    rows = [dividend(id='a'), dividend(id='b', ex_date='2026-10-10', gross_per_share='0.4'),
            dividend(id='c', ex_date='2026-10-11', gross_per_share='0.6')]
    row = usd(replay('110', '1997900', rows=rows))
    assert row['flow_native'] is None and 'DIVIDEND_AMBIGUOUS' in row['reasons']


def test_r5_kr_unique_gross_match_is_conditional():
    snaps = [snapshot(ANCHOR), snapshot(), snapshot(T1, krw='2010000')]
    for snap in snaps:
        snap['holdings'][0]['currency'] = 'KRW'
    cal = schedule([dividend(currency='KRW', market='KR', gross_per_share='1000')])
    cal['coverage'][0]['currency'] = 'KRW'
    row = next(r for r in infer_toss_flows(snaps, [], cal, coverage=coverage(), start=T0) if r['currency'] == 'KRW')
    assert row['flow_native'] == '0' and Decimal(row['income_native']) == 10000
    assert row['confidence'] == 'conditional' and 'DIVIDEND_GROSS_MATCHED' in row['reasons']


@pytest.mark.parametrize(('krw', 'accepted'), [('569480', True), ('565000', True), ('635000', True),
                                            ('564999.99', False), ('635000.01', False)])
def test_r3a_includes_observed_218_percent_and_exact_25_percent_bound(krw, accepted):
    values = run(s1=snapshot(T1, usd='1100', krw=krw))
    if accepted:
        assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in values)
        assert all('FX_CONVERSION_ESTIMATED_WITHIN_BAND' in r['reasons'] for r in values)
        # A supported internal conversion is not subject to the external-USD-flow budget.
        assert all('USD_FLOW_FX_VALUATION_UNSEPARATED' not in r['reasons'] for r in values)
    else:
        assert all(r['flow_native'] is None and 'FX_RATE_OUTSIDE_BAND' in r['reasons'] for r in values)


@pytest.mark.parametrize('residual', ['800', '-800', '800.0001', '-800.0001'])
def test_r6_usd_flow_error_budget_is_inclusive_and_applies_to_both_signs(residual):
    snaps = [snapshot(usd='1000'), snapshot(T1, usd=str(Decimal('1000') + Decimal(residual)))]
    for snap in snaps:
        snap['fx']['rate'] = '1000'
    values = infer_toss_flows(snaps, [], schedule(), coverage=coverage())
    row = usd(values)
    error = abs(Decimal(residual)) * 25
    assert Decimal(row['inputs']['usd_flow_fx_error_bound_krw']) == error
    if error <= 20000:
        assert Decimal(row['flow_native']) == Decimal(residual) and row['confidence'] == 'conditional'
    else:
        assert all(r['flow_native'] is None and r['confidence'] == 'unresolved' for r in values)
        assert 'USD_FLOW_FX_VALUATION_UNSEPARATED' in row['reasons']


@pytest.mark.parametrize(('currency', 'amount', 'small'), [('KRW', '5000', True), ('KRW', '-5000', True),
    ('KRW', '5000.000001', False), ('KRW', '-5000.000001', False), ('USD', '4', True), ('USD', '-4', True),
    ('USD', '4.000001', False), ('USD', '-4.000001', False)])
def test_r8_signed_income_cost_uses_closing_fx_and_exact_threshold(currency, amount, small):
    cash = {currency.lower(): str(Decimal('100' if currency == 'USD' else '2000000') + Decimal(amount))}
    snaps = [snapshot(), snapshot(T1, **cash)]
    snaps[0]['fx']['rate'], snaps[1]['fx']['rate'] = '1400', '1250'
    row = next(r for r in infer_toss_flows(snaps, [], schedule(), coverage=coverage()) if r['currency'] == currency)
    assert row['confidence'] == 'conditional'
    if small:
        assert row['flow_native'] == '0' and Decimal(row['income_native']) == Decimal(amount)
        assert row['diagnostics']['classification'] == 'income_other'
        assert row['diagnostics']['matched_income_native'] == row['income_native']
        assert row['events'] == [{'time': None, 'amount_native': amount, 'kind': 'income_other', 'external': False}]
        assert 'SMALL_RESIDUAL_TREATED_AS_INCOME' in row['reasons']
    else:
        assert Decimal(row['flow_native']) == Decimal(amount) and row['income_native'] == '0'
        assert all(e['kind'] != 'income_other' for e in row['events'])


def test_r8_never_hides_a_small_dividend_like_payment():
    row = usd(replay('104.25', fx='1000'))  # 4,250 KRW-equivalent but at the half-net guard.
    assert row['flow_native'] is None and 'DIVIDEND_AMOUNT_UNEXPLAINED' in row['reasons']
    assert 'SMALL_RESIDUAL_TREATED_AS_INCOME' not in row['reasons']


def test_all_resolved_paths_are_conditional_and_do_not_mutate_inputs():
    snaps, cal = [snapshot(), snapshot(T1)], schedule()
    before = deepcopy((snaps, cal))
    results = infer_toss_flows(snaps, [], cal, coverage=coverage())
    assert (snaps, cal) == before
    assert all(r['confidence'] == 'conditional' for r in results)
    assert all(r['inputs']['algorithm'] == 'toss-residual-v2' for r in results)
    assert usd(dividend_run('108.5'))['confidence'] == 'conditional'
