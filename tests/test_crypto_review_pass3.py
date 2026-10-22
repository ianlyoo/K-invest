"""R2 coherent Binance NAV: real signed clients, synthetic HTTP responses only."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_crypto_flow_coverage import binance
from test_crypto_nav_capture import (
    NOW,
    account_config_example,
    account_example,
    capture,
    examples,
    nav,
    zero_asset,
)

from crypto_futures_accounts import BINANCE_ALLOWLIST, BINANCE_WEIGHTS, FuturesError


def observed(tmp_path, data, **kwargs):
    path = tmp_path / 'risk.db'
    result = capture(path, data, **kwargs)
    return nav(path)['binance'], result


def test_only_two_approved_routes_and_documented_weights_added():
    old = {('GET', path) for path in ('/fapi/v1/time', '/fapi/v3/positionRisk', '/fapi/v3/balance',
        '/fapi/v1/userTrades', '/fapi/v1/income', '/sapi/v1/asset/transfer',
        '/sapi/v1/capital/deposit/hisrec', '/sapi/v1/capital/withdraw/history')}
    added = {('GET', '/fapi/v3/account'), ('GET', '/fapi/v1/accountConfig')}
    assert BINANCE_ALLOWLIST == old | added
    assert all(BINANCE_WEIGHTS[path] == 5 for _, path in added)
    client = binance(lambda request: pytest.fail('Forbidden route reached HTTP'))
    for method, path in [('POST', '/fapi/v1/order'), ('POST', '/fapi/v1/multiAssetsMargin'),
                         ('GET', '/fapi/v2/account'), ('GET', '/papi/v1/account')]:
        with pytest.raises(FuturesError, match='read unavailable'):
            client.request(method, path)


def test_mode_account_mode_read_order_and_provenance(tmp_path):
    calls = []
    value, result = observed(tmp_path, examples(), calls=calls)
    assert calls == ['/fapi/v3/positionRisk', '/fapi/v3/balance', '/fapi/v1/accountConfig',
                     '/fapi/v3/account', '/fapi/v1/accountConfig']
    assert value['value_krw_decimal'] == '154000.00000000'
    row = next(r for r in result['nav_evidence']['accounts'] if r['venue'] == 'binance')
    assert row['nav_account_mode'] == 'single_asset'
    assert row['nav_equity']['inputs']['source'] == '/fapi/v3/account'
    assert row['nav_price_time_basis'] == 'coherent_account_read_completion'


@pytest.mark.parametrize('legacy', ['price_move', 'empty_positions', 'other_wallet', 'invalid_balance',
                                   'invalid_positions', 'balance_outage', 'positions_outage', 'both_outage'])
def test_legacy_risk_reads_cannot_poison_coherent_nav(tmp_path, legacy):
    data = examples()
    if legacy == 'price_move':
        data['positions'][0].update(unRealizedProfit='10.03', markPrice='0.411', notional='12.33')
        data['balance'][0]['crossUnPnl'] = '10.05'
    elif legacy == 'empty_positions':
        data['positions'] = []
    elif legacy == 'other_wallet':
        data['balance'][0].update(asset='USDC', balance='999')
    elif legacy.startswith('invalid'):
        data[legacy.removeprefix('invalid_')] = {'error': 'synthetic'}
    else:
        data['bn_status'] = {k: 503 for k in ('balance', 'positions')
                             if legacy == 'both_outage' or legacy.startswith(k)}
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] == '154000.00000000'
    assert not value['reason_codes']


@pytest.mark.parametrize('mode', [True, False])
def test_mode_change_in_either_direction_is_unresolved(tmp_path, mode):
    data = examples()
    data['config']['multiAssetsMargin'] = mode
    data['config_after'] = dict(data['config'], multiAssetsMargin=not mode)
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_BINANCE_ACCOUNT_MODE_CHANGED' in value['reason_codes']
    assert all(row['nav_account_mode'] == 'unknown' for row in value['accounts'])


def test_multi_assets_coverage_is_not_proven_by_quote_assets_even_with_marks(tmp_path):
    data = examples()
    data['config']['multiAssetsMargin'] = True
    data['account']['assets'].append(zero_asset('USDC', '7'))
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_BINANCE_COLLATERAL_COVERAGE_UNVERIFIED' in value['reason_codes']


@pytest.mark.parametrize('when', ['config', 'config_after'])
@pytest.mark.parametrize('mutation', ['missing', 'null', 'string_false', 'zero', 'unknown', 'portfolio',
                                     'partial', 'seconds', 'outage', 'absent_object'])
def test_invalid_or_missing_mode_evidence_never_guesses(tmp_path, when, mutation):
    data = examples()
    data[when] = deepcopy(data['config'])
    if mutation == 'missing':
        del data[when]['multiAssetsMargin']
    elif mutation in ('null', 'string_false', 'zero', 'unknown'):
        data[when]['multiAssetsMargin'] = {'null': None, 'string_false': 'false', 'zero': 0, 'unknown': 'future'}[mutation]
    elif mutation in ('portfolio', 'partial'):
        data[when]['portfolioMargin' if mutation == 'portfolio' else 'partial'] = True
    elif mutation == 'seconds':
        data[when]['updateTime'] = 1791882600
    elif mutation == 'outage':
        data['bn_status'] = {when: 503}
    else:
        data[when] = None
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is None
    assert value['reason_codes']
    assert all(row['nav_account_mode'] == 'unknown' for row in value['accounts'])


@pytest.mark.parametrize('where', ['envelope', 'asset', 'position'])
@pytest.mark.parametrize('flag', ['partial', 'hasNext', 'nextCursor', 'error', 'complete'])
def test_truncation_metadata_rejects_the_whole_account(tmp_path, where, flag):
    data = examples()
    target = data['account'] if where == 'envelope' else data['account'][where + 's'][0]
    target[flag] = False if flag == 'complete' else True
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_INCOMPLETE_ENVELOPE' in value['reason_codes']


@pytest.mark.parametrize('mutation', ['missing_assets', 'missing_positions', 'empty_assets', 'empty_positions',
    'duplicate_asset', 'duplicate_position', 'unknown_asset', 'unknown_contract', 'seconds', 'micros',
    'number_type', 'bool_type', 'nan', 'missing_money', 'margin_total', 'pnl_total', 'wallet_total',
    'wrong_side', 'side_sign', 'mixed_sides', 'initial_margin', 'maint_margin', 'isolated_wallet',
    'asset_missing', 'envelope_error', 'pnl_cancellation_omitted_position'])
def test_same_response_completeness_sweep(tmp_path, mutation):
    data = examples()
    account = data['account']
    asset, position = account['assets'][0], account['positions'][0]
    if mutation.startswith('missing_') and mutation != 'missing_money':
        del account[mutation.removeprefix('missing_')]
    elif mutation.startswith('empty_'):
        account[mutation.removeprefix('empty_')] = []
    elif mutation.startswith('duplicate_'):
        key = mutation.removeprefix('duplicate_') + 's'
        account[key].append(deepcopy(account[key][0]))
    elif mutation == 'unknown_asset':
        asset['asset'] = 'BNB'
    elif mutation == 'unknown_contract':
        position['symbol'] = 'ETHBTC'
    elif mutation in ('seconds', 'micros'):
        position['updateTime'] = position['updateTime'] // 1000 if mutation == 'seconds' else position['updateTime'] * 1000
    elif mutation in ('number_type', 'bool_type', 'nan'):
        asset['walletBalance'] = {'number_type': 100, 'bool_type': True, 'nan': 'NaN'}[mutation]
    elif mutation == 'missing_money':
        del position['unrealizedProfit']
    elif mutation == 'margin_total':
        asset['marginBalance'] = '111'
    elif mutation == 'pnl_total':
        position['unrealizedProfit'] = '9.99'
    elif mutation == 'wallet_total':
        account['totalWalletBalance'] = '101'
    elif mutation == 'wrong_side':
        position['positionSide'] = 'FUTURE'
    elif mutation == 'side_sign':
        position['positionSide'] = 'SHORT'
    elif mutation == 'mixed_sides':
        account['positions'].append(dict(position, positionSide='LONG'))
    elif mutation == 'initial_margin':
        position['initialMargin'] = '0'
    elif mutation == 'maint_margin':
        position['maintMargin'] = '0'
    elif mutation == 'isolated_wallet':
        position['isolatedWallet'] = '20'
    elif mutation == 'asset_missing':
        position['symbol'] = 'ADAUSDC'
    elif mutation == 'envelope_error':
        data['account'] = {'code': -1, 'msg': 'synthetic'}
    else:
        # Missing offsetting P&L legs can still be exposed by their margin sum.
        position['unrealizedProfit'] = '10'
        asset.update(positionInitialMargin='3', initialMargin='3')
        account.update(totalPositionInitialMargin='3', totalInitialMargin='3')
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is None
    assert value['reason_codes']


@pytest.mark.parametrize('delta,valid', [('0.00000001', True), ('0.00000002', False), ('0.01', False)])
def test_rounding_bound_is_not_a_market_movement_tolerance(tmp_path, delta, valid):
    data = examples()
    data['account']['positions'][0]['unrealizedProfit'] = str(Decimal(10) + Decimal(delta))
    value, _ = observed(tmp_path, data)
    assert (value['value_krw_decimal'] is not None) == valid


def test_second_collateral_counted_once_without_legacy_balance_row(tmp_path):
    data = examples()
    data['account']['assets'].append(zero_asset('USDC', '7'))
    value, _ = observed(tmp_path, data)
    assert Decimal(value['value_krw_decimal']) == 110 * 1400 + 7 * 1390
    assert set(value['by_currency']) == {'USDT', 'USDC'}


def test_hedge_position_mode_and_changed_position_mode(tmp_path):
    data = examples()
    data['config']['dualSidePosition'] = True
    data['account']['positions'][0]['positionSide'] = 'LONG'
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is not None
    data['config_after'] = dict(data['config'], dualSidePosition=False)
    value, _ = observed(tmp_path / 'changed', data)
    assert 'CRYPTO_BINANCE_ACCOUNT_MODE_CHANGED' in value['reason_codes']


@pytest.mark.parametrize('case', ['elapsed', 'reversed_step'])
def test_unbounded_or_reversed_read_bracket_is_unresolved(tmp_path, case):
    tick = -1
    calls = []
    def clock():
        nonlocal tick
        tick += 1
        seconds = 20 * tick if case == 'elapsed' else -1 if len(calls) == 3 else 0
        return NOW + timedelta(seconds=seconds)
    value, _ = observed(tmp_path, examples(), clock=clock, calls=calls)
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_BINANCE_ACCOUNT_READ_WINDOW_EXCEEDED' in value['reason_codes']
    assert all(row['nav_account_mode'] == 'unknown' for row in value['accounts'])


def test_published_examples_use_real_signed_client_and_inconsistent_totals_are_not_nav(tmp_path):
    # Binance Account V3 / Account Configuration examples, accessed 2026-10-13.
    # The published illustration has 126.72469206 at top but 23.72469206 in its
    # sole asset row: parse it, but never silently treat it as a coherent book.
    account = account_example()
    account.update({key: '0.00000000' for key in account if key not in ('assets', 'positions')})
    for key in ('totalWalletBalance', 'totalMarginBalance', 'totalCrossWalletBalance', 'availableBalance', 'maxWithdrawAmount'):
        account[key] = '126.72469206'
    asset = zero_asset('USDT', '23.72469206')
    asset.update(availableBalance='23.72469206', maxWithdrawAmount='23.72469206', updateTime=1625474304765)
    account['assets'] = [asset]
    account['positions'] = [dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='1.000',
        unrealizedProfit='0.00000000', isolatedMargin='0.00000000', notional='0', isolatedWallet='0',
        initialMargin='0', maintMargin='0', updateTime=0)]
    config = dict(account_config_example(), dualSidePosition=True)
    def handler(request):
        assert request.method == 'GET' and request.url.host == 'fapi.binance.com'
        assert request.headers['X-MBX-APIKEY']
        assert {'timestamp', 'recvWindow', 'signature'} == set(request.url.params)
        return httpx.Response(200, json=config if request.url.path.endswith('accountConfig') else account)
    client = binance(handler)
    assert client.account_information() == account
    assert client.account_config() == config
    data = examples()
    data.update(account=account, config=dict(config, dualSidePosition=False))
    value, _ = observed(tmp_path, data)
    assert value['value_krw_decimal'] is None
    assert 'CRYPTO_TOTAL_MISMATCH' in value['reason_codes']


@pytest.mark.parametrize('method', ['account_config', 'account_information'])
def test_malformed_success_json_does_not_establish_evidence(method):
    client = binance(lambda request: httpx.Response(200, text='{'))
    with pytest.raises(FuturesError) as error:
        getattr(client, method)()
    assert error.value.code == 'FUTURES_INVALID_RESPONSE'
