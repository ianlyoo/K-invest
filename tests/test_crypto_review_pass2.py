"""Offline compatibility and coverage repair for re-review findings R1/R4(b).

The Binance R2 counterexample remains a documented blocker, not a fixed claim.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta

import httpx
import pytest
from test_collect_ledger import Binance, Equity, collect_ledger
from test_crypto_flow_coverage import hl
from test_crypto_flow_intervals import ADDRESS, OTHER, T0, T1, ms, result, setup
from test_crypto_nav_capture import capture, examples, nav
from test_crypto_review_regressions import persist

from crypto_flow_evidence import FlowEvidence
from crypto_flow_intervals import _coverage
from crypto_futures_accounts import BINANCE_ALLOWLIST
from portfolio_ledger import LedgerStore, canonical


def incomplete_delta(case):
    delta = {'type': 'internalTransfer', 'usdc': '7', 'user': OTHER, 'destination': ADDRESS, 'fee': '0'}
    if case.startswith('spot'):
        delta = {'type': 'spotTransfer', 'amount': '7', 'token': 'USDC', 'usdcValue': '7',
                 'user': OTHER, 'destination': ADDRESS, 'fee': '0'}
    elif case.startswith('withdraw'):
        delta = {'type': 'withdraw', 'usdc': '7', 'fee': '0', 'nonce': 1}
    elif case == 'send_fee':
        delta = {'type': 'send', 'amount': '7', 'token': 'USDC', 'user': OTHER,
                 'destination': ADDRESS, 'sourceDex': 'spot', 'destinationDex': ''}
    if case.endswith('_fee'):
        delta.pop('fee', None)
    elif case == 'withdraw_nonce':
        del delta['nonce']
    elif case == 'spot_value':
        del delta['usdcValue']
    elif case.endswith('_fee_null'):
        delta['fee'] = None
    elif case == 'extra_delta':
        delta['partial'] = True
    return delta


COMPAT_CASES = ['internal_fee', 'spot_fee', 'withdraw_nonce', 'spot_value',
                'withdraw_fee', 'send_fee', 'internal_fee_null', 'spot_fee_null',
                'extra_delta', 'extra_row']


@pytest.mark.parametrize('case', COMPAT_CASES)
@pytest.mark.parametrize('mixed', [False, True])
def test_legacy_ingestion_and_exit_survive_strict_rejection(tmp_path, monkeypatch, case, mixed):
    delta = incomplete_delta(case)
    page = [{'time': ms(T0 + timedelta(minutes=1)), 'hash': 'review-fixture', 'delta': delta}]
    if case == 'extra_row':
        page[0]['partial'] = True
    if mixed:
        page.insert(0, {'time': ms(T0 + timedelta(seconds=1)), 'hash': 'valid-deposit',
                        'delta': {'type': 'deposit', 'usdc': '2'}})
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=deepcopy(page) if body['type'] == 'userNonFundingLedgerUpdates' else [])

    # Frozen b8b19ff flow behavior: the unchanged legacy history/raw path,
    # including its historical advisory and CollectionRun exit calculation.
    legacy = LedgerStore(tmp_path / 'legacy.db')
    client = hl(handler)
    rows, reasons, failed = collect_ledger.history(client,
        lambda a, b: client.info('userNonFundingLedgerUpdates', ADDRESS, startTime=a, endTime=b),
        ms(T0), ms(T1), 500,
        lambda r: (r.get('tid'), r.get('hash'), r.get('time'), canonical(r.get('delta'))))
    legacy_run = collect_ledger.CollectionRun(legacy)
    account = legacy.hl_identity(ADDRESS)
    legacy_run.raw('hyperliquid/' + account + '/userNonFundingLedgerUpdates', rows, account,
                   ms(T0), ms(T1), [*reasons, 'HISTORY_CAP_UNVERIFIED'], failed)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T1 + timedelta(days=1)).astimezone(tz)
    monkeypatch.setattr(collect_ledger, 'datetime', Clock)
    store = setup(tmp_path)
    current = collect_ledger.collect(store, '2026-10-13', '2026-10-14', equity=Equity(),
        binance_factory=Binance, hyperliquid_factory=lambda: hl(handler))
    counts = []
    for source in (legacy, store):
        with source.read() as conn:
            counts.append(conn.execute("SELECT count(*) FROM ledger_events WHERE source='hyperliquid_flows'").fetchone()[0])
    assert (legacy_run.failed, counts[0]) == (False, len(page))
    assert (current['failed'], counts[1]) == (legacy_run.failed, counts[0])
    with store.read() as conn:
        evidence = list(conn.execute("SELECT json_extract(payload_json,'$.status') AS status,json_extract(payload_json,'$.verified_zero') AS verified_zero,json_extract(payload_json,'$.reasons') AS reason_codes_json FROM crypto_flow_windows WHERE source='hyperliquid' ORDER BY rowid DESC LIMIT 1"))
    assert evidence and all(r['status'] != 'complete' and r['verified_zero'] == 0 for r in evidence)
    strict_reasons = {code for r in evidence for code in json.loads(r['reason_codes_json'])}
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] is None and value['confidence'] == 'unresolved'
    assert strict_reasons & set(value['reasons'])


@pytest.mark.parametrize('malformed', [False, True])
def test_supplemental_failure_preserves_exit_and_distinguishes_semantic_evidence(tmp_path, monkeypatch, malformed):
    store = setup(tmp_path)
    calls = 0
    def handler(request):
        nonlocal calls
        body = json.loads(request.content)
        if body['type'] == 'userNonFundingLedgerUpdates':
            calls += 1
            if calls > 1:
                return httpx.Response(200, content=b'[{') if malformed else httpx.Response(503)
        return httpx.Response(200, json=[])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T1 + timedelta(days=1)).astimezone(tz)
    monkeypatch.setattr(collect_ledger, 'datetime', Clock)
    value = collect_ledger.collect(store, '2026-10-13', '2026-10-14', equity=Equity(),
        binance_factory=Binance, hyperliquid_factory=lambda: hl(handler))
    assert value['failed'] is False and calls == 2
    inferred = result(store, 'hyperliquid')[0]
    assert inferred['flow_native'] is None if malformed else inferred['flow_native'] == '0'
    assert ('CRYPTO_INVALID_RESPONSE' in inferred['reasons']) == malformed


@pytest.mark.parametrize('case', ['valid', 'enrichment', 'conflict', 'strict_only'])
def test_strict_evidence_keeps_recent_coverage_usable_and_preserves_legacy_facts(tmp_path, monkeypatch, case):
    store = setup(tmp_path)
    delta = {'type': 'internalTransfer', 'usdc': '7', 'user': OTHER, 'destination': ADDRESS, 'fee': '0'}
    strict = [{'time': ms(T0 + timedelta(minutes=1)), 'hash': 'fixture', 'delta': delta}]
    legacy = deepcopy(strict)
    if case == 'enrichment':
        del legacy[0]['delta']['fee']
    elif case == 'conflict':
        strict[0]['delta']['usdc'] = '8'
    elif case == 'strict_only':
        legacy = []
    calls = 0
    def handler(request):
        nonlocal calls
        if json.loads(request.content)['type'] != 'userNonFundingLedgerUpdates':
            return httpx.Response(200, json=[])
        calls += 1
        return httpx.Response(200, json=legacy if calls == 1 else strict)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T1 + timedelta(days=1)).astimezone(tz)
    monkeypatch.setattr(collect_ledger, 'datetime', Clock)
    value = collect_ledger.collect(store, '2026-10-13', '2026-10-14', equity=Equity(),
        binance_factory=Binance, hyperliquid_factory=lambda: hl(handler))
    assert value['failed'] is False and calls == 2
    with store.read() as conn:
        saved = conn.execute("SELECT event_id FROM ledger_events WHERE source='hyperliquid_flows'").fetchone()
        event = store.effective_event(conn, saved[0]) if saved else None
        proofs = list(conn.execute("SELECT json_extract(payload_json,'$.status') AS status FROM crypto_flow_windows WHERE source='hyperliquid' ORDER BY rowid DESC LIMIT 1"))
        assert conn.execute('SELECT count(*) FROM ledger_enrichments').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == 0
    if case == 'strict_only':
        assert event is None
    else:
        assert event['amount'] == '7' and event['fee'] == (None if case == 'enrichment' else '0')
    inferred = result(store, 'hyperliquid')[0]
    assert inferred['flow_native'] == (None if case == 'conflict' else '7')
    assert inferred['confidence'] == ('unresolved' if case == 'conflict' else 'observed')
    if case == 'conflict':
        assert 'LEDGER_CONTENT_CONFLICT' in inferred['reasons']
    assert proofs and all(row['status'] == 'complete' for row in proofs)


def test_two_new_complete_daily_windows_repair_broad_pagination_failure(tmp_path):
    store = setup(tmp_path)
    persist(store, 'transfer_MAIN_UMFUTURE', [], ['PAGINATION_WINDOW_GAP'], True)
    account = store.account_identity('binance:primary')
    mid = T1.replace(hour=0, minute=0, second=0, microsecond=0)
    for lo, hi in [(T0.replace(hour=0, minute=0), mid - timedelta(milliseconds=1)),
                   (mid, mid + timedelta(days=1) - timedelta(milliseconds=1))]:
        FlowEvidence(store).raw('binance/transfer_MAIN_UMFUTURE', [], account, ms(lo), ms(hi))
    value = result(store)[0]
    assert value['flow_native'] == '0' and value['confidence'] == 'observed'
    assert value['reasons'] == []


def coverage_row(ident, lo, hi, observed, *, status='complete', reasons=(), count=0, zero=1, account='fixture'):
    return dict(coverage_id=ident, source='hyperliquid', account_identity=account,
        stream='hyperliquid/fixture/userNonFundingLedgerUpdates', start_time=ms(lo), end_time=ms(hi),
        observed_at=observed.isoformat() if observed else None, status=status,
        reason_codes_json=json.dumps(list(reasons)), row_count=count, verified_zero=zero)


@pytest.mark.parametrize('case,repaired', [
    ('adjacent', True), ('overlap', True), ('reversed', True), ('populated', True),
    ('gap', False), ('older_half', False), ('same_time_half', False), ('missing_time', False),
    ('unverified_empty', False), ('limited_half', False), ('reasons_half', False),
    ('wrong_account', False), ('wrong_stream', False), ('wrong_venue', False),
    ('first_millisecond_gap', False), ('second_contradiction', False),
    ('clipped_overlap', True), ('last_instant', True),
])
def test_repair_union_requires_complete_newer_evidence_for_every_affected_millisecond(case, repaired):
    observed = T1 + timedelta(days=1)
    mid = T0 + (T1 - T0) / 2
    old = coverage_row('old', T0, T1, observed - timedelta(hours=1))
    bad = coverage_row('bad', T0, T1, observed, status='limited', reasons=['PAGINATION_WINDOW_GAP'], zero=0)
    left = coverage_row('left', T0, mid - timedelta(milliseconds=1), observed + timedelta(hours=1))
    right = coverage_row('right', mid, T1, observed + timedelta(hours=2))
    repairs = [left, right]
    if case == 'overlap':
        left['end_time'] = ms(mid + timedelta(minutes=1))
    elif case == 'reversed':
        repairs.reverse()
    elif case == 'populated':
        left.update(row_count=2, verified_zero=0)
    elif case == 'gap':
        right['start_time'] += 1
    elif case == 'older_half':
        left['observed_at'] = old['observed_at']
    elif case == 'same_time_half':
        left['observed_at'] = bad['observed_at']
    elif case == 'missing_time':
        left['observed_at'] = None
    elif case == 'unverified_empty':
        left['verified_zero'] = 0
    elif case == 'limited_half':
        left['status'] = 'limited'
    elif case == 'reasons_half':
        left['reason_codes_json'] = '["HISTORY_CAP_UNVERIFIED"]'
    elif case == 'wrong_account':
        left['account_identity'] = 'other'
    elif case == 'wrong_stream':
        left['stream'] = 'hyperliquid/fixture/userFunding'
    elif case == 'wrong_venue':
        left['source'] = 'binance'
    elif case == 'first_millisecond_gap':
        bad['start_time'] = ms(T0 + timedelta(hours=1))
        left['start_time'] = bad['start_time'] + 1
    elif case == 'second_contradiction':
        repairs.append(coverage_row('bad2', mid, T1, observed + timedelta(hours=3),
                       status='partial', reasons=['CRYPTO_INVALID_RESPONSE'], zero=0))
    elif case == 'clipped_overlap':
        bad.update(start_time=ms(T0 - timedelta(days=1)), end_time=ms(T1 + timedelta(days=1)))
    elif case == 'last_instant':
        bad['start_time'] = ms(T1)
    reasons, _ = _coverage([old, bad, *repairs], 'hyperliquid', {'fixture'}, T0, T1)
    assert (reasons == []) == repaired


@pytest.mark.parametrize('mark,pnl,account_pnl', [('110', '10', '10.01'), ('110.02', '10.02', '10.03')])
def test_r2_mark_move_resolves_with_coherent_approved_source(tmp_path, mark, pnl, account_pnl):
    assert ('GET', '/fapi/v3/account') in BINANCE_ALLOWLIST
    data = examples()
    data['positions'][0].update(entryPrice='100', breakEvenPrice='100', positionAmt='1',
        markPrice=mark, notional=mark, unRealizedProfit=pnl)
    data['balance'][0]['crossUnPnl'] = account_pnl
    path = tmp_path / 'risk.db'
    capture(path, data)
    value = nav(path)['binance']
    assert value['value_krw_decimal'] == '154000.00000000'
    assert not value['reason_codes']
