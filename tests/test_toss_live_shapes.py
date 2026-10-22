"""Live Toss shapes that doc-built validators rejected (observed over a 2026-10-14 -> 10-15 interval).

Structure and types follow the frozen 10/16 live copy; every value is synthetic.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from test_interval_returns import complete_schedule, fill_coverage
from test_toss_fill_evidence import FIXTURES, closed
from test_toss_fill_evidence import client as fill_client
from test_toss_flow_inference import T0, T1, coverage, fill, schedule, snapshot, usd
from test_toss_nav import END as NAV_END
from test_toss_nav import START as NAV_START
from test_toss_nav import capture

import interval_returns as ir
from portfolio_ledger import LedgerStore, normalize_transaction
from portfolio_model import toss_transactions
from toss_flow_inference import infer_toss_flows
from toss_nav_validation import validate_toss_response


@pytest.fixture
def client(monkeypatch):
    yield from fill_client.__wrapped__(monkeypatch)


def test_live_yfinance_fx_mark_resolves_the_tool_interval(tmp_path, monkeypatch):
    rows = [capture(tmp_path, monkeypatch, day=day, fx_source='yfinance').row for day in (NAV_START, NAV_END)]
    mark = rows[0]['fx_times']['USD']
    # Live 2.12.5 snapshots: float rate, bar-start price time, read time; no validity window.
    assert type(mark['rate_to_krw']) is float and mark['priced_at'].endswith('+01:00') and 'read_at' in mark
    assert 'valid_until' not in mark and 'rate_decimal' not in mark
    db = tmp_path / 'synthetic-nav.db'
    fill_coverage(db)
    for scope, references in (('toss', None), ('toss_investmcp', {'toss:primary'})):
        block = ir.ledgerless_returns(db, rows, scope, NAV_START, NAV_END, account_references=references,
                                      schedule_provider=complete_schedule)
        interval = block['intervals'][0]
        assert interval['reason_codes'] == ['SMALL_RESIDUAL_TREATED_AS_INCOME'], (scope, interval['reason_codes'])
        assert interval['confidence'] == 'conditional' and block['period']['return_pct'] is not None


@pytest.mark.parametrize('age_hours,resolved', [(23, True), (25, False)])
def test_fx_mark_without_validity_window_is_bounded_by_its_age(age_hours, resolved):
    start, end = snapshot(), snapshot(T1)
    for snap in (start, end):
        priced = datetime.fromisoformat(snap['time']) - timedelta(hours=age_hours)
        snap['fx'] = {'rate': '100.0', 'valid_from': priced.isoformat(), 'observed_at': snap['time'], 'valid_until': None}
    rows = infer_toss_flows([start, end], [], schedule(), coverage=coverage())
    assert all((r['flow_native'] == '0') is resolved for r in rows)
    assert all(('SNAPSHOT_PROVENANCE_INCOMPLETE' in r['reasons']) is not resolved for r in rows)


def amount_buy(quantity):
    """An amount-based US market buy as CLOSED history returns it: fractional fill, 6-dp amount."""
    return {'orderId': 'synthetic-amount-order', 'symbol': 'SYN', 'side': 'BUY', 'orderType': 'MARKET',
            'timeInForce': 'DAY', 'status': 'FILLED', 'price': None, 'quantity': quantity, 'orderAmount': '100',
            'currency': 'USD', 'orderedAt': '2026-03-28T23:00:00+09:00', 'canceledAt': None,
            'execution': {'filledQuantity': '0.512345', 'averageFilledPrice': '195.1803', 'filledAmount': '99.999651',
                          'commission': '0.35', 'tax': '0', 'filledAt': '2026-03-28T23:00:05+09:00',
                          'settlementDate': '2026-03-30'}}


@pytest.mark.parametrize('quantity', ['0', '0.512345'])
def test_amount_order_fill_page_is_complete_with_exact_decimals(client, tmp_path, quantity):
    client.pages['CLOSED'][0]['result']['orders'].append(amount_buy(quantity))
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete and rows.fill_reasons == [] and len(rows) == 2
    tx = next(tx for tx in toss_transactions(rows, 'toss:primary') if tx.currency == 'USD')
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read() as conn:
        event = normalize_transaction(store, conn, tx)
    assert (event['quantity'], event['gross_amount'], event['amount']) == ('0.512345', '99.999651', '-100.349651')


def live_order(order_type, side, currency, quantity, filled, average, gross, order_amount, status='FILLED'):
    """A CLOSED order as the 10/16 live pages return it: orderAmount set whatever its type, side or currency."""
    row = amount_buy(quantity)
    row.update(orderType=order_type, side=side, currency=currency, orderAmount=order_amount, status=status,
               price=(average or '195.18') if order_type == 'LIMIT' else None)
    row['execution'].update(filledQuantity=filled, averageFilledPrice=average, filledAmount=gross,
                            commission='105' if currency == 'KRW' else '0.35')
    if filled == '0':
        row['execution'].update(commission='0', filledAt=None, settlementDate=None)
    return row


@pytest.mark.parametrize('shape', [
    ('LIMIT', 'BUY', 'USD', '3', '3', '195.18', '585.54', '585.54'),
    ('LIMIT', 'SELL', 'USD', '3', '3', '195.6', '586.8', '585.54'),  # sold above the limit: gross past orderAmount
    ('LIMIT', 'BUY', 'USD', '7', '7', '195.185714', '1366.3', '1366.33'),  # 3 @ 195.18 + 4 @ 195.19, 6-dp average
    ('LIMIT', 'BUY', 'USD', '0.5', '0.5', '195.18', '97.59', '97.59'),
    ('LIMIT', 'BUY', 'KRW', '10', '10', '69950', '699500', '700000'),
    ('LIMIT', 'BUY', 'KRW', '3', '3', '70033.333333', '210100', '210100'),  # truncated 6-dp KRW average
    ('LIMIT', 'SELL', 'KRW', '10', '4', '70100', '280400', '700000', 'CANCELED'),
    ('MARKET', 'SELL', 'USD', '0.512345', '0.512345', '195.123456', '99.970527', '100'),
    ('MARKET', 'SELL', 'USD', '2', '2', '195.1803', '390.3606', '390.36'),
    ('LIMIT', 'BUY', 'USD', '5', '0', None, '0', '975.9', 'CANCELED'),
])
def test_live_closed_shapes_with_order_amount_are_complete(client, shape):
    client.pages['CLOSED'][0]['result']['orders'].append(live_order(*shape))
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete and rows.fill_reasons == [] and len(rows) == 1 + (shape[4] != '0')


@pytest.mark.parametrize('row', [
    live_order('LIMIT', 'BUY', 'KRW', '10.5', '10.5', '70000', '735000', '735000'),  # KRW quantities are whole
    live_order('MARKET', 'SELL', 'USD', '0.512345678', '0.512345678', '195.18', '99.999629', '100'),  # past 8 dp
    # A cent off, past half a cent plus the price and quantity precision; the old cent tolerance passed it.
    live_order('MARKET', 'SELL', 'USD', '0.512345', '0.512345', '195.123456', '99.980527', '100'),
    live_order('LIMIT', 'SELL', 'USD', '3', '4', '195.6', '782.4', '585.54', 'CANCELED'),
    live_order('LIMIT', 'SELL', 'USD', '3', '2', '195.6', '391.2', '585.54'),
    {**amount_buy('0'), 'orderAmount': '0'}, {**amount_buy('0'), 'quantity': '-1'},
    {**amount_buy('0'), 'execution': {'filledQuantity': '0', 'averageFilledPrice': None, 'filledAmount': None,
                                      'commission': None, 'tax': None, 'filledAt': None, 'settlementDate': None}},
])
def test_closed_order_outside_the_live_rules_invalidates(client, row):
    client.pages['CLOSED'][0]['result']['orders'].append(row)
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert not rows.complete and rows == [] and 'INVALID_FILL_TOTALS' in rows.fill_reasons


# 15 live rows (10/16): a whole quantity, a truncated 6-dp average price and a gross in cents.
# 195.18 + 195.18 + 195.20 = 585.56 averages 195.186666..., so 3 x 195.186666 = 585.559998;
# the tolerance is 3 x 0.000001 + 0.005. A gross a cent off is past it.
@pytest.mark.parametrize('gross,valid', [('585.56', True), ('585.57', False), ('585.55', False)])
def test_truncated_average_price_reconciles_within_its_precision(client, gross, valid):
    client.pages['CLOSED'][0]['result']['orders'].append(
        live_order('LIMIT', 'SELL', 'USD', '3', '3', '195.186666', gross, '585.54'))
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete is valid and ('INVALID_FILL_TOTALS' in rows.fill_reasons) is not valid


# R1 (2.12.7): a written scale widens nothing. "1.0" and KRW "3.0" are exact counts,
# and a 2-dp average carries the same 6-dp precision as "195.180000".
@pytest.mark.parametrize('currency,quantity,average,gross', [
    ('USD', '1.0', '195.180300', '196.1803'),  # $1 off
    ('KRW', '3.0', '70000', '211000'),  # 1,000 KRW off
    ('USD', '3', '195.18', '585.56'),  # 2 cents off
    ('USD', '3', '195.180000', '585.56'),
])
def test_written_scale_widens_no_fill_tolerance(client, currency, quantity, average, gross):
    client.pages['CLOSED'][0]['result']['orders'].append(
        live_order('LIMIT', 'BUY', currency, quantity, quantity, average, gross, gross))
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert not rows.complete and rows == [] and 'INVALID_FILL_TOTALS' in rows.fill_reasons


# A large fill keeps the API's precision band, past a cent: 10,000 x 0.000001 + 0.005 = 0.015.
@pytest.mark.parametrize('gross,valid', [('15000', True), ('15000.015', True), ('15000.025', False)])
def test_large_fill_reconciles_within_the_api_precision_band(client, gross, valid):
    client.pages['CLOSED'][0]['result']['orders'].append(
        live_order('LIMIT', 'BUY', 'USD', '10000', '10000', '1.5', gross, gross))
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete is valid and ('INVALID_FILL_TOTALS' in rows.fill_reasons) is not valid


# R1 (2.12.6): a $100 order whose execution reports $195.18 gross, as MARKET BUY and as LIMIT.
@pytest.mark.parametrize('order_type,quantity,gross,valid', [
    ('MARKET', '0', '195.180300', False),  # the amount bound: past $100 plus a cent
    ('LIMIT', '0', '195.180300', False),  # as R1 posed it: 1 filled of 0 ordered
    ('LIMIT', '1', '195.180300', True),  # a consistent LIMIT: its orderAmount informs, binds nothing
    ('LIMIT', '1', '100', False),  # 1 x 195.1803 is not 100: the product check
])
def test_r1_over_amount_fill_fails_under_its_own_rule(client, order_type, quantity, gross, valid):
    row = {**amount_buy(quantity), 'orderType': order_type, 'price': '195.1803' if order_type == 'LIMIT' else None}
    row['execution'].update(filledQuantity='1.000000', filledAmount=gross)
    client.pages['CLOSED'][0]['result']['orders'].append(row)
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete is valid and ('INVALID_FILL_TOTALS' in rows.fill_reasons) is not valid


@pytest.mark.parametrize('order_amount,valid', [('100', True), ('99.99', True), ('99.98', False)])
def test_amount_order_bounds_its_executed_gross_within_a_cent(client, order_amount, valid):
    # Gross 99.999651: inside 99.99 plus one USD minor unit, outside 99.98 plus one.
    client.pages['CLOSED'][0]['result']['orders'].append({**amount_buy('0'), 'orderAmount': order_amount})
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete is valid and ('INVALID_FILL_TOTALS' in rows.fill_reasons) is not valid


@pytest.mark.parametrize('zero,valid', [('0.000000', True), ('0.0000', True), ('0.000001', False)])
def test_unfilled_closed_order_zero_costs_at_any_scale(client, zero, valid):
    row = closed(client)
    row['status'] = 'CANCELED'
    row['execution'].update(filledQuantity='0', averageFilledPrice=None, filledAmount=zero, commission=zero,
                            tax='0', filledAt=None, settlementDate=None)
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete is valid and (rows.fill_reasons == []) is valid


@pytest.mark.parametrize('group,open_unverified', [('CLOSED', False), ('OPEN', True)])
def test_rejected_closed_history_is_not_an_open_order_failure(client, group, open_unverified):
    bad = deepcopy(closed(client)) | {'status': 'PARTIAL_FILLED'}
    bad['execution']['filledAmount'] = '700010'
    client.pages[group][0]['result']['orders'][:] = [bad]
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert not rows.complete and 'INVALID_FILL_TOTALS' in rows.fill_reasons
    assert ('TOSS_OPEN_ORDERS_UNVERIFIED' in rows.fill_reasons) is open_unverified
    # The NAV cash-basis check (OPEN orders, no request binding) keeps the label on any rejection.
    assert 'TOSS_OPEN_ORDERS_UNVERIFIED' in validate_toss_response('orders', {'result': None}).reason_codes


def test_amount_order_fill_feeds_cash_residual_inference():
    trade = fill(quantity='0.512345', price_native='195.1803', gross_amount='99.999651', fee='0.35', tax='0',
                 source_status='FILLED')
    rows = infer_toss_flows([snapshot(T0, usd='200'), snapshot(T1, usd='99.650349', qty='10.512345')], [trade],
                            schedule(), coverage=coverage())
    assert usd(rows)['flow_native'] == '0' and usd(rows)['diagnostics']['trade_cash_effect_native'] == '-100.349651'


# A live USD fill shape whose legacy float sum carries noise (-195.5302999…) against the
# exact strict amount (-195.5303). 21 legacy fills in the 10/16 run window have this shape.
NOISY = {'filledQuantity': '1.000000', 'averageFilledPrice': '195.1803', 'filledAmount': '195.180300',
         'commission': '0.35', 'tax': '0'}


def noisy_buy():
    row = {**amount_buy('0'), 'orderAmount': '200'}
    row['execution'].update(NOISY)
    return row


# Stream names as the collectors write them: legacy `toss/<hash>`, strict `toss/fills-v1/<hash>`.
LEGACY_STREAM, STRICT_STREAM = 'toss/0123456789abcdef', 'toss/fills-v1/0123456789abcdef'


def saved(store, tmp_path, stream, event):
    """Store a row through CollectionRun.save under `stream` (evidence plus coverage)."""
    from test_collect_ledger import collect_ledger

    collect_ledger.CollectionRun(store, tmp_path / 'raw').save(stream, [event], event['account_identity'],
                                                               '2026-03-28', '2026-03-29')


def noisy_pair(client, tmp_path):
    client.pages['CLOSED'][0]['result']['orders'][:] = [noisy_buy()]
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read() as conn:
        legacy = normalize_transaction(store, conn, toss_transactions(list(rows), 'toss:primary')[0])
        strict = normalize_transaction(store, conn, toss_transactions(rows, 'toss:primary')[0])
    assert strict['amount'] == '-195.530300' and legacy['amount'] != '-195.5303'
    return store, legacy, strict


@pytest.mark.parametrize('change,differences', [
    ({}, []),
    ({'amount': '-195.5403'}, ['amount']),  # one USD minor unit
    ({'amount': '-195.5304'}, ['amount']),  # one unit of the 4-decimal USD amount precision
    ({'fee': '0.36'}, ['fee']),
    ({'quantity': '1.00000001'}, ['quantity']),
    ({'symbol': 'SYN2'}, ['symbol']),
])
def test_strict_toss_fill_tolerates_only_legacy_float_noise(client, tmp_path, change, differences):
    store, legacy, strict = noisy_pair(client, tmp_path)
    saved(store, tmp_path, LEGACY_STREAM, legacy)
    with store.read() as conn:
        assert store.prepare(conn, strict)['differences'] == ['amount']  # without the hint: exact
        assert store.prepare(conn, {**strict, **change, '_legacy_float_noise': True})['differences'] == differences


@pytest.mark.parametrize('other', [{'source': 'kis_transactions'}, {'kind': 'income_other', 'side': None}])
def test_float_noise_hint_never_relaxes_other_sources_or_kinds(client, tmp_path, other):
    store, legacy, strict = noisy_pair(client, tmp_path)
    other = {**other, 'source_record_id': legacy['source_record_id'] + ':other'}
    saved(store, tmp_path, LEGACY_STREAM, {**legacy, **other})
    with store.read() as conn:
        assert store.prepare(conn, {**strict, **other, '_legacy_float_noise': True})['differences'] == ['amount']


@pytest.mark.parametrize('stream,differences', [(LEGACY_STREAM, []), (STRICT_STREAM, ['amount']), (None, ['amount'])])
def test_float_noise_is_tolerated_only_in_rows_the_legacy_collector_wrote(client, tmp_path, stream, differences):
    # R1 round 2 (2.12.6): digit count is no provenance. The same noisy value
    # written by the strict path, or with no evidence, compares exactly.
    store, legacy, strict = noisy_pair(client, tmp_path)
    if stream:
        saved(store, tmp_path, stream, legacy)
    else:
        store.append_many([(legacy, None)])
    with store.read() as conn:
        assert store.prepare(conn, {**strict, '_legacy_float_noise': True})['differences'] == differences


def test_enriched_value_on_a_legacy_row_compares_exactly(client, tmp_path):
    store, legacy, strict = noisy_pair(client, tmp_path)
    saved(store, tmp_path, LEGACY_STREAM, {**legacy, 'fee': None, 'fee_asset': None})
    # A later writer adds the fee the legacy row lacked; that value is not legacy-written.
    store.append_many([({**legacy, 'fee': '0.35004000000000001'}, None)])
    with store.read() as conn:
        assert store.prepare(conn, {**strict, '_legacy_float_noise': True})['differences'] == ['fee']


@pytest.mark.parametrize('legacy_commission,matches', [(None, True), ('0.36', False)])
def test_noisy_usd_legacy_fill_completes_the_strict_window(client, tmp_path, legacy_commission, matches):
    from test_collect_ledger import Binance, Hyperliquid, collect_ledger

    from portfolio_sources import PortfolioCollector, Providers
    client.pages['CLOSED'][0]['result']['orders'][:] = [noisy_buy()]
    if legacy_commission:
        client.pages['LEGACY'] = deepcopy(client.pages['CLOSED'])
        client.pages['LEGACY'][0]['result']['orders'][0]['execution']['commission'] = legacy_commission
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
        kis_accounts=lambda: [], kis_client=lambda label: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=True)
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
                                   binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    assert result['failed'] is False
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == 0
        events = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM ledger_events WHERE source='toss_transactions'")]
        assert len(events) == 1 and events[0]['amount'] != '-195.5303'  # the legacy row is not replaced
        window = conn.execute("SELECT * FROM ledger_coverage WHERE stream LIKE 'toss/fills-v1/%'").fetchone()
        reasons = json.loads(window['reason_codes_json'])
        assert (window['status'], reasons) == (('complete', []) if matches else ('limited', ['FILL_LEGACY_EVIDENCE_MISMATCH']))


@pytest.mark.parametrize('change,differences', [
    ({}, []),
    ({'fee': '0.35004', 'amount': '-100.349691'}, ['amount', 'fee']),  # R1: strict 0.35 -> 0.35004
    ({'amount': '-100.34965'}, ['amount']),  # a 6-dp strict amount is no float artefact
])
def test_stored_strict_fill_compares_exactly_under_the_hint(client, tmp_path, change, differences):
    client.pages['CLOSED'][0]['result']['orders'][:] = [amount_buy('0')]
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read() as conn:
        strict = normalize_transaction(store, conn, toss_transactions(rows, 'toss:primary')[0])
    saved(store, tmp_path, STRICT_STREAM, strict)
    with store.read() as conn:
        assert store.prepare(conn, {**strict, **change, '_legacy_float_noise': True})['differences'] == differences


@pytest.mark.parametrize('row,first,second', [
    (amount_buy('0'), '0.35', '0.35004'),  # R1
    (noisy_buy(), '0.35004000000000001', '0.35'),  # R1 round 2: float-like digits on a strict row
])
def test_strict_fee_correction_through_the_collector_is_a_mismatch(client, tmp_path, row, first, second):
    """R1 (2.12.6): a strict-only fill (legacy window empty) re-reported with a corrected fee."""
    from test_collect_ledger import Binance, Hyperliquid, collect_ledger

    from portfolio_sources import PortfolioCollector, Providers
    client.pages['LEGACY'] = [deepcopy(FIXTURES['empty'])]
    client.pages['CLOSED'][0]['result']['orders'][:] = [{**row, 'orderedAt': '2025-12-01T09:00:00+09:00'}]
    pages = deepcopy({group: client.pages[group] for group in ('OPEN', 'CLOSED')})
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
        kis_accounts=lambda: [], kis_client=lambda label: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=True)
    store = LedgerStore(tmp_path / 'ledger.db')
    for commission in (first, second):
        client.pages.update(deepcopy(pages))
        closed(client)['execution']['commission'] = commission
        result = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
                                       binance_factory=Binance, hyperliquid_factory=Hyperliquid)
        assert result['failed'] is False
    with store.read() as conn:
        events = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM ledger_events WHERE source='toss_transactions'")]
        assert [e['fee'] for e in events] == [first]  # the old values stay, flagged below
        windows = conn.execute("SELECT status,reason_codes_json FROM ledger_coverage "
                               "WHERE stream LIKE 'toss/fills-v1/%' ORDER BY rowid").fetchall()
        assert [(w['status'], json.loads(w['reason_codes_json'])) for w in windows] == [
            ('complete', []), ('limited', ['FILL_LEGACY_EVIDENCE_MISMATCH'])]
