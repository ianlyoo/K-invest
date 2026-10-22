"""Permanent ledger regressions; synthetic records and private temporary stores."""
import hashlib
import json
import sqlite3

import pytest

from portfolio_ledger import LedgerStore, ingest_capture


def event(**changes):
    return {"source": "binance_fills", "account_identity": "acct_" + "a" * 32,
            "source_record_id": "BTCUSDT:7", "kind": "fill", "symbol": "BTCUSDT",
            "execution_time_utc": "2026-07-10T00:00:00+00:00", "source_timezone": "UTC",
            "timestamp_precision": "millisecond", "side": "buy", "quantity": "1",
            "price_native": "2", "currency": "USDT", "fee": "0.1", "fee_asset": None, **changes}


def test_immutable_events_dedup_conflict_and_enrichment(tmp_path):
    store = LedgerStore(tmp_path / "portfolio.db")
    with store.transaction() as conn:
        assert store.append(conn, event()) == "new"
        assert store.append(conn, event()) == "duplicate"
        assert store.append(conn, event(fee_asset="USDT")) == "duplicate"
        assert store.append(conn, event(price_native="9")) == "conflict"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM ledger_conflicts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM ledger_enrichments").fetchone()[0] == 1
        for sql in ("UPDATE ledger_events SET kind='fee'", "DELETE FROM ledger_events"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_account_identity_is_durable_private_and_shared_store_is_additive(tmp_path):
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE nav_marker(value TEXT)")
        conn.execute("INSERT INTO nav_marker VALUES ('keep')")
    store = LedgerStore(db)
    first = store.account_identity("synthetic-private-account")
    assert first == LedgerStore(db).account_identity("synthetic-private-account")
    assert "synthetic-private-account" not in db.read_bytes().decode(errors="ignore")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM nav_marker").fetchone()[0] == "keep"


def capture_run(tmp_path, body=None, status="queried_window", limitations=()):
    run = tmp_path / "run-synthetic"
    run.mkdir()
    stream = "binance/userTrades_BTCUSDT"
    page = stream + "/1-2-000001.json"
    path = run / page
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body if body is not None else [{"symbol": "BTCUSDT", "id": 7,
        "time": 1783641600000, "side": "BUY", "qty": "1", "price": "2",
        "commission": "0.1", "commissionAsset": "USDT"}]))
    meta = path.with_suffix(".meta.json")
    meta.write_text(json.dumps({"http_status": 200, "params": {"startTime": 1, "endTime": 2}, "reason": None}))
    files = {str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, meta)}
    (run / "manifest.json").write_text(json.dumps({"files_sha256": files, "streams": {stream:
        {"start": 1, "end": 2, "status": status, "gaps": [], "limitations": list(limitations)}}}))
    return run


def test_capture_idempotent_evidence_coverage_and_dry_run(tmp_path):
    run = capture_run(tmp_path)
    db = tmp_path / "portfolio.db"
    assert ingest_capture(run, db, dry_run=True)["totals"]["new"] == 1
    assert not db.exists()
    first = ingest_capture(run, db)
    assert first["totals"]["new"] == 1
    assert ingest_capture(run, db)["totals"] == {"new": 0, "duplicate": 1, "conflict": 0, "skipped": 0}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM ledger_evidence").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM ledger_coverage").fetchone()[0] == "complete"


def test_capture_tampered_page_refused_before_writes(tmp_path):
    run = capture_run(tmp_path)
    next(run.rglob("1-2-000001.json")).write_text('[]')
    with pytest.raises(ValueError, match="CAPTURE_HASH_MISMATCH"):
        ingest_capture(run, tmp_path / "portfolio.db")


def test_limited_empty_history_does_not_verify_zero(tmp_path):
    run = capture_run(tmp_path, body=[], status="limited", limitations=["SYMBOL_DISCOVERY_INCOMPLETE"])
    db = tmp_path / "portfolio.db"
    ingest_capture(run, db)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status,verified_zero,reason_codes_json FROM ledger_coverage").fetchone()
        assert row == ("limited", 0, '["SYMBOL_DISCOVERY_INCOMPLETE"]')


def add_file(run, relative, content, stream=None, limitations=()):
    path = run / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    manifest = json.loads((run / 'manifest.json').read_text())
    manifest['files_sha256'][relative] = hashlib.sha256(content).hexdigest()
    if stream:
        manifest['streams'][stream] = {'start': 1, 'end': 2, 'status': 'limited' if limitations else 'queried_window',
                                       'gaps': [], 'limitations': list(limitations)}
    (run / 'manifest.json').write_text(json.dumps(manifest))


def export_bytes(header, rows):
    import csv
    import io
    import zipfile
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    writer.writerows(rows)
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, 'w') as archive:
        archive.writestr('export.csv', text.getvalue())
    return zipped.getvalue()


def export_fixture(tmp_path, disagreement=False):
    run = capture_run(tmp_path)
    trade_header = ['Uid', 'Time(UTC)', 'Symbol', 'Side', 'Position Side', 'Price', 'Quantity', 'Amount',
                    'Fee', 'Realized Profit', 'Buyer', 'Maker', 'Trade Id', 'Order Id']
    trades = [['private-synthetic-uid', '2026-07-10 00:00:00', 'BTCUSDT', 'BUY', 'BOTH',
               '9' if disagreement else '2', '1', '2', '0.10', '0', 'true', 'false', '7', '70'],
              ['private-synthetic-uid', '2026-07-10 00:00:00', 'BTCUSDT', 'SELL', 'BOTH', '2', '1', '2', '0.2', '1', 'false', 'false', '8', '80']]
    add_file(run, 'binance/trade_async/export.bin', export_bytes(trade_header, trades), 'binance/trade_async',
             ['EXPORT_CONTENT_COVERAGE_UNVERIFIED'])
    income = [{'time': 1783641600000, 'tranId': 11, 'incomeType': 'FUNDING_FEE', 'income': '-0.25', 'asset': 'USDT', 'symbol': 'BTCUSDT'}]
    add_file(run, 'binance/income/1-2-000002.json', json.dumps(income).encode(), 'binance/income')
    add_file(run, 'binance/income/1-2-000002.meta.json', json.dumps({'http_status': 200}).encode())
    header = ['Date(UTC)', 'type', 'Amount', 'Asset', 'Symbol', 'Transaction ID']
    rows = [['2026-07-10 00:00:00', 'FUNDING_FEE', '-0.250', 'USDT', 'BTCUSDT', '11'],
            ['2026-07-10 00:00:00', 'COMMISSION', '-0.1', 'USDT', 'BTCUSDT', '12']]
    add_file(run, 'binance/income_async/export.bin', export_bytes(header, rows), 'binance/income_async',
             ['EXPORT_CONTENT_COVERAGE_UNVERIFIED'])
    return run


def test_csv_api_overlap_dedup_unknown_fee_asset_and_private_uid(tmp_path):
    run = export_fixture(tmp_path)
    db = tmp_path / 'portfolio.db'
    report = ingest_capture(run, db)
    assert report['totals'] == {'new': 4, 'duplicate': 2, 'conflict': 0, 'skipped': 0}
    assert ingest_capture(run, db)['totals'] == {'new': 0, 'duplicate': 6, 'conflict': 0, 'skipped': 0}
    with sqlite3.connect(db) as conn:
        fees = dict(conn.execute("SELECT source_record_id,fee_asset FROM ledger_events WHERE kind='fill'"))
        assert fees == {'BTCUSDT:7': 'USDT', 'BTCUSDT:8': None}
        assert conn.execute('SELECT count(*) FROM ledger_evidence').fetchone()[0] == 6
        assert conn.execute('SELECT count(*) FROM ledger_coverage').fetchone()[0] == 4
    assert 'private-synthetic-uid' not in db.read_bytes().decode(errors='ignore')
    assert report['streams']['binance/trade_async']['coverage']['status'] == 'limited'


def test_csv_disagreement_never_overwrites_api_trade(tmp_path):
    run = export_fixture(tmp_path, disagreement=True)
    db = tmp_path / 'portfolio.db'
    assert ingest_capture(run, db)['totals']['conflict'] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT price_native FROM ledger_events WHERE source_record_id='BTCUSDT:7'").fetchone()[0] == '2'
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == 1


def test_manifest_without_actual_pages_is_not_verified_zero(tmp_path):
    run = capture_run(tmp_path)
    manifest = json.loads((run / 'manifest.json').read_text())
    manifest['files_sha256'] = {}
    (run / 'manifest.json').write_text(json.dumps(manifest))
    db = tmp_path / 'portfolio.db'
    ingest_capture(run, db)
    with sqlite3.connect(db) as conn:
        status, zero, reasons = conn.execute('SELECT status,verified_zero,reason_codes_json FROM ledger_coverage').fetchone()
        assert status == 'partial' and zero == 0 and 'RAW_EVIDENCE_UNAVAILABLE' in reasons


def test_transfer_reclassification_is_separate_append_only_history(tmp_path):
    store = LedgerStore(tmp_path / 'portfolio.db')
    transfer = event(kind='deposit')
    with store.transaction() as conn:
        store.append(conn, transfer)
        key = store.event_id(transfer)
        before = conn.execute('SELECT payload_json FROM ledger_events').fetchone()[0]
        store.classify_transfer(conn, key, 'internal', 'MATCHED_OTHER_PORTFOLIO_ACCOUNT')
        store.classify_transfer(conn, key, 'external_in', 'COUNTERPARTY_OUTSIDE_PORTFOLIO')
        assert conn.execute('SELECT payload_json FROM ledger_events').fetchone()[0] == before
        assert [r[0] for r in conn.execute('SELECT classification FROM ledger_transfer_classifications ORDER BY revision')] == ['unknown', 'internal', 'external_in']
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM ledger_transfer_classifications')


def test_missing_native_id_keeps_evidence_identity_without_claiming_dedup(tmp_path):
    run = capture_run(tmp_path, body=[{'symbol': 'BTCUSDT', 'time': 1783641600000, 'qty': '1', 'price': '2'}])
    db = tmp_path / 'portfolio.db'
    first = ingest_capture(run, db)
    assert first['totals']['new'] == 1
    assert ingest_capture(run, db)['totals']['new'] == 0
    coverage = first['streams']['binance/userTrades_BTCUSDT']['coverage']
    assert coverage['status'] == 'limited' and 'SOURCE_RECORD_ID_UNAVAILABLE' in coverage['reason_codes']


def test_hyperliquid_flows_keep_private_counterparties_and_unknown_boundary(tmp_path):
    from portfolio_ledger import normalize_row
    address = '0x' + 'a' * 40
    store = LedgerStore(tmp_path / 'portfolio.db')
    stream = 'hyperliquid/account-' + hashlib.sha256(address.encode()).hexdigest()[:16] + '/userNonFundingLedgerUpdates'
    row = {'hash': 'tx-hash', 'time': 1, 'delta': {'type': 'deposit', 'usdc': '50', 'user': address}}
    with store.transaction() as conn:
        normalized = normalize_row(store, conn, stream, row, {})
        store.append(conn, normalized)
        assert normalized['origin_account_identity'] == normalized['account_identity']
        assert conn.execute('SELECT classification FROM ledger_transfer_classifications').fetchone()[0] == 'unknown'
    assert address not in store.db_path.read_bytes().decode(errors='ignore')


def test_statement_rows_survive_source_undo_without_mutating_statement_store(tmp_path):
    from portfolio_ledger import ingest_statements
    from portfolio_model import Transaction, TxType
    from statement_import import StatementStore
    db = tmp_path / 'portfolio.db'
    statements = StatementStore(db)
    tx = Transaction('toss', 'toss:primary', 'statement-1', TxType.BUY, '2026-07-10', 'USD',
                     quantity=1, price=2, gross_amount=2, fee_status='unknown', tax_status='unknown')
    # Confirmed store schema is exercised directly with synthetic payloads.
    from dataclasses import asdict
    with sqlite3.connect(db) as conn:
        from statement_import import _SCHEMA
        conn.executescript(_SCHEMA)
        conn.execute('INSERT INTO statement_imports VALUES (?,?,?,?,0)', ('imp-1', 'b' * 64, 'toss:primary', '{}'))
        conn.execute('INSERT INTO statement_rows VALUES (?,?,?,?,?)', ('row-1', 'imp-1', 'toss:primary', '2026-07-10', json.dumps({**asdict(tx), 'tx_type': 'BUY'})))
    store = LedgerStore(db)
    with store.transaction() as conn:
        assert ingest_statements(store, conn)['new'] == 1
        assert ingest_statements(store, conn)['new'] == 0
    statements.undo('imp-1')
    with store.transaction() as conn:
        assert ingest_statements(store, conn)['new'] == 0
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 1
        assert conn.execute('SELECT undone FROM statement_imports').fetchone()[0] == 1
        assert conn.execute('SELECT source_undone FROM ledger_statement_revisions ORDER BY revision DESC LIMIT 1').fetchone()[0] == 1


def test_readthrough_observation_uses_stable_hl_identity_across_process_mask_rotation(tmp_path, monkeypatch):
    import crypto_futures_accounts as futures
    from portfolio_ledger import observe_futures
    address = '0x' + 'a' * 40
    client = type('Client', (), {'_addresses': [address]})()
    db = tmp_path / 'portfolio.db'
    for _ in range(2):
        masked = futures._identity('hyperliquid', address)['account_id']
        output = {'funding_payments': [{'account_id': masked, 'payment_id': 'h', 'symbol': 'BTC', 'time': 1, 'amount': '1'}],
                  'coverage': {'requested_window': {'start_time_ms': 0, 'end_time_ms': 2}, 'fetch_complete': True}, 'reason_codes': []}
        observe_futures('funding', 'hyperliquid', client, output, db)
        monkeypatch.setattr(futures, '_PRIVACY', futures._Privacy(b'new-synthetic-salt-for-next-process', []))
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 1
    assert address not in db.read_bytes().decode(errors='ignore')


def test_direct_toss_history_and_fanout_share_native_identity(tmp_path):
    from portfolio_ledger import observe_broker_history, observe_collection
    from portfolio_model import toss_transactions
    from portfolio_sources import Collection, PortfolioCollector
    rows = [{'orderId': 'order-1', 'orderedAt': '2026-07-10T09:00:00+09:00', 'side': 'BUY',
             'symbol': 'AAPL', 'currency': 'USD', 'execution': {'filledQuantity': 1,
             'averageFilledPrice': 2, 'filledAt': '2026-07-10T10:00:00+09:00'}}]
    db = tmp_path / 'portfolio.db'
    observe_broker_history('toss', 'toss:private-registry-account', 'toss:primary', '', rows, {'complete': True}, db)
    transactions = PortfolioCollector._journal_identity(toss_transactions(rows, 'toss:primary'), 'toss:private-registry-account')
    observe_collection(Collection(transactions=transactions, source_metadata={'toss:primary:orders': {'complete': True}}), db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 1
    assert 'private-registry-account' not in db.read_bytes().decode(errors='ignore')


def test_bad_export_header_is_atomic_and_never_creates_events(tmp_path):
    run = capture_run(tmp_path)
    add_file(run, 'binance/trade_async/export.bin', export_bytes(['wrong'], [['row']]), 'binance/trade_async')
    db = tmp_path / 'portfolio.db'
    with pytest.raises(ValueError, match='EXPORT_HEADER_UNAVAILABLE'):
        ingest_capture(run, db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 0


def test_ingest_cli_dry_run_prints_only_counts_and_no_rows(tmp_path, capsys, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('ledger_ingest_capture', Path(__file__).parents[1] / 'scripts/ledger_ingest_capture.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.os, 'umask', lambda _: None)
    run = export_fixture(tmp_path)
    db = tmp_path / 'portfolio.db'
    assert cli.main(['--run', str(run), '--db', str(db), '--dry-run']) == 0
    output = capsys.readouterr().out
    assert 'BTCUSDT' not in output and 'private-synthetic-uid' not in output
    assert json.loads(output)['totals']['new'] == 4
    assert not db.exists()


def test_new_database_mode_is_private(tmp_path):
    db = tmp_path / 'portfolio.db'
    LedgerStore(db).account_identity('synthetic')
    assert db.stat().st_mode & 0o777 == 0o600


def test_verified_alias_binding_dedups_prior_provisional_events_and_empty_coverage(tmp_path):
    store = LedgerStore(tmp_path / 'portfolio.db')
    old = store.account_identity('toss:primary')
    canonical = store.account_identity('toss:private-canonical-account')
    with store.transaction() as conn:
        store.append(conn, event(account_identity=old))
        store.bind_account(conn, 'toss:primary', 'toss:private-canonical-account')
        assert store.account_identity('toss:primary', conn) == canonical
        assert store.append(conn, event(account_identity=canonical)) == 'duplicate'
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 1
        with pytest.raises(ValueError, match='ACCOUNT_BINDING_CONFLICT'):
            store.bind_account(conn, 'toss:primary', 'toss:different-canonical-account')


def test_internal_timing_preserves_legacy_statement_shape_hashes_and_equality():
    from dataclasses import replace

    from portfolio_model import Transaction, TxType
    from statement_import import _payload
    original = Transaction('toss', 'toss:primary', '1', TxType.BUY, '2026-07-10', 'USD', quantity=1, price=2)
    timed = replace(original, execution_time_utc='2026-07-10T01:00:00.000+00:00', execution_source_timezone='UTC+09:00')
    assert original == timed
    assert _payload(original) == _payload(timed)
    assert 'execution_time_utc' not in _payload(timed)
    assert original.to_dict() == timed.to_dict()


def test_whole_second_time_under_a_finer_label_matches_its_sub_second_value(tmp_path):
    """C9: Binance income `time` arrived truncated to whole seconds until 2026-10-12, then with milliseconds."""
    store = LedgerStore(tmp_path / "portfolio.db")
    with store.transaction() as conn:
        assert store.append(conn, event(execution_time_utc="2026-10-05T07:24:37.000+00:00")) == "new"
        assert store.append(conn, event(execution_time_utc="2026-10-05T07:24:37.847+00:00")) == "duplicate"
        assert store.append(conn, event(execution_time_utc="2026-10-05T07:24:38.000+00:00")) == "conflict"
        assert store.append(conn, event(source_record_id="BTCUSDT:8", execution_time_utc="2026-10-05T07:24:37.100+00:00")) == "new"
        assert store.append(conn, event(source_record_id="BTCUSDT:8", execution_time_utc="2026-10-05T07:24:37.200+00:00")) == "conflict"
