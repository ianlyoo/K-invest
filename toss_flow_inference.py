"""Guarded Toss cash residuals, computed from immutable local evidence.

Pure replay API: ``infer_toss_flows(snapshots, fills, schedule, coverage=(),
start=None, end=None, accounts=None, transfers=(), listing_evidence=())``. No I/O
in this function.
All monetary/quantity inputs and outputs are finite decimal strings. Inputs:

* snapshots: one plain dict per account/version, with ``account``, ``snapshot_id``,
  ``version``, ``time`` (aware ISO), ``complete`` (bool), ``reasons`` (list),
  ``cash`` {KRW/USD: {value, basis, observed_at}}, ``open_order_count`` (int),
  ``holdings`` [{symbol, currency, quantity, price, priced_at}], and
  ``fx`` {rate, valid_from, valid_until, observed_at}; a null valid_until (a
  yfinance fallback mark per fx.policy, or a row stored before it) means
  valid_from + MAX_MARK_AGE_SECONDS. Cash basis must be
  TOSS_BUYING_POWER_NO_OPEN_ORDERS. An empty holdings list is explicit zero.
  Supply exactly one immutable version per account/instant. Include earlier
  snapshots needed to establish dividend entitlement before the ex-date.
* fills: normalized ``toss_transactions`` events with ``account``, ``event_id``,
  ``source_record_id``, ``symbol``, ``currency``, ``side`` (buy/sell), ``quantity``,
  ``gross_amount``, ``price_native``, ``fee``, ``tax``, ``execution_time_utc``,
  ``source_status``, plus optional ``reasons`` and ``version``. Order quantities
  are cumulative. Repeated IDs are a failure, never silently counted twice.
  Conflicting cumulative revisions are unresolved, not guessed incremental fills.
* coverage: [{account, start, end, status, contract, coverage_id, reasons}].
  Complete ``toss-fills-v1`` windows certify validated, exhausted OPEN/CLOSED
  scans without creation-date filters. Date-only/legacy coverage cannot do so.
  ``observed_at`` orders contradictory coverage and subsequent complete repairs.
* schedule: None means dividends are not modelled (a design decision; the
  tool path): no income matching, dividend cash stays in the residual and becomes
  an inferred flow or R8 income, and every computed record carries
  DIVIDENDS_COUNTED_AS_FLOWS. A US dividend paid gross with its tax debited in
  KRW fails the R3a FX pair (FX_RATE_OUTSIDE_BAND) and stays unresolved. Otherwise
  {rows: [{id, symbol, currency, market (US/KR), ex_date, pay_date,
  gross_per_share}], coverage: [{symbol, currency, start, end, status}]}.
  Rows may instead supply payment_start/payment_end and expected_pay_date with
  pay_date=None. Dates are market-local ISO dates. The Yahoo adapter projects
  validated ex-dates into conditional market payment windows (toss_dividends.py).
  Heuristic coverage has status=estimated and the dated policy basis/reason;
  absence under it supports only conditional classifications, never complete
  dividend coverage. Missing schedules cannot establish zero. Every potentially
  entitled symbol must have schedule coverage before a dividend match or zero.
  Unknown ex-date entitlement alone permits a conditional zero under R4' below;
  it never permits a dividend match or a nonzero residual classification.
  A history shortened to Yahoo's listing date yields to any earlier local
  holding, fill or supplied ex-date: DIVIDEND_LISTING_DATE_CONFLICT, unresolved.
* listing_evidence: optional [{symbol, currency, time}], the earliest holding or
  fill instant per symbol in ANY Toss account, whatever the requested scope.
* transfers: optional independent paired evidence [{id, out_account, in_account,
  currency, amount_native, time, evidence_ref, basis}], with basis
  ``ledger_transfer_match``. A shared amount alone is never matching evidence.

Intervals use consecutive complete account snapshots, (t0,t1], bridging missing
or incomplete middle snapshots without interpolating. An incomplete latest
endpoint yields unresolved diagnostic rows. start/end select interval END times;
ISO dates mean inclusive KST dates, aware ISO timestamps mean exact instants.

Cash arithmetic: delta - (sells - buys - commission - tax) - identified income.
Rounding tolerance is 1 KRW / 0.01 USD; quantities reconcile exactly. Dividend
net = gross * shares immediately before ex-date * (0.85 US / 0.846 KR), a
conditional withholding assumption, not tax advice. Announced pay dates use +/-3
calendar days; expected US/KR windows use the dated toss_dividends policy.
Unique subsets (at most 12 candidates) match within max(rounding, 0.1% of expected
amount). No annual dividend-rate approximation. Resolution rules:

* R1: residual within rounding while an eligible window is open means pending,
  not paid (DIVIDEND_PENDING_NOT_PAID); do not consume its schedule IDs.
* R4': zero residual with only unknown entitlement is permitted conditionally,
  retaining DIVIDEND_ENTITLEMENT_UNKNOWN plus its guarded-zero reason. Schedule,
  fill, quantity and snapshot failures still block, as does nonzero residual.
* R2: unmatched nonzero residual may be a flow (DIVIDEND_POSSIBLY_IN_FLOW), unless
  it lies in [half net, 1.25*gross] for any pending subset: then it is unresolved
  with DIVIDEND_AMOUNT_UNEXPLAINED. Neither case consumes a dividend.
* R5: with no net match, a unique gross match is allowed for KR. US gross also
  requires a negative KRW residual matching the 15% tax at either endpoint mark
  within 2.5%; book that actual debit as negative income/fee, not external flow.
* R3a: opposite KRW/USD residuals must imply an FX rate within 2.5% of BOTH valid
  endpoint marks (FX_CONVERSION_ESTIMATED_WITHIN_BAND). A frozen replay measured a max deviation
  of 2.18% across its statement conversion days; this is not a universal rate bound.
* R6: an inferred USD flow with abs(USD residual)*closing FX*2.5% > 20,000 KRW
  is unresolved (USD_FLOW_FX_VALUATION_UNSEPARATED), with its error bound in inputs.
* R8: after the preceding gates, an unexplained residual above rounding but at
  most 5,000 KRW (USD converted at closing snapshot FX) is signed income/cost,
  event kind income_other, never a flow (SMALL_RESIDUAL_TREATED_AS_INCOME).

Threshold source for R3a/R6/R8: an offline frozen-replay diagnosis (2026-10-14).
They are estimation policy, not measured live accuracy. R1/R4' can miss
a payment exactly offset by a withdrawal; R8 can misread a small transfer.
Outside-band or incomplete pairs remain unresolved. FX legs have external=False;
flow_native is zero at the account boundary and conversion costs remain return.
Inter-account transfers retain signed flow amounts and both marked legs; consumers
may cancel only when both accounts and the matching evidence are in their scope.

Invalid coverage/costs/quantity/FX never become zero. Remaining supported nonzero
residuals are conditional external flows with unknown timing. Every resolved Toss
outcome, including zero, is conditional; none is observed or verified. Estimated
dividend absence continues to carry DIVIDEND_ABSENCE_ESTIMATED_FROM_SCHEDULE on
both currencies because they share the FX check.
No database writes, migrations, broker calls, or return/tool-layer changes.
Public contracts: Toss OpenAPI 1.2.24 and yfinance quote calendar source, accessed
2026-10-13 (URLs in docs/toss-flow-inference.md).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
import time
from bisect import bisect_left, bisect_right
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_nav import MAX_MARK_AGE_SECONDS, TOSS_CASH_BASIS, fx_mark_quality
from ledger_returns import ReadBudgetExceeded, Reader, _effective, _roster
from portfolio_ledger import (
    CONFLICT_TIME_SQL,
    ENRICHMENT_TIME_SQL,
    FLOW_TIME_SQL,
    coverage_time_sql,
)
from toss_dividends import (
    ABSENCE_ESTIMATED,
    LISTING_BASIS,
    MAX_PAYMENT_LAG_DAYS,
    estimated_coverage,
)

CURRENCIES = ('KRW', 'USD')
TOLERANCE = {'KRW': Decimal('1'), 'USD': Decimal('.01')}
FX_BAND = Decimal('.025')
USD_FLOW_FX_BUDGET_KRW = Decimal('20000')
SMALL_RESIDUAL_KRW = Decimal('5000')
DIVIDENDS_AS_FLOWS = 'DIVIDENDS_COUNTED_AS_FLOWS'
INCOME_ADVISORIES = {
    ABSENCE_ESTIMATED, 'DIVIDEND_PENDING_NOT_PAID', 'DIVIDEND_POSSIBLY_IN_FLOW',
    'DIVIDEND_GROSS_MATCHED', 'DIVIDEND_WITHHOLDING_IN_KRW',
    'DIVIDEND_ENTITLEMENT_UNKNOWN_ZERO_RESIDUAL', 'FX_CONVERSION_ESTIMATED_WITHIN_BAND',
    'SMALL_RESIDUAL_TREATED_AS_INCOME', DIVIDENDS_AS_FLOWS,
}
WITHHOLDING_NET = {'US': Decimal('.85'), 'KR': Decimal('.846')}
KST = ZoneInfo('Asia/Seoul')
ZERO = Decimal(0)
FILL_STATES = {'PENDING', 'PARTIAL_FILLED', 'PENDING_CANCEL', 'PENDING_REPLACE', 'FILLED',
               'CANCELED', 'REJECTED', 'REPLACED', 'CANCEL_REJECTED', 'REPLACE_REJECTED'}
# Only observed contradictions can retract earlier complete proof. Outages,
# scan limits and filtered/incomplete attempts never establish new coverage.
FILL_SEMANTIC_CONTRADICTIONS = {
    'INVALID_SOURCE_RESPONSE', 'INVALID_FILL_RESPONSE', 'FILL_PAGING_INCONSISTENT',
    'DUPLICATE_FILL_ORDER', 'FILL_REQUEST_BINDING_MISMATCH', 'INVALID_FILL_TOTALS',
    'FILL_AMOUNT_OR_COST_MISSING', 'FILL_TIME_MISSING', 'INVALID_FILL_TIME',
    'FILL_LEGACY_EVIDENCE_MISMATCH', 'LEDGER_CONTENT_CONFLICT', 'CURRENCY_BINDING_MISMATCH',
    'SOURCE_RECORD_ID_UNAVAILABLE', 'UNSUPPORTED_OR_MISSING_RECORD_ID',
}


@lru_cache(maxsize=16384)
def _decimal(value):
    if not isinstance(value, str) or len(value) > 100 or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise ValueError('INVALID_DECIMAL_INPUT')
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError('INVALID_DECIMAL_INPUT')
    return result


@lru_cache(maxsize=16384)
def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('INVALID_INTERVAL_TIME')
    return result.astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat()


def _bound(value, end=False):
    if value is None:
        return datetime.max.replace(tzinfo=timezone.utc) if end else datetime.min.replace(tzinfo=timezone.utc)
    if len(value) == 10:
        day = date.fromisoformat(value) + (timedelta(days=1) if end else timedelta())
        result = datetime.combine(day, day_time(), KST).astimezone(timezone.utc)
        return result - timedelta(microseconds=1) if end else result
    return _time(value)


def _money(value):
    return format(value, 'f') if value is not None else None


def _holding_map(snapshot):
    result = {}
    if not isinstance(snapshot.get('holdings'), list):
        raise ValueError('HOLDINGS_MISSING')
    for row in snapshot['holdings']:
        symbol = row['symbol']
        if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip().upper() or symbol in result:
            raise ValueError('DUPLICATE_OR_INVALID_HOLDING')
        quantity = _decimal(row['quantity'])
        if row['currency'] not in CURRENCIES or quantity <= 0 or _decimal(row['price']) <= 0:
            raise ValueError('INVALID_HOLDING')
        _time(row['priced_at'])
        result[symbol] = (row['currency'], quantity)
    return result


def _snapshot_reasons(snapshot):
    reasons = list(snapshot.get('reasons') or [])
    if snapshot.get('complete') is not True:
        reasons.append('INCOMPLETE_TOSS_SNAPSHOT')
    count = snapshot.get('open_order_count')
    if type(count) is not int or count < 0:
        reasons.append('TOSS_OPEN_ORDERS_UNVERIFIED')
    elif count:
        reasons.append('TOSS_OPEN_ORDERS_PRESENT')
    try:
        _time(snapshot['time'])
        if not snapshot['snapshot_id'] or type(snapshot['version']) is not int:
            raise ValueError('SNAPSHOT_VERSION_MISSING')
        _holding_map(snapshot)
        for currency in CURRENCIES:
            cash = snapshot['cash'][currency]
            if cash['basis'] != TOSS_CASH_BASIS or _decimal(cash['value']) < 0:
                raise ValueError('CASH_BASIS_UNVERIFIED')
            _time(cash['observed_at'])
        fx = snapshot['fx']
        valid_from = _time(fx['valid_from'])
        # A Toss rate (fx.policy) carries validUntil; a yfinance fallback mark or a row stored
        # before fx.policy has none, so like daily_nav.fx_mark_quality its own price time bounds its age.
        until = (valid_from + timedelta(seconds=MAX_MARK_AGE_SECONDS) if fx.get('valid_until') is None
                 else _time(fx['valid_until']))
        if _decimal(fx['rate']) <= 0 or not valid_from <= _time(fx['observed_at']) <= until:
            raise ValueError('INVALID_SNAPSHOT_FX')
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
        reasons.append('SNAPSHOT_PROVENANCE_INCOMPLETE')
    return sorted(set(reasons))


def _coverage_reasons(coverage, account, t0, t1):
    windows, ids, complete, incomplete = [], [], [], []
    for row in coverage:
        if row.get('account') != account:
            continue
        try:
            a, b = _time(row['start']), _time(row['end'])
        except (KeyError, TypeError, AttributeError, ValueError):
            continue
        if row.get('contract') != 'toss-fills-v1' or a > b or b < t0 or a > t1:
            continue
        if (row.get('status') == 'complete' and row.get('contract') == 'toss-fills-v1'
                and not row.get('reasons') and a <= b and b >= t0 and a <= t1):
            windows.append((a, b))
            complete.append((a, b, row))
            ids.append(row.get('coverage_id'))
        else:
            findings = set(row.get('reasons') or [])
            if findings & FILL_SEMANTIC_CONTRADICTIONS:
                incomplete.append((a, b, row))
    reached = t0
    for a, b in sorted(windows):
        # Python's finest representable instant is one microsecond. Inclusive
        # windows ending at .999999 join the following midnight without a gap.
        if a > reached + timedelta(microseconds=1):
            break
        reached = max(reached, b)
    reasons = []
    if max((b for _, b in windows), default=t0) < t1:
        reasons.append('FILLS_NOT_COLLECTED_THROUGH_T1')
    if reached < t1:
        reasons.append('FILL_COVERAGE_GAP')
    for a, b, row in incomplete:
        # Contradictory financial/response evidence invalidates older proof.
        # A transport-only outage contributes no new evidence in either direction.
        try:
            observed = _time(row['observed_at'])
            repaired = any(x <= max(a, t0) and y >= min(b, t1)
                           and _time(proof['observed_at']) > observed for x, y, proof in complete)
        except (KeyError, TypeError, AttributeError, ValueError):
            repaired = False
        if not repaired:
            reasons.extend(row.get('reasons') or ['FILL_EVIDENCE_INCOMPLETE'])
    return reasons, sorted(x for x in ids if x is not None)


def _fills_between(fills, account, t0, t1):
    if isinstance(fills, _Replay):
        return fills.between(account, t0, t1)
    selected, reasons, seen = [], [], set()
    for row in fills:
        if row.get('account') != account:
            continue
        for conflict_time in row.get('conflict_times', []):
            try:
                if t0 < _time(conflict_time) <= t1:
                    reasons.append('LEDGER_CONTENT_CONFLICT')
            except (TypeError, AttributeError, ValueError):
                reasons.append('LEDGER_CONTENT_CONFLICT')
        try:
            stamp = _time(row['execution_time_utc'])
        except (KeyError, TypeError, AttributeError, ValueError):
            reasons.append('FILL_TIME_MISSING')
            continue
        if not t0 < stamp <= t1:
            continue
        try:
            identity = row['source_record_id']
            if not identity or identity in seen:
                reasons.append('DUPLICATE_FILL_ORDER')
            seen.add(identity)
            if not row['event_id'] or row['source_status'] not in FILL_STATES:
                reasons.append('INVALID_FILL_STATUS_OR_ID')
            if row['side'] not in ('buy', 'sell') or row['currency'] not in CURRENCIES:
                reasons.append('INVALID_FILL_BINDING')
            if not isinstance(row['symbol'], str) or row['symbol'] != row['symbol'].strip().upper() or not row['symbol']:
                reasons.append('INVALID_FILL_BINDING')
            values = {field: _decimal(row[field]) for field in ('quantity', 'gross_amount', 'price_native', 'fee', 'tax')}
            if any(value < 0 for value in values.values()) or values['quantity'] <= 0 or values['price_native'] <= 0:
                reasons.append('INVALID_FILL_TOTALS')
            elif abs(values['quantity'] * values['price_native'] - values['gross_amount']) > TOLERANCE[row['currency']]:
                reasons.append('INVALID_FILL_TOTALS')
            reasons.extend(row.get('reasons') or [])
            selected.append(row)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            reasons.append('FILL_AMOUNT_OR_COST_MISSING')
    return selected, sorted(set(reasons))


def _trade_effect(fills, currency):
    return sum(((1 if r['side'] == 'sell' else -1) * _decimal(r['gross_amount'])
                - _decimal(r['fee']) - _decimal(r['tax']) for r in fills if r['currency'] == currency), ZERO)


def _quantities_reconcile(s0, s1, fills):
    left, right = _holding_map(s0), _holding_map(s1)
    delta = {}
    for row in fills:
        key = (row['symbol'], row['currency'])
        delta[key] = delta.get(key, ZERO) + (1 if row['side'] == 'buy' else -1) * _decimal(row['quantity'])
    keys = {(s, c) for s, (c, _) in [*left.items(), *right.items()]} | set(delta)
    return all((right.get(s, (c, ZERO))[1] if right.get(s, (c, ZERO))[0] == c else ZERO)
               - (left.get(s, (c, ZERO))[1] if left.get(s, (c, ZERO))[0] == c else ZERO)
               == delta.get((s, c), ZERO) for s, c in keys)


def _entitlement(row, account, history, fills, coverage):
    zone = ZoneInfo('America/New_York') if row['market'] == 'US' else KST
    ex = datetime.combine(date.fromisoformat(row['ex_date']), day_time(), zone).astimezone(timezone.utc)
    if isinstance(fills, _Replay):
        key = (account, row['symbol'], row['currency'], row['ex_date'], row['market'])
        if key in fills.entitlements:
            return fills.entitlements[key]
        stamps, anchors = fills.anchors.get(account, ([], []))
        index = bisect_left(stamps, ex)
        anchors = anchors[index - 1:index] if index else []
    else:
        anchors = [s for s in history if s.get('account') == account and _time(s['time']) < ex and not _snapshot_reasons(s)]
    if not anchors:
        return None, [], ['DIVIDEND_ENTITLEMENT_UNKNOWN']
    anchor = max(anchors, key=lambda s: _time(s['time']))
    reasons, ids = _coverage_reasons(coverage, account, _time(anchor['time']), ex)
    trades, errors = _fills_between(fills, account, _time(anchor['time']), ex)
    if reasons or errors:
        return None, [], ['DIVIDEND_ENTITLEMENT_UNKNOWN']
    holdings = _holding_map(anchor)
    currency, quantity = holdings.get(row['symbol'], (row['currency'], ZERO))
    if currency != row['currency']:
        return None, [], ['DIVIDEND_ENTITLEMENT_UNKNOWN']
    quantity += sum(((1 if r['side'] == 'buy' else -1) * _decimal(r['quantity']) for r in trades
                     if r['symbol'] == row['symbol'] and r['currency'] == currency), ZERO)
    if quantity < 0:
        return None, [], ['DIVIDEND_ENTITLEMENT_UNKNOWN']
    result = quantity, [anchor['snapshot_id'], *ids, *(r['event_id'] for r in trades)], []
    if isinstance(fills, _Replay):
        fills.entitlements[key] = result
    return result


def _income(schedule, history, fills, coverage, account, currency, t0, t1, residual, used):
    candidates, reasons, inputs = [], [], []
    if isinstance(fills, _Replay):
        universe = {symbol for symbol, first_seen in fills.symbols.get((account, currency), {}).items() if first_seen <= t1}
    else:
        universe = {r['symbol'] for s in history if s.get('account') == account and _time(s['time']) <= t1
                    for r in s.get('holdings', []) if r.get('currency') == currency}
        universe |= {r['symbol'] for r in fills if r.get('account') == account and r.get('currency') == currency
                     and _time(r['execution_time_utc']) <= t1}
    market = 'US' if currency == 'USD' else 'KR'
    zone = ZoneInfo('America/New_York') if market == 'US' else KST
    first, last = t0.astimezone(zone).date(), t1.astimezone(zone).date()
    complete, estimated = set(), set()
    for row in schedule.get('coverage', []):
        if (row.get('currency') == currency
                and row.get('start', '') <= first.isoformat() and row.get('end', '') >= last.isoformat()):
            if estimated_coverage(row):
                estimated.add(row.get('symbol'))
            elif (row.get('status') == 'complete' and not row.get('reasons') and not
                  str(row.get('basis', '')).startswith(('market-payment-window-', 'estimated-market-payment-window-'))):
                complete.add(row.get('symbol'))
    advisory = [ABSENCE_ESTIMATED] if universe & estimated else []
    seen = set()
    for row in schedule.get('rows', []):
        if row.get('symbol') not in universe or row.get('currency') != currency:
            continue
        try:
            ex = date.fromisoformat(row['ex_date'])
            if 'payment_start' in row or 'payment_end' in row:
                pay_first, pay_last = date.fromisoformat(row['payment_start']), date.fromisoformat(row['payment_end'])
                if pay_first > pay_last or pay_last < ex:
                    raise ValueError('INVALID_PAYMENT_WINDOW')
            else:
                pay = date.fromisoformat(row['pay_date'])
                if pay < ex:
                    raise ValueError('INVALID_PAYMENT_DATE')
                pay_first, pay_last = pay - timedelta(days=3), pay + timedelta(days=3)
            if pay_last < first or pay_first > last:
                continue
            key = (row['symbol'], row['ex_date'], row.get('pay_date'), row.get('payment_start'), row.get('payment_end'))
            if key in seen or not row['id'] or row['market'] != market or _decimal(row['gross_per_share']) <= 0:
                reasons.append('INVALID_DIVIDEND_SCHEDULE')
                continue
            seen.add(key)
            if (account, row['id']) in used:
                continue
            quantity, evidence, errors = _entitlement(row, account, history, fills, coverage)
            reasons.extend(errors)
            inputs.extend(evidence)
            if quantity is not None and quantity > 0:
                gross = _decimal(row['gross_per_share']) * quantity
                candidates.append((row, gross * WITHHOLDING_NET[market], gross))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            reasons.append('INVALID_DIVIDEND_SCHEDULE')
    # Check every potentially entitled symbol BEFORE accepting any matching
    # subset. An unknown second payment can offset an external withdrawal.
    if universe - (complete | estimated):
        reasons.append('DIVIDEND_COVERAGE_INCOMPLETE')
    if set(reasons) == {'DIVIDEND_ENTITLEMENT_UNKNOWN'} and abs(residual) <= TOLERANCE[currency]:
        # R4': zero does not identify a dividend or consume entitlement. Keep the
        # unknown reason; only this guarded zero outcome makes it advisory.
        return ZERO, [], [*advisory, 'DIVIDEND_ENTITLEMENT_UNKNOWN',
                          'DIVIDEND_ENTITLEMENT_UNKNOWN_ZERO_RESIDUAL'], inputs
    if reasons:
        return None, [], reasons, inputs
    if candidates and abs(residual) <= TOLERANCE[currency]:
        # R1 precedes subset matching: even a tiny pending dividend must not be
        # consumed just because its amount falls within rounding of zero.
        return ZERO, [], [*advisory, 'DIVIDEND_PENDING_NOT_PAID'], inputs
    if len(candidates) > 12:
        return None, [], ['DIVIDEND_CANDIDATE_LIMIT'], inputs
    matches, gross_matches, dividend_like = [], [], False
    for count in range(1, len(candidates) + 1):
        for subset in itertools.combinations(candidates, count):
            expected = sum((net for _, net, _ in subset), ZERO)
            gross = sum((amount for _, _, amount in subset), ZERO)
            if abs(residual - expected) <= max(TOLERANCE[currency], expected * Decimal('.001')):
                matches.append(subset)
                if len(matches) > 1:
                    return None, [], ['DIVIDEND_AMBIGUOUS'], inputs
            if abs(residual - gross) <= max(TOLERANCE[currency], gross * Decimal('.001')) and len(gross_matches) < 2:
                gross_matches.append(subset)
            dividend_like |= expected / 2 <= residual <= gross * Decimal('1.25')
    if not matches and len(gross_matches) > 1:
        return None, [], ['DIVIDEND_AMBIGUOUS'], inputs
    if matches or gross_matches:
        gross_paid = not matches
        subset = (gross_matches if gross_paid else matches)[0]
        events = []
        for row, net, gross in subset:
            event = {'time': None, 'amount_native': _money(gross if gross_paid else net),
                     'kind': 'dividend', 'external': False, 'schedule_id': row['id'], 'pay_date': row.get('pay_date'),
                     **{key: row[key] for key in ('payment_start', 'payment_end', 'expected_pay_date', 'payment_basis') if key in row}}
            if gross_paid:
                event['withholding'] = 'gross'
                if currency == 'USD':
                    # _interval must prove the KRW leg before accepting this.
                    event.update(expected_tax_native=_money(gross - net), expected_tax_currency='USD')
            events.append(event)
        income = sum((_decimal(e['amount_native']) for e in events), ZERO)
        return income, events, [*advisory, *(['DIVIDEND_GROSS_MATCHED'] if gross_paid else [])], [
            *inputs, *(row['id'] for row, _, _ in subset)]
    if candidates:
        if dividend_like:
            return None, [], ['DIVIDEND_AMOUNT_UNEXPLAINED'], inputs
        # R2: a cash movement outside every subset's half-net..1.25*gross range
        # can be an inferred flow. Pending IDs remain available to later days.
        return ZERO, [], [*advisory, 'DIVIDEND_POSSIBLY_IN_FLOW'], inputs
    return ZERO, [], advisory, inputs


def _income_blocked(record):
    if record['income_native'] is None:
        return True
    advisory = INCOME_ADVISORIES
    if ('DIVIDEND_ENTITLEMENT_UNKNOWN_ZERO_RESIDUAL' in record['reasons']
            and _decimal(record['income_native']) == ZERO
            and abs(_decimal(record['diagnostics']['residual_native'])) <= TOLERANCE[record['currency']]):
        advisory = advisory | {'DIVIDEND_ENTITLEMENT_UNKNOWN'}
    return any(reason not in advisory for reason in record['reasons'])


def _split_dividend_withholding(krw, usd, s0, s1):
    """R5: gross USD income needs an actual matching negative KRW residual.

    Use either valid endpoint mark, as in the replay diagnosis. A transfer that exactly
    offsets the tax leaves no debit and cannot satisfy this leg. No amount is
    invented from the expected tax: the observed residual is the expense.
    """
    gross_events = [e for e in usd['events'] if e.get('withholding') == 'gross']
    if not gross_events:
        return
    tax = sum((_decimal(e['expected_tax_native']) for e in gross_events), ZERO)
    debit = _decimal(krw['diagnostics']['residual_native']) if not _income_blocked(krw) else ZERO
    matched = debit < 0 and tax > 0 and any(
        abs(-debit / (tax * _decimal(s['fx']['rate'])) - 1) <= FX_BAND for s in (s0, s1))
    if not matched:
        usd['income_native'] = usd['diagnostics']['matched_income_native'] = None
        usd['diagnostics']['residual_native'] = _money(
            _decimal(usd['diagnostics']['cash_delta_native']) - _decimal(usd['diagnostics']['trade_cash_effect_native']))
        usd['events'] = []
        usd['reasons'] = [r for r in usd['reasons'] if r != 'DIVIDEND_GROSS_MATCHED'] + [
            'DIVIDEND_WITHHOLDING_LEG_UNPROVEN', 'DIVIDEND_AMOUNT_UNEXPLAINED']
        return
    ids = [e['schedule_id'] for e in gross_events]
    krw['events'].append({'time': None, 'amount_native': _money(debit), 'kind': 'fee', 'external': False,
                          'fee_type': 'dividend_withholding', 'dividend_schedule_ids': ids})
    krw['income_native'] = _money(_decimal(krw['income_native']) + debit)
    krw['diagnostics'].update(matched_income_native=krw['income_native'], residual_native='0')
    for record in (krw, usd):
        record['reasons'].append('DIVIDEND_WITHHOLDING_IN_KRW')


def _record(s0, s1, account, currency):
    return {'source': 'toss', 'account': account, 'currency': currency, 't0': _iso(s0['time']),
            't1': _iso(s1['time']), 'start_snapshot_id': s0.get('snapshot_id'), 'end_snapshot_id': s1.get('snapshot_id'),
            'flow_native': None, 'income_native': None, 'events': [], 'timing': 'unknown',
            'method': 'cash_residual', 'confidence': 'unresolved', 'reasons': [],
            'inputs': {'snapshot_versions': [s0.get('version'), s1.get('version')], 'fill_ids': [],
                       'fill_versions': [], 'coverage_ids': [], 'dividend_evidence': [], 'algorithm': 'toss-residual-v2'},
            'diagnostics': {'cash_delta_native': None, 'trade_cash_effect_native': None,
                            'matched_income_native': None, 'residual_native': None, 'classification': 'unresolved'}}


def _interval(s0, s1, fills, schedule, coverage, history, used):
    account, t0, t1 = s0['account'], _time(s0['time']), _time(s1['time'])
    records = [_record(s0, s1, account, c) for c in CURRENCIES]
    quality = fills.quality if isinstance(fills, _Replay) else _snapshot_reasons
    reasons = [*quality(s0), *quality(s1)]
    covered, ids = _coverage_reasons(coverage, account, t0, t1)
    trades, errors = _fills_between(fills, account, t0, t1)
    reasons.extend([*covered, *errors])
    if not reasons and not _quantities_reconcile(s0, s1, trades):
        reasons.append('QUANTITY_UNRECONCILED')
    for record in records:
        record['reasons'] = sorted(set(reasons))
        record['inputs'].update(fill_ids=[r['event_id'] for r in trades],
            fill_versions=[r.get('version') for r in trades], coverage_ids=ids)
        c = record['currency']
        try:
            delta = _decimal(s1['cash'][c]['value']) - _decimal(s0['cash'][c]['value'])
            record['diagnostics']['cash_delta_native'] = _money(delta)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if errors:
            continue
        effect = _trade_effect(trades, c)
        record['diagnostics']['trade_cash_effect_native'] = _money(effect)
        record['diagnostics']['residual_native'] = _money(delta - effect)
        if reasons:
            continue
        if schedule is None:
            # Owner decision 10/17: dividends are not modelled. Any dividend cash
            # is part of the residual and becomes an inferred flow (or R8 income).
            income, events, income_reasons = ZERO, [], [DIVIDENDS_AS_FLOWS]
        else:
            income, events, income_reasons, evidence = _income(schedule, history, fills, coverage, account,
                                                             c, t0, t1, delta - effect, used)
            record['inputs']['dividend_evidence'] = evidence
            record['inputs']['dividend_reference_dates'] = [{k: p[k] for k in (
                'symbol', 'currency', 'observed_at', 'cache_status', 'start', 'end', 'status', 'basis') if k in p}
                for p in schedule.get('coverage', []) if p.get('currency') == c and p.get('observed_at')]
        record['reasons'].extend(income_reasons)
        record['income_native'] = _money(income)
        record['events'] = events
        record['diagnostics']['matched_income_native'] = _money(income)
        if income is not None:
            record['diagnostics']['residual_native'] = _money(delta - effect - income)
    krw, usd = records
    _split_dividend_withholding(krw, usd, s0, s1)
    # The same residuals feed the paired FX check, so estimated absence in one
    # currency cannot confer observed certainty on the other currency's zero.
    if any(ABSENCE_ESTIMATED in r['reasons'] for r in records):
        for record in records:
            if ABSENCE_ESTIMATED not in record['reasons']:
                record['reasons'].append(ABSENCE_ESTIMATED)
    if not any(_income_blocked(r) for r in records):
        k = _decimal(krw['diagnostics']['residual_native'])
        u = _decimal(usd['diagnostics']['residual_native'])
        if k * u < 0 and abs(k) > TOLERANCE['KRW'] and abs(u) > TOLERANCE['USD']:
            rate = abs(k / u)
            if all(abs(rate / _decimal(s['fx']['rate']) - 1) <= FX_BAND for s in (s0, s1)):
                pair = f"fx:{s0['snapshot_id']}:{s1['snapshot_id']}:{account}"
                for record, amount in zip(records, (k, u)):
                    record.update(flow_native='0', confidence='conditional')
                    record['events'].append({'time': None, 'amount_native': _money(amount), 'kind': 'fx_leg',
                                             'external': False, 'pair_id': pair})
                    record['diagnostics']['classification'] = 'fx_conversion'
                    record['reasons'].append('FX_CONVERSION_ESTIMATED_WITHIN_BAND')
            else:
                for record in records:
                    record['reasons'].append('FX_RATE_OUTSIDE_BAND')
    if not any(_income_blocked(r) for r in records) and usd['flow_native'] is None:
        residual = _decimal(usd['diagnostics']['residual_native'])
        # R6 bounds the mark/spread error of an inferred USD external flow.
        error_bound = abs(residual) * _decimal(s1['fx']['rate']) * FX_BAND
        usd['inputs'].update(usd_flow_fx_error_bound_krw=_money(error_bound),
                             usd_flow_fx_error_budget_krw=_money(USD_FLOW_FX_BUDGET_KRW), fx_band=_money(FX_BAND))
        if abs(residual) > TOLERANCE['USD'] and error_bound > USD_FLOW_FX_BUDGET_KRW:
            usd['reasons'].append('USD_FLOW_FX_VALUATION_UNSEPARATED')
    # A problem in one currency can hide a conversion in the other. Block both.
    if any(_income_blocked(r) for r in records):
        if any(not _income_blocked(r) for r in records):
            for record in records:
                record['reasons'].append('CROSS_CURRENCY_RESIDUAL_UNRESOLVED')
        return records
    for record in records:
        for event in record['events']:
            if event['kind'] == 'dividend':
                used.add((account, event['schedule_id']))
        if record['flow_native'] is not None:
            continue
        residual = _decimal(record['diagnostics']['residual_native'])
        value_krw = abs(residual) * (_decimal(s1['fx']['rate']) if record['currency'] == 'USD' else 1)
        if TOLERANCE[record['currency']] < abs(residual) and value_krw <= SMALL_RESIDUAL_KRW:
            # R8 is applied only after the dividend, entitlement and FX gates;
            # it cannot erase a dividend-like amount or missing evidence.
            record['income_native'] = _money(_decimal(record['income_native']) + residual)
            record['events'].append({'time': None, 'amount_native': _money(residual),
                                     'kind': 'income_other', 'external': False})
            record['reasons'].append('SMALL_RESIDUAL_TREATED_AS_INCOME')
            record['diagnostics'].update(matched_income_native=record['income_native'], residual_native='0')
            residual = ZERO
        if abs(residual) <= TOLERANCE[record['currency']]:
            record.update(flow_native='0', confidence='conditional')
            kinds = {e['kind'] for e in record['events']}
            record['diagnostics']['classification'] = ('dividend' if 'dividend' in kinds else
                'income_other' if 'income_other' in kinds else 'dividend_withholding' if 'fee' in kinds else 'zero')
            record['timing'] = 'unknown'
        else:
            record.update(flow_native=_money(residual), confidence='conditional')
            kind = 'deposit' if residual > 0 else 'withdrawal'
            record['events'].append({'time': None, 'amount_native': _money(residual), 'kind': kind, 'external': True})
            record['diagnostics']['classification'] = kind
    return records


def _mark_transfers(records, transfers):
    candidates = []
    for evidence in transfers:
        try:
            if (evidence['basis'] != 'ledger_transfer_match' or not evidence['evidence_ref'] or not evidence['id']
                    or evidence['out_account'] == evidence['in_account']):
                continue
            amount, when = _decimal(evidence['amount_native']), _time(evidence['time'])
            if amount <= 0:
                continue
            pairs = []
            for account, sign in ((evidence['out_account'], -1), (evidence['in_account'], 1)):
                found = [r for r in records if r['account'] == account and r['currency'] == evidence['currency']
                         and r['flow_native'] is not None and _time(r['t0']) < when <= _time(r['t1'])
                         and _decimal(r['flow_native']) == sign * amount and r['diagnostics']['classification'] in ('deposit', 'withdrawal')]
                if len(found) != 1:
                    break
                pairs.append(found[0])
            if len(pairs) != 2:
                continue
            candidates.append((pairs, evidence, when))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    for pairs, evidence, when in candidates:
        ambiguous = any(sum(any(r is record for r in other) for other, _, _ in candidates) != 1 for record in pairs)
        if ambiguous:
            for record in pairs:
                record.update(flow_native=None, confidence='unresolved')
                record['reasons'] = sorted(set([*record['reasons'], 'TRANSFER_EVIDENCE_AMBIGUOUS']))
                record['diagnostics']['classification'] = 'unresolved'
        else:
            for record, kind in zip(pairs, ('transfer_out', 'transfer_in')):
                event = next(e for e in record['events'] if e['external'])
                event.update(kind=kind, time=when.isoformat(), pair_id=evidence['id'], evidence_ref=evidence['evidence_ref'])
                record['diagnostics']['classification'] = kind
                record['timing'] = 'known'
                record['inputs']['transfer_evidence'] = evidence['evidence_ref']


def _transfer_inputs(reader, tables, mapped, start, end):
    """Only persisted pairs with a shared transaction reference qualify.

    Older amount/time heuristic matches do not identify Toss counterparties.
    Date-only statement transfers cannot supply an intraday timing claim.
    """
    if 'ledger_transfer_matches' not in tables:
        return []
    pairs = reader.rows(f'''SELECT m.* FROM ledger_events o INDEXED BY ledger_flow_window
        CROSS JOIN ledger_transfer_matches m ON o.event_id=m.out_event_id
        CROSS JOIN ledger_events i ON i.event_id=m.in_event_id
        WHERE o.kind IN ('deposit','withdrawal','transfer','transfer_internal','transfer_external_in','transfer_external_out')
          AND {FLOW_TIME_SQL.replace('execution_time_utc', 'o.execution_time_utc').replace('payload_json', 'o.payload_json')} >= julianday(?)
          AND {FLOW_TIME_SQL.replace('execution_time_utc', 'o.execution_time_utc').replace('payload_json', 'o.payload_json')} <= julianday(?)
          AND o.account_identity IN (SELECT value FROM json_each(?))
          AND i.account_identity IN (SELECT value FROM json_each(?))
          AND m.revision=(SELECT max(revision) FROM ledger_transfer_matches WHERE pair_id=m.pair_id)''',
        (start, end, json.dumps(sorted(mapped)), json.dumps(sorted(mapped))))
    ids = {p[key] for p in pairs for key in ('out_event_id', 'in_event_id')}
    if not ids:
        return []
    rows = reader.rows('SELECT event_id,payload_json FROM ledger_events WHERE event_id IN (SELECT value FROM json_each(?))',
                       (json.dumps(sorted(ids)),))
    effective, classifications, conflicts = _effective(reader, tables, rows)
    result = []
    for pair in pairs:
        evidence = json.loads(pair['evidence_json'])
        out, incoming = (effective[pair[k]] for k in ('out_event_id', 'in_event_id'))
        if (evidence.get('rule') != 'shared_reference' or not evidence.get('shared_reference')
                or any(pair[k] in conflicts or classifications.get(pair[k]) not in (None, 'internal')
                       for k in ('out_event_id', 'in_event_id'))):
            continue
        try:
            amount = _decimal(out['amount'])
            if (amount <= 0 or amount != _decimal(incoming['amount']) or out['currency'] != incoming['currency']
                    or _time(out['execution_time_utc']) != _time(incoming['execution_time_utc'])
                    or any(e.get('source_status') not in ('CONFIRMED', 'COMPLETED', 'SUCCESS') for e in (out, incoming))):
                continue
            result.append({'id': pair['pair_id'], 'basis': 'ledger_transfer_match',
                'evidence_ref': f"{pair['pair_id']}:{pair['revision']}", 'currency': out['currency'],
                'amount_native': _money(amount), 'time': out['execution_time_utc'],
                'out_account': mapped[out['account_identity']], 'in_account': mapped[incoming['account_identity']]})
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return result


class _Replay:
    """Per-call immutable evidence indexes, discarded after this replay."""

    def __init__(self, snapshots, fills):
        self.rows, self.index, self.invalid, self.anchors = list(fills), {}, {}, {}
        self.symbols, self.entitlements, self.reasons = {}, {}, {}
        for snapshot in snapshots:
            account, stamp = snapshot['account'], _time(snapshot['time'])
            reasons = self.reasons[id(snapshot)] = _snapshot_reasons(snapshot)
            if not reasons:
                self.anchors.setdefault(account, []).append(snapshot)
            for row in snapshot.get('holdings', []):
                self._symbol(account, row, stamp)
        for account, rows in self.anchors.items():
            rows.sort(key=lambda s: _time(s['time']))
            self.anchors[account] = ([_time(s['time']) for s in rows], rows)
        for index, row in enumerate(self.rows):
            account = row.get('account')
            try:
                stamp = _time(row['execution_time_utc'])
                self.index.setdefault(account, []).append((stamp, index))
                self._symbol(account, row, stamp)
            except (KeyError, TypeError, AttributeError, ValueError):
                self.invalid.setdefault(account, set()).add(index)
            for value in row.get('conflict_times', []):
                try:
                    self.index.setdefault(account, []).append((_time(value), index))
                except (TypeError, AttributeError, ValueError):
                    self.invalid.setdefault(account, set()).add(index)
        for account, rows in self.index.items():
            rows.sort()
            self.index[account] = ([stamp for stamp, _ in rows], [index for _, index in rows])

    def _symbol(self, account, row, stamp):
        values = self.symbols.setdefault((account, row.get('currency')), {})
        symbol = row.get('symbol')
        values[symbol] = min(stamp, values.get(symbol, stamp))

    def quality(self, snapshot):
        return self.reasons[id(snapshot)]

    def between(self, account, t0, t1):
        stamps, indexes = self.index.get(account, ([], []))
        selected = set(indexes[bisect_right(stamps, t0):bisect_right(stamps, t1)]) | self.invalid.get(account, set())
        return _fills_between([self.rows[i] for i in sorted(selected)], account, t0, t1)


def _reconcile_listing(schedule, snapshots, fills, evidence=()):
    """A firstTradeDate listing proof yields to earlier local evidence.

    A snapshot holding, fill or fill conflict time (any account), a supplied
    ex-date, or a listing_evidence instant (v14: accounts outside the requested
    scope), for that symbol on a market date before the proven listing makes its
    coverage DIVIDEND_LISTING_DATE_CONFLICT and drops its rows. Consistent
    evidence keeps the listing exception. Returns a new schedule; idempotent.
    """
    zones = {'USD': ZoneInfo('America/New_York'), 'KRW': KST}
    earliest = {}

    def seen(symbol, currency, day):
        earliest[symbol, currency] = min(day, earliest.get((symbol, currency), day))

    for snapshot in snapshots:
        for row in snapshot.get('holdings', []):
            if row.get('currency') in zones:
                seen(row.get('symbol'), row['currency'], _time(snapshot['time']).astimezone(zones[row['currency']]).date())
    for row in fills:
        for value in [row.get('execution_time_utc'), *row.get('conflict_times', [])]:
            try:
                seen(row.get('symbol'), row['currency'], _time(value).astimezone(zones[row['currency']]).date())
            except (KeyError, TypeError, AttributeError, ValueError):
                continue  # _Replay already marks such fills invalid for every interval.
    for row in evidence:
        if row.get('currency') in zones:
            seen(row.get('symbol'), row['currency'], _time(row['time']).astimezone(zones[row['currency']]).date())
    for row in schedule.get('rows', []):
        try:
            seen(row.get('symbol'), row.get('currency'), date.fromisoformat(row['ex_date']))
        except (KeyError, TypeError, ValueError):
            continue  # _income rejects such rows.
    voided, coverage = set(), []
    for row in schedule.get('coverage', []):
        key = (row.get('symbol'), row.get('currency'))
        if estimated_coverage(row) and row.get('listing_basis') == LISTING_BASIS and key in earliest:
            try:
                conflict = earliest[key] < date.fromisoformat(row['price_history_start'])
            except (KeyError, TypeError, ValueError):
                conflict = True
            if conflict:
                voided.add(key)
                row = {**{k: row[k] for k in ('symbol', 'currency', 'start', 'end', 'reference_symbol',
                                              'price_history_start', 'listing_basis') if k in row},
                       'status': 'limited', 'reasons': ['DIVIDEND_LISTING_DATE_CONFLICT', 'PUBLIC_DIVIDEND_COVERAGE_LIMITED'],
                       'local_evidence_start': earliest[key].isoformat()}
        coverage.append(row)
    rows = [row for row in schedule.get('rows', []) if (row.get('symbol'), row.get('currency')) not in voided]
    return {**schedule, 'rows': rows, 'coverage': coverage}


def infer_toss_flows(snapshots, fills, schedule, *, coverage=(), start=None, end=None, accounts=None, transfers=(),
                     listing_evidence=()):
    """Pure algorithm described in the module docstring; never mutates inputs."""
    lo, hi = _bound(start), _bound(end, end=True)
    if lo > hi:
        raise ValueError('INVALID_INTERVAL_RANGE')
    if schedule is not None:
        schedule = _reconcile_listing(schedule, snapshots, fills, listing_evidence)
    selected = set(accounts) if accounts is not None else {s['account'] for s in snapshots}
    records, used = [], set()
    replay = _Replay(snapshots, fills)
    with localcontext() as ctx:
        ctx.prec = 80
        for account in sorted(selected):
            ordered = sorted((s for s in snapshots if s.get('account') == account and _time(s['time']) <= hi),
                             key=lambda s: _time(s['time']))
            if len({_time(s['time']) for s in ordered}) != len(ordered):
                raise ValueError('AMBIGUOUS_SNAPSHOT_VERSIONS')
            anchor = None
            pairs = []
            for snapshot in ordered:
                if replay.quality(snapshot):
                    continue
                if anchor is not None:
                    pairs.append((anchor, snapshot))
                anchor = snapshot
            if ordered and anchor is not ordered[-1]:
                opening = anchor or (ordered[-2] if len(ordered) >= 2 else None)
                if opening:
                    pairs.append((opening, ordered[-1]))
            elif not pairs and len(ordered) >= 2:
                pairs.append((ordered[-2], ordered[-1]))
            for s0, s1 in pairs:
                interval = _interval(s0, s1, replay, schedule, coverage, ordered, used)
                if lo < _time(s1['time']) <= hi:
                    records.extend(interval)
        _mark_transfers(records, transfers)
    return records


def _account_snapshot(nav, account):
    row = (nav.get('account_nav') or {}).get(account) or {}
    reasons = []
    for metric in (row.get('nav_native') or {}).values():
        if metric.get('value') is None or metric.get('reason_codes'):
            reasons.append('INCOMPLETE_TOSS_SNAPSHOT')
    if set(row.get('nav_native') or {}) != set(CURRENCIES):
        reasons.append('INCOMPLETE_TOSS_SNAPSHOT')
    for mark in (nav.get('mark_quality') or {}).get('quotes', []):
        if mark.get('account') == account and mark.get('status') != 'current':
            reasons.extend(mark.get('reason_codes') or ['MARK_FRESHNESS_UNVERIFIED'])
    if not nav.get('mark_quality'):
        reasons.append('MARK_FRESHNESS_UNVERIFIED')
    fx = (nav.get('fx_times') or {}).get('USD') or {}
    reasons.extend(fx_mark_quality(fx, nav.get('valuation_at'))['reason_codes'])
    cash = {c: {'value': (row.get('cash', {}).get(c) or {}).get('value_decimal'),
                'basis': (row.get('cash', {}).get(c) or {}).get('basis_reason'),
                'observed_at': (row.get('cash', {}).get(c) or {}).get('observed_at')} for c in CURRENCIES}
    holdings = [{'symbol': c.get('symbol'), 'currency': c.get('currency'), 'name': c.get('name'), 'quantity': c.get('quantity_decimal'),
                 'price': c.get('valuation_price_decimal'), 'priced_at': c.get('priced_at')}
                for c in nav.get('components') or [] if c.get('account') == account and c.get('component_type') == 'holding']
    return {'account': account, 'snapshot_id': nav.get('snapshot_id'), 'version': nav.get('version'),
            'time': nav.get('valuation_at'), 'complete': row.get('source_status') == 'complete', 'reasons': reasons,
            'cash': cash, 'holdings': holdings, 'open_order_count': (row.get('cash_basis_evidence') or {}).get('open_order_count'),
            'instrument_bindings': row.get('instrument_bindings') or {},
            'instrument_binding_reasons': row.get('instrument_binding_reasons') or [],
            'fx': {'rate': fx.get('rate_decimal') or (str(fx['rate_to_krw']) if fx.get('rate_to_krw') is not None else None),
                   'valid_from': fx.get('priced_at'), 'valid_until': fx.get('valid_until'),
                   'observed_at': fx.get('read_at', nav.get('valuation_at'))}}


def _bounded_rows(reader, sql, params=()):
    """Stream a bounded indexed range; batch limits do not truncate evidence."""
    cursor = reader.conn.execute(sql, params)
    rows = []
    while True:
        reader.check()
        page = cursor.fetchmany(1000)
        if not page:
            return rows
        rows.extend(page)
        if len(rows) > 50000:
            raise ReadBudgetExceeded


def _listing_evidence(reader, tables, ceiling, end):
    """Earliest holding/fill instant per symbol in ANY Toss account.

    Selected accounts and the requested window do not bound ownership history:
    a secondary-account holding before a Yahoo listing date still contradicts it.
    One aggregate over the indexed nav-date and fill-time ranges up to ``end``.
    """
    parts = ["""SELECT json_extract(c.value,'$.symbol') symbol, json_extract(c.value,'$.currency') currency,
            min(n.day) day FROM n CROSS JOIN json_each(n.payload,'$.components') c
        WHERE json_extract(c.value,'$.component_type')='holding'
        AND json_extract(c.value,'$.account') GLOB 'toss:*' GROUP BY 1, 2""",
             f"""SELECT json_extract(payload_json,'$.symbol'), currency, min({FLOW_TIME_SQL})
        FROM ledger_events INDEXED BY ledger_flow_window
        WHERE kind='fill' AND {FLOW_TIME_SQL} <= julianday(?) AND source='toss_transactions' GROUP BY 1, 2"""]
    params = [ceiling, end]
    for table, index, expression in (
            ('ledger_conflicts', 'ledger_conflict_window', CONFLICT_TIME_SQL),
            ('ledger_enrichments', 'ledger_enrichment_window', ENRICHMENT_TIME_SQL)):
        if table in tables:
            parts.append(f"""SELECT json_extract(e.payload_json,'$.symbol'), e.currency, min(changed.day) FROM (
                SELECT event_id, min({expression}) day FROM {table} INDEXED BY {index}
                WHERE {expression} <= julianday(?) GROUP BY event_id) changed
                CROSS JOIN ledger_events e ON e.event_id=changed.event_id
                WHERE e.source='toss_transactions' AND e.kind='fill' GROUP BY 1, 2""")
            params.append(end)
    # MATERIALIZED (SQLite >= 3.35) reads each payload's time once, not per component.
    sql = ("""WITH n AS MATERIALIZED (SELECT v.payload_json payload,
            julianday(json_extract(v.payload_json,'$.valuation_at')) day
            FROM nav_snapshot_representatives r CROSS JOIN nav_snapshot_versions v ON v.snapshot_id=r.snapshot_id
            WHERE r.snapshot_date <= ?)
        SELECT symbol, currency, strftime('%Y-%m-%dT%H:%M:%SZ', min(day)) FROM ("""
           + ' UNION ALL '.join(parts) + ') WHERE day IS NOT NULL GROUP BY 1, 2')
    return [{'symbol': symbol, 'currency': currency, 'time': when} for symbol, currency, when in reader.rows(sql, params)]


def _read_inputs(db_path, start, end, accounts):
    lo, hi = _bound(start), _bound(end, end=True)
    if lo > hi or lo.year < 2000 or hi.year > 2100:
        raise ValueError('INVALID_INTERVAL_RANGE')
    # Warm up payment consumption for max-lag windows, plus an equally bounded
    # entitlement lookback. The opening complete/no-OPEN-order snapshot bounds
    # fills even when an order was created before the requested query window.
    floor = (lo - timedelta(days=2 * MAX_PAYMENT_LAG_DAYS + 6)).astimezone(KST).date().isoformat()
    ceiling = hi.astimezone(KST).date().isoformat()
    deadline = time.monotonic() + 2
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        reader = Reader(conn, deadline)
        tables = {r[0] for r in reader.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        nav_sql = """SELECT r.snapshot_date,v.payload_json FROM nav_snapshot_representatives r
                     JOIN nav_snapshot_versions v USING(snapshot_id) """
        rows = reader.rows(nav_sql + 'WHERE r.snapshot_date >= ? AND r.snapshot_date <= ? ORDER BY r.snapshot_date',
                           (floor, ceiling))
        navs = [json.loads(r['payload_json']) for r in rows]
        prior = reader.rows(nav_sql + 'WHERE r.snapshot_date < ? ORDER BY r.snapshot_date DESC LIMIT 1', (floor,))
        if prior:
            navs.insert(0, json.loads(prior[0]['payload_json']))
        names = sorted(set(accounts) if accounts is not None else
                       {a for nav in navs for a in nav.get('account_nav') or {} if a.startswith('toss:')})
        # If the immediate predecessor has open orders, seek the earlier complete
        # anchor by the representative date index; retain only needed anchors.
        missing = {a for a in names if prior and _snapshot_reasons(_account_snapshot(navs[0], a))}
        cursor_date = prior[0]['snapshot_date'] if prior else floor
        while missing:
            previous = reader.rows(nav_sql + 'WHERE r.snapshot_date < ? ORDER BY r.snapshot_date DESC LIMIT 16', (cursor_date,))
            if not previous:
                break
            for row in previous:
                nav = json.loads(row['payload_json'])
                found = {a for a in missing if not _snapshot_reasons(_account_snapshot(nav, a))}
                if found:
                    navs.insert(0, nav)
                    missing -= found
                cursor_date = row['snapshot_date']
                if not missing:
                    break
        snapshots = [_account_snapshot(nav, account) for nav in navs for account in names
                     if account in (nav.get('account_nav') or {}) and _time(nav['valuation_at']) <= hi]
        if not snapshots:
            return [], [], [], [], []
        mapped = {}
        for account in names:
            scopes, _, aliases, errors = _roster(reader, tables, [{'account_nav': {account: {}}}], 'toss', ('toss',))
            if errors:
                for snapshot in snapshots:
                    if snapshot['account'] == account:
                        snapshot['reasons'].extend(errors)
                continue
            for identity in scopes.get('toss', set()):
                mapped[identity] = account
                for alias, canonical in aliases.items():
                    if canonical == identity:
                        mapped[alias] = account
        keys = json.dumps(sorted(mapped))
        read_start = min(_time(s['time']) for s in snapshots).isoformat()
        read_end = hi.isoformat()
        base_sql = """SELECT event_id,payload_json,content_sha256 FROM ledger_events
            INDEXED BY ledger_flow_window WHERE kind='fill' AND source='toss_transactions'
            AND account_identity IN (SELECT value FROM json_each(?)) """
        raw = _bounded_rows(reader, base_sql + f'AND {FLOW_TIME_SQL} >= julianday(?) AND {FLOW_TIME_SQL} <= julianday(?)',
                            (keys, read_start, read_end))
        # Invalid/date-less events cannot silently disappear through a range
        # predicate. The same time index supports this explicit NULL bucket.
        raw += _bounded_rows(reader, base_sql + f'AND {FLOW_TIME_SQL} IS NULL', (keys,))
        # Old cumulative aggregates can be moved into the window by a conflict
        # or enrichment. Read the indexed revision window before dereferencing IDs.
        for table, index, expression in (
                ('ledger_conflicts', 'ledger_conflict_window', CONFLICT_TIME_SQL),
                ('ledger_enrichments', 'ledger_enrichment_window', ENRICHMENT_TIME_SQL)):
            if table in tables:
                unknown = f' OR {expression} IS NULL' if table == 'ledger_conflicts' else ''
                sql = f"""SELECT e.event_id,e.payload_json,e.content_sha256 FROM (
                    SELECT DISTINCT event_id FROM {table} INDEXED BY {index}
                    WHERE ({expression} >= julianday(?) AND {expression} <= julianday(?)){unknown}) changed
                    CROSS JOIN ledger_events e ON e.event_id=changed.event_id
                    WHERE e.source='toss_transactions' AND e.kind='fill'
                    AND e.account_identity IN (SELECT value FROM json_each(?))"""
                raw += _bounded_rows(reader, sql, (read_start, read_end, keys))
        raw = list({r['event_id']: r for r in raw}.values())
        effective, conflicts = {}, {}
        for index in range(0, len(raw), 500):
            batch, _, errors = _effective(reader, tables, raw[index:index + 500])
            effective.update(batch)
            conflicts.update(errors)
        fills = []
        for event_id, row in effective.items():
            fills.append({**row, 'event_id': event_id, 'account': mapped[row['account_identity']],
                'version': hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
                'conflict_times': [c.get('execution_time_utc') for c in conflicts.get(event_id, [])],
                'reasons': ['LEDGER_CONTENT_CONFLICT'] if event_id in conflicts else []})
        coverage = []
        if 'ledger_coverage' in tables:
            sql = f"""SELECT * FROM ledger_coverage INDEXED BY ledger_flow_coverage_window
                WHERE source='toss' AND {coverage_time_sql('end_time')} >= julianday(?)
                AND {coverage_time_sql('start_time')} <= julianday(?)
                AND account_identity IN (SELECT value FROM json_each(?)) AND stream LIKE 'toss/fills-v1/%'"""
            for row in _bounded_rows(reader, sql, (read_start, read_end, keys)):
                coverage.append({'account': mapped[row['account_identity']], 'start': row['start_time'],
                    'end': row['end_time'], 'status': row['status'], 'contract': 'toss-fills-v1',
                    'coverage_id': row['coverage_id'], 'observed_at': row['observed_at'],
                    'reasons': json.loads(row['reason_codes_json'])})
        return (snapshots, fills, coverage, _transfer_inputs(reader, tables, mapped, read_start, read_end),
                _listing_evidence(reader, tables, ceiling, read_end))


def dividend_schedule_provider(snapshots, start, end, *, fills=()):
    """Default public-reference seam; mock/inject for offline replay and tests.

    Daily disk/memory cache reads do not wait for network I/O. KR bindings use
    captured Toss instrument evidence or independently validated dual candidates.
    """
    from yfinance_api import YFinanceClient

    symbols = {(row['symbol'], row['currency']) for s in snapshots for row in s.get('holdings', [])}
    symbols |= {(row['symbol'], row['currency']) for row in fills}
    instruments, conflicts = {}, set()
    for snapshot in sorted(snapshots, key=lambda s: _time(s['time'])):
        for symbol, row in snapshot.get('instrument_bindings', {}).items():
            previous = instruments.get(symbol) or {}
            if previous.get('market') and any(previous.get(k) != row.get(k) for k in ('symbol', 'market', 'isinCode', 'currency')):
                conflicts.add(symbol)
            instruments[symbol] = row
        for holding in snapshot.get('holdings', []):
            if holding.get('currency') == 'KRW':
                if any(r.startswith('INVALID_') for r in snapshot.get('instrument_binding_reasons', [])):
                    conflicts.add(holding['symbol'])
                if holding.get('name') and holding['symbol'] not in instruments:
                    instruments[holding['symbol']] = {'name': holding['name']}
    start = min([_bound(start), *(_time(s['time']) for s in snapshots)]).isoformat()
    schedule = {'rows': [], 'coverage': []}
    with YFinanceClient() as client:
        for symbol, currency in sorted(symbols):
            if symbol in conflicts:
                data = {'rows': [], 'coverage': [{'symbol': symbol, 'currency': currency, 'start': start, 'end': end,
                        'status': 'limited', 'reasons': ['DIVIDEND_INSTRUMENT_CONFLICT']}]}
            else:
                data = client.get_dividend_schedule(symbol, currency=currency, start=start, end=end, background=True,
                                                   instrument=instruments.get(symbol))
            schedule['rows'].extend(data.get('rows', []))
            schedule['coverage'].extend(data.get('coverage', []))
    return _reconcile_listing(schedule, snapshots, fills)


def _unavailable(accounts, reason):
    return [{'source': 'toss', 'account': a, 'currency': c, 't0': None, 't1': None,
             'start_snapshot_id': None, 'end_snapshot_id': None, 'flow_native': None, 'income_native': None,
             'events': [], 'timing': 'unknown', 'method': 'cash_residual', 'confidence': 'unresolved',
             'reasons': [reason], 'inputs': {}, 'diagnostics': {'classification': 'unresolved',
             'cash_delta_native': None, 'trade_cash_effect_native': None, 'matched_income_native': None, 'residual_native': None}}
            for a in (accounts or ['toss:unknown']) for c in CURRENCIES]


def toss_interval_flows(db_path, start, end, accounts=None, *, schedule_provider=None):
    """Read immutable local NAV/fill evidence; optional public schedule seam.

    Missing DB/tables/legacy provenance produces unresolved records. Actual
    snapshot endpoints are never replaced by requested midnight boundaries.
    Without a schedule_provider (the tool path) dividends are not modelled and
    no public schedule is fetched; pass ``dividend_schedule_provider`` to match them.
    """
    try:
        snapshots, fills, coverage, transfers, listing_evidence = _read_inputs(db_path, start, end, accounts)
    except (sqlite3.Error, ReadBudgetExceeded, OSError, KeyError, TypeError, ValueError):
        return _unavailable(accounts, 'LOCAL_TOSS_EVIDENCE_UNAVAILABLE')
    if not snapshots:
        return _unavailable(accounts, 'TOSS_SNAPSHOTS_UNAVAILABLE')
    schedule = None
    if schedule_provider:
        try:
            schedule = schedule_provider(snapshots, start, end)
        except Exception:
            schedule = {'rows': [], 'coverage': []}
    try:
        return infer_toss_flows(snapshots, fills, schedule, coverage=coverage, start=start, end=end,
                               accounts=accounts, transfers=transfers, listing_evidence=listing_evidence)
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
        return _unavailable(accounts, 'INVALID_TOSS_INFERENCE_INPUT')


def toss_residual_diagnostics(db_path, start, end, *, schedule_provider=None):
    """Per-currency diagnostics and counts of ACCOUNT intervals (not row counts)."""
    records = toss_interval_flows(db_path, start, end, schedule_provider=schedule_provider)
    groups = {}
    rows = []
    for record in records:
        key = (record['account'], record['t0'], record['t1'])
        if record['t0'] is not None and record['t1'] is not None:
            groups[key] = groups.get(key, True) and record['confidence'] != 'unresolved'
        rows.append({k: record[k] for k in ('account', 'currency', 't0', 't1', 'reasons', 'inputs')} | record['diagnostics'])
    return {'rows': rows, 'reasons': sorted({reason for r in records for reason in r['reasons']}) if records else ['NO_TOSS_INTERVALS'],
            'counts': {'intervals_total': len(groups), 'intervals_classified': sum(groups.values()),
                                    'intervals_unresolved': len(groups) - sum(groups.values())}}
