"""Canonical Toss identities and real read clients over public example JSON.

Only MockTransport, synthetic authentication and disposable local SQLite run.
Every test has a behavioral assertion that fails on 70cff36.
"""
import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_toss_nav import capture, selected

from ledger_returns import scope_navs
from portfolio_engine import summarize_portfolio
from portfolio_sources import PortfolioCollector, Providers, collect_nav_evidence
from portfolio_store import NavStore, snapshot_summary
from toss_api import TossClient
from toss_nav_validation import validate_toss_response

INPUTS = ('holdings', 'prices', 'buying_power:KRW', 'buying_power:USD', 'orders', 'calendar', 'exchange_rate')
BENIGN = ({'error': None}, {'error': {}}, {'errors': []}, {'errors': None})


def assert_blocked(data, reason):
    account = data.row['account_nav']['toss:primary']
    assert all(value['value'] is None and reason in value['reason_codes']
               for value in account['nav_native'].values())
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] is None and reason in nav['reason_codes']
    assert data.legacy == summarize_portfolio(data.collection.holdings, data.collection.cash, data.fx)
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']


@pytest.mark.parametrize('source', ['holdings', 'prices'])
@pytest.mark.parametrize('alias', ['syn', ' SYN ', ' syn '])
@pytest.mark.parametrize('reverse', [False, True])
def test_pass7_canonical_duplicate_cannot_select_a_winner(tmp_path, monkeypatch, source, alias, reverse):
    def mutate(kind, payload):
        if kind == source:
            rows = payload['result']['items'] if kind == 'holdings' else payload['result']
            other = {**deepcopy(rows[0]), 'symbol': alias}
            rows.append(other)
            if reverse:
                rows.reverse()
        return payload
    data = capture(tmp_path, monkeypatch, nav_mutation=mutate)
    reason = 'DUPLICATE_HOLDING' if source == 'holdings' else 'DUPLICATE_QUOTE'
    assert_blocked(data, reason)
    if source == 'prices':
        quote = data.collection.nav_evidence['toss:primary']['quotes']['SYN']
        assert reason in quote['reason_codes'] and 'last_price' not in quote and 'priced_at' not in quote


@pytest.mark.parametrize('alias', ['syn', ' SYN ', ' syn '])
def test_pass7_single_quote_uses_holding_canonical_identifier(tmp_path, monkeypatch, alias):
    data = capture(tmp_path, monkeypatch, quote_symbol=alias)
    assert selected(data.row)['total_value_krw'] == 3500
    assert selected(data.row)['reason_codes'] == []


@pytest.mark.parametrize('market,currency', [('KR', 'USD'), ('US', 'KRW')])
def test_pass7_inventory_market_currency_must_agree_before_calendar_selection(tmp_path, monkeypatch, market, currency):
    def mutate(kind, payload):
        if kind == 'holdings':
            payload['result']['items'][0]['marketCountry'] = market
        return payload
    data = capture(tmp_path, monkeypatch, holding_currency=currency, quote_currency=currency, nav_mutation=mutate)
    assert_blocked(data, 'INVALID_SOURCE_RESPONSE')
    assert data.collection.nav_evidence['toss:primary']['market_calendars'] == {}


@pytest.mark.parametrize('source', INPUTS)
@pytest.mark.parametrize('marker', BENIGN, ids=['error-null', 'error-empty', 'errors-empty', 'errors-null'])
@pytest.mark.parametrize('nested', [False, True], ids=['envelope', 'nested'])
def test_pass7_empty_error_metadata_is_benign_but_nonempty_blocks(tmp_path, monkeypatch, source, marker, nested):
    def mutate(kind, payload, nonempty=False):
        if kind == source:
            target = payload
            if nested:
                target = payload.setdefault('extension', {}).setdefault('metadata', {})
            target.update({next(iter(marker)): {'code': 'SYNTHETIC_ERROR'}} if nonempty else marker)
        return payload
    good = capture(tmp_path, monkeypatch, nav_mutation=mutate, real_response_views=True)
    assert selected(good.row)['total_value_krw'] == 3500
    assert selected(good.row, 'toss')['total_value_krw'] == 6500
    assert selected(good.row)['reason_codes'] == []
    bad = capture(tmp_path, monkeypatch, nav_mutation=lambda kind, payload: mutate(kind, payload, True),
                  real_response_views=True)
    assert_blocked(bad, 'INVALID_SOURCE_RESPONSE')


@pytest.fixture
def http_examples():
    """GET 200 examples from public OpenAPI 1.2.24; no runtime network lookup."""
    examples = json.loads(Path(__file__).with_name('fixtures').joinpath('toss_nav_openapi_1_2_24.json').read_text())
    calls = []
    overrides = {}

    def handler(request):
        assert request.method == 'GET'
        path = request.url.path
        assert path in examples  # Auth/account discovery and order mutations cannot run.
        calls.append((path, dict(request.url.params)))
        key = {'/api/v1/holdings': 'withHoldings', '/api/v1/prices': 'usStock',
               '/api/v1/orders': 'empty', '/api/v1/exchange-rate': 'usdToKrwUp'}.get(path, 'businessDay')
        if path == '/api/v1/buying-power':
            key = request.url.params['currency'].lower()
        payload = deepcopy(overrides.get((path, key), examples[path][key]))
        if path == '/api/v1/prices':
            requested = request.url.params['symbols'].split(',')
            payload['result'] = [row for row in payload['result'] if row['symbol'] in requested]
        return httpx.Response(200, json=payload)

    # Bypass construction/configuration/cache; all actual GET/accessor code runs.
    client = object.__new__(TossClient)
    client._token = 'synthetic-test-token'
    client._token_expires = float('inf')
    client._account_seq = 1
    with httpx.Client(base_url='https://mock-toss.invalid', transport=httpx.MockTransport(handler), trust_env=False) as http:
        client._http = http
        yield SimpleNamespace(client=client, examples=examples, overrides=overrides, calls=calls)


def test_pass7_real_client_canonical_shapes_validate_public_openapi_examples(http_examples):
    client = http_examples.client
    reads = [('holdings', client.get_holdings(), {}, dict),
             ('prices', client.get_prices(['AAPL']), {'symbols': {'AAPL': 'USD'}}, list),
             ('buying_power', client.get_buying_power('KRW'), {'currency': 'KRW'}, dict),
             ('buying_power', client.get_buying_power('USD'), {'currency': 'USD'}, dict),
             ('orders', client.get_orders(status='OPEN'), {}, dict),
             ('calendar', client.get_market_calendar('US'), {'country': 'US'}, dict),
             ('calendar', client.get_market_calendar('KR'), {'country': 'KR'}, dict),
             ('exchange_rate', client.get_exchange_rate(), {'currency': 'USD'}, dict)]
    examples = http_examples.examples
    expected = [examples['/api/v1/holdings']['withHoldings'],
                examples['/api/v1/prices']['usStock']['result'],
                examples['/api/v1/buying-power']['krw']['result'],
                examples['/api/v1/buying-power']['usd']['result'],
                examples['/api/v1/orders']['empty'],
                examples['/api/v1/market-calendar/US']['businessDay']['result'],
                examples['/api/v1/market-calendar/KR']['businessDay']['result'],
                examples['/api/v1/exchange-rate']['usdToKrwUp']['result']]
    for (kind, response, binding, shape), legacy_json in zip(reads, expected):
        assert isinstance(response, shape)
        assert json.loads(json.dumps(response)) == legacy_json
        validated = validate_toss_response(kind, response, **binding)
        assert validated.reason_codes == [], (kind, validated.reason_codes)
        assert validated.value is not None
    assert len(http_examples.calls) == 8


def transport_snapshot(data, tmp_path, monkeypatch, *, canonical):
    """Public example fields with only clocks aligned to one 18:10 capture."""
    stamp = '2026-03-25T18:10:00+09:00'
    monkeypatch.setattr('portfolio_sources._now', lambda: stamp)
    monkeypatch.setattr('portfolio_sources._fx_read_time', lambda: '2026-03-25T18:10:03+09:00')
    examples = data.examples
    prices = deepcopy(examples['/api/v1/prices']['krStock'])
    prices['result'] += deepcopy(examples['/api/v1/prices']['usStock']['result'])
    for row in prices['result']:
        row['timestamp'] = stamp
    data.overrides[('/api/v1/prices', 'usStock')] = prices
    fx = deepcopy(examples['/api/v1/exchange-rate']['usdToKrwUp'])
    fx['result'].update(validFrom='2026-03-25T18:10:02+09:00', validUntil='2026-03-25T18:11:02+09:00')
    data.overrides[('/api/v1/exchange-rate', 'usdToKrwUp')] = fx
    client = data.client
    if canonical:
        for name in ('get_prices_evidence', 'get_buying_power_evidence', 'get_market_calendar_evidence'):
            monkeypatch.setattr(client, name, None)
    collector = PortfolioCollector(Providers(
        toss_labels=lambda: ['primary'], toss_client=lambda label: client,
        kis_accounts=lambda: [], kis_client=lambda label: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC_FAILURE'}},
        fx_quote=lambda currency: client.get_exchange_rate(base=currency, quote='KRW')))
    collection = collector.collect_positions()
    rates = collector.build_fx({row.currency for row in [*collection.holdings, *collection.cash]})
    legacy = summarize_portfolio(collection.holdings, collection.cash, rates)
    collect_nav_evidence(collector, collection)
    summary = snapshot_summary(legacy, collection, rates)
    row = NavStore(tmp_path / 'http-nav.db').upsert_snapshot(summary, datetime.fromisoformat(stamp).date().isoformat())
    return SimpleNamespace(collection=collection, fx=rates, legacy=legacy, row=row)


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
def test_pass7_real_client_mock_http_collection_is_complete(tmp_path, monkeypatch, http_examples, canonical):
    # Benign success metadata commonly accompanies client responses; test both
    # retained-envelope and public result views without mocking any client GET.
    for examples in http_examples.examples.values():
        for payload in examples.values():
            payload.update(error=None, errors=[])
    data = transport_snapshot(http_examples, tmp_path, monkeypatch, canonical=canonical)
    expected = float(Decimal('72000') * 100 + Decimal('5000000') +
                     (Decimal('185.70') * 10 + Decimal('3500.5')) * Decimal('1380.5'))
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] == expected, nav['reason_codes']
        assert nav['result_status'] == 'complete' and nav['reason_codes'] == []
    assert data.collection.nav_source_quality_reasons == {}
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']
    assert data.row['nav_bridge'] == data.legacy['nav_bridge']
    assert scope_navs([data.row], 'equity')[0] == data.row
    assert data.row['valuation_at'] == data.collection.fetched_at
    assert data.row['mark_quality']['fx']['USD']['freshness_checked_at'] == '2026-03-25T18:10:03+09:00'
    assert data.row['mark_quality']['fx']['USD']['freshness_time_basis'] == 'fx_read'
    assert set(data.collection.nav_evidence['toss:primary']['market_calendars']) == {'KR', 'US'}
    assert [params for path, params in http_examples.calls if path == '/api/v1/prices'] == [{'symbols': '005930,AAPL'}]
