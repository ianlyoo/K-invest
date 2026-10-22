"""win_rate_all next to win_rate_strict; and the trade review summary body.

Invented trades through the real compute_trade_review engine path (test_trade_review_coverage's
builders). Synthetic amounts only.
"""
import json

from test_trade_review_coverage import END, START, broken, cycle, trade

import portfolio_engine as pe
from portfolio_model import FxRates, TxType
from portfolio_sources import Collection


def review(rows, detail_level='full', fx=None):
    accounts = {t.account for t in rows}
    ledger = Collection(transactions=rows, health={a: {'status': 'ok'} for a in accounts},
                        requested_window={'start': '2000-01-01', 'end': END}, fetch_complete=True)
    positions = Collection(health={a: {'status': 'ok'} for a in accounts}, fetched_at=END + 'T03:00:00Z')
    return pe.compute_trade_review(ledger, positions, fx or FxRates({'USD': 1400}), START, END,
                                   detail_level=detail_level)


def loss(symbol, **costs):
    return [trade(symbol, TxType.BUY, '2026-02-02', 1, 100, symbol + '-buy', **costs),
            trade(symbol, TxType.SELL, '2026-02-03', 1, 90, symbol + '-sell', **costs)]


def test_win_rate_all_adds_tax_unknown_round_trips_and_flags_them():
    rows = [*cycle('CLEAN'), *loss('LOSS'), *cycle('TAXWIN', tax_status='unsupported'),
            *loss('TAXLOSS', tax_status='unknown'), *cycle('FEEGAP', fee_status='unknown')]
    totals = review(rows)['totals']
    assert totals['realized_win_rate'] == totals['win_rate_strict'] == 0.5
    assert totals['realized_win_rate_denominator'] == totals['win_rate_strict_eligible'] == 2
    # TAXWIN (tax unsupported) and TAXLOSS (tax unknown) join; FEEGAP (fee unknown) never does.
    assert totals['win_rate_all'] == 0.5 and totals['win_rate_all_eligible'] == 4
    assert totals['win_rate_all_taxes_unknown'] is True and totals['win_rate_all_taxes_unknown_cycles'] == 2
    assert totals['win_rate_all_reason_codes'] == []
    clean = review(cycle('CLEAN'))['totals']
    assert clean['win_rate_all'] == clean['win_rate_strict'] == 1.0
    assert clean['win_rate_all_taxes_unknown'] is False and clean['win_rate_all_eligible'] == 1


def test_win_rate_all_keeps_the_break_book_exclusion():
    totals = review([*cycle('CLEAN'), *broken(tax_status='unsupported')])['totals']
    assert totals['win_rate_strict_eligible'] == totals['win_rate_all_eligible'] == 1
    assert totals['win_rate_all_taxes_unknown_cycles'] == 0
    assert 'EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS' in totals['win_rate_all_reason_codes']


def many_symbols():
    rows = []
    for index in range(14):
        symbol = f'S{index:02d}'
        price = 100 + (index + 1) * 5
        rows += [trade(symbol, TxType.BUY, '2026-02-02', 1, 100, symbol + '-buy'),
                 trade(symbol, TxType.SELL, '2026-02-03', 1, price, symbol + '-sell',
                       tax_status='unsupported' if index % 2 else 'verified_zero')]
    return rows


def test_summary_keeps_totals_top_symbols_and_reason_groups_full_is_unchanged():
    rows = many_symbols()
    full, summary = review(rows), review(rows, 'summary')
    assert len(full['symbols']) == 14 and all('cohorts' in row for row in full['symbols'])
    assert 'cohorts' in full['totals'] and 'omitted_symbols' not in full
    assert summary['totals'] == {k: v for k, v in full['totals'].items() if k != 'cohorts'}
    top = summary['symbols']
    assert [row['rank'] for row in top] == list(range(1, 11))
    # |realized|: KRW when known, else the native diagnostic at the current rate; S13 gains most.
    assert [row['symbol'] for row in top] == [f'S{index:02d}' for index in range(13, 3, -1)]
    assert summary['omitted_symbols'] == 4 and summary['symbol_count'] == 14
    by_symbol = {row['symbol']: row for row in full['symbols']}
    for row in top:
        source = by_symbol[row['symbol']]
        assert row['win_rate_all'] == source['win_rate_all'] and row['reason_codes'] == source['reason_codes']
        assert row['diagnostic_pnl_by_currency'] == source['realized']['diagnostic_pnl_by_currency']
    # Every symbol with a reason is listed, by its codes only (top ones included).
    groups = summary['symbol_reason_groups']
    listed = {tuple(pair) for group in groups for pair in group['symbols']}
    expected = {(row['symbol'], row['currency']) for row in full['symbols']
                if row['reason_codes'] or row['realized_pnl_krw'] is None}
    assert listed == expected and sum(g['count'] for g in groups) == len(expected)
    assert all(set(group) == {'reason_codes', 'count', 'symbols'} for group in groups)
    assert len(json.dumps(summary, default=str)) < len(json.dumps(full, default=str)) / 2
