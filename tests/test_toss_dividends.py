"""No public data fetches: test real YFinanceClient through its mocked transport."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
from time import perf_counter
from types import SimpleNamespace

import pytest

from yfinance_api import YFinanceClient


@pytest.fixture(autouse=True)
def daily_cache_isolation(monkeypatch, tmp_path):
    from yfinance_api import _DIVIDEND_CACHE, _DIVIDEND_CACHE_LOCK

    with _DIVIDEND_CACHE_LOCK:
        _DIVIDEND_CACHE.clear()
    monkeypatch.setenv('KINVEST_CACHE_DIR', str(tmp_path / 'reference-cache'))
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: datetime(2026, 10, 13, 12, tzinfo=timezone.utc))
    yield
    for future in list(_DIVIDEND_CACHE.values()):
        future.result(timeout=10)
    with _DIVIDEND_CACHE_LOCK:
        _DIVIDEND_CACHE.clear()


def epoch(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def bars(chart, params, currency='USD'):
    """Add the weekday bars a real interval=1d chart has across period1..period2.

    Existing stamps (ex-dates) stay. Each quote list grows by as many values as
    bars added, so a deliberately short list stays short.
    """
    row = chart['chart']['result'][0]
    clock = 'T13:30:00' if currency == 'USD' else 'T00:00:00'
    day, have, added = datetime.fromtimestamp(params['period1'], timezone.utc).date(), set(row['timestamp']), []
    while (stamp := epoch(day.isoformat() + clock)) < params['period2']:
        if day.weekday() < 5 and stamp >= params['period1'] and stamp not in have:
            added.append(stamp)
        day += timedelta(days=1)
    row['timestamp'] = sorted(row['timestamp'] + added)
    for values in row['indicators']['quote'][0].values():
        values.extend([10] * len(added))
    return chart


def responses():
    ex, pay = epoch('2026-10-09T13:30:00'), epoch('2026-10-11T13:30:00')
    calendar = {'quoteSummary': {'error': None, 'result': [{'price': {'symbol': 'SYN', 'currency': 'USD'},
                'calendarEvents': {'exDividendDate': {'raw': ex}, 'dividendDate': {'raw': pay}}}]}}
    chart = {'chart': {'error': None, 'result': [{'meta': {'symbol': 'SYN', 'currency': 'USD'},
             'timestamp': [ex], 'indicators': {'quote': [{'close': [10]}]},
             'events': {'dividends': {str(ex): {'date': ex, 'amount': 1.0}}}}]}}
    return calendar, chart


def fetch(monkeypatch, payloads):
    seen = []
    def raw(url, params=None, timeout=None):
        seen.append(url)
        assert timeout == 5
        return deepcopy(payloads[0]) if 'quoteSummary' in url else bars(deepcopy(payloads[1]), params)
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    result = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    assert len(seen) == 2
    return result


def test_latest_calendar_and_ex_date_event_join_is_conditional(monkeypatch):
    result = fetch(monkeypatch, responses())
    assert result['rows'][0]['gross_per_share'] == '1.0'
    assert result['rows'][0]['ex_date'] == '2026-10-09'
    assert result['rows'][0]['pay_date'] == '2026-10-11'
    assert result['coverage'][0]['status'] == 'estimated'
    assert result['coverage'][0]['start'] == '2026-10-10'
    assert result['coverage'][0]['end'] == '2026-10-11'


@pytest.mark.parametrize('mutation', ['symbol', 'currency', 'error', 'missing-envelope', 'epoch-ms',
                                     'duplicate-result', 'amount-string', 'nonfinite', 'row-count', 'duplicate-date',
                                     'missing-amount', 'wrong-ex-date', 'truncated'])
def test_malformed_reference_response_has_no_numeric_schedule(monkeypatch, mutation):
    payloads = responses()
    row = payloads[1]['chart']['result'][0]
    dividends = row['events']['dividends']
    event = next(iter(dividends.values()))
    if mutation == 'symbol':
        row['meta']['symbol'] = 'OTHER'
    elif mutation == 'currency':
        row['meta']['currency'] = 'KRW'
    elif mutation == 'error':
        payloads[0]['quoteSummary']['error'] = {'code': 'synthetic'}
    elif mutation == 'missing-envelope':
        del payloads[0]['quoteSummary']['error']
    elif mutation == 'epoch-ms':
        event['date'] *= 1000
    elif mutation == 'duplicate-result':
        payloads[1]['chart']['result'].append(deepcopy(row))
    elif mutation == 'amount-string':
        event['amount'] = '1'
    elif mutation == 'nonfinite':
        event['amount'] = float('nan')
    elif mutation == 'row-count':
        row['indicators']['quote'][0]['close'] = []
    elif mutation == 'duplicate-date':
        dividends['duplicate'] = deepcopy(event)
    elif mutation == 'missing-amount':
        del event['amount']
    elif mutation == 'wrong-ex-date':
        payloads[0]['quoteSummary']['result'][0]['calendarEvents']['exDividendDate']['raw'] -= 86400
    elif mutation == 'truncated':
        row['extension'] = {'hasNext': True}
    result = fetch(monkeypatch, payloads)
    assert result['rows'] == []
    assert 'INVALID_PUBLIC_DIVIDEND_RESPONSE' in result['coverage'][0]['reasons']


def test_kr_exchange_suffix_is_not_guessed(monkeypatch):
    def raw(url, params=None, timeout=None):
        assert url.endswith(('005930.KS', '005930.KQ'))
        return {'quoteSummary': {'error': {'code': 'Not Found'}, 'result': None}}
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    data = YFinanceClient().get_dividend_schedule('005930', currency='KRW', start='2026-10-10', end='2026-10-11')
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'


@pytest.mark.parametrize('wrapped', [False, True])
def test_calendar_scalar_and_raw_midnight_epochs_keep_calendar_date(monkeypatch, wrapped):
    payloads = responses()
    calendar = payloads[0]['quoteSummary']['result'][0]['calendarEvents']
    for key, value in [('exDividendDate', epoch('2026-10-09T00:00:00')),
                       ('dividendDate', epoch('2026-10-11T00:00:00'))]:
        calendar[key] = {'raw': value} if wrapped else value
    result = fetch(monkeypatch, payloads)
    assert result['coverage'][0]['status'] == 'estimated'
    assert result['rows'][0]['ex_date'] == '2026-10-09'
    assert result['rows'][0]['pay_date'] == '2026-10-11'


@pytest.mark.parametrize('value', [None, True, '1791676800', 1791676800000, {}, {'raw': None}])
def test_calendar_invalid_or_unknown_date_is_not_zero_evidence(monkeypatch, value):
    payloads = responses()
    payloads[0]['quoteSummary']['result'][0]['calendarEvents']['exDividendDate'] = value
    result = fetch(monkeypatch, payloads)
    assert result['rows'] == [] and result['coverage'][0]['status'] == 'limited'
    assert 'INVALID_PUBLIC_DIVIDEND_RESPONSE' in result['coverage'][0]['reasons']


def test_validated_event_free_window_resolves_ordinary_holding_days(monkeypatch):
    from test_toss_flow_inference import T0, T1, coverage, snapshot

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    payloads = responses()
    payloads[0]['quoteSummary']['result'][0]['calendarEvents'] = {}
    payloads[1]['chart']['result'][0]['events'] = {}
    fetch(monkeypatch, payloads)
    snaps = [snapshot(), snapshot(T1)]
    schedule = dividend_schedule_provider(snaps, '2026-10-10', '2026-10-11')
    assert schedule['coverage'][0]['status'] == 'estimated'
    values = infer_toss_flows(snaps, [], schedule, coverage=coverage(), start=T0, end=T1)
    assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in values)
    # This proof cannot be reused for an uncovered later interval.
    tail = infer_toss_flows([snapshot(T1), snapshot('2026-10-12T09:10:00+00:00')], [],
                            schedule, coverage=coverage())
    assert all(r['flow_native'] is None for r in tail)


@pytest.mark.parametrize('legacy', [False, True])
def test_heuristic_policy_cannot_be_relabelled_as_complete_proof(monkeypatch, legacy):
    from test_toss_flow_inference import T1, coverage, snapshot

    from toss_flow_inference import infer_toss_flows

    payloads = responses()
    payloads[0]['quoteSummary']['result'][0]['calendarEvents'] = {}
    payloads[1]['chart']['result'][0]['events'] = {}
    data = fetch(monkeypatch, payloads)
    data['coverage'][0].update(status='complete', reasons=[])
    if legacy:
        data['coverage'][0]['basis'] = 'market-payment-window-2026-10-13'
    values = infer_toss_flows([snapshot(), snapshot(T1)], [], data, coverage=coverage())
    assert all(r['flow_native'] is None and r['confidence'] == 'unresolved' for r in values)
    assert any('DIVIDEND_COVERAGE_INCOMPLETE' in r['reasons'] for r in values)


def test_known_calendar_payment_resolves_through_default_provider(monkeypatch):
    from test_toss_flow_inference import T0, T1, coverage, snapshot, usd

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    payloads = responses()
    calendar = payloads[0]['quoteSummary']['result'][0]['calendarEvents']
    for key in ('exDividendDate', 'dividendDate'):
        calendar[key] = calendar[key]['raw']
    fetch(monkeypatch, payloads)
    snaps = [snapshot('2026-10-08T09:10:00+00:00'), snapshot(), snapshot(T1, usd='108.5')]
    schedule = dividend_schedule_provider(snaps, '2026-10-10', '2026-10-11')
    row = usd(infer_toss_flows(snaps, [], schedule, coverage=coverage(), start=T0, end=T1))
    assert row['flow_native'] == '0' and row['income_native'] == '8.500'
    assert row['confidence'] == 'conditional'


def test_ex_event_without_known_payment_gets_conditional_market_window(monkeypatch):
    payloads = responses()
    payloads[0]['quoteSummary']['result'][0]['calendarEvents'] = {}
    result = fetch(monkeypatch, payloads)
    # Ex+0 includes early payments; the window never provides complete proof.
    assert result['coverage'][0]['status'] == 'estimated'
    assert result['rows'][0]['payment_start'] == '2026-10-09'
    assert result['rows'][0]['pay_date'] is None


def mock_history(monkeypatch, ex_dates, *, symbol='SYN', currency='USD', calendar=None):
    payloads = responses()
    payloads[0]['quoteSummary']['result'][0].update(
        price={'symbol': symbol, 'currency': currency}, calendarEvents=calendar or {})
    row = payloads[1]['chart']['result'][0]
    stamps = [epoch(d + ('T13:30:00' if currency == 'USD' else 'T00:00:00')) for d in ex_dates]
    row.update(meta={'symbol': symbol, 'currency': currency}, timestamp=stamps,
               indicators={'quote': [{'close': [10] * len(stamps)}]},
               events={'dividends': {str(t): {'date': t, 'amount': 1.0} for t in stamps}})
    seen = []
    def raw(url, params=None, timeout=None):
        seen.append((url, params))
        if currency == 'KRW' and not url.endswith(symbol):
            error = RuntimeError('synthetic not found')
            error.response = SimpleNamespace(status_code=404, json=lambda: {'quoteSummary': {
                'result': None, 'error': {'code': 'Not Found', 'description': f'Quote not found for symbol: {url.rsplit("/", 1)[-1]}'}}})
            raise error
        return deepcopy(payloads[0]) if 'quoteSummary' in url else bars(deepcopy(payloads[1]), params, currency)
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    return seen


def test_all_historical_ex_dates_get_us_payment_windows_and_match(monkeypatch):
    from test_toss_flow_inference import coverage, snapshot, usd

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    seen = mock_history(monkeypatch, ['2026-01-02', '2026-04-02', '2026-07-02'])
    client = YFinanceClient()
    data = client.get_dividend_schedule('SYN', currency='USD', start='2026-01-01', end='2026-10-01')
    assert len(data['rows']) == 3 and data['coverage'][0]['status'] == 'estimated'
    row = data['rows'][1]
    assert (row['payment_start'], row['expected_pay_date'], row['payment_end'], row['pay_date']) == (
        '2026-04-02', '2026-04-16', '2026-06-01', None)
    snaps = [snapshot('2026-04-01T09:10:00+00:00'), snapshot('2026-04-15T09:10:00+00:00'),
             snapshot('2026-04-16T09:10:00+00:00', usd='108.5')]
    schedule = dividend_schedule_provider(snaps, snaps[1]['time'], snaps[2]['time'])
    windows = [{**coverage()[0], 'start': '2026-01-01T00:00:00+00:00'}]
    result = usd(infer_toss_flows(snaps, [], schedule, coverage=windows, start=snaps[1]['time']))
    assert result['flow_native'] == '0' and result['income_native'] == '8.500'
    assert result['confidence'] == 'conditional' and result['events'][0]['time'] is None
    assert result['events'][0]['expected_pay_date'] == '2026-04-16'
    assert len(seen) == 2  # Other instances and query windows share the daily cache.
    ordinary = client.get_dividend_schedule('SYN', currency='USD', start='2026-06-03', end='2026-06-04')
    assert ordinary['rows'] == [] and ordinary['coverage'][0]['status'] == 'estimated'


@pytest.mark.parametrize(('ex', 'paid'), [('2025-12-29', '2026-04-15'), ('2026-03-30', '2026-05-15')])
def test_kr_expected_window_matches_december_april_and_quarterly_payment(monkeypatch, ex, paid):
    from test_toss_flow_inference import coverage, snapshot

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    seen = mock_history(monkeypatch, [ex], symbol='005930.KS', currency='KRW')
    data = YFinanceClient().get_dividend_schedule('005930', currency='KRW', start=ex, end=paid)
    assert data['coverage'][0]['status'] == 'estimated'
    assert data['rows'][0]['reference_symbol'] == '005930.KS' and data['rows'][0]['symbol'] == '005930'
    assert data['rows'][0]['pay_date'] is None
    when = datetime.fromisoformat(paid).replace(hour=9, minute=10, tzinfo=timezone.utc)
    snaps = [snapshot((datetime.fromisoformat(ex).replace(tzinfo=timezone.utc) - timedelta(days=1)).isoformat()),
             snapshot((when - timedelta(days=1)).isoformat()), snapshot(when.isoformat(), krw='2000008.46')]
    for snap in snaps:
        snap['holdings'][0].update(symbol='005930', currency='KRW')
    schedule = dividend_schedule_provider(snaps, snaps[1]['time'], snaps[2]['time'])
    windows = [{**coverage()[0], 'start': '2025-01-01T00:00:00+00:00'}]
    values = infer_toss_flows(snaps, [], schedule, coverage=windows, start=snaps[1]['time'])
    result = next(r for r in values if r['currency'] == 'KRW')
    assert result['flow_native'] == '0' and result['income_native'] == '8.4600'
    assert result['confidence'] == 'conditional' and len(seen) == 3


def test_missing_pay_field_is_valid_but_explicit_unknown_is_not(monkeypatch):
    calendar = {'exDividendDate': epoch('2026-04-02T00:00:00')}
    mock_history(monkeypatch, ['2026-04-02'], calendar=calendar)
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-04-15', end='2026-04-16')
    assert data['coverage'][0]['status'] == 'estimated' and data['rows'][0]['pay_date'] is None


def test_current_announced_payment_without_chart_amount_cannot_prove_zero(monkeypatch):
    mock_history(monkeypatch, [], calendar={'exDividendDate': epoch('2026-10-13T00:00:00'),
                                          'dividendDate': epoch('2026-10-13T00:00:00')})
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-12', end='2026-10-13')
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert 'INVALID_PUBLIC_DIVIDEND_RESPONSE' in data['coverage'][0]['reasons']


def test_announced_payment_overrides_window_and_preserves_event_identity(monkeypatch):
    seen = mock_history(monkeypatch, ['2026-04-02'])
    client = YFinanceClient()
    old = client.get_dividend_schedule('SYN', currency='USD', start='2026-04-01', end='2026-05-10')['rows'][0]
    mock_history(monkeypatch, ['2026-04-02'], calendar={'exDividendDate': epoch('2026-04-02T00:00:00'),
                                                      'dividendDate': epoch('2026-04-10T00:00:00')})
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    row = client.get_dividend_schedule('SYN', currency='USD', start='2026-04-01', end='2026-05-10')['rows'][0]
    assert row['id'] == old['id'] and row['payment_basis'] == 'announced_calendar'
    assert (row['payment_start'], row['payment_end']) == ('2026-04-07', '2026-04-13')
    assert len(seen) == 2


def test_daily_cache_projection_copy_and_rollover(monkeypatch):
    seen = mock_history(monkeypatch, ['2026-04-02'])
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-04-10', end='2026-04-16')
    data['rows'][0]['gross_per_share'] = '999'
    second = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-04-12', end='2026-04-18')
    assert second['rows'][0]['gross_per_share'] == '1.0' and len(seen) == 2
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-04-12', end='2026-04-18')
    assert len(seen) == 4


def test_failure_is_cached_per_day(monkeypatch):
    calls = []
    def raw(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('synthetic reference unavailable')
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    for end in ('2026-04-15', '2026-04-16', '2026-04-17'):
        data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-04-01', end=end)
        assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert len(calls) == 1


def test_overlapping_expected_windows_remain_ambiguous(monkeypatch):
    from test_toss_flow_inference import coverage, snapshot, usd

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    mock_history(monkeypatch, ['2026-04-02', '2026-04-03'])
    YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-04-01', end='2026-04-16')
    snaps = [snapshot('2026-04-01T09:10:00+00:00'), snapshot('2026-04-15T09:10:00+00:00'),
             snapshot('2026-04-16T09:10:00+00:00', usd='108.5')]
    schedule = dividend_schedule_provider(snaps, snaps[1]['time'], snaps[2]['time'])
    windows = [{**coverage()[0], 'start': '2026-01-01T00:00:00+00:00'}]
    result = usd(infer_toss_flows(snaps, [], schedule, coverage=windows, start=snaps[1]['time']))
    assert result['flow_native'] is None and 'DIVIDEND_AMBIGUOUS' in result['reasons']


def test_production_cold_reference_never_waits_for_network(monkeypatch):
    from test_toss_flow_inference import T0, T1, snapshot

    from toss_flow_inference import dividend_schedule_provider
    from yfinance_api import _DIVIDEND_CACHE

    entered, release = Event(), Event()
    calls = []
    payloads = responses()
    def raw(url, **kwargs):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return deepcopy(payloads[0]) if 'quoteSummary' in url else bars(deepcopy(payloads[1]), kwargs['params'])
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    try:
        began = perf_counter()
        data = dividend_schedule_provider([snapshot(), snapshot(T1)], T0, T1)
        assert perf_counter() - began < .5
        assert 'PUBLIC_DIVIDEND_REFRESH_PENDING' in data['coverage'][0]['reasons']
        assert entered.wait(2)
        dividend_schedule_provider([snapshot(), snapshot(T1)], T0, T1)
        assert len(calls) == 1
    finally:
        release.set()
        for future in list(_DIVIDEND_CACHE.values()):
            future.result(timeout=10)
    assert dividend_schedule_provider([snapshot(), snapshot(T1)], T0, T1)['coverage'][0]['status'] == 'estimated'
    assert len(calls) == 2


@pytest.mark.parametrize(('symbol', 'currency', 'ex', 'pay'), [
    # State Street 2026 schedule, revision 2026-09-23 p3; accessed 2026-10-13.
    ('SPY', 'USD', '2026-03-20', '2026-04-30'),
    ('SPYG', 'USD', '2026-03-23', '2026-03-25'),
    # Synthetic boundary of the design KR allowance, not an issuer claim.
    ('005930.KS', 'KRW', '2026-03-20', '2026-08-17'),
])
@pytest.mark.parametrize('matched', [False, True])
def test_widened_windows_match_net_or_leave_zero_pending(monkeypatch, symbol, currency, ex, pay, matched):
    from test_toss_flow_inference import coverage, snapshot

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    mock_history(monkeypatch, [ex], symbol=symbol, currency=currency)
    canonical = symbol.split('.')[0]
    anchor = datetime.fromisoformat(ex).replace(hour=9, minute=10, tzinfo=timezone.utc) - timedelta(days=1)
    paid = datetime.fromisoformat(pay).replace(hour=9, minute=10, tzinfo=timezone.utc)
    cash = {'usd': '108.5'} if currency == 'USD' else {'krw': '2000008.46'}
    snaps = [snapshot(anchor.isoformat()), snapshot((paid - timedelta(days=1)).isoformat()),
             snapshot(paid.isoformat(), **(cash if matched else {}))]
    for snap in snaps:
        snap['holdings'][0].update(symbol=canonical, currency=currency)
    client = YFinanceClient()
    data = client.get_dividend_schedule(canonical, currency=currency, start=ex, end=pay)
    assert data['coverage'][0]['status'] == 'estimated'
    assert data['rows'][0]['payment_start'] <= pay <= data['rows'][0]['payment_end']
    schedule = dividend_schedule_provider(snaps, snaps[1]['time'], snaps[2]['time'])
    windows = [{**coverage()[0], 'start': '2025-01-01T00:00:00+00:00', 'end': '2027-01-01T00:00:00+00:00'}]
    values = infer_toss_flows(snaps, [], schedule, coverage=windows, start=snaps[1]['time'])
    result = next(r for r in values if r['currency'] == currency)
    if matched:
        assert result['flow_native'] == '0' and result['confidence'] == 'conditional'
        assert result['diagnostics']['classification'] == 'dividend'
        assert result['events'][0]['kind'] == 'dividend'
    else:
        # The design accepts this estimated pending-zero outcome. An exactly
        # offsetting dividend/withdrawal remains an explicit estimation risk.
        assert result['flow_native'] == '0' and result['income_native'] == '0'
        assert result['events'] == [] and 'DIVIDEND_PENDING_NOT_PAID' in result['reasons']
        assert all(r['confidence'] == 'conditional' for r in values)


@pytest.mark.parametrize(('currency', 'symbol', 'lag'), [('USD', 'SYN', 62), ('KRW', '005930.KS', 152)])
@pytest.mark.parametrize('delta', ['zero', 'deposit', 'withdrawal', 'fx'])
def test_absence_outside_estimated_windows_never_becomes_observed(monkeypatch, currency, symbol, lag, delta):
    from test_toss_flow_inference import coverage, snapshot

    from toss_dividends import ABSENCE_ESTIMATED
    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    ex = '2026-03-20'
    mock_history(monkeypatch, [ex], symbol=symbol, currency=currency)
    when = datetime.fromisoformat(ex).replace(hour=9, minute=10, tzinfo=timezone.utc) + timedelta(days=lag)
    cash = {'deposit': {'usd': '200'}, 'withdrawal': {'usd': '50'},
            'fx': {'usd': '200', 'krw': '1860000'}, 'zero': {}}[delta]
    snaps = [snapshot((when - timedelta(days=1)).isoformat()), snapshot(when.isoformat(), **cash)]
    for snap in snaps:
        snap['holdings'][0].update(symbol=symbol.split('.')[0], currency=currency)
    data = YFinanceClient().get_dividend_schedule(symbol.split('.')[0], currency=currency,
        start=snaps[0]['time'], end=snaps[1]['time'])
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'estimated'
    schedule = dividend_schedule_provider(snaps, snaps[0]['time'], snaps[1]['time'])
    windows = [{**coverage()[0], 'start': '2025-01-01T00:00:00+00:00', 'end': '2027-01-01T00:00:00+00:00'}]
    values = infer_toss_flows(snaps, [], schedule, coverage=windows)
    assert all(r['confidence'] == 'conditional' and ABSENCE_ESTIMATED in r['reasons'] for r in values)
    usd = next(r for r in values if r['currency'] == 'USD')
    assert usd['flow_native'] == {'zero': '0', 'deposit': '100', 'withdrawal': '-50', 'fx': '0'}[delta]
    assert usd['diagnostics']['classification'] == ('fx_conversion' if delta == 'fx' else delta)


@pytest.mark.parametrize(('currency', 'symbol', 'before', 'after', 'later'), [
    # New York 23:50 and 00:10 fall on one UTC day (10/16), as at the 2.12.7 rollout.
    ('USD', 'SYN', '2026-10-16T03:50:00+00:00', '2026-10-16T04:10:00+00:00', '2026-10-16T23:00:00+00:00'),
    # Seoul 23:50 and 00:10 fall on one UTC day (10/15); 10/16 14:00 UTC is still Seoul's 10/16.
    ('KRW', '005930.KS', '2026-10-15T14:50:00+00:00', '2026-10-15T15:10:00+00:00', '2026-10-16T14:00:00+00:00'),
])
def test_refresh_day_follows_the_market_calendar_not_utc(monkeypatch, currency, symbol, before, after, later):
    seen = mock_history(monkeypatch, ['2026-03-20'], symbol=symbol, currency=currency)

    def get(when):
        monkeypatch.setattr('yfinance_api._dividend_now', lambda: datetime.fromisoformat(when))
        return YFinanceClient().get_dividend_schedule(symbol.split('.')[0], currency=currency,
                                                      start='2026-10-14', end='2026-10-15')
    early = get(before)
    # The market's 10/15 has not closed: the copy is clipped to 10/14, never extended.
    assert early['coverage'][0]['status'] == 'estimated' and early['coverage'][0]['end'] == '2026-10-14'
    calls = len(seen)
    late = get(after)
    assert len(seen) == 2 * calls
    assert late['coverage'][0]['status'] == 'estimated' and late['coverage'][0]['end'] == '2026-10-15'
    assert get(later)['coverage'][0]['status'] == 'estimated' and len(seen) == 2 * calls


def test_market_day_jobs_of_both_currencies_coexist_in_memory(monkeypatch):
    import yfinance_api

    seen = mock_history(monkeypatch, ['2026-03-20'], symbol='005930.KS', currency='KRW')
    krw = yfinance_api.yf.Ticker
    seen += mock_history(monkeypatch, ['2026-03-20'])
    usd = yfinance_api.yf.Ticker
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: usd(symbol) if symbol == 'SYN' else krw(symbol))
    # 03:00 UTC is 10/15 in New York and 10/16 in Seoul.
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: datetime(2026, 10, 16, 3, tzinfo=timezone.utc))
    for _ in range(2):
        for symbol, currency in (('SYN', 'USD'), ('005930', 'KRW')):
            data = YFinanceClient().get_dividend_schedule(symbol, currency=currency, start='2026-10-13', end='2026-10-14')
            assert data['coverage'][0]['status'] == 'estimated'
    assert sorted(key[0] for key in yfinance_api._DIVIDEND_CACHE) == ['2026-10-15', '2026-10-16']


def mock_live(monkeypatch, *, symbol='SYN', currency='USD', quote_type='ETF', calendar=None, extra=None,
              listed='1993-01-29', ex_dates=(), drop=lambda day: False, chart_rows=True):
    """Anonymised live shapes from a fetch diagnostic (2026-10-16).

    ETFs: quoteSummary carries the price module only. Charts carry
    meta.firstTradeDate and weekday bars from the listing date or lookback start.
    """
    clock = 'T13:30:00' if currency == 'USD' else 'T00:00:00'
    price = {'symbol': symbol, 'currency': currency, 'quoteType': quote_type}
    summary = {'price': price, **({} if calendar is None else {'calendarEvents': calendar}), **(extra or {})}
    stamps = [epoch(d + clock) for d in ex_dates]
    meta = {'symbol': symbol, 'currency': currency, 'instrumentType': quote_type}
    if listed:
        meta['firstTradeDate'] = epoch(listed + clock)
    def raw(url, params=None, timeout=None):
        if currency == 'KRW' and not url.endswith(symbol):
            error = RuntimeError('synthetic not found')
            error.response = SimpleNamespace(status_code=404, json=lambda: {'quoteSummary': {
                'result': None, 'error': {'code': 'Not Found', 'description': f'Quote not found for symbol: {url.rsplit("/", 1)[-1]}'}}})
            raise error
        if 'quoteSummary' in url:
            return {'quoteSummary': {'error': None, 'result': [deepcopy(summary)]}}
        if not chart_rows:
            # A live chart with no rows in range: meta only, empty indicators.
            return {'chart': {'error': None, 'result': [{'meta': dict(meta), 'indicators': {'quote': [{}]}}]}}
        row = bars({'chart': {'error': None, 'result': [{'meta': dict(meta), 'timestamp': [],
                   'indicators': {'quote': [{'close': []}]}}]}}, params, currency)['chart']['result'][0]
        row['timestamp'] = [t for t in row['timestamp'] if not drop(datetime.fromtimestamp(t, timezone.utc).date())
                            and (not listed or t >= meta['firstTradeDate'])]
        row['indicators']['quote'][0]['close'] = [10] * len(row['timestamp'])
        if stamps:
            row['events'] = {'dividends': {str(t): {'date': t, 'amount': 1.0} for t in stamps}}
        return {'chart': {'error': None, 'result': [row]}}
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))


@pytest.mark.parametrize(('symbol', 'currency', 'ex_dates'), [
    ('SYN', 'USD', ['2025-12-19', '2026-03-20', '2026-06-18', '2026-09-18']),
    ('0000Z0.KS', 'KRW', ['2026-07-30', '2026-08-28', '2026-09-29']),
])
def test_price_only_etf_summary_relies_on_chart_dividend_events(monkeypatch, symbol, currency, ex_dates):
    from toss_dividend_cache import load
    from toss_dividends import PAYMENT_POLICY

    mock_live(monkeypatch, symbol=symbol, currency=currency, ex_dates=ex_dates)
    canonical = symbol.split('.')[0]
    data = YFinanceClient().get_dividend_schedule(canonical, currency=currency, start='2026-10-10', end='2026-10-11')
    proof = data['coverage'][0]
    # No announced calendar: every chart event gets an estimated window, never complete.
    assert proof['status'] == 'estimated' and proof['listing_basis'] is None
    assert proof['price_history_start'] == proof['evidence_start']
    assert [r['ex_date'] for r in data['rows']] == (ex_dates[-1:] if currency == 'USD' else ex_dates)
    assert all(r['pay_date'] is None and r['payment_basis'] == PAYMENT_POLICY for r in data['rows'])
    assert load(canonical, currency, datetime(2026, 10, 13, 12, tzinfo=timezone.utc))['failure_reason'] is None


@pytest.mark.parametrize(('quote_type', 'extra'), [('EQUITY', None), ('ETF', {'summaryDetail': {}})])
def test_only_a_price_only_etf_summary_may_omit_the_calendar(monkeypatch, quote_type, extra):
    mock_live(monkeypatch, quote_type=quote_type, extra=extra)
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert data['coverage'][0]['reasons'][0] == 'DIVIDEND_CALENDAR_MISSING'


def test_dual_probe_failure_keeps_the_answering_suffix_and_its_reason(monkeypatch):
    mock_live(monkeypatch, symbol='0000Z0.KQ', currency='KRW', chart_rows=False)
    data = YFinanceClient().get_dividend_schedule('0000Z0', currency='KRW', start='2026-10-10', end='2026-10-11')
    proof = data['coverage'][0]
    assert data['rows'] == [] and (proof['status'], proof['reasons'][0]) == ('limited', 'DIVIDEND_PRICE_HISTORY_MISSING')
    assert (proof['symbol'], proof['reference_symbol']) == ('0000Z0', '0000Z0.KQ')


@pytest.mark.parametrize(('symbol', 'currency', 'listed'), [('SYN', 'USD', '2026-01-05'), ('0000Z0.KS', 'KRW', '2025-10-01')])
def test_recent_listing_is_proven_by_chart_first_trade_date(monkeypatch, symbol, currency, listed):
    from toss_dividend_cache import load
    from toss_dividends import LISTING_BASIS

    mock_live(monkeypatch, symbol=symbol, currency=currency, listed=listed)
    canonical = symbol.split('.')[0]
    data = YFinanceClient().get_dividend_schedule(canonical, currency=currency, start='2026-10-10', end='2026-10-11')
    proof = data['coverage'][0]
    assert data['rows'] == [] and proof['status'] == 'estimated'
    assert (proof['price_history_start'], proof['listing_basis']) == (listed, LISTING_BASIS)
    assert load(canonical, currency, datetime(2026, 10, 13, 12, tzinfo=timezone.utc))['good'] is not None


@pytest.mark.parametrize(('case', 'reason'), [
    ('bars-before-listing', 'DIVIDEND_LISTING_DATE_CONFLICT'),
    ('event-before-listing', 'DIVIDEND_LISTING_DATE_CONFLICT'),
    ('late-bars-without-listing', 'DIVIDEND_PRICE_HISTORY_GAP'),
    ('late-bars-after-listing', 'DIVIDEND_PRICE_HISTORY_GAP'),
])
def test_listing_date_contradictions_fail_closed(monkeypatch, case, reason):
    import yfinance_api

    listed = None if case == 'late-bars-without-listing' else '2026-01-05'
    late = {'late-bars-without-listing', 'late-bars-after-listing'}
    mock_live(monkeypatch, listed=listed, ex_dates=['2025-12-19'] if case == 'event-before-listing' else (),
              drop=lambda day: case in late and day < datetime(2026, 2, 2).date())
    if case == 'bars-before-listing':
        inner = yfinance_api.yf.Ticker
        def shifted(symbol):
            raw = inner(symbol)._data.get_raw_json
            def moved(url, **kwargs):
                payload = raw(url, **kwargs)
                if 'chart' in payload:
                    payload['chart']['result'][0]['meta']['firstTradeDate'] += 7 * 86400
                return payload
            return SimpleNamespace(_data=SimpleNamespace(get_raw_json=moved))
        monkeypatch.setattr('yfinance_api.yf.Ticker', shifted)
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert data['coverage'][0]['reasons'][0] == reason


@pytest.mark.parametrize('case', ['holding', 'fill', 'ex-date', 'consistent'])
def test_local_evidence_before_the_listing_voids_the_listing_exception(monkeypatch, case):
    """Yahoo lists SYN on 10/6 with no dividends, yet it was held earlier."""
    from test_toss_flow_inference import coverage, fill, snapshot, usd

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    mock_live(monkeypatch, listed='2026-10-06')
    YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-01', end='2026-10-12')  # warm cache
    days = ['2026-09-30', '2026-10-03', '2026-10-04'] if case == 'holding' else ['2026-10-06', '2026-10-07', '2026-10-08']
    snaps = [snapshot(days[0] + 'T09:10:00+00:00'), snapshot(days[1] + 'T09:10:00+00:00'),
             snapshot(days[2] + 'T09:10:00+00:00', usd='108.5')]
    # Any account's fill counts, even one outside the selected intervals.
    fills = [fill(account='toss:secondary', execution_time_utc='2026-10-02T14:00:00+00:00')] if case == 'fill' else []
    schedule = dividend_schedule_provider(snaps, snaps[1]['time'], snaps[2]['time'], fills=fills)
    proof = schedule['coverage'][0]
    if case == 'ex-date':
        # An injected schedule is reconciled by infer_toss_flows itself.
        schedule['rows'].append({'id': 'supplied', 'symbol': 'SYN', 'currency': 'USD', 'market': 'US',
                                 'ex_date': '2026-10-01', 'pay_date': '2026-10-30', 'gross_per_share': '0.85'})
    windows = [{**coverage()[0], 'start': '2026-09-01T00:00:00+00:00'}]
    row = usd(infer_toss_flows(snaps, fills, schedule, coverage=windows, start=snaps[1]['time']))
    if case in ('consistent', 'ex-date'):
        assert proof['status'] == 'estimated' and proof['price_history_start'] == '2026-10-06'
    if case == 'consistent':
        assert row['confidence'] == 'conditional' and row['flow_native'] == '8.5'
        return
    if case != 'ex-date':
        assert schedule['rows'] == [] and proof['status'] == 'limited'
        assert proof['reasons'][0] == 'DIVIDEND_LISTING_DATE_CONFLICT'
        assert proof['local_evidence_start'] == ('2026-09-30' if case == 'holding' else '2026-10-02')
    # The cash increase is neither a deposit nor dividend income: it stays unresolved.
    assert row['confidence'] == 'unresolved' and row['flow_native'] is None and row['income_native'] is None
    assert 'DIVIDEND_COVERAGE_INCOMPLETE' in row['reasons']


def nav_version(when, accounts):
    """One complete NAV payload; accounts maps account -> (USD cash, SYN quantity or None)."""
    from daily_nav import TOSS_CASH_BASIS

    stamp = when.isoformat()
    return {'snapshot_date': stamp[:10], 'snapshot_id': stamp, 'version': 1, 'valuation_at': stamp,
            'mark_quality': {'quotes': []},
            'account_nav': {account: {'source_status': 'complete',
                'nav_native': {c: {'value': 100, 'reason_codes': []} for c in ('KRW', 'USD')},
                'cash': {c: {'value_decimal': cash if c == 'USD' else '2000000', 'basis_reason': TOSS_CASH_BASIS,
                             'observed_at': stamp} for c in ('KRW', 'USD')},
                'cash_basis_evidence': {'open_order_count': 0}} for account, (cash, _) in accounts.items()},
            'components': [{'account': account, 'provider': 'toss', 'component_type': 'holding', 'symbol': 'SYN',
                            'currency': 'USD', 'quantity_decimal': qty, 'valuation_price_decimal': '10',
                            'priced_at': stamp} for account, (_, qty) in accounts.items() if qty],
            'fx_times': {'USD': {'rate_decimal': '1400', 'rate_to_krw': 1400, 'priced_at': stamp, 'read_at': stamp,
                                 'valid_until': (when + timedelta(minutes=1)).isoformat()}}}


@pytest.mark.parametrize('case', ['holding', 'fill', 'consistent'])
def test_primary_only_reader_sees_other_account_evidence_before_the_listing(tmp_path, monkeypatch, case):
    """A secondary account held SYN before its 10/6 listing; the query selects only primary."""
    from portfolio_ledger import LedgerStore
    from portfolio_store import NavStore
    from toss_flow_inference import dividend_schedule_provider, toss_interval_flows

    mock_live(monkeypatch, listed='2026-10-06')
    YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-01', end='2026-10-12')  # warm cache
    primary, secondary = 'toss:primary', 'toss:secondary'
    at = lambda day: datetime.fromisoformat(day + 'T09:10:00+00:00')  # noqa: E731
    other = (secondary, '50', '10' if case != 'fill' else None)
    navs = [nav_version(at('2026-10-06' if case == 'consistent' else '2026-09-30'), {other[0]: other[1:]}),
            nav_version(at('2026-10-07'), {primary: ('100', '10')}),
            nav_version(at('2026-10-08'), {primary: ('108.5', '10')})]
    path = tmp_path / 'listing.db'
    store = LedgerStore(path)
    if case == 'fill':
        with store.transaction() as conn:
            store.append(conn, {'source': 'toss_transactions', 'account_identity': store.account_identity(secondary),
                'source_record_id': 'secondary-buy', 'kind': 'fill', 'symbol': 'SYN', 'currency': 'USD',
                'side': 'buy', 'quantity': '1', 'price_native': '10', 'gross_amount': '10', 'fee': '0', 'tax': '0',
                'execution_time_utc': '2026-10-02T14:00:00+00:00', 'source_status': 'FILLED'})
    with NavStore(path)._connect() as conn:
        for payload in navs:
            NavStore._insert_version(conn, payload)
    store.record_coverage('toss', store.account_identity(primary), 'toss/fills-v1/synthetic',
                          '2026-09-01T00:00:00+00:00', '2026-10-12T00:00:00+00:00', 'complete', [], 0, 'synthetic')
    rows = toss_interval_flows(path, '2026-10-08', '2026-10-08', [primary],
                               schedule_provider=dividend_schedule_provider)
    row = next(r for r in rows if r['currency'] == 'USD')
    assert {r['account'] for r in rows} == {primary}
    if case == 'consistent':
        assert row['confidence'] == 'conditional' and row['flow_native'] == '8.5'
        return
    assert row['confidence'] == 'unresolved' and row['flow_native'] is None and row['income_native'] is None
    assert 'DIVIDEND_COVERAGE_INCOMPLETE' in row['reasons']


@pytest.mark.parametrize(('symbol', 'currency', 'last_bar', 'gap', 'ok'), [
    ('SYN', 'USD', '2026-05-22', 5, True), ('SYN', 'USD', '2026-05-22', 6, False),
    # Chuseok 2025 left an 8-day live KR gap; 11 days covers a 2017-length closure.
    ('005930.KS', 'KRW', '2025-10-02', 11, True), ('005930.KS', 'KRW', '2025-10-02', 12, False),
])
def test_equity_zero_dividend_needs_bars_without_abnormal_gaps(monkeypatch, symbol, currency, last_bar, gap, ok):
    first = datetime.fromisoformat(last_bar).date()
    mock_live(monkeypatch, symbol=symbol, currency=currency, quote_type='EQUITY', calendar={},
              drop=lambda day: first < day < first + timedelta(days=gap))
    data = YFinanceClient().get_dividend_schedule(symbol.split('.')[0], currency=currency,
                                                  start='2026-10-10', end='2026-10-11')
    assert data['rows'] == []
    if ok:
        assert data['coverage'][0]['status'] == 'estimated'
    else:
        assert data['coverage'][0]['status'] == 'limited'
        assert data['coverage'][0]['reasons'][0] == 'DIVIDEND_PRICE_HISTORY_GAP'


def test_equity_zero_dividend_with_truncated_recent_bars_fails_closed(monkeypatch):
    mock_live(monkeypatch, quote_type='EQUITY', calendar={}, drop=lambda day: day > datetime(2026, 10, 5).date())
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    assert data['coverage'][0]['status'] == 'limited'
    assert data['coverage'][0]['reasons'][0] == 'DIVIDEND_PRICE_HISTORY_GAP'


@pytest.mark.parametrize('shape', ['no-timestamp', 'empty-timestamp'])
def test_chart_without_bars_stays_unresolved_and_cache_keeps_the_reason(monkeypatch, shape):
    import yfinance_api
    from toss_dividend_cache import load

    mock_live(monkeypatch, chart_rows=shape != 'no-timestamp', drop=lambda day: shape == 'empty-timestamp')
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert data['coverage'][0]['reasons'][0] == 'DIVIDEND_PRICE_HISTORY_MISSING'
    record = load('SYN', 'USD', datetime(2026, 10, 13, 12, tzinfo=timezone.utc))
    assert record['good'] is None and record['failure_reason'] == 'DIVIDEND_PRICE_HISTORY_MISSING'
    with yfinance_api._DIVIDEND_CACHE_LOCK:
        yfinance_api._DIVIDEND_CACHE.clear()
    # Another process the same market day reuses the specific failure.
    again = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    assert again['coverage'][0]['reasons'] == ['DIVIDEND_PRICE_HISTORY_MISSING']


@pytest.mark.parametrize('tamper', ['pre-v12-copy', 'unproven-listing', 'unknown-basis'])
def test_cached_copy_without_a_proven_bar_span_is_not_evidence(monkeypatch, tamper):
    import json

    from toss_dividend_cache import load, path_for
    from toss_dividends import LISTING_BASIS

    mock_history(monkeypatch, ['2026-03-20'])
    YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-10', end='2026-10-11')
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    assert load('SYN', 'USD', now) is not None
    path = path_for('SYN', 'USD')
    record = json.loads(path.read_text())
    proof = record['good']['schedule']['coverage'][0]
    if tamper == 'pre-v12-copy':
        del proof['price_history_start'], proof['listing_basis']
    elif tamper == 'unproven-listing':
        proof['listing_basis'] = LISTING_BASIS  # A listing must postdate the lookback start.
    else:
        proof['listing_basis'] = 'other'
    path.write_text(json.dumps(record))
    assert load('SYN', 'USD', now) is None


def test_request_past_the_cached_day_clips_coverage_per_interval(monkeypatch):
    # Live 10/17: the tool asks up to today; the copy ends at the market's last closed day.
    from test_toss_flow_inference import coverage, snapshot

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows

    mock_history(monkeypatch, ['2026-03-20'])
    # 16:00 UTC is New York's 10/16 midday: the copy ends 10/15.
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: datetime(2026, 10, 16, 16, tzinfo=timezone.utc))
    data = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-14', end='2026-10-17')
    assert data['coverage'][0]['status'] == 'estimated' and data['coverage'][0]['end'] == '2026-10-15'
    # A request starting after the copy's end proves nothing.
    late = YFinanceClient().get_dividend_schedule('SYN', currency='USD', start='2026-10-16', end='2026-10-17')
    assert late['coverage'][0]['reasons'] == ['DIVIDEND_WINDOW_OUTSIDE_DAILY_CACHE']

    snaps = [snapshot(f'2026-10-{d}T09:10:00+00:00') for d in (14, 15, 16)]
    schedule = dividend_schedule_provider(snaps, snaps[0]['time'], '2026-10-17')
    windows = [{**coverage()[0], 'start': '2025-01-01T00:00:00+00:00', 'end': '2027-01-01T00:00:00+00:00'}]
    values = [r for r in infer_toss_flows(snaps, [], schedule, coverage=windows) if r['currency'] == 'USD']
    by_end = {r['t1'][:10]: r for r in values}
    assert 'DIVIDEND_COVERAGE_INCOMPLETE' not in by_end['2026-10-15']['reasons']
    assert by_end['2026-10-15']['confidence'] == 'conditional'
    assert 'DIVIDEND_COVERAGE_INCOMPLETE' in by_end['2026-10-16']['reasons']
    assert by_end['2026-10-16']['flow_native'] is None
