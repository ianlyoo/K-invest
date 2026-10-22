"""Full-tree risk/snapshot replay with synthetic HTTP and blocked sockets."""
import contextlib
import importlib.util
import io
import json
import socket
import sqlite3
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT, OUTPUT, FIXTURES = map(Path, sys.argv[1:4])
CASES = json.loads(sys.argv[4])
sys.path.insert(0, str(ROOT))


def blocked(*args, **kwargs):
    raise AssertionError('Replay network forbidden')


socket.socket.connect = socket.socket.connect_ex = blocked
import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import crypto_futures_accounts as clients  # noqa: E402
import daily_nav  # noqa: E402
import futures_risk as risk  # noqa: E402
import portfolio_store  # noqa: E402
import provider_pacing  # noqa: E402
from portfolio_model import CashBalance, FxRates  # noqa: E402
from portfolio_sources import Collection  # noqa: E402

for module in (clients, daily_nav, risk, portfolio_store, provider_pacing):
    assert Path(module.__file__).resolve().is_relative_to(ROOT.resolve())
DATA = json.loads(FIXTURES.read_text())
NOW = datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)
ADDRESSES = ['0x' + c * 40 for c in ('a', 'b')]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
risk.uuid4 = portfolio_store.uuid4 = lambda: UUID(int=1)
risk.secrets.token_bytes = lambda size: b'R' * size
portfolio_store._now = lambda: NOW.isoformat()


def script(name):
    spec = importlib.util.spec_from_file_location('replay_' + name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def risk_case(case):
    folder = OUTPUT / case
    folder.mkdir(parents=True)
    data, trace, config_reads = deepcopy(DATA['examples']), [], [0]
    if case == 'published_illustration':
        data['account'] = DATA['published_account']
    elif case == 'hl_default':
        data['mode'] = 'default'
    elif case == 'hl_spot_extra':
        data['spot']['partial'] = True
    elif case == 'hl_state_extra':
        data['mode'] = 'default'
        data['state']['partial'] = True
    endpoint, failure = (case.split(':') if ':' in case else ('', ''))
    def handler(request):
        body = json.loads(request.content) if request.content else None
        trace.append([request.method, request.url.path, body])
        if body:
            kind = {'userAbstraction': 'mode', 'perpDexs': 'dexes', 'spotClearinghouseState': 'spot',
                    'clearinghouseState': 'state'}[body['type']]
        else:
            kind = {'/fapi/v3/positionRisk': 'positions', '/fapi/v3/balance': 'balance',
                    '/fapi/v3/account': 'account', '/fapi/v1/accountConfig': 'config'}[request.url.path]
            if kind == 'config':
                config_reads[0] += 1
                if config_reads[0] == 2:
                    kind = 'config_after'
        payload = deepcopy(data['config' if kind == 'config_after' else kind])
        if kind == endpoint:
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout')
            if failure in ('503', '429'):
                return httpx.Response(int(failure), headers={'Retry-After': '60'}, json={})
            if failure == 'missing':
                del payload['positions' if kind == 'account' else 'multiAssetsMargin']
            if failure == 'truncated':
                return httpx.Response(200, content=b'{')
        return httpx.Response(200, json=payload)
    provider_pacing.BINANCE_BUDGET = provider_pacing.RequestBudget()
    bn = clients.BinanceAccountClient('synthetic', KEY, clock=lambda: NOW.timestamp(), sleeper=lambda _: None,
                                     http=httpx.Client(transport=httpx.MockTransport(handler)))
    hl = clients.HyperliquidAccountClient(ADDRESSES, sleeper=lambda _: None,
                                        http=httpx.Client(transport=httpx.MockTransport(handler)))
    store = risk.RiskStore(folder / 'risk.db')
    collector = risk.RiskCollector(store, binance_factory=lambda: bn, hyperliquid_factory=lambda: hl, clock=lambda: NOW)
    saved, output = {}, io.StringIO()
    def capture():
        saved.update(collector.capture())
        return saved
    with contextlib.redirect_stdout(output):
        code = script('snapshot_futures_risk').main([], collector=SimpleNamespace(capture=capture))
    legacy = {k: v for k, v in saved.items() if k not in ('nav_evidence', 'calculation_version')}
    tables = {}
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        for table in ('futures_risk_snapshots', 'futures_risk_venues', 'futures_risk_accounts', 'futures_risk_positions'):
            rows = []
            for record in conn.execute('SELECT * FROM ' + table):
                row = dict(record)
                if table == 'futures_risk_snapshots':
                    row.pop('calculation_version')
                    payload = json.loads(row['payload_json'])
                    payload.pop('nav_evidence', None)
                    payload.pop('calculation_version', None)
                    row['payload_json'] = json.dumps(payload, sort_keys=True)
                rows.append(row)
            tables[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
        nav = daily_nav.crypto_nav(conn, {'valuation_at': NOW.isoformat(), 'fx_times': DATA['fx']})['crypto_nav']
    return dict(legacy=legacy, tables=tables, exit_code=code, job_output=json.loads(output.getvalue()),
                trace=trace, nav=nav, nav_evidence=saved.get('nav_evidence'),
                legacy_client_cooldown=bn._cooldown_until > 0)


def snapshot_case(case):
    folder = OUTPUT / case
    folder.mkdir(parents=True)
    store = portfolio_store.NavStore(folder / 'nav.db')
    calls, output = [], io.StringIO()
    def positions():
        calls.append('positions')
        return Collection(cash=[CashBalance('toss', 'toss:primary', 'KRW', 1000)],
            fetched_at=NOW.isoformat(), source_metadata={'toss:primary': {'complete': True}},
            health={'toss:primary': {'status': 'ok'}})
    def fx(currencies):
        calls.append(['fx', sorted(currencies)])
        return FxRates({'USD': 1400}, as_of=NOW.isoformat(), priced_at_by_currency={'USD': NOW.isoformat()})
    sys.modules['server'] = SimpleNamespace(_get_nav_store=lambda: store,
        _get_collector=lambda: SimpleNamespace(collect_positions=positions, build_fx=fx))
    job = script('snapshot_nav')
    job.apply_env_file = lambda *args: None
    job.validate_live_snapshot_date = lambda value: '2026-10-13'
    if hasattr(job, 'prepare_crypto_snapshot'):
        import crypto_nav_capture as capture
        def prepare(summary, path):
            if case == 'snapshot:serialization':
                summary['crypto_fx_times'] = {'USDT': object()}
                return
            if case in ('snapshot:exception', 'snapshot:mutation_exception'):
                if case.endswith('mutation_exception'):
                    summary.update(total_assets_krw=-999, collection_complete=False, reason_codes=['BAD'])
                raise RuntimeError('synthetic helper exception')
            if case == 'snapshot:invalid_nav':
                # Invalid additive payload; legacy risk rows remain readable.
                return
            def quotes():
                def handler(request):
                    if case == 'snapshot:quote_timeout':
                        raise httpx.ReadTimeout('synthetic')
                    if case == 'snapshot:quote_429':
                        return httpx.Response(429, headers={'Retry-After': '60'})
                    return httpx.Response(200, json=DATA['tickers'] if case != 'snapshot:quote_invalid' else [])
                return capture.StablecoinQuotes(http=httpx.Client(transport=httpx.MockTransport(handler)),
                    clock=lambda: NOW.timestamp(), sleeper=lambda _: None)
            def refresh():
                if case == 'snapshot:capture_exception':
                    raise RuntimeError('synthetic capture failure')
                return {'snapshot_id': 'missing-fixture'}
            return capture.prepare_crypto_snapshot(summary, path, quote_factory=quotes, capture=refresh, clock=lambda: NOW)
        job.prepare_crypto_snapshot = prepare
    if case == 'snapshot:invalid_nav':
        risk.RiskStore(store.db_path).append({'snapshot_id': 'invalid-nav', 'started_at': NOW.isoformat(),
            'completed_at': NOW.isoformat(), 'source_status': 'complete', 'accounts': [], 'positions': [],
            'venues': [], 'nav_evidence': {'version': 'crypto_risk_nav_v1', 'accounts': None, 'venues': None}})
    provider_pacing.UPBIT_QUOTE_BUDGET = provider_pacing.RequestBudget(.11)
    sys.argv = ['snapshot_nav.py']
    with contextlib.redirect_stdout(output):
        code = job.main()
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = {table: [dict(row) for row in conn.execute('SELECT * FROM ' + table)]
                for table in ('nav_snapshots', 'nav_collection_attempts')}
    value = store.get_snapshot('2026-10-13')
    # The integrated Toss provenance is additive and checked separately.
    # Every pre-existing broker field, including FX/mark quality, still matches.
    legacy = deepcopy({k: v for k, v in value.items() if k not in ('crypto_nav', 'consolidated_nav_krw', 'kis_capture')})
    equity_additions = {}
    for account, details in legacy['account_nav'].items():
        if not account.startswith('toss:'):
            continue
        extra = {key: details.pop(key) for key in ('instrument_bindings', 'instrument_binding_reasons') if key in details}
        basis = details['cash_basis_evidence']
        if 'open_order_count' in basis:
            extra['open_order_count'] = basis.pop('open_order_count')
        for currency, cash in details['cash'].items():
            fields = {key: cash.pop(key) for key in ('value_decimal', 'observed_at') if key in cash}
            if fields:
                extra.setdefault('cash', {})[currency] = fields
        if extra:
            equity_additions[account] = extra
    return dict(exit_code=code, tables=rows, legacy=legacy, calls=calls, crypto=value['crypto_nav'],
                equity_additions=equity_additions, kis=value.get('kis_capture'))


print(json.dumps({case: snapshot_case(case) if case.startswith('snapshot:') else risk_case(case)
                  for case in CASES}, sort_keys=True))
