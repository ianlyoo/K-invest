"""Strict Yahoo dividend history with conditional expected payment windows.

Policy dated 2026-10-13: US expected lag 14 calendar days,
estimated window ex+0..ex+60; KR ex+0..ex+150 (expected 60 days, 105 in December).
These are inference assumptions, not reported payment dates or guarantees.
US centre: an offline training-month calibration dated 2026-10-13.
Bounds are design allowances, not universal absence proof. State Street's
2026 schedule, revision 2026-09-23, page 3, lists SPY March 20 -> April 30
(41 days) and SPYG March 23 -> March 25 (2 days):
https://www.ssga.com/library-content/products/fund-data/etfs/us/distribution/SPDR_Dividend_Distribution_Schedule.pdf
(accessed 2026-10-13). Apple's 2026 record/pay history includes 3-day gaps
(https://investor.apple.com/dividend-history/default.aspx, accessed 2026-10-13).
KR allowance: Samsung's official FAQ lists March/June/September records paid in
May/August/November and December records paid in April
(https://dividend.samsung.com/faq, accessed 2026-10-13). This is an example, not
a universal Korean rule; record dates differ from ex-dates by settlement days.
A validated announced payment overrides the estimate with pay +/-3 days.

Every validated historical ex-date gets one window. No overlapping window permits
only conditional estimated absence, with full maximum-lag ex-date lookback.
It never establishes complete/verified payment coverage or observed zero.
Invalid/missing responses never support zero. Gross amounts come from chart
events, never annual dividendRate.

Presence and absence both need the chart's daily bars to cover max(lookback,
listing) through the evidence end with no gap beyond a market closure. The
listing date is chart meta.firstTradeDate: no dividend can predate it. Live
2026-10-16: the longest bar gaps were US 4 and KR 8 calendar days
(Chuseok 2025-10-02 -> 10-10); the bounds below add margin for one extra closure.
A chart without bars leaves the symbol unresolved.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from toss_nav_validation import incomplete_envelope

PAYMENT_LAGS = {'US': (0, 60), 'KR': (0, 150)}
MAX_PAYMENT_LAG_DAYS = 150
PAYMENT_POLICY = 'estimated-market-payment-window-v2-2026-10-13'
ABSENCE_ESTIMATED = 'DIVIDEND_ABSENCE_ESTIMATED_FROM_SCHEDULE'
MAX_BAR_GAP_DAYS = {'US': 5, 'KR': 11}
LISTING_BASIS = 'yahoo-chart-meta-firstTradeDate'


def estimated_coverage(proof):
    return (proof.get('status') == 'estimated' and proof.get('basis') == PAYMENT_POLICY
            and proof.get('reasons') == [ABSENCE_ESTIMATED])


def reference_missing(payload, symbol, endpoint):
    """Only explicit provider not-found bodies prove alternative absence.

    Published Yahoo response examples in yfinance issues #2646 (2025-12-18)
    and #2367 (2025-03-17), accessed 2026-10-13; links in the flow docs.
    A bare HTTP 404, unknown error, empty result or malformed body is insufficient.
    """
    description = (f'Quote not found for symbol: {symbol}' if endpoint == 'quoteSummary'
                   else 'No data found, symbol may be delisted')
    return endpoint in ('quoteSummary', 'chart') and payload == {endpoint: {
        'result': None, 'error': {'code': 'Not Found', 'description': description}}}


def payment_window(ex_date, market):
    ex = date.fromisoformat(ex_date)
    low, high = PAYMENT_LAGS[market]
    centre = 14 if market == 'US' else 105 if ex.month == 12 else 60
    return {'payment_start': (ex + timedelta(days=low)).isoformat(),
            'payment_end': (ex + timedelta(days=high)).isoformat(),
            'expected_pay_date': (ex + timedelta(days=centre)).isoformat(),
            'pay_date': None, 'payment_basis': PAYMENT_POLICY}


def instrument_reference(symbol, instrument):
    """Map a validated Toss /stocks market to Yahoo; country/physical venue cannot.

    Public Toss OpenAPI 1.2.24 StockInfo, accessed 2026-10-13:
    https://openapi.tossinvest.com/openapi-docs/latest/openapi.json.
    KOSPI -> .KS, KOSDAQ -> .KQ. An old snapshot without StockInfo requires
    independent validated KRW schedules from BOTH candidate suffixes, exactly
    one successful candidate plus explicit documented not-found/no-data for the
    alternative, and name/ISIN cross-checks where both sources have them.
    No Yahoo search-result identity is accepted as instrument proof.
    """
    base = symbol.split('.')[0]
    if instrument and ('market' in instrument or instrument.get('basis') == 'toss-stock-info-v1'):
        if (instrument.get('basis') != 'toss-stock-info-v1' or instrument.get('symbol') != base
                or instrument.get('currency') != 'KRW' or instrument.get('market') not in ('KOSPI', 'KOSDAQ')
                or not isinstance(instrument.get('name'), str) or not instrument['name']
                or not re.fullmatch(r'KR[A-Z0-9]{9}[0-9]', instrument.get('isinCode', ''))):
            raise ValueError('INVALID_TOSS_INSTRUMENT_BINDING')
        reference = base + ('.KS' if instrument['market'] == 'KOSPI' else '.KQ')
        if '.' in symbol and symbol != reference:
            raise ValueError('DIVIDEND_INSTRUMENT_MISMATCH')
        return reference
    return None  # A caller-supplied Yahoo suffix is not a validated market field.


def check_instrument_identity(binding, symbol, instrument):
    reference = binding['reference_symbol']
    base = symbol.split('.')[0]
    if reference not in (base + '.KS', base + '.KQ') or binding.get('basis') not in (
            'toss-stock-info-v1', 'unique-validated-kr-schedule'):
        raise ValueError('DIVIDEND_INSTRUMENT_MISMATCH')
    if binding['basis'] == 'toss-stock-info-v1' and not re.fullmatch(r'KR[A-Z0-9]{9}[0-9]', binding.get('toss_isin', '')):
        raise ValueError('DIVIDEND_INSTRUMENT_MISMATCH')
    expected = instrument_reference(symbol, instrument)
    if expected and reference != expected:
        raise ValueError('DIVIDEND_INSTRUMENT_MISMATCH')
    if not instrument:
        return
    if instrument.get('isinCode') and binding.get('toss_isin') not in (None, instrument['isinCode']):
        raise ValueError('DIVIDEND_ISIN_MISMATCH')
    if instrument.get('isinCode') and any(v != instrument['isinCode'] for v in binding.get('isins', [])):
        raise ValueError('DIVIDEND_ISIN_MISMATCH')
    if not expected:
        def normalized(value):
            return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())
        source_names = {normalized(instrument[k]) for k in ('name', 'englishName') if instrument.get(k)}
        reference_names = {normalized(v) for v in binding.get('names', []) if v}
        if source_names and reference_names and not source_names & reference_names:
            raise ValueError('DIVIDEND_NAME_MISMATCH')


def _epoch(value):
    if type(value) is not int or not 946684800 <= value < 4133980800:
        raise ValueError('INVALID_DIVIDEND_EPOCH')
    return value


def _date(value, market):
    return datetime.fromtimestamp(_epoch(value), timezone.utc).astimezone(
        ZoneInfo('America/New_York' if market == 'US' else 'Asia/Seoul')).date().isoformat()


def validate_calendar(payload, symbol, currency):
    """One validator for raw quoteSummary/calendarEvents + price responses."""
    if not isinstance(payload, dict) or incomplete_envelope(payload):
        raise ValueError('INVALID_DIVIDEND_CALENDAR')
    envelope = payload['quoteSummary']
    if envelope['error'] is not None or len(envelope['result']) != 1:
        raise ValueError('INVALID_DIVIDEND_CALENDAR')
    row = envelope['result'][0]
    if row['price']['symbol'] != symbol or row['price']['currency'] != currency:
        raise ValueError('DIVIDEND_BINDING_MISMATCH')
    if 'calendarEvents' not in row:
        # Live 2026-10-16: Yahoo answers ETFs with the price module only. That
        # announces no future dividend; chart ex-date events still decide.
        if set(row) != {'price'} or row['price'].get('quoteType') != 'ETF':
            raise ValueError('DIVIDEND_CALENDAR_MISSING')
        return None
    calendar = row['calendarEvents']
    if not isinstance(calendar, dict):
        raise ValueError('INVALID_DIVIDEND_CALENDAR')
    ex, pay = calendar.get('exDividendDate'), calendar.get('dividendDate')
    if 'exDividendDate' not in calendar and 'dividendDate' not in calendar:
        return None
    # formatted=false returns scalar seconds; formatted responses wrap them in
    # raw. Both shapes pass the same epoch checks (no bool/string/ms coercion).
    ex = _epoch(ex['raw'] if isinstance(ex, dict) else ex)
    pay = None if 'dividendDate' not in calendar else _epoch(pay['raw'] if isinstance(pay, dict) else pay)
    if pay is not None and ex > pay:
        raise ValueError('INVALID_DIVIDEND_CALENDAR')
    return ex, pay


def validate_dividend_chart(payload, symbol, currency, lo, hi):
    """One validator for raw chart events, retaining envelope and row checks."""
    if not isinstance(payload, dict) or incomplete_envelope(payload):
        raise ValueError('INVALID_DIVIDEND_CHART')
    envelope = payload['chart']
    if envelope['error'] is not None or len(envelope['result']) != 1:
        raise ValueError('INVALID_DIVIDEND_CHART')
    row = envelope['result'][0]
    if row['meta']['symbol'] != symbol or row['meta']['currency'] != currency:
        raise ValueError('DIVIDEND_BINDING_MISMATCH')
    stamps = row.get('timestamp')
    if not stamps:
        raise ValueError('DIVIDEND_PRICE_HISTORY_MISSING')
    if not isinstance(stamps, list) or len(set(stamps)) != len(stamps) or any(not lo <= _epoch(t) < hi for t in stamps):
        raise ValueError('INVALID_DIVIDEND_CHART')
    quotes = row['indicators']['quote']
    if (not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict)
            or any(not isinstance(values, list) or len(values) != len(stamps) for values in quotes[0].values())):
        raise ValueError('INVALID_DIVIDEND_CHART')
    dividends = row.get('events', {}).get('dividends', {})
    if not isinstance(dividends, dict):
        raise ValueError('INVALID_DIVIDEND_CHART')
    events, seen = [], set()
    for key, event in dividends.items():
        stamp = _epoch(event['date'])
        if str(stamp) != key or stamp in seen or not lo <= stamp < hi:
            raise ValueError('DUPLICATE_OR_INVALID_DIVIDEND')
        seen.add(stamp)
        if type(event['amount']) not in (int, float):
            raise ValueError('INVALID_DIVIDEND_AMOUNT')
        amount = Decimal(str(event['amount']))
        if not amount.is_finite() or amount <= 0:
            raise ValueError('INVALID_DIVIDEND_AMOUNT')
        events.append((stamp, format(amount, 'f')))
    return events


def price_history_start(payload, market, first, last):
    """Prove daily bar timestamps cover max(first, listing)..last; return that start.

    A listing after `first` is proven by meta.firstTradeDate alone; bars before
    it contradict that field. Bar values are not inspected.
    """
    row = payload['chart']['result'][0]
    listing = row['meta'].get('firstTradeDate')
    if listing is not None:
        if type(listing) is not int or not -2208988800 <= listing < 4133980800:
            raise ValueError('INVALID_DIVIDEND_CHART')
        listing = datetime.fromtimestamp(listing, timezone.utc).astimezone(
            ZoneInfo('America/New_York' if market == 'US' else 'Asia/Seoul')).date()
    days = sorted({date.fromisoformat(_date(t, market)) for t in row['timestamp']})
    if listing is not None and days[0] < listing:
        raise ValueError('DIVIDEND_LISTING_DATE_CONFLICT')
    start = max(first, listing) if listing is not None else first
    edges = [start - timedelta(days=1), *(d for d in days if start <= d <= last), last + timedelta(days=1)]
    if any((b - a).days > MAX_BAR_GAP_DAYS[market] for a, b in zip(edges, edges[1:])):
        raise ValueError('DIVIDEND_PRICE_HISTORY_GAP')
    return start, listing is not None and listing > first


def schedule_from_responses(calendar, chart, *, symbol, currency, start, end, lo, hi, as_of=None):
    """Validate both envelopes once, then project all ex-dates to payment windows."""
    result = {'rows': [], 'coverage': [{'symbol': symbol, 'currency': currency, 'start': start, 'end': end,
                'status': 'limited', 'reasons': ['PUBLIC_DIVIDEND_COVERAGE_LIMITED']}]}
    try:
        dates = validate_calendar(calendar, symbol, currency)
        events = validate_dividend_chart(chart, symbol, currency, lo, hi)
        market = 'US' if currency == 'USD' else 'KR'
        zone = ZoneInfo('America/New_York' if market == 'US' else 'Asia/Seoul')
        def bound(value):
            return (datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(zone).date()
                    if len(value) > 10 else date.fromisoformat(value))
        first, last = bound(start), bound(end)
        observed = as_of or datetime.now(timezone.utc)
        ex_first = datetime.fromtimestamp(lo, zone).date() + timedelta(days=1)
        ex_last = min(datetime.fromtimestamp(hi, zone).date(), observed.astimezone(zone).date()) - timedelta(days=1)
        min_lag, max_lag = PAYMENT_LAGS[market]
        if (first > last or first < ex_first + timedelta(days=max_lag)
                or last > min(ex_last + timedelta(days=min_lag), observed.astimezone(zone).date())):
            raise ValueError('UNOBSERVED_DIVIDEND_WINDOW')
        price_start, listed = price_history_start(chart, market, ex_first, ex_last)
        known_ex, known_pay = None, None
        if dates is not None:
            known_ex = datetime.fromtimestamp(dates[0], timezone.utc).date().isoformat()
            known_pay = datetime.fromtimestamp(dates[1], timezone.utc).date().isoformat() if dates[1] is not None else None
            matched = [stamp for stamp, _ in events if _date(stamp, market) == known_ex]
            pay_overlaps = known_pay is not None and first - timedelta(days=3) <= date.fromisoformat(known_pay) <= last + timedelta(days=3)
            if (ex_first <= date.fromisoformat(known_ex) <= ex_last or pay_overlaps) and len(matched) != 1:
                raise ValueError('DIVIDEND_AMOUNT_NOT_BOUND_TO_EX_DATE')
        seen = set()
        for stamp, amount in sorted(events):
            ex_date = _date(stamp, market)
            if ex_date in seen or stamp > observed.timestamp():
                raise ValueError('DUPLICATE_OR_INVALID_DIVIDEND')
            if listed and ex_date < price_start.isoformat():
                raise ValueError('DIVIDEND_LISTING_DATE_CONFLICT')
            seen.add(ex_date)
            window = payment_window(ex_date, market)
            if ex_date == known_ex and known_pay is not None:
                pay = date.fromisoformat(known_pay)
                window.update(pay_date=known_pay, expected_pay_date=known_pay,
                    payment_start=(pay - timedelta(days=3)).isoformat(),
                    payment_end=(pay + timedelta(days=3)).isoformat(), payment_basis='announced_calendar')
            identity = hashlib.sha256(f'{symbol}|{ex_date}|{amount}'.encode()).hexdigest()
            result['rows'].append({'id': identity, 'symbol': symbol, 'currency': currency, 'market': market,
                                   'ex_date': ex_date, 'gross_per_share': amount, **window})
        result['coverage'][0].update(start=first.isoformat(), end=last.isoformat(), status='estimated',
            reasons=[ABSENCE_ESTIMATED], basis=PAYMENT_POLICY,
            evidence_start=ex_first.isoformat(), evidence_end=ex_last.isoformat(),
            price_history_start=price_start.isoformat(), listing_basis=LISTING_BASIS if listed else None)
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation, OverflowError) as exc:
        # The specific cause leads, so the cache's failure_reason keeps it.
        known = isinstance(exc, ValueError) and re.fullmatch(r'[A-Z]+(?:_[A-Z]+)+', str(exc))
        result['rows'] = []
        result['coverage'][0]['reasons'] = [str(exc) if known else 'DIVIDEND_RESPONSE_SHAPE_UNEXPECTED',
                                            'PUBLIC_DIVIDEND_COVERAGE_LIMITED', 'INVALID_PUBLIC_DIVIDEND_RESPONSE']
    return result
