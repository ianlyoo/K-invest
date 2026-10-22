"""Real TossClient HTTP path, synthetic candles in the documented 1.2.24 shape.

https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
No provider credentials or account data are used.
"""
import asyncio
import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import market_series as ms
import server
from toss_api import TossClient

NOW = datetime(2026, 10, 18, 0, tzinfo=timezone.utc)


def candle(day, symbol='SPY', close='100'):
    kr = symbol.isdigit()
    stamp = datetime.combine(date.fromisoformat(str(day)), datetime.min.time(), ZoneInfo('Asia/Seoul' if kr else 'America/New_York'))
    return {'timestamp': stamp.isoformat(), 'openPrice': close, 'highPrice': close,
            'lowPrice': close, 'closePrice': close, 'volume': '1000', 'currency': 'KRW' if kr else 'USD'}


def client_for(monkeypatch, rows, mutate=None):
    """Only HTTP/auth are faked; _get, rate pacing, get_candles and validator run."""
    calls = []
    client = object.__new__(TossClient)
    client._token_cache_path = Path('/unused-synthetic-token-cache')
    client._ensure_token = lambda: ''

    def respond(request):
        q = request.url.params
        calls.append((request.url.path, dict(q)))
        if request.url.path == '/api/v1/stocks':
            return httpx.Response(200, json={'result': [{'symbol': s, 'name': f'{s} fixture'} for s in q['symbols'].split(',')]})
        assert request.url.path == '/api/v1/candles'
        assert q['interval'] == '1d' and q['adjusted'] == 'true' and q['count'] == '200'
        symbol = q['symbol']
        if symbol not in rows:
            return httpx.Response(404, json={'error': {'code': 'not-found', 'message': 'fixture'}})
        cutoff = datetime.fromisoformat(q['before'])
        available = sorted((r for r in rows[symbol] if datetime.fromisoformat(r['timestamp']) <= cutoff), key=lambda r: r['timestamp'], reverse=True)
        page = available[:200]
        # Documented inclusive cursor includes the last candle again next page.
        payload = {'result': {'candles': deepcopy(page), 'nextBefore': page[-1]['timestamp'] if len(available) > 200 else None}}
        if mutate:
            mutate(payload)
        return httpx.Response(200, json=payload)

    client._http = httpx.Client(base_url='https://fixture.invalid', transport=httpx.MockTransport(respond))
    monkeypatch.setattr(server, '_get_toss', lambda: client)
    return client, calls


def run(client, symbols, **kwargs):
    return ms.build_market_series(client, symbols, start_date='2026-10-01', end_date='2026-10-16', now=NOW, **kwargs)


def test_mixed_calendar_alias_and_native_returns(monkeypatch):
    client, calls = client_for(monkeypatch, {
        '005930': [candle(d, '005930', p) for d, p in [('2026-10-01', '100'), ('2026-10-02', '105'), ('2026-10-05', '110')]],
        'ACME': [candle(d, 'ACME', p) for d, p in [('2026-10-01', '200'), ('2026-10-05', '210'), ('2026-10-06', '300')]],
        '069500': [candle(d, '069500', p) for d, p in [('2026-10-01', '100'), ('2026-10-05', '102')]],
    })
    data = run(client, ['005930', 'ACME'], benchmark='KOSPI')
    assert data['base_date'] == '2026-10-01' and data['as_of'] == '2026-10-05'
    assert data['benchmark']['proxy_for'] == 'KOSPI'
    assert all([p['date'] for p in s['points']] == ['2026-10-01', '2026-10-05'] for s in data['series'])
    assert data['series'][0]['points'][-1]['index'] == 110
    assert data['table'][0]['period_return_pct'] == 10
    assert data['table'][0]['vs_benchmark_pp'] == 8
    assert [s['currency'] for s in data['series']] == ['KRW', 'USD', 'KRW']
    assert 'KODEX 200 ETF' in data['source_line'] and 'proxy' in data['series'][-1]['name']
    assert 'adjustment unverified' in data['source_line']
    assert any('Native currency' in w for w in data['warnings'])
    assert all(path in ('/api/v1/candles', '/api/v1/stocks') for path, _ in calls)


@pytest.mark.parametrize('alias,symbol', [(k, v[0]) for k, v in ms.ALIASES.items()])
def test_every_alias_is_named_proxy(monkeypatch, alias, symbol):
    client, _ = client_for(monkeypatch, {symbol: [candle('2026-10-01', symbol)]})
    data = run(client, [symbol], benchmark=alias)
    assert len(data['series']) == 1
    assert data['benchmark']['kind'] == 'proxy'
    assert ms.ALIASES[alias][1] in data['series'][0]['name']
    assert alias + ' = ' in data['source_line']


def test_partial_failure_and_failed_benchmark(monkeypatch):
    client, _ = client_for(monkeypatch, {'SPY': [candle('2026-10-01')]})
    result = server.get_market_series(['SPY', 'BAD!', 'UNKNOWN'], start_date='2026-10-01', end_date='2026-10-16', benchmark='KOSPI')
    assert set(result) == {'ok', 'data', 'error', 'meta'} and result['ok']
    assert {e['code'] for e in result['data']['errors']} == {'INVALID_SYMBOL', 'PROVIDER_ERROR'}
    assert result['data']['table'][0]['vs_benchmark_pp'] is None
    assert len(result['data']['series']) == 1


def test_no_common_dates_and_all_failed_are_errors(monkeypatch):
    client, _ = client_for(monkeypatch, {'SPY': [candle('2026-10-01')], 'QQQ': [candle('2026-10-02', 'QQQ')]})
    result = server.get_market_series(['SPY', 'QQQ'], start_date='2026-10-01', end_date='2026-10-16')
    assert not result['ok'] and result['data']['errors'][-1]['code'] == 'NO_COMMON_DATES'
    assert server.get_market_series(['INVALID!'])['ok'] is False


@pytest.mark.parametrize('mutation', [
    lambda e: e.update(error={'code': 'partial'}),
    lambda e: e.update(complete=False),
    lambda e: e['result'].update(partial=True),
    lambda e: e['result'].update(symbol='OTHER'),
    lambda e: e['result'].pop('nextBefore'),
    lambda e: e['result']['candles'][0].pop('volume'),
    lambda e: e['result']['candles'][0].update(closePrice='NaN'),
    lambda e: e['result']['candles'][0].update(closePrice='0'),
    lambda e: e['result']['candles'][0].update(closePrice=100),
    lambda e: e['result']['candles'][0].update(currency='KRW'),
    lambda e: e['result']['candles'][0].update(timestamp='2027-01-01T00:00:00-05:00'),
    lambda e: e['result']['candles'][0].update(timestamp='2026-10-01T01:00:00-04:00'),
    lambda e: e['result']['candles'].append(deepcopy(e['result']['candles'][0])),
    lambda e: e['result'].update(nextBefore='2027-01-01T00:00:00-05:00'),
])
def test_full_envelope_and_documented_shape_fail_closed(monkeypatch, mutation):
    client, _ = client_for(monkeypatch, {'SPY': [candle('2026-10-01')]}, mutation)
    data = run(client, ['SPY'])
    assert not data['series'] and data['errors'][0]['code'] == 'INVALID_CANDLES'


@pytest.mark.parametrize('symbol,zone,pre,post', [
    ('005930', 'Asia/Seoul', '15:39:59', '15:40:00'),
    ('SPY', 'America/New_York', '16:09:59', '16:10:00'),
])
def test_confirmed_bar_rule_with_margin(monkeypatch, symbol, zone, pre, post):
    client, _ = client_for(monkeypatch, {symbol: [candle('2026-10-15', symbol), candle('2026-10-16', symbol)]})
    for clock, count in [(pre, 1), (post, 2)]:
        now = datetime.fromisoformat('2026-10-16T' + clock).replace(tzinfo=ZoneInfo(zone))
        data = ms.build_market_series(client, [symbol], start_date='2026-10-15', end_date='2026-10-16', now=now)
        assert len(data['series'][0]['points']) == count
        assert any('unconfirmed' in w for w in data['warnings']) == (count == 1)


def test_us_winter_close_uses_et_not_fixed_utc():
    assert not ms._confirmed(date(2026, 1, 12), 'SPY', datetime(2026, 1, 12, 21, 9, tzinfo=timezone.utc))
    assert ms._confirmed(date(2026, 1, 12), 'SPY', datetime(2026, 1, 12, 21, 10, tzinfo=timezone.utc))


def test_paging_inclusive_overlap_dates_bound_and_budget(monkeypatch):
    symbols = ['SPY', 'QQQ', 'AAPL', 'ACME', 'MSFT', 'NVDA', 'QCOM', '005930']
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(1000)]
    client, calls = client_for(monkeypatch, {s: [candle(d, s, str(100+i)) for i, d in enumerate(days)] for s in symbols})
    data = ms.build_market_series(client, symbols, start_date='2024-01-15', end_date='2026-09-20', now=NOW)
    assert len(data['series']) == 8 and not data['errors']
    assert data['sampling'].startswith('weekly')
    for series in data['series']:
        labels = [p['date'] for p in series['points']]
        assert labels == sorted(set(labels)) and len(labels) <= 260
        assert labels[0] == '2024-01-15' and labels[-1] == '2026-09-20'
        assert series['points'][0]['index'] == 100
    assert len(json.dumps(server._ok(data), ensure_ascii=False).encode()) < 100_000
    assert len([p for p, _ in calls if p == '/api/v1/candles']) > 8


@pytest.mark.parametrize('kwargs', [
    {'symbols': []}, {'symbols': ['A'] * 9}, {'symbols': ['A'], 'base': 0},
    {'symbols': ['A'], 'base': float('inf')}, {'symbols': ['A'], 'period': '2y'},
    {'symbols': ['A'], 'end_date': '2026-10-01'},
    {'symbols': ['A'], 'start_date': '20261001'},
    {'symbols': ['A'], 'start_date': '2000-01-01'},
    {'symbols': ['A'], 'start_date': '2026-10-19'},
    {'symbols': list('ABCDEFGH'), 'benchmark': 'SPY'},
])
def test_input_errors_before_http(monkeypatch, kwargs):
    client, calls = client_for(monkeypatch, {})
    with pytest.raises(ms.SeriesError, match='.'):
        ms.build_market_series(client, now=NOW, **kwargs)
    assert not calls


def test_period_month_clipping():
    now = datetime(2026, 3, 31, tzinfo=timezone.utc)
    assert ms._window('1mo', None, None, now) == (date(2026, 2, 28), date(2026, 3, 31))
    assert ms._window('ytd', None, None, now)[0] == date(2026, 1, 1)


@pytest.mark.parametrize('day,kst_hour', [('2026-10-16', 13), ('2026-01-12', 14)])
def test_recorded_us_daily_midnight_encoded_in_kst(monkeypatch, day, kst_hour):
    # Recorded Toss responses: US daily timestamps encode midnight ET as
    # 13:00 KST in DST (14:00 standard time), not midnight in the encoded offset.
    row = candle(day)
    row['timestamp'] = f'{day}T{kst_hour}:00:00.000+09:00'
    client, _ = client_for(monkeypatch, {'SPY': [row]})
    data = ms.build_market_series(client, ['SPY'], start_date=day, end_date=day, now=NOW)
    assert not data['errors'] and data['series'][0]['points'][0]['date'] == day


def test_real_registry_wire_response_budget(monkeypatch):
    from mcp.types import CallToolResult
    symbols = ['MSFT', 'ACME', 'NVDA', 'SPY']
    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(150)]
    client_for(monkeypatch, {s: [candle(d, s, str(100 + i / 10)) for i, d in enumerate(days)] for s in symbols})
    content, structured = asyncio.run(server.mcp.call_tool('get_market_series', {
        'symbols': symbols, 'start_date': '2026-05-01', 'end_date': '2026-10-01'}))
    result = CallToolResult(content=content, structuredContent=structured)
    assert len(result.model_dump_json().encode()) < 100_000
    data = structured.get('result', structured)['data']
    assert data['sampling'].startswith('weekly')
    assert len(data['series']) == 4 and not data['errors']
