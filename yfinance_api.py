#!/usr/bin/env python3
"""yfinance data client — financials, key metrics, and market data.

Uses yfinance (no API key required) for US, Korean (.KS/.KQ), and Japanese stocks.
Provides financial statements, key metrics, and historical OHLCV data.
"""

from __future__ import annotations

import logging
from typing import Any

import yfinance as yf

from yfinance_consensus import analyst_consensus
from yfinance_metrics import dividend_yield_pct, growth_fields, null_reason, pct

logger = logging.getLogger("k-invest.yf")


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
            "debt_to_equity_pct": pct(info.get("debtToEquity")),
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
