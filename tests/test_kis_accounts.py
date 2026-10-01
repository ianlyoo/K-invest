from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kinvest_common import NotConfiguredError
from kis_accounts import KISAccountRegistry

_KIS_VARS = [
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_CANO",
    "KIS_ACNT_PRDT_CD",
    "KIS_URL_BASE",
    "KIS_ENV_FILE",
    "KIS_ACCOUNTS_JSON",
]


@pytest.fixture(autouse=True)
def _clean_kis_env(monkeypatch):
    for var in _KIS_VARS + ["APP_KEY", "APP_SECRET", "CANO", "ACNT_PRDT_CD", "URL_BASE"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")


def _accounts_json(*entries: dict) -> str:
    return json.dumps(list(entries))


def test_env_pair_alone_yields_the_primary_account(monkeypatch) -> None:
    monkeypatch.setenv("KIS_CANO", "12345678")
    monkeypatch.setenv("KIS_ACNT_PRDT_CD", "01")

    registry = KISAccountRegistry.from_sources()

    assert registry.account_labels() == ["primary"]
    assert registry.account_for("primary").cano == "12345678"
    assert registry.default_label() == "primary"


def test_pension_account_is_added_next_to_the_env_primary(monkeypatch) -> None:
    monkeypatch.setenv("KIS_CANO", "12345678")
    monkeypatch.setenv("KIS_ACNT_PRDT_CD", "01")
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json({"label": "pension", "cano": "87654321", "acnt_prdt_cd": "22"}),
    )

    registry = KISAccountRegistry.from_sources()

    assert sorted(registry.account_labels()) == ["pension", "primary"]
    pension = registry.account_for("pension")
    assert (pension.cano, pension.acnt_prdt_cd) == ("87654321", "22")


def test_pension_product_codes_are_domestic_only(monkeypatch) -> None:
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json(
            {"label": "general", "cano": "11111111", "acnt_prdt_cd": "01"},
            {"label": "yeongeum", "cano": "22222222", "acnt_prdt_cd": "22"},
            {"label": "irp", "cano": "33333333", "acnt_prdt_cd": "29"},
        ),
    )

    registry = KISAccountRegistry.from_sources()

    assert registry.account_for("general").supports_overseas is True
    assert registry.account_for("yeongeum").supports_overseas is False
    assert registry.account_for("irp").supports_overseas is False
    assert registry.account_for("yeongeum").kind == "pension"


def test_json_entry_overrides_the_env_pair_for_the_same_account(monkeypatch) -> None:
    monkeypatch.setenv("KIS_CANO", "12345678")
    monkeypatch.setenv("KIS_ACNT_PRDT_CD", "01")
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json({"label": "brokerage", "cano": "12345678", "acnt_prdt_cd": "01"}),
    )

    registry = KISAccountRegistry.from_sources()

    assert registry.account_labels() == ["brokerage"]


def test_default_label_falls_back_to_the_first_account(monkeypatch) -> None:
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json(
            {"label": "brokerage", "cano": "11111111"},
            {"label": "pension", "cano": "22222222", "acnt_prdt_cd": "22"},
        ),
    )

    registry = KISAccountRegistry.from_sources()

    assert registry.default_label() == "brokerage"
    assert registry.account_for("").cano == "11111111"


def test_product_code_defaults_to_01_and_labels_are_normalized(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ACCOUNTS_JSON", _accounts_json({"label": "My Account", "cano": "11111111"}))

    registry = KISAccountRegistry.from_sources()

    account = registry.account_for("my_account")
    assert account.acnt_prdt_cd == "01"
    assert account.label == "my_account"


def test_entry_without_cano_inherits_the_env_account_number(monkeypatch) -> None:
    """연금저축 often shares its 종합계좌번호 and differs only in 계좌상품코드."""
    monkeypatch.setenv("KIS_CANO", "12345678")
    monkeypatch.setenv("KIS_ACNT_PRDT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNTS_JSON", _accounts_json({"label": "pension", "acnt_prdt_cd": "22"}))

    registry = KISAccountRegistry.from_sources()

    assert sorted(registry.account_labels()) == ["pension", "primary"]
    pension = registry.account_for("pension")
    assert (pension.cano, pension.acnt_prdt_cd) == ("12345678", "22")
    assert pension.supports_overseas is False
    # The shared 종합계좌번호 must not collapse the two accounts into one.
    assert registry.account_for("primary").acnt_prdt_cd == "01"


def test_entry_without_cano_is_ignored_when_no_env_account_exists(monkeypatch, caplog) -> None:
    monkeypatch.setenv("KIS_ACCOUNTS_JSON", _accounts_json(
        {"label": "broken", "acnt_prdt_cd": "22"},
        {"label": "pension", "cano": "22222222", "acnt_prdt_cd": "22"},
    ))

    registry = KISAccountRegistry.from_sources()

    assert registry.account_labels() == ["pension"]
    assert "no cano" in caplog.text


def test_accounts_payload_accepts_the_nested_object_form(monkeypatch) -> None:
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        json.dumps({"accounts": [{"label": "pension", "cano": "22222222", "acnt_prdt_cd": "22"}]}),
    )

    registry = KISAccountRegistry.from_sources()

    assert registry.account_labels() == ["pension"]


def test_no_account_anywhere_is_not_configured(monkeypatch) -> None:
    with pytest.raises(NotConfiguredError) as exc:
        KISAccountRegistry.from_sources()

    assert "KIS_ACCOUNTS_JSON" in str(exc.value)


def test_metadata_never_exposes_the_account_number_or_key(monkeypatch) -> None:
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json(
            {"label": "pension", "cano": "22222222", "acnt_prdt_cd": "22", "app_key": "own", "app_secret": "s"}
        ),
    )

    metadata = KISAccountRegistry.from_sources().accounts_metadata()

    assert metadata == [
        {
            "label": "pension",
            "acnt_prdt_cd": "22",
            "kind": "pension",
            "supports_overseas": False,
            "own_app_key": True,
        }
    ]


def test_client_is_bound_to_its_account_and_cached(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KIS_CANO", "12345678")
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json({"label": "pension", "cano": "87654321", "acnt_prdt_cd": "22"}),
    )

    registry = KISAccountRegistry.from_sources(cache_dir=tmp_path)
    client = registry.client_for("pension")

    assert (client.cano, client.acnt_prdt_cd) == ("87654321", "22")
    assert client.supports_overseas is False
    assert client.app_key == "key"  # shared env credentials
    assert registry.client_for("pension") is client


def test_account_with_its_own_app_key_gets_its_own_token_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json(
            {"label": "pension", "cano": "87654321", "acnt_prdt_cd": "22", "app_key": "k2", "app_secret": "s2"}
        ),
    )

    client = KISAccountRegistry.from_sources(cache_dir=tmp_path).client_for("pension")

    assert client.app_key == "k2"
    assert client._token_cache_path == tmp_path / "kis_token_pension.json"


def test_shared_app_key_accounts_share_the_default_token_cache(monkeypatch, tmp_path: Path) -> None:
    import kis_api

    monkeypatch.setenv(
        "KIS_ACCOUNTS_JSON",
        _accounts_json({"label": "pension", "cano": "87654321", "acnt_prdt_cd": "22"}),
    )

    client = KISAccountRegistry.from_sources(cache_dir=tmp_path).client_for("pension")

    assert client._token_cache_path == kis_api.TOKEN_CACHE_PATH


def test_unknown_label_raises_key_error(monkeypatch) -> None:
    monkeypatch.setenv("KIS_CANO", "12345678")

    registry = KISAccountRegistry.from_sources()

    with pytest.raises(KeyError):
        registry.client_for("nope")


# ── server tool dispatch ─────────────────────────────────


class _FakeClient:
    def __init__(self, supports_overseas: bool = True, fail: Exception | None = None) -> None:
        self.supports_overseas = supports_overseas
        self.calls: list[str] = []
        self._fail = fail

    def get_domestic_balance(self) -> dict:
        self.calls.append("domestic")
        if self._fail:
            raise self._fail
        return {"stocks": [], "summary": []}

    def get_overseas_balance(self) -> dict:
        self.calls.append("overseas")
        return {"stocks": []}


class _FakeRegistry:
    """A 종합계좌 plus a 연금저축 account, as the server sees them."""

    def __init__(self, clients: dict[str, _FakeClient]) -> None:
        self._clients = clients

    def account_labels(self) -> list[str]:
        return list(self._clients)

    def default_label(self) -> str:
        return "primary"

    def accounts_metadata(self) -> list[dict]:
        return [{"label": label} for label in self._clients]

    def client_for(self, label: str = "") -> _FakeClient:
        return self._clients[label or self.default_label()]


@pytest.fixture
def kis_tools(monkeypatch):
    import server

    clients = {"primary": _FakeClient(), "pension": _FakeClient(supports_overseas=False)}
    monkeypatch.setattr(server, "_get_kis_registry", lambda: _FakeRegistry(clients))
    return server, clients


def test_balance_tool_defaults_to_the_default_account(kis_tools) -> None:
    server, clients = kis_tools

    result = server.get_kis_domestic_balance()

    assert result["ok"] is True
    assert clients["primary"].calls == ["domestic"]
    assert clients["pension"].calls == []


def test_balance_tool_queries_the_named_pension_account(kis_tools) -> None:
    server, clients = kis_tools

    result = server.get_kis_domestic_balance(account="pension")

    assert result["ok"] is True
    assert clients["pension"].calls == ["domestic"]


def test_account_all_fans_out_over_every_account(kis_tools) -> None:
    server, clients = kis_tools

    result = server.get_kis_domestic_balance(account="all")

    assert [entry["account"] for entry in result["data"]["accounts"]] == ["primary", "pension"]
    assert clients["pension"].calls == ["domestic"]


def test_one_failing_account_does_not_fail_the_others(kis_tools, monkeypatch) -> None:
    server, clients = kis_tools
    clients["pension"]._fail = RuntimeError("KIS error: 조회 실패")

    result = server.get_kis_domestic_balance(account="all")

    entries = {entry["account"]: entry for entry in result["data"]["accounts"]}
    assert result["ok"] is True
    assert entries["primary"]["error"] is None
    assert entries["pension"]["error"]["code"]


def test_overseas_balance_is_skipped_for_a_pension_account(kis_tools) -> None:
    server, clients = kis_tools

    result = server.get_kis_overseas_balance(account="pension")

    assert result["data"]["code"] == "OVERSEAS_NOT_SUPPORTED"
    assert clients["pension"].calls == []


def test_accounts_tool_lists_labels_without_credentials(kis_tools) -> None:
    server, _clients = kis_tools

    result = server.get_kis_accounts()

    assert [entry["label"] for entry in result["data"]["accounts"]] == ["primary", "pension"]
    assert result["data"]["default"] == "primary"
