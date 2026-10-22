"""Ledgerless interval returns: linked Modified Dietz TWR *estimates*.

Per scope, consecutive complete scope NAV snapshots (``ledger_returns.scope_navs``)
form intervals ``(t0, t1]`` on their actual valuation instants, never midnight
buckets. Per-interval flow records (shared flow-record shape, produced on the fly by
``toss_flow_inference.toss_interval_flows``,
``crypto_flow_intervals.crypto_interval_flows`` and
``kis_flow_inference.kis_interval_flows``) supply external flows:

    r = (N1 - N0 - sum F) / (N0 + sum w F),   w = (t1 - tf) / (t1 - t0)

Each gross external event is one term: a known time uses its weight; an unknown
time uses the midpoint of the possible weights as the estimate and start/end
weights as sensitivity bounds. If the denominator can reach zero or below
within those bounds the interval is null; so an unknown-time deposit and
withdrawal null it only then (NAV 1000, +/-1000), else (NAV 10000) they give a
conditional estimate with bounds. Intervals link as ``prod(1 + r) - 1``;
linked bounds multiply factor ranges sign-aware. Records bind to NAV endpoints
by exact immutable snapshot ID, valuation instant and declared version, never
by time alone, and must tile each interval contiguously in time. This is a
TWR *estimate* (``modified_dietz_linked_twr_estimate``); exact TWR needs a
valuation at every flow instant. ``portfolio_engine.time_weighted_return`` (the
end-flow arithmetic) is untouched and never relabelled.

Unknown is never zero: an unresolved/invalid/missing flow record, an
incomplete endpoint or a non-positive Dietz denominator makes that interval and
every period containing it null with reason codes. No return is given over a
subset of days: such a period shows its per-interval estimates and day coverage
(resolved/total days, unresolved days with reasons) instead. Separately,
``tracked`` links the unbroken run of resolved intervals that ends at the latest
resolved one, as its own dated window (null with ``NO_RESOLVED_INTERVAL``); an
unresolved interval ends the run, so it never skips a day. Multi-day gaps bridge
only complete endpoints whose flow records tile the whole bridged interval.

Income is performance, never flow: internal ``dividend``/``income_other`` events
and ``income_native`` (e.g. a small residual booked as income, ``conditional``)
stay in the gain; their record reasons are carried on the interval.

KRW conversion: NAV at valuation FX (scope NAV); flows at event FX when the
record carries it (optional additive keys ``events[].amount_krw`` or
``fx_to_krw`` on the event or record). Without event FX a non-KRW flow is
converted at the mean of the two endpoint marks and the mark range widens the
sensitivity bounds (``FLOW_FX_FROM_SNAPSHOT_MARKS``); such flows never count
as complete coverage. A crypto estimate may carry ``events[].amount_krw_range``
(stablecoin FX interpolated between capture marks, an unproven fee basis): it
widens the bounds the same way. Scope percentages are never summed.

Labels: ``verified`` only when the scope passed the frozen backtest bar in
``ACCURACY`` and every flow in the period is observed with known time and
event FX; otherwise a computable number is an ``estimate``; else
``unavailable`` with reasons; each interval and ``tracked`` carry the same label
rule. XIRR only when every flow is dated and observed.
No database writes or migrations, and no network I/O in the engine. Toss flows
do not model dividends (a design decision): no public dividend schedule
is fetched, a dividend counts as an inferred deposit (or, at most 5,000 KRW, as
income) and every computed Toss interval carries ``DIVIDENDS_COUNTED_AS_FLOWS``;
Toss results stay ``conditional``, so never ``verified``. KIS (2.13.0) works the
same way from the 18:10 KIS capture alone (KIS publishes no transaction history the
owner can obtain): every KIS flow is inferred, dividends are not modelled, the
label is always ``estimate`` (ACCURACY['kis'] forward_pending, never passes the
bar here) and days before the first capture stay null (KIS_CAPTURE_MISSING), never
back-filled. Providers run on a bounded helper thread.

Consolidated (2.13.1) sums the KRW levels of the included scopes toss (every Toss
account), kis and crypto, one Modified Dietz per interval on the sums:

    r = (sum N1 - sum N0 - sum F) / (sum N0 + sum w F)

Each scope's NAV and flows stay in KRW at the marks that scope already uses, each
flow weighted within its own scope's interval; scope percentages are never averaged.
Intervals run between consecutive dates on which any included scope has an interval
boundary. One has a value only when every included scope has resolved levels and
flows over exactly the same capture dates; otherwise it is null with
CONSOLIDATED_SCOPE_MISSING and ``missing_scopes`` (each scope's own reasons). A scope
is excluded (``excluded_scopes``) only when genuinely empty: configured absent with no
stored row funded (a non-zero NAV, holding or balance, complete or not), or a complete
zero NAV at every point; a failed read never excludes it. Money moved between scopes is not matched: it nets only when both
legs land in the same interval. The label is the weakest part's;
ACCURACY['consolidated'] is a composite of its components, never measured as a whole.

Equity (the tools' default scope) is the same composite over toss and kis only
(``COMPOSITE_SCOPES``): one Modified Dietz on the summed Toss + KIS KRW levels, null whenever
either part is missing on the same capture dates (CONSOLIDATED_SCOPE_MISSING), label the weakest
part's (KIS: always estimate).

``effective_period`` is the requested period clipped to the stored range it can be measured on:
from the first to the last complete snapshot inside the period (``clipped`` and
EFFECTIVE_START_AFTER_PERIOD_START / EFFECTIVE_END_BEFORE_PERIOD_END say which end moved). Its
return links every interval inside that range by the period's own rules (any unresolved interval
makes it null); it is never a return over a subset of that range's days. ``period`` is unchanged.
"""
from __future__ import annotations

import contextvars
import importlib
import inspect
import itertools
import json
import re
import threading
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

METHOD = 'modified_dietz_linked_twr_estimate'
SUPPORTED_SCOPES = ('toss', 'toss_investmcp', 'crypto', 'kis', 'consolidated', 'equity')
UNSUPPORTED_SCOPES = ()
CONSOLIDATED_SCOPES = ('toss', 'kis', 'crypto')
# Scopes built by consolidate() from their parts' levels and flows.
COMPOSITE_SCOPES = {'consolidated': CONSOLIDATED_SCOPES, 'equity': ('toss', 'kis')}
KST = ZoneInfo('Asia/Seoul')
SOURCES = {'toss', 'binance', 'hyperliquid', 'kis'}
EVENT_KINDS = {'deposit', 'withdrawal', 'transfer_in', 'transfer_out', 'dividend', 'income_other', 'fee', 'fx_leg',
               'unknown'}
FLOW_EVENT_KINDS = {'deposit', 'withdrawal', 'transfer_in', 'transfer_out'}
TIMINGS = {'known', 'unknown'}
METHODS = {'api_direct', 'cash_residual', 'statement'}
CONFIDENCES = ('observed', 'conditional', 'unresolved')
REQUIRED_KEYS = ('source', 'account', 'currency', 't0', 't1', 'start_snapshot_id', 'end_snapshot_id',
                 'flow_native', 'income_native', 'events', 'timing', 'method', 'confidence', 'reasons', 'inputs')
MAX_UNCERTAIN_DIMENSIONS = 12
REASON_CODE = re.compile(r'^[A-Z0-9_]{1,64}$')
SUMMARY_INTERVALS = 31
PROVIDERS = {'toss': ('toss_flow_inference', 'toss_interval_flows'),
             'crypto': ('crypto_flow_intervals', 'crypto_interval_flows'),
             'kis': ('kis_flow_inference', 'kis_interval_flows')}
# Own bounded budget: the tool waits at most this long, one inference per provider in
# flight. Inference re-reads all stored history (~10 s CPU at ~300 snapshots).
FLOW_BUDGET_SECONDS = 5.0
_INFLIGHT = {'toss': threading.Lock(), 'crypto': threading.Lock(), 'kis': threading.Lock()}
# Promotion bar from the design: p95 daily error <= 1 bp, |monthly drift| <= 5 bp.
BAR = {'p95_daily_error_bp': 1.0, 'max_abs_monthly_drift_bp': 5.0}
# Measured frozen-replay accuracy (an offline backtest, summary only; no raw data). Toss
# errors are point estimates under stated assumptions (statement-simulated cash, date-only statement
# rows at 12:00 KST, a constant bond level), never error bounds; an interval is excluded only
# when its cash delta or quantity reconciliation depends on an uncertain replay input.
# Reruns at other sampled placements and constant levels are sensitivity, not a bound.
# Monthly drift covers only months whose every interval was scored and resolved (the tool
# gives no return over a subset of days); the resolved-subset drift is a diagnostic. Below
# the bar (scripts: _passes) the scope ships as an estimate with these measured errors.
ACCURACY = {
    'toss_investmcp': {'status': 'measured', 'passed_bar': False, 'basis': 'statement-simulated',
                       'error_basis': 'point_estimate',
                       'assumptions': ['statement-simulated cash', 'date-only statement rows at 12:00 KST',
                                       'constant bond level'],
                       'window': '2025-10-05..2026-10-04', 'intervals_total': 255, 'intervals_eligible': 249,
                       'intervals_resolved': 231, 'resolution_rate': 0.9277, 'intervals_excluded': 6,
                       'exclusions_by_reason': {'REPLAY_STATEMENT_CASH_UNKNOWN': 6, 'REPLAY_TRUTH_UNKNOWN': 6,
                                                'REPLAY_BOND_POSITION_UNPARSED': 1},
                       'p95_daily_error_bp': 1.528, 'max_abs_monthly_drift_bp': 27.292,
                       'monthly_drift_basis': 'months_with_every_interval_resolved', 'months_scored': 4, 'months': 13,
                       'max_abs_monthly_drift_bp_resolved_subset': 34.572,
                       'holdout': {'start': '2026-09-05', 'intervals_total': 21, 'intervals_eligible': 21,
                                   'intervals_resolved': 18, 'resolution_rate': 0.8571, 'p95_daily_error_bp': 1.528,
                                   'max_abs_monthly_drift_bp': 1.696, 'months_scored': 1, 'months': 2,
                                   'max_abs_monthly_drift_bp_resolved_subset': 1.696},
                       # 2.12.10 shipped method: dividends not modelled (schedule=None), so a paid dividend
                       # counts as an inferred deposit. The dividend matcher measured 1.157 bp / 10.568 bp.
                       'dividends': 'not_modelled',
                       'sensitivity_not_bound': {
                           'statement_row_placement': {
                               '09:00 KST': {'p95_daily_error_bp': 1.528, 'max_abs_monthly_drift_bp': 27.526},
                               '15:30 KST': {'p95_daily_error_bp': 1.856, 'max_abs_monthly_drift_bp': 27.032}},
                           'nav_level': {'p95_daily_error_bp': 1.529, 'max_abs_monthly_drift_bp': 27.324,
                                         'sampled': 'constant KRW level over each interval level range, gains held fixed'}},
                       'inference': 'toss_flow_inference.infer_toss_flows (2.12.10, schedule=None)',
                       'reason_codes': ['BACKTEST_BELOW_BAR', 'BACKTEST_EVALUATION_INCOMPLETE',
                                        'STATEMENT_SIMULATED_OBSERVATIONS', 'STATEMENT_ROW_TIME_ASSUMED',
                                        'BACKTEST_ERROR_BOUND_UNAVAILABLE']},
    'toss': {'status': 'unmeasured', 'passed_bar': False,
             'reason_codes': ['TOSS_PRIMARY_HAS_NO_STATEMENT_TRUTH']},
    'crypto': {'status': 'unmeasured', 'passed_bar': False, 'window': '2025-11-01..2026-10-04',
               'days_eligible': 108, 'days_total': 338,
               'binance_match_basis': 'amount_asset_only', 'binance_match_method': 'maximum_assignment',
               'reason_codes': ['CRYPTO_NAV_DENOMINATOR_UNAVAILABLE_IN_FROZEN_COPY', 'BINANCE_OWNER_ROWS_UNTIMED',
                                'BINANCE_TRANSFER_ROUTE_UNVERIFIED', 'BINANCE_MATCH_ATTRIBUTION_AMBIGUOUS',
                                'HYPERLIQUID_NO_OWNER_STATEMENT_TRUTH']},
    # No backtest is possible (no KIS statement or flow history exists); accuracy is measured
    # forward against KIS's own reported P&L, never here.
    'kis': {'status': 'forward_pending', 'passed_bar': False,
            'basis': 'forward live reconciliation vs KIS-reported evaluation and P&L',
            'series_start': 'first 18:10 KIS capture (kis_ledgerless_v1, 2.13.0)', 'dividends': 'not_modelled',
            'inference': 'kis_flow_inference.infer_kis_flows (kis-residual-v1)',
            'reason_codes': ['KIS_FORWARD_ACCURACY_PENDING', 'KIS_NO_FLOW_TRUTH', 'KIS_NO_BACKTEST_HISTORY']},
}


def _composite_accuracy(accuracy, scopes=CONSOLIDATED_SCOPES):
    """ACCURACY['consolidated']: derived from its components' entries; no error figures of its own."""
    components = {scope: {'status': (accuracy.get(scope) or {}).get('status', 'unmeasured'),
                          'passed_bar': bool((accuracy.get(scope) or {}).get('passed_bar')),
                          'reason_codes': list((accuracy.get(scope) or {}).get('reason_codes') or [])}
                  for scope in scopes}
    return {'status': 'composite', 'passed_bar': False,
            'basis': 'KRW levels of the included scopes; never backtested or measured as a whole',
            'label_rule': 'weakest component label', 'components': components,
            'reason_codes': ['CONSOLIDATED_COMPOSITE_UNMEASURED']
            + (['CONSOLIDATED_COMPONENT_BELOW_BAR'] if not all(c['passed_bar'] for c in components.values()) else [])}


ACCURACY['consolidated'] = _composite_accuracy(ACCURACY)
ACCURACY['equity'] = _composite_accuracy(ACCURACY, COMPOSITE_SCOPES['equity'])


class InvalidRecord(ValueError):
    pass


def _decimal(value, *, nullable=False):
    """Exact money only: str/int/Decimal; floats/bools are type drift."""
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise InvalidRecord('FLOW_RECORD_NUMBER_TYPE')
    try:
        result = Decimal(value.strip() if isinstance(value, str) else value)
    except (InvalidOperation, ValueError):
        raise InvalidRecord('FLOW_RECORD_NUMBER_INVALID') from None
    if not result.is_finite():
        raise InvalidRecord('FLOW_RECORD_NUMBER_INVALID')
    return result


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def _float(value, digits):
    return None if value is None else round(float(value), digits)


def _pct(value):
    return None if value is None else round(float(value * 100), 4)


def _record_versions(record):
    """Snapshot versions the provider read: ``snapshot_versions`` or ``*_snapshot_version``."""
    inputs = record['inputs']
    pair = inputs.get('snapshot_versions')
    pair = pair if isinstance(pair, list) and len(pair) == 2 else [None, None]
    return inputs.get('start_snapshot_version', pair[0]), inputs.get('end_snapshot_version', pair[1])


def _event_rate(event):
    """Event FX: ``fx_to_krw`` and/or the snapshot mark ``fx.rate_to_krw``; both present must agree."""
    rate = _decimal(event.get('fx_to_krw'), nullable=True)
    mark = event.get('fx')
    if mark is not None:
        value = mark.get('rate_to_krw') if isinstance(mark, dict) else None
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise InvalidRecord('FLOW_RECORD_FX_INVALID')
        marked = _decimal(str(value))  # Snapshot marks convert JSON floats the same way (daily_nav.number)
        if rate is not None and marked != rate:
            raise InvalidRecord('FLOW_EVENT_FX_INCONSISTENT')
        rate = marked
    if rate is not None and rate <= 0:
        raise InvalidRecord('FLOW_RECORD_FX_INVALID')
    return rate


def _check_events(flow, flow_krw, events, external):
    """Resolved records: every event and the aggregate agree, whatever the timing.

    Every explicit KRW amount must equal native x effective FX (event FX, else
    record FX; KRW is exactly 1, other currencies within 1 KRW); contradictory
    facts make the record invalid, never arithmetic.
    """
    for e in events:
        if e['krw'] is None or e['amount'] is None:
            continue
        if (e['krw'] > 0) != (e['amount'] > 0) or (e['krw'] == 0) != (e['amount'] == 0):
            raise InvalidRecord('FLOW_EVENT_SIGN_INCONSISTENT')
        if e['rate'] is not None and abs(e['amount'] * e['rate'] - e['krw']) > (0 if e['rate_exact'] else 1):
            raise InvalidRecord('FLOW_EVENT_FX_INCONSISTENT')
    for e in external:
        if e['kind'] not in FLOW_EVENT_KINDS:  # unknown, or a performance kind marked external
            raise InvalidRecord('FLOW_EVENT_UNCLASSIFIED_EXTERNAL')
        inflow = e['kind'] in ('deposit', 'transfer_in')
        if e['amount'] == 0 or (e['amount'] > 0) != inflow:
            raise InvalidRecord('FLOW_EVENT_SIGN_INCONSISTENT')
    if any(not e['external'] and e['kind'] in FLOW_EVENT_KINDS and e['amount'] != 0 for e in events):
        raise InvalidRecord('FLOW_EVENT_EXTERNAL_INCONSISTENT')
    if any(e['kind'] == 'unknown' and e['amount'] != 0 for e in events):  # zero-amount failed legs stay legal
        raise InvalidRecord('FLOW_EVENT_UNCLASSIFIED')
    if sum((e['amount'] for e in external), Decimal(0)) != flow:
        raise InvalidRecord('FLOW_EVENTS_INCONSISTENT')
    # Aggregate KRW after effective conversion: explicit amounts exactly, derived
    # (native x rate) ones within 1 KRW each.
    effective = [e['krw'] if e['krw'] is not None else None if e['rate'] is None else e['amount'] * e['rate']
                 for e in external]
    if flow_krw is not None and all(value is not None for value in effective):
        derived = sum(1 for e in external if e['krw'] is None and not e['rate_exact'])
        if abs(sum(effective, Decimal(0)) - flow_krw) > derived:
            raise InvalidRecord('FLOW_KRW_INCONSISTENT')


def validate_record(record):
    """One strict validator for the shared per-interval flow record shape.

    Unresolved records are envelopes: unknown event amounts are allowed and the
    provider reasons survive, but they never reach arithmetic. Resolved records
    are checked event by event and in aggregate (native and KRW) for any timing.
    """
    if not isinstance(record, dict) or any(key not in record for key in REQUIRED_KEYS):
        raise InvalidRecord('FLOW_RECORD_SHAPE')
    if record['source'] not in SOURCES:
        raise InvalidRecord('FLOW_RECORD_SOURCE_UNKNOWN')
    for key in ('account', 'currency'):
        if not isinstance(record[key], str) or not record[key].strip():
            raise InvalidRecord('FLOW_RECORD_SHAPE')
    t0, t1 = _instant(record['t0']), _instant(record['t1'])
    if t0 is None or t1 is None or t1 <= t0:
        raise InvalidRecord('FLOW_RECORD_TIME_INVALID')
    for key in ('start_snapshot_id', 'end_snapshot_id'):
        if record[key] is not None and not isinstance(record[key], str):
            raise InvalidRecord('FLOW_RECORD_SHAPE')
    if record['timing'] not in TIMINGS or record['method'] not in METHODS or record['confidence'] not in CONFIDENCES:
        raise InvalidRecord('FLOW_RECORD_ENUM_UNKNOWN')
    if not isinstance(record['reasons'], list) or not all(isinstance(r, str) for r in record['reasons']):
        raise InvalidRecord('FLOW_RECORD_SHAPE')
    if not isinstance(record['inputs'], dict) or not isinstance(record['events'], list):
        raise InvalidRecord('FLOW_RECORD_SHAPE')
    flow = _decimal(record['flow_native'], nullable=True)
    income = _decimal(record['income_native'], nullable=True)
    if (flow is None) != (record['confidence'] == 'unresolved'):
        raise InvalidRecord('FLOW_RECORD_CONFIDENCE_INCONSISTENT')
    resolved = flow is not None
    currency = record['currency'].strip().upper()
    record_fx = _decimal(record.get('fx_to_krw'), nullable=True)
    if record_fx is not None and record_fx <= 0:
        raise InvalidRecord('FLOW_RECORD_FX_INVALID')
    if currency == 'KRW' and record_fx is not None and record_fx != 1:
        raise InvalidRecord('FLOW_RECORD_FX_INCONSISTENT')
    flow_krw = _decimal(record.get('flow_krw'), nullable=True)
    events = []
    for event in record['events']:
        if not isinstance(event, dict) or event.get('kind') not in EVENT_KINDS or not isinstance(event.get('external'), bool):
            raise InvalidRecord('FLOW_EVENT_SHAPE')
        stamp = event.get('time')
        when = None if stamp is None else _instant(stamp)
        if stamp is not None and (when is None or resolved and not t0 < when <= t1):
            raise InvalidRecord('FLOW_EVENT_TIME_INVALID')
        fx = _event_rate(event)
        if currency == 'KRW' and fx is not None and fx != 1:
            raise InvalidRecord('FLOW_EVENT_FX_INCONSISTENT')
        krw = _decimal(event.get('amount_krw'), nullable=True)
        # Effective conversion: KRW is exactly 1; otherwise event FX overrides the record fallback.
        rate = Decimal(1) if currency == 'KRW' else fx if fx is not None else record_fx
        # A provider's KRW range (interpolated FX, unproven fee basis): only on an external
        # converted flow of an estimate, and it must contain the amount.
        span = event.get('amount_krw_range')
        if span is not None:
            if (not isinstance(span, list) or len(span) != 2 or krw is None or not event['external']
                    or record['confidence'] == 'observed'):
                raise InvalidRecord('FLOW_EVENT_RANGE_INVALID')
            span = (_decimal(span[0]), _decimal(span[1]))
            if not span[0] <= krw <= span[1]:
                raise InvalidRecord('FLOW_EVENT_RANGE_INVALID')
        events.append({'time': when, 'amount': _decimal(event.get('amount_native'), nullable=not resolved),
                       'kind': event['kind'], 'external': event['external'], 'fx': fx, 'krw': krw,
                       'rate': rate, 'rate_exact': currency == 'KRW', 'krw_range': span})
    external = [e for e in events if e['external']]
    blocked = []
    if resolved:
        _check_events(flow, flow_krw, events, external)
        if record['timing'] == 'known' and any(e['time'] is None for e in external):
            raise InvalidRecord('FLOW_EVENT_TIME_INVALID')
        # Crypto records carry event-time KRW (``flow_krw``/``conversion_status``). Missing
        # event FX blocks the record whatever its net native sum; NAV marks never stand in.
        # A stablecoin rate interpolated between the capture marks is an estimate, never observed.
        if record.get('conversion_status') == 'interpolated' and record['confidence'] == 'observed':
            raise InvalidRecord('FLOW_RECORD_CONFIDENCE_INCONSISTENT')
        if ('flow_krw' in record or 'conversion_status' in record) and (
                record.get('conversion_status', 'observed') not in ('observed', 'interpolated')
                or any(e['krw'] is None for e in external)
                or flow_krw is None and external):
            blocked.append('FLOW_KRW_CONVERSION_UNRESOLVED')
    start_version, end_version = _record_versions(record)
    return {'source': record['source'], 'account': record['account'], 'currency': currency,
            't0': t0, 't1': t1, 'start_snapshot_id': record['start_snapshot_id'],
            'end_snapshot_id': record['end_snapshot_id'], 'start_version': start_version,
            'end_version': end_version, 'flow': flow, 'income': income,
            'external': external, 'timing': record['timing'], 'method': record['method'],
            'confidence': record['confidence'], 'reasons': list(record['reasons']), 'fx': record_fx,
            'blocked': blocked}


def _safe_reasons(record):
    """Provider reason codes of a record the validator rejected (codes only, never free text)."""
    reasons = record.get('reasons') if isinstance(record, dict) else None
    return [r for r in reasons if isinstance(r, str) and REASON_CODE.match(r)] if isinstance(reasons, list) else []


def _canonical(record):
    try:
        return json.dumps(record, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def dietz_terms(n0, n1, flows):
    """(gain, denominator) of Modified Dietz at the flows' point estimates."""
    n0, n1 = Decimal(n0), Decimal(n1)
    return (n1 - n0 - sum((f['amount'] for f in flows), Decimal(0)),
            n0 + sum((f['weight'] * f['amount'] for f in flows), Decimal(0)))


def interval_return(n0, n1, flows):
    """Modified Dietz for one interval with KRW flows already weighted.

    ``flows``: dicts with ``amount`` (estimate), ``weight`` (estimate) and
    optional uncertainty ranges ``amount_range``/``weight_range``. Returns
    (estimate, (low, high) | None, reasons). The denominator must stay positive
    over every admissible timing/FX choice, else the interval is null: its
    minimum is separable per flow (corners of each flow's weight x amount box).
    Over that box the return is monotone in each coordinate, so corners bound it.
    """
    n0, n1 = Decimal(n0), Decimal(n1)
    gain, denominator = dietz_terms(n0, n1, flows)
    if denominator <= 0:
        return None, None, ['NON_POSITIVE_DIETZ_DENOMINATOR']
    lowest = n0 + sum((min(w * a for w in (f.get('weight_range') or (f['weight'],))
                           for a in (f.get('amount_range') or (f['amount'],))) for f in flows), Decimal(0))
    if lowest <= 0:
        return None, None, ['SENSITIVITY_DENOMINATOR_NON_POSITIVE']
    estimate = gain / denominator
    dimensions = []
    for index, f in enumerate(flows):
        for key in ('amount', 'weight'):
            span = f.get(key + '_range')
            if span and span[0] != span[1]:
                dimensions.append((index, key, span))
    if not dimensions:
        return estimate, (estimate, estimate), []
    if len(dimensions) > MAX_UNCERTAIN_DIMENSIONS:
        return estimate, None, ['SENSITIVITY_TOO_MANY_UNCERTAIN_FLOWS']
    values = []
    for corner in itertools.product((0, 1), repeat=len(dimensions)):
        chosen = [dict(f) for f in flows]
        for pick, (index, key, span) in zip(corner, dimensions):
            chosen[index][key] = span[pick]
        corner_gain = n1 - n0 - sum((f['amount'] for f in chosen), Decimal(0))
        corner_denominator = n0 + sum((f['weight'] * f['amount'] for f in chosen), Decimal(0))
        values.append(corner_gain / corner_denominator)
    return estimate, (min(values), max(values)), []


def link(returns):
    factor = Decimal(1)
    for value in returns:
        factor *= 1 + value
    return factor - 1


def link_bounds(bounds):
    """Linked (low, high) of independent interval ranges: factors may be negative, so each
    step takes the min/max of all four endpoint products."""
    low = high = Decimal(1)
    for a, b in bounds:
        products = (low * (1 + a), low * (1 + b), high * (1 + a), high * (1 + b))
        low, high = min(products), max(products)
    return low - 1, high - 1


def _rate(point, currency):
    if currency == 'KRW':
        return Decimal(1)
    mark = ((point.get('fx_times') or {}).get(currency) or {}).get('rate_to_krw')
    try:
        rate = Decimal(str(mark)) if mark is not None and not isinstance(mark, bool) else None
    except InvalidOperation:
        return None
    return rate if rate is not None and rate.is_finite() and rate > 0 else None


def _in_scope(account, scope, accounts):
    if scope == 'kis':
        return account.startswith('kis:')
    if scope == 'toss':
        return account.startswith('toss:')
    if scope == 'toss_investmcp':
        return account.startswith('toss:') and account in accounts
    return account in ('binance', 'hyperliquid')


def _nonzero(value):
    try:
        return Decimal(str(value)) != 0
    except InvalidOperation:
        return True


def _required_keys(point, scope, accounts):
    """(account, currency) pairs whose flows must be covered at an endpoint.

    Every scope account needs records; a currency is required when its cash or
    native NAV is unknown or nonzero at the endpoint. Zero-valued currencies
    still must tile when any record for them exists.
    """
    keys, venues = set(), set()
    if scope in ('toss', 'toss_investmcp'):
        for account in sorted(k for k in (point.get('account_nav') or {}) if _in_scope(k, scope, accounts)):
            row = (point.get('account_nav') or {}).get(account) or {}
            for group in ('cash', 'nav_native'):
                for currency, metric in (row.get(group) or {}).items():
                    value = (metric or {}).get('value')
                    if value is None or value != 0:
                        keys.add((account, currency.upper()))
            venues.add(account)
    elif scope == 'kis':
        capture = point.get('kis_capture') if isinstance(point.get('kis_capture'), dict) else {}
        for account, row in sorted((capture.get('accounts') or {}).items()):
            if not account.startswith('kis:') or not isinstance(row, dict):
                continue
            for group in ('cash_native', 'nav_native'):
                for currency, metric in (row.get(group) or {}).items():
                    value = (metric or {}).get('value')
                    if value is None or _nonzero(value):
                        keys.add((account, currency.upper()))
            venues.add(account)
    else:
        for venue in ('binance', 'hyperliquid'):
            venues.add(venue)
            for currency, metric in (((point.get('crypto_nav') or {}).get(venue) or {}).get('by_currency') or {}).items():
                value = (metric or {}).get('value')
                if value is None or value != 0:
                    keys.add((venue, currency.upper()))
    return keys, venues


def _point(nav):
    reasons = list(nav.get('reason_codes') or [])
    value = nav.get('total_value_krw')
    when = _instant(nav.get('valuation_at'))
    if value is None and not reasons:
        reasons.append('MISSING_NAV_VALUE')
    if when is None:
        reasons.append('UNKNOWN_VALUATION_TIME')
    total = None
    if value is not None and not isinstance(value, bool):
        try:
            total = Decimal(str(value))
        except InvalidOperation:
            total = None
        if total is None or not total.is_finite():
            reasons.append('INVALID_NAV_VALUE')
            total = None
        elif total < 0:
            reasons.append('NEGATIVE_NAV')
    return {'nav': nav, 'snapshot_id': nav.get('snapshot_id'), 'date': nav.get('snapshot_date'),
            'instant': when, 'value': total, 'reasons': list(dict.fromkeys(reasons))}


def _bind(points, snapshot_id, stamp, version):
    """(index, None) when a record endpoint names exactly one NAV point by immutable ID,
    at the same instant and version; else (None, reason). Never rebinds by time."""
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None, 'FLOW_RECORD_SNAPSHOT_UNBOUND'
    found = [index for index, point in enumerate(points) if point['snapshot_id'] == snapshot_id]
    if not found:
        # Same instant, other ID: the provider read another version than the pinned NAV.
        same = any(point['instant'] == stamp for point in points)
        return None, 'FLOW_RECORD_SNAPSHOT_VERSION_MISMATCH' if same else 'FLOW_RECORD_SNAPSHOT_UNKNOWN'
    if len(found) > 1:
        return None, 'FLOW_RECORD_SNAPSHOT_AMBIGUOUS'
    point = points[found[0]]
    if point['instant'] != stamp:
        return None, 'FLOW_RECORD_SNAPSHOT_TIME_MISMATCH'
    stored = point['nav'].get('version')
    if stored is not None and version is None:
        return None, 'FLOW_RECORD_SNAPSHOT_VERSION_UNDECLARED'
    if stored is not None and version != stored:
        return None, 'FLOW_RECORD_SNAPSHOT_VERSION_MISMATCH'
    return found[0], None


def _flow_terms(record, lo, hi, t0, t1, start_point, end_point):
    """KRW flow terms for one record placed in scope interval (t0, t1]."""
    span = (t1 - t0).total_seconds()
    reasons, complete = [], record['confidence'] == 'observed'

    def weight(stamp):
        return Decimal(str((t1 - stamp).total_seconds())) / Decimal(str(span))

    def fx(event_fx=None):
        if record['currency'] == 'KRW':
            return Decimal(1), None
        explicit = event_fx or record['fx']
        if explicit is not None:
            return explicit, None
        marks = [_rate(start_point['nav'], record['currency']), _rate(end_point['nav'], record['currency'])]
        if any(m is None for m in marks):
            return None, None
        return (marks[0] + marks[1]) / 2, (min(marks), max(marks))

    # One term per gross external event: opposite flows never net away, a known
    # event time keeps its weight and only unknown times get the midpoint/range.
    terms, dated, undated = [], [], False
    for event in record['external']:
        if event['krw'] is not None:
            rate_range, amount = None, event['krw']
        else:
            rate, rate_range = fx(event['fx'])
            if rate is None:
                return None, ['MISSING_FLOW_FX'], False, [], False
            amount = event['amount'] * rate
        term = {'amount': amount}
        if event['krw_range'] is not None:
            complete = False
            term['amount_range'] = event['krw_range']
        if rate_range is not None:
            reasons.append('FLOW_FX_FROM_SNAPSHOT_MARKS')
            complete = False
            term['amount_range'] = (event['amount'] * rate_range[0], event['amount'] * rate_range[1])
        if event['time'] is None:
            weights = (weight(hi), weight(lo))
            term.update(weight=(weights[0] + weights[1]) / 2, weight_range=weights)
            undated = True
        elif not t0 < event['time'] <= t1:
            return None, ['FLOW_EVENT_OUTSIDE_INTERVAL'], False, [], False
        else:
            term['weight'] = weight(event['time'])
            dated.append((event['time'], term))
        terms.append(term)
    if undated:
        reasons.append('FLOW_TIMING_UNKNOWN')
        complete = False
    return terms, list(dict.fromkeys(reasons)), complete, dated, undated


def compute(navs, records, scope, start, end, *, accounts=(), accuracy=None, detail_level='full',
            provider_reasons=()):
    """Pure engine: scoped NAV dicts + validated-on-entry flow records -> block."""
    accuracy = ACCURACY if accuracy is None else accuracy
    if scope not in SUPPORTED_SCOPES:
        return unavailable(scope, start, end, ['SCOPE_NOT_YET_SUPPORTED_LEDGERLESS'], accuracy)
    if scope in COMPOSITE_SCOPES:  # needs every scope's parts: consolidate()
        return unavailable(scope, start, end, ['CONSOLIDATED_NEEDS_SCOPE_PARTS'], accuracy)
    points, complete, intervals, unplaced, normalized = _scope_intervals(navs, records, scope, accounts)
    period = _period(points, complete, intervals, start, end, [*provider_reasons, *unplaced])
    effective = _effective_period(points, complete, intervals, start, end, [*provider_reasons, *unplaced])
    return _block(scope, start, end, period, intervals, points, normalized, accuracy, detail_level,
                  effective=effective)


def _scope_intervals(navs, records, scope, accounts=()):
    """(points, complete point indexes, interval rows, unplaced invalid-record reasons, records) of one scope.

    A row whose levels and flows resolved keeps them as ``_levels`` (KRW NAV at both
    ends, weighted KRW flow terms) even when its own Dietz denominator is not positive:
    consolidated sums levels, never percentages.
    """
    points = sorted((_point(nav) for nav in navs), key=lambda p: (p['date'] or '', p['instant'] or datetime.min.replace(tzinfo=KST)))
    complete = [i for i, p in enumerate(points) if not p['reasons'] and p['value'] is not None]
    reasons = []
    normalized, invalid = [], []
    seen = {}
    for record in records or []:
        if isinstance(record, dict) and isinstance(record.get('account'), str) and not _in_scope(record['account'], scope, accounts):
            continue
        try:
            item = validate_record(record)
        except InvalidRecord as exc:
            stamps = (_instant(record.get('t0')), _instant(record.get('t1'))) if isinstance(record, dict) else (None, None)
            invalid.append((str(exc), stamps, _safe_reasons(record)))
            continue
        if (item['source'] != 'kis' if scope == 'kis' else item['source'] == 'kis'
                or (item['source'] == 'toss') != (scope != 'crypto')
                or (scope == 'crypto' and item['account'] != item['source'])):
            invalid.append(('FLOW_RECORD_SOURCE_MISMATCH', (item['t0'], item['t1']), item['reasons']))
            continue
        key = (item['account'], item['currency'], item['t0'], item['t1'])
        text = _canonical(record)
        if key in seen:
            if seen[key] != text or text is None:
                invalid.append(('DUPLICATE_FLOW_RECORD', (item['t0'], item['t1']), []))
            continue
        seen[key] = text
        normalized.append(item)
    intervals, placed = [], set()
    for a, b in zip(complete, complete[1:]):
        p0, p1 = points[a], points[b]
        t0, t1 = p0['instant'], p1['instant']
        days = (t1.astimezone(KST).date() - t0.astimezone(KST).date()).days
        row = {'t0': t0.isoformat(), 't1': t1.isoformat(), 'start_date': p0['date'], 'end_date': p1['date'],
               'start_snapshot_id': p0['snapshot_id'], 'end_snapshot_id': p1['snapshot_id'],
               'start_nav_krw': _float(p0['value'], 2), 'end_nav_krw': _float(p1['value'], 2),
               'return_pct': None, 'sensitivity_bounds_pct': None, 'flows_krw': None,
               'flows_native': {}, 'income_native': {}, 'timing': 'known', 'confidence': 'observed',
               'methods': [], 'bridged': b - a > 1 or days > 1, 'skipped_snapshots': [
                   {'snapshot_id': points[i]['snapshot_id'], 'date': points[i]['date'], 'reason_codes': points[i]['reasons']}
                   for i in range(a + 1, b)], 'reason_codes': [], '_complete_flows': True, '_dated': True,
               '_events': []}
        errors = []
        if t1 <= t0:
            errors.append('NON_INCREASING_SNAPSHOT_TIME')
        for index, (code, (lo, hi), provided) in enumerate(invalid):
            if _touches(lo, hi, t0, t1):
                errors.extend(['INVALID_FLOW_RECORD', code, *provided])
                placed.add(index)
        required, venues = _required_keys(p0['nav'], scope, accounts)
        later, _ = _required_keys(p1['nav'], scope, accounts)
        required |= later
        terms = []
        by_key = {}
        for item in normalized:
            if item['t1'] <= t0 or item['t0'] >= t1:
                continue
            i, why = _bind(points, item['start_snapshot_id'], item['t0'], item['start_version'])
            j, other = _bind(points, item['end_snapshot_id'], item['t1'], item['end_version'])
            if why or other:
                errors.extend([why or other, *item['reasons']])
                continue
            if j <= i:
                errors.append('FLOW_RECORD_SNAPSHOT_ORDER_INVALID')
                continue
            if i < a or j > b:
                errors.append('FLOW_RECORD_STRADDLES_INTERVAL')
                continue
            by_key.setdefault((item['account'], item['currency']), []).append((i, j, item))
        accounts_seen = {account for account, _ in by_key}
        if not venues:
            errors.append('FLOW_SCOPE_ACCOUNTS_UNKNOWN')
        for venue in venues - accounts_seen:
            if not any(account == venue for account, _ in required):
                required.add((venue, '*'))
        for key in sorted(required | set(by_key)):
            chain = sorted(by_key.get(key, []), key=lambda entry: (entry[2]['t0'], entry[0]))
            if key[1] == '*' or not chain:
                errors.append('FLOW_RECORDS_MISSING')
                continue
            # Contiguous on actual instants, not only on point indices.
            cursor = t0
            for _, _, item in chain:
                if item['t0'] != cursor:
                    errors.append('FLOW_RECORDS_DO_NOT_TILE_INTERVAL')
                    break
                cursor = item['t1']
            else:
                if cursor != t1:
                    errors.append('FLOW_RECORDS_DO_NOT_TILE_INTERVAL')
        for key, chain in sorted(by_key.items()):
            for i, j, item in chain:
                row['methods'].append(item['method'])
                if item['confidence'] == 'unresolved' or item['flow'] is None:
                    errors.extend(['UNRESOLVED_FLOW', *item['reasons']])
                    continue
                if item['blocked']:
                    errors.extend([*item['blocked'], *item['reasons']])
                    continue
                if item['confidence'] == 'conditional':
                    row['confidence'] = 'conditional' if row['confidence'] == 'observed' else row['confidence']
                row['reason_codes'].extend(item['reasons'])
                currency = item['currency']
                row['flows_native'][currency] = str(Decimal(row['flows_native'].get(currency, '0')) + item['flow'])
                if item['income'] is not None and item['income'] != 0:
                    row['income_native'][currency] = str(Decimal(row['income_native'].get(currency, '0')) + item['income'])
                found, notes, observed, dated, undated = _flow_terms(
                    item, points[i]['instant'], points[j]['instant'], t0, t1, p0, p1)
                if found is None:
                    errors.extend(notes)
                    continue
                row['reason_codes'].extend(notes)
                if undated:
                    row['timing'] = 'unknown'
                    row['_dated'] = False
                    row['confidence'] = 'conditional' if row['confidence'] == 'observed' else row['confidence']
                row['_complete_flows'] &= observed
                terms.extend(found)
                row['_events'].extend(dated)
        row['methods'] = sorted(set(row['methods']))
        if errors:
            row.update(confidence='unresolved', reason_codes=list(dict.fromkeys(errors + row['reason_codes'])))
            row['_complete_flows'] = False
            intervals.append(row)
            continue
        row['_levels'] = {'n0': p0['value'], 'n1': p1['value'], 'terms': terms, 'complete': row['_complete_flows'],
                          'confidence': row['confidence'], 'reason_codes': list(row['reason_codes'])}
        estimate, bounds, notes = interval_return(p0['value'], p1['value'], terms)
        row['reason_codes'] = list(dict.fromkeys(row['reason_codes'] + notes))
        row['flows_krw'] = _float(sum((t['amount'] for t in terms), Decimal(0)), 2)
        if estimate is None:
            row['confidence'] = 'unresolved'
            row['_complete_flows'] = False
        else:
            row['_return'] = estimate
            row['_bounds'] = bounds
            row['_dietz'] = dietz_terms(p0['value'], p1['value'], terms)
            row['return_pct'] = _pct(estimate)
            row['sensitivity_bounds_pct'] = {'low': _pct(bounds[0]), 'high': _pct(bounds[1])} if bounds else None
        if row['bridged']:
            row['reason_codes'].append('BRIDGED_MULTI_SNAPSHOT_INTERVAL' if row['skipped_snapshots'] else 'MULTI_DAY_INTERVAL')
        intervals.append(row)
    for index, (code, _, provided) in enumerate(invalid):
        if index not in placed:  # an in-scope invalid record that fits no interval still blocks the period
            reasons.extend(['INVALID_FLOW_RECORD', code, *provided])
    return points, complete, intervals, reasons, normalized


AGGREGATION = {'formula': '(sum N1 - sum N0 - sum F) / (sum N0 + sum w F) over the included scopes',
               'levels': "each scope's KRW NAV and KRW flows at the FX marks that scope already uses",
               'weights': "each flow weighted within its own scope's interval", 'percentages_averaged': False,
               'cross_scope_transfers': 'not matched; they net only when both legs land in the same interval'}


def consolidate(parts, start, end, *, excluded=None, accuracy=None, detail_level='full', reasons=(),
                scope='consolidated'):
    """Pure consolidated engine over ``parts``: scope -> (scoped NAVs, flow records, accounts, load reasons).

    ``excluded`` maps a scope excluded as empty to its reason codes. Intervals run between
    consecutive dates on which any included scope has an interval boundary; one has a value only
    when every included scope has resolved levels and flows over exactly those dates.
    ``scope`` names the composite (``COMPOSITE_SCOPES``): consolidated, or equity (toss + kis).
    """
    composite = scope
    accuracy = ACCURACY if accuracy is None else accuracy
    included = [scope for scope in COMPOSITE_SCOPES[composite] if scope in parts]
    accuracy = {**accuracy, composite: _composite_accuracy(accuracy, included)}
    extra = {'included_scopes': included,
             'excluded_scopes': [{'scope': scope, 'reason_codes': list(codes)} for scope, codes in (excluded or {}).items()],
             'missing_scopes': [], 'aggregation': AGGREGATION}
    if not included:
        block = unavailable(composite, start, end, ['CONSOLIDATED_NO_INCLUDED_SCOPE'], accuracy)
        del block['explanation']
        block.update(extra)
        block['explanation'] = _explain(block)
        return block
    reasons, records, built = list(reasons), [], {}
    for scope in included:
        navs, scope_records, accounts, loaded = parts[scope]
        points, complete, intervals, unplaced, normalized = _scope_intervals(navs, scope_records, scope, accounts)
        dates, rows = {}, {}
        for point in points:
            dates.setdefault(point['date'], []).append(point)
        for row in intervals:
            rows.setdefault((row['start_date'], row['end_date']), []).append(row)
        built[scope] = (dates, rows, list(loaded), points, len(complete))
        records.extend(normalized)
        reasons.extend(unplaced)
    points = []
    for day in sorted({day for dates, *_ in built.values() for day in dates if day}):
        here = [built[scope][0].get(day) or [] for scope in included]
        whole = all(len(found) == 1 and not found[0]['reasons'] and found[0]['value'] is not None for found in here)
        stamps = [p['instant'] for found in here for p in found if p['instant'] is not None]
        ids = [p['snapshot_id'] for found in here for p in found if p['snapshot_id']]
        points.append({'nav': {}, 'snapshot_id': ids[0] if ids else None, 'date': day,
                       'instant': max(stamps) if stamps else None,
                       'value': sum((found[0]['value'] for found in here), Decimal(0)) if whole else None,
                       'reasons': [] if whole else ['CONSOLIDATED_SCOPE_MISSING']})
    by_date = {point['date']: point for point in points}
    grid = sorted({day for _, rows, *_ in built.values() for key in rows for day in key if day})
    intervals, summary = [], {}
    for a, b in zip(grid, grid[1:]):
        present, missing = {}, []
        for scope in included:
            dates, rows, loaded, *_ = built[scope]
            found = rows.get((a, b)) or []
            if len(found) == 1 and '_levels' in found[0] and all(len(dates.get(day) or []) == 1 for day in (a, b)):
                present[scope] = found[0]
            else:
                missing.append({'scope': scope, 'reason_codes': list(dict.fromkeys([*loaded, *_gap(found, dates, a, b)]))})
        for entry in missing:
            item = summary.setdefault(entry['scope'], {'scope': entry['scope'], 'intervals_missing': 0, 'reason_codes': []})
            item['intervals_missing'] += 1
            item['reason_codes'] = list(dict.fromkeys(item['reason_codes'] + entry['reason_codes']))
        intervals.append(_consolidated_row(by_date[a], by_date[b], present, missing, accuracy,
                                           detail_level in ('full', 'diagnostic')))
    for scope in included if not intervals else ():  # nothing to pair: say why per scope
        dates, rows, loaded, scope_points, complete = built[scope]
        if complete < 2:
            codes = [*loaded, *(code for point in scope_points for code in point['reasons'])]
            summary[scope] = {'scope': scope, 'intervals_missing': 0,
                              'reason_codes': list(dict.fromkeys(codes)) or ['INSUFFICIENT_NAV_SNAPSHOTS']}
    extra['missing_scopes'] = [summary[scope] for scope in included if scope in summary]
    whole = [i for i, p in enumerate(points) if not p['reasons'] and p['value'] is not None]
    period = _period(points, whole, intervals, start, end, reasons)
    return _block(composite, start, end, period, intervals, points, records, accuracy, detail_level, extra,
                  effective=_effective_period(points, whole, intervals, start, end, reasons))


def _gap(found, dates, a, b):
    """A scope's own reasons for having no resolved interval over exactly (a, b]."""
    if any(len(dates.get(day) or []) > 1 for day in (a, b)) or len(found) > 1:
        return ['CONSOLIDATED_SNAPSHOT_DATE_AMBIGUOUS']
    if found:  # the scope's own interval over these dates did not resolve
        return list(found[0]['reason_codes'])
    reasons = []
    for day in (a, b):
        here = dates.get(day)
        if not here:
            reasons.append('MISSING_SCOPE_SNAPSHOT')
        elif here[0]['reasons'] or here[0]['value'] is None:
            reasons.extend(here[0]['reasons'] or ['MISSING_NAV_VALUE'])
    return reasons or ['CONSOLIDATED_SCOPE_INTERVAL_MISMATCH']


def _consolidated_row(p0, p1, present, missing, accuracy, full):
    """One consolidated interval: Modified Dietz on the summed levels, or null naming the missing scopes."""
    row = {'t0': p0['instant'].isoformat() if p0['instant'] else None,
           't1': p1['instant'].isoformat() if p1['instant'] else None,
           'start_date': p0['date'], 'end_date': p1['date'],
           'start_snapshot_id': p0['snapshot_id'], 'end_snapshot_id': p1['snapshot_id'],
           'start_nav_krw': None, 'end_nav_krw': None, 'return_pct': None, 'sensitivity_bounds_pct': None,
           'flows_krw': None, 'timing': 'known', 'confidence': 'observed', 'methods': [],
           'bridged': any(part['bridged'] for part in present.values()), 'reason_codes': [],
           'missing_scopes': missing, '_complete_flows': False, '_dated': True, '_events': []}
    if full:  # each part as its own scope shows it (t0/t1 are the latest valuation instant on each date)
        row['components'] = [{'scope': scope, 't0': part['t0'], 't1': part['t1'],
                              'start_nav_krw': part['start_nav_krw'], 'end_nav_krw': part['end_nav_krw'],
                              'flows_krw': part['flows_krw'], 'return_pct': part['return_pct'],
                              'timing': part['timing'], 'confidence': part['_levels']['confidence'],
                              'label': _label(scope, part['_levels']['complete'], accuracy)}
                             for scope, part in present.items()]
    if missing:
        row.update(confidence='unresolved', reason_codes=list(dict.fromkeys(
            ['CONSOLIDATED_SCOPE_MISSING', *(code for entry in missing for code in entry['reason_codes'])])))
        return row
    terms, n0, n1, complete = [], Decimal(0), Decimal(0), True
    for part in present.values():
        levels = part['_levels']
        n0, n1, complete = n0 + levels['n0'], n1 + levels['n1'], complete and levels['complete']
        terms.extend(levels['terms'])
        row['methods'].extend(part['methods'])
        row['reason_codes'].extend(levels['reason_codes'])
        row['_events'].extend(part['_events'])
        if not part['_dated']:
            row.update(timing='unknown', _dated=False)
        if levels['confidence'] != 'observed':
            row['confidence'] = 'conditional'
    estimate, bounds, notes = interval_return(n0, n1, terms)
    row.update(start_nav_krw=_float(n0, 2), end_nav_krw=_float(n1, 2), methods=sorted(set(row['methods'])),
               flows_krw=_float(sum((t['amount'] for t in terms), Decimal(0)), 2),
               reason_codes=list(dict.fromkeys(row['reason_codes'] + notes)))
    if estimate is None:
        row['confidence'] = 'unresolved'
    else:
        row.update(_return=estimate, _bounds=bounds, _dietz=dietz_terms(n0, n1, terms), _complete_flows=complete,
                   return_pct=_pct(estimate),
                   sensitivity_bounds_pct={'low': _pct(bounds[0]), 'high': _pct(bounds[1])} if bounds else None)
    if row['bridged']:
        row['reason_codes'].append('BRIDGED_MULTI_SNAPSHOT_INTERVAL' if any(
            part['skipped_snapshots'] for part in present.values()) else 'MULTI_DAY_INTERVAL')
    return row


def _touches(lo, hi, t0, t1):
    """Whether an invalid record's stamps overlap scope interval (t0, t1]."""
    if lo is None or hi is None:
        return True
    if hi <= lo:  # degenerate or reversed: closed overlap, conservative
        return hi <= t1 and lo >= t0
    return lo < t1 and hi > t0


def _period(points, complete, intervals, start, end, provider_reasons):
    reasons = list(provider_reasons)
    first = points[0] if points else None
    last = points[-1] if points else None
    if not points or first['date'] != start or first['reasons'] or first['value'] is None:
        reasons.append('MISSING_START_NAV')
    if not points or last['date'] != end or last['reasons'] or last['value'] is None:
        reasons.append('MISSING_END_NAV')
    if len(complete) < 2:
        reasons.append('INSUFFICIENT_NAV_SNAPSHOTS')
    for row in intervals:
        if '_return' not in row:
            reasons.append('UNRESOLVED_INTERVAL_IN_PERIOD')
            reasons.extend(row['reason_codes'])
    reasons = list(dict.fromkeys(reasons))
    period = {'start': start, 'end': end,
              't0': intervals[0]['t0'] if intervals else None, 't1': intervals[-1]['t1'] if intervals else None,
              'return_pct': None, 'sensitivity_bounds_pct': None, 'xirr_pct': None, 'xirr_reason_codes': [],
              'reason_codes': reasons, 'intervals': len(intervals)}
    if reasons:
        period['xirr_reason_codes'] = ['PERIOD_RETURN_UNAVAILABLE']
        return period
    period['_return'] = link(row['_return'] for row in intervals)
    period['return_pct'] = _pct(period['_return'])
    if all(row['_bounds'] for row in intervals):
        low, high = link_bounds(row['_bounds'] for row in intervals)
        period['sensitivity_bounds_pct'] = {'low': _pct(low), 'high': _pct(high)}
    else:
        period['reason_codes'] = ['SENSITIVITY_BOUNDS_UNAVAILABLE']
    period['_complete_flows'] = all(row['_complete_flows'] for row in intervals)
    period['xirr_pct'], period['xirr_reason_codes'] = _xirr(intervals, points, complete)
    return period


def _effective_period(points, complete, intervals, start, end, provider_reasons):
    """The period over [first, last] complete snapshot inside [start, end], by _period's own rules.

    None when fewer than two complete snapshots fall inside. KIS_CAPTURE_MISSING marks a period
    opening before the first KIS capture; a clipped range starts at a complete (captured) point.
    """
    inside = [i for i in complete if points[i]['date'] and start <= points[i]['date'] <= end]
    if len(inside) < 2:
        return None
    lo, hi = inside[0], inside[-1]
    first, last = points[lo]['date'], points[hi]['date']
    rows = [row for row in intervals if row['start_date'] and row['end_date']
            and first <= row['start_date'] and row['end_date'] <= last]
    effective = _period(points[lo:hi + 1], [i - lo for i in complete if lo <= i <= hi], rows, first, last,
                        [code for code in provider_reasons if code != 'KIS_CAPTURE_MISSING'])
    effective['clipped'] = (first, last) != (start, end)
    effective['clip_reason_codes'] = (['EFFECTIVE_START_AFTER_PERIOD_START'] if first != start else []) + (
        ['EFFECTIVE_END_BEFORE_PERIOD_END'] if last != end else [])
    return effective


def _xirr(intervals, points, complete):
    from portfolio_engine import xirr
    if not all(row['_dated'] and row['_complete_flows'] for row in intervals):
        return None, ['XIRR_REQUIRES_DATED_COMPLETE_FLOWS']
    start, end = points[complete[0]], points[complete[-1]]
    flows = [(start['instant'].astimezone(KST).date().isoformat(), -float(start['value']))]
    for row in intervals:
        for stamp, term in row['_events']:
            flows.append((stamp.astimezone(KST).date().isoformat(), -float(term['amount'])))
    flows.append((end['instant'].astimezone(KST).date().isoformat(), float(end['value'])))
    try:
        rate = xirr(flows)
    except (OverflowError, ZeroDivisionError, ValueError):
        rate = None
    if rate is None:
        return None, ['XIRR_NONCONVERGENT']
    return round(rate * 100, 4), []


def _date(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _day_coverage(start, end, intervals, period_reasons, cap, capture_from=None):
    """Resolved/total KST days of the requested period; never a return over the resolved days.

    An interval covers days (start_date, end_date]. Days that no resolved interval covers are listed as
    unresolved spans with their reasons (before the first or after the last complete snapshot:
    MISSING_START_NAV / MISSING_END_NAV); ``cap`` keeps the latest spans and counts the rest.
    ``capture_from`` (kis scope): days up to the first KIS capture date are KIS_CAPTURE_MISSING.
    """
    first, last = _date(start), _date(end)
    if first is None or last is None or last < first:
        return {'days_total': None, 'days_resolved': None, 'days_unresolved': None, 'unresolved_days': [],
                'unresolved_days_by_reason': {}, 'unresolved_days_omitted': 0}
    spans, resolved = [], 0

    def add(a, b, reasons):
        a, b = max(a, first), min(b, last)
        if b > a:
            spans.append({'start_date': a.isoformat(), 'end_date': b.isoformat(), 'days': (b - a).days,
                          'reason_codes': list(reasons)})
    edges = [(_date(row['start_date']), _date(row['end_date'])) for row in intervals]
    edges = [(a, b) for a, b in edges if a is not None and b is not None]
    captured = _date(capture_from) if capture_from else None
    if captured is not None:
        add(first, min(captured, edges[0][0]) if edges else captured, ['KIS_CAPTURE_MISSING'])
    after = max(first, captured or first)
    if not intervals or len(edges) != len(intervals):
        add(after, last, period_reasons or ['INSUFFICIENT_NAV_SNAPSHOTS'])
    else:
        add(after, edges[0][0], ['MISSING_START_NAV'])
        for row, (a, b) in zip(intervals, edges):
            if '_return' in row:
                resolved += max(0, (min(b, last) - max(a, first)).days)
            else:
                add(a, b, row['reason_codes'])
        add(edges[-1][1], last, ['MISSING_END_NAV'])
    by_reason = {}
    for span in spans:
        for code in span['reason_codes']:
            by_reason[code] = by_reason.get(code, 0) + span['days']
    total = (last - first).days
    omitted = max(0, len(spans) - cap) if cap else 0
    return {'days_total': total, 'days_resolved': resolved, 'days_unresolved': total - resolved,
            'unresolved_days': spans[omitted:], 'unresolved_days_by_reason': by_reason,
            'unresolved_days_omitted': omitted}


def _first_kis_capture(scope, points, end):
    """kis scope: the first snapshot date with a KIS capture (``end`` when none), if an earlier one lacks it."""
    if scope != 'kis' or not any('KIS_CAPTURE_MISSING' in p['reasons'] for p in points):
        return None
    return next((p['date'] for p in points if p['date'] and 'KIS_CAPTURE_MISSING' not in p['reasons']), end)


def _label(scope, complete_flows, accuracy):
    entry = accuracy.get(scope) or {}
    if scope in COMPOSITE_SCOPES:  # the weakest part: verified only when every included part would be
        components = (entry.get('components') or {}).values()
        passed = bool(components) and all(component.get('passed_bar') for component in components)
    else:
        passed = bool(entry.get('passed_bar'))
    return 'verified' if passed and complete_flows else 'estimate'


def _accuracy(scope, accuracy):
    entry = dict(accuracy.get(scope) or {'status': 'unmeasured', 'passed_bar': False, 'reason_codes': ['BACKTEST_PENDING']})
    entry.setdefault('bar', BAR)
    return entry


def _block(scope, start, end, period, intervals, points, records, accuracy, detail_level, extra=None,
           effective=None):
    available = period['return_pct'] is not None
    label = _label(scope, period.get('_complete_flows', False), accuracy) if available else 'unavailable'
    resolved = sum('_return' in row for row in intervals)
    confidences = {name: sum(r['confidence'] == name for r in records) for name in CONFIDENCES}
    carried = {}  # reasons on resolved (estimate) intervals, e.g. income booked from a small residual
    for row in intervals:
        for code in row['reason_codes'] if '_return' in row else ():
            carried[code] = carried.get(code, 0) + 1
    coverage = {'snapshots_total': len(points), 'snapshots_complete': sum(not p['reasons'] and p['value'] is not None for p in points),
                'intervals_total': len(intervals), 'intervals_resolved': resolved,
                'intervals_unresolved': len(intervals) - resolved,
                'intervals_bridged': sum(r['bridged'] for r in intervals),
                'intervals_unknown_timing': sum(r['timing'] == 'unknown' for r in intervals if '_return' in r),
                'resolved_interval_reasons': carried,
                **_day_coverage(start, end, intervals, period['reason_codes'],
                                None if detail_level in ('full', 'diagnostic') else SUMMARY_INTERVALS,
                                capture_from=_first_kis_capture(scope, points, end)),
                'flow_records': len(records), 'flow_records_by_confidence': confidences}
    public = [{**{k: v for k, v in row.items() if not k.startswith('_')},
               'label': _label(scope, row['_complete_flows'], accuracy) if '_return' in row else 'unavailable'}
              for row in intervals]
    if detail_level == 'diagnostic':  # backtest replay only (exact Dietz terms); the tool offers summary/full
        for row, item in zip(public, intervals):
            gain, denominator = item.get('_dietz', (None, None))
            row.update(dietz_gain_krw=None if gain is None else str(gain),
                       dietz_denominator_krw=None if denominator is None else str(denominator))
    omitted = 0
    if detail_level not in ('full', 'diagnostic') and len(public) > SUMMARY_INTERVALS:
        omitted = len(public) - SUMMARY_INTERVALS
        public = public[-SUMMARY_INTERVALS:]
    block = {'label': label, 'method': METHOD, 'scope': scope, 'is_exact_twr': False,
             'period': {k: v for k, v in period.items() if not k.startswith('_')},
             'intervals': public, 'intervals_omitted': omitted,
             'accuracy': _accuracy(scope, accuracy), 'coverage': coverage,
             'reason_codes': period['reason_codes'] if not available else [],
             'valuation_timezone': 'Asia/Seoul', **_tracked(scope, start, end, intervals, accuracy),
             **_effective_block(scope, effective, accuracy), **(extra or {})}
    block['explanation'] = _explain(block)
    return block


def _effective_block(scope, effective, accuracy):
    if effective is None:
        return {'effective_period': None, 'effective_period_reason_codes': ['INSUFFICIENT_NAV_SNAPSHOTS']}
    public = {k: v for k, v in effective.items() if not k.startswith('_')}
    public['label'] = (_label(scope, effective.get('_complete_flows', False), accuracy)
                       if effective['return_pct'] is not None else 'unavailable')
    return {'effective_period': public, 'effective_period_reason_codes': []}


def _tracked(scope, start, end, intervals, accuracy):
    """The linked return over the unbroken run of resolved intervals that ends at the latest resolved one.

    A separate, explicitly dated window, never the period's return: intervals tile the complete
    snapshots without gaps, so any unresolved interval (or one outside the period) ends the run, and
    no unresolved day is ever skipped inside it. After the last snapshot there is no interval, so the
    current day (MISSING_END_NAV) never breaks it.
    """
    first, last = _date(start), _date(end)

    def inside(row):
        a, b = _date(row['start_date']), _date(row['end_date'])
        return (first is None or a is not None and a >= first) and (last is None or b is not None and b <= last)
    run = []
    for row in reversed(intervals):
        if '_return' in row and inside(row):
            run.append(row)
        elif run:
            break
    if not run:
        return {'tracked': None, 'tracked_reason_codes': ['NO_RESOLVED_INTERVAL']}
    run.reverse()
    reasons = list(dict.fromkeys(code for row in run for code in row['reason_codes']))
    bounds = None
    if all(row['_bounds'] for row in run):
        low, high = link_bounds(row['_bounds'] for row in run)
        bounds = {'low': _pct(low), 'high': _pct(high)}
    else:
        reasons.append('SENSITIVITY_BOUNDS_UNAVAILABLE')
    return {'tracked': {'start_date': run[0]['start_date'], 'end_date': run[-1]['end_date'],
                        't0': run[0]['t0'], 't1': run[-1]['t1'], 'intervals': len(run),
                        'return_pct': _pct(link(row['_return'] for row in run)), 'sensitivity_bounds_pct': bounds,
                        'label': _label(scope, all(row['_complete_flows'] for row in run), accuracy),
                        'reason_codes': reasons},
            'tracked_reason_codes': []}


def _measured(accuracy):
    """Backtest accuracy in words: point estimates under stated assumptions, never an error bound."""
    if accuracy.get('status') == 'composite':
        parts = ', '.join(f"{scope} {entry.get('status', 'unmeasured').replace('_', ' ')}"
                          for scope, entry in (accuracy.get('components') or {}).items())
        return f"accuracy not measured as a whole (composite of {parts or 'no scope'}; label is the weakest part's)"
    if accuracy.get('status') == 'forward_pending':
        return (f"accuracy not yet measured (forward pending: {accuracy.get('basis', 'forward reconciliation')})"
                + (', dividends not modelled (a paid dividend counts as a deposit)'
                   if accuracy.get('dividends') == 'not_modelled' else '') + ', label always estimate')
    if accuracy.get('status') != 'measured':
        return 'accuracy unmeasured' + (f" ({accuracy['reason_codes'][0]})" if accuracy.get('reason_codes') else '')
    assumed = accuracy.get('assumptions')
    measured = (f"backtest p95 daily error {accuracy.get('p95_daily_error_bp')} bp"
                + (f" (point estimate assuming {', '.join(assumed)}; not an error bound)" if assumed else '')
                + f" on {accuracy.get('basis', 'replayed')} data")
    if 'max_abs_monthly_drift_bp' in accuracy:  # say which months the drift covers
        value, scored = accuracy['max_abs_monthly_drift_bp'], accuracy.get('months_scored')
        if accuracy.get('monthly_drift_basis') != 'months_with_every_interval_resolved':
            measured += f", max monthly drift {value} bp"
        elif value is None:
            measured += f", monthly drift unmeasured (none of {accuracy.get('months')} months had every interval resolved)"
        else:
            measured += (f", max monthly drift {value} bp over the {scored} of {accuracy.get('months')} months "
                         f"with every interval resolved")
        if accuracy.get('max_abs_monthly_drift_bp_resolved_subset') is not None:
            measured += f" ({accuracy['max_abs_monthly_drift_bp_resolved_subset']} bp on resolved days only, a diagnostic)"
    if accuracy.get('intervals_eligible') is not None and accuracy.get('intervals_total'):
        measured += (f", {accuracy['intervals_eligible']} of {accuracy['intervals_total']} intervals scored"
                     + (f", {accuracy['intervals_resolved']} resolved" if accuracy.get('intervals_resolved') is not None else ''))
    if accuracy.get('dividends') == 'not_modelled':
        measured += ', dividends not modelled (a paid dividend counts as a deposit)'
    if accuracy.get('sensitivity_not_bound'):
        measured += ', sampled timing/level sensitivities in accuracy.sensitivity_not_bound (sensitivity, not a bound)'
    if not accuracy.get('passed_bar'):
        measured += ', below the promotion bar'
    return measured


def _explain(block):
    text = _explain_period(block)
    if block['scope'] == 'kis' and 'KIS_CAPTURE_MISSING' in (block['period'].get('reason_codes') or []):
        text += (' KIS returns start at the first 18:10 KIS capture; days before it are not back-filled '
                 '(KIS_CAPTURE_MISSING).')
    effective = block.get('effective_period')
    if effective and effective['clipped'] and effective['return_pct'] is not None:
        text += (f" Over the stored range {effective['start']} to {effective['end']} (effective_period, the period "
                 f"clipped to its first and last complete snapshot): {'about ' if effective['label'] == 'estimate' else ''}"
                 f"{effective['return_pct']}% ({effective['label']}).")
    if block['scope'] in COMPOSITE_SCOPES:
        text += _consolidated_text(block)
    return text


def _consolidated_text(block):
    """How the consolidated number is built and which scopes are missing or excluded, in words."""
    def codes(entry):
        return ', '.join(entry['reason_codes'][:3]) or 'no reason recorded'
    included = block.get('included_scopes') or []
    name = 'Consolidated' if block['scope'] == 'consolidated' else 'Equity (Toss + KIS)'
    text = (f" {name} adds up the KRW NAV and KRW flows of {', '.join(included) or 'no scope'} and applies one "
            "Modified Dietz to the sums (each scope at its own FX marks); scope percentages are never averaged. "
            "An interval has a value only when every included scope has one for the same capture dates.")
    missing = block.get('missing_scopes') or []
    if missing:
        def where(count):
            return f" in {count} interval{'' if count == 1 else 's'}" if count else ' (no two complete snapshots)'
        text += ' Missing: ' + '; '.join(
            f"{entry['scope']}{where(entry['intervals_missing'])} ({codes(entry)})" for entry in missing) + '.'
    excluded = block.get('excluded_scopes') or []
    if excluded:
        text += ' Excluded as empty for the owner: ' + '; '.join(
            f"{entry['scope']} ({codes(entry)})" for entry in excluded) + '.'
    example = 'Toss to a bank to Binance' if block['scope'] == 'consolidated' else 'Toss to a bank to KIS'
    return text + (f' Money moved between scopes (e.g. {example}) is a withdrawal in one scope and '
                   'a deposit in another; it nets only when both land in the same interval.')


def _explain_period(block):
    scope, period, coverage = block['scope'], block['period'], block['coverage'] or {}
    measured = _measured(block['accuracy'])
    if block['label'] == 'unavailable':
        if not coverage.get('days_total') or not block['intervals'] and not block['intervals_omitted']:
            reasons = ', '.join(block['reason_codes'][:3]) or 'no data'
            return (f"UNAVAILABLE: no ledgerless return for {scope} {period['start']} to {period['end']} "
                    f"({reasons}); unknown flows are never treated as zero." + _tracked_text(block, reasons))
        top = ', '.join(f'{code} {days} d' for code, days in sorted(
            coverage['unresolved_days_by_reason'].items(), key=lambda kv: (-kv[1], kv[0]))[:3])
        return (f"UNAVAILABLE: no return for {scope} {period['start']} to {period['end']}: "
                f"{coverage['days_resolved']} of {coverage['days_total']} days resolved, "
                f"{coverage['days_unresolved']} unresolved ({top}). "
                "A return over only the resolved days is never given, because it would leave out the unresolved "
                "days; the per-interval (daily) estimates and coverage.unresolved_days show what is known and why "
                "the rest is not." + _tracked_text(block, f"{coverage['days_unresolved']} of its "
                                                          f"{coverage['days_total']} days are unresolved ({top})")
                + f" Unknown flows are never treated as zero; {measured}.")
    bounds = period.get('sensitivity_bounds_pct')
    spread = f" (flow-timing/FX range {bounds['low']}% to {bounds['high']}%)" if bounds else ''
    if block['label'] == 'verified':
        return (f"VERIFIED: linked Modified Dietz return for {scope} {period['start']} to {period['end']} is "
                f"{period['return_pct']}%{spread}; {measured}.")
    return (f"ESTIMATE, not verified: linked Modified Dietz return for {scope} {period['start']} to "
            f"{period['end']} is about {period['return_pct']}%{spread}; flows are inferred or incomplete; {measured}.")


def _tracked_text(block, because):
    """One sentence for an unavailable period that has a tracked window; empty otherwise."""
    tracked = block.get('tracked')
    if not tracked:
        return ''
    bounds, count = tracked['sensitivity_bounds_pct'], tracked['intervals']
    spread = f", flow-timing/FX range {bounds['low']}% to {bounds['high']}%" if bounds else ''
    return (f" Tracked since {tracked['start_date']} (to {tracked['end_date']}): "
            f"{'about ' if tracked['label'] == 'estimate' else ''}{tracked['return_pct']}% ({tracked['label']}{spread}) "
            f"over {count} consecutive resolved interval{'' if count == 1 else 's'} with no unresolved day between, "
            f"a separate dated window (tracked), not this period's return; the full period is unavailable "
            f"because {because}.")


def unavailable(scope, start, end, reasons, accuracy=None):
    accuracy = ACCURACY if accuracy is None else accuracy
    period = {'start': start, 'end': end, 't0': None, 't1': None, 'return_pct': None,
              'sensitivity_bounds_pct': None, 'xirr_pct': None, 'xirr_reason_codes': ['PERIOD_RETURN_UNAVAILABLE'],
              'reason_codes': list(reasons), 'intervals': 0}
    block = {'label': 'unavailable', 'method': METHOD, 'scope': scope, 'is_exact_twr': False, 'period': period,
             'intervals': [], 'intervals_omitted': 0,
             'accuracy': _accuracy(scope, accuracy) if scope in SUPPORTED_SCOPES else
             {'status': 'unmeasured', 'passed_bar': False, 'reason_codes': ['SCOPE_NOT_YET_SUPPORTED_LEDGERLESS'], 'bar': BAR},
             'coverage': None, 'reason_codes': list(reasons), 'valuation_timezone': 'Asia/Seoul',
             'tracked': None, 'tracked_reason_codes': ['NO_RESOLVED_INTERVAL'],
             'effective_period': None, 'effective_period_reason_codes': list(reasons)}
    block['explanation'] = _explain(block)
    return block


def _provider(name):
    module_name, function = PROVIDERS[name]
    try:
        return getattr(importlib.import_module(module_name), function)
    except (ImportError, AttributeError):
        return None


def load_records(db_path, scope, start, end, *, accounts=(), budget_seconds=None, providers=None,
                 schedule_provider=None):
    """Call the scope's flow provider within a bounded wait; never raise.

    The provider runs on a helper thread (context copied); past the budget the
    period is unresolved and the late result is discarded. Provider sentinel
    records without interval times become reasons, never intervals.
    """
    name = 'crypto' if scope == 'crypto' else 'kis' if scope == 'kis' else 'toss'
    function = (providers or {}).get(name) if providers is not None else _provider(name)
    if function is None:
        return None, ['FLOW_INFERENCE_UNAVAILABLE']
    kwargs = {}
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    budget = FLOW_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    if name == 'toss' and 'accounts' in parameters:
        kwargs['accounts'] = sorted(accounts)
    if 'budget_seconds' in parameters:
        kwargs['budget_seconds'] = budget
    if name == 'toss' and schedule_provider is not None and 'schedule_provider' in parameters:
        kwargs['schedule_provider'] = schedule_provider  # tests/replay only; the tool path models no dividends
    deadline = time.monotonic() + budget
    gate = _INFLIGHT[name]
    if not gate.acquire(timeout=max(0.0, budget)):
        return None, ['FLOW_INFERENCE_BUSY']
    outcome = {}

    def work():
        try:
            outcome['records'] = function(db_path, start, end, **kwargs)
        except Exception:  # noqa: BLE001 - provider failure is an unresolved period, never a number.
            outcome['failed'] = True
        finally:
            outcome['done'] = time.monotonic()
            gate.release()
    worker = threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True,
                              name='ledgerless-%s-flows' % name)
    try:
        worker.start()
    except RuntimeError:
        gate.release()
        return None, ['FLOW_INFERENCE_FAILED']
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive() or outcome.get('done', deadline + 1) > deadline:  # late results are discarded
        return None, ['FLOW_READ_BUDGET_EXCEEDED']
    records = outcome.get('records')
    if outcome.get('failed') or not isinstance(records, list):
        return None, ['FLOW_INFERENCE_FAILED']
    sentinels = [r for r in records if not isinstance(r, dict) or r.get('t0') is None or r.get('t1') is None]
    if sentinels:
        reasons = sorted({c for r in sentinels if isinstance(r, dict) for c in r.get('reasons') or [] if isinstance(c, str)})
        return None, reasons or ['FLOW_INFERENCE_FAILED']
    return records, []


def account_selection(db_path, navs, scope, *, budget_seconds=None):
    """(Toss account keys inside the scope, reasons).

    toss_investmcp uses the owner-export account identity, independently of flow
    coverage; without that identity it is unavailable, never all Toss accounts.
    """
    if scope == 'toss':
        return sorted({k for nav in navs for k in (nav.get('account_nav') or {}) if k.startswith('toss:')}), []
    if scope != 'toss_investmcp':
        return [], []
    from ledger_returns import investmcp_accounts
    return investmcp_accounts(db_path, navs, **({} if budget_seconds is None else {'budget_seconds': budget_seconds}))


def scope_accounts(db_path, navs, scope, *, budget_seconds=None):
    """Toss account keys inside the scope (empty when the selection is unavailable)."""
    return account_selection(db_path, navs, scope, budget_seconds=budget_seconds)[0]


def _exclusion(scoped, configured, scope, accounts):
    """Reason codes when a scope is genuinely empty for the owner, else None.

    Configuration decides, never a failed read: a scope configured absent (False, not
    unknown) is excluded unless a stored row of it is funded, complete or not: a non-zero
    NAV, or a holding or cash balance not proven zero (e.g. USDT whose FX mark failed).
    Real money is never dropped. A configured scope only when every stored point is a
    complete zero.
    """
    points = [_point(nav) for nav in scoped]
    funded = any(not p['reasons'] and p['value'] is not None and p['value'] != 0 for p in points) or \
        any(_required_keys(nav, scope, accounts)[0] for nav in scoped)
    if configured is False and not funded:
        return ['SCOPE_NOT_CONFIGURED']
    if points and all(not p['reasons'] and p['value'] == 0 for p in points):
        return ['ZERO_NAV_THROUGHOUT']
    return None


def _consolidated_returns(db_path, navs, start, end, *, configured, detail_level, budget_seconds, providers, accuracy,
                          schedule_provider, composite='consolidated'):
    """Each included scope's stored NAVs and flow records (its own provider, own budget), then consolidate()."""
    from ledger_returns import scope_navs
    parts, excluded = {}, {}
    for scope in COMPOSITE_SCOPES[composite]:
        accounts, selection_reasons = account_selection(db_path, navs, scope, budget_seconds=budget_seconds)
        scoped = scope_navs(navs, scope, account_references=accounts)
        why = _exclusion(scoped, (configured or {}).get(scope), scope, accounts)
        if why:
            excluded[scope] = why
            continue
        usable = sum(nav.get('total_value_krw') is not None and not nav.get('reason_codes') for nav in scoped)
        records, reasons = ([], []) if usable < 2 else load_records(
            db_path, scope, start, end, accounts=accounts, budget_seconds=budget_seconds, providers=providers,
            schedule_provider=schedule_provider)
        parts[scope] = (scoped, records or [], accounts, [*selection_reasons, *reasons])
    return consolidate(parts, start, end, excluded=excluded, accuracy=accuracy, detail_level=detail_level,
                       scope=composite)


def ledgerless_returns(db_path, navs, scope, start, end, *, account_references=None, detail_level='summary',
                       budget_seconds=None, providers: dict[str, Callable] | None = None,
                       accuracy: dict[str, Any] | None = None, schedule_provider: Callable | None = None,
                       configured: dict[str, bool | None] | None = None):
    """Tool entry: stored NAV versions + on-the-fly flow records -> block.

    ``navs`` are the stored daily representatives (or pinned versions) before any
    scope selection. Never raises; failures become ``unavailable`` + reasons.
    ``configured`` (consolidated and equity): scope -> True/False/None (unknown) from the
    server's configuration; only False can exclude a scope.
    """
    if scope not in SUPPORTED_SCOPES:
        return unavailable(scope, start, end, ['SCOPE_NOT_YET_SUPPORTED_LEDGERLESS'], accuracy)
    try:
        if scope in COMPOSITE_SCOPES:
            return _consolidated_returns(db_path, navs, start, end, configured=configured, detail_level=detail_level,
                                         budget_seconds=budget_seconds, providers=providers, accuracy=accuracy,
                                         schedule_provider=schedule_provider, composite=scope)
        from ledger_returns import scope_navs
        accounts, selection_reasons = (sorted(account_references), []) if account_references is not None else \
            account_selection(db_path, navs, scope, budget_seconds=budget_seconds)
        if selection_reasons:
            return unavailable(scope, start, end, selection_reasons, accuracy)
        scoped = scope_navs(navs, scope, account_references=accounts)
        usable = sum(nav.get('total_value_krw') is not None and not nav.get('reason_codes') for nav in scoped)
        records, reasons = ([], []) if usable < 2 else load_records(
            db_path, scope, start, end, accounts=accounts, budget_seconds=budget_seconds, providers=providers,
            schedule_provider=schedule_provider)
        # A KIS period opening before the first KIS capture says so (no back-fill); its
        # start NAV is missing either way, so this never changes availability.
        first = sorted(scoped, key=lambda nav: nav.get('snapshot_date') or '')[:1]
        if scope == 'kis' and first and 'KIS_CAPTURE_MISSING' in first[0]['reason_codes']:
            reasons = ['KIS_CAPTURE_MISSING', *reasons]
        return compute(scoped, records or [], scope, start, end, accounts=accounts, accuracy=accuracy,
                       detail_level=detail_level, provider_reasons=reasons)
    except Exception:  # noqa: BLE001 - additive block must never break the existing tool output.
        return unavailable(scope, start, end, ['LEDGERLESS_COMPUTATION_FAILED'], accuracy)
