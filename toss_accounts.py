from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from kinvest_common import NotConfiguredError
from kinvest_common import cache_dir as _default_cache_dir
from toss_api import TossClient


@dataclass(frozen=True, slots=True)
class TossCredentials:
    label: str
    client_id: str
    client_secret: str


def _clean_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip().lower()).strip("_")
    return label or "account"


def _account_from_mapping(raw: Mapping[str, Any], fallback_label: str) -> TossCredentials | None:
    client_id = str(raw.get("client_id") or "").strip()
    client_secret = str(raw.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    label = _clean_label(str(raw.get("label") or raw.get("name") or fallback_label))
    return TossCredentials(label=label, client_id=client_id, client_secret=client_secret)


def _accounts_from_payload(payload: Any) -> list[TossCredentials]:
    if not isinstance(payload, dict):
        return []
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        parsed: list[TossCredentials] = []
        for index, item in enumerate(accounts):
            if isinstance(item, dict):
                account = _account_from_mapping(item, "primary" if index == 0 else f"account_{index + 1}")
                if account is not None:
                    parsed.append(account)
        return parsed
    account = _account_from_mapping(payload, "primary")
    return [account] if account is not None else []


def _dedupe(accounts: list[TossCredentials]) -> list[TossCredentials]:
    seen_labels: set[str] = set()
    seen_client_ids: set[str] = set()
    unique: list[TossCredentials] = []
    for account in accounts:
        if account.label in seen_labels or account.client_id in seen_client_ids:
            continue
        seen_labels.add(account.label)
        seen_client_ids.add(account.client_id)
        unique.append(account)
    return unique


class TossAccountRegistry:
    def __init__(self, accounts: list[TossCredentials], cache_dir: Path | None = None) -> None:
        if not accounts:
            raise NotConfiguredError(
                "toss",
                "No Toss credentials configured — set TOSS_CLIENT_ID/TOSS_CLIENT_SECRET "
                "or point TOSS_CREDS_FILE at a credentials JSON file",
            )
        self._accounts = accounts
        self._cache_dir = cache_dir or _default_cache_dir()
        self._clients: dict[str, TossClient] = {}

    @classmethod
    def from_sources(
        cls,
        env: Mapping[str, str] | None = None,
        creds_file: Path | None = None,
        cache_dir: Path | None = None,
    ) -> TossAccountRegistry:
        source_env = env if env is not None else os.environ
        if creds_file is None:
            raw_path = source_env.get("TOSS_CREDS_FILE", "").strip()
            creds_file = Path(raw_path).expanduser() if raw_path else None
        accounts: list[TossCredentials] = []
        raw_json = source_env.get("TOSS_ACCOUNTS_JSON", "").strip()
        if raw_json:
            accounts.extend(_accounts_from_payload(json.loads(raw_json)))
        if creds_file is not None and creds_file.exists():
            accounts.extend(_accounts_from_payload(json.loads(creds_file.read_text())))
        env_account = _account_from_mapping(
            {
                "label": "primary",
                "client_id": source_env.get("TOSS_CLIENT_ID", ""),
                "client_secret": source_env.get("TOSS_CLIENT_SECRET", ""),
            },
            "primary",
        )
        if env_account is not None:
            accounts.append(env_account)
        return cls(_dedupe(accounts), cache_dir=cache_dir)

    def account_labels(self) -> list[str]:
        return [account.label for account in self._accounts]

    def credentials_for(self, label: str) -> TossCredentials:
        normalized = _clean_label(label or "primary")
        for account in self._accounts:
            if account.label == normalized:
                return account
        raise KeyError(normalized)

    def client_for(self, label: str = "primary") -> TossClient:
        account = self.credentials_for(label)
        existing = self._clients.get(account.label)
        if existing is not None:
            return existing
        cache_path = self._cache_dir / ("token.json" if account.label == "primary" else f"token_{account.label}.json")
        client = TossClient(
            client_id=account.client_id,
            client_secret=account.client_secret,
            token_cache_path=cache_path,
        )
        self._clients[account.label] = client
        return client
