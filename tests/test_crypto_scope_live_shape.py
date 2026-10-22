"""Live 2.12.3 crypto scope shape: broker-attributed causes, the capture-time cut, Upbit marks."""
import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_crypto_flow_intervals import ADDRESS, coverage, ms
from test_ledger_returns import wired as base_wired

import ledger_returns as lr
from crypto_flow_evidence import FlowEvidence, normalize
from crypto_flow_intervals import (
    EVIDENCE_PAD,
    REQUIRED_STREAMS,
    _span,
    crypto_interval_flows,
)
from daily_nav import (
    CRYPTO_BOUNDARY_TOLERANCE_SECONDS,
    MAX_CRYPTO_AGE_SECONDS,
    crypto_boundary,
    instant,
)
from portfolio_ledger import LedgerStore, normalize_rows
from portfolio_store import NavStore

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'live_crypto_scope_shape.json').read_text())
LIVE = FIXTURE['snapshot']
# Live top-level reasons, all attributed to kis:* / resolved toss:* accounts.
LIVE_REASONS = ['UNKNOWN_COMPONENT_PRICE_TIME', 'UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS',
                'SETTLEMENT_BASIS_REFERENCE_ASSUMPTION']


@pytest.fixture
def wired(tmp_path, monkeypatch):
    return base_wired.__wrapped__(tmp_path, monkeypatch)


def shift(value, days, key=None):
    """Move every date and aware timestamp by whole days, keeping intra-run offsets."""
    if isinstance(value, dict):
        return {k: shift(v, days, k) for k, v in value.items()}
    if isinstance(value, list):
        return [shift(v, days) for v in value]
    if key in ('snapshot_date', 'date'):
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    stamp = instant(value) if isinstance(value, str) and 'T' in value else None
    return (stamp + timedelta(days=days)).isoformat() if stamp else value


def live_nav(days=0, *, usdt='100', ids=None):
    nav = shift(deepcopy(LIVE), days)
    nav['snapshot_id'] = 'live-' + nav['snapshot_date']
    binance = nav['crypto_nav']['binance']
    binance['by_currency']['USDT'].update(value=float(usdt), decimal_value=usdt)
    krw = Decimal(usdt) * Decimal(nav['crypto_nav']['fx_times']['USDT']['rate_to_krw'])
    binance.update(value_krw=float(krw), value_krw_decimal=str(krw))
    for venue, identity in (ids or {}).items():
        for row in nav['crypto_nav'][venue]['accounts']:
            row['ledger_account_identity'] = identity
    return nav


def scoped(nav):
    return lr.scope_navs([nav], 'crypto')[0]


def cut(nav):
    return instant(nav['crypto_nav']['observed_at'])


def stored(tmp_path, usdts, mutate=lambda rows: None):
    """Consecutive live-shape NAVs ending 10-14 in one DB, flow coverage around every capture."""
    path = tmp_path / 'crypto.db'
    ledger = LedgerStore(path)
    ids = {'binance': ledger.account_identity('binance:primary'), 'hyperliquid': ledger.hl_identity(ADDRESS)}
    rows = [live_nav(day, usdt=usdt, ids=ids) for day, usdt in zip(range(1 - len(usdts), 1), usdts)]
    mutate(rows)
    with NavStore(path)._connect() as conn:
        for row in rows:
            NavStore._insert_version(conn, row)
    coverage(ledger, start=cut(rows[0]) - timedelta(minutes=1), end=cut(rows[-1]) + timedelta(minutes=1))
    return path, ledger, ids, rows


def transfer(ledger, account, stamp):
    """50 USDT MAIN -> UM futures (70,000 KRW at the Upbit USDT mark)."""
    raw = {'tranId': 808, 'timestamp': ms(stamp), 'amount': '50', 'asset': 'USDT',
           'type': 'MAIN_UMFUTURE', 'status': 'CONFIRMED'}
    with ledger.read() as conn:
        events = normalize(ledger, conn, 'binance/transfer_MAIN_UMFUTURE', raw)
    FlowEvidence(ledger).save('binance', 'transfer_MAIN_UMFUTURE', events, account,
                              ms(stamp - timedelta(minutes=1)), ms(stamp + timedelta(minutes=1)))


def tool_blocks(wired, monkeypatch, path, rows):
    wired.navs[:] = rows
    monkeypatch.setattr(type(wired.server._get_nav_store()), 'db_path', path)
    period = (rows[0]['snapshot_date'], rows[-1]['snapshot_date'])
    monkeypatch.setattr(wired.server.portfolio_engine, 'resolve_period', lambda _: period)
    return [tool(input_snapshot_ids=['synthetic'], portfolio_scope='crypto', detail_level='full')['data']['ledgerless_returns']
            for tool in (wired.server.get_portfolio_performance, wired.server.get_portfolio_history)]


def binance_usdt(path, rows):
    return [r for r in crypto_interval_flows(path, rows[0]['snapshot_date'], rows[-1]['snapshot_date'])
            if r['source'] == 'binance' and r['currency'] == 'USDT']


def test_live_shape_resolves_only_the_crypto_scope():
    nav = live_nav()
    assert nav['reason_codes'] == LIVE_REASONS and nav['result_status'] == 'partial'
    assert nav['crypto_nav']['observed_at'] > nav['nav_evidence_completed_at']  # captured after Toss evidence
    stored_nav = deepcopy(nav)
    result = scoped(nav)
    assert result['reason_codes'] == [] and result['total_value_krw'] == 140000 + 69500
    # The calculation copy's point is the capture completion the crypto valuation used.
    assert result['valuation_at'] == nav['crypto_nav']['observed_at']
    assert instant(result['valuation_at']) == instant('2026-10-14T11:43:11.027755Z')
    assert nav == stored_nav  # the stored payload is never rewritten


def test_live_shape_crypto_returns_are_estimates_in_both_tools(wired, tmp_path, monkeypatch):
    path, ledger, ids, rows = stored(tmp_path, ['100', '151'])
    t0, t1 = cut(rows[0]), cut(rows[1])
    event = instant(rows[0]['valuation_at']) + timedelta(minutes=3)  # 173.5 s after the Upbit USDT mark
    transfer(ledger, ids['binance'], event)
    # 209,500 -> 280,900 KRW with a 70,000 KRW contribution at the Upbit USDT event mark.
    weight = Decimal(str((t1 - event).total_seconds())) / Decimal(str((t1 - t0).total_seconds()))
    expected = float(round(Decimal(1400) / (Decimal(209500) + weight * 70000) * 100, 4))
    [flow] = binance_usdt(path, rows)
    assert (instant(flow['t0']), instant(flow['t1']), flow['flow_krw']) == (t0, t1, '70000')
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['label'] == 'estimate' and block['period']['return_pct'] == expected, block
        row = block['intervals'][0]
        assert (row['start_nav_krw'], row['end_nav_krw'], row['flows_krw']) == (209500, 280900, 70000)
        assert row['timing'] == 'known'


@pytest.mark.parametrize('gap', ['end', 'start'])
def test_transfer_between_valuation_and_capture_lands_in_its_capture_interval(gap, wired, tmp_path, monkeypatch):
    """Review: a transfer 7 s after valuation_at, before the venues are read, was split at
    valuation_at into the next interval (+33 % / -20 % on zero gain). The capture cut
    places it before the NAV that already holds it: every interval earns 0 %."""
    usdts = ['100', '100', '150'] if gap == 'end' else ['100', '150', '150']
    path, ledger, ids, rows = stored(tmp_path, usdts)
    holder = rows[2 if gap == 'end' else 1]  # first capture that holds the 50 USDT
    event = instant(holder['valuation_at']) + timedelta(seconds=7)
    binance = holder['crypto_nav']['binance']['accounts'][0]
    assert instant(holder['crypto_nav']['fx_times']['USDT']['priced_at']) < event < cut(holder)
    assert event < instant(binance['observed_at']) < instant(binance['nav_price_time'])
    transfer(ledger, ids['binance'], event)
    flows = binance_usdt(path, rows)
    assert [(instant(f['t1']), f['flow_krw'], f['confidence'], f['reasons']) for f in flows] == [
        (cut(rows[1]), '0' if gap == 'end' else '70000', 'observed', []),
        (cut(rows[2]), '70000' if gap == 'end' else '0', 'observed', [])]
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['label'] == 'estimate' and not block['reason_codes'], block
        assert block['period']['return_pct'] == 0, block
        assert [(r['return_pct'], r['flows_krw']) for r in block['intervals']] == [
            (0, 0 if gap == 'end' else 70000), (0, 70000 if gap == 'end' else 0)], block


@pytest.mark.parametrize('side', ['start', 'end'])
def test_transfer_inside_a_venue_capture_span_is_unresolved(side, wired, tmp_path, monkeypatch):
    """Binance was read 0.63 s after the cut: a transfer between them has no honest side."""
    path, ledger, ids, rows = stored(tmp_path, ['100', '150'])
    capture = rows[0 if side == 'start' else 1]
    event = cut(capture) + timedelta(milliseconds=300)
    assert event < instant(capture['crypto_nav']['binance']['accounts'][0]['nav_price_time'])
    transfer(ledger, ids['binance'], event)
    [flow] = binance_usdt(path, rows)
    assert flow['confidence'] == 'unresolved' and 'CRYPTO_FLOW_DURING_NAV_CAPTURE' in flow['reasons']
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['label'] != 'estimate' and block['period']['return_pct'] is None, block


@pytest.mark.parametrize('side', ['start', 'end'])
def test_conflicting_candidate_time_inside_a_capture_span_is_unresolved(side, wired, tmp_path, monkeypatch):
    """Review recheck: transfer 808 is saved twice with conflicting times. The saved time
    lies 90 s outside the interval, but the conflicting candidate falls in a capture span
    beyond the cut (before the start cut, after the end cut). It was +33.4129 %."""
    def binance_read_before_cut(rows):
        for nav in rows if side == 'start' else ():
            for row in nav['crypto_nav']['binance']['accounts']:
                row['nav_price_time'] = (cut(nav) - timedelta(seconds=5)).isoformat()
    path, ledger, ids, rows = stored(tmp_path, ['100', '150'], binance_read_before_cut)
    boundary = cut(rows[0 if side == 'start' else 1])
    transfer(ledger, ids['binance'], boundary + timedelta(seconds=-90 if side == 'start' else 90))
    transfer(ledger, ids['binance'], boundary + timedelta(milliseconds=-2500 if side == 'start' else 300))
    [flow] = binance_usdt(path, rows)
    assert flow['confidence'] == 'unresolved' and flow['reasons'] == ['CRYPTO_FLOW_DURING_NAV_CAPTURE'], flow
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['label'] != 'estimate' and block['period']['return_pct'] is None, block


@pytest.mark.parametrize('offset_ms, resolved', [(0, False), (1, True)])
def test_whole_second_time_may_be_truncated_into_the_capture_span(offset_ms, resolved, wired, tmp_path, monkeypatch):
    """11:43:11.000 is 28 ms before the end cut, but a whole-second value may be a truncated
    second (LedgerStore.same_fact), up to 11:43:11.999 inside the Binance read span.
    11:43:11.001 has millisecond precision and stays before the cut."""
    path, ledger, ids, rows = stored(tmp_path, ['100', '150'])
    stamp = cut(rows[1]).replace(microsecond=offset_ms * 1000)
    assert stamp < cut(rows[1])
    transfer(ledger, ids['binance'], stamp)
    [flow] = binance_usdt(path, rows)
    if resolved:
        assert flow['confidence'] == 'observed' and flow['flow_krw'] == '70000', flow
    else:
        assert flow['confidence'] == 'unresolved' and flow['reasons'] == ['CRYPTO_FLOW_DURING_NAV_CAPTURE'], flow
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['period']['return_pct'] == (0 if resolved else None), block
        assert (block['label'] == 'estimate') is resolved, block


@pytest.mark.parametrize('storage', ['strict', 'legacy'])
@pytest.mark.parametrize('offset_ms, resolved', [(0, False), (1, True)])
def test_evidence_just_before_the_range_is_selected_for_its_precision(storage, offset_ms, resolved,
                                                                      wired, tmp_path, monkeypatch):
    """Review recheck 2: transfer 909 at 11:43:11.000 is 28 ms before the start cut and the
    evidence range, so it was never read (+33.4129 %). As a whole second it may lie inside the
    start Binance read span. 11:43:11.001 is read too, but it stays before the cut and resolved."""
    path = tmp_path / 'crypto.db'
    ledger = LedgerStore(path)
    ids = {'binance': ledger.account_identity('binance:primary'), 'hyperliquid': ledger.hl_identity(ADDRESS)}
    rows = [live_nav(-1, usdt='150' if resolved else '100', ids=ids), live_nav(0, usdt='150', ids=ids)]
    with NavStore(path)._connect() as conn:
        for row in rows:
            NavStore._insert_version(conn, row)
    for venue, streams in REQUIRED_STREAMS.items():  # complete over each padded band
        lo, hi, _ = _span(*rows, venue)
        for leaf in streams:
            FlowEvidence(ledger).save(venue, leaf, [], ids[venue], ms(lo - EVIDENCE_PAD), ms(hi + EVIDENCE_PAD) + 1)
    stamp = cut(rows[0]).replace(microsecond=offset_ms * 1000)
    assert stamp < _span(*rows, 'binance')[0]
    raw = {'tranId': 909, 'timestamp': ms(stamp), 'amount': '50', 'asset': 'USDT',
           'type': 'MAIN_UMFUTURE', 'status': 'CONFIRMED'}
    stream = 'binance/transfer_MAIN_UMFUTURE'
    with ledger.read() as conn:
        strict, legacy = normalize(ledger, conn, stream, raw), normalize_rows(ledger, conn, stream, raw, {})
    if storage == 'strict':  # an earlier fetch window that ends at the transfer
        FlowEvidence(ledger).save('binance', 'transfer_MAIN_UMFUTURE', strict, ids['binance'],
                                  ms(stamp - timedelta(seconds=60)), ms(stamp))
    else:
        ledger.append_many([(event, None) for event in legacy])
    [flow] = binance_usdt(path, rows)
    if resolved:
        assert flow['confidence'] == 'observed' and flow['flow_krw'] == '0' and flow['reasons'] == [], flow
    else:
        assert flow['confidence'] == 'unresolved' and flow['reasons'] == ['CRYPTO_FLOW_DURING_NAV_CAPTURE'], flow
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['period']['return_pct'] == (0 if resolved else None), block
        assert (block['label'] == 'estimate') is resolved, block



@pytest.mark.parametrize('side', ['start', 'end'])
@pytest.mark.parametrize('band, reasons', [
    ('failed', ['CRYPTO_FLOW_COVERAGE_INCOMPLETE', 'SOURCE_STREAM_FAILED']),
    ('missing', ['CRYPTO_FLOW_COVERAGE_INCOMPLETE']),
    ('complete', []),
])
def test_coverage_must_be_complete_over_the_padded_band(side, band, reasons, wired, tmp_path, monkeypatch):
    """Review recheck 3: the transfer stream is complete over the exact range, but its fetch
    of the padded band next to a capture failed or never ran. A transfer there at a truncated
    whole second may reach the capture span, so the observed zero flow (+33.4129 %) is not
    proven. Only complete band coverage resolves; the (t0, t1] cut stays exact."""
    path = tmp_path / 'crypto.db'
    ledger = LedgerStore(path)
    ids = {'binance': ledger.account_identity('binance:primary'), 'hyperliquid': ledger.hl_identity(ADDRESS)}
    rows = [live_nav(-1, usdt='100', ids=ids), live_nav(0, usdt='150', ids=ids)]
    with NavStore(path)._connect() as conn:
        for row in rows:
            NavStore._insert_version(conn, row)
    writer = FlowEvidence(ledger)
    for venue, streams in REQUIRED_STREAMS.items():
        lo, hi, _ = _span(*rows, venue)
        for leaf in streams:
            if leaf != 'transfer_MAIN_UMFUTURE':
                writer.save(venue, leaf, [], ids[venue], ms(lo - EVIDENCE_PAD), ms(hi + EVIDENCE_PAD) + 1)
    lo, hi, _ = _span(*rows, 'binance')
    if side == 'start':  # the band [lo - pad, lo) is a separate earlier fetch
        exact, gap = (ms(lo), ms(hi + EVIDENCE_PAD) + 1), (ms(lo - EVIDENCE_PAD), ms(lo) - 1)
    else:  # the band (hi, hi + pad] is a separate later fetch
        exact, gap = (ms(lo - EVIDENCE_PAD), ms(hi) + 1), (ms(hi) + 2, ms(hi + EVIDENCE_PAD) + 1)
    writer.save('binance', 'transfer_MAIN_UMFUTURE', [], ids['binance'], *exact)
    if band == 'failed':
        writer.save('binance', 'transfer_MAIN_UMFUTURE', [], ids['binance'], *gap, ['SOURCE_STREAM_FAILED'], True)
    elif band == 'complete':
        writer.save('binance', 'transfer_MAIN_UMFUTURE', [], ids['binance'], *gap)
    [flow] = binance_usdt(path, rows)
    assert flow['reasons'] == reasons and [instant(flow['t0']), instant(flow['t1'])] == [cut(r) for r in rows], flow
    blocks = tool_blocks(wired, monkeypatch, path, rows)
    if band == 'complete':
        assert flow['confidence'] == 'observed' and flow['flow_krw'] == '0', flow
        assert all(b['label'] == 'estimate' and b['period']['return_pct'] == 33.4129 for b in blocks), blocks
    else:
        assert flow['confidence'] == 'unresolved' and flow['flow_krw'] is None, flow
        assert all(b['label'] != 'estimate' and b['period']['return_pct'] is None for b in blocks), blocks

def _read(venue, index, **times):
    return lambda nav: nav['crypto_nav'][venue]['accounts'][index].update(times)


def _seconds_from_cut(seconds):
    return (instant(LIVE['crypto_nav']['observed_at']) + timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize('mutate, reason', [
    # The venues were not read at one instant: HL 61 s before the capture completion.
    (_read('hyperliquid', 0, nav_price_time=_seconds_from_cut(-CRYPTO_BOUNDARY_TOLERANCE_SECONDS - 1)),
     'CRYPTO_OBSERVATION_TIMES_INCOHERENT'),
    (_read('binance', 4, nav_price_time=_seconds_from_cut(-CRYPTO_BOUNDARY_TOLERANCE_SECONDS - 1)),
     'CRYPTO_OBSERVATION_TIMES_INCOHERENT'),
    (_read('hyperliquid', 0, nav_price_time=None), 'UNKNOWN_CRYPTO_OBSERVATION_TIME'),
    (_read('binance', 0, nav_price_time='not-a-time'), 'UNKNOWN_CRYPTO_OBSERVATION_TIME'),
    (lambda n: n['crypto_nav']['hyperliquid'].update(accounts=[]), 'UNKNOWN_CRYPTO_OBSERVATION_TIME'),
])
def test_venue_reads_without_one_instant_block(mutate, reason):
    nav = live_nav()
    mutate(nav)
    result = scoped(nav)
    assert reason in result['reason_codes'] and result['total_value_krw'] is None
    assert result['valuation_at'] == nav['valuation_at']  # no instant is guessed


@pytest.mark.parametrize('binance, hyperliquid, accepted', [
    (-60, 0, True), (-60.001, 0, False), (0, 60, True), (0, 60.001, False),
    (-30, 30, True), (-30, 30.001, False),
    # Review recheck: each read within 60 s of the cut is not enough; the reads span 118 / 120 s.
    (-59, 59, False), (-60, 60, False),
    # The cut belongs to the same span: reads 58-59 s before it fit, 61 s do not.
    (-59, -58, True), (-61, -60.5, False),
])
def test_venue_reads_and_cut_fit_one_tolerance_span(binance, hyperliquid, accepted):
    nav = live_nav()
    nav['updated_at'] = _seconds_from_cut(61)  # the write bound cannot mask the span check
    for mark in nav['crypto_nav']['fx_times'].values():  # Upbit read at the cut never widens the span
        mark.update(read_at=nav['crypto_nav']['observed_at'], observed_at=nav['crypto_nav']['observed_at'])
    for venue, offset in (('binance', binance), ('hyperliquid', hyperliquid)):
        for index in range(len(nav['crypto_nav'][venue]['accounts'])):
            _read(venue, index, nav_price_time=_seconds_from_cut(offset))(nav)
    result = scoped(nav)
    if accepted:
        assert result['reason_codes'] == [] and result['valuation_at'] == nav['crypto_nav']['observed_at']
    else:
        assert result['reason_codes'] == ['CRYPTO_OBSERVATION_TIMES_INCOHERENT'], result['reason_codes']
        assert result['total_value_krw'] is None and result['valuation_at'] == nav['valuation_at']


def test_incoherent_capture_blocks_both_tools(wired, tmp_path, monkeypatch):
    late = _read('hyperliquid', 0, nav_price_time=_seconds_from_cut(-CRYPTO_BOUNDARY_TOLERANCE_SECONDS - 1))
    path, _, _, rows = stored(tmp_path, ['100', '100'], lambda rows: late(rows[1]))
    assert binance_usdt(path, rows) == []  # no flow interval ends at a guessed instant
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['label'] != 'estimate' and block['period']['return_pct'] is None, block
        # The engine drops the blocked end point, as for any blocked NAV.
        assert block['label'] == 'unavailable' and 'MISSING_END_NAV' in block['reason_codes'], block
        assert block['coverage']['snapshots_complete'] == 1, block


def detail(reason, account=None, currency=None):
    return {'reason_code': reason, 'account': account, 'currency': currency, 'resolved_for_native_nav': False}


@pytest.mark.parametrize('roster', ['absent', 'account_nav', 'components'])
@pytest.mark.parametrize('account', ['unknown:source', 'upbit:primary', 'binance:primary', 'hyperliquid',
                                     'crypto', None, 'kis:domestic', 'toss:primary', 'toss:unlisted'])
def test_only_broker_prefixed_causes_leave_the_crypto_scope(roster, account):
    """Review: roster or component presence never proves a cause is outside crypto."""
    nav = live_nav()
    reason = 'REVIEW_UNKNOWN_SOURCE_FAILURE'
    nav['reason_codes'].append(reason)
    nav['quality_reason_details'].append(detail(reason, account, 'USDT'))
    if account and roster == 'account_nav':
        nav['account_nav'].setdefault(account, {})
    if account and roster == 'components':
        nav['components'].append({'account': account, 'currency': 'USDT'})
    result = scoped(nav)
    broker = str(account).startswith(('toss:', 'kis:'))
    assert (reason in result['reason_codes']) is not broker
    assert result['total_value_krw'] == (209500 if broker else None)


@pytest.mark.parametrize('account', ['unknown:source', 'upbit:primary', 'kis:domestic'])
def test_automatically_attributed_component_causes_follow_the_prefix_rule(account):
    nav = live_nav()
    nav.pop('quality_reason_details')
    nav['reason_codes'] = ['UNKNOWN_COMPONENT_PRICE_TIME']
    nav['components'].append({'account': account, 'currency': 'USDT', 'priced_at': None})
    result = scoped(nav)
    assert result['reason_codes'] == ([] if account == 'kis:domestic' else ['UNKNOWN_COMPONENT_PRICE_TIME'])


@pytest.mark.parametrize('reason, details', [
    ('UNATTRIBUTED_CAUSE', []),  # no detail at all
    ('UNKNOWN_COMPONENT_PRICE_TIME', [detail('UNKNOWN_COMPONENT_PRICE_TIME', currency='USD')]),
    ('MISSING_FX_RATE', [detail('MISSING_FX_RATE', 'kis:overseas', 'USD'), detail('MISSING_FX_RATE')]),
    ('FAILED_SOURCES', [detail('FAILED_SOURCES', 'unknown:source')]),
    ('FAILED_SOURCES', [detail('FAILED_SOURCES', 'binance')]),
    ('SNAPSHOT_DATE_MISMATCH', [detail('SNAPSHOT_DATE_MISMATCH', 'kis:domestic', 'KRW')]),
])
def test_unattributed_venue_and_date_causes_still_block(reason, details):
    nav = live_nav()
    nav['reason_codes'].append(reason)
    nav['quality_reason_details'].extend(details)
    result = scoped(nav)
    assert reason in result['reason_codes'] and result['total_value_krw'] is None
    assert not (set(LIVE_REASONS) - {reason}) & set(result['reason_codes'])


@pytest.mark.parametrize('mutate, reason', [
    # The version write is stamped 11:43:12 (whole seconds); one second later is after it.
    (lambda n: n['crypto_nav'].update(observed_at='2026-10-14T11:43:13Z'), 'SNAPSHOT_DATE_MISMATCH'),
    (lambda n: n['crypto_nav']['hyperliquid']['accounts'][0].update(observed_at='2026-10-14T11:43:13Z'),
     'SNAPSHOT_DATE_MISMATCH'),
    (lambda n: n['crypto_nav']['fx_times']['USDT'].update(priced_at='2026-10-14T11:43:13Z'), 'SNAPSHOT_DATE_MISMATCH'),
    # Without the write time only valuation_at bounds the capture, as before.
    (lambda n: n.pop('updated_at'), 'SNAPSHOT_DATE_MISMATCH'),
    (lambda n: n.update(updated_at='not-a-time'), 'SNAPSHOT_DATE_MISMATCH'),
    (lambda n: n['crypto_nav'].update(observed_at=(instant(n['valuation_at']) - timedelta(
        seconds=MAX_CRYPTO_AGE_SECONDS + 1)).isoformat()), 'STALE_CRYPTO_SNAPSHOT'),
    (lambda n: n['crypto_nav']['binance']['accounts'][3].update(observed_at=(instant(n['valuation_at']) - timedelta(
        seconds=MAX_CRYPTO_AGE_SECONDS + 1)).isoformat()), 'STALE_CRYPTO_ACCOUNT_OBSERVATION'),
    (lambda n: n['crypto_nav'].update(observed_at=None), 'UNKNOWN_CRYPTO_OBSERVATION_TIME'),
    (_read('binance', 0, nav_price_time='2026-10-14T11:43:13Z'), 'SNAPSHOT_DATE_MISMATCH'),
    # An Upbit mark's last trade must be within 300 s of its own read.
    (lambda n: n['crypto_nav']['fx_times']['USDC'].update(priced_at=(instant(
        n['crypto_nav']['fx_times']['USDC']['read_at']) - timedelta(seconds=301)).isoformat()), 'STALE_CRYPTO_FX_RATE'),
])
def test_capture_after_write_or_beyond_max_age_still_blocks(mutate, reason):
    nav = live_nav()
    mutate(nav)
    result = scoped(nav)
    assert reason in result['reason_codes'] and result['total_value_krw'] is None


def at(nav, valued, captured, written):
    nav.update(valuation_at=valued, observed_at=valued, updated_at=written)
    crypto = nav['crypto_nav']
    crypto['observed_at'] = captured
    for venue in ('binance', 'hyperliquid'):
        for row in crypto[venue]['accounts']:
            row.update(observed_at=captured, nav_price_time=captured)
    for mark in crypto['fx_times'].values():
        mark.update(priced_at=captured, read_at=captured, observed_at=captured)
    return nav


@pytest.mark.parametrize('valued, captured, written', [
    # 23:59:50 KST valuation, captured 00:00:10 KST and written 00:00:12 KST the next day.
    ('2026-10-14T14:59:50Z', '2026-10-14T15:00:10Z', '2026-10-14T15:00:12+00:00'),
    # The capture is the point instant, so a capture from the previous KST day cannot stand in either.
    ('2026-10-13T15:00:20Z', '2026-10-13T14:59:50Z', '2026-10-13T15:00:25+00:00'),
])
def test_capture_keeps_the_kst_same_day_rule(valued, captured, written):
    nav = at(live_nav(), valued, captured, written)
    nav['snapshot_date'] = '2026-10-14'
    assert scoped(nav)['reason_codes'] == ['SNAPSHOT_DATE_MISMATCH']
    same_day = at(live_nav(), '2026-10-14T14:59:20Z', '2026-10-14T14:59:50Z', '2026-10-14T14:59:55+00:00')
    assert scoped(same_day)['reason_codes'] == []


def test_capture_bound_is_the_whole_write_second_and_fits_the_timer_run():
    nav = live_nav()
    nav['crypto_nav']['observed_at'] = '2026-10-14T11:43:12.999Z'  # updated_at is truncated to 11:43:12
    assert scoped(nav)['reason_codes'] == []
    # The 18:10 KST timer run of 10-13 reused a capture from before valuation_at and wrote 5 s later.
    timer, nav = FIXTURE['timer_run_2026_10_13'], live_nav(-1)
    nav.update({key: timer[key] for key in ('valuation_at', 'nav_evidence_completed_at', 'created_at', 'updated_at')},
               observed_at=timer['valuation_at'])
    nav['crypto_nav']['observed_at'] = timer['crypto_observed_at']
    for venue in ('binance', 'hyperliquid'):
        for row in nav['crypto_nav'][venue]['accounts']:
            row['observed_at'] = timer[venue + '_account_observed_at']
            del row['nav_price_time']  # a legacy futures_risk_v1 capture: venue reads are observed_at
    for mark in nav['crypto_nav']['fx_times'].values():  # synthetic marks read with the capture
        mark.update(priced_at=timer['crypto_observed_at'], read_at=timer['crypto_observed_at'],
                    observed_at=timer['crypto_observed_at'])
    assert scoped(nav)['reason_codes'] == []



# Live 10/15 18:10 KST snapshot, crypto times (UTC): Upbit KRW-USDC trades thinly.
LIVE_10_15 = {'observed_at': '2026-10-15T09:10:32.318Z', 'valuation_at': '2026-10-15T09:10:04+00:00',
              'read_at': '2026-10-15T09:10:12.183Z',
              'USDC': '2026-10-15T09:05:30.310Z', 'USDT': '2026-10-15T09:10:07.964Z'}


def moved(value, delta):
    """Move every aware timestamp by delta; dates stay."""
    if isinstance(value, dict):
        return {k: moved(v, delta) for k, v in value.items()}
    if isinstance(value, list):
        return [moved(v, delta) for v in value]
    stamp = instant(value) if isinstance(value, str) and 'T' in value else None
    return (stamp + delta).isoformat() if stamp else value


def live_10_15(usdt='100', ids=None):
    """The live 10/15 crypto times on the 10/14 capture's shape.

    Venue reads keep 10/14's offsets from the cut (Hyperliquid 7.0 s before, Binance
    0.6 s after). The USDC mark last traded 281.873 s before its read, 302.008 s before the cut.
    """
    nav = live_nav(1, usdt=usdt, ids=ids)
    nav = moved(nav, instant(LIVE_10_15['observed_at']) - cut(nav))
    nav.update(valuation_at=LIVE_10_15['valuation_at'], updated_at='2026-10-15T09:10:33+00:00')
    nav['crypto_nav']['observed_at'] = LIVE_10_15['observed_at']
    for currency, mark in nav['crypto_nav']['fx_times'].items():
        mark.update(priced_at=LIVE_10_15[currency], read_at=LIVE_10_15['read_at'], observed_at=LIVE_10_15['read_at'])
    return nav


def test_live_10_15_usdc_mark_is_judged_at_its_read():
    """An earlier version measured the mark from the cut (302 s, STALE_CRYPTO_FX_RATE) while the capture
    measured it from its read (282 s, accepted). One rule now: the 10/15 point is complete."""
    nav = live_10_15()
    usdc = nav['crypto_nav']['fx_times']['USDC']
    assert (instant(usdc['read_at']) - instant(usdc['priced_at'])).total_seconds() == 281.873
    assert (cut(nav) - instant(usdc['priced_at'])).total_seconds() == 302.008
    result = scoped(nav)
    assert result['reason_codes'] == [] and result['valuation_at'] == LIVE_10_15['observed_at']
    assert result['total_value_krw'] == 140000 + 69500


def test_live_10_14_to_10_15_crypto_interval_resolves_in_both_tools(wired, tmp_path, monkeypatch):
    path = tmp_path / 'crypto.db'
    ledger = LedgerStore(path)
    ids = {'binance': ledger.account_identity('binance:primary'), 'hyperliquid': ledger.hl_identity(ADDRESS)}
    rows = [live_nav(0, ids=ids), live_10_15(ids=ids)]
    with NavStore(path)._connect() as conn:
        for row in rows:
            NavStore._insert_version(conn, row)
    coverage(ledger, start=cut(rows[0]) - timedelta(minutes=1), end=cut(rows[1]) + timedelta(minutes=1))
    [flow] = binance_usdt(path, rows)
    assert flow['confidence'] == 'observed' and flow['flow_krw'] == '0' and flow['reasons'] == [], flow
    for block in tool_blocks(wired, monkeypatch, path, rows):
        assert block['label'] == 'estimate' and block['period']['return_pct'] == 0 and not block['reason_codes'], block


@pytest.mark.parametrize('edge, beyond, accepted', [
    # Read before the capture: with the last venue read it must fit one 60 s span.
    ('before', 0, True), ('before', 0.001, False),
    # Read after the capture (e.g. a reused capture): with the first venue read likewise.
    ('after', 0, True), ('after', 0.001, False),
])
def test_upbit_read_must_join_the_capture_span(edge, beyond, accepted):
    nav = live_10_15()
    nav['updated_at'] = '2026-10-15T09:12:00+00:00'  # written after every read
    crypto = nav['crypto_nav']
    _, venues, _ = crypto_boundary(crypto)
    times = [cut(nav), *(time for span in venues.values() for time in span)]
    width = timedelta(seconds=CRYPTO_BOUNDARY_TOLERANCE_SECONDS + beyond)
    read = max(times) - width if edge == 'before' else min(times) + width
    for mark in crypto['fx_times'].values():  # each still 281.873 s old at its read
        mark.update(read_at=read.isoformat(), observed_at=read.isoformat(),
                    priced_at=(read - timedelta(seconds=281.873)).isoformat())
    assert scoped(nav)['reason_codes'] == ([] if accepted else ['STALE_CRYPTO_FX_RATE'])


@pytest.mark.parametrize('age, accepted', [
    (281.873, True), (300, True), (300.001, False),
    (-5, True), (-5.001, False),  # a trade after the read only within clock skew
])
def test_upbit_mark_age_is_taken_at_its_read(age, accepted):
    nav = live_10_15()
    usdc = nav['crypto_nav']['fx_times']['USDC']
    usdc['priced_at'] = (instant(usdc['read_at']) - timedelta(seconds=age)).isoformat()
    assert scoped(nav)['reason_codes'] == ([] if accepted else ['STALE_CRYPTO_FX_RATE'])


def test_mark_without_its_own_read_time_is_judged_at_the_cut():
    nav = live_10_15()
    for mark in nav['crypto_nav']['fx_times'].values():
        del mark['read_at']
    assert scoped(nav)['reason_codes'] == ['STALE_CRYPTO_FX_RATE']  # USDC: 302 s before the cut


@pytest.mark.parametrize('age_ms', [281873, 300000, 300001])
def test_capture_nav_write_and_returns_share_one_stablecoin_age_rule(age_ms, tmp_path):
    """The same KRW-USDC trade age is accepted or rejected alike by the Upbit capture,
    the daily NAV write and the crypto return scope."""
    from test_crypto_nav_capture import MS, capture, quote_client, tickers
    from test_crypto_nav_capture import nav as write_nav

    from crypto_nav_capture import utc
    from crypto_validation import CryptoValidationError
    fresh = age_ms <= 300000
    payload = tickers()
    payload[1].update(trade_timestamp=MS - age_ms)  # KRW-USDC
    if fresh:
        fx = quote_client(payload).read()
    else:
        with pytest.raises(CryptoValidationError) as error:
            quote_client(payload).read()
        assert error.value.code == 'STALE_CRYPTO_FX_RATE'
        fx = quote_client().read()
        fx['USDC']['priced_at'] = utc(MS - age_ms)
    path = tmp_path / 'db'
    capture(path)
    assert ('STALE_CRYPTO_FX_RATE' in write_nav(path, fx)['hyperliquid']['reason_codes']) is not fresh
    nav = live_10_15()
    usdc = nav['crypto_nav']['fx_times']['USDC']
    usdc['priced_at'] = (instant(usdc['read_at']) - timedelta(milliseconds=age_ms)).isoformat()
    assert ('STALE_CRYPTO_FX_RATE' in scoped(nav)['reason_codes']) is not fresh


@pytest.mark.parametrize('earlier, fresh', [(0, True), (59, True), (61, False), (86400, False)])
def test_capture_nav_write_requires_the_mark_read_to_join_the_capture(earlier, fresh, tmp_path):
    """R1: yesterday's mark, priced and read together, is fresh against its own read. The
    NAV write must also place that read in the capture's coherent span, as the engine does."""
    from test_crypto_nav_capture import capture, quote_client
    from test_crypto_nav_capture import nav as write_nav
    path = tmp_path / 'db'
    capture(path)
    fx = quote_client().read()
    for mark in fx.values():
        for key in ('priced_at', 'read_at'):
            mark[key] = (instant(mark[key]) - timedelta(seconds=earlier)).isoformat()
    for venue in ('binance', 'hyperliquid'):
        values = write_nav(path, fx)[venue]
        assert ('STALE_CRYPTO_FX_RATE' in values['reason_codes']) is not fresh
        assert (values['value_krw'] is None) is not fresh


def test_crypto_fx_checks_use_the_upbit_marks_that_valued_the_nav():
    nav = live_nav()
    assert {'USDT', 'USDC'}.isdisjoint(nav['fx_times'])  # broker marks never carried stablecoins
    missing = deepcopy(nav)
    del missing['crypto_nav']['fx_times']['USDT']
    assert 'UNKNOWN_FX_PRICE_TIME' in scoped(missing)['reason_codes']
    # A broker-side mark can never stand in for the failed Upbit read the valuation used.
    failed = deepcopy(nav)
    failed['crypto_nav']['fx_times']['USDC'] = {'rate_to_krw': None, 'priced_at': None,
                                                'source': 'upbit_public_ticker', 'reason_codes': ['STALE_CRYPTO_FX_RATE']}
    failed['fx_times']['USDC'] = dict(nav['crypto_nav']['fx_times']['USDC'])
    assert {'UNKNOWN_FX_PRICE_TIME', 'STALE_CRYPTO_FX_RATE'} <= set(scoped(failed)['reason_codes'])
    flagged = deepcopy(nav)
    flagged['crypto_nav']['fx_times']['USDT']['reason_codes'] = ['CRYPTO_FUTURE_QUOTE']
    assert scoped(flagged)['reason_codes'] == ['CRYPTO_FUTURE_QUOTE']
    absent = deepcopy(nav)
    del absent['crypto_nav']['fx_times']  # legacy captures fall back exactly as daily_nav.crypto_nav does
    assert scoped(absent)['reason_codes'] == ['UNKNOWN_FX_PRICE_TIME']
    absent['fx_times'].update(deepcopy(nav['crypto_nav']['fx_times']))
    assert scoped(absent)['reason_codes'] == []
