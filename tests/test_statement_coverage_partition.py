"""Present API rows always win; failed chunks never establish coverage."""
from dataclasses import replace

import portfolio_engine as engine
import statement_import as m
from portfolio_model import FxRates, Transaction, TxType
from portfolio_sources import Collection


def test_present_api_preserves_equal_occurrences_without_unique_ids(tmp_path):
    api = Transaction('toss', 'toss:synthetic', 'unidentified', TxType.BUY, '2025-03-03', 'KRW',
        symbol='SYN', quantity=2, gross_amount=100, fee_status='verified_zero', tax_status='verified_zero')
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('one-occurrence', api.account, [replace(api, tx_id='statement')], {})
    collection = Collection(transactions=[api, api], health={'toss:synthetic:orders': {'status': 'error'}},
        requested_window={'start': '2025-01-01', 'end': '2025-06-30'})
    m.merge_statements(collection, store, '2025-01-01', '2025-06-30', None, None)
    assert len(collection.transactions) == 2
    assert collection.transactions == [api, api]


def test_matching_partial_api_row_outside_successful_coverage_stays_authoritative(tmp_path):
    buy = Transaction('toss', 'toss:synthetic', 'buy', TxType.BUY, '2025-03-03', 'KRW',
        symbol='SYN', quantity=10, gross_amount=1000, fee_status='verified_zero', tax_status='verified_zero')
    sale = replace(buy, tx_id='sell', tx_type=TxType.SELL, trade_date='2025-05-05', gross_amount=1200)
    api = [replace(buy, tx_id='api-buy', fee_status='unknown'), replace(sale, tx_id='api-sale')]
    store = m.StatementStore(tmp_path / 'ledger.db')
    store.apply('partial-window', buy.account, [buy, sale], {}, api_rows=api)
    collection = Collection(transactions=api, requested_window={'start': '2025-01-01', 'end': '2025-06-30'},
        health={'toss:synthetic:orders': {'status': 'error'}}, failed_chunks=[{
            'source': 'toss:synthetic:orders', 'status': 'failed',
            'requested_window': {'start': '2025-01-01', 'end': '2025-03-31'}}],
        source_metadata={'toss:synthetic:orders': {'fetched_windows': [
            {'start': '2025-04-01', 'end': '2025-06-30'}]}})
    for _ in range(2):
        m.merge_statements(collection, store, '2025-01-01', '2025-06-30', None, None)
        assert len(collection.transactions) == 2
        assert collection.transactions == api
        assert collection.transactions[1].tx_id == 'api-sale'
        coverage = collection.source_metadata['statement:toss:partial-window']['coverage']
        assert coverage['api_authoritative_count'] == 2
        assert coverage['statement_used_count'] == 0
        assert coverage['api']['observed_window']['start'] == '2025-03-03'
        assert collection.transactions[0].fee_status == 'unknown'
    # API cost availability and failed-source eligibility remain unchanged.
    totals = engine.compute_realized_pnl(collection.transactions, FxRates(),
        input_quality=engine.ledger_input_quality(collection))['totals']
    assert totals['realized_pnl_krw'] is None
    assert 'FAILED_SOURCES' in totals['reason_codes']


def test_near_date_conflict_does_not_require_a_guessed_code_alias():
    api = Transaction('toss', 'toss:synthetic', 'api', TxType.BUY, '2025-03-03', 'USD',
        symbol='SYN', quantity=2, gross_amount=100)
    statement = replace(api, tx_id='stmt', trade_date='2025-03-05', symbol='US0000000001')
    result = m.deduplicate([statement], [api])
    assert result['new'] == []
    assert result['conflicting'][0]['reason_codes'] == ['STATEMENT_NEAR_MATCH_CONFLICT']
