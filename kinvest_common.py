"""Shared runtime helpers for K-invest provider modules."""

from __future__ import annotations

import os
from pathlib import Path


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
