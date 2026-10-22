"""Run the actual cbbb6bf store against synthetic databases migrated by d46c564."""
import hashlib
import inspect
import sqlite3
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from portfolio_store import NavStore


@pytest.fixture
def old_store():
    source = (Path(__file__).parent / 'fixtures/legacy_portfolio_store.py').read_text()
    assert hashlib.sha256(source.encode()).hexdigest() == 'e1f6b6e50858297c6c9b623765df3ba0f941707fabe5f8ce48300652fc269446'
    module = ModuleType('cbbb6bf_portfolio_store')
    exec(compile(source, 'cbbb6bf:portfolio_store.py', 'exec'), module.__dict__)
    return module.NavStore


def test_legacy_store_fixture_is_available_in_release_archives_without_git(monkeypatch):
    def no_git(*args, **kwargs):
        raise RuntimeError('release archives have no Git object database')
    monkeypatch.setattr(subprocess, 'check_output', no_git)
    assert old_store.__wrapped__().__name__ == 'NavStore'


def test_cbbb6bf_reads_and_writes_migrated_database_without_lost_daily_rows(tmp_path, old_store):
    path = tmp_path / 'nav.db'
    new = NavStore(path)
    versions = [new.upsert_snapshot({'total_assets_krw': value}, day)
                for day, value in [('2026-10-01', 100), ('2026-10-02', 110)]]
    old = old_store(path)
    assert old.count() == 2
    assert [row['total_value_krw'] for row in old.range('2026-10-01', '2026-10-03')] == [100, 110]
    assert old.upsert_snapshot({'total_assets_krw': 120}, '2026-10-02')['total_value_krw'] == 120
    old.upsert_snapshot({'total_assets_krw': 130}, '2026-10-03')
    assert old.count() == 3 and old.bounds() == ('2026-10-01', '2026-10-03')
    assert [row['total_value_krw'] for row in new.range('2026-10-01', '2026-10-03')] == [100, 120, 130]
    for version in versions:
        assert new.select_snapshots([version['snapshot_id']]) == [version]
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM nav_snapshots').fetchone()[0] == 3


def test_cbbb6bf_upserts_are_imported_once_and_representatives_remain_consistent(tmp_path, old_store):
    path = tmp_path / 'nav.db'
    new, old = NavStore(path), old_store(path)
    first = new.upsert_snapshot({'total_assets_krw': 100}, '2026-10-02')
    previous = first
    for expected_version, total in enumerate([120, 140, 160], 2):
        old.upsert_snapshot({'total_assets_krw': total}, '2026-10-02')
        representative = new.get_snapshot('2026-10-02')
        assert representative['version'] == expected_version
        assert representative['total_value_krw'] == total
        assert representative['calculation_version'] == 'legacy_unknown'
        assert new.get_snapshot('2026-10-02') == representative
        assert new.select_snapshots([previous['snapshot_id']]) == [previous]
        previous = representative
    latest = new.upsert_snapshot({'total_assets_krw': 180}, '2026-10-02')
    assert latest['version'] == 5 and old.get_snapshot('2026-10-02')['total_value_krw'] == 180
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM nav_snapshot_versions').fetchone()[0] == 5
        assert conn.execute('SELECT snapshot_id FROM nav_snapshot_representatives').fetchone()[0] == latest['snapshot_id']
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_summary_snapshot_default_off_and_optional_write_is_disclosed():
    import asyncio

    import server
    assert inspect.signature(server.get_portfolio_summary).parameters['snapshot'].default is False
    tool = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == 'get_portfolio_summary')
    # The MCP tool has no snapshot argument and never writes; snapshot_portfolio_nav writes.
    assert 'snapshot' not in tool.inputSchema['properties']
    assert 'Never writes a NAV snapshot' in tool.description
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
