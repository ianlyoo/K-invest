"""Approved single-database window schema, transaction and isolation regressions."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_collect_ledger import collect_ledger
from test_crypto_flow_coverage import binance
from test_crypto_flow_intervals import (
    T0,
    T1,
    bn_transfer,
    identities,
    ms,
    result,
    setup,
)

import crypto_flow_evidence as evidence
from portfolio_ledger import LedgerStore, canonical

ROOT = Path(__file__).parents[1]


def windows(store):
    with store.read() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM crypto_flow_windows ORDER BY rowid')]


def legacy_tables(store):
    with store.read() as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ledger_%' ORDER BY name")]
        return {name: list(map(tuple, conn.execute('SELECT * FROM ' + name + ' ORDER BY rowid'))) for name in names}


def failure(store):
    client = binance(lambda request: httpx.Response(200, content=b'{'))
    rows, reasons, failed = collect_ledger.flow_history(client, 'transfer_MAIN_UMFUTURE',
        ms(T0), ms(T1), observed_at_ms=ms(T1))
    assert failed and 'CRYPTO_INVALID_RESPONSE' in reasons
    return rows, reasons, failed


def save_failure(store, fetched):
    rows, reasons, failed = fetched
    return evidence.FlowEvidence(store).raw('binance/transfer_MAIN_UMFUTURE', rows,
        identities(store)['binance'], ms(T0), ms(T1), reasons, failed)


def test_schema_creation_is_idempotent_and_does_not_change_existing_tables(tmp_path):
    store = setup(tmp_path)
    before = legacy_tables(store)
    with store.read() as conn:
        old_ddl = list(map(tuple, conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'ledger_%' ORDER BY name")))
        conn.execute('DROP TABLE crypto_flow_windows')  # Simulate a pre-feature database; no live data.
    for _ in range(2):
        store = LedgerStore(store.db_path)
        with store.read() as conn:
            assert conn.execute('SELECT count(*) FROM crypto_flow_windows').fetchone()[0] == 0
            assert list(map(tuple, conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'ledger_%' ORDER BY name"))) == old_ddl
            assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='index' AND name LIKE 'crypto_flow_windows_%'").fetchone()[0] == 2
            assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'crypto_flow_windows_%'").fetchone()[0] == 3
    assert legacy_tables(store) == before


def test_read_does_not_create_missing_table_or_import_old_files(tmp_path):
    store = setup(tmp_path)
    with store.read() as conn:
        conn.execute('DROP TABLE crypto_flow_windows')
    old = tmp_path / 'crypto-flow-evidence-v1'
    old.mkdir()
    (old / 'index.jsonl').write_text('untrusted obsolete evidence')
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    value = result(store)[0]
    assert value['flow_native'] is None and 'CRYPTO_FLOW_COVERAGE_INCOMPLETE' in value['reasons']
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    assert (old / 'index.jsonl').read_text() == 'untrusted obsolete evidence'


@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE', 'REPLACE', 'REPLACE_ROWID'])
def test_committed_windows_cannot_be_changed_removed_or_replaced(tmp_path, operation):
    store = setup(tmp_path)
    bn_transfer(store, amount='7')
    before = windows(store)
    with store.read() as conn, pytest.raises(sqlite3.IntegrityError, match='immutable'):
        if operation == 'REPLACE_ROWID':
            first = conn.execute('SELECT rowid,* FROM crypto_flow_windows LIMIT 1').fetchone()
            values = list(first)
            values[1] = 'new-window-id-same-rowid'
            conn.execute('INSERT OR REPLACE INTO crypto_flow_windows(rowid,' + ','.join(first.keys()[1:])
                + ') VALUES (' + ','.join('?' for _ in values) + ')', values)
        elif operation == 'REPLACE':
            conn.execute('INSERT OR REPLACE INTO crypto_flow_windows SELECT * FROM crypto_flow_windows LIMIT 1')
        elif operation == 'UPDATE':
            conn.execute("UPDATE crypto_flow_windows SET payload_json='{}'")
        else:
            conn.execute('DELETE FROM crypto_flow_windows')
    assert windows(store) == before
    assert result(store)[0]['flow_native'] == '7'


def test_one_window_uses_one_insert_and_never_writes_a_legacy_table(tmp_path, monkeypatch):
    store = setup(tmp_path)
    before, calls, transaction = legacy_tables(store), [], store.transaction
    @contextmanager
    def traced():
        with transaction() as conn:
            class Proxy:
                def execute(self, sql, params=()):
                    calls.append(sql)
                    return conn.execute(sql, params)
            yield Proxy()
    monkeypatch.setattr(store, 'transaction', traced)
    bn_transfer(store)
    assert len(calls) == 1 and calls[0].startswith('INSERT INTO crypto_flow_windows')
    assert '(window_id,source,account_identity,stream,start_time,end_time,observed_at,' in calls[0]
    assert legacy_tables(store) == before
    assert not (tmp_path / 'crypto-flow-evidence-v1').exists()
    payload = json.loads(windows(store)[-1]['payload_json'])
    assert len(payload['events']) == 2 and payload['status'] == 'complete'
    assert result(store)[0]['flow_native'] == '50'


@pytest.mark.parametrize('stage', ['before_insert', 'after_insert'])
def test_process_crash_before_commit_leaves_no_window_or_partial_proof(tmp_path, stage):
    store = setup(tmp_path)
    before, old = windows(store), legacy_tables(store)
    child = '''import os,sys
from contextlib import contextmanager
from crypto_flow_evidence import FlowEvidence
from portfolio_ledger import LedgerStore
store=LedgerStore(sys.argv[1])
account=store.account_identity('binance:primary')
transaction=store.transaction
@contextmanager
def crash():
    with transaction() as conn:
        if sys.argv[2]=='before_insert': os._exit(23)
        yield conn
        os._exit(23)
store.transaction=crash
FlowEvidence(store).save('binance','transfer_MAIN_UMFUTURE',[],account,sys.argv[3],sys.argv[4],['CRYPTO_INVALID_RESPONSE'],True)
'''
    done = subprocess.run([sys.executable, '-c', child, str(store.db_path), stage, T0.isoformat(), T1.isoformat()],
        cwd=ROOT, env={'PATH': os.environ.get('PATH', ''), 'TMPDIR': str(tmp_path),
                       'PYTHONDONTWRITEBYTECODE': '1', 'KINVEST_CACHE_DIR': str(tmp_path / 'cache')},
        capture_output=True, text=True, timeout=20)
    assert done.returncode == 23, done.stderr
    assert windows(store) == before and legacy_tables(store) == old
    # The prior zero is the only committed proof, not an orphaned new failure.
    assert result(store)[0]['flow_native'] == '0'
    assert not (tmp_path / 'crypto-flow-evidence-v1').exists()


@pytest.mark.parametrize('prior_zero', [False, True])
def test_real_locked_database_leaves_no_facts_or_failure_coverage(tmp_path, prior_zero):
    store = setup(tmp_path)
    if not prior_zero:
        with store.read() as conn:
            conn.execute('DROP TABLE crypto_flow_windows')
        store = LedgerStore(store.db_path)
    fetched = failure(store)
    before, old = windows(store), legacy_tables(store)
    store.busy_timeout = .01
    with sqlite3.connect(store.db_path) as lock:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            save_failure(store, fetched)
        lock.rollback()
    assert windows(store) == before and legacy_tables(store) == old
    value = result(store)[0]
    assert value['flow_native'] == ('0' if prior_zero else None)
    save_failure(store, fetched)
    value = result(store)[0]
    assert value['flow_native'] is None and 'CRYPTO_INVALID_RESPONSE' in value['reasons']


def test_commit_budget_failure_rolls_back_the_already_executed_insert(tmp_path, monkeypatch):
    store = setup(tmp_path)
    fetched, before, budget = failure(store), windows(store), store.budget
    calls = 0
    def expires(conn):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError('LEDGER_WRITE_BUDGET_EXHAUSTED')
        budget(conn)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'budget', expires)
        with pytest.raises(sqlite3.OperationalError, match='WRITE_BUDGET'):
            save_failure(store, fetched)
    assert calls == 2 and windows(store) == before
    assert result(store)[0]['flow_native'] == '0'


def test_partial_page_facts_and_failure_status_commit_together(tmp_path):
    store = setup(tmp_path)
    def response(request):
        if request.url.params['current'] == '1':
            return httpx.Response(200, json={'total': 2, 'rows': [dict(tranId=1, timestamp=ms(T0 + timedelta(minutes=1)),
                type='MAIN_UMFUTURE', asset='USDT', amount='8', status='CONFIRMED')]})
        return httpx.Response(200, content=b'{')
    fetched = collect_ledger.flow_history(binance(response), 'transfer_MAIN_UMFUTURE',
        ms(T0), ms(T1), observed_at_ms=ms(T1), page_size=1)
    assert fetched[0] and fetched[2] and 'CRYPTO_INVALID_RESPONSE' in fetched[1]
    save_failure(store, fetched)
    payload = json.loads(windows(store)[-1]['payload_json'])
    assert payload['status'] == 'partial' and payload['row_count'] == len(payload['events']) == 2
    assert not payload['verified_zero'] and 'CRYPTO_INVALID_RESPONSE' in payload['reasons']
    value = result(store)[0]
    assert value['flow_native'] is None and value['confidence'] == 'unresolved'


@pytest.mark.parametrize('older_first', [False, True])
@pytest.mark.parametrize('alias', [False, True])
def test_older_out_of_window_conflicting_observation_cannot_disappear(tmp_path, older_first, alias):
    store = setup(tmp_path)
    account = identities(store)['binance']
    previous = store.account_identity('binance:previous') if alias else account
    old = dict(tranId=1, timestamp=ms(T0 - timedelta(days=3)), amount='7', asset='USDT',
               type='MAIN_UMFUTURE', status='CONFIRMED')
    def historical():
        with store.read() as conn:
            facts = evidence.normalize(store, conn, 'binance/transfer_MAIN_UMFUTURE', old)
        for fact in facts:
            fact['account_identity'] = previous
        evidence.FlowEvidence(store).save('binance', 'transfer_MAIN_UMFUTURE', facts, previous,
            ms(T0 - timedelta(days=4)), ms(T0 - timedelta(days=2)))
    if older_first:
        historical()
    bn_transfer(store, ident=1, amount='8')
    if not older_first:
        historical()
    if alias:
        with store.transaction() as conn:
            store.bind_account(conn, 'binance:previous', 'binance:primary')
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    value = result(store)[0]
    assert value['flow_native'] is None and 'LEDGER_CONTENT_CONFLICT' in value['reasons']
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    assert not (tmp_path / 'crypto-flow-evidence-v1').exists()
    # A reopened connection still sees all committed identity history.
    assert result(LedgerStore(store.db_path))[0]['confidence'] == 'unresolved'


def test_concurrent_writers_keep_independent_atomic_observations(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    account = store.account_identity('binance:primary')
    def save(_):
        return evidence.FlowEvidence(store).save('binance', 'deposit', [], account, ms(T0), ms(T1))
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(row['status'] == 'complete' for row in pool.map(save, range(8)))
    rows = windows(store)
    assert len(rows) == len({row['window_id'] for row in rows}) == 8
    assert all(json.loads(row['payload_json'])['verified_zero'] for row in rows)
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_coverage').fetchone()[0] == 0


def test_window_index_limits_payload_reads_to_requested_windows(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    account = store.account_identity('binance:primary')
    for day in range(180):
        lo = T0 + timedelta(days=day)
        evidence.FlowEvidence(store).save('binance', 'deposit', [], account, ms(lo), ms(lo + timedelta(hours=12)))
    lo = T0 + timedelta(days=90)
    calls = []
    with store.read() as conn:
        conn.set_trace_callback(calls.append)
        events, coverage = evidence.read_view(conn, store.db_path, {}, lo + timedelta(hours=1), lo + timedelta(hours=2))
        plan = list(conn.execute('EXPLAIN QUERY PLAN SELECT window_id FROM crypto_flow_windows INDEXED BY crypto_flow_windows_time WHERE end_time>? AND start_time<=?',
                                (lo.isoformat(), lo.isoformat())))
    assert not events and len(coverage) == 1 and coverage[0]['status'] == 'complete'
    assert sum(sql.startswith('SELECT payload_json FROM crypto_flow_windows') for sql in calls) == 1
    assert any('SEARCH' in row[3] and 'crypto_flow_windows_time' in row[3] for row in plan)


@pytest.mark.parametrize('field', ['MAX_QUERY_WINDOWS', 'MAX_QUERY_BYTES', 'READ_BUDGET_SECONDS'])
def test_query_budgets_never_return_partial_observed_totals(tmp_path, monkeypatch, field):
    store = setup(tmp_path)
    bn_transfer(store)
    monkeypatch.setattr(evidence, field, 0)
    value = result(store)[0]
    assert value['flow_native'] is None and 'CRYPTO_FLOW_EVIDENCE_UNAVAILABLE' in value['reasons']


def test_row_budget_rejects_whole_observation_before_insert(tmp_path, monkeypatch):
    store = setup(tmp_path)
    before = windows(store)
    monkeypatch.setattr(evidence, 'MAX_BYTES', 1)
    with pytest.raises(ValueError, match='BUDGET_EXHAUSTED'):
        bn_transfer(store)
    assert windows(store) == before


def test_bad_committed_digest_cannot_be_repaired_by_a_newer_complete_window(tmp_path):
    store = setup(tmp_path)
    row = windows(store)[0]
    row['window_id'] = 'synthetic-invalid-digest'
    row['content_sha256'] = '0' * 64
    # Fault injection into the new table only; immutability of existing rows stays enabled.
    with store.transaction() as conn:
        conn.execute('INSERT INTO crypto_flow_windows VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))
    bn_transfer(store)
    value = result(store, row['source'])[0]
    assert value['flow_native'] is None and 'CRYPTO_FLOW_EVIDENCE_UNAVAILABLE' in value['reasons']


def test_read_transaction_observes_old_or_new_window_never_uncommitted_proof(tmp_path):
    store = setup(tmp_path)
    row = windows(store)[0]
    payload = json.loads(row['payload_json'])
    payload.update(status='partial', reasons=['CRYPTO_INVALID_RESPONSE'], verified_zero=False)
    row.update(window_id='synthetic-pending-window', payload_json=canonical(payload),
               content_sha256=hashlib.sha256(canonical(payload).encode()).hexdigest())
    with store.transaction() as writer:
        writer.execute('INSERT INTO crypto_flow_windows VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))
        assert result(store, row['source'])[0]['flow_native'] == '0'
    value = result(store, row['source'])[0]
    assert value['flow_native'] is None and 'CRYPTO_INVALID_RESPONSE' in value['reasons']
