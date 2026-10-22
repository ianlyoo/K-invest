"""L2 synthetic flow collection, conservative matching and private provenance."""
import json
import sqlite3

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_collect_ledger import Equity, collect_ledger

import crypto_futures_accounts as futures
from portfolio_ledger import LedgerStore, ingest_capture, normalize_row

START = 1790812800000
ADDRESS = '0x' + 'a' * 40
TX = '0x' + 'b' * 64
ROUTES = ('MAIN_UMFUTURE', 'UMFUTURE_MAIN', 'MAIN_FUNDING', 'FUNDING_MAIN',
          'FUNDING_UMFUTURE', 'UMFUTURE_FUNDING')


def flow(store, stream, row):
    with store.read() as conn:
        return normalize_row(store, conn, stream, row, {})


def deposit(store, ident='deposit', amount='99.000000', tx=TX):
    return flow(store, 'binance/deposit', dict(id=ident, txId=tx, amount=amount, coin='USDC',
                insertTime=START + 1000, status=1, network='ARBITRUM', address=ADDRESS))


def withdrawal(store, ident=TX, amount='99.000000', fee='1.000000'):
    return flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
                dict(hash=ident, time=START, delta=dict(type='withdraw', usdc=amount, fee=fee)))


def append(store, events):
    return store.append_many([(event, None) for event in events])


def match_count(store):
    with store.read() as conn:
        return conn.execute('SELECT count(*) FROM ledger_transfer_matches').fetchone()[0]


def test_flow_read_allowlists_and_sapi_host():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])
    client = futures.BinanceAccountClient('synthetic', rsa.generate_private_key(public_exponent=65537, key_size=2048),
        http=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None)
    for path in ('/sapi/v1/asset/transfer', '/sapi/v1/capital/deposit/hisrec', '/sapi/v1/capital/withdraw/history'):
        client.request('GET', path, {'startTime': START, 'endTime': START + 10, **({'type': 'MAIN_UMFUTURE'} if path.endswith('/transfer') else {})})
        assert calls[-1].url.host == 'api.binance.com'
        with pytest.raises(futures.FuturesError):
            client.request('POST', path)
    hl = futures.HyperliquidAccountClient([ADDRESS], http=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None)
    hl.info('userNonFundingLedgerUpdates', ADDRESS, startTime=START, endTime=START + 10)
    assert json.loads(calls[-1].content)['type'] == 'userNonFundingLedgerUpdates'


def test_pagination_and_window_stitching():
    calls = []
    class Client:
        max_pages = 20
        def request(self, method, path, params):
            calls.append(params.copy())
            assert params['endTime'] - params['startTime'] < 90 * futures.DAY_MS
            offset = params.get('offset', 0)
            if offset == 0:
                return [{'id': str(params['startTime']) + '-1', 'insertTime': params['startTime'],
                         'coin': 'USDT', 'amount': '1', 'walletType': 0, 'status': 1},
                        {'id': str(params['startTime']) + '-2', 'insertTime': params['endTime'],
                         'coin': 'USDT', 'amount': '1', 'walletType': 0, 'status': 1}]
            return []
    rows, reasons, failed = collect_ledger.flow_history(Client(), 'deposit', START, START + 180 * futures.DAY_MS,
                                                        page_size=2, observed_at_ms=START + 180 * futures.DAY_MS)
    assert len(rows) == 6
    assert not failed
    assert 'HISTORY_RETENTION_UNVERIFIED' in reasons
    windows = [(c['startTime'], c['endTime']) for c in calls if c['offset'] == 0]
    assert windows[0][0] == START and windows[-1][1] == START + 180 * futures.DAY_MS
    assert all(a[1] + 1 == b[0] for a, b in zip(windows, windows[1:]))
    assert any(c['offset'] == 2 for c in calls)


def test_transfer_pagination_retention_and_page_budget():
    class Client:
        max_pages = 2
        def _clock(self):
            return START / 1000
        def request(self, method, path, params):
            assert params['size'] == 100 and params['current'] in (1, 2)
            return {'total': 999, 'rows': [{'tranId': params['current'] * 100 + i,
                'timestamp': params['startTime'], 'asset': 'USDT', 'amount': '1',
                'type': params['type'], 'status': 'CONFIRMED'} for i in range(100)]}
    rows, reasons, failed = collect_ledger.flow_history(Client(), 'transfer_MAIN_UMFUTURE',
                                                        START - 365 * futures.DAY_MS, START)
    assert len(rows) == 200 and failed
    assert 'TRANSFER_SIX_MONTH_RETENTION' in reasons and 'PAGE_BUDGET' in reasons


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('payload', [{'total': 0}, {'total': 0, 'rows': None}, {'total': 0, 'rows': []}])
def test_transfer_zero_total_without_rows_is_empty(route, payload):
    calls = []
    class Client:
        max_pages = 40
        def request(self, method, path, params):
            calls.append(params)
            assert method == 'GET' and path == '/sapi/v1/asset/transfer'
            assert params['type'] == route and params['current'] == 1
            return payload
    rows, reasons, failed = collect_ledger.flow_history(Client(), 'transfer_' + route,
        START, START + 1000, observed_at_ms=START + 1000)
    assert rows == [] and not failed
    assert reasons == []
    assert len(calls) == 1


@pytest.mark.parametrize('payload', [None, [], {}, {'total': True}, {'total': '0'}, {'total': -1},
    {'total': 1}, {'total': 1, 'rows': None}, {'total': 0, 'rows': {}},
    {'total': 0, 'rows': [dict(tranId=1)]}, {'total': 1, 'rows': ['bad-row']},
    {'total': 0, 'code': -2015, 'msg': 'private account error'},
    {'total': 0, 'rows': [], 'code': -2015}, {'total': 0, 'error': 'private error'},
    {'total': 0, 'success': False}])
def test_transfer_response_shape_rejection_has_distinct_reason(tmp_path, payload):
    class Client:
        max_pages = 40
        def request(self, *args):
            return payload
    rows, reasons, failed = collect_ledger.flow_history(Client(), 'transfer_MAIN_UMFUTURE',
        START, START + 1000, observed_at_ms=START + 1000)
    assert not rows and failed
    assert reasons == ['HISTORY_SHAPE_UNAVAILABLE']
    assert 'private' not in str(reasons)
    store = LedgerStore(tmp_path / 'ledger.db')
    run = collect_ledger.CollectionRun(store)
    run.raw('binance/transfer_MAIN_UMFUTURE', rows, store.account_identity('binance:primary'),
            START, START + 1000, reasons, failed)
    with store.read() as conn:
        row = conn.execute('SELECT status,verified_zero FROM ledger_coverage').fetchone()
        assert row['status'] == 'partial' and row['verified_zero'] == 0


@pytest.mark.parametrize('failure', ['shape', 'http'])
def test_failed_transfer_page_preserves_rows_and_splits_reasons(failure):
    class Client:
        max_pages = 40
        def request(self, method, path, params):
            if params['current'] == 1:
                return {'total': 2, 'rows': [dict(tranId=1, timestamp=START,
                    asset='USDT', amount='1', type=params['type'], status='CONFIRMED')]}
            if failure == 'shape':
                return {'total': 2}
            raise futures.FuturesError('FUTURES_UPSTREAM_ERROR', status=400)
    rows, reasons, failed = collect_ledger.flow_history(Client(), 'transfer_MAIN_UMFUTURE',
        START, START + 1000, page_size=1, observed_at_ms=START + 1000)
    assert [row['tranId'] for row in rows] == [1] and failed
    assert ('HISTORY_SHAPE_UNAVAILABLE' in reasons) == (failure == 'shape')
    assert ('SOURCE_STREAM_FAILED' in reasons) == (failure == 'http')
    assert ('UNQUERIED_WINDOW_GAP' in reasons) == (failure == 'http')


def test_collection_empty_transfer_objects_are_limited_without_failure(tmp_path):
    from test_collect_ledger import Binance, Hyperliquid
    class Client(Binance):
        def request(self, method, path, params=None):
            if path == '/sapi/v1/asset/transfer':
                return {'total': 0}
            return super().request(method, path, params)
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-07-12', '2026-07-12', equity=Equity(),
                                   binance_factory=Client, hyperliquid_factory=Hyperliquid)
    assert not result['failed']
    with store.read() as conn:
        rows = list(conn.execute("SELECT stream,status,reason_codes_json FROM ledger_coverage WHERE stream LIKE 'binance/transfer_%'"))
        assert len(rows) == 6
        assert {row['status'] for row in rows} == {'limited'}
        assert all(json.loads(row['reason_codes_json']) == ['TRANSFER_SIX_MONTH_RETENTION'] for row in rows)


def test_wallet_route_pair_match_and_unknown_ingest(tmp_path):
    from portfolio_ledger import normalize_rows
    store = LedgerStore(tmp_path / 'db')
    with store.read() as conn:
        events = normalize_rows(store, conn, 'binance/transfer_MAIN_UMFUTURE',
            dict(tranId=42, timestamp=START, amount='50.00', asset='USDC', status='CONFIRMED'), {})
    assert len(events) == 2 and {e['direction'] for e in events} == {'in', 'out'}
    append(store, events)
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT classification FROM ledger_transfer_classifications')] == ['unknown', 'unknown']
    assert store.match_transfers() == 1
    assert store.match_transfers() == 0
    assert append(store, events)['new'] == 0
    assert match_count(store) == 1
    assert store.match_transfers(rule_version='l2-v2') == 1
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT revision FROM ledger_transfer_matches ORDER BY revision')] == [1, 2]


def test_hl_binance_tx_hash_match_and_revision_idempotency(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    append(store, [withdrawal(store), deposit(store)])
    assert store.match_transfers() == 1
    assert store.match_transfers() == 0
    with store.read() as conn:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM ledger_transfer_matches').fetchone()[0])
        assert evidence['rule'] == 'shared_reference'
        assert evidence['fee'] == '1'
        assert conn.execute("SELECT count(*) FROM ledger_transfer_classifications WHERE classification='internal'").fetchone()[0] == 2
    assert ADDRESS not in store.db_path.read_bytes().decode(errors='ignore')


def test_shared_ref_preferred_over_amount_candidate(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    append(store, [withdrawal(store), deposit(store), deposit(store, 'other', tx='other-ref')])
    assert store.match_transfers() == 1


@pytest.mark.parametrize('case', ['ambiguous', 'fee_mismatch', 'missing_fee', 'currency', 'pending', 'outside_window', 'conflict'])
def test_unsafe_candidates_stay_unknown(tmp_path, case):
    store = LedgerStore(tmp_path / 'db')
    out = withdrawal(store, ident='no-shared-ref')
    incoming = deposit(store, tx=None)
    events = [out, incoming]
    if case == 'ambiguous':
        events.append(deposit(store, 'second', tx=None))
    elif case == 'fee_mismatch':
        incoming['amount'] = '98.000000'
    elif case == 'missing_fee':
        out['fee'] = None
    elif case == 'currency':
        incoming['currency'] = 'USDT'
    elif case == 'pending':
        incoming['source_status'] = '0'
    elif case == 'outside_window':
        incoming['execution_time_utc'] = '2026-10-03T00:00:00.000+00:00'
    append(store, events)
    if case == 'conflict':
        append(store, [{**incoming, 'amount': '123'}])
    assert store.match_transfers() == 0
    with store.read() as conn:
        assert {r[0] for r in conn.execute('SELECT classification FROM ledger_transfer_classifications')} == {'unknown'}


def test_match_table_triggers(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    append(store, [withdrawal(store), deposit(store)])
    store.match_transfers()
    with store.read() as conn:
        for sql in ('UPDATE ledger_transfer_matches SET revision=99', 'DELETE FROM ledger_transfer_matches'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                conn.execute(sql)


def test_partial_rows_skipped_with_reasons_and_collection_replay_privacy(tmp_path):
    class Binance:
        max_pages = 40
        def _clock(self):
            return START / 1000
        def request(self, method, path, params=None):
            if path == '/sapi/v1/capital/deposit/hisrec':
                return [dict(id='deposit', amount='99.000000', coin='USDC', status=1, insertTime=START + 1000,
                             txId=TX, address=ADDRESS), dict(id='partial'), 'unsupported']
            return {'rows': [], 'total': 0} if path.endswith('/transfer') else []
    class HL:
        max_pages = 40
        _addresses = [ADDRESS]
        def info(self, kind, address, **params):
            return [dict(time=START, hash=TX, delta=dict(type='withdraw', usdc='99.000000', fee='1.000000')),
                    dict(time=START, hash='unsupported', delta=dict(type='unknown'))] if kind == 'userNonFundingLedgerUpdates' else []
    store = LedgerStore(tmp_path / 'db')
    kwargs = dict(equity=Equity(), binance_factory=Binance, hyperliquid_factory=HL)
    first = collect_ledger.collect(store, '2026-10-01', '2026-10-01', **kwargs)
    assert first['transfer_matches'] == 1
    assert sum(v['skipped'] for v in first['streams'].values()) == 3
    second = collect_ledger.collect(store, '2026-10-01', '2026-10-01', **kwargs)
    assert sum(v['new'] for v in second['streams'].values()) == 0
    assert second['transfer_matches'] == 0
    assert ADDRESS not in json.dumps(first)
    for p in (tmp_path / 'ledger-raw').rglob('*.json'):
        assert ADDRESS not in p.read_text()
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM ledger_coverage WHERE reason_codes_json LIKE '%UNSUPPORTED_OR_MISSING_RECORD_ID%'").fetchone()[0] >= 2


def test_capture_ingest_matches_and_replays(tmp_path):
    import hashlib
    run = tmp_path / 'capture'
    stream = 'binance/transfer_MAIN_UMFUTURE'
    folder = run / stream
    folder.mkdir(parents=True)
    files = {'page.json': [dict(tranId=42, timestamp=START, amount='50.00', asset='USDC', status='CONFIRMED')],
             'page.meta.json': {'http_status': 200}}
    hashes = {}
    for name, body in files.items():
        path = folder / name
        path.write_text(json.dumps(body))
        hashes[stream + '/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (run / 'manifest.json').write_text(json.dumps({'files_sha256': hashes, 'streams': {stream: {'status': 'complete'}}}))
    db = tmp_path / 'db'
    first = ingest_capture(run, db)
    assert first['totals']['new'] == 2 and first['transfer_matches'] == 1
    second = ingest_capture(run, db)
    assert second['totals']['new'] == 0 and second['transfer_matches'] == 0


@pytest.mark.parametrize('route', ROUTES)
def test_all_six_wallet_routes_and_pending_status(tmp_path, route):
    from portfolio_ledger import normalize_rows
    store = LedgerStore(tmp_path / 'db')
    with store.read() as conn:
        pending = normalize_rows(store, conn, 'binance/transfer_' + route,
            dict(tranId=1, timestamp=START, amount='2.00', asset='USDC', status='PENDING'), {})
        complete = normalize_rows(store, conn, 'binance/transfer_' + route,
            dict(tranId=2, timestamp=START, amount='2.00', asset='USDC', status='CONFIRMED'), {})
    append(store, pending + complete)
    assert store.match_transfers() == 1
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM ledger_transfer_classifications WHERE classification='unknown'").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM ledger_transfer_classifications WHERE classification='internal'").fetchone()[0] == 2


def test_hl_wallet_and_configured_address_pairing(tmp_path):
    from portfolio_ledger import normalize_rows
    other = '0x' + 'c' * 40
    store = LedgerStore(tmp_path / 'db')
    account = store.hl_identity(ADDRESS)
    other_account = store.hl_identity(other)
    row = dict(hash=TX, time=START, delta=dict(type='internalTransfer', usdc='5.00', fee='0',
                                            user=ADDRESS, destination=other))
    observed = [flow(store, 'hyperliquid/' + a + '/userNonFundingLedgerUpdates', row)
                for a in (account, other_account)]
    with store.read() as conn:
        observed += normalize_rows(store, conn, 'hyperliquid/' + account + '/userNonFundingLedgerUpdates',
            dict(hash='wallet-ref', time=START, delta=dict(type='accountClassTransfer', usdc='2.00', toPerp=True)), {})
    append(store, observed)
    assert store.match_transfers() == 2
    assert ADDRESS not in store.db_path.read_bytes().decode(errors='ignore')
    assert other not in store.db_path.read_bytes().decode(errors='ignore')


def test_amount_window_and_precision_matching_without_shared_ref(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    incoming = deposit(store, tx=None, amount='99.000001')
    outgoing = withdrawal(store, ident='different-ref')
    append(store, [outgoing, incoming])
    assert store.match_transfers() == 1
    with store.read() as conn:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM ledger_transfer_matches').fetchone()[0])
        assert evidence['rule'] == 'time_amount_fee' and evidence['precision_unit'] == '0.000001'


def test_hash_prefix_normalization_and_reserved_legs(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    append(store, [withdrawal(store), deposit(store, tx=TX[2:].upper())])
    assert store.match_transfers() == 1
    append(store, [deposit(store, ident='later-candidate')])
    assert store.match_transfers() == 0
    assert store.match_transfers(rule_version='l2-v2') == 1
    assert match_count(store) == 2


def test_binance_withdrawal_net_amount_never_pairs_a_fee_deducted_credit_and_preserves_source_precision(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    outgoing = flow(store, 'binance/withdraw', dict(id='withdraw', amount='100.000000', transactionFee='1.000000',
        coin='USDC', status=6, applyTime='2026-10-01 00:00:00.123456', txId=TX, walletType=1, address=ADDRESS))
    incoming = flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
        dict(time=START + 1000, hash=TX, delta=dict(type='deposit', usdc='99.000000')))
    assert outgoing['execution_time_utc'] == '2026-10-01T00:00:00.123456+00:00'
    assert outgoing['wallet'] == 'FUNDING' and outgoing['timestamp_precision'] == 'microsecond'
    # 2.12.12: `amount` is what the recipient gets (the fee is debited on top), so 100 never credits 99.
    assert outgoing['fee_basis'] == 'separate_debit'
    append(store, [outgoing, incoming])
    assert store.match_transfers() == 0


def test_binance_withdrawal_net_amount_pairs_its_equal_credit(tmp_path):
    # Synthetic: 99.19808 USDC left with fee 0.2; the wallet debit was 99.39808.
    store = LedgerStore(tmp_path / 'db')
    outgoing = flow(store, 'binance/withdraw', dict(id='withdraw', amount='99.19808', transactionFee='0.2',
        coin='USDC', status=6, applyTime='2026-10-01 00:00:00', txId=TX, walletType=0, address=ADDRESS))
    incoming = flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
        dict(time=START + 1000, hash=TX, delta=dict(type='deposit', usdc='99.19808')))
    append(store, [outgoing, incoming])
    assert store.match_transfers() == 1
    with store.read() as conn:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM ledger_transfer_matches').fetchone()[0])
    assert (evidence['fee'], evidence['fee_asset'], evidence['fee_basis']) == ('0.2', 'USDC', 'separate_debit')


def test_fact_change_before_match_write_defers_ambiguous_pair(tmp_path, monkeypatch):
    from contextlib import contextmanager
    store = LedgerStore(tmp_path / 'db')
    append(store, [withdrawal(store, ident='unshared'), deposit(store, tx=None)])
    original = store.transaction
    called = False
    @contextmanager
    def changed():
        nonlocal called
        if not called:
            called = True
            with original() as conn:
                store.append(conn, deposit(store, 'new-candidate', tx=None))
        with original() as conn:
            yield conn
    monkeypatch.setattr(store, 'transaction', changed)
    assert store.match_transfers() == 0
    assert match_count(store) == 0


def test_failed_page_keeps_prior_rows_and_gap_reasons():
    class Client:
        max_pages = 5
        def request(self, method, path, params):
            if params['offset']:
                raise RuntimeError('private account failure')
            return [dict(id='first', insertTime=START, coin='USDT', amount='1', walletType=0, status=1)]
    rows, reasons, failed = collect_ledger.flow_history(Client(), 'deposit', START, START + 1000, page_size=1)
    assert rows[0]['id'] == 'first' and failed
    assert 'SOURCE_STREAM_FAILED' in reasons and 'UNQUERIED_WINDOW_GAP' in reasons
    assert 'private account failure' not in str(reasons)


def test_hl_pagination_bisects_dense_windows_and_enforces_budget():
    class Client:
        max_pages = 10
    calls = []
    def call(lo, hi):
        calls.append((lo, hi))
        return [dict(hash=str(t), time=t, delta={'type': 'deposit', 'usdc': '1.00'})
                for t in range(lo, min(hi + 1, lo + 2))]
    rows, reasons, failed = collect_ledger.history(Client(), call, START, START + 3, 2, lambda r: r['hash'])
    assert {r['time'] for r in rows} == set(range(START, START + 4))
    assert not reasons and not failed
    assert len(calls) > 1
    Client.max_pages = 1
    _, reasons, failed = collect_ledger.history(Client(), call, START, START + 3, 2, lambda r: r['hash'])
    assert failed and 'PAGE_BUDGET' in reasons
    rows, reasons, failed = collect_ledger.history(Client(), lambda lo, hi: [dict(time=START + 99)],
                                                  START, START + 3, 2, lambda r: r['time'])
    assert not rows and failed and 'OUT_OF_WINDOW_RESPONSE' in reasons


def test_legacy_wallet_record_is_enriched_without_duplicate_outgoing_leg(tmp_path):
    from portfolio_ledger import normalize_rows
    store = LedgerStore(tmp_path / 'db')
    legacy = dict(source='binance_transfers', account_identity=store.account_identity('binance:primary'),
        source_record_id='42', native_record_id='42', kind='transfer_internal', transfer_route='MAIN_UMFUTURE',
        amount='50', currency='USDC', execution_time_utc='2026-10-01T00:00:00.000+00:00',
        source_timezone='UTC', timestamp_precision='millisecond', identity_quality='native_record',
        record_kind='source_record', source_status='CONFIRMED')
    append(store, [legacy])
    with store.read() as conn:
        events = normalize_rows(store, conn, 'binance/transfer_MAIN_UMFUTURE',
            dict(tranId=42, timestamp=START, amount='50.00', asset='USDC', status='CONFIRMED'), {})
    assert append(store, events) == {'new': 1, 'duplicate': 1, 'conflict': 0, 'skipped': 0}
    assert store.match_transfers() == 1
    assert append(store, events)['new'] == 0
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 2
        assert store.effective_event(conn, store.event_id(legacy))['direction'] == 'out'


def test_fee_basis_evidence_enriches_unknown_then_matches(tmp_path):
    # A withdrawal stored before 2.12.12 has no basis; the next fetch of it records one as an enrichment.
    store = LedgerStore(tmp_path / 'db')
    outgoing = flow(store, 'binance/withdraw', dict(id='withdraw', amount='100.000000', transactionFee='1.000000',
        coin='USDC', status=6, applyTime='2026-10-01 00:00:00', txId=TX))
    incoming = flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
        dict(time=START + 1000, hash=TX, delta=dict(type='deposit', usdc='100.000000')))
    assert outgoing['fee_basis'] == 'separate_debit'
    append(store, [{**outgoing, 'fee_basis': None}, incoming])
    assert store.match_transfers() == 0
    assert append(store, [outgoing])['conflict'] == 0
    assert store.match_transfers() == 1


def test_original_timestamp_precision_detects_microsecond_conflict(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    raw = dict(id='withdraw', amount='1.00', coin='USDC', status=6, applyTime='2026-10-01 00:00:00.123456')
    first = flow(store, 'binance/withdraw', raw)
    second = flow(store, 'binance/withdraw', {**raw, 'applyTime': '2026-10-01 00:00:00.123457'})
    assert append(store, [first])['new'] == 1
    assert append(store, [second])['conflict'] == 1
    for fraction, expected in (('.123', 'millisecond'), ('.000000', 'microsecond'), ('.1', 'fractional_second_1')):
        normalized = flow(store, 'binance/withdraw', {**raw, 'applyTime': '2026-10-01 00:00:00' + fraction})
        assert normalized['timestamp_precision'] == expected
