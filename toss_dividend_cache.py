"""Atomic, process-shared public dividend evidence in kinvest_common.cache_dir().

Only validated schedules are retained as good evidence. Failed daily refreshes
keep that copy's observation time and coverage unchanged. No account facts,
holdings, credentials, SQLite tables, or migrations are stored here.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from kinvest_common import cache_dir
from toss_dividends import (
    LISTING_BASIS,
    PAYMENT_LAGS,
    PAYMENT_POLICY,
    check_instrument_identity,
    estimated_coverage,
    payment_window,
)

VERSION = 1
MAX_BYTES = 2_000_000


def _stamp(value):
    when = datetime.fromisoformat(value)
    if when.utcoffset() is None:
        raise ValueError('CACHE_TIME_UNKNOWN')
    return when.astimezone(timezone.utc)


def path_for(symbol, currency):
    identity = hashlib.sha256(f'{currency}|{symbol}'.encode()).hexdigest()
    return cache_dir() / 'dividend-schedules-v1' / (identity + '.json')


def _valid_good(good, symbol, currency, now):
    """Validate serialized output as strictly as its in-memory contract."""
    when = _stamp(good['observed_at'])
    if when > now:
        return False
    data = good['schedule']
    proof, = data['coverage']
    if (proof['symbol'] != symbol or proof['currency'] != currency or not estimated_coverage(proof)
            or not isinstance(data['rows'], list)):
        return False
    first, last = date.fromisoformat(proof['start']), date.fromisoformat(proof['end'])
    zone = ZoneInfo('America/New_York' if currency == 'USD' else 'Asia/Seoul')
    observed = when.astimezone(zone).date()
    evidence_first, evidence_last = date.fromisoformat(proof['evidence_start']), date.fromisoformat(proof['evidence_end'])
    minimum, maximum = PAYMENT_LAGS['US' if currency == 'USD' else 'KR']
    if (first > last or evidence_first > evidence_last or evidence_last >= observed
            or first < evidence_first + timedelta(days=maximum)
            or last > min(observed, evidence_last + timedelta(days=minimum))):
        return False
    # Copies without a proven bar span (older cache format) are not evidence.
    price_first = date.fromisoformat(proof['price_history_start'])
    listed = proof['listing_basis'] == LISTING_BASIS
    if not (evidence_first < price_first <= observed if listed
            else proof['listing_basis'] is None and price_first == evidence_first):
        return False
    reference = proof['reference_symbol']
    if reference not in ([symbol] if currency == 'USD' or '.' in symbol else [symbol + '.KS', symbol + '.KQ']):
        return False
    if currency == 'KRW' and data.get('binding', {}).get('reference_symbol') != reference:
        return False
    if currency == 'KRW':
        check_instrument_identity(data['binding'], symbol, None)
    seen = set()
    for row in data['rows']:
        if (row['symbol'] != symbol or row['currency'] != currency or row['reference_symbol'] != reference
                or row['market'] != ('US' if currency == 'USD' else 'KR') or row['id'] in seen
                or not isinstance(row['gross_per_share'], str)):
            return False
        seen.add(row['id'])
        amount = Decimal(row['gross_per_share'])
        if not amount.is_finite() or amount <= 0:
            return False
        expected = payment_window(row['ex_date'], row['market'])
        earliest = price_first if listed else evidence_first - timedelta(days=1)
        if not earliest <= date.fromisoformat(row['ex_date']) <= observed:
            return False
        identity = hashlib.sha256(f"{reference}|{row['ex_date']}|{row['gross_per_share']}".encode()).hexdigest()
        if row['id'] != identity:
            return False
        if row['pay_date'] is not None:
            pay = date.fromisoformat(row['pay_date'])
            if pay < date.fromisoformat(row['ex_date']):
                return False
            expected.update(pay_date=pay.isoformat(), expected_pay_date=pay.isoformat(),
                payment_start=(pay - timedelta(days=3)).isoformat(),
                payment_end=(pay + timedelta(days=3)).isoformat(), payment_basis='announced_calendar')
        if any(row[key] != value for key, value in expected.items()):
            return False
    return True


def load(symbol, currency, now, *, preserve_newer=False):
    try:
        with path_for(symbol, currency).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        record = json.loads(raw)
        # Readers reject future evidence. Writers under the lock must retain a
        # valid newer commit even if the wall clock moved backwards, not erase it.
        checked_at = max(now, _stamp(record['last_attempt_at'])) if preserve_newer else now
        if (record['version'] != VERSION or record['policy'] != PAYMENT_POLICY
                or record['symbol'] != symbol or record['currency'] != currency
                or _stamp(record['last_attempt_at']) > checked_at
                or record['good'] is not None and not _valid_good(record['good'], symbol, currency, checked_at)):
            return None
        if record['failure_reason'] is not None and not isinstance(record['failure_reason'], str):
            return None
        if record['good'] is None and not record['failure_reason'] or record['good'] and (
                _stamp(record['good']['observed_at']) > _stamp(record['last_attempt_at'])):
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        return None


def market_day(currency, when):
    """One refresh per market calendar day: its eve is the last fully observed date.

    A UTC day would pin a US copy fetched before New York midnight to D-2 until the
    next UTC midnight, and a KR copy fetched before Seoul midnight to D-1.
    """
    return when.astimezone(ZoneInfo('America/New_York' if currency == 'USD' else 'Asia/Seoul')).date()


def attempted_today(record, now):
    return bool(record and market_day(record['currency'], _stamp(record['last_attempt_at']))
                >= market_day(record['currency'], now))


def schedule(record, *, pending=False):
    if record is None or record['good'] is None:
        return None
    good = record['good']
    result = deepcopy(good['schedule'])
    status = 'refresh_pending' if pending else 'refresh_failed' if record['failure_reason'] else 'current'
    for proof in result['coverage']:
        proof.update(observed_at=good['observed_at'], cache_status=status)
    result['cache_evidence'] = {'observed_at': good['observed_at'], 'last_attempt_at': record['last_attempt_at'],
                               'status': status, 'refresh_reason': record['failure_reason']}
    return result


def _save(path, record):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.stem + '.', suffix='.tmp', delete=False) as stream:
            name = stream.name
            json.dump(record, stream, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _attempt(symbol, currency, now, fetch, previous):
    try:
        data = fetch(now)
    except Exception:
        data = {'rows': [], 'coverage': [{'symbol': symbol, 'currency': currency,
            'start': now.date().isoformat(), 'end': now.date().isoformat(), 'status': 'limited',
            'reasons': ['PUBLIC_DIVIDEND_SCHEDULE_UNAVAILABLE']}]}
    good = {'observed_at': now.isoformat(), 'schedule': data}
    try:
        valid = _valid_good(good, symbol, currency, now)
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        valid = False
    reason = None if valid else next(iter((data.get('coverage') or [{}])[0].get('reasons', [])),
                                    'PUBLIC_DIVIDEND_SCHEDULE_UNAVAILABLE')
    record = {'version': VERSION, 'policy': PAYMENT_POLICY, 'symbol': symbol, 'currency': currency,
              'last_attempt_at': now.isoformat(), 'failure_reason': reason,
              'good': good if valid else previous['good'] if previous else None}
    return data, record


def refresh(symbol, currency, fetch, *, clock=None):
    """Re-read time and disk under the lock; fetch receives that observation time.

    No queue-time timestamp reaches the fetch or decides cache validity. A lock
    spans fetch/commit. The final merge also prevents any older work or failure
    from replacing newer good evidence, including across wall-clock rollback.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    path = path_for(symbol, currency)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        now = clock()
        data, record = _attempt(symbol, currency, now, fetch, load(symbol, currency, now))
        return schedule(record) or data  # Read-only cache still permits bounded old evidence.
    with os.fdopen(descriptor, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = clock()
        previous = load(symbol, currency, now, preserve_newer=True)
        if attempted_today(previous, now):
            return schedule(previous) or {'rows': [], 'coverage': [], 'cache_failure': previous['failure_reason']}
        data, record = _attempt(symbol, currency, now, fetch, previous)
        latest = load(symbol, currency, clock(), preserve_newer=True)
        if latest and _stamp(latest['last_attempt_at']) >= _stamp(record['last_attempt_at']):
            return schedule(latest) or data
        if latest and latest['good'] and (not record['good'] or
                _stamp(latest['good']['observed_at']) > _stamp(record['good']['observed_at'])):
            record['good'] = latest['good']
        try:
            _save(path, record)
        except OSError:
            pass  # A failed write cannot erase valid in-memory/reference evidence.
        return schedule(record) or data
