"""Real MCP transport and concurrent synthetic state; never contact providers."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anyio
import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient
from test_auth_transport import URL
from test_oauth_durable import provider
from test_service_auth import HEADERS, application, initialize, rpc

import response_shape
import server
from auth_observability import request_id_context
from oauth_registration import build_dcr_routes
from service_auth import SERVICE_READ_TOOLS, is_service_request


def test_long_sync_tool_keeps_asgi_health_oauth_auth_and_short_tool_responsive(
    tmp_path, monkeypatch, owner_credentials, record_property,
):
    app, _, _ = application()
    p = provider(tmp_path, resource_url=URL)
    monkeypatch.setattr(server, '_oauth_provider', p)
    app.custom_route('/healthz', methods=['GET'])(server.healthz)
    for route in build_dcr_routes(p, AnyHttpUrl(URL), ['read']):
        if route.path != '/authorize':
            app.custom_route(route.path, methods=list(route.methods or []))(route.endpoint)
    app.custom_route('/authorize', methods=['GET'])(server._oauth_authorize)
    entered, finished = threading.Event(), threading.Event()

    def context_result(ctx: Context) -> dict[str, Any]:
        return {'service': is_service_request(app._mcp_server.request_context.request),
                'request_id': request_id_context.get(),
                'path': app._mcp_server.request_context.request.url.path,
                'context_request_id': str(ctx.request_id)}

    def long(ctx: Context) -> dict[str, Any]:
        entered.set()
        try:
            time.sleep(3)
            return context_result(ctx)
        finally:
            finished.set()

    def short(ctx: Context) -> dict[str, Any]:
        return context_result(ctx)

    for name, fn in (('get_toss_trade_history', long), ('get_toss_accounts', short)):
        app.remove_tool(name)
        app.add_tool(fn, name=name)

    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client).status_code == 200
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(rpc, client, 'tools/call', {'name': 'get_toss_trade_history', 'arguments': {}})
            assert entered.wait(5), 'Long tool never started'
            started = time.monotonic()
            health = client.get('/healthz')
            health_elapsed = time.monotonic() - started
            quick = rpc(client, 'tools/call', {'name': 'get_toss_accounts', 'arguments': {}})
            short_before_long = not finished.is_set()
            listed = rpc(client, 'tools/list')
            denied = rpc(client, 'tools/list', headers={**HEADERS, 'Authorization': 'Bearer synthetic-invalid'})
            forbidden = rpc(client, 'tools/call', {'name': 'get_quote', 'arguments': {}})
            metadata = client.get('/.well-known/oauth-authorization-server')
            authorize = client.get('/authorize')
            controls_elapsed = time.monotonic() - started
            controls_before_long = not finished.is_set()
            long_response = slow.result(timeout=5)

    record_property('health_seconds', health_elapsed)
    record_property('all_controls_seconds', controls_elapsed)
    assert health.status_code == 200 and health.text == 'ok'
    assert health_elapsed < .5, f'/healthz blocked for {health_elapsed:.3f}s'
    assert short_before_long, 'Second sync tool waited for the long tool'
    assert controls_before_long and controls_elapsed < .5
    assert {t['name'] for t in listed.json()['result']['tools']} == SERVICE_READ_TOOLS
    assert denied.status_code == 401
    assert forbidden.json()['result']['content'][0]['text'] == 'Forbidden'
    assert metadata.status_code == 200 and authorize.status_code == 400
    for response in (quick, long_response):
        result = response.json()['result']
        assert not result['isError']
        assert result['structuredContent'] == {
            'service': True, 'request_id': response.headers['x-request-id'],
            'path': '/mcp', 'context_request_id': '1',
        }
    assert quick.headers['x-request-id'] != long_response.headers['x-request-id']


@pytest.mark.asyncio
async def test_sync_dispatch_retains_validation_errors_results_and_async_tools():
    app, reference = server.ObservedFastMCP('offloop'), FastMCP('reference')
    loop_thread = threading.get_ident()

    def sync(value: int = 4) -> dict[str, int]:
        """A synthetic structured tool."""
        if value < 0:
            raise ValueError('synthetic failure')
        return {'value': value}

    async def asynchronous() -> dict[str, bool]:
        return {'on_event_loop': threading.get_ident() == loop_thread}

    for instance in (app, reference):
        instance.add_tool(sync)
        instance.add_tool(asynchronous)
    assert [t.model_dump() for t in await app.list_tools()] == [t.model_dump() for t in await reference.list_tools()]
    # Same structured result; the text block is its compact JSON (MCP 2025-06-18 wire form).
    (content, structured), (_, expected) = await app.call_tool('sync', {'value': '5'}), await reference.call_tool('sync', {'value': '5'})
    assert structured == expected and [block.text for block in content] == [response_shape.compact_json(expected)]
    _, result = await app.call_tool('asynchronous', {})
    assert result == {'on_event_loop': True}
    for arguments in ({'value': -1}, {'value': 'invalid'}):
        with pytest.raises(ToolError) as expected:
            await reference.call_tool('sync', arguments)
        with pytest.raises(ToolError) as actual:
            await app.call_tool('sync', arguments)
        assert str(actual.value) == str(expected.value)


@pytest.mark.asyncio
async def test_sync_dispatch_is_bounded_and_cancellation_keeps_worker_slot():
    app = server.ObservedFastMCP('bounded')
    app._tool_limiter = anyio.CapacityLimiter(2)
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = peak = 0

    def blocking() -> dict[str, bool]:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        try:
            assert release.wait(5)
            return {'ok': True}
        finally:
            with guard:
                active -= 1

    app.add_tool(blocking)
    scope = anyio.CancelScope()

    async def cancelled_call():
        with scope:
            await app.call_tool('blocking', {})

    async with anyio.create_task_group() as group:
        group.start_soon(cancelled_call)
        group.start_soon(app.call_tool, 'blocking', {})
        try:
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(.01)
            scope.cancel()
            group.start_soon(app.call_tool, 'blocking', {})
            await anyio.sleep(.05)
            assert peak == active == 2
            assert app._tool_limiter.borrowed_tokens == 2
        finally:
            release.set()
    assert active == 0 and peak == 2


@pytest.mark.asyncio
async def test_registered_catalog_matches_2_14_0_plus_merge():
    catalog = [t.model_dump(mode='json') for t in await server.mcp.list_tools()]
    assert len(catalog) == 68
    encoded = json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode()
    # The earlier catalog hashed 86d35b3e...; 2.12.10 adds one ledgerless_returns sentence (5da52038...) and
    # one tracked line to the get_portfolio_performance and get_portfolio_history
    # descriptions only; 2.12.11 notes the kept 18:10 representative in get_portfolio_summary's
    # snapshot argument and snapshot_portfolio_nav; 2.13.0 adds portfolio_scope 'kis' (schema and one
    # description line) to get_portfolio_performance and get_portfolio_history only; 2.13.1 names
    # the consolidated ledgerless basis in the same two descriptions.
    # 2.14.0: explicit annotations, ledger disclosures, summary default off and market series.
    # History/performance/account-health read-only; performance ledger disclosure.
    # Merged catalog (local/operator list): 12 merged tools appended, get_portfolio_summary
    # read-only without its snapshot argument (T0).
    # The equity ledgerless basis and the effective_period line, in the
    # get_portfolio_performance and get_portfolio_history descriptions only (was 1a2c5c07...).
    # get_trade_review's win-rate and summary/full lines only (was bfd0cd56...).
    # get_trading_pnl's summary line (round_trips = get_trade_review's summary body;
    # was 36c6b4af...), then get_portfolio_history's history line without get_portfolio_summary(snapshot=True)
    # (was 05244292...).
    # get_realized_pnl's reconcile argument says ENGINE_QUANTITY_DRIFT books are null
    # either way, as in get_trade_review (was 940c1d32...).
    # get_market_info's description names each section's provider (was d94c4f04...).
    # export_portfolio_dataset and snapshot_portfolio_nav say "only on an explicit
    # request", and get_portfolio_returns' include_series line (was fd99e57e...).
    assert hashlib.sha256(encoded).hexdigest() == '7f95e424f9c713c5e9a1a87b4d77d2350ebeb1f4fe5be9373f00412b2ffed0f5'


def _simultaneous(operation, count=8):
    start = threading.Barrier(count)

    def run(index):
        start.wait(timeout=5)
        return operation(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


@pytest.mark.parametrize('broker', ['toss', 'kis'])
def test_account_registry_constructs_one_client_under_concurrent_first_use(tmp_path, monkeypatch, broker):
    from types import SimpleNamespace

    import kis_accounts
    import toss_accounts

    created = []

    def construct(**kwargs):
        time.sleep(.02)
        result = SimpleNamespace()
        created.append(result)
        return result

    if broker == 'toss':
        monkeypatch.setattr(toss_accounts, 'TossClient', construct)
        registry = toss_accounts.TossAccountRegistry([
            toss_accounts.TossCredentials('primary', 'fixture-client', 'fixture-secret'),
        ], cache_dir=tmp_path)
    else:
        monkeypatch.setattr(kis_accounts, 'KISClient', construct)
        registry = kis_accounts.KISAccountRegistry([
            kis_accounts.KISAccount('primary', '00000000', '01'),
        ], cache_dir=tmp_path)
    results = _simultaneous(lambda _: registry.client_for('primary'))
    assert len(created) == 1 and all(result is created[0] for result in results)


def test_nested_singleton_initialization_is_once_and_reentrant(monkeypatch):
    from types import SimpleNamespace

    created = []
    client = object()

    def construct():
        time.sleep(.02)
        registry = SimpleNamespace(client_for=lambda _: client)
        created.append(registry)
        return registry

    monkeypatch.setattr(server, '_toss_accounts', None)
    monkeypatch.setattr(server, '_toss_client', None)
    monkeypatch.setattr(server.TossAccountRegistry, 'from_sources', construct)
    assert all(result is client for result in _simultaneous(lambda _: server._get_toss()))
    assert len(created) == 1


def test_collector_health_snapshots_match_each_overlapping_outcome():
    from portfolio_sources import PortfolioCollector, Providers

    operations = threading.Barrier(8)
    collector = PortfolioCollector(Providers(
        toss_labels=lambda: [], toss_client=lambda _: None, kis_accounts=lambda: [], kis_client=lambda _: None,
        error_formatter=lambda _: {'error': {'code': 'SYNTHETIC'}},
        health_observer=lambda *args, **kwargs: time.sleep(.01),
    ))

    def call(index):
        def operation():
            # A health lock held around broker IO would deadlock here.
            operations.wait(timeout=5)
            if index % 2:
                raise ValueError('synthetic failure')
            return index
        return collector._run('toss:fixture', 'toss', operation)

    results = _simultaneous(call)
    for index, (value, health) in enumerate(results):
        assert health['status'] == ('error' if index % 2 else 'ok')
        assert value == (None if index % 2 else index)
    assert set(collector.health_report()) == {'toss:fixture'}


def test_statement_identity_initializes_once_across_workers(tmp_path, monkeypatch):
    import statement_import

    generated = []

    def synthetic_salt(size):
        time.sleep(.02)
        generated.append(size)
        return b's' * size

    monkeypatch.setattr(statement_import.secrets, 'token_bytes', synthetic_salt)
    store = statement_import.StatementStore(tmp_path / 'statements.db')
    ids = _simultaneous(lambda _: store.account_id('synthetic-account'))
    assert len(set(ids)) == 1 and generated == [32]


def test_all_yfinance_entrypoints_share_one_session_guard(monkeypatch):
    from types import SimpleNamespace

    import pandas as pd
    import yfinance as yf

    from portfolio_sources import yfinance_fx_quote
    from yfinance_api import YFinanceClient

    active = peak = 0
    lock = threading.Lock()

    def touch():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.01)
        with lock:
            active -= 1

    class Ticker:
        income_stmt = balance_sheet = cash_flow = None
        earnings_estimate = revenue_estimate = recommendations_summary = None

        def __init__(self, symbol):
            touch()
            self.symbol = symbol

        @property
        def info(self):
            touch()
            return {'symbol': self.symbol, 'exchange': 'NMS'}

        def history(self, **kwargs):
            touch()
            return pd.DataFrame({'Close': [4.2], 'High': [4.2], 'Low': [4.2]},
                                index=pd.DatetimeIndex(['2026-10-01'], tz='UTC'))

    monkeypatch.setattr(yf, 'Ticker', Ticker)
    monkeypatch.setattr(server, '_get_toss', lambda: SimpleNamespace(get_stock_info=lambda _: []))
    client = YFinanceClient()
    operations = [lambda: client.get_financials('AMZN'), lambda: client.get_key_metrics('AMZN'),
                  lambda: client.get_ohlcv('AMZN'), server._risk_free_decimal, server.get_risk_free_rate,
                  lambda: server._resolve_market('AMZN'), lambda: yfinance_fx_quote('USD')]
    results = _simultaneous(lambda index: operations[index](), len(operations))
    assert len(results) == 7 and peak == 1 and active == 0


@pytest.mark.asyncio
async def test_sqlite_store_instances_work_across_mcp_worker_threads(tmp_path):
    import asyncio

    from test_snapshot_versions import summary

    from portfolio_export import DatasetStore
    from portfolio_ledger import LedgerStore
    from portfolio_store import NavStore

    app = server.ObservedFastMCP('sqlite-workers')
    nav = NavStore(tmp_path / 'nav.db')
    datasets = DatasetStore(tmp_path / 'datasets.db')
    start = threading.Barrier(2)

    @app.tool()
    def persist(value: int) -> dict[str, int]:
        start.wait(timeout=5)
        snapshot = nav.upsert_snapshot(summary(total=value), '2026-10-02')
        dataset_id = datasets.freeze('transactions', [], {'summary': {'result_status': 'complete'}})
        page = datasets.page('transactions', dataset_id)
        # LedgerStore is request-local in production; its connection stays on
        # this worker, including initialization and the read-index setup.
        with LedgerStore(tmp_path / 'ledger.db').connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM ledger_state').fetchone()[0] > 0
        return {'version': snapshot['version'], 'rows': page['page_count']}

    responses = await asyncio.gather(app.call_tool('persist', {'value': 100}), app.call_tool('persist', {'value': 200}))
    assert sorted(result['version'] for _, result in responses) == [1, 2]
    assert all(result['rows'] == 0 for _, result in responses)
