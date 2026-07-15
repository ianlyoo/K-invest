#!/usr/bin/env python3
"""SEC EDGAR data client - financials (CompanyFacts) + insider trades (Form 4)."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx

from sec_derived import add_derived_metrics
from sec_insider import get_insider_trades
from sec_quality import add_earnings_quality
from sec_segments import get_segment_revenue
from sec_ttm import add_quarterly_ttm

logger = logging.getLogger("k-invest.sec")

DEFAULT_SEC_UA = "k-invest/2.0 (research@example.com)"
SEC_BASE = "https://data.sec.gov"
TEN_K_FORMS = {"10-K", "10-K/A"}
DURATION_METRICS = {
    "revenue", "net_income", "operating_income", "gross_profit", "rd_expense",
    "interest_expense", "operating_cash_flow", "capex",
}
SHARE_METRICS = {"shares_outstanding"}


def _sec_headers() -> dict[str, str]:
    return {
        "User-Agent": os.environ.get("SEC_USER_AGENT", DEFAULT_SEC_UA),
        "Accept-Encoding": "gzip, deflate",
    }


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _duration_days(item: dict[str, Any]) -> int | None:
    start = _parse_date(item.get("start"))
    end = _parse_date(item.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


class SECClient:
    """SEC EDGAR data client (read-only, no API key required)."""

    def __init__(self):
        self._http = httpx.Client(timeout=15.0, headers=_sec_headers())

    def _get_json(self, url: str) -> dict[str, Any] | None:
        try:
            resp = self._http.get(url)
            if resp.status_code != 200:
                logger.warning("SEC API %d: %s", resp.status_code, url)
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except httpx.HTTPError as exc:
            logger.warning("SEC API error: %s", exc)
            return None

    def _get_text(self, url: str) -> str | None:
        try:
            resp = self._http.get(url)
            if resp.status_code != 200:
                return None
            return resp.text
        except httpx.HTTPError as exc:
            logger.warning("SEC text fetch error: %s", exc)
            return None

    def get_cik(self, ticker: str) -> str | None:
        data = self._get_json("https://www.sec.gov/files/company_tickers.json")
        if not isinstance(data, dict):
            return None
        ticker_upper = ticker.upper().strip()
        for item in data.values():
            if isinstance(item, dict) and str(item.get("ticker", "")).upper() == ticker_upper:
                cik = item.get("cik_str")
                if cik is not None:
                    return f"{int(cik):010d}"
        return None

    def get_company_facts(self, cik: str) -> dict[str, Any]:
        data = self._get_json(f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json")
        return data if isinstance(data, dict) else {}

    def get_financials(self, ticker: str) -> dict[str, Any]:
        cik = self.get_cik(ticker)
        if not cik:
            return {"error": f"CIK not found for ticker {ticker}"}

        facts = self.get_company_facts(cik)
        if not facts:
            return {"error": f"CompanyFacts not available for {ticker} (CIK: {cik})"}

        concepts = {
            "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
            "net_income": ["NetIncomeLoss", "ProfitLoss"],
            "operating_income": ["OperatingIncomeLoss"],
            "total_assets": ["Assets"],
            "total_liabilities": ["Liabilities"],
            "stockholders_equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
            "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndShortTermInvestments"],
            "total_debt": ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndFinanceLeaseObligationsCurrent"],
            "current_assets": ["AssetsCurrent"],
            "current_liabilities": ["LiabilitiesCurrent"],
            "gross_profit": ["GrossProfit"],
            "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
            "rd_expense": ["ResearchAndDevelopmentExpense"],
            "interest_expense": ["InterestExpenseNonOperating", "InterestExpense"],
            "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
            "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
            "shares_outstanding": ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"],
        }

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        metric_by_year: dict[int, dict[str, Any]] = defaultdict(dict)
        warnings: list[str] = []

        for metric, tags in concepts.items():
            selected = self._select_metric_facts(us_gaap, metric, tags)
            if not selected:
                warnings.append(f"missing_metric:{metric}")
                continue
            for fiscal_year, fact in selected.items():
                metric_by_year[fiscal_year][metric] = {
                    "value": fact.get("val"),
                    "unit": fact.get("unit"),
                    "period_type": "FY",
                    "period_end": fact.get("end"),
                    "filed": fact.get("filed"),
                    "form": fact.get("form"),
                    "frame": fact.get("frame"),
                    "concept": fact.get("concept"),
                }

        annuals: list[dict[str, Any]] = []
        derived_metrics: set[str] = set()
        for fiscal_year in sorted(metric_by_year.keys(), reverse=True)[:5]:
            row: dict[str, Any] = {"fiscal_year": fiscal_year}
            row.update(metric_by_year[fiscal_year])
            add_derived_metrics(row)
            for metric in ("gross_profit", "free_cash_flow"):
                if isinstance(row.get(metric), dict) and row[metric].get("derived"):
                    derived_metrics.add(metric)
            period_ends = [str(v.get("period_end")) for v in metric_by_year[fiscal_year].values() if isinstance(v, dict) and v.get("period_end")]
            filed_dates = [str(v.get("filed")) for v in metric_by_year[fiscal_year].values() if isinstance(v, dict) and v.get("filed")]
            forms = [str(v.get("form")) for v in metric_by_year[fiscal_year].values() if isinstance(v, dict) and v.get("form")]
            row["period_end"] = max(period_ends) if period_ends else None
            row["filed"] = max(filed_dates) if filed_dates else None
            row["form"] = forms[0] if forms else None
            annuals.append(row)
        if "gross_profit" in derived_metrics:
            warnings = [warning for warning in warnings if warning != "missing_metric:gross_profit"]

        result = {
            "entity": {
                "ticker": ticker.upper(),
                "cik": cik,
                "name": facts.get("entityName") or facts.get("entity", {}).get("name", ""),
                "fiscal_year_end": facts.get("fiscalYearEnd") or facts.get("entity", {}).get("fiscalYearEnd", ""),
            },
            "annuals": annuals,
            "warnings": warnings,
            "experimental": False,
            "methodology": {
                "forms": sorted(TEN_K_FORMS),
                "period_filter": "fp=FY; fiscal_year is derived from period_end year; duration metrics require 300-380 day periods; original fiscal-year filing wins over later comparative rows",
            },
        }
        add_quarterly_ttm(result, facts)
        add_earnings_quality(result, facts)
        result["segment_revenue"] = get_segment_revenue(self._get_json, self._get_text, cik, ticker)
        return result

    def get_ttm_financials(self, ticker: str) -> dict[str, Any]:
        data = self.get_financials(ticker)
        return {"entity": data.get("entity"), "ttm": data.get("ttm"), "quarters": data.get("quarters"), "warnings": data.get("warnings", [])}

    def _select_metric_facts(
        self,
        us_gaap: dict[str, Any],
        metric: str,
        tags: list[str],
    ) -> dict[int, dict[str, Any]]:
        chosen: dict[int, dict[str, Any]] = {}
        for tag in tags:
            tag_data = us_gaap.get(tag)
            if not isinstance(tag_data, dict):
                continue
            units = tag_data.get("units", {})
            unit_name = "shares" if metric in SHARE_METRICS else "USD"
            values = units.get(unit_name) or units.get("USD") or units.get("shares") or []
            if not isinstance(values, list):
                continue
            for raw in values:
                if not isinstance(raw, dict):
                    continue
                normalized = self._normalize_company_fact(metric, tag, unit_name, raw)
                if normalized is None:
                    continue
                fiscal_year = normalized["fy"]
                previous = chosen.get(fiscal_year)
                if previous is None or self._fact_rank(normalized) > self._fact_rank(previous):
                    chosen[fiscal_year] = normalized
            if chosen:
                break
        return dict(sorted(chosen.items(), reverse=True)[:5])

    @staticmethod
    def _normalize_company_fact(
        metric: str,
        concept: str,
        unit_name: str,
        raw: dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(raw.get("form", "")).upper() not in TEN_K_FORMS:
            return None
        if str(raw.get("fp", "")).upper() != "FY":
            return None
        end_date = _parse_date(raw.get("end"))
        if end_date is None:
            return None
        fiscal_year = end_date.year
        source_fy = None
        raw_fy = raw.get("fy")
        try:
            if raw_fy is not None:
                source_fy = int(raw_fy)
        except (TypeError, ValueError):
            source_fy = None
        days = _duration_days(raw)
        if metric in DURATION_METRICS:
            if days is None or not 300 <= days <= 380:
                return None
        item = dict(raw)
        item["fy"] = fiscal_year
        item["source_fy"] = source_fy
        item["concept"] = concept
        item["unit"] = unit_name
        item["duration_days"] = days
        return item

    @staticmethod
    def _fact_rank(item: dict[str, Any]) -> tuple[int, int, int, str]:
        same_fiscal_year = 1 if item.get("source_fy") == item.get("fy") else 0
        form_rank = 1 if str(item.get("form", "")).upper() == "10-K/A" else 0
        duration = item.get("duration_days")
        duration_rank = 1 if duration is None or 330 <= int(duration) <= 370 else 0
        return (same_fiscal_year, form_rank, duration_rank, str(item.get("filed") or ""))

    def get_insider_trades(
        self, ticker: str, days_back: int = 180, detail_level: str = "summary"
    ) -> dict[str, Any]:
        return get_insider_trades(self, ticker, days_back, detail_level)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
