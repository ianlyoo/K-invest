"""Append-only ledger, release initialization, bounded intervals and status ordering."""
import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_crypto_flow_intervals import (
    T0,
    T1,
    coverage,
    identities,
    ms,
    result,
    setup,
    snapshot,
)
from test_crypto_review_pass7 import legacy_tables
from test_crypto_review_pass9 import transfer_facts

import crypto_flow_evidence as evidence
import crypto_flow_intervals as intervals
import portfolio_ledger as ledger

ROOT = Path(__file__).parents[1]


def test_schema_entry_point_only_creates_new_objects_and_is_idempotent(tmp_path, monkeypatch):
    store = setup(tmp_path)
    old_rows = legacy_tables(store)
    with store.read() as conn:
        conn.execute('DROP TABLE crypto_flow_windows')
        before = list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
    connect, statements = sqlite3.connect, []

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(ledger.sqlite3, 'connect', traced)
        for _ in range(2):
            ledger.ensure_crypto_flow_schema(store.db_path)
    assert len(statements) == 12  # One table, two indexes, three triggers each run.
    assert all(sql.strip().startswith(('CREATE TABLE IF NOT EXISTS crypto_flow_windows',
        'CREATE INDEX IF NOT EXISTS crypto_flow_windows_',
        'CREATE TRIGGER IF NOT EXISTS crypto_flow_windows_')) for sql in statements)
    assert legacy_tables(store) == old_rows
    with connect(store.db_path) as conn:
        after = list(conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name'))
    assert [tuple(row) for row in before] == [row for row in after if 'crypto_flow_windows' not in row[1]]


def test_schema_cli_creates_no_legacy_objects_or_data(tmp_path):
    path = tmp_path / 'existing.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE existing_data(value TEXT)')
        conn.execute("INSERT INTO existing_data VALUES ('preserve')")
    for _ in range(2):
        done = subprocess.run([sys.executable, '-m', 'portfolio_ledger', '--ensure-schema', str(path)],
            cwd=ROOT, env={'PATH': os.environ.get('PATH', ''), 'TMPDIR': str(tmp_path),
                'PYTHONDONTWRITEBYTECODE': '1', 'KINVEST_CACHE_DIR': str(tmp_path / 'cache')},
            capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, done.stderr
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM existing_data').fetchall() == [('preserve',)]
        assert conn.execute('SELECT count(*) FROM crypto_flow_windows').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'ledger_%'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'crypto_flow_windows%'").fetchone()[0] == 6


def test_schema_entry_point_rejects_missing_path_without_creating_database(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        ledger.ensure_crypto_flow_schema(path)
    assert not path.exists()


def test_365_day_range_budgets_each_actual_interval_in_one_read_snapshot(tmp_path, monkeypatch):
    store = ledger.LedgerStore(tmp_path / 'year.db')
    accounts = [identities(store)['hyperliquid'], store.hl_identity('0x' + 'c' * 40)]
    for day in range(366):
        stamp = T0 + timedelta(days=day)
        snapshot(store, stamp, 'day-' + str(day), hl_accounts=accounts)
        if day:  # each daily fetch ends just after its capture, through the coverage pad
            coverage(store, start=stamp - timedelta(days=10), end=stamp + intervals.EVIDENCE_PAD, hl_accounts=accounts)
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM crypto_flow_windows').fetchone()[0] == 3650
    original, reads = intervals.read_view, []

    def read(conn, path, aliases, lo, hi):
        assert conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0] == 1
        events, proofs = original(conn, path, aliases, lo, hi)
        reads.append((id(conn), lo, hi, len(proofs)))
        return events, proofs

    monkeypatch.setattr(intervals, 'read_view', read)
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    rows = intervals.crypto_interval_flows(store.db_path, T0, T0 + timedelta(days=365))
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    assert len(reads) == 365 and len({r[0] for r in reads}) == 1
    # Each read is one actual interval plus the evidence pad on both sides; it also
    # selects the ten windows that end just after the interval start.
    assert all(hi - lo == timedelta(days=1) + 2 * intervals.EVIDENCE_PAD and 10 <= count <= 120
               for _, lo, hi, count in reads)
    assert len(rows) == 730
    for venue, selected in [('binance', rows[:365]), ('hyperliquid', rows[365:])]:
        assert all(row['source'] == venue and row['flow_native'] == row['flow_krw'] == '0'
            and row['confidence'] == 'observed' and row['reasons'] == [] for row in selected)
        assert [row['start_snapshot_id'] for row in selected] == ['day-' + str(day) for day in range(365)]
        assert selected[-1]['end_snapshot_id'] == 'day-365'


def observe(store, facts, source, status, when, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return when  # Preserve offsets to test chronological, not text, order.

    rows = [{**fact, 'source_status': status} for fact in facts]
    with monkeypatch.context() as patch:
        patch.setattr(ledger if source == 'legacy' else evidence, 'datetime', Clock)
        if source == 'legacy':
            store.append_many([(row, None) for row in rows])
        else:
            evidence.FlowEvidence(store).save('binance', 'transfer_MAIN_UMFUTURE', rows,
                facts[0]['account_identity'], ms(T0), ms(T1))


@pytest.mark.parametrize('reverse_write_order', [False, True])
@pytest.mark.parametrize('older,newer,old_status,new_status', [
    ('legacy', 'strict', 'CONFIRMED', 'PENDING'),
    ('strict', 'strict', 'CONFIRMED', 'PENDING'),
    ('strict', 'legacy', 'PENDING', 'CONFIRMED'),
    ('legacy', 'strict', 'PENDING', 'CONFIRMED'),
    ('strict', 'legacy', 'CONFIRMED', 'PENDING'),
])
def test_newest_observation_wins_even_when_older_status_is_final(
        tmp_path, monkeypatch, reverse_write_order, older, newer, old_status, new_status):
    store = setup(tmp_path)
    facts = transfer_facts(store)
    observations = [(older, old_status, datetime.fromisoformat('2026-10-14T12:00:00+09:00')),
                    (newer, new_status, datetime.fromisoformat('2026-10-14T04:00:00+00:00'))]
    for source, status, stamp in reversed(observations) if reverse_write_order else observations:
        observe(store, facts, source, status, stamp, monkeypatch)
    row = result(store)[0]
    assert row['flow_native'] == ('7' if new_status == 'CONFIRMED' else None)
    assert ('CRYPTO_PENDING_FLOW_STATUS' in row['reasons']) is (new_status == 'PENDING')
    assert 'LEDGER_CONTENT_CONFLICT' not in row['reasons']


def test_reported_revision_status_and_observation_time_override_sticky_legacy_status(tmp_path, monkeypatch):
    store = setup(tmp_path)
    facts = transfer_facts(store)
    for source, status, stamp in [
        ('legacy', 'CONFIRMED', '2026-10-14T01:00:00+00:00'),
        ('legacy', 'PENDING', '2026-10-14T04:00:00+00:00'),
        ('legacy', 'PROCESSING', '2026-10-14T12:00:00+09:00'),  # Later revision, older observation.
        ('strict', 'CONFIRMED', '2026-10-14T03:30:00+00:00'),
    ]:
        observe(store, facts, source, status, datetime.fromisoformat(stamp), monkeypatch)
    with store.read() as conn:
        assert {r[0] for r in conn.execute('SELECT effective_status FROM ledger_status_revisions')} == {'CONFIRMED'}
        rows, _ = evidence.read_view(conn, store.db_path, {}, T0, T1)
    assert all(row['source_status'] == 'PENDING' for row in rows)
    row = result(store)[0]
    assert row['flow_native'] is None and 'CRYPTO_PENDING_FLOW_STATUS' in row['reasons']


def test_latest_identical_strict_observation_still_wins_over_legacy_status(tmp_path, monkeypatch):
    store = setup(tmp_path)
    facts = transfer_facts(store)
    for source, status, hour in [('strict', 'PENDING', 1), ('legacy', 'CONFIRMED', 2), ('strict', 'PENDING', 3)]:
        observe(store, facts, source, status, T1 + timedelta(hours=hour), monkeypatch)
    row = result(store)[0]
    assert row['flow_native'] is None and 'CRYPTO_PENDING_FLOW_STATUS' in row['reasons']
