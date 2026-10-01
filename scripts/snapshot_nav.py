#!/usr/bin/env python3
"""Record one daily portfolio NAV snapshot, for cron or a systemd timer.

Runs the same collection path as the `snapshot_portfolio_nav` MCP tool, in-process
and without starting the server. Idempotent per date: re-running refreshes today's
row rather than appending.

Usage:
    python3 scripts/snapshot_nav.py                # today
    python3 scripts/snapshot_nav.py --date 2026-08-18
    python3 scripts/snapshot_nav.py --force        # store even if a source failed

Exit codes: 0 stored, 1 refused (a broker source failed) or errored.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kinvest_common import apply_env_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default="", help="ISO YYYY-MM-DD to record under (default: today)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="store even when a broker source failed (the NAV will be understated)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    apply_env_file(Path.cwd() / ".env")
    apply_env_file(root / ".env")

    import portfolio_engine
    import server

    collector = server._get_collector()
    collection = collector.collect_positions()
    currencies = {h.currency for h in collection.holdings} | {c.currency for c in collection.cash}
    fx = collector.build_fx(currencies)
    summary = portfolio_engine.summarize_portfolio(collection.holdings, collection.cash, fx)

    incomplete = collection.failed_sources + collection.truncated_sources
    if incomplete and not args.force:
        print(f"refusing to snapshot: incomplete sources -> {', '.join(incomplete)}")
        for name in incomplete:
            error = collection.health[name].get("last_error") or {}
            print(f"  {name}: {error.get('code')} {error.get('message', '')[:160]}")
        print("re-run with --force to store an incomplete NAV anyway")
        return 1

    store = server._get_nav_store()
    stored = store.upsert_snapshot(summary, snapshot_date=args.date or None, fx_usdkrw=fx.rates.get("USD"))
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
