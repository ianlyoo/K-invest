"""KIS ledgerless capture for the scheduled 18:10 KST snapshot (payload key ``kis_capture``).

Per configured KIS account, read-only TRs pass kis_api's strict contract
(``validate_kis_response``) and are stored as finite decimal strings. A failed
or invalid read leaves the fields that depend on it null with a reason code; it
never becomes zero and never blocks the Toss/crypto snapshot (scripts/snapshot_nav.py
runs this last, in isolation, and stores only this key).

NAV and cash bases (KIS API portal field lists):

* Domestic NAV is TTTC8434R ``output2.tot_evlu_amt`` (AFHR_FLPR_YN=N) =
  securities + D+2 cash ``prvs_rcdl_excc_amt``. Holdings
  include today's executions and D+2 cash already reflects their T+2 settlement,
  so a fill moves securities and cash in the same snapshot: trade cash effects
  belong to the interval of the fill, not of its settlement date. That is why
  the domestic cash basis is D+2 cash (``dnca_tot_amt``, T+0, would move only
  at settlement). The identity tot = scts_evlu_amt + D+2 cash is checked; loans
  are not modelled (tot_loan_amt > 0 nulls the NAV).
* Overseas NAV per currency is CTRP6504R (execution basis) read on the foreign
  basis (02): output1 ``frcr_evlu_amt2`` summed by ``buy_crcy_cd`` plus output2
  ``frcr_dncl_amt_2``. On 02, output3 is a raw sum across currencies (live 10/17),
  so output3 (KRW totals, ``ustl_*``, truth ``evlu_pfls_amt_smtl``) comes from a
  second read on the KRW basis (01), whose rows must list the same products.
  When output2 reports unsettled buy/sell totals, ``frcr_dncl_amt_2`` has two
  unproven readings: already net of the day's executions (KIS field list)
  or, the reference assumption, excluding them (cash = deposit + sells - buys,
  offered only when output3 ``ustl_*`` agree). The foreign cash then counts only
  when one reading is consistent with KIS's own KRW figure, output3 ``frcr_evlu_tota``,
  at the NAV marks within 1 bp (+ 1 KRW per currency); else those currencies are null
  (KIS_FX_CASH_CROSS_CHECK_FAILED / KIS_FX_CASH_UNSETTLED_UNVERIFIED). Consistency
  is all it shows (``decided_by: consistent_with_kis_frcr_evlu_tota``): KIS likely
  derives that figure from the same cash field, so it cannot say which reading is
  right; scripts/kis_forward_accuracy.py flags the pattern that would. With no
  unsettled trade the check is still computed and a gap beyond it is the
  non-nulling advisory KIS_FX_CASH_CROSS_CHECK_MISMATCH (account ``advisories``,
  shown in kis_capture_status). A currency whose cash fields are all zero needs no
  rate: it counts as 0. The check, its gaps and the reading it decided are stored
  (``overseas.cash_check``); live 10/17 data (USD buy and sell unsettled) did not
  record which reading held.
  KRW values use KIS's own valuation rate, the scope's marks: the rows' single
  ``bass_exrt`` per currency (JPY is quoted per 100 units), else the posted
  ``frst_bltn_exrt`` for per-unit currencies; the KRW-basis ``evlu_amt_smtl``
  must agree with the foreign rows at that rate (live 10/17: 0 bp per row).
* CTRP6548R ``tot_asst_amt`` (settlement basis) is one optional call, stored only.

Price time (KIS returns none): each domestic holding's TTTC8434R price is accepted
only when it matches a price whose time is known, else the account's price time is
null with a reason. Accepted: the KRX close of the newest session (FHKST01010400
market J, ``stck_bsop_date``; 15:30 KST, ``krx_regular_close``). AFHR_FLPR_YN=N prices
NXT-traded stocks at NXT (observed 10/17: 25-69 bp off
the KRX close); such a price is matched to NXT's daily row for that session (market
NX): once NXT has ended, its close (20:00 KST, ``nxt_after_market_close``); while NXT
is open, its current price within one tick, stamped at the balance read
(``nxt_after_market_at_read``). KIS posts NXT's close late: until then the newest NX
row's close repeats the KRX close while its open, range and volume are NXT's own
(live Sun 10/18 01:29 for Friday 10/16; older rows carry NXT's close). A price
inside that row's low-high is then NXT's last trade (``*_in_range`` bases, counted
as ``nxt_in_range``), unless NX already lists a newer session than KRX's newest (a
catch-up run before KRX opens: the price may be that session's NXT pre-market trade;
KIS_NXT_SESSION_AHEAD_OF_KRX). Anything else is KIS_NXT_PRICE_NOT_IDENTIFIED.
Overseas closes are dated by HHDFS76240000 ``xymd`` of each market group's largest
holding, accepted only on an exact price match among the two newest sessions,
named for the row matched (``us_regular_close`` newest, ``*_previous_session``;
live 10/17: newest row). A newest row whose close is still ahead is a session in
progress, skipped for the match: on weekdays at 18:10 KST KIS lists that day's US
session (daytime/pre-market) with its live quote as the close and values US holdings
at it (observed Mon 10/19 18:47 KST: holdings equal that row or moved between the
reads, none equal Friday's close). Such a price, within 1% of that row's close on
today's US date, is stamped at the CTRP6504R read (``us_extended_session_at_read``),
else KIS_US_PRICE_NOT_A_RECENT_CLOSE; a match with a closed session's close keeps that
time, with the read as the group's latest price time. Other markets keep
KIS_SESSION_DATE_IN_FUTURE. ``nav_price_time`` is the oldest component price time,
``nav_price_time_latest`` the newest; ``nav_price_time_basis`` names the bases.

Forward-accuracy truth (stored, never used for NAV): TTTC8434R
``bfdy_tot_asst_evlu_amt``/``asst_icdc_amt``/``evlu_pfls_smtl_amt``, CTRP6504R
(KRW basis) ``evlu_pfls_amt_smtl``, TTTC8708R daily ``rlzt_pfls`` and TTTS3039R rows
(available from T+1 only; stored with the queried dates).
Trade cash effects (inputs of kis_flow_inference): TTTC0081R fills, TTTC8715R
fee + tax per day, CTOS4001R settlements (KRW estimate until the day after
settlement), over a LOOKBACK_DAYS window so consecutive captures can be
differenced. TTTS3035R flags US fills on the current US date at capture time
(CTRP6504R may not reflect US trades after about 18:10 KST).

Pacing is kis_api's per-app-key spacing (KIS_MIN_REQUEST_INTERVAL, 0.25 s, i.e.
<= 4 requests/s, well under KIS's ~20/s). The whole capture has its own budget
(CAPTURE_BUDGET_SECONDS), which also caps the read in flight (KISClient.time_limit).
No order TRs, no writes, no secrets: account numbers are never stored.
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from kis_api import KISAPIError, KISResponseError

CAPTURE_VERSION = 'kis_ledgerless_v1'
KST = ZoneInfo('Asia/Seoul')
LOOKBACK_DAYS = 10
CAPTURE_BUDGET_SECONDS = 120.0
KRX_CLOSE = (15, 30)
NXT_CLOSE = (20, 0)  # NXT after-market ends 20:00 KST
# KRX/NXT stock tick by price (KRW, 2023 table): (upper bound, tick); 1,000 above.
TICKS = ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500))
MAX_PRICE_CHECKS = 40
CASH_CHECK_BP = Decimal(1)  # foreign-cash check tolerance: 1 bp of frcr_evlu_tota (+ rounding)
# CTRP6504R ovrs_excg_cd -> (group, HHDFS76240000 EXCD, market zone, regular close)
MARKETS = {
    'NASD': ('US', 'NAS', 'America/New_York', (16, 0)),
    'NYSE': ('US', 'NYS', 'America/New_York', (16, 0)),
    'AMEX': ('US', 'AMS', 'America/New_York', (16, 0)),
    'TKSE': ('JP', 'TSE', 'Asia/Tokyo', (15, 30)),
    'SEHK': ('HK', 'HKS', 'Asia/Hong_Kong', (16, 0)),
    'SHAA': ('CN', 'SHS', 'Asia/Shanghai', (15, 0)),
    'SZAA': ('CN', 'SZS', 'Asia/Shanghai', (15, 0)),
}
# TTTS3039R is queried per exchange; NASD covers all US exchanges at KIS.
REALIZED_EXCHANGES = {'US': ('NASD',), 'JP': ('TKSE',), 'HK': ('SEHK',), 'CN': ('SHAA', 'SZAA')}
US_EXCHANGES = ('NASD', 'NYSE', 'AMEX')
# Groups KIS quotes outside regular hours (US daytime and pre-market trading):
# its daily rows list that session with the live quote as close (live Mon 10/19 18:47 KST).
EXTENDED_SESSION_GROUPS = ('US',)
# A quote in the session in progress moves between the balance and the daily read: accepted
# within 1% of that row's close (observed 10/19 18:47-19:11 KST: all within 7.3 bp).
EXTENDED_QUOTE_TOLERANCE = Decimal('0.01')
BASES_READ_ATTEMPTS = 3  # CTRP6504R 02 + 01 pairs read until they agree
CURRENCY_GROUPS = {'USD': 'US', 'JPY': 'JP', 'HKD': 'HK', 'CNY': 'CN'}
# KIS quotes these rates per 100 units (published CTRP6504R example: JPY bass_exrt
# 981.11 with KRW prices = JPY price x 9.8111; live 10/17 confirmed). Checked by the KRW-basis identity.
RATE_UNITS = {'JPY': 100}
ZERO = Decimal(0)


def _d(value):
    return Decimal(value) if value is not None else None


def _s(value):
    return format(value, 'f') if value is not None else None


def metric(value, *reasons):
    reasons = list(dict.fromkeys(r for r in reasons if r))
    return {'value': None if reasons or value is None else _s(value),
            'status': 'unknown' if reasons or value is None else 'derived',
            'reason_codes': reasons or ([] if value is not None else ['KIS_VALUE_UNAVAILABLE'])}


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def _close_time(day, zone, hour_minute):
    stamp = datetime.strptime(day, '%Y%m%d')
    return datetime(stamp.year, stamp.month, stamp.day, *hour_minute, tzinfo=ZoneInfo(zone))


class _Reads:
    """Calls one validated read; records status per TR. Failures become reason codes."""

    def __init__(self, deadline, clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self.deadline, self.clock, self.now = deadline, clock, now
        self.log = {}

    def __call__(self, name, call, *args):
        left = self.deadline - self.clock()
        if left <= 0:
            self.log[name] = {'status': 'skipped', 'reason_codes': ['KIS_CAPTURE_BUDGET_EXCEEDED']}
            return None
        # The read in flight is capped by the time left (KISClient.time_limit: per-request
        # timeouts and rate-limit waits), so the budget bounds the capture, not just its start.
        limit = getattr(getattr(call, '__self__', None), 'time_limit', None)
        try:
            with limit(left) if callable(limit) else nullcontext():
                result = call(*args)
        except KISResponseError as exc:
            self.log[name] = {'status': 'invalid', 'reason_codes': [exc.reason], 'field': exc.field}
            return None
        except KISAPIError as exc:
            self.log[name] = {'status': 'failed', 'reason_codes': [
                'KIS_CAPTURE_BUDGET_EXCEEDED' if self.clock() >= self.deadline else
                'KIS_RATE_LIMITED' if exc.rate_limited else 'KIS_SOURCE_UNAVAILABLE']}
            return None
        except Exception:  # noqa: BLE001 - any read failure is a null field with a reason, never a crash
            self.log[name] = {'status': 'failed', 'reason_codes': [
                'KIS_CAPTURE_BUDGET_EXCEEDED' if self.clock() >= self.deadline else 'KIS_READ_FAILED']}
            return None
        outputs = result['outputs']
        self.log[name] = {'status': 'ok', 'pages': result['pages'], 'read_at': _iso(self.now()),
                          'rows': {k: len(v) for k, v in outputs.items() if isinstance(v, list)},
                          'padding_rows': result.get('padding_rows', 0)}
        return outputs

    def failed(self, name):
        entry = self.log.get(name) or {}
        return [f'{name.split(":")[0]}_{code}' if not code.startswith('KIS_') else code
                for code in entry.get('reason_codes', [])] or [f'KIS_{name.split(":")[0]}_UNAVAILABLE']


def _domestic(reads, client, today):
    out = {'nav': None, 'cash': None, 'reasons': [], 'fields': None, 'positions': {}, 'price': None}
    balance = reads('TTTC8434R', client.ledgerless_domestic_balance)
    if balance is None:
        out['reasons'].append('KIS_DOMESTIC_BALANCE_UNAVAILABLE')
        out['price'] = {'market': 'KRX', 'status': 'unknown', 'reason_codes': ['KIS_DOMESTIC_BALANCE_UNAVAILABLE']}
        return out
    summary = balance['output2']
    out['fields'] = {k: summary[k] for k in (
        'tot_evlu_amt', 'scts_evlu_amt', 'evlu_amt_smtl_amt', 'prvs_rcdl_excc_amt', 'dnca_tot_amt',
        'nxdy_excc_amt', 'bfdy_tot_asst_evlu_amt', 'asst_icdc_amt', 'evlu_pfls_smtl_amt', 'tot_loan_amt')}
    total, securities, d2 = _d(summary['tot_evlu_amt']), _d(summary['scts_evlu_amt']), _d(summary['prvs_rcdl_excc_amt'])
    if _d(summary['tot_loan_amt'] or '0') > 0:
        out['reasons'].append('KIS_LOAN_NOT_MODELLED')
    if abs(securities + d2 - total) > 1:
        out['reasons'].append('KIS_DOMESTIC_TOTAL_IDENTITY_FAILED')
    if not out['reasons']:
        out['nav'], out['cash'] = total, d2
    held = []
    for row in balance['output1']:
        qty = _d(row['hldg_qty'])
        out['positions'][row['pdno']] = _s(_d(out['positions'].get(row['pdno'], '0')) + qty)
        if qty > 0:
            held.append((_d(row['evlu_amt']), row['pdno'], _d(row['prpr'])))
    read_at = datetime.fromisoformat(reads.log['TTTC8434R']['read_at'])
    out['price'] = _krx_price_time(reads, client, held, today, read_at)
    return out


def _priced(session, close, basis, read_at):
    """(price time, basis) of a matched daily row; a session still open at the read is priced at the read."""
    stamp = _close_time(session, 'Asia/Seoul', close)
    return (stamp, basis) if stamp <= read_at else (read_at, basis.replace('_close', '_at_read'))


def _tick(price):
    return Decimal(next((tick for bound, tick in TICKS if price < bound), 1000))


def _nxt_priced(row, krx_close, price, session, read_at, in_range=True):
    """(price time, basis) of a price set on NXT, from NXT's daily row for the session; else None.

    ``in_range=False`` refuses the range rule (NX already lists a newer session).
    """
    close, nx_close = _close_time(session, 'Asia/Seoul', NXT_CLOSE), _d(row['stck_clpr'])
    after = 'after_market_' if read_at >= _close_time(session, 'Asia/Seoul', KRX_CLOSE) else ''
    if read_at >= close and nx_close == price:
        return close, 'nxt_after_market_close'
    if read_at < close and abs(nx_close - price) <= _tick(price):
        return read_at, f'nxt_{after}at_read'
    # NXT's close not yet posted: the row repeats the KRX close, its range and volume are NXT's.
    # While NXT is open the two daily reads differ by up to a tick (live Mon 10/19 19:15 KST,
    # 2 of 4 reads of each holding), so that repeat is matched within one tick until 20:00.
    low, high, volume = _d(row.get('stck_lwpr')), _d(row.get('stck_hgpr')), _d(row.get('acml_vol'))
    repeats = nx_close == krx_close or (read_at < close and abs(nx_close - krx_close) <= _tick(krx_close))
    if in_range and repeats and volume and low is not None and high is not None and low <= price <= high:
        return (close, 'nxt_after_market_close_in_range') if read_at >= close else (
            read_at, f'nxt_{after}at_read_in_range')
    return None


def _krx_price_time(reads, client, held, today, read_at):
    """Price time of every domestic holding: oldest (``price_time``) and newest (``latest_price_time``)."""
    if not held:
        return {'market': 'KRX', 'status': 'not_needed', 'basis': 'no_domestic_holdings', 'reason_codes': []}
    if any(not (len(symbol) == 6 and symbol.isalnum()) for _value, symbol, _price in held):
        return {'market': 'KRX', 'status': 'unknown', 'reason_codes': ['KIS_KRX_PRICE_SYMBOL_UNAVAILABLE']}
    if len(held) > MAX_PRICE_CHECKS:
        return {'market': 'KRX', 'status': 'unknown', 'reason_codes': ['KIS_KRX_PRICE_CHECK_LIMIT']}
    stamps, sessions, nxt, in_range, reasons = [], set(), 0, 0, []
    for _value, symbol, price in sorted(held, reverse=True):
        name = f'FHKST01010400:{symbol}'
        daily = reads(name, client.ledgerless_domestic_daily_closes, symbol)
        if daily is None:
            reasons.extend(reads.failed(name))
            continue
        rows = sorted(daily['output'], key=lambda r: r['stck_bsop_date'], reverse=True)
        if not rows:
            reasons.append('KIS_KRX_SESSION_UNAVAILABLE')
            continue
        session = rows[0]['stck_bsop_date']
        if session > today.strftime('%Y%m%d'):
            reasons.append('KIS_SESSION_DATE_IN_FUTURE')
            continue
        if _d(rows[0]['stck_clpr']) == price:
            sessions.add(session)
            stamps.append(_priced(session, KRX_CLOSE, 'krx_regular_close', read_at))
            continue
        # A price set on NXT (AFHR_FLPR_YN=N still does that): never stamped at the KRX 15:30.
        name = f'FHKST01010400:NX:{symbol}'
        nx = reads(name, client.ledgerless_domestic_daily_closes, symbol, 'NX')
        row = None if nx is None else next((r for r in nx['output'] if r['stck_bsop_date'] == session), None)
        # NX already lists a newer session (a catch-up run before KRX opens): the price may be
        # that session's pre-market trade, never placed inside the older session's range.
        ahead = nx is not None and any(r['stck_bsop_date'] > session for r in nx['output'])
        stamp = None if row is None else _nxt_priced(row, _d(rows[0]['stck_clpr']), price, session, read_at,
                                                     in_range=not ahead)
        if nx is None:
            reasons.extend(reads.failed(name))
        elif row is None:
            reasons.append('KIS_KRX_PRICE_NOT_LATEST_CLOSE')
        elif stamp is None:
            reasons.append('KIS_NXT_SESSION_AHEAD_OF_KRX' if ahead else 'KIS_NXT_PRICE_NOT_IDENTIFIED')
        else:
            sessions.add(session)
            stamps.append(stamp)
            nxt += 1
            in_range += stamp[1].endswith('_in_range')
    base = {'market': 'KRX', 'checked': len(held), 'nxt_priced': nxt, 'nxt_in_range': in_range}
    if reasons:
        return {**base, 'status': 'unknown', 'reason_codes': list(dict.fromkeys(reasons))}
    return {**base, 'status': 'derived', 'session_date': min(sessions), 'sessions': len(sessions),
            'price_time': _iso(min(stamps)[0]), 'latest_price_time': _iso(max(stamps)[0]),
            'basis': '+'.join(sorted({basis for _, basis in stamps})), 'reason_codes': []}


def _bases_agree(balance, totals):
    """The foreign-basis rows at their bass_exrt equal the KRW-basis evlu_amt_smtl (1 bp + rounding)."""
    krw = sum((_d(r['frcr_evlu_amt2']) * _d(r['bass_exrt']) / RATE_UNITS.get(r['buy_crcy_cd'].upper(), 1)
               for r in balance['output1']), ZERO)
    return abs(krw - _d(totals['evlu_amt_smtl'])) <= Decimal(len(balance['output1']) + 2) + krw * Decimal('0.0001')


def _overseas(reads, client, now_utc):
    out = {'nav': {}, 'cash': {}, 'fx': {}, 'reasons': [], 'positions': {}, 'cash_rows': {}, 'totals': None,
           'prices': [], 'groups': set(), 'cash_basis': {}, 'cash_check': None, 'cost_native': {}}
    for _attempt in range(BASES_READ_ATTEMPTS):
        balance = reads('CTRP6504R', client.ledgerless_overseas_balance)
        if balance is None:
            out['reasons'].append('KIS_OVERSEAS_BALANCE_UNAVAILABLE')
            return out
        # output3 is KRW only on the KRW basis (01); on 02 it is a raw mixed-currency sum (live 10/17).
        krw = reads('CTRP6504R:KRW', client.ledgerless_overseas_balance, '01')
        # Live quotes (US daytime/pre-market at 18:10 KST) can move between the two reads and
        # break the KRW identity below (live 10/19 19:22 KST, 1 of 5 captures): read the pair again.
        if krw is None or _bases_agree(balance, krw['output3']):
            break
    totals = None if krw is None else krw['output3']
    if krw is None:
        out['reasons'].append('KIS_OVERSEAS_KRW_TOTALS_UNAVAILABLE')
    elif [r['pdno'] for r in krw['output1']] != [r['pdno'] for r in balance['output1']]:
        out['reasons'].append('KIS_OVERSEAS_BASES_INCONSISTENT')
    out['totals'] = None if totals is None else dict(totals)
    securities, rates, largest = {}, {}, {}
    for row in balance['output1']:
        currency, qty = row['buy_crcy_cd'].upper(), _d(row['ccld_qty_smtl1'])
        value, rate = _d(row['frcr_evlu_amt2']), _d(row['bass_exrt'])
        entry = out['positions'].setdefault(row['pdno'], {'currency': currency, 'exchange': row['ovrs_excg_cd'],
                                                          'quantity': '0'})
        if entry['currency'] != currency:
            out['reasons'].append('KIS_OVERSEAS_POSITION_CURRENCY_CONFLICT')
        entry['quantity'] = _s(_d(entry['quantity']) + qty)
        securities[currency] = securities.get(currency, ZERO) + value
        # Forward-accuracy truth only: KIS converts this cost at today's bass_exrt in its KRW P&L.
        cost = _d(row.get('frcr_pchs_amt'))
        known = out['cost_native'].get(currency, ZERO)
        out['cost_native'][currency] = None if cost is None or known is None else known + cost
        if qty > 0:
            rates.setdefault(currency, set()).add(rate)
            market = MARKETS.get(row['ovrs_excg_cd'].upper())
            if market is None:
                out['reasons'].append('KIS_OVERSEAS_MARKET_UNKNOWN')
                continue
            out['groups'].add(market[0])
            candidate = (value * rate, row['pdno'], row['ovrs_excg_cd'].upper(), _d(row['ovrs_now_pric1']))
            if market[0] not in largest or candidate > largest[market[0]]:
                largest[market[0]] = candidate
    rows = {}
    for row in balance['output2']:
        currency = row['crcy_cd'].upper()
        if currency == 'KRW':
            continue  # KRW deposit is the domestic D+2 cash; never counted twice
        if currency in rows:
            out['reasons'].append('KIS_OVERSEAS_CASH_DUPLICATE')
        rows[currency] = row
        out['cash_rows'][currency] = {k: row[k] for k in (
            'frcr_dncl_amt_2', 'frcr_buy_amt_smtl', 'frcr_sll_amt_smtl', 'frst_bltn_exrt',
            'frcr_drwg_psbl_amt_1', 'nxdy_frcr_drwg_psbl_amt')}
    # KRW per unit: the rows' single bass_exrt (KIS's valuation rate), else the posted
    # rate for per-unit currencies. A per-100 posted rate without a row to check it is unused.
    for currency in set(securities) | set(rows):
        unit = RATE_UNITS.get(currency, 1)
        posted = _d(rows[currency]['frst_bltn_exrt']) if currency in rows else ZERO
        held = rates.get(currency, set())
        if len(held) == 1:
            out['fx'][currency] = {'rate_to_krw': _s(next(iter(held)) / unit), 'quote_unit': unit,
                                   'source': 'CTRP6504R.output1.bass_exrt'}
        elif posted > 0 and unit == 1:
            out['fx'][currency] = {'rate_to_krw': _s(posted), 'quote_unit': 1,
                                   'source': 'CTRP6504R.output2.frst_bltn_exrt'}
    cash_by, out['cash_check'] = _foreign_cash(rows, totals, out['fx'])
    if totals is not None and not _bases_agree(balance, totals):
        out['reasons'].append('KIS_OVERSEAS_TOTAL_MISMATCH')
    for currency in sorted(set(securities) | set(rows)):
        reasons = list(out['reasons'])
        row = rows.get(currency)
        cash = ZERO
        if row is not None and cash_by is None:
            reasons.append(out['cash_check']['reason'])
        elif row is not None:
            cash = cash_by[currency]
            out['cash_basis'][currency] = out['cash_check']['decided']
        else:
            out['cash_basis'][currency] = 'no_cash_row_zero'
        native = securities.get(currency, ZERO) + cash
        if currency not in out['fx'] and native != 0:
            reasons.append('KIS_FX_RATE_UNAVAILABLE')
        out['nav'][currency] = None if reasons else native
        out['cash'][currency] = None if reasons else cash
        out.setdefault('nav_reasons', {})[currency] = reasons
    for group, (_value, symbol, exchange, price) in sorted(largest.items()):
        out['prices'].append(_overseas_price_time(reads, client, group, symbol, exchange, price, now_utc))
    return out


def _foreign_cash(rows, totals, fx):
    """Foreign cash per currency (``frcr_dncl_amt_2`` row) and the stored check behind it.

    With no unsettled trade (output2 ``frcr_buy_amt_smtl``/``frcr_sll_amt_smtl`` zero) the
    cash is ``frcr_dncl_amt_2``; a check gap beyond tolerance is then the non-nulling
    advisory KIS_FX_CASH_CROSS_CHECK_MISMATCH. With one, two readings exist and neither
    is proven: ``frcr_dncl_amt_2`` already net of the day's executions (KIS field
    list), or the reference assumption ``frcr_dncl_amt_2_plus_unsettled`` (+ sells - buys;
    offered only when output3 ``ustl_*`` agree with the per-currency totals). A reading
    counts only when its cash at the NAV marks is consistent with KIS's own KRW figure,
    output3 ``frcr_evlu_tota``, within CASH_CHECK_BP of it plus 1 KRW per currency (+1);
    the closer consistent reading wins. That shows consistency, not which reading is
    right (KIS likely derives the figure from the same cash field). None consistent, or
    no figure to check: (None, check) and the currencies are null with the check's
    reason. A currency with every cash field zero needs no rate (it counts as 0).
    """
    pending = any(_d(r['frcr_buy_amt_smtl']) or _d(r['frcr_sll_amt_smtl']) for r in rows.values())
    check = {'check': 'CTRP6504R.output3.frcr_evlu_tota', 'unsettled_trades': pending, 'decided': None,
             'decided_by': None, 'gap_krw': {}, 'tolerance_krw': None, 'reason': None, 'advisories': []}
    readings = {'frcr_dncl_amt_2': {c: _d(r['frcr_dncl_amt_2']) for c, r in rows.items()}}
    rates = {c: _d((fx.get(c) or {}).get('rate_to_krw')) for c in rows}
    rates.update({c: ZERO for c, r in rows.items() if rates[c] is None and not any(
        _d(r[k]) for k in ('frcr_dncl_amt_2', 'frcr_buy_amt_smtl', 'frcr_sll_amt_smtl'))})
    if pending and totals is not None and None not in rates.values():
        unsettled = {side: sum((_d(r[f'frcr_{side}_amt_smtl']) * rates[c] for c, r in rows.items()), ZERO)
                     for side in ('buy', 'sll')}
        if all(abs(unsettled[side] - _d(totals[f'ustl_{side}_amt_smtl']))
               <= max(Decimal(10), _d(totals[f'ustl_{side}_amt_smtl']) * Decimal('0.01')) for side in unsettled):
            readings['frcr_dncl_amt_2_plus_unsettled'] = {
                c: _d(r['frcr_dncl_amt_2']) + _d(r['frcr_sll_amt_smtl']) - _d(r['frcr_buy_amt_smtl'])
                for c, r in rows.items()}
    if totals is not None and rows and None not in rates.values():
        reported = _d(totals['frcr_evlu_tota'])
        tolerance = Decimal(len(rows) + 1) + reported * CASH_CHECK_BP / 10000
        check['tolerance_krw'] = _s(tolerance.quantize(Decimal('0.01')))
        for name, cash in readings.items():
            gap = abs(sum((cash[c] * rates[c] for c in cash), ZERO) - reported)
            check['gap_krw'][name] = _s(gap.quantize(Decimal('0.01')))
    if not pending:
        check.update(decided='frcr_dncl_amt_2', decided_by='no_unsettled_trades')
        if check['tolerance_krw'] is not None and Decimal(check['gap_krw']['frcr_dncl_amt_2']) > Decimal(
                check['tolerance_krw']):
            check['advisories'].append('KIS_FX_CASH_CROSS_CHECK_MISMATCH')  # shown, never nulls the cash
        return readings['frcr_dncl_amt_2'], check
    if check['tolerance_krw'] is None:
        check['reason'] = 'KIS_FX_CASH_UNSETTLED_UNVERIFIED'
        return None, check
    passing = sorted((Decimal(gap), name != 'frcr_dncl_amt_2', name) for name, gap in check['gap_krw'].items()
                     if Decimal(gap) <= Decimal(check['tolerance_krw']))
    if not passing:
        check['reason'] = 'KIS_FX_CASH_CROSS_CHECK_FAILED'
        return None, check
    name = passing[0][2]
    check.update(decided=name, decided_by='consistent_with_kis_frcr_evlu_tota')
    return readings[name], check


def _overseas_price_time(reads, client, group, symbol, exchange, price, now_utc):
    _group, excd, zone, close = MARKETS[exchange]
    name = f'HHDFS76240000:{group}'
    base = {'market': group, 'symbol': symbol, 'exchange': exchange}
    daily = reads(name, client.ledgerless_overseas_daily_closes, symbol, excd)
    if daily is None:
        return {**base, 'status': 'unknown', 'reason_codes': reads.failed(name)}
    rows = sorted(daily['output2'], key=lambda r: r['xymd'], reverse=True)
    # A session whose close is still ahead is in progress (US daytime/pre-market at 18:10 KST).
    live = rows[0] if rows and _close_time(rows[0]['xymd'], zone, close) > now_utc else None
    closed = rows[1:3] if live else rows[:2]
    read_at = (reads.log.get('CTRP6504R') or {}).get('read_at')
    extended = live is not None and group in EXTENDED_SESSION_GROUPS and read_at is not None
    for index, row in enumerate(closed):
        if _d(row['clos']) == price:
            stamp = _close_time(row['xymd'], zone, close)
            # Named for the row matched: the newest session, or the one before it.
            basis = f'{group.lower()}_regular_close' + ('' if index == 0 else '_previous_session')
            out = {**base, 'status': 'derived', 'session_date': row['xymd'], 'price_time': _iso(stamp),
                   'basis': basis, 'reason_codes': []}
            # The other holdings of the group may already carry the session's live quotes.
            return {**out, 'latest_price_time': read_at} if extended else out
    if extended and live['xymd'] == now_utc.astimezone(ZoneInfo(zone)).strftime('%Y%m%d') and (
            abs(price - _d(live['clos'])) <= _d(live['clos']) * EXTENDED_QUOTE_TOLERANCE):
        # KIS's quote in today's session in progress, as of the balance read.
        return {**base, 'status': 'derived', 'session_date': live['xymd'], 'price_time': read_at,
                'basis': f'{group.lower()}_extended_session_at_read', 'reason_codes': []}
    if live and group not in EXTENDED_SESSION_GROUPS:
        return {**base, 'status': 'unknown', 'session_date': live['xymd'], 'reason_codes': ['KIS_SESSION_DATE_IN_FUTURE']}
    return {**base, 'status': 'unknown', 'reason_codes': [f'KIS_{group}_PRICE_NOT_A_RECENT_CLOSE']}


def _trades(reads, client, start, end, overseas, groups, us_date):
    out = {'window': [start, end], 'domestic_fills': None, 'domestic_costs': None, 'overseas_settlements': None,
           'us_fills_on_capture_date': None, 'reasons': []}
    fills = reads('TTTC0081R', client.ledgerless_domestic_fills, start, end)
    if fills is None:
        out['reasons'].append('KIS_DOMESTIC_FILLS_UNAVAILABLE')
    else:
        out['domestic_fills'] = [{k: r[k] for k in ('ord_dt', 'odno', 'sll_buy_dvsn_cd', 'pdno', 'tot_ccld_qty',
                                                     'avg_prvs', 'tot_ccld_amt', 'excg_dvsn_cd', 'ord_tmd')}
                                 for r in fills['output1'] if _d(r['tot_ccld_qty']) > 0]
        keys = [(r['ord_dt'], r['odno']) for r in out['domestic_fills']]
        if len(set(keys)) != len(keys):
            out['reasons'].append('KIS_DOMESTIC_FILL_DUPLICATE')
            out['domestic_fills'] = None
    costs = reads('TTTC8715R', client.ledgerless_domestic_trade_costs, start, end)
    if costs is None:
        out['reasons'].append('KIS_DOMESTIC_COSTS_UNAVAILABLE')
    else:
        by_day = {}
        for row in costs['output1']:
            day = by_day.setdefault(row['trad_dt'], {'fee': ZERO, 'tl_tax': ZERO, 'buy_qty': ZERO, 'sll_qty': ZERO})
            for key in ('fee', 'tl_tax', 'buy_qty', 'sll_qty'):
                day[key] += _d(row[key])
        out['domestic_costs'] = {day: {k: _s(v) for k, v in values.items()} for day, values in sorted(by_day.items())}
    if overseas:
        settlements = reads('CTOS4001R', client.ledgerless_overseas_settlements, start, end)
        if settlements is None:
            out['reasons'].append('KIS_OVERSEAS_SETTLEMENTS_UNAVAILABLE')
        else:
            out['overseas_settlements'] = [dict(r) for r in settlements['output1']]
        us_fills = reads('TTTS3035R', client.ledgerless_overseas_fills, us_date, us_date)
        if us_fills is None:
            out['reasons'].append('KIS_US_FILLS_UNVERIFIED')
        else:
            out['us_fills_on_capture_date'] = sum(
                1 for r in us_fills['output'] if r['ovrs_excg_cd'].upper() in US_EXCHANGES
                and r['ord_dt'] == us_date and _d(r['ft_ccld_qty']) > 0)
    return out


def _truth(reads, client, start, end, domestic, overseas_state, groups):
    out = {'domestic': None, 'overseas': None, 'daily_pnl': None, 'daily_pnl_totals': None,
           'overseas_realized': {}, 'reasons': []}
    if domestic['fields']:
        out['domestic'] = {k: domestic['fields'][k] for k in ('bfdy_tot_asst_evlu_amt', 'asst_icdc_amt',
                                                              'evlu_pfls_smtl_amt', 'tot_evlu_amt')}
    if overseas_state and overseas_state['totals']:
        out['overseas'] = {'evlu_pfls_amt_smtl': overseas_state['totals']['evlu_pfls_amt_smtl'],
                           'cost_native': {c: None if v is None else _s(v)
                                           for c, v in sorted(overseas_state['cost_native'].items())}}
    pnl = reads('TTTC8708R', client.ledgerless_domestic_daily_pnl, start, end)
    if pnl is None:
        out['reasons'].append('KIS_DOMESTIC_DAILY_PNL_UNAVAILABLE')
    else:
        out['daily_pnl'] = sorted((dict(r) for r in pnl['output1']), key=lambda r: r['trad_dt'])
        out['daily_pnl_totals'] = dict(pnl['output2'])
    for group in sorted(groups):
        for exchange in REALIZED_EXCHANGES.get(group, ()):
            name = f'TTTS3039R:{exchange}'
            realized = reads(name, client.ledgerless_overseas_realized, start, end, exchange)
            out['overseas_realized'][exchange] = (
                {'status': 'unknown', 'reason_codes': reads.failed(name)} if realized is None else
                {'status': 'reported', 'queried': [start, end], 'available_from': 'T+1',
                 'rows': [dict(r) for r in realized['output1']], 'totals': dict(realized['output2'])})
    return out


def _account(client, account, now_utc, reads):
    today = now_utc.astimezone(KST).date()
    start = (today - timedelta(days=LOOKBACK_DAYS)).strftime('%Y%m%d')
    end = today.strftime('%Y%m%d')
    us_date = now_utc.astimezone(ZoneInfo('America/New_York')).strftime('%Y%m%d')
    supports_overseas = bool(getattr(account, 'supports_overseas', True))
    domestic = _domestic(reads, client, today)
    overseas = _overseas(reads, client, now_utc) if supports_overseas else None
    groups = set(overseas['groups']) if overseas else set()
    trades = _trades(reads, client, start, end, supports_overseas, groups, us_date)
    # Realized P&L also for markets traded in the window but no longer held.
    groups |= {CURRENCY_GROUPS[r['crcy_cd'].upper()] for r in trades['overseas_settlements'] or ()
               if r['crcy_cd'].upper() in CURRENCY_GROUPS}
    truth = _truth(reads, client, start, end, domestic, overseas, groups)
    assets = reads('CTRP6548R', client.ledgerless_account_assets)
    nav_native = {'KRW': metric(domestic['nav'], *domestic['reasons'])}
    cash_native = {'KRW': metric(domestic['cash'], *domestic['reasons'])}
    cash_basis = {'KRW': 'TTTC8434R.output2.prvs_rcdl_excc_amt (D+2)'}
    fx = {}
    prices = [domestic['price']]
    if overseas is not None:
        if not overseas['nav']:
            for key in ('USD',):
                nav_native.setdefault(key, metric(None, *overseas['reasons']))
                cash_native.setdefault(key, metric(None, *overseas['reasons']))
        for currency in sorted(overseas['nav']):
            reasons = overseas['nav_reasons'][currency]
            nav_native[currency] = metric(overseas['nav'][currency], *reasons)
            cash_native[currency] = metric(overseas['cash'][currency], *reasons)
            cash_basis[currency] = overseas['cash_basis'].get(currency)
        fx = overseas['fx']
        prices.extend(overseas['prices'])
    reasons = []
    total = ZERO
    for currency, value in nav_native.items():
        if value['value'] is None:
            reasons.extend(value['reason_codes'])
            continue
        rate = Decimal(1) if currency == 'KRW' else _d((fx.get(currency) or {}).get('rate_to_krw'))
        if rate is None and _d(value['value']) != 0:
            reasons.append('KIS_FX_RATE_UNAVAILABLE')
            continue
        total += _d(value['value']) * (rate or ZERO)  # zero with no rate counts as 0
    price_times, price_reasons = [], []
    for price in prices:
        if price['status'] == 'derived':
            price_times.append((price['price_time'], price.get('latest_price_time', price['price_time']),
                                price['basis']))
        elif price['status'] != 'not_needed':
            price_reasons.extend(price['reason_codes'])
    completed = reads.now()
    if price_reasons:
        nav_price_time, latest, basis = None, None, None
    elif price_times:
        nav_price_time, latest = min(t for t, _, _ in price_times), max(t for _, t, _ in price_times)
        basis = '+'.join(sorted({part for _, _, b in price_times for part in b.split('+')}))
    else:
        nav_price_time = latest = _iso(completed)
        basis = 'cash_only_read_completion'
    if trades['us_fills_on_capture_date']:
        reasons.append('KIS_US_FILLS_NEAR_CAPTURE')
    status = 'complete' if not reasons and not price_reasons else 'partial'
    return {
        'label': account.label, 'kind': getattr(account, 'kind', None), 'supports_overseas': supports_overseas,
        'status': status, 'reasons': list(dict.fromkeys(reasons + price_reasons)),
        'advisories': list(((overseas or {}).get('cash_check') or {}).get('advisories') or []),
        'nav_native': nav_native, 'cash_native': cash_native, 'cash_basis': cash_basis, 'fx': fx,
        'nav_krw': metric(None if reasons else total, *reasons),
        'nav_price_time': nav_price_time, 'nav_price_time_latest': latest, 'nav_price_time_basis': basis,
        'price_times': prices,
        'positions': {'domestic': domestic['positions'], 'overseas': overseas['positions'] if overseas else {}},
        'domestic': domestic['fields'],
        'overseas': None if overseas is None else {'totals': overseas['totals'], 'cash': overseas['cash_rows'],
                                                    'cash_check': overseas['cash_check']},
        'cross_check': {'CTRP6548R': None if assets is None else assets['output2']},
        'truth': truth, 'trades': trades, 'observed_at': _iso(completed),
    }


def capture_kis(accounts, client_for, *, now=None, budget_seconds=CAPTURE_BUDGET_SECONDS,
                clock=time.monotonic, wall=None):
    """``{'kis_capture': {...}}`` for the snapshot payload. Never raises.

    accounts: configured KIS accounts (``.label``, ``.supports_overseas``, ``.kind``);
    client_for: label -> KISClient. Account numbers are never stored.
    """
    wall = wall or (lambda: datetime.now(timezone.utc))
    started = now or wall()
    capture = {'version': CAPTURE_VERSION, 'started_at': _iso(started), 'accounts': {}, 'reasons': [],
               'reads': {}}
    deadline = clock() + budget_seconds
    try:
        accounts = list(accounts() if callable(accounts) else accounts)
    except Exception:  # noqa: BLE001 - no KIS configuration is a reason, not a failure of the snapshot
        capture['reasons'].append('KIS_NOT_CONFIGURED')
        accounts = []
    for account in accounts:
        key = 'kis:' + account.label
        reads = _Reads(deadline, clock=clock, now=wall)
        try:
            capture['accounts'][key] = _account(client_for(account.label), account, started, reads)
        except Exception:  # noqa: BLE001
            capture['accounts'][key] = {'label': account.label, 'status': 'unavailable',
                                        'reasons': ['KIS_CAPTURE_ACCOUNT_FAILED'], 'nav_native': {},
                                        'nav_krw': metric(None, 'KIS_CAPTURE_ACCOUNT_FAILED'),
                                        'observed_at': _iso(wall())}
        capture['reads'][key] = reads.log
    capture['observed_at'] = _iso(wall())
    return {'kis_capture': capture}
