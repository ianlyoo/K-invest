"""Inventory reconciliation through real Toss GETs over MockTransport.

Failing cases must block on inventory evidence before any native NAV is usable.
The genuine-empty and public-example controls intentionally pass on d7deeab.
"""
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest
import test_toss_nav_review7 as review7
from test_toss_nav import selected
from test_toss_nav_review7 import assert_blocked, transport_snapshot

http_examples = review7.http_examples
HOLDINGS = '/api/v1/holdings'
FIELDS = ('market', 'purchase')
MISMATCHES = [(field, currency, 'one-decimal-unit') for field in FIELDS for currency in ('krw', 'usd')]
MISMATCHES += [(field, 'usd', mismatch) for field in FIELDS for mismatch in ('null-total', 'missing-total')]


def overview_totals(overview, field):
    return overview['marketValue']['amount'] if field == 'market' else overview['totalPurchaseAmount']


def snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical):
    http_examples.overrides[(HOLDINGS, 'withHoldings')] = {'result': overview}
    data = transport_snapshot(http_examples, tmp_path, monkeypatch, canonical=canonical)
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']
    assert data.row['nav_bridge'] == data.legacy['nav_bridge']
    assert any(path == HOLDINGS for path, _ in http_examples.calls)
    return data


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('mutation', ['empty-items', 'remove-kr', 'remove-us'])
def test_pass8_missing_inventory_items_cannot_certify_nav(tmp_path, monkeypatch, http_examples, canonical, mutation):
    overview = deepcopy(http_examples.examples[HOLDINGS]['withHoldings']['result'])
    if mutation == 'empty-items':
        overview['items'] = []
    else:
        country = 'KR' if mutation == 'remove-kr' else 'US'
        overview['items'] = [row for row in overview['items'] if row['marketCountry'] != country]
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert_blocked(data, 'INVALID_SOURCE_RESPONSE')
    assert data.collection.nav_source_quality_reasons['toss:primary'] == ['INVALID_SOURCE_RESPONSE']
    assert data.collection.nav_evidence['toss:primary']['market_calendars'] == {}
    # The transport returns only requested quotes, so quote binding is valid;
    # inventory reconciliation is the reason this account becomes unknown.
    assert 'CURRENCY_BINDING_MISMATCH' not in selected(data.row)['reason_codes']


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('field,currency,mismatch', MISMATCHES)
def test_pass8_each_currency_and_total_requires_exact_item_sum(tmp_path, monkeypatch, http_examples,
                                                              canonical, field, currency, mismatch):
    overview = deepcopy(http_examples.examples[HOLDINGS]['withHoldings']['result'])
    totals = overview_totals(overview, field)
    if mismatch == 'missing-total':
        totals.pop(currency)
    else:
        # The discrepancy is below binary-float resolution but is still a
        # documented decimal unit; exact source reconciliation must detect it.
        totals[currency] = None if mismatch == 'null-total' else format(
            Decimal(totals[currency]) + Decimal('0.000000000000000001'), 'f')
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    assert_blocked(data, 'INVALID_SOURCE_RESPONSE')
    assert data.collection.nav_source_quality_reasons['toss:primary'] == ['INVALID_SOURCE_RESPONSE']


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('totals_kind', ['zero', 'null-usd', 'mixed', 'omitted-usd'])
def test_pass8_genuinely_empty_inventory_stays_complete(tmp_path, monkeypatch, http_examples, canonical, totals_kind):
    overview = deepcopy(http_examples.examples[HOLDINGS]['emptyHoldings']['result'])
    for field in FIELDS:
        totals = overview_totals(overview, field)
        # OpenAPI requires decimal KRW; USD is optional/nullable. Preserve those
        # shapes while checking zero/null/omitted totals for the empty inventory.
        totals.update(krw='0.0000', usd=None if totals_kind == 'null-usd'
                      or totals_kind == 'mixed' and field == 'purchase' else '0.0000')
        if totals_kind == 'omitted-usd':
            totals.pop('usd')
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    expected = float(Decimal('5000000') + Decimal('3500.5') * Decimal('1380.5'))
    assert data.collection.holdings == []
    assert data.collection.nav_source_quality_reasons == {}
    assert not any(path == '/api/v1/prices' or path.startswith('/api/v1/market-calendar/')
                   for path, _ in http_examples.calls)
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] == expected and nav['result_status'] == 'complete'
        assert nav['reason_codes'] == []


@pytest.mark.parametrize('canonical', [False, True], ids=['envelope-evidence', 'canonical-client'])
@pytest.mark.parametrize('amount_format', ['openapi-example', 'decimal-normalized', 'full-precision'])
def test_pass8_complete_openapi_inventory_stays_complete(tmp_path, monkeypatch, http_examples, canonical, amount_format):
    overview = deepcopy(http_examples.examples[HOLDINGS]['withHoldings']['result'])
    if amount_format == 'decimal-normalized':
        for field in FIELDS:
            for currency, value in overview_totals(overview, field).items():
                overview_totals(overview, field)[currency] = format(Decimal(value), '.6f')
    elif amount_format == 'full-precision':
        # All 29 significant digits fit the documented 30-character decimal
        # format. A default 28-digit arithmetic context must not round the sum.
        value = '12345678901234567890.123456789'
        row = next(row for row in overview['items'] if row['currency'] == 'USD')
        row['marketValue'].update(amount=value, purchaseAmount=value)
        with localcontext() as context:
            context.prec = 60
            row['lastPrice'] = format(Decimal(value) / Decimal(row['quantity']), 'f')
        for field in FIELDS:
            overview_totals(overview, field)['usd'] = value
    data = snapshot_with_inventory(http_examples, tmp_path, monkeypatch, overview, canonical)
    expected = float(Decimal('72000') * 100 + Decimal('5000000') +
                     (Decimal('185.70') * 10 + Decimal('3500.5')) * Decimal('1380.5'))
    assert data.collection.nav_source_quality_reasons == {}
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] == expected, nav['reason_codes']
        assert nav['result_status'] == 'complete' and nav['reason_codes'] == []
