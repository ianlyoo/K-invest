"""SEC configuration and source-local degradation, with synthetic HTTP only."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import server
from health_status import HealthStatus
from kinvest_common import NotConfiguredError

VALID_UA = "InvestMCP offline fixtures/1.0 (sec-fixtures@fixtures.test)"


@pytest.fixture
def load_sec(monkeypatch):
    """Import with controlled config without changing the server's cached module."""
    def load(value):
        if value is None:
            monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        else:
            monkeypatch.setenv("SEC_USER_AGENT", value)
        path = Path(__file__).resolve().parents[1] / "sec_api.py"
        spec = importlib.util.spec_from_file_location("synthetic_sec_api", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return load


INVALID_UAS = [
    None, "", " ", "InvestMCP without a contact", "contact@localhost",
    "k-invest/2.0 (research@example.com)",
    "contact@fixtures.localhost", "contact@example.com", "contact@example.org",
    "contact@example.net", "contact@sub.example.com", "contact@sub.EXAMPLE.ORG",
    "contact@fixtures.invalid", "contact@nested.fixtures.invalid",
    "contact@fixtures.example", "contact@nested.fixtures.example",
    VALID_UA + "\r", VALID_UA + "\n", VALID_UA + "\r\nInjected: header",
    VALID_UA + "\t", VALID_UA + "\x7f", VALID_UA + "é",
    "x" * (201 - len(VALID_UA)) + VALID_UA,
    "contact@fixtures.test.invalid", "contact@fixtures.test_example.com",
    "contact@-fixtures.test", "contact@fixtures-.test",
    VALID_UA + " placeholder@example.com", VALID_UA + " placeholder@localhost",
]


@pytest.mark.parametrize("value", INVALID_UAS)
def test_invalid_configuration_blocks_every_sec_tool(load_sec, monkeypatch, caplog, value):
    sec = load_sec(value)
    requests = []

    def forbidden(*args, **kwargs):
        requests.append((args, kwargs))
        pytest.fail("Unconfigured SEC attempted HTTP")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(server, "SECClient", sec.SECClient)
    monkeypatch.setattr(server, "_sec_client", None)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    with caplog.at_level(logging.WARNING, logger="k-invest"):
        results = [tool("AAPL") for tool in (
            server.get_sec_financials, server.get_ttm_financials, server.get_insider_trades,
        )]
    assert sec.sec_user_agent_configured() is False
    assert requests == []
    for result in results:
        assert result["ok"] is False and result["data"] is None
        assert result["error"]["code"] == "SEC_NOT_CONFIGURED"
        assert result["error"]["kind"] == "not_configured"
        assert result["error"]["provider"] == "sec"
        assert result["error"]["retryable"] is False
    if value and value.strip():
        assert value not in str(results) + caplog.text


@pytest.mark.parametrize("method,url", [
    ("_get_json", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"),
    ("_get_text", "https://www.sec.gov/Archives/edgar/data/1/fixture.xml"),
])
def test_transport_gate_also_blocks_client_without_constructor(load_sec, method, url):
    sec = load_sec(None)
    client = object.__new__(sec.SECClient)
    with pytest.raises(NotConfiguredError, match="SEC") as caught:
        getattr(client, method)(url)
    assert caught.value.provider == "sec"


@pytest.mark.parametrize("value", [
    VALID_UA, "contact@fixtures.test", "Contact <first.last+sec@corp.test>",
    "x" * (200 - len(VALID_UA)) + VALID_UA,
])
def test_valid_configuration_is_sent_verbatim_and_read_once(load_sec, monkeypatch, value):
    sec = load_sec(value)
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"fixture": {"ticker": "AAPL", "cik_str": 1}})

    real_client = httpx.Client
    monkeypatch.setattr(sec.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(response), trust_env=False,
    ))
    monkeypatch.setenv("SEC_USER_AGENT", "replacement@example.com")
    assert sec.sec_user_agent_configured() is True
    with sec.SECClient() as client:
        assert client.get_cik("AAPL") == "0000000001"
        client.get_company_facts("0000000001")
        client._get_text("https://www.sec.gov/Archives/edgar/data/1/fixture.xml")
    assert len(requests) == 3
    assert all(request.headers["User-Agent"] == value for request in requests)


def test_import_without_config_stays_unconfigured_after_env_change(load_sec, monkeypatch):
    sec = load_sec(None)
    monkeypatch.setenv("SEC_USER_AGENT", VALID_UA)
    assert sec.sec_user_agent_configured() is False
    with pytest.raises(NotConfiguredError):
        sec.SECClient()


@pytest.mark.parametrize("method", ["_get_json", "_get_text"])
@pytest.mark.parametrize("outcome", ["error", "http_status", "success"])
def test_user_agent_never_appears_in_sec_logs(load_sec, monkeypatch, caplog, method, outcome):
    sec = load_sec(VALID_UA)

    def response(request):
        if outcome == "error":
            raise httpx.RequestError("Synthetic transport failure " + VALID_UA, request=request)
        return httpx.Response(403 if outcome == "http_status" else 200, json={"fixture": True})

    real_client = httpx.Client
    monkeypatch.setattr(sec.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(response), trust_env=False,
    ))
    with caplog.at_level(logging.DEBUG, logger="k-invest.sec"), sec.SECClient() as client:
        result = getattr(client, method)("https://data.sec.gov/fixture")
    if outcome == "success":
        assert result is not None
    else:
        assert result is None
    if outcome == "error":
        assert caplog.records
    assert VALID_UA not in caplog.text + str(result)


@pytest.mark.parametrize("value,expected", [(None, False), ("contact@example.com", False), (VALID_UA, True)])
def test_passive_status_exposes_only_configuration_boolean(load_sec, monkeypatch, value, expected):
    sec = load_sec(value)
    monkeypatch.setattr(server, "sec_user_agent_configured", sec.sec_user_agent_configured)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    monkeypatch.setattr(server, "_oauth_provider", None)

    def forbidden(*args, **kwargs):
        pytest.fail("Passive health constructed SEC client")

    monkeypatch.setattr(server, "_get_sec", forbidden)
    result = server.get_service_health()
    assert result["data"]["sec_user_agent_configured"] is expected
    assert "User-Agent" not in str(result)
    if value:
        assert value not in str(result)


def wire_unconfigured_sec(load_sec, monkeypatch, value):
    sec = load_sec(value)
    monkeypatch.setattr(server, "SECClient", sec.SECClient)
    monkeypatch.setattr(server, "_sec_client", None)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    return sec


@pytest.mark.parametrize("value", [None, "contact@example.com", VALID_UA + "\r\n"])
def test_stock_snapshot_preserves_other_sources_when_sec_unconfigured(load_sec, monkeypatch, value):
    wire_unconfigured_sec(load_sec, monkeypatch, value)
    quotes, metrics = {"quotes": [{"provider": "toss", "price": 100}]}, {"price": 100}
    monkeypatch.setattr(server, "compare_quotes", lambda *args, **kwargs: {"data": quotes})
    monkeypatch.setattr(server, "_get_yf", lambda: SimpleNamespace(get_key_metrics=lambda symbol: metrics))
    result = server.get_stock_snapshot("AAPL", market="us")
    assert result["ok"] is True
    assert result["data"]["quote_comparison"] == quotes
    assert result["data"]["key_metrics"] == metrics
    assert result["data"]["insider_summary"] is None
    assert result["data"]["insider_error"]["code"] == "SEC_NOT_CONFIGURED"


@pytest.mark.parametrize("value", [None, "contact@example.com", VALID_UA + "\r\n"])
def test_health_probe_keeps_other_sources_and_reports_sec_taxonomy(load_sec, monkeypatch, value):
    wire_unconfigured_sec(load_sec, monkeypatch, value)
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_prices=lambda symbol: {"price": 100}))
    monkeypatch.setattr(server, "_get_kis", lambda: SimpleNamespace(get_overseas_quote=lambda *args, **kwargs: {"price": 100}))
    monkeypatch.setattr(server, "_get_yf", lambda: SimpleNamespace(get_key_metrics=lambda symbol: {"price": 100}))
    monkeypatch.setattr(server, "_get_binance", lambda: SimpleNamespace(price_ticker=lambda symbol: {"price": 100}))
    result = server.health_check()
    assert result["ok"] is True
    assert result["data"]["sec"]["status"] == "not_configured"
    assert result["data"]["sec"]["error"]["code"] == "SEC_NOT_CONFIGURED"
    assert all(result["data"][name]["status"] == "ok" for name in (
        "toss_market_data", "kis_market_data", "yfinance", "binance_futures",
    ))


@pytest.mark.parametrize("value", [None, "contact@example.com", VALID_UA + "\r\n"])
def test_yfinance_growth_preserves_quarterly_data_and_marks_sec_unconfigured(load_sec, monkeypatch, value):
    import sec_api
    from yfinance_metrics import growth_fields

    sec = load_sec(value)
    monkeypatch.setattr(sec_api, "SECClient", sec.SECClient)
    result = growth_fields("AAPL", {"revenueGrowth": 0.1, "earningsGrowth": 0.2})
    assert result["revenue_growth_pct_yoy_quarterly"] == 10
    assert result["earnings_growth_pct_yoy_quarterly"] == 20
    assert result["growth_meta"]["source"] == "yfinance.info"
    assert result["growth_meta_sec_annual"]["error"]["code"] == "SEC_NOT_CONFIGURED"
    assert "revenue_growth_pct_annual_sec" not in result
