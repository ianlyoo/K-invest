from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kinvest_common import NotConfiguredError
from toss_accounts import TossAccountRegistry


def test_registry_when_legacy_file_then_primary_account(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({"client_id": "id_primary", "client_secret": "secret_primary"}))

    registry = TossAccountRegistry.from_sources(env={}, creds_file=creds_file, cache_dir=tmp_path)

    assert registry.account_labels() == ["primary"]
    assert registry.credentials_for("primary").client_id == "id_primary"


def test_registry_when_multi_file_then_all_accounts_available(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "accounts": [
                    {"label": "primary", "client_id": "id_primary", "client_secret": "secret_primary"},
                    {"label": "secondary", "client_id": "id_secondary", "client_secret": "secret_secondary"},
                ]
            }
        )
    )

    registry = TossAccountRegistry.from_sources(env={}, creds_file=creds_file, cache_dir=tmp_path)

    assert registry.account_labels() == ["primary", "secondary"]
    assert registry.credentials_for("secondary").client_secret == "secret_secondary"


def test_registry_when_creds_file_env_then_file_used(tmp_path: Path) -> None:
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({"client_id": "id_env_file", "client_secret": "sec_env_file"}))

    registry = TossAccountRegistry.from_sources(
        env={"TOSS_CREDS_FILE": str(creds_file)}, cache_dir=tmp_path
    )

    assert registry.credentials_for("primary").client_id == "id_env_file"


def test_registry_when_nothing_configured_then_not_configured_error(tmp_path: Path) -> None:
    with pytest.raises(NotConfiguredError) as exc:
        TossAccountRegistry.from_sources(env={}, cache_dir=tmp_path)
    assert exc.value.provider == "toss"
    assert "TOSS_CLIENT_ID" in str(exc.value)
