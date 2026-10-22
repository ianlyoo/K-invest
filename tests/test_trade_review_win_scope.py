"""2.6.2: synthetic book exclusions, without adjusting trades or positions."""
import hashlib
import json
from pathlib import Path

import pytest

import portfolio_engine as pe
from portfolio_model import FxRates, Transaction, TxType
from portfolio_sources import Collection

START, END = '2026-02-01', '2026-02-28'


def trade(symbol, side, day, qty, price, key, account='toss:synthetic', currency='KRW'):
    return Transaction('toss', account, account + ':' + currency + ':' + key, side, day, currency, symbol=symbol,
        quantity=qty, gross_amount=qty * price, fee_status='verified_zero', tax_status='verified_zero')


def cycle(symbol, price=120, account='toss:synthetic', currency='KRW'):
    return [trade(symbol, TxType.BUY, '2026-02-02', 1, 100, symbol + '-buy', account, currency),
        trade(symbol, TxType.SELL, '2026-02-03', 1, price, symbol + '-sell', account, currency)]


def broken(symbol='BROKEN', account='toss:synthetic', currency='KRW', closed=True):
    # One known closed cycle plus an unexplained remainder: 24 buys, 23 sells, 0 held.
    return (cycle(symbol, account=account, currency=currency) if closed else []) + [
        trade(symbol, TxType.BUY, '2026-02-05', 23 if closed else 24, 100, 'remaining-buy', account, currency),
        trade(symbol, TxType.SELL, '2026-02-06', 22 if closed else 23, 110, 'remaining-sell', account, currency)]


def fixture(name):
    clean = [*cycle('WIN'), *cycle('LOSS', 90)]
    rows = broken() if name == 'all_excluded' else clean
    if name in {'mixed', 'failed', 'truncated'}:
        rows = [*rows, *broken()]
    if name == 'no_breaks':
        rows = [*rows, *cycle('THIRD')]
    if name == 'out_of_scope':
        rows = [*rows, trade('OUTSIDE', TxType.BUY, '2026-03-02', 7, 100, 'outside')]
    ledger = Collection(transactions=rows, health={'toss:synthetic': {'status': 'ok'}},
        requested_window={'start': '2000-01-01', 'end': END}, fetched_at=END + 'T03:00:00Z', fetch_complete=True)
    if name == 'failed':
        ledger.health['toss:unavailable'] = {'status': 'error'}
    if name == 'truncated':
        ledger.health['toss:unavailable'] = {'status': 'truncated'}
        ledger.truncated_sources = ['toss:unavailable']
    positions = Collection(health={'toss:synthetic': {'status': 'empty'}}, fetched_at=END + 'T03:00:00Z')
    return ledger, positions


def review(name):
    ledger, positions = fixture(name)
    return pe.compute_trade_review(ledger, positions, FxRates(), START, END, detail_level='full')


# Additive engine-quantity and incomplete-basis fields, the v2 reconciliation
# label and the corrected ordering text are normalized away, so books without a quantity break keep
# their original pins. Break fixtures are re-pinned: a drifted book's realized P&L is now null
# (ENGINE_QUANTITY_DRIFT), its sales move to the incomplete-basis subset; win rates are unchanged.
ADDITIVE_FIELDS = {'engine_qty', 'engine_inferred_opening_qty', 'engine_quantity_difference',
    'engine_quantity_drift_breaks', 'proceeds', 'known_cost', 'unmatched_quantity', 'inferred_opening_qty',
    'proceeds_by_currency', 'known_partial_cost_by_currency', 'unmatched_quantity_by_symbol',
    'realized_pnl_status', 'realized_pnl_reason_codes',
    # win_rate_strict (= realized_win_rate) and win_rate_all, both additive.
    'win_rate_strict', 'win_rate_strict_eligible', 'win_rate_all', 'win_rate_all_eligible',
    'win_rate_all_taxes_unknown', 'win_rate_all_taxes_unknown_cycles', 'win_rate_all_reason_codes'}


def additive_normalized(value):
    if isinstance(value, dict):
        return {key: 'quantity_reconciliation_v1' if key == 'calculation_version'  # gitleaks:allow (synthetic test value)
            else 'trade_date then tx_id; intraday chronology unavailable' if key == 'ordering'
            else additive_normalized(item) for key, item in value.items() if key not in ADDITIVE_FIELDS}
    return [additive_normalized(item) for item in value] if isinstance(value, list) else value


def non_win_hash(result):
    # Pinned from 80b9b08, before the fix. Cover every other metric and status,
    # including detailed sales, closed cycles, reconciliation and cohort values.
    # New basis_scope labels are additive; the original numeric pins stay intact.
    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items()
                    if not key.startswith('realized_win_rate') and key != 'basis_scope'}
        return [strip(item) for item in value] if isinstance(value, list) else value
    return hashlib.sha256(json.dumps(strip(additive_normalized(result)), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def assert_exclusion(result, count=1):
    totals = result['totals']
    assert totals['realized_win_rate_scope'] == 'excludes_books_with_quantity_breaks'
    assert totals['realized_win_rate_excluded_cycle_count'] == count
    books = totals['realized_win_rate_excluded_books']
    assert len(books) == 1
    assert {key: books[0][key] for key in ('account', 'symbol', 'currency', 'excluded_cycle_count')} == {
        'account': 'toss:synthetic', 'symbol': 'BROKEN', 'currency': 'KRW', 'excluded_cycle_count': count}
    actual_break = result['reconciliation']['reconciliation_breaks'][0]
    assert set(books[0]['reason_codes']) == set(actual_break['reason_codes'])
    assert 'RECONCILIATION_BREAK' in books[0]['reason_codes']


def test_mixed_win_rate_excludes_only_broken_book_and_keeps_symbol_rates():
    result = review('mixed')
    totals = result['totals']
    assert totals['realized_win_rate'] == 0.5
    assert totals['realized_win_rate_denominator'] == 2
    assert totals['realized_win_rate_reason_codes'] == ['EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS']
    assert_exclusion(result)
    book = result['reconciliation']['reconciliation_breaks'][0]
    assert (book['buys'], book['sells'], book['ending_qty']) == (24, 23, 0)
    symbols = {row['symbol']: row for row in result['symbols']}
    assert symbols['WIN']['realized_win_rate'] == 1
    assert symbols['LOSS']['realized_win_rate'] == 0
    assert symbols['BROKEN']['realized_win_rate'] is None
    assert symbols['BROKEN']['realized_win_rate_denominator'] == 0
    assert symbols['BROKEN']['realized_win_rate_reason_codes'] == [
        'RECONCILIATION_BREAK', 'EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS']
    assert result['result_status'] == 'partial'
    assert totals['realized_pnl_krw'] is None


def test_all_known_cycles_excluded_stays_null_with_reconciliation_reason():
    result = review('all_excluded')
    totals = result['totals']
    assert totals['realized_win_rate'] is None
    assert totals['realized_win_rate_denominator'] == 0
    assert totals['realized_win_rate_reason_codes'] == [
        'RECONCILIATION_BREAK', 'EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS']
    assert_exclusion(result)


def test_no_breaks_keeps_rate_and_adds_empty_scope_metadata():
    result = review('no_breaks')
    for item in [result['totals'], *result['symbols']]:
        assert item['realized_win_rate_scope'] == 'all_known_cycles'
        assert item['realized_win_rate_excluded_books'] == []
        assert item['realized_win_rate_excluded_cycle_count'] == 0
        assert item['realized_win_rate_reason_codes'] == []
    assert result['totals']['realized_win_rate'] == 2 / 3
    assert result['totals']['realized_win_rate_denominator'] == 3


def test_no_known_cycles_keeps_existing_no_cycle_reason():
    ledger, positions = fixture('no_breaks')
    ledger.transactions = []
    totals = pe.compute_trade_review(ledger, positions, FxRates(), START, END)['totals']
    assert totals['realized_win_rate'] is None
    assert totals['realized_win_rate_denominator'] == 0
    assert totals['realized_win_rate_reason_codes'] == ['NO_KNOWN_BASIS_CLOSED_ROUND_TRIPS']
    assert totals['realized_win_rate_scope'] == 'all_known_cycles'
    assert totals['realized_win_rate_excluded_books'] == []


@pytest.mark.parametrize('name,reason', [('failed', 'FAILED_SOURCES'), ('truncated', 'TRUNCATED_SOURCES')])
def test_non_break_reasons_still_null_the_scoped_rate(name, reason):
    result = review(name)
    assert result['totals']['realized_win_rate'] is None
    assert reason in result['totals']['realized_win_rate_reason_codes']
    assert result['totals']['realized_win_rate_denominator'] == 2
    assert_exclusion(result)


def test_out_of_scope_break_is_not_listed_or_used_for_win_rate():
    result = review('out_of_scope')
    assert result['reconciliation']['reconciliation_breaks'][0]['symbol'] == 'OUTSIDE'
    totals = result['totals']
    assert totals['realized_win_rate'] == 0.5
    assert totals['realized_win_rate_denominator'] == 2
    assert totals['realized_win_rate_scope'] == 'all_known_cycles'
    assert totals['realized_win_rate_excluded_books'] == []
    assert totals['realized_win_rate_reason_codes'] == []


def test_broken_book_without_closed_cycles_is_still_listed():
    ledger, positions = fixture('mixed')
    ledger.transactions = [*cycle('WIN'), *cycle('LOSS', 90), *broken(closed=False)]
    result = pe.compute_trade_review(ledger, positions, FxRates(), START, END)
    assert result['totals']['realized_win_rate'] == 0.5
    assert result['totals']['realized_win_rate_reason_codes'] == ['EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS']
    assert_exclusion(result, count=0)


def test_exclusions_use_account_and_currency_and_sort_deterministically():
    ledger, positions = fixture('mixed')
    ledger.transactions = [*cycle('SAME'), *broken('SAME', 'toss:other'),
        *broken('SAME', currency='USD')]
    positions.health['toss:other'] = {'status': 'empty'}
    result = pe.compute_trade_review(ledger, positions, FxRates({'USD': 1400}), START, END)
    totals = result['totals']
    assert totals['realized_win_rate'] == 1
    assert totals['realized_win_rate_denominator'] == 1
    assert totals['realized_win_rate_excluded_cycle_count'] == 2
    assert [(book['account'], book['symbol'], book['currency'], book['excluded_cycle_count'])
        for book in totals['realized_win_rate_excluded_books']] == [
            ('toss:other', 'SAME', 'KRW', 1), ('toss:synthetic', 'SAME', 'USD', 1)]


@pytest.mark.parametrize('name', ['mixed', 'all_excluded', 'no_breaks', 'failed', 'truncated', 'out_of_scope'])
def test_every_non_win_rate_field_matches_pinned_261_baseline(name):
    baseline = json.loads((Path(__file__).parent / 'fixtures' / 'non_win_metrics.golden.json').read_text())
    assert non_win_hash(review(name)) == baseline[name]
