"""Published Toss OpenAPI examples through real methods + mocked HTTP only."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from portfolio_ledger import LedgerStore, normalize_transaction
from portfolio_model import toss_transactions
from portfolio_sources import PortfolioCollector, Providers
from toss_api import TossClient

FIXTURES = json.loads(Path(__file__).with_name('fixtures').joinpath('toss_fill_openapi_1_2_24.json').read_text())


@pytest.fixture
def client(monkeypatch):
    from provider_pacing import RequestBudget

    monkeypatch.setattr('toss_api._RATE_BUDGETS', {'ORDER_HISTORY': RequestBudget(0)})
    pages = {'OPEN': [deepcopy(FIXTURES['empty'])], 'CLOSED': [deepcopy(FIXTURES['completedWithNextPage']), deepcopy(FIXTURES['empty'])]}
    calls = []
    def handler(request):
        assert request.method == 'GET' and request.url.path == '/api/v1/orders'
        params = dict(request.url.params)
        calls.append(params)
        if 'from' in params:
            assert params['status'] == 'CLOSED' and 'to' in params
            return httpx.Response(200, json=deepcopy(pages.get('LEGACY', pages['CLOSED'])[1 if params.get('cursor') else 0]))
        assert 'to' not in params  # Strict evidence must not clip by creation date.
        return httpx.Response(200, json=pages[params['status']].pop(0))
    obj = object.__new__(TossClient)
    obj._token, obj._token_expires, obj._account_seq = 'synthetic', float('inf'), 1
    with httpx.Client(base_url='https://mock-toss.invalid', transport=httpx.MockTransport(handler), trust_env=False) as http:
        obj._http = http
        yield SimpleNamespace(api=obj, pages=pages, calls=calls)


def closed(client):
    return client.pages['CLOSED'][0]['result']['orders'][0]


def test_documented_pages_exhaust_once_and_keep_exact_decimals(client, tmp_path):
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete and len(rows) == 1 and rows.fill_reasons == []
    assert [r['status'] for r in client.calls] == ['OPEN', 'CLOSED', 'CLOSED']
    assert rows.fill_end == '2026-03-29T14:59:59.999999+00:00'
    tx = toss_transactions(rows, 'toss:primary')[0]
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read() as conn:
        event = normalize_transaction(store, conn, tx)
    assert event['gross_amount'] == '700000' and event['fee'] == '1400' and event['tax'] == '0'
    assert event['amount'] == '-701400'
    assert 'toss_execution' not in tx.to_dict()


def test_old_order_filled_inside_window_and_partial_canceled_count(client):
    row = closed(client)
    row.update(orderedAt='2025-12-01T09:00:00+09:00', status='CANCELED')
    row['execution'].update(filledQuantity='2', filledAmount='140000', filledAt='2026-03-29T01:00:00+09:00')
    rows = client.api.get_fill_history_evidence('2026-03-29', '2026-03-29')
    assert rows.complete and len(rows) == 1
    assert toss_transactions(rows, 'toss:primary')[0].quantity == 2


def test_published_open_partial_fill_is_not_dropped(client):
    client.pages['OPEN'][0] = deepcopy(FIXTURES['pendingMixed'])
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete
    txs = toss_transactions(rows, 'toss:primary')
    assert len(txs) == 2 and sum(tx.quantity for tx in txs) == 12


@pytest.mark.parametrize('mutation', [
    'missing-commission', 'null-commission', 'missing-tax', 'unknown-status', 'numeric-amount',
    'missing-envelope', 'error-envelope', 'nested-truncation', 'missing-next', 'duplicate-page',
    'duplicate-row', 'timezone', 'epoch-ms', 'gross-mismatch', 'quantity-over-order', 'wrong-group',
    'currency-enum', 'extra-unparsed-row', 'missing-filled-at', 'future-time',
])
def test_malformed_response_invalidates_whole_fill_window(client, mutation):
    row = closed(client)
    page = client.pages['CLOSED'][0]
    if mutation == 'missing-commission':
        del row['execution']['commission']
    elif mutation == 'null-commission':
        row['execution']['commission'] = None
    elif mutation == 'missing-tax':
        del row['execution']['tax']
    elif mutation == 'unknown-status':
        row['status'] = 'PARTIALLY_CANCELED_NEW'
    elif mutation == 'numeric-amount':
        row['execution']['filledAmount'] = 700000
    elif mutation == 'missing-envelope':
        client.pages['CLOSED'][0] = page['result']
    elif mutation == 'error-envelope':
        page['error'] = {'code': 'synthetic'}
    elif mutation == 'nested-truncation':
        row['metadata'] = {'truncated': True}
    elif mutation == 'missing-next':
        page['result']['nextCursor'] = None
    elif mutation == 'duplicate-page':
        client.pages['CLOSED'][1] = deepcopy(page)
    elif mutation == 'duplicate-row':
        page['result']['orders'].append(deepcopy(row))
    elif mutation == 'timezone':
        row['execution']['filledAt'] = '2026-03-28T09:31:15'
    elif mutation == 'epoch-ms':
        row['execution']['filledAt'] = 1774657875000
    elif mutation == 'gross-mismatch':
        row['execution']['filledAmount'] = '700010'
    elif mutation == 'quantity-over-order':
        row['execution']['filledQuantity'] = '11'
    elif mutation == 'wrong-group':
        row['status'] = 'PENDING'
    elif mutation == 'currency-enum':
        row['currency'] = 'USDT'
    elif mutation == 'extra-unparsed-row':
        page['result']['orders'].append(None)
    elif mutation == 'missing-filled-at':
        row['execution']['filledAt'] = None
    elif mutation == 'future-time':
        row['execution']['filledAt'] = '2099-03-28T09:31:15+09:00'
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert not rows.complete and rows == [] and rows.fill_reasons


def test_reordered_distinct_pages_do_not_change_fills(client):
    first = closed(client)
    second = deepcopy(first)
    second['orderId'] = 'synthetic-earlier-order'
    second['orderedAt'] = '2026-03-01T09:00:00+09:00'
    client.pages['CLOSED'][1]['result']['orders'] = [second]
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete and len(rows) == 2


def test_cap_does_not_imply_complete_history(client):
    client.pages['OPEN'][0] = deepcopy(FIXTURES['pendingMixed'])
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29', max_orders=1)
    assert not rows.complete and rows.fill_reasons == ['FILL_ROW_LIMIT']


def test_daily_collector_retains_fill_contract_windows(client):
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
                                            kis_accounts=lambda: [], kis_client=lambda label: None,
                                            error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}),
                                   strict_toss_fills=True)
    result = collector.collect_transactions('2026-03-28', '2026-03-29')
    windows = result.toss_fill_windows['toss:primary:orders']
    assert len(windows) == 1 and windows[0]['complete'] and not windows[0]['reasons']
    assert result.transactions[0].toss_execution is None  # Legacy ingestion stays identical.
    assert windows[0]['transactions'][0].toss_execution['filledAmount'] == '700000'


def test_strict_daily_collection_persists_actual_fill_window(client, tmp_path):
    from test_collect_ledger import collect_ledger

    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
                                            kis_accounts=lambda: [], kis_client=lambda label: None,
                                            error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}),
                                   strict_toss_fills=True)
    def unavailable():
        raise RuntimeError('Synthetic unrelated source failure')
    store = LedgerStore(tmp_path / 'ledger.db')
    collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
                           binance_factory=unavailable, hyperliquid_factory=unavailable,
                           raw_dir=tmp_path / 'raw')
    with store.read() as conn:
        row = conn.execute("SELECT start_time,end_time,status,row_count FROM ledger_coverage WHERE stream LIKE 'toss/fills-v1/%'").fetchone()
        assert tuple(row) == ('2026-03-27T15:00:00+00:00', '2026-03-29T14:59:59.999999+00:00', 'complete', 1)
        event = conn.execute("SELECT payload_json FROM ledger_events WHERE source='toss_transactions'").fetchone()
        assert json.loads(event[0])['gross_amount'] == '700000'


def test_strict_fill_net_preserves_thirty_character_amounts(client, tmp_path):
    closed(client)['execution']['commission'] = '0.00000000000000000000001'
    rows = client.api.get_fill_history_evidence('2026-03-28', '2026-03-29')
    assert rows.complete
    store = LedgerStore(tmp_path / 'ledger.db')
    with store.read() as conn:
        event = normalize_transaction(store, conn, toss_transactions(rows, 'toss:primary')[0])
    assert event['amount'] == '-700000.00000000000000000000001'


@pytest.mark.parametrize('cost', ['tax', 'commission'])
def test_nullable_cost_preserves_legacy_ingestion_exit_and_limits_inference(client, tmp_path, cost, monkeypatch):
    from test_collect_ledger import Binance, Hyperliquid, collect_ledger
    from test_toss_flow_inference import coverage, infer_toss_flows, schedule, snapshot

    # Equal synthetic DB salts permit byte-for-byte normalized-payload comparison.
    monkeypatch.setattr('portfolio_ledger.secrets.token_bytes', lambda n: b'0' * n)
    closed(client)['execution'][cost] = None
    saved = deepcopy(client.pages)
    outcomes, payloads = [], []
    for strict in (False, True):
        client.pages.clear()
        client.pages.update(deepcopy(saved))
        collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
            kis_accounts=lambda: [], kis_client=lambda label: None,
            error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=strict)
        store = LedgerStore(tmp_path / ('strict.db' if strict else 'legacy.db'))
        result = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
            binance_factory=Binance, hyperliquid_factory=Hyperliquid)
        with store.read() as conn:
            rows = conn.execute("SELECT payload_json FROM ledger_events WHERE source='toss_transactions'").fetchall()
            payloads.append([r[0] for r in rows])
            outcomes.append((result['failed'], len(rows)))
            if strict:
                window = conn.execute("SELECT * FROM ledger_coverage WHERE stream LIKE 'toss/fills-v1/%'").fetchone()
                assert window['status'] == 'limited'
                reasons = json.loads(window['reason_codes_json'])
                assert 'FILL_AMOUNT_OR_COST_MISSING' in reasons
                windows = [{**coverage()[0], 'status': window['status'], 'reasons': reasons}]
                inferred = infer_toss_flows([snapshot(), snapshot('2026-10-11T09:10:00+00:00')], [],
                                           schedule(), coverage=windows)
                assert all(r['flow_native'] is None for r in inferred)
    assert outcomes == [(False, 1), (False, 1)]
    assert payloads[0] == payloads[1]
    assert json.loads(payloads[1][0])['tax' if cost == 'tax' else 'fee'] is None


@pytest.mark.parametrize('failure', ['old-incomplete', 'lifetime-cap', 'transport'])
def test_strict_history_failure_does_not_disable_accepted_daily_window(client, tmp_path, failure, monkeypatch):
    from test_collect_ledger import Binance, Hyperliquid, collect_ledger

    if failure == 'transport':
        def evidence(*args, **kwargs):
            raise RuntimeError('synthetic transport failure')
        monkeypatch.setattr(client.api, 'get_fill_history_evidence', evidence)
    else:
        # OPEN rows are outside the old CLOSED creation-date stream.
        client.pages['OPEN'][0] = deepcopy(FIXTURES['pendingMixed'])
        if failure == 'old-incomplete':
            row = client.pages['OPEN'][0]['result']['orders'][1]
            row['execution']['commission'] = None
        else:
            original = client.api.get_fill_history_evidence
            monkeypatch.setattr(client.api, 'get_fill_history_evidence',
                                lambda *a, **kw: original(*a, **kw, max_orders=1))
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
        kis_accounts=lambda: [], kis_client=lambda label: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=True)
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
                                   binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    assert result['failed'] is False
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM ledger_events WHERE source='toss_transactions'").fetchone()[0] == 1
        row = conn.execute("SELECT status,reason_codes_json FROM ledger_coverage WHERE stream LIKE 'toss/fills-v1/%'").fetchone()
        assert row['status'] == 'limited' and json.loads(row['reason_codes_json'])


def test_strict_supplement_adds_old_created_fills_when_legacy_window_is_empty(client, tmp_path):
    from test_collect_ledger import Binance, Hyperliquid, collect_ledger

    client.pages['LEGACY'] = [deepcopy(FIXTURES['empty'])]
    closed(client)['orderedAt'] = '2025-12-01T09:00:00+09:00'
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
        kis_accounts=lambda: [], kis_client=lambda label: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=True)
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
                                   binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    assert result['failed'] is False
    with store.read() as conn:
        events = conn.execute("SELECT * FROM ledger_events WHERE source='toss_transactions'").fetchall()
        assert len(events) == 1
        window = conn.execute("SELECT * FROM ledger_coverage WHERE stream LIKE 'toss/fills-v1/%'").fetchone()
        assert window['status'] == 'complete' and window['account_identity'] == events[0]['account_identity']


def test_strict_precision_mismatch_does_not_replace_legacy_or_fail_job(client, tmp_path):
    from test_collect_ledger import Binance, Hyperliquid, collect_ledger

    closed(client)['execution']['commission'] = '0.00000000000000000000001'
    collector = PortfolioCollector(Providers(toss_labels=lambda: ['primary'], toss_client=lambda label: client.api,
        kis_accounts=lambda: [], kis_client=lambda label: None,
        error_formatter=lambda exc: {'error': {'code': 'SYNTHETIC'}}), strict_toss_fills=True)
    store = LedgerStore(tmp_path / 'ledger.db')
    result = collect_ledger.collect(store, '2026-03-28', '2026-03-29', equity=collector,
                                   binance_factory=Binance, hyperliquid_factory=Hyperliquid)
    assert result['failed'] is False
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_conflicts').fetchone()[0] == 0
        window = conn.execute("SELECT * FROM ledger_coverage WHERE stream LIKE 'toss/fills-v1/%'").fetchone()
        assert window['status'] == 'limited'
        assert 'FILL_LEGACY_EVIDENCE_MISMATCH' in json.loads(window['reason_codes_json'])
