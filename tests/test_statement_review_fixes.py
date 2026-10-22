"""2.6.0 review regressions: invented trades and in-memory parser substitutes only."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import portfolio_engine as engine
import server
import statement_import as m
from portfolio_model import FxRates, Transaction, TxType
from portfolio_sources import Collection


def trade(side, day, quantity, price, tx_id):
    return Transaction('toss', 'toss:syn', tx_id, side, day, 'KRW', symbol='SYN',
        quantity=quantity, price=price, gross_amount=quantity * price,
        fee_status='verified_zero', tax_status='verified_zero',
        net_amount=(-1 if side is TxType.BUY else 1) * quantity * price)


def ledger(rows=(), start='2026-01-01', end='2026-06-30'):
    return Collection(transactions=list(rows), health={'toss:syn:orders': {'status': 'ok'}},
        fetch_complete=True, requested_window={'start': start, 'end': end},
        fetched_at=end + 'T12:00:00Z')


def preview_manager(tmp_path, monkeypatch, rows, api, *, filename='synthetic.pdf'):
    from toss_accounts import TossAccountRegistry, TossCredentials
    parsed = m.ParsedStatement(filename, 1, '64321867530',
        {'start': '2025-01-01', 'end': '2026-06-30'}, list(rows), {}, [])
    monkeypatch.setattr(m, 'read_inbox', lambda *a: b'invented')
    monkeypatch.setattr(m.TossPdfParser, 'parse', lambda *a: parsed)
    store = m.StatementStore(tmp_path / 'ledger.db')
    manager = m.PreviewManager(store, tmp_path, ledger=lambda *a: ledger(api, *a[:2]), labels=lambda: ['syn'],
        registry=lambda: TossAccountRegistry([TossCredentials('syn', 'invented-client', 'invented-secret',
            '64321867530')], cache_dir=tmp_path))
    return store, manager, manager.preview('toss', filename)


@pytest.mark.parametrize('variant', ['silent', 'gap'])
def test_summary_preserves_import_time_matches_outside_ytd(tmp_path, monkeypatch, variant):
    buy = trade(TxType.BUY, '2025-03-03', 10, 100, 's1')
    old_sale = trade(TxType.SELL, '2025-11-03', 5 if variant == 'silent' else 10, 120, 's2')
    statement = [buy, old_sale]
    api_at_import = [replace(old_sale, tx_id='a2')]
    if variant == 'silent':
        later_buy = trade(TxType.BUY, '2025-12-01', 5, 300, 's3')
        statement.append(later_buy)
        api_at_import.append(replace(later_buy, tx_id='a3'))
        ytd = [trade(TxType.SELL, '2026-05-04', 10, 250, 'a4')]
        expected = 500
    else:
        ytd = [trade(TxType.BUY, '2026-02-02', 5, 200, 'a3'), trade(TxType.SELL, '2026-05-04', 5, 210, 'a4')]
        expected = 50
    store, manager, preview = preview_manager(tmp_path, monkeypatch, statement, api_at_import)
    manager.apply(preview['preview_id'], True)
    saved, _ = store.load('2025-01-01', '2025-12-31')
    assert len(saved) == len(statement)
    assert saved[1].statement_provenance['match_status'] == 'matched'
    assert saved[1].statement_provenance['matched_api_key']
    calls = []
    def transactions(start, end, *args):
        calls.append((start, end))
        return m.merge_statements(ledger(ytd, start, end), store, start, end, None, None)
    positions = Collection(health={'toss:syn': {'status': 'empty'}}, fetched_at='2026-06-30T12:00:00Z')
    collector = SimpleNamespace(collect_positions=lambda: positions, collect_transactions=transactions,
        build_fx=lambda _: FxRates(), _providers=SimpleNamespace(statement_store=lambda: store))
    monkeypatch.setattr(server, '_get_collector', lambda: collector)
    monkeypatch.setattr(engine, 'resolve_period', lambda period, **kwargs: ('2000-01-01' if period == 'ALL' else '2026-01-01', '2026-06-30'))
    result = server.get_portfolio_summary(snapshot=False)['data']
    assert result['realized_pnl_krw'] == expected
    assert result['realized_pnl_status'] == 'complete'
    assert calls == [('2026-01-01', '2026-06-30')]


def test_current_api_precedence_and_retention_fallback_are_counted_once(tmp_path, monkeypatch):
    rows = [trade(TxType.BUY, '2025-03-03', 10, 100, 's1'), trade(TxType.SELL, '2025-11-03', 10, 120, 's2')]
    api = [replace(t, tx_id='api-' + t.tx_id) for t in rows]
    store, manager, preview = preview_manager(tmp_path, monkeypatch, rows, api)
    manager.apply(preview['preview_id'], True)
    covered = m.merge_statements(ledger(api, '2025-01-01'), store, '2025-01-01', '2026-06-30', None, None)
    assert covered.transactions == api
    assert covered.source_metadata['statement:toss:synthetic.pdf']['coverage']['statement_used_count'] == 0
    assert covered.source_metadata['statement:toss:synthetic.pdf']['coverage']['api_authoritative_count'] == 2
    aged = m.merge_statements(ledger([], '2025-01-01'), store, '2025-01-01', '2026-06-30', None, None)
    m.merge_statements(aged, store, '2025-01-01', '2026-06-30', None, None)
    assert len(aged.transactions) == 2
    assert all(t.statement_provenance['coverage']['decision'] == 'outside_api_coverage' for t in aged.transactions)
    assert engine.compute_realized_pnl(aged.transactions, FxRates())['totals']['realized_pnl_krw'] == 200


def test_before_observed_api_range_and_failed_chunks_are_not_coverage(tmp_path):
    early = trade(TxType.BUY, '2025-03-03', 10, 100, 'early')
    recent = trade(TxType.SELL, '2025-11-03', 10, 120, 'recent')
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('before-observed', 'toss:syn', [early], {})
    current = ledger([recent], '2025-01-01')
    m.merge_statements(current, store, '2025-01-01', '2026-06-30', None, None)
    m.merge_statements(current, store, '2025-01-01', '2026-06-30', None, None)
    used = [t for t in current.transactions if t.source and t.source.startswith('statement:')]
    assert len(used) == 1
    coverage = used[0].statement_provenance['coverage']
    assert coverage['decision'] == 'outside_api_coverage'
    assert coverage['api_coverage']['requested_windows'][0]['start'] == '2025-01-01'
    assert coverage['api_coverage']['observed_window']['start'] == '2025-11-03'
    current.health['toss:syn:orders']['status'] = 'error'
    current.failed_chunks = [{'source': 'toss:syn:orders'}]
    assert m._api_coverage(current)['toss:syn']['windows'] == []
    current.health['toss:syn:orders']['status'] = 'truncated'
    current.truncated_sources = ['toss:syn:orders']
    assert m._api_coverage(current)['toss:syn']['windows'] == []


def test_all_ledger_consumers_use_the_same_merged_evidence(tmp_path, monkeypatch):
    from portfolio_export import collect_dataset
    statement = [trade(TxType.BUY, '2025-03-03', 10, 100, 's1'),
        trade(TxType.SELL, '2025-11-03', 5, 120, 's2'), trade(TxType.BUY, '2025-12-01', 5, 300, 's3')]
    api = [trade(TxType.SELL, '2026-05-04', 10, 250, 'current')]
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('all-consumers', 'toss:syn', statement, {}, api_rows=statement[1:])
    positions = Collection(health={'toss:syn': {'status': 'empty'}}, fetched_at='2026-06-30T12:00:00Z')
    def transactions(start, end, accounts=None, symbol=None, **kwargs):
        current = ledger([t for t in api if start <= t.trade_date <= end], start, end)
        return m.merge_statements(current, store, start, end, accounts, symbol)
    collector = SimpleNamespace(collect_transactions=transactions, collect_positions=lambda *a: positions,
        build_fx=lambda *a: FxRates(), _providers=SimpleNamespace(statement_store=lambda: store))
    monkeypatch.setattr(server, '_get_collector', lambda: collector)
    monkeypatch.setattr(server, '_get_nav_store', lambda: SimpleNamespace(range=lambda *a: []))
    monkeypatch.setattr(engine, 'resolve_period', lambda period, **kwargs: ('2000-01-01' if period == 'ALL' else '2026-01-01', '2026-06-30'))
    assert server.get_portfolio_summary(snapshot=False)['data']['realized_pnl_krw'] == 500
    assert server.get_realized_pnl(account='toss:syn')['data']['totals']['realized_pnl_krw'] == 500
    assert server.get_portfolio_performance(risk_free_rate=0)['data']['pnl']['realized_krw'] == 500
    assert server.get_trade_review(account='toss:syn')['data']['totals']['realized_pnl_krw'] == 500
    response = server.get_all_transactions('2025-01-01', '2026-06-30', 'toss:syn')
    assert len(response['data']['transactions']) == 4
    rows, manifest = collect_dataset('realized_pnl', '2026-01-01', '2026-06-30', collector=collector, accounts=['toss:syn'])
    assert manifest['summary']['totals']['realized_pnl_krw'] == 500
    assert len([r for r in rows if r['record_type'] == 'transaction']) == 4


def test_partial_api_chunks_leave_failed_dates_uncovered():
    current = ledger([trade(TxType.BUY, '2025-03-03', 1, 100, 'a'),
        trade(TxType.SELL, '2025-11-03', 1, 120, 'b')], '2025-01-01')
    current.health['toss:syn:orders']['status'] = 'error'
    current.failed_chunks = [{'source': 'toss:syn:orders',
        'requested_window': {'start': '2025-04-01', 'end': '2025-09-30'}}]
    current.source_metadata['toss:syn:orders'] = {'fetched_windows': [
        {'start': '2025-01-01', 'end': '2025-03-31'},
        {'start': '2025-10-01', 'end': '2026-06-30'}]}
    coverage = m._api_coverage(current)
    assert coverage['toss:syn']['requested_windows'] == [current.requested_window]
    assert not m._inside_coverage(trade(TxType.BUY, '2025-06-02', 1, 90, 'gap'), coverage)
    assert m._inside_coverage(current.transactions[0], coverage)


@pytest.mark.parametrize('api_day,statement_day,api_code,statement_code', [
    ('2025-03-03', '2025-03-05', 'SYN', 'SYN'),  # API omitted settlementDate; T+2
    ('2025-03-07', '2025-03-11', 'SYN', 'SYN'),  # weekend isn't a business day
    ('2025-03-03', '2025-03-03', 'SYN', 'US0000000001'),
])
def test_near_statement_duplicates_are_conflicts(api_day, statement_day, api_code, statement_code):
    api = replace(trade(TxType.BUY, api_day, 10, 100, 'api'), symbol=api_code)
    statement = replace(trade(TxType.BUY, statement_day, 10, 100, 'stmt'), symbol=statement_code)
    result = m.deduplicate([statement], [api])
    assert result['new'] == []
    assert result['conflicting'][0]['reason_codes'] == ['STATEMENT_NEAR_MATCH_CONFLICT']


def test_unmatched_trade_inside_observed_coverage_blocks_apply(tmp_path, monkeypatch):
    statement = [trade(TxType.BUY, '2025-05-05', 3, 77, 'stmt')]
    api = [trade(TxType.BUY, '2025-03-03', 10, 100, 'start'),
        trade(TxType.SELL, '2025-11-03', 10, 120, 'end')]
    store, manager, preview = preview_manager(tmp_path, monkeypatch, statement, api)
    assert preview['new_inside_api_coverage'] == 1
    assert manager.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError) as error:
        manager.apply(preview['preview_id'], True)
    assert error.value.code == 'NEW_INSIDE_API_COVERAGE'
    assert not store.db_path.exists()


def test_before_observed_trade_is_not_blocked_by_requested_history(tmp_path, monkeypatch):
    statement = [trade(TxType.BUY, '2025-03-03', 10, 100, 'early')]
    api = [trade(TxType.SELL, '2025-11-03', 10, 120, 'recent')]
    statement.append(replace(api[0], tx_id='stmt-recent'))
    store, manager, preview = preview_manager(tmp_path, monkeypatch, statement, api)
    assert preview['new_inside_api_coverage'] == 0
    manager.apply(preview['preview_id'], True)
    current = ledger(api, '2025-01-01')
    for _ in range(2):
        m.merge_statements(current, store, '2025-01-01', '2026-06-30', None, None)
    assert len(current.transactions) == 2
    assert current.transactions[0].statement_provenance['coverage']['decision'] == 'outside_api_coverage'


def test_apply_refreshes_new_inside_coverage_guard(tmp_path, monkeypatch):
    statement = [trade(TxType.BUY, '2025-05-05', 3, 77, 'stmt')]
    store, manager, preview = preview_manager(tmp_path, monkeypatch, statement, [])
    manager.ledger = lambda *a: ledger([trade(TxType.BUY, '2025-03-03', 10, 100, 'a'),
        trade(TxType.SELL, '2025-11-03', 10, 120, 'b')], *a[:2])
    with pytest.raises(m.StatementError) as error:
        manager.apply(preview['preview_id'], True)
    assert error.value.code == 'NEW_INSIDE_API_COVERAGE'
    assert not store.db_path.exists()


@pytest.mark.parametrize('configured', ['643-21-867531', '', 'opaque-identifier-7530'])
def test_unconfirmed_first_account_binding_is_blocked(tmp_path, monkeypatch, configured):
    from toss_accounts import TossAccountRegistry, TossCredentials
    store, manager, _ = preview_manager(tmp_path, monkeypatch,
        [trade(TxType.BUY, '2025-03-03', 10, 100, 'stmt')], [])
    manager.registry = lambda: TossAccountRegistry([
        TossCredentials('syn', 'invented-client', 'invented-secret', configured)], cache_dir=tmp_path)
    preview = manager.preview('toss', 'synthetic.pdf', 'auto')
    assert not preview['detected_account']['suffix_confirmed']
    assert manager.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError) as error:
        manager.apply(preview['preview_id'], True)
    assert error.value.code == 'STATEMENT_ACCOUNT_UNCONFIRMED'
    assert not store.db_path.exists()


def test_correct_registry_account_passes_without_exposing_credentials(tmp_path, monkeypatch):
    from toss_accounts import TossAccountRegistry, TossCredentials
    store, manager, _ = preview_manager(tmp_path, monkeypatch,
        [trade(TxType.BUY, '2025-03-03', 10, 100, 'stmt')], [])
    manager.registry = lambda: TossAccountRegistry([
        TossCredentials('syn', 'invented-client', 'invented-secret', '643-21-867530')], cache_dir=tmp_path)
    preview = manager.preview('toss', 'synthetic.pdf')
    assert preview['detected_account']['suffix_confirmed']
    assert not manager.apply(preview['preview_id'])['blocked']
    manager.apply(preview['preview_id'], True)
    _, imports = store.load('2025-01-01', '2026-06-30')
    for value in ('64321867530', '643-21-867530', 'invented-secret', 'invented-client'):
        assert value not in str(preview) + str(imports)


def test_first_binding_without_any_api_match_is_blocked(tmp_path, monkeypatch):
    from toss_accounts import TossAccountRegistry, TossCredentials
    statement = [trade(TxType.BUY, '2025-03-03', 10, 100, 'stmt')]
    api = [trade(TxType.SELL, '2025-11-03', 10, 120, 'api')]
    store, manager, _ = preview_manager(tmp_path, monkeypatch, statement, api)
    manager.registry = lambda: TossAccountRegistry([
        TossCredentials('syn', 'invented-client', 'invented-secret', '64321867530')], cache_dir=tmp_path)
    preview = manager.preview('toss', 'synthetic.pdf')
    assert preview['new_inside_api_coverage'] == 0
    assert manager.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError) as error:
        manager.apply(preview['preview_id'], True)
    assert error.value.code == 'STATEMENT_ACCOUNT_NO_API_MATCH'
    assert not store.db_path.exists()


def test_store_rejects_unconfirmed_first_binding(tmp_path):
    store = m.StatementStore(tmp_path / 'ledger.db')
    with pytest.raises(m.StatementError) as error:
        store.apply('unconfirmed', 'toss:syn', [trade(TxType.BUY, '2025-03-03', 10, 100, 'stmt')],
            {'detected_account': {'account_id': 'acct_invented', 'suffix_confirmed': False}})
    assert error.value.code == 'STATEMENT_ACCOUNT_UNCONFIRMED'
    assert store.load('2025-01-01', '2026-06-30')[0] == []


def test_undo_reapply_with_shrunken_api_preserves_parsed_row_identity(tmp_path, monkeypatch):
    import sqlite3
    rows = [trade(TxType.BUY, '2025-03-03', 10, 100, 'stmt-buy'),
        trade(TxType.SELL, '2025-11-03', 10, 120, 'stmt-sell')]
    store, manager, preview = preview_manager(tmp_path, monkeypatch, rows, [replace(rows[0], tx_id='api-buy')])
    applied = manager.apply(preview['preview_id'], True)
    first = store.load('2025-01-01', '2026-06-30')[0]
    identities = [t.execution_id for t in first]
    assert store.undo(applied['import_id'])['removed_count'] == 2
    manager.ledger = lambda *a: ledger([], *a[:2])
    fresh = manager.preview('toss', 'synthetic.pdf')
    reapplied = manager.apply(fresh['preview_id'], True)
    assert reapplied['import_id'] == applied['import_id']
    final = store.load('2025-01-01', '2026-06-30')[0]
    assert [t.execution_id for t in final] == identities
    assert [t.statement_provenance['parsed_position'] for t in final] == [0, 1]
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM statement_rows').fetchone()[0] == 2
    merged = m.merge_statements(ledger([], '2025-01-01'), store, '2025-01-01', '2026-06-30', None, None)
    assert engine.compute_realized_pnl(merged.transactions, FxRates())['totals']['realized_pnl_krw'] == 200


def test_trade_review_statement_rows_are_not_orders(tmp_path):
    statement = [trade(TxType.BUY, '2026-02-02', 1, 100, 's1'),
        trade(TxType.SELL, '2026-03-02', 1, 120, 's2')]
    api = [trade(TxType.BUY, '2026-04-01', 1, 200, 'a1'),
        trade(TxType.SELL, '2026-05-04', 1, 210, 'a2')]
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('review-only', 'toss:syn', statement, {})
    current = m.merge_statements(ledger(api), store, '2026-01-01', '2026-06-30', None, None)
    positions = Collection(health={'toss:syn': {'status': 'empty'}}, fetched_at='2026-06-30T12:00:00Z')
    # The per-symbol metric table is detail_level="full" (summary lists the top symbols only).
    result = engine.compute_trade_review(current, positions, FxRates(), '2026-01-01', '2026-06-30', detail_level='full')
    for metrics in (result['totals'], result['symbols'][0]):
        assert metrics['order_count'] == 2
        assert metrics['statement_trade_count'] == 2
    assert result['totals']['round_trip_count'] == 2
    assert result['totals']['realized_pnl_krw'] == 30


def test_trade_review_win_rate_reconciliation_is_scoped():
    rows = [trade(TxType.BUY, '2026-02-02', 1, 100, 'buy'),
        trade(TxType.SELL, '2026-03-02', 1, 120, 'sell'),
        replace(trade(TxType.BUY, '2026-04-01', 10, 200, 'unexplained'), symbol='OTHER')]
    positions = Collection(health={'toss:syn': {'status': 'empty'}}, fetched_at='2026-06-30T12:00:00Z')
    # Per-symbol reason codes are in the detail_level="full" table.
    result = engine.compute_trade_review(ledger(rows), positions, FxRates(), '2026-01-01', '2026-06-30', detail_level='full')
    assert result['totals']['realized_win_rate'] == 1
    assert result['totals']['realized_win_rate_denominator'] == 1
    assert result['totals']['realized_win_rate_reason_codes'] == ['EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS']
    assert result['totals']['realized_win_rate_excluded_books'][0]['symbol'] == 'OTHER'
    assert result['totals']['realized_win_rate_excluded_books'][0]['excluded_cycle_count'] == 0
    unbroken = next(s for s in result['symbols'] if s['symbol'] == 'SYN')
    assert unbroken['realized_win_rate'] == 1
    assert unbroken['realized_win_rate_reason_codes'] == []
    scoped = engine.compute_trade_review(ledger(rows), positions, FxRates(), '2026-01-01', '2026-06-30', symbol='OTHER')
    assert scoped['totals']['realized_win_rate'] is None
    assert 'RECONCILIATION_BREAK' in scoped['totals']['realized_win_rate_reason_codes']
    broken_same_symbol = [*rows[:2], trade(TxType.BUY, '2026-04-01', 10, 200, 'unexplained')]
    broken = engine.compute_trade_review(ledger(broken_same_symbol), positions, FxRates(), '2026-01-01', '2026-06-30')
    assert broken['totals']['realized_win_rate'] is None
    assert 'RECONCILIATION_BREAK' in broken['totals']['realized_win_rate_reason_codes']
