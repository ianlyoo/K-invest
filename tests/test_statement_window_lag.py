"""Invented settlement-boundary trades; no PDFs or broker IO."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import portfolio_engine as engine
import server
import statement_import as m
from portfolio_model import FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection
from toss_accounts import TossAccountRegistry, TossCredentials


def trade(side, day, qty, price, key, symbol='SYN', settle=None):
    return Transaction('toss', 'toss:syn', key, side, day, 'KRW', symbol=symbol,
        quantity=qty, price=price, gross_amount=qty * price, settle_date=settle,
        fee_status='verified_zero', tax_status='verified_zero')


def ledger(rows, start='2026-01-01', end='2026-06-30'):
    return Collection(transactions=list(rows), health={'toss:syn:orders': {'status': 'ok'}},
        requested_window={'start': start, 'end': end}, fetch_complete=True,
        fetched_at=end + 'T03:00:00Z')


def test_yearstart_summary_matches_all_history_and_repeated_merges(tmp_path, monkeypatch):
    old = trade(TxType.BUY, '2025-12-30', 10, 100, 'old', settle='2026-01-02')
    current = [trade(TxType.BUY, '2026-01-02', 1, 50, 'other', 'OTH', '2026-01-06'),
        trade(TxType.SELL, '2026-03-02', 10, 150, 'sale', settle='2026-03-04')]
    statement = [replace(t, tx_id='stmt-' + t.tx_id, trade_date=t.settle_date, settle_date=None)
        for t in [old, *current]]
    store = m.StatementStore(tmp_path / 'synthetic.db')
    store.apply('synthetic-yearstart', 'toss:syn', statement, {}, api_rows=[old, *current])
    all_history = m.merge_statements(ledger([old, *current], '2025-01-01'), store,
        '2025-01-01', '2026-06-30', None, None)
    expected = engine.compute_realized_pnl(all_history.transactions, FxRates(),
        '2026-01-01', '2026-06-30', input_quality=engine.ledger_input_quality(all_history))['totals']
    assert expected['realized_pnl_krw'] == 500
    assert expected['status'] == 'complete'
    ytd = ledger(current)
    for _ in range(2):
        m.merge_statements(ytd, store, '2026-01-01', '2026-06-30', None, None)
        m.merge_statements(ytd, store, '1970-01-01', '2025-12-31', None, None)
        totals = engine.compute_realized_pnl(ytd.transactions, FxRates(),
            '2026-01-01', '2026-06-30', input_quality=engine.ledger_input_quality(ytd))['totals']
        assert totals['realized_pnl_krw'] == expected['realized_pnl_krw']
        assert totals['status'] == 'complete'
        assert len(ytd.transactions) == 3
        assert [t for t in ytd.transactions if not t.source.startswith('statement:')] == current
    positions = Collection(holdings=[Holding('toss', 'toss:syn', 'OTH', 'KRW', 1,
        last_price=50, market_value=50, cost_basis=50, unrealized_pnl=0)],
        health={'toss:syn': {'status': 'ok'}}, fetched_at='2026-06-30T03:00:00Z')
    calls = []
    def collect(start, end, *args):
        calls.append((start, end))
        return m.merge_statements(ledger(current, start, end), store, start, end, None, None)
    collector = SimpleNamespace(collect_transactions=collect, collect_positions=lambda: positions,
        build_fx=lambda *a: FxRates(), _providers=SimpleNamespace(statement_store=lambda: store))
    monkeypatch.setattr(server, '_get_collector', lambda: collector)
    monkeypatch.setattr(engine, 'resolve_period', lambda *a, **kw: ('2026-01-01', '2026-06-30'))
    summary = server.get_portfolio_summary(snapshot=False)['data']
    assert summary['realized_pnl_krw'] == 500
    assert summary['realized_pnl_status'] == 'complete'
    assert summary['realized_pnl_reason_codes'] == []
    assert calls == [('2026-01-01', '2026-06-30')]


@pytest.mark.parametrize('day,blocked', [('2026-01-02', False), ('2026-01-07', True)])
def test_apply_gate_uses_statement_window_start_lag(tmp_path, monkeypatch, day, blocked):
    api = [trade(TxType.BUY, '2026-01-01', 1, 50, 'anchor', 'OTH'),
        trade(TxType.SELL, '2026-01-30', 1, 60, 'end', 'OTH')]
    rows = [trade(TxType.BUY, day, 10, 100, 'basis'), *api]
    parsed = m.ParsedStatement('invented-lag', 1, '64321867530',
        {'start': '2026-01-01', 'end': '2026-01-30'}, rows, {}, [])
    monkeypatch.setattr(m, 'read_inbox', lambda *a: b'invented')
    monkeypatch.setattr(m.TossPdfParser, 'parse', lambda *a: parsed)
    store = m.StatementStore(tmp_path / 'synthetic.db')
    registry = TossAccountRegistry([TossCredentials('syn', 'invented', 'invented',
        '64321867530')], cache_dir=tmp_path)
    manager = m.PreviewManager(store, tmp_path, ledger=lambda *a: ledger(api, *a[:2]),
        labels=registry.account_labels, registry=lambda: registry)
    preview = manager.preview('toss', 'synthetic.pdf')
    assert preview['new_inside_api_coverage'] == int(blocked)
    assert manager.apply(preview['preview_id'])['blocked'] is blocked
    if blocked:
        with pytest.raises(m.StatementError) as error:
            manager.apply(preview['preview_id'], True)
        assert error.value.code == 'NEW_INSIDE_API_COVERAGE'
        assert not store.db_path.exists()
    else:
        assert preview['result_status'] == 'complete'
        assert manager.apply(preview['preview_id'], True)['written_count'] == 3


@pytest.mark.parametrize('start,day,inside', [
    ('2026-01-01', '2026-01-02', False),
    ('2026-01-01', '2026-01-05', False),
    ('2026-01-01', '2026-01-06', True),
    ('2026-01-01', '2026-01-07', True),
    ('2026-01-02', '2026-01-05', False),
    ('2026-01-02', '2026-01-06', False),
    ('2026-01-02', '2026-01-07', True),
    ('2026-01-02', '2026-01-08', True),
])
def test_statement_lag_counts_business_days_and_keeps_boundary_covered(start, day, inside):
    current = ledger([trade(TxType.BUY, start, 1, 50, 'start'),
        trade(TxType.SELL, '2026-01-30', 1, 60, 'end')], start, '2026-01-30')
    assert m._inside_coverage(trade(TxType.BUY, day, 10, 100, 'stmt'), m._api_coverage(current)) is inside


def test_lag_applies_to_each_successful_chunk_not_observed_start():
    current = ledger([trade(TxType.BUY, '2026-01-20', 1, 50, 'first'),
        trade(TxType.SELL, '2026-03-20', 1, 60, 'last')])
    current.source_metadata['toss:syn:orders'] = {'fetched_windows': [
        {'start': '2026-01-01', 'end': '2026-01-30'},
        {'start': '2026-03-02', 'end': '2026-03-20'}]}
    coverage = m._api_coverage(current)
    assert m._inside_coverage(trade(TxType.BUY, '2026-01-21', 10, 100, 'observed'), coverage)
    assert not m._inside_coverage(trade(TxType.BUY, '2026-03-03', 10, 100, 'lag'), coverage)
    assert m._inside_coverage(trade(TxType.BUY, '2026-03-06', 10, 100, 'covered'), coverage)
