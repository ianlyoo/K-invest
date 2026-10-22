"""Pass-one review regressions: synthetic providers and disposable local stores."""
import time
from copy import deepcopy
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import pytest
from test_daily_nav import risk_capture
from test_toss_nav import START, capture, selected

from portfolio_store import NavStore, snapshot_summary


@pytest.mark.parametrize('marker', [
    {'partial': True}, {'nextCursor': 'synthetic-continuation'},
    {'result': {'orders': [], 'hasNext': False, 'partial': True}},
    {'result': {'orders': [], 'hasNext': False, 'next_cursor': 'synthetic-continuation'}},
    {'error': {'code': 'SYNTHETIC_PARTIAL'}},
    {'result': {'orders': [], 'hasNext': False, 'nextCursor': 0}},
    {'result': {'orders': [None], 'hasNext': False}},
    {'result': {'orders': [{'orderId': 'synthetic-order'}]}},
])
def test_review_open_order_partial_or_cursor_never_certifies_cash(tmp_path, monkeypatch, marker):
    payload = {'result': {'orders': [], 'hasNext': False}, **marker}
    row = capture(tmp_path, monkeypatch, order_payload=payload).row
    cash = row['account_nav']['toss:primary']['cash']['USD']
    assert cash['value'] is None
    assert 'TOSS_OPEN_ORDERS_UNVERIFIED' in selected(row)['reason_codes']


def test_review_truncated_toss_holdings_block_account_completeness(tmp_path, monkeypatch):
    data = capture(tmp_path, monkeypatch, holdings_truncated=True)
    assert 'toss:primary' in data.collection.truncated_sources
    assert data.row['collection_source_quality']['truncated_sources'] == ['toss:primary']
    assert data.row['account_nav']['toss:primary']['source_status'] == 'partial'
    assert selected(data.row)['total_value_krw'] is None
    assert 'TRUNCATED_SOURCES' in selected(data.row)['reason_codes']


@pytest.mark.parametrize('change,reason', [
    ({'quote_price': '10.02'}, None), ({'quote_currency': 'KRW'}, 'QUOTE_CURRENCY_MISMATCH'),
    ({'quote_symbol': 'OTHER'}, 'MISSING_TOSS_QUOTE'), ({'quote_price': 'NaN'}, 'INVALID_QUOTE_PRICE'),
    ({'quote_price': '10.0101'}, None), ({'quote_price': '10.0100'}, None),
])
def test_review_native_quote_valuation_preserves_legacy_and_rejects_invalid_quotes(tmp_path, monkeypatch, change, reason):
    for price in ('10.0000', '10'):
        good = capture(tmp_path, monkeypatch, quote_price=price)
        assert selected(good.row)['total_value_krw'] == 3500
        assert good.collection.holdings[0].last_price == 10
    data = capture(tmp_path, monkeypatch, **change)
    component = next(c for c in data.row['components'] if c['account'] == 'toss:primary' and c['component_type'] == 'holding')
    if reason is None:
        assert component['priced_at'] == START + 'T09:10:00Z'
        assert component['native_market_value']['value'] == float(2 * Decimal(change['quote_price']))
        assert selected(data.row)['total_value_krw'] == float(1500 + 200 * Decimal(change['quote_price']))
        assert selected(data.row)['result_status'] == 'complete'
    else:
        assert component['native_market_value']['value'] is None
        assert reason in component['reason_codes']
        assert reason in selected(data.row)['reason_codes']
        assert selected(data.row)['total_value_krw'] is None
    assert 'QUOTE_PRICE_MISMATCH' not in component['reason_codes']
    assert 'QUOTE_PRICE_MISMATCH' not in selected(data.row)['reason_codes']
    assert component['last_price'] == 10 and component['market_value'] == 20
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']


MONDAY = '2026-10-12'
FRIDAY_CLOSE = '2026-10-09T20:00:00Z'  # Saturday 05:00 KST.


def us_calendar(*, holiday=False):
    def day(date, start, end):
        return {'date': date, 'regularMarket': {'startTime': start, 'endTime': end}}
    return {'previousBusinessDay': day('2026-10-09', '2026-10-09T13:30:00Z', FRIDAY_CLOSE),
            'today': {'date': MONDAY, 'regularMarket': None} if holiday else
                day(MONDAY, MONDAY + 'T13:30:00Z', MONDAY + 'T20:00:00Z'),
            'nextBusinessDay': day('2026-10-13', '2026-10-13T13:30:00Z', '2026-10-13T20:00:00Z')}


@pytest.mark.parametrize('holiday', [False, True])
def test_review_monday_1810_friday_us_close_is_current(tmp_path, monkeypatch, holiday):
    row = capture(tmp_path, monkeypatch, day=MONDAY, quote_stamp=FRIDAY_CLOSE, calendar=us_calendar(holiday=holiday)).row
    quote = next(q for q in row['mark_quality']['quotes'] if q['account'] == 'toss:primary' and q['component_type'] == 'holding')
    assert quote['age_seconds'] > 24 * 3600
    assert quote['status'] == 'current' and quote['reason_codes'] == []
    assert selected(row)['total_value_krw'] == 4050
    assert selected(row)['reason_codes'] == []


@pytest.mark.parametrize('change,reason', [
    ({'quote_stamp': '2026-09-10T20:00:00Z', 'calendar': us_calendar()}, 'STALE_QUOTE'),
    ({'quote_stamp': FRIDAY_CLOSE, 'calendar': None}, 'MARK_FRESHNESS_UNVERIFIED'),
    ({'fx_stamp': '2026-09-10T20:00:00Z'}, 'STALE_FX'),
])
def test_review_stale_marks_block_toss_scope(tmp_path, monkeypatch, change, reason):
    row = capture(tmp_path, monkeypatch, day=MONDAY, **change).row
    assert selected(row)['total_value_krw'] is None
    assert reason in selected(row)['reason_codes']
    assert selected(row, 'toss')['total_value_krw'] is None


def test_review_fx_validity_interval_can_cover_an_older_mark(tmp_path, monkeypatch):
    row = capture(tmp_path, monkeypatch, day=MONDAY,
        fx_stamp='2026-09-10T20:00:00Z', fx_until=MONDAY + 'T10:00:00Z').row
    assert row['mark_quality']['fx']['USD']['status'] == 'current'
    assert row['fx_times']['USD']['valid_until'] == MONDAY + 'T10:00:00Z'
    assert selected(row)['total_value_krw'] == 4050


def test_review_a_regular_close_invalidates_even_a_same_day_quote(tmp_path, monkeypatch):
    from daily_nav import mark_quality
    row = capture(tmp_path, monkeypatch, day=MONDAY).row
    row['snapshot_date'] = '2026-10-13'  # Monday after the US close is Tuesday KST.
    row['valuation_at'] = row['observed_at'] = row['nav_evidence_completed_at'] = MONDAY + 'T21:00:00Z'
    row['fx_times']['USD']['priced_at'] = row['valuation_at']
    if 'read_at' in row['fx_times']['USD']:
        row['fx_times']['USD']['read_at'] = row['valuation_at']
    for component in row['components']:
        if component['provider'] == 'toss':
            component['priced_at'] = MONDAY + 'T19:00:00Z'
            if component['component_type'] == 'holding':
                component['market_calendar'] = {'country': 'US', **us_calendar()}
    row['mark_quality'] = mark_quality(row)
    assert selected(row)['total_value_krw'] is None
    assert 'STALE_QUOTE' in selected(row)['reason_codes']


def test_review_evidence_completion_preserves_crypto_selection_and_flow_cutoff(tmp_path, monkeypatch):
    from portfolio_ledger import LedgerStore
    data = capture(tmp_path, monkeypatch)
    completed = START + 'T09:12:00Z'
    data.collection.nav_completed_at = completed
    summary = snapshot_summary(data.legacy, data.collection, data.fx)
    path = tmp_path / 'synthetic-cutoff.db'
    risk_capture(path, START + 'T09:10:00Z', snapshot_id='at-collection')
    risk_capture(path, completed, snapshot_id='at-evidence-completion')
    ledger = LedgerStore(path)
    with ledger.transaction() as conn:
        ledger.append(conn, dict(source='synthetic', account_identity='acct_' + 'a' * 32,
            source_record_id='after-collection', kind='deposit', amount='7', currency='KRW',
            execution_time_utc=START + 'T09:11:00Z'))
    row = NavStore(path).upsert_snapshot(summary, START)
    assert row['valuation_at'] == data.collection.fetched_at
    assert row['observed_at'] == data.collection.fetched_at
    assert row['nav_evidence_completed_at'] == completed
    assert row['crypto_nav']['risk_snapshot_id'] == 'at-collection'
    assert row['external_flows']['observed_events'] == []


@pytest.mark.parametrize('tool', ['get_portfolio_summary', 'snapshot_portfolio_nav'])
def test_review_tool_optional_evidence_has_one_budget_and_discards_late_results(tmp_path, monkeypatch, tool):
    import portfolio_sources
    import server
    data = capture(tmp_path, monkeypatch)
    data.collection.nav_evidence = {}
    data.collection.nav_completed_at = None
    release, finished = Event(), Event()

    def evidence(collection):
        try:
            release.wait(.25)
            collection.nav_evidence = {
                account: {'open_orders': 'none', 'quote_times': {('SYN', 'USD'): START + 'T09:10:00Z'},
                          'quote_prices': {('SYN', 'USD'): price}}
                for account, price in [('toss:primary', 10), ('toss:secondary', 20)]}
            collection.nav_completed_at = START + 'T09:10:01Z'
        finally:
            finished.set()

    collector = SimpleNamespace(collect_positions=lambda: data.collection, build_fx=lambda currencies: data.fx,
                                collect_nav_evidence=evidence)
    monkeypatch.setattr(portfolio_sources, 'NAV_EVIDENCE_BUDGET_SECONDS', .03, raising=False)
    monkeypatch.setattr(server, '_get_collector', lambda: collector)
    monkeypatch.setattr(server, '_get_nav_store', lambda: NavStore(tmp_path / 'synthetic-tools.db'))
    monkeypatch.setattr(server, 'validate_live_snapshot_date', lambda value: START)
    started = time.monotonic()
    try:
        response = server.get_portfolio_summary(include_realized=False, snapshot=False) if tool == 'get_portfolio_summary' else server.snapshot_portfolio_nav()
        elapsed = time.monotonic() - started
        assert response['ok'] is True
        assert elapsed < .15, elapsed
        row = response['data'] if tool == 'get_portfolio_summary' else response['data']['snapshot']
        cash = row['account_nav']['toss:primary']['cash']['USD']
        assert cash['value'] is None and 'NAV_EVIDENCE_BUDGET_EXCEEDED' in cash['reason_codes']
        assert selected(row)['total_value_krw'] is None
        frozen = deepcopy(data.collection.nav_evidence)
    finally:
        release.set()
        assert finished.wait(1)
    assert data.collection.nav_evidence == frozen
