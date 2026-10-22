"""Dashboard comparison regressions: synthetic broker bodies, no credentials."""
import json

import pytest
from test_kis_portfolio import FakeKIS

from kis_api import KISAPIError
from portfolio_sources import PortfolioCollector, Providers


def test_repeated_cursor_stops_early_and_preserves_unique_pages():
    def respond(path, params, tr_id, tr_cont):
        return {"output1": [{"n": len(client.calls)}], "ctx_area_fk100": "same", "ctx_area_nk100": "same"}, "M"

    client = FakeKIS(respond)
    bodies, truncated = client._paginate("/synthetic", {}, "TR", ctx="100", max_pages=10)
    assert len(client.calls) == len(bodies) == 2
    assert truncated is True
    assert client.calls[1]["tr_cont"] == "N"
    assert client.calls[1]["params"]["CTX_AREA_FK100"] == "same"


def test_replayed_profit_page_is_not_counted_twice():
    body = {"output1": [{"pdno": "005930", "rlzt_pfls": "10000"}], "ctx_area_fk100": "same", "ctx_area_nk100": "same"}
    client = FakeKIS(lambda *args: (body, "M"))
    data = client.get_domestic_realized_pnl("20260401", "20260630")
    assert len(client.calls) == 2
    assert data["trades"] == body["output1"]
    assert data["truncated"] is True


def test_more_pages_without_cursor_is_truncated_without_requery():
    client = FakeKIS(lambda *args: ({"output1": [], "ctx_area_fk200": "  ", "ctx_area_nk200": ""}, "F"))
    bodies, truncated = client._paginate("/synthetic", {}, "TR")
    assert len(client.calls) == len(bodies) == 1
    assert truncated is True


@pytest.mark.parametrize("error", [
    KISAPIError(429, {}),
    KISAPIError(503, {"msg_cd": "OPSQ0003", "msg1": "일시적 조회 오류"}),
    KISAPIError(200, {"msg_cd": "EGW00123", "msg1": "인증 오류"}),
])
def test_domestic_fallback_does_not_retry_unrelated_errors(error):
    def respond(*args):
        raise error

    client = FakeKIS(respond)
    with pytest.raises(KISAPIError) as caught:
        client.get_domestic_trade_history("20260401", "20260630")
    assert caught.value is error
    assert len(client.calls) == 1


def collector():
    return PortfolioCollector(Providers(
        toss_labels=lambda: [], toss_client=lambda _: None,
        kis_accounts=lambda: [], kis_client=lambda _: None,
        error_formatter=lambda _: {"error": {"code": "KIS_API_ERROR", "kind": "provider_unavailable", "message": "Provider is unavailable."}},
    ))


def test_kis_health_keeps_bounded_masked_broker_diagnostic(caplog):
    c = collector()
    error = KISAPIError(200, {"msg_cd": "OPSQ0003", "msg1": "조회 오류 계좌 12345678-22 token synthetic-secret " + "긴" * 200})
    _, entry = c._run("kis:pension:domestic:trades", "kis", lambda: (_ for _ in ()).throw(error))
    diagnostic = entry["last_error"]["broker_diagnostic"]
    assert diagnostic["msg_cd"] == "OPSQ0003"
    assert "조회 오류" in diagnostic["msg1"]
    assert len(diagnostic["msg1"]) <= 160
    assert "12345678" not in json.dumps(c.health_report()) + caplog.text
    assert "synthetic-secret" not in json.dumps(c.health_report()) + caplog.text
    assert diagnostic == c.health_report()["kis:pension:domestic:trades"]["last_error"]["broker_diagnostic"]


@pytest.mark.parametrize("body", [{}, {"msg_cd": "12345678", "msg1": "1234 5678-22"}, {"msg_cd": "OPSQ0003 token-secret", "msg1": "ＡＢＣ１２３"}, {"msg_cd": {"secret": "private"}, "msg1": ["private"]}])
def test_kis_diagnostic_rejects_untrusted_shapes(body):
    diagnostic = collector()._record_failure("kis:domestic:trades", "kis", KISAPIError(400, body))["last_error"]["broker_diagnostic"]
    assert diagnostic["msg_cd"] is None
    assert diagnostic["msg1"] is None or not any(x.isdigit() for x in diagnostic["msg1"])
    assert "private" not in str(diagnostic)


def test_non_kis_failure_does_not_get_kis_diagnostics():
    assert "broker_diagnostic" not in collector()._record_failure("toss:primary:orders", "toss", ValueError("synthetic"))["last_error"]
