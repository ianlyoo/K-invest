"""EGW00215 is a KIS rate limit like EGW00201, with a capped back-off.

EGW00215 cap per request: at most 3 tries and RATE_LIMIT_MAX_WAIT_SECONDS (3 s) of waiting in
total, sequential in the calling thread (EGW00201 / HTTP 429 keep their existing budget). Real KISClient over httpx.MockTransport; sleeps recorded,
not slept. Synthetic bodies only.
"""
from __future__ import annotations

import json
import logging
import random
import sys
import threading
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import kis_api  # noqa: E402
from kis_api import KISAPIError, KISClient  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "kis_published"
LIMITED = {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "synthetic per-second limit"}


class Script:
    def __init__(self, answers):
        self.answers = list(answers)
        self.threads: list[int] = []

    def __call__(self, request):
        self.threads.append(threading.get_ident())
        status, body, headers = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def make(monkeypatch, tmp_path):
    slept: list[float] = []
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(kis_api.time, "sleep", slept.append)
    monkeypatch.setenv("KIS_TOKEN_CACHE", str(tmp_path / "token.json"))

    def build(answers):
        client = KISClient(cano="00000001", acnt_prdt_cd="01", app_key="synthetic-key", app_secret="synthetic")
        script = Script(answers)
        client._http = httpx.Client(transport=httpx.MockTransport(script), timeout=5)
        client._token, client._token_expires = "synthetic-token", 9e18
        return client, script

    build.slept = slept
    return build


def ok_body():
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output": {"stck_prpr": "1000"}}


def test_egw00215_is_retried_and_logged(make, caplog):
    client, script = make([(500, LIMITED, {}), (200, ok_body(), {})])
    with caplog.at_level(logging.WARNING, logger=kis_api.logger.name):
        data, _ = client._request("/uapi/domestic-stock/v1/quotations/inquire-price", {}, "FHKST01010100")
    assert data["rt_cd"] == "0" and len(script.threads) == 2 and len(make.slept) == 1
    record = next(r.getMessage() for r in caplog.records if "rate limit" in r.getMessage())
    assert "EGW00215" in record and "attempt 1/2" in record and "retrying in" in record
    assert "00000001" not in record  # no account data in the log


def test_persistent_egw00215_stops_after_three_tries_within_the_cap(make):
    client, script = make([(500, LIMITED, {})])
    with pytest.raises(KISAPIError) as exc:
        client._request("/uapi/domestic-stock/v1/trading/inquire-balance", {}, "TTTC8434R")
    assert exc.value.rate_limited and exc.value.body["msg_cd"] == "EGW00215"
    assert len(script.threads) == 3
    assert sum(make.slept) <= kis_api.RATE_LIMIT_MAX_WAIT_SECONDS


def test_a_long_retry_after_is_not_waited(make):
    client, script = make([(500, LIMITED, {"Retry-After": "10"})])
    with pytest.raises(KISAPIError):
        client._request("/uapi/domestic-stock/v1/trading/inquire-balance", {}, "TTTC8434R")
    assert len(script.threads) == 1 and make.slept == []


@pytest.mark.parametrize("seed", range(40))
def test_total_wait_never_exceeds_the_cap(make, seed):
    rng = random.Random(seed)
    answers = [(rng.choice([500, 429]), LIMITED,
                {"Retry-After": str(rng.choice([0, 1, 2, 3, 5]))} if rng.random() < .5 else {})
               for _ in range(3)] + [(500, LIMITED, {})]
    client, script = make(answers)
    random.seed(seed)
    with pytest.raises(KISAPIError):
        client._request("/uapi/domestic-stock/v1/trading/inquire-balance", {}, "TTTC8434R")
    assert len(script.threads) <= 3
    assert sum(make.slept) <= kis_api.RATE_LIMIT_MAX_WAIT_SECONDS


def test_egw00201_keeps_its_retry(make):
    client, script = make([(500, dict(LIMITED, msg_cd="EGW00201"), {}), (200, ok_body(), {})])
    data, _ = client._request("/uapi/domestic-stock/v1/quotations/inquire-price", {}, "FHKST01010100")
    assert data["rt_cd"] == "0" and len(script.threads) == 2


def test_capture_reads_retry_sequentially_in_the_calling_thread(make):
    # The 18:10 capture's strict read path (read_validated): same retry, no new threads.
    body = json.loads((FIXTURES / "CTRP6548R.json").read_text(encoding="utf-8"))
    client, script = make([(500, LIMITED, {}), (200, body, {"tr_cont": "D"})])
    before = threading.active_count()
    result = client.ledgerless_account_assets()
    assert result["outputs"] and set(script.threads) == {threading.get_ident()}
    assert threading.active_count() == before and len(make.slept) == 1


def test_other_errors_are_not_retried(make):
    client, script = make([(500, {"rt_cd": "1", "msg_cd": "EGW00500", "msg1": "synthetic"}, {})])
    with pytest.raises(KISAPIError) as exc:
        client._request("/uapi/domestic-stock/v1/trading/inquire-balance", {}, "TTTC8434R")
    assert not exc.value.rate_limited and len(script.threads) == 1 and make.slept == []
