"""Third review: calendar proof through collection, frozen snapshots and scopes."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from test_toss_nav import capture, selected
from test_toss_nav_review import FRIDAY_CLOSE, MONDAY, us_calendar


def assert_calendar_blocked(data):
    assert data.collection.nav_evidence['toss:primary']['market_calendars']['US'] is None
    for quote in data.row['mark_quality']['quotes']:
        if quote['account'].startswith('toss:') and quote['component_type'] == 'holding':
            assert quote['status'] == 'unknown'
            assert quote['reason_codes'] == ['MARK_FRESHNESS_UNVERIFIED']
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] is None
        assert nav['result_status'] == 'partial'
        assert 'MARK_FRESHNESS_UNVERIFIED' in nav['reason_codes']
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']


@pytest.mark.parametrize('location,metadata', [
    ('root', {'partial': True}),
    ('root', {'error': {'code': 'SYNTHETIC_CALENDAR_ERROR'}}),
    ('root', {'errors': ['SYNTHETIC_CALENDAR_ERROR']}),
    ('root', {'error': 'SYNTHETIC_CALENDAR_ERROR'}),
    ('root', {'truncated': True}),
    ('today', {'errorMessage': 'synthetic calendar error'}),
    ('previous_session', {'error_code': 'SYNTHETIC_CALENDAR_ERROR'}),
])
def test_pass3_partial_or_error_calendar_with_null_session_blocks_snapshot(tmp_path, monkeypatch, location, metadata):
    calendar = us_calendar(holiday=True)
    target = {'root': calendar, 'today': calendar['today'],
              'previous_session': calendar['previousBusinessDay']['regularMarket']}[location]
    target.update(metadata)
    data = capture(tmp_path, monkeypatch, day=MONDAY, quote_stamp=FRIDAY_CLOSE, calendar=calendar)
    # Before this fix, metadata was discarded and the null session certified a
    # holiday. The entire snapshot path must retain an unknown freshness instead.
    assert_calendar_blocked(data)


def test_pass3_monday_session_closing_tuesday_blocks_snapshot_and_stored_scope(tmp_path, monkeypatch):
    calendar = us_calendar()
    calendar['today']['regularMarket']['endTime'] = '2026-10-13T13:00:00Z'  # Tuesday 09:00 New York.
    assert_calendar_blocked(capture(tmp_path, monkeypatch, day=MONDAY,
        quote_stamp=FRIDAY_CLOSE, calendar=calendar))

    # The same defect in already stored evidence must block even if the stored
    # mark_quality incorrectly says current (scope reads revalidate components).
    good = capture(tmp_path, monkeypatch, day=MONDAY, quote_stamp=FRIDAY_CLOSE, calendar=us_calendar())
    for component in good.row['components']:
        if component['provider'] == 'toss' and component['component_type'] == 'holding':
            component['market_calendar'] = {'country': 'US', **deepcopy(calendar)}
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(good.row, scope)
        assert nav['total_value_krw'] is None
        assert 'MARK_FRESHNESS_UNVERIFIED' in nav['reason_codes']

    # A holiday is an explicit whole-session closure, never a session with an
    # absent endpoint. Check all missing/null endpoint shapes on the full path.
    for endpoint in ('startTime', 'endTime'):
        for missing in (False, True):
            broken = us_calendar()
            if missing:
                del broken['today']['regularMarket'][endpoint]
            else:
                broken['today']['regularMarket'][endpoint] = None
            assert_calendar_blocked(capture(tmp_path, monkeypatch, day=MONDAY,
                quote_stamp=FRIDAY_CLOSE, calendar=broken))


@pytest.mark.parametrize('holiday', [False, True])
@pytest.mark.parametrize('envelope', [False, True])
def test_pass3_valid_calendar_stays_complete_and_partial_capture_stays_unknown(tmp_path, monkeypatch, holiday, envelope):
    calendar = us_calendar(holiday=holiday)
    # Toss expresses US endpoints in KST. The close has the following KST date
    # but still the declared New York date; valid calendars must stay complete.
    for day in calendar.values():
        if day['regularMarket'] is not None:
            date = day['date']
            next_day = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
            day['regularMarket'] = {'startTime': date + 'T22:30:00+09:00',
                                    'endTime': next_day + 'T05:00:00+09:00'}
    payload = {'result': calendar} if envelope else calendar
    good = capture(tmp_path, monkeypatch, day=MONDAY, quote_stamp=FRIDAY_CLOSE,
        calendar=payload, calendar_envelope=envelope)
    for scope, expected in (('toss_investmcp', 4050), ('toss', 7050)):
        nav = selected(good.row, scope)
        assert nav['total_value_krw'] == expected
        assert nav['result_status'] == 'complete' and nav['reason_codes'] == []
    if envelope:
        assert good.clients['primary'].calendar_calls == ['/api/v1/market-calendar/US']
        assert good.clients['secondary'].calendar_calls == []
        # The existing result-only client output remains unchanged.
        assert good.clients['primary'].get_market_calendar('US') == calendar

    # Pair the positive control with a partial response to prove that completeness
    # is earned from evidence, including metadata outside the legacy result view.
    partial = deepcopy(payload)
    partial['partial'] = True
    bad = capture(tmp_path, monkeypatch, day=MONDAY, quote_stamp=FRIDAY_CLOSE,
        calendar=partial, calendar_envelope=envelope)
    assert_calendar_blocked(bad)
    assert selected(good.row)['result_status'] == 'complete'
    assert selected(bad.row)['total_value_krw'] is None
