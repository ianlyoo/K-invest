"""Published instrument shapes, real mocked clients, and persistent public cache."""
import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import perf_counter
from types import SimpleNamespace

import httpx
import pytest
from test_toss_dividends import bars

from toss_api import TossClient
from toss_dividends import instrument_reference
from toss_nav_validation import validate_toss_response
from yfinance_api import YFinanceClient

FIXTURE = json.loads(Path(__file__).with_name('fixtures').joinpath('toss_stock_info_openapi_1_2_24.json').read_text())
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    from yfinance_api import _DIVIDEND_CACHE
    _DIVIDEND_CACHE.clear()
    monkeypatch.setenv('KINVEST_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: NOW)
    yield
    for job in _DIVIDEND_CACHE.values():
        job.result(timeout=10)
    _DIVIDEND_CACHE.clear()


@pytest.fixture
def stock_client(monkeypatch):
    from provider_pacing import RequestBudget
    monkeypatch.setattr('toss_api._RATE_BUDGETS', {'STOCK': RequestBudget(0)})
    state = SimpleNamespace(payload=deepcopy(FIXTURE['examples']['krxStock']['value']), calls=[])
    def handler(request):
        assert request.method == 'GET' and request.url.path == '/api/v1/stocks'
        state.calls.append(dict(request.url.params))
        return httpx.Response(200, json=state.payload)
    obj = object.__new__(TossClient)
    obj._token, obj._token_expires, obj._account_seq = 'synthetic', float('inf'), 1
    with httpx.Client(base_url='https://mock-toss.invalid', transport=httpx.MockTransport(handler), trust_env=False) as http:
        obj._http = http
        state.api = obj
        yield state


def binding():
    row = deepcopy(FIXTURE['examples']['krxStock']['value']['result'][0])
    return {**row, 'basis': 'toss-stock-info-v1', 'observed_at': NOW.isoformat()}


def yahoo(monkeypatch, *, valid=('005930.KS',), currency='KRW', names=None, isin=None, fail=False):
    calls = []
    def raw(url, **kwargs):
        calls.append(url)
        if fail:
            raise TimeoutError('synthetic refresh outage')
        symbol = url.rsplit('/', 1)[-1]
        if symbol not in valid:
            raise httpx.HTTPStatusError('synthetic missing candidate', request=httpx.Request('GET', url),
                response=httpx.Response(404, json={'quoteSummary': {'result': None, 'error': {
                    'code': 'Not Found', 'description': f'Quote not found for symbol: {symbol}'}}}))
        meta = {'symbol': symbol, 'currency': currency}
        if names:
            meta['longName'] = names
        if isin:
            meta['isin'] = isin
        if 'quoteSummary' in url:
            return {'quoteSummary': {'error': None, 'result': [{'price': meta, 'calendarEvents': {}}]}}
        return bars({'chart': {'error': None, 'result': [{'meta': meta, 'timestamp': [],
            'indicators': {'quote': [{'close': []}]}, 'events': {}}]}}, kwargs['params'], currency)
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=raw)))
    return calls


def get(**kwargs):
    return YFinanceClient().get_dividend_schedule('005930', currency='KRW', start='2026-10-10', end='2026-10-11', **kwargs)


@pytest.mark.parametrize('example', ['krxStock', 'etf'])
def test_published_instrument_response_binds_existing_client_route(stock_client, example):
    stock_client.payload = deepcopy(FIXTURE['examples'][example]['value'])
    symbol = stock_client.payload['result'][0]['symbol']
    payload = stock_client.api.get_stock_info_evidence([symbol])
    valid = validate_toss_response('stock_info', payload, symbols={symbol: 'KRW'})
    assert valid.value is not None and not valid.reason_codes
    assert instrument_reference(symbol, {**valid.value[0], 'basis': 'toss-stock-info-v1'}) == symbol + '.KS'
    assert stock_client.calls == [{'symbols': symbol}]


@pytest.mark.parametrize('bad', ['duplicate', 'missing', 'wrong-symbol', 'currency', 'market', 'isin', 'truncated', 'error'])
def test_invalid_instrument_envelope_never_produces_binding(stock_client, bad):
    payload = stock_client.payload
    row = payload['result'][0]
    if bad == 'duplicate':
        payload['result'].append(deepcopy(row))
    elif bad == 'missing':
        del row['market']
    elif bad == 'wrong-symbol':
        row['symbol'] = '000000'
    elif bad == 'currency':
        row['currency'] = 'USD'
    elif bad == 'market':
        row['market'] = 'NXT'
    elif bad == 'isin':
        row['isinCode'] = 'unknown'
    elif bad == 'truncated':
        payload['hasNext'] = True
    else:
        payload['error'] = {'code': 'partial'}
    data = stock_client.api.get_stock_info_evidence(['005930'])
    valid = validate_toss_response('stock_info', data, symbols={'005930': 'KRW'})
    assert valid.value is None and valid.reason_codes


@pytest.mark.parametrize(('market', 'suffix'), [('KOSPI', 'KS'), ('KOSDAQ', 'KQ')])
def test_verified_kr_market_uses_only_bound_ticker_and_resolves_both_flows(monkeypatch, market, suffix):
    from test_toss_flow_inference import T0, T1, coverage, snapshot

    from toss_flow_inference import dividend_schedule_provider, infer_toss_flows
    instrument = {**binding(), 'market': market}
    calls = yahoo(monkeypatch, valid=('005930.' + suffix,))
    data = get(instrument=instrument)
    assert data['coverage'][0]['status'] == 'estimated' and len(calls) == 2
    snaps = [snapshot(), snapshot(T1)]
    for snap in snaps:
        snap['holdings'][0].update(symbol='005930', currency='KRW', name='삼성전자')
        snap['instrument_bindings'] = {'005930': instrument}
    schedule = dividend_schedule_provider(snaps, T0, T1)
    rows = infer_toss_flows(snaps, [], schedule, coverage=coverage())
    assert all(r['flow_native'] == '0' and r['confidence'] == 'conditional' for r in rows)
    krw = next(r for r in rows if r['currency'] == 'KRW')
    assert krw['inputs']['dividend_reference_dates'][0]['observed_at'] == NOW.isoformat()
    assert len(calls) == 2


def test_snapshot_retains_instrument_binding_separately_from_nav(tmp_path, monkeypatch):
    from test_toss_nav import capture

    from toss_flow_inference import _account_snapshot
    row = {'symbol': '005930', 'currency': 'KRW', 'quantity': '2', 'lastPrice': '10',
           'marketValue': {'amount': '20'}}
    captured = capture(tmp_path, monkeypatch, holding_currency='KRW', primary_holdings=[row],
        quote_symbol='005930', quote_currency='KRW', instrument_payload=FIXTURE['examples']['krxStock']['value'])
    snap = _account_snapshot(captured.row, 'toss:primary')
    assert snap['instrument_bindings']['005930']['market'] == 'KOSPI'
    assert snap['instrument_bindings']['005930']['isinCode'] == 'KR7005930003'
    assert snap['instrument_binding_reasons'] == []


@pytest.mark.parametrize('bad', ['both-valid', 'wrong-currency', 'name', 'isin', 'outage'])
def test_dual_candidate_fallback_requires_unique_valid_kr_identity(monkeypatch, bad):
    options = {'valid': ('005930.KS', '005930.KQ')} if bad == 'both-valid' else {}
    if bad == 'wrong-currency':
        options['currency'] = 'USD'
    if bad == 'name':
        options['names'] = 'A different issuer'
    if bad == 'isin':
        options['isin'] = 'KR7000000000'
    if bad == 'outage':
        options['fail'] = True
    yahoo(monkeypatch, **options)
    data = get(instrument={'name': '삼성전자', 'isinCode': 'KR7005930003'})
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'


def test_legacy_fallback_cross_checks_available_names(monkeypatch):
    calls = yahoo(monkeypatch, names='삼성전자', isin='KR7005930003')
    data = get(instrument={'name': '삼성전자', 'isinCode': 'KR7005930003'})
    assert data['coverage'][0]['status'] == 'estimated' and len(calls) == 3
    assert data['binding']['basis'] == 'unique-validated-kr-schedule'


@pytest.mark.parametrize('field', ['market', 'isinCode'])
def test_cached_instrument_cannot_bypass_new_conflicting_identity(monkeypatch, field):
    calls = yahoo(monkeypatch)
    assert get(instrument=binding())['coverage'][0]['status'] == 'estimated'
    changed = {**binding(), field: 'KOSDAQ' if field == 'market' else 'KR7000000000'}
    data = get(instrument=changed)
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert len(calls) == 2


def test_same_day_memory_restart_and_separate_process_use_persisted_copy(monkeypatch, tmp_path):
    from yfinance_api import _DIVIDEND_CACHE
    calls = yahoo(monkeypatch)
    original = get(instrument=binding())
    _DIVIDEND_CACHE.clear()
    assert get(instrument=binding(), background=True) == original and len(calls) == 2
    code = '''
import json
from datetime import datetime, timezone
import yfinance_api as api
api._dividend_now = lambda: datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
def forbidden(*args, **kwargs):
    raise AssertionError('A persisted daily schedule must not fetch')
api.yf.Ticker = forbidden
print(json.dumps(api.YFinanceClient().get_dividend_schedule('005930', currency='KRW',
    start='2026-10-10', end='2026-10-11', background=True)))
'''
    child = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
        env={'KINVEST_CACHE_DIR': str(tmp_path / 'cache')}, capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(child.stdout) == original


def test_stale_refresh_failure_keeps_original_date_and_cannot_extend_coverage(monkeypatch):
    from toss_dividend_cache import load
    from yfinance_api import _DIVIDEND_CACHE
    yahoo(monkeypatch)
    original = get(instrument=binding())
    before = load('005930', 'KRW', NOW)['good']
    later = NOW + timedelta(days=2)
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: later)
    calls = yahoo(monkeypatch, fail=True)
    data = get(instrument=binding())
    assert data['coverage'][0]['status'] == 'estimated'
    assert data['coverage'][0]['observed_at'] == original['coverage'][0]['observed_at']
    assert data['coverage'][0]['cache_status'] == 'refresh_failed'
    assert load('005930', 'KRW', later)['good'] == before
    _DIVIDEND_CACHE.clear()
    get(instrument=binding())
    future = YFinanceClient().get_dividend_schedule('005930', currency='KRW', start='2026-10-14', end='2026-10-15')
    assert future['coverage'][0]['status'] == 'limited' and len(calls) == 1
    assert future['cache_evidence']['observed_at'] == NOW.isoformat()


def test_stale_copy_is_available_while_refresh_is_blocked(monkeypatch):
    from yfinance_api import _DIVIDEND_CACHE
    yahoo(monkeypatch)
    get(instrument=binding())
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: NOW + timedelta(days=2))
    entered, release = Event(), Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise TimeoutError('synthetic outage')
    monkeypatch.setattr('yfinance_api.yf.Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=blocked)))
    try:
        began = perf_counter()
        data = get(instrument=binding(), background=True)
        assert perf_counter() - began < .5
        assert data['coverage'][0]['status'] == 'estimated' and data['coverage'][0]['cache_status'] == 'refresh_pending'
        assert data['coverage'][0]['observed_at'] == NOW.isoformat() and entered.wait(2)
    finally:
        release.set()
        for job in _DIVIDEND_CACHE.values():
            job.result(timeout=10)


def test_failed_refresh_with_unwritable_cache_keeps_old_copy(monkeypatch):
    import toss_dividend_cache
    yahoo(monkeypatch)
    get(instrument=binding())
    monkeypatch.setattr('yfinance_api._dividend_now', lambda: NOW + timedelta(days=2))
    yahoo(monkeypatch, fail=True)
    def denied(*args, **kwargs):
        raise PermissionError('synthetic cache read-only')
    monkeypatch.setattr(toss_dividend_cache.os, 'open', denied)
    data = get(instrument=binding())
    assert data['coverage'][0]['status'] == 'estimated' and data['coverage'][0]['observed_at'] == NOW.isoformat()
    assert data['coverage'][0]['cache_status'] == 'refresh_failed'


@pytest.mark.parametrize('damage', ['truncated', 'binding', 'coverage-date', 'coverage-start', 'version', 'old-policy', 'complete'])
def test_corrupt_persistent_schedule_is_not_numeric_evidence(monkeypatch, damage):
    from toss_dividend_cache import path_for
    from yfinance_api import _DIVIDEND_CACHE
    yahoo(monkeypatch)
    get(instrument=binding())
    path = path_for('005930', 'KRW')
    record = json.loads(path.read_text())
    if damage == 'binding':
        record['good']['schedule']['coverage'][0]['reference_symbol'] = 'OTHER.KS'
    elif damage == 'coverage-date':
        record['good']['schedule']['coverage'][0]['end'] = '2099-01-01'
    elif damage == 'coverage-start':
        record['good']['schedule']['coverage'][0]['start'] = '2000-01-01'
    elif damage == 'version':
        record['version'] = -1
    elif damage == 'old-policy':
        record['policy'] = 'market-payment-window-2026-10-13'
    elif damage == 'complete':
        record['good']['schedule']['coverage'][0].update(status='complete', reasons=[])
    path.write_text('{' if damage == 'truncated' else json.dumps(record))
    _DIVIDEND_CACHE.clear()
    yahoo(monkeypatch, fail=True)
    data = get(instrument=binding())
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'


@pytest.mark.parametrize('bad', ['error-envelope', 'currency', 'chart', 'timeout', 'bare-404',
                               'wrong-symbol-404', 'partial-not-found', 'server-error'])
@pytest.mark.parametrize('symbol', ['005930', '005930.KS'])
def test_invalid_alternative_never_establishes_kr_binding(monkeypatch, bad, symbol):
    yahoo(monkeypatch, valid=('005930.KS', '005930.KQ'))
    import yfinance_api
    raw = yfinance_api.yf.Ticker('005930')._data.get_raw_json
    def mixed(url, **kwargs):
        data = raw(url, **kwargs)
        if url.endswith('.KQ'):
            if bad == 'timeout':
                raise TimeoutError('synthetic alternative outage')
            if bad in ('bare-404', 'wrong-symbol-404', 'server-error'):
                body = {'quoteSummary': {'result': None, 'error': {
                    'code': 'Not Found', 'description': 'Quote not found for symbol: ' +
                    ('OTHER.KQ' if bad == 'wrong-symbol-404' else '005930.KQ')}}}
                response = httpx.Response(404, text='not found') if bad == 'bare-404' else httpx.Response(
                    500 if bad == 'server-error' else 404, json=body)
                raise httpx.HTTPStatusError('synthetic failure', request=httpx.Request('GET', url), response=response)
            if 'quoteSummary' in url:
                if bad == 'currency':
                    data['quoteSummary']['result'][0]['price']['currency'] = 'USD'
                elif bad == 'error-envelope':
                    data['quoteSummary'] = {'result': None, 'error': {'code': 'Unavailable', 'description': 'try later'}}
                elif bad == 'partial-not-found':
                    data = {'quoteSummary': {'result': None, 'error': {'code': 'Not Found',
                        'description': 'Quote not found for symbol: 005930.KQ'}}, 'hasNext': True}
            elif bad == 'chart':
                data['chart']['result'][0]['indicators'] = {}
        return data
    monkeypatch.setattr(yfinance_api.yf, 'Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=mixed)))
    data = YFinanceClient().get_dividend_schedule(symbol, currency='KRW', start='2026-10-10', end='2026-10-11')
    assert data['rows'] == [] and data['coverage'][0]['status'] == 'limited'
    assert 'DIVIDEND_INSTRUMENT_UNRESOLVED' in data['coverage'][0]['reasons']


@pytest.mark.parametrize('endpoint', ['quoteSummary', 'chart'])
@pytest.mark.parametrize('http_error', [False, True])
def test_only_explicit_documented_absence_accepts_the_unique_candidate(monkeypatch, endpoint, http_error):
    import yfinance_api
    yahoo(monkeypatch, valid=('005930.KS', '005930.KQ'))
    raw = yfinance_api.yf.Ticker('005930')._data.get_raw_json
    def mixed(url, **kwargs):
        if url.endswith('.KQ') and endpoint in url:
            body = {endpoint: {'result': None, 'error': {'code': 'Not Found', 'description':
                'Quote not found for symbol: 005930.KQ' if endpoint == 'quoteSummary' else
                'No data found, symbol may be delisted'}}}
            if http_error:
                raise httpx.HTTPStatusError('synthetic explicit absence', request=httpx.Request('GET', url),
                                           response=httpx.Response(404, json=body))
            return body
        return raw(url, **kwargs)
    monkeypatch.setattr(yfinance_api.yf, 'Ticker', lambda symbol: SimpleNamespace(_data=SimpleNamespace(get_raw_json=mixed)))
    data = get()
    assert data['coverage'][0]['status'] == 'estimated'
    assert data['binding']['reference_symbol'] == '005930.KS'


@pytest.mark.parametrize('advance', [timedelta(seconds=2), timedelta(days=1)])
def test_queued_older_job_rechecks_time_after_newer_process_commits(monkeypatch, advance):
    from concurrent.futures import Future

    import yfinance_api
    from toss_dividend_cache import load, path_for, refresh

    calls = yahoo(monkeypatch)
    time = [NOW]
    monkeypatch.setattr(yfinance_api, '_dividend_now', lambda: time[0])
    pending = []
    def submit(function, *args, **kwargs):
        future = Future()
        pending.append((future, function, args, kwargs))
        return future
    monkeypatch.setattr(yfinance_api, '_DIVIDEND_REFRESH', SimpleNamespace(submit=submit))
    assert get(background=True)['coverage'][0]['status'] == 'limited'
    # This writer commits after the old job queues, but before it gets the lock.
    time[0] += advance
    client = YFinanceClient()
    refresh('005930', 'KRW', lambda observed: client._fetch_daily_dividends('005930', 'KRW', observed, binding()),
            clock=lambda: time[0])
    before = path_for('005930', 'KRW').read_bytes()
    for future, function, args, kwargs in pending:
        future.set_result(function(*args, **kwargs))
    assert path_for('005930', 'KRW').read_bytes() == before
    assert len(calls) == 2
    record = load('005930', 'KRW', time[0])
    assert record['good']['observed_at'] == time[0].isoformat()
    assert record['last_attempt_at'] == time[0].isoformat()


@pytest.mark.parametrize('failed', [False, True])
def test_commit_merge_cannot_erase_a_newer_good_copy(monkeypatch, failed):
    from toss_dividend_cache import _save, load, path_for, refresh

    yahoo(monkeypatch)
    get(instrument=binding())
    original = load('005930', 'KRW', NOW)
    newer = deepcopy(original)
    later = NOW + timedelta(days=1)
    newer['last_attempt_at'] = newer['good']['observed_at'] = (later + timedelta(seconds=1)).isoformat()
    def out_of_order(observed):
        assert observed == later
        # Defensive commit merge: emulate a newer committed writer appearing
        # during old work, even if it did not cooperate with this file lock.
        _save(path_for('005930', 'KRW'), newer)
        if failed:
            raise TimeoutError('synthetic older refresh failure')
        return original['good']['schedule']
    data = refresh('005930', 'KRW', out_of_order, clock=lambda: later)
    assert data['coverage'][0]['observed_at'] == newer['good']['observed_at']
    assert load('005930', 'KRW', later + timedelta(seconds=2)) == newer


def test_clock_rollback_preserves_newer_committed_evidence(monkeypatch):
    from toss_dividend_cache import load, refresh

    yahoo(monkeypatch)
    get(instrument=binding())
    before = load('005930', 'KRW', NOW)
    def forbidden(observed):
        raise AssertionError('An older clock must not refresh or erase newer evidence')
    refresh('005930', 'KRW', forbidden, clock=lambda: NOW - timedelta(seconds=1))
    assert load('005930', 'KRW', NOW) == before
