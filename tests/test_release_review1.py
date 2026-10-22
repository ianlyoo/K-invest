"""Corrective review regressions, synthetic stores and clients only."""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest
from test_collect_ledger import collect_ledger
from test_portfolio_ledger import event, export_fixture
from test_portfolio_sources import build

import crypto_futures_accounts as futures
import portfolio_ledger as ledger


def test_csv_seconds_matches_api_milliseconds_and_keeps_both_evidence(tmp_path):
    run = export_fixture(tmp_path)
    page = run / 'binance/userTrades_BTCUSDT/1-2-000001.json'
    rows = json.loads(page.read_text())
    rows[0]['time'] += 123
    page.write_text(json.dumps(rows))
    manifest = json.loads((run / 'manifest.json').read_text())
    manifest['files_sha256'][str(page.relative_to(run))] = hashlib.sha256(page.read_bytes()).hexdigest()
    (run / 'manifest.json').write_text(json.dumps(manifest))
    db = tmp_path / 'portfolio.db'
    assert ledger.ingest_capture(run, db)['totals'] == {'new': 4, 'duplicate': 2, 'conflict': 0, 'skipped': 0}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT execution_time_utc FROM ledger_events WHERE source_record_id='BTCUSDT:7'").fetchone()[0].endswith('.123+00:00')
        assert conn.execute("SELECT count(*) FROM ledger_evidence WHERE event_id=(SELECT event_id FROM ledger_events WHERE source_record_id='BTCUSDT:7')").fetchone()[0] == 2


def test_income_identity_includes_type_and_symbol_for_api_and_csv(tmp_path):
    store = ledger.LedgerStore(tmp_path / 'portfolio.db')
    with store.transaction() as conn:
        ids = []
        for kind in ('FUNDING_FEE', 'COMMISSION'):
            api = ledger.normalize_row(store, conn, 'binance/income', {'tranId': 7, 'incomeType': kind,
                                       'symbol': 'BTCUSDT', 'asset': 'USDT', 'income': '1', 'time': 1783641600000}, {})
            csv = ledger.normalize_row(store, conn, 'binance/income_async', {'Transaction ID': '7', 'type': kind,
                                       'Symbol': 'BTCUSDT', 'Asset': 'USDT', 'Amount': '1', 'Date(UTC)': '2026-07-10 00:00:00'}, {})
            assert ledger.LedgerStore.event_id(api) == ledger.LedgerStore.event_id(csv)
            ids.append(api['source_record_id'])
            assert store.append(conn, api) == 'new'
            assert store.append(conn, csv) == 'duplicate'
        assert len(set(ids)) == 2


def test_salt_rejects_mutation_and_invalid_startup(tmp_path):
    db = tmp_path / 'portfolio.db'
    store = ledger.LedgerStore(db)
    original = store.account_identity('private')
    with sqlite3.connect(db) as conn:
        for statement in ('UPDATE ledger_state SET value=zeroblob(32)', 'DELETE FROM ledger_state'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
        conn.execute('DROP TRIGGER ledger_state_no_update')
        conn.execute("UPDATE ledger_state SET value='bad'")
    with pytest.raises(ValueError, match='IDENTITY_STATE_INVALID'):
        ledger.LedgerStore(db).account_identity('private')
    assert original.startswith('acct_')


def test_repeat_request_skips_raw_evidence_and_coverage_writes(tmp_path, monkeypatch):
    store = ledger.LedgerStore(tmp_path / 'portfolio.db')
    account = store.account_identity('binance:primary')
    args = (store, 'binance/fills', account, [event(account_identity=account)], 1, 2, True, [])
    ledger.preserve_observation(*args)
    with sqlite3.connect(store.db_path) as conn:
        before = [conn.execute('SELECT count(*) FROM ledger_' + table).fetchone()[0] for table in ('events', 'evidence', 'coverage')]
    before_files = list((tmp_path / 'ledger-raw').rglob('*.json'))
    monkeypatch.setattr(ledger.os, 'fsync', lambda _: pytest.fail('Duplicate request wrote raw evidence'))
    assert ledger.preserve_observation(*args)['new'] == 0
    with sqlite3.connect(store.db_path) as conn:
        assert before == [conn.execute('SELECT count(*) FROM ledger_' + table).fetchone()[0] for table in ('events', 'evidence', 'coverage')]
    assert before_files == list((tmp_path / 'ledger-raw').rglob('*.json'))


def test_10k_collection_does_not_starve_concurrent_nav_writer(tmp_path, monkeypatch):
    store = ledger.LedgerStore(tmp_path / 'portfolio.db')
    account = store.account_identity('binance:primary')
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('CREATE TABLE nav_fixture(value TEXT)')
    batch_calls = 0
    original = collect_ledger.canonical
    def bounded(value):
        nonlocal batch_calls
        if isinstance(value, list) and len(value) == 10000:
            batch_calls += 1
            assert batch_calls <= 3, 'Whole batch repeatedly serialized per row'
        return original(value)
    monkeypatch.setattr(collect_ledger, 'canonical', bounded)
    ready, written = threading.Event(), threading.Event()
    failures, holds = [], []
    real_transaction = store.transaction
    @contextmanager
    def tracked():
        with real_transaction() as conn:
            begin = time.monotonic()
            ready.set()
            yield conn
        holds.append(time.monotonic() - begin)
    monkeypatch.setattr(store, 'transaction', tracked)
    def nav():
        ready.wait(5)
        try:
            with sqlite3.connect(store.db_path, timeout=1) as conn:
                conn.execute("INSERT INTO nav_fixture VALUES ('nav-survived')")
            written.set()
        except Exception as exc:
            failures.append(type(exc).__name__)
    worker = threading.Thread(target=nav)
    worker.start()
    rows = [{'id': i, 'symbol': 'BTCUSDT', 'time': 1783641600123, 'qty': '1', 'price': '2', 'side': 'BUY'} for i in range(10000)]
    try:
        run = collect_ledger.CollectionRun(store)
        run.raw('binance/userTrades_BTCUSDT', rows, account, 1, 2)
    finally:
        worker.join(5)
    assert written.is_set() and not failures
    assert max(holds) <= 1
    assert sum(c['new'] for c in run.counts.values()) == 10000


class EmptyBinance:
    max_pages = 40
    identity = {'account_id': 'synthetic'}
    def retention_start(self):
        return 0
    def request(self, *args, **kwargs):
        return []


def test_futures_ledger_failure_leaves_every_existing_field_unchanged():
    baseline = futures.FuturesAccounts(EmptyBinance, None).query('funding', 'binance', '2026-07-10', '2026-07-10')
    def fail(*_):
        raise ValueError('private failure')
    actual = futures.FuturesAccounts(EmptyBinance, None, ledger_observer=fail).query('funding', 'binance', '2026-07-10', '2026-07-10')
    metadata = actual['venues']['binance'].pop('ledger_persistence', None)
    assert actual == baseline
    assert metadata == {'status': 'partial', 'reason_codes': ['LEDGER_WRITE_FAILED']}


def test_equity_ledger_failure_leaves_health_completeness_unchanged():
    baseline = build().collect_transactions('2026-02-01', '2026-02-28')
    collector = build()
    def fail(*_):
        raise ValueError('private failure')
    collector._providers.ledger_observer = fail
    actual = collector.collect_transactions('2026-02-01', '2026-02-28')
    assert actual.failed_sources == baseline.failed_sources
    assert actual.health.keys() == baseline.health.keys()
    for source in baseline.health:
        assert actual.health[source]['status'] == baseline.health[source]['status']
    assert actual.fetch_complete == baseline.fetch_complete
    assert actual.ledger_persistence == {'status': 'partial', 'reason_codes': ['LEDGER_WRITE_FAILED']}


def test_locked_request_returns_within_two_seconds_and_additive_failure(tmp_path, monkeypatch):
    import server
    db = tmp_path / 'portfolio.db'
    ledger.LedgerStore(db).account_identity('binance:primary')
    locked = threading.Event()
    def owner():
        with sqlite3.connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            locked.set()
            time.sleep(2.4)  # Old 30s timeout fails quickly once the fixture releases.
    thread = threading.Thread(target=owner)
    thread.start()
    assert locked.wait(2)
    engine = futures.FuturesAccounts(EmptyBinance, None, ledger_observer=lambda *a: ledger.observe_futures(*a, db_path=db))
    monkeypatch.setattr(server, '_crypto_futures_accounts', engine)
    start = time.monotonic()
    response = server.get_crypto_futures_funding_payments('binance', '2026-07-10', '2026-07-10')
    assert response['ok'] is True
    output = response['data']
    elapsed = time.monotonic() - start
    thread.join(3)
    assert elapsed <= 2.1
    assert output['venues']['binance']['ledger_persistence']['reason_codes'] == ['LEDGER_WRITE_FAILED']
    assert output['result_status'] == 'complete'


def test_hl_stream_identifiers_are_private_across_different_salts(tmp_path):
    from test_collect_ledger import ADDRESS, Binance, Equity, Hyperliquid
    streams = []
    for directory in (tmp_path / 'a', tmp_path / 'b'):
        directory.mkdir()
        store = ledger.LedgerStore(directory / 'portfolio.db')
        collect_ledger.collect(store, '2026-07-12', '2026-07-12', equity=Equity(), binance_factory=Binance, hyperliquid_factory=Hyperliquid)
        with sqlite3.connect(store.db_path) as conn:
            streams.append([row[0] for row in conn.execute("SELECT stream FROM ledger_coverage WHERE source='hyperliquid' ORDER BY stream")])
        public_hash = hashlib.sha256(ADDRESS.encode()).hexdigest()[:16]
        assert public_hash not in str(streams[-1])
    assert streams[0] != streams[1]


def test_equity_tool_keeps_complete_true_and_reports_additive_persistence(tmp_path, monkeypatch):
    import server
    collector = build()
    def fail(*_):
        raise ValueError('private failure')
    collector._providers.ledger_observer = fail
    monkeypatch.setattr(server, '_get_collector', lambda: collector)
    response = server.get_all_transactions('2026-02-01', '2026-02-28', reconcile=False)
    assert response['ok']
    data = response['data']
    assert data['complete'] is True and data['fetch_complete'] is True
    assert data['sources_failed'] == [] and 'ledger:storage' not in data['health']
    assert data['ledger_persistence'] == {'status': 'partial', 'reason_codes': ['LEDGER_WRITE_FAILED']}


def test_hl_capture_page_keys_use_hmac_and_original_source_bytes_stay_unchanged(tmp_path):
    from test_collect_ledger import ADDRESS
    from test_portfolio_ledger import add_file, capture_run
    run = capture_run(tmp_path)
    public = hashlib.sha256(ADDRESS.encode()).hexdigest()[:16]
    stream = 'hyperliquid/account-' + public + '/userFunding'
    original = json.dumps([{'hash': 'hash-1', 'time': 1, 'delta': {'coin': 'BTC', 'usdc': '1'}}]).encode()
    relative = stream + '/1.json'
    add_file(run, relative, original, stream)
    add_file(run, stream + '/1.meta.json', json.dumps({'http_status': 200}).encode())
    db = tmp_path / 'portfolio.db'
    report = ledger.ingest_capture(run, db)
    assert public not in str(report['streams'])
    with sqlite3.connect(db) as conn:
        assert all(public not in row[0] for row in conn.execute("SELECT page_path FROM ledger_evidence"))
        assert all(public not in row[0] for row in conn.execute("SELECT stream FROM ledger_coverage"))
    assert (run / relative).read_bytes() == original
