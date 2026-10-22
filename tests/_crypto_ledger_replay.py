"""Subprocess replay: import every production module from the requested tree.

Only synthetic, published-shape HTTP fixtures are used. No .env, credentials,
external database or network is read. Parent compares complete legacy tables.
"""
import contextlib
import importlib.util
import inspect
import io
import json
import socket
import sqlite3
import sys
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT, OUTPUT = map(Path, sys.argv[1:3])
CASES = json.loads(sys.argv[3])
sys.path.insert(0, str(ROOT))


def blocked(*args, **kwargs):
    raise AssertionError('Replay network forbidden')


socket.socket.connect = socket.socket.connect_ex = blocked
import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import crypto_futures_accounts as futures  # noqa: E402
import kis_api  # noqa: E402
import ledger_flows  # noqa: E402
import portfolio_ledger as ledger  # noqa: E402
import portfolio_sources  # noqa: E402
import provider_pacing  # noqa: E402
import toss_api  # noqa: E402
from portfolio_sources import PortfolioCollector, Providers  # noqa: E402

spec = importlib.util.spec_from_file_location('replay_collector', ROOT / 'scripts/collect_ledger.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
for module in (futures, ledger_flows, ledger, provider_pacing, collector, portfolio_sources, toss_api, kis_api):
    assert Path(module.__file__).resolve().is_relative_to(ROOT.resolve())

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
STAMP = int(datetime(2026, 10, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
ADDRESSES = ['0x' + c * 40 for c in ('a', 'b')]
OTHER = '0x' + 'c' * 40
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EQUITY_FIXTURES = json.loads(Path(__file__).with_name('fixtures').joinpath('toss_fill_openapi_1_2_24.json').read_text())


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


collector.datetime = ledger.datetime = ledger_flows.datetime = Clock
collector.uuid4 = lambda: UUID(int=1)
ledger.secrets.token_bytes = lambda size: b'F' * size


class Day(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 11)


collector.date = Day


def equity(case, trace, extra):
    """Real clients, HTTP parsing, pagination, adapters and strict Toss windows."""
    fixture = deepcopy(EQUITY_FIXTURES['completedWithNextPage'])
    order = fixture['result']['orders'][0]
    order['orderedAt'] = '2026-10-11T09:30:00+09:00'
    order['execution'].update(filledAt='2026-10-11T09:31:15+09:00', settlementDate='2026-10-13')
    if case in ('toss_null_commission', 'toss_null_tax'):
        order['execution']['commission' if case.endswith('commission') else 'tax'] = None
    if case == 'toss_strict_precision':
        order['execution']['commission'] = '0.00000000000000000000001'
    if case == 'toss_strict_only':
        order['orderedAt'] = '2025-12-01T09:30:00+09:00'

    def response(request):
        params = dict(request.url.params)
        if request.url.path == '/api/v1/orders':
            strict = 'from' not in params
            (extra if strict else trace).append([request.method, request.url.path, params])
            if strict and case == 'toss_strict_timeout':
                raise httpx.ReadTimeout('synthetic')
            if strict and case in ('toss_strict_503', 'toss_strict_429'):
                return httpx.Response(503 if case.endswith('503') else 429,
                                      headers={'Retry-After': '0'}, json={})
            payload = EQUITY_FIXTURES['empty'] if (params.get('cursor') or params.get('status') == 'OPEN'
                or not strict and case == 'toss_strict_only') else fixture
            return httpx.Response(200, json=payload)
        assert request.url.path == '/uapi/domestic-stock/v1/trading/inquire-daily-ccld'
        trace.append([request.method, request.url.path, params])
        return httpx.Response(200, headers={'tr_cont': 'D'}, json={'rt_cd': '0', 'output2': {}, 'output1': [
            dict(ord_dt='20261011', odno='1001', pdno='005930', prdt_name='Synthetic',
                 sll_buy_dvsn_cd='02', tot_ccld_qty='1', tot_ccld_amt='70000', avg_prvs='70000', prsm_tlex_smtl='10')]})

    toss_api._RATE_BUDGETS = {'ORDER_HISTORY': provider_pacing.RequestBudget(0)}
    toss = object.__new__(toss_api.TossClient)
    toss.client_id = 'synthetic-' + case
    toss._token, toss._token_expires, toss._account_seq = 'synthetic', float('inf'), 1
    toss._http = httpx.Client(base_url='https://toss.invalid', transport=httpx.MockTransport(response))
    kis_api.MIN_REQUEST_INTERVAL = 0
    kis = object.__new__(kis_api.KISClient)
    kis.app_key, kis.app_secret, kis.cano, kis.acnt_prdt_cd = 'synthetic-' + case, 'synthetic', '12345678', '22'
    kis.url_base, kis._token, kis._token_expires = 'https://kis.invalid', 'synthetic', float('inf')
    kis._http = httpx.Client(transport=httpx.MockTransport(response))
    options = {'strict_toss_fills': True} if 'strict_toss_fills' in inspect.signature(PortfolioCollector).parameters else {}
    return PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda _: toss,
        kis_accounts=lambda: [SimpleNamespace(label='primary', supports_overseas=False)], kis_client=lambda _: kis,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC', 'kind': 'provider_unavailable'}}), **options)


def delta_for(case):
    delta = {'type': 'internalTransfer', 'usdc': '7', 'user': OTHER,
             'destination': ADDRESSES[0], 'fee': '0'}
    if case.startswith('spot'):
        delta = dict(type='spotTransfer', amount='7', token='USDC', usdcValue='7',
                     user=OTHER, destination=ADDRESSES[0], fee='0')
    elif case.startswith('withdraw'):
        delta = dict(type='withdraw', usdc='7', fee='0', nonce=1)
    elif case.startswith('send'):
        delta = dict(type='send', amount='7', token='USDC', user=OTHER,
                     destination=ADDRESSES[0], sourceDex='spot', destinationDex='', fee='0')
    if case.endswith('_fee'):
        delta.pop('fee', None)
    elif case == 'withdraw_nonce':
        del delta['nonce']
    elif case == 'spot_value':
        del delta['usdcValue']
    elif case.endswith('_fee_null'):
        delta['fee'] = None
    elif case == 'extra_delta':
        delta['partial'] = True
    return delta


def snapshot(store, directory):
    result = {}
    with store.read() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ledger_%' ORDER BY name")]
        for table in tables:
            query = 'SELECT * FROM ' + table
            rows = []
            for record in conn.execute(query):
                row = dict(record)
                for key, value in row.items():
                    if isinstance(value, bytes):
                        row[key] = value.hex()
                    elif isinstance(value, str):
                        row[key] = value.replace(str(directory), '<fixture-root>')
                rows.append(row)
            result[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return result


def run(case):
    directory = OUTPUT / case
    directory.mkdir(parents=True)
    store = ledger.LedgerStore(directory / 'portfolio.db')
    trace, extra, counts, phase = [], [], {}, {'strict': False}
    captured = {}
    original_match = store.match_transfers

    def match(*args, **kwargs):
        result = original_match(*args, **kwargs)
        phase['strict'] = True
        captured['tables'] = snapshot(store, directory)
        captured['trace'] = list(trace)
        return result
    store.match_transfers = match

    def response(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        safe = {k: v for k, v in params.items() if k not in ('signature', 'timestamp', 'recvWindow')}
        trace.append([request.method, path, body or safe])
        key = (body['user'], body['type']) if body else (path, params.get('type'))
        counts[key] = counts.get(key, 0) + 1
        if phase['strict'] and (case in ('supplemental_429', 'supplemental_503', 'supplemental_json', 'supplemental_budget')):
            if body and body['user'] == ADDRESSES[0] and body['type'] == 'userNonFundingLedgerUpdates':
                if case == 'supplemental_json':
                    return httpx.Response(200, content=b'[{')
                if case == 'supplemental_budget':
                    return httpx.Response(200, json=[dict(time=STAMP, hash=str(i), delta=dict(type='deposit', usdc='1')) for i in range(500)])
                return httpx.Response(429 if case.endswith('429') else 503, headers={'Retry-After': '60'})
        if body:
            kind = body['type']
            if kind == 'userFillsByTime':
                payload = [dict(coin='BTC', px='2', sz='1', side='B', time=STAMP, startPosition='0',
                    dir='Open Long', closedPnl='0', hash='fixture-fill', oid=1, crossed=True,
                    fee='0.1', tid=9, feeToken='USDC', builderFee='0')]
            elif kind == 'userFunding':
                payload = [dict(time=STAMP, hash='fixture-funding', delta=dict(type='funding', coin='BTC', usdc='0.1', szi='1', fundingRate='0.0001'))]
            else:
                cases = case.removeprefix('mixed_')
                if case == 'published':
                    deltas = [dict(type='deposit', usdc='2'), dict(type='withdraw', usdc='1', fee='0', nonce=1),
                        delta_for('internal_valid'), delta_for('spot_valid'),
                        dict(type='subAccountTransfer', usdc='1', user=OTHER, destination=body['user']),
                        dict(type='accountClassTransfer', usdc='1', toPerp=True), delta_for('send_valid')]
                else:
                    deltas = [delta_for(cases)]
                if case.startswith('mixed_'):
                    deltas.insert(0, dict(type='deposit', usdc='2'))
                for delta in deltas:
                    if 'destination' in delta:
                        delta['destination'] = body['user']
                payload = [dict(time=STAMP, hash='fixture-flow-' + str(i), delta=delta) for i, delta in enumerate(deltas)]
                if cases == 'extra_row':
                    payload[-1]['partial'] = True
            return httpx.Response(200, json=payload)
        if path.endswith('/income'):
            payload = [dict(tranId=i + 1, incomeType=kind, income='-0.1', asset='USDT', symbol='BTCUSDT', time=STAMP)
                       for i, kind in enumerate(('FUNDING_FEE', 'COMMISSION', 'REALIZED_PNL', 'TRANSFER'))]
        elif path.endswith('/positionRisk'):
            payload = [dict(symbol='BTCUSDT'), dict(symbol='ETHUSDT')]
        elif path.endswith('/userTrades'):
            payload = [dict(symbol=params['symbol'], id=1, orderId=2, side='BUY', positionSide='BOTH',
                time=STAMP, qty='1', price='2', quoteQty='2', realizedPnl='0', commission='0.1', commissionAsset='USDT', buyer=True, maker=False)]
        elif path.endswith('/transfer'):
            payload = dict(total=1, rows=[dict(tranId=list(futures.BINANCE_WALLET_ROUTES).index(params['type']) + 1,
                type=params['type'], asset='USDT', amount='1', timestamp=STAMP, status='CONFIRMED')])
        else:
            row = dict(id='fixture-capital', amount='7', coin='USDT', network='ETH', walletType=0, txId='0x' + 'd' * 64)
            if path.endswith('/deposit/hisrec'):
                row.update(insertTime=STAMP, status=1, confirmTimes='12/12')
                if case == 'wallet_missing':
                    del row['walletType']
                elif case == 'wallet_unknown':
                    row['walletType'] = 2
                elif case == 'wallet_extra':
                    row['futureMetadata'] = 'supplementary'
            else:
                row.update(applyTime='2026-10-11 01:00:00', completeTime='2026-10-11 01:00:00',
                           status=6, transactionFee='0.004', transferType=0)
            payload = [row]
        return httpx.Response(200, json=payload)

    provider_pacing.BINANCE_BUDGET = provider_pacing.RequestBudget()
    bn = futures.BinanceAccountClient('synthetic', KEY, clock=lambda: NOW.timestamp(),
        sleeper=lambda _: None, http=httpx.Client(transport=httpx.MockTransport(response)), max_pages=3)
    hl = futures.HyperliquidAccountClient(ADDRESSES, sleeper=lambda _: None,
        http=httpx.Client(transport=httpx.MockTransport(response)), max_pages=3)
    if hasattr(collector, 'collect_inference'):
        original = collector.collect_inference
        def inference(*args, **kwargs):
            before = snapshot(store, directory)
            if case == 'supplemental_db_locked':
                with sqlite3.connect(store.db_path) as lock:
                    lock.execute('BEGIN IMMEDIATE')
                    timeout, store.busy_timeout = store.busy_timeout, .01
                    try:
                        value = original(*args, **kwargs)
                    finally:
                        store.busy_timeout = timeout
                        lock.rollback()
            elif case == 'supplemental_db_after_insert':
                transaction = store.transaction
                @contextlib.contextmanager
                def failed_commit():
                    with transaction() as conn:
                        yield conn
                        raise sqlite3.OperationalError('synthetic failure before commit')
                store.transaction = failed_commit
                try:
                    value = original(*args, **kwargs)
                finally:
                    store.transaction = transaction
            else:
                value = original(*args, **kwargs)
            assert before == snapshot(store, directory), 'crypto supplemental phase modified ledger facts'
            return value
        collector.collect_inference = inference
    collection = equity(case, trace, extra)
    collector.equity_collector = lambda: collection
    futures.BinanceAccountClient.from_environment = staticmethod(lambda: bn)
    futures.HyperliquidAccountClient.from_environment = staticmethod(lambda: hl)
    collector.LedgerStore = lambda path: store
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = collector.main(['--db', str(store.db_path), '--days', '1'])
    outcome = json.loads(output.getvalue())
    if hasattr(collector, 'collect_inference'):
        collector.collect_inference = original
    after = snapshot(store, directory)
    assert all(row in after[table] for table, rows in captured['tables'].items() for row in rows), 'supplemental phase replaced legacy facts'
    inference = outcome.pop('inference', None)
    toss_inference = outcome.pop('toss_inference', None)
    files = {str(p.relative_to(directory / 'ledger-raw')): p.read_text()
             for p in (directory / 'ledger-raw').rglob('*.json')}
    with store.read() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='crypto_flow_windows'").fetchone()
        strict_windows = conn.execute('SELECT count(*) FROM crypto_flow_windows').fetchone()[0] if exists else 0
    return dict(legacy=outcome, exit_code=exit_code, tables=captured['tables'], strict_windows=strict_windows,
                trace=captured['trace'], raw=files, inference=inference, all_requests=len(trace),
                toss_inference=toss_inference, toss_requests=extra,
                toss_proofs=[row for row in after['ledger_coverage'] if row['stream'].startswith('toss/fills-v1/')])


print(json.dumps({case: run(case) for case in CASES}, sort_keys=True))
