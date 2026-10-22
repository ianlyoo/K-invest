"""Synthetic outbound admission tests under eight concurrent MCP dispatches."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx
import pytest

import sec_api
import server


def peak_second(timestamps):
    return max(sum(start <= stamp < start + 1 for stamp in timestamps) for start in timestamps)


@pytest.mark.asyncio
async def test_eight_sec_financials_calls_preserve_serial_request_cap(monkeypatch, record_property):
    monkeypatch.setattr(sec_api, '_require_sec_user_agent', lambda: None)
    timestamps = []
    lock = threading.Lock()

    def handler(request):
        with lock:
            timestamps.append(time.monotonic())
        time.sleep(.2)
        if request.url.path.endswith('company_tickers.json'):
            payload = {'0': {'ticker': 'TEST', 'cik_str': 1}}
        elif '/companyfacts/' in request.url.path:
            payload = {'entityName': 'Synthetic issuer', 'facts': {}}
        else:
            payload = {}
        return httpx.Response(200, json=payload)

    client = object.__new__(sec_api.SECClient)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client._http:
        monkeypatch.setattr(server, '_get_sec', lambda: client)
        app = server.ObservedFastMCP('synthetic-sec-budget')
        app.add_tool(server.get_sec_financials)
        results = await asyncio.gather(*[
            app.call_tool('get_sec_financials', {'symbol': 'TEST'}) for _ in range(8)
        ])
    assert all(result[1]['ok'] for result in results)
    assert len(timestamps) == 24
    peak = peak_second(timestamps)
    record_property('sec_requests', len(timestamps))
    record_property('sec_peak_requests_per_second', peak)
    assert peak <= 4, f'Concurrent SEC calls burst to {peak} requests/second'


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.value

    def sleep(self, delay):
        with self.lock:
            self.value += delay


@pytest.mark.asyncio
async def test_sec_json_and_text_share_budget_across_clients(monkeypatch):
    import provider_pacing

    clock, sent = Clock(), []
    monkeypatch.setattr(provider_pacing, 'SEC_BUDGET', provider_pacing.RequestBudget(.26, clock=clock.now, sleeper=clock.sleep))
    monkeypatch.setattr(sec_api, '_require_sec_user_agent', lambda: None)

    def handler(request):
        sent.append(clock.now())
        return httpx.Response(200, json={'ok': True})

    clients = [object.__new__(sec_api.SECClient) for _ in range(8)]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        for client in clients:
            client._http = http
        app = server.ObservedFastMCP('mixed-sec')

        @app.tool()
        def read(index: int) -> dict[str, Any]:
            method = clients[index]._get_json if index % 2 else clients[index]._get_text
            return {'value': method('https://sec.test.invalid/fixture')}

        results = await asyncio.gather(*[app.call_tool('read', {'index': i}) for i in range(8)])
    assert len(sent) == 8 and all(result[1]['value'] is not None for result in results)
    assert peak_second(sent) <= 4


@pytest.mark.parametrize('status', [403, 429, 503])
def test_sec_cooldown_covers_other_clients_and_both_response_types(monkeypatch, status):
    import provider_pacing

    clock, sent = Clock(), []
    monkeypatch.setattr(provider_pacing, 'SEC_BUDGET', provider_pacing.RequestBudget(.26, clock=clock.now, sleeper=clock.sleep))
    monkeypatch.setattr(sec_api, '_require_sec_user_agent', lambda: None)

    def handler(request):
        sent.append(clock.now())
        return httpx.Response(status if len(sent) == 1 else 200, headers={'Retry-After': '7'}, json={'ok': True})

    first, second = object.__new__(sec_api.SECClient), object.__new__(sec_api.SECClient)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        first._http = second._http = http
        assert first._get_json('https://sec.test.invalid/json') is None
        assert second._get_text('https://sec.test.invalid/text') is None
        assert len(sent) == 1
        clock.sleep(7)
        assert second._get_text('https://sec.test.invalid/text') is not None
    assert len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('shared_credentials', [True, False])
async def test_eight_toss_reads_keep_group_caps_and_single_refresh(monkeypatch, shared_credentials):
    import provider_pacing
    import toss_api

    clock, sent = Clock(), []
    monkeypatch.setattr(toss_api, '_RATE_BUDGETS', {
        name: provider_pacing.RequestBudget(budget.interval, clock=clock.now, sleeper=clock.sleep)
        for name, budget in toss_api._RATE_BUDGETS.items()
    })

    def handler(request):
        sent.append((toss_api._rate_group(request.url.path), clock.now()))
        if request.method == 'POST':
            return httpx.Response(200, json={'access_token': 'synthetic-only', 'expires_in': 3600})
        return httpx.Response(200, json={'result': {}})

    clients = []
    with httpx.Client(base_url='https://toss.test.invalid', transport=httpx.MockTransport(handler)) as http:
        for index in range(1 if shared_credentials else 8):
            client = object.__new__(toss_api.TossClient)
            client.client_id, client.client_secret = f'fixture-{index}', 'synthetic-only'
            client._token, client._token_expires, client._http = None, 0, http
            client._load_cached_token = lambda: False
            client._save_cached_token = lambda token, expires, c=client: setattr(c, '_token_expires', time.time() + expires)
            clients.append(client)
        app = server.ObservedFastMCP('toss-rate')

        @app.tool()
        def read(index: int) -> dict[str, Any]:
            return clients[index % len(clients)]._get('/api/v1/holdings')

        await asyncio.gather(*[app.call_tool('read', {'index': i}) for i in range(8)])
    auth = [stamp for group, stamp in sent if group == 'AUTH']
    assets = [stamp for group, stamp in sent if group == 'ASSET']
    assert len(auth) == (1 if shared_credentials else 8) and len(assets) == 8
    assert peak_second(auth) <= 5 and peak_second(assets) <= 5


def test_toss_admission_honors_existing_nav_deadline(monkeypatch):
    import provider_pacing
    import toss_api
    from portfolio_sources import _NAV_READ_DEADLINE

    budget = provider_pacing.RequestBudget(10)
    monkeypatch.setitem(toss_api._RATE_BUDGETS, 'ACCOUNT', budget)
    client = object.__new__(toss_api.TossClient)
    with httpx.Client(base_url='https://toss.test.invalid', transport=httpx.MockTransport(
            lambda _: pytest.fail('An expired NAV admission must not send'))) as client._http:
        with budget.request():
            pass
        token = _NAV_READ_DEADLINE.set(time.monotonic() + .05)
        try:
            with pytest.raises(TimeoutError, match='budget exceeded'):
                client._send('get', '/api/v1/accounts')
        finally:
            _NAV_READ_DEADLINE.reset(token)


@pytest.fixture
def signing_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.asyncio
async def test_eight_binance_public_and_signed_calls_share_admission(monkeypatch, signing_key):
    from urllib.parse import parse_qs

    import provider_pacing
    from binance_futures_api import BinanceFuturesClient
    from crypto_futures_accounts import BinanceAccountClient

    clock, sent = Clock(), []
    monkeypatch.setattr(provider_pacing, 'BINANCE_BUDGET', provider_pacing.RequestBudget(clock=clock.now, sleeper=clock.sleep))

    def handler(request):
        stamp = clock.now()
        signed = request.url.path.endswith('income')
        if signed:
            # Admission may wait seconds behind a public call: sign afterwards.
            assert int(parse_qs(request.url.query.decode())['timestamp'][0]) == int(stamp * 1000)
        sent.append((stamp, 3.01 if signed else 1.01))
        return httpx.Response(200, json={'symbol': 'TEST', 'price': '1'})

    with httpx.Client(base_url='https://fapi.binance.com', transport=httpx.MockTransport(handler)) as http:
        public = BinanceFuturesClient(http)
        accounts = [BinanceAccountClient('synthetic-only', signing_key, http=http,
                    clock=clock.now, sleeper=clock.sleep) for _ in range(4)]
        app = server.ObservedFastMCP('binance-budget')

        @app.tool()
        def read(index: int) -> dict[str, Any]:
            return accounts[index // 2].request('GET', '/fapi/v1/income') if index % 2 else public.price_ticker('TEST')

        await asyncio.gather(*[app.call_tool('read', {'index': i}) for i in range(8)])
    assert len(sent) == 8
    assert all(b[0] - a[0] >= a[1] - 1e-6 for a, b in zip(sent, sent[1:]))
    assert peak_second([stamp for stamp, _ in sent]) <= 1


@pytest.mark.parametrize('public_first', [True, False])
@pytest.mark.parametrize('status', [418, 429])
def test_binance_cooldown_is_shared_in_both_directions(monkeypatch, signing_key, public_first, status):
    import provider_pacing
    from binance_futures_api import BinanceAPIError, BinanceFuturesClient
    from crypto_futures_accounts import BinanceAccountClient, FuturesError

    clock, sent = Clock(), []
    monkeypatch.setattr(provider_pacing, 'BINANCE_BUDGET', provider_pacing.RequestBudget(clock=clock.now, sleeper=clock.sleep))

    def handler(request):
        sent.append(clock.now())
        return httpx.Response(status if len(sent) == 1 else 200, headers={'Retry-After': '7'}, json={'price': '1'})

    with httpx.Client(base_url='https://fapi.binance.com', transport=httpx.MockTransport(handler)) as http:
        public = BinanceFuturesClient(http)
        account = BinanceAccountClient('synthetic-only', signing_key, http=http, clock=clock.now, sleeper=clock.sleep)
        calls = [lambda: public.price_ticker('TEST'), lambda: account.request('GET', '/fapi/v1/time')]
        if not public_first:
            calls.reverse()
        for call in calls:
            with pytest.raises((BinanceAPIError, FuturesError)) as error:
                call()
            assert error.value.status == status
        assert len(sent) == 1
        clock.sleep(7)
        # A different account has no per-client cooldown; shared admission has expired.
        other = BinanceAccountClient('synthetic-only', signing_key, http=http, clock=clock.now, sleeper=clock.sleep)
        assert other.request('GET', '/fapi/v1/time') == {'price': '1'}
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_eight_yfinance_calls_remain_serial(monkeypatch):
    from types import SimpleNamespace

    import yfinance as yf

    from yfinance_api import YFinanceClient

    lock = threading.Lock()
    active = peak = calls = 0

    def history(**kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        time.sleep(.02)
        with lock:
            active -= 1
        return None

    monkeypatch.setattr(yf, 'Ticker', lambda _: SimpleNamespace(history=history))
    app = server.ObservedFastMCP('yfinance-budget')
    app.add_tool(YFinanceClient().get_ohlcv)
    await asyncio.gather(*[app.call_tool('get_ohlcv', {'symbol': 'TEST'}) for _ in range(8)])
    assert calls == 8 and peak == 1 and active == 0


@pytest.mark.asyncio
async def test_eight_hyperliquid_reads_keep_existing_weight_spacing():
    from crypto_futures_accounts import HyperliquidAccountClient

    stamps = []
    address = '0x' + 'a' * 40

    def handler(request):
        stamps.append(time.monotonic())
        time.sleep(.005)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HyperliquidAccountClient([address], http=http)
        app = server.ObservedFastMCP('hyperliquid-budget')

        @app.tool()
        def read() -> dict:
            return client.info('clearinghouseState', address)

        await asyncio.gather(*[app.call_tool('read', {}) for _ in range(8)])
    assert len(stamps) == 8 and min(b - a for a, b in zip(stamps, stamps[1:])) >= .095
