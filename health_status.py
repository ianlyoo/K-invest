"""Passive observations: no clients, credential files, upstream calls or state writes."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable

_provider_observer: Callable[..., None] | None = None
_SUCCESS = frozenset({"ok", "ok_empty", "truncated", "partial"})


def set_provider_observer(observer: Callable[..., None]) -> None:
    global _provider_observer
    _provider_observer = observer


def observe_provider_outcome(provider: str, error: Exception | None = None) -> None:
    """Observe final transport outcomes without changing provider responses."""
    if _provider_observer is not None:
        try:
            _provider_observer(provider, error)
        except Exception:  # health instrumentation must not break a broker read
            pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HealthStatus:
    def __init__(self):
        self.observations: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def record(self, source: str, *, status: str, count: int | None = None,
               complete: bool | None = None, error_code: str | None = None,
               truncated: bool = False, error: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._record(source, status, count, complete, error_code, truncated, error)
            provider = source.split(":", 1)[0]
            if provider in {"kis", "toss"} and provider != source:
                self._record(provider, status, count, complete, error_code, truncated, error)
                self.observations[provider]["latest_source"] = source

    def _record(self, source, status, count, complete, error_code, truncated, error):
        now = time.time()
        at = timestamp()
        old = self.observations.get(source, {})
        success = status in _SUCCESS
        # Metadata repeats the active failure with only its taxonomy code.
        # Preserve richer sanitized history and its original timestamp. After
        # recovery (or for a new structured error), record a new failure instead.
        duplicate = (not success and old.get("status") == status
            and old.get("error_code") == error_code and bool(old.get("last_error"))
            and (error is None or set(error) <= {"code"}))
        self.observations[source] = {"source": source, "status": status,
            "last_attempt_at": at, "last_success_at": at if success else old.get("last_success_at"),
            "observed_epoch": now, "last_success_epoch": now if success else old.get("last_success_epoch"),
            "error_code": None if success else error_code,
            "last_error_at": old.get("last_error_at") if success or duplicate else at,
            "last_error": old.get("last_error") if success or duplicate else dict(error or {"code": error_code}),
            "observation_scope": "latest_provider_call" if ":" not in source else "latest_source_call",
            "complete": complete, "truncated": truncated, "result_count": count}

    def report(self, configured: dict[str, bool | None], stale_after: int = 300) -> dict:
        with self._lock:
            return self._report(configured, stale_after)

    def _report(self, configured, stale_after):
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
                    # Aged out: idle (nothing called since a success) is not an outage.
                    row["last_observed_status"] = observed["status"]
                    row["stale_reason"] = "idle" if observed["status"] in _SUCCESS else "last_attempt_failed"
                    row["status"] = "stale"
                    row["complete"] = None
            else:
                row = dict(source=source, status="not_configured" if configured.get(source) is False else "unknown",
                    last_attempt_at=None, last_success_at=None, freshness_seconds=None,
                    error_code=None, last_error_at=None, last_error=None,
                    complete=None, truncated=False, result_count=None)
            row["configuration_present"] = configured.get(source)
            sources.append(row)
        stale = [s for s in sources if s["status"] == "stale"]
        idle = sum(s.get("stale_reason") == "idle" for s in stale)
        seen = [s for s in sources if s["status"] not in ("not_configured", "unknown")]
        # An idle source counts as its last observed (successful) status, so idle sources never
        # turn a healthy service partial; when every observed source is idle the service is stale.
        statuses = {s["last_observed_status"] if s.get("stale_reason") == "idle" else s["status"] for s in sources}
        overall = "not_configured" if not statuses or statuses == {"not_configured"} else \
            "stale" if seen and idle == len(seen) else \
            "ok_empty" if statuses == {"ok_empty"} else "ok" if statuses <= {"ok", "ok_empty"} else \
            "unknown" if statuses <= {"unknown", "not_configured"} else \
            "partial" if statuses & {"ok", "ok_empty", "partial", "truncated"} else \
            "stale" if "stale" in statuses else "unavailable"
        return {"overall_status": overall, "checked_at": timestamp(), "sources": sources,
                "stale_after_seconds": stale_after,
                "stale_breakdown": {"idle": idle, "last_attempt_failed": len(stale) - idle},
                "stale_reason": None if not stale else "idle" if idle == len(stale) else "last_attempt_failed"}
