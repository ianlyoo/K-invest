"""Price drift regressions: synthetic collection through native NAV scopes."""
from decimal import Decimal

import pytest
from test_toss_nav import START, capture, selected
from test_toss_nav_review import MONDAY

from ledger_returns import scope_navs
from portfolio_engine import summarize_portfolio
from portfolio_store import snapshot_summary


def primary_holding(data, symbol='SYN'):
    return next(c for c in data.row['components'] if c['account'] == 'toss:primary'
                and c['component_type'] == 'holding' and c['symbol'] == symbol)


@pytest.mark.parametrize('currency,expected,legacy', [('USD', 4350, 4050), ('KRW', 1875, 1872)])
def test_pass4_price_drift_uses_same_quote_price_time_for_native_nav(tmp_path, monkeypatch, currency, expected, legacy):
    # US pre-market / KR after-market: holdings at 18:10:00, the quote at :04,
    # read at :05. Legacy collection/flow/crypto cutoffs must remain at :00.
    priced = MONDAY + 'T09:10:04Z'
    completed = MONDAY + 'T09:10:05Z'
    data = capture(tmp_path, monkeypatch, day=MONDAY, holding_currency=currency,
        quote_currency=currency, quote_price='12.5000', quote_stamp=priced, quote_read_at=completed)
    nav = selected(data.row)
    assert nav['total_value_krw'] == expected, nav['reason_codes']
    assert nav['result_status'] == 'complete' and nav['reason_codes'] == []
    assert selected(data.row, 'toss')['total_value_krw'] == expected + 3000
    component = primary_holding(data)
    assert component['valuation_price_source'] == 'toss_prices'
    assert component['valuation_price_decimal'] == '12.5000'
    assert component['priced_at'] == priced
    assert component['legacy_price_decimal'] == '11'
    assert component['quote_price_decimal'] == '12.5000'
    assert Decimal(component['price_drift_decimal']) == Decimal('1.5')
    assert component['native_market_value']['value'] == 25
    assert Decimal(component['native_market_value']['value_decimal']) == Decimal('25')
    assert component['native_market_value']['status'] == 'derived'
    assert component['reason_codes'] == []
    assert 'QUOTE_PRICE_MISMATCH' not in nav['reason_codes']
    assert data.collection.holdings[0].last_price == component['last_price'] == 11
    assert data.collection.holdings[0].market_value == component['market_value'] == 22
    assert data.row['valuation_at'] == data.row['observed_at'] == data.collection.fetched_at == MONDAY + 'T09:10:00Z'
    assert data.row['nav_evidence_completed_at'] == completed
    old_primary = data.legacy['by_account']['toss:primary']
    assert old_primary['total_krw'] == legacy
    assert data.legacy == summarize_portfolio(data.collection.holdings, data.collection.cash, data.fx)
    summary = snapshot_summary(data.legacy, data.collection, data.fx)
    assert all(summary[key] == value for key, value in data.legacy.items())
    assert summary['nav_bridge'] == data.row['nav_bridge'] == data.legacy['nav_bridge']
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']
    assert scope_navs([data.row], 'equity')[0] == data.row
    assert scope_navs([data.row], 'consolidated')[0]['total_value_krw'] is None
    assert all('native_market_value' not in c and 'valuation_price_source' not in c
               for c in data.row['components'] if c['provider'] == 'kis')


def test_pass4_one_missing_held_symbol_quote_makes_native_nav_unknown(tmp_path, monkeypatch):
    data = capture(tmp_path, monkeypatch, extra_holdings=[{
        'symbol': 'UNQUOTED', 'currency': 'USD', 'quantity': 3,
        'lastPrice': '4', 'marketValue': {'amount': 12}}])
    # cb06541 blocks the scope but incorrectly leaves native NAV valued from
    # legacy holdings. A missing quote must make the actual native metric unknown.
    native = data.row['account_nav']['toss:primary']['nav_native']['USD']
    assert native['value'] is None
    assert native['status'] == 'unknown' and 'MISSING_TOSS_QUOTE' in native['reason_codes']
    assert primary_holding(data)['native_market_value']['value'] == 20
    missing = primary_holding(data, 'UNQUOTED')
    assert missing['native_market_value']['value'] is None
    assert missing['priced_at'] is None and missing['quote_price_decimal'] is None
    assert missing['legacy_price_decimal'] == '4' and missing['market_value'] == 12
    assert ('prices', ['SYN', 'UNQUOTED']) in data.clients['primary'].calls
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] is None
        assert 'MISSING_TOSS_QUOTE' in nav['reason_codes']
        assert 'QUOTE_PRICE_MISMATCH' not in nav['reason_codes']
    secondary = scope_navs([data.row], 'toss_investmcp', account_references={'toss:secondary'})[0]
    assert secondary['total_value_krw'] == 3000 and secondary['reason_codes'] == []
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']


def test_pass4_good_quote_preserves_component_but_incomplete_inventory_blocks_native_nav(tmp_path, monkeypatch):
    data = capture(tmp_path, monkeypatch, extra_holdings=[{
        'symbol': 'EXTRA', 'currency': 'USD', 'quantity': 3, 'lastPrice': '4'}], primary_quotes=[
        {'symbol': 'SYN', 'currency': 'USD', 'lastPrice': '10', 'timestamp': START + 'T09:10:00Z'},
        {'symbol': 'EXTRA', 'currency': 'USD', 'lastPrice': '5', 'timestamp': START + 'T09:10:00Z'}])
    nav = selected(data.row)
    assert nav['total_value_krw'] is None
    assert 'INVALID_SOURCE_RESPONSE' in nav['reason_codes'] and nav['result_status'] == 'partial'
    component = primary_holding(data, 'EXTRA')
    assert component['market_value'] is None
    assert component['native_market_value']['value'] is None
    assert 'INVALID_SOURCE_RESPONSE' in component['reason_codes']
    assert component['legacy_price_decimal'] == '4' and component['quote_price_decimal'] == '5'
    assert 'MISSING_POSITION_VALUATION' in data.row['reason_codes']
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']
    assert data.row['nav_bridge'] == data.legacy['nav_bridge']


@pytest.mark.parametrize('change,reason', [
    ({'timestamp': None}, 'UNKNOWN_QUOTE_TIME'),
    ({'currency': 'KRW'}, 'QUOTE_CURRENCY_MISMATCH'),
    ({'lastPrice': '0'}, 'INVALID_QUOTE_PRICE'),
    ({'lastPrice': '-1'}, 'INVALID_QUOTE_PRICE'),
    ({'lastPrice': 'NaN'}, 'INVALID_QUOTE_PRICE'),
    ({'lastPrice': None}, 'INVALID_QUOTE_PRICE'),
    ({'timestamp': 'malformed'}, 'INVALID_QUOTE_TIME'),
])
def test_pass4_invalid_quote_makes_component_and_native_nav_unknown(tmp_path, monkeypatch, change, reason):
    quote = {'symbol': 'SYN', 'currency': 'USD', 'lastPrice': '10', 'timestamp': START + 'T09:10:00Z', **change}
    data = capture(tmp_path, monkeypatch, primary_quotes=[quote])
    native = data.row['account_nav']['toss:primary']['nav_native']['USD']
    assert native['value'] is None
    assert native['status'] == 'unknown' and reason in native['reason_codes']
    component = primary_holding(data)
    assert component['valuation_price_source'] == 'toss_prices'
    assert component['native_market_value']['value'] is None
    assert component['native_market_value']['status'] == 'unknown'
    assert component['priced_at'] == quote['timestamp']
    assert reason in component['reason_codes']
    assert component['legacy_price_decimal'] == '10'
    assert component['last_price'] == 10 and component['market_value'] == 20
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] is None and reason in nav['reason_codes']
        assert 'QUOTE_PRICE_MISMATCH' not in nav['reason_codes']
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']
