"""Synthetic count mismatches and non-flow amount fallback policy."""
import json

import pytest
from test_ledger_returns import performance
from test_ledger_returns import wired as base_wired
from test_owner_flows import HEADER_TEXT, NUMBER, activity, module, records
from test_statement_import import COLUMNS, HEADER, line, synthetic_pdf

from portfolio_ledger import LedgerStore


@pytest.fixture
def wired(tmp_path, monkeypatch):
    return base_wired.__wrapped__(tmp_path, monkeypatch)


def statement(tmp_path, kind, *, foreign=False, blank_balance=True, fx='', settlement='125', native=True):
    columns = [c if c != '인지/농특' else '변제/연체합' for c in COLUMNS if not foreign or c != '거래세']
    values = dict.fromkeys(columns, '0')
    values.update(거래일자='2026.01.01', 거래구분=kind, 환율=fx,
                  거래대금='150', 정산금액=settlement, 잔고='' if blank_balance else '7', 잔액='999')
    values['종목명(종목코드)'] = ''
    header = HEADER_TEXT.replace(HEADER, line(columns))
    if foreign:
        header = header.replace('원화 거래내역', '외화 거래내역')
    main = line([values[c] for c in columns])
    native_row = ''
    if foreign and native:
        money = set(columns[3:]) - {'환율', '거래수량', '잔고'}
        cells = {c: '($ 0)' if c in money else '' for c in columns}
        cells.update(거래대금='($ 3)', 정산금액='($ 2.50)', 잔액='($ 20)')
        native_row = '\n' + line([cells[c] for c in columns])
    file = tmp_path / 'positioned.pdf'
    file.write_bytes(synthetic_pdf([header + '\n' + main + native_row]))
    return file


def ingest(tmp_path, file):
    db = tmp_path / 'ledger.db'
    counts = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    store = LedgerStore(db)
    with store.read() as conn:
        facts = json.loads(conn.execute('SELECT facts_json FROM ledger_owner_export_rows').fetchone()[0])
        coverage = dict(conn.execute('SELECT * FROM ledger_coverage').fetchone())
    return counts, records(store)[0], facts, coverage


@pytest.mark.parametrize('kind,classification', [('이체입금', 'external_in'), ('이체출금', 'external_out'), ('이자입금', 'evidence')])
def test_unverified_blank_balance_follows_amount_fallback_policy(tmp_path, kind, classification):
    counts, event, facts, coverage = ingest(tmp_path, statement(tmp_path, kind))
    expected = 'evidence' if classification == 'evidence' else 'unknown'
    assert counts[expected] == 1
    assert event['amount'] is facts['native_amount'] is None
    assert facts['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED']
    assert coverage['status'] == ('complete' if expected == 'evidence' else 'partial')


def test_fx_filled_rate_unverified_blank_balance_is_internal_evidence(tmp_path):
    counts, event, _, _ = ingest(tmp_path, statement(tmp_path, '환전원화입금', fx='1300'))
    assert counts['internal'] == 1 and counts['unknown'] == 0
    assert event['amount'] is None and event['currency'] is None


def test_foreign_dividend_unverified_blanks_use_amount_fallback(tmp_path):
    counts, event, facts, coverage = ingest(tmp_path, statement(tmp_path, '외화증권배당금입금', foreign=True))
    assert counts['evidence'] == 1 and counts['unknown'] == 0
    assert event['amount'] is facts['native_amount'] is None
    assert facts['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED']
    assert coverage['status'] == 'complete'


@pytest.mark.parametrize('kind,classification,event_kind', [
    ('배당금입금', 'evidence', 'income_other'),
    ('외화증권배당금입금', 'evidence', 'income_other'),
    ('환전원화입금', 'internal', 'corporate_action'),
    ('환전외화출금', 'internal', 'corporate_action'),
])
def test_unparseable_non_flow_amount_keeps_classification(tmp_path, kind, classification, event_kind):
    counts, event, facts, coverage = ingest(tmp_path, statement(tmp_path, kind, settlement='UNPARSEABLE'))
    assert counts[classification] == 1 and counts['unknown'] == 0
    assert event['kind'] == event_kind and event['amount'] is None
    assert facts['native_amount'] is None
    assert facts['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED']
    # Amount diagnostics concern non-flows, not completeness of cash flows.
    assert coverage['status'] == 'complete' and json.loads(coverage['reason_codes_json']) == []


@pytest.mark.parametrize('kind', ['이체입금', '이체출금', '대출', '알수없는입금'])
def test_unparseable_transfer_or_unrecognized_kind_stays_unknown(tmp_path, kind):
    counts, event, _, coverage = ingest(tmp_path, statement(tmp_path, kind, settlement='UNPARSEABLE'))
    assert counts['unknown'] == 1
    assert event['amount'] is None and event['classification'] == 'unknown'
    assert coverage['status'] == 'partial'
    assert 'OWNER_EXPORT_UNKNOWN_ACTIVITY' in json.loads(coverage['reason_codes_json'])


def test_fx_unparsed_amount_is_not_linked_as_conversion_proof(tmp_path):
    file = statement(tmp_path, '환전원화출금', settlement='UNPARSEABLE')
    # Both known FX directions remain internal, but missing money cannot link.
    file.write_bytes(synthetic_pdf([HEADER_TEXT + '\n' + activity('환전원화출금', 'BAD') + '\n' + activity('환전외화입금', native='1')]))
    db = tmp_path / 'ledger.db'
    counts = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    assert counts['internal'] == 2 and counts['unknown'] == 0
    with LedgerStore(db).read() as conn:
        facts = [json.loads(r[0]) for r in conn.execute('SELECT facts_json FROM ledger_owner_export_rows')]
    assert all(f['fx_pair'] is None for f in facts)


@pytest.mark.parametrize('settlement', ['1234567890123456789012345', '1,2345,678'])
def test_unresolved_count_mismatch_transfer_fails_closed(tmp_path, settlement):
    counts, event, _, coverage = ingest(tmp_path, statement(tmp_path, '이체입금', settlement=settlement))
    assert counts['unknown'] == 1 and event['amount'] is None
    assert coverage['status'] == 'partial'


@pytest.mark.parametrize('kind', ['배당금입금', '환전원화입금'])
def test_unparsed_non_flow_amount_does_not_block_returns(wired, tmp_path, kind):
    file = statement(tmp_path, kind, settlement='UNPARSEABLE')
    module().ingest_owner_export('toss_pdf', file, wired.ledger.db_path, account_suffix='4321')
    for nav, value in zip(wired.navs, (100, 110)):
        nav['account_nav'] = {'toss:only': {'source_status': 'complete', 'nav_native': {
            'KRW': {'value': value, 'status': 'derived', 'reason_codes': []}}}}
    with wired.ledger.transaction() as conn:
        wired.ledger.bind_account(conn, 'toss:only', 'toss:' + NUMBER)
    data = performance(wired, portfolio_scope='toss')
    assert data['returns']['twr_pct'] == data['returns']['xirr_pct'] == 10
