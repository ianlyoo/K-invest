from __future__ import annotations

import pytest

from error_taxonomy import classify_error
from health_status import HealthStatus
from kinvest_common import NotConfiguredError
from kis_api import KISAPIError
from toss_api import TossAPIError


@pytest.mark.parametrize("exc,kind", [
    (TossAPIError(401, {"secret": "fixture-sensitive"}), "broker_auth_failed"),
    (KISAPIError(200, {"msg_cd": "EGW00123"}), "broker_auth_failed"),
    (KISAPIError(500, {"msg_cd": "EGW00201", "msg1": "fixture-sensitive"}), "rate_limited"),
    (TossAPIError(503, {"account": "fixture-sensitive"}), "provider_unavailable"),
    (TimeoutError("fixture-sensitive"), "provider_unavailable"),
    (NotConfiguredError("toss", "fixture-sensitive"), "not_configured"),
])
def test_taxonomy_redacts_broker_errors(exc, kind):
    value = classify_error(exc)
    assert value["kind"] == kind
    assert "fixture-sensitive" not in str(value)


def test_passive_health_unknown_empty_failed_partial_stale(monkeypatch):
    h = HealthStatus()
    assert h.report({"toss": False})["overall_status"] == "not_configured"
    assert h.report({"toss": True})["overall_status"] == "unknown"
    h.record("toss", status="ok_empty", count=0, complete=True)
    assert h.report({"toss": True})["overall_status"] == "ok_empty"
    h.record("kis", status="provider_unavailable", error_code="provider_unavailable")
    value = h.report({"toss": True, "kis": True})
    assert value["overall_status"] == "partial"
    assert next(x for x in value["sources"] if x["source"] == "kis")["result_count"] is None
    h.observations["toss"]["observed_epoch"] -= 1000
    assert next(x for x in h.report({"toss": True})["sources"] if x["source"] == "toss")["status"] == "stale"


def test_service_health_never_builds_clients_or_reads_credentials(monkeypatch):
    from pathlib import Path

    import server
    def forbidden(*args, **kwargs):
        raise AssertionError("Passive health attempted an active operation")
    for name in ["_get_toss", "_get_kis", "_get_toss_registry", "_get_kis_registry", "_get_collector", "_get_nav_store"]:
        monkeypatch.setattr(server, name, forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    monkeypatch.setattr(server, "_toss_accounts", None)
    monkeypatch.setattr(server, "_kis_accounts", None)
    monkeypatch.setenv("TOSS_CREDS_FILE", "/fixture-unopened")
    result = server.get_service_health()
    assert result["ok"] and result["meta"]["mode"] == "passive"
    assert result["data"]["overall_status"] == "unknown"
