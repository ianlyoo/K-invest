"""Consolidated ledgerless returns (2.13.1): Toss + KIS + crypto levels in one store.

Each stored row carries all three scopes, as the 18:10 scheduled snapshot does: Toss
``account_nav``, the ``kis_capture`` and ``crypto_nav`` with its Upbit marks. KIS and crypto
flows come from their real providers (kis_flow_inference, crypto_flow_intervals) over the
same SQLite file; Toss flows are inference-shaped records (Toss inference needs its own evidence).
No network and no broker calls.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from test_crypto_flow_intervals import T0, bn_transfer, coverage, identities
from test_kis_flow_inference import account as kis_account
from test_kis_flow_inference import nav as kis_nav
from test_kis_flow_inference import stamp as kis_stamp

import interval_returns as ir
import kis_flow_inference as kf
from crypto_flow_intervals import EVIDENCE_PAD, crypto_interval_flows
from ledger_returns import scope_navs
from portfolio_ledger import LedgerStore
from portfolio_store import NavStore

DAYS = [(T0 + timedelta(days=i)).date().isoformat() for i in range(4)]  # 10/13..10/16, 18:10 KST captures
WEIGHT = Decimal(1439) / Decimal(1440)  # 50 USDT enters UM futures one minute after the 10/13 capture


def metric(value):
    return {'value': value, 'status': 'derived', 'reason_codes': []}


def crypto_nav(store, usdt, usdc, ident, *, status='derived', at=None):
    """``at``: the risk capture's own completion (and every venue read), else the row's valuation_at."""
    accounts = identities(store)
    timing = {'observed_at': at, 'nav_price_time': at} if at else {}

    def venue(name, currency, amount, scope, abstraction):
        return {'status': status, 'value_krw': amount * 1400 if status == 'derived' else None,
                'reason_codes': [] if status == 'derived' else ['MISSING_CRYPTO_FX_RATE'],
                'by_currency': {currency: {'value': amount, 'decimal_value': str(amount)}},
                'accounts': [{'account_id': 'risk_0', 'ledger_account_identity': accounts[name], 'asset': currency,
                              'scope': scope, 'dex': '', 'abstraction': abstraction, **timing}]}
    return {'risk_snapshot_id': 'risk-' + ident, **({'observed_at': at} if at else {}),
            'binance': venue('binance', 'USDT', usdt, 'futures_collateral', None),
            'hyperliquid': venue('hyperliquid', 'USDC', usdc, 'unified_shared_wallet', 'unifiedAccount')}


def build(tmp_path, days, *, crypto_status='derived', crypto_lead=None):
    """Stored representatives: ``days`` = [(toss KRW, kis account entry, (USDT, USDC) or None)] from 10/13.

    ``crypto_lead``: crypto is captured (and its Upbit marks read) that long before the 18:10 Toss valuation.
    """
    store = LedgerStore(tmp_path / 'db')
    rows = []
    for i, (toss, kis, crypto) in enumerate(days):
        when = T0 + timedelta(days=i)
        ident = f'day{i}'
        row = {'snapshot_id': ident, 'snapshot_date': DAYS[i], 'version': 1, 'valuation_at': when.isoformat(),
               'total_value_krw': toss, 'result_status': 'complete', 'reason_codes': [],
               'account_nav': {'toss:primary': {'source_status': 'complete', 'cash': {'KRW': metric(toss / 10)},
                                                'nav_native': {'KRW': metric(toss)}}},
               'kis_capture': kis_nav(DAYS[i], kis)['kis_capture'], 'fx_times': {}}
        if crypto is not None:
            at = (when - crypto_lead).isoformat() if crypto_lead else None
            row['crypto_nav'] = crypto_nav(store, *crypto, ident, status=crypto_status, at=at)
            row['fx_times'] = {c: {'rate_to_krw': '1400', 'priced_at': at or when.isoformat(),
                                   'observed_at': at or when.isoformat(),
                                   'source': 'upbit_public_ticker', 'reason_codes': []} for c in ('USDT', 'USDC')}
        with NavStore(store.db_path)._connect() as conn:
            NavStore._insert_version(conn, row)
        rows.append(row)
    lead = crypto_lead or timedelta(0)
    coverage(store, start=T0 - lead - EVIDENCE_PAD, end=T0 + timedelta(days=len(days) - 1) + EVIDENCE_PAD)
    return store, rows


def toss_records(rows):
    """The inference's shape for quiet Toss intervals: a conditional zero with the dividend advisory."""
    def provider(db_path, start, end, accounts=None):
        return [{'source': 'toss', 'account': 'toss:primary', 'currency': 'KRW', 't0': a['valuation_at'],
                 't1': b['valuation_at'], 'start_snapshot_id': a['snapshot_id'], 'end_snapshot_id': b['snapshot_id'],
                 'flow_native': '0', 'income_native': None, 'events': [], 'timing': 'unknown',
                 'method': 'cash_residual', 'confidence': 'conditional', 'reasons': ['DIVIDENDS_COUNTED_AS_FLOWS'],
                 'inputs': {'snapshot_versions': [1, 1]}}
                for a, b in zip(rows, rows[1:]) if start <= a['snapshot_date'] and b['snapshot_date'] <= end]
    return provider


def providers(rows):
    return {'toss': toss_records(rows), 'kis': kf.kis_interval_flows, 'crypto': crypto_interval_flows}


def kis(day, securities, held='10'):
    return kis_account(day, krw='1000000', usd='100', securities=securities, domestic={'005930': held})


@pytest.fixture
def three(tmp_path):
    # Toss 1.00m -> 1.01m -> 1.03m -> 1.04m; KIS 1.84m -> 1.8514m (gain) -> flat -> 005930 10 -> 12 shares
    # with no fill (unresolved); crypto 280k -> 350k (+50 USDT transfer in) -> 352.8k (USDC gain) -> flat.
    store, rows = build(tmp_path, [(1_000_000, kis(DAYS[0], '700000'), (100, 100)),
                                   (1_010_000, kis(DAYS[1], '711400'), (150, 100)),
                                   (1_030_000, kis(DAYS[2], '711400'), (150, 102)),
                                   (1_040_000, kis(DAYS[3], '853680', held='12'), (150, 102))])
    bn_transfer(store, stamp=T0 + timedelta(minutes=1))
    return store, rows


def consolidated(store, rows, start=DAYS[0], end=DAYS[3], detail_level='full', **kwargs):
    navs = [row for row in rows if start <= row['snapshot_date'] <= end]
    return ir.ledgerless_returns(store.db_path, navs, 'consolidated', start, end, providers=providers(rows),
                                 detail_level=detail_level, **kwargs)


def pct(value):
    return round(float(value * 100), 4)


# Hand computation on the levels: R = (sum N1 - sum N0 - sum F) / (sum N0 + sum w F).
N0, N1, N2 = Decimal(3_120_000), Decimal(3_211_400), Decimal(3_234_200)
R1 = (N1 - N0 - 70_000) / (N0 + WEIGHT * 70_000)  # crypto flow, weighted inside the crypto interval
R2 = (N2 - N1) / N1


def test_levels_are_summed_and_a_null_kis_interval_names_kis_and_its_reason(three):
    store, rows = three
    before = store.db_path.read_bytes()
    block = consolidated(store, rows)
    assert store.db_path.read_bytes() == before  # read-only
    assert block['scope'] == 'consolidated' and block['method'] == 'modified_dietz_linked_twr_estimate'
    assert block['included_scopes'] == ['toss', 'kis', 'crypto'] and block['excluded_scopes'] == []
    first, second, third = block['intervals']
    # All three resolve and crypto has a flow: 21,400 / (3,120,000 + 1439/1440 x 70,000).
    assert (first['start_nav_krw'], first['end_nav_krw'], first['flows_krw']) == (3_120_000, 3_211_400, 70_000)
    assert first['return_pct'] == pct(R1) == 0.6709 and first['label'] == 'estimate'
    assert first['sensitivity_bounds_pct'] == {'low': pct(R1), 'high': pct(R1)}
    assert first['missing_scopes'] == [] and first['confidence'] == 'conditional'
    parts = {part['scope']: part for part in first['components']}
    assert parts['crypto']['flows_krw'] == 70_000 and parts['crypto']['timing'] == 'known'
    assert {scope: part['label'] for scope, part in parts.items()} == {
        'toss': 'estimate', 'kis': 'estimate', 'crypto': 'estimate'}
    # Never an average of the scope percentages: (1% + 0.6196% + 0%) / 3 = 0.5399%, not 0.6709%.
    assert [parts[s]['return_pct'] for s in ('toss', 'kis', 'crypto')] == [1.0, 0.6196, 0.0]
    assert round(sum(part['return_pct'] for part in parts.values()) / 3, 4) == 0.5399
    # Each part is exactly what its own scope reports for that interval.
    for scope in ('toss', 'kis', 'crypto'):
        own = ir.ledgerless_returns(store.db_path, rows, scope, DAYS[0], DAYS[3], providers=providers(rows),
                                    detail_level='full')
        assert own['intervals'][0]['return_pct'] == parts[scope]['return_pct'], scope
    # All resolve without a flow: 22,800 / 3,211,400.
    assert second['return_pct'] == pct(R2) == 0.71 and second['flows_krw'] == 0
    # KIS saw 005930 go from 10 to 12 shares with no fill: null, naming kis and its own reason.
    assert third['return_pct'] is None and third['label'] == 'unavailable' and third['start_nav_krw'] is None
    assert [entry['scope'] for entry in third['missing_scopes']] == ['kis']
    assert 'KIS_DOMESTIC_QUANTITY_UNRECONCILED' in third['missing_scopes'][0]['reason_codes']
    assert third['reason_codes'][0] == 'CONSOLIDATED_SCOPE_MISSING'
    assert [part['scope'] for part in third['components']] == ['toss', 'crypto']
    # The period holds the null interval: no return over a subset of days; tracked is dated separately.
    assert block['label'] == 'unavailable' and block['period']['return_pct'] is None
    assert 'CONSOLIDATED_SCOPE_MISSING' in block['reason_codes']
    assert block['coverage']['days_resolved'] == 2 and block['coverage']['days_total'] == 3
    assert block['tracked']['return_pct'] == pct((1 + R1) * (1 + R2) - 1)
    assert (block['tracked']['start_date'], block['tracked']['end_date']) == (DAYS[0], DAYS[2])
    assert block['missing_scopes'] == [{'scope': 'kis', 'intervals_missing': 1,
                                        'reason_codes': third['missing_scopes'][0]['reason_codes']}]
    assert 'Missing: kis in 1 interval (' in block['explanation']
    assert 'scope percentages are never averaged' in block['explanation']
    accuracy = block['accuracy']
    assert accuracy['status'] == 'composite' and accuracy['passed_bar'] is False
    assert {scope: c['status'] for scope, c in accuracy['components'].items()} == {
        'toss': 'unmeasured', 'kis': 'forward_pending', 'crypto': 'unmeasured'}
    assert 'KIS_FORWARD_ACCURACY_PENDING' in accuracy['components']['kis']['reason_codes']
    assert not any(key.endswith('_bp') for key in accuracy)  # no invented error figures
    assert 'composite of toss unmeasured, kis forward pending, crypto unmeasured' in block['explanation']


def test_a_period_where_every_scope_resolves_links_the_consolidated_intervals(three):
    store, rows = three
    block = consolidated(store, rows, end=DAYS[2])
    assert block['label'] == 'estimate' and block['reason_codes'] == []
    assert block['period']['return_pct'] == pct((1 + R1) * (1 + R2) - 1) == 1.3856
    assert [row['return_pct'] for row in block['intervals']] == [pct(R1), pct(R2)]
    assert block['explanation'].startswith('ESTIMATE, not verified: linked Modified Dietz return for consolidated')
    # Toss and KIS flows are inferred (conditional): XIRR needs dated, observed flows everywhere.
    assert block['period']['xirr_reason_codes'] == ['XIRR_REQUIRES_DATED_COMPLETE_FLOWS']
    assert 'DIVIDENDS_COUNTED_AS_FLOWS' in block['intervals'][0]['reason_codes']
    assert 'components' not in consolidated(store, rows, end=DAYS[2], detail_level='summary')['intervals'][0]


def test_a_scope_absent_from_configuration_is_excluded_never_missing(tmp_path):
    days = [(1_000_000, kis(DAYS[0], '700000'), None), (1_010_000, kis(DAYS[1], '711400'), None)]
    store, rows = build(tmp_path, days)
    absent = consolidated(store, rows, end=DAYS[1], configured={'toss': True, 'kis': True, 'crypto': False})
    assert absent['excluded_scopes'] == [{'scope': 'crypto', 'reason_codes': ['SCOPE_NOT_CONFIGURED']}]
    assert absent['included_scopes'] == ['toss', 'kis'] and list(absent['accuracy']['components']) == ['toss', 'kis']
    # Toss + KIS only: (2,861,400 - 2,840,000) / 2,840,000.
    assert absent['period']['return_pct'] == pct(Decimal(21_400) / 2_840_000) == 0.7535
    assert absent['label'] == 'estimate' and 'Excluded as empty for the owner: crypto (SCOPE_NOT_CONFIGURED)' in \
        absent['explanation']
    # Unknown configuration never excludes: the same rows leave crypto missing, and the interval null.
    unknown = consolidated(store, rows, end=DAYS[1], configured={'toss': True, 'kis': True, 'crypto': None})
    assert unknown['excluded_scopes'] == [] and unknown['period']['return_pct'] is None
    assert unknown['intervals'][0]['missing_scopes'] == [{'scope': 'crypto', 'reason_codes': ['MISSING_CRYPTO_NAV']}]
    assert consolidated(store, rows, end=DAYS[1])['intervals'][0]['missing_scopes'][0]['scope'] == 'crypto'


def test_configuration_never_drops_stored_money_and_a_failed_read_never_excludes(three, tmp_path):
    store, rows = three
    # Configured absent but a complete non-zero crypto NAV is stored: included (real money is never dropped).
    funded = consolidated(store, rows, end=DAYS[2], configured={'toss': True, 'kis': True, 'crypto': False})
    assert funded['excluded_scopes'] == [] and funded['period']['return_pct'] == pct((1 + R1) * (1 + R2) - 1)
    # Configured, but the crypto reads failed: missing with the read's reason, never excluded.
    days = [(1_000_000, kis(DAYS[0], '700000'), (100, 100)), (1_010_000, kis(DAYS[1], '711400'), (100, 100))]
    (tmp_path / 'failed').mkdir()
    failed_store, failed_rows = build(tmp_path / 'failed', days, crypto_status='unknown')
    failed = consolidated(failed_store, failed_rows, end=DAYS[1], configured={'toss': True, 'kis': True, 'crypto': True})
    assert failed['excluded_scopes'] == [] and failed['period']['return_pct'] is None
    missing = failed['intervals'][0]['missing_scopes']
    assert missing[0]['scope'] == 'crypto' and 'MISSING_CRYPTO_FX_RATE' in missing[0]['reason_codes']
    # A complete zero at every point is genuinely empty: excluded, and Toss + KIS give the number.
    (tmp_path / 'zero').mkdir()
    zero_store, zero_rows = build(tmp_path / 'zero', [(t, k, (0, 0)) for t, k, _ in days])
    assert [nav['total_value_krw'] for nav in scope_navs(zero_rows, 'crypto')] == [0.0, 0.0]
    zero = consolidated(zero_store, zero_rows, end=DAYS[1], configured={'toss': True, 'kis': True, 'crypto': True})
    assert zero['excluded_scopes'] == [{'scope': 'crypto', 'reason_codes': ['ZERO_NAV_THROUGHOUT']}]
    assert zero['period']['return_pct'] == pct(Decimal(21_400) / 2_840_000)


def test_stored_money_keeps_a_scope_whose_every_read_failed_even_when_configured_absent(tmp_path):
    # Crypto configured absent, every stored point failed (no FX mark), yet 100 USDT + 100 USDC are stored:
    # funded, so missing with the read's reason and a null period, never Toss + KIS alone (0.7535 %).
    days = [(1_000_000, kis(DAYS[0], '700000'), (100, 100)), (1_010_000, kis(DAYS[1], '711400'), (100, 100))]
    store, rows = build(tmp_path, days, crypto_status='unknown')
    absent = {'toss': True, 'kis': True, 'crypto': False}
    block = consolidated(store, rows, end=DAYS[1], configured=absent)
    assert block['excluded_scopes'] == [] and block['included_scopes'] == ['toss', 'kis', 'crypto']
    assert block['period']['return_pct'] is None and block['label'] == 'unavailable'
    missing = block['intervals'][0]['missing_scopes']
    assert [entry['scope'] for entry in missing] == ['crypto'] and 'MISSING_CRYPTO_FX_RATE' in missing[0]['reason_codes']
    # The same failed reads over balances proven zero hold no money: excluded, Toss + KIS give the number.
    (tmp_path / 'zero').mkdir()
    zero_store, zero_rows = build(tmp_path / 'zero', [(t, k, (0, 0)) for t, k, _ in days], crypto_status='unknown')
    zero = consolidated(zero_store, zero_rows, end=DAYS[1], configured=absent)
    assert zero['excluded_scopes'] == [{'scope': 'crypto', 'reason_codes': ['SCOPE_NOT_CONFIGURED']}]
    assert zero['period']['return_pct'] == pct(Decimal(21_400) / 2_840_000) == 0.7535


def test_each_scope_weights_its_flows_within_its_own_capture_instants(tmp_path):
    # Same dates, three instants: crypto is captured at 16:40 KST, Toss at 18:10 and KIS at 18:11.
    lead = timedelta(minutes=90)
    store, rows = build(tmp_path, [(1_000_000, kis(DAYS[0], '700000'), (100, 100)),
                                   (1_010_000, kis(DAYS[1], '711400'), (150, 100))], crypto_lead=lead)
    bn_transfer(store, stamp=T0 - timedelta(minutes=70))  # 17:00 KST: inside crypto's interval, before Toss's
    row = consolidated(store, rows, end=DAYS[1])['intervals'][0]
    parts = {part['scope']: part for part in row['components']}
    assert {scope: (part['t0'], part['t1']) for scope, part in parts.items()} == {
        'toss': (T0.isoformat(), (T0 + timedelta(days=1)).isoformat()),
        'kis': (kis_stamp(DAYS[0]), kis_stamp(DAYS[1])),
        'crypto': ((T0 - lead).isoformat(), (T0 + timedelta(days=1) - lead).isoformat())}
    assert row['t0'] == kis_stamp(DAYS[0])  # the latest instant on the date
    assert parts['crypto']['flows_krw'] == 70_000 and parts['crypto']['timing'] == 'known'
    # Weighted in crypto's own (16:40, 16:40]: (24 h - 20 min) / 24 h, not 18:10's 1439/1440 (0.6709 %).
    weight = Decimal(1420) / Decimal(1440)
    assert row['return_pct'] == pct((N1 - N0 - 70_000) / (N0 + weight * 70_000)) == 0.6711


def test_label_is_the_weakest_part(three):
    store, rows = three
    navs = rows[:2]
    passed = {scope: {'status': 'measured', 'passed_bar': True} for scope in ('toss', 'kis', 'crypto')}
    crypto = (scope_navs(navs, 'crypto'), crypto_interval_flows(store.db_path, DAYS[0], DAYS[1]), [], [])
    alone = ir.consolidate({'crypto': crypto}, DAYS[0], DAYS[1], accuracy=passed)
    # Every crypto flow is observed with its event FX and time: on a passed bar that part is verified.
    assert alone['label'] == 'verified' and alone['intervals'][0]['label'] == 'verified'
    assert alone['accuracy']['passed_bar'] is False  # the composite itself is never measured
    toss = (scope_navs(navs, 'toss'), toss_records(rows)(None, DAYS[0], DAYS[1]), ['toss:primary'], [])
    both = ir.consolidate({'crypto': crypto, 'toss': toss}, DAYS[0], DAYS[1], accuracy=passed)
    assert both['label'] == 'estimate'  # Toss flows are inferred: the weakest part decides
    assert ir.consolidate({'crypto': crypto}, DAYS[0], DAYS[1])['label'] == 'estimate'  # shipped ACCURACY


def test_tools_show_the_consolidated_block(three, tmp_path, monkeypatch):
    from test_ledger_returns import wired as base_wired
    wired = base_wired.__wrapped__(tmp_path, monkeypatch)
    server = wired.server
    store, rows = three
    wired.navs[:] = rows
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', store.db_path)
    monkeypatch.setattr(ir, '_provider', lambda name: providers(rows)[name])
    monkeypatch.setattr(server, '_scope_configuration', lambda: {'toss': True, 'kis': True, 'crypto': True})
    before = store.db_path.read_bytes()
    for tool in (server.get_portfolio_performance, server.get_portfolio_history):
        result = tool(input_snapshot_ids=['synthetic'], portfolio_scope='consolidated')
        assert result['ok'], result.get('error')
        block = result['data']['ledgerless_returns']
        assert block['scope'] == 'consolidated' and block['label'] == 'unavailable'
        assert [row['return_pct'] for row in block['intervals']] == [pct(R1), pct(R2), None]
        assert block['intervals'][-1]['missing_scopes'][0]['scope'] == 'kis'
        assert 'components' not in block['intervals'][0]  # summary keeps the table compact
        assert block['tracked']['return_pct'] == pct((1 + R1) * (1 + R2) - 1)
        assert block['accuracy']['components']['kis']['status'] == 'forward_pending'
    full = server.get_portfolio_history(input_snapshot_ids=['synthetic'], portfolio_scope='consolidated',
                                        detail_level='full')['data']['ledgerless_returns']
    assert [part['scope'] for part in full['intervals'][0]['components']] == ['toss', 'kis', 'crypto']
    assert store.db_path.read_bytes() == before


def record_collector(server, monkeypatch):
    """The live branch: live holdings and the whole broker history (~150 s live). Records every use."""
    live, collected = server._get_collector(), []

    class Collector:
        def __getattr__(self, name):
            collected.append(name)
            return getattr(live, name)
    monkeypatch.setattr(server, '_get_collector', lambda: Collector())
    return collected


def test_consolidated_performance_never_collects_broker_history_and_says_it_has_no_pnl_decomposition(
        three, tmp_path, monkeypatch):
    from test_ledger_returns import wired as base_wired
    wired = base_wired.__wrapped__(tmp_path, monkeypatch)
    server = wired.server
    store, rows = three
    wired.navs[:] = rows
    monkeypatch.setattr(type(server._get_nav_store()), 'db_path', store.db_path)
    monkeypatch.setattr(ir, '_provider', lambda name: providers(rows)[name])
    monkeypatch.setattr(server, '_scope_configuration', lambda: {'toss': True, 'kis': True, 'crypto': True})
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (DAYS[0], DAYS[3]))
    monkeypatch.setattr(server, '_risk_free_decimal', lambda: 0.04)
    collected = record_collector(server, monkeypatch)
    result = server.get_portfolio_performance(portfolio_scope='consolidated')  # no snapshot IDs: live mode
    assert result['ok'], result.get('error')
    assert collected == []
    data = result['data']
    assert data['replay_scope'].startswith('snapshot_nav') and data['risk']['risk_free_rate_used'] == 0.04
    # No P&L decomposition, said plainly: every part is null and unavailable, with its reason.
    assert data['pnl']['realized_krw'] is None and data['metrics']['realized_krw']['reason_codes'] == ['LEDGER_NOT_PINNED']
    assert data['pnl']['unrealized_krw'] is None and data['metrics']['unrealized_krw']['status'] == 'unavailable'
    for field in ('fees', 'taxes', 'dividends', 'interest'):
        assert data['pnl'][field + '_krw'] is None and data['pnl'][field + '_status'] == 'unavailable'
        assert 'LEDGER_NOT_PINNED' in data['pnl'][field + '_reason_codes']
    assert data['flows']['buy_amount_krw'] is None and data['flows']['sell_amount_krw'] is None
    assert data['methodology']['pnl_decomposition_scope'] == 'none (LEDGER_NOT_PINNED)'
    assert [line for line in data['limitations'] if line.startswith('Consolidated has no P&L decomposition')]
    # The ledgerless returns are the stored ones, as in replay (consolidated returns: the next test).
    block = data['ledgerless_returns']
    assert [row['return_pct'] for row in block['intervals']] == [pct(R1), pct(R2), None]
    replay = server.get_portfolio_performance(input_snapshot_ids=['synthetic'], portfolio_scope='consolidated')['data']
    assert replay['ledgerless_returns'] == block


def test_consolidated_live_mode_returns_and_risk_are_the_stored_m3_ones(tmp_path, monkeypatch):
    # The rows above cannot carry certified consolidated returns (their Hyperliquid unified wallet is not a
    # flow boundary), so these are consolidated NAVs with every venue's flows covered: 1000 -> 1100
    # -> 1045 -> 1200 KRW over four days, with a 100 KRW deposit on the last.
    from test_ledger_returns import coverage as m3_coverage
    from test_ledger_returns import flow as m3_flow
    from test_ledger_returns import wired as base_wired
    wired = base_wired.__wrapped__(tmp_path, monkeypatch)
    server = wired.server
    days = ['2025-12-29', '2025-12-30', '2025-12-31', '2026-01-01']
    wired.navs[:] = [dict(snapshot_date=day, total_value_krw=level / 2, snapshot_id='snap-' + day,
                          consolidated_nav_krw={'value': level, 'status': 'derived', 'reason_codes': []},
                          valuation_timezone='Asia/Seoul', account_nav={})
                     for day, level in zip(days, (1000, 1100, 1045, 1200))]
    m3_coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'), lo=days[0], hi=days[-1])
    m3_flow(wired, amount='100')  # 2026-01-01 01:00 KST
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (days[0], days[-1]))
    monkeypatch.setattr(server, '_risk_free_decimal', lambda: 0.04)
    collected = record_collector(server, monkeypatch)
    result = server.get_portfolio_performance(portfolio_scope='consolidated')  # no snapshot IDs: live mode
    assert result['ok'], result.get('error')
    assert collected == []
    data = result['data']
    assert result['result_status'] == data['result_status'] == 'partial'  # no P&L decomposition
    assert data['start_nav_krw'] == 1000 and data['end_nav_krw'] == 1200
    assert data['returns']['twr_pct'] == 10  # 1.1 x 0.95 x (1200 - 100) / 1045
    assert all(data['returns'][name] is not None for name in ('simple_return_pct', 'twr_pct', 'xirr_pct'))
    # Risk on the flow-adjusted daily returns, at the live risk-free rate.
    daily = [Decimal(1100) / 1000 - 1, Decimal(1045) / 1100 - 1, Decimal(1200 - 100) / 1045 - 1]
    excess = sum(daily) / 3 * 252 - Decimal('0.04')
    deviation = (sum((r - sum(daily) / 3) ** 2 for r in daily) / 2).sqrt()
    assert data['risk']['sharpe'] == round(float(excess / (deviation * Decimal(252).sqrt())), 4)
    assert data['risk']['sortino'] == round(float(excess / (Decimal('0.05') * Decimal(252).sqrt())), 4)
    assert data['risk']['metrics']['sharpe']['status'] == data['risk']['metrics']['sortino']['status'] == 'available'
    # Exactly replay's: the returns at replay's default rate, the risk at the same rate.
    replay = server.get_portfolio_performance(input_snapshot_ids=['synthetic'], portfolio_scope='consolidated')['data']
    assert replay['returns'] == data['returns'] and replay['metrics'] == data['metrics']
    replay = server.get_portfolio_performance(input_snapshot_ids=['synthetic'], portfolio_scope='consolidated',
                                              risk_free_rate=0.04)['data']
    assert replay['risk'] == data['risk']


def test_scope_configuration_reads_names_and_file_presence_only(tmp_path, monkeypatch):
    import server
    for name in ('HYPERLIQUID_ACCOUNT_ADDRESSES', 'CREDENTIALS_DIRECTORY', 'BINANCE_FUTURES_API_KEY_FILE',
                 'BINANCE_FUTURES_SIGNING_KEY_FILE'):
        monkeypatch.delenv(name, raising=False)
    assert server._scope_configuration()['crypto'] is False
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    assert server._scope_configuration()['crypto'] is None  # named place, nothing there: unknown, never excluded
    for suffix in ('binance-futures-api-key', 'binance-futures-signing-key'):
        (tmp_path / suffix).touch()
    assert server._scope_configuration()['crypto'] is True
    monkeypatch.delenv('CREDENTIALS_DIRECTORY')
    monkeypatch.setenv('HYPERLIQUID_ACCOUNT_ADDRESSES', '0x' + 'a' * 40)
    assert server._scope_configuration()['crypto'] is True
    assert set(server._scope_configuration()) == {'toss', 'kis', 'crypto'}
