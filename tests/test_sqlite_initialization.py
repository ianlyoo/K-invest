"""Fresh synthetic SQLite files; deterministic WAL-switch lock contention."""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from test_snapshot_versions import summary

from portfolio_store import NavStore


def test_first_connections_serialize_exclusive_wal_switch(tmp_path, monkeypatch):
    connect = sqlite3.connect
    exclusive = Lock()
    switches = []

    class ContendedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.upper().replace(" ", "") == "PRAGMAJOURNAL_MODE=WAL":
                # Model SQLite's mode-switch exclusive lock, whose busy failure
                # can be immediate even when the connection has a busy timeout.
                if not exclusive.acquire(blocking=False):
                    raise sqlite3.OperationalError("database is locked")
                try:
                    switches.append(sql)
                    time.sleep(.02)
                    return super().execute(sql, *args, **kwargs)
                finally:
                    exclusive.release()
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=ContendedConnection, **kw))
    store = NavStore(tmp_path / "fresh.db")
    start = Barrier(8)

    def write(i):
        start.wait(timeout=5)
        return store.upsert_snapshot(summary(total=100+i), "2026-10-02")

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(write, range(8)))
    assert sorted(row["version"] for row in rows) == list(range(1, 9))
    assert len(switches) == 1


def test_existing_wal_database_does_not_request_mode_switch(tmp_path, monkeypatch):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    connect = sqlite3.connect

    class NoSwitchConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            assert sql.upper().replace(" ", "") != "PRAGMAJOURNAL_MODE=WAL"
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=NoSwitchConnection, **kw))
    store = NavStore(path)
    assert store.upsert_snapshot(summary(), "2026-10-02")["version"] == 1


@pytest.mark.parametrize("message,expected_calls", [("database is locked", 3), ("disk I/O error", 1)])
def test_wal_retry_is_bounded_and_does_not_hide_other_errors(tmp_path, monkeypatch, message, expected_calls):
    import portfolio_store
    store = NavStore(tmp_path / "bounded.db")
    calls = []
    monkeypatch.setattr(portfolio_store.time, "sleep", lambda _: None)

    class BrokenConnection:
        def execute(self, sql):
            calls.append(sql)
            raise sqlite3.OperationalError(message)

    with pytest.raises(sqlite3.OperationalError, match=message):
        store._ensure_wal(BrokenConnection())
    assert len(calls) == expected_calls


def test_repeated_fresh_databases_with_multiple_store_instances(tmp_path):
    for trial in range(12):
        path = tmp_path / f"fresh-{trial}.db"
        stores = [NavStore(path) for _ in range(8)]
        start = Barrier(8)

        def write(i):
            start.wait(timeout=5)
            return stores[i].upsert_snapshot(summary(total=100+i), "2026-10-02")

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(write, range(8)))
        assert sorted(row["version"] for row in rows) == list(range(1, 9))
