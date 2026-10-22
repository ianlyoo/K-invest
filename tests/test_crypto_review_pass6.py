"""Integrated supplemental-save, preserved-conflict and legacy-conflict regressions."""
import hashlib
from datetime import timedelta

import httpx
import pytest
from test_collect_ledger import Binance, Hyperliquid, collect_ledger
from test_crypto_flow_coverage import hl
from test_crypto_flow_intervals import (
    ADDRESS,
    OTHER,
    T0,
    T1,
    bn_transfer,
    identities,
    ms,
    result,
    setup,
)
from test_toss_fill_evidence import (
    client as toss_client,  # noqa: F401 — shared real-client HTTP fixture
)

import crypto_flow_evidence as evidence
from portfolio_ledger import LedgerStore, normalize_rows
from portfolio_sources import PortfolioCollector, Providers


def page(amount='7', *, stamp=None, native='fixture-transfer'):
    return [{'time': ms(stamp or T0 + timedelta(minutes=1)), 'hash': native,
             'delta': {'type': 'internalTransfer', 'usdc': amount, 'fee': '0',
                       'user': OTHER, 'destination': ADDRESS}}]


def strict(store, rows):
    client = hl(lambda request: httpx.Response(200, json=rows))
    selected, reasons, failed = collect_ledger.hyperliquid_flow_history(client, ADDRESS, ms(T0), ms(T1))
    assert not reasons and not failed
    account = identities(store)['hyperliquid']
    evidence.FlowEvidence(store).raw('hyperliquid/' + account + '/userNonFundingLedgerUpdates',
                                    selected, account, ms(T0), ms(T1))


def legacy(store, rows):
    account = identities(store)['hyperliquid']
    run = collect_ledger.CollectionRun(store)
    run.raw('hyperliquid/' + account + '/userNonFundingLedgerUpdates', rows, account, ms(T0), ms(T1))
    assert not run.failed  # C9: preserved contradictions do not fail ingestion.


@pytest.mark.parametrize('amounts', [('7',), ('7', '8'), ('8', '7'), ('7', '8', '8')])
def test_known_legacy_contradictions_remain_unresolved_after_strict_fetch(tmp_path, amounts):
    store = setup(tmp_path)
    for amount in amounts:
        legacy(store, page(amount))
    strict(store, page('8'))
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] is None and value['confidence'] == 'unresolved'
    assert 'LEDGER_CONTENT_CONFLICT' in value['reasons']
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == (0 if len(amounts) == 1 else 1)


def test_complete_empty_strict_fetch_does_not_erase_accepted_legacy_movement(tmp_path):
    store = setup(tmp_path)
    legacy(store, page())
    strict(store, [])
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] is None
    assert 'CRYPTO_LEGACY_FLOW_NOT_RECONCILED' in value['reasons']


@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('conflict', [False, True])
def test_alias_binding_and_same_second_precision_preserve_financial_conflicts(tmp_path, alias, conflict):
    store = setup(tmp_path)
    account = identities(store)['hyperliquid']
    stamp = T0 + timedelta(minutes=1)
    with store.read() as conn:
        old = normalize_rows(store, conn, 'hyperliquid/' + account + '/userNonFundingLedgerUpdates', page()[0], {})[0]
    if alias:
        old['account_identity'] = store.account_identity('hyperliquid:old-fixture-alias')
    store.append_many([(old, None)])
    if alias:
        with store.transaction() as conn:
            store.bind_account(conn, 'hyperliquid:old-fixture-alias', 'hyperliquid:' + ADDRESS)
    strict(store, page('8' if conflict else '7', stamp=stamp + timedelta(milliseconds=500)))
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] == (None if conflict else '7')
    assert ('LEDGER_CONTENT_CONFLICT' in value['reasons']) is conflict


def test_unrelated_native_identity_and_out_of_window_legacy_facts_do_not_poison_flow(tmp_path):
    store = setup(tmp_path)
    legacy(store, page('7', native='outside', stamp=T0 - timedelta(days=1)))
    strict(store, page('8'))
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] == '8' and value['confidence'] == 'observed'


@pytest.mark.parametrize('changed', [False, True])
def test_legacy_numeric_identity_spelling_binds_without_hiding_amount_changes(tmp_path, changed):
    store = setup(tmp_path)
    account = identities(store)['binance']
    run = collect_ledger.CollectionRun(store)
    run.raw('binance/transfer_MAIN_UMFUTURE', [dict(tranId='0007', timestamp=ms(T0 + timedelta(minutes=1)),
        amount='7', asset='USDT', type='MAIN_UMFUTURE', status='CONFIRMED')], account, ms(T0), ms(T1))
    bn_transfer(store, ident=7, amount='8' if changed else '7')
    value = result(store)[0]
    assert value['flow_native'] == (None if changed else '7')
    assert ('LEDGER_CONTENT_CONFLICT' in value['reasons']) is changed


@pytest.mark.parametrize('anchor_inside', [False, True])
def test_preserved_conflict_veto_includes_both_original_and_candidate_time(tmp_path, anchor_inside):
    store = setup(tmp_path)
    account = identities(store)['hyperliquid']
    inside, outside = T0 + timedelta(minutes=1), T0 - timedelta(days=1)
    with store.read() as conn:
        original, candidate = [normalize_rows(store, conn, 'hyperliquid/' + account + '/userNonFundingLedgerUpdates',
            page(amount, stamp=stamp)[0], {})[0] for amount, stamp in
            [('7', inside if anchor_inside else outside), ('8', outside if anchor_inside else inside)]]
    candidate['source_record_id'] = original['source_record_id']
    store.append_many([(original, None), (candidate, None)])
    strict(store, page('7' if anchor_inside else '8'))
    value = result(store, 'hyperliquid')[0]
    assert value['flow_native'] is None and 'LEDGER_CONTENT_CONFLICT' in value['reasons']


@pytest.mark.parametrize('stage', ['normalize', 'save'])
def test_toss_supplemental_persistence_exceptions_cannot_fail_legacy_job(request, tmp_path, monkeypatch, stage):
    client = request.getfixturevalue('toss_client')
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda _: client.api,
        kis_accounts=lambda: [], kis_client=lambda _: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=True)
    store = LedgerStore(tmp_path / 'db')
    original = collect_ledger.collect_toss_inference
    def supplemental(*args, **kwargs):
        def failed(*a, **k):
            raise OSError('synthetic')
        with monkeypatch.context() as patch:
            patch.setattr(collect_ledger if stage == 'normalize' else collect_ledger.CollectionRun,
                          'normalize_transaction' if stage == 'normalize' else 'save', failed)
            return original(*args, **kwargs)
    monkeypatch.setattr(collect_ledger, 'collect_toss_inference', supplemental)
    value = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
        binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    assert value['failed'] is False
    assert value['toss_inference']['status'] == 'limited'
    assert all('SOURCE_COLLECTION_FAILED' not in row['coverage']['reason_codes'] for row in value['streams'].values())


def test_supplemental_save_contract_keeps_skips_and_failures_out_of_legacy_exit(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    run = collect_ledger.CollectionRun(store)
    account = store.account_identity('toss:primary')
    run.save('toss/fills-v1/fixture', [None], account, '2026-03-28', '2026-03-29', failed=True, affects_exit=False)
    assert not run.failed
    assert run.counts['stream-1']['coverage']['status'] == 'partial'
