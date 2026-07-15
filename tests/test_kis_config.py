from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kinvest_common import NotConfiguredError
from kis_api import KISClient

_KIS_VARS = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_CANO", "KIS_ACNT_PRDT_CD", "KIS_URL_BASE", "KIS_ENV_FILE"]


@pytest.fixture(autouse=True)
def _clean_kis_env(monkeypatch):
    for var in _KIS_VARS + ["APP_KEY", "APP_SECRET", "CANO", "ACNT_PRDT_CD", "URL_BASE"]:
        monkeypatch.delenv(var, raising=False)


def test_kis_when_direct_env_then_used(monkeypatch) -> None:
    monkeypatch.setenv("KIS_APP_KEY", "key_direct")
    monkeypatch.setenv("KIS_APP_SECRET", "secret_direct")
    monkeypatch.setenv("KIS_CANO", "12345678")

    client = KISClient()

    assert client.app_key == "key_direct"
    assert client.cano == "12345678"
    assert client.url_base == "https://openapi.koreainvestment.com:9443"


def test_kis_when_env_file_then_unprefixed_keys_accepted(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "kis.env"
    env_file.write_text("APP_KEY=key_file\nAPP_SECRET=secret_file\nCANO=87654321\n")
    monkeypatch.setenv("KIS_ENV_FILE", str(env_file))

    client = KISClient()

    assert client.app_key == "key_file"
    assert client.cano == "87654321"


def test_kis_when_both_then_direct_env_wins(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "kis.env"
    env_file.write_text("APP_KEY=key_file\nAPP_SECRET=secret_file\n")
    monkeypatch.setenv("KIS_ENV_FILE", str(env_file))
    monkeypatch.setenv("KIS_APP_KEY", "key_direct")

    client = KISClient()

    assert client.app_key == "key_direct"
    assert client.app_secret == "secret_file"


def test_kis_when_nothing_then_not_configured(monkeypatch) -> None:
    with pytest.raises(NotConfiguredError) as exc:
        KISClient()
    assert exc.value.provider == "kis"
    assert "KIS_APP_KEY" in str(exc.value)


def test_kis_when_bare_env_names_without_file_then_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("APP_KEY", "bare_key")
    monkeypatch.setenv("APP_SECRET", "bare_secret")
    with pytest.raises(NotConfiguredError):
        KISClient()
