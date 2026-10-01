"""Registry of the KIS accounts one app key may query (일반/연금저축/IRP).

KIS scopes every account endpoint by 종합계좌번호 (CANO) + 계좌상품코드
(ACNT_PRDT_CD), so a second account — a 연금저축 (22) or IRP (29) account — is the
same credentials pointed at a different account pair. Accounts come from
KIS_ACCOUNTS_JSON; the KIS_CANO/KIS_ACNT_PRDT_CD env pair stays the "primary"
account so existing deployments keep working untouched.

KIS issues an app key per HTS ID, and a pension account is not always linked to
the same one, so an account may carry its own app_key/app_secret; when it does it
gets its own token cache file, since the cache is scoped to one key pair.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from kinvest_common import NotConfiguredError
from kinvest_common import cache_dir as _default_cache_dir
from kis_api import PENSION_PRODUCT_CODES, KISClient, resolve_credentials

logger = logging.getLogger("k-invest.kis_accounts")

DEFAULT_LABEL = "primary"


@dataclass(frozen=True, slots=True)
class KISAccount:
    """One KIS account. Empty app_key/app_secret means "use the shared env key"."""

    label: str
    cano: str
    acnt_prdt_cd: str
    app_key: str = ""
    app_secret: str = ""
    account_id: str = ""

    @property
    def kind(self) -> str:
        return "pension" if self.acnt_prdt_cd in PENSION_PRODUCT_CODES else "general"

    @property
    def supports_overseas(self) -> bool:
        """연금저축/퇴직연금 accounts cannot hold overseas-listed securities."""
        return self.acnt_prdt_cd not in PENSION_PRODUCT_CODES

    def metadata(self) -> dict[str, Any]:
        """Describe the account without exposing the account number or key."""
        return {
            "label": self.label,
            "acnt_prdt_cd": self.acnt_prdt_cd,
            "kind": self.kind,
            "supports_overseas": self.supports_overseas,
            "own_app_key": bool(self.app_key),
        }


def _clean_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip().lower()).strip("_")
    return label or DEFAULT_LABEL


def _account_from_mapping(
    raw: Mapping[str, Any], fallback_label: str, default_cano: str = ""
) -> KISAccount | None:
    # A 연금저축 account often shares its 종합계좌번호 with the 종합계좌 and differs
    # only in 계좌상품코드, so an entry may omit `cano` and inherit KIS_CANO.
    cano = str(raw.get("cano") or raw.get("CANO") or "").strip() or default_cano
    if not cano:
        logger.warning(
            "KIS_ACCOUNTS_JSON entry %r has no cano and KIS_CANO is unset — ignored",
            raw.get("label") or fallback_label,
        )
        return None
    product = str(raw.get("acnt_prdt_cd") or raw.get("ACNT_PRDT_CD") or "").strip() or "01"
    return KISAccount(
        account_id=str(raw.get("account_id") or ""),
        label=_clean_label(str(raw.get("label") or raw.get("name") or fallback_label)),
        cano=cano,
        acnt_prdt_cd=product,
        app_key=str(raw.get("app_key") or "").strip(),
        app_secret=str(raw.get("app_secret") or "").strip(),
    )


def _accounts_from_payload(payload: Any, default_cano: str = "") -> list[KISAccount]:
    """Parse KIS_ACCOUNTS_JSON: a list of accounts, {"accounts": [...]}, or one account.

    Entries without a `cano` fall back to `default_cano` (the KIS_CANO account).
    """
    if isinstance(payload, dict):
        nested = payload.get("accounts")
        payload = nested if isinstance(nested, list) else [payload]
    if not isinstance(payload, list):
        return []
    parsed: list[KISAccount] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        account = _account_from_mapping(
            item, DEFAULT_LABEL if index == 0 else f"account_{index + 1}", default_cano
        )
        if account is not None:
            parsed.append(account)
    return parsed


def _dedupe(accounts: list[KISAccount]) -> list[KISAccount]:
    """First occurrence wins, so a KIS_ACCOUNTS_JSON entry overrides the env pair."""
    seen_labels: set[str] = set()
    seen_accounts: set[tuple[str, str]] = set()
    unique: list[KISAccount] = []
    for account in accounts:
        identity = (account.cano, account.acnt_prdt_cd)
        if account.label in seen_labels or identity in seen_accounts:
            continue
        seen_labels.add(account.label)
        seen_accounts.add(identity)
        unique.append(account)
    return unique


class KISAccountRegistry:
    """Configured KIS accounts and one lazily built client per account."""

    def __init__(self, accounts: list[KISAccount], cache_dir: Path | None = None) -> None:
        if not accounts:
            raise NotConfiguredError(
                "kis",
                "No KIS account configured — set KIS_CANO/KIS_ACNT_PRDT_CD "
                "(or KIS_ACCOUNTS_JSON for several accounts, e.g. a 연금저축 account)",
            )
        self._accounts = accounts
        self._cache_dir = cache_dir or _default_cache_dir()
        self._clients: dict[str, KISClient] = {}

    @classmethod
    def from_sources(
        cls,
        env: Mapping[str, str] | None = None,
        cache_dir: Path | None = None,
    ) -> KISAccountRegistry:
        source_env = env if env is not None else os.environ
        resolved = resolve_credentials(dict(source_env))
        accounts: list[KISAccount] = []
        raw_json = source_env.get("KIS_ACCOUNTS_JSON", "").strip()
        if raw_json:
            accounts.extend(_accounts_from_payload(json.loads(raw_json), resolved["cano"]))
        env_account = _account_from_mapping(
            {"label": DEFAULT_LABEL, "cano": resolved["cano"], "acnt_prdt_cd": resolved["acnt_prdt_cd"]},
            DEFAULT_LABEL,
        )
        if env_account is not None:
            accounts.append(env_account)
        return cls(_dedupe(accounts), cache_dir=cache_dir)

    def accounts(self) -> list[KISAccount]:
        return list(self._accounts)

    def account_labels(self) -> list[str]:
        return [account.label for account in self._accounts]

    def default_label(self) -> str:
        """"primary" when configured, else the first account."""
        labels = self.account_labels()
        return DEFAULT_LABEL if DEFAULT_LABEL in labels else labels[0]

    def accounts_metadata(self) -> list[dict[str, Any]]:
        return [account.metadata() for account in self._accounts]

    def account_for(self, label: str = "") -> KISAccount:
        normalized = _clean_label(label) if label.strip() else self.default_label()
        for account in self._accounts:
            if account.label == normalized:
                return account
        raise KeyError(normalized)

    def client_for(self, label: str = "") -> KISClient:
        account = self.account_for(label)
        existing = self._clients.get(account.label)
        if existing is not None:
            return existing
        # Accounts on the shared app key also share its token cache; one with its
        # own key needs its own file, or the two would invalidate each other.
        token_cache_path = (
            self._cache_dir / f"kis_token_{account.label}.json" if account.app_key else None
        )
        client = KISClient(
            cano=account.cano,
            acnt_prdt_cd=account.acnt_prdt_cd,
            app_key=account.app_key,
            app_secret=account.app_secret,
            token_cache_path=token_cache_path,
        )
        client.journal_account_key = "kis:" + (account.account_id or account.label)
        self._clients[account.label] = client
        return client
