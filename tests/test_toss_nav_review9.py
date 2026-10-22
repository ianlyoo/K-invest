"""Row quantity/price/value consistency through real GETs and frozen scopes."""
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal

import pytest
import test_toss_nav_review7 as review7
from test_toss_nav import selected
from test_toss_nav_review7 import assert_blocked
from test_toss_nav_review8 import HOLDINGS, snapshot_with_inventory

http_examples = review7.http_examples
BAD_ROWS = [
    ('005930', 'quantity', '0'), ('005930', 'quantity', '101'), ('005930', 'lastPrice', '72001'),
    ('AAPL', 'quantity', '0'), ('AAPL', 'quantity', '10.0001'), ('AAPL', 'lastPrice', '178.51'),
    ('005930', 'zero-small-value', '0.5'), ('AAPL', 'zero-small-value', '0.005'),
    ('005930', 'amount-delta', '1.000000000000000001'),
    ('005930', 'amount-delta', '-1.000000000000000001'),
    ('AAPL', 'amount-delta', '0.010000000000000001'),
    ('AAPL', 'amount-delta', '-0.010000000000000001'),
]


def inventory_row(http_examples, symbol):
    overview = deepcopy(http_examples.examples[HOLDINGS]['withHoldings']['result'])
    row = next(row for row in overview['items'] if row['symbol'] == symbol)
    return overview, row


def set_market_value(overview, row, value):
    row['marketValue']['amount'] = value
    overview['marketValue']['amount'][row['currency'].lower()] = value


def assert_complete(data, *, samsung_quantity=Decimal('100'), apple_quantity=Decimal('10')):
    expected = float(samsung_quantity * Decimal('72000') + Decimal('5000000') +
                     (apple_quantity * Decimal('185.70') + Decimal('3500.5')) * Decimal('1380.5'))
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] == expected, nav['reason_codes']
        assert nav['result_status'] == 'complete' and nav['reason_codes'] == []
    assert data.collection.nav_source_quality_reasons == {}


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('symbol,mutation,value', BAD_ROWS, ids=[f'{s}/{m}/{v}' for s, m, v in BAD_ROWS])
def test_pass9_inconsistent_raw_holding_blocks_before_normalization(tmp_path, monkeypatch, http_examples,
                                                                 canonical, symbol, mutation, value):
    overview, row = inventory_row(http_examples, symbol)
    if mutation == 'zero-small-value':
        row['quantity'] = '0'
        set_market_value(overview, row, value)
    elif mutation == 'amount-delta':
        set_market_value(overview, row, format(Decimal(row['marketValue']['amount']) + Decimal(value), 'f'))
    else:
        row[mutation] = value
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert_blocked(data, 'INVALID_SOURCE_RESPONSE')
    assert data.collection.nav_source_quality_reasons['toss:primary'] == ['INVALID_SOURCE_RESPONSE']
    assert data.collection.health['toss:primary']['status'] == 'ok'
    assert data.collection.failed_sources == [] and data.collection.truncated_sources == []
    assert data.collection.nav_evidence['toss:primary']['market_calendars'] == {}
    if Decimal(row['quantity']) == 0:
        assert not any(holding.symbol == symbol for holding in data.collection.holdings)


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('quantity', ['0.333333', '0.00002'], ids=['round-up', 'round-down-to-zero'])
def test_pass9_fractional_us_share_rounded_to_cent_stays_complete(tmp_path, monkeypatch, http_examples,
                                                               canonical, quantity):
    overview, row = inventory_row(http_examples, 'AAPL')
    row['quantity'] = quantity
    product = Decimal(quantity) * Decimal(row['lastPrice'])
    rounded = product.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    assert product != rounded and abs(product - rounded) < Decimal('0.01')
    set_market_value(overview, row, format(rounded, 'f'))
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert_complete(data, apple_quantity=Decimal(quantity))
    component = next(c for c in data.row['components'] if c.get('symbol') == 'AAPL')
    assert component['market_value'] == float(rounded)
    assert component['native_market_value']['value'] == float(Decimal(quantity) * Decimal('185.70'))
    assert component['valuation_price_source'] == 'toss_prices'


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('symbol,delta', [('005930', '1'), ('005930', '-1'), ('AAPL', '0.01'), ('AAPL', '-0.01')])
def test_pass9_one_currency_unit_boundary_is_inclusive(tmp_path, monkeypatch, http_examples, canonical, symbol, delta):
    overview, row = inventory_row(http_examples, symbol)
    set_market_value(overview, row, format(Decimal(row['marketValue']['amount']) + Decimal(delta), 'f'))
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert_complete(data)


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('quantity', [0, 0.0, '0', '0.0000'], ids=['integer-zero', 'float-zero', 'string-zero', 'decimal-zero'])
def test_pass9_zero_quantity_with_zero_value_stays_complete(tmp_path, monkeypatch, http_examples, canonical, quantity):
    overview, row = inventory_row(http_examples, '005930')
    row['quantity'] = quantity
    set_market_value(overview, row, '0.0000')
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert not any(holding.symbol == '005930' for holding in data.collection.holdings)
    assert_complete(data, samsung_quantity=Decimal(0))


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
def test_pass9_openapi_control_satisfies_row_equality_and_stays_complete(tmp_path, monkeypatch, http_examples, canonical):
    overview = deepcopy(http_examples.examples[HOLDINGS]['withHoldings']['result'])
    for row in overview['items']:
        assert Decimal(row['marketValue']['amount']) == Decimal(row['quantity']) * Decimal(row['lastPrice'])
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert_complete(data)
