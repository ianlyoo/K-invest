"""Toss NAV acceptance: synthetic clients and local disposable SQLite only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import MethodType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ledger_returns import read_flows, scope_navs
from portfolio_engine import compute_performance, summarize_portfolio
from portfolio_ledger import LedgerStore
from portfolio_model import FxRates
from portfolio_sources import PortfolioCollector, Providers
from portfolio_store import NavStore, snapshot_summary

START, END = '2026-10-10', '2026-10-11'
DEFAULT_CALENDAR = object()


def documented_holding(item):
    """Complete public-schema row, preserving explicitly missing NAV inputs."""
    row = deepcopy(item)
    row.update({key: row.get(key, value) for key, value in {
        'name': 'Synthetic', 'marketCountry': 'US' if row.get('currency') == 'USD' else 'KR',
        'averagePurchasePrice': '0',
        'profitLoss': dict.fromkeys(('amount', 'amountAfterCost', 'rate', 'rateAfterCost'), '0'),
        'dailyProfitLoss': {'amount': '0', 'rate': '0'}, 'cost': {'commission': '0', 'tax': None}}.items()})
    if type(row.get('quantity')) in (int, float) and row['quantity'] != 0:
        row['quantity'] = str(row['quantity'])
    if 'marketValue' in row:
        row['marketValue'] = {**{'purchaseAmount': '0', 'amountAfterCost': '0'}, **row['marketValue']}
        if type(row['marketValue'].get('amount')) in (int, float):
            row['marketValue']['amount'] = str(row['marketValue']['amount'])
    return row


def documented_inventory(items):
    zero = {'krw': '0', 'usd': '0'}
    rows = [documented_holding(row) for row in items]
    def totals(field):
        return {key: str(sum((Decimal(row.get('marketValue', {}).get(field, '0')) for row in rows
                             if row.get('currency') == currency), Decimal(0)))
                for currency, key in (('KRW', 'krw'), ('USD', 'usd'))}
    return {'totalPurchaseAmount': totals('purchaseAmount'),
        'marketValue': {'amount': totals('amount'), 'amountAfterCost': zero},
        'profitLoss': {'amount': zero, 'amountAfterCost': zero, 'rate': '0', 'rateAfterCost': '0'},
        'dailyProfitLoss': {'amount': zero, 'rate': '0'}, 'items': rows}


def documented_order():
    return {'orderId': 'synthetic-open', 'symbol': 'SYN', 'currency': 'USD', 'side': 'BUY',
        'orderType': 'LIMIT', 'timeInForce': 'DAY', 'status': 'PENDING', 'quantity': '1',
        'orderedAt': START + 'T09:10:00Z', 'execution': {'filledQuantity': '0',
        'averageFilledPrice': None, 'filledAmount': None, 'commission': None,
        'tax': None, 'filledAt': None, 'settlementDate': None}}


def synthetic_calendar(day, country='US'):
    """Synthetic regular-session fixtures, including explicit weekend closure."""
    today = datetime.fromisoformat(day).date()
    previous, following = today - timedelta(days=1), today + timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    def session(date):
        zone = ZoneInfo('Asia/Seoul' if country == 'KR' else 'America/New_York')
        regular = None if date.weekday() >= 5 else {
            'startTime': datetime(date.year, date.month, date.day, 9, 0 if country == 'KR' else 30, tzinfo=zone).isoformat(),
            'endTime': datetime(date.year, date.month, date.day, 15 if country == 'KR' else 16,
                                30 if country == 'KR' else 0, tzinfo=zone).isoformat()}
        return {'date': date.isoformat(), **({'integrated': {'regularMarket': regular} if regular else None}
            if country == 'KR' else {'regularMarket': regular})}
    return {'previousBusinessDay': session(previous), 'today': session(today), 'nextBusinessDay': session(following)}


def capture(tmp_path, monkeypatch, *, day=START, orders='none', timestamp=True, fx_time=True,
            fail_kis=False, fail_prices=False, cash_missing=False, order_payload=None,
            holdings_truncated=False, quote_price=None, quote_currency='USD', quote_symbol='SYN',
            quote_stamp=None, calendar=DEFAULT_CALENDAR, fx_stamp=None, fx_until=None,
            holding_price=None, fail_calendar=False, fx_read_at=None, calendar_envelope=False,
            holding_currency='USD', extra_holdings=None, primary_quotes=None, quote_read_at=None,
            primary_holdings=None, nav_mutation=None, real_response_views=False, instrument_payload=None,
            fx_source='toss'):
    stamp = day + 'T09:10:00Z'
    clock = [stamp]
    monkeypatch.setattr('portfolio_sources._now', lambda: clock[0])
    monkeypatch.setattr('portfolio_sources._fx_read_time', lambda: clock[0], raising=False)
    clients = {}

    def mutate(kind, payload, primary=True):
        return nav_mutation(kind, deepcopy(payload)) if nav_mutation is not None and primary else payload

    class Toss:
        def __init__(self, label):
            self.label, self.calls = label, []

        def get_holdings(self):
            primary = self.label == 'primary'
            price = 10 if day == START else 11
            items = [{'symbol': 'SYN',
                'currency': holding_currency if primary else 'USD',
                'quantity': 2 if primary else 1,
                'lastPrice': holding_price if primary and holding_price is not None else str(price if primary else 20),
                'marketValue': {'amount': 2 * price if primary else 20}},
                *(deepcopy(extra_holdings or []) if primary else [])]
            if primary and primary_holdings is not None:
                items = deepcopy(primary_holdings)
            return mutate('holdings', {'truncated': holdings_truncated and primary,
                                      'result': documented_inventory(items)}, primary)

        def get_buying_power(self, currency):
            return self.get_buying_power_evidence(currency).get('result', {})

        def get_buying_power_evidence(self, currency):
            value = {'KRW': 1000 if day == START else 1350, 'USD': 5} if self.label == 'primary' else {'KRW': 700, 'USD': 3}
            return mutate('buying_power:' + currency, {'result': {'currency': currency,
                'cashBuyingPower': None if cash_missing and currency == 'USD' else str(value[currency])}},
                self.label == 'primary')

        def get_orders(self, **kwargs):
            self.calls.append(('orders', kwargs))
            assert kwargs == {'status': 'OPEN'}
            if self.label == 'primary':
                if order_payload is not None:
                    return order_payload
                if orders == 'failure':
                    raise RuntimeError('synthetic read failed')
                if orders == 'malformed':
                    return {'result': {'hasNext': False}}
                if orders == 'ambiguous':
                    return {'result': {'orders': []}}
                if orders == 'paged':
                    return {'result': {'orders': [], 'hasNext': True, 'nextCursor': 'synthetic-next'}}
                if orders == 'present':
                    return {'result': {'orders': [documented_order()], 'hasNext': False, 'nextCursor': None}}
            return mutate('orders', {'result': {'orders': [], 'hasNext': False, 'nextCursor': None}},
                          self.label == 'primary')

        def get_prices(self, symbols):
            return self.get_prices_evidence(symbols).get('result', [])

        def get_stock_info_evidence(self, symbols):
            self.calls.append(('stock_info', symbols))
            if instrument_payload is None:
                raise RuntimeError('synthetic instrument unavailable')
            return deepcopy(instrument_payload)

        def get_prices_evidence(self, symbols):
            self.calls.append(('prices', symbols))
            expected = (sorted(item['symbol'] for item in primary_holdings) if self.label == 'primary' and primary_holdings is not None
                        else sorted({'SYN', *(item['symbol'] for item in extra_holdings or [])}) if self.label == 'primary' else ['SYN'])
            assert symbols == expected
            if fail_prices and self.label == 'primary':
                raise RuntimeError('synthetic quote failed')
            if quote_read_at is not None:
                clock[0] = quote_read_at
            if primary_quotes is not None and self.label == 'primary':
                return mutate('prices', {'result': deepcopy(primary_quotes)})
            price = (10 if day == START else 11) if self.label == 'primary' else 20
            return mutate('prices', {'result': [{'symbol': quote_symbol if self.label == 'primary' else 'SYN',
                     'currency': quote_currency if self.label == 'primary' else 'USD',
                     'lastPrice': quote_price if self.label == 'primary' and quote_price is not None else str(price),
                     'timestamp': quote_stamp or (stamp if timestamp or self.label == 'secondary' else None)}]},
                          self.label == 'primary')

        def get_market_calendar(self, country):
            payload = self.get_market_calendar_evidence(country)
            return payload.get('result') if isinstance(payload, dict) else payload

        def get_market_calendar_evidence(self, country):
            assert country in ('KR', 'US')
            if fail_calendar:
                raise RuntimeError('synthetic calendar failure')
            value = synthetic_calendar(day, country) if calendar is DEFAULT_CALENDAR else calendar
            return mutate('calendar', value if isinstance(value, dict) and 'result' in value else {'result': value})

    class KIS:
        def get_domestic_balance(self):
            if fail_kis:
                raise RuntimeError('synthetic KIS failure')
            return {'stocks': [{'pdno': '000001', 'hldg_qty': '1', 'evlu_amt': '100'}],
                    'summary': [{'dnca_tot_amt': '50'}]}

        def get_overseas_balance(self):
            return {'stocks': [{'ovrs_pdno': 'KSYN', 'ovrs_cblc_qty': '1',
                'ovrs_stck_evlu_amt': '15', 'currency': 'USD'},
                {'ovrs_pdno': 'JSYN', 'ovrs_cblc_qty': '1', 'ovrs_stck_evlu_amt': '7', 'currency': 'JPY'}],
                'cash': [{'crcy_cd': 'JPY', 'frcr_dncl_amt_2': '0',
                          'frcr_sll_amt_smtl': '0', 'frcr_buy_amt_smtl': '0'}], 'errors': {}}

    clients.update({label: Toss(label) for label in ('primary', 'secondary')})
    if calendar_envelope:
        # Exercise the real client's result-only legacy accessor and full-envelope
        # NAV accessor without constructing a client or reading credentials.
        from toss_api import TossClient
        def calendar_get(client, path):
            assert path == '/api/v1/market-calendar/US'
            client.calendar_calls.append(path)
            return calendar
        for client in clients.values():
            client.calendar_calls = []
            client._get = MethodType(calendar_get, client)
            client.get_market_calendar = MethodType(TossClient.get_market_calendar, client)
            if hasattr(TossClient, 'get_market_calendar_evidence'):
                client.get_market_calendar_evidence = MethodType(TossClient.get_market_calendar_evidence, client)

    def exchange_response(currency):
        rate = 100 if currency == 'USD' else 1
        priced = (fx_stamp or stamp) if fx_time else None
        expiry = fx_until or ((datetime.fromisoformat(priced.replace('Z', '+00:00')) + timedelta(hours=1))
                              .isoformat() if priced else stamp)
        return mutate('exchange_rate', {'result': {'rate': str(rate), 'midRate': str(rate), 'basisPoint': '0',
            'rateChangeType': 'EQUAL', 'baseCurrency': currency, 'quoteCurrency': 'KRW',
            'validFrom': priced, 'validUntil': expiry}})

    if real_response_views:
        from toss_api import TossClient
        def read_response(client, path, params=None, account=False):
            if path == '/api/v1/prices':
                assert not account
                return Toss.get_prices_evidence(client, params['symbols'].split(','))
            if path == '/api/v1/buying-power':
                assert account
                return Toss.get_buying_power_evidence(client, params['currency'])
            if path == '/api/v1/exchange-rate':
                assert params == {'baseCurrency': 'USD', 'quoteCurrency': 'KRW'} and not account
                return exchange_response('USD')
            if path.startswith('/api/v1/market-calendar/'):
                assert not account
                return Toss.get_market_calendar_evidence(client, path.rsplit('/', 1)[1])
            raise AssertionError('Unexpected synthetic endpoint')
        for client in clients.values():
            client._get = MethodType(read_response, client)
            for name in ('get_prices', 'get_prices_evidence', 'get_buying_power', 'get_buying_power_evidence',
                         'get_market_calendar', 'get_market_calendar_evidence', 'get_exchange_rate'):
                if hasattr(TossClient, name):
                    setattr(client, name, MethodType(getattr(TossClient, name), client))
                elif name.endswith('_evidence'):
                    # Hide the fake's new accessor on the older source revision.
                    setattr(client, name, None)

    def fx_quote(currency):
        if fx_read_at is not None:
            clock[0] = fx_read_at
        rate = 100 if currency == 'USD' else 1
        if fx_source == 'yfinance' and currency == 'USD':
            # Live shape (portfolio_sources.yfinance_fx_quote): a float close and its
            # daily bar start with the source offset; no validFrom/validUntil.
            return {'rate': float(rate), 'priced_at': day + 'T00:00:00+01:00'}
        if hasattr(FxRates(), 'priced_at_by_currency'):
            if currency != 'USD':
                return {'rate': rate, 'priced_at': None}  # Alternate FX source for KIS only.
            if real_response_views:
                return clients['primary'].get_exchange_rate(base=currency, quote='KRW')
            class Mark(dict):
                pass
            payload = exchange_response(currency)
            result = payload.get('result') if isinstance(payload, dict) else None
            value = Mark(result if isinstance(result, dict) else {})
            value.nav_envelope = payload
            # Keep the same legacy result values on either revision; only the
            # new validator inspects envelope evidence before trusting them.
            return value
        return rate  # Identical scalar rates exercise the baseline's NAV defect.

    collector = PortfolioCollector(Providers(
        toss_labels=lambda: list(clients), toss_client=clients.__getitem__,
        kis_accounts=lambda: [SimpleNamespace(label='primary', supports_overseas=True)],
        kis_client=lambda label: KIS(), error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC_READ_FAILED'}},
        fx_quote=fx_quote))
    collection = collector.collect_positions()
    fx = collector.build_fx({r.currency for r in [*collection.holdings, *collection.cash]})
    legacy = summarize_portfolio(collection.holdings, collection.cash, fx)
    # Running this fixture on the baseline exercises the old output, so every
    # acceptance test fails on its behavioral assertion rather than import.
    if hasattr(collector, 'collect_nav_evidence'):
        collector.collect_nav_evidence(collection)
    assert summarize_portfolio(collection.holdings, collection.cash, fx) == legacy
    summary = snapshot_summary(legacy, collection, fx)
    assert all(summary[k] == value for k, value in legacy.items()
               if k != 'nav_bridge' or not (collection.failed_sources or collection.truncated_sources))
    row = NavStore(tmp_path / 'synthetic-nav.db').upsert_snapshot(summary, day)
    return SimpleNamespace(row=row, clients=clients, collection=collection, fx=fx, legacy=legacy)


def selected(row, scope='toss_investmcp'):
    return scope_navs([row], scope, account_references={'toss:primary'})[0]


def assert_valid(row):
    value = selected(row)
    assert value['total_value_krw'] == (3500 if row['snapshot_date'] == START else 4050), value['reason_codes']
    assert value['reason_codes'] == []
    assert value['result_status'] == 'complete'


def test_toss_nav_acceptance_preserves_legacy_and_other_scopes(tmp_path, monkeypatch):
    data = capture(tmp_path, monkeypatch)
    assert_valid(data.row)
    toss = selected(data.row, 'toss')
    assert toss['total_value_krw'] == 6500 and toss['reason_codes'] == []
    for account in ('toss:primary', 'toss:secondary'):
        assert data.row['account_nav'][account]['cash_basis_evidence'] == {
            'open_orders': 'none', 'open_order_count': 0,
            'observed_at': START + 'T09:10:00Z', 'source': 'toss_orders.status=OPEN'}
        for currency in ('KRW', 'USD'):
            cash = data.row['account_nav'][account]['cash'][currency]
            assert cash['basis'] == 'derived'
            assert cash['basis_reason'] == 'TOSS_BUYING_POWER_NO_OPEN_ORDERS'
            assert cash['reason_codes'] == []
        assert data.clients[account.split(':')[1]].calls == [('orders', {'status': 'OPEN'}), ('prices', ['SYN'])]
    assert all(c['priced_at'] is None for c in data.row['components'] if c['provider'] == 'kis')
    assert data.row['fx_times']['USD']['priced_at'] == START + 'T09:10:00Z'
    assert {'UNKNOWN_COMPONENT_PRICE_TIME', 'UNKNOWN_FX_PRICE_TIME', 'UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS',
            'SETTLEMENT_BASIS_REFERENCE_ASSUMPTION'} <= set(data.row['reason_codes'])
    assert scope_navs([data.row], 'equity')[0] == data.row
    result = compute_performance([], [data.row], None, FxRates(), START, START,
                                 external_cashflows_coverage={'status': 'verified_zero'})
    assert result['returns']['twr_pct'] is None
    assert 'UNKNOWN_COMPONENT_PRICE_TIME' in result['metrics']['twr_pct']['reason_codes']
    consolidated = scope_navs([data.row], 'consolidated')[0]
    assert consolidated['total_value_krw'] is None and consolidated['reason_codes']


@pytest.mark.parametrize('orders,reason', [('present', 'TOSS_OPEN_ORDERS_PRESENT'),
    ('failure', 'TOSS_OPEN_ORDERS_UNVERIFIED'), ('malformed', 'TOSS_OPEN_ORDERS_UNVERIFIED'),
    ('ambiguous', 'TOSS_OPEN_ORDERS_UNVERIFIED'), ('paged', 'TOSS_OPEN_ORDERS_UNVERIFIED')])
def test_toss_cash_requires_complete_empty_order_page(tmp_path, monkeypatch, orders, reason):
    good = capture(tmp_path, monkeypatch)
    assert_valid(good.row)
    data = capture(tmp_path, monkeypatch, orders=orders)
    value = selected(data.row)
    assert value['total_value_krw'] is None and reason in value['reason_codes']
    assert data.row['account_nav']['toss:primary']['cash']['USD']['value'] is None
    assert len([c for c in data.clients['primary'].calls if c[0] == 'orders']) == 1
    # Another account's open order cannot block an explicitly selected account.
    secondary = scope_navs([data.row], 'toss_investmcp', account_references={'toss:secondary'})[0]
    assert secondary['total_value_krw'] == 3000 and secondary['reason_codes'] == []
    assert selected(data.row, 'toss')['total_value_krw'] is None


@pytest.mark.parametrize('change,reason', [({'timestamp': False}, 'UNKNOWN_COMPONENT_PRICE_TIME'),
    ({'fail_prices': True}, 'UNKNOWN_COMPONENT_PRICE_TIME'), ({'fx_time': False}, 'UNKNOWN_FX_PRICE_TIME'),
    ({'cash_missing': True}, 'MISSING_CASH_BALANCE')])
def test_toss_missing_nav_inputs_stay_unknown(tmp_path, monkeypatch, change, reason):
    assert_valid(capture(tmp_path, monkeypatch).row)
    row = capture(tmp_path, monkeypatch, **change).row
    value = selected(row)
    assert value['total_value_krw'] is None and reason in value['reason_codes']


@pytest.mark.parametrize('reason', ['SNAPSHOT_DATE_MISMATCH', 'UNKNOWN_VALUATION_TIME',
                                   'INVALID_VALUATION_TIME', 'UNATTRIBUTED_FAILURE'])
def test_toss_keeps_applicable_and_unattributed_snapshot_failures(tmp_path, monkeypatch, reason):
    row = capture(tmp_path, monkeypatch).row
    assert_valid(row)
    row['reason_codes'].append(reason)
    value = selected(row)
    assert value['total_value_krw'] is None and reason in value['reason_codes']


def test_toss_excludes_only_attributed_collection_failures(tmp_path, monkeypatch):
    row = capture(tmp_path, monkeypatch, fail_kis=True).row
    assert 'FAILED_SOURCES' in row['reason_codes']
    assert_valid(row)
    row = deepcopy(row)
    row['quality_reason_details'].append({'reason_code': 'FAILED_SOURCES', 'account': 'toss', 'currency': None})
    assert 'FAILED_SOURCES' in selected(row)['reason_codes']
    row['quality_reason_details'].append({'reason_code': 'FAILED_SOURCES', 'account': None, 'currency': None})
    assert selected(row)['total_value_krw'] is None


def test_toss_settlement_warning_is_resolved_only_by_documented_cash_basis(tmp_path, monkeypatch):
    row = capture(tmp_path, monkeypatch).row
    assert_valid(row)
    details = row['quality_reason_details']
    toss = [d for d in details if d['reason_code'] == 'UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS' and d['account'] == 'toss:primary']
    assert toss and all(d['resolved_for_native_nav'] for d in toss)
    assert 'UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS' in row['nav_bridge']['reconciliation']['difference_reasons']
    assert row['account_nav']['toss:primary']['unsettled']['USD']['receivables']['value'] is None
    # Older snapshots have no zero-open-order proof: no retrospective upgrade.
    legacy = deepcopy(row)
    legacy.pop('quality_reason_details')
    legacy['account_nav']['toss:primary']['cash']['USD']['basis_reason'] = 'BUYING_POWER_IS_NOT_CASH'
    assert 'UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS' in selected(legacy)['reason_codes']


def test_toss_reference_settlement_assumption_is_scoped_to_its_cash_basis(tmp_path, monkeypatch):
    data = capture(tmp_path, monkeypatch)
    assert_valid(data.row)
    data.collection.cash = [replace(c, native_cash_includes_unsettled=True,
        positions_basis='trade_date_reference_assumption', unsettled_receivables=0, unsettled_payables=0)
        if c.account == 'toss:primary' else c for c in data.collection.cash]
    summary = snapshot_summary(summarize_portfolio(data.collection.holdings, data.collection.cash, data.fx),
                               data.collection, data.fx)
    row = NavStore(tmp_path / 'synthetic-reference.db').upsert_snapshot(summary, START)
    assert_valid(row)
    assert 'SETTLEMENT_BASIS_REFERENCE_ASSUMPTION' in row['nav_bridge']['reconciliation']['difference_reasons']
    details = [d for d in row['quality_reason_details'] if d['reason_code'] == 'SETTLEMENT_BASIS_REFERENCE_ASSUMPTION']
    assert any(d['account'] == 'toss:primary' and d['resolved_for_native_nav'] for d in details)
    assert any(d['account'] == 'kis:overseas' and not d['resolved_for_native_nav'] for d in details)


def test_toss_two_snapshot_covered_cashflows_twr_is_ten_percent(tmp_path, monkeypatch):
    navs = [capture(tmp_path, monkeypatch, day=day).row for day in (START, END)]
    for nav in navs:
        assert_valid(nav)
    account = 'acct_' + '1' * 32
    ledger = LedgerStore(tmp_path / 'synthetic-ledger.db')
    with ledger.transaction() as conn:
        conn.execute('INSERT INTO ledger_account_aliases VALUES (?,?,?)',
                     (ledger._privacy.opaque('toss:primary'), account, END + 'T09:10:00Z'))
        event = dict(source='toss_cashflows', account_identity=account, source_record_id='synthetic-deposit',
            native_record_id='synthetic-deposit', kind='deposit', amount='200', currency='KRW',
            execution_time_utc=END + 'T08:00:00Z', source_timezone='UTC', timestamp_precision='second',
            activity_date=END, direction='in', source_status='completed', identity_quality='native_record')
        assert ledger.append(conn, event) == 'new'
        ledger.classify_transfer(conn, ledger.event_id(event), 'external_in', 'SYNTHETIC_CLASSIFICATION')
    start_ms = int(datetime.fromisoformat(START + 'T00:00:00+09:00').timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(END + 'T23:59:59+09:00').timestamp() * 1000) + 999
    ledger.record_coverage('toss', account, 'toss/cashflows', start_ms, end_ms, 'complete', [], 1, 'synthetic')
    scoped = scope_navs(navs, 'toss_investmcp', account_references={'toss:primary'})
    flow_navs = [{**nav, 'account_nav': {'toss:primary': nav['account_nav']['toss:primary']}} for nav in scoped]
    evidence = read_flows(ledger.db_path, START, END, 'toss', flow_navs)
    assert evidence['coverage']['status'] == 'complete', evidence['coverage']['reason_codes']
    assert evidence['flows_by_date'] == {END: 200}
    result = compute_performance([], scoped, None, FxRates(), START, END,
        external_cashflows_coverage=evidence['coverage'], ledger_flow_input=evidence)
    # (4050 - 200) / 3500 - 1 = 10%; spot FX and KIS defects do not interfere.
    assert result['returns']['twr_pct'] == 10
    assert result['metrics']['twr_pct']['reason_codes'] == []


def test_fx_preserves_provider_quote_time_without_inventing_one(tmp_path, monkeypatch):
    data = capture(tmp_path, monkeypatch)
    assert_valid(data.row)
    collector = PortfolioCollector(Providers(lambda: [], lambda label: None, lambda: [], lambda label: None,
        lambda exc: {}, fx_quote=lambda currency: {'rate': 123, 'priced_at': START + 'T07:00:00Z'}))
    fx = collector.build_fx({'USD'})
    assert fx.rates == {'USD': 123}
    assert fx.priced_at_by_currency == {'USD': START + 'T07:00:00Z'}
    collector._providers.fx_quote = lambda currency: 123
    assert collector.build_fx({'USD'}).priced_at_by_currency == {'USD': None}


@pytest.mark.parametrize('zoned', [True, False, None])
def test_default_fx_keeps_only_the_sources_zoned_mark_time(tmp_path, monkeypatch, zoned):
    assert_valid(capture(tmp_path, monkeypatch).row)
    import yfinance

    stamp = datetime.fromisoformat(START + 'T07:00:00' + ('+00:00' if zoned else '')) if zoned is not None else 'unknown'

    class History:
        empty = False
        index = [stamp]
        def __getitem__(self, key):
            assert key == 'Close'
            return SimpleNamespace(iloc=[321])

    monkeypatch.setattr(yfinance, 'Ticker', lambda pair: SimpleNamespace(history=lambda **kwargs: History()))
    collector = PortfolioCollector(Providers(lambda: [], lambda label: None, lambda: [], lambda label: None, lambda exc: {}))
    fx = collector.build_fx({'USD'})
    assert fx.rates == {'USD': 321}
    assert fx.priced_at_by_currency == {'USD': stamp.isoformat() if zoned else None}
