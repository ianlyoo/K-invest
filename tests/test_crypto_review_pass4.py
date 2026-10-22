"""Full-tree b8b19ff differential replay and supplemental isolation gates."""
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
from test_collect_ledger import Binance, Equity, Hyperliquid, collect_ledger
from test_crypto_flow_coverage import binance, hl
from test_crypto_flow_intervals import result
from test_crypto_review_pass2 import COMPAT_CASES

import provider_pacing
from crypto_flow_evidence import FlowEvidence
from crypto_futures_accounts import FuturesError
from ledger_flows import BINANCE_WITHDRAWAL_FEE_BASIS
from portfolio_ledger import LedgerStore, canonical, digest

ROOT = Path(__file__).parents[1]
CASES = ['published', 'wallet_missing', 'wallet_unknown', 'wallet_extra',
         'supplemental_429', 'supplemental_503', 'supplemental_json', 'supplemental_budget',
         'supplemental_db_locked', 'supplemental_db_after_insert',
         'toss_null_commission', 'toss_null_tax', 'toss_strict_503', 'toss_strict_timeout',
         'toss_strict_429', 'toss_strict_precision', 'toss_strict_only',
         *COMPAT_CASES, *('mixed_' + case for case in COMPAT_CASES)]


@pytest.fixture(scope='module')
def replay(tmp_path_factory, require_git_commit):
    require_git_commit('b8b19ff')
    directory = tmp_path_factory.mktemp('crypto-full-tree-replay')
    baseline = directory / 'baseline'
    baseline.mkdir()
    archive = subprocess.run(['git', 'archive', 'b8b19ff'], cwd=ROOT, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(baseline, filter='data')
    outcomes = []
    for name, tree in [('baseline', baseline), ('candidate', ROOT)]:
        env = {'PATH': os.environ.get('PATH', ''), 'TMPDIR': str(directory),
               'PYTHONDONTWRITEBYTECODE': '1', 'KINVEST_CACHE_DIR': str(directory / name / 'cache')}
        child = subprocess.run([sys.executable, str(ROOT / 'tests/_crypto_ledger_replay.py'),
            str(tree), str(directory / name / 'data'), json.dumps(CASES)], cwd=tree,
            env=env, capture_output=True, text=True, timeout=90)
        assert child.returncode == 0, child.stderr
        outcomes.append(json.loads(child.stdout))
    return outcomes


def without_withdrawal_fee_basis(run):
    """2.12.12 intentionally stores the Binance withdrawal fee basis (b8b19ff: null).

    Reverse exactly that field; every content and raw-page hash must then match again.
    """
    run, changed = copy.deepcopy(run), set()
    for key, text in run['raw'].items():
        page = json.loads(text)
        for fact in page:
            if fact.get('source') == 'binance_withdraw':
                assert fact['fee_basis'] == BINANCE_WITHDRAWAL_FEE_BASIS
                fact['fee_basis'] = None
                changed.add(key)
        run['raw'][key] = canonical(page)
    for row in run['tables']['ledger_events']:
        if row['source'] == 'binance_withdraw':
            payload = {**json.loads(row['payload_json']), 'fee_basis': None}
            row['payload_json'], row['content_sha256'] = canonical(payload), digest(payload)
    for row in run['tables']['ledger_evidence']:
        if f"{row['run_id']}/{row['page_path']}" in changed:
            row['file_sha256'] = hashlib.sha256(run['raw'][f"{row['run_id']}/{row['page_path']}"].encode()).hexdigest()
    for table in ('ledger_events', 'ledger_evidence'):
        run['tables'][table].sort(key=lambda row: json.dumps(row, sort_keys=True))
    return run


@pytest.mark.parametrize('case', CASES)
def test_every_legacy_stream_matches_full_b8b19ff_tree(replay, case):
    baseline, current = replay[0][case], without_withdrawal_fee_basis(replay[1][case])
    for field in ('legacy', 'exit_code', 'tables', 'trace', 'raw'):
        assert current[field] == baseline[field], (case, field)
    sources = {event['source'] for event in current['tables']['ledger_events']}
    expected = {'toss_transactions', 'kis_transactions', 'binance_income', 'binance_fills',
            'binance_transfers', 'binance_deposit', 'binance_withdraw', 'hyperliquid_fills',
            'hyperliquid_funding', 'hyperliquid_flows'}
    assert expected - ({'toss_transactions'} if case == 'toss_strict_only' else set()) <= sources
    assert len([row for row in current['tables']['ledger_events'] if row['source'] == 'hyperliquid_fills']) == 2
    assert baseline['exit_code'] == 0
    assert current['all_requests'] > len(current['trace'])
    assert current['inference']['status'] in ('complete', 'limited')
    assert current['inference']['status'] == ('complete' if case == 'published' or case.startswith('toss_') else 'limited')
    if case.startswith('supplemental_db_'):
        assert current['strict_windows'] == 0
    assert not baseline['toss_requests'] and current['toss_requests']
    assert len(current['toss_proofs']) == 1 and not baseline['toss_proofs']
    assert current['toss_proofs'][0]['status'] == ('limited' if case.startswith('toss_')
                                                 and case != 'toss_strict_only' else 'complete')


@pytest.mark.parametrize('venue', ['binance', 'hyperliquid'])
def test_supplemental_429_cannot_poison_original_client(venue, monkeypatch):
    monkeypatch.setattr(provider_pacing, 'BINANCE_BUDGET', provider_pacing.RequestBudget())
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '60'}) if len(calls) == 1 else httpx.Response(200, json=[])
    original = binance(handler) if venue == 'binance' else hl(handler)
    supplemental = original.inference_client()
    original_spacing = original._next_request
    def read(client):
        return client.request('GET', '/fapi/v3/balance') if venue == 'binance' else client.info('userFunding', client._addresses[0])
    with pytest.raises(FuturesError) as error:
        read(supplemental)
    assert error.value.code == 'FUTURES_RATE_LIMITED'
    assert original._next_request == original_spacing
    assert original._cooldown_until == 0
    assert read(original) == []
    with pytest.raises(FuturesError) as error:
        read(supplemental)
    assert error.value.code == 'FUTURES_RATE_LIMITED'
    assert len(calls) == 2


def test_supplemental_budget_shares_admission_but_not_new_cooldown():
    clock, sleeps = [0.0], []
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    original = provider_pacing.RequestBudget(clock=lambda: clock[0], sleeper=sleep)
    supplemental = provider_pacing.SupplementalBudget(original)
    with original.request(interval=2):
        pass
    with supplemental.request(interval=2, sleeper=sleep):
        pass
    assert sleeps == [2]
    supplemental.defer(429, 60)
    with original.request(interval=2):
        pass
    assert sleeps == [2, 2]
    original.defer(429, 60)
    another = provider_pacing.SupplementalBudget(original)
    with pytest.raises(provider_pacing.RequestThrottled):
        with another.request():
            pytest.fail('prior provider cooldown was bypassed')


@pytest.mark.parametrize('stage', ['phase', 'write'])
def test_supplemental_exception_never_changes_job_exit(tmp_path, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise OSError('synthetic failure')
    monkeypatch.setattr(collect_ledger if stage == 'phase' else FlowEvidence,
                        'collect_inference' if stage == 'phase' else 'save', fail)
    store = LedgerStore(tmp_path / 'db')
    value = collect_ledger.collect(store, '2026-10-11', '2026-10-11', equity=Equity(),
        binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    assert value['failed'] is False
    assert value['inference']['status'] in ('unavailable', 'limited')
    assert all(row['coverage']['status'] != 'partial' for row in value['streams'].values())



def test_legacy_ledger_rows_and_zero_coverage_are_never_strict_proof(tmp_path):
    from test_crypto_flow_intervals import T0, T1, ms, snapshot
    store = LedgerStore(tmp_path / 'db')
    snapshot(store, T0, 'start')
    snapshot(store, T1, 'end')
    account = store.account_identity('binance:primary')
    run = collect_ledger.CollectionRun(store)
    for leaf in ('deposit', 'withdraw', *('transfer_' + r for r in __import__('crypto_futures_accounts').BINANCE_WALLET_ROUTES)):
        run.raw('binance/' + leaf, [], account, ms(T0), ms(T1))
    value = result(store)[0]
    assert value['flow_native'] is None and 'CRYPTO_FLOW_COVERAGE_INCOMPLETE' in value['reasons']
