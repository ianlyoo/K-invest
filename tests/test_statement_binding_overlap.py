"""Account binding by exact synthetic API evidence, without credential config edits."""
from dataclasses import replace

import pytest

import statement_import as m
from portfolio_model import Transaction, TxType
from portfolio_sources import Collection
from toss_accounts import TossAccountRegistry, TossCredentials


def trades(count=5):
    return [Transaction('toss', 'unbound', f'synthetic-{i}', TxType.BUY,
        f'2025-03-{i + 3:02d}', 'KRW', symbol=f'SYN{i}', quantity=i + 1,
        gross_amount=(i + 1) * 100, fee_status='verified_zero', tax_status='verified_zero')
        for i in range(count)]


def manager(tmp_path, monkeypatch, statement, *, api_count=None, wrong=False, labels=('syn',)):
    parsed = m.ParsedStatement('synthetic-overlap', 1, '64321867530',
        {'start': '2025-01-01', 'end': '2025-12-31'}, statement, {}, [])
    monkeypatch.setattr(m, 'read_inbox', lambda *a: b'invented parser input')
    monkeypatch.setattr(m.TossPdfParser, 'parse', lambda *a: parsed)
    selected = 'toss:' + labels[0]
    api = [replace(t, account=selected, tx_id='api-' + t.tx_id,
        gross_amount=t.gross_amount + 1 if wrong else t.gross_amount)
        for t in statement[:api_count]]
    collection = Collection(transactions=api, health={selected + ':orders': {'status': 'ok'}},
        requested_window=parsed.covered_period, fetch_complete=True)
    registry = TossAccountRegistry([TossCredentials(label, 'invented-' + label, 'invented-secret')
        for label in labels], cache_dir=tmp_path)
    store = m.StatementStore(tmp_path / 'ledger.db')
    return store, m.PreviewManager(store, tmp_path, ledger=lambda *a: collection,
        labels=registry.account_labels, registry=lambda: registry), collection


@pytest.mark.parametrize('count', [5, 6])
def test_empty_registry_id_accepts_five_or_more_exact_covered_api_matches(tmp_path, monkeypatch, count):
    store, importer, _ = manager(tmp_path, monkeypatch, trades(count))
    preview = importer.preview('toss', 'synthetic.pdf')
    detected = preview['detected_account']
    assert not detected['suffix_confirmed']
    assert detected['overlap_confirmed']
    assert detected['binding_evidence'] == 'api_overlap'
    assert detected['matched_inside_api_coverage'] == count
    assert detected['new_inside_api_coverage'] == detected['conflicts'] == 0
    assert detected['reason_codes'] == []
    assert not importer.apply(preview['preview_id'])['blocked']
    applied = importer.apply(preview['preview_id'], True)
    assert applied['written_count'] == count
    assert applied['detected_account']['binding_evidence'] == 'api_overlap'
    saved, imports = store.load('2025-01-01', '2025-12-31')
    assert len(saved) == count
    assert imports[0]['metadata']['detected_account']['binding_evidence'] == 'api_overlap'
    assert store.binding_verified('toss:syn', detected['account_id'])
    for private in ('64321867530', 'invented-secret', 'invented-syn'):
        assert private not in str(preview) + str(applied) + str(imports)


def test_same_statement_against_other_single_auto_label_is_blocked(tmp_path, monkeypatch):
    store, importer, _ = manager(tmp_path, monkeypatch, trades(), wrong=True, labels=('other',))
    preview = importer.preview('toss', 'synthetic.pdf', 'auto')
    detected = preview['detected_account']
    assert detected['matched_inside_api_coverage'] == 0
    assert detected['conflicts'] == 5
    assert detected['binding_evidence'] is None
    assert importer.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError):
        importer.apply(preview['preview_id'], True)
    assert not store.db_path.exists()


def test_four_exact_covered_api_matches_cannot_confirm_first_binding(tmp_path, monkeypatch):
    store, importer, _ = manager(tmp_path, monkeypatch, trades(4))
    preview = importer.preview('toss', 'synthetic.pdf')
    detected = preview['detected_account']
    assert detected['matched_inside_api_coverage'] == 4
    assert detected['new_inside_api_coverage'] == detected['conflicts'] == 0
    assert not detected['overlap_confirmed']
    assert detected['binding_evidence'] is None
    assert importer.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError) as error:
        importer.apply(preview['preview_id'], True)
    assert error.value.code == 'STATEMENT_ACCOUNT_UNCONFIRMED'
    assert not store.db_path.exists()


def test_five_overlaps_plus_one_covered_unmatched_row_blocks_binding(tmp_path, monkeypatch):
    rows = [*trades(), replace(trades(1)[0], tx_id='unmatched', symbol='UNMATCHED', trade_date='2025-03-05',
        quantity=99, gross_amount=9900)]
    store, importer, _ = manager(tmp_path, monkeypatch, rows, api_count=5)
    preview = importer.preview('toss', 'synthetic.pdf')
    detected = preview['detected_account']
    assert detected['matched_inside_api_coverage'] == 5
    assert preview['new_inside_api_coverage'] == detected['new_inside_api_coverage'] == 1
    assert not detected['overlap_confirmed']
    assert detected['binding_evidence'] is None
    assert importer.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError) as error:
        importer.apply(preview['preview_id'], True)
    assert error.value.code == 'NEW_INSIDE_API_COVERAGE'
    assert not store.db_path.exists()


def test_auto_with_two_configured_labels_still_requires_explicit_account(tmp_path, monkeypatch):
    store, importer, _ = manager(tmp_path, monkeypatch, trades(), labels=('syn', 'other'))
    with pytest.raises(m.StatementError) as error:
        importer.preview('toss', 'synthetic.pdf', 'auto')
    assert error.value.code == 'STATEMENT_ACCOUNT_REQUIRED'
    preview = importer.preview('toss', 'synthetic.pdf', 'toss:syn')
    assert preview['detected_account']['binding_evidence'] == 'api_overlap'
    importer.apply(preview['preview_id'], True)
    assert store.db_path.exists()


@pytest.mark.parametrize('kind', ['failed', 'truncated', 'outside_successful_window'])
def test_overlaps_outside_successful_coverage_cannot_confirm_account(tmp_path, monkeypatch, kind):
    store, importer, collection = manager(tmp_path, monkeypatch, trades())
    if kind == 'failed':
        collection.health['toss:syn:orders']['status'] = 'error'
        collection.failed_chunks = [{'source': 'toss:syn:orders'}]
    elif kind == 'truncated':
        collection.health['toss:syn:orders']['status'] = 'truncated'
        collection.truncated_sources = ['toss:syn:orders']
    else:
        collection.source_metadata['toss:syn:orders'] = {'fetched_windows': [
            {'start': '2025-06-01', 'end': '2025-12-31'}]}
    preview = importer.preview('toss', 'synthetic.pdf')
    assert preview['dedup']['matched'] == 5
    assert preview['detected_account']['matched_inside_api_coverage'] == 0
    assert not preview['detected_account']['overlap_confirmed']
    assert importer.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError):
        importer.apply(preview['preview_id'], True)
    assert not store.db_path.exists()


def test_apply_rechecks_overlap_threshold_on_current_fetch(tmp_path, monkeypatch):
    store, importer, collection = manager(tmp_path, monkeypatch, trades())
    preview = importer.preview('toss', 'synthetic.pdf')
    assert not importer.apply(preview['preview_id'])['blocked']
    collection.transactions = collection.transactions[:4]
    with pytest.raises(m.StatementError) as error:
        importer.apply(preview['preview_id'], True)
    assert error.value.code == 'STATEMENT_ACCOUNT_UNCONFIRMED'
    assert not store.db_path.exists()


def test_five_exact_overlaps_plus_a_conflict_do_not_confirm_binding(tmp_path, monkeypatch):
    store, importer, collection = manager(tmp_path, monkeypatch, trades(6))
    collection.transactions[-1] = replace(collection.transactions[-1], gross_amount=601)
    preview = importer.preview('toss', 'synthetic.pdf')
    detected = preview['detected_account']
    assert detected['matched_inside_api_coverage'] == 5
    assert detected['conflicts'] == 1
    assert not detected['overlap_confirmed']
    assert detected['binding_evidence'] is None
    assert importer.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError):
        importer.apply(preview['preview_id'], True)
    assert not store.db_path.exists()


def test_five_raw_matches_with_only_four_in_coverage_are_insufficient(tmp_path, monkeypatch):
    store, importer, collection = manager(tmp_path, monkeypatch, trades())
    # Feb 27 + three weekdays = Mar 4: only the last four statement rows count.
    collection.source_metadata['toss:syn:orders'] = {'fetched_windows': [
        {'start': '2025-02-27', 'end': '2025-12-31'}]}
    preview = importer.preview('toss', 'synthetic.pdf')
    assert preview['dedup']['matched'] == 5
    assert preview['detected_account']['matched_inside_api_coverage'] == 4
    assert importer.apply(preview['preview_id'])['blocked']
    with pytest.raises(m.StatementError):
        importer.apply(preview['preview_id'], True)
    assert not store.db_path.exists()


def test_registry_suffix_is_reported_as_preferred_binding_evidence(tmp_path, monkeypatch):
    store, importer, _ = manager(tmp_path, monkeypatch, trades())
    importer.registry = lambda: TossAccountRegistry([
        TossCredentials('syn', 'invented-client', 'invented-secret', '643-21-867530')], cache_dir=tmp_path)
    preview = importer.preview('toss', 'synthetic.pdf')
    detected = preview['detected_account']
    assert detected['suffix_confirmed'] and detected['overlap_confirmed']
    assert detected['binding_evidence'] == 'registry_suffix'
    assert not importer.apply(preview['preview_id'])['blocked']
    importer.apply(preview['preview_id'], True)
    assert store.binding_verified('toss:syn', detected['account_id'])


def test_confirmed_overlap_survives_undo_and_retention_expiry(tmp_path, monkeypatch):
    store, importer, collection = manager(tmp_path, monkeypatch, trades())
    preview = importer.preview('toss', 'synthetic.pdf')
    applied = importer.apply(preview['preview_id'], True)
    store.undo(applied['import_id'])
    collection.transactions = []
    fresh = importer.preview('toss', 'synthetic.pdf')
    detected = fresh['detected_account']
    assert detected['previously_verified']
    assert not detected['overlap_confirmed']
    assert detected['matched_inside_api_coverage'] == 0
    assert detected['binding_evidence'] == 'api_overlap'
    assert detected['previous_confirmation']['matched_inside_api_coverage'] == 5
    importer.apply(fresh['preview_id'], True)
    assert store.binding_verified('toss:syn', detected['account_id'])
    assert len(store.load('2025-01-01', '2025-12-31')[0]) == 5


@pytest.mark.parametrize('counts', [(4, 0, 0), (5, 1, 0), (5, 0, 1)])
def test_store_rejects_overlap_flag_without_all_required_counts(tmp_path, counts):
    store = m.StatementStore(tmp_path / 'ledger.db')
    row = replace(trades(1)[0], account='toss:syn')
    detected = {'account_id': 'acct_synthetic', 'suffix_confirmed': False, 'overlap_confirmed': True,
        **dict(zip(('matched_inside_api_coverage', 'new_inside_api_coverage', 'conflicts'), counts))}
    with pytest.raises(m.StatementError) as error:
        store.apply('invalid-proof', 'toss:syn', [row], {'detected_account': detected})
    assert error.value.code == 'STATEMENT_ACCOUNT_UNCONFIRMED'
    assert store.load('2025-01-01', '2025-12-31')[0] == []
