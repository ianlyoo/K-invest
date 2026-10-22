"""Every boundary NAV states its basis (live holdings vs stored snapshot) and as-of.

compute_performance runs on synthetic stored rows; the tool path uses test_ledger_returns' wired
synthetic store and collector (no broker, no credentials). Synthetic amounts only.
"""
from test_ledger_returns import END, coverage, wired  # noqa: F401 - pytest fixture

import portfolio_engine as engine
from portfolio_model import FxRates

ROWS = [{'snapshot_date': '2026-10-20', 'total_value_krw': 100.0, 'snapshot_id': 'snap-20',
         'valuation_at': '2026-10-20T09:10:00+00:00'},
        {'snapshot_date': '2026-10-21', 'total_value_krw': 110.0, 'snapshot_id': 'snap-21',
         'valuation_at': '2026-10-21T09:10:00+00:00'}]


def perform(current=None, as_of=None, **quality):
    return engine.compute_performance([], ROWS, current, FxRates(), '2026-10-20', '2026-10-22',
                                      current_nav_as_of=as_of, input_quality=quality or None)


def test_stored_end_nav_names_its_snapshot_and_valuation_time():
    data = perform()
    assert (data['start_nav_basis'], data['start_nav_as_of']) == ('stored_snapshot', '2026-10-20T09:10:00+00:00')
    assert (data['start_nav_snapshot_date'], data['start_nav_snapshot_id']) == ('2026-10-20', 'snap-20')
    assert data['end_nav_krw'] == 110.0
    assert (data['end_nav_basis'], data['end_nav_as_of']) == ('stored_snapshot', '2026-10-21T09:10:00+00:00')
    assert (data['end_nav_snapshot_date'], data['end_nav_snapshot_id']) == ('2026-10-21', 'snap-21')


def test_live_end_nav_is_labelled_live_with_the_holdings_read_time():
    data = perform(125.0, '2026-10-22T01:02:03+00:00')
    assert data['end_nav_krw'] == 125.0
    assert (data['end_nav_basis'], data['end_nav_as_of']) == ('live_holdings', '2026-10-22T01:02:03+00:00')
    assert data['end_nav_snapshot_id'] is None and data['end_nav_snapshot_date'] is None
    assert data['start_nav_basis'] == 'stored_snapshot'


def test_null_nav_has_null_basis():
    data = perform(125.0, '2026-10-22T01:02:03+00:00', missing_position_valuation=['AAA'])
    assert data['end_nav_krw'] is None and data['end_nav_basis'] is None and data['end_nav_as_of'] is None
    empty = engine.compute_performance([], [], None, FxRates(), '2026-10-20', '2026-10-22')
    assert empty['start_nav_basis'] is None and empty['end_nav_basis'] is None


def test_tool_reports_which_basis_the_end_nav_used(wired):  # noqa: F811
    for row in wired.navs:
        row['valuation_at'] = row['snapshot_date'] + 'T09:10:00+00:00'
    # No ledger flow evidence: the end NAV is the live holdings total, labelled with the positions read time.
    live = wired.server.get_portfolio_performance()
    assert live['data']['end_nav_basis'] == 'live_holdings' and live['data']['end_nav_snapshot_id'] is None
    assert live['data']['end_nav_as_of'] == live['meta']['fetched_at'] is not None
    # With ledger flow evidence (the live default today) it is the stored boundary snapshot instead.
    coverage(wired)
    stored = wired.server.get_portfolio_performance()['data']
    assert 'ledger_flow_coverage' in stored
    assert (stored['end_nav_basis'], stored['end_nav_snapshot_id']) == ('stored_snapshot', 'snap-' + END)
    assert stored['end_nav_as_of'] == END + 'T09:10:00+00:00'
    history = wired.server.get_portfolio_history(input_snapshot_ids=['synthetic'])['data']
    assert history['latest_nav_basis'] == {'basis': 'stored_snapshot', 'as_of': END + 'T09:10:00+00:00',
                                           'snapshot_date': END, 'snapshot_id': 'snap-' + END,
                                           'value_field': "total_value_krw (the stored row's equity total)"}
