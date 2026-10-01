"""Passive observations: no clients, credential files, upstream calls or state writes."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HealthStatus:
    def __init__(self):
        self.observations: dict[str, dict[str, Any]] = {}

    def record(self, source: str, *, status: str, count: int | None = None,
               complete: bool | None = None, error_code: str | None = None,
               truncated: bool = False) -> None:
        now = time.time()
        old = self.observations.get(source, {})
        success = status in {"ok", "ok_empty", "truncated", "partial"}
        self.observations[source] = {"source": source, "status": status,
            "last_attempt_at": timestamp(), "last_success_at": timestamp() if success else old.get("last_success_at"),
            "observed_epoch": now, "last_success_epoch": now if success else old.get("last_success_epoch"),
            "error_code": error_code, "complete": complete, "truncated": truncated, "result_count": count}

    def report(self, configured: dict[str, bool | None], stale_after: int = 300) -> dict:
        now = time.time()
        sources = []
        for source in sorted(set(configured) | set(self.observations)):
            observed = self.observations.get(source)
            if observed:
                row = {k: v for k, v in observed.items() if not k.endswith("epoch")}
                last_success = observed.get("last_success_epoch")
                age = max(0, now-last_success) if last_success is not None else None
                row["freshness_seconds"] = round(age, 3) if age is not None else None
                if now-observed["observed_epoch"] > stale_after:
                    row["status"] = "stale"
                    row["complete"] = None
            else:
                row = dict(source=source, status="not_configured" if configured.get(source) is False else "unknown",
                    last_attempt_at=None, last_success_at=None, freshness_seconds=None,
                    error_code=None, complete=None, truncated=False, result_count=None)
            row["configuration_present"] = configured.get(source)
            sources.append(row)
        statuses = {s["status"] for s in sources}
        overall = "not_configured" if not statuses or statuses == {"not_configured"} else \
            "ok_empty" if statuses == {"ok_empty"} else "ok" if statuses <= {"ok", "ok_empty"} else \
            "unknown" if statuses <= {"unknown", "not_configured"} else \
            "partial" if statuses & {"ok", "ok_empty", "partial", "truncated"} else \
            "stale" if "stale" in statuses else "unavailable"
        return {"overall_status": overall, "checked_at": timestamp(), "sources": sources}
