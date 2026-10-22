"""Page identity and safe diagnostics using synthetic owner statements only."""
import importlib
import json

import pytest
from test_owner_flows import HEADER_TEXT, activity, module, pdf, records
from test_statement_import import synthetic_pdf

from portfolio_ledger import LedgerStore


@pytest.mark.parametrize('first,second,kinds', [
    ('주식병합출고', '이체입금', ['corporate_action', 'deposit']),
    ('배당금입금', '이체입금', ['income_other', 'deposit']),
    ('이체입금', '이체출금', ['deposit', 'withdrawal']),
    ('이체입금', '이체입금', ['deposit', 'deposit']),
])
def test_toss_same_line_on_different_pages_is_distinct(tmp_path, first, second, kinds):
    file = tmp_path / 'multipage.pdf'
    file.write_bytes(synthetic_pdf([HEADER_TEXT + '\n' + activity(k) for k in (first, second)]))
    parsed = module().TossFlowParser().parse(file.read_bytes())
    assert len({r['line'] for r in parsed.rows}) == 1
    assert {r['page'] for r in parsed.rows} == {1, 2}
    db = tmp_path / 'ledger.db'
    result = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    store = LedgerStore(db)
    events = records(store)
    assert sorted(e['kind'] for e in events) == sorted(kinds)
    assert result['duplicate'] == 0
    assert sorted(e['classification'] for e in events if e['kind'] == 'deposit') == ['external_in'] * kinds.count('deposit')
    if 'withdrawal' in kinds:
        assert next(e['classification'] for e in events if e['kind'] == 'withdrawal') == 'external_out'
    with store.read() as conn:
        assert conn.execute('SELECT count(DISTINCT event_id) FROM ledger_owner_export_rows').fetchone()[0] == 2
        assert conn.execute('SELECT row_count FROM ledger_coverage').fetchone()[0] == 2
        before = '\n'.join(conn.iterdump())
    assert all(v == 0 for v in module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321').values())
    with store.read() as conn:
        assert '\n'.join(conn.iterdump()) == before


@pytest.mark.parametrize('field,value', [('kind', 'corporate_action'), ('source', 'toss_other')])
def test_owner_stored_event_mismatch_is_specific_and_atomic(tmp_path, monkeypatch, field, value):
    file = pdf(tmp_path, activity('이체입금'))
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read():
        pass
    prepare = LedgerStore.prepare

    def mismatched(self, conn, event):
        return prepare(self, conn, {**event, field: value})

    monkeypatch.setattr(LedgerStore, 'prepare', mismatched)
    with pytest.raises(ValueError, match='^OWNER_EXPORT_EVENT_IDENTITY_MISMATCH$'):
        module().ingest_owner_export('toss_pdf', file, store.db_path, account_suffix='4321')
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 0
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ledger_owner_export_imports'").fetchone()


@pytest.mark.parametrize('message,expected', [
    ('OWNER_EXPORT_EVENT_IDENTITY_MISMATCH', 'OWNER_EXPORT_EVENT_IDENTITY_MISMATCH'),
    ('ACCOUNT_SUFFIX_MISMATCH', 'ACCOUNT_SUFFIX_MISMATCH'),
    ('A_B', 'A_B'),
    ('A' * 81, 'A' * 81),
    ('bad 가상고객 2026.01.01 50', 'OWNER_EXPORT_INGEST_FAILED'),
    ('BAD\n', 'OWNER_EXPORT_INGEST_FAILED'),
    ('AB', 'OWNER_EXPORT_INGEST_FAILED'),
    ('A' * 82, 'OWNER_EXPORT_INGEST_FAILED'),
])
def test_owner_cli_error_code_is_bounded(tmp_path, monkeypatch, capsys, message, expected):
    cli = importlib.import_module('scripts.ledger_ingest_owner_export')

    def fail(*args, **kwargs):
        raise ValueError(message)

    monkeypatch.setattr(cli, 'ingest_owner_export', fail)
    assert cli.main(['--kind', 'toss_pdf', '--file', 'synthetic.pdf', '--db', str(tmp_path / 'db')]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {'error_code': expected}
    assert captured.err == ''


def test_owner_dry_run_counts_by_safe_activity_kind(tmp_path, capsys):
    cli = importlib.import_module('scripts.ledger_ingest_owner_export')
    file = pdf(tmp_path, activity('대출'), activity('대출'), activity('이체입금(가상고객)'),
               activity('이체입금(가상은행)'), activity('이체출금(가상고객)'),
               activity('대출(가상고객)'), activity('UNKNOWN20260101'), activity('가상고객'))
    db = tmp_path / 'ledger.db'
    assert cli.main(['--kind', 'toss_pdf', '--file', str(file), '--db', str(db),
                     '--account-suffix', '4321', '--dry-run']) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result['unknown_by_kind'] == {'대출': 3, 'unknown': 2}
    assert result['external_in_by_kind'] == {'이체입금': 2}
    assert result['external_out_by_kind'] == {'이체출금': 1}
    for key in ('unknown', 'external_in', 'external_out'):
        assert sum(result[key + '_by_kind'].values()) == result[key]
    assert not db.exists()
    assert captured.err == ''
    for private in ('가상고객', '가상은행', '2026', '50', '4321', 'UNKNOWN20260101'):
        assert private not in captured.out


def test_owner_dry_run_existing_database_and_replay_are_unchanged(tmp_path):
    db = tmp_path / 'ledger.db'
    store = LedgerStore(db)
    with store.read() as conn:
        before = '\n'.join(conn.iterdump())
    file = pdf(tmp_path, activity('이체입금'))
    result = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321', dry_run=True)
    assert result['external_in_by_kind'] == {'이체입금': 1}
    with store.read() as conn:
        assert '\n'.join(conn.iterdump()) == before
    module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    with store.read() as conn:
        before = '\n'.join(conn.iterdump())
    result = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321', dry_run=True)
    assert result['unknown_by_kind'] == result['external_in_by_kind'] == result['external_out_by_kind'] == {}
    assert all(result[k] == 0 for k in module().COUNTS)
    with store.read() as conn:
        assert '\n'.join(conn.iterdump()) == before
