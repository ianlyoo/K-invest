"""Synthetic account reads; throwaway RSA keys and mock transports only."""
import ast
import asyncio
import base64
import json
import logging
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import crypto_futures_accounts as futures
import server

START = 1790812800000  # 2026-10-01 UTC
END = START + 86400000 - 1
ADDRESS = '0x' + 'a' * 36 + '1234'
HIP3_UI_VALUE = Decimal('2145.00')
HIP3_LEGACY_RATIO = Decimal('1.2300')
HIP3_SNAPSHOT_TIME = START + 5 * futures.DAY_MS + (15 * 60 + 7) * 60000  # 10/7 00:07 KST.


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def binance(signing_key, handler, **kwargs):
    return futures.BinanceAccountClient('synthetic-api-key', signing_key,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: START / 1000, sleeper=lambda _: None, **kwargs)


def hyperliquid(handler, addresses=None, **kwargs):
    return futures.HyperliquidAccountClient(addresses or [ADDRESS],
        http=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None, **kwargs)


def test_rsa_signs_exact_encoded_query_and_sends_header(signing_key):
    def handler(request):
        query = request.url.query.decode()
        unsigned, signature = query.rsplit('&signature=', 1)
        signing_key.public_key().verify(base64.b64decode(unquote(signature)),
            unsigned.encode(), padding.PKCS1v15(), hashes.SHA256())
        params = parse_qs(unsigned)
        assert params['timestamp'] == [str(START)]
        assert int(params['recvWindow'][0]) <= 10000
        assert request.headers['X-MBX-APIKEY'] == 'synthetic-api-key'
        return httpx.Response(200, json=[])
    client = binance(signing_key, handler)
    assert client.request('GET', '/fapi/v3/positionRisk') == []
    assert 'synthetic-api-key' not in repr(client)


@pytest.mark.parametrize('method,path', [('POST','/fapi/v3/balance'), ('GET','/fapi/v1/order'),
                                         ('GET','https://evil.invalid/fapi/v3/balance')])
def test_binance_allowlist_precedes_io(signing_key, method, path):
    client = binance(signing_key, lambda r: pytest.fail('Unexpected I/O'))
    with pytest.raises(futures.FuturesError):
        client.request(method, path)


@pytest.mark.parametrize('kind', ['order', 'userLedgerUpdates', 'meta'])
def test_hyperliquid_allowlist_precedes_io(kind):
    client = hyperliquid(lambda r: pytest.fail('Unexpected I/O'))
    with pytest.raises(futures.FuturesError):
        client.info(kind, ADDRESS)


def test_static_module_paths_are_only_allowlisted():
    source = Path(futures.__file__).read_text()
    literals = [n.value for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    paths = {v for v in literals if v.startswith('/') and len(v) > 1}
    assert paths == {'/fapi/v1/time','/fapi/v3/positionRisk','/fapi/v3/balance',
                     '/fapi/v3/account', '/fapi/v1/accountConfig',
                     '/fapi/v1/userTrades','/fapi/v1/income','/info', '/sapi/',
                     '/sapi/v1/asset/transfer', '/sapi/v1/capital/deposit/hisrec',
                     '/sapi/v1/capital/withdraw/history'}
    assert '/exchange' not in source


def test_clock_skew_syncs_public_time_and_retries_once(signing_key):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/fapi/v1/time':
            assert 'X-MBX-APIKEY' not in request.headers
            assert not request.url.query
            return httpx.Response(200, json={'serverTime': START + 4000})
        if len(calls) == 1:
            return httpx.Response(400, json={'code': -1021, 'msg': 'private details'})
        assert parse_qs(request.url.query.decode())['timestamp'] == [str(START + 4000)]
        return httpx.Response(200, json=[])
    assert binance(signing_key, handler).request('GET','/fapi/v3/balance') == []
    assert len(calls) == 3


def test_clock_skew_has_one_retry_only(signing_key):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'serverTime':START}) if request.url.path.endswith('time') else httpx.Response(400,json={'code':-1021})
    with pytest.raises(futures.FuturesError):
        binance(signing_key,handler).request('GET','/fapi/v3/balance')
    assert len(calls) == 3


@pytest.mark.parametrize('status', [429,418])
def test_rate_limits_stop_without_retry_and_keep_retry_after(signing_key,status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status,headers={'Retry-After':'17'},json={'msg':'sensitive'})
    client = binance(signing_key,handler)
    for _ in range(2):
        with pytest.raises(futures.FuturesError) as caught:
            client.request('GET','/fapi/v3/balance')
        assert caught.value.retryable
        assert caught.value.retry_after_seconds == 17
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['status','transport','json','redirect'])
def test_secrets_never_escape_errors_logs_or_repr(signing_key,caplog,failure):
    signatures = []
    def handler(request):
        signatures.append(parse_qs(request.url.query.decode())['signature'][0])
        leak = 'synthetic-api-key ' + signatures[-1] + ' -----BEGIN PRIVATE KEY-----'  # gitleaks:allow (synthetic test value)
        if failure == 'transport':
            raise httpx.ConnectError(leak,request=request)
        if failure == 'json':
            return httpx.Response(200,text=leak)
        if failure == 'redirect':
            return httpx.Response(302,headers={'Location':'https://evil.invalid/?'+leak})
        return httpx.Response(400,json={'msg':leak})
    caplog.set_level(logging.DEBUG)
    client = binance(signing_key,handler)
    with pytest.raises(futures.FuturesError) as caught:
        client.request('GET','/fapi/v3/balance')
    output = repr(client) + repr(caught.value) + str(caught.value) + caplog.text
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    for secret in ['synthetic-api-key','-----BEGIN PRIVATE KEY-----',*signatures]:
        assert secret not in output


def test_credentials_only_files_with_systemd_defaults(signing_key,tmp_path):
    key_file = tmp_path / 'binance-futures-api-key'
    pem_file = tmp_path / 'binance-futures-signing-key'
    key_file.write_text('synthetic-api-key\n')
    pem_file.write_bytes(signing_key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    client = futures.BinanceAccountClient.from_environment({'CREDENTIALS_DIRECTORY':str(tmp_path)})
    assert isinstance(client,futures.BinanceAccountClient)
    assert 'synthetic-api-key' not in repr(client)
    with pytest.raises(futures.FuturesError) as caught:
        futures.BinanceAccountClient.from_environment({'BINANCE_FUTURES_API_KEY':'ignored','BINANCE_FUTURES_SIGNING_KEY':'ignored'})
    assert caught.value.code == 'BINANCE_FUTURES_NOT_CONFIGURED'


@pytest.mark.parametrize('address', ['0x1234', 'xyz', '0x'+'g'*40])
def test_invalid_address_is_not_exposed(address):
    with pytest.raises(futures.FuturesError) as caught:
        futures.HyperliquidAccountClient.from_environment({'HYPERLIQUID_ACCOUNT_ADDRESSES':address})
    assert address not in str(caught.value)


def test_positions_balances_unknowns_and_address_privacy(signing_key):
    def bh(request):
        if request.url.path.endswith('balance'):
            return httpx.Response(200,json=[{'asset':'USDT','balance':'100','crossUnPnl':'5','availableBalance':'80'}])
        return httpx.Response(200,json=[{'symbol':'BTCUSDT','positionAmt':'-2','entryPrice':'10','markPrice':'12','unRealizedProfit':'-4','marginAsset':'USDT'},
                                        {'symbol':'ETHUSDT','positionAmt':'0'}])
    def hh(request):
        assert request.url.path == '/info' and request.method == 'POST'
        body = json.loads(request.content)
        if body['type'] == 'perpDexs':
            assert body == {'type': 'perpDexs'}
            return httpx.Response(200, json=[None])
        assert body['user'] == ADDRESS
        if body['type'] == 'userAbstraction':
            return httpx.Response(200, json='default')
        if body['type'] == 'spotClearinghouseState':
            return httpx.Response(200, json={'balances': []})
        return httpx.Response(200,json={'assetPositions':[{'position':{'coin':'ETH','szi':'2','entryPx':'20','positionValue':'50','unrealizedPnl':'10','leverage':{'type':'isolated','value':3}}}],
                                      'marginSummary':{'accountValue':'100','totalMarginUsed':'10'},'withdrawable':'90'})
    result = futures.FuturesAccounts(lambda:binance(signing_key,bh),lambda:hyperliquid(hh)).query('positions','all')
    assert result['result_status'] == 'complete'
    b,h = result['venues']['binance'],result['venues']['hyperliquid']
    assert len(b['positions']) == 1
    assert b['positions'][0]['side'] == 'short'
    assert b['positions'][0]['leverage'] is None
    assert b['positions'][0]['margin_mode'] is None
    assert b['balances'][0]['wallet_balance_includes_unrealized_pnl'] is False
    assert h['positions'][0]['mark_price'] is None  # not returned by info
    assert h['balances'][0]['equity'] == '100'
    assert h['balances'][0]['equity_includes_unrealized_pnl'] is True
    assert ADDRESS not in json.dumps(result)
    assert h['positions'][0]['account_id'].startswith('acct_')
    assert h['positions'][0]['account_last4'] == '1234'


def hip3_states():
    """Synthetic unified-account states; PnL consistent with the legacy ratio.

    Values, sizes, PnL allocation, entry prices, leverage and split of held
    margin are all synthetic. Margin summaries satisfy equity = raw
    USD + PnL; totalMarginUsed sums to spot hold, not to totalRawUsd.
    """
    def state(value, positions):
        rows, pnl_total, margin_total, notional_total = [], Decimal(0), Decimal(0), Decimal(0)
        for coin, size, pnl, entry, margin in positions:
            notional = Decimal(size) * Decimal(entry) + Decimal(pnl)
            rows.append({'type': 'oneWay', 'position': {
                'coin': coin, 'szi': size, 'entryPx': entry, 'unrealizedPnl': pnl,
                'positionValue': str(notional), 'marginUsed': margin,
                'leverage': {'type': 'isolated', 'value': 3}}})
            pnl_total += Decimal(pnl)
            margin_total += Decimal(margin)
            notional_total += notional
        return {'assetPositions': rows,
                'marginSummary': {'accountValue': value, 'totalMarginUsed': str(margin_total),
                    'totalRawUsd': str(Decimal(value) - pnl_total), 'totalNtlPos': str(notional_total)},
                'crossMarginSummary': {'accountValue': '0', 'totalMarginUsed': '0',
                    'totalRawUsd': '0', 'totalNtlPos': '0'},
                'crossMaintenanceMarginUsed': '0',
                'withdrawable': str(Decimal(value) - margin_total), 'time': HIP3_SNAPSHOT_TIME}
    return {'': state('0', []),
            'xyz': state('1361.52', [('xyz:SYNA', '2.5', '310', '1000', '1000'),
                                     ('xyz:SYNB', '6.2', '120', '200', '354.29')]),
            'para': state('250.00', [('para:SYNC', '5.1', '57.98', '100', '250.00')])}


def hip3_info_fixture():
    """Public-info response shapes with supplied balances and inferred PnL.

    Aggregate PnL = UI * legacy ratio - spot total = 487.98.
    This is one consistent synthetic allocation, not a live capture.
    """
    return {'userAbstraction': 'unifiedAccount',
            'perpDexs': [None, {'name': 'xyz'}, {'name': 'para'}],
            'spotClearinghouseState': {'balances': [
                {'coin': 'USDC', 'token': 0, 'total': '2150.37', 'hold': '1604.29', 'entryNtl': '0'}]},
            'clearinghouseState': hip3_states()}


def hip3_client(*, mode='unifiedAccount', states=None, dexes=None, spot=None, fail=None, **kwargs):
    fixture = hip3_info_fixture()
    states = fixture['clearinghouseState'] if states is None else states
    dexes = fixture['perpDexs'] if dexes is None else dexes
    spot = fixture['spotClearinghouseState'] if spot is None else spot
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        kind = body['type']
        if kind == 'perpDexs':
            assert body == {'type': 'perpDexs'}
        else:
            assert body['user'] == ADDRESS
        if fail and fail(body):
            return httpx.Response(503, json={'private': ADDRESS})
        payload = (states[body.get('dex', '')] if kind == 'clearinghouseState' else
                   {'userAbstraction': mode, 'perpDexs': dexes, 'spotClearinghouseState': spot}[kind])
        return httpx.Response(200, json=payload)

    return hyperliquid(handler, **kwargs), calls


def hl_positions(client):
    return futures.FuturesAccounts(None, lambda: client).query('positions', 'hyperliquid')['venues']['hyperliquid']


def test_unified_hip3_value_and_native_positions():
    client, calls = hip3_client()
    h = hl_positions(client)
    assert h['result_status'] == 'complete' and h['coverage']['fetch_complete']
    assert len(calls) == h['coverage']['pages_fetched'] == 6
    assert {(p['dex'], p['symbol'], p['signed_size']) for p in h['positions']} == {
        ('xyz', 'xyz:SYNA', '2.5'), ('xyz', 'xyz:SYNB', '6.2'), ('para', 'para:SYNC', '5.1')}
    assert {b['dex'] for b in h['balances']} == {'', 'xyz', 'para'}
    assert all(not b['equity_usable_for_aggregation'] for b in h['balances'] if b['balance_scope'] == 'perpetual')
    value = h['account_values'][0]
    assert value['value'] == h['totals_by_asset']['USDC']['value'] == '2150.37'
    assert value['status'] == 'complete'
    assert value['provenance']['spot_USDC_total'] == '2150.37'
    assert value['provenance']['position_unrealized_pnl'] == '487.98'
    assert value['provenance']['formula'] == 'spot_USDC_total'
    assert value['provenance']['scope'] == h['totals_by_asset']['USDC']['scope'] == 'USDC_shared_balance'
    assert value['provenance']['unrealized_pnl_in_spot_total'] == 'unverified'
    assert value['ui_portfolio_value_status'] == h['totals_by_asset']['USDC']['ui_portfolio_value_status'] == 'unverified'
    old_value = Decimal(value['provenance']['spot_USDC_total']) + Decimal(value['provenance']['position_unrealized_pnl'])
    assert old_value == Decimal('2638.35')
    assert old_value / HIP3_UI_VALUE == HIP3_LEGACY_RATIO
    assert (Decimal(value['value']) / HIP3_UI_VALUE).quantize(Decimal('0.00000001')) == Decimal('1.00250350')
    assert abs(Decimal(value['value']) / HIP3_UI_VALUE - 1) < Decimal('0.01')
    assert {b['dex']: b['equity'] for b in h['balances'] if b['balance_scope'] == 'perpetual'} == {
        '': '0', 'xyz': '1361.52', 'para': '250.00'}
    spot_balance = next(b for b in h['balances'] if b['balance_scope'] == 'spot')
    assert spot_balance['wallet_balance_includes_unrealized_pnl'] is None
    assert spot_balance['equity_includes_unrealized_pnl'] is None
    assert value['provenance']['dex_account_value_sum'] == '1611.52'
    assert value['provenance']['dexes_fetched'] == ['', 'xyz', 'para']
    assert ADDRESS not in json.dumps(h)


def test_unified_fixture_reconciles_margin_and_equity_components():
    fixture = hip3_info_fixture()
    states = fixture['clearinghouseState']
    assert fixture['userAbstraction'] == 'unifiedAccount'
    assert fixture['perpDexs'] == [None, {'name': 'xyz'}, {'name': 'para'}]
    assert states['']['assetPositions'] == [] and states['']['marginSummary']['accountValue'] == '0'
    assert {dex: state['marginSummary']['accountValue'] for dex, state in states.items()} == {
        '': '0', 'xyz': '1361.52', 'para': '250.00'}
    total_pnl = Decimal(0)
    for state in states.values():
        assert state['time'] == 1791299220000
        pnl = sum((Decimal(row['position']['unrealizedPnl']) for row in state['assetPositions']), Decimal(0))
        summary = state['marginSummary']
        assert Decimal(summary['totalRawUsd']) + pnl == Decimal(summary['accountValue'])
        total_pnl += pnl
    spot = fixture['spotClearinghouseState']['balances'][0]
    assert Decimal(spot['total']) == Decimal('2150.37')
    assert sum((Decimal(s['marginSummary']['totalMarginUsed']) for s in states.values()), Decimal(0)) == Decimal(spot['hold'])
    assert total_pnl == HIP3_UI_VALUE * HIP3_LEGACY_RATIO - Decimal(spot['total']) == Decimal('487.98')


@pytest.mark.parametrize('dex', ['', 'xyz', 'para'])
def test_any_dex_failure_preserves_other_rows_and_marks_total_partial(dex):
    client, calls = hip3_client(fail=lambda body: body['type'] == 'clearinghouseState' and body['dex'] == dex)
    h = hl_positions(client)
    assert len(calls) == 6
    assert h['result_status'] == h['account_values'][0]['status'] == h['totals_by_asset']['USDC']['status'] == 'partial'
    assert not h['coverage']['fetch_complete']
    assert 'HYPERLIQUID_DEX_READ_FAILED' in h['account_values'][0]['reason_codes']
    assert h['totals_by_asset']['USDC']['value'] is None
    assert h['account_values'][0]['provenance']['dexes_failed'] == [dex]
    assert all(p['dex'] != dex for p in h['positions'])
    assert any(b['balance_scope'] == 'spot' for b in h['balances'])
    assert ADDRESS not in json.dumps(h)


@pytest.mark.parametrize('pnl', ['680', '-680', '0', None, 'NaN'])
def test_unified_shared_balance_does_not_add_or_require_position_pnl(pnl):
    states = hip3_states()
    states['xyz']['assetPositions'][0]['position']['unrealizedPnl'] = pnl
    states['xyz']['marginSummary']['accountValue'] = '999999'
    states['para']['marginSummary']['accountValue'] = '888888'
    client, _ = hip3_client(states=states)
    h = hl_positions(client)
    assert h['account_values'][0]['value'] == h['totals_by_asset']['USDC']['value'] == '2150.37'
    assert h['result_status'] == 'complete' and h['coverage']['fetch_complete']
    assert h['account_values'][0]['ui_portfolio_value_status'] == 'unverified'


@pytest.mark.parametrize('hold', ['0', '1604.29', '2150.37'])
def test_unified_hold_is_not_an_additional_asset_or_equity_deduction(hold):
    client, _ = hip3_client(spot={'balances': [{'coin': 'USDC', 'total': '2150.37', 'hold': hold}]})
    h = hl_positions(client)
    assert h['totals_by_asset']['USDC']['value'] == '2150.37'
    assert next(b for b in h['balances'] if b['balance_scope'] == 'spot')['available_balance'] == str(
        Decimal('2150.37') - Decimal(hold))


@pytest.mark.parametrize('mode', ['default', 'disabled'])
def test_standard_separate_wallets_keep_dex_equity_without_readding_pnl(mode):
    states = hip3_states()
    states['']['marginSummary']['accountValue'] = '100'
    states['xyz']['assetPositions'][0]['position'].pop('unrealizedPnl')
    client, _ = hip3_client(mode=mode, states=states)
    h = hl_positions(client)
    assert h['result_status'] == 'complete'
    assert h['account_values'][0]['value'] == '3861.89'  # 2150.37 + 100 + 1361.52 + 250.00.
    assert all(b['equity_usable_for_aggregation'] for b in h['balances'])
    assert next(b for b in h['balances'] if b['balance_scope'] == 'perpetual' and b['dex'] == '')['equity'] == '100'


@pytest.mark.parametrize('budget', [1, 3, 4, 5])
def test_position_reads_share_existing_request_budget(budget):
    client, calls = hip3_client(max_pages=budget)
    h = hl_positions(client)
    assert len(calls) == h['coverage']['pages_fetched'] == budget
    assert 'PAGE_BUDGET' in h['reason_codes']
    assert h['account_values'][0]['value'] is None and h['totals_by_asset']['USDC']['status'] == 'partial'


def test_dex_discovery_has_a_hard_cap_and_deduplicates_default():
    dexes = [None] + [{'name': 'd' + str(i)} for i in range(20)]
    states = {'': hip3_states()['']}
    states.update({d['name']: states[''] for d in dexes if d})
    client, calls = hip3_client(dexes=dexes, states=states)
    h = hl_positions(client)
    requested = [b['dex'] for b in calls if b['type'] == 'clearinghouseState']
    assert len(requested) == 1 + futures.HYPERLIQUID_MAX_NAMED_DEXES
    assert requested.count('') == 1
    assert h['totals_by_asset']['USDC']['value'] is None
    assert 'HYPERLIQUID_DEX_LIMIT' in h['account_values'][0]['reason_codes']
    client, _ = hip3_client(dexes=[None, None, {'name': 'xyz'}, {'name': 'xyz'}])
    assert hl_positions(client)['account_values'][0]['provenance']['dexes_fetched'] == ['', 'xyz']


@pytest.mark.parametrize('missing', ['spot_total', 'spot_failure', 'discovery_failure', 'mode_failure', 'bad_position'])
def test_unknown_value_inputs_never_become_zero_or_complete(missing):
    states = hip3_states()
    spot = {'balances': [{'coin': 'USDC', 'total': '2150.37', 'hold': '1604.29'}]}
    if missing == 'spot_total':
        spot['balances'][0].pop('total')
    elif missing == 'bad_position':
        states['xyz']['assetPositions'].append({})
    failures = {'spot_failure': 'spotClearinghouseState', 'discovery_failure': 'perpDexs',
                'mode_failure': 'userAbstraction'}
    client, _ = hip3_client(states=states, spot=spot, fail=lambda body: body['type'] == failures.get(missing))
    h = hl_positions(client)
    assert h['account_values'][0]['value'] is None
    assert h['account_values'][0]['status'] == h['totals_by_asset']['USDC']['status'] == 'partial'
    assert 'HYPERLIQUID_MISSING_ACCOUNT_VALUE_INPUT' in h['reason_codes']


@pytest.mark.parametrize('mode', ['portfolioMargin', 'dexAbstraction', 'unknown'])
def test_unsupported_or_unknown_abstraction_preserves_reads_without_false_total(mode):
    client, _ = hip3_client(mode=mode)
    h = hl_positions(client)
    assert len(h['positions']) == 3
    assert h['account_values'][0]['value'] is None
    assert h['result_status'] == 'partial'


@pytest.mark.parametrize('kind,params', [
    ('clearinghouseState', {'startTime': START}), ('clearinghouseState', {'endTime': END}),
    ('userFillsByTime', {'dex': 'xyz'}), ('userFunding', {'dex': 'para'}),
    ('spotClearinghouseState', {'dex': ''}), ('userAbstraction', {'startTime': START}),
    ('perpDexs', {'endTime': END}), ('clearinghouseState', {'dex': 1}),
    ('clearinghouseState', {'dex': ADDRESS}), ('clearinghouseState', {'dex': '../xyz'}),
    ('userFunding', {'startTime': True}), ('userFunding', {'startTime': END, 'endTime': START}),
    ('userFunding', {'type': 'perpDexs'}), ('userFunding', {'aggregateByTime': True}),
])
def test_hyperliquid_exact_type_param_guard_blocks_io(kind, params):
    client = hyperliquid(lambda r: pytest.fail('Unexpected I/O'))
    with pytest.raises(futures.FuturesError) as caught:
        client.info(kind, ADDRESS, **params)
    assert caught.value.code == 'FUTURES_READ_NOT_ALLOWED'


@pytest.mark.parametrize('body', [
    {'type': 'userFunding', 'user': ADDRESS, 'dex': 'xyz'},
    {'type': 'clearinghouseState', 'user': ADDRESS, 'startTime': START},
    {'type': 'perpDexs', 'user': ADDRESS}, {'type': 'userAbstraction', 'user': '0x' + 'b' * 40},
    {'type': 'spotClearinghouseState', 'user': ADDRESS, 'extra': True}, {'type': []},
])
def test_transport_cannot_bypass_hyperliquid_body_guard(body):
    client = hyperliquid(lambda r: pytest.fail('Unexpected I/O'))
    with pytest.raises(futures.FuturesError):
        client._send('POST', '/info', body=body, weight=2)


@pytest.mark.parametrize('operation,coin', [('fills', 'xyz:SYNA'), ('funding', 'para:SYNC')])
def test_hip3_history_preserves_native_coin_without_dex_request(operation, coin):
    def handler(request):
        body = json.loads(request.content)
        assert set(body) == {'type', 'user', 'startTime', 'endTime'}
        row = ({'coin': coin, 'tid': 42, 'time': START, 'sz': '1'} if operation == 'fills' else
               {'delta': {'coin': coin, 'usdc': '-0.2'}, 'hash': 'synthetic-hip3', 'time': START})
        return httpx.Response(200, json=[row])
    h = futures.FuturesAccounts(None, lambda: hyperliquid(handler)).query(
        operation, 'hyperliquid', '2026-10-01', '2026-10-01', coin)['venues']['hyperliquid']
    rows = h['fills' if operation == 'fills' else 'funding_payments']
    assert len(rows) == 1 and rows[0]['symbol'] == coin and rows[0]['dex'] == coin.split(':')[0]


@pytest.mark.parametrize('dexes', [[None, {'name': '../bad'}], [None, {}], {}])
def test_invalid_discovery_never_certifies_a_default_only_total(dexes):
    client, _ = hip3_client(dexes=dexes)
    h = hl_positions(client)
    assert h['account_values'][0]['value'] is None
    assert 'HYPERLIQUID_DEX_DISCOVERY_INCOMPLETE' in h['reason_codes']


def test_non_usdc_spot_tokens_remain_native_and_outside_usdc_value():
    client, _ = hip3_client(spot={'balances': [
        {'coin': 'USDC', 'total': '2150.37', 'hold': '1604.29'},
        {'coin': 'HYPE', 'total': '20', 'hold': '2'}]})
    h = hl_positions(client)
    assert h['account_values'][0]['value'] == '2150.37'
    balance = next(b for b in h['balances'] if b['asset'] == 'HYPE')
    assert balance['wallet_balance'] == '20' and balance['available_balance'] == '18'
    assert balance['equity'] is None and not balance['equity_usable_for_aggregation']


def test_duplicate_positions_do_not_double_count_pnl():
    states = hip3_states()
    states['xyz']['assetPositions'].append(states['xyz']['assetPositions'][0])
    client, _ = hip3_client(states=states)
    h = hl_positions(client)
    assert len(h['positions']) == 3
    assert h['account_values'][0]['value'] is None and h['result_status'] == 'partial'


def test_multiple_address_values_and_budget_are_account_scoped():
    addresses = [ADDRESS, '0x' + 'b' * 36 + '5678']
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body['type'] == 'perpDexs':
            payload = [None]
        elif body['type'] == 'userAbstraction':
            payload = 'unifiedAccount'
        elif body['type'] == 'spotClearinghouseState':
            payload = {'balances': [{'coin': 'USDC', 'total': '100', 'hold': '10'}]}
        else:
            payload = {'assetPositions': [{'position': {'coin': 'ETH', 'szi': '1', 'unrealizedPnl': '-1'}}],
                       'marginSummary': {'accountValue': '10'}}
        return httpx.Response(200, json=payload)
    h = hl_positions(hyperliquid(handler, addresses=addresses))
    assert h['totals_by_asset']['USDC']['value'] == '200'
    assert len({p['account_id'] for p in h['positions']}) == len(h['account_values']) == 2
    assert all(row['value'] == '100' for row in h['account_values'])
    calls.clear()
    h = hl_positions(hyperliquid(handler, addresses=addresses, max_pages=5))
    assert len(calls) == 5
    assert h['account_values'][0]['status'] == 'complete'
    assert h['account_values'][1]['status'] == 'partial'
    assert h['totals_by_asset']['USDC']['value'] is None


def test_missing_venue_does_not_block_other_venue():
    def missing():
        raise futures.FuturesError('BINANCE_FUTURES_NOT_CONFIGURED')
    result = futures.FuturesAccounts(missing,lambda:hyperliquid(lambda r:httpx.Response(200,json=[]))).query('funding','all','2026-10-01','2026-10-01')
    assert result['result_status'] == 'partial'
    assert result['venues']['binance']['source_status'] == 'not_configured'
    assert result['venues']['hyperliquid']['source_status'] == 'empty'
    assert result['venues']['binance']['totals_by_asset'] is None


def test_time_windows_and_dense_same_millisecond_are_never_silently_dropped(signing_key):
    calls = []
    def handler(request):
        params = parse_qs(request.url.query.decode())
        lo,hi = int(params['startTime'][0]),int(params['endTime'][0])
        calls.append((lo,hi))
        assert hi-lo < 7*86400000
        rows = [{'id':i,'symbol':'BTCUSDT','time':START,'qty':'1','commission':'0.1','commissionAsset':'USDT'} for i in range(1000)] if lo <= START <= hi else []
        return httpx.Response(200,json=rows)
    client = binance(signing_key,handler,max_pages=40)
    result = futures.FuturesAccounts(lambda:client,None).query('fills','binance','2026-10-01','2026-10-09','BTCUSDT')
    venue = result['venues']['binance']
    assert venue['source_status'] == 'truncated'
    assert len(venue['fills']) == 1000
    assert venue['coverage']['pages_fetched'] == len(calls)
    assert 'DENSE_TIMESTAMP' in venue['reason_codes']


def test_hyperliquid_pages_fills_and_filters_spot_and_symbol():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        lo,hi = body['startTime'],body['endTime']
        if hi-lo > 100:
            rows = [{'tid':i,'coin':'BTC','time':lo,'sz':'1','px':'10','fee':'0.01','feeToken':'USDC'} for i in range(2000)]
        else:
            rows = [{'tid':lo,'coin':'BTC','time':lo,'sz':'1','px':'10','fee':'0.01','feeToken':'USDC'},
                    {'tid':lo+1,'coin':'@1','time':lo,'sz':'1'}, {'tid':lo+2,'coin':'ETH','time':lo,'sz':'1'}]
        return httpx.Response(200,json=rows)
    client = hyperliquid(handler,max_pages=5)
    result = futures.FuturesAccounts(None,lambda:client).query('fills','hyperliquid','2026-10-01','2026-10-01','BTC')
    venue = result['venues']['hyperliquid']
    assert venue['source_status'] == 'truncated'
    assert len(calls) == 5
    assert all(r['symbol']=='BTC' for r in venue['fills'])
    assert all(r['fee_asset']=='USDC' for r in venue['fills'])


def test_inferred_binance_symbols_disclose_partial_coverage(signing_key):
    def handler(request):
        if request.url.path.endswith('income'):
            return httpx.Response(200,json=[{'symbol':'BTCUSDT','time':START,'tranId':1,'income':'-1','asset':'USDT'}])
        if request.url.path.endswith('positionRisk'):
            return httpx.Response(200,json=[{'symbol':'ETHUSDT','positionAmt':'1'}])
        return httpx.Response(200,json=[{'id':1,'symbol':parse_qs(request.url.query.decode())['symbol'][0],'time':START,'qty':'1'}])
    result = futures.FuturesAccounts(lambda:binance(signing_key,handler),None).query('fills','binance','2026-10-01','2026-10-01')
    b = result['venues']['binance']
    assert {r['symbol'] for r in b['fills']} == {'BTCUSDT','ETHUSDT'}
    assert b['coverage']['symbol_selection'] == 'income_history_and_current_positions'
    assert b['result_status'] == 'partial'


def test_signed_funding_totals_by_asset_and_null_unknown(signing_key):
    def handler(request):
        return httpx.Response(200,json=[{'tranId':1,'time':START,'symbol':'BTCUSDT','asset':'USDT','income':'-1.25'},
                                      {'tranId':2,'time':START+1,'symbol':'BTCUSDT','asset':'USDT','income':'0.75'},
                                      {'tranId':3,'time':START+2,'symbol':'ETHUSDC','asset':'USDC','income':None}])
    b = futures.FuturesAccounts(lambda:binance(signing_key,handler),None).query('funding','binance','2026-10-01','2026-10-01')['venues']['binance']
    assert b['totals_by_asset']['USDT']['value'] == '-0.50'
    assert b['totals_by_asset']['USDC']['value'] is None
    assert b['result_status'] == 'partial'


def test_hyperliquid_funding_signed_and_multi_account_ids():
    addresses = [ADDRESS,'0x'+'b'*40]
    def handler(request):
        return httpx.Response(200,json=[{'hash':'synthetic-hash','time':START,'delta':{'coin':'ETH','usdc':'-2.5'}}])
    client = hyperliquid(handler,addresses)
    result = futures.FuturesAccounts(None,lambda:client).query('funding','hyperliquid','2026-10-01','2026-10-01')
    h = result['venues']['hyperliquid']
    assert h['totals_by_asset']['USDC']['value'] == '-5.0'
    assert len({r['account_id'] for r in h['funding_payments']}) == 2
    assert all(a not in json.dumps(result) for a in addresses)


@pytest.mark.parametrize('start,end', [('bad','2026-10-01'),('2026-10-02','2026-10-01')])
def test_bad_dates_return_envelope_before_io(start,end):
    result = server.get_crypto_futures_fills('all',start,end)
    assert result['ok'] is False and result['error']['code'] == 'INVALID_FUTURES_QUERY'


def test_new_tool_catalog_annotations_help_and_existing_raw_contract():
    tools = {t.name:t for t in asyncio.run(server.mcp.list_tools())}
    baseline = json.loads((Path(__file__).parent/'fixtures/baseline_tool_catalog.json').read_text())
    names = {'get_crypto_futures_positions','get_crypto_futures_fills','get_crypto_futures_funding_payments'}
    assert names <= set(tools)  # exact catalog pinned in test_tool_removal
    for name in names:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.openWorldHint is True
    for name,original in baseline['raw_tools'].items():
        current = tools[name].model_dump(mode='json', exclude_none=True)
        # 2.14.0 intentionally changes hints and appends the ledger disclosure;
        # argument/output schemas and every other description byte stay pinned.
        current.pop('annotations', None)
        current['description'] = current['description'].replace('\n    Fetched broker rows are recorded in the local append-only ledger (no broker writes).', '')
        assert current == {k: v for k, v in original.items() if k != 'annotations'}
    help_data = server.get_invest_mcp_help('futures_account')['data']
    # The registry also carries the merged account tools (tools_v2).
    assert {r['name'] for r in help_data['tool_contracts']} == names | {'get_crypto_positions', 'get_crypto_account_history'}
    description = tools['get_crypto_futures_funding_payments'].description
    assert 'get_binance_funding_rate' in description and 'paid or received' in description


def test_transport_itself_rejects_unknown_paths(signing_key):
    for client in (binance(signing_key,lambda r:pytest.fail('I/O')),
                   hyperliquid(lambda r:pytest.fail('I/O'))):
        with pytest.raises(futures.FuturesError):
            client._send('GET','/unsafe',weight=1)


def test_hyperliquid_normalizes_and_deduplicates_addresses():
    client = futures.HyperliquidAccountClient.from_environment({
        'HYPERLIQUID_ACCOUNT_ADDRESSES':ADDRESS.upper().replace('0X','0x')+','+ADDRESS})
    assert client._addresses == [ADDRESS]
    assert ADDRESS not in repr(client)


def test_unknown_row_ids_remain_partial_and_do_not_collide(signing_key):
    client = binance(signing_key, lambda r:httpx.Response(200,json=[
        {'symbol':'BTCUSDT','time':START,'qty':'1'},
        {'symbol':'BTCUSDT','time':START+1,'qty':'2'}]))
    b = futures.FuturesAccounts(lambda:client,None).query('fills','binance','2026-10-01','2026-10-01','BTCUSDT')['venues']['binance']
    assert len(b['fills']) == 2
    assert 'MISSING_ROW_ID' in b['reason_codes']
    assert b['result_status'] == 'partial'


def test_binance_retention_never_claims_historical_completeness(signing_key):
    client = binance(signing_key,lambda r:httpx.Response(200,json=[]))
    b = futures.FuturesAccounts(lambda:client,None).query('funding','binance','2026-01-01','2026-01-01')['venues']['binance']
    assert b['source_status'] == 'truncated'
    assert 'RETENTION_LIMIT' in b['reason_codes']
    assert b['result_status'] == 'partial'


def test_hyperliquid_history_retention_is_explicit():
    client = hyperliquid(lambda r:httpx.Response(200,json=[]))
    h = futures.FuturesAccounts(None,lambda:client).query('fills','hyperliquid','2026-10-01','2026-10-01')['venues']['hyperliquid']
    assert h['coverage']['history_limit'] == 'latest_10000_fills'
    assert h['coverage']['fetch_complete'] is True
    assert h['result_status'] == 'partial'


def test_positions_preserve_successful_rows_after_balance_failure(signing_key):
    def handler(request):
        if request.url.path.endswith('balance'):
            return httpx.Response(503,json={'msg':'synthetic-api-key'})
        return httpx.Response(200,json=[{'symbol':'BTCUSDT','positionAmt':'1'}])
    client = binance(signing_key,handler)
    b = futures.FuturesAccounts(lambda:client,None).query('positions','binance')['venues']['binance']
    assert len(b['positions']) == 1
    assert b['source_status'] == 'failed' and b['result_status'] == 'partial'
    assert b['balances'] == []
    assert b['positions'][0]['field_status']['leverage']['status'] == 'unavailable'
    assert 'synthetic-api-key' not in json.dumps(b)


def test_binance_non_dense_history_is_time_paged_without_gaps(signing_key):
    windows = []
    def handler(request):
        params = parse_qs(request.url.query.decode())
        lo,hi = int(params['startTime'][0]),int(params['endTime'][0])
        windows.append((lo,hi))
        return httpx.Response(200,json=[{'id':lo,'symbol':'BTCUSDT','time':lo,'qty':'1',
                                       'commission':'0.1','commissionAsset':'USDT'}])
    b = futures.FuturesAccounts(lambda:binance(signing_key,handler),None).query('fills','binance','2026-10-01','2026-10-09','BTCUSDT')['venues']['binance']
    assert len(windows) == 2 and windows[0][1]+1 == windows[1][0]
    assert b['coverage']['pages_fetched'] == 2 and b['result_status'] == 'complete'
    assert b['coverage']['observed_window']['start_time_ms'] == START


def test_hyperliquid_failure_does_not_hide_binance_funding(signing_key,monkeypatch):
    def hh(request):
        raise httpx.ConnectError(ADDRESS,request=request)
    engine = futures.FuturesAccounts(lambda:binance(signing_key,lambda r:httpx.Response(200,json=[])),lambda:hyperliquid(hh))
    monkeypatch.setattr(server,'_crypto_futures_accounts',engine)
    result = server.get_crypto_futures_funding_payments('all','2026-10-01','2026-10-01')
    assert result['ok'] and result['meta']['result_status'] == 'partial'
    assert result['data']['source_status'] == {'binance':'empty','hyperliquid':'failed'}
    assert ADDRESS not in json.dumps(result)
