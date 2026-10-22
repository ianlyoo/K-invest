"""API trade dates and occurrences survive repeated merges."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import portfolio_engine as engine
import server
import statement_import as m
from portfolio_export import collect_dataset
from portfolio_model import FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection

START, END = '2026-01-01', '2026-06-30'


def trade(side, day, quantity, price, tx_id, settle=None):
    return Transaction('toss', 'toss:syn', tx_id, side, day, 'KRW', symbol='SYN',
        quantity=quantity, price=price, gross_amount=quantity * price,
        fee_status='verified_zero', tax_status='verified_zero', settle_date=settle,
        net_amount=(-1 if side is TxType.BUY else 1) * quantity * price)


def fixture(tmp_path, variant):
    old = trade(TxType.BUY, '2025-06-02', 10, 100, 'api-old')
    statement = [replace(old, tx_id='stmt-old')]
    if variant == 'edge_cancel':
        current = [trade(TxType.BUY, '2026-03-02', 5, 200, 'api-buy', '2026-03-04'),
            trade(TxType.SELL, '2026-03-03', 5, 300, 'api-sell', '2026-03-05')]
        expected, ending = 833.33, 10
    else:
        current = [trade(TxType.SELL, '2026-03-02', 5, 150, 'api-sell', '2026-03-04')]
        expected, ending = 250, 5
    statement.extend(replace(t, tx_id='stmt-' + t.tx_id, trade_date=t.settle_date, settle_date=None)
        for t in current)
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply(variant, 'toss:syn', statement, {'covered_period': {
        'start': '2025-06-01', 'end': '2026-03-31'}}, api_rows=current)
    return store, [old, *current], expected, ending


def ledger(api, start=START):
    return Collection(transactions=list(api), health={'toss:syn:orders': {'status': 'ok'}},
        requested_window={'start': start, 'end': END}, fetch_complete=True,
        fetched_at=END + 'T03:00:00Z')


def signature(rows):
    return sorted((t.trade_date, t.tx_type.value, t.quantity, t.gross_amount) for t in rows)


@pytest.mark.parametrize('variant', ['edge_cancel', 'edge_resurrect'])
def test_edge_cancel_and_resurrect_merge_once_and_twice_identically(tmp_path, variant):
    store, api, expected, _ = fixture(tmp_path, variant)
    one = m.merge_statements(ledger(api, '2000-01-01'), store, '2000-01-01', END, None, None)
    two = m.merge_statements(ledger(api[1:]), store, START, END, None, None)
    assert two.transactions == api[1:]  # Present API rows keep execution dates, including edge settlements.
    m.merge_statements(two, store, '2000-01-01', '2025-12-31', None, None)
    for _ in range(2):
        m.merge_statements(two, store, START, END, None, None)
        m.merge_statements(two, store, '2000-01-01', '2025-12-31', None, None)
        assert signature(two.transactions) == signature(one.transactions) == signature(api)
        assert len(two.transactions) == len(api)
        result = engine.compute_realized_pnl(two.transactions, FxRates(), START, END)
        assert result['totals']['realized_pnl_krw'] == expected
        assert result['totals']['status'] == 'complete'


@pytest.mark.parametrize('variant', ['edge_cancel', 'edge_resurrect'])
def test_edge_fixture_summary_realized_transactions_and_export_agree(tmp_path, monkeypatch, variant):
    store, api, expected, ending = fixture(tmp_path, variant)
    positions = Collection(holdings=[Holding('toss', 'toss:syn', 'SYN', 'KRW', ending,
        last_price=150, market_value=ending * 150, cost_basis=ending * 100, unrealized_pnl=ending * 50)],
        health={'toss:syn': {'status': 'ok'}}, fetched_at=END + 'T03:00:00Z')
    collected = []
    requests = []
    def transactions(start, end, accounts=None, symbol=None, **kwargs):
        requests.append((start, end))
        result = ledger([t for t in api if start <= t.trade_date <= end], start)
        collected.append(m.merge_statements(result, store, start, end, accounts, symbol))
        return result
    collector = SimpleNamespace(collect_transactions=transactions, collect_positions=lambda *a: positions,
        build_fx=lambda *a: FxRates(), _providers=SimpleNamespace(statement_store=lambda: store))
    monkeypatch.setattr(server, '_get_collector', lambda: collector)
    monkeypatch.setattr(engine, 'resolve_period', lambda period, **kwargs:
        ('2000-01-01' if period == 'ALL' else START, END))
    summary = server.get_portfolio_summary(snapshot=False)['data']
    assert summary['realized_pnl_krw'] == expected
    assert summary['realized_pnl_status'] == 'complete'
    assert requests == [(START, END)]  # Summary's pre-year evidence requires no second broker read.
    realized = server.get_realized_pnl(account='toss:syn')['data']
    assert realized['totals']['realized_pnl_krw'] == expected
    assert realized['totals']['status'] == 'complete'
    txs = server.get_all_transactions('2000-01-01', END, 'toss:syn')['data']['transactions']
    assert [(t['trade_date'], t['tx_id']) for t in txs] == [(t.trade_date, t.tx_id) for t in api]
    rows, manifest = collect_dataset('realized_pnl', START, END, collector=collector, accounts=['toss:syn'])
    assert manifest['summary']['totals']['realized_pnl_krw'] == expected
    exported = [r['data'] for r in rows if r['record_type'] == 'transaction']
    assert [(t['trade_date'], t['tx_id']) for t in exported] == [(t.trade_date, t.tx_id) for t in api]
    assert all(signature(c.transactions) == signature(api) for c in collected)


def test_failed_chunk_keeps_present_api_trade_dates_and_fills_only_missing_trade(tmp_path):
    api = [trade(TxType.BUY, '2026-01-30', 3, 100, 'api-jan', '2026-02-03'),
        trade(TxType.BUY, '2026-03-10', 2, 100, 'api-mar', '2026-03-12')]
    missing = trade(TxType.BUY, '2026-02-16', 4, 100, 'missing')
    statement = [replace(t, tx_id='stmt-' + t.tx_id, trade_date=t.settle_date, settle_date=None) for t in api]
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('failed-chunk', 'toss:syn', [statement[0], missing, statement[1]], {}, api_rows=api)
    current = ledger(api)
    current.failed_chunks = [{'source': 'toss:syn:orders', 'status': 'failed',
        'requested_window': {'start': '2026-02-01', 'end': '2026-02-28'}}]
    current.source_metadata['toss:syn:orders'] = {'fetched_windows': [
        {'start': '2026-01-01', 'end': '2026-01-31'}, {'start': '2026-03-01', 'end': '2026-06-30'}]}
    for _ in range(2):
        m.merge_statements(current, store, START, END, None, None)
        m.merge_statements(current, store, '2000-01-01', '2025-12-31', None, None)
        assert signature(current.transactions) == signature([api[0], missing, api[1]])
        assert [(t.trade_date, t.settle_date) for t in current.transactions if not t.source.startswith('statement:')] == [
            ('2026-01-30', '2026-02-03'), ('2026-03-10', '2026-03-12')]
        assert sum(t.quantity for t in current.transactions) == 9
        assert len(current.transactions) == 3
        assert not current.fetch_complete


def test_statement_only_inside_coverage_never_fills_and_never_certifies_a_total(tmp_path):
    api = [trade(TxType.BUY, '2026-03-02', 1, 100, 'start'),
        trade(TxType.SELL, '2026-03-06', 1, 120, 'end')]
    gap = replace(trade(TxType.BUY, '2026-03-04', 7, 70, 'gap'), symbol='GAP')
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('covered-gap', 'toss:syn', [gap], {})
    current = ledger(api)
    for _ in range(2):
        m.merge_statements(current, store, START, END, None, None)
        assert current.transactions == api
        totals = engine.compute_realized_pnl(current.transactions, FxRates(), START, END,
            input_quality=engine.ledger_input_quality(current))['totals']
        assert totals['realized_pnl_krw'] is None
        assert totals['status'] == 'partial'
