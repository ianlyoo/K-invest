"""Owner-flow review regressions: invented exports and local databases only."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from test_ledger_returns import coverage, performance
from test_owner_flows import CSV_HEADER, HEADER_TEXT, NUMBER, activity, pdf, records
from test_owner_flows import wired as base_wired
from test_statement_import import synthetic_pdf

from ledger_returns import read_flows, scope_navs
from owner_flows import TossFlowParser, ingest_owner_export
from portfolio_ledger import LedgerStore


@pytest.fixture
def wired(tmp_path, monkeypatch):
    return base_wired.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('scope', ['toss', 'toss_investmcp', 'crypto'])
@pytest.mark.parametrize('reason', ['SNAPSHOT_DATE_MISMATCH', 'UNKNOWN_FX_PRICE_TIME'])
def test_scoped_nav_preserves_snapshot_quality(wired, tmp_path, scope, reason):
    if scope == 'crypto':
        coverage(wired, venues=('binance', 'hyperliquid'))
        for nav, value in zip(wired.navs, (100, 110)):
            for provider in ('binance', 'hyperliquid'):
                nav['crypto_nav'][provider].update(value_krw=value / 2, status='derived', reason_codes=[])
    else:
        ingest_owner_export('toss_pdf', pdf(tmp_path, activity('이체입금')), wired.ledger.db_path, account_suffix='4321')
        with wired.ledger.transaction() as conn:
            wired.ledger.bind_account(conn, 'toss:only', 'toss:' + NUMBER)
        for nav, value in zip(wired.navs, (100, 160)):
            nav['account_nav'] = {'toss:only': {'source_status': 'complete', 'nav_native': {
                'KRW': {'value': value, 'status': 'derived', 'reason_codes': []}}}}
    assert performance(wired, portfolio_scope=scope)['returns']['twr_pct'] == 10
    wired.navs[-1].update(result_status='partial', reason_codes=[reason])
    result = performance(wired, portfolio_scope=scope)
    for metric in ('simple_return_pct', 'twr_pct', 'xirr_pct'):
        assert result['returns'][metric] is None
        assert reason in result['metrics'][metric]['reason_codes']


@pytest.mark.parametrize('scope', ['toss', 'toss_investmcp', 'crypto'])
def test_scoped_nav_validates_valuation_and_price_times(scope):
    nav = dict(snapshot_date='2026-01-01', result_status='complete', reason_codes=[],
        valuation_at='2026-01-02T00:00:00+09:00', account_nav={'toss:only': {
            'source_status': 'complete', 'nav_native': {'USD': {'value': 100, 'status': 'reported'}}}},
        fx_times={'USD': {'rate_to_krw': 1000, 'priced_at': None}},
        crypto_nav={p: {'value_krw': 100, 'status': 'derived'} for p in ('binance', 'hyperliquid')})
    scoped = scope_navs([nav], scope, account_references=['toss:only'])[0]
    assert scoped['total_value_krw'] is None
    assert 'SNAPSHOT_DATE_MISMATCH' in scoped['reason_codes']
    if scope != 'crypto':
        assert 'UNKNOWN_FX_PRICE_TIME' in scoped['reason_codes']
    assert nav['result_status'] == 'complete'


@pytest.mark.parametrize('kind', ['toss_pdf', 'binance_csv'])
def test_overlapping_owner_export_refuses_atomically(tmp_path, kind):
    db = tmp_path / 'ledger.db'
    if kind == 'toss_pdf':
        first = pdf(tmp_path, activity('이체입금'))
        kwargs = {'account_suffix': '4321'}
        second_bytes = synthetic_pdf([HEADER_TEXT + '\n' + activity('이체입금') + '\n '])
    else:
        first = tmp_path / 'synthetic.csv'
        first.write_text(CSV_HEADER + 'fake,2026-01-01 12:00:00,Spot,Deposit,KRW,50,\n')
        second_bytes = first.read_bytes().replace(b'fake', b'other-invented')
        kwargs = {}
    ingest_owner_export(kind, first, db, **kwargs)
    with LedgerStore(db).read() as conn:
        before = '\n'.join(conn.iterdump())
    assert not any(ingest_owner_export(kind, first, db, **kwargs).values())
    second = tmp_path / ('second.pdf' if kind == 'toss_pdf' else 'second.csv')
    second.write_bytes(second_bytes)
    with pytest.raises(ValueError, match='^OWNER_EXPORT_OVERLAP_UNRECONCILED$'):
        ingest_owner_export(kind, second, db, **kwargs)
    with LedgerStore(db).read() as conn:
        assert '\n'.join(conn.iterdump()) == before


def two_accounts(tmp_path, first_row, second_row):
    db = tmp_path / 'ledger.db'
    first = pdf(tmp_path, first_row)
    ingest_owner_export('toss_pdf', first, db, account_suffix='4321')
    other = tmp_path / 'other.pdf'
    other.write_bytes(synthetic_pdf([HEADER_TEXT.replace(NUMBER, '654-32-999999') + '\n' + second_row]))
    result = ingest_owner_export('toss_pdf', other, db, account_suffix='9999')
    return LedgerStore(db), result


def test_interest_is_never_a_transfer_matching_candidate(tmp_path):
    store, result = two_accounts(tmp_path, activity('이체출금', '100'), activity('이자입금', '100'))
    assert result['evidence'] == 1
    rows = {r['kind']: r for r in records(store)}
    assert rows['withdrawal']['classification'] == 'external_out'
    assert rows['income_other']['classification'] is None
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_transfer_matches').fetchone()[0] == 0


@pytest.mark.parametrize('foreign', [False, True])
def test_source_decimal_precision_prevents_wrong_internal_match(tmp_path, foreign):
    store, _ = two_accounts(tmp_path, activity('이체출금', '100.00', native='100.00' if foreign else None),
                           activity('이체입금', '101.00', native='101.00' if foreign else None))
    rows = records(store)
    assert [r['amount_precision'] for r in rows] == ['0.01', '0.01']
    assert {r['classification'] for r in rows} == {'external_out', 'external_in'}


@pytest.mark.parametrize('amount', ['1,2345,678', '12,34', '1,,000', '100,00.00'])
def test_malformed_money_is_unknown_not_numeric(tmp_path, amount):
    file = pdf(tmp_path, activity('이체입금', amount))
    result = ingest_owner_export('toss_pdf', file, tmp_path / 'db', account_suffix='4321')
    assert result['unknown'] == 1 and result['external_in'] == 0
    assert records(LedgerStore(tmp_path / 'db'))[0]['amount'] is None
    with LedgerStore(tmp_path / 'db').read() as conn:
        facts = json.loads(conn.execute('SELECT facts_json FROM ledger_owner_export_rows').fetchone()[0])
        assert facts['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED']


def test_unknown_activity_labels_are_allowlisted(tmp_path):
    name = '가상인물입금'
    file = pdf(tmp_path, activity(name))
    result = ingest_owner_export('toss_pdf', file, tmp_path / 'db', account_suffix='4321', dry_run=True)
    assert result['unknown_by_kind'] == {'unknown': 1}
    ingest_owner_export('toss_pdf', file, tmp_path / 'db', account_suffix='4321')
    with LedgerStore(tmp_path / 'db').read() as conn:
        facts = conn.execute('SELECT facts_json FROM ledger_owner_export_rows').fetchone()[0]
        assert name not in facts and json.loads(facts)['activity_label'] == 'unknown'
    parsed = TossFlowParser().parse_pages([HEADER_TEXT + '\n' + activity('이체입금(가상인물)')])
    assert parsed.rows[0]['facts']['activity_label'] == '이체입금'


def test_unknown_activity_is_never_internalized(tmp_path):
    store, result = two_accounts(tmp_path, activity('이체출금', '100'), activity('가상인물입금', '100'))
    assert result['unknown'] == 1
    assert {r['classification'] for r in records(store)} == {'external_out', 'unknown'}


def test_nonoverlapping_csv_exports_and_reported_precision(tmp_path):
    db = tmp_path / 'ledger.db'
    for day in ('2026-01-01', '2026-01-02'):
        file = tmp_path / (day + '.csv')
        file.write_text(CSV_HEADER + f'fake,{day} 12:00:00,USD-M Futures,Deposit,KRW,100.00,\n')
        assert ingest_owner_export('binance_csv', file, db)['external_in'] == 1
    assert [r['amount_precision'] for r in records(LedgerStore(db))] == ['0.01', '0.01']


def test_malformed_native_money_uses_the_existing_amount_fallback(tmp_path):
    parsed = TossFlowParser().parse_pages([HEADER_TEXT + '\n' + activity('이체입금', native='1,2345,678')
        + '\n' + activity('외화증권배당금입금', native='12,34')])
    assert [r['classification'] for r in parsed.rows] == ['unknown', 'evidence']
    assert all(r['amount'] is None and r['facts']['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED'] for r in parsed.rows)


@pytest.mark.parametrize('stamp,reason', [(None, 'UNKNOWN_VALUATION_TIME'),
    ('2026-01-01T12:00:00', 'INVALID_VALUATION_TIME'), ('not-a-time', 'INVALID_VALUATION_TIME')])
def test_scoped_nav_rejects_missing_or_naive_valuation(stamp, reason):
    nav = dict(snapshot_date='2026-01-01', valuation_at=stamp, result_status='complete',
        account_nav={'toss:only': {'source_status': 'complete', 'nav_native': {'KRW': {'value': 100, 'status': 'reported'}}}})
    scoped = scope_navs([nav], 'toss')[0]
    assert scoped['total_value_krw'] is None and reason in scoped['reason_codes']


def test_scoped_nav_preserves_unavailable_status_and_checks_component_and_crypto_times():
    nav = dict(snapshot_date='2026-01-01', valuation_at='2026-01-01T12:00:00+09:00',
        result_status='unavailable', reason_codes=[], components=[{'account': 'toss:only', 'currency': 'KRW', 'priced_at': None}],
        account_nav={'toss:only': {'source_status': 'complete', 'nav_native': {'KRW': {'value': 100, 'status': 'reported'}}}},
        crypto_nav={'observed_at': '2026-01-01T01:00:00+09:00', **{
            p: {'value_krw': 100, 'status': 'reported', 'accounts': [{'asset': 'KRW', 'observed_at': None}]}
            for p in ('binance', 'hyperliquid')}})
    for scope, reason in [('toss', 'UNKNOWN_COMPONENT_PRICE_TIME'), ('crypto', 'STALE_CRYPTO_SNAPSHOT')]:
        scoped = scope_navs([nav], scope)[0]
        assert scoped['result_status'] == 'unavailable' and scoped['total_value_krw'] is None
        assert 'INCOMPLETE_SNAPSHOT' in scoped['reason_codes'] and reason in scoped['reason_codes']


def test_1200_csv_deposits_ingest_and_read_within_budget(tmp_path):
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + ''.join(
        f'fake,2026-01-01 12:00:00,USD-M Futures,Deposit,KRW,1.00,{i}\n' for i in range(1200)))
    db = tmp_path / 'ledger.db'
    began = time.monotonic()
    result = ingest_owner_export('binance_csv', file, db)
    elapsed = time.monotonic() - began
    assert result['external_in'] == 1200
    assert elapsed < 5, elapsed
    store = LedgerStore(db)
    with store.transaction() as conn:
        store.bind_account(conn, 'synthetic-binance-risk', 'binance:primary')
        hl_identity = store.account_identity('hyperliquid:synthetic-owner', conn)
        store.bind_account(conn, 'synthetic-hl-risk', 'hyperliquid:synthetic-owner')
    navs = [dict(snapshot_date='2026-01-01', crypto_nav={
        'binance': {'accounts': [{'account_id': 'synthetic-binance-risk', 'asset': 'KRW', 'scope': 'futures_collateral', 'dex': ''}]},
        'hyperliquid': {'accounts': [{'account_id': 'synthetic-hl-risk', 'asset': 'KRW', 'scope': 'perp_dex', 'dex': ''}]}})]
    store.record_coverage('hyperliquid', hl_identity, 'hyperliquid/nonFundingLedger',
        '2026-01-01T00:00:00+09:00', '2026-01-01T23:59:59.999+09:00', 'complete', [], 0, 'synthetic')
    began = time.monotonic()
    result = read_flows(db, '2026-01-01', '2026-01-01', 'crypto', navs)
    assert time.monotonic() - began < 0.5
    assert result['coverage']['reason_codes'] == []
    assert result['observed_deposits_krw'] == 1200


@pytest.mark.parametrize('mode', ['absent', 'delete', 'wal', 'live_wal'])
def test_dry_run_never_creates_target_sidecars(tmp_path, mode):
    file = pdf(tmp_path, activity('이체입금'))
    target = tmp_path / 'target'
    target.mkdir()
    db = target / 'ledger.db'
    live = None
    if mode != 'absent':
        with LedgerStore(db).read():
            pass
        with sqlite3.connect(db) as conn:
            conn.execute('PRAGMA journal_mode=' + ('DELETE' if mode == 'delete' else 'WAL'))
        conn.close()
        if mode == 'live_wal':
            live = sqlite3.connect(db)
            live.execute('CREATE TABLE synthetic_uncheckpointed(value)')
            live.commit()
            ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    before = {p.name for p in target.iterdir()}
    try:
        result = ingest_owner_export('toss_pdf', file, db, account_suffix='4321', dry_run=True)
        assert result['external_in'] == (0 if mode == 'live_wal' else 1)
        assert {p.name for p in target.iterdir()} == before
        if mode != 'absent':
            with sqlite3.connect(db) as check:
                assert check.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == (1 if mode == 'live_wal' else 0)
    finally:
        if live:
            live.close()
    assert not Path(str(db) + '-journal').exists()


def test_dry_run_refuses_a_changing_snapshot(tmp_path, monkeypatch):
    import owner_flows
    db = tmp_path / 'ledger.db'
    with LedgerStore(db).read():
        pass
    copy = owner_flows.shutil.copyfile

    def changing(source, destination):
        result = copy(source, destination)
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        return result

    monkeypatch.setattr(owner_flows.shutil, 'copyfile', changing)
    with pytest.raises(ValueError, match='^OWNER_EXPORT_DRY_RUN_SNAPSHOT_CHANGED$'):
        ingest_owner_export('toss_pdf', pdf(tmp_path, activity('이체입금')), db, account_suffix='4321', dry_run=True)
