"""KIS ledgerless flows and the kis returns scope: synthetic captures, stored versions, real capture output.

No network and no broker calls: captures are synthetic dicts in the kis_capture shape,
or kis_nav_capture output over the fake transport of test_kis_ledgerless_capture.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import interval_returns as ir
import kis_flow_inference as kf
from ledger_returns import scope_navs

KST = timezone(timedelta(hours=9))
ACCOUNT = 'kis:primary'
DAYS = ['2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21']


def stamp(day, hour=18, minute=11):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=KST).astimezone(timezone.utc).isoformat()


def metric(value):
    return {'value': value, 'status': 'derived', 'reason_codes': []}


def account(day, *, krw='1000000', usd='100', jpy=None, fills=(), costs=None, settlements=(), domestic=None,
            overseas=None, window_days=10, status='complete', reasons=(), usd_rate='1400', jpy_rate='9.5',
            supports_overseas=True, us_fills=0, securities='0'):
    """One kis_capture account entry: NAV = cash + ``securities`` KRW (inference reads cash/trades/positions)."""
    end = datetime.fromisoformat(day)
    window = [(end - timedelta(days=window_days)).strftime('%Y%m%d'), end.strftime('%Y%m%d')]
    cash = {'KRW': metric(krw)}
    fx = {}
    if supports_overseas:
        cash['USD'] = metric(usd)
        fx['USD'] = {'rate_to_krw': usd_rate, 'quote_unit': 1, 'source': 'CTRP6504R.output1.bass_exrt'}
        if jpy is not None:
            cash['JPY'] = metric(jpy)
            fx['JPY'] = {'rate_to_krw': jpy_rate, 'quote_unit': 100, 'source': 'CTRP6504R.output1.bass_exrt'}
    nav = {c: metric(m['value']) for c, m in cash.items()}
    nav['KRW'] = metric(format(Decimal(krw) + Decimal(securities), 'f'))
    total = sum(Decimal(m['value']) * (Decimal(fx[c]['rate_to_krw']) if c != 'KRW' else 1) for c, m in nav.items())
    return {'label': 'primary', 'kind': 'brokerage', 'supports_overseas': supports_overseas, 'status': status,
            'reasons': list(reasons), 'nav_native': nav, 'cash_native': cash, 'fx': fx,
            'nav_krw': metric(None if status != 'complete' else format(total, 'f')),
            'nav_price_time': stamp(day, 15, 30), 'nav_price_time_basis': 'krx_regular_close',
            'positions': {'domestic': dict(domestic or {}), 'overseas': copy.deepcopy(overseas or {})},
            'trades': {'window': window, 'domestic_fills': list(fills), 'reasons': [],
                       'domestic_costs': dict(costs or {}),
                       'overseas_settlements': list(settlements) if supports_overseas else None,
                       'us_fills_on_capture_date': us_fills if supports_overseas else None},
            'observed_at': stamp(day)}


def fill(day, odno, side, symbol, qty, amount):
    return {'ord_dt': day.replace('-', ''), 'odno': odno, 'sll_buy_dvsn_cd': side, 'pdno': symbol,
            'tot_ccld_qty': qty, 'avg_prvs': str(Decimal(amount) / Decimal(qty)), 'tot_ccld_amt': amount,
            'excg_dvsn_cd': '02', 'ord_tmd': '100000'}


def settlement(day, side, symbol, qty, amount, currency='USD', krw='0', settle=None):
    return {'trad_dt': day.replace('-', ''), 'sttl_dt': (settle or day).replace('-', ''), 'sll_buy_dvsn_cd': side,
            'pdno': symbol, 'ccld_qty': qty, 'ft_ccld_unpr2': str(Decimal(amount) / Decimal(qty)),
            'tr_frcr_amt2': amount, 'frcr_excc_amt_1': amount, 'wcrc_excc_amt': krw, 'crcy_cd': currency,
            'erlm_exrt': '0'}


def snap(day, entry, version=1):
    return {'account': ACCOUNT, 'snapshot_id': f'sid-{day}', 'version': version, 'time': entry['observed_at'],
            'capture': entry, 'reasons': []}


def infer(*entries, **kwargs):
    snapshots = [snap(DAYS[i], e) for i, e in enumerate(entries)]
    records = kf.infer_kis_flows(snapshots, **kwargs)
    for record in records:
        ir.validate_record(record)  # every record passes the engine's one validator
    return records


def by_currency(records):
    return {r['currency']: r for r in records}


def test_quiet_interval_is_a_conditional_zero_with_the_dividend_advisory():
    records = by_currency(infer(account(DAYS[0]), account(DAYS[1])))
    assert set(records) == {'KRW', 'USD'}
    for record in records.values():
        assert record['flow_native'] == '0' and record['confidence'] == 'conditional'
        assert record['reasons'] == ['DIVIDENDS_COUNTED_AS_FLOWS'] and record['source'] == 'kis'
        assert record['inputs']['algorithm'] == 'kis-residual-v1' and record['timing'] == 'unknown'


def test_krw_deposit_is_an_inferred_flow_with_unknown_time():
    records = by_currency(infer(account(DAYS[0]), account(DAYS[1], krw='2000000')))
    krw = records['KRW']
    assert krw['flow_native'] == '1000000' and krw['diagnostics']['classification'] == 'deposit'
    assert krw['events'] == [{'time': None, 'amount_native': '1000000', 'kind': 'deposit', 'external': True}]
    assert records['USD']['flow_native'] == '0'


def test_domestic_trades_and_costs_land_in_the_fill_interval_on_the_d2_basis():
    buy = fill(DAYS[1], '0001', '02', '005930', '10', '500000')
    sell = fill(DAYS[2], '0002', '01', '005930', '4', '220000')
    costs1 = {'20261019': {'fee': '75', 'tl_tax': '0', 'buy_qty': '10', 'sll_qty': '0'}}
    costs2 = {**costs1, '20261020': {'fee': '33', 'tl_tax': '396', 'buy_qty': '0', 'sll_qty': '4'}}
    records = infer(account(DAYS[0]),
                    account(DAYS[1], krw='499925', fills=[buy], costs=costs1, domestic={'005930': '10'}),
                    account(DAYS[2], krw='719496', fills=[buy, sell], costs=costs2, domestic={'005930': '6'}))
    krw = [r for r in records if r['currency'] == 'KRW']
    assert [r['flow_native'] for r in krw] == ['0', '0']
    assert krw[0]['diagnostics']['trade_cash_effect_native'] == '-500075'
    assert krw[1]['diagnostics']['trade_cash_effect_native'] == '219571'
    assert krw[1]['inputs']['fill_ids'] == ['20261020:0002']


def test_cumulative_fill_growth_after_a_capture_counts_only_the_new_part():
    early = fill(DAYS[0], '0009', '02', '900300', '5', '250000')
    late = fill(DAYS[0], '0009', '02', '900300', '10', '500000')  # NXT after-market, same order
    records = by_currency(infer(account(DAYS[0], krw='750000', fills=[early], domestic={'900300': '5'}),
                                account(DAYS[1], krw='500000', fills=[late], domestic={'900300': '10'})))
    assert records['KRW']['diagnostics']['trade_cash_effect_native'] == '-250000'
    assert records['KRW']['flow_native'] == '0'


def test_position_change_without_fills_blocks_every_currency():
    records = infer(account(DAYS[0], domestic={'005930': '10'}), account(DAYS[1], domestic={'005930': '12'}))
    for record in records:
        assert record['flow_native'] is None and record['confidence'] == 'unresolved'
        assert 'KIS_DOMESTIC_QUANTITY_UNRECONCILED' in record['reasons']


def test_small_residual_is_income_never_a_flow():
    records = by_currency(infer(account(DAYS[0]), account(DAYS[1], krw='1003200')))
    krw = records['KRW']
    assert krw['flow_native'] == '0' and krw['income_native'] == '3200'
    assert 'SMALL_RESIDUAL_TREATED_AS_INCOME' in krw['reasons']
    assert krw['events'][0]['kind'] == 'income_other' and krw['diagnostics']['residual_native'] == '0'


def test_overseas_trade_uses_the_foreign_settlement_amount():
    row = settlement(DAYS[1], '02', 'AAPL', '5', '1001.25')
    overseas = {'AAPL': {'currency': 'USD', 'exchange': 'NASD', 'quantity': '5'}}
    records = by_currency(infer(account(DAYS[0], usd='2000'),
                                account(DAYS[1], usd='998.75', settlements=[row], overseas=overseas)))
    assert records['USD']['diagnostics']['trade_cash_effect_native'] == '-1001.25'
    assert records['USD']['flow_native'] == '0' and records['USD']['inputs']['settlement_rows_new'] == 1


def test_opposite_residuals_within_band_are_an_fx_conversion():
    records = by_currency(infer(account(DAYS[0]), account(DAYS[1], krw='-400000', usd='1100')))
    for currency in ('KRW', 'USD'):
        assert records[currency]['flow_native'] == '0'
        assert records[currency]['events'][0]['kind'] == 'fx_leg'
        assert 'FX_CONVERSION_ESTIMATED_WITHIN_BAND' in records[currency]['reasons']
    assert records['KRW']['events'][0]['pair_id'] == records['USD']['events'][0]['pair_id']


def test_opposite_residuals_outside_band_null_the_interval():
    records = infer(account(DAYS[0]), account(DAYS[1], krw='-400000', usd='1000'))
    for record in records:
        assert record['flow_native'] is None and 'FX_RATE_OUTSIDE_BAND' in record['reasons']


def test_foreign_flow_valuation_gate_and_cross_currency_block():
    dividend = by_currency(infer(account(DAYS[0]), account(DAYS[1], usd='150')))
    assert dividend['USD']['flow_native'] == '50'  # 70,000 KRW: a deposit (dividends not modelled)
    assert dividend['USD']['inputs']['foreign_flow_fx_error_bound_krw'] == '1750.000'
    large = by_currency(infer(account(DAYS[0]), account(DAYS[1], usd='1100')))
    assert 'USD_FLOW_FX_VALUATION_UNSEPARATED' in large['USD']['reasons']
    assert large['KRW']['flow_native'] is None and 'CROSS_CURRENCY_RESIDUAL_UNRESOLVED' in large['KRW']['reasons']


def test_three_currency_residuals_that_cannot_pair_uniquely_are_null():
    records = infer(account(DAYS[0], jpy='0'), account(DAYS[1], krw='-1000000', usd='300', jpy='30000'))
    assert all(r['flow_native'] is None for r in records)
    assert all('KIS_CROSS_CURRENCY_RESIDUAL_UNEXPLAINED' in r['reasons'] for r in records)


def test_settlement_revisions_and_unsettled_krw_estimates():
    row = settlement(DAYS[0], '02', 'AAPL', '5', '1000', krw='1400000', settle=DAYS[1])
    overseas = {'AAPL': {'currency': 'USD', 'exchange': 'NASD', 'quantity': '5'}}
    gone = infer(account(DAYS[0], settlements=[row], overseas=overseas), account(DAYS[1], overseas=overseas))
    assert all('KIS_OVERSEAS_SETTLEMENT_REVISED' in r['reasons'] and r['flow_native'] is None for r in gone)
    revised = dict(row, wcrc_excc_amt='1410000')
    moved = by_currency(infer(account(DAYS[0], settlements=[row], overseas=overseas),
                              account(DAYS[1], krw='990000', settlements=[revised], overseas=overseas)))
    assert 'KIS_UNSETTLED_REVISION_UNEXPLAINED' in moved['KRW']['reasons'] and moved['KRW']['flow_native'] is None
    assert moved['KRW']['inputs']['krw_estimate_revision'] == '10000'
    small = by_currency(infer(account(DAYS[0], settlements=[row], overseas=overseas),
                              account(DAYS[1], krw='2000000', settlements=[dict(row, wcrc_excc_amt='1403000')],
                                      overseas=overseas)))
    assert small['KRW']['flow_native'] == '1000000'  # a 3,000 KRW revision cannot explain a deposit


def test_trade_window_gap_and_missing_trade_reads_are_reasons_never_zero():
    gap = infer(account(DAYS[0]), account(DAYS[2], window_days=0))
    assert all('KIS_TRADE_WINDOW_GAP' in r['reasons'] and r['flow_native'] is None for r in gap)
    assert all(r['diagnostics']['trade_cash_effect_native'] is None for r in gap)
    missing = account(DAYS[1])
    missing['trades']['domestic_fills'] = None
    blocked = infer(account(DAYS[0]), missing)
    assert all('KIS_DOMESTIC_FILLS_UNAVAILABLE' in r['reasons'] and r['flow_native'] is None for r in blocked)
    no_settlements = account(DAYS[1])
    no_settlements['trades']['overseas_settlements'] = None
    usd = by_currency(infer(account(DAYS[0]), no_settlements))['USD']
    assert usd['diagnostics']['trade_cash_effect_native'] is None and usd['flow_native'] is None


def test_us_trade_missing_from_the_balance_is_detected_not_attributed():
    row = settlement(DAYS[1], '02', 'NVDA', '3', '420')
    records = infer(account(DAYS[0], usd='1000'), account(DAYS[1], usd='580', settlements=[row]))
    for record in records:
        assert record['flow_native'] is None
        assert {'KIS_OVERSEAS_QUANTITY_UNRECONCILED', 'KIS_US_TRADE_TIMING_SUSPECTED'} <= set(record['reasons'])


def test_incomplete_middle_capture_is_bridged_and_latest_incomplete_is_diagnostic():
    late = account(DAYS[1], status='partial', reasons=['KIS_US_FILLS_NEAR_CAPTURE'], us_fills=1)
    records = infer(account(DAYS[0]), late, account(DAYS[2], krw='1500000'))
    assert {(r['start_snapshot_id'], r['end_snapshot_id']) for r in records} == {('sid-2026-10-18', 'sid-2026-10-20')}
    assert by_currency(records)['KRW']['flow_native'] == '500000'
    trailing = infer(account(DAYS[0]), late)
    assert all('KIS_US_FILLS_NEAR_CAPTURE' in r['reasons'] and r['flow_native'] is None for r in trailing)
    missing_cash = account(DAYS[1])
    missing_cash['cash_native']['KRW'] = {'value': None, 'status': 'unknown', 'reason_codes': ['X']}
    assert all('KIS_CASH_UNAVAILABLE' in r['reasons'] for r in infer(account(DAYS[0]), missing_cash))


def test_domestic_only_account_needs_no_overseas_reads():
    records = infer(account(DAYS[0], supports_overseas=False), account(DAYS[1], krw='1000500', supports_overseas=False))
    assert [r['currency'] for r in records] == ['KRW'] and records[0]['income_native'] == '500'


def test_new_currency_is_zero_at_the_capture_that_does_not_list_it():
    records = by_currency(infer(account(DAYS[0]), account(DAYS[1], jpy='100')))
    assert records['JPY']['diagnostics']['cash_delta_native'] == '100'
    assert records['JPY']['income_native'] == '100'  # 950 KRW at the closing KIS mark: R8


def test_currency_held_only_as_securities_gets_a_record():
    entries = [account(day) for day in DAYS[:2]]
    for entry in entries:
        entry['nav_native']['HKD'] = metric('500')
    records = by_currency(infer(*entries))
    assert records['HKD']['flow_native'] == '0' and records['HKD']['diagnostics']['cash_delta_native'] == '0'


def test_start_end_select_interval_end_times():
    records = infer(account(DAYS[0]), account(DAYS[1]), account(DAYS[2]), start=DAYS[2], end=DAYS[2])
    assert {r['end_snapshot_id'] for r in records} == {'sid-2026-10-20'}


# ── Scope points, engine and stored versions ─────────────────────────

def nav(day, entry=None, *, version=1, reasons=None):
    capture = {'version': 'kis_ledgerless_v1', 'started_at': stamp(day, 18, 10), 'observed_at': stamp(day),
               'accounts': {ACCOUNT: entry or account(day)}, 'reasons': [], 'reads': {}}
    return {'snapshot_id': f'sid-{day}', 'snapshot_date': day, 'version': version, 'valuation_at': stamp(day, 16),
            'total_value_krw': 1.0, 'reason_codes': reasons or [], 'result_status': 'complete',
            'kis_capture': capture}


def test_scope_points_come_from_the_capture_alone():
    point = scope_navs([nav(DAYS[0])], 'kis')[0]
    assert point['total_value_krw'] == 1000000 + 100 * 1400 and point['reason_codes'] == []
    assert point['valuation_at'] == stamp(DAYS[0]) and point['fx_times']['USD']['rate_to_krw'] == '1400'
    # Toss/crypto snapshot failures do not block KIS; KIS failures do.
    assert scope_navs([nav(DAYS[0], reasons=['STALE_TOSS_QUOTE'])], 'kis')[0]['reason_codes'] == []
    legacy = {k: v for k, v in nav(DAYS[0]).items() if k != 'kis_capture'}
    assert scope_navs([legacy], 'kis')[0]['reason_codes'] == ['KIS_CAPTURE_MISSING']
    stale = account(DAYS[0])
    stale['nav_price_time'] = stamp('2026-10-09', 15, 30)
    assert 'STALE_KIS_PRICE' in scope_navs([nav(DAYS[0], stale)], 'kis')[0]['reason_codes']
    unknown = account(DAYS[0], status='partial', reasons=['KIS_KRX_PRICE_NOT_LATEST_CLOSE'])
    point = scope_navs([nav(DAYS[0], unknown)], 'kis')[0]
    assert point['total_value_krw'] is None and 'KIS_KRX_PRICE_NOT_LATEST_CLOSE' in point['reason_codes']
    two = nav(DAYS[0])
    two['kis_capture']['accounts']['kis:pension'] = account(DAYS[0], usd_rate='1401')
    assert 'KIS_FX_MARKS_INCONSISTENT' in scope_navs([two], 'kis')[0]['reason_codes']


def engine(navs, records):
    return ir.ledgerless_returns(None, navs, 'kis', navs[0]['snapshot_date'], navs[-1]['snapshot_date'],
                                 providers={'kis': lambda db_path, start, end: records}, detail_level='full')


def test_kis_block_is_an_estimate_with_method_label_and_accuracy():
    entries = [account(DAYS[0]), account(DAYS[1], krw='2000000'), account(DAYS[2], krw='2000000', securities='10000')]
    navs = [nav(day, e) for day, e in zip(DAYS, entries)]
    records = kf.infer_kis_flows([snap(d, e) for d, e in zip(DAYS, entries)])
    block = engine(navs, records)
    assert block['label'] == 'estimate' and block['scope'] == 'kis'
    assert block['method'] == 'modified_dietz_linked_twr_estimate' and block['is_exact_twr'] is False
    # Interval 1: +1,000,000 deposit, no gain; interval 2: 10,000 / 2,140,000.
    assert [row['return_pct'] for row in block['intervals']] == [0.0, 0.4673]
    assert block['period']['return_pct'] == 0.4673
    assert block['accuracy']['status'] == 'forward_pending' and block['accuracy']['passed_bar'] is False
    assert 'accuracy not yet measured (forward pending' in block['explanation']
    assert all(row['label'] == 'estimate' for row in block['intervals'])
    # Even a passed bar never verifies KIS: every flow is inferred (conditional).
    forced = ir.ledgerless_returns(None, navs, 'kis', DAYS[0], DAYS[2], providers={'kis': lambda *a: records},
                                   accuracy={'kis': {'status': 'measured', 'passed_bar': True}})
    assert forced['label'] == 'estimate'


def test_history_before_the_first_capture_is_null_with_a_reason():
    legacy = [{k: v for k, v in nav(day).items() if k != 'kis_capture'} for day in DAYS[:2]]
    navs = legacy + [nav(DAYS[2]), nav(DAYS[3])]
    entries = [account(DAYS[2]), account(DAYS[3])]
    records = kf.infer_kis_flows([snap(DAYS[2], entries[0]), snap(DAYS[3], entries[1])])
    block = ir.ledgerless_returns(None, navs, 'kis', DAYS[0], DAYS[3], providers={'kis': lambda *a: records})
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert 'KIS_CAPTURE_MISSING' in block['period']['reason_codes']
    # Per-day reason: the days before the first capture are KIS_CAPTURE_MISSING, not a missing start NAV.
    coverage = block['coverage']
    assert coverage['unresolved_days'] == [{'start_date': DAYS[0], 'end_date': DAYS[2], 'days': 2,
                                            'reason_codes': ['KIS_CAPTURE_MISSING']}]
    assert coverage['unresolved_days_by_reason'] == {'KIS_CAPTURE_MISSING': 2}
    assert 'not back-filled' in block['explanation']
    assert block['tracked']['start_date'] == DAYS[2] and block['tracked']['label'] == 'estimate'
    none = ir.ledgerless_returns(None, legacy, 'kis', DAYS[0], DAYS[1])
    assert none['label'] == 'unavailable' and 'KIS_CAPTURE_MISSING' in none['reason_codes']
    assert none['coverage']['unresolved_days_by_reason'] == {'KIS_CAPTURE_MISSING': 1}


def test_missing_account_records_and_foreign_sources_never_pass():
    entries = [account(DAYS[0]), account(DAYS[1])]
    navs = [nav(day, e) for day, e in zip(DAYS, entries)]
    records = kf.infer_kis_flows([snap(d, e) for d, e in zip(DAYS, entries)])
    usd_only = engine(navs, [r for r in records if r['currency'] == 'USD'])
    assert usd_only['period']['return_pct'] is None and 'FLOW_RECORDS_MISSING' in usd_only['intervals'][0]['reason_codes']
    toss_source = engine(navs, [dict(r, source='toss') for r in records])
    assert toss_source['period']['return_pct'] is None
    assert 'FLOW_RECORD_SOURCE_MISMATCH' in toss_source['intervals'][0]['reason_codes']
    # A KIS record never enters the Toss scope.
    assert ir.compute([], [dict(records[0], account='toss:primary')], 'toss', DAYS[0], DAYS[1])['label'] == 'unavailable'


def store_versions(tmp_path, entries):
    from portfolio_store import NavStore
    store = NavStore(tmp_path / 'nav.db')
    for day, entry in entries:
        capture = nav(day, entry)['kis_capture']
        store.upsert_snapshot({'total_assets_krw': 100, 'result_status': 'complete', 'components': [],
                               'fx_times': {}, 'valuation_at': stamp(day, 16), 'kis_capture': capture}, day)
    return store


def test_provider_reads_stored_versions_read_only(tmp_path):
    store = store_versions(tmp_path, [(DAYS[0], account(DAYS[0])), (DAYS[1], account(DAYS[1], krw='1500000')),
                                      (DAYS[2], account(DAYS[2], krw='1500000'))])
    before = (tmp_path / 'nav.db').read_bytes()
    records = kf.kis_interval_flows(store.db_path, DAYS[1], DAYS[2])
    assert (tmp_path / 'nav.db').read_bytes() == before
    krw = [r for r in records if r['currency'] == 'KRW']
    assert [r['flow_native'] for r in krw] == ['500000', '0']  # the DAYS[0] anchor was read before start
    navs = store.range(DAYS[0], DAYS[2])
    block = ir.ledgerless_returns(store.db_path, navs, 'kis', DAYS[0], DAYS[2], detail_level='full')
    assert block['label'] == 'estimate' and block['period']['return_pct'] == 0.0, block['reason_codes']
    assert kf.kis_interval_flows(tmp_path / 'absent.db', DAYS[0], DAYS[1])[0]['reasons'] == [
        'LOCAL_KIS_EVIDENCE_UNAVAILABLE']
    (tmp_path / 'empty').mkdir()
    empty = store_versions(tmp_path / 'empty', [])
    empty.count()  # creates the schema
    assert kf.kis_interval_flows(empty.db_path, DAYS[0], DAYS[1])[0]['reasons'] == ['KIS_CAPTURE_MISSING']
    assert kf.kis_residual_diagnostics(store.db_path, DAYS[0], DAYS[2])['counts'] == {
        'intervals_total': 2, 'intervals_classified': 2, 'intervals_unresolved': 0}


def test_real_capture_output_feeds_inference_and_the_engine(tmp_path):
    import test_kis_ledgerless_capture as capture_tests
    first, _ = capture_tests.run_capture(capture_tests.routes())
    second = copy.deepcopy(first)
    # Next day: a KRW deposit of 300,000 KRW lands in D+2 cash; nothing traded.
    entry = second['accounts'][ACCOUNT]
    cash = Decimal(entry['cash_native']['KRW']['value']) + 300000
    for group in ('cash_native', 'nav_native'):
        entry[group]['KRW']['value'] = format(Decimal(entry[group]['KRW']['value']) + 300000, 'f')
    entry['nav_krw']['value'] = format(Decimal(entry['nav_krw']['value']) + 300000, 'f')
    moved = datetime.fromisoformat(first['observed_at']) + timedelta(days=1)
    second['observed_at'] = entry['observed_at'] = moved.isoformat()
    entry['trades']['window'] = [(moved.astimezone(KST) - timedelta(days=10)).strftime('%Y%m%d'),
                                 moved.astimezone(KST).strftime('%Y%m%d')]
    snapshots = [{'account': ACCOUNT, 'snapshot_id': f's{i}', 'version': 1, 'time': c['observed_at'],
                  'capture': c['accounts'][ACCOUNT], 'reasons': []} for i, c in enumerate((first, second))]
    records = by_currency(kf.infer_kis_flows(snapshots))
    for record in records.values():
        ir.validate_record(record)
    assert records['KRW']['flow_native'] == '300000' and cash > 0
    assert records['USD']['flow_native'] == '0' and records['JPY']['flow_native'] == '0'
    json.dumps(list(records.values()), allow_nan=False)


def test_weekday_and_saturday_captures_both_end_a_kis_interval():
    import test_kis_ledgerless_capture as capture_tests
    # 18:10 KST captures, the largest domestic holding priced on NXT. Thu and Fri: NXT open to
    # 20:00 (Thu: NX's current price 1 tick off; Fri: NXT's close not yet posted, the live shape).
    # Sat: Friday's session, NX row still not posted (as live on Sun 10/18 01:29).
    def at(day):
        return datetime(2026, 10, day, 9, 10, tzinfo=timezone.utc)
    captures = [
        capture_tests.nxt_capture(at(15), '20261015', '20261014',
                                  {'NAS': ('20261014', '20261013'), 'TSE': ('20261015', '20261014')},
                                  nx_close='128800')[0],
        capture_tests.nxt_capture(at(16), '20261016', '20261015',
                                  {'NAS': ('20261015', '20261014'), 'TSE': ('20261016', '20261015')})[0],
        capture_tests.nxt_capture(at(17), '20261016', '20261015',
                                  {'NAS': ('20261016', '20261015'), 'TSE': ('20261016', '20261015')})[0]]
    days = ['2026-10-15', '2026-10-16', '2026-10-17']
    bases = [c['accounts'][ACCOUNT]['nav_price_time_basis'] for c in captures]
    assert bases == ['jp_regular_close+krx_regular_close+nxt_after_market_at_read+us_regular_close',
                     'jp_regular_close+krx_regular_close+nxt_after_market_at_read_in_range+us_regular_close',
                     'jp_regular_close+krx_regular_close+nxt_after_market_close_in_range+us_regular_close']
    latest = [c['accounts'][ACCOUNT]['nav_price_time_latest'] for c in captures]
    assert latest == [at(15).isoformat(), at(16).isoformat(), '2026-10-16T20:00:00+00:00']  # Sat: US Fri close
    navs = []
    for day, capture in zip(days, captures):
        assert capture['accounts'][ACCOUNT]['status'] == 'complete', capture['accounts'][ACCOUNT]['reasons']
        navs.append({'snapshot_id': f'sid-{day}', 'snapshot_date': day, 'version': 1,
                     'valuation_at': capture['observed_at'], 'total_value_krw': 1.0, 'reason_codes': [],
                     'result_status': 'complete', 'kis_capture': capture})
    points = scope_navs(navs, 'kis')
    # The 7-day age check runs against the 18:10 observation: price times at the read pass.
    assert [p['reason_codes'] for p in points] == [[], [], []]
    records = kf.infer_kis_flows([{'account': ACCOUNT, 'snapshot_id': n['snapshot_id'], 'version': 1,
                                   'time': c['observed_at'], 'capture': c['accounts'][ACCOUNT], 'reasons': []}
                                  for n, c in zip(navs, captures)])
    block = engine(navs, records)
    assert block['label'] == 'estimate', block['reason_codes']
    assert [row['return_pct'] for row in block['intervals']] == [0.0, 0.0]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    from test_ledger_returns import wired as base_wired
    return base_wired.__wrapped__(tmp_path, monkeypatch)


def test_tools_return_the_kis_block_from_stored_captures(wired, tmp_path, monkeypatch):
    server = wired.server
    entries = [(DAYS[0], account(DAYS[0])), (DAYS[1], account(DAYS[1], krw='1500000')),
               (DAYS[2], account(DAYS[2], krw='1500000', securities='16400'))]
    store = store_versions(tmp_path, entries)
    before = store.db_path.read_bytes()
    wired.navs[:] = store.range(DAYS[0], DAYS[2])
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', store.db_path)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (DAYS[0], DAYS[2]))
    for tool in (server.get_portfolio_performance, server.get_portfolio_history):
        result = tool(portfolio_scope='kis', detail_level='full')
        assert result['ok'], result.get('error')
        block = result['data']['ledgerless_returns']
        # 1,640,000 -> 1,640,000 + 16,400 with no flow: 1%.
        assert block['scope'] == 'kis' and block['label'] == 'estimate', block['reason_codes']
        assert block['period']['return_pct'] == 1.0
        assert [row['flows_native'] for row in block['intervals']] == [{'KRW': '500000', 'USD': '0'},
                                                                        {'KRW': '0', 'USD': '0'}]
        assert block['accuracy']['status'] == 'forward_pending'
        assert block['explanation'].startswith('ESTIMATE, not verified')
    assert store.db_path.read_bytes() == before


def test_history_output_keeps_the_capture_in_storage_only(wired, tmp_path, monkeypatch):
    server = wired.server
    entries = [(DAYS[0], account(DAYS[0])), (DAYS[1], account(DAYS[1], krw='1500000'))]
    store = store_versions(tmp_path, entries)
    stored = store.range(DAYS[0], DAYS[1])
    assert all('kis_capture' in row for row in stored)  # storage keeps it
    plain = [{k: v for k, v in row.items() if k != 'kis_capture'} for row in stored]
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', store.db_path)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (DAYS[0], DAYS[1]))

    def run(rows, tool, without=(), **kwargs):
        wired.navs[:] = copy.deepcopy(rows)
        result = tool(**kwargs)
        assert result['ok'], result.get('error')
        result['meta'].pop('query_id', None)  # fresh per call
        for key in without:
            result['data'].pop(key)
        return json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)

    # Toss/equity output is byte-identical whether or not the rows carry a KIS capture, except (since
    # 2.15) the equity ledgerless estimate, whose KIS part is the capture itself.
    for tool in (server.get_portfolio_history, server.get_portfolio_performance):
        for scope in ('equity', 'toss'):
            for detail in ('summary', 'full'):
                kwargs = dict(portfolio_scope=scope, detail_level=detail,
                              without=('ledgerless_returns',) if scope == 'equity' else ())
                assert run(stored, tool, **kwargs) == run(plain, tool, **kwargs), (tool.__name__, scope, detail)
        wired.navs[:] = copy.deepcopy(stored)
        assert 'kis' in tool(portfolio_scope='equity')['data']['ledgerless_returns']['included_scopes']
    for scope in ('equity', 'toss', 'kis'):
        text = run(stored, server.get_portfolio_history, portfolio_scope=scope, detail_level='full')
        assert '"kis_capture"' not in text and ('kis_capture_status' in text) == (scope == 'kis')
    data = server.get_portfolio_history(portfolio_scope='kis')['data']
    status = data['kis_capture_status']
    assert status['status'] == 'stored' and status['snapshot_date'] == DAYS[1]
    assert set(status['accounts'][ACCOUNT]) == {'status', 'reason_codes', 'nav_price_time', 'nav_price_time_basis'}
    wired.navs[:] = plain
    status = server.get_portfolio_history(portfolio_scope='kis')['data']['kis_capture_status']
    assert status == {'snapshot_date': DAYS[1], 'status': 'missing', 'reason_codes': ['KIS_CAPTURE_MISSING']}
