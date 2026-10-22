"""Strict, pure validators for the crypto NAV and capital-flow read paths.

Only fixed reason codes escape validation. No response bodies or identities are
included in errors. See docs/crypto-nav-flows.md for dated provider references.
The transport binds account reads to the signed account / requested HL address;
history validators additionally bind rows to the window and requested route.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class CryptoValidationError(ValueError):
    def __init__(self, code='CRYPTO_INVALID_RESPONSE'):
        super().__init__(code)
        self.code = code


def require(condition, code='CRYPTO_INVALID_RESPONSE'):
    if not condition:
        raise CryptoValidationError(code)


def decimal(value):
    require(type(value) in (str, int, float, Decimal), 'CRYPTO_INVALID_NUMBER')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        result = None
    require(result is not None and result.is_finite(), 'CRYPTO_INVALID_NUMBER')
    return result


def token(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:@.\-]{1,80}', value),
            'CRYPTO_INVALID_IDENTITY')
    return value


def millis(value, *, allow_zero=False):
    # Reject seconds/microseconds and booleans, including otherwise plausible
    # time values that would silently move a cash flow to another date.
    require(type(value) is int and ((allow_zero and value == 0) or
            946684800000 <= value < 4102444800000), 'CRYPTO_INVALID_TIMESTAMP')
    return value


def rows(value):
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value))
    return value


def object_shape(value, fields, *, required=()):
    """Only documented fields can establish a complete object response.

    This also rejects error/partial/pagination metadata on otherwise plausible
    empty or populated responses. Unknown contract extensions require review.
    """
    require(isinstance(value, dict))
    require(set(value) <= set(fields), 'CRYPTO_INCOMPLETE_ENVELOPE')
    require(set(required) <= set(value))
    return value


def unique(seen, key):
    require(key not in seen, 'CRYPTO_DUPLICATE_IDENTITY')
    seen.add(key)


def binance_balances(payload):
    seen, aliases = set(), set()
    for row in rows(payload):
        object_shape(row, {'accountAlias', 'asset', 'balance', 'crossWalletBalance', 'crossUnPnl',
                          'availableBalance', 'maxWithdrawAmount', 'marginAvailable', 'updateTime'})
        aliases.add(token(row.get('accountAlias')))
        unique(seen, token(row.get('asset')))
        for field in ('balance', 'crossWalletBalance', 'crossUnPnl', 'availableBalance', 'maxWithdrawAmount'):
            decimal(row.get(field))
        require(type(row.get('marginAvailable')) is bool)
        millis(row.get('updateTime'), allow_zero=True)
    require(len(aliases) <= 1, 'CRYPTO_REQUEST_MISMATCH')
    return payload


def binance_positions(payload):
    seen = set()
    for row in rows(payload):
        object_shape(row, {'symbol', 'positionSide', 'positionAmt', 'entryPrice', 'breakEvenPrice',
            'markPrice', 'unRealizedProfit', 'liquidationPrice', 'isolatedMargin', 'notional', 'marginAsset',
            'isolatedWallet', 'initialMargin', 'maintMargin', 'positionInitialMargin', 'openOrderInitialMargin',
            'adl', 'bidNotional', 'askNotional', 'updateTime', 'marginType'})
        symbol, side = token(row.get('symbol')), row.get('positionSide')
        require(side in ('BOTH', 'LONG', 'SHORT'), 'CRYPTO_UNKNOWN_POSITION_SIDE')
        unique(seen, (symbol, side))
        token(row.get('marginAsset'))
        size = decimal(row.get('positionAmt'))
        require((side != 'LONG' or size >= 0) and (side != 'SHORT' or size <= 0))
        for field in ('unRealizedProfit', 'markPrice', 'notional', 'isolatedWallet', 'isolatedMargin'):
            decimal(row.get(field))
        require(decimal(row['isolatedWallet']) >= 0, 'CRYPTO_BINANCE_MARGIN_SCOPE_UNAVAILABLE')
        if 'marginType' in row:
            require(row['marginType'] in ('cross', 'isolated'), 'CRYPTO_BINANCE_MARGIN_SCOPE_UNAVAILABLE')
        require(size != 0 or decimal(row['unRealizedProfit']) == decimal(row['notional']) == 0,
                'CRYPTO_TOTAL_MISMATCH')
        require(abs(decimal(row['notional']) - size * decimal(row['markPrice'])) <= Decimal('0.000001'),
                'CRYPTO_TOTAL_MISMATCH')
        millis(row.get('updateTime'), allow_zero=True)
    return payload


def binance_nav_inputs(positions, balances):
    """Reconcile independent positionRisk-v3 and balance-v3 account views.

    balance includes isolated wallets; crossUnPnl excludes isolated positions.
    Both scopes must reconcile per collateral before summing wallet + all P&L.
    A sequential-read mismatch is unavailable, never a reason to omit P&L.
    """
    binance_positions(positions)
    binance_balances(balances)
    cross_pnl, isolated_wallets = defaultdict(Decimal), defaultdict(Decimal)
    assets = {row['asset'] for row in balances}
    tolerance = Decimal('0.00000001')
    for row in positions:
        asset = row['marginAsset']
        require(asset in assets, 'CRYPTO_BINANCE_ACCOUNT_POSITION_MISMATCH')
        wallet, margin, pnl = (decimal(row[k]) for k in ('isolatedWallet', 'isolatedMargin', 'unRealizedProfit'))
        isolated = wallet != 0 or margin != 0
        if 'marginType' in row:
            require((row['marginType'] == 'isolated') == isolated or wallet == margin == pnl == 0,
                    'CRYPTO_BINANCE_MARGIN_SCOPE_UNAVAILABLE')
            isolated = row['marginType'] == 'isolated'
        if isolated:
            require(abs(margin - wallet - pnl) <= tolerance, 'CRYPTO_BINANCE_ACCOUNT_POSITION_MISMATCH')
            isolated_wallets[asset] += wallet
        else:
            cross_pnl[asset] += pnl
    for row in balances:
        asset = row['asset']
        require(abs(cross_pnl[asset] - decimal(row['crossUnPnl'])) <= tolerance
                and abs(decimal(row['balance']) - decimal(row['crossWalletBalance']) - isolated_wallets[asset]) <= tolerance,
                'CRYPTO_BINANCE_ACCOUNT_POSITION_MISMATCH')


def binance_account_config(payload):
    fields = {'feeTier', 'canTrade', 'canDeposit', 'canWithdraw', 'dualSidePosition',
              'updateTime', 'multiAssetsMargin', 'tradeGroupId'}
    object_shape(payload, fields, required=fields)
    for field in ('canTrade', 'canDeposit', 'canWithdraw', 'dualSidePosition', 'multiAssetsMargin'):
        require(type(payload[field]) is bool, 'CRYPTO_BINANCE_ACCOUNT_MODE_UNAVAILABLE')
    for field in ('feeTier', 'tradeGroupId'):
        require(type(payload[field]) is int and -1 <= payload[field] < 2**63)
    require(payload['feeTier'] >= 0)
    millis(payload['updateTime'], allow_zero=True)
    return payload


def _binance_money(value):
    # These endpoints document decimal strings, not JSON numbers or exponents.
    require(isinstance(value, str) and re.fullmatch(r'-?\d+(?:\.\d+)?', value),
            'CRYPTO_INVALID_NUMBER')
    return decimal(value)


def binance_position_asset(symbol):
    # USD-M linear contracts settle in their USDT/USDC quote asset. Quarterly
    # contracts append the expiry date; unknown contract naming stays null.
    match = re.fullmatch(r'[A-Z0-9]+(USDT|USDC)(?:_(\d{6}))?', token(symbol))
    require(match is not None, 'CRYPTO_BINANCE_POSITION_COLLATERAL_UNAVAILABLE')
    if match[2]:
        try:
            datetime.strptime(match[2], '%y%m%d')
        except ValueError:
            raise CryptoValidationError('CRYPTO_BINANCE_POSITION_COLLATERAL_UNAVAILABLE') from None
    return match[1]


def binance_account(payload):
    """Validate one coherent V3 account envelope; never compare other reads."""
    totals = {'totalInitialMargin', 'totalMaintMargin', 'totalWalletBalance', 'totalUnrealizedProfit',
              'totalMarginBalance', 'totalPositionInitialMargin', 'totalOpenOrderInitialMargin',
              'totalCrossWalletBalance', 'totalCrossUnPnl', 'availableBalance', 'maxWithdrawAmount'}
    asset_money = {'walletBalance', 'unrealizedProfit', 'marginBalance', 'maintMargin', 'initialMargin',
                   'positionInitialMargin', 'openOrderInitialMargin', 'crossWalletBalance', 'crossUnPnl',
                   'availableBalance', 'maxWithdrawAmount'}
    position_money = {'positionAmt', 'unrealizedProfit', 'isolatedMargin', 'notional',
                      'isolatedWallet', 'initialMargin', 'maintMargin'}
    object_shape(payload, totals | {'assets', 'positions'}, required=totals | {'assets', 'positions'})
    for field in totals:
        _binance_money(payload[field])
    assets, seen = {}, set()
    tolerance = Decimal('0.00000001')
    mismatch = 'CRYPTO_BINANCE_ACCOUNT_POSITION_MISMATCH'
    for row in rows(payload['assets']):
        fields = asset_money | {'asset', 'updateTime'}
        object_shape(row, fields, required=fields)
        asset = token(row['asset'])
        unique(seen, asset)
        assets[asset] = row
        for field in asset_money:
            _binance_money(row[field])
        # Live V3 lists every margin asset (BNB, ETH, FDUSD, ...). One outside
        # the valued set is accepted only as a proven zero in every money field.
        require(asset in ('USDT', 'USDC', 'BTC') or all(decimal(row[f]) == 0 for f in asset_money),
                'CRYPTO_BINANCE_UNKNOWN_COLLATERAL')
        millis(row['updateTime'], allow_zero=True)
        require(abs(decimal(row['marginBalance']) - decimal(row['walletBalance'])
                    - decimal(row['unrealizedProfit'])) <= tolerance, 'CRYPTO_TOTAL_MISMATCH')
        require(abs(decimal(row['initialMargin']) - decimal(row['positionInitialMargin'])
                    - decimal(row['openOrderInitialMargin'])) <= tolerance, 'CRYPTO_TOTAL_MISMATCH')
    require(bool(assets), 'CRYPTO_BINANCE_COLLATERAL_COVERAGE_UNVERIFIED')
    sums, seen, sides, counts = defaultdict(lambda: defaultdict(Decimal)), set(), defaultdict(set), defaultdict(int)
    for row in rows(payload['positions']):
        fields = position_money | {'symbol', 'positionSide', 'updateTime'}
        object_shape(row, fields, required=fields)
        asset = binance_position_asset(row['symbol'])
        require(asset in assets, mismatch)
        side = row['positionSide']
        require(side in ('BOTH', 'LONG', 'SHORT'), 'CRYPTO_UNKNOWN_POSITION_SIDE')
        unique(seen, (row['symbol'], side))
        sides[row['symbol']].add(side)
        require(not ('BOTH' in sides[row['symbol']] and len(sides[row['symbol']]) > 1), mismatch)
        for field in position_money:
            _binance_money(row[field])
        size = decimal(row['positionAmt'])
        require((side != 'LONG' or size >= 0) and (side != 'SHORT' or size <= 0), mismatch)
        require(size != 0 or decimal(row['unrealizedProfit']) == decimal(row['notional']) == 0, mismatch)
        require(all(decimal(row[f]) >= 0 for f in ('isolatedWallet', 'initialMargin', 'maintMargin')), mismatch)
        millis(row['updateTime'], allow_zero=True)
        for field in ('unrealizedProfit', 'initialMargin', 'maintMargin', 'isolatedWallet'):
            sums[asset][field] += decimal(row[field])
        counts[asset] += 1
    for asset, row in assets.items():
        # One unit at the documented 8-decimal precision per position bounds
        # rounding only; this is never a price-movement tolerance.
        rounding = tolerance * max(1, counts[asset])
        for total, field in (('unrealizedProfit', 'unrealizedProfit'),
                             ('positionInitialMargin', 'initialMargin'), ('maintMargin', 'maintMargin')):
            require(abs(decimal(row[total]) - sums[asset][field]) <= rounding, mismatch)
        require(abs(decimal(row['walletBalance']) - decimal(row['crossWalletBalance'])
                    - sums[asset]['isolatedWallet']) <= rounding, mismatch)
    return payload


def binance_account_nav(before, account, after):
    """Mode binding and single-asset totals after the response validators."""
    require(isinstance(before, dict) and isinstance(after, dict), 'CRYPTO_BINANCE_ACCOUNT_MODE_UNAVAILABLE')
    require(isinstance(account, dict), 'CRYPTO_BINANCE_ACCOUNT_UNAVAILABLE')
    mode = ('multiAssetsMargin', 'dualSidePosition')
    require(all(before[k] == after[k] for k in mode), 'CRYPTO_BINANCE_ACCOUNT_MODE_CHANGED')
    # V3 documents quote assets only; it cannot prove coverage of ETH/BNB/etc
    # eligible in Multi-Assets Mode, even when all returned assets have marks.
    require(not before['multiAssetsMargin'], 'CRYPTO_BINANCE_COLLATERAL_COVERAGE_UNVERIFIED')
    for row in account['positions']:
        require((row['positionSide'] != 'BOTH') == before['dualSidePosition'],
                'CRYPTO_BINANCE_POSITION_MODE_MISMATCH')
    usdt = next((r for r in account['assets'] if r['asset'] == 'USDT'), {})
    for total, field in (('totalWalletBalance', 'walletBalance'), ('totalUnrealizedProfit', 'unrealizedProfit'),
                        ('totalMarginBalance', 'marginBalance'), ('totalInitialMargin', 'initialMargin'),
                        ('totalMaintMargin', 'maintMargin'), ('totalPositionInitialMargin', 'positionInitialMargin'),
                        ('totalOpenOrderInitialMargin', 'openOrderInitialMargin'),
                        ('totalCrossWalletBalance', 'crossWalletBalance'), ('totalCrossUnPnl', 'crossUnPnl')):
        require(abs(decimal(account[total]) - decimal(usdt.get(field, '0'))) <= Decimal('0.00000001'),
                'CRYPTO_TOTAL_MISMATCH')


def hyperliquid_state(payload, dex=''):
    fields = {'marginSummary', 'crossMarginSummary', 'crossMaintenanceMarginUsed', 'withdrawable', 'assetPositions', 'time'}
    object_shape(payload, fields, required=fields)
    millis(payload.get('time'))
    summary = payload['marginSummary']
    summary_fields = {'accountValue', 'totalRawUsd', 'totalNtlPos', 'totalMarginUsed'}
    for key in ('marginSummary', 'crossMarginSummary'):
        object_shape(payload[key], summary_fields, required=summary_fields)
        for field in summary_fields:
            decimal(payload[key][field])
    decimal(payload['crossMaintenanceMarginUsed'])
    decimal(payload['withdrawable'])
    seen, notionals = set(), Decimal(0)
    for item in rows(payload.get('assetPositions')):
        object_shape(item, {'type', 'position'}, required={'type', 'position'})
        require(item.get('type') == 'oneWay' and isinstance(item.get('position'), dict))
        row = item['position']
        object_shape(row, {'coin', 'cumFunding', 'entryPx', 'leverage', 'liquidationPx', 'marginUsed',
                          'maxLeverage', 'positionValue', 'returnOnEquity', 'szi', 'unrealizedPnl'})
        coin = token(row.get('coin'))
        require((coin.split(':', 1)[0] if ':' in coin else '') == dex, 'CRYPTO_REQUEST_MISMATCH')
        unique(seen, coin)
        for field in ('szi', 'unrealizedPnl', 'positionValue', 'marginUsed'):
            decimal(row.get(field))
        leverage = row.get('leverage')
        object_shape(leverage, {'type', 'value', 'rawUsd'}, required={'type', 'value'})
        require(leverage.get('type') in ('cross', 'isolated'))
        require(decimal(leverage.get('value')) > 0)
        notionals += abs(decimal(row['positionValue']))
    require(abs(notionals - decimal(summary['totalNtlPos'])) <= Decimal('0.000001'),
            'CRYPTO_TOTAL_MISMATCH')
    return payload


def hyperliquid_spot(payload):
    # Live adds tokenToAvailableAfterMaintenance; shape-checked only, never valued.
    object_shape(payload, {'balances', 'tokenToAvailableAfterMaintenance'}, required={'balances'})
    require(isinstance(payload.get('tokenToAvailableAfterMaintenance', []), (list, dict)))
    coins, tokens = set(), set()
    for row in rows(payload.get('balances')):
        fields = {'coin', 'token', 'total', 'hold', 'entryNtl'}
        object_shape(row, fields, required=fields)
        unique(coins, token(row.get('coin')))
        require(type(row.get('token')) is int and row['token'] >= 0)
        unique(tokens, row['token'])
        require((row['coin'] == 'USDC') == (row['token'] == 0), 'CRYPTO_REQUEST_MISMATCH')
        decimal(row.get('total'))
        decimal(row.get('entryNtl'))
        require(decimal(row.get('hold')) >= 0)
    return payload


def _withdraw_time(value):
    require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?', value), 'CRYPTO_INVALID_TIMESTAMP')
    try:
        stamp = int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        stamp = None
    return millis(stamp)


def binance_flow_page(payload, leaf, params):
    """Validate and canonicalize one page before deduplication or persistence."""
    transfer = leaf.startswith('transfer_')
    limit = params['size' if transfer else 'limit']
    if transfer:
        require(isinstance(payload, dict) and type(payload.get('total')) is int and payload['total'] >= 0,
                'HISTORY_SHAPE_UNAVAILABLE')
        require(set(payload) <= {'total', 'rows'}, 'HISTORY_SHAPE_UNAVAILABLE')
        page = payload.get('rows')
        if page is None and payload['total'] == 0:
            page = []
        require(isinstance(page, list) and all(isinstance(row, dict) for row in page), 'HISTORY_SHAPE_UNAVAILABLE')
        require(len(page) <= min(payload['total'], limit), 'HISTORY_SHAPE_UNAVAILABLE')
        expected = min(limit, max(0, payload['total'] - (params['current'] - 1) * limit))
        require(len(page) == expected, 'PAGINATION_WINDOW_GAP')
    else:
        page = rows(payload)
        require(len(page) <= limit, 'HISTORY_SHAPE_UNAVAILABLE')
    if transfer:
        page = [{**row, 'tranId': binance_transfer_id(row.get('tranId'))} for row in page]
    seen = set()
    for row in page:
        ident = row.get('tranId' if transfer else 'id')
        require(type(ident) in (str, int) and str(ident), 'CRYPTO_INVALID_IDENTITY')
        unique(seen, str(ident))
        token(row.get('asset' if transfer else 'coin'))
        require(decimal(row.get('amount')) > 0)
        if transfer:
            object_shape(row, {'asset', 'amount', 'type', 'status', 'tranId', 'timestamp'})
            require(row.get('type') == params['type'], 'CRYPTO_REQUEST_MISMATCH')
            require(row.get('status') in ('CONFIRMED', 'FAILED', 'PENDING'), 'CRYPTO_UNKNOWN_FLOW_STATUS')
            stamp = millis(row.get('timestamp'))
        else:
            object_shape(row, {'id', 'amount', 'coin', 'network', 'status', 'address', 'addressTag', 'txId',
                'insertTime', 'transferType', 'unlockConfirm', 'walletType', 'confirmTimes', 'travelRuleStatus',
                'completeTime', 'sourceAddress'} if leaf == 'deposit' else
                {'id', 'amount', 'transactionFee', 'coin', 'status', 'address', 'txId', 'applyTime', 'network',
                 'transferType', 'withdrawOrderId', 'info', 'confirmNo', 'walletType', 'txKey', 'completeTime'})
            # Capital-history IDs are opaque strings, unlike numeric tranId.
            token(row.get('id'))
            allowed = (0, 1, 2, 6, 7, 8) if leaf == 'deposit' else (0, 2, 3, 4, 5, 6)
            require(type(row.get('status')) is int and row['status'] in allowed, 'CRYPTO_UNKNOWN_FLOW_STATUS')
            require(type(row.get('walletType')) is int and row['walletType'] in (0, 1), 'CRYPTO_UNKNOWN_WALLET')
            stamp = millis(row.get('insertTime')) if leaf == 'deposit' else _withdraw_time(row.get('applyTime'))
            if leaf == 'withdraw':
                require(decimal(row.get('transactionFee')) >= 0)
        require(params['startTime'] <= stamp <= params['endTime'], 'OUT_OF_WINDOW_RESPONSE')
    return page


def binance_transfer_id(value):
    """Explicitly support decimal-string drift of a positive signed int64 ID."""
    require(type(value) is int or isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value),
            'CRYPTO_INVALID_IDENTITY')
    value = int(value)
    require(0 < value <= 9223372036854775807, 'CRYPTO_INVALID_IDENTITY')
    return value


HL_FLOW_TYPES = frozenset({'deposit', 'withdraw', 'internalTransfer', 'spotTransfer',
                           'subAccountTransfer', 'accountClassTransfer', 'send'})
HL_FEE_TYPES = frozenset({'withdraw', 'internalTransfer', 'spotTransfer', 'send'})
HL_DELTA_FIELDS = {
    'deposit': {'type', 'usdc'},
    'withdraw': {'type', 'usdc', 'nonce', 'fee'},
    'internalTransfer': {'type', 'usdc', 'user', 'destination', 'fee'},
    'spotTransfer': {'type', 'amount', 'token', 'usdcValue', 'user', 'destination', 'fee'},
    'subAccountTransfer': {'type', 'usdc', 'user', 'destination'},
    'accountClassTransfer': {'type', 'usdc', 'toPerp'},
    # The send contract is less fully documented: require explicit fee evidence
    # rather than infer a free transfer from an omitted field.
    'send': {'type', 'amount', 'token', 'user', 'destination', 'sourceDex', 'destinationDex', 'fee'},
}
# Signed user transfers also report their action nonce (null on some spot
# transfers), which the official delta table omits for these types; the strict
# validator rejected the live 10/15 send for it. An empty feeToken names no token.
HL_NONCE_TYPES = frozenset({'spotTransfer', 'send'})


def hyperliquid_flow_page(payload, address, lo, hi):
    seen = set()
    for row in rows(payload):
        object_shape(row, {'time', 'hash', 'delta'}, required={'time', 'hash', 'delta'})
        stamp = millis(row.get('time'))
        require(lo <= stamp <= hi, 'OUT_OF_WINDOW_RESPONSE')
        delta = row.get('delta')
        require(isinstance(delta, dict))
        typ = delta.get('type')
        require(typ in HL_FLOW_TYPES, 'UNKNOWN_HYPERLIQUID_DELTA_TYPE')
        if typ in HL_FEE_TYPES:
            require('fee' in delta, 'CRYPTO_WITHDRAWAL_FEE_UNAVAILABLE' if typ == 'withdraw'
                    else 'CRYPTO_TRANSFER_FEE_UNAVAILABLE')
        optional = {'fee', 'nativeTokenFee', 'feeToken', 'usdcValue', *(['nonce'] if typ in HL_NONCE_TYPES else [])}
        object_shape(delta, HL_DELTA_FIELDS[typ] | optional, required=HL_DELTA_FIELDS[typ])
        unique(seen, (token(row.get('hash')), stamp, typ))
        if typ in ('spotTransfer', 'send'):
            require('amount' in delta and 'token' in delta and 'usdc' not in delta, 'CRYPTO_FLOW_CURRENCY_UNAVAILABLE')
        else:
            require('usdc' in delta and 'amount' not in delta, 'CRYPTO_FLOW_CURRENCY_UNAVAILABLE')
        require(decimal(delta.get('usdc', delta.get('amount'))) > 0)
        if 'usdc' not in delta:
            token(delta.get('token'))
        if 'fee' in delta:
            require(decimal(delta['fee']) >= 0)
        if 'usdcValue' in delta:
            require(decimal(delta['usdcValue']) >= 0)
        if delta.get('feeToken', '') != '':
            token(delta['feeToken'])
        if 'nativeTokenFee' in delta:
            require(decimal(delta['nativeTokenFee']) == 0, 'CRYPTO_NATIVE_TOKEN_FEE_UNVERIFIED')
        if typ == 'withdraw' or delta.get('nonce') is not None:
            require(type(delta['nonce']) is int and 0 <= delta['nonce'] <= 18446744073709551615,
                    'CRYPTO_INVALID_IDENTITY')
        if typ == 'accountClassTransfer':
            require(type(delta.get('toPerp')) is bool, 'CRYPTO_UNKNOWN_WALLET')
        elif typ not in ('deposit', 'withdraw'):
            endpoints = [delta.get('user'), delta.get('destination')]
            require(all(isinstance(v, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', v) for v in endpoints),
                    'CRYPTO_FLOW_DIRECTION_UNAVAILABLE')
            require(address.lower() in [v.lower() for v in endpoints], 'CRYPTO_REQUEST_MISMATCH')
            if typ == 'send':
                require(isinstance(delta.get('sourceDex'), str) and isinstance(delta.get('destinationDex'), str),
                        'CRYPTO_UNKNOWN_WALLET')
    return payload
