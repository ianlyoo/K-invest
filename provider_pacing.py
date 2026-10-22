"""Process-wide synchronous request admission; no identities or payloads retained."""
from __future__ import annotations

import math
import time
from contextlib import contextmanager
from threading import RLock


class RequestThrottled(Exception):
    """Only safe, numeric upstream status and remaining cooldown."""

    def __init__(self, status, retry_after):
        super().__init__('Provider request cooldown')
        self.status = status
        self.retry_after_seconds = retry_after


class RequestBudget:
    def __init__(self, interval=0.0, *, clock=time.monotonic, sleeper=time.sleep):
        self.interval = interval
        self._clock = clock
        self._sleeper = sleeper
        self._lock = RLock()
        self._next_request = 0.0
        self._cooldown_until = 0.0
        self._status = 429

    @contextmanager
    def request(self, *, interval=None, sleeper=None, remaining=None):
        """Hold admission through the response; start spacing after completion.

        Holding the lock across IO prevents queued threads from bunching after
        waking. An optional remaining-time callback also bounds lock/sleep waits.
        A cooldown rejects locally instead of occupying workers for minutes.
        """
        timeout = remaining() if remaining else None
        acquired = self._lock.acquire() if timeout is None else self._lock.acquire(timeout=max(0, timeout))
        if not acquired:
            raise TimeoutError('Provider request budget exceeded')
        admitted = False
        try:
            if remaining:
                remaining()
            now = self._clock()
            if now < self._cooldown_until:
                raise RequestThrottled(self._status, math.ceil(self._cooldown_until - now))
            delay = max(0, self._next_request - now)
            timeout = remaining() if remaining else None
            if timeout is not None and delay >= timeout:
                raise TimeoutError('Provider request budget exceeded')
            if delay:
                (sleeper or self._sleeper)(delay)
            if remaining:
                remaining()
            admitted = True
            yield self
        finally:
            if admitted:
                self._next_request = self._clock() + (self.interval if interval is None else interval)
            self._lock.release()

    def defer(self, status, seconds):
        with self._lock:
            self._status = status
            self._cooldown_until = max(self._cooldown_until, self._clock() + max(0, seconds))


class SupplementalBudget(RequestBudget):
    """Own cooldown, under the existing provider's shared admission spacing.

    Honor a prior provider cooldown, but never propagate supplemental 429s into
    legacy callers. Shared admission still serializes IO and spaces requests.
    """
    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    @contextmanager
    def request(self, *, interval=None, sleeper=None, remaining=None):
        with super().request(interval=interval, sleeper=sleeper, remaining=remaining):
            with self.provider.request(interval=interval, sleeper=sleeper, remaining=remaining):
                yield self


# Four requests/second at most, including JSON, archives and text. The 10 ms
# margin avoids boundary bursts and stays below the reviewed serial fixture.
SEC_BUDGET = RequestBudget(.26)
# Public and signed Binance reads consume the same process-wide admission path.
# Callers supply conservative intervals for their request/weight classes.
BINANCE_BUDGET = RequestBudget()
# Upbit public ticker group: documented ceiling 10/s, serialized with margin.
UPBIT_QUOTE_BUDGET = RequestBudget(.11)
