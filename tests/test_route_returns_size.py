"""get_portfolio_returns' summary trims per-symbol realized rows and fixed metric fields
(Claude I04 on 2.15.1 stored the MTD default to a file). Synthetic numbers, live key structure."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge_budget as budget  # noqa: E402

import response_shape  # noqa: E402
import server  # noqa: E402
import tools_v2  # noqa: E402

stub = budget.stub
MOVED_ROW_KEYS = {"name", "market", "quantity_sold", "proceeds", "cost_basis", "realized_pnl", "realized_fx_krw",
                  "fees", "taxes", "sell_count", "fees_status", "taxes_status"}


@pytest.fixture
def performance(stub, monkeypatch):
    monkeypatch.setattr(server, "get_portfolio_performance", budget._performance)


def _before_route():
    """The 2.15.1 summary: the same section with hooks added later."""
    out = tools_v2._Sections()
    out.add("returns", "get_portfolio_performance", budget._performance(period="MTD", portfolio_scope="equity"))
    return out.envelope(period="MTD", scope="equity", hooks=tools_v2._RETURNS_SUMMARY[:2])


def test_summary_rows_keep_the_answer_keys_and_full_keeps_every_key(performance):
    summary = tools_v2.get_portfolio_returns(period="MTD")
    full = tools_v2.get_portfolio_returns(period="MTD", detail_level="full")
    rows, full_rows = summary["data"]["returns"]["realized_by_symbol"], full["data"]["returns"]["realized_by_symbol"]
    assert len(rows) == len(full_rows) == 10
    for row, whole in zip(rows, full_rows):
        assert set(row) == set(tools_v2._REALIZED_ROW) and row == {key: whole[key] for key in row}
        assert set(whole) - set(row) == MOVED_ROW_KEYS
    assert rows[3]["cost_basis_complete"] is False and rows[7]["reason_codes"] == ["TAXES_UNSUPPORTED", "FEES_UNKNOWN"]
    for group in (full["data"]["returns"]["metrics"], full["data"]["returns"]["risk"]["metrics"]):
        assert all("method_version" in metric and "required_inputs" in metric for metric in group.values())
    risk = summary["data"]["returns"]["risk"]["metrics"]
    assert all("method_version" not in metric and "required_inputs" not in metric for metric in risk.values())
    omitted = summary["meta"]["detail"]["omitted"]
    assert omitted["data.returns.risk.metrics.*.method_version"] == 7
    assert budget._kept_or_tallied(full, summary) == set()


def test_summary_is_smaller_than_the_2_15_1_summary(performance):
    summary = tools_v2.get_portfolio_returns(period="MTD")
    before = _before_route()
    # the generic summary pass already left out the null realized_fx_krw
    assert before["data"]["returns"]["realized_by_symbol"][0].keys() >= MOVED_ROW_KEYS - {"realized_fx_krw"}
    saved = budget._size(before) - budget._size(summary)
    assert saved >= 2500, saved
    assert budget._size(summary) <= budget.LIMIT


def test_a_reason_code_in_a_moved_row_key_is_tallied(stub, monkeypatch):
    def performance(**kwargs):
        env = budget._performance(**kwargs)
        env["data"]["realized_by_symbol"][0]["fees_status"] = {"status": "unknown", "reason_codes": ["SYN_ONLY_HERE"]}
        return env
    monkeypatch.setattr(server, "get_portfolio_performance", performance)
    summary = tools_v2.get_portfolio_returns()
    tally = summary["meta"]["detail"]["omitted_reason_codes"]
    assert tally["data.returns.realized_by_symbol[]"] == {"SYN_ONLY_HERE": 1}


def test_the_old_tool_summary_keeps_its_rows():
    """Only the merged tool's summary moves the row keys; get_portfolio_performance's own policy is unchanged."""
    env = budget._performance(period="MTD", portfolio_scope="equity")
    shaped = response_shape.shape(env, response_shape.POLICIES["get_portfolio_performance"], response_shape.SUMMARY)
    for row in shaped["data"]["realized_by_symbol"]:
        assert row.keys() >= MOVED_ROW_KEYS - {"realized_fx_krw"} | set(tools_v2._REALIZED_ROW)
    assert shaped["data"]["metrics"]["twr_pct"]["method_version"] == "flow_eligibility_v1"
