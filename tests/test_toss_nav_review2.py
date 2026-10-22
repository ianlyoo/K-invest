"""Second recheck regressions, with synthetic marks and local SQLite only."""
from decimal import Decimal

import pytest
from test_toss_nav import capture, selected
from test_toss_nav_review import MONDAY

from daily_nav import mark_quality
from portfolio_store import NavStore, snapshot_summary


@pytest.mark.parametrize('holding,quote', [
    ('10.0000', '10.0100'),
    ('10.0000000000000002', '10.0000000000000001'),
])
def test_pass2_exact_source_decimal_drift_is_visible_without_blocking_quote_nav(tmp_path, monkeypatch, holding, quote):
    data = capture(tmp_path, monkeypatch, holding_price=holding, quote_price=quote)
    component = next(c for c in data.row['components'] if c['account'] == 'toss:primary' and c['component_type'] == 'holding')
    assert component['priced_at'] == data.collection.fetched_at
    assert component['legacy_price_decimal'] == holding
    assert component['quote_price_decimal'] == quote
    assert Decimal(component['price_drift_decimal']) == Decimal(quote) - Decimal(holding)
    assert component['native_market_value']['value'] == float(2 * Decimal(quote))
    assert selected(data.row)['total_value_krw'] == float(1500 + 200 * Decimal(quote))
    assert selected(data.row)['result_status'] == 'complete'
    assert 'QUOTE_PRICE_MISMATCH' not in selected(data.row)['reason_codes']
    assert component['valuation_price_decimal'] == quote
    assert component['last_price'] == 10 and component['market_value'] == 20


@pytest.mark.parametrize('change', [
    {'calendar': None}, {'calendar': {'today': {}}}, {'fail_calendar': True},
])
def test_pass2_missing_calendar_blocks_1900_quote_after_2000_close(tmp_path, monkeypatch, change):
    row = capture(tmp_path, monkeypatch, day=MONDAY, **change).row
    row['snapshot_date'] = '2026-10-13'  # Monday 21:00 UTC is Tuesday in Seoul.
    row['valuation_at'] = row['observed_at'] = row['nav_evidence_completed_at'] = MONDAY + 'T21:00:00Z'
    component = next(c for c in row['components'] if c['account'] == 'toss:primary' and c['component_type'] == 'holding')
    component['priced_at'] = MONDAY + 'T19:00:00Z'
    row['mark_quality'] = mark_quality(row)
    quote = next(q for q in row['mark_quality']['quotes'] if q['account'] == 'toss:primary' and q['component_type'] == 'holding')
    assert 'MARK_FRESHNESS_UNVERIFIED' in quote['reason_codes']
    assert selected(row)['total_value_krw'] is None
    assert 'MARK_FRESHNESS_UNVERIFIED' in selected(row)['reason_codes']


def test_pass2_explicit_fx_expiry_blocks_even_within_24_hours(tmp_path, monkeypatch):
    row = capture(tmp_path, monkeypatch, day=MONDAY,
        fx_stamp=MONDAY + 'T09:00:00Z', fx_until=MONDAY + 'T09:09:59Z').row
    assert 'STALE_FX' in row['mark_quality']['fx']['USD']['reason_codes']
    assert selected(row)['total_value_krw'] is None
    assert 'STALE_FX' in selected(row)['reason_codes']


@pytest.mark.parametrize('until', [None, 'malformed'])
def test_pass2_supplied_invalid_fx_expiry_cannot_use_24_hour_fallback(tmp_path, monkeypatch, until):
    data = capture(tmp_path, monkeypatch, day=MONDAY)
    data.fx.valid_until_by_currency['USD'] = until
    summary = snapshot_summary(data.legacy, data.collection, data.fx)
    row = NavStore(tmp_path / 'synthetic-invalid-expiry.db').upsert_snapshot(summary, MONDAY)
    assert 'INVALID_FX_VALID_UNTIL' in row['mark_quality']['fx']['USD']['reason_codes']
    assert selected(row)['total_value_krw'] is None


@pytest.mark.parametrize('priced,read,age', [('04', '05', 1), ('05.250', '05.500', .25)])
def test_pass2_live_1810_fx_uses_its_read_time_without_false_future_or_expiry(tmp_path, monkeypatch, priced, read, age):
    # 18:10:00 KST collection, mark updated at :04 and read at :05. The optional
    # NAV reads can complete at :30, after this FX mark's explicit :10 expiry.
    read_at = MONDAY + f'T09:10:{read}Z'
    data = capture(tmp_path, monkeypatch, day=MONDAY,
        fx_stamp=MONDAY + f'T09:10:{priced}Z', fx_until=MONDAY + 'T09:10:10Z', fx_read_at=read_at)
    for completion in (None, MONDAY + 'T09:10:30Z'):
        data.collection.nav_completed_at = completion
        summary = snapshot_summary(data.legacy, data.collection, data.fx)
        mark = summary['mark_quality']['fx']['USD']
        assert mark['status'] == 'current' and mark['reason_codes'] == []
        assert mark['freshness_checked_at'] == read_at and mark['age_seconds'] == age
        assert mark['freshness_time_basis'] == 'fx_read'
        assert summary['valuation_at'] == summary['observed_at'] == MONDAY + 'T09:10:00Z'
        assert summary['fx_times']['USD']['read_at'] == read_at
        row = NavStore(tmp_path / 'synthetic-read-clock.db').upsert_snapshot(summary, MONDAY)
        assert selected(row)['total_value_krw'] == 4050
        assert selected(row)['reason_codes'] == []


def test_pass2_fx_expired_at_read_blocks_even_when_collection_start_was_valid(tmp_path, monkeypatch):
    row = capture(tmp_path, monkeypatch, day=MONDAY,
        fx_stamp=MONDAY + 'T09:09:00Z', fx_until=MONDAY + 'T09:10:02Z',
        fx_read_at=MONDAY + 'T09:10:05Z').row
    assert row['valuation_at'] == MONDAY + 'T09:10:00Z'
    assert 'STALE_FX' in row['mark_quality']['fx']['USD']['reason_codes']
    assert selected(row)['total_value_krw'] is None
    assert 'STALE_FX' in selected(row)['reason_codes']
