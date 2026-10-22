"""Strict internal Toss NAV contracts; legacy response views are untouched.

Shape/type contracts are pinned to the public OpenAPI 1.2.24 schemas:
https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
No runtime schema/network lookup is performed. Only sanitized evidence/reason
codes leave validation; a failed response never becomes a valuation input.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext

_SCHEMAS = {'AfterMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                       'singlePriceAuctionEndTime': {'oneOf': [{'format': 'date-time',
                                                                                'type': 'string'},
                                                                               {'type': 'null'}]},
                                       'startTime': {'format': 'date-time', 'type': 'string'}},
                        'required': ['startTime', 'endTime'],
                        'type': 'object'},
 'ApiResponse': {'properties': {'result': {}}, 'required': ['result'], 'type': 'object'},
 'BuyingPowerResponse': {'properties': {'cashBuyingPower': {'format': 'decimal',
                                                            'maxLength': 30,
                                                            'type': 'string'},
                                        'currency': {'$ref': '#/components/schemas/Currency'}},
                         'required': ['currency', 'cashBuyingPower'],
                         'type': 'object'},
 'Cost': {'properties': {'commission': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                         'tax': {'format': 'decimal', 'maxLength': 30, 'type': ['string', 'null']}},
          'required': ['commission'],
          'type': 'object'},
 'Currency': {'enum': ['KRW', 'USD'], 'type': 'string'},
 'DailyProfitLoss': {'properties': {'amount': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                    'rate': {'format': 'decimal', 'maxLength': 30, 'type': 'string'}},
                     'required': ['amount', 'rate'],
                     'type': 'object'},
 'ExchangeRateResponse': {'properties': {'baseCurrency': {'$ref': '#/components/schemas/Currency'},
                                         'basisPoint': {'format': 'decimal',
                                                        'maxLength': 30,
                                                        'type': 'string'},
                                         'midRate': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                         'quoteCurrency': {'$ref': '#/components/schemas/Currency'},
                                         'rate': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                         'rateChangeType': {'enum': ['UP', 'EQUAL', 'DOWN'],
                                                            'type': 'string'},
                                         'validFrom': {'format': 'date-time', 'type': 'string'},
                                         'validUntil': {'format': 'date-time', 'type': 'string'}},
                          'required': ['baseCurrency',
                                       'quoteCurrency',
                                       'rate',
                                       'midRate',
                                       'basisPoint',
                                       'rateChangeType',
                                       'validFrom',
                                       'validUntil'],
                          'type': 'object'},
 'HoldingsItem': {'properties': {'averagePurchasePrice': {'format': 'decimal',
                                                          'maxLength': 30,
                                                          'type': 'string'},
                                 'cost': {'$ref': '#/components/schemas/Cost'},
                                 'currency': {'$ref': '#/components/schemas/Currency'},
                                 'dailyProfitLoss': {'$ref': '#/components/schemas/DailyProfitLoss'},
                                 'lastPrice': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                 'marketCountry': {'$ref': '#/components/schemas/MarketCountry'},
                                 'marketValue': {'$ref': '#/components/schemas/MarketValue'},
                                 'name': {'type': 'string'},
                                 'profitLoss': {'$ref': '#/components/schemas/ProfitLoss'},
                                 'quantity': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                 'symbol': {'type': 'string'}},
                  'required': ['symbol',
                               'name',
                               'marketCountry',
                               'currency',
                               'quantity',
                               'lastPrice',
                               'averagePurchasePrice',
                               'marketValue',
                               'profitLoss',
                               'dailyProfitLoss',
                               'cost'],
                  'type': 'object'},
 'HoldingsOverview': {'properties': {'dailyProfitLoss': {'$ref': '#/components/schemas/OverviewDailyProfitLoss'},
                                     'items': {'items': {'$ref': '#/components/schemas/HoldingsItem'},
                                               'type': 'array'},
                                     'marketValue': {'$ref': '#/components/schemas/OverviewMarketValue'},
                                     'profitLoss': {'$ref': '#/components/schemas/OverviewProfitLoss'},
                                     'totalPurchaseAmount': {'allOf': [{'$ref': '#/components/schemas/Price'}]}},
                      'required': ['totalPurchaseAmount',
                                   'marketValue',
                                   'profitLoss',
                                   'dailyProfitLoss',
                                   'items'],
                      'type': 'object'},
 'IntegratedHour': {'properties': {'afterMarket': {'oneOf': [{'$ref': '#/components/schemas/AfterMarketSession'},
                                                             {'type': 'null'}]},
                                   'preMarket': {'oneOf': [{'$ref': '#/components/schemas/PreMarketSession'},
                                                           {'type': 'null'}]},
                                   'regularMarket': {'oneOf': [{'$ref': '#/components/schemas/RegularMarketSession'},
                                                               {'type': 'null'}]}},
                    'type': 'object'},
 'KrMarketCalendarResponse': {'properties': {'nextBusinessDay': {'$ref': '#/components/schemas/KrMarketDay'},
                                             'previousBusinessDay': {'$ref': '#/components/schemas/KrMarketDay'},
                                             'today': {'$ref': '#/components/schemas/KrMarketDay'}},
                              'required': ['today', 'previousBusinessDay', 'nextBusinessDay'],
                              'type': 'object'},
 'KrMarketDay': {'properties': {'date': {'format': 'date', 'type': 'string'},
                                'integrated': {'oneOf': [{'$ref': '#/components/schemas/IntegratedHour'},
                                                         {'type': 'null'}]}},
                 'required': ['date'],
                 'type': 'object'},
 'MarketCountry': {'enum': ['KR', 'US'], 'type': 'string'},
 'MarketValue': {'properties': {'amount': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                'amountAfterCost': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                'purchaseAmount': {'format': 'decimal', 'maxLength': 30, 'type': 'string'}},
                 'required': ['purchaseAmount', 'amount', 'amountAfterCost'],
                 'type': 'object'},
 'Order': {'properties': {'canceledAt': {'format': 'date-time', 'type': ['string', 'null']},
                          'currency': {'$ref': '#/components/schemas/Currency'},
                          'execution': {'allOf': [{'$ref': '#/components/schemas/OrderExecution'}],
                                        'type': 'object'},
                          'orderAmount': {'format': 'decimal', 'maxLength': 30, 'type': ['string', 'null']},
                          'orderId': {'type': 'string'},
                          'orderType': {'enum': ['LIMIT', 'MARKET'], 'type': 'string'},
                          'orderedAt': {'format': 'date-time', 'type': 'string'},
                          'price': {'format': 'decimal', 'maxLength': 30, 'type': ['string', 'null']},
                          'quantity': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                          'side': {'enum': ['BUY', 'SELL'], 'type': 'string'},
                          'status': {'$ref': '#/components/schemas/OrderStatus'},
                          'symbol': {'type': 'string'},
                          'timeInForce': {'enum': ['DAY', 'CLS', 'OPG'], 'type': 'string'}},
           'required': ['orderId',
                        'symbol',
                        'side',
                        'orderType',
                        'timeInForce',
                        'status',
                        'quantity',
                        'currency',
                        'orderedAt',
                        'execution'],
           'type': 'object'},
 'OrderExecution': {'properties': {'averageFilledPrice': {'format': 'decimal',
                                                          'maxLength': 30,
                                                          'type': ['string', 'null']},
                                   'commission': {'format': 'decimal',
                                                  'maxLength': 30,
                                                  'type': ['string', 'null']},
                                   'filledAmount': {'format': 'decimal',
                                                    'maxLength': 30,
                                                    'type': ['string', 'null']},
                                   'filledAt': {'format': 'date-time', 'type': ['string', 'null']},
                                   'filledQuantity': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                   'settlementDate': {'format': 'date', 'type': ['string', 'null']},
                                   'tax': {'format': 'decimal', 'maxLength': 30, 'type': ['string', 'null']}},
                    'required': ['filledQuantity',
                                 'averageFilledPrice',
                                 'filledAmount',
                                 'commission',
                                 'tax',
                                 'filledAt',
                                 'settlementDate'],
                    'type': 'object'},
 'OrderStatus': {'enum': ['PENDING',
                          'PENDING_CANCEL',
                          'PENDING_REPLACE',
                          'PARTIAL_FILLED',
                          'FILLED',
                          'CANCELED',
                          'REJECTED',
                          'CANCEL_REJECTED',
                          'REPLACE_REJECTED',
                          'REPLACED'],
                 'type': 'string'},
 'OverviewDailyProfitLoss': {'properties': {'amount': {'allOf': [{'$ref': '#/components/schemas/Price'}]},
                                            'rate': {'format': 'decimal', 'maxLength': 30, 'type': 'string'}},
                             'required': ['amount', 'rate'],
                             'type': 'object'},
 'OverviewMarketValue': {'properties': {'amount': {'allOf': [{'$ref': '#/components/schemas/Price'}]},
                                        'amountAfterCost': {'allOf': [{'$ref': '#/components/schemas/Price'}]}},
                         'required': ['amount', 'amountAfterCost'],
                         'type': 'object'},
 'OverviewProfitLoss': {'properties': {'amount': {'allOf': [{'$ref': '#/components/schemas/Price'}]},
                                       'amountAfterCost': {'allOf': [{'$ref': '#/components/schemas/Price'}]},
                                       'rate': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                       'rateAfterCost': {'format': 'decimal',
                                                         'maxLength': 30,
                                                         'type': 'string'}},
                        'required': ['amount', 'amountAfterCost', 'rate', 'rateAfterCost'],
                        'type': 'object'},
 'PaginatedOrderResponse': {'properties': {'hasNext': {'type': 'boolean'},
                                           'nextCursor': {'type': ['string', 'null']},
                                           'orders': {'items': {'$ref': '#/components/schemas/Order'},
                                                      'type': 'array'}},
                            'required': ['orders', 'nextCursor', 'hasNext'],
                            'type': 'object'},
 'PreMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                     'singlePriceAuctionStartTime': {'oneOf': [{'format': 'date-time',
                                                                                'type': 'string'},
                                                                               {'type': 'null'}]},
                                     'startTime': {'format': 'date-time', 'type': 'string'}},
                      'required': ['startTime', 'endTime'],
                      'type': 'object'},
 'Price': {'properties': {'krw': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                          'usd': {'format': 'decimal', 'maxLength': 30, 'type': ['string', 'null']}},
           'required': ['krw'],
           'type': 'object'},
 'PriceResponse': {'properties': {'currency': {'$ref': '#/components/schemas/Currency'},
                                  'lastPrice': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                                  'symbol': {'type': 'string'},
                                  'timestamp': {'format': 'date-time', 'type': ['string', 'null']}},
                   'required': ['symbol', 'lastPrice', 'currency'],
                   'type': 'object'},
 'ProfitLoss': {'properties': {'amount': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                               'amountAfterCost': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                               'rate': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                               'rateAfterCost': {'format': 'decimal', 'maxLength': 30, 'type': 'string'}},
                'required': ['amount', 'amountAfterCost', 'rate', 'rateAfterCost'],
                'type': 'object'},
 'RegularMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                         'singlePriceAuctionStartTime': {'oneOf': [{'format': 'date-time',
                                                                                    'type': 'string'},
                                                                                   {'type': 'null'}]},
                                         'startTime': {'format': 'date-time', 'type': 'string'}},
                          'required': ['startTime', 'endTime'],
                          'type': 'object'},
 'UsAfterMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                         'startTime': {'format': 'date-time', 'type': 'string'}},
                          'required': ['startTime', 'endTime'],
                          'type': 'object'},
 'UsDayMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                       'startTime': {'format': 'date-time', 'type': 'string'}},
                        'required': ['startTime', 'endTime'],
                        'type': 'object'},
 'UsMarketCalendarResponse': {'properties': {'nextBusinessDay': {'$ref': '#/components/schemas/UsMarketDay'},
                                             'previousBusinessDay': {'$ref': '#/components/schemas/UsMarketDay'},
                                             'today': {'$ref': '#/components/schemas/UsMarketDay'}},
                              'required': ['today', 'previousBusinessDay', 'nextBusinessDay'],
                              'type': 'object'},
 'UsMarketDay': {'properties': {'afterMarket': {'oneOf': [{'$ref': '#/components/schemas/UsAfterMarketSession'},
                                                          {'type': 'null'}]},
                                'date': {'format': 'date', 'type': 'string'},
                                'dayMarket': {'oneOf': [{'$ref': '#/components/schemas/UsDayMarketSession'},
                                                        {'type': 'null'}]},
                                'preMarket': {'oneOf': [{'$ref': '#/components/schemas/UsPreMarketSession'},
                                                        {'type': 'null'}]},
                                'regularMarket': {'oneOf': [{'$ref': '#/components/schemas/UsRegularMarketSession'},
                                                            {'type': 'null'}]}},
                 'required': ['date'],
                 'type': 'object'},
 'UsPreMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                       'startTime': {'format': 'date-time', 'type': 'string'}},
                        'required': ['startTime', 'endTime'],
                        'type': 'object'},
 'UsRegularMarketSession': {'properties': {'endTime': {'format': 'date-time', 'type': 'string'},
                                           'startTime': {'format': 'date-time', 'type': 'string'}},
                            'required': ['startTime', 'endTime'],
                            'type': 'object'}}


# OpenAPI 1.2.24 /stocks, accessed 2026-10-13. The published ETF example
# omits englishName; validate it when supplied, while name/ISIN/market remain required.
_SCHEMAS.update({'KrMarketDetail': {'properties': {'krxTradingSuspended': {'type': 'boolean'},
                                   'liquidationTrading': {'type': 'boolean'},
                                   'nxtSupported': {'type': 'boolean'},
                                   'nxtTradingSuspended': {'type': ['boolean', 'null']}},
                    'required': ['liquidationTrading', 'nxtSupported', 'krxTradingSuspended'],
                    'type': 'object'},
 'StockInfo': {'properties': {'currency': {'$ref': '#/components/schemas/Currency'},
                              'delistDate': {'format': 'date', 'type': ['string', 'null']},
                              'englishName': {'type': 'string'},
                              'isCommonShare': {'type': 'boolean'},
                              'isinCode': {'type': 'string'},
                              'koreanMarketDetail': {'oneOf': [{'$ref': '#/components/schemas/KrMarketDetail'},
                                                               {'type': 'null'}]},
                              'leverageFactor': {'format': 'decimal',
                                                 'maxLength': 30,
                                                 'type': ['string', 'null']},
                              'listDate': {'format': 'date', 'type': ['string', 'null']},
                              'market': {'enum': ['KOSPI',
                                                  'KOSDAQ',
                                                  'NYSE',
                                                  'NASDAQ',
                                                  'AMEX',
                                                  'KR_ETC',
                                                  'US_ETC'],
                                         'type': 'string'},
                              'name': {'type': 'string'},
                              'securityType': {'enum': ['STOCK',
                                                        'FOREIGN_STOCK',
                                                        'DEPOSITARY_RECEIPT',
                                                        'INFRASTRUCTURE_FUND',
                                                        'REIT',
                                                        'ETF',
                                                        'FOREIGN_ETF',
                                                        'ETN',
                                                        'STOCK_WARRANTS'],
                                               'type': 'string'},
                              'sharesOutstanding': {'format': 'decimal', 'maxLength': 30, 'type': 'string'},
                              'status': {'enum': ['SCHEDULED', 'ACTIVE', 'DELISTED'], 'type': 'string'},
                              'symbol': {'type': 'string'}},
               'required': ['symbol',
                            'name',
                            'isinCode',
                            'market',
                            'securityType',
                            'isCommonShare',
                            'status',
                            'currency',
                            'sharesOutstanding'],
               'type': 'object'}})


@dataclass
class Validation:
    value: object = None
    reason_codes: list[str] = field(default_factory=list)
    quotes: dict[str, dict] = field(default_factory=dict)


class EnvelopeResult(dict):
    """Same legacy dict/JSON output, retaining the envelope for internal FX use."""
    def __init__(self, envelope):
        super().__init__(envelope.get('result', {}))
        self.nav_envelope = envelope


def incomplete_envelope(value):
    """Inspect every nested object before any result projection/normalization."""
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).replace('_', '').replace('-', '').lower()
            if (name.startswith('error') and bool(item)
                    or name in ('partial', 'ispartial', 'truncated', 'istruncated', 'hasnext', 'hasnextpage', 'hasmore')
                    and item is not False
                    or name in ('nextcursor', 'nextpagecursor', 'nextpage', 'continuationtoken')
                    and item is not None and item != ''):
                return True
            if incomplete_envelope(item):
                return True
    elif isinstance(value, list):
        return any(incomplete_envelope(item) for item in value)
    return False


def _decimal(value):
    if not isinstance(value, str) or re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', value) is None:
        return None
    try:
        result = Decimal(value)
        return result if result.is_finite() and value == value.strip() and ',' not in value else None
    except InvalidOperation:
        return None


def _matches(value, schema, field_name=''):
    if '$ref' in schema:
        name = schema['$ref'].rsplit('/', 1)[1]
        return _matches(value, _SCHEMAS[name], name)
    for key in ('allOf', 'oneOf', 'anyOf'):
        if key in schema:
            checks = [_matches(value, part, field_name) for part in schema[key]]
            if not (all(checks) if key == 'allOf' else sum(checks) == 1 if key == 'oneOf' else any(checks)):
                return False
    types = schema.get('type')
    if types is not None:
        types = types if isinstance(types, list) else [types]
        valid_type = any({'object': isinstance(value, dict), 'array': isinstance(value, list),
                          'string': isinstance(value, str), 'boolean': isinstance(value, bool),
                          'null': value is None}.get(kind, False) for kind in types)
        # Preserve the existing handling of legitimate numeric zero quantities.
        zero = field_name == 'HoldingsItem.quantity' and type(value) in (int, float) and value == 0
        if not valid_type and not zero:
            return False
        if zero:
            return True
    if value is None:
        return True
    if 'enum' in schema and value not in schema['enum']:
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get('required', [])):
            return False
        return all(_matches(value[key], definition, f'{field_name}.{key}') for key, definition
                   in schema.get('properties', {}).items() if key in value)
    if isinstance(value, list):
        return all(_matches(item, schema.get('items', {})) for item in value)
    if isinstance(value, str):
        if len(value) > schema.get('maxLength', len(value)):
            return False
        format_name = schema.get('format')
        if format_name == 'decimal':
            return _decimal(value) is not None
        try:
            if format_name == 'date':
                return date.fromisoformat(value).isoformat() == value
            if format_name == 'date-time':
                return (re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                                     r'(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})', value) is not None
                        and datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is not None)
        except ValueError:
            return False
    return True


def _identity(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def canonical_symbol(value):
    """Match Toss holding normalization: trim and uppercase, retain prefixes."""
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _inventory_consistent(overview):
    """Reconcile the complete, typed inventory against both currency totals.

    OpenAPI examples sum exactly; no rounding tolerance is documented. Null
    overview amounts are valid only for currencies with no inventory rows.
    Extend precision to preserve all decimal places and summation carry digits.
    """
    for currency, key in (('KRW', 'krw'), ('USD', 'usd')):
        rows = [row for row in overview['items'] if row['currency'] == currency]
        for totals, item_field in ((overview['marketValue']['amount'], 'amount'),
                                  (overview['totalPurchaseAmount'], 'purchaseAmount')):
            reported = totals.get(key)
            if reported is None:
                if rows:
                    return False
                continue
            amounts = [row['marketValue'][item_field] for row in rows]
            with localcontext() as context:
                # Each decimal string bounds both integer and fraction widths;
                # twice the widest input plus count digits prevents rounding.
                context.prec = max(28, 2 * max(map(len, [reported, *amounts])) + len(str(len(amounts) + 1)))
                if sum((Decimal(value) for value in amounts), Decimal(0)) != Decimal(reported):
                    return False
    return True


def _holding_consistent(row):
    """Check a typed row before normalization can discard a zero position.

    Allow at most one currency unit of rounding for nonzero quantities; zero
    quantities must have exactly zero value. Preserve full multiplication and
    subtraction precision, independently of the default Decimal precision.
    """
    quantity, price, amount = str(row['quantity']), row['lastPrice'], row['marketValue']['amount']
    with localcontext() as context:
        context.prec = max(28, len(quantity) + len(price) + len(amount) + 4)
        quantity, price, amount = Decimal(quantity), Decimal(price), Decimal(amount)
        if quantity == 0:
            return amount == 0
        unit = Decimal('1' if row['currency'] == 'KRW' else '0.01')
        return abs(amount - quantity * price) <= unit


def calendar_shape(value):
    """Both countries' stored regular sessions use the flat date/session view."""
    schema = _SCHEMAS['UsMarketCalendarResponse']
    return _matches(value, schema) and not incomplete_envelope(value)


def validate_toss_response(kind, payload, *, currency=None, symbols=None, country=None, order_request=None):
    """Reject unsupported input shapes without interrupting the collection."""
    try:
        return _validate_response(kind, payload, currency=currency, symbols=symbols, country=country,
                                  order_request=order_request)
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        reasons = ['INVALID_SOURCE_RESPONSE']
        if kind == 'orders' and (order_request or {}).get('status', 'OPEN') == 'OPEN':
            reasons.append('TOSS_OPEN_ORDERS_UNVERIFIED')
        elif kind == 'calendar':
            reasons.append('MARK_FRESHNESS_UNVERIFIED')
        return Validation(reason_codes=reasons)


def _validate_response(kind, payload, *, currency=None, symbols=None, country=None, order_request=None):
    """One entry point/contract per response, before any row can be selected.

    Duplicate detection precedes validity/binding selection and is symbol-wide.
    Request bindings and endpoint semantics follow the same envelope/type gate.
    ``quotes`` is sanitized diagnostic evidence, never a successful response
    when reason_codes is nonempty. Legacy callers still use their old values.
    """
    envelope = getattr(payload, 'nav_envelope', payload)
    wrapped = isinstance(envelope, dict) and 'result' in envelope
    # Holdings/orders retain their envelope; the other public client methods
    # return the documented result list/dict. Internal evidence reads may keep
    # the full envelope, which must pass the same metadata checks.
    result = envelope['result'] if wrapped else payload
    reasons, quotes = [], {}
    schemas = {'holdings': 'HoldingsOverview', 'prices': None, 'buying_power': 'BuyingPowerResponse',
               'orders': 'PaginatedOrderResponse', 'calendar': 'KrMarketCalendarResponse' if country == 'KR'
               else 'UsMarketCalendarResponse', 'exchange_rate': 'ExchangeRateResponse'}
    schema = {'type': 'array', 'items': {'$ref': '#/components/schemas/' + (
        'StockInfo' if kind == 'stock_info' else 'PriceResponse')}} if kind in ('prices', 'stock_info') else {
        '$ref': '#/components/schemas/' + schemas[kind]}
    checked_envelope = envelope
    if kind == 'orders' and order_request and wrapped and isinstance(result, dict):
        # CLOSED history is paginated. Only its documented paging fields may
        # announce a next page; nested partial/error flags still invalidate it.
        checked_envelope = {**envelope, 'result': {k: v for k, v in result.items()
                                                 if k not in ('hasNext', 'nextCursor')}}
    envelope_ok = (wrapped or kind not in ('holdings', 'orders', 'stock_info')) and not incomplete_envelope(checked_envelope)
    shape_ok = _matches(result, schema)
    if not envelope_ok or not shape_ok:
        reasons.append('INVALID_SOURCE_RESPONSE')
    if kind == 'stock_info' and shape_ok:
        requested = symbols or {}
        identities = [canonical_symbol(row['symbol']) for row in result]
        if (len(set(identities)) != len(identities) or set(identities) != set(requested)
                or not 1 <= len(result) <= 200):
            reasons.append('INVALID_INSTRUMENT_IDENTITY')
        for row in result:
            if (row['symbol'] not in requested or row['currency'] != requested.get(row['symbol'])
                    or not _identity(row['name']) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', row['isinCode'])
                    or row['currency'] == 'KRW' and row['market'] not in ('KOSPI', 'KOSDAQ', 'KR_ETC')
                    or row['currency'] == 'USD' and row['market'] not in ('NASDAQ', 'NYSE', 'AMEX', 'US_ETC')):
                reasons.append('INVALID_INSTRUMENT_BINDING')
    if kind in ('holdings', 'prices'):
        rows = result.get('items') if kind == 'holdings' and isinstance(result, dict) else result
        rows = rows if isinstance(rows, list) else []
        seen, duplicate_symbols = set(), set()
        for row in rows:
            if isinstance(row, dict) and canonical_symbol(row.get('symbol')):
                symbol = canonical_symbol(row['symbol'])
                if symbol in seen:
                    duplicate_symbols.add(symbol)
                seen.add(symbol)
            if (not isinstance(row, dict) or not canonical_symbol(row.get('symbol'))
                    or not _identity(row.get('currency'))):
                reasons.append('INVALID_SOURCE_RESPONSE')
        if duplicate_symbols:
            reasons.append('DUPLICATE_HOLDING' if kind == 'holdings' else 'DUPLICATE_QUOTE')
        if kind == 'holdings':
            from portfolio_model import toss_holdings_quality
            reasons.extend(toss_holdings_quality({'result': {'items': rows}}))
            if any(isinstance(row, dict) and {'KR': 'KRW', 'US': 'USD'}.get(
                    row.get('marketCountry')) != row.get('currency') for row in rows):
                reasons.append('INVALID_SOURCE_RESPONSE')
            if shape_ok and not _inventory_consistent(result):
                reasons.append('INVALID_SOURCE_RESPONSE')
            if shape_ok and any(not _holding_consistent(row) for row in rows):
                reasons.append('INVALID_SOURCE_RESPONSE')
        else:
            requested = {canonical_symbol(symbol): currency for symbol, currency in (symbols or {}).items()}
            for row in rows:
                if not isinstance(row, dict) or not canonical_symbol(row.get('symbol')):
                    continue
                symbol = canonical_symbol(row['symbol'])
                if symbol not in requested:
                    reasons.append('CURRENCY_BINDING_MISMATCH')
                    continue
                local = []
                if row.get('currency') != requested[symbol]:
                    local.extend(['CURRENCY_BINDING_MISMATCH', 'QUOTE_CURRENCY_MISMATCH'])
                price = _decimal(row.get('lastPrice'))
                if price is None or price <= 0:
                    local.append('INVALID_QUOTE_PRICE')
                stamp = row.get('timestamp')
                if stamp is None:
                    local.append('UNKNOWN_QUOTE_TIME')
                elif not _matches(stamp, {'type': 'string', 'format': 'date-time'}):
                    local.append('INVALID_QUOTE_TIME')
                if not _matches(row, _SCHEMAS['PriceResponse']):
                    local.append('INVALID_SOURCE_RESPONSE')
                reasons.extend(local)
                quotes[symbol] = {'currency': row.get('currency'), 'last_price': row.get('lastPrice'),
                                  'priced_at': stamp, **({'reason_codes': local} if local else {})}
            for symbol in duplicate_symbols & requested.keys():
                quotes[symbol] = {'reason_codes': ['DUPLICATE_QUOTE']}
            if not envelope_ok or not shape_ok:
                for symbol in requested:
                    quotes.setdefault(symbol, {'reason_codes': ['INVALID_SOURCE_RESPONSE']})
                    quotes[symbol].setdefault('reason_codes', []).append('INVALID_SOURCE_RESPONSE')
            if set(requested) - seen:
                reasons.append('MISSING_TOSS_QUOTE')
    elif kind == 'buying_power' and isinstance(result, dict):
        if result.get('currency') != currency:
            reasons.append('CURRENCY_BINDING_MISMATCH')
        amount = _decimal(result.get('cashBuyingPower'))
        if amount is None or amount < 0:
            reasons.append('INVALID_SOURCE_RESPONSE')
        if amount is None:
            reasons.append('MISSING_CASH_BALANCE')
    elif kind == 'orders':
        rows = result.get('orders') if isinstance(result, dict) else None
        rows = rows if isinstance(rows, list) else []
        ids = [row.get('orderId') if isinstance(row, dict) else None for row in rows]
        if any(not _identity(value) for value in ids) or len(set(ids)) != len(ids):
            reasons.append('INVALID_SOURCE_RESPONSE')
        if order_request:
            reasons.extend(_order_page_reasons(result, order_request, shape_ok))
        # A rejected CLOSED history page is a fill-coverage failure; it says
        # nothing about OPEN orders (the NAV cash basis) and is not labelled so.
        if reasons and (order_request or {}).get('status', 'OPEN') == 'OPEN':
            reasons.append('TOSS_OPEN_ORDERS_UNVERIFIED')
    elif kind == 'calendar':
        from daily_nav import calendar_evidence
        if isinstance(result, dict) and 'country' in result and result['country'] != country:
            reasons.append('CURRENCY_BINDING_MISMATCH')
        calendar = calendar_evidence(result, country) if not reasons else None
        if calendar is None:
            reasons.extend(['INVALID_SOURCE_RESPONSE', 'MARK_FRESHNESS_UNVERIFIED'])
        result = calendar
    elif kind == 'exchange_rate' and isinstance(result, dict):
        if result.get('baseCurrency') != currency or result.get('quoteCurrency') != 'KRW':
            reasons.append('CURRENCY_BINDING_MISMATCH')
        if any(_decimal(result.get(key)) is None or _decimal(result[key]) <= 0 for key in ('rate', 'midRate')):
            reasons.append('INVALID_SOURCE_RESPONSE')
    reasons = list(dict.fromkeys(reasons))
    return Validation(None if reasons else result, reasons, quotes)


def _order_page_reasons(result, request, shape_ok):
    """Fill evidence uses the same Order schema as OPEN NAV validation.

    Public OpenAPI 1.2.24, accessed 2026-10-13. Quantities/amounts are cumulative
    per order, not individual executions. Missing costs never mean zero.
    """
    if not shape_ok:
        return ['INVALID_FILL_RESPONSE']
    rows, more, cursor = result['orders'], result['hasNext'], result['nextCursor']
    group = request['status']
    allowed = ({'PENDING', 'PARTIAL_FILLED', 'PENDING_CANCEL', 'PENDING_REPLACE'} if group == 'OPEN'
               else {'FILLED', 'CANCELED', 'REJECTED', 'REPLACED', 'CANCEL_REJECTED',
                     'REPLACE_REJECTED', 'PARTIAL_FILLED'})
    reasons = []
    if (more and (not rows or not _identity(cursor)) or not more and cursor is not None
            or group == 'OPEN' and more or group == 'CLOSED' and len(rows) > request['limit']):
        reasons.append('FILL_PAGING_INCONSISTENT')
    ids = [row['orderId'] for row in rows]
    if len(set(ids)) != len(ids) or set(ids) & set(request.get('seen_ids', ())):
        reasons.append('DUPLICATE_FILL_ORDER')
    observed = datetime.fromisoformat(request['observed_at'].replace('Z', '+00:00'))
    for row in rows:
        if (row['status'] not in allowed or not canonical_symbol(row['symbol'])
                or request.get('symbol') and canonical_symbol(row['symbol']) != canonical_symbol(request['symbol'])):
            reasons.append('FILL_REQUEST_BINDING_MISMATCH')
        execution = row['execution']
        quantity, filled = _decimal(row['quantity']), _decimal(execution['filledQuantity'])
        ordered = datetime.fromisoformat(row['orderedAt'].replace('Z', '+00:00'))
        # OpenAPI 1.2.24 (accessed 2026-10-15): Order.orderAmount "주문 금액 (USD). 금액 기반
        # US 시장가 매수 주문에만 해당. 그 외 null". Live CLOSED pages (observed
        # 2026-10-16) carry it on LIMIT, SELL and KRW orders too, so its presence marks
        # no amount order. It bounds OrderExecution.filledAmount ("총 체결 금액") within a
        # cent only on a MARKET BUY USD order, whose market-set filledQuantity the ordered
        # quantity (may be 0) does not bound. Elsewhere it is informational: live LIMIT
        # SELL fills exceed it.
        amount_buy = (row.get('orderAmount') is not None and row['orderType'] == 'MARKET'
                      and row['side'] == 'BUY' and row['currency'] == 'USD')
        # USD quantities may be fractional to 8 dp on any side and type; KRW ones are whole.
        places = 0 if row['currency'] == 'KRW' else 8
        if (filled < 0 or ordered > observed
                or any(len(text.partition('.')[2].rstrip('0')) > places
                       for text in (row['quantity'], execution['filledQuantity']))):
            reasons.append('INVALID_FILL_TOTALS')
        elif amount_buy:
            amount = _decimal(row['orderAmount'])
            gross = Decimal(0) if execution['filledAmount'] is None else _decimal(execution['filledAmount'])
            if (amount is None or gross is None or amount <= 0 or gross > amount + Decimal('.01')
                    or quantity < 0 or row['status'] == 'FILLED' and filled <= 0):
                reasons.append('INVALID_FILL_TOTALS')
        elif quantity <= 0 or filled > quantity or row['status'] == 'FILLED' and filled != quantity:
            reasons.append('INVALID_FILL_TOTALS')
        if filled > 0:
            values = {k: _decimal(execution[k]) for k in ('filledAmount', 'averageFilledPrice', 'commission', 'tax')}
            if any(value is None or value < 0 for value in values.values()):
                reasons.append('FILL_AMOUNT_OR_COST_MISSING')
            elif values['averageFilledPrice'] <= 0 or values['filledAmount'] <= 0:
                reasons.append('INVALID_FILL_TOTALS')
            else:
                # filledQuantity × averageFilledPrice reconciles with filledAmount within the
                # API's own precision. Toss truncates the average at 6 dp and strips trailing
                # zeros (every observed live filled row fits), so its written
                # scale means nothing and each unit filled is off by under 1e-6; filledAmount
                # within half a minor unit; a numerically fractional quantity (8 dp) off by under
                # 1e-8 at the price, a whole one an exact count. Past 5,000 units the band exceeds
                # a cent but stays under 1 ppm of the fill.
                with localcontext() as ctx:
                    ctx.prec = 80
                    price, minor = values['averageFilledPrice'], Decimal(1) if row['currency'] == 'KRW' else Decimal('.01')
                    tolerance = filled * Decimal('1e-6') + minor / 2
                    if filled != filled.to_integral_value():
                        tolerance += price * Decimal('1e-8')
                    if abs(filled * price - values['filledAmount']) > tolerance:
                        reasons.append('INVALID_FILL_TOTALS')
            stamp = execution['filledAt']
            if stamp is None:
                reasons.append('FILL_TIME_MISSING')
            else:
                when = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if not ordered <= when <= observed:
                    reasons.append('INVALID_FILL_TIME')
        elif any(execution[k] is not None and _decimal(execution[k]) != 0
                 for k in ('filledAmount', 'commission', 'tax')):
            # Any decimal zero ("0.000000" too) is zero; only a nonzero cost is invalid.
            reasons.append('INVALID_FILL_TOTALS')
    return reasons


def validate_fx_input(mark, currency):
    """Toss envelopes use the strict FX contract; alternative sources keep theirs.

    Alternate FX source marks are rate/priced_at dictionaries or scalar rates.
    Their selected values are never replaced, but bad metadata/rates are still
    rejected. A nullable or absent scalar price time retains the freshness guard.
    """
    if hasattr(mark, 'nav_envelope'):
        return validate_toss_response('exchange_rate', mark.nav_envelope, currency=currency)
    if isinstance(mark, dict) and any(key in mark for key in (
            'validFrom', 'validUntil', 'baseCurrency', 'quoteCurrency', 'result')):
        return validate_toss_response('exchange_rate', mark, currency=currency)
    rate = mark.get('rate') if isinstance(mark, dict) else mark
    numeric = Decimal(str(rate)) if type(rate) in (int, float) else _decimal(rate)
    invalid = numeric is None or not numeric.is_finite() or numeric <= 0 or incomplete_envelope(mark)
    if isinstance(mark, dict):
        invalid |= 'priced_at' not in mark or mark['priced_at'] is not None and not _matches(
            mark['priced_at'], {'type': 'string', 'format': 'date-time'})
    return Validation(reason_codes=['INVALID_SOURCE_RESPONSE'] if invalid else [])
