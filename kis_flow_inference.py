"""Guarded KIS cash residuals, computed from the stored 18:10 KIS capture alone.

Pure replay API: ``infer_kis_flows(snapshots, start=None, end=None, accounts=None)``;
no I/O. ``kis_interval_flows(db_path, start, end, accounts=None)`` reads the stored
daily NAV versions read-only (payload key ``kis_capture``, kis_nav_capture.py) and
returns per-interval flow records in the shared flow-record shape
(interval_returns.validate_record): source ``kis``, method ``cash_residual``. KIS
publishes no deposit/withdrawal history, so every flow here is inferred.
All monetary/quantity inputs and outputs are finite decimal strings.

snapshots: one dict per KIS account and stored version, with ``account``
(``kis:<label>``), ``snapshot_id``, ``version``, ``time`` (the scope instant,
``kis_capture.observed_at``), ``capture`` (that account's capture entry) and
``reasons``. Intervals use consecutive complete account captures, (t0,t1],
bridging missing or incomplete middle captures without interpolating; an
incomplete latest capture yields unresolved diagnostic rows. start/end select
interval END times; ISO dates mean inclusive KST dates.

Cash basis, chosen so trade effects land in the same interval as the trade:
domestic KRW is D+2 cash ``prvs_rcdl_excc_amt``; overseas cash per currency is
CTRP6504R ``frcr_dncl_amt_2`` (on a day with unsettled trades, only the reading
of it that KIS's own ``frcr_evlu_tota`` confirms at capture, kis_nav_capture
``_foreign_cash``; else null). Both are projected at execution,
like the execution-basis holdings, so a fill moves securities and this cash in
the first capture that lists it, whatever its settlement date (KRX T+2, US T+1).
T+0 cash (``dnca_tot_amt``) would move only at settlement and leave every trade
as a residual in a later interval.

    residual = cash delta - trade cash effect          (per currency, native)

* Domestic effect: TTTC0081R fills new at t1, keyed by order date and number,
  cumulative quantity/amount differenced against t0 (+ sells - buys at
  ``tot_ccld_amt``), minus TTTC8715R fee + tl_tax per trade date differenced the
  same way. A fill or cost that shrinks or disappears is a revision: unresolved.
* Overseas effect: CTOS4001R rows new at t1 (a multiset of the trade fields; the
  KRW estimate ``wcrc_excc_amt``/``erlm_exrt`` is excluded, it changes until the day
  after settlement), + sells - buys at ``frcr_excc_amt_1`` in ``crcy_cd``.
* The t1 capture's trade window must start on or before t0's KST date
  (KIS_TRADE_WINDOW_GAP). Quantities reconcile exactly per symbol: position delta
  = new fills (KIS_DOMESTIC_/KIS_OVERSEAS_QUANTITY_UNRECONCILED); a US symbol also
  gets KIS_US_TRADE_TIMING_SUSPECTED, since CTRP6504R may miss US trades made after
  about 18:10 KST. A capture that saw US fills on its own US date is incomplete
  already (KIS_US_FILLS_NEAR_CAPTURE), so it is never an endpoint.

Classification mirrors toss_flow_inference with its thresholds:

* R3a: when exactly two currencies have residuals above 5,000 KRW (closing KIS
  mark) with opposite signs, their KRW values must agree within 2.5% at both
  endpoint marks: an FX conversion (fx_leg, external False,
  FX_CONVERSION_ESTIMATED_WITHIN_BAND); else FX_RATE_OUTSIDE_BAND. Opposite residuals
  above 5,000 KRW in three or more currencies cannot be paired uniquely:
  KIS_CROSS_CURRENCY_RESIDUAL_UNEXPLAINED. Smaller legs fall to R8, which books the
  conversion cost of both legs as income.
* Unsettled revision: if KIS moved the KRW estimate of already-listed overseas
  trades by more than 5,000 KRW (or a revision cannot be attributed) and an
  unpaired KRW residual exceeds 5,000 KRW, that residual may be FX P&L, not a
  flow: KIS_UNSETTLED_REVISION_UNEXPLAINED.
* R6: an inferred foreign flow with abs(residual) x closing mark x 2.5% > 20,000
  KRW is unresolved (<CCY>_FLOW_FX_VALUATION_UNSEPARATED).
* R8: a residual above rounding but at most 5,000 KRW is income_other
  (SMALL_RESIDUAL_TREATED_AS_INCOME), never a flow.
* Otherwise a residual above rounding is a conditional deposit/withdrawal with
  unknown timing; within rounding (1 KRW, 1 JPY, 0.01 otherwise) it is zero.

A problem in one currency blocks all of the account's currencies
(CROSS_CURRENCY_RESIDUAL_UNRESOLVED): it can hide a conversion. Dividends and
interest are not modelled (as for Toss): such cash is part of
the residual, an inferred flow or R8 income, and every computed record carries
DIVIDENDS_COUNTED_AS_FLOWS. A currency a complete capture does not list is zero
there (KIS lists every currency it holds), including a currency held only as
securities. Every resolved outcome is conditional, never observed or verified.

* Closed cash row: a foreign currency whose t0 cash was read while trades were
  unsettled (``overseas.cash_check.unsettled_trades``) and whose t1 capture has no
  cash row (``cash_basis`` ``no_cash_row_zero``) has a residual nothing explains:
  it can be an integrated-margin settlement conversion, not an external flow (the
  10/18 -> 10/20 USD row). Unresolved (KIS_CASH_ROW_CLOSED_AFTER_SETTLEMENT), so
  it blocks the account's interval, until KIS or the owner confirms the mechanism.

Limits: no inferred flow can be checked against flow truth (KIS has no
deposit/withdrawal endpoint); scripts/kis_forward_accuracy.py compares returns
with KIS's own reported P&L instead. Fees and taxes are assumed to be in the
projected cash when the fill is listed; if KIS books them later they appear as
small residuals (R8). No database writes, migrations or broker calls.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from kis_nav_capture import CAPTURE_VERSION, US_EXCHANGES
from ledger_returns import ReadBudgetExceeded, Reader

ALGORITHM = 'kis-residual-v1'
TOLERANCE = {'KRW': Decimal('1'), 'JPY': Decimal('1')}
DEFAULT_TOLERANCE = Decimal('.01')
FX_BAND = Decimal('.025')
FOREIGN_FLOW_FX_BUDGET_KRW = Decimal('20000')
SMALL_RESIDUAL_KRW = Decimal('5000')
DIVIDENDS_AS_FLOWS = 'DIVIDENDS_COUNTED_AS_FLOWS'
ADVISORIES = {'FX_CONVERSION_ESTIMATED_WITHIN_BAND', 'SMALL_RESIDUAL_TREATED_AS_INCOME', DIVIDENDS_AS_FLOWS}
# Quantity sign of a fill; its cash sign is the opposite (a buy spends).
SIDES = {'01': -1, '02': 1}
SETTLEMENT_KEY = ('trad_dt', 'sttl_dt', 'sll_buy_dvsn_cd', 'pdno', 'ccld_qty', 'ft_ccld_unpr2', 'tr_frcr_amt2',
                  'frcr_excc_amt_1', 'crcy_cd')
# Stored representatives read before the requested start to find each account's
# opening anchor; then up to ANCHOR_STEPS earlier batches of ANCHOR_BATCH rows.
ANCHOR_LOOKBACK_DAYS = 14
ANCHOR_BATCH = 16
ANCHOR_STEPS = 4
READ_BUDGET_SECONDS = 2.0
KST = ZoneInfo('Asia/Seoul')
ZERO = Decimal(0)
_DAY = re.compile(r'^\d{8}$')


def _decimal(value):
    if not isinstance(value, str) or len(value) > 100 or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise ValueError('INVALID_DECIMAL_INPUT')
    return Decimal(value)


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


def _tolerance(currency):
    return TOLERANCE.get(currency, DEFAULT_TOLERANCE)


def _quality(snapshot):
    """Why this capture cannot be an interval endpoint (empty when it can)."""
    reasons = list(snapshot.get('reasons') or [])
    capture = snapshot.get('capture')
    if not isinstance(capture, dict):
        return sorted(set(reasons + ['KIS_CAPTURE_MISSING']))
    if capture.get('status') != 'complete' or (capture.get('nav_krw') or {}).get('value') is None:
        reasons.extend(['KIS_CAPTURE_INCOMPLETE', *(capture.get('reasons') or [])])
    cash = capture.get('cash_native')
    if not isinstance(cash, dict) or 'KRW' not in cash or any(
            not isinstance(m, dict) or m.get('value') is None for m in cash.values()):
        reasons.append('KIS_CASH_UNAVAILABLE')
    return sorted(set(reasons))


def _cash(capture, currency):
    metric = capture['cash_native'].get(currency)
    return ZERO if metric is None else _decimal(metric['value'])


def _rate(capture, currency):
    if currency == 'KRW':
        return Decimal(1)
    value = ((capture.get('fx') or {}).get(currency) or {}).get('rate_to_krw')
    try:
        rate = _decimal(value)
    except ValueError:
        return None
    return rate if rate > 0 else None


def _window(capture):
    window = (capture.get('trades') or {}).get('window')
    if isinstance(window, list) and len(window) == 2 and all(isinstance(d, str) and _DAY.match(d) for d in window):
        return window
    return None


def _domestic(c0, c1, floor):
    """(KRW cash effect, signed quantities per symbol, fill ids, reasons) of trades new at c1."""
    t0, t1 = c0.get('trades') or {}, c1.get('trades') or {}
    reasons = []
    if t0.get('domestic_fills') is None or t1.get('domestic_fills') is None:
        reasons.append('KIS_DOMESTIC_FILLS_UNAVAILABLE')
    if t0.get('domestic_costs') is None or t1.get('domestic_costs') is None:
        reasons.append('KIS_DOMESTIC_COSTS_UNAVAILABLE')
    if reasons:
        return None, None, [], reasons
    before = {(r['ord_dt'], r['odno']): r for r in t0['domestic_fills'] if r['ord_dt'] >= floor}
    after = {(r['ord_dt'], r['odno']): r for r in t1['domestic_fills'] if r['ord_dt'] >= floor}
    effect, quantities, ids = ZERO, {}, []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if new is None or old is not None and (old['pdno'], old['sll_buy_dvsn_cd']) != (new['pdno'], new['sll_buy_dvsn_cd']):
            reasons.append('KIS_DOMESTIC_FILL_REVISED')
            continue
        quantity = _decimal(new['tot_ccld_qty']) - (_decimal(old['tot_ccld_qty']) if old else ZERO)
        amount = _decimal(new['tot_ccld_amt']) - (_decimal(old['tot_ccld_amt']) if old else ZERO)
        if quantity < 0 or amount < 0:
            reasons.append('KIS_DOMESTIC_FILL_REVISED')
            continue
        if quantity == 0 and amount == 0:
            continue
        sign = SIDES[new['sll_buy_dvsn_cd']]
        quantities[new['pdno']] = quantities.get(new['pdno'], ZERO) + sign * quantity
        effect -= sign * amount
        ids.append(':'.join(key))
    costs0, costs1 = t0['domestic_costs'], t1['domestic_costs']
    for day in sorted(d for d in set(costs0) | set(costs1) if d >= floor):
        old, new = costs0.get(day), costs1.get(day)
        if new is None:
            reasons.append('KIS_DOMESTIC_COSTS_REVISED')
            continue
        cost = _decimal(new['fee']) + _decimal(new['tl_tax'])
        if old is not None:
            cost -= _decimal(old['fee']) + _decimal(old['tl_tax'])
        if cost < 0:
            reasons.append('KIS_DOMESTIC_COSTS_REVISED')
            continue
        effect -= cost
    return effect, quantities, ids, reasons


def _overseas(c0, c1, floor):
    """(effects per currency, signed quantities, revision facts, reasons) of rows new at c1."""
    rows0 = (c0.get('trades') or {}).get('overseas_settlements')
    rows1 = (c1.get('trades') or {}).get('overseas_settlements')
    if rows0 is None or rows1 is None:
        return None, None, None, ['KIS_OVERSEAS_SETTLEMENTS_UNAVAILABLE']
    groups0, groups1 = {}, {}
    for rows, groups in ((rows0, groups0), (rows1, groups1)):
        for row in rows:
            if row['trad_dt'] >= floor:
                groups.setdefault(tuple(row[k] for k in SETTLEMENT_KEY), []).append(_decimal(row['wcrc_excc_amt']))
    count0 = Counter({k: len(v) for k, v in groups0.items()})
    count1 = Counter({k: len(v) for k, v in groups1.items()})
    reasons = ['KIS_OVERSEAS_SETTLEMENT_REVISED'] if count0 - count1 else []
    effects, quantities, added = {}, {}, count1 - count0
    for key, n in added.items():
        row = dict(zip(SETTLEMENT_KEY, key))
        sign, currency = SIDES[row['sll_buy_dvsn_cd']], row['crcy_cd'].upper()
        quantity, settled = quantities.get(row['pdno'], (ZERO, currency))
        if settled != currency:
            reasons.append('KIS_OVERSEAS_POSITION_CURRENCY_CONFLICT')
        quantities[row['pdno']] = (quantity + sign * _decimal(row['ccld_qty']) * n, currency)
        effects[currency] = effects.get(currency, ZERO) - sign * _decimal(row['frcr_excc_amt_1']) * n
    # KRW settlement estimates of rows already listed at t0: moved amount, or unattributable.
    revision, unattributed = ZERO, False
    for key in set(groups0) & set(groups1):
        if count0[key] == count1[key]:
            revision += abs(sum(groups1[key], ZERO) - sum(groups0[key], ZERO))
        elif sorted(groups0[key]) != sorted(groups1[key])[:count0[key]]:
            unattributed = True
    facts = {'krw_estimate_revision': revision, 'unattributed': unattributed, 'rows_new': sum(added.values())}
    return effects, quantities, facts, reasons


def _reconcile(c0, c1, domestic_quantities, overseas_quantities, overseas):
    reasons = []
    positions0, positions1 = c0.get('positions') or {}, c1.get('positions') or {}
    if domestic_quantities is not None:
        held0, held1 = positions0.get('domestic') or {}, positions1.get('domestic') or {}
        for symbol in set(held0) | set(held1) | set(domestic_quantities):
            change = _decimal(held1.get(symbol, '0')) - _decimal(held0.get(symbol, '0'))
            if change != domestic_quantities.get(symbol, ZERO):
                reasons.append('KIS_DOMESTIC_QUANTITY_UNRECONCILED')
    if overseas and overseas_quantities is not None:
        held0, held1 = positions0.get('overseas') or {}, positions1.get('overseas') or {}
        for symbol in set(held0) | set(held1) | set(overseas_quantities):
            change = (_decimal((held1.get(symbol) or {}).get('quantity', '0'))
                      - _decimal((held0.get(symbol) or {}).get('quantity', '0')))
            filled, currency = overseas_quantities.get(symbol, (ZERO, None))
            if change != filled:
                reasons.append('KIS_OVERSEAS_QUANTITY_UNRECONCILED')
                position = held1.get(symbol) or held0.get(symbol) or {}
                if (position.get('exchange') or '').upper() in US_EXCHANGES or not position and currency == 'USD':
                    reasons.append('KIS_US_TRADE_TIMING_SUSPECTED')
    return reasons


def _record(s0, s1, account, currency):
    return {'source': 'kis', 'account': account, 'currency': currency, 't0': _iso(s0['time']),
            't1': _iso(s1['time']), 'start_snapshot_id': s0.get('snapshot_id'), 'end_snapshot_id': s1.get('snapshot_id'),
            'flow_native': None, 'income_native': None, 'events': [], 'timing': 'unknown',
            'method': 'cash_residual', 'confidence': 'unresolved', 'reasons': [],
            'inputs': {'snapshot_versions': [s0.get('version'), s1.get('version')], 'fill_ids': [],
                       'settlement_rows_new': 0, 'trade_window': None, 'algorithm': ALGORITHM},
            'diagnostics': {'cash_delta_native': None, 'trade_cash_effect_native': None,
                            'matched_income_native': None, 'residual_native': None, 'classification': 'unresolved'}}


def _blocked(record):
    return record['income_native'] is None or any(reason not in ADVISORIES for reason in record['reasons'])


def _interval(s0, s1):
    account, c0, c1 = s0['account'], s0['capture'], s1['capture']
    reasons = [*_quality(s0), *_quality(s1)]
    overseas_scope = bool(c0.get('supports_overseas', True) or c1.get('supports_overseas', True))
    # '*': no trade effect is known for currencies other than those listed.
    floor, effects, ids, revision = None, {'KRW': None, '*': None}, [], None
    window0, window1 = _window(c0), _window(c1)
    if window0 is None or window1 is None:
        reasons.append('KIS_TRADE_WINDOW_UNAVAILABLE')
    elif window1[0] > _time(s0['time']).astimezone(KST).strftime('%Y%m%d'):
        reasons.append('KIS_TRADE_WINDOW_GAP')
    else:
        floor = window1[0]
        krw, domestic_quantities, ids, trade_reasons = _domestic(c0, c1, floor)
        effects['KRW'] = krw
        overseas_quantities = {}
        if overseas_scope:
            foreign, overseas_quantities, revision, found = _overseas(c0, c1, floor)
            trade_reasons.extend(found)
            if foreign is not None:
                effects.update(foreign)
                effects.pop('*')
        else:
            effects.pop('*')
        if not trade_reasons:
            trade_reasons.extend(_reconcile(c0, c1, domestic_quantities, overseas_quantities, overseas_scope))
        reasons.extend(trade_reasons)
    # Every currency the engine requires (cash or NAV at either end) gets a record.
    held = [set(c.get(group) or {}) for c in (c0, c1) for group in ('cash_native', 'nav_native')]
    currencies = ['KRW'] + sorted((set().union(*held) | {c for c in effects if c != '*'}) - {'KRW'})
    records = [_record(s0, s1, account, c) for c in currencies]
    reasons = sorted(set(reasons))
    for record in records:
        c = record['currency']
        record['reasons'] = list(reasons)
        record['inputs'].update(fill_ids=ids, trade_window=floor,
                                settlement_rows_new=revision['rows_new'] if revision else 0)
        try:
            delta = _cash(c1, c) - _cash(c0, c)
            record['diagnostics']['cash_delta_native'] = _money(delta)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        effect = effects[c] if c in effects else None if '*' in effects else ZERO
        if effect is None:
            continue
        record['diagnostics']['trade_cash_effect_native'] = _money(effect)
        record['diagnostics']['residual_native'] = _money(delta - effect)
        if reasons:
            continue
        # Dividends/interest are not modelled; they stay in the residual.
        record['reasons'].append(DIVIDENDS_AS_FLOWS)
        record['income_native'] = record['diagnostics']['matched_income_native'] = '0'
    pending = ((c0.get('overseas') or {}).get('cash_check') or {}).get('unsettled_trades') is True
    for record in records:
        c, residual = record['currency'], record['diagnostics']['residual_native']
        if (pending and c != 'KRW' and residual is not None and abs(_decimal(residual)) > _tolerance(c)
                and (c1.get('cash_basis') or {}).get(c) == 'no_cash_row_zero'
                and (c0.get('cash_basis') or {}).get(c) not in (None, 'no_cash_row_zero')):
            record['reasons'].append('KIS_CASH_ROW_CLOSED_AFTER_SETTLEMENT')
    if any(_blocked(r) for r in records):
        return _cross_blocked(records)
    residual = {r['currency']: _decimal(r['diagnostics']['residual_native']) for r in records}
    marks = {c: (_rate(c0, c), _rate(c1, c)) for c in residual}
    for record in records:
        c = record['currency']
        if abs(residual[c]) > _tolerance(c) and marks[c][1] is None:
            record['reasons'].append('KIS_FX_RATE_UNAVAILABLE')
    if any(_blocked(r) for r in records):
        return _cross_blocked(records)
    value = {c: abs(residual[c]) * marks[c][1] for c in residual if abs(residual[c]) > _tolerance(c)}
    large = [c for c in value if value[c] > SMALL_RESIDUAL_KRW]
    by_currency = {r['currency']: r for r in records}
    if len(large) == 2 and residual[large[0]] * residual[large[1]] < 0:
        a, b = large
        bands = [abs((abs(residual[a]) * marks[a][i]) / (abs(residual[b]) * marks[b][i]) - 1)
                 for i in (0, 1) if marks[a][i] is not None and marks[b][i] is not None]
        if bands and all(band <= FX_BAND for band in bands):
            pair = f"fx:{s0['snapshot_id']}:{s1['snapshot_id']}:{account}:{a}:{b}"
            for c in (a, b):
                record = by_currency[c]
                record.update(flow_native='0', confidence='conditional')
                record['events'].append({'time': None, 'amount_native': _money(residual[c]), 'kind': 'fx_leg',
                                         'external': False, 'pair_id': pair})
                record['diagnostics']['classification'] = 'fx_conversion'
                record['reasons'].append('FX_CONVERSION_ESTIMATED_WITHIN_BAND')
        else:
            for record in records:
                record['reasons'].append('FX_RATE_OUTSIDE_BAND')
    elif len(large) > 2 and len({residual[c] > 0 for c in large}) > 1:
        for record in records:
            record['reasons'].append('KIS_CROSS_CURRENCY_RESIDUAL_UNEXPLAINED')
    krw = by_currency['KRW']
    if (krw['flow_native'] is None and value.get('KRW', ZERO) > SMALL_RESIDUAL_KRW and revision is not None
            and (revision['krw_estimate_revision'] > SMALL_RESIDUAL_KRW or revision['unattributed'])):
        krw['reasons'].append('KIS_UNSETTLED_REVISION_UNEXPLAINED')
    if revision is not None:
        krw['inputs']['krw_estimate_revision'] = _money(revision['krw_estimate_revision'])
    for record in records:
        c = record['currency']
        if c == 'KRW' or record['flow_native'] is not None or c not in value:
            continue
        # R6 bounds the mark/spread error of an inferred foreign external flow.
        bound = value[c] * FX_BAND
        record['inputs'].update(foreign_flow_fx_error_bound_krw=_money(bound),
                                foreign_flow_fx_error_budget_krw=_money(FOREIGN_FLOW_FX_BUDGET_KRW),
                                fx_band=_money(FX_BAND))
        if bound > FOREIGN_FLOW_FX_BUDGET_KRW:
            record['reasons'].append(f'{c}_FLOW_FX_VALUATION_UNSEPARATED')
    if any(_blocked(r) for r in records):
        return _cross_blocked(records)
    for record in records:
        if record['flow_native'] is not None:
            continue
        c = record['currency']
        amount = residual[c]
        if c in value and value[c] <= SMALL_RESIDUAL_KRW:
            # R8, only after the FX-pair, revision and valuation gates.
            record['income_native'] = _money(amount)
            record['events'].append({'time': None, 'amount_native': _money(amount), 'kind': 'income_other',
                                     'external': False})
            record['reasons'].append('SMALL_RESIDUAL_TREATED_AS_INCOME')
            record['diagnostics'].update(matched_income_native=record['income_native'], residual_native='0')
            amount = ZERO
        if abs(amount) <= _tolerance(c):
            record.update(flow_native='0', confidence='conditional')
            record['diagnostics']['classification'] = 'income_other' if record['events'] else 'zero'
        else:
            kind = 'deposit' if amount > 0 else 'withdrawal'
            record.update(flow_native=_money(amount), confidence='conditional')
            record['events'].append({'time': None, 'amount_native': _money(amount), 'kind': kind, 'external': True})
            record['diagnostics']['classification'] = kind
    return records


def _cross_blocked(records):
    """A problem in one currency can hide a conversion in another: block all of them."""
    if any(not _blocked(r) for r in records):
        for record in records:
            record['reasons'].append('CROSS_CURRENCY_RESIDUAL_UNRESOLVED')
    for record in records:
        record.update(flow_native=None, confidence='unresolved', events=[])
        record['reasons'] = list(dict.fromkeys(record['reasons']))
        record['diagnostics']['classification'] = 'unresolved'
    return records


def infer_kis_flows(snapshots, *, start=None, end=None, accounts=None):
    """Pure algorithm described in the module docstring; never mutates inputs."""
    lo, hi = _bound(start), _bound(end, end=True)
    if lo > hi:
        raise ValueError('INVALID_INTERVAL_RANGE')
    selected = set(accounts) if accounts is not None else {s['account'] for s in snapshots}
    records = []
    with localcontext() as ctx:
        ctx.prec = 80
        for account in sorted(selected):
            ordered = sorted((s for s in snapshots if s.get('account') == account and _time(s['time']) <= hi),
                             key=lambda s: _time(s['time']))
            if len({_time(s['time']) for s in ordered}) != len(ordered):
                raise ValueError('AMBIGUOUS_SNAPSHOT_VERSIONS')
            anchor, pairs = None, []
            for snapshot in ordered:
                if _quality(snapshot):
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
                if lo < _time(s1['time']) <= hi:
                    records.extend(_interval(s0, s1))
    return records


def kis_snapshots(nav, accounts=None):
    """Per-account snapshot dicts of one stored NAV version (empty without a KIS capture)."""
    capture = nav.get('kis_capture')
    if not isinstance(capture, dict) or not isinstance(capture.get('accounts'), dict):
        return []
    try:
        _time(capture.get('observed_at'))
    except (AttributeError, TypeError, ValueError):
        return []
    reasons = [] if capture.get('version') == CAPTURE_VERSION else ['KIS_CAPTURE_VERSION_UNSUPPORTED']
    return [{'account': key, 'snapshot_id': nav.get('snapshot_id'), 'version': nav.get('version'),
             'time': capture['observed_at'], 'capture': entry, 'reasons': list(reasons)}
            for key, entry in sorted(capture['accounts'].items())
            if key.startswith('kis:') and (accounts is None or key in accounts)]


def _stored(row):
    capture = row['kis_capture']
    return {'snapshot_id': row['snapshot_id'], 'version': row['version'],
            'kis_capture': json.loads(capture) if isinstance(capture, str) else None}


def _read_inputs(db_path, start, end, accounts):
    lo, hi = _bound(start), _bound(end, end=True)
    if lo > hi or lo.year < 2000 or hi.year > 2100:
        raise ValueError('INVALID_INTERVAL_RANGE')
    floor = (lo - timedelta(days=ANCHOR_LOOKBACK_DAYS)).astimezone(KST).date().isoformat()
    ceiling = hi.astimezone(KST).date().isoformat()
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        reader = Reader(conn, time.monotonic() + READ_BUDGET_SECONDS)
        # Only each version's KIS capture is decoded; the rest of the payload stays in SQLite.
        nav_sql = """SELECT r.snapshot_date,v.snapshot_id,v.version,
                            json_extract(v.payload_json,'$.kis_capture') AS kis_capture
                     FROM nav_snapshot_representatives r JOIN nav_snapshot_versions v USING(snapshot_id) """
        rows = reader.rows(nav_sql + 'WHERE r.snapshot_date >= ? AND r.snapshot_date <= ? ORDER BY r.snapshot_date',
                           (floor, ceiling))
        snapshots = [s for r in rows for s in kis_snapshots(_stored(r), accounts)]
        # Seek back for an opening complete capture of each account (bounded batches).
        cursor = rows[0]['snapshot_date'] if rows else floor
        names = set(accounts) if accounts is not None else {s['account'] for s in snapshots}
        for _ in range(ANCHOR_STEPS):
            opened = {s['account'] for s in snapshots if not _quality(s) and _time(s['time']) <= lo}
            if not names - opened:
                break
            previous = reader.rows(nav_sql + 'WHERE r.snapshot_date < ? ORDER BY r.snapshot_date DESC LIMIT ?',
                                   (cursor, ANCHOR_BATCH))
            if not previous:
                break
            cursor = previous[-1]['snapshot_date']
            earlier = [s for r in reversed(previous) for s in kis_snapshots(_stored(r), accounts)]
            if not earlier:
                break  # before the first KIS capture: nothing older to find
            snapshots = earlier + snapshots
    return snapshots


def _unavailable(accounts, reason):
    return [{'source': 'kis', 'account': a, 'currency': 'KRW', 't0': None, 't1': None,
             'start_snapshot_id': None, 'end_snapshot_id': None, 'flow_native': None, 'income_native': None,
             'events': [], 'timing': 'unknown', 'method': 'cash_residual', 'confidence': 'unresolved',
             'reasons': [reason], 'inputs': {}, 'diagnostics': {'classification': 'unresolved',
             'cash_delta_native': None, 'trade_cash_effect_native': None, 'matched_income_native': None,
             'residual_native': None}}
            for a in (accounts or ['kis:unknown'])]


def kis_interval_flows(db_path, start, end, accounts=None):
    """Read the stored KIS captures read-only and infer per-interval flow records.

    Missing DB/captures produce unresolved sentinel records (t0/t1 None) that the
    returns engine turns into reasons; stored history without a KIS capture is never
    back-filled.
    """
    try:
        snapshots = _read_inputs(db_path, start, end, accounts)
    except (sqlite3.Error, ReadBudgetExceeded, OSError, KeyError, TypeError, ValueError):
        return _unavailable(accounts, 'LOCAL_KIS_EVIDENCE_UNAVAILABLE')
    if not snapshots:
        return _unavailable(accounts, 'KIS_CAPTURE_MISSING')
    try:
        return infer_kis_flows(snapshots, start=start, end=end, accounts=accounts)
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
        return _unavailable(accounts, 'INVALID_KIS_INFERENCE_INPUT')


def kis_residual_diagnostics(db_path, start, end):
    """Per-currency diagnostics and counts of ACCOUNT intervals (not row counts)."""
    records = kis_interval_flows(db_path, start, end)
    groups, rows = {}, []
    for record in records:
        key = (record['account'], record['t0'], record['t1'])
        if record['t0'] is not None and record['t1'] is not None:
            groups[key] = groups.get(key, True) and record['confidence'] != 'unresolved'
        rows.append({k: record[k] for k in ('account', 'currency', 't0', 't1', 'reasons', 'inputs')} | record['diagnostics'])
    return {'rows': rows, 'reasons': sorted({r for record in records for r in record['reasons']}) if records else ['NO_KIS_INTERVALS'],
            'counts': {'intervals_total': len(groups), 'intervals_classified': sum(groups.values()),
                       'intervals_unresolved': len(groups) - sum(groups.values())}}
