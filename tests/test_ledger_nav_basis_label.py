"""consolidated_nav_krw.basis names the marks the total used; stored legacy rows still resolve.

The label comes from the real snapshot path (NavStore.upsert_snapshot -> daily_nav.crypto_nav) on
test_daily_nav's synthetic risk capture (a summary built without quotes, as the 18:10 snapshot path does
until 2.15.1, so broker marks); the reader is the real consolidated get_portfolio_performance
over test_ledger_returns' wired synthetic store. Synthetic numbers only.
"""
import pytest
from test_daily_nav import fixture_summary, risk_capture
from test_ledger_returns import (  # noqa: F401 - pytest fixture
    blocked,
    coverage,
    flow,
    performance,
    wired,
)

import daily_nav
from daily_nav import (
    BROKER_MARKS_CONSOLIDATED_NAV_BASIS,
    CONSOLIDATED_NAV_BASIS,
    LEGACY_CONSOLIDATED_NAV_BASIS,
)
from portfolio_store import NavStore

NEW = 'broker_accounts_at_canonical_marks_plus_verified_futures_equity_before_settlement_adjustments'
BROKER = 'broker_accounts_at_broker_marks_plus_verified_futures_equity_before_settlement_adjustments'
LEGACY = 'legacy_broker_assets_plus_verified_futures_equity_before_settlement_adjustments'


def test_snapshot_rows_without_quotes_carry_the_broker_marks_label(tmp_path):
    assert (CONSOLIDATED_NAV_BASIS, BROKER_MARKS_CONSOLIDATED_NAV_BASIS, LEGACY_CONSOLIDATED_NAV_BASIS) == (
        NEW, BROKER, LEGACY)
    path = tmp_path / 'nav.db'
    risk_capture(path)
    _, summary = fixture_summary()
    summary['fx_times'].update({c: {'rate_to_krw': 1300, 'priced_at': '2026-10-10T09:10:00Z',
                                    'observed_at': '2026-10-10T09:10:00Z'} for c in ('USDT', 'USDC')})
    row = NavStore(path).upsert_snapshot(summary, '2026-10-10')
    assert row['consolidated_nav_krw']['value'] is not None
    assert summary['mark_basis'] == 'broker_marks' and row['consolidated_nav_krw']['basis'] == BROKER


def test_unavailable_crypto_evidence_carries_the_same_label(tmp_path, monkeypatch):
    def broken(conn, summary):
        raise RuntimeError('synthetic')

    monkeypatch.setattr(daily_nav, '_crypto_nav', broken)
    _, summary = fixture_summary()
    row = NavStore(tmp_path / 'nav.db').upsert_snapshot(summary, '2026-10-10')
    assert row['consolidated_nav_krw']['reason_codes'] == ['CRYPTO_NAV_EVIDENCE_UNAVAILABLE']
    assert row['consolidated_nav_krw']['basis'] == BROKER


@pytest.mark.parametrize('labels', [(NEW, NEW), (BROKER, BROKER), (LEGACY, LEGACY), (LEGACY, BROKER), (BROKER, NEW)])
def test_consolidated_returns_read_new_and_stored_legacy_rows(wired, labels):  # noqa: F811
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    flow(wired, amount='100')
    for nav, label in zip(wired.navs, labels):
        nav['consolidated_nav_krw']['basis'] = label
    data = performance(wired, portfolio_scope='consolidated')
    assert data['returns']['twr_pct'] == 10


def test_an_unknown_basis_still_fails_closed(wired):  # noqa: F811
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    flow(wired, amount='100')
    wired.navs[-1]['consolidated_nav_krw']['basis'] = 'broker_assets_unlabelled'
    blocked(performance(wired, portfolio_scope='consolidated'), 'FLOW_NAV_BOUNDARY_UNKNOWN')
