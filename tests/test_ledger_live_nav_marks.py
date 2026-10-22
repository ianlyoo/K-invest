"""Performance's live end NAV is get_portfolio_summary's total (canonical marks, fx.policy FX).

Both tools run on one synthetic collector, with the real TossClient.get_mark_quotes over a mocked prices
response (the test_pos_marks_risk harness) and test_ledger_returns' wired synthetic NAV store.
The expected total is recomputed here from the quote rows. Synthetic numbers only.
"""
import pytest
from test_ledger_returns import coverage, wired  # noqa: F401 - pytest fixture
from test_pos_marks_risk import _ROWS, _holdings, _PriceClient

from portfolio_model import FxRates
from portfolio_sources import Collection

USD = 1000.0
# 280 x 000001 at its Toss quote 1010, 40 x 000002 at 505, 15.5 x SYNA at 20.2 USD.
CANONICAL = 1010.0 * 280 + 505.0 * 40 + 20.2 * 15.5 * USD
# No quote source: each symbol at the broker price of the account holding the most (unknown_time).
FALLBACK = 1000.0 * 280 + 500.0 * 40 + 20.0 * 15.5 * USD


class Collector:
    def collect_positions(self, *args):
        health = {h.account: {'account': h.account, 'status': 'ok', 'last_error': None} for h in _holdings()}
        return Collection(holdings=_holdings(), health=health, fetched_at='2026-01-01T09:10:05+00:00')

    def collect_transactions(self, *args):
        return Collection(health={'synthetic': {'status': 'ok'}})

    def build_fx(self, currencies):
        return FxRates(rates={'USD': USD})

    def health_report(self):
        return {}


@pytest.fixture
def live(wired, monkeypatch):  # noqa: F811
    monkeypatch.setattr(wired.server, '_get_collector', lambda: Collector())
    monkeypatch.setattr(wired.server, '_get_toss', lambda *a, **k: _PriceClient(_ROWS))
    return wired


def test_live_end_nav_equals_the_summary_total_on_canonical_marks(live):
    performance = live.server.get_portfolio_performance(detail_level='full')['data']
    summary = live.server.get_portfolio_summary(include_realized=False, snapshot=False)['data']
    assert CANONICAL != pytest.approx(FALLBACK)
    assert performance['end_nav_krw'] == summary['total_assets_krw'] == pytest.approx(CANONICAL)
    # Unrealized P&L comes from the same summary; here the empty synthetic ledger breaks reconciliation,
    # so performance withholds it (existing rule) while the summary reports the holdings' figure.
    assert summary['unrealized_pnl_krw'] is not None
    assert performance['metrics']['unrealized_krw']['reason_codes'] == ['RECONCILIATION_BREAK']
    # Earlier labels kept; the valuation label names the marks and FX the figure used.
    assert performance['end_nav_basis'] == 'live_holdings'
    assert performance['end_nav_as_of'] == '2026-01-01T09:10:05+00:00'
    assert performance['end_nav_valuation'] == {
        'value_field': 'get_portfolio_summary.total_assets_krw', 'mark_policy': 'latest_timed_quote_v1',
        'mark_status_counts': summary['mark_status_counts'], 'fx_policy': 'toss_rate_v1'}


def test_without_a_quote_source_both_fall_back_to_the_same_labelled_marks(live, monkeypatch):
    def unavailable(*a, **k):
        raise RuntimeError('toss down')

    monkeypatch.setattr(live.server, '_get_toss', unavailable)
    performance = live.server.get_portfolio_performance(detail_level='full')['data']
    summary = live.server.get_portfolio_summary(include_realized=False, snapshot=False)['data']
    assert performance['end_nav_krw'] == summary['total_assets_krw'] == pytest.approx(FALLBACK)
    assert performance['end_nav_valuation']['mark_status_counts'] == summary['mark_status_counts']
    assert set(summary['mark_status_counts']) == {'unknown_time'}


def test_a_stored_end_nav_has_no_live_valuation_label(live):
    coverage(live)  # ledger flow evidence: the end NAV is the stored boundary snapshot
    performance = live.server.get_portfolio_performance(detail_level='full')['data']
    assert performance['end_nav_basis'] == 'stored_snapshot' and performance['end_nav_valuation'] is None
