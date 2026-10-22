"""Risk-job legacy compatibility and optional snapshot evidence isolation."""
import io
import json
import os
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest
from test_crypto_nav_capture import (
    account_example,
    capture,
    examples,
    nav,
    quote_client,
    tickers,
    zero_asset,
)

from futures_risk import RiskCollector

ROOT = Path(__file__).parents[1]
RISK_CASES = ['published_coherent', 'published_illustration', 'hl_default', 'hl_spot_extra', 'hl_state_extra',
    *(endpoint + ':' + failure for endpoint in ('account', 'config', 'config_after')
      for failure in ('503', '429', 'timeout', 'missing', 'truncated')),
    'positions:503', 'balance:429', 'state:503', 'positions:truncated', 'balance:truncated', 'state:truncated']
SNAPSHOT_CASES = ['snapshot:' + case for case in ('exception', 'mutation_exception', 'serialization', 'quote_timeout',
                  'quote_429', 'quote_invalid', 'capture_exception', 'invalid_nav')]


def published_account():
    account = account_example()
    account.update({key: '0.00000000' for key in account if key not in ('assets', 'positions')})
    for key in ('totalWalletBalance', 'totalMarginBalance', 'totalCrossWalletBalance', 'availableBalance', 'maxWithdrawAmount'):
        account[key] = '126.72469206'
    asset = zero_asset('USDT', '23.72469206')
    asset.update(availableBalance='23.72469206', maxWithdrawAmount='23.72469206', updateTime=1625474304765)
    account['assets'] = [asset]
    account['positions'] = [dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt='1.000',
        unrealizedProfit='0.00000000', isolatedMargin='0.00000000', notional='0', isolatedWallet='0',
        initialMargin='0', maintMargin='0', updateTime=0)]
    return account


@pytest.fixture(scope='module')
def replay(tmp_path_factory, require_git_commit):
    require_git_commit('b8b19ff')
    directory = tmp_path_factory.mktemp('crypto-risk-full-tree-replay')
    baseline = directory / 'baseline'
    baseline.mkdir()
    archive = subprocess.run(['git', 'archive', 'b8b19ff'], cwd=ROOT, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(baseline, filter='data')
    fixtures = directory / 'fixtures.json'
    fixtures.write_text(json.dumps({'examples': examples(), 'published_account': published_account(),
        'tickers': tickers(), 'fx': quote_client().read()}))
    outcomes = []
    for name, tree in [('baseline', baseline), ('candidate', ROOT)]:
        env = {'PATH': os.environ.get('PATH', ''), 'TMPDIR': str(directory),
               'PYTHONDONTWRITEBYTECODE': '1', 'KINVEST_CACHE_DIR': str(directory / name / 'cache')}
        child = subprocess.run([sys.executable, str(ROOT / 'tests/_crypto_risk_replay.py'), str(tree),
            str(directory / name / 'data'), str(fixtures), json.dumps([*RISK_CASES, *SNAPSHOT_CASES])],
            cwd=tree, env=env, capture_output=True, text=True, timeout=90)
        assert child.returncode == 0, child.stderr
        outcomes.append(json.loads(child.stdout))
    return outcomes


@pytest.mark.parametrize('case', RISK_CASES)
def test_risk_job_full_tree_differential_preserves_all_legacy_rows_and_exit(replay, case):
    baseline, current = (run[case] for run in replay)
    for field in ('legacy', 'tables', 'exit_code', 'job_output'):
        assert current[field] == baseline[field], (case, field)
    assert current['trace'][:len(baseline['trace'])] == baseline['trace']
    assert not any(call[1] in ('/fapi/v3/account', '/fapi/v1/accountConfig') for call in baseline['trace'])
    expected_failure = case.split(':')[0] in ('positions', 'balance', 'state')
    assert baseline['exit_code'] == int(expected_failure)
    assert len([r for r in current['legacy']['positions'] if r['venue'] == 'hyperliquid']) == (0 if case.startswith('state:') else 2)
    if case.split(':')[0] in ('account', 'config', 'config_after') or case == 'published_illustration':
        assert current['nav']['binance']['value_krw_decimal'] is None
        assert current['nav']['binance']['reason_codes']
    elif not expected_failure:
        assert current['nav']['binance']['value_krw_decimal'] == '154000.00000000'
    if case in ('hl_spot_extra', 'hl_state_extra'):
        assert current['nav']['hyperliquid']['value_krw_decimal'] is None
    assert current['legacy_client_cooldown'] == baseline['legacy_client_cooldown']
    supplemental = current['nav_evidence']
    assert supplemental['version'] == 'crypto_risk_nav_v1'
    assert '0x' + 'a' * 40 not in json.dumps(supplemental)
    if not expected_failure:
        assert len(supplemental['venues'][0]['reads']) == 3
        assert len(current['legacy']['venues'][0]['reads']) == 2


@pytest.mark.parametrize('case', SNAPSHOT_CASES)
def test_snapshot_job_optional_failure_preserves_broker_rows_status_and_exit(replay, case):
    baseline, current = (run[case] for run in replay)
    for field in ('legacy', 'tables', 'exit_code', 'calls'):
        assert current[field] == baseline[field], (case, field)
    assert current['exit_code'] == 0
    assert current['tables']['nav_collection_attempts'][0]['status'] == 'complete'
    assert current['tables']['nav_snapshots'][0]['total_value_krw'] == 1000
    assert baseline['equity_additions'] == {}
    assert current['equity_additions'] == {'toss:primary': {
        'instrument_bindings': {}, 'instrument_binding_reasons': [], 'open_order_count': None,
        'cash': {currency: {'value_decimal': None, 'observed_at': None} for currency in ('KRW', 'USD')}}}
    for venue in ('binance', 'hyperliquid'):
        assert current['crypto'][venue]['value_krw'] is None
        assert current['crypto'][venue]['reason_codes']
    # 2.13.0: the KIS capture is additive; without a KIS client it is a reason inside kis_capture only.
    assert baseline['kis'] is None
    assert current['kis'] == {'version': 'kis_ledgerless_v1', 'accounts': {}, 'reasons': ['KIS_CAPTURE_FAILED']}


@pytest.mark.parametrize('failure', ['exception', 'serialization'])
def test_unexpected_nav_phase_failure_cannot_fail_persisted_risk(tmp_path, monkeypatch, failure):
    original = RiskCollector._capture_nav
    def fail(self, legacy, context):
        if failure == 'exception':
            raise RuntimeError('synthetic')
        return {'bad': object()}
    data = examples()
    good = capture(tmp_path / 'good.db', deepcopy(data))
    monkeypatch.setattr(RiskCollector, '_capture_nav', fail)
    broken = capture(tmp_path / 'broken.db', deepcopy(data))
    for field in ('exit_code', 'source_status', 'data_status', 'reason_codes'):
        assert broken[field] == good[field]
    assert broken['exit_code'] == 0
    assert nav(tmp_path / 'broken.db')['binance']['value_krw'] is None
    assert broken['nav_evidence']['venues'][0]['reason_codes'] == ['CRYPTO_NAV_COLLECTION_FAILED']
    monkeypatch.setattr(RiskCollector, '_capture_nav', original)


def test_optional_crypto_output_serialization_failure_stays_in_crypto_channel():
    from daily_nav import crypto_nav

    value = crypto_nav(None, {'crypto_fx_times': {'USDT': object()}})
    assert value['crypto_nav']['binance']['value_krw'] is None
    assert value['consolidated_nav_krw']['reason_codes'] == ['CRYPTO_NAV_EVIDENCE_UNAVAILABLE']
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize('valid', [True, False])
def test_interval_conversion_uses_separate_crypto_marks(tmp_path, valid):
    from test_crypto_flow_intervals import (
        T0,
        T1,
        bn_transfer,
        coverage,
        result,
        snapshot,
    )

    from portfolio_ledger import LedgerStore
    from portfolio_store import NavStore

    store = LedgerStore(tmp_path / 'db')
    for stamp, ident in ((T0, 'start'), (T1, 'end')):
        payload = snapshot(store, stamp, ident)
        payload['crypto_nav']['fx_times'] = payload.pop('fx_times')
        if not valid:
            payload['crypto_nav']['fx_times']['USDT']['rate_to_krw'] = None
        payload.update(snapshot_id=ident + '-crypto-fx', version=2)
        with NavStore(store.db_path)._connect() as conn:
            NavStore._insert_version(conn, payload)
    coverage(store)
    bn_transfer(store)
    value = result(store)[0]
    assert value['flow_native'] == '50'
    assert value['flow_krw'] == ('70000' if valid else None)
