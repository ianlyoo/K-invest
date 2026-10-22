"""SEC: facts merged per period across concept tags; TTM net income; fiscal_year_end.

Synthetic CompanyFacts in the shape of an issuer that moved from NetIncomeLoss to ProfitLoss
(as some issuers do): NetIncomeLoss stops after FY2024, ProfitLoss carries FY2025 and FY2026.
The real SECClient path runs over a mocked HTTP transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import provider_pacing  # noqa: E402
import sec_api  # noqa: E402

CIK = "0000000042"
FY23 = ("2022-10-31", "2023-10-29")
FY24 = ("2023-10-30", "2024-11-03")
FY25 = ("2024-11-04", "2025-11-02")


def _fact(start, end, val, fy, fp, form, filed):
    return {"start": start, "end": end, "val": val, "fy": fy, "fp": fp, "form": form, "filed": filed}


def _quarters(scale):
    """FY2025 Q1/Q2 direct, Q3 YTD; FY2026 Q1-Q3 direct; FY2025 annual."""
    return [
        _fact(*FY25, 10 * scale, 2025, "FY", "10-K", "2025-12-18"),
        _fact("2024-11-04", "2025-02-02", 2 * scale, 2025, "Q1", "10-Q", "2025-03-12"),
        _fact("2025-02-03", "2025-05-04", 2 * scale + 1, 2025, "Q2", "10-Q", "2025-06-11"),
        _fact("2024-11-04", "2025-08-03", 7 * scale, 2025, "Q3", "10-Q", "2025-09-10"),
        _fact("2025-11-03", "2026-02-01", 3 * scale, 2026, "Q1", "10-Q", "2026-03-11"),
        _fact("2026-02-02", "2026-05-03", 3 * scale + 1, 2026, "Q2", "10-Q", "2026-06-10"),
        _fact("2026-05-04", "2026-08-02", 3 * scale + 2, 2026, "Q3", "10-Q", "2026-09-09"),
    ]


def _roll_forward(scale):
    """Equity roll-forward rows: earlier quarters of the year repeated under the later 10-Q's fp."""
    return [
        _fact("2025-11-03", "2026-02-01", 3 * scale, 2026, "Q2", "10-Q", "2026-06-10"),
        _fact("2025-11-03", "2026-02-01", 3 * scale, 2026, "Q3", "10-Q", "2026-09-09"),
        _fact("2026-02-02", "2026-05-03", 3 * scale + 1, 2026, "Q3", "10-Q", "2026-09-09"),
        _fact("2024-11-04", "2025-02-02", 2 * scale, 2025, "Q3", "10-Q", "2025-09-10"),
    ]


def _companyfacts():
    net_income_loss = [
        _fact(*FY23, 100, 2023, "FY", "10-K", "2023-12-14"),
        _fact(*FY24, 120, 2024, "FY", "10-K", "2024-12-20"),
        _fact("2019-05-06", "2019-08-04", 30, 2019, "Q3", "10-Q", "2019-09-12"),
    ]
    profit_loss = [_fact(*FY24, 121, 2025, "FY", "10-K", "2025-12-18")] + _roll_forward(20) + _quarters(20)
    return {
        "cik": 42,
        "entityName": "Synthetic Semis Inc",
        "facts": {"us-gaap": {
            "NetIncomeLoss": {"units": {"USD": net_income_loss}},
            "ProfitLoss": {"units": {"USD": profit_loss}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": _quarters(100)}},
        }},
    }


def _client(monkeypatch, submissions):
    monkeypatch.setattr(sec_api, "_SEC_USER_AGENT", "InvestMCP fixtures/1.0 (sec-fixtures@fixtures.test)")
    monkeypatch.setattr(provider_pacing, "SEC_BUDGET", provider_pacing.RequestBudget(0))
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        if url.endswith("/files/company_tickers.json"):
            return httpx.Response(200, json={"0": {"cik_str": 42, "ticker": "SYNX", "title": "Synthetic"}})
        if url.endswith(f"/companyfacts/CIK{CIK}.json"):
            return httpx.Response(200, json=_companyfacts())
        if url.endswith(f"/submissions/CIK{CIK}.json") and submissions is not None:
            return httpx.Response(200, json=submissions)
        return httpx.Response(404)

    client = sec_api.SECClient()
    client._http = httpx.Client(transport=httpx.MockTransport(handler), headers=sec_api._sec_headers())
    return client, seen


SUBMISSIONS = {"fiscalYearEnd": "1102", "filings": {"recent": {
    "form": ["10-Q"], "accessionNumber": ["0000000042-26-000009"], "filingDate": ["2026-09-09"]}}}


def test_annual_net_income_takes_each_year_from_the_first_tag_reporting_it(monkeypatch):
    client, _ = _client(monkeypatch, SUBMISSIONS)
    annuals = {row["fiscal_year"]: row for row in client.get_financials("SYNX")["annuals"]}
    assert annuals[2025]["net_income"]["value"] == 200
    assert annuals[2025]["net_income"]["concept"] == "ProfitLoss"
    assert annuals[2024]["net_income"]["value"] == 120  # NetIncomeLoss is listed first
    assert annuals[2024]["net_income"]["concept"] == "NetIncomeLoss"
    assert annuals[2023]["net_income"]["concept"] == "NetIncomeLoss"


def test_ttm_net_income_sums_the_latest_four_quarters_of_one_concept(monkeypatch):
    client, _ = _client(monkeypatch, SUBMISSIONS)
    data = client.get_ttm_financials("SYNX")
    ttm = data["ttm"]
    assert [q["period_end"] for q in ttm["quarters_included"]] == ["2026-08-02", "2026-05-03", "2026-02-01", "2025-11-02"]
    # Q4 FY2025 = FY 200 - Q1..Q3 YTD 140, all ProfitLoss; FY2026 Q1-Q3 = 60 + 61 + 62
    assert ttm["net_income"]["value"] == 60 + 61 + 62 + (200 - 140)
    assert ttm["net_income"]["concepts"] == ["ProfitLoss"]
    assert ttm["revenue"]["value"] == 300 + 301 + 302 + (1000 - 700)
    q4 = next(q for q in data["quarters"] if q["period_end"] == "2025-11-02")
    assert q4["net_income"]["concept"] == "ProfitLoss" and q4["net_income"]["formula"] == "FY - Q1_Q3_ytd"
    assert "operating_cash_flow" in ttm["missing_metrics"]


def test_equity_roll_forward_rows_do_not_replace_the_filing_quarter(monkeypatch):
    client, _ = _client(monkeypatch, SUBMISSIONS)
    quarters = {(q["fiscal_year"], q["fiscal_period"]): q for q in client.get_ttm_financials("SYNX")["quarters"]}
    assert [quarters[(2026, fp)]["net_income"]["value"] for fp in ("Q1", "Q2", "Q3")] == [60, 61, 62]
    assert [quarters[(2026, fp)]["net_income"]["period_end"] for fp in ("Q1", "Q2", "Q3")] == [
        "2026-02-01", "2026-05-03", "2026-08-02"]
    assert quarters[(2025, "Q4")]["net_income"]["value"] == 200 - 140


def test_a_derived_quarter_never_mixes_two_concepts(monkeypatch):
    client, _ = _client(monkeypatch, SUBMISSIONS)
    quarters = client.get_ttm_financials("SYNX")["quarters"]
    # NetIncomeLoss has FY2024 but no FY2024 Q1-Q3, ProfitLoss has no FY2024 quarters either:
    # no FY2024 Q4 is derived from NetIncomeLoss FY minus ProfitLoss YTD.
    assert not any(q["fiscal_year"] == 2024 and q["fiscal_period"] == "Q4" and "net_income" in q for q in quarters)


def test_fiscal_year_end_comes_from_submissions_fetched_once(monkeypatch):
    client, seen = _client(monkeypatch, SUBMISSIONS)
    data = client.get_financials("SYNX")
    assert data["entity"]["fiscal_year_end"] == "1102"
    assert "missing_fiscal_year_end" not in data["warnings"]
    assert sum(url.endswith(f"/submissions/CIK{CIK}.json") for url in seen) == 1


@pytest.mark.parametrize("submissions", [None, {"fiscalYearEnd": ""}, {"fiscalYearEnd": "1340"}])
def test_fiscal_year_end_unavailable_is_null_with_a_warning(monkeypatch, submissions):
    client, _ = _client(monkeypatch, submissions)
    data = client.get_ttm_financials("SYNX")
    assert data["entity"]["fiscal_year_end"] is None
    assert "missing_fiscal_year_end" in data["warnings"]


def test_ttm_tool_envelope_carries_the_merged_figures(monkeypatch):
    import server

    client, _ = _client(monkeypatch, SUBMISSIONS)
    monkeypatch.setattr(server, "_get_sec", lambda: client)
    result = server.get_ttm_financials("SYNX")
    assert result["ok"]
    assert result["data"]["ttm"]["net_income"]["value"] == 243
    assert result["data"]["entity"]["fiscal_year_end"] == "1102"
