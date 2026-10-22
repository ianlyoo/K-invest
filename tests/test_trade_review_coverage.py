"""2.6.3 coverage: invented trades, unchanged 2.6.2 metric eligibility."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import portfolio_engine as pe
from portfolio_model import FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection

START, END = '2026-02-01', '2026-02-28'
BASELINE_CASES = ('mixed', 'break_and_tax', 'failed', 'all_excluded', 'unknown_costs',
    'no_breaks', 'empty', 'one_account_break', 'lowercase_holding', 'missing_fx')


def trade(symbol, side, day, qty, price, key, account='toss:synthetic', currency='USD', **costs):
    return Transaction('toss', account, account + ':' + currency + ':' + key, side, day, currency,
        symbol=symbol, quantity=qty, gross_amount=qty * price, fee_status=costs.get('fee_status', 'verified_zero'),
        tax_status=costs.get('tax_status', 'verified_zero'))


def cycle(symbol, *, account='toss:synthetic', **costs):
    return [trade(symbol, TxType.BUY, '2026-02-02', 1, 100, symbol + '-buy', account, **costs),
        trade(symbol, TxType.SELL, '2026-02-03', 1, 120, symbol + '-sell', account, **costs)]


def broken(symbol='BROKEN', account='toss:synthetic', **costs):
    return [*cycle(symbol, account=account, **costs),
        trade(symbol, TxType.BUY, '2026-02-05', 3, 100, 'remaining-buy', account),
        trade(symbol, TxType.SELL, '2026-02-06', 2, 110, 'remaining-sell', account)]


def fixture(name):
    rows, holdings = cycle('CLEAN'), []
    if name in {'mixed', 'failed'}:
        rows += broken()
    elif name == 'all_excluded':
        rows = broken()
    elif name == 'break_and_tax':
        rows += broken(tax_status='unsupported')
    elif name in {'unknown_costs', 'missing_fx'}:
        rows += [*cycle('TAXONLY', tax_status='unsupported'),
            *cycle('OVERLAP', tax_status='unsupported', fee_status='unknown')]
    elif name in {'one_account_break', 'lowercase_holding'}:
        rows = [*cycle('SAME', account='toss:clean'), *broken('SAME', 'toss:broken')]
        if name == 'lowercase_holding':
            holdings = [Holding('toss', 'toss:broken', 'same', 'USD', 2,
                cost_basis=200, unrealized_pnl=0, market_value=200, last_price=100)]
    elif name == 'empty':
        rows = []
    accounts = {t.account for t in rows} or {'toss:synthetic'}
    ledger = Collection(transactions=rows, health={a: {'status': 'ok'} for a in accounts},
        requested_window={'start': '2000-01-01', 'end': END}, fetch_complete=True)
    if name == 'failed':
        ledger.health['toss:unavailable'] = {'status': 'error'}
    positions = Collection(holdings=holdings, health={a: {'status': 'ok'} for a in accounts},
        fetched_at=END + 'T03:00:00Z')
    return ledger, positions, FxRates() if name == 'missing_fx' else FxRates({'USD': 1400})


def review(name):
    ledger, positions, fx = fixture(name)
    return pe.compute_trade_review(ledger, positions, fx, START, END, detail_level='full')


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


def legacy_hash(result):
    # Additive coverage/scope labels and the approved exclusion-marker nit are
    # omitted. Preserve the original pins: no existing accounting field changed.
    # Win rates, denominators, other reasons, sales, cycles and reconciliation stay pinned.
    def strip(value):
        if isinstance(value, dict):
            return {key: [code for code in item if code != 'EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS']
                if key == 'realized_win_rate_reason_codes' else strip(item)
                for key, item in value.items() if key not in {'realized_win_rate_coverage', 'basis_scope'}}
        return [strip(item) for item in value] if isinstance(value, list) else value
    return hashlib.sha256(json.dumps(strip(additive_normalized(result)), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pinned_rates(result):
    return [{'symbol': row.get('symbol'), 'currency': row.get('currency'),
        'value': row['realized_win_rate'], 'denominator': row['realized_win_rate_denominator']}
        for row in [result['totals'], *result['symbols']]]


@pytest.mark.parametrize('name', BASELINE_CASES)
def test_all_existing_fields_including_win_rate_match_pinned_262_baseline(name):
    baseline = json.loads((Path(__file__).parent / 'fixtures' / 'existing_metrics.golden.json').read_text())
    result = review(name)
    assert legacy_hash(result) == baseline[name]['sha256']
    assert pinned_rates(result) == baseline[name]['win_rates']


@pytest.mark.parametrize('name', BASELINE_CASES)
def test_coverage_primary_counts_balance_for_totals_and_each_symbol(name):
    result = review(name)
    for metrics in [result['totals'], *result['symbols']]:
        coverage = metrics['realized_win_rate_coverage']
        assert coverage['eligible'] == metrics['realized_win_rate_denominator']
        assert coverage['closed_round_trips'] == metrics['round_trip_count']
        assert coverage['eligible'] + sum(coverage['excluded_by_reason'].values()) == coverage['closed_round_trips']
        assert sum(coverage['excluded_sole_reason'].values()) <= sum(coverage['excluded_by_reason'].values())


def test_break_precedes_tax_unsupported_and_any_reason_retains_overlap():
    result = review('break_and_tax')
    assert result['totals']['realized_win_rate_coverage'] == {
        'eligible': 1, 'closed_round_trips': 2, 'excluded_by_reason': {'QUANTITY_BREAK_BOOK': 1},
        'excluded_any_reason': {'QUANTITY_BREAK_BOOK': 1, 'TAXES_UNSUPPORTED': 1},
        'excluded_sole_reason': {}}
    broken_metrics = next(row for row in result['symbols'] if row['symbol'] == 'BROKEN')
    assert broken_metrics['realized_win_rate_coverage']['excluded_by_reason'] == {'QUANTITY_BREAK_BOOK': 1}


@pytest.mark.parametrize('name', ['unknown_costs', 'missing_fx'])
def test_sole_reason_counts_tax_only_but_not_tax_fee_overlap_ignoring_fx(name):
    result = review(name)
    coverage = result['totals']['realized_win_rate_coverage']
    assert coverage['eligible'] == 1
    assert coverage['excluded_by_reason'] == {'TAXES_UNSUPPORTED': 2}
    assert coverage['excluded_sole_reason'] == {'TAXES_UNSUPPORTED': 1}
    assert coverage['excluded_any_reason']['TAXES_UNSUPPORTED'] == 2
    assert coverage['excluded_any_reason']['FEES_UNKNOWN'] == 1
    symbols = {row['symbol']: row for row in result['symbols']}
    assert symbols['TAXONLY']['realized_win_rate_coverage']['excluded_sole_reason'] == {'TAXES_UNSUPPORTED': 1}
    assert symbols['OVERLAP']['realized_win_rate_coverage']['excluded_sole_reason'] == {}
    if name == 'missing_fx':
        assert coverage['excluded_any_reason']['MISSING_FX_RATE'] == 2
        assert 'MISSING_FX_RATE' not in coverage['excluded_by_reason']


def test_quantity_break_is_a_sole_reason_for_otherwise_eligible_cycle():
    result = review('mixed')
    for metrics in [result['totals'], next(row for row in result['symbols'] if row['symbol'] == 'BROKEN')]:
        assert metrics['realized_win_rate_coverage']['excluded_sole_reason'] == {'QUANTITY_BREAK_BOOK': 1}


def test_empty_window_reports_zero_counts():
    assert review('empty')['totals']['realized_win_rate_coverage'] == {
        'eligible': 0, 'closed_round_trips': 0, 'excluded_by_reason': {},
        'excluded_any_reason': {}, 'excluded_sole_reason': {}}


def test_failed_sources_keep_book_exclusion_marker_while_rate_stays_null():
    result = review('failed')
    metrics = result['totals']
    assert metrics['realized_win_rate'] is None
    assert 'FAILED_SOURCES' in metrics['realized_win_rate_reason_codes']
    assert 'EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS' in metrics['realized_win_rate_reason_codes']
    assert metrics['realized_win_rate_denominator'] == 1


@pytest.mark.parametrize('name', ['one_account_break', 'lowercase_holding'])
def test_per_symbol_rate_uses_clean_account_and_folds_break_book_case(name):
    result = review(name)
    assert len(result['symbols']) == 1
    metrics = result['symbols'][0]
    assert metrics['realized_win_rate'] == 1
    assert metrics['realized_win_rate_denominator'] == 1
    assert len(metrics['realized_win_rate_excluded_books']) == 1
    book = metrics['realized_win_rate_excluded_books'][0]
    assert (book['account'], book['symbol'], book['currency']) == ('toss:broken', 'SAME', 'USD')
    assert book['excluded_cycle_count'] == 1
    assert metrics['realized_win_rate_coverage']['excluded_by_reason'] == {'QUANTITY_BREAK_BOOK': 1}


@pytest.mark.parametrize('codes,basis,amount,primary', [
    (['FEES_UNKNOWN'], False, None, 'INCOMPLETE_COST_BASIS'),
    (['MISSING_PROCEEDS', 'TAXES_UNSUPPORTED'], True, None, 'MISSING_PROCEEDS'),
    (['TAXES_UNKNOWN', 'TAXES_UNSUPPORTED'], True, None, 'TAXES_UNSUPPORTED'),
    (['FEES_UNSUPPORTED', 'TAXES_UNKNOWN'], True, None, 'TAXES_UNKNOWN'),
    (['FEES_UNKNOWN', 'FEES_UNSUPPORTED'], True, None, 'FEES_UNSUPPORTED'),
    (['Z_OTHER', 'FEES_UNKNOWN'], True, None, 'FEES_UNKNOWN'),
    (['Z_OTHER', 'A_OTHER'], True, None, 'A_OTHER'),
    (['MISSING_FX_RATE'], True, None, 'REALIZED_PNL_UNAVAILABLE'),
])
def test_reporting_precedence_and_fallback_on_synthetic_cycle_evidence(monkeypatch, codes, basis, amount, primary):
    # Isolate coverage reporting from replay; real cost propagation is covered above.
    ledger, positions, fx = fixture('no_breaks')
    evidence = {'account': 'toss:synthetic', 'symbol': 'CLEAN', 'currency': 'USD',
        'cost_basis_complete': basis, 'realized_pnl': amount, 'reason_codes': codes}
    monkeypatch.setattr(pe, '_review_cycles', lambda *a: ([], [evidence]))
    result = pe.compute_trade_review(ledger, positions, fx, START, END)
    coverage = result['totals']['realized_win_rate_coverage']
    assert coverage['eligible'] == 0
    assert coverage['closed_round_trips'] == 1
    assert coverage['excluded_by_reason'] == {primary: 1}
    assert coverage['excluded_any_reason'][primary] == 1


def test_real_missing_basis_and_proceeds_are_counted_without_replay_changes():
    ledger, positions, fx = fixture('no_breaks')
    ledger.transactions = [replace(row, gross_amount=None, price=None) for row in ledger.transactions]
    result = pe.compute_trade_review(ledger, positions, fx, START, END, detail_level='full')
    cycle_row = result['round_trips'][0]
    assert not cycle_row['cost_basis_complete']
    assert 'MISSING_PROCEEDS' in cycle_row['reason_codes']
    coverage = result['totals']['realized_win_rate_coverage']
    assert coverage['excluded_by_reason'] == {'INCOMPLETE_COST_BASIS': 1}
    assert coverage['excluded_any_reason'] == {'INCOMPLETE_COST_BASIS': 1, 'MISSING_PROCEEDS': 1}
    assert coverage['excluded_sole_reason'] == {}
