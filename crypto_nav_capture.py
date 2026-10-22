"""Bounded crypto evidence reads inside the existing daily NAV job.

Importing this module does no I/O. Public quotes and account clients are injected
in tests. Production uses only the existing read-only futures allowlists.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import provider_pacing
from crypto_validation import (
    CryptoValidationError,
    decimal,
    millis,
    object_shape,
    require,
    rows,
    unique,
)
from daily_nav import MAX_STABLECOIN_AGE_SECONDS, instant, stablecoin_mark_fresh
from kinvest_common import retry_after_seconds

MAX_CAPTURE_AGE_SECONDS = 300
STABLECOINS = ('USDT', 'USDC')


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def validate_tickers(payload, requested, observed_ms):
    """Exact requested pair set, unique rows, actual trade time, positive price."""
    result, seen = {}, set()
    for row in rows(payload):
        object_shape(row, {'market', 'trade_date', 'trade_time', 'trade_date_kst', 'trade_time_kst',
            'trade_timestamp', 'opening_price', 'high_price', 'low_price', 'trade_price', 'prev_closing_price',
            'change', 'change_price', 'signed_change_price', 'change_rate', 'signed_change_rate', 'trade_volume',
            'acc_trade_price', 'acc_trade_price_24h', 'acc_trade_volume', 'acc_trade_volume_24h',
            'highest_52_week_price', 'highest_52_week_date', 'lowest_52_week_price', 'lowest_52_week_date', 'timestamp'})
        market = row.get('market')
        require(market in requested, 'CRYPTO_QUOTE_REQUEST_MISMATCH')
        unique(seen, market)
        price = decimal(row.get('trade_price'))
        require(price > 0, 'CRYPTO_INVALID_QUOTE')
        stamp, updated = millis(row.get('trade_timestamp')), millis(row.get('timestamp'))
        require(stamp <= updated <= observed_ms + 5000, 'CRYPTO_FUTURE_QUOTE')
        priced_at, read_at = utc(stamp), utc(observed_ms)
        # The same rule daily_nav and the return engine apply to the stored mark.
        require(stablecoin_mark_fresh(instant(priced_at), instant(read_at)), 'STALE_CRYPTO_FX_RATE')
        result[market.removeprefix('KRW-')] = {
            'rate_to_krw': str(price), 'source': 'upbit_public_ticker', 'market': market,
            'priced_at': priced_at, 'observed_at': read_at, 'read_at': read_at,
            'max_age_seconds': MAX_STABLECOIN_AGE_SECONDS, 'reason_codes': []}
    require(seen == set(requested), 'MISSING_CRYPTO_FX_RATE')
    return result


class StablecoinQuotes:
    def __init__(self, *, http=None, clock=time.time, sleeper=time.sleep):
        self.http = http
        self.clock, self.sleeper = clock, sleeper

    def read(self):
        pairs = tuple('KRW-' + coin for coin in STABLECOINS)
        owned = self.http is None
        http = self.http or httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
        error = None
        try:
            with provider_pacing.UPBIT_QUOTE_BUDGET.request(sleeper=self.sleeper):
                response = http.get('https://api.upbit.com/v1/ticker',
                                    params={'markets': ','.join(pairs)}, timeout=8,
                                    follow_redirects=False)
                if response.status_code == 429:
                    delay = retry_after_seconds(response.headers.get('Retry-After'))
                    provider_pacing.UPBIT_QUOTE_BUDGET.defer(429, delay if delay is not None else 60)
                require(response.status_code == 200, 'CRYPTO_QUOTE_UNAVAILABLE')
                return validate_tickers(response.json(), pairs, int(self.clock() * 1000))
        except CryptoValidationError as exc:
            error = CryptoValidationError(exc.code)
        except Exception:
            error = CryptoValidationError('CRYPTO_QUOTE_UNAVAILABLE')
        finally:
            if owned:
                http.close()
        raise error


def prepare_crypto_snapshot(summary, db_path, *, quote_factory=StablecoinQuotes,
                            capture=None, clock=lambda: datetime.now(timezone.utc)):
    """Attach FX evidence; refresh absent, legacy, or >5-minute-old risk capture.

    This mutates only the pending summary and appends through the existing risk
    store. Never rewrite an old NAV. Capture failure stays a null diagnostic.
    """
    from futures_risk import CALCULATION_VERSION, RiskCollector, RiskStore

    # Supplemental quotes must not change legacy broker mark quality/status.
    fx = summary.setdefault('crypto_fx_times', {})
    try:
        fx.update(quote_factory().read())
    except Exception as exc:
        reason = exc.code if isinstance(exc, CryptoValidationError) else 'CRYPTO_QUOTE_UNAVAILABLE'
        for asset in STABLECOINS:
            fx[asset] = {'rate_to_krw': None, 'priced_at': None, 'source': 'upbit_public_ticker',
                         'reason_codes': [reason]}
    now, selected = clock(), None
    path = Path(db_path)
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
                selected = conn.execute('''SELECT snapshot_id, completed_at, calculation_version
                    FROM futures_risk_snapshots WHERE julianday(completed_at) <= julianday(?)
                    ORDER BY julianday(completed_at) DESC LIMIT 1''', (now.isoformat(),)).fetchone()
        except sqlite3.Error:
            selected = None
    stale = True
    if selected and selected[2] == CALCULATION_VERSION:
        try:
            age = (now - datetime.fromisoformat(selected[1].replace('Z', '+00:00'))).total_seconds()
            stale = not 0 <= age <= MAX_CAPTURE_AGE_SECONDS
        except (ValueError, TypeError):
            pass
    if stale:
        try:
            result = capture() if capture else RiskCollector(RiskStore(path)).capture()
            summary['crypto_risk_snapshot_id'] = result['snapshot_id']
        except Exception:
            summary['crypto_capture_reasons'] = ['CRYPTO_SNAPSHOT_REFRESH_FAILED']
    elif selected:
        summary['crypto_risk_snapshot_id'] = selected[0]
    summary['crypto_max_age_seconds'] = MAX_CAPTURE_AGE_SECONDS
    return summary
