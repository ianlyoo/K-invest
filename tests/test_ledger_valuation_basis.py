"""Endpoints on different valuation basis labels are flagged, never blocked.

The label (daily_nav's consolidated_nav_krw.basis) names what the valuation actually used: legacy rows,
broker marks (summarize_portfolio without quotes, the 18:10 snapshot path until 2.15.1) or canonical marks
(with timed quotes). Readers are the real get_portfolio_performance (replay and live end) and
get_portfolio_history over test_ledger_returns' wired synthetic store; the live end and the quotes use the
P2-e harness (the real TossClient.get_mark_quotes over a mocked prices response). Synthetic numbers only.
"""
import json

import pytest
from test_ledger_live_nav_marks import (  # noqa: F401 - pytest fixture
    CANONICAL,
    FALLBACK,
    live,
)
from test_ledger_returns import (  # noqa: F401 - pytest fixture
    END,
    START,
    coverage,
    flow,
    performance,
    wired,
)
from test_pos_marks_risk import _ROWS, _holdings, _PriceClient

import daily_nav
import portfolio_engine
import portfolio_store
from portfolio_model import FxRates

NEW = 'broker_accounts_at_canonical_marks_plus_verified_futures_equity_before_settlement_adjustments'
BROKER = 'broker_accounts_at_broker_marks_plus_verified_futures_equity_before_settlement_adjustments'
LEGACY = 'legacy_broker_assets_plus_verified_futures_equity_before_settlement_adjustments'
NOTE = ('revaluation between the endpoints: legacy = broker values + yfinance FX; broker_marks = one broker '
        'price per symbol + Toss FX; canonical_marks = timed quotes + Toss FX')
STEPS = [(LEGACY, BROKER), (BROKER, NEW), (LEGACY, NEW)]


def label(w, *labels):
    for nav, basis in zip(w.navs, labels):
        nav['consolidated_nav_krw']['basis'] = basis


def flagged(data, before, after, start=START):
    assert data['warning_codes'] == ['VALUATION_BASIS_CHANGED']
    assert data['valuation_basis_change']['intervals'] == [
        {'start_date': start, 'end_date': END, 'from_basis': before, 'to_basis': after}]
    assert data['valuation_basis_change']['note'].startswith(NOTE)


def not_flagged(data):
    assert data['warning_codes'] == [] and data['valuation_basis_change'] is None


def history(w, **kwargs):
    result = w.server.get_portfolio_history(input_snapshot_ids=['synthetic'], **kwargs)
    assert result['ok'], result.get('error')
    return result['data']


def test_the_label_names_the_marks_the_valuation_used():
    fx = FxRates(rates={'USD': 1000.0})
    requested = {h.symbol: h.currency for h in _holdings()}
    quoted = portfolio_engine.summarize_portfolio(_holdings(), [], fx,
                                                  quotes=_PriceClient(_ROWS).get_mark_quotes(requested))
    broker = portfolio_engine.summarize_portfolio(_holdings(), [], fx)
    rejected = portfolio_engine.summarize_portfolio(
        _holdings(), [], fx, quotes={s: {'reason_codes': ['QUOTE_SOURCE_UNAVAILABLE']} for s in requested})
    assert (quoted['mark_basis'], broker['mark_basis'], rejected['mark_basis']) == (
        'canonical_marks', 'broker_marks', 'broker_marks')
    assert [daily_nav.crypto_nav(None, s)['consolidated_nav_krw']['basis'] for s in (quoted, broker, rejected)] == [
        NEW, BROKER, BROKER]
    # A version stored before daily NAV evidence is re-read through the store's payload reader.
    raw = json.dumps({'snapshot_date': START, 'total_value_krw': 1.0, 'snapshot_id': 'legacy'})
    assert portfolio_store._version_payload(raw)['consolidated_nav_krw']['basis'] == LEGACY


@pytest.mark.parametrize('scope', ['equity', 'consolidated'])
@pytest.mark.parametrize('before,after', STEPS)
def test_replay_across_a_label_change_keeps_returns_and_flags(wired, scope, before, after):  # noqa: F811
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    flow(wired, amount='100' if scope == 'consolidated' else '50')
    label(wired, before, after)
    data = performance(wired, portfolio_scope=scope, detail_level='full')
    assert data['returns']['twr_pct'] == 10 and data['metrics']['twr_pct']['reason_codes'] == []
    flagged(data, before, after)
    [row] = data['pnl_series']['daily']
    assert row['warning_codes'] == ['VALUATION_BASIS_CHANGED'] and row['pnl_krw'] is not None
    assert 'VALUATION_BASIS_CHANGED' not in row['metrics']['pnl_krw']['reason_codes']


@pytest.mark.parametrize('labels', [(NEW, NEW), (BROKER, BROKER), (LEGACY, LEGACY), (LEGACY, None), ()])
def test_same_or_missing_labels_are_not_flagged(wired, labels):  # noqa: F811
    coverage(wired)
    flow(wired)
    for nav in wired.navs:
        nav['consolidated_nav_krw'].pop('basis', None)
    label(wired, *labels)
    data = performance(wired, detail_level='full')
    assert data['returns']['twr_pct'] == 10
    not_flagged(data)
    assert data['pnl_series']['daily'][0]['warning_codes'] == []


@pytest.mark.parametrize('before,after', STEPS)
def test_history_flags_the_window_and_the_series_row(wired, before, after):  # noqa: F811
    coverage(wired)
    flow(wired)
    label(wired, before, after)
    data = history(wired, detail_level='full')
    flagged(data, before, after)
    [row] = data['pnl_series']['daily']
    assert row['warning_codes'] == ['VALUATION_BASIS_CHANGED'] and row['pnl_krw'] == 10
    assert data['statistics']['max_drawdown_pct'] is not None and data['statistics']['raw_asset_change_pct'] == 60
    assert data['statistics']['metrics']['max_drawdown_pct']['reason_codes'] == []
    label(wired, after, after)
    not_flagged(history(wired))


@pytest.mark.parametrize('stored', [BROKER, LEGACY])
def test_a_live_canonical_end_vs_an_older_stored_basis_is_flagged(live, stored):  # noqa: F811 - pytest fixture
    label(live, stored, stored)
    data = live.server.get_portfolio_performance(detail_level='full')['data']
    assert data['end_nav_basis'] == 'live_holdings' and data['end_nav_krw'] == pytest.approx(CANONICAL)
    flagged(data, stored, NEW, start=END)


def test_a_live_end_on_the_stored_basis_is_not_flagged(live, monkeypatch):  # noqa: F811 - pytest fixture
    label(live, NEW, NEW)
    not_flagged(live.server.get_portfolio_performance()['data'])

    def unavailable(*a, **k):
        raise RuntimeError('toss down')

    # No quote source: the live end is at broker marks too, like the stored 18:10 rows.
    monkeypatch.setattr(live.server, '_get_toss', unavailable)
    label(live, BROKER, BROKER)
    data = live.server.get_portfolio_performance()['data']
    assert data['end_nav_krw'] == pytest.approx(FALLBACK)
    not_flagged(data)
    label(live, NEW, NEW)
    flagged(live.server.get_portfolio_performance()['data'], NEW, BROKER, start=END)


def test_scopes_flag_only_what_changes_their_figure():
    def rows(before, after):
        return [{'snapshot_date': day, 'total_value_krw': 1.0, 'consolidated_nav_krw': {'basis': basis}}
                for day, basis in ((START, before), (END, after))]

    for before, after in STEPS:
        for scope in ('kis', 'crypto'):  # valued by their own captures
            assert portfolio_engine.valuation_basis_change(rows(before, after), scope) == {
                'warning_codes': [], 'valuation_basis_change': None}
    # The toss scopes value the accounts' own Toss-quoted NAV at fx_times: only the legacy FX step applies.
    for scope in ('toss', 'toss_investmcp'):
        for before, after in ((LEGACY, BROKER), (LEGACY, NEW)):
            assert portfolio_engine.valuation_basis_change(rows(before, after), scope)['warning_codes'] == [
                'VALUATION_BASIS_CHANGED']
        assert portfolio_engine.valuation_basis_change(rows(BROKER, NEW), scope)['warning_codes'] == []
