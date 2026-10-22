"""Read-only API flows on consecutive complete crypto NAV instants (t0, t1].

Money is returned as Decimal strings. Immutable snapshot IDs/versions, ledger
strict-evidence hashes and coverage IDs make each result reproducible. Only
local SQLite/evidence reads occur here; no network, schema or write I/O.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from crypto_flow_evidence import read_view
from crypto_futures_accounts import BINANCE_WALLET_ROUTES
from crypto_nav_capture import STABLECOINS
from crypto_validation import (
    HL_FEE_TYPES,
    HL_FLOW_TYPES,
    CryptoValidationError,
    binance_transfer_id,
)
from daily_nav import crypto_boundary, instant, number

CALCULATION_VERSION = 'crypto_flows_v11'
# An unproven fee basis is an estimate (confidence conditional, KRW range on the
# event) instead of a null when the fee is in the flow's own stablecoin and within
# 1 % of the flow and 5 USD (stablecoins 1:1 as USD); otherwise it stays unresolved.
ESTIMATE_CAP_SHARE, ESTIMATE_CAP_USD = Decimal('0.01'), Decimal(5)
FEE_ESTIMATES = {'CRYPTO_TRANSFER_FEE_BASIS_ESTIMATED', 'CRYPTO_WITHDRAWAL_FEE_BASIS_ESTIMATED'}
INTERPOLATED = 'CRYPTO_EVENT_FX_INTERPOLATED'
FLOW_SOURCES = ('binance_transfers', 'binance_deposit', 'binance_withdraw', 'hyperliquid_flows')
# Ticks per second of a recorded execution time (as LedgerStore.same_fact reads them).
PRECISION = {'second': 1, 'millisecond': 1000, 'microsecond': 1000000,
             **{f'fractional_second_{digits}': 10 ** digits for digits in range(1, 7)}}
# Evidence reads reach past the guarded range by twice the coarsest reading tick
# (_readings: 1 s). A fact whose precision range touches a capture span is then
# always selected, including at the open SQL bounds and under julianday rounding.
# Coverage must be complete over that same band, or an unfetched truncated time
# could still reach a capture span. Only the (t0, t1] split keeps the exact range.
EVIDENCE_PAD = timedelta(seconds=2)
REQUIRED_STREAMS = {
    'binance': {'deposit', 'withdraw', *('transfer_' + route for route in BINANCE_WALLET_ROUTES)},
    'hyperliquid': {'userNonFundingLedgerUpdates'},
}


def _time(value):
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if type(value) is int or isinstance(value, str) and value.isdigit():
        try:
            return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        except (ValueError, OverflowError):
            return None
    return instant(value)


def _bound(value, end=False):
    if isinstance(value, str) and len(value) == 10:
        day = date.fromisoformat(value)
        result = datetime.combine(day, time(), ZoneInfo('Asia/Seoul'))
        return result + timedelta(days=1) - timedelta(microseconds=1) if end else result
    result = _time(value)
    if result is None:
        raise ValueError('CRYPTO_INVALID_INTERVAL')
    return result


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _cut(snapshot):
    """(instant, {venue: read span}) the crypto NAV represents.

    The instant is the coherent capture completion the valuation used, the same
    one ledger_returns.scope_navs gives the crypto point; None when the venues
    were not read at one instant. Payloads without crypto timing keep
    valuation_at. Each venue's span joins its NAV reads with that instant.
    """
    crypto = snapshot.get('crypto_nav') or {}
    if 'observed_at' not in crypto:
        return _time(snapshot.get('valuation_at')), {}
    cut, reads, _ = crypto_boundary(crypto)
    return cut, {venue: (min(lo, cut), max(hi, cut)) for venue, (lo, hi) in reads.items()} if cut else {}


def _complete(snapshot, venue):
    nav = (snapshot.get('crypto_nav') or {}).get(venue) or {}
    # A USD-proxied stablecoin value (daily_nav fx_proxy) is a display estimate, not a return endpoint.
    return (nav.get('status') in ('derived', 'complete') and not nav.get('reason_codes') and not nav.get('fx_proxy')
            and number(nav.get('value_krw_decimal', nav.get('value_krw'))) is not None
            and _cut(snapshot)[0] is not None)


def _readings(row, stamp):
    """Every (earliest, latest) instant the flow's evidence admits.

    Covers the saved time and each conflicting candidate, each read as truncated or
    rounded at its precision. A whole-second value may be a truncated second, and an
    unknown label is read as seconds.
    """
    result = []
    for when in [stamp, *row.get('_conflict_times', [])]:
        if when is not None:
            scale = 1 if when.microsecond == 0 else PRECISION.get(row.get('timestamp_precision'), 1)
            tick = timedelta(microseconds=1000000 // scale)
            result.append((when - tick, when + tick))
    return result


def _span(left, right, venue):
    """Evidence range of one interval: (t0, t1] widened by both endpoint read spans."""
    (t0, starts), (t1, ends) = _cut(left), _cut(right)
    guards = [span for span in (starts.get(venue), ends.get(venue)) if span]
    return min([t0] + [lo for lo, _ in guards]), max([t1] + [hi for _, hi in guards]), guards


def _account_cut(nav, aliases):
    result, reasons = {}, []
    for row in nav.get('accounts') or []:
        account = row.get('ledger_account_identity')
        if not account:
            reasons.append('CRYPTO_ACCOUNT_SCOPE_UNBOUND')
            continue
        account = aliases.get(account, account)
        mode = row.get('abstraction') if row.get('scope') != 'futures_collateral' else 'futures'
        cut = (mode, row.get('scope'), row.get('dex'))
        if account in result and result[account] != cut:
            reasons.append('CRYPTO_ACCOUNT_SCOPE_AMBIGUOUS')
        result[account] = cut
    if not result:
        reasons.append('CRYPTO_ACCOUNT_SCOPE_UNBOUND')
    return result, reasons


def _covers(windows, start, end):
    """Union inclusive millisecond windows over the requested overlap."""
    edge = start
    for lo, hi in sorted(windows):
        if lo <= edge + timedelta(milliseconds=1):
            edge = max(edge, hi)
    return edge >= end


def _coverage(records, venue, accounts, t0, t1):
    if any(row.get('_global_failure') for row in records):
        return ['CRYPTO_FLOW_EVIDENCE_UNAVAILABLE'], []
    reasons, ids = [], set()
    for account in accounts:
        for stream in sorted(REQUIRED_STREAMS[venue]):
            candidates = [r for r in records if r['source'] == venue and r['account_identity'] == account
                          and r['stream'].split('/')[-1] == stream
                          and 'LEGACY_INGESTION_ONLY' not in json.loads(r['reason_codes_json'])]
            windows = []
            for row in candidates:
                lo, hi = _time(row['start_time']), _time(row['end_time'])
                if lo is None or hi is None or hi <= t0 or lo > t1:
                    continue
                ids.add(row['coverage_id'])
                codes = json.loads(row['reason_codes_json'])
                if (row['status'] == 'complete' and not codes
                        and (row['row_count'] > 0 or row['verified_zero'] == 1)):
                    windows.append((lo, hi))
            if not _covers(windows, t0, t1):
                reasons.append('CRYPTO_FLOW_COVERAGE_INCOMPLETE')
                for row in candidates:
                    lo, hi = _time(row['start_time']), _time(row['end_time'])
                    if lo and hi and hi > t0 and lo <= t1:
                        reasons.extend(json.loads(row['reason_codes_json']))
            # A later semantic contradiction is stronger than an older good
            # fetch. A transport outage alone does not retract observed history.
            for row in candidates:
                codes = json.loads(row['reason_codes_json'])
                hard = [c for c in codes if c.startswith(('CRYPTO_', 'UNKNOWN_HYPERLIQUID_', 'PAGINATION_'))
                        or c in ('HISTORY_SHAPE_UNAVAILABLE', 'OUT_OF_WINDOW_RESPONSE', 'LEDGER_CONTENT_CONFLICT',
                                 'DENSE_TIMESTAMP')]
                a, b = _time(row['start_time']), _time(row['end_time'])
                if not hard or a is None or b is None or b <= t0 or a > t1:
                    continue
                if row.get('_integrity_failure'):
                    reasons.extend(hard)
                    continue  # Immutable fact loss requires restoration, never coverage repair.
                observed = _time(row['observed_at'])
                repairs = []
                for good in candidates:
                    lo, hi, fetched = (_time(good[k]) for k in ('start_time', 'end_time', 'observed_at'))
                    if (good['status'] == 'complete' and not json.loads(good['reason_codes_json'])
                            and (good['row_count'] > 0 or good['verified_zero'] == 1)
                            and lo is not None and hi is not None and lo <= hi
                            and observed is not None and fetched is not None and fetched > observed):
                        repairs.append((lo, hi))
                # If a is inside (t0,t1], its first millisecond is included and
                # must itself be repaired; t0 remains an open boundary.
                repair_start = max(a - timedelta(milliseconds=1), t0)
                repaired = _covers(repairs, repair_start, min(b, t1))
                if not repaired:
                    reasons.extend(hard)
    return sorted(set(reasons)), sorted(ids)


def _inside(account, wallet, accounts):
    if account not in accounts:
        return False
    mode, scope, dex = accounts[account]
    if mode == 'unifiedAccount' and scope == 'unified_shared_wallet':
        return wallet in ('perp', 'spot') or isinstance(wallet, str) and wallet.startswith('dex:')
    if mode in ('default', 'disabled') and scope == 'perp_dex':
        return wallet == ('perp' if dex == '' else 'dex:' + dex)
    return None


def _fee_capped(event, amount, fee):
    """A fee in the flow's own stablecoin (checked by _classify), within the estimate cap."""
    return event['currency'] in STABLECOINS and fee <= amount * ESTIMATE_CAP_SHARE and fee <= ESTIMATE_CAP_USD


def _flow_type(event):
    if event.get('flow_type') is not None:
        return event['flow_type']
    # Old expanded legs append ':in' after the original typed source identity.
    kinds = set(str(event.get('source_record_id', '')).split(':')) & HL_FLOW_TYPES
    return next(iter(kinds)) if len(kinds) == 1 else None


def _classify(event, venue, accounts):
    """Return signed capital and event kind; fees never become capital."""
    amount = number(event.get('amount'))
    if amount is None or amount <= 0:
        return None, 'unknown', ['CRYPTO_FLOW_AMOUNT_UNAVAILABLE']
    status = str(event.get('source_status')).upper()
    if venue == 'binance':
        if event['source'] in ('binance_deposit', 'binance_withdraw'):
            # On-chain events in excluded wallets do not cross the futures cut,
            # and their fees are not expenses of that cut either.
            if event.get('wallet') not in ('MAIN', 'FUNDING'):
                return None, 'unknown', ['CRYPTO_UNKNOWN_WALLET']
            return Decimal(0), event.get('kind'), []
        if status == 'FAILED':
            return Decimal(0), 'unknown', []
        if status not in ('CONFIRMED', 'COMPLETED'):
            return None, 'unknown', ['CRYPTO_PENDING_FLOW_STATUS']
        route = event.get('transfer_route')
        if route not in BINANCE_WALLET_ROUTES:
            return None, 'unknown', ['CRYPTO_UNKNOWN_WALLET']
        origin, destination = route.split('_')
        sign = int(destination == 'UMFUTURE') - int(origin == 'UMFUTURE')
        return amount * sign, 'transfer_in' if sign > 0 else 'transfer_out', []
    if status not in ('COMPLETED', 'CONFIRMED'):
        return None, 'unknown', ['CRYPTO_PENDING_FLOW_STATUS']
    typ = _flow_type(event)
    # Old normalized events may predate the strict raw-response validator.
    # Missing a documented fee never establishes a free boundary crossing.
    if typ in HL_FEE_TYPES and number(event.get('fee')) is None:
        return None, 'unknown', ['CRYPTO_WITHDRAWAL_FEE_UNAVAILABLE' if typ == 'withdraw'
                                 else 'CRYPTO_TRANSFER_FEE_UNAVAILABLE']
    if number(event.get('fee')) is not None and number(event['fee']) < 0:
        return None, 'unknown', ['CRYPTO_INVALID_NUMBER']
    if number(event.get('fee')) and event.get('fee_asset') != event['currency']:
        # A fee in another asset (an explicit HL feeToken such as HYPE) or of unknown
        # denomination is no band on this flow, and no cost in its unit either.
        return None, 'unknown', ['CRYPTO_FEE_ASSET_UNVERIFIED']
    if typ in ('deposit', 'withdraw'):
        if not _inside(event['account_identity'], event.get('wallet'), accounts):
            return None, 'unknown', ['CRYPTO_UNKNOWN_WALLET']
        # The API documents a fee, not whether delta.usdc is gross debit or
        # recipient credit. A small stablecoin fee becomes an estimate banded by
        # the fee; a larger one stays unresolved. Zero fee needs no basis assumption.
        fee = number(event.get('fee'))
        if typ == 'withdraw' and fee != 0:
            if not _fee_capped(event, amount, fee):
                return None, 'withdrawal', ['CRYPTO_WITHDRAWAL_FEE_BASIS_UNVERIFIED']
            return -amount, 'withdrawal', ['CRYPTO_WITHDRAWAL_FEE_BASIS_ESTIMATED']
        return amount if typ == 'deposit' else -amount, 'deposit' if typ == 'deposit' else 'withdrawal', []
    if typ not in ('internalTransfer', 'spotTransfer', 'subAccountTransfer', 'accountClassTransfer', 'send'):
        return None, 'unknown', ['UNKNOWN_HYPERLIQUID_DELTA_TYPE']
    origin, destination = event.get('origin_account_identity'), event.get('destination_account_identity')
    origin_wallet, destination_wallet = event.get('origin_wallet'), event.get('destination_wallet')
    if typ == 'accountClassTransfer' and event.get('transfer_route') in ('spot_perp', 'perp_spot'):
        origin = destination = event['account_identity']
        origin_wallet, destination_wallet = event['transfer_route'].split('_')
    elif typ != 'send':
        origin_wallet = destination_wallet = 'spot' if typ == 'spotTransfer' else 'perp'
    if not origin or not destination or not origin_wallet or not destination_wallet:
        return None, 'unknown', ['CRYPTO_FLOW_DIRECTION_UNAVAILABLE']
    a, b = _inside(origin, origin_wallet, accounts), _inside(destination, destination_wallet, accounts)
    if a is None or b is None:
        return None, 'unknown', ['CRYPTO_UNKNOWN_WALLET']
    sign = int(b) - int(a)
    kind = 'transfer_in' if sign > 0 else 'transfer_out'
    fee = number(event.get('fee'))
    # A send debits its fee on top of amount: an observed send arrived at the next
    # venue with more than a fee-inclusive amount could deliver. Other crossing fees have no proven basis.
    if sign and fee not in (None, Decimal(0)) and typ != 'send':
        if not _fee_capped(event, amount, fee):
            return None, 'unknown', ['CRYPTO_TRANSFER_FEE_BASIS_UNVERIFIED']
        return amount * sign, kind, ['CRYPTO_TRANSFER_FEE_BASIS_ESTIMATED']
    return amount * sign, kind, []


def _event_key(row):
    if row['source'] == 'binance_transfers':
        return (row['source'], row.get('account_identity'), row.get('native_record_id'), row.get('transfer_route'))
    typ = _flow_type(row)
    if row['source'] != 'hyperliquid_flows' or typ in ('deposit', 'withdraw'):
        return (row['source'], row.get('account_identity'), row.get('native_record_id'), row.get('execution_time_utc'))
    # Only typed counterpart observations and expanded spot/perp legs share a
    # cross-account key. Two deposits in one transaction are distinct credits.
    return (row['source'], row.get('transaction_ref') or row.get('native_record_id'),
            typ, row.get('execution_time_utc'))


def _event_fx(marks, currency, stamp):
    if currency == 'KRW':
        return {'rate_to_krw': '1', 'priced_at': _iso(stamp), 'source': 'native_krw'}
    available = []
    for mark in marks.get(currency, []):
        priced, rate = _time(mark.get('priced_at')), number(mark.get('rate_to_krw'))
        if (priced is not None and rate is not None and rate > 0 and mark.get('source')
                and not mark.get('reason_codes') and 0 <= (stamp - priced).total_seconds() <= 300):
            available.append((priced, mark))
    return max(available, key=lambda item: item[0])[1] if available else None


def _bracket_fx(left, right, currency, stamp):
    """(mark, |m1 - m0|): a stablecoin rate linear in time between the interval's two capture marks.

    Only for a stablecoin flow without an event-time mark; None when either
    endpoint lacks a clean mark for the coin.
    """
    points = []
    for snapshot in (left, right):
        mark = {**(snapshot.get('fx_times') or {}),
                **((snapshot.get('crypto_nav') or {}).get('fx_times') or {})}.get(currency) or {}
        priced, rate = _time(mark.get('priced_at')), number(mark.get('rate_to_krw'))
        if priced is None or rate is None or rate <= 0 or not mark.get('source') or mark.get('reason_codes'):
            return None, None
        points.append((priced, rate))
    (a, m0), (b, m1) = points
    if b <= a:
        return None, None
    share = min(max(Decimal(str((stamp - a).total_seconds())) / Decimal(str((b - a).total_seconds())), Decimal(0)),
                Decimal(1))
    rate = m0 if m1 == m0 else (m0 + (m1 - m0) * share).quantize(Decimal('0.00000001'))
    return {'rate_to_krw': str(rate), 'source': 'linear_interpolation', 'between': [_iso(a), _iso(b)]}, abs(m1 - m0)


def _interval(left, right, venue, events, coverage, aliases):
    t0, t1 = _cut(left)[0], _cut(right)[0]
    lo, hi, guards = _span(left, right, venue)
    first, last = left['crypto_nav'][venue], right['crypto_nav'][venue]
    accounts, reasons = _account_cut(first, aliases)
    if t1 <= t0:
        reasons.append('CRYPTO_INVALID_INTERVAL')
    end_accounts, end_reasons = _account_cut(last, aliases)
    reasons.extend(end_reasons)
    if accounts != end_accounts:
        reasons.append('CRYPTO_ACCOUNT_SCOPE_CHANGED')
    coverage_reasons, coverage_ids = _coverage(coverage, venue, accounts, lo - EVIDENCE_PAD, hi + EVIDENCE_PAD)
    reasons.extend(coverage_reasons)
    grouped = defaultdict(list)
    versions = []
    for row in events:
        if row['source'].split('_', 1)[0] != venue or row['account_identity'] not in accounts:
            continue
        if row.get('record_kind') == 'owner_csv':
            continue  # Statement precedence belongs to the caller, never double count it here.
        if any(stamp is None or t0 < stamp <= t1 for stamp in row.get('_conflict_times', [])):
            reasons.append('LEDGER_CONTENT_CONFLICT')
        stamp = _time(row.get('execution_time_utc'))
        if any(early <= b and a <= late for early, late in _readings(row, stamp) for a, b in guards):
            # Between a venue read and the cut, the NAV and the split disagree on its side.
            reasons.append('CRYPTO_FLOW_DURING_NAV_CAPTURE')
        if stamp is None:
            reasons.append('CRYPTO_FLOW_TIME_UNAVAILABLE')
            continue
        if not t0 < stamp <= t1:
            continue
        versions.append(row['_version'])
        if row.get('_conflict'):
            reasons.append('LEDGER_CONTENT_CONFLICT')
        if row.get('_legacy_only'):
            reasons.append('CRYPTO_LEGACY_FLOW_NOT_RECONCILED')
            continue  # A known fact can veto inference but cannot prove strict completeness.
        if row['source'] == 'binance_transfers':
            try:
                row = {**row, 'native_record_id': str(binance_transfer_id(row.get('native_record_id')))}
            except CryptoValidationError as exc:
                reasons.append(exc.code)
                continue
        grouped[_event_key(row)].append(row)
    by_currency, flags = defaultdict(list), defaultdict(set)
    for group in grouped.values():
        row = group[0]
        currency = row.get('currency')
        facts = {(r.get('amount'), r.get('currency'), r.get('execution_time_utc'), r.get('source_status'),
                  r.get('origin_account_identity'), r.get('destination_account_identity')) for r in group}
        legs = [(r.get('account_identity'), r.get('direction'), r.get('wallet')) for r in group]
        if len(facts) != 1 or len(legs) != len(set(legs)):
            reasons.append('CRYPTO_DUPLICATE_OR_CONFLICTING_LEG')
        if not currency:
            reasons.append('CRYPTO_FLOW_CURRENCY_UNAVAILABLE')
            continue
        amount, kind, codes = _classify(row, venue, accounts)
        reasons.extend(code for code in codes if code not in FEE_ESTIMATES)
        stamp = _time(row['execution_time_utc'])
        event = {'time': _iso(stamp), 'amount_native': str(amount) if amount is not None else None,
                 'kind': kind, 'external': amount != 0 if amount is not None else True,
                 'amount_krw': str(Decimal(0)) if amount == 0 else None, 'fx': None,
                 'event_ids': sorted(r['_version']['event_id'] for r in group)}
        if FEE_ESTIMATES & set(codes):
            # Gross debit or recipient credit: the crossing is uncertain by the fee.
            event['fee_band_native'] = str(number(row['fee']))
            flags[currency].update(FEE_ESTIMATES & set(codes))
        by_currency[currency].append(event)
        fee = number(row.get('fee'))
        if venue == 'hyperliquid' and fee is not None and fee != 0:
            by_currency[currency].append({'time': _iso(stamp), 'amount_native': str(-fee),
                'kind': 'fee', 'external': False, 'amount_krw': None, 'fx': None,
                'event_ids': event['event_ids']})
    currencies = set(first.get('by_currency') or {}) | set(last.get('by_currency') or {}) | set(by_currency)
    return [{'venue': venue, 'currency': currency, 'left': left, 'right': right, 't0': t0, 't1': t1,
             'events': by_currency[currency], 'reasons': reasons, 'flags': flags[currency],
             'versions': versions, 'coverage_ids': coverage_ids} for currency in sorted(currencies)]


def _record(draft, marks):
    """The shared flow record of one venue currency over (t0, t1]."""
    left, right, currency, reasons = draft['left'], draft['right'], draft['currency'], draft['reasons']
    flags, conversion = set(draft['flags']), []
    external = [e for e in draft['events'] if e['external']]
    for event in external:
        amount = number(event['amount_native'])
        if amount is None:
            conversion.append('CRYPTO_EVENT_FX_UNAVAILABLE')
            continue
        stamp = _time(event['time'])
        mark, spread = _event_fx(marks, currency, stamp), Decimal(0)
        if mark is None and currency in STABLECOINS:
            mark, spread = _bracket_fx(left, right, currency, stamp)
            if mark is not None:
                flags.add(INTERPOLATED)
        if mark is None:
            conversion.append('CRYPTO_EVENT_FX_UNAVAILABLE')
            continue
        rate = number(mark['rate_to_krw'])
        value = amount * rate
        # Sensitivity: the coin's mark move over the interval, plus either fee basis.
        half = abs(amount) * spread + (number(event.get('fee_band_native')) or Decimal(0)) * rate
        event.update(fx=mark, amount_krw=str(value))
        if half:
            event['amount_krw_range'] = [str(value - half), str(value + half)]
    native = sum((number(e['amount_native']) for e in external), Decimal(0)) if not reasons else None
    converted = sum((number(e['amount_krw']) for e in external), Decimal(0)) if not reasons and not conversion else None
    t0, t1 = draft['t0'], draft['t1']
    return {'source': draft['venue'], 'account': draft['venue'], 'currency': currency,
        't0': _iso(t0), 't1': _iso(t1), 'start_snapshot_id': left['snapshot_id'], 'end_snapshot_id': right['snapshot_id'],
        'flow_native': str(native) if native is not None else None, 'income_native': None,
        'flow_krw': str(converted) if converted is not None else None,
        'events': draft['events'], 'timing': 'unknown' if reasons else 'known', 'method': 'api_direct',
        'confidence': 'unresolved' if reasons else 'conditional' if flags else 'observed',
        'reasons': sorted(set(reasons + conversion) | flags),
        'conversion_status': ('unresolved' if reasons or conversion else
                              'interpolated' if INTERPOLATED in flags else 'observed'),
        'inputs': {'calculation_version': CALCULATION_VERSION,
            'start_snapshot_version': left.get('version'), 'end_snapshot_version': right.get('version'),
            'risk_snapshot_ids': [left['crypto_nav'].get('risk_snapshot_id'), right['crypto_nav'].get('risk_snapshot_id')],
            'ledger_versions': draft['versions'], 'coverage_ids': draft['coverage_ids']}}


def crypto_interval_flows(db_path, start, end):
    """Dates select KST snapshot dates; aware instants select exact UTC bounds.

    The first and last snapshots must lie inside the requested range. Incomplete
    NAV days are bridged only when the entire resulting flow interval is covered.
    A complete native flow may have null flow_krw when no event-time mark exists.
    """
    lo, hi = _bound(start), _bound(end, True)
    if hi < lo:
        raise ValueError('CRYPTO_INVALID_INTERVAL')
    path = Path(db_path)
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'nav_snapshot_versions', 'nav_snapshot_representatives'} <= tables:
            return []
        snapshots = [json.loads(r[0]) for r in conn.execute('''SELECT v.payload_json FROM nav_snapshot_versions v
            JOIN nav_snapshot_representatives r USING(snapshot_id) ORDER BY r.snapshot_date''')]
        marks = defaultdict(list)
        for snapshot in snapshots:
            quotes = {**(snapshot.get('fx_times') or {}),
                      **((snapshot.get('crypto_nav') or {}).get('fx_times') or {})}
            for currency, mark in quotes.items():
                marks[currency].append(mark)
        snapshots = [s for s in snapshots if _cut(s)[0] is not None and lo <= _cut(s)[0] <= hi]
        snapshots.sort(key=lambda s: _cut(s)[0])
        aliases = dict(conn.execute('SELECT alias_identity,canonical_identity FROM ledger_account_aliases')) if 'ledger_account_aliases' in tables else {}
        jobs, drafts, result = defaultdict(list), [], defaultdict(list)
        for venue in ('binance', 'hyperliquid'):
            complete = [s for s in snapshots if _complete(s, venue)]
            for left, right in zip(complete, complete[1:]):
                jobs[_span(left, right, venue)[:2]].append((venue, left, right))
        # Apply the evidence budget to each actual NAV interval. Reuse a view
        # only for matching boundaries, then release it within this one snapshot.
        for (since, until), group in sorted(jobs.items()):
            events, coverage = (read_view(conn, path, aliases, since - EVIDENCE_PAD, until + EVIDENCE_PAD)
                                if 'ledger_coverage' in tables else ([], []))
            for row in events:
                row['_conflict_times'] = [_time(stamp) for stamp in row['_conflict_times']]
            for venue, left, right in group:
                drafts.extend(_interval(left, right, venue, events, coverage, aliases))
        for draft in drafts:
            result[draft['venue']].append(_record(draft, marks))
        return [row for venue in ('binance', 'hyperliquid') for row in result[venue]]
