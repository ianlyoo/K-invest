"""Owner-export acceptance with invented PDF/CSV bytes only."""
import hashlib
import importlib
import json
import sqlite3

import pytest
from test_ledger_returns import blocked, performance
from test_ledger_returns import wired as base_wired
from test_statement_import import HEADER, line, synthetic_pdf

from portfolio_ledger import LedgerStore

NUMBER = '654-32-994321'
HEADER_TEXT = ('거래내역서\n성명 가상고객\n계좌 번호 ' + NUMBER +
               '\n조회 기간 2025년 1월 1일 ~ 2026년 1월 1일\n원화 거래내역\n' + HEADER)
CSV_HEADER = 'User ID,Time,Account,Operation,Coin,Change,Remark\n'


@pytest.fixture
def wired(tmp_path, monkeypatch):
    return base_wired.__wrapped__(tmp_path, monkeypatch)


def module():
    return importlib.import_module('owner_flows')


def activity(kind, amount='50', day='2026.01.01', *, native=None):
    row = line([day, kind, '', '', '0', amount, amount, '0', '0', '0', '0', '0', '0', '999'])
    if native is not None:
        row += '\n' + line(['', '', '', '', '', f'($ {native})', f'($ {native})',
                            '($ 0)', '($ 0)', '($ 0)', '($ 0)', '($ 0)', '', '($ 999)'])
    return row


def pdf(tmp_path, *rows):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(synthetic_pdf([HEADER_TEXT + '\n' + '\n'.join(rows)]))
    return path


def imported(tmp_path, *rows, **kwargs):
    file = pdf(tmp_path, *rows)
    db = tmp_path / 'ledger.db'
    result = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321', **kwargs)
    return LedgerStore(db), result, file


def records(store):
    with store.read() as conn:
        return [dict(store.effective_event(conn, r[0]), classification=r[1]) for r in conn.execute(
            '''SELECT e.event_id,(SELECT classification FROM ledger_transfer_classifications c
               WHERE c.event_id=e.event_id ORDER BY revision DESC LIMIT 1) FROM ledger_events e''')]


def test_toss_external_flows_provenance_coverage_and_private_identity(tmp_path):
    store, result, file = imported(tmp_path, activity('이체입금(가상고객)'), activity('이체출금(가상은행)', '20'))
    rows = records(store)
    assert sorted(r['classification'] for r in rows) == ['external_in', 'external_out']
    assert {r['source'] for r in rows} == {'toss_cashflows'}
    assert {r['amount'] for r in rows} == {'50', '20'}
    assert result['external_in'] == result['external_out'] == 1
    with store.read() as conn:
        proofs = list(conn.execute('SELECT * FROM ledger_owner_export_rows'))
        assert len(proofs) == 2
        assert all(r['file_sha256'] == hashlib.sha256(file.read_bytes()).hexdigest() for r in proofs)
        assert all(r['parser_version'] and r['page'] == 1 and r['line'] > 0 and len(r['row_hash']) == 64 for r in proofs)
        coverage = conn.execute('SELECT * FROM ledger_coverage').fetchone()
        assert coverage['source'] == 'toss' and coverage['stream'] == 'toss/cashflows'
        assert coverage['status'] == 'complete'
        data = '\n'.join(conn.iterdump())
    assert NUMBER not in data and NUMBER.replace('-', '') not in data and '가상고객' not in data and '가상은행' not in data


def test_dedicated_toss_account_reuses_registry_journal_identity_and_display_label(tmp_path):
    from toss_accounts import TossAccountRegistry, TossCredentials
    registry = TossAccountRegistry([TossCredentials('investmcp_only', 'invented-id', 'invented-secret', NUMBER)], cache_dir=tmp_path)
    journal_key = registry.client_for('investmcp_only').journal_account_key
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.transaction() as conn:
        store.bind_account(conn, 'toss:investmcp_only', journal_key)
        identity = store.account_identity('toss:investmcp_only', conn)
    file = pdf(tmp_path, activity('이체입금'))
    module().ingest_owner_export('toss_pdf', file, store.db_path, account_suffix='4321')
    assert records(store)[0]['account_identity'] == identity
    assert module().account_display_label('4321') == 'InvestMCP 전용'


def test_toss_fx_pair_internal_native_evidence_no_flow(tmp_path):
    store, result, _ = imported(tmp_path, activity('환전원화출금', '1000'), activity('환전외화입금', '1000', native='1'))
    assert result['internal'] == 2
    assert all(r['kind'] not in ('deposit', 'withdrawal', 'transfer') for r in records(store))
    with store.read() as conn:
        facts = [json.loads(r[0]) for r in conn.execute('SELECT facts_json FROM ledger_owner_export_rows')]
    assert {f['native_amount'] for f in facts} == {'1000', '1'}
    assert len({f['fx_pair'] for f in facts}) == 1 and facts[0]['fx_pair'] is not None


@pytest.mark.parametrize('kind', ['배당금입금', '외화증권배당금입금', '배당세출금', '이자입금',
    '외화이자입금', '외화이자세금출금', '대여료입금', '캐시백입금', '해외채권세금출금', '외화주식병합단주대금입금'])
def test_toss_income_tax_evidence_not_flows(tmp_path, kind):
    store, result, _ = imported(tmp_path, activity(kind, native='2' if '외화' in kind else None))
    assert result['evidence'] == 1
    assert records(store)[0]['kind'] == 'income_other'


@pytest.mark.parametrize('kind', ['대차거래대여출고', '대차거래상환입고', '주식병합출고', '외화채권매수', '외화채권매도', '구매', '판매'])
def test_toss_non_cash_trade_not_flows(tmp_path, kind):
    store, result, _ = imported(tmp_path, activity(kind))
    assert result['non_cash'] == 1
    assert records(store)[0]['kind'] not in ('deposit', 'withdrawal', 'transfer')


@pytest.mark.parametrize('kind', ['대출', '알수없는입금'])
def test_toss_unknown_activity_blocks_returns(tmp_path, kind):
    store, result, _ = imported(tmp_path, activity(kind))
    assert result['unknown'] == 1 and records(store)[0]['classification'] == 'unknown'
    with store.read() as conn:
        coverage = conn.execute('SELECT * FROM ledger_coverage').fetchone()
        assert coverage['status'] == 'partial'
        assert 'OWNER_EXPORT_UNKNOWN_ACTIVITY' in json.loads(coverage['reason_codes_json'])


def test_unknown_toss_activity_keeps_metrics_null(wired, tmp_path):
    file = pdf(tmp_path, activity('대출'))
    module().ingest_owner_export('toss_pdf', file, wired.ledger.db_path, account_suffix='4321')
    for nav, value in zip(wired.navs, (100, 160)):
        nav['account_nav'] = {'toss:only': {'source_status': 'complete', 'nav_native': {
            'KRW': {'value': value, 'status': 'derived'}}}}
    with wired.ledger.transaction() as conn:
        wired.ledger.bind_account(conn, 'toss:only', 'toss:' + NUMBER)
    blocked(performance(wired, portfolio_scope='toss'), 'OWNER_EXPORT_UNKNOWN_ACTIVITY')


def test_toss_suffix_mismatch_refuses_zero_writes(tmp_path):
    path = pdf(tmp_path, activity('이체입금'))
    db = tmp_path / 'absent.db'
    with pytest.raises(ValueError, match='ACCOUNT_SUFFIX_MISMATCH'):
        module().ingest_owner_export('toss_pdf', path, db, account_suffix='0000')
    assert not db.exists()


def test_owner_export_reingest_noop_and_dry_run_no_writes(tmp_path):
    file = pdf(tmp_path, activity('이체입금'))
    db = tmp_path / 'ledger.db'
    module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321', dry_run=True)
    assert not db.exists()
    first = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    with sqlite3.connect(db) as conn:
        before = '\n'.join(conn.iterdump())
    assert first['external_in'] == 1
    second = module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321')
    assert all(value == 0 for value in second.values())
    module().ingest_owner_export('toss_pdf', file, db, account_suffix='4321', dry_run=True)
    with sqlite3.connect(db) as conn:
        assert '\n'.join(conn.iterdump()) == before


def test_binance_csv_api_coverage_precedence_and_outside_added(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.transaction() as conn:
        account = store.account_identity('binance:primary', conn)
        store.coverage(conn, 'binance', account, 'binance/deposit', '2026-01-01T00:00:00+09:00',
                       '2026-01-01T23:59:59.999+09:00', 'limited', ['HISTORY_RETENTION_UNVERIFIED'], 0, 'api')
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + 'invented,2026-01-01 09:00:00,Spot,Deposit,USDT,10,private remark\n'
                    'invented,2025-12-31 09:00:00,Spot,Deposit,USDT,20,private remark\n'
                    'invented,2025-12-31 10:00:00,Spot,Withdraw,USDT,-3,private remark\n')
    result = module().ingest_owner_export('binance_csv', file, store.db_path)
    assert result['api_covered'] == 1 and result['external_in'] == result['external_out'] == 1
    assert {r['amount'] for r in records(store)} == {'20', '3'}


def test_binance_csv_internal_transfer_convert_and_return_evidence(tmp_path):
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + 'invented,2026-01-01 09:00:00,Spot,Transfer Between Spot and UM Futures,USDT,-5,hidden\n'
        'invented,2026-01-01 09:00:00,USD-M Futures,Transfer Between Spot and UM Futures,USDT,5,hidden\n'
        'invented,2026-01-01 10:00:00,Spot,Binance Convert,USDT,-1,hidden\n'
        'invented,2026-01-01 11:00:00,USD-M Futures,Funding Fee,USDT,-1,hidden\n')
    result = module().ingest_owner_export('binance_csv', file, tmp_path / 'ledger.db')
    assert result['internal'] == 3 and result['evidence'] == 1
    assert all(r['classification'] not in ('external_in', 'external_out') for r in records(LedgerStore(tmp_path / 'ledger.db')))


def test_toss_export_hand_computed_returns_and_kis_stays_unknown(wired, tmp_path):
    file = pdf(tmp_path, activity('이체입금', '50'))
    module().ingest_owner_export('toss_pdf', file, wired.ledger.db_path, account_suffix='4321')
    for nav, value in zip(wired.navs, (100, 160)):
        nav['account_nav'] = {'toss:only': {'source_status': 'complete', 'nav_native': {
            'KRW': {'value': value, 'status': 'derived', 'reason_codes': []}}},
            'kis:uncovered': {'source_status': 'complete', 'nav_native': {
            'KRW': {'value': 1, 'status': 'derived', 'reason_codes': []}}}}
    with wired.ledger.transaction() as conn:
        wired.ledger.bind_account(conn, 'toss:only', 'toss:' + NUMBER)
    data = performance(wired, portfolio_scope='toss')
    assert data['returns']['twr_pct'] == data['returns']['xirr_pct'] == 10
    assert data['account_labels'] == {'toss': ['InvestMCP 전용']}
    blocked(performance(wired, portfolio_scope='equity'), 'FLOW_COVERAGE_GAP')
    blocked(performance(wired, portfolio_scope='consolidated'), 'FLOW_COVERAGE_GAP')


def test_crypto_scope_uses_only_crypto_nav_and_flow_roster(wired):
    from test_ledger_returns import coverage
    coverage(wired, venues=('binance', 'hyperliquid'))
    for nav, value in zip(wired.navs, (100, 110)):
        for provider in ('binance', 'hyperliquid'):
            nav['crypto_nav'][provider].update(value_krw=value / 2, status='derived', reason_codes=[])
    data = performance(wired, portfolio_scope='crypto')
    assert data['returns']['twr_pct'] == data['returns']['xirr_pct'] == 10


def test_owner_export_cli_counts_only_privacy(tmp_path, capsys):
    from scripts.ledger_ingest_owner_export import main
    file = pdf(tmp_path, activity('이체입금(가상고객)'))
    assert main(['--kind', 'toss_pdf', '--file', str(file), '--db', str(tmp_path / 'db'), '--account-suffix', '4321']) == 0
    output = capsys.readouterr().out
    assert set(json.loads(output)) <= {'external_in', 'external_out', 'internal', 'evidence', 'non_cash', 'unknown', 'api_covered', 'duplicate'}
    for private in ('가상고객', NUMBER, NUMBER.replace('-', ''), '4321', hashlib.sha256(file.read_bytes()).hexdigest()):
        assert private not in output


def test_owner_transfer_matching_three_business_days_and_ambiguity(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.transaction() as conn:
        for account, direction, day in [('first', 'out', '2025-01-03'), ('second', 'in', '2025-01-08')]:
            event = dict(source='toss_cashflows', account_identity=store.account_identity('toss:' + account, conn),
                source_record_id=account, kind='withdrawal' if direction == 'out' else 'deposit',
                source_timezone='Asia/Seoul', timestamp_precision='date_only', activity_date=day,
                direction=direction, currency='KRW', amount='50', amount_precision='1', fee='0', fee_asset='KRW',
                source_status='completed', identity_quality='statement_row')
            store.append(conn, event)
            store.classify_transfer(conn, store.event_id(event), 'external_' + direction, 'OWNER_EXPORT_REPORTED_EXTERNAL')
    assert store.match_transfers() == 1
    assert {r['classification'] for r in records(store)} == {'internal'}


def test_binance_owner_declaration_unblocks_early_crypto_coverage(wired, tmp_path):
    from test_ledger_returns import coverage
    floor, first, end = '2025-11-01', '2026-06-19', '2026-07-01'
    wired.navs[0]['snapshot_date'], wired.navs[-1]['snapshot_date'] = floor, end
    coverage(wired, venues=('hyperliquid',), lo=floor, hi=end)
    from test_ledger_returns import ACCOUNTS
    with wired.ledger.transaction() as conn:
        wired.ledger.bind_account(conn, 'binance:primary', ACCOUNTS['binance'])
    for nav, value in zip(wired.navs, (100, 110)):
        for venue in ('binance', 'hyperliquid'):
            nav['crypto_nav'][venue].update(value_krw=value / 2, status='derived', reason_codes=[])
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + f'invented,{first} 09:00:00,USD-M Futures,Funding Fee,KRW,0,hidden\n'
                    f'invented,{end} 09:00:00,USD-M Futures,Funding Fee,KRW,0,hidden\n')
    module().ingest_owner_export('binance_csv', file, wired.ledger.db_path)
    blocked(performance(wired, portfolio_scope='crypto'), 'FLOW_COVERAGE_GAP')
    module().ingest_owner_export('binance_csv', file, wired.ledger.db_path,
        owner_declared_no_activity_before=first, coverage_floor=floor)
    data = performance(wired, portfolio_scope='crypto')
    assert data['returns']['twr_pct'] == 10
    declaration = data['ledger_flow_coverage']['owner_declarations'][0]
    assert declaration['status'] == 'owner_declared' and declaration['reason_codes'] == ['OWNER_DECLARED_NO_ACTIVITY']
    blocked(performance(wired, portfolio_scope='consolidated'), 'FLOW_COVERAGE_GAP')
    with wired.ledger.read() as conn:
        rows = list(conn.execute("SELECT * FROM ledger_coverage WHERE reason_codes_json='[\"OWNER_DECLARED_NO_ACTIVITY\"]'"))
        assert rows and all(r['row_count'] == 0 and r['verified_zero'] == 0 for r in rows)
    with wired.ledger.transaction() as conn:
        wired.ledger.append(conn, dict(source='binance_income', account_identity=ACCOUNTS['binance'],
            source_record_id='late-arriving-prior-activity', kind='funding', amount='1', currency='KRW',
            execution_time_utc='2026-06-18T00:00:00+00:00'))
    blocked(performance(wired, portfolio_scope='crypto'), 'OWNER_DECLARATION_CONTRADICTED')


def test_owner_declaration_contradicting_csv_refuses_zero_writes(tmp_path):
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + 'invented,2026-06-18 09:00:00,Spot,Deposit,USDT,1,hidden\n')
    db = tmp_path / 'absent.db'
    with pytest.raises(ValueError, match='OWNER_DECLARATION_CONTRADICTED'):
        module().ingest_owner_export('binance_csv', file, db,
            owner_declared_no_activity_before='2026-06-19', coverage_floor='2025-11-01')
    assert not db.exists()


def test_investmcp_toss_scope_excludes_uncovered_0654_and_uses_local_nav(wired, tmp_path, monkeypatch):
    file = pdf(tmp_path, activity('이체입금', '50'))
    module().ingest_owner_export('toss_pdf', file, wired.ledger.db_path, account_suffix='4321')
    for nav, value in zip(wired.navs, (100, 160)):
        nav['account_nav'] = {'toss:only': {'source_status': 'complete', 'nav_native': {
            'KRW': {'value': value, 'status': 'derived'}}},
            'toss:other': {'source_status': 'complete', 'nav_native': {'KRW': {'value': 1, 'status': 'derived'}}}}
    with wired.ledger.transaction() as conn:
        wired.ledger.bind_account(conn, 'toss:only', 'toss:' + NUMBER)
    blocked(performance(wired, portfolio_scope='toss'), 'FLOW_COVERAGE_GAP')
    def forbidden():
        raise AssertionError('New venue scopes must use local NAV and flows only')
    monkeypatch.setattr(wired.server, '_get_collector', forbidden)
    result = wired.server.get_portfolio_performance(portfolio_scope='toss_investmcp')
    assert result['ok'], result.get('error')
    assert result['data']['returns']['twr_pct'] == result['data']['returns']['xirr_pct'] == 10
    assert result['data']['account_labels'] == {'toss': ['InvestMCP 전용']}


def test_owner_export_transaction_rollback_and_immutable_evidence(tmp_path, monkeypatch):
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read():
        pass
    file = pdf(tmp_path, activity('이체입금'))
    original = LedgerStore.coverage
    def fail(*args, **kwargs):
        raise ValueError('synthetic transaction failure')
    monkeypatch.setattr(LedgerStore, 'coverage', fail)
    with pytest.raises(ValueError):
        module().ingest_owner_export('toss_pdf', file, store.db_path, account_suffix='4321')
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='ledger_owner_export_imports'").fetchone()[0] == 0
    monkeypatch.setattr(LedgerStore, 'coverage', original)
    module().ingest_owner_export('toss_pdf', file, store.db_path, account_suffix='4321')
    with store.read() as conn:
        for table in ('ledger_owner_export_imports', 'ledger_owner_export_rows'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                conn.execute(f'DELETE FROM {table}')
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ledger_owner_declarations_no_update'").fetchone()


def test_binance_csv_api_arriving_later_wins_without_double_counting(wired, tmp_path):
    from test_ledger_returns import ACCOUNTS, coverage
    coverage(wired, venues=('binance', 'hyperliquid'))
    with wired.ledger.transaction() as conn:
        wired.ledger.bind_account(conn, 'binance:primary', ACCOUNTS['binance'])
    # Remove no immutable data: make CSV fall outside the existing API proof.
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + 'invented,2024-12-31 09:00:00,USD-M Futures,Deposit,KRW,50,hidden\n')
    module().ingest_owner_export('binance_csv', file, wired.ledger.db_path)
    assert len(records(wired.ledger)) == 1
    with wired.ledger.transaction() as conn:
        event = dict(source='binance_deposit', account_identity=ACCOUNTS['binance'], source_record_id='native-api',
            kind='deposit', execution_time_utc='2024-12-31T00:00:00+00:00', source_timezone='UTC',
            activity_date='2024-12-31', timestamp_precision='second', amount='50', currency='KRW',
            direction='in', wallet='UMFUTURE', source_status='1', identity_quality='native_record')
        wired.ledger.append(conn, event)
        wired.ledger.classify_transfer(conn, wired.ledger.event_id(event), 'external_in', 'SYNTHETIC_VERIFIED_BOUNDARY')
    for nav in wired.navs:
        for venue in ('binance', 'hyperliquid'):
            nav['crypto_nav'][venue].update(value_krw=50, status='derived', reason_codes=[])
    # Read the older slice directly; an exact API counterpart wins even before
    # a successful API coverage observation has arrived.
    from ledger_returns import read_flows
    evidence = read_flows(wired.ledger.db_path, '2024-12-31', '2024-12-31', 'crypto', wired.navs)
    assert evidence['observed_deposits_krw'] == 50


def test_owner_declaration_dry_run_idempotency_and_api_contradiction(tmp_path):
    file = tmp_path / 'synthetic.csv'
    file.write_text(CSV_HEADER + 'invented,2026-06-19 09:00:00,Spot,Deposit,USDT,1,hidden\n')
    db = tmp_path / 'ledger.db'
    options = dict(owner_declared_no_activity_before='2026-06-19', coverage_floor='2025-11-01')
    module().ingest_owner_export('binance_csv', file, db, dry_run=True, **options)
    assert not db.exists()
    module().ingest_owner_export('binance_csv', file, db, **options)
    store = LedgerStore(db)
    with store.read() as conn:
        before = '\n'.join(conn.iterdump())
    assert all(v == 0 for v in module().ingest_owner_export('binance_csv', file, db, **options).values())
    with store.read() as conn:
        assert '\n'.join(conn.iterdump()) == before
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE ledger_owner_declarations SET reason_code=reason_code')
    with store.transaction() as conn:
        store.append(conn, dict(source='binance_income', account_identity=store.account_identity('binance:primary', conn),
            source_record_id='older-api', kind='funding', execution_time_utc='2026-06-18T00:00:00+00:00',
            amount='1', currency='USDT'))
    with pytest.raises(ValueError, match='OWNER_DECLARATION_CONTRADICTED'):
        module().ingest_owner_export('binance_csv', file, db, **options)


def test_owner_ambiguous_transfer_legs_stay_unknown(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    with store.transaction() as conn:
        for key, direction in [('out', 'out'), ('in1', 'in'), ('in2', 'in')]:
            event = dict(source='toss_cashflows', account_identity=store.account_identity('toss:' + key, conn),
                source_record_id=key, kind='transfer', source_timezone='Asia/Seoul', activity_date='2026-01-01',
                direction=direction, amount='50', currency='KRW', amount_precision='1', fee='0', fee_asset='KRW',
                source_status='completed', identity_quality='statement_row')
            store.append(conn, event)
            store.classify_transfer(conn, store.event_id(event), 'external_' + direction, 'OWNER_EXPORT_REPORTED_EXTERNAL')
    assert store.match_transfers() == 0
    assert {r['classification'] for r in records(store)} == {'unknown'}


def test_new_scopes_preserve_unknown_provider_guard_on_default_equity(wired):
    from test_ledger_returns import coverage
    coverage(wired)
    wired.navs[0]['account_nav']['unverified:account'] = {}
    blocked(performance(wired), 'ACCOUNT_FLOW_COVERAGE_MISSING')
