"""Shared runtime helpers for K-invest provider modules."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from threading import RLock

# yfinance owns a process-wide session/cookie cache. Cover every local entry
# point with the same reentrant lock, including FX and direct Treasury reads.
YFINANCE_LOCK = RLock()


def synchronized(lock):
    """Guard short shared-state operations or one provider's synchronous work."""
    def decorate(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)
        return guarded
    return decorate


class NotConfiguredError(RuntimeError):
    """A provider is not configured; its tools cannot run until env vars are set."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(message)


def cache_dir() -> Path:
    """Token/cache directory. Override with KINVEST_CACHE_DIR."""
    raw = os.environ.get("KINVEST_CACHE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "k-invest"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from an env file. Missing/unreadable file -> empty dict."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


def apply_env_file(path: Path) -> None:
    """setdefault os.environ from an env file (existing env vars win)."""
    for key, value in load_env_file(path).items():
        os.environ.setdefault(key, value)


def retry_after_seconds(value: str | None) -> int | None:
    """Parse a bounded Retry-After without retaining arbitrary header text."""
    if not value:
        return None
    try:
        if value.isascii() and value.isdigit():
            delay = int(value)
        else:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            delay = max(0, int((deadline-datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None
    return delay if 0 <= delay <= 86400 else None


def local_calendar_date(timestamp: str) -> str:
    """Convert an observation instant to the host calendar used by ledger windows.

    Date-only synthetic/legacy observations already name a local calendar day.
    Keep UTC timestamps intact in the response; convert only for date comparisons.
    """
    if not timestamp:
        return ""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, TypeError, AttributeError):
        return ""
