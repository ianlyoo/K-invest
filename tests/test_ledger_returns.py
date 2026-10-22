"""Ledger returns acceptance with synthetic SQLite evidence; no providers or credentials."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from portfolio_ledger import LedgerStore
from portfolio_model import FxRates
from portfolio_sources import Collection

START, END = '2025-01-01', '2026-01-01'
ROUTES = ('MAIN_UMFUTURE', 'UMFUTURE_MAIN', 'MAIN_FUNDING', 'FUNDING_MAIN',
          'FUNDING_UMFUTURE', 'UMFUTURE_FUNDING')
ACCOUNTS = {v: 'acct_' + c * 32 for v, c in zip(('toss', 'kis', 'binance', 'hyperliquid'), '1234')}


def millis(day):
    return int(datetime.fromisoformat(day + 'T00:00:00+09:00').timestamp() * 1000)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    import server
    db = tmp_path / 'synthetic.db'
    ledger = LedgerStore(db)
    with ledger.read():
        pass
    navs = [dict(snapshot_date=day, total_value_krw=value, snapshot_id='snap-' + day,
                 consolidated_nav_krw={'value': value * 2, 'status': 'derived', 'reason_codes': []},
                 valuation_timezone='Asia/Seoul', account_nav={})
            for day, value in ((START, 100), (END, 160))]

    class Store:
        db_path = db
        def range(self, *args):
            return navs
        def select_snapshots(self, *args):
            return navs
        def collection_health(self, *args):
            return {'expected_days': None, 'expected_day_gaps': None, 'calendar_status': 'unknown'}
        def bounds(self):
            return START, END
        def count(self):
            return len(navs)

    class Collector:
        def collect_positions(self):
            return Collection(health={'synthetic': {'status': 'ok'}})
        def collect_transactions(self, *args):
            return Collection(health={'synthetic': {'status': 'ok'}})
        def build_fx(self, *args):
            return FxRates({'USD': 9999})  # Spot is never historical flow FX.

    monkeypatch.setattr(server, '_get_nav_store', lambda: Store())
    monkeypatch.setattr(server, '_get_collector', lambda: Collector())
    monkeypatch.setattr(server, '_risk_free_decimal', lambda: 0)
    monkeypatch.setattr(server.portfolio_engine, 'resolve_period', lambda _: (START, END))
    return SimpleNamespace(server=server, ledger=ledger, navs=navs)


def seed_roster(w):
    """Certifiable fixtures need actual NAV membership and durable alias proof."""
    for nav in w.navs:
        for provider in ('toss', 'kis'):
            nav['account_nav'].setdefault(provider + ':fixture', {})
        nav.setdefault('crypto_nav', {v: {'accounts': [{'account_id': ACCOUNTS[v], 'asset': 'KRW',
            'scope': 'futures_collateral' if v == 'binance' else 'perp_dex', 'dex': ''}]}
            for v in ('binance', 'hyperliquid')})
        nav['consolidated_nav_krw']['basis'] = 'legacy_broker_assets_plus_verified_futures_equity_before_settlement_adjustments'
    with w.ledger.transaction() as conn:
        for provider in ('toss', 'kis', 'binance', 'hyperliquid'):
            reference = provider + ':fixture' if provider in ('toss', 'kis') else ACCOUNTS[provider]
            conn.execute('INSERT OR IGNORE INTO ledger_account_aliases VALUES (?,?,?)',
                (w.ledger._privacy.opaque(reference), ACCOUNTS[provider], datetime.now(timezone.utc).isoformat()))


def coverage(w, venues=('toss', 'kis'), lo=START, hi=END, status='complete'):
    seed_roster(w)
    for venue in venues:
        streams = ['cashflows'] if venue in ('toss', 'kis') else (
            ['deposit', 'withdraw'] + ['transfer_' + r for r in ROUTES]
            if venue == 'binance' else ['nonFundingLedger'])
        for stream in streams:
            w.ledger.record_coverage(venue, ACCOUNTS[venue], venue + '/' + stream,
                millis(lo), millis(hi) + 86_400_000 - 1, status,
                [] if status == 'complete' else ['SYNTHETIC_GAP'], 1, 'synthetic')


def flow(w, *, classification='external_in', amount='50', currency='KRW',
         stamp='2025-12-31T16:00:00+00:00', venue='toss', record='flow-1', fx=None):
    event = dict(source=venue + '_cashflows', account_identity=ACCOUNTS[venue],
        source_record_id=record, native_record_id=record,
        kind='withdrawal' if classification == 'external_out' else 'deposit',
        execution_time_utc=stamp, source_timezone='UTC', timestamp_precision='second',
        activity_date=stamp[:10], currency=currency, amount=amount, fx_rate=fx,
        direction='out' if classification == 'external_out' else 'in',
        source_status='completed', identity_quality='native_record')
    with w.ledger.transaction() as conn:
        assert w.ledger.append(conn, event) == 'new'
        event_id = conn.execute('SELECT event_id FROM ledger_events ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        w.ledger.classify_transfer(conn, event_id, classification, 'SYNTHETIC_CLASSIFICATION')
    return event, event_id


def performance(w, **kwargs):
    result = w.server.get_portfolio_performance(input_snapshot_ids=['synthetic'], **kwargs)
    assert result['ok'], result.get('error')
    return result['data']


def blocked(data, reason):
    for name in ('simple_return_pct', 'twr_pct', 'xirr_pct'):
        assert data['returns'][name] is None
        assert reason in data['metrics'][name]['reason_codes']
        assert data['metrics'][name]['coverage']['first_uncovered_gap'] is not None


def test_external_only_hand_computed_twr_xirr_and_kst_date(wired):
    coverage(wired)
    flow(wired)
    data = performance(wired)
    assert data['returns']['twr_pct'] == 10
    assert data['returns']['xirr_pct'] == 10  # Existing XIRR uses actual days / 365.
    assert data['flows']['flows_by_date'] == {END: 50}
    assert data['metrics']['twr_pct']['coverage']['covered_window'] == {'start': START, 'end': END}


def test_matched_internal_pair_excluded(wired):
    coverage(wired)
    _, out_id = flow(wired, classification='internal', venue='toss', record='out')
    _, in_id = flow(wired, classification='internal', venue='kis', record='in')
    with wired.ledger.transaction() as conn:
        conn.execute('INSERT INTO ledger_transfer_matches VALUES (?,?,?,?,?,?,?,?)',
            ('synthetic-pair', 1, out_id, in_id, 'synthetic', '{}', 'internal', datetime.now(timezone.utc).isoformat()))
    data = performance(wired)
    assert data['flows']['flows_by_date'] == {}
    assert data['returns']['twr_pct'] == 60


def test_unknown_flow_blocks_returns(wired):
    coverage(wired)
    flow(wired, classification='unknown')
    blocked(performance(wired), 'UNCLASSIFIED_FLOWS')


def test_conflicted_flow_blocks_returns(wired):
    coverage(wired)
    event, _ = flow(wired)
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'amount': '51'}) == 'conflict'
    blocked(performance(wired), 'CONFLICTED_FLOWS')


def test_missing_flow_date_fx_never_uses_spot(wired):
    coverage(wired)
    flow(wired, currency='USD')
    blocked(performance(wired), 'MISSING_FLOW_FX')


def test_coverage_gap_blocks_returns(wired):
    coverage(wired, hi='2025-06-01')
    flow(wired)
    blocked(performance(wired), 'FLOW_COVERAGE_GAP')


def test_period_before_flow_coverage(wired):
    coverage(wired, lo='2025-11-01')
    flow(wired)
    blocked(performance(wired), 'PERIOD_BEFORE_FLOW_COVERAGE')


def test_consolidated_uses_m3_nav_and_all_venue_coverage(wired):
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    flow(wired, amount='100')
    data = performance(wired, portfolio_scope='consolidated')
    assert data['start_nav_krw'] == 200
    assert data['end_nav_krw'] == 320
    assert data['returns']['twr_pct'] == 10
    assert data['portfolio_scope'] == 'consolidated'


def test_history_uses_same_ledger_flow_source(wired):
    coverage(wired)
    flow(wired)
    data = wired.server.get_portfolio_history(input_snapshot_ids=['synthetic'], detail_level='full')['data']
    assert data['pnl_series']['daily'][0]['net_flow_krw'] == 50
    assert data['statistics']['metrics']['max_drawdown_pct']['coverage']['covered_window'] == {'start': START, 'end': END}


def test_no_raw_identifiers_in_return_output(wired):
    coverage(wired)
    flow(wired, record='0x' + 'b' * 64)
    data = performance(wired)
    assert data['returns']['twr_pct'] == 10
    serialized = json.dumps(data)
    assert '0x' + 'b' * 64 not in serialized
    assert ACCOUNTS['toss'] not in serialized


def test_default_no_ledger_evidence_is_byte_compatible(wired, monkeypatch):
    before = performance(wired)
    monkeypatch.setattr(wired.server, '_get_nav_store', lambda: SimpleNamespace(
        select_snapshots=lambda _: wired.navs,
        collection_health=lambda *args: {'expected_days': None, 'expected_day_gaps': None, 'calendar_status': 'unknown'}))
    assert performance(wired) == before


@pytest.mark.parametrize('tool', ['get_portfolio_performance', 'get_portfolio_history'])
def test_defaults_match_e749352_response_fixture(wired, tool):
    expected = json.loads(Path('tests/fixtures/returns_defaults.golden.json').read_text())[tool]
    result = getattr(wired.server, tool)(input_snapshot_ids=['synthetic'], detail_level='full')
    assert result['ok']
    # 2.12.10 adds one key, ledgerless_returns; every existing field is byte-identical. Equity is a supported
    # ledgerless scope since 2.15; here neither part is configured or funded, so it is unavailable.
    block = result['data'].pop('ledgerless_returns')
    assert block['label'] == 'unavailable' and block['reason_codes'] == ['CONSOLIDATED_NO_INCLUDED_SCOPE']
    # 2.15 adds NAV basis/as-of labels; they describe the stored rows and change no existing field.
    added = [key for key in result['data'] if key.endswith(('_nav_basis', '_nav_as_of', '_nav_snapshot_date',
                                                            '_nav_snapshot_id')) or key == 'latest_nav_basis']
    assert sorted(added) == (['latest_nav_basis'] if tool == 'get_portfolio_history' else sorted(
        f'{side}_nav_{field}' for side in ('start', 'end') for field in ('basis', 'as_of', 'snapshot_date', 'snapshot_id')))
    for key in added:
        result['data'].pop(key)
    # fx_valuation follows fx.policy; a replay values every NAV at its own snapshot's FX (history
    # gains the key). The fixture's text was the earlier "current spot rate (yfinance <CUR>KRW=X)".
    note = result['data']['methodology'].pop('fx_valuation')
    assert "stored snapshot NAVs keep the FX each snapshot was valued at" in note and 'fx.policy' in note
    expected['methodology'].pop('fx_valuation', None)
    # Deploy-date flag: additive warning keys, empty here (the synthetic rows carry no basis label).
    assert result['data'].pop('warning_codes') == [] and result['data'].pop('valuation_basis_change') is None
    for row in result['data']['pnl_series']['daily']:
        assert row.pop('warning_codes') == []
    assert json.dumps(result['data'], sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_missing_consolidated_nav_never_falls_back_to_equity(wired):
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    flow(wired)
    wired.navs[-1]['consolidated_nav_krw'] = {'value': None, 'status': 'unknown', 'reason_codes': ['STALE_CRYPTO_SNAPSHOT']}
    blocked(performance(wired, portfolio_scope='consolidated'), 'STALE_CRYPTO_SNAPSHOT')


def test_missing_in_scope_venue_and_partial_stream_block(wired):
    coverage(wired, ('toss', 'kis', 'binance'))
    flow(wired)
    blocked(performance(wired, portfolio_scope='consolidated'), 'FLOW_COVERAGE_GAP')
    coverage(wired, ('hyperliquid',), status='limited')
    blocked(performance(wired, portfolio_scope='consolidated'), 'FLOW_COVERAGE_GAP')


def test_internal_classification_without_matched_pair_blocks(wired):
    coverage(wired)
    flow(wired, classification='internal')
    blocked(performance(wired), 'UNCLASSIFIED_FLOWS')


def test_historical_event_fx_and_signed_withdrawals(wired):
    coverage(wired)
    flow(wired, currency='USD', fx='2', amount='25')
    flow(wired, classification='external_out', currency='USD', fx='2', amount='10', record='out')
    data = performance(wired)
    assert data['flows']['flows_by_date'] == {END: 30}
    assert data['flows']['withdrawals_krw'] == 20
    assert data['returns']['twr_pct'] == 30


def test_actual_midperiod_xirr_dates_and_interval_end_twr(wired):
    coverage(wired)
    flow(wired, stamp='2025-06-30T16:00:00+00:00')
    data = performance(wired)
    rate = data['returns']['xirr_pct'] / 100
    # Independently evaluate the dated NPV, rather than calling the solver.
    assert abs(-100 - 50 / (1 + rate) ** (181 / 365) + 160 / (1 + rate)) < 0.0002
    assert data['flows']['flows_by_date'] == {'2025-07-01': 50}
    assert data['returns']['twr_pct'] == 10


def test_read_only_does_not_create_database_or_change_ledger(wired, tmp_path):
    from ledger_returns import read_flows
    missing = tmp_path / 'missing.db'
    assert read_flows(missing, START, END) is None
    assert not missing.exists()
    coverage(wired)
    flow(wired)
    before = wired.ledger.db_path.read_bytes()
    assert performance(wired)['returns']['twr_pct'] == 10
    assert wired.ledger.db_path.read_bytes() == before


def test_explicit_zero_confirmation_preserves_existing_behavior(wired):
    coverage(wired, status='limited')
    flow(wired, classification='unknown')
    data = performance(wired, external_cashflows_verified_zero=True)
    assert data['returns']['twr_pct'] == 60
    assert data['flows']['external_cashflows']['status'] == 'verified_zero'
    assert 'ledger_flow_coverage' not in data


def test_multi_account_coverage_cannot_certify_other_account(wired):
    coverage(wired)
    event, _ = flow(wired)
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'account_identity': 'acct_' + 'e' * 32,
            'kind': 'fill', 'source_record_id': 'other-account-fill'}) == 'new'
        conn.execute('INSERT INTO ledger_account_aliases VALUES (?,?,?)',
            (wired.ledger._privacy.opaque('toss:second'), 'acct_' + 'e' * 32, datetime.now(timezone.utc).isoformat()))
    for nav in wired.navs:
        nav['account_nav']['toss:second'] = {}
    blocked(performance(wired), 'FLOW_COVERAGE_GAP')


def test_gap_between_complete_windows_not_silently_shortened(wired):
    coverage(wired, hi='2025-06-30')
    coverage(wired, lo='2025-07-02')
    flow(wired)
    data = performance(wired)
    blocked(data, 'FLOW_COVERAGE_GAP')
    evidence = data['metrics']['twr_pct']['coverage']
    assert evidence['first_uncovered_gap']['start'] == '2025-07-01'
    assert len(evidence['covered_windows']) == 2


def test_consolidated_history_preserves_stored_equity_nav_fields(wired):
    coverage(wired, ('toss', 'kis', 'binance', 'hyperliquid'))
    flow(wired, amount='100')
    before = json.dumps(wired.navs, sort_keys=True)
    result = wired.server.get_portfolio_history(input_snapshot_ids=['synthetic'],
        detail_level='full', portfolio_scope='consolidated')
    assert result['ok']
    data = result['data']
    assert data['snapshots'] == wired.navs
    assert data['latest']['total_value_krw'] == 160
    assert data['pnl_series']['daily'][0]['pnl_krw'] == 20
    metric_coverage = data['pnl_series']['daily'][0]['metrics']['pnl_krw']['coverage']
    assert metric_coverage['covered_window'] == {'start': START, 'end': END}
    assert metric_coverage['external_cashflows'] == 'explicit_mapping'
    assert metric_coverage['ledger_flow_coverage']['source'] == 'permanent_ledger'
    assert json.dumps(wired.navs, sort_keys=True) == before


def test_missing_requested_boundary_nav_blocks_ledger_history(wired):
    coverage(wired)
    flow(wired)
    wired.navs[0]['snapshot_date'] = '2025-01-02'
    result = wired.server.get_portfolio_history(start_date=START, end_date=END)
    assert result['ok']
    metric = result['data']['statistics']['metrics']['max_drawdown_pct']
    assert metric['value'] is None
    assert 'MISSING_START_NAV' in metric['reason_codes']


def test_same_day_nav_fx_is_accepted_but_other_day_mark_is_not(wired):
    coverage(wired)
    flow(wired, currency='USD', amount='25')
    wired.navs[-1]['fx_times'] = {'USD': {'rate_to_krw': 2, 'priced_at': '2025-12-31T16:00:00Z'}}
    assert performance(wired)['returns']['twr_pct'] == 10
    wired.navs[-1]['fx_times']['USD']['priced_at'] = '2025-12-30T16:00:00Z'
    blocked(performance(wired), 'MISSING_FLOW_FX')


def test_latest_classification_and_enrichment_are_effective(wired):
    coverage(wired)
    event, event_id = flow(wired, classification='unknown', currency='USD', amount='25')
    blocked(performance(wired), 'UNCLASSIFIED_FLOWS')
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'fx_rate': '2'}) == 'duplicate'
        wired.ledger.classify_transfer(conn, event_id, 'external_in', 'SYNTHETIC_OWNER_REVISION')
    assert performance(wired)['returns']['twr_pct'] == 10


def test_crypto_only_ledger_leaves_default_equity_response_unchanged(wired):
    before = performance(wired)
    coverage(wired, ('binance', 'hyperliquid'))
    flow(wired, venue='binance', classification='unknown')
    assert performance(wired) == before


def test_pending_flow_blocks_until_completed_status_revision(wired):
    coverage(wired)
    event, event_id = flow(wired)
    # A failed completed event cannot be matched or certified just by classification.
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'source_status': 'FAILED'}) == 'duplicate'
    blocked(performance(wired), 'INCOMPLETE_FLOW_STATUS')
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'source_status': 'completed'}) == 'duplicate'
    assert performance(wired)['returns']['twr_pct'] == 10


def test_failed_matched_peer_outside_period_invalidates_internal_evidence(wired):
    coverage(wired)
    event, out_id = flow(wired, classification='internal', venue='toss', record='old-out',
                        stamp='2024-12-31T00:00:00+00:00')
    _, in_id = flow(wired, classification='internal', venue='kis', record='in')
    with wired.ledger.transaction() as conn:
        conn.execute('INSERT INTO ledger_transfer_matches VALUES (?,?,?,?,?,?,?,?)',
            ('synthetic-pair', 1, out_id, in_id, 'synthetic', '{}', 'internal', datetime.now(timezone.utc).isoformat()))
    assert performance(wired)['returns']['twr_pct'] == 60
    with wired.ledger.transaction() as conn:
        assert wired.ledger.append(conn, {**event, 'source_status': 'FAILED'}) == 'duplicate'
    blocked(performance(wired), 'UNCLASSIFIED_FLOWS')
