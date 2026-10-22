"""Bounded, native-currency Toss close series; no portfolio or analysis inputs.

Candle contract: Toss OpenAPI 1.2.24, Candle/CandlePageResponse and ApiResponse,
https://openapi.tossinvest.com/openapi-docs/latest/openapi.json (2026-10-18).
Validate the full envelope before the legacy paging path projects/deduplicates it.
"""
from __future__ import annotations

import calendar
import json
import math
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from toss_nav_validation import incomplete_envelope

ALIASES = {
    'KOSPI': ('069500', 'KODEX 200 ETF'),
    'KOSDAQ': ('229200', 'KODEX 코스닥150 ETF'),
    'S&P500': ('SPY', 'SPDR S&P 500 ETF Trust'),
    'SPX': ('SPY', 'SPDR S&P 500 ETF Trust'),
    'NASDAQ': ('QQQ', 'Invesco QQQ Trust ETF'),
}
PRICE_BASIS = 'price return (adjustment unverified)'
MAX_PAGES = 20  # At most 4,000 raw daily bars/symbol; never silently truncate.
MAX_DAYS = 3653  # Ten years; longer explicit windows fail before fetching.


class SeriesError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _stamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp
    except (TypeError, ValueError):
        raise SeriesError('INVALID_CANDLES', 'Candle timestamps must include a timezone.') from None


def _decimal(value, *, positive=True):
    if not isinstance(value, str) or len(value) > 30 or re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) is None:
        raise SeriesError('INVALID_CANDLES', 'Candle numeric fields must be decimal strings.')
    n = Decimal(value)
    if not n.is_finite() or (n <= 0 if positive else n < 0):
        raise SeriesError('INVALID_CANDLES', 'Candle prices must be finite and positive; volume nonnegative.')
    return n


def validate_candle_page(envelope, request):
    """One strict validator, shared by production and real-client HTTP tests.

    Raw pages are documented newest first. The existing client sorts them; the
    series merge emits unique ascending dates bounded to the requested window.
    nextBefore is allowed pagination; partial/error flags elsewhere are refused.
    """
    if (not isinstance(envelope, dict) or 'result' not in envelope
            or incomplete_envelope(envelope) or envelope.get('ok') is False
            or envelope.get('complete') is False):
        raise SeriesError('INVALID_CANDLES', 'Incomplete or invalid candle envelope.')
    page = envelope['result']
    if (not isinstance(page, dict) or not isinstance(page.get('candles'), list)
            or 'nextBefore' not in page or len(page['candles']) > request['count']
            or page.get('complete') is False or page.get('symbol', request['symbol']) != request['symbol']):
        raise SeriesError('INVALID_CANDLES', 'Invalid candle page shape or request binding.')
    cutoff = _stamp(request['before'])
    previous = None
    dates = set()
    expected_currency = 'KRW' if request['symbol'].isdigit() else 'USD'
    exchange_zone = ZoneInfo('Asia/Seoul' if expected_currency == 'KRW' else 'America/New_York')
    for row in page['candles']:
        if not isinstance(row, dict):
            raise SeriesError('INVALID_CANDLES', 'Invalid candle row.')
        stamp = _stamp(row.get('timestamp'))
        local = stamp.astimezone(exchange_zone)
        if (stamp > cutoff or local.time() != time() or local.date() != stamp.date() or local.date() in dates
                or (previous is not None and stamp >= previous)
                or row.get('currency') != expected_currency
                or row.get('symbol', request['symbol']) != request['symbol']):
            raise SeriesError('INVALID_CANDLES', 'Candle date/order/currency does not match the request.')
        prices = [_decimal(row.get(k)) for k in ('openPrice', 'highPrice', 'lowPrice', 'closePrice')]
        _decimal(row.get('volume'), positive=False)
        if not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]:
            raise SeriesError('INVALID_CANDLES', 'Inconsistent candle OHLC prices.')
        dates.add(stamp.date())
        previous = stamp
    cursor = page['nextBefore']
    if cursor is not None:
        next_stamp = _stamp(cursor)
        if previous is None or next_stamp >= cutoff or next_stamp > previous:
            raise SeriesError('INVALID_CANDLES', 'Candle cursor does not advance.')


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise SeriesError('INVALID_ARGUMENT', 'Dates must use YYYY-MM-DD.') from None


def _window(period, start_date, end_date, now):
    if period not in ('1mo', '3mo', '6mo', 'ytd', '1y'):
        raise SeriesError('INVALID_ARGUMENT', 'period must be 1mo, 3mo, 6mo, ytd or 1y.')
    today = now.astimezone(ZoneInfo('Asia/Seoul')).date()
    if end_date and not start_date:
        raise SeriesError('INVALID_ARGUMENT', 'end_date requires start_date.')
    end = _date(end_date) if end_date else today
    if start_date:
        start = _date(start_date)
    elif period == 'ytd':
        start = date(end.year, 1, 1)
    else:
        months = {'1mo': 1, '3mo': 3, '6mo': 6, '1y': 12}[period]
        year, month = divmod(end.year * 12 + end.month - 1 - months, 12)
        start = date(year, month + 1, min(end.day, calendar.monthrange(year, month + 1)[1]))
    if start > end or end > today or (end - start).days > MAX_DAYS:
        raise SeriesError('INVALID_ARGUMENT', 'Use an ordered, nonfuture window of at most ten years.')
    return start, end


def _confirmed(day, symbol, now):
    kr = symbol.isdigit()
    local = now.astimezone(ZoneInfo('Asia/Seoul' if kr else 'America/New_York'))
    # Conservative fixed regular-session close, including DST. Ten-minute Toss
    # publication margin also applied to US. Early-close days wait until 16:10.
    close = time(15, 40) if kr else time(16, 10)
    return day < local.date() or (day == local.date() and local.time().replace(tzinfo=None) >= close)


def _read_symbol(client, symbol, start, end, now, warnings):
    points, before, complete = {}, None, False
    for _ in range(MAX_PAGES):
        page = client.get_candles(symbol, interval='1d', count=200, adjusted=True,
            before=before, start_date=start.isoformat(), end_date=end.isoformat(),
            _validate_page=validate_candle_page)
        if page['reason_codes'] or page['complete'] is None:
            raise SeriesError('INVALID_CANDLES', 'Candle paging quality is unverified.')
        for row in page['candles']:
            day = _stamp(row['timestamp']).date()
            if not start <= day <= end:
                raise SeriesError('INVALID_CANDLES', 'Candle lies outside requested dates.')
            if not _confirmed(day, symbol, now):
                warning = f'{symbol}: unconfirmed current/future session bar excluded (close + 10-minute Toss margin).'
                if warning not in warnings:
                    warnings.append(warning)
                continue
            value = float(row['closePrice'])
            label = day.isoformat()
            if label in points and points[label] != value:
                raise SeriesError('INVALID_CANDLES', 'Conflicting closes at a page boundary.')
            points[label] = value
        if page['complete']:
            complete = True
            break
        cursor = page['next_cursor']
        if not cursor or cursor == before:
            raise SeriesError('INVALID_CANDLES', 'Candle paging did not advance.')
        before = cursor
    if not complete:
        raise SeriesError('PAGE_LIMIT', 'Candle page budget exceeded; use a shorter window.')
    if not points:
        raise SeriesError('NO_DATA', 'No confirmed daily closes in the requested window.')
    return points


def _sample(dates, limit=260):
    weeks = {}
    for day in dates:
        weeks[_date(day).isocalendar()[:2]] = day
    selected = sorted({dates[0], *weeks.values(), dates[-1]})
    if len(selected) > limit:
        step = math.ceil((len(selected) - 1) / (limit - 2))
        selected = sorted({*selected[::step], selected[-1]})
    return selected


def build_market_series(client, symbols, period='1mo', start_date=None, end_date=None,
                        benchmark=None, base=100, *, now=None):
    now = now or datetime.now(timezone.utc)
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 8
            or any(not isinstance(s, str) or len(s) > 32 for s in symbols)
            or isinstance(base, bool) or not isinstance(base, (int, float))
            or not math.isfinite(base) or not 0 < base <= 1_000_000):
        raise SeriesError('INVALID_ARGUMENT', 'Use 1–8 symbols and a finite base in (0, 1000000].')
    start, end = _window(period, start_date, end_date, now)
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols))
    bench = None
    if benchmark is not None:
        if not isinstance(benchmark, str) or not 1 <= len(benchmark.strip()) <= 32:
            raise SeriesError('INVALID_ARGUMENT', 'benchmark must be a symbol or a supported alias.')
        benchmark = benchmark.strip().upper()
        bench = {'symbol': ALIASES.get(benchmark, (benchmark,))[0]}
        if benchmark in ALIASES:
            bench.update(proxy_for=benchmark, name=ALIASES[benchmark][1], kind='proxy')
        symbols = list(dict.fromkeys([*symbols, bench['symbol']]))
    if len(symbols) > 8:
        raise SeriesError('INVALID_ARGUMENT', 'At most eight distinct symbols including the benchmark.')
    warnings = ['Native currency only (KRW/USD); no FX conversion or dividend reinvestment.',
                'Adjusted daily candles requested; split/dividend adjustment and historical coverage are unverified.']
    errors, raw, names = [], {}, {}
    for symbol in symbols:
        if re.fullmatch(r'(?:[0-9]{6}|[A-Z]{1,10}(?:[.-][A-Z]{1,2})?)', symbol) is None:
            errors.append({'symbol': symbol, 'code': 'INVALID_SYMBOL', 'message': 'Use a six-digit KR code or US ticker.'})
            continue
        try:
            raw[symbol] = _read_symbol(client, symbol, start, end, now, warnings)
        except SeriesError as exc:
            errors.append({'symbol': symbol, 'code': exc.code, 'message': str(exc)})
        except Exception:
            # Provider errors can carry arbitrary upstream text; never echo it.
            errors.append({'symbol': symbol, 'code': 'PROVIDER_ERROR', 'message': 'Toss daily candles unavailable.'})
    if raw:
        try:
            records = client.get_stock_info(list(raw))
            if isinstance(records, list):
                names = {r['symbol']: r['name'][:120] for r in records if isinstance(r, dict)
                         and r.get('symbol') in raw and isinstance(r.get('name'), str)}
        except Exception:
            pass
    common = sorted(set.intersection(*(set(p) for p in raw.values()))) if raw else []
    if raw and not common:
        errors.append({'symbol': '*', 'code': 'NO_COMMON_DATES', 'message': 'Successful series have no shared confirmed dates.'})
    if any(len(points) != len(common) for points in raw.values()):
        warnings.append('Intersection of successful series calendars only; unmatched dates removed, no fill.')
    if bench and bench['symbol'] not in raw:
        warnings.append('Benchmark unavailable; vs_benchmark_pp is null.')
    if errors:
        warnings.append('Partial symbol failures are listed in errors; inspect before comparing.')
    returns = {symbol: (points[common[-1]] / points[common[0]] - 1) * 100 for symbol, points in raw.items()} if common else {}
    bench_return = returns.get(bench['symbol']) if bench else None
    data = {'series': [], 'table': [], 'base_date': common[0] if common else None,
            'benchmark': bench, 'as_of': common[-1] if common else None,
            'warnings': warnings, 'source_line': '', 'errors': errors, 'sampling': 'daily'}
    selected = _sample(common) if len(common) > 260 else common

    def render():
        data['series'] = []
        for symbol, points in raw.items():
            if not common:
                continue
            name = names.get(symbol, symbol)
            if bench and bench['symbol'] == symbol and 'proxy_for' in bench:
                name = f"{bench['name']} ({bench['proxy_for']} proxy)"
            data['series'].append({'symbol': symbol, 'name': name,
                'currency': 'KRW' if symbol.isdigit() else 'USD', 'provider': 'toss', 'price_basis': PRICE_BASIS,
                'points': [{'date': day, 'close': points[day], 'index': round(points[day] / points[common[0]] * base, 6)} for day in selected]})
    render()
    # FastMCP also sends a pretty text copy alongside structuredContent.
    # Reserve room for both, table/metadata and UTF-8 names in the wire budget.
    if len(json.dumps(data, ensure_ascii=False).encode()) > 25_000:
        selected = _sample(common, min(260, max(16, 20_000 // (len(raw) * 100))))
        render()
    if selected != common:
        data['sampling'] = 'weekly (last common close per ISO week; first/last retained; multiweek stride if needed)'
        warnings.append('Weekly-sampled to fit 260 points per series and response size budget; period endpoints retained.')
    for symbol, points in raw.items():
        if common:
            data['table'].append({'symbol': symbol, 'first_date': common[0], 'last_date': common[-1],
                'first_close': points[common[0]], 'last_close': points[common[-1]],
                'period_return_pct': round(returns[symbol], 6),
                'vs_benchmark_pp': None if bench_return is None else round(returns[symbol] - bench_return, 6)})
    parts = ['Toss daily close', PRICE_BASIS, 'native currency only']
    if bench and 'proxy_for' in bench:
        parts.append(f"{bench['proxy_for']} = {bench['name']} proxy")
    if data['sampling'] != 'daily':
        parts.append('weekly-sampled')
    parts.append('as of ' + (data['as_of'] or 'unavailable'))
    data['source_line'] = ' · '.join(parts)
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) >= 98_000:
        raise SeriesError('RESPONSE_LIMIT', 'Series response exceeded the size budget; use fewer symbols.')
    return data
