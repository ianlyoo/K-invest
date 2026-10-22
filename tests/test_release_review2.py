"""Shared request budgets and strict CSV validation; synthetic inputs only."""
import io
import json
import sqlite3
import time
import zipfile
from types import SimpleNamespace

import pytest
from test_portfolio_ledger import add_file, export_bytes, export_fixture

import portfolio_ledger as ledger


@pytest.mark.parametrize('provider', ['kis', 'toss'])
def test_history_two_accounts_share_two_second_persistence_budget(tmp_path, monkeypatch, provider):
    import server
    db = tmp_path / 'portfolio.db'
    ledger.LedgerStore(db).account_identity('synthetic')
    lock = sqlite3.connect(db)
    lock.execute('BEGIN IMMEDIATE')
    class Client:
        supports_overseas = True
        journal_account_key = 'synthetic'
        def get_domestic_trade_history(self, *_):
            return {'trades': [], 'truncated': False}
        get_overseas_trade_history = get_domestic_trade_history
        def get_trade_history(self, **_):
            return []
    registry = SimpleNamespace(account_labels=lambda: ['first', 'second'],
                               client_for=lambda _: Client(), default_label=lambda: 'first')
    monkeypatch.setattr(server, f'_get_{provider}_registry', lambda: registry)
    monkeypatch.setattr(server, '_get_nav_store', lambda: SimpleNamespace(db_path=db))
    try:
        start = time.monotonic()
        if provider == 'kis':
            response = server.get_kis_trade_history('20260710', '20260710', 'all')
        else:
            response = server.get_toss_trade_history(account='all')
        elapsed = time.monotonic() - start
    finally:
        lock.rollback()
        lock.close()
    assert elapsed <= 2.1
    assert response['ok'] and response['meta']['complete'] is True
    assert len(response['meta']['sources']) == (4 if provider == 'kis' else 2)
    for source in response['meta']['sources']:
        assert source['ledger_persistence'] == {'status': 'partial', 'reason_codes': ['LEDGER_WRITE_FAILED']}


@pytest.mark.parametrize('malformed', ['quote', 'short', 'long', 'duplicate_header'])
def test_malformed_export_is_refused_before_any_event_write(tmp_path, capsys, malformed):
    from scripts.ledger_ingest_capture import main
    run = export_fixture(tmp_path)
    header = 'Trade Id,Symbol,Time(UTC),Quantity,Price\n'
    valid = '8,BTCUSDT,2026-07-10 00:00:00,1,2\n'
    rows = {
        'quote': '7,BTCUSDT,2026-07-10 00:00:00,1,"2\n' + valid,
        'short': '7,BTCUSDT,2026-07-10 00:00:00,1\n',
        'long': '7,BTCUSDT,2026-07-10 00:00:00,1,2,extra\n',
        'duplicate_header': valid,
    }
    if malformed == 'duplicate_header':
        header = 'Trade Id,Symbol,Time(UTC),Quantity,Price,Price\n'
        rows[malformed] = valid.rstrip() + ',3\n'
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        archive.writestr('trades.csv', header + valid + rows[malformed])
    add_file(run, 'binance/trade_async/export.bin', body.getvalue(), 'binance/trade_async')
    db = tmp_path / 'portfolio.db'
    result = main(['--run', str(run), '--db', str(db)])
    assert result != 0
    assert json.loads(capsys.readouterr().out) == {'error_code': 'LEDGER_CAPTURE_INGEST_FAILED'}
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 0


def test_blank_required_value_rows_are_skipped_with_reason_not_refused(tmp_path, capsys):
    # Real Binance income exports contain well-formed rows with an empty type.
    from scripts.ledger_ingest_capture import main
    run = export_fixture(tmp_path)
    header = ['Date(UTC)', 'type', 'Amount', 'Asset', 'Symbol', 'Transaction ID']
    rows = [['2026-07-10 00:00:00', 'FUNDING_FEE', '-0.250', 'USDT', 'BTCUSDT', '11'],
            ['2026-07-10 00:00:00', '', '-0.1', 'USDT', 'BTCUSDT', '12'],
            ['2026-07-10 00:00:00', 'COMMISSION', '-0.1', 'USDT', 'BTCUSDT', ' ']]
    add_file(run, 'binance/income_async/export.bin', export_bytes(header, rows), 'binance/income_async',
             ['EXPORT_CONTENT_COVERAGE_UNVERIFIED'])
    db = tmp_path / 'portfolio.db'
    assert main(['--run', str(run), '--db', str(db)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['totals']['skipped'] == 2 and report['totals']['conflict'] == 0
    skipped = [row for row in report['streams'].values() if row['skipped']]
    assert len(skipped) == 1
    assert skipped[0]['coverage']['status'] == 'partial'
    assert 'EXPORT_ROW_REQUIRED_FIELD_MISSING' in skipped[0]['coverage']['reason_codes']
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM ledger_events WHERE source_record_id LIKE '%12%'").fetchone()[0] == 0
