#!/usr/bin/env python3
"""Record one daily portfolio NAV snapshot, for cron or a systemd timer.

Runs the same collection path as the `snapshot_portfolio_nav` MCP tool, in-process
and without starting the server. Re-running appends an immutable version and
moves today's representative; older version IDs remain available for replay.

Usage:
    python3 scripts/snapshot_nav.py                # today
    python3 scripts/snapshot_nav.py --date YYYY-MM-DD  # must be today
    python3 scripts/snapshot_nav.py --force        # store even if a source failed

Exit codes: 0 stored, 1 refused (a broker source failed) or errored.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crypto_nav_capture import prepare_crypto_snapshot  # noqa: E402
from kinvest_common import apply_env_file  # noqa: E402
from portfolio_sources import collect_nav_evidence  # noqa: E402
from portfolio_store import snapshot_summary, validate_live_snapshot_date  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default="", help="ISO YYYY-MM-DD (must be today; current prices only)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="store even when a broker source failed (the NAV will be understated)",
    )
    args = parser.parse_args()
    try:
        when = validate_live_snapshot_date(args.date)
    except ValueError as exc:
        print(f"refusing to snapshot: {exc}")
        return 1

    root = Path(__file__).resolve().parents[1]
    apply_env_file(Path.cwd() / ".env")
    apply_env_file(root / ".env")

    import portfolio_engine
    import server

    store = server._get_nav_store()
    try:
        collector = server._get_collector()
        collection = collector.collect_positions()
        currencies = {h.currency for h in collection.holdings} | {c.currency for c in collection.cash}
        fx = collector.build_fx(currencies)
        collect_nav_evidence(collector, collection)
        summary = snapshot_summary(
            portfolio_engine.summarize_portfolio(collection.holdings, collection.cash, fx), collection, fx)
    except Exception:  # noqa: BLE001 — timer failures must be visible in persisted health
        store.record_attempt(when, "unavailable", ["COLLECTION_FAILED"])
        print("snapshot collection failed")
        return 1

    incomplete = collection.failed_sources + collection.truncated_sources
    if incomplete and not args.force:
        store.record_attempt(when, "unavailable", summary["reason_codes"])
        print(f"refusing to snapshot: incomplete sources -> {', '.join(incomplete)}")
        for name in incomplete:
            error = collection.health[name].get("last_error") or {}
            print(f"  {name}: {error.get('code')} {error.get('message', '')[:160]}")
        print("re-run with --force to store an incomplete NAV anyway")
        return 1

    try:
        pending = deepcopy(summary)
        prepare_crypto_snapshot(pending, store.db_path)
        # Publish only supplemental fields. An unexpected helper failure cannot
        # alter broker evidence, source success or the legacy write/exit path.
        extra = {key: pending[key] for key in ('crypto_risk_snapshot_id',
            'crypto_capture_reasons', 'crypto_max_age_seconds', 'crypto_fx_times') if key in pending}
        extra = json.loads(json.dumps(extra, allow_nan=False))
    except Exception:
        extra = {'crypto_capture_reasons': ['CRYPTO_SNAPSHOT_PREPARATION_FAILED']}
    summary.update(extra)

    # KIS ledgerless capture last, in isolation: only the kis_capture key is added
    # and a KIS failure is a reason inside it, never a refused or failed snapshot.
    try:
        from kis_nav_capture import capture_kis
        kis_extra = capture_kis(lambda: server._get_kis_registry().accounts(), server._get_kis)
        kis_extra = {'kis_capture': json.loads(json.dumps(kis_extra['kis_capture'], allow_nan=False))}
    except Exception:
        kis_extra = {'kis_capture': {'version': 'kis_ledgerless_v1', 'accounts': {},
                                     'reasons': ['KIS_CAPTURE_FAILED']}}
    summary.update(kis_extra)

    try:
        stored = store.upsert_snapshot(summary, snapshot_date=when, fx_usdkrw=fx.rates.get("USD"))
    except Exception:  # noqa: BLE001 — a failed write is not a successful collection
        store.record_attempt(when, "unavailable", ["SNAPSHOT_WRITE_FAILED"])
        print("snapshot write failed")
        return 1
    print(
        f"stored {stored['snapshot_date']}: total={stored['total_value_krw']:,.0f} KRW "
        f"positions={stored.get('position_count')} sources_ok={len(collection.ok_sources)} "
        f"db={store.db_path}"
    )
    if incomplete:
        print(f"WARNING: incomplete — sources: {', '.join(incomplete)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
