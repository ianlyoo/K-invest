#!/usr/bin/env python3
"""Probe the KIS account endpoints K-invest depends on and report status only.

Verifies that each TR ID is accepted and returns rows, without printing any
balance, price, symbol, or credential. Output per endpoint is limited to:
HTTP/rt_cd, msg_cd, msg1, row counts, and the response field names.

Usage:
    python3 scripts/probe_kis.py                       # last 30 days
    python3 scripts/probe_kis.py --days 120            # spans the 3-month TR boundary
    python3 scripts/probe_kis.py --start 20260101 --end 20260818
    python3 scripts/probe_kis.py --account pension     # one KIS_ACCOUNTS_JSON account
    python3 scripts/probe_kis.py --account all         # every configured account

    # Reuse a running server's token cache so no new token is minted:
    python3 scripts/probe_kis.py --token-cache ~/.cache/k-invest/kis_token.json

Credentials are read the same way the server reads them (KIS_APP_KEY/KIS_APP_SECRET
or KIS_ENV_FILE); this script never prints them. Exit code is 0 when every probe
succeeded, 1 when any failed.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinvest_common import NotConfiguredError, apply_env_file  # noqa: E402

_SECRET_HINTS = ("key", "secret", "token", "cano", "acnt", "passwd", "password")


def _redact(text: str) -> str:
    """Drop anything that looks like a credential from an error string."""
    cleaned = []
    for chunk in str(text).split():
        lowered = chunk.lower()
        cleaned.append("<redacted>" if any(hint in lowered for hint in _SECRET_HINTS) else chunk)
    return " ".join(cleaned)[:300]


def _describe(value: Any) -> str:
    """Summarize a payload as shape and field names — never values.

    Booleans and empty containers are shown verbatim: they carry no account data,
    and `truncated` in particular is the signal that a result is incomplete.
    """
    if isinstance(value, bool):
        return f"{value}" + ("  <-- INCOMPLETE, widen KIS_HISTORY_MAX_PAGES" if value else "")
    if isinstance(value, list):
        if not value:
            return "0 rows"
        first = value[0]
        fields = ", ".join(sorted(first)[:8]) if isinstance(first, dict) else type(first).__name__
        suffix = ", ..." if isinstance(first, dict) and len(first) > 8 else ""
        return f"{len(value)} rows; fields: {fields}{suffix}"
    if isinstance(value, dict):
        if not value:
            return "empty object"
        fields = ", ".join(sorted(value)[:8])
        suffix = ", ..." if len(value) > 8 else ""
        return f"object; fields: {fields}{suffix}"
    return type(value).__name__


def _run(label: str, tr_ids: str, probe: Callable[[], dict[str, Any]], api_error: type[Exception]) -> bool:
    print(f"\n── {label}  [{tr_ids}]")
    try:
        result = probe()
    except api_error as e:
        body = getattr(e, "body", None)
        body = body if isinstance(body, dict) else {}
        print(f"   FAIL  http={getattr(e, 'status', '?')} rt_cd={body.get('rt_cd')} "
              f"msg_cd={body.get('msg_cd')} msg1={_redact(body.get('msg1', ''))}")
        if not body:
            print(f"   body: {_redact(str(getattr(e, 'body', e)))}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"   FAIL  {type(e).__name__}: {_redact(str(e))}")
        return False
    print("   OK    rt_cd=0")
    for key, value in result.items():
        print(f"         {key}: {_describe(value)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="lookback window in days (default 30)")
    parser.add_argument("--start", default="", help="YYYYMMDD (overrides --days)")
    parser.add_argument("--end", default="", help="YYYYMMDD (default today)")
    parser.add_argument("--env-file", default="", help="path to an env file with KIS credentials")
    parser.add_argument(
        "--account",
        default="",
        help="KIS_ACCOUNTS_JSON account label to probe, or 'all' (default: the primary account)",
    )
    parser.add_argument(
        "--token-cache",
        default="",
        help="reuse an existing KIS token cache file instead of minting a new token "
        "(point this at a running server's cache to leave its token untouched)",
    )
    args = parser.parse_args()

    if args.env_file:
        os.environ.setdefault("KIS_ENV_FILE", args.env_file)
    if args.token_cache:
        # kis_api resolves TOKEN_CACHE_PATH at import time, so this must be set first.
        os.environ["KIS_TOKEN_CACHE"] = str(Path(args.token_cache).expanduser())
    apply_env_file(Path.cwd() / ".env")
    apply_env_file(Path(__file__).resolve().parents[1] / ".env")

    from kis_accounts import KISAccountRegistry
    from kis_api import _OVERSEAS_MARKETS, KISAPIError, _split_by_recency

    end = args.end or date.today().strftime("%Y%m%d")
    start = args.start or (
        datetime.strptime(end, "%Y%m%d").date() - timedelta(days=args.days)
    ).strftime("%Y%m%d")

    print(f"KIS endpoint probe — window {start}..{end}")
    print("(status, message codes, row counts, and field names only — no values)")

    try:
        registry = KISAccountRegistry.from_sources()
    except NotConfiguredError as e:
        print(f"\nNOT CONFIGURED: {e}")
        print("Set KIS_APP_KEY/KIS_APP_SECRET, or pass --env-file /path/to/kis.env")
        return 1

    wanted = args.account.strip().lower()
    if wanted == "all":
        labels = registry.account_labels()
    else:
        labels = [wanted or registry.default_label()]

    segments = _split_by_recency(start, end)
    print(f"\ndomestic history splits into: {[(tr, s, e) for tr, s, e in segments]}")

    results: list[bool] = []
    for label in labels:
        try:
            client = registry.client_for(label)
        except KeyError:
            print(f"\nUNKNOWN ACCOUNT: {label} (configured: {', '.join(registry.account_labels())})")
            results.append(False)
            continue

        account = registry.account_for(label)
        print(f"\n{'=' * 60}")
        print(f"account: {label}  (계좌상품코드 {account.acnt_prdt_cd}, {account.kind})")

        probes: list[tuple[str, str, Callable[[], dict[str, Any]]]] = [
            ("국내 주식 잔고", "TTTC8434R", client.get_domestic_balance),
            ("국내 체결내역", " + ".join(tr for tr, _, _ in segments),
             lambda client=client: client.get_domestic_trade_history(start, end)),
            ("국내 기간별매매손익", "TTTC8715R",
             lambda client=client: client.get_domestic_realized_pnl(start, end)),
            ("국내 주문가능현금", "TTTC8908R", client.get_domestic_orderable_cash),
        ]
        if account.supports_overseas:
            probes[1:1] = [
                (
                    f"해외 주식 잔고 (전 시장: {', '.join(_OVERSEAS_MARKETS)})",
                    "TTTS3012R + CTRP6504R",
                    client.get_overseas_balance,
                ),
            ]
            probes.extend([
                ("해외 주문체결내역", "TTTS3035R",
                 lambda client=client: client.get_overseas_trade_history(start, end)),
                ("해외 일별거래내역", "CTOS4001R",
                 lambda client=client: client.get_overseas_period_trans(start, end)),
                ("해외 기간손익", "TTTS3039R",
                 lambda client=client: client.get_overseas_realized_pnl(start, end)),
                ("해외 주문가능현금", "TTTS3007R", client.get_overseas_orderable_cash),
            ])
        else:
            print("(연금 계좌 — 해외 엔드포인트는 건너뜀)")

        results.extend(_run(name, tr_ids, probe, KISAPIError) for name, tr_ids, probe in probes)
        client.close()

    failed = results.count(False)
    print(f"\n{'=' * 60}")
    print(f"{len(results) - failed}/{len(results)} probes OK")
    if failed:
        print("Any FAIL above with msg_cd=OPSQ0002 means that TR ID is not valid for")
        print("this account — paste the output back so the TR ID can be corrected.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
