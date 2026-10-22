"""Fifth review: duplicate quote and raw inventory quality on the full NAV path."""
from copy import deepcopy

import pytest
from test_toss_nav import START, capture, selected
from test_toss_nav_review4 import primary_holding

from ledger_returns import scope_navs
from portfolio_engine import summarize_portfolio
from portfolio_store import snapshot_summary

EARLIER = {'symbol': 'SYN', 'currency': 'USD', 'lastPrice': '10', 'timestamp': START + 'T09:09:59Z'}
LATER = {'symbol': 'SYN', 'currency': 'USD', 'lastPrice': '12', 'timestamp': START + 'T09:10:00Z'}


def assert_legacy_and_other_account_unchanged(data):
    assert data.legacy == summarize_portfolio(data.collection.holdings, data.collection.cash, data.fx)
    summary = snapshot_summary(data.legacy, data.collection, data.fx)
    assert all(summary[key] == value for key, value in data.legacy.items())
    assert data.row['total_value_krw'] == data.legacy['total_assets_krw']
    assert data.row['nav_bridge'] == data.legacy['nav_bridge']
    assert scope_navs([data.row], 'equity')[0] == data.row
    secondary = scope_navs([data.row], 'toss_investmcp', account_references={'toss:secondary'})[0]
    assert secondary['total_value_krw'] == 3000 and secondary['reason_codes'] == []
    assert secondary['result_status'] == 'complete'


@pytest.mark.parametrize('quotes', [
    [EARLIER, LATER],
    [LATER, EARLIER],
    [{**EARLIER, 'lastPrice': '0'}, LATER],
    [LATER, {**EARLIER, 'lastPrice': '0'}],
    [{**EARLIER, 'timestamp': None}, LATER],
    [EARLIER, EARLIER],
    [EARLIER, LATER, EARLIER],
    [EARLIER, LATER, {**LATER, 'currency': 'KRW'}],
], ids=['earlier-later', 'later-earlier', 'valid-after-invalid-price', 'invalid-after-valid',
        'valid-after-null-time', 'identical', 'third-cannot-restore', 'other-currency-cannot-restore'])
def test_pass5_duplicate_quote_invalidates_symbol_and_native_nav(tmp_path, monkeypatch, quotes):
    data = capture(tmp_path, monkeypatch, primary_quotes=deepcopy(quotes))
    native = data.row['account_nav']['toss:primary']['nav_native']['USD']
    assert native['value'] is None
    assert 'DUPLICATE_QUOTE' in native['reason_codes']
    component = primary_holding(data)
    assert component['native_market_value']['value'] is None
    assert component['native_market_value']['status'] == 'unknown'
    assert 'DUPLICATE_QUOTE' in component['reason_codes']
    assert component['priced_at'] is None and component['quote_price_decimal'] is None
    assert component['legacy_price_decimal'] == '10'
    assert component['last_price'] == 10 and component['market_value'] == 20
    assert data.collection.nav_evidence['toss:primary']['quotes']['SYN'] == {'reason_codes': ['DUPLICATE_QUOTE']}
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] is None and nav['result_status'] == 'partial'
        assert 'DUPLICATE_QUOTE' in nav['reason_codes']
    assert_legacy_and_other_account_unchanged(data)


@pytest.mark.parametrize('quantity', [
    {}, {'quantity': None}, {'quantity': 'malformed'}, {'quantity': ''},
    {'quantity': {}}, {'quantity': []}, {'quantity': True}, {'quantity': False},
    {'quantity': -1}, {'quantity': '-0.01'}, {'quantity': '-1e-9999'},
], ids=['missing', 'null', 'malformed', 'empty', 'object', 'list', 'true', 'false',
        'negative-number', 'negative-string', 'negative-underflow'])
def test_pass5_invalid_raw_quantity_blocks_cash_only_native_nav(tmp_path, monkeypatch, quantity):
    raw = {'symbol': 'SYN', 'currency': 'USD', 'lastPrice': '10', 'marketValue': {'amount': 20}, **quantity}
    data = capture(tmp_path, monkeypatch, primary_holdings=[raw])
    account = data.row['account_nav']['toss:primary']
    assert account['source_status'] == 'partial'
    assert all(m['value'] is None and 'INVALID_HOLDING_QUANTITY' in m['reason_codes']
               for m in account['nav_native'].values())
    assert not any(h.account == 'toss:primary' for h in data.collection.holdings)
    assert data.collection.health['toss:primary']['status'] == 'ok'
    assert data.collection.failed_sources == [] and data.collection.truncated_sources == []
    assert set(data.collection.nav_source_quality_reasons) == {'toss:primary'}
    assert 'INVALID_HOLDING_QUANTITY' in data.collection.nav_source_quality_reasons['toss:primary']
    assert data.row['collection_source_quality']['reason_codes_by_source'] == data.collection.nav_source_quality_reasons
    details = [d for d in data.row['quality_reason_details'] if d['reason_code'] == 'INVALID_HOLDING_QUANTITY']
    assert details and all(d['account'] == 'toss:primary' and not d['resolved_for_native_nav'] for d in details)
    assert not any(call[0] == 'prices' for call in data.clients['primary'].calls)
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(data.row, scope)
        assert nav['total_value_krw'] is None and nav['result_status'] == 'partial'
        assert 'INVALID_HOLDING_QUANTITY' in nav['reason_codes']
    assert_legacy_and_other_account_unchanged(data)

    # Explicit empty inventory and legitimate numeric/string zeros remain valid
    # cash-only accounts. Pair controls with the regression so every new case
    # fails on 4c49e0a, where the invalid inventory also looked cash-only/complete.
    for rows in ([], *([{**raw, 'quantity': zero, 'marketValue': {'amount': '0'}}]
                       for zero in (0, 0.0, '0', '0.0000'))):
        good = capture(tmp_path, monkeypatch, primary_holdings=rows)
        assert selected(good.row)['total_value_krw'] == 1500
        assert selected(good.row)['reason_codes'] == []
        assert selected(good.row, 'toss')['total_value_krw'] == 4500
        assert good.row['account_nav']['toss:primary']['source_status'] == 'complete'
        assert good.collection.nav_source_quality_reasons == {}
        assert_legacy_and_other_account_unchanged(good)
