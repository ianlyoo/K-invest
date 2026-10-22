#!/usr/bin/env python3
"""yfinance data client — financials, key metrics, and market data.

Uses yfinance (no API key required) for US, Korean (.KS/.KQ), and Japanese stocks.
Provides financial statements, key metrics, and historical OHLCV data.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

import yfinance as yf

from kinvest_common import YFINANCE_LOCK, synchronized
from yfinance_consensus import analyst_consensus
from yfinance_metrics import (
    dividend_yield_pct,
    growth_fields,
    null_reason,
    pct,
    percent_value,
)

logger = logging.getLogger("k-invest.yf")

# One bounded background worker shares the existing Yahoo transport lock. A cold
# production request queues a refresh and returns unresolved immediately; neither
# repeated tool calls nor changing query windows cause repeated network reads.
_DIVIDEND_CACHE = {}
_DIVIDEND_CACHE_LOCK = Lock()
_DIVIDEND_REFRESH = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dividend-reference')
_DIVIDEND_CACHE_LIMIT = 256


class _DividendJob:
    def __init__(self, future, previous):
        self.future, self.previous = future, previous

    def done(self):
        return self.future.done()

    def result(self, timeout=None):
        return self.future.result(timeout=timeout)

    def cancel(self):
        return self.future.cancel()


def _dividend_now():
    return datetime.now(timezone.utc)


def _dividend_unavailable(symbol, currency, start, end, reason):
    return {'rows': [], 'coverage': [{'symbol': symbol, 'currency': currency, 'start': start, 'end': end,
            'status': 'limited', 'reasons': [reason]}]}


def _schedule_bound(value, currency):
    if len(value) == 10:
        return datetime.fromisoformat(value).date()
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.utcoffset() is None:
        raise ValueError('INVALID_SCHEDULE_RANGE')
    return stamp.astimezone(ZoneInfo('America/New_York' if currency == 'USD' else 'Asia/Seoul')).date()


def _project_schedule(data, symbol, currency, start, end, instrument=None):
    result = deepcopy(data)
    try:
        first, last = _schedule_bound(start, currency), _schedule_bound(end, currency)
        proof = result['coverage'][0]
        from toss_dividends import check_instrument_identity, estimated_coverage
        if not estimated_coverage(proof):
            if proof.get('status') != 'limited' or not proof.get('reasons'):
                raise ValueError('INVALID_SCHEDULE_RANGE')
            # Invalid evidence stays invalid; projection must not mask its cause
            # with an incidental boundary failure or retain numeric rows.
            result['rows'] = []
            proof.update(symbol=symbol, currency=currency, start=start, end=end)
            return result
        if currency == 'KRW':
            check_instrument_identity(result.get('binding') or {}, symbol, instrument)
        if first > last or first.isoformat() < proof['start'] or first.isoformat() > proof['end']:
            missing = _dividend_unavailable(symbol, currency, start, end, 'DIVIDEND_WINDOW_OUTSIDE_DAILY_CACHE')
            missing['cache_evidence'] = result.get('cache_evidence')
            return missing
        # A request past the copy's last fully observed day is clipped to that day,
        # never extended: the proof's end bounds which intervals can use it.
        last = min(last, date.fromisoformat(proof['end']))
        proof.update(start=first.isoformat(), end=last.isoformat())
        result['rows'] = [r for r in result['rows'] if r['payment_start'] <= last.isoformat()
                          and r['payment_end'] >= first.isoformat()]
        return result
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith(('DIVIDEND_', 'INVALID_TOSS_')) else 'INVALID_SCHEDULE_RANGE'
        return _dividend_unavailable(symbol, currency, start, end, reason)
    except (KeyError, TypeError, IndexError):
        return _dividend_unavailable(symbol, currency, start, end, 'INVALID_SCHEDULE_RANGE')


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace(",", "")
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (ValueError, TypeError):
        return None


def _row_value(df, keys: list[str]) -> float | None:
    """Extract a row value from a yfinance DataFrame by trying multiple key names."""
    if df is None or df.empty:
        return None
    for key in keys:
        if key in df.index:
            val = df.loc[key].iloc[0] if len(df.loc[key]) > 0 else None
            return _safe_float(val)
    return None


def _period_end(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "date", None)
    if callable(isoformat):
        return str(value.date())
    return str(value)[:10] if str(value) else None


def _row_with_period(df, keys: list[str]) -> dict[str, Any] | None:
    if df is None or df.empty:
        return None
    for key in keys:
        if key not in df.index:
            continue
        row = df.loc[key]
        if len(row) == 0:
            continue
        return {
            "value": _safe_float(row.iloc[0]),
            "period_type": "FY/provider_latest",
            "period_end": _period_end(row.index[0]),
        }
    return None


class YFinanceClient:
    """yfinance-based data client (no API key required).

    Supports US, Korean (.KS/.KQ), and Japanese stocks.
    """

    def __init__(self):
        # yfinance uses requests internally; no httpx client needed
        pass

    def get_dividend_schedule(self, symbol: str, *, currency: str, start: str, end: str,
                              background: bool = False, instrument: dict | None = None) -> dict:
        """Daily, per-symbol validated history cache shared across client instances.

        The production adapter uses background=True: a cold/missing/expired day
        uses a persisted good copy immediately, or returns refresh-pending when
        none exists. A single worker and per-symbol file lock refresh through this
        client at most once per market calendar day (New York for USD, Seoul for
        KRW) across processes. Failed refreshes keep the old copy's observation
        time/coverage; query bounds never extend it.
        The cache lives under kinvest_common.cache_dir(), with 256 in-memory symbols
        and three years per schedule. Synchronous mode supports mocked prewarming.
        """
        if not isinstance(symbol, str) or not (
                currency == 'USD' and re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,19}', symbol)
                or currency == 'KRW' and re.fullmatch(r'[0-9A-Z]{6}(?:\.K[QS])?', symbol)):
            return _dividend_unavailable(symbol, currency, start, end, 'DIVIDEND_SYMBOL_BINDING_UNAVAILABLE')
        now = _dividend_now()
        from toss_dividend_cache import (
            attempted_today,
            load,
            market_day,
            refresh,
            schedule,
        )

        key = (market_day(currency, now).isoformat(), symbol, currency)
        with _DIVIDEND_CACHE_LOCK:
            for old in list(_DIVIDEND_CACHE):
                if old[0] != market_day(old[2], now).isoformat():
                    _DIVIDEND_CACHE.pop(old).cancel()
            future = _DIVIDEND_CACHE.get(key)
            if future is None:
                if len(_DIVIDEND_CACHE) >= _DIVIDEND_CACHE_LIMIT:
                    return _dividend_unavailable(symbol, currency, start, end, 'DIVIDEND_CACHE_CAPACITY')
                previous = load(symbol, currency, now)
                if attempted_today(previous, now):
                    task = Future()
                    task.set_result(schedule(previous) or _dividend_unavailable(symbol, currency, start, end,
                        previous['failure_reason'] or 'PUBLIC_DIVIDEND_SCHEDULE_UNAVAILABLE'))
                else:
                    identity = deepcopy(instrument)
                    task = _DIVIDEND_REFRESH.submit(refresh, symbol, currency,
                        lambda observed: self._fetch_daily_dividends(symbol, currency, observed, identity),
                        clock=_dividend_now)
                future = _DividendJob(task, previous)
                _DIVIDEND_CACHE[key] = future
        if background and not future.done():
            previous_data = schedule(future.previous, pending=True)
            if previous_data:
                return _project_schedule(previous_data, symbol, currency, start, end, instrument)
            return _dividend_unavailable(symbol, currency, start, end, 'PUBLIC_DIVIDEND_REFRESH_PENDING')
        try:
            return _project_schedule(future.result(), symbol, currency, start, end, instrument)
        except Exception:
            return _dividend_unavailable(symbol, currency, start, end, 'PUBLIC_DIVIDEND_SCHEDULE_UNAVAILABLE')

    @synchronized(YFINANCE_LOCK)
    def _fetch_daily_dividends(self, symbol, currency, now, instrument=None):
        from toss_dividends import (
            MAX_PAYMENT_LAG_DAYS,
            check_instrument_identity,
            estimated_coverage,
            instrument_reference,
            reference_missing,
            schedule_from_responses,
            validate_calendar,
        )

        zone = ZoneInfo('America/New_York' if currency == 'USD' else 'Asia/Seoul')
        # With an ex+0 lower bound, today's incomplete history cannot even
        # support estimated absence. Keep the last fully observed market date.
        last = now.astimezone(zone).date() - timedelta(days=1)
        first = last - timedelta(days=3 * 366)
        lo = int(datetime.combine(first - timedelta(days=MAX_PAYMENT_LAG_DAYS + 2),
                                  datetime.min.time(), timezone.utc).timestamp())
        hi = int(now.timestamp())
        try:
            data = yf.Ticker(symbol)._data
            bound = instrument_reference(symbol, instrument) if currency == 'KRW' else symbol
            base = symbol.split('.')[0]
            references = [bound] if bound else [base + '.KS', base + '.KQ']
            candidates, failures = [], []
            for reference in references:
                endpoint = 'quoteSummary'
                try:
                    calendar = data.get_raw_json(f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{reference}',
                        params={'modules': 'calendarEvents,price', 'formatted': 'false'}, timeout=5)
                    if len(references) == 2 and reference_missing(calendar, reference, endpoint):
                        continue
                    if len(references) == 2:
                        validate_calendar(calendar, reference, currency)
                    endpoint = 'chart'
                    chart = data.get_raw_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{reference}',
                        params={'period1': lo, 'period2': hi, 'interval': '1d', 'events': 'div'}, timeout=5)
                    if len(references) == 2 and reference_missing(chart, reference, endpoint):
                        continue
                    result = schedule_from_responses(calendar, chart, symbol=reference, currency=currency,
                        start=first.isoformat(), end=last.isoformat(), lo=lo, hi=hi, as_of=now)
                    if not estimated_coverage(result['coverage'][0]):
                        failures.append(result)
                        continue
                    if currency == 'KRW':
                        price, meta = calendar['quoteSummary']['result'][0]['price'], chart['chart']['result'][0]['meta']
                        if any(k in row and (not isinstance(row[k], str) or not row[k]) for row in (price, meta)
                               for k in ('shortName', 'longName', 'name', 'isin', 'isinCode')):
                            raise ValueError('INVALID_REFERENCE_IDENTITY')
                        binding = {'reference_symbol': reference, 'basis': 'toss-stock-info-v1' if bound and instrument
                            and instrument.get('market') else 'unique-validated-kr-schedule',
                            'names': [v for row in (price, meta) for k in ('shortName', 'longName', 'name')
                                      if isinstance((v := row.get(k)), str) and v],
                            'isins': [v for row in (price, meta) for k in ('isin', 'isinCode')
                                      if isinstance((v := row.get(k)), str) and v]}
                        if binding['basis'] == 'toss-stock-info-v1':
                            binding['toss_isin'] = instrument['isinCode']
                        check_instrument_identity(binding, symbol, instrument)
                        result['binding'] = binding
                    candidates.append((reference, result))
                except Exception as exc:
                    response = getattr(exc, 'response', None)
                    if len(references) == 2 and getattr(response, 'status_code', None) == 404:
                        try:
                            if reference_missing(response.json(), reference, endpoint):
                                continue
                        except (ValueError, TypeError, AttributeError):
                            pass
                    # An unavailable candidate is not proof that only the other
                    # exchange exists; a dual probe with any outage stays unknown.
                    return _dividend_unavailable(symbol, currency, first.isoformat(), last.isoformat(),
                                                 'DIVIDEND_INSTRUMENT_UNRESOLVED')
            if len(candidates) != 1 or failures:
                # One answering reference (the other proven missing) keeps its specific reason.
                if not candidates and len(failures) == 1:
                    for proof in failures[0]['coverage']:
                        proof.update(reference_symbol=proof['symbol'], symbol=symbol)
                    return failures[0]
                return _dividend_unavailable(symbol, currency, first.isoformat(), last.isoformat(),
                                             'DIVIDEND_INSTRUMENT_UNRESOLVED')
            reference, result = candidates[0]
            if currency == 'KRW' and '.' in symbol and symbol != reference:
                return _dividend_unavailable(symbol, currency, first.isoformat(), last.isoformat(),
                                             'DIVIDEND_INSTRUMENT_MISMATCH')
            for row in [*result['rows'], *result['coverage']]:
                row.update(symbol=symbol, reference_symbol=reference)
            return result
        except Exception:
            return _dividend_unavailable(symbol, currency, first.isoformat(), last.isoformat(),
                                         'PUBLIC_DIVIDEND_SCHEDULE_UNAVAILABLE')

    @synchronized(YFINANCE_LOCK)
    def get_financials(self, symbol: str) -> dict[str, Any]:
        """Get key financial data from yfinance.

        Args:
            symbol: Stock ticker. US: 'AAPL', Korean: '005930.KS', Japanese: '7203.T'

        Returns:
            Dict with income statement, balance sheet, cash flow, and key metrics.
        """
        ticker = yf.Ticker(symbol)
        warnings: list[str] = []

        try:
            info = ticker.info or {}
        except Exception as e:
            info = {}
            warnings.append(f"info failed: {e}")

        try:
            income = ticker.income_stmt
        except Exception as e:
            income = None
            warnings.append(f"income statement failed: {e}")

        try:
            balance = ticker.balance_sheet
        except Exception as e:
            balance = None
            warnings.append(f"balance sheet failed: {e}")

        try:
            cashflow = ticker.cash_flow
        except Exception as e:
            cashflow = None
            warnings.append(f"cash flow failed: {e}")

        result: dict[str, Any] = {
            "symbol": symbol.upper(),
            "currency": info.get("currency", ""),
            "exchange": info.get("exchange", ""),
            "market_cap": _safe_float(info.get("marketCap")),
            "enterprise_value": _safe_float(info.get("enterpriseValue")),
            "shares_outstanding": _safe_float(info.get("sharesOutstanding")),
            "warnings": warnings,
        }

        # Income Statement
        if income is not None and not income.empty:
            result["income_statement"] = {
                "revenue": _row_with_period(income, ["Total Revenue", "Operating Revenue"]),
                "gross_profit": _row_with_period(income, ["Gross Profit"]),
                "operating_income": _row_with_period(income, ["Operating Income"]),
                "net_income": _row_with_period(income, ["Net Income", "Net Income Common Stockholders"]),
                "ebit": _row_with_period(income, ["EBIT"]),
                "ebitda": _row_with_period(income, ["EBITDA"]),
                "rd_expense": _row_with_period(income, ["Research And Development", "Research Development"]),
                "interest_expense": _row_with_period(income, ["Interest Expense", "Interest Expense Non Operating"]),
                "operating_expense": _row_with_period(income, ["Operating Expense", "Total Expenses"]),
            }

        # Balance Sheet
        if balance is not None and not balance.empty:
            result["balance_sheet"] = {
                "total_assets": _row_value(balance, ["Total Assets"]),
                "total_liabilities": _row_value(balance, ["Total Liabilities Net Minority Interest", "Total Liabilities"]),
                "current_assets": _row_value(balance, ["Current Assets", "Total Current Assets"]),
                "current_liabilities": _row_value(balance, ["Current Liabilities", "Total Current Liabilities"]),
                "stockholders_equity": _row_value(balance, ["Stockholders Equity", "Total Equity Gross Minority Interest"]),
                "total_debt": _row_value(balance, ["Total Debt", "Long Term Debt And Capital Lease Obligation"]),
                "cash": _row_value(balance, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]),
                "goodwill": _row_value(balance, ["Goodwill And Other Intangible Assets", "Goodwill"]),
            }

        # Cash Flow
        if cashflow is not None and not cashflow.empty:
            ocf = _row_value(cashflow, ["Operating Cash Flow", "Total Cash From Operating Activities"])
            capex = _row_value(cashflow, ["Capital Expenditure", "Capital Expenditures"])
            fcf = _row_value(cashflow, ["Free Cash Flow"])
            if fcf is None and ocf is not None and capex is not None:
                fcf = ocf + capex
            result["cash_flow"] = {
                "operating_cash_flow": ocf,
                "capex": capex,
                "free_cash_flow": fcf,
                "dividends": _row_value(cashflow, ["Cash Dividends Paid", "Common Stock Dividend Paid"]),
                "depreciation": _row_value(cashflow, ["Depreciation And Amortization", "Depreciation"]),
            }

        normalized_dividend_yield, dividend_yield_meta = dividend_yield_pct(info)

        # Valuation metrics from info
        valuation: dict[str, Any] = {
            "trailing_pe": _safe_float(info.get("trailingPE")),
            "forward_pe": _safe_float(info.get("forwardPE")),
            "peg_ratio": _safe_float(info.get("pegRatio")),
            "price_to_book": _safe_float(info.get("priceToBook")),
            "price_to_sales": _safe_float(info.get("priceToSalesTrailing12Months")),
            "ev_to_ebitda": _safe_float(info.get("enterpriseToEbitda")),
            "ev_to_revenue": _safe_float(info.get("enterpriseToRevenue")),
            "dividend_yield_pct": normalized_dividend_yield,
            "beta": _safe_float(info.get("beta")),
        }
        if dividend_yield_meta is not None:
            valuation["dividend_yield_meta"] = dividend_yield_meta
        result["valuation"] = valuation

        # Profitability metrics
        result["profitability"] = {
            "gross_margin_pct": pct(info.get("grossMargins")),
            "operating_margin_pct": pct(info.get("operatingMargins")),
            "net_margin_pct": pct(info.get("profitMargins")),
            "roe_pct": pct(info.get("returnOnEquity")),
            "roa_pct": pct(info.get("returnOnAssets")),
            "roce_pct": pct(info.get("returnOnCapitalEmployed")),
            "period_type": "TTM/provider_latest",
        }

        return result

    @synchronized(YFINANCE_LOCK)
    def get_key_metrics(self, symbol: str) -> dict[str, Any]:
        """Get key financial metrics summary from yfinance info.

        Args:
            symbol: Stock ticker (e.g., 'AAPL', '005930.KS')

        Returns:
            Dict with key metrics: P/E, PEG, P/B, margins, ROE, etc.
        """
        ticker = yf.Ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}

        normalized_dividend_yield, dividend_yield_meta = dividend_yield_pct(info)
        null_reasons = {
            key: reason for key, reason in {
                "trailing_pe": null_reason(info, "trailingPE"),
                "price_to_book": null_reason(info, "priceToBook"),
            }.items() if reason is not None
        }

        result = {
            "symbol": symbol.upper(),
            "name": info.get("longName", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "currency": info.get("currency", ""),
            "market_cap": _safe_float(info.get("marketCap")),
            "trailing_pe": _safe_float(info.get("trailingPE")),
            "forward_pe": _safe_float(info.get("forwardPE")),
            "peg_ratio": _safe_float(info.get("pegRatio")),
            "price_to_book": _safe_float(info.get("priceToBook")),
            "price_to_sales": _safe_float(info.get("priceToSalesTrailing12Months")),
            "ev_to_ebitda": _safe_float(info.get("enterpriseToEbitda")),
            "dividend_yield_pct": normalized_dividend_yield,
            "beta": _safe_float(info.get("beta")),
            "gross_margin_pct": pct(info.get("grossMargins")),
            "operating_margin_pct": pct(info.get("operatingMargins")),
            "net_margin_pct": pct(info.get("profitMargins")),
            "roe_pct": pct(info.get("returnOnEquity")),
            "roa_pct": pct(info.get("returnOnAssets")),
            "debt_to_equity_pct": percent_value(info.get("debtToEquity")),
            "period_type": "TTM/provider_latest",
            "current_ratio": _safe_float(info.get("currentRatio")),
            "quick_ratio": _safe_float(info.get("quickRatio")),
        }
        result.update(growth_fields(symbol, info))
        result["analyst_consensus"] = analyst_consensus(ticker, info)
        if dividend_yield_meta is not None:
            result["dividend_yield_meta"] = dividend_yield_meta
        if null_reasons:
            result["null_reasons"] = null_reasons
        return result

    @synchronized(YFINANCE_LOCK)
    def get_ohlcv(self, symbol: str, period: str = "1y") -> dict[str, Any]:
        """Get historical OHLCV data from yfinance.

        Args:
            symbol: Stock ticker (e.g., 'AAPL', '005930.KS')
            period: '1mo', '3mo', '6mo', '1y', '2y', '5y'

        Returns:
            Dict with OHLCV data summary (last price, 52w high/low, etc.)
        """
        ticker = yf.Ticker(symbol)
        try:
            hist = ticker.history(period=period)
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}

        if hist is None or hist.empty:
            return {"symbol": symbol, "error": "No data available"}

        last_row = hist.iloc[-1]
        return {
            "symbol": symbol.upper(),
            "last_close": _safe_float(last_row.get("Close")),
            "last_open": _safe_float(last_row.get("Open")),
            "last_high": _safe_float(last_row.get("High")),
            "last_low": _safe_float(last_row.get("Low")),
            "last_volume": _safe_float(last_row.get("Volume")),
            "period": period,
            "data_points": len(hist),
            "fifty_two_week_high": _safe_float(hist["High"].max()),
            "fifty_two_week_low": _safe_float(hist["Low"].min()),
            "period_return": _safe_float(
                (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
            ) if len(hist) > 1 else None,
        }

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
