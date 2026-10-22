"""Release review regressions. Synthetic data only; no account/API access."""
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from test_daily_nav import fixture_summary
from test_l2_flows import ADDRESS, START, TX, append, deposit, flow, withdrawal

from portfolio_ledger import LedgerStore, normalize_row, normalize_rows
from portfolio_store import NavStore
from scripts.collect_ledger import CollectionRun

OTHER = '0x' + 'c' * 40
OTHER_TX = '0x' + 'd' * 64


def test_outside_hl_transfer_and_contradictory_refs_stay_unknown(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    outgoing = flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
        dict(hash=TX, time=START, delta=dict(type='internalTransfer', usdc='99.000000', fee='0',
                                          user=ADDRESS, destination=OTHER)))
    append(store, [outgoing, deposit(store, tx=OTHER_TX)])
    assert store.match_transfers() == 0
    with store.read() as conn:
        assert {r[0] for r in conn.execute('SELECT classification FROM ledger_transfer_classifications')} == {'unknown'}
    # Even eligible bridge types cannot override contradictory reference facts.
    clean = LedgerStore(tmp_path / 'bridge.db')
    append(clean, [withdrawal(clean), deposit(clean, tx=OTHER_TX)])
    assert clean.match_transfers() == 0


def test_known_outside_counterparty_blocks_shared_hash_bridge(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    incoming = flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
        dict(hash=TX, time=START + 1000, delta=dict(type='deposit', usdc='99.000000')))
    outgoing = flow(store, 'binance/withdraw', dict(id='withdraw', txId=TX, coin='USDC', amount='99.000000',
        transactionFee='0', status=6, applyTime='2026-10-01 00:00:00', address=OTHER))
    append(store, [outgoing, incoming])
    assert store.match_transfers() == 0


def test_raw_hash_absent_from_database_daily_and_nav_evidence(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    run = CollectionRun(store)
    account = store.hl_identity(ADDRESS)
    row = dict(hash=TX, time=START, delta=dict(type='withdraw', usdc='99.000000', fee='1',
                                            user=ADDRESS, destination=OTHER))
    run.raw('hyperliquid/' + account + '/userNonFundingLedgerUpdates', [row], account, START, START + 1000)
    # Hash-only native IDs in Binance capture/prospective history are private too.
    run.raw('binance/deposit', [dict(txId=OTHER_TX, coin='USDC', amount='99.000000', status=1,
                                  insertTime=START, address=ADDRESS)], store.account_identity('binance:primary'), START, START + 1000)
    run.raw('hyperliquid/' + account + '/userFunding',
            [dict(hash=TX, time=START, delta=dict(coin='BTC', usdc='1'))], account, START, START + 1000)
    _, summary = fixture_summary()
    summary['valuation_at'] = '2026-10-01T00:10:00Z'
    nav = NavStore(store.db_path).upsert_snapshot(summary, '2026-10-01')
    assert len(nav['external_flows']['observed_events']) == 2
    outputs = [store.db_path.read_bytes(), json.dumps(run.counts).encode(), json.dumps(nav).encode()]
    outputs += [p.read_bytes() for p in run.root.rglob('*.json')]
    for raw in (TX, OTHER_TX, ADDRESS, OTHER):
        assert all(raw.encode() not in output for output in outputs)
    replay = CollectionRun(store)
    replay.raw('hyperliquid/' + account + '/userNonFundingLedgerUpdates', [row], account, START, START + 1000)
    assert sum(c['new'] + c['conflict'] for c in replay.counts.values()) == 0


def test_wallet_identity_includes_route_and_leg(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    events = []
    with store.read() as conn:
        for route in ('MAIN_UMFUTURE', 'MAIN_FUNDING'):
            events.extend(normalize_rows(store, conn, 'binance/transfer_' + route,
                dict(tranId=42, timestamp=START, amount='7.00', asset='USDC', status='CONFIRMED'), {}))
    assert append(store, events[:2])['new'] == 2
    second = append(store, events[2:])
    assert second['new'] == 2 and second['conflict'] == 0
    replay = append(store, events)
    assert replay['new'] == replay['conflict'] == 0
    assert len({e['source_record_id'] for e in events}) == 4
    assert {e['native_record_id'] for e in events} == {'42'}
    assert store.match_transfers() == 2


def test_pending_confirmed_flow_revisions_match_without_conflicts(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    def observations(status, amount='7.00'):
        with store.read() as conn:
            return normalize_rows(store, conn, 'binance/transfer_MAIN_UMFUTURE',
                dict(tranId=42, timestamp=START, amount=amount, asset='USDC', status=status), {})
    pending = observations('PENDING')
    append(store, pending)
    assert store.match_transfers() == 0
    confirmed = observations('CONFIRMED')
    assert append(store, confirmed)['conflict'] == 0
    assert store.match_transfers() == 1
    assert append(store, pending)['conflict'] == 0
    with store.read() as conn:
        for row in conn.execute('SELECT event_id,payload_json FROM ledger_events'):
            assert json.loads(row[1])['source_status'] == 'PENDING'
            assert store.effective_event(conn, row[0])['source_status'] == 'CONFIRMED'
        assert conn.execute('SELECT count(*) FROM ledger_status_revisions').fetchone()[0] >= 2
        for sql in ('UPDATE ledger_status_revisions SET revision=99', 'DELETE FROM ledger_status_revisions'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    assert append(store, observations('CONFIRMED', amount='9.00'))['conflict'] == 2


@pytest.mark.parametrize('terminal', ['CONFIRMED', 'COMPLETED', 'FAILED', 'CANCELLED'])
def test_terminal_status_does_not_regress_and_repeated_observation_is_idempotent(tmp_path, terminal):
    store = LedgerStore(tmp_path / 'ledger.db')
    def observation(status):
        with store.read() as conn:
            return normalize_rows(store, conn, 'binance/transfer_MAIN_UMFUTURE',
                dict(tranId=42, timestamp=START, amount='7', asset='USDC', status=status), {})
    append(store, observation(terminal))
    append(store, observation('PENDING'))
    append(store, observation('PENDING'))
    with store.read() as conn:
        ids = [r[0] for r in conn.execute('SELECT event_id FROM ledger_events')]
        assert {store.effective_event(conn, key)['source_status'] for key in ids} == {terminal}
        assert conn.execute('SELECT count(*) FROM ledger_status_revisions').fetchone()[0] == 2
    assert store.match_transfers() == (1 if terminal in ('CONFIRMED', 'COMPLETED') else 0)


def test_status_with_amount_conflict_does_not_promote_effective_state(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    pending = deposit(store)
    pending['source_status'] = '0'
    append(store, [pending, withdrawal(store)])
    assert append(store, [{**pending, 'source_status': '1', 'amount': '9'}])['conflict'] == 1
    with store.read() as conn:
        assert store.effective_event(conn, store.event_id(pending))['source_status'] == '0'
        assert conn.execute('SELECT count(*) FROM ledger_status_revisions').fetchone()[0] == 0
    assert store.match_transfers() == 0


def test_configured_destination_allows_real_binance_withdraw_hl_deposit(tmp_path):
    store = LedgerStore(tmp_path / 'ledger.db')
    incoming = flow(store, 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates',
        dict(hash=TX, time=START + 1000, delta=dict(type='deposit', usdc='99.000000')))
    outgoing = flow(store, 'binance/withdraw', dict(id='withdraw', txId=TX, coin='USDC', amount='99.000000',
        transactionFee='0', status=6, applyTime='2026-10-01 00:00:00', address=ADDRESS))
    append(store, [outgoing, incoming])
    assert store.match_transfers() == 1


@pytest.mark.parametrize('typ', ['internalTransfer', 'subAccountTransfer', 'spotTransfer'])
def test_hl_named_transfer_is_never_a_binance_bridge_leg(tmp_path, typ):
    store = LedgerStore(tmp_path / 'ledger.db')
    stream = 'hyperliquid/' + store.hl_identity(ADDRESS) + '/userNonFundingLedgerUpdates'
    # Both accounts are configured, but this delta is an HL account transfer.
    store.hl_identity(OTHER)
    outgoing = flow(store, stream, dict(hash=TX, time=START,
        delta=dict(type=typ, user=ADDRESS, destination=OTHER, usdc='99.000000', fee='0')))
    append(store, [outgoing, deposit(store)])
    assert store.match_transfers() == 0


def test_271_database_replays_without_changing_existing_nontransfer_identities(tmp_path):
    # Execute the actual release normalizer/schema against synthetic inputs only.
    root = Path(__file__).parents[1]
    legacy = ModuleType('ledger_271_fixture')
    # Vendored 2.7.1 source (a fresh clone has no release history to read it from).
    source = (root / 'tests' / 'fixtures' / 'legacy_portfolio_ledger.py.txt').read_text()
    exec(compile(source, 'portfolio_ledger_271_fixture.py', 'exec'), legacy.__dict__)
    old = legacy.LedgerStore(tmp_path / 'legacy.db')
    account = old.hl_identity(ADDRESS)
    inputs = [
        ('binance/userTrades_BTCUSDT', dict(id=7, symbol='BTCUSDT', time=START, side='BUY', qty='1', price='2')),
        ('binance/income', dict(tranId=8, incomeType='FUNDING_FEE', symbol='BTCUSDT', time=START, income='1', asset='USDT')),
        ('hyperliquid/' + account + '/userFillsByTime', dict(tid=9, coin='BTC', time=START, side='B', sz='1', px='2')),
        ('hyperliquid/' + account + '/userFunding', dict(hash=TX, time=START, delta=dict(coin='BTC', usdc='1'))),
        ('hyperliquid/' + account + '/userNonFundingLedgerUpdates',
         dict(hash=OTHER_TX, time=START, delta=dict(type='deposit', usdc='7', user=ADDRESS))),
        ('binance/deposit', dict(id=TX, txId=TX, coin='USDC', amount='7', status=1, insertTime=START, address=ADDRESS)),
    ]
    with old.transaction() as conn:
        old_events = [legacy.normalize_row(old, conn, stream, row, {}) for stream, row in inputs]
        for event in old_events:
            assert old.append(conn, event) == 'new'
        original = list(conn.execute('SELECT event_id,payload_json FROM ledger_events ORDER BY event_id'))
    store = LedgerStore(old.db_path)
    with store.read() as conn:
        events = [normalize_row(store, conn, stream, row, {}) for stream, row in inputs]
    result = append(store, events)
    assert result['new'] == result['conflict'] == 0
    assert result['duplicate'] == len(inputs)
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute('SELECT event_id,payload_json FROM ledger_events ORDER BY event_id')] == [tuple(r) for r in original]
        effective = [store.effective_event(conn, r[0]) for r in original]
        assert all(raw not in json.dumps(effective) for raw in (TX, OTHER_TX, ADDRESS))
    # Venue IDs have stable identities; legacy raw hashes retain only their old
    # immutable event IDs and first observations, with private effective facts.
    for event, previous in zip(events[:3], old_events[:3]):
        assert store.event_id(event) == old.event_id(previous)
