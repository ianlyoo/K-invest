"""Frozen 2.7.1 snapshot capture for offline compatibility checks."""
from typing import Any


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def snapshot_summary(summary: dict[str, Any], collection, fx) -> dict[str, Any]:
    """Capture normalized valuation inputs; fetch time is never quote time.

    Current normalized broker holdings do not expose market-mark timestamps.
    Explicit priced_at attributes are retained if a source later supplies them.
    FxRates.as_of is the collection time, not the underlying close timestamp.
    """
    data = dict(summary)
    data["valuation_at"] = collection.fetched_at or _now()
    data["observed_at"] = collection.fetched_at or _now()
    data["components"] = [dict(item.to_dict(), component_type=kind,
                               priced_at=getattr(item, "priced_at", None))
                          for kind, items in (("holding", collection.holdings), ("cash", collection.cash))
                          for item in items]
    quote_times = getattr(fx, "priced_at_by_currency", {})
    data["fx_times"] = {currency: {"rate_to_krw": rate, "priced_at": quote_times.get(currency),
                                 "observed_at": fx.as_of}
                        for currency, rate in sorted(fx.rates.items())}
    reasons = list(data.get("reason_codes", []))
    if collection.failed_sources:
        reasons.append("FAILED_SOURCES")
    if collection.truncated_sources:
        reasons.append("TRUNCATED_SOURCES")
    if fx.missing:
        reasons.append("MISSING_FX_RATE")
    data["collection_complete"] = not (collection.failed_sources or collection.truncated_sources)
    if not data["collection_complete"]:
        bridge = dict(data.get("nav_bridge") or {})
        bridge["settlement_adjusted_nav_krw"] = None
        reconciliation = dict(bridge.get("reconciliation") or {})
        reconciliation["status"] = "unavailable"
        reconciliation["difference_reasons"] = list(dict.fromkeys(
            reconciliation.get("difference_reasons", []) + ["INCOMPLETE_PORTFOLIO_SOURCES"]))
        bridge["reconciliation"] = reconciliation
        data["nav_bridge"] = bridge
        data["result_status"] = "partial"
    else:
        data.setdefault("result_status", "complete")
    data["reason_codes"] = reasons
    return data
