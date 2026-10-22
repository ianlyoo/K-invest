"""Ledgerless equity (Toss + KIS) estimate in the default scope, and effective_period clipping.

Stored rows carry Toss ``account_nav`` and a ``kis_capture`` (test_consolidated_returns.build); KIS
flows come from the real kis_flow_inference provider over the same SQLite file, Toss flows are inference-shaped
conditional zero records. Synthetic amounts only; no network, no broker calls.
"""
from __future__ import annotations

from decimal import Decimal

from test_consolidated_returns import DAYS, build, kis, providers
from test_kis_flow_inference import account as kis_account

import interval_returns as ir

# Equity levels by hand: Toss 1.00m -> 1.01m -> 1.03m -> 1.04m, KIS 1.84m -> 1.8514m -> 1.8514m -> 1.8514m.
EQUITY = [Decimal(2_840_000), Decimal(2_861_400), Decimal(2_881_400), Decimal(2_891_400)]


def pct(value):
    return round(float(value * 100), 4)


def equity(store, rows, start, end, detail_level='full', **kwargs):
    navs = [row for row in rows if start <= row['snapshot_date'] <= end]
    return ir.ledgerless_returns(store.db_path, navs, 'equity', start, end, providers=providers(rows),
                                 detail_level=detail_level, **kwargs)


def four_days(tmp_path, *, partial_kis_day=None):
    days = []
    for i, (toss, securities) in enumerate(((1_000_000, '700000'), (1_010_000, '711400'),
                                            (1_030_000, '711400'), (1_040_000, '711400'))):
        entry = kis(DAYS[i], securities)
        if i == partial_kis_day:  # the stored 10/19 row: KIS valued the US session still in progress
            entry = kis_account(DAYS[i], krw='1000000', usd='100', securities=securities, domestic={'005930': '10'},
                                status='partial', reasons=['KIS_SESSION_DATE_IN_FUTURE'])
        days.append((toss, entry, None))
    return build(tmp_path, days)


def test_equity_sums_toss_and_kis_levels_and_excludes_crypto(tmp_path):
    store, rows = four_days(tmp_path)
    block = equity(store, rows, DAYS[0], DAYS[3])
    assert block['scope'] == 'equity' and block['included_scopes'] == ['toss', 'kis']
    assert block['excluded_scopes'] == [] and block['missing_scopes'] == []
    first = block['intervals'][0]
    assert (first['start_nav_krw'], first['end_nav_krw'], first['flows_krw']) == (2_840_000, 2_861_400, 0)
    assert first['return_pct'] == pct((EQUITY[1] - EQUITY[0]) / EQUITY[0])
    assert {part['scope'] for part in first['components']} == {'toss', 'kis'}
    assert block['period']['return_pct'] == pct(EQUITY[3] / EQUITY[0] - 1)
    # The weakest part labels it: KIS is forward pending, so never verified.
    assert block['label'] == 'estimate' and block['accuracy']['status'] == 'composite'
    assert set(block['accuracy']['components']) == {'toss', 'kis'}
    assert 'Equity (Toss + KIS) adds up' in block['explanation']
    assert block['effective_period']['clipped'] is False
    assert block['effective_period']['return_pct'] == block['period']['return_pct']


def test_period_before_the_first_snapshot_clips_to_the_stored_range(tmp_path):
    store, rows = four_days(tmp_path)
    block = equity(store, rows, '2026-09-21', '2026-10-21')
    assert block['period']['return_pct'] is None and 'MISSING_START_NAV' in block['period']['reason_codes']
    effective = block['effective_period']
    assert (effective['start'], effective['end'], effective['clipped']) == (DAYS[0], DAYS[3], True)
    assert effective['clip_reason_codes'] == ['EFFECTIVE_START_AFTER_PERIOD_START', 'EFFECTIVE_END_BEFORE_PERIOD_END']
    assert effective['return_pct'] == pct(EQUITY[3] / EQUITY[0] - 1) and effective['label'] == 'estimate'
    assert effective['intervals'] == 3 and effective['reason_codes'] == []
    assert block['effective_period_reason_codes'] == []
    assert f'Over the stored range {DAYS[0]} to {DAYS[3]} (effective_period' in block['explanation']


def test_a_partial_kis_day_nulls_equity_there_and_the_tracked_estimate_remains(tmp_path):
    store, rows = four_days(tmp_path, partial_kis_day=1)
    block = equity(store, rows, DAYS[0], DAYS[3])
    first, second, third = block['intervals']
    for row in (first, second):  # Toss has these dates, KIS does not: never a Toss-only figure
        assert row['return_pct'] is None and 'CONSOLIDATED_SCOPE_MISSING' in row['reason_codes']
        assert [entry['scope'] for entry in row['missing_scopes']] == ['kis']
    assert third['return_pct'] == pct((EQUITY[3] - EQUITY[2]) / EQUITY[2])
    assert block['period']['return_pct'] is None and block['effective_period']['return_pct'] is None
    assert 'UNRESOLVED_INTERVAL_IN_PERIOD' in block['effective_period']['reason_codes']
    # The default call still returns a labelled, dated estimate over the latest gap-free run.
    tracked = block['tracked']
    assert (tracked['start_date'], tracked['end_date'], tracked['label']) == (DAYS[2], DAYS[3], 'estimate')
    assert tracked['return_pct'] == third['return_pct']


def test_effective_period_needs_two_complete_snapshots(tmp_path):
    store, rows = four_days(tmp_path)
    block = equity(store, rows[:1], DAYS[0], DAYS[3])
    assert block['effective_period'] is None
    assert block['effective_period_reason_codes'] == ['INSUFFICIENT_NAV_SNAPSHOTS']


def test_single_scopes_and_consolidated_also_carry_effective_period(tmp_path):
    store, rows = four_days(tmp_path)
    navs = list(rows)
    kis_block = ir.ledgerless_returns(store.db_path, navs, 'kis', '2026-10-01', DAYS[3], providers=providers(rows))
    assert kis_block['period']['return_pct'] is None
    # KIS_CAPTURE_MISSING never applies to a range that starts at a captured point.
    assert kis_block['effective_period']['start'] == DAYS[0] and kis_block['effective_period']['return_pct'] is not None
    consolidated = ir.ledgerless_returns(store.db_path, navs, 'consolidated', DAYS[0], DAYS[3], providers=providers(rows))
    assert 'effective_period' in consolidated and consolidated['scope'] == 'consolidated'
    assert ir.unavailable('equity', DAYS[0], DAYS[3], ['X'])['effective_period'] is None


def test_server_passes_the_scope_configuration_for_equity(monkeypatch):
    import server
    seen = {}

    def capture(db_path, navs, scope, start, end, **kwargs):
        seen[scope] = kwargs
        return {}
    monkeypatch.setattr(ir, 'ledgerless_returns', capture)
    monkeypatch.setattr(server, '_scope_configuration', lambda: {'toss': True, 'kis': True, 'crypto': False})
    for scope in ('equity', 'consolidated', 'toss'):
        server._ledgerless_block(object(), [], DAYS[0], DAYS[3], scope, None, 'summary')
    assert seen['equity']['configured'] == {'toss': True, 'kis': True, 'crypto': False}
    assert seen['consolidated']['configured'] == seen['equity']['configured']
    assert 'configured' not in seen['toss']
