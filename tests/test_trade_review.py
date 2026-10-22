"""Synthetic review, never strategy attribution or live broker IO."""
import asyncio
from dataclasses import replace

import pytest

import portfolio_engine as pe
import server
from portfolio_model import FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection

START, END = '2026-02-01', '2026-02-28'
FX = FxRates({'USD': 1464.3, 'JPY': 9})


def trade(side, day, qty=10, price=100, **kwargs):
    return Transaction('toss', 'toss:masked', kwargs.pop('tx_id', day + side.value),
                       side, day, kwargs.pop('currency', 'USD'), symbol='AMZN',
                       quantity=qty, gross_amount=qty * price,
                       fee_status='verified_zero', tax_status='verified_zero', **kwargs)


def collection(rows=(), holdings=()):
    return Collection(transactions=list(rows), holdings=list(holdings),
                      health={'toss:masked': {'status': 'ok'}},
                      fetched_at=END + 'T12:00:00+00:00', fetch_complete=True,
                      requested_window={'start': '2000-01-01', 'end': END},
                      observed_window={'start': min((r.trade_date for r in rows), default=None),
                                       'end': max((r.trade_date for r in rows), default=None)})


def review(rows, holdings=(), detail='full', ledger=None, positions=None, fx=FX):
    return pe.compute_trade_review(ledger or collection(rows), positions or collection(holdings=holdings),
                                   fx, START, END, detail_level=detail)


def test_round_trips_partial_closes_and_open_loss():
    rows = [trade(TxType.BUY, '2026-02-02'), trade(TxType.SELL, '2026-02-03', 4, 110),
            trade(TxType.SELL, '2026-02-04', 6, 110), trade(TxType.BUY, '2026-02-05')]
    hold = Holding('toss', 'toss:masked', 'AMZN', 'USD', 10, unrealized_pnl=-200, cost_basis=1000)
    result = review(rows, [hold])
    assert result['totals']['order_count'] == 4
    assert result['totals']['round_trip_count'] == 1
    assert result['totals']['realized_win_rate'] == 1
    assert result['totals']['realized_win_rate_denominator'] == 1
    assert result['totals']['open_unrealized_pnl_by_currency'] == {'USD': -200}
    assert result['round_trips'][0]['holding_period_days'] == 2
    assert result['symbols'][0]['round_trip_count'] == 1


def test_cohorts_separate_pre_window_sales_from_new_result():
    rows = [trade(TxType.BUY, '2026-01-01'), trade(TxType.SELL, '2026-02-02', price=80),
            trade(TxType.BUY, '2026-02-03'), trade(TxType.SELL, '2026-02-04', price=110)]
    result = review(rows)
    cohorts = result['totals']['cohorts']
    assert cohorts['pre_window_liquidation']['realized_pnl_by_currency'] == {'USD': -200}
    assert cohorts['new_in_window']['realized_pnl_by_currency'] == {'USD': 100}
    assert result['totals']['round_trip_count'] == 1  # both legs in window
    assert result['sales'][0]['holding_period_days'] == 32


def test_mixed_origin_uses_proportional_quantities_without_new_cost_basis():
    rows = [trade(TxType.BUY, '2026-01-01'), trade(TxType.BUY, '2026-02-02', price=200),
            trade(TxType.SELL, '2026-02-03', price=180)]
    result = review(rows, [Holding('toss', 'toss:masked', 'AMZN', 'USD', 10, cost_basis=1500, unrealized_pnl=300)])
    assert result['totals']['realized']['realized_pnl_by_currency'] == {'USD': 300}
    for name in ('new_in_window', 'pre_window_liquidation'):
        assert result['totals']['cohorts'][name]['realized_pnl_by_currency'] == {'USD': 150}
        assert result['totals']['cohorts'][name]['quantity_sold'] == 5


def test_order_aggregate_is_not_fill_or_thesis_count():
    result = review([trade(TxType.BUY, '2026-02-02')])
    for item in (result['totals'], result['symbols'][0]):
        assert item['fill_count'] is None
        assert item['fill_count_reason_codes'] == ['ORDER_AGGREGATE_NOT_FILLS']
        assert item['thesis_count'] is None
        assert item['thesis_count_reason_codes'] == ['NO_INVESTMENT_JOURNAL']


def test_unknown_basis_nulls_all_krw_totals_and_holding_time():
    result = review([trade(TxType.SELL, '2026-02-02', fee=1, tax=2)])
    totals = result['totals']
    for key in ('realized_pnl_krw', 'fees_krw', 'taxes_krw'):
        assert totals[key] is None
        assert 'INCOMPLETE_COST_BASIS' in totals[key + '_reason_codes']
    assert totals['cohorts']['unknown_origin']['realized_pnl_by_currency'] is None
    assert totals['cohorts']['unknown_origin']['diagnostic_pnl_by_currency'] == {'USD': 997}
    assert result['sales'][0]['holding_period_days'] is None
    assert totals['realized_win_rate'] is None
    assert totals['round_trip_count'] == 0


def test_multi_currency_costs_and_missing_fx():
    rows = [trade(TxType.BUY, '2026-02-02', fee=2),
            trade(TxType.BUY, '2026-02-03', currency='JPY', fee=3, tax=1)]
    result = review(rows, fx=FxRates({'USD': 1464.3}))
    assert result['totals']['fees_by_currency'] == {'USD': 2, 'JPY': 3}
    assert result['totals']['taxes_by_currency'] == {'USD': 0, 'JPY': 1}
    assert result['totals']['fees_krw'] is None
    assert 'MISSING_FX_RATE' in result['reason_codes']


def test_reconciliation_break_propagates_to_metrics():
    result = review([trade(TxType.BUY, '2026-02-02')])
    assert result['reconciliation']['status'] == 'unreconciled'
    assert 'RECONCILIATION_BREAK' in result['reason_codes']
    assert result['result_status'] == 'partial'
    assert result['totals']['realized_pnl_krw'] is None


def test_summary_omits_details_preserves_metrics():
    rows = [trade(TxType.BUY, '2026-02-02'), trade(TxType.SELL, '2026-02-03')]
    full, summary = review(rows), review(rows, detail='summary')
    # Summary totals drop only cohorts; symbols are the ranked top rows (full has the table).
    assert summary['totals'] == {k: v for k, v in full['totals'].items() if k != 'cohorts'}
    assert [row['symbol'] for row in summary['symbols']] == [row['symbol'] for row in full['symbols']]
    assert summary['symbols'][0]['rank'] == 1 and summary['omitted_symbols'] == 0
    assert summary['symbols'][0]['win_rate_all'] == full['symbols'][0]['win_rate_all']
    assert 'cohorts' in full['totals'] and 'cohorts' in full['symbols'][0]
    assert 'sales' not in summary and 'round_trips' not in summary
    assert full['sales'] and full['round_trips']
    assert {x['metric'] for x in full['not_computed']} == {'alpha', 'benchmark', 'turnover_effect', 'intent', 'lesson'}


def test_account_books_do_not_pair_unrelated_legs():
    rows = [trade(TxType.BUY, '2026-02-02'),
            replace(trade(TxType.SELL, '2026-02-03'), account='toss:other')]
    result = review(rows)
    assert result['totals']['round_trip_count'] == 0
    assert result['totals']['realized_pnl_krw'] is None
    assert 'INCOMPLETE_COST_BASIS' in result['totals']['realized']['reason_codes']


def test_same_day_partial_sales_keep_individual_engine_basis_status():
    rows = [trade(TxType.BUY, '2026-02-02'),
            trade(TxType.SELL, '2026-02-03', 4, 110, tx_id='a'),
            trade(TxType.SELL, '2026-02-03', 6, 120, tx_id='b')]
    result = review(rows)
    assert [r['realized_pnl'] for r in result['sales']] == [40, 120]
    assert result['round_trips'][0]['realized_pnl'] == 160


def test_unknown_costs_exclude_round_trip_from_win_denominator():
    rows = [replace(trade(TxType.BUY, '2026-02-02'), fee_status='unknown'),
            trade(TxType.SELL, '2026-02-03', price=110)]
    result = review(rows)
    assert result['totals']['round_trip_count'] == 1
    assert result['totals']['realized_win_rate_denominator'] == 0
    assert result['totals']['realized_win_rate'] is None
    assert result['totals']['realized_pnl_krw'] is None


@pytest.mark.parametrize('failed,expected', [(False, 'empty'), (True, 'unavailable')])
def test_failed_sources_distinct_from_successful_empty(failed, expected):
    ledger = collection()
    if failed:
        ledger.health = {'toss:masked': {'status': 'error'}}
    result = review([], ledger=ledger)
    assert result['result_status'] == expected
    if failed:
        assert result['totals']['realized_pnl_krw'] is None
        assert result['totals']['order_count'] is None
        assert result['sources_failed'] == ['toss:masked']


def test_tool_signature_help_annotations_and_end_only_date(monkeypatch):
    rows = [trade(TxType.BUY, '2026-02-02'), trade(TxType.SELL, '2026-02-03')]
    calls = []

    class Collector:
        def collect_transactions(self, start, end, accounts, symbol):
            calls.append((start, end, accounts, symbol))
            return collection(rows)

        def collect_positions(self, accounts):
            return collection()

        def build_fx(self, currencies):
            return FX

    monkeypatch.setattr(server, '_get_collector', lambda: Collector())
    response = server.get_trade_review(period='MTD', end_date=END, account='toss:masked', symbol='AMZN')
    assert response['ok'] is True
    assert response['data']['period'] == {'start': START, 'end': END}
    assert calls == [('2000-01-01', END, ['toss:masked'], 'AMZN')]
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools['get_trade_review']
    assert tool.annotations.readOnlyHint is True
    assert tool.inputSchema['properties']['period']['enum'] == list(pe.PERIODS)
    assert 'get_trade_review' in server.get_invest_mcp_help('portfolio')['data']['markdown']


def test_additive_sales_hook_preserves_original_engine_result():
    rows = [trade(TxType.BUY, '2026-01-01'),
            trade(TxType.SELL, '2026-02-03', 4, 110, tx_id='a'),
            trade(TxType.SELL, '2026-02-03', 8, 120, tx_id='b'),
            trade(TxType.BUY, '2026-02-04', 5),
            trade(TxType.SELL, '2026-02-05', 5, 130, tx_id='c')]
    original = pe.compute_realized_pnl(rows, FX, START, END, _include_account_books=False)
    hooked = pe.compute_realized_pnl_with_sales(rows, FX, START, END)
    sales = hooked.pop('sales')
    assert hooked == original
    # B's oversell proves 2 shares held before the ledger, so a's average omitted them.
    assert [s['cost_basis_complete'] for s in sales] == [False, False, True]


def test_execution_count_requires_explicit_fill_records_and_parent_orders():
    class Fill(Transaction):
        def to_dict(self):
            return {**super().to_dict(), 'record_kind': 'execution'}

    rows = [Fill(**{field: getattr(trade(TxType.BUY, '2026-02-02', qty=5), field)
                    for field in Transaction.__dataclass_fields__}),
            Fill(**{field: getattr(trade(TxType.BUY, '2026-02-03', qty=5), field)
                    for field in Transaction.__dataclass_fields__})]
    rows = [replace(r, execution_id=str(i), identity_quality='native_execution', parent_order_id='same-order')
            for i, r in enumerate(rows)]
    totals = review(rows)['totals']
    assert totals['fill_count'] == 2
    assert totals['order_count'] == 1
    aggregate = replace(trade(TxType.BUY, '2026-02-02'), identity_quality='native_execution', execution_id='x')
    assert review([aggregate])['totals']['fill_count'] is None


def test_unknown_row_cost_currency_is_null_with_reported_subset():
    rows = [replace(trade(TxType.BUY, '2026-02-02'), fee_status='unknown')]
    totals = review(rows)['totals']
    assert totals['fees_by_currency'] == {'USD': None}
    assert totals['fees_reported_by_currency'] == {'USD': 0}


def test_missing_fx_retains_known_native_pnl():
    rows = [trade(TxType.BUY, '2026-02-02'), trade(TxType.SELL, '2026-02-03', price=110)]
    result = review(rows, fx=FxRates())
    assert result['totals']['realized']['realized_pnl_by_currency'] == {'USD': 100}
    assert result['totals']['realized_pnl_krw'] is None
    assert result['totals']['cohorts']['new_in_window']['acquired_quantity'] == 10


def test_historical_end_current_positions_mismatch_is_visible():
    positions = collection()
    positions.fetched_at = '2026-03-01T12:00:00+00:00'
    result = review([trade(TxType.BUY, '2026-02-02')], positions=positions)
    assert 'HOLDINGS_AS_OF_MISMATCH' in result['reason_codes']
    assert result['coverage']['positions_as_of'] == positions.fetched_at
