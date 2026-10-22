"""Ledgerless interval engine: synthetic NAVs and shared-shape flow records only."""
import json
import sqlite3
import threading
import time
import types
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from test_ledger_returns import wired as base_wired
from test_toss_nav import END as NAV_END
from test_toss_nav import START as NAV_START
from test_toss_nav import capture

import interval_returns as ir
from portfolio_engine import time_weighted_return

ACCOUNT = 'toss:primary'
PASSED = {'toss_investmcp': {'status': 'measured', 'passed_bar': True, 'p95_daily_error_bp': 0.5,
                             'basis': 'synthetic'}}


def at(day, clock='18:10'):
    return f'{day}T{clock}:00+09:00'


def nav(day, value, *, reasons=(), sid=None, when=None, usd=0.0, fx=1400):
    return {'snapshot_date': day, 'snapshot_id': sid or 'snap-' + day, 'valuation_at': when or at(day),
            'total_value_krw': value, 'reason_codes': list(reasons),
            'account_nav': {ACCOUNT: {'cash': {'KRW': {'value': 1.0}, 'USD': {'value': usd}},
                                      'nav_native': {'KRW': {'value': value}}}},
            'fx_times': {'USD': {'rate_to_krw': fx}}}


def record(n0, n1, flow='0', *, currency='KRW', timing='unknown', events=(), confidence=None,
           income=None, reasons=(), method='cash_residual', account=ACCOUNT, **extra):
    if confidence is None:
        confidence = 'unresolved' if flow is None else 'observed' if flow == '0' else 'conditional'
    if not events and flow not in (None, '0'):  # a residual flow is one external event of unknown time
        events = [event(None, flow, 'deposit' if Decimal(flow) > 0 else 'withdrawal')]
    return {'source': 'toss', 'account': account, 'currency': currency,
            't0': n0['valuation_at'], 't1': n1['valuation_at'],
            'start_snapshot_id': n0['snapshot_id'], 'end_snapshot_id': n1['snapshot_id'],
            'flow_native': flow, 'income_native': income, 'events': list(events), 'timing': timing,
            'method': method, 'confidence': confidence, 'reasons': list(reasons), 'inputs': {}, **extra}


def event(stamp, amount, kind='deposit'):
    return {'time': stamp, 'amount_native': amount, 'kind': kind, 'external': True}


def run(navs, records, scope='toss_investmcp', accuracy=None, **kwargs):
    return ir.compute(navs, records, scope, navs[0]['snapshot_date'], navs[-1]['snapshot_date'],
                      accounts=[ACCOUNT], accuracy=accuracy, **kwargs)


def midpoint(n0, n1):
    a, b = datetime.fromisoformat(n0['valuation_at']), datetime.fromisoformat(n1['valuation_at'])
    return (a + (b - a) / 2).isoformat()


# Design worked example: start 10m, +1%, then 4m deposit and buy, then +1%.
START, END = nav('2026-10-12', 10_000_000), nav('2026-10-13', 14_241_000)


def test_intraday_deposit_buy_unknown_timing_midpoint_and_sensitivity():
    block = run([START, END], [record(START, END, '4000000')])
    assert block['label'] == 'estimate' and block['method'] == 'modified_dietz_linked_twr_estimate'
    assert block['is_exact_twr'] is False
    assert block['period']['return_pct'] == 2.0083  # halfway Dietz, -0.17 bp vs exact
    assert block['period']['sensitivity_bounds_pct'] == {'low': 1.7214, 'high': 2.41}
    row = block['intervals'][0]
    assert row['timing'] == 'unknown' and row['confidence'] == 'conditional'
    assert row['flows_krw'] == 4_000_000 and 'FLOW_TIMING_UNKNOWN' in row['reason_codes']
    assert 'ESTIMATE, not verified' in block['explanation']


def test_diagnostic_detail_adds_exact_dietz_terms_for_the_backtest_only():
    block = run([START, END], [record(START, END, '4000000')], detail_level='diagnostic')
    row = block['intervals'][0]  # 14.241m - 10m - 4m over 10m + 4m / 2
    assert (Decimal(row['dietz_gain_krw']), Decimal(row['dietz_denominator_krw'])) == (241_000, 12_000_000)
    assert 'dietz_gain_krw' not in run([START, END], [record(START, END, '4000000')])['intervals'][0]


def test_intraday_deposit_buy_known_timing_end_flow_and_exact():
    halfway = run([START, END], [record(START, END, '4000000', timing='known', method='api_direct',
                                       confidence='observed', events=[event(midpoint(START, END), '4000000')])])
    assert halfway['period']['return_pct'] == 2.0083
    assert halfway['period']['sensitivity_bounds_pct'] == {'low': 2.0083, 'high': 2.0083}
    at_end = run([START, END], [record(START, END, '4000000', timing='known', method='api_direct',
                                      confidence='observed', events=[event(END['valuation_at'], '4000000')])])
    assert at_end['period']['return_pct'] == 2.41  # end-flow arithmetic, +40 bp
    # The existing end-flow TWR is untouched and still reports 2.41%.
    legacy = [{'snapshot_date': n['snapshot_date'], 'total_value_krw': n['total_value_krw']} for n in (START, END)]
    assert time_weighted_return(legacy, {'2026-10-13': 4_000_000}) == 0.0241
    # Exact TWR needs a valuation at the flow instant: 10m -> 14.1m (flow at t1) -> 14.241m.
    flow_at = at('2026-10-13', '09:00')
    middle = nav('2026-10-13', 14_100_000, when=flow_at, sid='snap-flow')
    later = nav('2026-10-14', 14_241_000)
    exact = run([START, middle, later], [
        record(START, middle, '4000000', timing='known', confidence='observed', method='api_direct',
               events=[event(flow_at, '4000000')]),
        record(middle, later, '0')])
    assert exact['period']['return_pct'] == 2.01
    assert [r['return_pct'] for r in exact['intervals']] == [1.0, 1.0]


def test_missed_dividend_and_unmatched_fee_drift():
    paid = nav('2026-10-13', 10_020_000)
    truth = run([START, paid], [record(START, paid, '0', income='20000')])
    naive = run([START, paid], [record(START, paid, '20000')])
    assert truth['period']['return_pct'] == 0.2 and naive['period']['return_pct'] == 0.0
    assert round((naive['period']['return_pct'] - truth['period']['return_pct']) * 100, 4) == -20  # bp
    assert truth['intervals'][0]['income_native'] == {'KRW': '20000'}
    charged = nav('2026-10-13', 9_998_000)
    truth = run([START, charged], [record(START, charged, '0')])
    naive = run([START, charged], [record(START, charged, '-2000')])
    assert truth['period']['return_pct'] == -0.02 and naive['period']['return_pct'] == 0.0
    assert round((naive['period']['return_pct'] - truth['period']['return_pct']) * 100, 4) == 2


def test_open_order_endpoint_is_null_and_blocks_containing_period():
    # A 2m reservation would fake -20% through buying power: the endpoint is incomplete.
    held = nav('2026-10-13', 8_000_000, reasons=['TOSS_OPEN_ORDERS_PRESENT'])
    block = run([START, held], [record(START, held, '0')])
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert 'MISSING_END_NAV' in block['reason_codes'] and 'INSUFFICIENT_NAV_SNAPSHOTS' in block['reason_codes']
    assert block['intervals'] == [] and 'UNAVAILABLE' in block['explanation']
    # Bridged only with complete interval flows over the whole bridge.
    after = nav('2026-10-14', 10_200_000)
    bridged = run([START, held, after], [record(START, after, '0')])
    row = bridged['intervals'][0]
    assert bridged['period']['return_pct'] == 2.0 and row['bridged'] is True
    assert row['skipped_snapshots'][0]['reason_codes'] == ['TOSS_OPEN_ORDERS_PRESENT']
    unresolved = run([START, held, after], [record(START, held, None, reasons=['OPEN_ORDERS_AT_ENDPOINT']),
                                           record(held, after, '0')])
    assert unresolved['period']['return_pct'] is None
    assert {'UNRESOLVED_FLOW', 'OPEN_ORDERS_AT_ENDPOINT'} <= set(unresolved['reason_codes'])


def test_missing_snapshot_bridges_two_day_interval_without_daily_values():
    later = nav('2026-10-14', 10_200_000)
    block = run([START, later], [record(START, later, '0')])
    assert block['period']['return_pct'] == 2.0
    row = block['intervals'][0]
    assert row['bridged'] is True and row['skipped_snapshots'] == []
    assert 'MULTI_DAY_INTERVAL' in row['reason_codes'] and len(block['intervals']) == 1
    # Flow records must tile the bridged interval; a gap leaves it unresolved.
    gap = nav('2026-10-13', None, reasons=['MISSING_TOSS_NAV'])
    partial = run([START, gap, later], [record(gap, later, '0')])
    assert partial['period']['return_pct'] is None
    assert 'FLOW_RECORDS_DO_NOT_TILE_INTERVAL' in partial['intervals'][0]['reason_codes']
    tiled = run([START, gap, later], [record(START, gap, '0'), record(gap, later, '0')])
    assert tiled['period']['return_pct'] == 2.0


def test_unresolved_interval_nulls_period_and_shows_day_coverage_never_a_subset_return():
    days = [nav(f'2026-10-{d:02d}', v) for d, v in ((10, 100.0), (11, 110.0), (12, 121.0), (13, 133.1))]
    records = [record(days[0], days[1], '0'), record(days[1], days[2], None, reasons=['QUANTITY_UNRECONCILED']),
               record(days[2], days[3], '0')]
    block = run(days, records)
    assert block['period']['return_pct'] is None and block['label'] == 'unavailable'
    assert {'UNRESOLVED_INTERVAL_IN_PERIOD', 'UNRESOLVED_FLOW', 'QUANTITY_UNRECONCILED'} <= set(block['reason_codes'])
    assert [r['return_pct'] for r in block['intervals']] == [10.0, None, 10.0]  # the daily estimates stay visible
    assert 'latest_continuous_span' not in block and 'latest_continuous_span' not in block['coverage']
    coverage = block['coverage']
    assert (coverage['days_total'], coverage['days_resolved'], coverage['days_unresolved']) == (3, 2, 1)
    assert coverage['intervals_unresolved'] == 1 and coverage['unresolved_days_omitted'] == 0
    [span] = coverage['unresolved_days']
    assert (span['start_date'], span['end_date'], span['days']) == ('2026-10-11', '2026-10-12', 1)
    assert {'UNRESOLVED_FLOW', 'QUANTITY_UNRECONCILED'} <= set(span['reason_codes'])
    assert coverage['unresolved_days_by_reason']['QUANTITY_UNRECONCILED'] == 1
    text = block['explanation']
    assert text.startswith('UNAVAILABLE: no return for toss_investmcp 2026-10-10 to 2026-10-13: 2 of 3 days resolved, '
                           '1 unresolved (')
    assert 'A return over only the resolved days is never given' in text and 'coverage.unresolved_days' in text
    # Never the 21% over the resolved days: the only figures are the dated tracked window after the gap.
    before, tracked = text.split(' Tracked since ')
    tracked, after = tracked.split(' Unknown flows ')
    assert '%' not in before + after and '21' not in tracked
    assert tracked.startswith('2026-10-12 (to 2026-10-13): about 10.0% (estimate, flow-timing/FX range 10.0% to 10.0%)')
    linked = run(days, [record(a, b, '0') for a, b in zip(days, days[1:])])
    assert linked['period']['return_pct'] == 33.1
    assert (linked['coverage']['days_resolved'], linked['coverage']['unresolved_days']) == (3, [])


def test_day_coverage_names_missing_endpoints_and_caps_spans_in_summary(monkeypatch):
    days = [nav(f'2026-10-{d:02d}', v) for d, v in ((10, 100.0), (11, 110.0), (12, 121.0), (13, 133.1))]
    head = [nav('2026-10-09', None, reasons=['MISSING_TOSS_NAV'])] + days
    block = run(head, [record(a, b, '0') for a, b in zip(days, days[1:])])
    assert block['period']['return_pct'] is None and 'MISSING_START_NAV' in block['reason_codes']
    assert (block['coverage']['days_total'], block['coverage']['days_resolved']) == (4, 3)
    assert block['coverage']['unresolved_days'] == [
        {'start_date': '2026-10-09', 'end_date': '2026-10-10', 'days': 1, 'reason_codes': ['MISSING_START_NAV']}]
    assert '3 of 4 days resolved, 1 unresolved (MISSING_START_NAV 1 d)' in block['explanation']
    # Every other interval unresolved: summary keeps the latest spans, totals still count every day.
    monkeypatch.setattr(ir, 'SUMMARY_INTERVALS', 2)
    first = datetime.fromisoformat('2026-08-01')
    many = [nav((first + timedelta(days=i)).date().isoformat(), 100.0) for i in range(11)]
    records = [record(a, b, None if i % 2 else '0', reasons=['QUANTITY_UNRECONCILED'] if i % 2 else [])
               for i, (a, b) in enumerate(zip(many, many[1:]))]
    summary = run(many, records, detail_level='summary')['coverage']
    full = run(many, records, detail_level='full')['coverage']
    assert (summary['days_total'], summary['days_resolved'], summary['days_unresolved']) == (10, 5, 5)
    assert len(full['unresolved_days']) == 5 and full['unresolved_days_omitted'] == 0
    assert summary['unresolved_days'] == full['unresolved_days'][-2:] and summary['unresolved_days_omitted'] == 3
    assert summary['unresolved_days_by_reason']['QUANTITY_UNRECONCILED'] == 5


def chain(values, first='2026-10-10'):
    start = datetime.fromisoformat(first)
    return [nav((start + timedelta(days=i)).date().isoformat(), v) for i, v in enumerate(values)]


def test_tracked_run_starts_after_the_latest_gap_and_never_skips_it():
    days = chain([100.0, 110.0, 121.0, 133.1, 146.41])
    records = [record(days[0], days[1], '0'), record(days[1], days[2], None, reasons=['QUANTITY_UNRECONCILED']),
               record(days[2], days[3], '0'), record(days[3], days[4], '0')]
    block = run(days, records)
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert [row['label'] for row in block['intervals']] == ['estimate', 'unavailable', 'estimate', 'estimate']
    tracked = block['tracked']
    assert (tracked['start_date'], tracked['end_date'], tracked['intervals']) == ('2026-10-12', '2026-10-14', 2)
    assert (tracked['t0'], tracked['t1']) == (days[2]['valuation_at'], days[4]['valuation_at'])
    assert tracked['return_pct'] == 21.0 and tracked['label'] == 'estimate'  # 10/12 to 10/14, not the 46.41%
    assert tracked['sensitivity_bounds_pct'] == {'low': 21.0, 'high': 21.0} and tracked['reason_codes'] == []
    assert block['tracked_reason_codes'] == []
    # The unresolved interval is never linked over: a gap anywhere ends the run.
    records[3] = record(days[3], days[4], None, reasons=['QUANTITY_UNRECONCILED'])
    later = run(days, records)['tracked']
    assert (later['start_date'], later['end_date'], later['return_pct']) == ('2026-10-12', '2026-10-13', 10.0)


def test_tracked_run_ends_at_the_latest_resolved_interval_and_carries_its_reasons():
    days = chain([10_000_000, 10_000_000, 14_241_000, 14_383_410])
    head = [nav('2026-10-09', None, reasons=['MISSING_TOSS_NAV'])] + days
    records = [record(days[0], days[1], None, reasons=['QUANTITY_UNRECONCILED']),
               record(days[1], days[2], '4000000'), record(days[2], days[3], '0')]
    # Today has no snapshot yet: MISSING_END_NAV covers 10/13 to 10/14 but is no interval, so no break.
    block = ir.compute(head, records, 'toss_investmcp', '2026-10-09', '2026-10-14', accounts=[ACCOUNT])
    assert {'MISSING_START_NAV', 'MISSING_END_NAV'} <= set(block['reason_codes'])
    tracked = block['tracked']
    assert (tracked['start_date'], tracked['end_date'], tracked['intervals']) == ('2026-10-11', '2026-10-13', 2)
    assert tracked['end_date'] == block['intervals'][-1]['end_date']
    assert tracked['return_pct'] == 3.0284  # (1 + 2.0083%)(1 + 1%) - 1, linked like the period
    assert tracked['sensitivity_bounds_pct'] == {'low': 2.7386, 'high': 3.4341}
    assert {'FLOW_TIMING_UNKNOWN', 'MULTI_DAY_INTERVAL'} & set(tracked['reason_codes']) == {'FLOW_TIMING_UNKNOWN'}
    # Without the last snapshot the run ends at the latest resolved interval, a day earlier.
    shorter = ir.compute(head[:-1], records[:-1], 'toss_investmcp', '2026-10-09', '2026-10-14', accounts=[ACCOUNT])
    assert (shorter['tracked']['end_date'], shorter['tracked']['return_pct']) == ('2026-10-12', 2.0083)
    # A period that resolves completely tracks the whole period.
    whole = run(days[1:], records[1:])
    assert whole['label'] == 'estimate' and whole['tracked']['return_pct'] == whole['period']['return_pct']


def test_no_resolved_interval_leaves_tracked_null_with_a_reason():
    days = chain([100.0, 110.0, 121.0])
    block = run(days, [record(a, b, None, reasons=['QUANTITY_UNRECONCILED']) for a, b in zip(days, days[1:])])
    assert block['tracked'] is None and block['tracked_reason_codes'] == ['NO_RESOLVED_INTERVAL']
    assert [row['label'] for row in block['intervals']] == ['unavailable', 'unavailable']
    assert 'Tracked' not in block['explanation']
    single = run(days[:1], [])
    assert single['intervals'] == [] and single['tracked'] is None
    assert single['tracked_reason_codes'] == ['NO_RESOLVED_INTERVAL']
    for block in (ir.unavailable('crypto', '2026-10-10', '2026-10-12', ['LEDGERLESS_COMPUTATION_FAILED']),
                  ir.ledgerless_returns(None, [START, END], 'kis', '2026-10-12', '2026-10-13')):
        assert block['tracked'] is None and block['tracked_reason_codes'] == ['NO_RESOLVED_INTERVAL']


def test_interval_and_tracked_labels_follow_the_period_label_rule():
    stamp = midpoint(START, END)
    dated = record(START, END, '4000000', timing='known', confidence='observed', method='api_direct',
                   events=[event(stamp, '4000000')])
    later, last = nav('2026-10-14', 14_383_410), nav('2026-10-15', 14_000_000)
    held = [dated, record(END, later, '0'), record(later, last, None, reasons=['QUANTITY_UNRECONCILED'])]
    verified = run([START, END, later, last], held, accuracy=PASSED)
    assert [row['label'] for row in verified['intervals']] == ['verified', 'verified', 'unavailable']
    assert verified['tracked']['label'] == 'verified' and verified['tracked']['return_pct'] == 3.0284
    assert 'Tracked since 2026-10-12 (to 2026-10-14): 3.0284% (verified' in verified['explanation']
    # One inferred flow in the run makes that interval and the whole tracked window an estimate.
    guessed = run([START, END, later, last], [record(START, END, '4000000'), *held[1:]], accuracy=PASSED)
    assert [row['label'] for row in guessed['intervals']] == ['estimate', 'verified', 'unavailable']
    assert guessed['tracked']['label'] == 'estimate'
    assert [row['label'] for row in run([START, END, later, last], held)['intervals']] == [
        'estimate', 'estimate', 'unavailable']  # below the bar, never verified


def test_explanation_names_the_tracked_window_and_why_the_period_is_unavailable():
    days = chain([100.0, 110.0, 121.0, 133.1])
    head = [nav('2026-10-01', None, reasons=['MISSING_TOSS_NAV'])] + days
    block = ir.compute(head, [record(a, b, '0') for a, b in zip(days, days[1:])], 'toss_investmcp',
                       '2026-10-01', '2026-10-14', accounts=[ACCOUNT])
    text = block['explanation']
    assert block['label'] == 'unavailable' and block['tracked']['return_pct'] == 33.1
    assert text.startswith('UNAVAILABLE: no return for toss_investmcp 2026-10-01 to 2026-10-14: 3 of 13 days '
                           'resolved, 10 unresolved (MISSING_START_NAV 9 d, MISSING_END_NAV 1 d). '
                           'A return over only the resolved days is never given')
    assert ('the rest is not. Tracked since 2026-10-10 (to 2026-10-13): about 33.1% (estimate, flow-timing/FX '
            'range 33.1% to 33.1%) over 3 consecutive resolved intervals with no unresolved day between, a separate '
            'dated window (tracked), not this period\'s return; the full period is unavailable because 10 of its 13 '
            'days are unresolved (MISSING_START_NAV 9 d, MISSING_END_NAV 1 d). Unknown flows are never treated as '
            'zero; ') in text
    assert text.count('Tracked since') == 1 and block['period']['return_pct'] is None
    # An available period keeps its existing sentence and adds none.
    whole = run(days, [record(a, b, '0') for a, b in zip(days, days[1:])])
    assert whole['explanation'].startswith('ESTIMATE, not verified') and 'Tracked' not in whole['explanation']


def income(n0, n1, amount, *, confidence='observed', reasons=(), external=False, kind='income_other'):
    flow = amount if external else '0'
    return record(n0, n1, flow, confidence=confidence, income=amount, reasons=reasons,
                  events=[{'time': None, 'amount_native': amount, 'kind': kind, 'external': external}])


def test_income_other_and_income_native_are_performance_never_flow():
    paid = nav('2026-10-13', 10_020_000)
    # A small residual booked as income (rule R8): conditional, its reasons carried on the interval.
    booked = run([START, paid], [income(START, paid, '20000', confidence='conditional', reasons=['PROVIDER_CODE'])],
                 accuracy=PASSED)
    row = booked['intervals'][0]
    assert booked['period']['return_pct'] == 0.2 and row['flows_krw'] == 0.0  # in the gain, not the denominator
    assert row['income_native'] == {'KRW': '20000'} and 'PROVIDER_CODE' in row['reason_codes']
    assert booked['label'] == 'estimate' and row['confidence'] == 'conditional'
    assert booked['coverage']['resolved_interval_reasons']['PROVIDER_CODE'] == 1
    assert booked['explanation'].startswith('ESTIMATE, not verified')
    observed = run([START, paid], [income(START, paid, '20000')], accuracy=PASSED)
    assert observed['period']['return_pct'] == 0.2 and observed['label'] == 'verified'
    # Income marked as an external flow is a contradiction, never arithmetic.
    for kind in ('income_other', 'dividend'):
        wrong = run([START, paid], [income(START, paid, '20000', external=True, kind=kind)])
        assert wrong['period']['return_pct'] is None
        assert {'INVALID_FLOW_RECORD', 'FLOW_EVENT_UNCLASSIFIED_EXTERNAL'} <= set(wrong['intervals'][0]['reason_codes'])


@pytest.mark.parametrize('mutate, code', [
    (lambda r: r.update(flow_native=4000000.0), 'FLOW_RECORD_NUMBER_TYPE'),
    (lambda r: r.update(timing='sometimes'), 'FLOW_RECORD_ENUM_UNKNOWN'),
    (lambda r: r.update(source='upbit'), 'FLOW_RECORD_SOURCE_UNKNOWN'),
    (lambda r: r.pop('inputs'), 'FLOW_RECORD_SHAPE'),
    (lambda r: r.update(confidence='observed', flow_native=None), 'FLOW_RECORD_CONFIDENCE_INCONSISTENT'),
    (lambda r: r.update(t1=r['t0']), 'FLOW_RECORD_TIME_INVALID'),
    (lambda r: r.update(timing='known', events=[event(END['valuation_at'], '1')]), 'FLOW_EVENTS_INCONSISTENT'),
    (lambda r: r.update(timing='known', events=[event('2026-10-20T00:00:00+09:00', '4000000')]), 'FLOW_EVENT_TIME_INVALID'),
    (lambda r: r.update(events=[{'time': None, 'amount_native': '1', 'kind': 'gift', 'external': True}]), 'FLOW_EVENT_SHAPE'),
])
def test_strict_record_validator_blocks_interval(mutate, code):
    bad = record(START, END, '4000000')
    mutate(bad)
    block = run([START, END], [bad])
    assert block['period']['return_pct'] is None
    assert {'INVALID_FLOW_RECORD', code} <= set(block['intervals'][0]['reason_codes'])


def test_duplicates_are_idempotent_but_conflicts_block():
    same = record(START, END, '4000000')
    assert run([START, END], [same, dict(same)])['period']['return_pct'] == 2.0083
    other = record(START, END, '3000000')
    block = run([START, END], [same, other])
    assert block['period']['return_pct'] is None and 'DUPLICATE_FLOW_RECORD' in block['reason_codes']


def test_missing_required_currency_or_provider_blocks():
    with_usd = nav('2026-10-13', 14_241_000, usd=10.0)
    block = run([START, with_usd], [record(START, with_usd, '4000000')])
    assert 'FLOW_RECORDS_MISSING' in block['intervals'][0]['reason_codes']
    ok = run([START, with_usd], [record(START, with_usd, '4000000'), record(START, with_usd, '0', currency='USD')])
    assert ok['period']['return_pct'] == 2.0083
    none = run([START, END], [], provider_reasons=['FLOW_INFERENCE_UNAVAILABLE'])
    assert none['label'] == 'unavailable'
    assert {'FLOW_INFERENCE_UNAVAILABLE', 'FLOW_RECORDS_MISSING'} <= set(none['reason_codes'])
    # Another account's record is outside the scope and is never borrowed.
    elsewhere = run([START, END], [record(START, END, '4000000', account='toss:secondary')])
    assert 'FLOW_RECORDS_MISSING' in elsewhere['reason_codes']


def test_non_positive_denominators():
    empty = nav('2026-10-12', 0.0)
    block = run([empty, nav('2026-10-13', 0.0)], [record(empty, nav('2026-10-13', 0.0), '-10')])
    assert 'NON_POSITIVE_DIETZ_DENOMINATOR' in block['intervals'][0]['reason_codes']
    assert block['period']['return_pct'] is None
    # Midpoint denominator positive, start-weight corner -50: an admissible timing makes the
    # denominator nonpositive, so the interval is null (never the +200% midpoint).
    small, drained = nav('2026-10-12', 100.0), nav('2026-10-13', 0.0)
    block = run([small, drained], [record(small, drained, '-150')])
    row = block['intervals'][0]
    assert row['return_pct'] is None and row['sensitivity_bounds_pct'] is None
    assert row['confidence'] == 'unresolved' and 'SENSITIVITY_DENOMINATOR_NON_POSITIVE' in row['reason_codes']
    assert block['period']['return_pct'] is None and block['label'] == 'unavailable'
    # Same class through FX-mark ranges: a USD withdrawal whose KRW range crosses the denominator.
    # 1000 USD leaves one minute after t0: KRW 1.449m at the mean mark keeps the denominator
    # positive, the 1500 end mark does not.
    rich, poor = nav('2026-10-12', 1_460_000, usd=1.0, fx=1400), nav('2026-10-13', 0.0, usd=1.0, fx=1500)
    fx_cross = run([rich, poor], [record(rich, poor, '0'), record(
        rich, poor, '-1000', currency='USD', timing='known', events=[event(at('2026-10-12', '18:11'), '-1000', 'withdrawal')])])
    assert fx_cross['period']['return_pct'] is None
    assert 'SENSITIVITY_DENOMINATOR_NON_POSITIVE' in fx_cross['intervals'][0]['reason_codes']
    # Too many uncertain flows to enumerate corners still requires a positive denominator range.
    many = [event(None, '-10', 'withdrawal') for _ in range(13)]
    crowded = run([small, nav('2026-10-13', 0.0)], [record(small, nav('2026-10-13', 0.0), '-130', events=many)])
    assert crowded['period']['return_pct'] is None


def test_usd_flow_fx_event_rate_versus_endpoint_marks():
    start, end = nav('2026-10-12', 10_000_000, usd=1.0, fx=1400), nav('2026-10-13', 11_420_000, usd=1.0, fx=1420)
    marks = run([start, end], [record(start, end, '0'), record(start, end, '1000', currency='USD')])
    row = marks['intervals'][0]
    assert row['flows_krw'] == 1_410_000 and 'FLOW_FX_FROM_SNAPSHOT_MARKS' in row['reason_codes']
    low, high = row['sensitivity_bounds_pct']['low'], row['sensitivity_bounds_pct']['high']
    assert low < row['return_pct'] < high
    timed = record(start, end, '1000', currency='USD', timing='known', confidence='observed', method='api_direct',
                   events=[{**event(midpoint(start, end), '1000'), 'fx_to_krw': '1410'}])
    exact = run([start, end], [record(start, end, '0'), timed])
    assert exact['intervals'][0]['sensitivity_bounds_pct']['low'] == exact['intervals'][0]['return_pct']
    no_marks = dict(end, fx_times={})
    missing = run([start, no_marks], [record(start, no_marks, '0'), record(start, no_marks, '1000', currency='USD')])
    assert 'MISSING_FLOW_FX' in missing['intervals'][0]['reason_codes']


def test_xirr_only_for_dated_observed_flows_and_labels():
    stamp = midpoint(START, END)
    dated = record(START, END, '4000000', timing='known', confidence='observed', method='api_direct',
                   events=[event(stamp, '4000000')])
    later = nav('2026-11-13', 14_500_000)
    flows = [dated, record(END, later, '0')]
    observed = run([START, END, later], flows, accuracy=PASSED)
    assert observed['label'] == 'verified' and observed['period']['xirr_pct'] is not None
    assert observed['explanation'].startswith('VERIFIED')
    estimate = run([START, END, later], flows)
    assert estimate['label'] == 'estimate' and estimate['accuracy']['passed_bar'] is False
    assert estimate['accuracy']['error_basis'] == 'point_estimate' and estimate['accuracy']['basis'] == 'statement-simulated'
    assert ('backtest p95 daily error 1.528 bp (point estimate assuming statement-simulated cash, date-only statement '
            'rows at 12:00 KST, constant bond level; not an error bound) on statement-simulated data, max monthly '
            'drift 27.292 bp over the 4 of 13 months with every interval resolved (34.572 bp on resolved days only, '
            'a diagnostic), 249 of 255 intervals scored, 231 resolved, dividends not modelled (a paid dividend counts '
            'as a deposit), sampled timing/level sensitivities in '
            'accuracy.sensitivity_not_bound (sensitivity, not a bound), below the promotion bar') in estimate['explanation']
    assert 'worst' not in json.dumps(estimate['accuracy']) + estimate['explanation']
    unscored = {'toss_investmcp': {'status': 'measured', 'passed_bar': False, 'basis': 'statement-simulated',
                                   'p95_daily_error_bp': 3.6, 'max_abs_monthly_drift_bp': None,
                                   'monthly_drift_basis': 'months_with_every_interval_resolved', 'months_scored': 0,
                                   'months': 2}}
    measured = run([START, END, later], flows, accuracy=unscored)
    assert ('backtest p95 daily error 3.6 bp on statement-simulated data, monthly drift unmeasured (none of 2 months '
            'had every interval resolved), below the promotion bar') in measured['explanation']
    guessed = run([START, END, later], [record(START, END, '4000000'), record(END, later, '0')], accuracy=PASSED)
    assert guessed['label'] == 'estimate' and guessed['period']['xirr_pct'] is None
    assert guessed['period']['xirr_reason_codes'] == ['XIRR_REQUIRES_DATED_COMPLETE_FLOWS']


def test_unsupported_scopes_are_null_with_reason():
    block = ir.ledgerless_returns(None, [START, END], 'unknown_scope', '2026-10-12', '2026-10-13')
    assert block['label'] == 'unavailable' and block['reason_codes'] == ['SCOPE_NOT_YET_SUPPORTED_LEDGERLESS']
    assert block['period']['return_pct'] is None
    # Equity is supported: Toss + KIS levels; rows without either part leave it null, naming each.
    block = ir.ledgerless_returns(None, [START, END], 'equity', '2026-10-12', '2026-10-13')
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert block['included_scopes'] == ['toss', 'kis']
    assert [entry['scope'] for entry in block['missing_scopes']] == ['toss', 'kis']
    # consolidated is supported (2.13.1): rows without KIS/crypto data leave it null, naming each scope.
    block = ir.ledgerless_returns(None, [START, END], 'consolidated', '2026-10-12', '2026-10-13')
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert [entry['scope'] for entry in block['missing_scopes']] == ['toss', 'kis', 'crypto']
    # kis is supported from its first capture on; earlier days are null, never back-filled.
    block = ir.ledgerless_returns(None, [START, END], 'kis', '2026-10-12', '2026-10-13')
    assert block['label'] == 'unavailable' and 'KIS_CAPTURE_MISSING' in block['reason_codes']
    assert block['period']['return_pct'] is None


def test_summary_detail_caps_interval_table():
    first = datetime.fromisoformat('2026-08-01')
    days = [nav((first + timedelta(days=i)).date().isoformat(), 100.0 + i) for i in range(40)]
    records = [record(a, b, '0') for a, b in zip(days, days[1:])]
    summary = run(days, records, detail_level='summary')
    assert len(summary['intervals']) == ir.SUMMARY_INTERVALS and summary['intervals_omitted'] == 39 - ir.SUMMARY_INTERVALS
    assert summary['period']['return_pct'] == 39.0
    assert len(run(days, records, detail_level='full')['intervals']) == 39


def test_provider_loading_is_bounded_and_fail_closed(monkeypatch):
    calls = []

    def provider(db_path, start, end, accounts=None, budget_seconds=None):
        calls.append((start, end, accounts, budget_seconds))
        return []
    records, reasons = ir.load_records('db', 'toss_investmcp', '2026-10-12', '2026-10-13',
                                       accounts=[ACCOUNT], budget_seconds=0.5, providers={'toss': provider})
    assert records == [] and reasons == [] and calls == [('2026-10-12', '2026-10-13', [ACCOUNT], 0.5)]

    release = threading.Event()

    def slow(db_path, start, end):
        release.wait(5)
        return []
    assert ir.load_records('db', 'crypto', 'a', 'b', budget_seconds=0.01, providers={'crypto': slow}) == (
        None, ['FLOW_READ_BUDGET_EXCEEDED'])
    release.set()

    def broken(db_path, start, end):
        raise RuntimeError('secret detail must not leak')
    assert ir.load_records('db', 'crypto', 'a', 'b', providers={'crypto': broken}) == (None, ['FLOW_INFERENCE_FAILED'])
    assert ir.load_records('db', 'crypto', 'a', 'b', providers={}) == (None, ['FLOW_INFERENCE_UNAVAILABLE'])
    monkeypatch.setattr(ir, 'PROVIDERS', {'toss': ('module_that_does_not_exist', 'f'), 'crypto': ('nope', 'f')})
    assert ir.load_records('db', 'toss', 'a', 'b') == (None, ['FLOW_INFERENCE_UNAVAILABLE'])


@pytest.fixture
def wired(tmp_path, monkeypatch):
    return base_wired.__wrapped__(tmp_path, monkeypatch)


def captured_records(navs, accounts):
    """Synthetic provider output for real captured Toss NAV rows: a KRW 200 deposit of unknown time."""
    out = []
    for account in accounts:
        for currency in ('KRW', 'USD'):
            flow = '200' if (account, currency) == ('toss:primary', 'KRW') else '0'
            out.append(record({'valuation_at': navs[0]['valuation_at'], 'snapshot_id': navs[0]['snapshot_id']},
                              {'valuation_at': navs[1]['valuation_at'], 'snapshot_id': navs[1]['snapshot_id']},
                              flow, currency=currency, account=account))
            out[-1]['inputs'] = {'snapshot_versions': [navs[0].get('version'), navs[1].get('version')]}
    return out


def test_real_scope_navs_path_with_captured_toss_rows(tmp_path, monkeypatch):
    navs = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    seen = []

    def provider(db_path, start, end, accounts=None):
        seen.append(accounts)
        return captured_records(navs, accounts)
    block = ir.ledgerless_returns(None, navs, 'toss_investmcp', NAV_START, NAV_END,
                                  account_references={'toss:primary'}, providers={'toss': provider})
    assert seen == [['toss:primary']]
    # (4050 - 3500 - 200) / (3500 + 0.5 * 200); start/end weights give 10% and 9.4595%.
    assert block['period']['return_pct'] == 9.7222 and block['label'] == 'estimate'
    assert block['period']['sensitivity_bounds_pct'] == {'low': 9.4595, 'high': 10.0}
    assert block['intervals'][0]['flows_native'] == {'KRW': '200', 'USD': '0'}
    missing_usd = ir.ledgerless_returns(None, navs, 'toss_investmcp', NAV_START, NAV_END,
                                        account_references={'toss:primary'},
                                        providers={'toss': lambda *a, **k: captured_records(navs, ['toss:primary'])[:1]})
    assert missing_usd['period']['return_pct'] is None
    assert 'FLOW_RECORDS_MISSING' in missing_usd['intervals'][0]['reason_codes']
    # Fewer than two usable scoped NAVs never calls the provider.
    calls = []
    lone = ir.ledgerless_returns(None, navs[:1], 'toss_investmcp', NAV_START, NAV_END,
                                 account_references={'toss:primary'}, providers={'toss': lambda *a: calls.append(a)})
    assert calls == [] and lone['label'] == 'unavailable'


def test_tool_block_is_additive_and_unsupported_scope_is_null(wired, monkeypatch):
    import server
    before = json.dumps(wired.navs, sort_keys=True)
    first = server.get_portfolio_performance(input_snapshot_ids=['synthetic'])
    history = server.get_portfolio_history(input_snapshot_ids=['synthetic'], portfolio_scope='consolidated')
    assert first['ok'] and history['ok']
    for data in (first['data'], history['data']):
        block = data['ledgerless_returns']
        assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
        assert block['method'] == 'modified_dietz_linked_twr_estimate'
    # Equity: supported; the synthetic server configures neither Toss nor KIS and funds no part.
    assert first['data']['ledgerless_returns']['scope'] == 'equity'
    assert first['data']['ledgerless_returns']['reason_codes'] == ['CONSOLIDATED_NO_INCLUDED_SCOPE']
    assert history['data']['ledgerless_returns']['scope'] == 'consolidated'
    monkeypatch.setattr(server, '_ledgerless_block', lambda *args: None)
    second = server.get_portfolio_performance(input_snapshot_ids=['synthetic'])
    strip = {k: v for k, v in first['data'].items() if k != 'ledgerless_returns'}
    assert json.dumps(strip, sort_keys=True, default=str) == json.dumps(
        {k: v for k, v in second['data'].items() if k != 'ledgerless_returns'}, sort_keys=True, default=str)
    assert json.dumps(wired.navs, sort_keys=True) == before


def test_tool_block_for_toss_scope_uses_stored_versions(wired, tmp_path, monkeypatch):
    import server
    rows = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    wired.navs[:] = rows
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (NAV_START, NAV_END))
    monkeypatch.setattr(ir, '_provider', lambda name: (lambda db_path, start, end, accounts=None:
                                                       captured_records(rows, accounts)))
    for tool in (server.get_portfolio_performance, server.get_portfolio_history):
        result = tool(input_snapshot_ids=['synthetic'], portfolio_scope='toss', detail_level='full')
        assert result['ok'], result.get('error')
        block = result['data']['ledgerless_returns']
        # Toss scope: 6500 -> 7050 with the 200 deposit; (7050 - 6500 - 200) / (6500 + 100).
        assert block['period']['return_pct'] == 5.303 and block['label'] == 'estimate'
        assert block['period']['sensitivity_bounds_pct'] == {'low': 5.2239, 'high': 5.3846}
        assert block['explanation'].startswith('ESTIMATE, not verified')
        assert block['accuracy']['status'] == 'unmeasured'


def investmcp_identity(db, nav_key, *, label='InvestMCP 전용', bind=True):
    """Owner-export identity rows only (salt, import, alias); no flow coverage rows."""
    from owner_flows import SCHEMA
    from portfolio_ledger import LedgerStore
    store = LedgerStore(db)
    with store.transaction() as conn:
        for sql in SCHEMA:
            conn.execute(sql)
        statement = 'toss:000-00-000000'  # synthetic statement account
        if bind:
            store.bind_account(conn, nav_key, statement)
        conn.execute('INSERT INTO ledger_owner_export_imports VALUES (?,?,?,?,?,?,?,?)',
                     ('import-synthetic', 'sha-synthetic', 'synthetic', 'toss_pdf', store.account_identity(statement, conn),
                      label, '2026-01-01T00:00:00+09:00', '2026-01-31T23:59:59+09:00'))


def test_toss_investmcp_ledgerless_selects_toss_primary_without_flow_coverage(wired, tmp_path, monkeypatch):
    import ledger_returns
    import server
    rows = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    db = tmp_path / 'identity.db'
    investmcp_identity(db, None, bind=False)  # as on live: label-keyed NAVs, the import identity has no alias
    before = db.read_bytes()
    flows = ledger_returns.read_flows(db, NAV_START, NAV_END, 'toss_investmcp', rows)
    assert flows['coverage']['status'] == 'unavailable' and flows['_nav_accounts'] == []
    assert ir.scope_accounts(db, rows, 'toss_investmcp') == [ledger_returns.INVESTMCP_TOSS_ACCOUNT_KEY] == ['toss:primary']
    seen = []

    def provider(db_path, start, end, accounts=None):
        seen.append(accounts)
        return captured_records(rows, accounts)
    block = ir.ledgerless_returns(db, rows, 'toss_investmcp', NAV_START, NAV_END, providers={'toss': provider})
    assert seen == [['toss:primary']] and block['label'] == 'estimate'
    assert block == ir.ledgerless_returns(None, rows, 'toss_investmcp', NAV_START, NAV_END,
                                          account_references={'toss:primary'}, providers={'toss': provider})
    # Tool path, including a flow read that yields no account list (as on live): same selection.
    wired.navs[:] = rows
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', db)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (NAV_START, NAV_END))
    monkeypatch.setattr(ir, '_provider', lambda name: provider)
    for flows in (ledger_returns.read_flows, lambda path, start, end, scope, navs=(): ledger_returns._unavailable(
            start, end, scope, 'FLOW_READ_BUDGET_EXCEEDED')):
        monkeypatch.setattr(server, 'read_flows', flows)
        seen.clear()
        result = server.get_portfolio_performance(portfolio_scope='toss_investmcp')
        assert result['ok'], result.get('error')
        assert result['data']['ledger_flow_coverage']['status'] == 'unavailable'
        assert seen == [['toss:primary']] and result['data']['ledgerless_returns'] == block
    assert db.read_bytes() == before


def bind_elsewhere(db, key):
    from portfolio_ledger import LedgerStore
    store = LedgerStore(db)
    with store.transaction() as conn:
        store.bind_account(conn, key, 'toss:999-99-999999')  # another synthetic statement account


@pytest.mark.parametrize('case', ['no_db', 'no_import', 'other_label', 'no_salt', 'secondary_only', 'secondary_bound',
                                  'secondary_bound_no_primary', 'primary_bound_elsewhere', 'combined'])
def test_toss_investmcp_ledgerless_without_identity_is_unavailable_never_secondary(wired, tmp_path, monkeypatch, case):
    import server
    rows = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    db = tmp_path / 'identity.db'
    if case in ('secondary_bound_no_primary', 'combined'):  # review P1 (a), and with (b)
        investmcp_identity(db, 'toss:secondary')
        if case == 'combined':
            bind_elsewhere(db, 'toss:primary')
        else:
            rows = [{**row, 'account_nav': {k: v for k, v in row['account_nav'].items() if k != 'toss:primary'}} for row in rows]
    elif case == 'primary_bound_elsewhere':  # review P1 (b)
        investmcp_identity(db, None, bind=False)
        bind_elsewhere(db, 'toss:primary')
    elif case == 'other_label':
        investmcp_identity(db, None, label='Toss', bind=False)
    elif case == 'secondary_only':  # snapshots without toss:primary: the import never selects another account
        investmcp_identity(db, None, bind=False)
        rows = [{**row, 'account_nav': {k: v for k, v in row['account_nav'].items() if k != 'toss:primary'}} for row in rows]
    elif case == 'secondary_bound':  # an alias contradicting the fixed key: no number, never secondary
        investmcp_identity(db, 'toss:secondary')
    elif case == 'no_import':
        investmcp_identity(db, 'toss:secondary')
        with sqlite3.connect(db) as conn:
            conn.execute('DROP TABLE ledger_owner_export_imports')
    elif case == 'no_salt':
        from owner_flows import SCHEMA
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE ledger_state(key TEXT PRIMARY KEY, value BLOB)')
            conn.execute(SCHEMA[0])
            conn.execute("INSERT INTO ledger_owner_export_imports VALUES ('i','s','p','toss_pdf','acct','InvestMCP 전용','a','b')")
    calls = []
    block = ir.ledgerless_returns(db, rows, 'toss_investmcp', NAV_START, NAV_END,
                                  providers={'toss': lambda *a, **k: calls.append(a)})
    assert calls == [] and block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert block['reason_codes'] == block['period']['reason_codes'] == ['INVESTMCP_ACCOUNT_IDENTITY_UNAVAILABLE']
    assert ir.scope_accounts(db, rows, 'toss_investmcp') == []
    wired.navs[:] = rows
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', db)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (NAV_START, NAV_END))
    monkeypatch.setattr(ir, '_provider', lambda name: lambda *a, **k: calls.append(a))
    for tool in (server.get_portfolio_performance, server.get_portfolio_history):
        result = tool(portfolio_scope='toss_investmcp')
        assert result['ok'], result.get('error')
        assert result['data']['ledgerless_returns']['reason_codes'] == ['INVESTMCP_ACCOUNT_IDENTITY_UNAVAILABLE']
    assert calls == []


def fill_coverage(db):
    from portfolio_ledger import LedgerStore
    store = LedgerStore(db)
    for account in ('toss:primary', 'toss:secondary'):
        store.record_coverage('toss', store.account_identity(account), 'toss/fills-v1/synthetic',
                              NAV_START + 'T09:10:00+00:00', NAV_END + 'T09:10:00+00:00', 'complete', [], 0, 'synthetic-run')


def complete_schedule(snapshots, start, end):
    return {'rows': [], 'coverage': [{'symbol': 'SYN', 'currency': 'USD', 'start': '2026-10-01', 'end': '2026-10-15',
                                      'status': 'complete'}]}


def w1_schedule(status):
    """Stand-ins for the production provider (toss_flow_inference.dividend_schedule_provider) outputs."""
    from toss_dividends import ABSENCE_ESTIMATED, PAYMENT_POLICY
    proof = {'symbol': 'SYN', 'currency': 'USD', 'start': '2026-10-01', 'end': '2026-10-15'}
    proof.update({'pending': {'status': 'limited', 'reasons': ['PUBLIC_DIVIDEND_REFRESH_PENDING']},
                  'estimated': {'status': 'estimated', 'basis': PAYMENT_POLICY, 'reasons': [ABSENCE_ESTIMATED]}}[status])
    seen = []

    def provider(snapshots, start, end, *, fills=()):
        seen.append(fills)
        return {'rows': [], 'coverage': [dict(proof)]}
    provider.seen = seen
    return provider


def test_tool_block_runs_w1_toss_interval_flows_on_stored_evidence(wired, tmp_path, monkeypatch):
    import server
    rows = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    db = tmp_path / 'synthetic-nav.db'
    fill_coverage(db)
    wired.navs[:] = rows
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', db)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (NAV_START, NAV_END))
    before = db.read_bytes()
    # Owner decision 10/17: the tool path models no dividends and never calls the public schedule provider.
    import toss_flow_inference

    def no_schedule(*args, **kwargs):
        raise AssertionError('dividend schedule fetched on the tool path')
    monkeypatch.setattr(toss_flow_inference, 'dividend_schedule_provider', no_schedule)
    monkeypatch.setattr(ir, 'ACCURACY', {'toss': {'status': 'measured', 'passed_bar': True}})
    for tool in (server.get_portfolio_performance, server.get_portfolio_history):
        block = tool(input_snapshot_ids=['synthetic'], portfolio_scope='toss', detail_level='full')['data'][
            'ledgerless_returns']
        # Rule R8 books the KRW 1000 -> 1350 cash residual (350 <= 5,000 KRW) as income, never a
        # flow: (7050 - 6500) / 6500 keeps it in the gain with its reason carried. The conditional-only
        # records keep it an estimate even on a scope that passed the bar.
        assert block['label'] == 'estimate' and block['period']['return_pct'] == 8.4615
        assert block['period']['sensitivity_bounds_pct'] == {'low': 8.4615, 'high': 8.4615}
        row = block['intervals'][0]
        assert row['flows_native'] == {'KRW': '0', 'USD': '0'} and row['income_native'] == {'KRW': '350'}
        assert row['flows_krw'] == 0.0 and row['confidence'] == 'conditional'
        assert {'DIVIDENDS_COUNTED_AS_FLOWS', 'SMALL_RESIDUAL_TREATED_AS_INCOME'} <= set(row['reason_codes'])
        assert not any(code.startswith('DIVIDEND_') for code in row['reason_codes'])
        assert block['coverage']['resolved_interval_reasons']['SMALL_RESIDUAL_TREATED_AS_INCOME'] == 1
    assert db.read_bytes() == before


def test_inference_unavailable_sentinels_never_become_intervals(tmp_path, monkeypatch):
    rows = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    absent = tmp_path / 'absent.db'
    block = ir.ledgerless_returns(absent, rows, 'toss', NAV_START, NAV_END, schedule_provider=complete_schedule)
    assert not absent.exists() and block['label'] == 'unavailable'
    assert 'LOCAL_TOSS_EVIDENCE_UNAVAILABLE' in block['reason_codes'] and block['intervals'][0]['return_pct'] is None


def crypto_navs(db):
    with sqlite3.connect(db) as conn:
        return [json.loads(r[0]) for r in conn.execute(
            'SELECT payload_json FROM nav_snapshot_versions ORDER BY snapshot_date')]


def test_crypto_block_runs_w2_crypto_interval_flows(tmp_path):
    from test_crypto_flow_intervals import T0 as CRYPTO_T0
    from test_crypto_flow_intervals import bn_transfer
    from test_crypto_flow_intervals import setup as crypto_setup
    store = crypto_setup(tmp_path)
    stamp = CRYPTO_T0 + timedelta(minutes=1)
    bn_transfer(store, stamp=stamp)
    block = ir.ledgerless_returns(store.db_path, crypto_navs(store.db_path), 'crypto', '2026-10-13', '2026-10-14',
                                  detail_level='full')
    # Both venues 280000 KRW at each end; 50 USDT enters UM futures at event FX 1400, 1 minute after t0.
    weight = Decimal(1439) / Decimal(1440)
    expected = float(round(Decimal(-70000) / (Decimal(280000) + weight * 70000) * 100, 4))
    assert block['label'] == 'estimate' and block['period']['return_pct'] == expected
    assert block['intervals'][0]['flows_krw'] == 70000 and block['intervals'][0]['timing'] == 'known'


def test_crypto_flow_without_event_fx_is_never_observed_at_nav_marks(tmp_path):
    from test_crypto_flow_intervals import T0 as CRYPTO_T0
    from test_crypto_flow_intervals import bn_transfer
    from test_crypto_flow_intervals import setup as crypto_setup
    store = crypto_setup(tmp_path)
    bn_transfer(store, stamp=CRYPTO_T0 + timedelta(minutes=10))  # newest mark is older than five minutes
    block = ir.ledgerless_returns(store.db_path, crypto_navs(store.db_path), 'crypto', '2026-10-13', '2026-10-14',
                                  detail_level='full')
    row = block['intervals'][0]
    # A stablecoin between two capture marks is interpolated: an estimate at best.
    assert block['label'] == 'estimate' and row['confidence'] == 'conditional' and row['return_pct'] is not None
    assert row['reason_codes'] == ['CRYPTO_EVENT_FX_INTERPOLATED']


@pytest.mark.parametrize('funded', [None, 'BTC'])
def test_live_zero_binance_assets_keep_crypto_returns_in_both_tools(wired, tmp_path, monkeypatch, funded):
    """Live 11-asset V3 shape: ten proven zeros need no FX; a funded unmarked asset still voids returns."""
    from copy import deepcopy

    from test_crypto_flow_intervals import coverage
    from test_crypto_nav_capture import NOW, live_shape, nav, quote_client, zero_asset
    from test_crypto_nav_capture import capture as crypto_capture

    import ledger_returns as lr
    from crypto_flow_intervals import EVIDENCE_PAD
    from portfolio_ledger import LedgerStore
    from portfolio_store import NavStore
    path, rows = tmp_path / 'crypto.db', []
    for day in range(2):
        stamp = NOW + timedelta(days=day)
        data = live_shape()
        data['spot']['tokenToAvailableAfterMaintenance'] = [[0, '20.0']]
        if funded:
            next(r for r in data['account']['assets'] if r['asset'] == funded).update(zero_asset(funded, '1'))
        crypto_capture(path, data, clock=stamp)
        fx = quote_client().read()  # USDT/USDC marks only, as in production
        for mark in fx.values():
            mark.update(priced_at=stamp.isoformat(), observed_at=stamp.isoformat(), read_at=stamp.isoformat())
        row = {'snapshot_id': f'crypto-{day}', 'snapshot_date': stamp.date().isoformat(), 'version': 1,
               'valuation_at': stamp.isoformat(), 'total_value_krw': 432000, 'result_status': 'complete',
               'reason_codes': [], 'crypto_nav': nav(path, fx=fx, stamp=stamp), 'fx_times': fx}
        assert len(row['crypto_nav']['binance']['by_currency']) == 11
        with NavStore(path)._connect() as conn:
            NavStore._insert_version(conn, row)
        rows.append(row)
    coverage(LedgerStore(path), start=NOW - EVIDENCE_PAD, end=NOW + timedelta(days=1) + EVIDENCE_PAD)
    wired.navs[:] = rows
    monkeypatch.setattr(type(wired.server._get_nav_store()), 'db_path', path)
    monkeypatch.setattr(wired.server.portfolio_engine, 'resolve_period',
                        lambda _: (rows[0]['snapshot_date'], rows[-1]['snapshot_date']))
    for tool in (wired.server.get_portfolio_performance, wired.server.get_portfolio_history):
        block = tool(input_snapshot_ids=['synthetic'], portfolio_scope='crypto')['data']['ledgerless_returns']
        if funded:
            assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
        else:
            assert block['label'] == 'estimate' and block['period']['return_pct'] == 0.0, block
    if funded:
        return
    assert all(r['total_value_krw'] == 432000 and r['reason_codes'] == [] for r in lr.scope_navs(rows, 'crypto'))
    # The exemption is exact: a non-zero, unknown, or provenance-only asset still needs an FX time.
    for mutate in (lambda bn: bn['by_currency']['BNB'].update(value=0.5, decimal_value='0.5'),
                   lambda bn: bn['by_currency']['BNB'].update(status='unknown'),
                   lambda bn: bn['by_currency']['BNB'].update(decimal_value='0.000000001'),
                   lambda bn: bn['accounts'].append(dict(bn['accounts'][0], asset='XAU'))):
        changed = deepcopy(rows)
        mutate(changed[0]['crypto_nav']['binance'])
        assert 'UNKNOWN_FX_PRICE_TIME' in lr.scope_navs(changed, 'crypto')[0]['reason_codes']


def test_slow_provider_wait_is_bounded_and_single_flight():
    release = threading.Event()

    def slow(db_path, start, end):
        release.wait(5)
        return []
    began = time.monotonic()
    assert ir.load_records('db', 'crypto', 'a', 'b', budget_seconds=0.05, providers={'crypto': slow}) == (
        None, ['FLOW_READ_BUDGET_EXCEEDED'])
    assert ir.load_records('db', 'crypto', 'a', 'b', budget_seconds=0.05, providers={'crypto': slow}) == (
        None, ['FLOW_INFERENCE_BUSY'])
    assert time.monotonic() - began < 1.0  # the provider would hold for 5 s
    release.set()
    assert ir.load_records('db', 'crypto', 'a', 'b', budget_seconds=1, providers={'crypto': lambda *a: []}) == ([], [])


def test_result_finished_after_its_deadline_is_discarded(monkeypatch):
    # N1: the worker's clock reads past the deadline, as when the calling thread is scheduled late.
    late = types.SimpleNamespace(monotonic=lambda: 105.0 if threading.current_thread().name.startswith('ledgerless')
                                 else 100.0)
    monkeypatch.setattr(ir, 'time', late)
    assert ir.load_records('db', 'crypto', 'a', 'b', budget_seconds=1, providers={'crypto': lambda *a: []}) == (
        None, ['FLOW_READ_BUDGET_EXCEEDED'])


def w1_snapshot(day, krw, usd='0', quantity='10'):
    when = datetime.fromisoformat(at(day))
    stamp = when.isoformat()
    return {'account': ACCOUNT, 'snapshot_id': 'snap-' + day, 'version': 1, 'time': stamp, 'complete': True,
            'reasons': [], 'open_order_count': 0,
            'holdings': [{'symbol': 'SYN', 'currency': 'USD', 'quantity': quantity, 'price': '10', 'priced_at': stamp}],
            'cash': {c: {'value': v, 'basis': 'TOSS_BUYING_POWER_NO_OPEN_ORDERS', 'observed_at': stamp}
                     for c, v in (('KRW', krw), ('USD', usd))},
            'fx': {'rate': '1400', 'valid_from': (when - timedelta(hours=12)).isoformat(),
                   'valid_until': (when + timedelta(hours=12)).isoformat(), 'observed_at': stamp}}


def test_inference_conditional_zero_flows_are_estimates_never_verified():
    from toss_flow_inference import infer_toss_flows
    snapshots = [w1_snapshot('2026-10-12', '1000'), w1_snapshot('2026-10-13', '1000')]
    coverage = [{'account': ACCOUNT, 'start': '2026-01-01T00:00:00+00:00', 'end': '2026-10-14T00:00:00+00:00',
                 'status': 'complete', 'contract': 'toss-fills-v1', 'coverage_id': 'c1', 'reasons': []}]
    navs = [dict(nav(day, value), version=1) for day, value in (('2026-10-12', 197_000), ('2026-10-13', 198_000))]
    for status in ('estimated', 'complete'):
        provider = w1_schedule('estimated') if status == 'estimated' else complete_schedule
        records = infer_toss_flows(snapshots, [], provider(snapshots, '2026-10-12', '2026-10-13'), coverage=coverage,
                                   accounts=[ACCOUNT])
        assert [r['flow_native'] for r in records] == ['0', '0']
        block = run(navs, records, accuracy=PASSED)
        assert block['period']['return_pct'] == round(1000 / 197_000 * 100, 4)
        # Resolution marks every resolved Toss record conditional, even a zero on a complete schedule
        # (test-only shape): never verified, whatever the bar.
        assert {r['confidence'] for r in records} == {'conditional'}
        assert block['label'] == 'estimate' and 'ESTIMATE, not verified' in block['explanation']
        if status == 'estimated':  # The production adapter only ever yields estimated absence
            assert 'DIVIDEND_ABSENCE_ESTIMATED_FROM_SCHEDULE' in block['intervals'][0]['reason_codes']


# ---- Review counterexamples, each swept over its input class ----

def versioned(day, value, version, **kwargs):
    return dict(nav(day, value, **kwargs), version=version)


def test_snapshot_binding_requires_exact_id_instant_and_version():
    other = record(START, END, '4000000', start_snapshot_id='different-start-version',
                   end_snapshot_id='different-end-version')
    late = record(START, END, '4000000', t0=midpoint(START, END))  # IDs name the endpoints, coverage starts 12 h late
    cases = [(other, 'FLOW_RECORD_SNAPSHOT_VERSION_MISMATCH'), (late, 'FLOW_RECORD_SNAPSHOT_TIME_MISMATCH'),
             (record(START, END, '4000000', start_snapshot_id=None), 'FLOW_RECORD_SNAPSHOT_UNBOUND'),
             (record(START, END, '4000000', start_snapshot_id='never-stored', t0=midpoint(START, END)),
              'FLOW_RECORD_SNAPSHOT_UNKNOWN')]
    for bad, code in cases:
        block = run([START, END], [bad])
        assert block['period']['return_pct'] is None and code in block['intervals'][0]['reason_codes'], code
    twin = dict(END, snapshot_id=START['snapshot_id'])
    assert 'FLOW_RECORD_SNAPSHOT_AMBIGUOUS' in run([START, twin], [record(START, END, '4000000')])['reason_codes']
    # Stored versions must match what the provider declares it read (a version list or *_snapshot_version keys).
    a, z = versioned('2026-10-12', 10_000_000, 2), versioned('2026-10-13', 14_241_000, 1)
    for inputs, expected in (({}, 'FLOW_RECORD_SNAPSHOT_VERSION_UNDECLARED'),
                             ({'snapshot_versions': [1, 1]}, 'FLOW_RECORD_SNAPSHOT_VERSION_MISMATCH'),
                             ({'start_snapshot_version': 2, 'end_snapshot_version': 2}, 'FLOW_RECORD_SNAPSHOT_VERSION_MISMATCH'),
                             ({'snapshot_versions': [2, 1]}, None),
                             ({'start_snapshot_version': 2, 'end_snapshot_version': 1}, None)):
        block = run([a, z], [record(a, z, '4000000', inputs=inputs)])
        if expected is None:
            assert block['period']['return_pct'] == 2.0083
        else:
            assert block['period']['return_pct'] is None and expected in block['reason_codes']
    # Overlapping records for one currency do not tile the interval contiguously.
    later = nav('2026-10-14', 10_200_000)
    gap = nav('2026-10-13', None, reasons=['MISSING_TOSS_NAV'])
    overlap = run([START, gap, later], [record(START, later, '0'), record(gap, later, '0')])
    assert 'FLOW_RECORDS_DO_NOT_TILE_INTERVAL' in overlap['intervals'][0]['reason_codes']


def test_tool_block_with_pinned_versions_never_borrows_representative_flows(wired, tmp_path, monkeypatch):
    import server
    rows = [capture(tmp_path, monkeypatch, day=day).row for day in (NAV_START, NAV_END)]
    db = tmp_path / 'synthetic-nav.db'
    fill_coverage(db)
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', db)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (NAV_START, NAV_END))
    import toss_flow_inference
    monkeypatch.setattr(toss_flow_inference, 'dividend_schedule_provider', w1_schedule('estimated'))
    # Pinned corrections at the same valuation instants: the inference reads the representatives.
    wired.navs[:] = [dict(r, snapshot_id='pinned-' + r['snapshot_id']) for r in rows]
    block = server.get_portfolio_performance(input_snapshot_ids=['synthetic'], portfolio_scope='toss',
                                             detail_level='full')['data']['ledgerless_returns']
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert 'FLOW_RECORD_SNAPSHOT_VERSION_MISMATCH' in block['intervals'][0]['reason_codes']


def crypto_record(flow, events, **extra):
    base = record(START, END, flow, currency='USDT', timing='known', confidence='observed', method='api_direct',
                  events=events, account='binance')
    base.update(source='binance', **extra)
    return base


def test_real_w2_net_zero_transfers_without_event_fx_resolve_only_as_an_estimate(tmp_path):
    from test_crypto_flow_intervals import T0 as CRYPTO_T0
    from test_crypto_flow_intervals import bn_transfer, result
    from test_crypto_flow_intervals import setup as crypto_setup
    store = crypto_setup(tmp_path)
    bn_transfer(store, ident=1, stamp=CRYPTO_T0 + timedelta(minutes=1))
    bn_transfer(store, ident=2, stamp=CRYPTO_T0 + timedelta(hours=2), route='UMFUTURE_MAIN')  # no event mark
    provided = result(store)[0]
    # In before out is no round trip; the unmarked leg is interpolated between the capture marks.
    assert (provided['flow_native'], provided['flow_krw'], provided['conversion_status']) == ('0', '0', 'interpolated')
    assert [e['fx']['source'] for e in provided['events']] == ['upbit_public_ticker', 'linear_interpolation']
    block = ir.ledgerless_returns(store.db_path, crypto_navs(store.db_path), 'crypto', '2026-10-13', '2026-10-14',
                                  detail_level='full')
    row = block['intervals'][0]
    assert block['label'] == 'estimate' and row['flows_krw'] == 0 and row['confidence'] == 'conditional'
    assert row['reason_codes'] == ['CRYPTO_EVENT_FX_INTERPOLATED']
    # Synthetic sweep of the event-time KRW contract.
    stamp = midpoint(START, END)
    marked = {**event(stamp, '50', 'transfer_in'), 'amount_krw': '70000'}
    unmarked = {**event(stamp, '-50', 'transfer_out'), 'amount_krw': None}
    blocked = [crypto_record('0', [marked, unmarked], flow_krw=None, conversion_status='unresolved'),
               crypto_record('0', [marked, unmarked], flow_krw=None, conversion_status='observed'),
               crypto_record('50', [marked], flow_krw=None),
               crypto_record('50', [marked], flow_krw='70000', conversion_status='pending')]
    for item in blocked:
        assert ir.validate_record(item)['blocked'] == ['FLOW_KRW_CONVERSION_UNRESOLVED']
    assert ir.validate_record(crypto_record('50', [marked], flow_krw='70000', conversion_status='observed'))['blocked'] == []
    with pytest.raises(ir.InvalidRecord, match='FLOW_KRW_INCONSISTENT'):
        ir.validate_record(crypto_record('50', [marked], flow_krw='1', conversion_status='observed'))


def test_unknown_time_gross_flows_never_net_to_known_zero():
    pair = [event(None, '1000'), event(None, '-1000', 'withdrawal')]
    a, z = nav('2026-10-12', 1000), nav('2026-10-13', 1100)
    # Reviewer case: the withdrawal may precede the deposit and empty the account, so no number.
    tight = run([a, z], [record(a, z, '0', confidence='observed', events=pair)], accuracy=PASSED)
    assert tight['period']['return_pct'] is None and tight['label'] == 'unavailable'
    assert 'SENSITIVITY_DENOMINATOR_NON_POSITIVE' in tight['intervals'][0]['reason_codes']
    year = run([nav('2026-01-01', 1000), z], [record(nav('2026-01-01', 1000), z, '0', confidence='observed', events=pair)],
               accuracy=PASSED)
    assert year['label'] == 'unavailable' and year['period']['xirr_pct'] is None
    b, y = nav('2026-10-12', 10000), nav('2026-10-13', 11000)
    wide = run([b, y], [record(b, y, '0', confidence='observed', events=pair)], accuracy=PASSED)
    row = wide['intervals'][0]
    assert wide['period']['return_pct'] == 10.0 and wide['label'] == 'estimate'
    assert wide['period']['sensitivity_bounds_pct'] == {'low': 9.0909, 'high': 11.1111}
    assert row['timing'] == 'unknown' and row['confidence'] == 'conditional' and 'FLOW_TIMING_UNKNOWN' in row['reason_codes']
    assert wide['period']['xirr_pct'] is None
    # A known event keeps its time inside an otherwise unknown-timing record.
    mixed = run([START, END], [record(START, END, '4000000', events=[event(END['valuation_at'], '3000000'),
                                                                      event(None, '1000000')])])
    assert mixed['period']['return_pct'] == 2.2952  # 241000 / (10m + 0 x 3m + 0.5 x 1m)
    assert mixed['period']['sensitivity_bounds_pct'] == {'low': 2.1909, 'high': 2.41}
    # Explicit event FX on an unknown-time event is used, never endpoint marks.
    s, e = nav('2026-10-12', 10_000_000, usd=1.0, fx=1400), nav('2026-10-13', 11_420_000, usd=1.0, fx=1420)
    priced = run([s, e], [record(s, e, '0'), record(s, e, '1000', currency='USD',
                                                    events=[{**event(None, '1000'), 'fx_to_krw': '1500'}])])
    assert priced['intervals'][0]['flows_krw'] == 1_500_000
    assert 'FLOW_FX_FROM_SNAPSHOT_MARKS' not in priced['intervals'][0]['reason_codes']


@pytest.mark.parametrize('mutate, code', [
    (lambda r: r.update(events=[event(None, '1')]), 'FLOW_EVENTS_INCONSISTENT'),  # unknown timing is checked too
    (lambda r: r.update(flow_native='0', confidence='observed', timing='known',
                        events=[event(END['valuation_at'], '1000', 'unknown')]), 'FLOW_EVENT_UNCLASSIFIED_EXTERNAL'),
    (lambda r: r.update(events=[event(None, '4000000', 'dividend')]), 'FLOW_EVENT_UNCLASSIFIED_EXTERNAL'),
    (lambda r: r.update(events=[event(None, '4000000', 'withdrawal')]), 'FLOW_EVENT_SIGN_INCONSISTENT'),
    (lambda r: r.update(flow_native='-4000000', events=[event(None, '-4000000', 'transfer_in')]),
     'FLOW_EVENT_SIGN_INCONSISTENT'),
    (lambda r: r.update(events=[{**event(None, '4000000'), 'amount_krw': '-1'}]), 'FLOW_EVENT_SIGN_INCONSISTENT'),
    (lambda r: r.update(events=[{**event(None, '4000000'), 'amount_krw': '4000000', 'fx_to_krw': '2'}]),
     'FLOW_EVENT_FX_INCONSISTENT'),
    (lambda r: r.update(events=[event(None, '4000000'), {**event(None, '5', 'transfer_in'), 'external': False}]),
     'FLOW_EVENT_EXTERNAL_INCONSISTENT'),
    (lambda r: r.update(events=[event(None, '4000000'), {**event(None, '-3', 'unknown'), 'external': False}]),
     'FLOW_EVENT_UNCLASSIFIED'),  # unknown anywhere leaves the interval unresolved
    (lambda r: r.update(events=[event(None, None)]), 'FLOW_RECORD_NUMBER_TYPE'),  # resolved records stay strict
    (lambda r: r.update(flow_krw='1', events=[{**event(None, '4000000'), 'amount_krw': '4000000'}]),
     'FLOW_KRW_INCONSISTENT'),
])
def test_every_event_and_the_aggregate_are_validated(mutate, code):
    bad = record(START, END, '4000000', reasons=['PROVIDER_CODE'])
    mutate(bad)
    block = run([START, END], [bad])
    assert block['period']['return_pct'] is None
    assert {'INVALID_FLOW_RECORD', code, 'PROVIDER_CODE'} <= set(block['intervals'][0]['reason_codes'])


def test_krw_deposit_with_contradictory_amount_krw_is_never_arithmetic():
    """Reviewer case: KRW 100 deposit claiming 200 KRW gave -10% observed (verified with a passed bar)."""
    a, z = nav('2026-10-12', 1000), nav('2026-10-13', 1100)

    def deposit(**extra):
        return record(a, z, '100', timing='known', confidence='observed',
                      events=[{**event(z['valuation_at'], '100'), **extra}])
    for accuracy in (None, PASSED):
        block = run([a, z], [deposit(amount_krw='200')], accuracy=accuracy)
        assert block['period']['return_pct'] is None and block['label'] == 'unavailable'
        assert {'INVALID_FLOW_RECORD', 'FLOW_EVENT_FX_INCONSISTENT'} <= set(block['intervals'][0]['reason_codes'])
    agreed = run([a, z], [deposit(amount_krw='100')], accuracy=PASSED)
    assert agreed['period']['return_pct'] == 0.0 and agreed['label'] == 'verified'


def _usd(events, **extra):
    return record(START, END, str(sum(Decimal(e['amount_native']) for e in events if e['external'])),
                  currency='USD', timing='known', confidence='observed', events=events, **extra)


def _at(amount, kind='deposit', **extra):
    return {**event(END['valuation_at'], amount, kind), **extra}


W2_MARK = {'priced_at': '2026-10-13T09:10:00+00:00', 'source': 'synthetic'}


@pytest.mark.parametrize('item, code', [
    # Implicit KRW: amount_krw must equal amount_native exactly; any other rate is a contradiction.
    (record(START, END, '100', timing='known', events=[_at('100', amount_krw='100.5')]), 'FLOW_EVENT_FX_INCONSISTENT'),
    (record(START, END, '100', timing='known', events=[_at('100', fx_to_krw='1400')]), 'FLOW_EVENT_FX_INCONSISTENT'),
    (record(START, END, '100', timing='known', events=[_at('100', fx={**W2_MARK, 'rate_to_krw': '1400'})]),
     'FLOW_EVENT_FX_INCONSISTENT'),
    (record(START, END, '100', timing='known', events=[_at('100')], fx_to_krw='1400'), 'FLOW_RECORD_FX_INCONSISTENT'),
    (record(START, END, '100', timing='known', events=[_at('100')], flow_krw='150'), 'FLOW_KRW_INCONSISTENT'),
    # Record FX is the effective rate for events without their own: 100 x 3 is 300, not 200.
    (_usd([_at('100', amount_krw='200')], fx_to_krw='3'), 'FLOW_EVENT_FX_INCONSISTENT'),
    (_usd([_at('100', amount_krw='300')], fx_to_krw='3'), None),
    (_usd([_at('100', amount_krw='200', fx_to_krw='2')], fx_to_krw='3'), None),  # event FX overrides the fallback
    (_usd([_at('100', amount_krw='300', fx_to_krw='2')], fx_to_krw='3'), 'FLOW_EVENT_FX_INCONSISTENT'),
    # Snapshot marks are event FX too, and must agree with an explicit fx_to_krw.
    (_usd([_at('100', amount_krw='300', fx={**W2_MARK, 'rate_to_krw': '2'})]), 'FLOW_EVENT_FX_INCONSISTENT'),
    (_usd([_at('100', amount_krw='200', fx={**W2_MARK, 'rate_to_krw': 2.0})]), None),
    (_usd([_at('100', fx_to_krw='3', fx={**W2_MARK, 'rate_to_krw': '2'})]), 'FLOW_EVENT_FX_INCONSISTENT'),
    (_usd([_at('100', fx={**W2_MARK, 'rate_to_krw': None})]), 'FLOW_RECORD_FX_INVALID'),
    (_usd([_at('100', fx='1400')]), 'FLOW_RECORD_FX_INVALID'),
    # Different events may carry different rates; the aggregate reconciles after effective conversion.
    (_usd([_at('100', fx_to_krw='2'), _at('100', amount_krw='400')], fx_to_krw='4', flow_krw='600'), None),
    (_usd([_at('100', fx_to_krw='2'), _at('100', amount_krw='400')], fx_to_krw='4', flow_krw='500'),
     'FLOW_KRW_INCONSISTENT'),
    (_usd([_at('100')], fx_to_krw='3', flow_krw='301'), None),  # derived conversion: 1 KRW rounding
    (_usd([_at('100')], fx_to_krw='3', flow_krw='310'), 'FLOW_KRW_INCONSISTENT'),
    # Non-external events are reconciled as well.
    (_usd([_at('100'), _at('-1', 'fee', external=False, amount_krw='-5')], fx_to_krw='3'), 'FLOW_EVENT_FX_INCONSISTENT'),
    (_usd([_at('100'), _at('0', 'transfer_in', external=False, amount_krw='7')]), 'FLOW_EVENT_SIGN_INCONSISTENT'),
    (_usd([_at('100'), _at('-1', 'fee', external=False, amount_krw='-3')], fx_to_krw='3'), None),
])
def test_explicit_krw_reconciles_with_currency_and_effective_fx(item, code):
    if code is None:
        assert ir.validate_record(item)['confidence'] == 'observed'
        return
    with pytest.raises(ir.InvalidRecord, match=code):
        ir.validate_record(item)
    # Contradictory facts never reach arithmetic: the interval is null with the reason.
    block = run([START, END], [item, record(START, END, '0')] if item['currency'] == 'USD' else [item])
    assert block['period']['return_pct'] is None
    assert {'INVALID_FLOW_RECORD', code} <= set(block['intervals'][0]['reason_codes'])


def test_linked_sensitivity_bounds_multiply_sign_aware():
    days = [nav('2026-10-%d' % d, 100) for d in (11, 12, 13)]
    block = run(days, [record(days[0], days[1], '400'), record(days[1], days[2], '400')])
    assert [r['sensitivity_bounds_pct'] for r in block['intervals']] == [{'low': -400.0, 'high': -80.0}] * 2
    assert block['period']['return_pct'] == -88.8889
    assert block['period']['sensitivity_bounds_pct'] == {'low': -160.0, 'high': 800.0}
    d = Decimal
    assert ir.link_bounds([(d('-0.1'), d('0.2')), (d('0.05'), d('0.1'))]) == (d('0.9') * d('1.05') - 1, d('1.2') * d('1.1') - 1)
    assert ir.link_bounds([(d('-1.5'), d('-0.5')), (d('-4'), d('1'))]) == (d('-2.5'), d('0.5'))


def test_unresolved_records_keep_provider_reasons(tmp_path):
    from test_crypto_flow_intervals import hl_flow, result
    from test_crypto_flow_intervals import setup as crypto_setup
    store = crypto_setup(tmp_path)
    hl_flow(store, 'withdraw', usdc='50', fee='1')
    provided = result(store, venue='hyperliquid')[0]
    assert provided['confidence'] == 'unresolved' and any(e['amount_native'] is None for e in provided['events'])
    block = ir.ledgerless_returns(store.db_path, crypto_navs(store.db_path), 'crypto', '2026-10-13', '2026-10-14')
    assert {'UNRESOLVED_FLOW', 'CRYPTO_WITHDRAWAL_FEE_BASIS_UNVERIFIED'} <= set(block['reason_codes'])
    assert 'FLOW_RECORD_NUMBER_TYPE' not in block['reason_codes'] and block['period']['return_pct'] is None
    envelope = record(START, END, None, reasons=['PROVIDER_CODE'], events=[event(None, None, 'unknown')])
    assert {'UNRESOLVED_FLOW', 'PROVIDER_CODE'} <= set(run([START, END], [envelope])['reason_codes'])
    # A rejected record keeps its provider codes, never free text.
    drifted = record(START, END, '4000000', reasons=['PROVIDER_CODE', 'free text'], flow_native=4000000.0)
    reasons = run([START, END], [drifted])['reason_codes']
    assert {'INVALID_FLOW_RECORD', 'FLOW_RECORD_NUMBER_TYPE', 'PROVIDER_CODE'} <= set(reasons) and 'free text' not in reasons


def test_production_tool_path_passes_no_dividend_provider(monkeypatch):
    import toss_flow_inference
    assert ir._provider('toss') is toss_flow_inference.toss_interval_flows
    seen = []

    def provider(db_path, start, end, accounts=None, *, schedule_provider=None):
        seen.append(schedule_provider)
        return []
    monkeypatch.setattr(ir, '_provider', lambda name: provider)
    assert ir.load_records('db', 'toss', '2026-10-12', '2026-10-13', budget_seconds=1) == ([], [])
    # No override: toss_interval_flows then models no dividends (2.12.10) and fetches no schedule.
    assert seen == [None] and not hasattr(ir, 'offline_dividend_schedule')
