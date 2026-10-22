"""get_toss_accounts exposes only the masked last 4 per label, so callers bind by account number."""

import json

import server
from toss_api import TossClient

# Synthetic account numbers only.
FULL = {"primary": "900-11-22223456", "secondary": "900-77-88887890"}


def fake_client(label, monkeypatch=None, *, fail=False, accounts=None):
    client = TossClient(client_id="synthetic-id-" + label, client_secret="synthetic-secret")
    calls = []

    def get_accounts():
        calls.append(1)
        if fail:
            raise RuntimeError("upstream down")
        if accounts is not None:
            return accounts
        return [{"accountSeq": 1, "accountNo": FULL[label], "accountType": "GENERAL"},
                {"accountSeq": 2, "accountNo": "900-00-00009999", "accountType": "OTHER"}]

    client.get_accounts = get_accounts
    client.calls = calls
    return client


class Registry:
    def __init__(self, clients):
        self.clients = clients

    def account_labels(self):
        return list(self.clients)

    def client_for(self, label="primary"):
        return self.clients[label]


def call(monkeypatch, clients):
    monkeypatch.setattr(server, "_get_toss_registry", lambda: Registry(clients))
    return server.get_toss_accounts()


def test_masked_last4_per_label_and_no_full_number(monkeypatch):
    clients = {label: fake_client(label) for label in ("primary", "secondary")}
    result = call(monkeypatch, clients)
    data = result.get("data", result)
    assert data["accounts"] == ["primary", "secondary"]
    assert data["default"] == "primary" and data["count"] == 2
    assert data["masked"] == {"primary": "••••3456", "secondary": "••••7890"}
    dumped = json.dumps(result, ensure_ascii=False)
    for full in [*FULL.values(), "900-00-00009999"]:
        assert full not in dumped
        assert full.replace("-", "") not in dumped
        assert full.replace("-", "")[:-4] not in dumped
    # The masked number is the account that account-scoped reads use (first accountSeq).
    assert clients["primary"].get_account_seq() == 1


def test_masked_is_cached_with_account_seq(monkeypatch):
    clients = {"primary": fake_client("primary")}
    call(monkeypatch, clients)
    call(monkeypatch, clients)
    assert len(clients["primary"].calls) == 1


def test_unresolvable_label_is_omitted_not_guessed(monkeypatch):
    clients = {
        "primary": fake_client("primary", fail=True),
        "secondary": fake_client("secondary"),
        "odd": fake_client("odd", accounts=[{"accountSeq": 3, "accountNo": "12"}]),
    }
    data = call(monkeypatch, clients)
    data = data.get("data", data)
    assert data["accounts"] == ["primary", "secondary", "odd"]
    assert data["masked"] == {"secondary": "••••7890"}
    assert "upstream down" not in json.dumps(data, ensure_ascii=False)


def test_unusable_number_never_resets_account_seq_and_backs_off(monkeypatch):
    client = fake_client("odd", accounts=[{"accountSeq": 3, "accountNo": "12"}])
    assert client.get_account_seq() == 3
    assert client.get_account_masked() is None
    assert client._account_seq == 3  # Concurrent account reads never see None.
    calls = len(client.calls)
    assert client.get_account_masked() is None
    assert len(client.calls) == calls  # Backoff: no upstream call within the retry window.


def test_upstream_failure_is_retried_only_after_backoff(monkeypatch):
    import toss_api

    client = fake_client("primary", fail=True)
    clients = {"primary": client}
    for _ in range(3):
        data = call(monkeypatch, clients)
        assert data.get("data", data)["masked"] == {}
    assert len(client.calls) == 1
    monkeypatch.setattr(toss_api, "_MASK_RETRY_SECONDS", 0.0)
    call(monkeypatch, clients)
    assert len(client.calls) == 2


def test_concurrent_failures_share_one_upstream_lookup():
    import threading
    import time as _time

    client = fake_client("primary", fail=True)
    original = client.get_accounts

    def slow_failing():
        _time.sleep(0.05)
        return original()

    client.get_accounts = slow_failing
    errors = []

    def discover():
        try:
            client.get_account_masked()
        except Exception as exc:  # The first caller sees the upstream failure.
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=discover) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(client.calls) == 1
    assert errors == ["RuntimeError"]
