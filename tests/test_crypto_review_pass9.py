"""Query-local identity indexes preserve evidence and scale across rolling captures."""
import copy
import json
import sqlite3
import subprocess
import time
import types
from datetime import timedelta
from pathlib import Path

import pytest
from test_crypto_flow_intervals import T0, T1, identities, ms, setup, snapshot

import crypto_flow_evidence as evidence
import crypto_flow_intervals as intervals
from portfolio_ledger import LedgerStore, canonical

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope='module')
def reference(require_git_commit):
    require_git_commit('0ffb8e3')
    module = types.ModuleType('crypto_flow_evidence_before_identity_index')
    code = subprocess.check_output(['git', 'show', '0ffb8e3:crypto_flow_evidence.py'], cwd=ROOT, text=True)
    exec(compile(code, '0ffb8e3/crypto_flow_evidence.py', 'exec'), module.__dict__)
    module.READ_BUDGET_SECONDS = 60  # Only the old reference; production stays at five.
    # Windows written now are v2: the same payload shape, read the same way here
    # (v2 only adds trust in HL fee assets, which these Binance facts lack).
    module.VERSION = evidence.VERSION
    return module


def transfer_facts(store, ident=1, stamp=None, amount='7'):
    with store.read() as conn:
        return evidence.normalize(store, conn, 'binance/transfer_MAIN_UMFUTURE',
            dict(tranId=ident, timestamp=ms(stamp or T0 + timedelta(minutes=1)),
                 amount=amount, asset='USDT', type='MAIN_UMFUTURE', status='CONFIRMED'))


def save(store, facts):
    return evidence.FlowEvidence(store).save('binance', 'transfer_MAIN_UMFUTURE', facts,
        facts[0]['account_identity'], ms(T0), ms(T1))


def compare_reference(store, reference, monkeypatch):
    before = intervals.crypto_interval_flows(store.db_path, T0, T1)
    with monkeypatch.context() as patch:
        patch.setattr(intervals, 'read_view', reference.read_view)
        expected = intervals.crypto_interval_flows(store.db_path, T0, T1)
    assert canonical(before) == canonical(expected)
    return next(row for row in before if row['source'] == 'binance')


@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('changed_amount', [False, True])
def test_exact_id_fallback_preserves_aliases_and_conflicts(tmp_path, reference, monkeypatch, alias, changed_amount):
    store = setup(tmp_path)
    original = transfer_facts(store)
    if alias:
        for fact in original:
            fact['account_identity'] = store.account_identity('binance:previous')
    store.append_many([(fact, None) for fact in original])
    if alias:
        with store.transaction() as conn:
            store.bind_account(conn, 'binance:previous', 'binance:primary')
    strict = transfer_facts(store, amount='8' if changed_amount else '7')
    # Exact source IDs must still match when coarse native metadata changed.
    for fact in strict:
        fact['native_record_id'] = 'different-native-metadata'
    save(store, strict)
    row = compare_reference(store, reference, monkeypatch)
    assert ('LEDGER_CONTENT_CONFLICT' in row['reasons']) is changed_amount
    assert all(len(v['legacy_facts']) == 1 for v in row['inputs']['ledger_versions'])


def test_batched_metadata_preserves_enrichment_status_and_conflict_order(tmp_path, reference, monkeypatch):
    store = setup(tmp_path)
    facts = transfer_facts(store)
    for status, extra in [('PENDING', {}), ('CONFIRMED', {'fee': '0'}),
                          ('PROCESSING', {'fee': '0', 'tax': '0'})]:
        store.append_many([({**fact, 'source_status': status, **extra}, None) for fact in facts])
    for amount in ('8', '9', '10'):
        store.append_many([({**fact, 'amount': amount}, None) for fact in facts])
    for status in ('PENDING', 'CONFIRMED', 'PENDING'):
        save(store, [{**fact, 'source_status': status} for fact in facts])
    # Pass 10 intentionally replaces sticky terminal status with newest observed
    # status; the financial conflict and all ordered provenance remain identical.
    current = intervals.crypto_interval_flows(store.db_path, T0, T1)
    with monkeypatch.context() as patch:
        patch.setattr(intervals, 'read_view', reference.read_view)
        previous = intervals.crypto_interval_flows(store.db_path, T0, T1)
    assert [row['inputs'] for row in current] == [row['inputs'] for row in previous]
    row = next(row for row in current if row['source'] == 'binance')
    assert 'CRYPTO_PENDING_FLOW_STATUS' in row['reasons']
    assert row['flow_native'] is None and 'LEDGER_CONTENT_CONFLICT' in row['reasons']
    assert all(len(v['legacy_facts'][0]['conflicts']) == 3 for v in row['inputs']['ledger_versions'])


def test_legacy_only_aliases_join_the_exact_index_without_becoming_proof(tmp_path, reference, monkeypatch):
    store = setup(tmp_path)
    facts = transfer_facts(store)
    old = [{**fact, 'account_identity': store.account_identity('binance:previous')} for fact in facts]
    store.append_many([(fact, None) for fact in [*old, *facts]])
    with store.transaction() as conn:
        store.bind_account(conn, 'binance:previous', 'binance:primary')
    row = compare_reference(store, reference, monkeypatch)
    assert row['flow_native'] is None and 'CRYPTO_LEGACY_FLOW_NOT_RECONCILED' in row['reasons']


def test_exact_lookup_does_not_rescan_the_fact_list_for_each_legacy_identity(tmp_path):
    store = LedgerStore(tmp_path / 'db')
    facts = [fact for ident in range(1, 101) for fact in transfer_facts(store, ident)]
    save(store, facts)
    with store.read() as conn:
        rows, _ = evidence.read_view(conn, store.db_path, {}, T0, T1)
    store.append_many([(fact, None) for fact in facts])
    class CountedRows(list):
        visits = 0

        def __iter__(self):
            for row in super().__iter__():
                self.visits += 1
                yield row
    rows = CountedRows(rows)
    with store.read() as conn:
        evidence._legacy_facts(conn, rows, {}, T0, T1)
    assert rows.visits <= 3 * len(facts)
    assert all(len(row['_version']['legacy_facts']) == 1 for row in rows)


@pytest.mark.parametrize('damage', ['boolean_amount', 'account', 'venue', 'missing_field', 'extra_field'])
def test_repeated_fact_cache_never_bypasses_validation(tmp_path, damage):
    store = setup(tmp_path)
    facts = transfer_facts(store, amount='1')
    facts[0]['amount'] = 1
    save(store, facts)
    with store.read() as conn:
        payload = json.loads(conn.execute('SELECT payload_json FROM crypto_flow_windows ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    cache = {}
    evidence._facts(payload, cache)
    damaged = copy.deepcopy(payload)
    if damage == 'boolean_amount':
        damaged['events'][0]['amount'] = True  # True == 1, but never a valid amount.
    elif damage == 'account':
        damaged['account_identity'] = store.account_identity('binance:other')
    elif damage == 'venue':
        damaged['venue'] = 'hyperliquid'
    elif damage == 'missing_field':
        damaged['events'][0].pop('fee')
    else:
        damaged['events'][0]['extra'] = None
    with pytest.raises(ValueError, match='EVENT_INVALID'):
        evidence._facts(damaged, cache)


@pytest.mark.parametrize('related', [False, True])
def test_metadata_intersection_preserves_selection_before_shape_validation(tmp_path, reference, monkeypatch, related):
    store = setup(tmp_path)
    facts = transfer_facts(store)
    save(store, facts)
    with store.read() as conn:
        row = dict(conn.execute('SELECT * FROM crypto_flow_windows ORDER BY rowid DESC LIMIT 1').fetchone())
    row.update(window_id='malformed-older-metadata', start_time=(T0 - timedelta(days=10)).isoformat(),
               end_time=(T0 - timedelta(days=9)).isoformat(),
               native_keys_json=canonical({'key': evidence._link(facts[0]) if related else 'unrelated'}))
    # Deliberately malformed synthetic history; the existing rows stay immutable.
    with store.transaction() as conn:
        conn.execute('INSERT INTO crypto_flow_windows VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))
    result = compare_reference(store, reference, monkeypatch)
    assert result['flow_native'] == (None if related else '7')


@pytest.fixture(scope='module')
def overlapping_year(tmp_path_factory):
    """The review's real-normalizer 100-facts/day, ten-day-overlap year model."""
    store = LedgerStore(tmp_path_factory.mktemp('crypto-overlapping-year') / 'year.db')
    base = T0 - timedelta(days=366)
    daily = []
    with store.read() as conn:
        for day in range(-10, 365):
            daily.append([fact for movement in range(50) for fact in evidence.normalize(
                store, conn, 'binance/transfer_MAIN_UMFUTURE',
                dict(tranId=(day + 10) * 50 + movement + 1,
                     timestamp=ms(base + timedelta(days=day, minutes=movement + 1)),
                     amount='1.23456789', asset='USDT', type='MAIN_UMFUTURE', status='CONFIRMED'))])
    store.append_many([(event, None) for facts in daily for event in facts])
    account = identities(store)['binance']
    hl_accounts = [store.account_identity('hyperliquid:synthetic-' + str(i)) for i in range(2)]
    # Keep WAL open during fixture construction, as the collector does while running.
    with sqlite3.connect(store.db_path) as keeper:
        for day in range(365):  # each fetch ends just after its daily cut, through the coverage pad
            lo, hi = base + timedelta(days=day - 10), base + timedelta(days=day) + intervals.EVIDENCE_PAD
            facts = [event for rows in daily[day:day + 10] for event in rows]
            writer = evidence.FlowEvidence(store)
            writer.save('binance', 'transfer_MAIN_UMFUTURE', facts, account, ms(lo), ms(hi))
            for leaf in sorted(intervals.REQUIRED_STREAMS['binance'] - {'transfer_MAIN_UMFUTURE'}):
                writer.save('binance', leaf, [], account, ms(lo), ms(hi))
            for hl in hl_accounts:
                writer.save('hyperliquid', 'userNonFundingLedgerUpdates', [], hl, ms(lo), ms(hi))
        keeper.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    for day in (180, 363):
        lo = base + timedelta(days=day)
        snapshot(store, lo, 'year-start-' + str(day))
        snapshot(store, lo + timedelta(days=1), 'year-end-' + str(day))
    with store.read() as conn:
        assert conn.execute('SELECT count(*) FROM crypto_flow_windows').fetchone()[0] == 3650
        assert conn.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == 37500
    return store


@pytest.mark.parametrize('day', [180, 363])
def test_daily_interval_stays_inside_budget_with_a_year_of_overlapping_facts(overlapping_year, day):
    store = overlapping_year
    lo = T0 - timedelta(days=366 - day)
    started = time.perf_counter()
    rows = intervals.crypto_interval_flows(store.db_path, lo, lo + timedelta(days=1))
    elapsed = time.perf_counter() - started
    row = next(row for row in rows if row['source'] == 'binance')
    # Hourly facts have no event-time mark: USDT is interpolated between the capture marks.
    assert row['flow_native'] == '61.72839450' and row['confidence'] == 'conditional'
    assert row['reasons'] == ['CRYPTO_EVENT_FX_INTERPOLATED'] and row['conversion_status'] == 'interpolated'
    assert len(row['events']) == 50 and len(row['inputs']['ledger_versions']) == 100
    # Generous CI headroom; the five-second production guard is unchanged.
    assert elapsed < 4.5, f'one-day overlap query took {elapsed:.3f}s'
    print(f'overlap-year day={day}: {elapsed:.3f}s')
