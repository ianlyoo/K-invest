"""Offline token rejection and shared-cache renewal regressions."""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from threading import Event

import httpx
import pytest

import kis_api
from binance_futures_api import BinanceAPIError
from error_taxonomy import classify_error
from kis_api import KISAPIError, KISClient
from toss_api import TossAPIError

OLD_TOKEN = "synthetic-rejected-token-sentinel"
NEW_TOKEN = "synthetic-renewed-token-sentinel"
APP_KEY = "synthetic-app-key-sentinel"
APP_SECRET = "synthetic-app-secret-sentinel"
SENTINELS = (OLD_TOKEN, NEW_TOKEN, APP_KEY, APP_SECRET)
SUCCESS = {"rt_cd": "0", "output": {"ok": True}}


def rejection(code=None):
    return {"rt_cd": "1", "msg_cd": code or "", "msg1": " ".join(SENTINELS)}


def write_cache(client, token, *, expires_at=None, scope=None):
    client._token_cache_path.write_text(json.dumps({
        "scope": client._token_scope_key() if scope is None else scope,
        "access_token": token,
        "expires_at": time.time() + 3600 if expires_at is None else expires_at,
    }))
    client._token_cache_path.chmod(0o600)


@pytest.fixture
def make_client(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="k-invest.kis")
    clients = []

    def make(responses=(), *, on_get=None, on_token=None, cached=True):
        # Avoid credential resolution entirely; every value and file is synthetic.
        client = object.__new__(KISClient)
        client.app_key = APP_KEY
        client.app_secret = APP_SECRET
        client.url_base = "https://kis.test.invalid"
        client._token_cache_path = tmp_path / "kis_token.json"
        client._token = OLD_TOKEN
        client._token_expires = time.time() + 3600
        client._throttle = lambda: None
        calls = {"get": [], "token": []}
        pending = list(responses)

        def handler(request):
            if request.method == "POST":
                assert request.url.path == "/oauth2/tokenP"
                calls["token"].append(request)
                if on_token:
                    on_token(client, request)
                return httpx.Response(200, json={"access_token": NEW_TOKEN, "expires_in": 3600})
            assert request.method == "GET"
            calls["get"].append(request)
            if on_get:
                on_get(client, request, len(calls["get"]))
            assert pending, "Unexpected extra resend"
            status, body = pending.pop(0)
            return httpx.Response(status, json=body, headers={"tr_cont": "M"})

        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        if cached:
            write_cache(client, OLD_TOKEN)
        return client, calls

    yield make
    for client in clients:
        client._http.close()


def request(client):
    return client._request_unlocked("/synthetic/read", {"fixture": "value"}, "SYNTHETIC_TR", "N")


def auth_warnings(caplog):
    return [r for r in caplog.records if r.name == "k-invest.kis" and r.levelno == logging.WARNING and r.message.startswith("KIS auth rejection:")]


def assert_safe(caplog, exc=None):
    text = caplog.text + (str(exc) + repr(exc) if exc is not None else "")
    assert all(value not in text for value in SENTINELS)


@pytest.mark.parametrize("status", [200, 400, 403, 500, 503])
@pytest.mark.parametrize("code", ["EGW00123", "EGW00121", "EGW00110"])
def test_token_code_renews_once_regardless_of_http_status(make_client, caplog, status, code):
    client, calls = make_client([(status, rejection(code)), (200, SUCCESS)])
    data, continuation = request(client)
    assert data == SUCCESS and continuation == "M"
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert [r.headers["authorization"] for r in calls["get"]] == [f"Bearer {OLD_TOKEN}", f"Bearer {NEW_TOKEN}"]
    for sent in calls["get"]:
        assert sent.headers["tr_id"] == "SYNTHETIC_TR"
        assert sent.headers["tr_cont"] == "N"
        assert sent.url.params["fixture"] == "value"
    assert auth_warnings(caplog)[0].message == f"KIS auth rejection: msg_cd={code} HTTP={status}"
    assert len(auth_warnings(caplog)) == 1
    assert_safe(caplog)


def test_repeated_500_token_rejection_has_one_renewal_and_safe_auth_error(make_client, caplog):
    client, calls = make_client([(500, rejection("EGW00121")), (500, rejection("EGW00121"))])
    with pytest.raises(KISAPIError) as caught:
        request(client)
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert caught.value.status == 500
    assert caught.value.body["msg_cd"] == "EGW00121"
    assert classify_error(caught.value)["kind"] == "broker_auth_failed"
    assert len(auth_warnings(caplog)) == 1
    assert_safe(caplog, caught.value)


@pytest.mark.parametrize("first,second", [(401, 500), (500, 401), (401, 200), (200, 401), (401, 401)])
def test_401_and_token_message_paths_share_one_renewal(make_client, caplog, first, second):
    client, calls = make_client([
        (first, rejection(None if first == 401 else "EGW00123")),
        (second, rejection(None if second == 401 else "EGW00121")),
    ])
    with pytest.raises(KISAPIError) as caught:
        request(client)
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert classify_error(caught.value)["kind"] == "broker_auth_failed"
    assert len(auth_warnings(caplog)) == 1
    assert_safe(caplog, caught.value)


def test_401_still_refreshes_once_and_resends(make_client, caplog):
    client, calls = make_client([(401, rejection()), (200, SUCCESS)])
    assert request(client)[0] == SUCCESS
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert auth_warnings(caplog)[0].message == "KIS auth rejection: msg_cd=unavailable HTTP=401"  # gitleaks:allow (synthetic test value)
    assert_safe(caplog)


def test_401_does_not_log_arbitrary_msg_cd(make_client, caplog):
    client, _ = make_client([(401, rejection(APP_SECRET)), (200, SUCCESS)])
    request(client)
    assert auth_warnings(caplog)[0].message == "KIS auth rejection: msg_cd=unavailable HTTP=401"  # gitleaks:allow (synthetic test value)
    assert_safe(caplog)


def track_unlinks(monkeypatch, cache_path):
    unlinked = []
    original = Path.unlink

    def tracked(path, *args, **kwargs):
        if path == cache_path:
            unlinked.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", tracked)
    return unlinked


def test_peer_refreshed_cache_is_adopted_without_tokenP_or_unlink(make_client, monkeypatch, caplog):
    saved = []

    def peer_refresh(client, _, count):
        if count == 1:
            write_cache(client, NEW_TOKEN)
            saved.append((client._token_cache_path.stat().st_ino, client._token_cache_path.read_bytes()))

    client, calls = make_client([(500, rejection("EGW00123")), (200, SUCCESS)], on_get=peer_refresh)
    unlinked = track_unlinks(monkeypatch, client._token_cache_path)
    assert request(client)[0] == SUCCESS
    assert calls["token"] == [] and len(calls["get"]) == 2
    assert calls["get"][1].headers["authorization"] == f"Bearer {NEW_TOKEN}"
    assert unlinked == []
    assert (client._token_cache_path.stat().st_ino, client._token_cache_path.read_bytes()) == saved[0]
    assert_safe(caplog)


def test_same_rejected_cache_is_unlinked_and_replaced_once(make_client, monkeypatch, caplog):
    client, calls = make_client([(500, rejection("EGW00123")), (200, SUCCESS)])
    unlinked = track_unlinks(monkeypatch, client._token_cache_path)
    request(client)
    assert len(unlinked) == 1 and len(calls["token"]) == 1
    cache = json.loads(client._token_cache_path.read_text())
    assert cache["access_token"] == NEW_TOKEN
    assert cache["scope"] == client._token_scope_key()
    assert client._token_cache_path.stat().st_mode & 0o777 == 0o600
    assert_safe(caplog)


def test_missing_cache_still_renews_once(make_client, caplog):
    client, calls = make_client([(500, rejection("EGW00123")), (200, SUCCESS)], cached=False)
    assert request(client)[0] == SUCCESS
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert_safe(caplog)


def test_foreign_scope_token_is_not_adopted_or_unlinked(make_client, monkeypatch):
    client, calls = make_client()
    write_cache(client, NEW_TOKEN, scope="synthetic-other-app-scope")
    unlinked = track_unlinks(monkeypatch, client._token_cache_path)
    client._invalidate_token()
    assert client._token is None
    assert json.loads(client._token_cache_path.read_text())["access_token"] == NEW_TOKEN
    assert unlinked == [] and calls["token"] == []


def test_non_token_500_stays_provider_unavailable_without_renewal(make_client, caplog):
    client, calls = make_client([(500, rejection("OPSQ0002"))])
    with pytest.raises(KISAPIError) as caught:
        request(client)
    assert classify_error(caught.value)["kind"] == "provider_unavailable"
    assert calls["token"] == [] and len(calls["get"]) == 1
    assert json.loads(client._token_cache_path.read_text())["access_token"] == OLD_TOKEN
    assert auth_warnings(caplog) == []
    assert_safe(caplog, caught.value)


@pytest.mark.parametrize("after_renewal", [False, True])
def test_business_error_exception_never_echoes_credentials(make_client, caplog, after_renewal):
    responses = [(500, rejection("EGW00123"))] if after_renewal else []
    client, calls = make_client([*responses, (200, rejection("OPSQ0002"))])
    with pytest.raises(KISAPIError) as caught:
        request(client)
    assert classify_error(caught.value)["kind"] == "provider_unavailable"
    assert len(calls["token"]) == int(after_renewal)
    assert_safe(caplog, caught.value)


def test_unreadable_cache_is_not_unlinked_and_renews_once(make_client, monkeypatch, caplog):
    client, calls = make_client([(500, rejection("EGW00123")), (200, SUCCESS)])
    unlinked = track_unlinks(monkeypatch, client._token_cache_path)
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == client._token_cache_path:
            raise PermissionError(APP_SECRET)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert request(client)[0] == SUCCESS
    assert client._token_cache_path.exists() and unlinked == []
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert calls["get"][1].headers["authorization"] == f"Bearer {NEW_TOKEN}"
    assert "KIS token cache read failed" in caplog.text
    assert_safe(caplog)


LOCK_WARNING = "KIS token cache lock unavailable; continuing without cross-process lock"


def lock_warnings(caplog):
    return [r for r in caplog.records if r.name == "k-invest.kis" and r.message == LOCK_WARNING]


def read_only_cache(client, monkeypatch):
    """Simulate read-only filesystem errors even when pytest runs as root."""
    original_open = kis_api.os.open
    original_write = Path.write_text
    original_unlink = Path.unlink
    lock_path = str(client._token_cache_path) + ".lock"

    def open_file(path, flags, *args, **kwargs):
        if str(path) == lock_path and flags & (kis_api.os.O_RDWR | kis_api.os.O_CREAT):
            raise PermissionError(APP_SECRET)
        return original_open(path, flags, *args, **kwargs)

    def write_file(path, *args, **kwargs):
        if path == client._token_cache_path:
            raise PermissionError(APP_SECRET)
        return original_write(path, *args, **kwargs)

    def unlink_file(path, *args, **kwargs):
        if path == client._token_cache_path:
            raise PermissionError(APP_SECRET)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(kis_api.os, "open", open_file)
    monkeypatch.setattr(Path, "write_text", write_file)
    monkeypatch.setattr(Path, "unlink", unlink_file)


@pytest.mark.parametrize("cached", [True, False])
def test_read_only_cache_dir_still_allows_requests(make_client, monkeypatch, caplog, cached):
    client, calls = make_client([(200, SUCCESS)], cached=cached)
    client._token = None
    client._token_expires = 0
    read_only_cache(client, monkeypatch)
    assert client._request("/synthetic/read", {})[0] == SUCCESS
    assert len(calls["token"]) == int(not cached)
    assert len(lock_warnings(caplog)) == 1
    assert_safe(caplog)


def test_unopenable_lock_warns_once_per_client_and_allows_requests(make_client, monkeypatch, caplog):
    client, calls = make_client([(200, SUCCESS), (200, SUCCESS)])
    original = kis_api.os.open

    def unavailable(path, *args, **kwargs):
        if str(path) == str(client._token_cache_path) + ".lock":
            raise PermissionError(APP_SECRET)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(kis_api.os, "open", unavailable)
    for _ in range(2):
        client._token = None
        client._token_expires = 0
        assert request(client)[0] == SUCCESS
    assert calls["token"] == []
    assert len(lock_warnings(caplog)) == 1
    assert_safe(caplog)


def test_existing_lock_can_be_opened_read_only(make_client, monkeypatch, caplog):
    client, calls = make_client([(200, SUCCESS)])
    Path(str(client._token_cache_path) + ".lock").write_text("")
    client._token = None
    client._token_expires = 0
    read_only_cache(client, monkeypatch)
    assert request(client)[0] == SUCCESS
    assert calls["token"] == []
    assert lock_warnings(caplog) == []
    assert_safe(caplog)


@pytest.mark.parametrize("operation", ["mkdir", "flock"])
def test_lock_setup_errors_fall_back_and_close_open_fds(make_client, monkeypatch, caplog, operation):
    client, calls = make_client([(200, SUCCESS)])
    client._token = None
    client._token_expires = 0
    opened, closed = [], []
    original_open, original_close = kis_api.os.open, kis_api.os.close

    def tracked_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path) == str(client._token_cache_path) + ".lock":
            opened.append(fd)
        return fd

    def tracked_close(fd):
        if fd in opened:
            closed.append(fd)
        return original_close(fd)

    def unavailable(*args, **kwargs):
        raise OSError(APP_SECRET)

    monkeypatch.setattr(kis_api.os, "open", tracked_open)
    monkeypatch.setattr(kis_api.os, "close", tracked_close)
    if operation == "mkdir":
        monkeypatch.setattr(Path, "mkdir", unavailable)
    else:
        monkeypatch.setattr(kis_api.fcntl, "flock", unavailable)
    assert request(client)[0] == SUCCESS
    assert calls["token"] == []
    assert opened == closed
    assert len(lock_warnings(caplog)) == 1
    assert_safe(caplog)


def test_rejected_token_is_not_readopted_when_cache_cannot_be_unlinked(make_client, monkeypatch, caplog):
    client, calls = make_client([(500, rejection("EGW00123")), (200, SUCCESS)])
    read_only_cache(client, monkeypatch)
    assert request(client)[0] == SUCCESS
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert json.loads(client._token_cache_path.read_text())["access_token"] == OLD_TOKEN
    assert calls["get"][1].headers["authorization"] == f"Bearer {NEW_TOKEN}"
    assert len(lock_warnings(caplog)) == 1
    assert_safe(caplog)


@pytest.mark.parametrize("code", ["EGW00123", "EGW00121", "EGW00110"])
def test_second_token_429_is_auth_failure_with_original_http_status(make_client, monkeypatch, caplog, code):
    client, calls = make_client([(500, rejection(code)), (429, rejection(code))])
    outcomes = []
    monkeypatch.setattr(kis_api, "observe_provider_outcome", lambda _, exc=None: outcomes.append(classify_error(exc) if exc else None))
    with pytest.raises(KISAPIError) as caught:
        client._request("/synthetic/read", {})
    result = classify_error(caught.value)
    assert result["kind"] == "broker_auth_failed"
    assert result["upstream_status"] == 429
    assert outcomes[-1]["kind"] == "broker_auth_failed"
    assert len(calls["token"]) == 1 and len(calls["get"]) == 2
    assert_safe(caplog, caught.value)


@pytest.mark.parametrize("code", [None, "EGW00201", "OPSQ0002"])
def test_ordinary_429_still_classifies_rate_limited(make_client, monkeypatch, caplog, code):
    client, calls = make_client([(429, rejection(code))])
    monkeypatch.setattr(kis_api, "RATE_LIMIT_RETRIES", 0)
    with pytest.raises(KISAPIError) as caught:
        client._request("/synthetic/read", {})
    assert classify_error(caught.value)["kind"] == "rate_limited"
    assert classify_error(caught.value)["upstream_status"] == 429
    assert calls["token"] == []
    assert_safe(caplog, caught.value)


@pytest.mark.parametrize("error_type", [TossAPIError, BinanceAPIError])
@pytest.mark.parametrize("code", [None, "EGW00123", "EGW00121", "EGW00110"])
def test_other_providers_keep_429_classification(error_type, code):
    exc = (BinanceAPIError("Synthetic provider rejection", status=429, body=rejection(code))
           if error_type is BinanceAPIError else error_type(429, rejection(code)))
    result = classify_error(exc)
    assert result["kind"] == "rate_limited"
    assert result["upstream_status"] == 429
    assert all(value not in str(result) for value in SENTINELS)


@pytest.mark.parametrize("token_first", [True, False])
def test_auth_recovery_and_existing_rate_limit_retry_interoperate(make_client, monkeypatch, caplog, token_first):
    token_error = (500, rejection("EGW00123"))
    rate_error = (500, rejection("EGW00201"))
    sequence = [token_error, rate_error] if token_first else [rate_error, token_error]
    client, calls = make_client([*sequence, (200, SUCCESS)])
    slept = []
    monkeypatch.setattr(kis_api.time, "sleep", slept.append)
    assert request(client)[0] == SUCCESS
    assert len(calls["token"]) == 1 and len(calls["get"]) == 3
    assert len(slept) == 1 and len(auth_warnings(caplog)) == 1
    assert_safe(caplog)


def test_invalidation_waits_for_peer_renewal_then_reuses_peer_cache(make_client, caplog):
    creating_token = Event()
    release_writer = Event()
    rejected = Event()

    def hold_token_creation(*_):
        creating_token.set()
        assert release_writer.wait(2), "Synthetic writer was not released"

    writer, writer_calls = make_client(on_token=hold_token_creation)
    writer._token = None
    writer._token_expires = 0
    reader, reader_calls = make_client([(500, rejection("EGW00123")), (200, SUCCESS)], cached=False,
                                       on_get=lambda _, __, n: rejected.set() if n == 1 else None)
    write_cache(writer, OLD_TOKEN, expires_at=time.time() - 60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer_result = pool.submit(writer._ensure_token)
        try:
            assert creating_token.wait(2)
            reader_result = pool.submit(request, reader)
            assert rejected.wait(2)
            with pytest.raises(FutureTimeout):
                reader_result.result(timeout=.05)
        finally:
            release_writer.set()
        assert writer_result.result(timeout=2) == NEW_TOKEN
        assert reader_result.result(timeout=2)[0] == SUCCESS
    assert len(writer_calls["token"]) == 1 and reader_calls["token"] == []
    assert json.loads(reader._token_cache_path.read_text())["access_token"] == NEW_TOKEN
    assert reader_calls["get"][1].headers["authorization"] == f"Bearer {NEW_TOKEN}"
    assert_safe(caplog)
