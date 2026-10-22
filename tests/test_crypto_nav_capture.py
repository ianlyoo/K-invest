"""Provider-example-shaped fixtures through real clients, with mocked HTTP only.

Examples: Binance balance-v3 / positionRisk-v3, HL perpetuals / spot docs,
Upbit list-tickers. References and access date: docs/crypto-nav-flows.md.
All account identifiers and financial scenarios below are synthetic.
"""
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from crypto_futures_accounts import BinanceAccountClient, HyperliquidAccountClient
from crypto_nav_capture import StablecoinQuotes, prepare_crypto_snapshot
from crypto_validation import CryptoValidationError
from daily_nav import crypto_nav
from futures_risk import RiskCollector, RiskStore
from portfolio_ledger import LedgerStore

NOW = datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)
ADDRESS = '0x' + 'a' * 40


def account_example():
    # V3's published complete field shape; coherent synthetic USDT amounts.
    return {'totalInitialMargin': '1', 'totalMaintMargin': '0.05',
        'totalWalletBalance': '100.00000000', 'totalUnrealizedProfit': '10.00000000',
        'totalMarginBalance': '110.00000000', 'totalPositionInitialMargin': '1',
        'totalOpenOrderInitialMargin': '0', 'totalCrossWalletBalance': '100.00000000',
        'totalCrossUnPnl': '10', 'availableBalance': '1', 'maxWithdrawAmount': '1',
        'assets': [{'asset': 'USDT', 'walletBalance': '100.00000000', 'unrealizedProfit': '10.00000000',
            'marginBalance': '110.00000000', 'maintMargin': '0.05', 'initialMargin': '1',
            'positionInitialMargin': '1', 'openOrderInitialMargin': '0', 'crossWalletBalance': '100.00000000',
            'crossUnPnl': '10', 'availableBalance': '1', 'maxWithdrawAmount': '1', 'updateTime': MS}],
        'positions': [{'symbol': 'ADAUSDT', 'positionSide': 'BOTH', 'positionAmt': '30',
            'unrealizedProfit': '10', 'isolatedMargin': '0', 'notional': '12.31427700',
            'isolatedWallet': '0', 'initialMargin': '1', 'maintMargin': '0.05', 'updateTime': MS}]}


def account_config_example():
    return {'feeTier': 0, 'canTrade': True, 'canDeposit': True, 'canWithdraw': True,
            'dualSidePosition': False, 'updateTime': 0, 'multiAssetsMargin': False, 'tradeGroupId': -1}


def zero_asset(asset='BTC', wallet='0'):
    row = {k: '0' for k in account_example()['assets'][0]}
    row.update(asset=asset, walletBalance=wallet, crossWalletBalance=wallet, marginBalance=wallet, updateTime=0)
    return row


def examples():
    # Published field shapes; values adjusted to one reconciled synthetic book.
    balance = [{'accountAlias': 'fixture', 'asset': 'USDT', 'balance': '100.00000000',
        'crossWalletBalance': '100.00000000', 'crossUnPnl': '10.00000000',
        'availableBalance': '1.00000000', 'maxWithdrawAmount': '1.00000000',
        'marginAvailable': True, 'updateTime': MS}]
    positions = [{'symbol': 'ADAUSDT', 'positionSide': 'BOTH', 'positionAmt': '30',
        'entryPrice': '0.385', 'breakEvenPrice': '0.385077', 'markPrice': '0.41047590',
        'unRealizedProfit': '10', 'liquidationPrice': '0', 'isolatedMargin': '0',
        'notional': '12.31427700', 'marginAsset': 'USDT', 'isolatedWallet': '0',
        'initialMargin': '1', 'maintMargin': '0.05', 'positionInitialMargin': '1',
        'openOrderInitialMargin': '0', 'adl': 2, 'bidNotional': '0', 'askNotional': '0',
        'updateTime': MS}]
    state = {'assetPositions': [{'type': 'oneWay', 'position': {
        'coin': 'ETH', 'entryPx': '2986.3', 'leverage': {'type': 'isolated', 'value': 20, 'rawUsd': '-95.059824'},
        'liquidationPx': '2866.26936529', 'marginUsed': '4.967826', 'maxLeverage': 50,
        'positionValue': '100.02765', 'returnOnEquity': '-0.0026789', 'szi': '0.0335',
        'unrealizedPnl': '-0.0134'}}],
        'marginSummary': {'accountValue': '13109.482328', 'totalMarginUsed': '4.967826',
                          'totalNtlPos': '100.02765', 'totalRawUsd': '13009.454678'},
        'crossMarginSummary': {'accountValue': '13104.514502', 'totalMarginUsed': '0',
                              'totalNtlPos': '0', 'totalRawUsd': '13104.514502'},
        'crossMaintenanceMarginUsed': '0', 'withdrawable': '13104.514502', 'time': MS}
    return {'balance': balance, 'positions': positions, 'account': account_example(),
        'config': account_config_example(), 'state': state,
        'spot': {'balances': [{'coin': 'USDC', 'token': 0, 'total': '200', 'hold': '180', 'entryNtl': '0'}]},
        'mode': 'unifiedAccount', 'dexes': [None]}


def tickers(stamp=MS):
    return [{'market': 'KRW-' + coin, 'trade_price': price, 'trade_timestamp': stamp,
             'timestamp': stamp, 'trade_date': '20261013', 'trade_time': '091000'}
            for coin, price in (('USDT', 1400), ('USDC', 1390))]


def quote_client(payload=None, status=200):
    def handler(request):
        assert request.method == 'GET' and request.url.host == 'api.upbit.com'
        assert request.url.path == '/v1/ticker'
        assert request.url.params['markets'] == 'KRW-USDT,KRW-USDC'
        assert 'authorization' not in request.headers
        return httpx.Response(status, json=tickers() if payload is None else payload)
    return StablecoinQuotes(http=httpx.Client(transport=httpx.MockTransport(handler)),
                            clock=lambda: MS / 1000, sleeper=lambda _: None)


def capture(path, data=None, clock=NOW, calls=None):
    data = data or examples()
    ledger = LedgerStore(path)
    ledger.account_identity('binance:primary')
    ledger.hl_identity(ADDRESS)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config_calls = 0
    def bn(request):
        nonlocal config_calls
        assert request.method == 'GET'
        path = request.url.path
        if calls is not None:
            calls.append(path)
        kind = {'/fapi/v3/balance': 'balance', '/fapi/v3/positionRisk': 'positions',
                '/fapi/v3/account': 'account', '/fapi/v1/accountConfig': 'config'}[path]
        if kind == 'config':
            config_calls += 1
            if config_calls == 2 and 'config_after' in data:
                kind = 'config_after'
        return httpx.Response(data.get('bn_status', {}).get(kind, 200), json=deepcopy(data[kind]))
    def hl(request):
        body = json.loads(request.content)
        assert request.method == 'POST' and request.url.path == '/info'
        if body['type'] != 'perpDexs':
            assert body['user'] == ADDRESS
        payload = {'clearinghouseState': data['state'], 'spotClearinghouseState': data['spot'],
                   'userAbstraction': data['mode'], 'perpDexs': data['dexes']}[body['type']]
        return httpx.Response(200, json=deepcopy(payload))
    binance = BinanceAccountClient('synthetic-key', key,
        http=httpx.Client(transport=httpx.MockTransport(bn)), clock=lambda: MS / 1000, sleeper=lambda _: None)
    hyperliquid = HyperliquidAccountClient([ADDRESS],
        http=httpx.Client(transport=httpx.MockTransport(hl)), sleeper=lambda _: None)
    return RiskCollector(RiskStore(path), binance_factory=lambda: binance,
        hyperliquid_factory=lambda: hyperliquid, clock=clock if callable(clock) else lambda: clock).capture()


def nav(path, fx=None, stamp=NOW):
    summary = {'valuation_at': stamp.isoformat(), 'fx_times': quote_client().read() if fx is None else fx,
               'crypto_max_age_seconds': 300}
    with sqlite3.connect(path) as conn:
        return crypto_nav(conn, summary)['crypto_nav']


def test_equity_once_and_direct_stablecoin_quotes_through_real_clients(tmp_path):
    path = tmp_path / 'db'
    result = capture(path)
    values = nav(path)
    assert values['binance']['by_currency']['USDT']['decimal_value'] == '110.00000000'
    assert values['binance']['value_krw_decimal'] == '154000.00000000'
    assert values['hyperliquid']['value_krw_decimal'] == '278000'
    assert values['hyperliquid']['by_currency']['USDC']['decimal_value'] == '200'
    assert len(values['hyperliquid']['accounts']) == 1
    assert all(r['ledger_account_identity'] for v in ('binance', 'hyperliquid') for r in values[v]['accounts'])
    assert ADDRESS not in json.dumps(result)


@pytest.mark.parametrize('mode', ['default', 'disabled'])
def test_standard_hl_values_only_perp_account_equity(tmp_path, mode):
    data = examples()
    data['mode'] = mode
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['hyperliquid']['by_currency']['USDC']['decimal_value'] == '13109.482328'


@pytest.mark.parametrize('mutation', ['duplicate_balance', 'missing_balance', 'missing_pnl', 'duplicate_position',
                                     'unknown_side', 'seconds_time', 'bool_number'])
def test_invalid_binance_nav_response_never_supplies_nav(tmp_path, mutation):
    data = examples()
    account = data['account']
    if mutation == 'duplicate_balance':
        account['assets'].append(deepcopy(account['assets'][0]))
    elif mutation == 'missing_balance':
        del account['assets'][0]['walletBalance']
    elif mutation == 'missing_pnl':
        del account['positions'][0]['unrealizedProfit']
    elif mutation == 'duplicate_position':
        account['positions'].append(deepcopy(account['positions'][0]))
    elif mutation == 'unknown_side':
        account['positions'][0]['positionSide'] = 'FUTURE_ENUM'
    elif mutation == 'seconds_time':
        account['positions'][0]['updateTime'] //= 1000
    else:
        account['assets'][0]['walletBalance'] = True
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['binance']['value_krw'] is None
    assert nav(path)['binance']['reason_codes']


@pytest.mark.parametrize('mutation', ['duplicate_spot', 'missing_spot', 'wrong_usdc_identity', 'wrong_usdc_symbol', 'other_asset',
                                     'unknown_mode', 'stale_state', 'bad_total'])
def test_hl_unsupported_or_incomplete_inputs_are_unresolved(tmp_path, mutation):
    data = examples()
    if mutation == 'duplicate_spot':
        data['spot']['balances'].append(deepcopy(data['spot']['balances'][0]))
    elif mutation == 'missing_spot':
        del data['spot']['balances'][0]['total']
    elif mutation == 'wrong_usdc_identity':
        data['spot']['balances'][0]['token'] = 99
    elif mutation == 'wrong_usdc_symbol':
        data['spot']['balances'][0]['coin'] = 'OTHER'
    elif mutation == 'other_asset':
        data['spot']['balances'].append({'coin': 'HYPE', 'token': 150, 'total': '1', 'hold': '0', 'entryNtl': '0'})
    elif mutation == 'unknown_mode':
        data['mode'] = 'futureAccountMode'
    else:
        data['mode'] = 'default'
        if mutation == 'stale_state':
            data['state']['time'] -= 301000
        else:
            data['state']['marginSummary']['totalNtlPos'] = '0'
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['hyperliquid']['value_krw'] is None


def test_explicit_zero_collateral_needs_no_missing_mark(tmp_path):
    data = examples()
    data['account']['assets'].append(zero_asset())
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['binance']['value_krw'] == 154000


def test_nonzero_collateral_without_mark_is_null(tmp_path):
    data = examples()
    data['account']['assets'].append(zero_asset(wallet='1'))
    path = tmp_path / 'db'
    capture(path, data)
    assert nav(path)['binance']['value_krw'] is None
    assert 'MISSING_CRYPTO_FX_RATE' in nav(path)['binance']['reason_codes']


# Live 2.12.x V3 margin-asset list (read-only probe, key names only); amounts synthetic.
LIVE_ASSETS = ('U', 'BFUSD', 'BNB', 'RWUSD', 'BTC', 'USDT', 'USD1', 'USDC', 'FDUSD', 'LDUSDT', 'ETH')


def live_shape(legacy_rows=True):
    data = examples()
    usdt = data['account']['assets'][0]
    data['account']['assets'] = [usdt if asset == 'USDT' else zero_asset(asset) for asset in LIVE_ASSETS]
    if legacy_rows:
        data['balance'] += [dict(data['balance'][0], asset=asset, balance='0', crossWalletBalance='0',
            crossUnPnl='0', availableBalance='0', maxWithdrawAmount='0') for asset in LIVE_ASSETS if asset != 'USDT']
    return data


@pytest.mark.parametrize('legacy_rows', [True, False])
def test_live_binance_asset_list_with_only_usdt_funded_is_complete(tmp_path, legacy_rows):
    path = tmp_path / 'db'
    capture(path, live_shape(legacy_rows))
    value = nav(path)['binance']
    assert value['reason_codes'] == [] and value['value_krw'] == 154000
    assert value['value_krw_decimal'] == '154000.00000000'
    assert {c: v['decimal_value'] for c, v in value['by_currency'].items() if c != 'USDT'} == {
        asset: '0' for asset in LIVE_ASSETS if asset != 'USDT'}


@pytest.mark.parametrize('field', ['walletBalance', 'maxWithdrawAmount'])
def test_live_binance_nonzero_unvalued_asset_is_unknown_collateral(tmp_path, field):
    data = live_shape()
    bnb = next(r for r in data['account']['assets'] if r['asset'] == 'BNB')
    if field == 'walletBalance':
        bnb.update(zero_asset('BNB', '1'))
    else:
        bnb[field] = '1'
    path = tmp_path / 'db'
    capture(path, data)
    value = nav(path)['binance']
    assert value['value_krw'] is None
    assert 'CRYPTO_BINANCE_UNKNOWN_COLLATERAL' in value['reason_codes']


@pytest.mark.parametrize('extra', [[[0, '20.0']], {'0': '20.0'}])
def test_live_hl_spot_maintenance_key_is_accepted_but_not_valued(tmp_path, extra):
    data = examples()
    data['spot']['tokenToAvailableAfterMaintenance'] = extra
    data['spot']['balances'].append({'coin': 'HYPE', 'token': 150, 'total': '0.0', 'hold': '0.0', 'entryNtl': '0.0'})
    path = tmp_path / 'db'
    capture(path, data)
    value = nav(path)['hyperliquid']
    assert value['reason_codes'] == [] and value['value_krw_decimal'] == '278000'


@pytest.mark.parametrize('key,extra', [('futureSpotField', []), ('tokenToAvailableAfterMaintenance', '20.0')])
def test_unknown_or_malformed_hl_spot_key_is_still_rejected(tmp_path, key, extra):
    data = examples()
    data['spot'][key] = extra
    path = tmp_path / 'db'
    capture(path, data)
    value = nav(path)['hyperliquid']
    assert value['value_krw'] is None
    assert value['reason_codes']
    if key == 'futureSpotField':
        assert 'CRYPTO_INCOMPLETE_ENVELOPE' in value['reason_codes']


@pytest.mark.parametrize('mutation', ['missing_pair', 'duplicate', 'wrong_pair', 'stale_trade',
                                     'missing_time', 'seconds', 'negative', 'bool', 'error_envelope'])
def test_ticker_validator_rejects_unusable_real_http_response(mutation):
    payload = tickers()
    if mutation == 'missing_pair':
        payload.pop()
    elif mutation == 'duplicate':
        payload.append(deepcopy(payload[0]))
    elif mutation == 'wrong_pair':
        payload[0]['market'] = 'USD-USDT'
    elif mutation == 'stale_trade':
        payload[0]['trade_timestamp'] -= 301000
    elif mutation == 'missing_time':
        del payload[0]['trade_timestamp']
    elif mutation == 'seconds':
        payload[0]['trade_timestamp'] //= 1000
    elif mutation in ('negative', 'bool'):
        payload[0]['trade_price'] = -1 if mutation == 'negative' else True
    else:
        payload = {'error': {'name': 'mock'}}
    with pytest.raises(CryptoValidationError):
        quote_client(payload).read()


def test_snapshot_refreshes_stale_capture_and_retains_failed_quote_reason(tmp_path):
    path = tmp_path / 'db'
    old = capture(path, clock=NOW - timedelta(minutes=10))
    calls = []
    def refresh():
        calls.append(True)
        return capture(path)
    summary = {'valuation_at': NOW.isoformat()}
    prepare_crypto_snapshot(summary, path, quote_factory=lambda: quote_client(status=503),
                            capture=refresh, clock=lambda: NOW)
    assert calls == [True]
    assert summary['crypto_risk_snapshot_id'] != old['snapshot_id']
    assert summary['crypto_fx_times']['USDT']['rate_to_krw'] is None
    assert summary['crypto_fx_times']['USDT']['reason_codes'] == ['CRYPTO_QUOTE_UNAVAILABLE']
    prepare_crypto_snapshot(summary, path, quote_factory=quote_client, capture=refresh, clock=lambda: NOW)
    assert calls == [True]


def test_daily_nav_rechecks_stale_fx_instead_of_trusting_a_positive_rate(tmp_path):
    path = tmp_path / 'db'
    capture(path)
    fx = quote_client().read()
    fx['USDT']['priced_at'] = (NOW - timedelta(minutes=6)).isoformat()
    values = nav(path, fx)
    assert values['binance']['value_krw'] is None
    assert 'STALE_CRYPTO_FX_RATE' in values['binance']['reason_codes']
