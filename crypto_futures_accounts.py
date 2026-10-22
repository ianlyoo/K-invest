"""Account-only crypto futures reads. No portfolio store or NAV integration.

All I/O is guarded by exact allowlists. Authenticated URLs and transport messages
are deliberately excluded from logging and public exceptions. Amounts retain
native Decimal strings; unknown data is never converted to zero.
"""
from __future__ import annotations

import base64
import calendar
import copy
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import provider_pacing
from kinvest_common import retry_after_seconds
from portfolio_export import _Privacy

BINANCE_ALLOWLIST = frozenset({('GET', '/fapi/v1/time'), ('GET', '/fapi/v3/positionRisk'),
    ('GET', '/fapi/v3/account'), ('GET', '/fapi/v1/accountConfig'),
    ('GET', '/fapi/v3/balance'), ('GET', '/fapi/v1/userTrades'), ('GET', '/fapi/v1/income'),
    ('GET', '/sapi/v1/asset/transfer'), ('GET', '/sapi/v1/capital/deposit/hisrec'),
    ('GET', '/sapi/v1/capital/withdraw/history')})
BINANCE_WALLET_ROUTES = ('MAIN_UMFUTURE', 'UMFUTURE_MAIN', 'MAIN_FUNDING',
                         'FUNDING_MAIN', 'FUNDING_UMFUTURE', 'UMFUTURE_FUNDING')
HYPERLIQUID_ALLOWLIST = frozenset({'clearinghouseState', 'userFillsByTime', 'userFunding',
    'spotClearinghouseState', 'perpDexs', 'userAbstraction', 'userNonFundingLedgerUpdates'})
HYPERLIQUID_PARAMS = {'clearinghouseState': frozenset({'dex'}),
    'userFillsByTime': frozenset({'startTime', 'endTime'}),
    'userFunding': frozenset({'startTime', 'endTime'}),
    'userNonFundingLedgerUpdates': frozenset({'startTime', 'endTime'}),
    'spotClearinghouseState': frozenset(), 'perpDexs': frozenset(),
    'userAbstraction': frozenset()}
HYPERLIQUID_MAX_NAMED_DEXES = 16
# Documented /info weights against the 1200/minute per-IP budget (hyperliquid-docs
# for-developers/api/rate-limits-and-user-limits, read 2026-10-22), paced at weight/20 s:
# a positions read is 2x20 + (1 + dexes)x2 weight, about 3 s, where 120 each cost 18 s.
# History reads add weight per 20 rows returned, so they keep a 120 up-front charge
# (a full 2000-row fills page is 20 + 100).
HYPERLIQUID_WEIGHTS = {'clearinghouseState': 2, 'spotClearinghouseState': 2,
    'perpDexs': 20, 'userAbstraction': 20,
    'userFillsByTime': 120, 'userFunding': 120, 'userNonFundingLedgerUpdates': 120}
BINANCE_WEIGHTS = {'/fapi/v1/time': 1, '/fapi/v3/positionRisk': 5,
                  '/fapi/v3/account': 5, '/fapi/v1/accountConfig': 5,
                  '/fapi/v3/balance': 5, '/fapi/v1/userTrades': 5, '/fapi/v1/income': 30,
                  '/sapi/v1/asset/transfer': 1, '/sapi/v1/capital/deposit/hisrec': 1,
                  '/sapi/v1/capital/withdraw/history': 18000}
DAY_MS = 86400000
# Round 3: one positions read per venue and configured account serves the positions tool,
# portfolio risk and the summary for this long. Only complete reads are kept.
POSITIONS_CACHE_SECONDS = 30
# A positions read waits this long for a fills/funding history read (its paced pages can
# hold the venues for minutes), then serves the cached copy or PERPS_READ_BUSY. It still waits for
# another positions read, whose result it then shares from the cache.
POSITIONS_BUSY_WAIT_SECONDS = 2.0
# Unconfigured fallback (tests, scripts): the export HMAC-SHA256 acct_ scheme over a
# process-local salt, so keys stay opaque but rotate on restart.
_PRIVACY = _Privacy(secrets.token_bytes(32), [])
# Once the server configures it, account_id is futures_risk's RiskStore.account_id
# (HMAC-SHA256 over the identity salt persisted in the portfolio DB): stable across restarts
# and equal in positions, fills, funding and the stored risk / crypto NAV rows.
_ACCOUNT_KEYS = {'factory': None, 'store': None, 'failed_factory': None}
_ACCOUNT_KEYS_LOCK = threading.Lock()


def configure_account_keys(store_factory):
    """`store_factory()` -> an object with RiskStore.account_id (the server: a read-only
    futures_risk.RiskKeys); opened on first use, then reused. A failed factory is not
    retried, so keys keep one scope per process and no call waits on a retry."""
    with _ACCOUNT_KEYS_LOCK:
        _ACCOUNT_KEYS.update(factory=store_factory, store=None, failed_factory=None)


def account_key_scope():
    """'stable_db_salt' once the DB salt is in use, else 'process_local'."""
    return 'stable_db_salt' if _ACCOUNT_KEYS['store'] is not None else 'process_local'


def _account_key(venue, identifier):
    with _ACCOUNT_KEYS_LOCK:
        factory = _ACCOUNT_KEYS['factory']
        if _ACCOUNT_KEYS['store'] is None and factory is not None and factory is not _ACCOUNT_KEYS.get('failed_factory'):
            try:
                _ACCOUNT_KEYS['store'] = factory()
            except Exception:  # noqa: BLE001 — no DB salt: the process-local key, labelled by scope
                _ACCOUNT_KEYS['failed_factory'] = factory
                logging.getLogger(__name__).warning('account key store unavailable: ACCOUNT_KEY_PROCESS_LOCAL')
        store = _ACCOUNT_KEYS['store']
    return store.account_id(venue, identifier) if store is not None else _PRIVACY.opaque(venue + ':' + identifier)


class _NoAccountWireLogs(logging.Filter):
    def filter(self, record):
        return False


# httpx INFO exposes signed URLs; httpcore DEBUG exposes header bytes. Filters
# remain effective if a caller enables DEBUG or installs an additional handler.
for _logger_name in ('httpx', 'httpcore', 'httpcore.connection', 'httpcore.http11',
                     'httpcore.http2', 'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(_logger_name).addFilter(_NoAccountWireLogs())


class FuturesError(RuntimeError):
    """Fixed public errors; never retain requests, response bodies or causes."""
    def __init__(self, code, *, status=None, retryable=False, retry_after=None):
        super().__init__('Crypto futures account read unavailable')
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after_seconds = retry_after

    def public(self):
        return {'code': self.code, 'message': str(self), 'upstream_status': self.status,
                'retryable': self.retryable, 'retry_after_seconds': self.retry_after_seconds}


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def _amount(value):
    number = _number(value)
    return str(number) if number is not None else None


def _text(value):
    # Only typed identifier tokens may cross from upstream responses.
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.search(r'0x[0-9a-fA-F]{40}(?![0-9a-fA-F])', value):
        return None
    return value if re.fullmatch(r'[A-Za-z0-9_:@.\-]{1,80}', value) else None


def _timestamp(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _identity(venue, identifier):
    return {'account_id': _account_key(venue, identifier),
            'account_last4': identifier[-4:] if venue == 'hyperliquid' else None}


def _hyperliquid_body_allowed(body, addresses):
    if not isinstance(body, dict) or not isinstance(body.get('type'), str):
        return False
    kind = body['type']
    if kind not in HYPERLIQUID_ALLOWLIST:
        return False
    required = {'type'} if kind == 'perpDexs' else {'type', 'user'}
    if not required <= set(body) or set(body) - required - HYPERLIQUID_PARAMS[kind]:
        return False
    if kind != 'perpDexs' and body['user'] not in addresses:
        return False
    if 'dex' in body and (not isinstance(body['dex'], str) or
            (body['dex'] != '' and (not re.fullmatch(r'[A-Za-z0-9_\-]{1,80}', body['dex'])
                                   or _text(body['dex']) != body['dex']))):
        return False
    for field in ('startTime', 'endTime'):
        if field in body and _timestamp(body[field]) is None:
            return False
    return not ('startTime' in body and 'endTime' in body and body['endTime'] < body['startTime'])


def _window(start, end):
    try:
        lo = datetime.strptime(start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        hi = datetime.strptime(end, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if lo.strftime('%Y-%m-%d') != start or hi.strftime('%Y-%m-%d') != end or hi < lo:
            raise ValueError
        return int(lo.timestamp()*1000), int(hi.timestamp()*1000) + DAY_MS - 1
    except (TypeError, ValueError, OverflowError):
        pass
    raise FuturesError('INVALID_FUTURES_QUERY')


def _rows(payload):
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise FuturesError('FUTURES_INVALID_RESPONSE')
    return payload


class _ReadClient:
    def __init__(self, http, base_url, sleeper, max_pages):
        self._http = http or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self._base_url = base_url
        self._sleeper = sleeper
        self.max_pages = min(max(max_pages, 1), 40)
        self._lock = threading.RLock()
        self._next_request = 0.0
        self._cooldown_until = 0.0
        self._cooldown_error = None
        self._budget = provider_pacing.BINANCE_BUDGET

    def inference_client(self):
        """Separate mutable transport state, preserving prior rate limits.

        Credentials and the HTTP connection pool stay in memory. No environment
        reload or new route is needed. An inherited cooldown is honored, while
        new supplemental failures cannot mutate the legacy client's state.
        """
        client = copy.copy(self)
        client._lock = threading.RLock()
        client._budget = provider_pacing.SupplementalBudget(self._budget)
        return client

    def _send(self, method, path, *, weight, query='', headers=None, body=None, prepare=None):
        # Guard the common transport too; no alternate internal entry point may
        # evade a venue's allowlist or redirect an authenticated request.
        binance = self._base_url == 'https://fapi.binance.com'
        if binance:
            if (method, path) not in BINANCE_ALLOWLIST:
                raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        elif (method != 'POST' or path != '/info' or query or headers or prepare
              or not _hyperliquid_body_allowed(body, getattr(self, '_addresses', ()))):
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        error = None
        with self._lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                raise FuturesError(self._cooldown_error.code, status=self._cooldown_error.status,
                                   retryable=True, retry_after=self._cooldown_error.retry_after_seconds)
            # Public and signed Binance reads share a conservative weight budget.
            # Withdrawal history has a separate UID limit; retain <=1/second.
            interval = 1.01 if path == '/sapi/v1/capital/withdraw/history' else weight / 10 + .01
            admission = self._budget.request(interval=interval, sleeper=self._sleeper) if binance else nullcontext()
            try:
                with admission:
                    if not binance:
                        self._sleeper(max(0, self._next_request-now))
                        self._next_request = time.monotonic() + weight / 20
                    # Sign only AFTER waiting for admission, so a queued public
                    # request cannot consume the signed timestamp's recvWindow.
                    if prepare is not None:
                        query, headers = prepare()
                    try:
                        base_url = 'https://api.binance.com' if path.startswith('/sapi/') else self._base_url
                        url = base_url + path + ('?' + query if query else '')
                        response = self._http.request(method, url, headers=headers, json=body,
                                                      follow_redirects=False)
                        if response.status_code in (418, 429):
                            delay = retry_after_seconds(response.headers.get('Retry-After'))
                            error = FuturesError('FUTURES_RATE_LIMITED', status=response.status_code,
                                                 retryable=True, retry_after=delay)
                            self._cooldown_until = time.monotonic() + (delay if delay is not None else 60)
                            self._cooldown_error = error
                            if binance:
                                self._budget.defer(response.status_code, delay if delay is not None else 60)
                        elif not 200 <= response.status_code < 300:
                            code = 'FUTURES_UPSTREAM_ERROR'
                            # Keep only the numeric skew signal; upstream messages are private.
                            if response.status_code == 400:
                                try:
                                    payload = response.json()
                                    if isinstance(payload, dict) and payload.get('code') == -1021:
                                        code = 'BINANCE_CLOCK_SKEW'
                                except ValueError:
                                    pass
                            error = FuturesError(code, status=response.status_code,
                                                 retryable=response.status_code >= 500)
                        else:
                            try:
                                return response.json()
                            except ValueError:
                                # A received but malformed success body is a
                                # semantic failure, not a transport outage.
                                error = FuturesError('FUTURES_INVALID_RESPONSE')
                    except Exception:
                        error = FuturesError('FUTURES_TRANSPORT_ERROR', retryable=True)
            except provider_pacing.RequestThrottled as exc:
                error = FuturesError('FUTURES_RATE_LIMITED', status=exc.status,
                                     retryable=True, retry_after=exc.retry_after_seconds)
        # Raise outside the except suite: no secret-bearing exception context.
        raise error


class BinanceAccountClient(_ReadClient):
    def __init__(self, api_key, signing_key, *, http=None, clock=time.time,
                 sleeper=time.sleep, recv_window=5000, max_pages=40):
        if not api_key or not isinstance(signing_key, rsa.RSAPrivateKey):
            raise FuturesError('BINANCE_FUTURES_INVALID_CONFIG')
        super().__init__(http, 'https://fapi.binance.com', sleeper, max_pages)
        self._api_key = api_key
        self._signing_key = signing_key
        self._clock = clock
        self._skew_ms = 0
        self._recv_window = min(max(recv_window, 1), 10000)

    @property
    def identity(self):
        return _identity('binance', self._api_key)

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        directory = env.get('CREDENTIALS_DIRECTORY', '')
        paths = [env.get(name) or (str(Path(directory) / suffix) if directory else '')
                 for name, suffix in [('BINANCE_FUTURES_API_KEY_FILE', 'binance-futures-api-key'),
                                      ('BINANCE_FUTURES_SIGNING_KEY_FILE', 'binance-futures-signing-key')]]
        if not all(paths):
            raise FuturesError('BINANCE_FUTURES_NOT_CONFIGURED')
        try:
            api_key = Path(paths[0]).read_text().strip()
            signing_key = serialization.load_pem_private_key(Path(paths[1]).read_bytes(), password=None)
            if api_key and '\n' not in api_key and isinstance(signing_key, rsa.RSAPrivateKey):
                return cls(api_key, signing_key)
        except FileNotFoundError:
            error = FuturesError('BINANCE_FUTURES_NOT_CONFIGURED')
        except Exception:
            error = FuturesError('BINANCE_FUTURES_INVALID_CONFIG')
        else:
            error = FuturesError('BINANCE_FUTURES_INVALID_CONFIG')
        raise error

    def request(self, method, path, params=None):
        if (method, path) not in BINANCE_ALLOWLIST:
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        params = dict(params or {})
        if 'signature' in params or 'timestamp' in params or 'recvWindow' in params:
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        if path == '/sapi/v1/asset/transfer' and params.get('type') not in BINANCE_WALLET_ROUTES:
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        if path == '/fapi/v1/income' and 'incomeType' in params and params.get('incomeType') not in ('FUNDING_FEE', 'COMMISSION', 'REALIZED_PNL'):
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        with self._lock:
            for attempt in range(2):
                if path == '/fapi/v1/time':
                    return self._send(method, path, weight=1)
                def prepare():
                    query = urlencode({**params, 'timestamp': int(self._clock()*1000)+self._skew_ms,
                                       'recvWindow': self._recv_window})
                    signed = self._signing_key.sign(query.encode(), padding.PKCS1v15(), hashes.SHA256())
                    signature = base64.b64encode(signed).decode('ascii')
                    return query + '&' + urlencode({'signature': signature}), {'X-MBX-APIKEY': self._api_key}

                error = None
                try:
                    return self._send(method, path, weight=BINANCE_WEIGHTS[path], prepare=prepare)
                except FuturesError as exc:
                    error = FuturesError(exc.code, status=exc.status, retryable=exc.retryable,
                                         retry_after=exc.retry_after_seconds)
                except Exception:
                    error = FuturesError('BINANCE_SIGNING_ERROR')
                if error.code == 'BINANCE_CLOCK_SKEW' and attempt == 0:
                    server_time = self.request('GET', '/fapi/v1/time')
                    stamp = _timestamp(server_time.get('serverTime')) if isinstance(server_time, dict) else None
                    if stamp is None:
                        raise FuturesError('FUTURES_INVALID_RESPONSE')
                    self._skew_ms = stamp - int(self._clock()*1000)
                    continue
                raise error

    def _validated_account_read(self, path, validator):
        from crypto_validation import CryptoValidationError

        payload = self.request('GET', path)
        error = None
        try:
            return validator(payload)
        except CryptoValidationError as exc:
            error = FuturesError(exc.code)
        raise error

    def account_config(self):
        from crypto_validation import binance_account_config

        return self._validated_account_read('/fapi/v1/accountConfig', binance_account_config)

    def account_information(self):
        from crypto_validation import binance_account

        return self._validated_account_read('/fapi/v3/account', binance_account)

    def retention_start(self):
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        year, month = now.year, now.month-3
        if month <= 0:
            year, month = year-1, month+12
        return int(now.replace(year=year, month=month,
            day=min(now.day, calendar.monthrange(year, month)[1])).timestamp()*1000)


class HyperliquidAccountClient(_ReadClient):
    def __init__(self, addresses, *, http=None, sleeper=time.sleep, max_pages=40):
        normalized = list(dict.fromkeys(address.strip().lower() for address in addresses))
        if not normalized or any(not re.fullmatch(r'0x[0-9a-f]{40}', address) for address in normalized):
            raise FuturesError('HYPERLIQUID_INVALID_CONFIG')
        super().__init__(http, 'https://api.hyperliquid.xyz', sleeper, max_pages)
        self._addresses = normalized

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        value = env.get('HYPERLIQUID_ACCOUNT_ADDRESSES', '').strip()
        if not value:
            raise FuturesError('HYPERLIQUID_NOT_CONFIGURED')
        return cls(value.split(','))

    def info(self, kind, address, **params):
        if not isinstance(kind, str) or kind not in HYPERLIQUID_ALLOWLIST or set(params) - HYPERLIQUID_PARAMS[kind]:
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        body = {'type': kind, **params}
        if kind != 'perpDexs':
            body['user'] = address
        if address not in self._addresses or not _hyperliquid_body_allowed(body, self._addresses):
            raise FuturesError('FUTURES_READ_NOT_ALLOWED')
        return self._send('POST', '/info', weight=HYPERLIQUID_WEIGHTS[kind], body=body)


class _Query:
    def __init__(self, client, operation, start, end):
        self.client = client
        self.operation = operation
        self.start, self.end = start, end
        self.pages = 0
        self.reasons = set()
        self.rows = {}
        self.times = []
        self.symbol_selection = None

    def read(self, call):
        if self.pages >= self.client.max_pages:
            self.reasons.add('PAGE_BUDGET')
            return None
        self.pages += 1
        return call()

    def history(self, call, start, end, limit, key, consume):
        # Bisect full pages rather than advancing past their final timestamp.
        # Works with newest-first, unordered rows, and same-millisecond ties.
        pending = [(start, min(end, start+7*DAY_MS-1))]
        cursor = pending[0][1]+1
        while cursor <= end:
            pending.append((cursor, min(end, cursor+7*DAY_MS-1)))
            cursor = pending[-1][1]+1
            if len(pending) > self.client.max_pages:
                self.reasons.add('PAGE_BUDGET')
                break
        while pending:
            lo, hi = pending.pop(0)
            payload = self.read(lambda: call(lo, hi))
            if payload is None:
                break
            rows = _rows(payload)
            for row in rows:
                stamp = _timestamp(row.get('time'))
                if stamp is None:
                    self.reasons.add('MISSING_TIMESTAMP')
                    continue
                if not lo <= stamp <= hi:
                    self.reasons.add('OUT_OF_WINDOW_RESPONSE')
                    continue
                consume(row, key(row))
            if len(rows) >= limit:
                if lo == hi:
                    self.reasons.add('DENSE_TIMESTAMP')
                else:
                    middle = (lo+hi)//2
                    pending[0:0] = [(lo, middle), (middle+1, hi)]

    def add(self, key, row):
        if key is None or row.get('fill_id', row.get('payment_id', 'position')) is None:
            self.reasons.add('MISSING_ROW_ID')
            key = tuple(sorted((k, str(v)) for k, v in row.items()))
        self.rows[key] = row

    def finish(self, venue, output, error=None):
        result = {**output, 'coverage': {'requested_window': {'start_time_ms': self.start, 'end_time_ms': self.end,
                                                          'timezone': 'UTC'},
            'observed_window': {'start_time_ms': min(self.times) if self.times else None,
                                'end_time_ms': max(self.times) if self.times else None},
            'pages_fetched': self.pages, 'symbol_selection': self.symbol_selection,
            'historical_coverage': 'venue_retention_limited' if venue == 'binance' else 'venue_history_limited',
            'history_limit': 'past_three_months' if venue == 'binance' else (
                'latest_10000_fills' if self.operation == 'fills' else None),
            'account_scope': 'configured_USD_M_account' if venue == 'binance' else (
                'configured_addresses_all_perpetual_dexes_and_spot' if self.operation == 'positions' else 'configured_addresses_perpetuals')}}
        if error:
            result['error'] = error.public()
            result['source_status'] = 'not_configured' if error.code.endswith('NOT_CONFIGURED') else 'failed'
            self.reasons.add(error.code)
        else:
            result['error'] = None
            result['source_status'] = 'truncated' if self.reasons & {'PAGE_BUDGET', 'DENSE_TIMESTAMP', 'SYMBOL_DISCOVERY_INCOMPLETE', 'RETENTION_LIMIT'} else (
                'ok' if any(output.get(field) for field in ('positions', 'balances', 'fills', 'funding_payments')) else 'empty')
        result['reason_codes'] = sorted(self.reasons)
        result['result_status'] = 'unavailable' if error and not self.rows and not output.get('balances') else ('partial' if self.reasons else 'complete')
        result['coverage']['fetch_complete'] = not error and not self.reasons - {'HYPERLIQUID_HISTORY_LIMIT', 'MISSING_AMOUNT_OR_ASSET'}
        if self.operation == 'funding':
            result['totals_by_asset'] = _totals(list(self.rows.values()), result['result_status']) if not error or self.rows else None
        return result


def _totals(rows, status):
    assets = {}
    for row in rows:
        asset = row.get('asset')
        if asset is None:
            continue
        assets.setdefault(asset, []).append(_number(row.get('amount')))
    return {asset: {'value': str(sum(values, Decimal(0))) if all(v is not None for v in values) else None,
                    'status': status if all(v is not None for v in values) else 'unavailable',
                    'reason_codes': (['PARTIAL_COVERAGE'] if status != 'complete' else []) +
                                    ([] if all(v is not None for v in values) else ['MISSING_AMOUNT'])}
            for asset, values in assets.items()}


def _position(row, venue, identity):
    size = _number(row.get('positionAmt') if venue == 'binance' else row.get('szi'))
    if size == 0:
        return None
    hl = venue == 'hyperliquid'
    leverage = row.get('leverage') or {}
    mode = row.get('marginType') if not hl else leverage.get('type') if isinstance(leverage, dict) else None
    position = {**identity, 'symbol': _text(row.get('coin' if hl else 'symbol')),
        'side': 'long' if size is not None and size > 0 else 'short' if size is not None else None,
        'size': str(abs(size)) if size is not None else None, 'signed_size': _amount(size),
        'asset': 'USDC' if hl else _text(row.get('marginAsset')),
        'entry_price': _amount(row.get('entryPx' if hl else 'entryPrice')),
        'mark_price': _amount(row.get('markPx' if hl else 'markPrice')),
        'unrealized_pnl': _amount(row.get('unrealizedPnl' if hl else 'unRealizedProfit')),
        'leverage': _amount(leverage.get('value') if hl and isinstance(leverage, dict) else row.get('leverage')),
        'margin_mode': mode if mode in ('cross', 'isolated') else None,
        'liquidation_price': _amount(row.get('liquidationPx' if hl else 'liquidationPrice')),
        'notional': _amount(row.get('positionValue' if hl else 'notional'))}
    position['field_status'] = {field: {'value': position[field],
        'status': 'unavailable' if position[field] is None else 'complete',
        'reason_codes': ['FIELD_NOT_RETURNED'] if position[field] is None else []}
        for field in ('size', 'entry_price', 'mark_price', 'unrealized_pnl', 'leverage',
                      'margin_mode', 'liquidation_price', 'notional', 'asset')}
    # Hyperliquid clearinghouseState sends no markPx; its positionValue is size x mark, so the mark
    # is derived (as futures_risk does) and labelled, while mark_price stays the venue's own field.
    if hl:
        notional = _number(row.get('positionValue'))
        derived = abs(notional) / abs(size) if position['mark_price'] is None and notional is not None and size else None
        position['mark_price_derived'] = {'value': _amount(derived),
            'status': 'derived' if derived is not None else 'unavailable',
            'source': 'positionValue/szi' if derived is not None else None,
            'formula': 'abs(positionValue) / abs(szi)' if derived is not None else None,
            'reason_codes': [] if derived is not None else ['MARK_REPORTED' if position['mark_price'] is not None
                                                            else 'MARK_DERIVATION_INPUT_UNAVAILABLE']}
    _liquidation_distance(position)
    return position


#: Liquidation-distance sign convention, stated once per positions response (same formula as futures_risk).
LIQUIDATION_DISTANCE = {
    'field': 'distance_to_liquidation_pct',
    'formula': '(mark - liquidation_price) / mark * 100 for a long; (liquidation_price - mark) / mark * 100 for a short',
    'sign': 'positive: the adverse mark move, in % of the mark, that reaches liquidation; 0 at liquidation; '
            'negative: the mark is already past liquidation_price',
    'mark': 'the venue mark_price, else mark_price_derived (Hyperliquid sends no mark)',
    'unavailable': 'null when liquidation_price is missing or not positive (no liquidation level reported) or no mark',
}


def _liquidation_distance(position):
    reported = _number(position['mark_price'])
    derived = _number((position.get('mark_price_derived') or {}).get('value'))
    mark, basis = (reported, 'venue') if reported is not None else (derived, 'derived') if derived is not None else (None, None)
    liquidation = _number(position['liquidation_price'])
    reasons = ([] if liquidation is not None and liquidation > 0 else ['LIQUIDATION_PRICE_UNAVAILABLE']) + \
              ([] if mark is not None and mark > 0 else ['MARK_UNAVAILABLE']) + ([] if position['side'] else ['SIDE_UNAVAILABLE'])
    distance = None
    if not reasons:
        move = mark - liquidation if position['side'] == 'long' else liquidation - mark
        distance = (move / mark * 100).quantize(Decimal('0.0001'))
    position['distance_to_liquidation_pct'] = _amount(distance)
    position['field_status']['distance_to_liquidation_pct'] = {'value': position['distance_to_liquidation_pct'],
        'status': 'unavailable' if reasons else 'derived', 'mark_basis': basis if not reasons else None,
        'reason_codes': reasons}


def _funding(row, venue, identity):
    hl = venue == 'hyperliquid'
    delta = row.get('delta') or {} if hl else row
    return {**identity, 'payment_id': _text(str(row.get('hash' if hl else 'tranId', ''))) or None,
        'symbol': _text(delta.get('coin' if hl else 'symbol')),
        'time': _timestamp(row.get('time')), 'asset': 'USDC' if hl else _text(row.get('asset')),
        'amount': _amount(delta.get('usdc' if hl else 'income'))}


def _fill(row, venue, identity):
    hl = venue == 'hyperliquid'
    return {**identity, 'fill_id': _text(str(row.get('tid' if hl else 'id', ''))) or None,
        'symbol': _text(row.get('coin' if hl else 'symbol')), 'time': _timestamp(row.get('time')),
        'side': {'B': 'buy', 'A': 'sell', 'BUY': 'buy', 'SELL': 'sell'}.get(row.get('side')),
        'size': _amount(row.get('sz' if hl else 'qty')), 'price': _amount(row.get('px' if hl else 'price')),
        'fee': _amount(row.get('fee' if hl else 'commission')),
        'fee_asset': _text(row.get('feeToken' if hl else 'commissionAsset')),
        'realized_pnl': _amount(row.get('closedPnl' if hl else 'realizedPnl')),
        'pnl_asset': 'USDC' if hl else _text(row.get('marginAsset'))}


class FuturesAccounts:
    def __init__(self, binance_factory: Callable | None = BinanceAccountClient.from_environment,
                 hyperliquid_factory: Callable | None = HyperliquidAccountClient.from_environment,
                 ledger_observer: Callable | None = None, *,
                 positions_cache_seconds: float = POSITIONS_CACHE_SECONDS,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
                 busy_wait_seconds: float = POSITIONS_BUSY_WAIT_SECONDS):
        self._factories = {'binance': binance_factory, 'hyperliquid': hyperliquid_factory}
        self._ledger_observer = ledger_observer
        self._clients = {}
        self._lock = threading.RLock()
        self._positions_ttl = min(max(positions_cache_seconds, 0), 60)
        self._clock, self._wall_clock = clock, wall_clock
        self._positions_cache = {}
        self._cache_lock = threading.Lock()
        self._busy_wait = max(busy_wait_seconds, 0)
        self._holder = None  # the operation holding self._lock, read by waiting positions callers

    @staticmethod
    def _cache_key(name, client):
        # Venue plus the configured account (Binance API key, Hyperliquid address set); never output.
        return (name, getattr(client, '_api_key', None), tuple(getattr(client, '_addresses', ())))

    def _cached_positions(self, name, client):
        with self._cache_lock:
            entry = self._positions_cache.get(self._cache_key(name, client))
            if entry is None:
                return None
            stored_at, read_at, result = entry
            age = self._clock() - stored_at
            if not 0 <= age < self._positions_ttl:
                self._positions_cache.pop(self._cache_key(name, client), None)
                return None
            return {**copy.deepcopy(result), 'cache': self._cache_state(True, True, age, read_at)}

    @contextmanager
    def _venues(self, operation):
        """Hold the venue lock; a positions read yields False instead of waiting out a history read."""
        if operation != 'positions':
            held = self._lock.acquire()
        else:
            held = self._lock.acquire(timeout=self._busy_wait)
            while not held and self._holder == 'positions':
                held = self._lock.acquire(timeout=self._busy_wait)
            held = held or self._lock.acquire(blocking=False)
        try:
            if held:
                self._holder = operation
            yield held
        finally:
            if held:
                self._holder = None
                self._lock.release()

    def _busy_positions(self, name):
        """Positions while a history read holds the venues: the cached copy, else unread with PERPS_READ_BUSY."""
        client = self._clients.get(name)
        cached = self._cached_positions(name, client) if client is not None else None
        if cached is not None:
            return cached
        error = (FuturesError('PERPS_READ_BUSY', retryable=True) if self._factories[name] is not None else
                 FuturesError('BINANCE_FUTURES_NOT_CONFIGURED' if name == 'binance' else 'HYPERLIQUID_NOT_CONFIGURED'))
        query = _Query(type('Missing', (), {'max_pages': 0})(), 'positions', None, None)
        return {**query.finish(name, {'positions': [], 'balances': []}, error), 'cache': None}

    def _cache_state(self, hit, stored, age, read_at):
        return {'hit': hit, 'stored': stored, 'age_seconds': round(age, 1), 'ttl_seconds': self._positions_ttl,
                'read_at': datetime.fromtimestamp(read_at, timezone.utc).isoformat(timespec='seconds')}

    def query(self, operation, venue='all', start_date='', end_date='', symbol=''):
        if venue not in ('all', 'binance', 'hyperliquid') or operation not in ('positions', 'fills', 'funding'):
            raise FuturesError('INVALID_FUTURES_QUERY')
        start, end = _window(start_date, end_date) if operation != 'positions' else (None, None)
        if not isinstance(symbol, str):
            raise FuturesError('INVALID_FUTURES_QUERY')
        symbol = symbol.strip().upper()
        if symbol and not re.fullmatch(r'[A-Z0-9_:@.\-]{1,40}', symbol):
            raise FuturesError('INVALID_FUTURES_QUERY')
        results = {}
        ledger_failed = False
        ledger_seconds = 1.8
        names = ('binance', 'hyperliquid') if venue == 'all' else (venue,)
        with self._venues(operation) as held:
            if not held:
                results = {name: self._busy_positions(name) for name in names}
            for name in names if held else ():
                error, output, query, read_at, read_started = None, {}, None, None, None
                field = {'positions':'positions', 'fills':'fills', 'funding':'funding_payments'}[operation]
                try:
                    if name not in self._clients:
                        factory = self._factories[name]
                        if factory is None:
                            raise FuturesError('BINANCE_FUTURES_NOT_CONFIGURED' if name == 'binance' else 'HYPERLIQUID_NOT_CONFIGURED')
                        self._clients[name] = factory()
                    client = self._clients[name]
                    if operation == 'positions':
                        cached = self._cached_positions(name, client)
                        if cached is not None:
                            results[name] = cached
                            continue
                    read_at, read_started = self._wall_clock(), self._clock()
                    query = _Query(client, operation, start, end)
                    if name == 'binance':
                        output = self._binance(query, symbol)
                    else:
                        output = self._hyperliquid(query, symbol)
                except FuturesError as exc:
                    error = exc
                except Exception:
                    error = FuturesError('FUTURES_INVALID_RESPONSE')
                if query is None:
                    query = _Query(type('Missing', (), {'max_pages': 0})(), operation, start, end)
                output[field] = list(query.rows.values())
                if operation == 'positions':
                    output.setdefault('balances', [])
                results[name] = query.finish(name, output, error)
                if operation == 'positions':
                    # Failed, partial or truncated reads are served once and never reused.
                    stored = error is None and results[name]['result_status'] == 'complete' and self._positions_ttl > 0
                    if stored:
                        # Age counts from the start of the read, not its end.
                        with self._cache_lock:
                            self._positions_cache[self._cache_key(name, client)] = (
                                read_started, read_at, copy.deepcopy(results[name]))
                    results[name]['cache'] = None if read_at is None else self._cache_state(False, stored, 0, read_at)
                if self._ledger_observer and operation in ('fills', 'funding'):
                    ledger_started = time.monotonic()
                    try:
                        from portfolio_ledger import persistence_budget
                        if ledger_failed or ledger_seconds <= 0:
                            raise RuntimeError('LEDGER_WRITE_FAILED')
                        with persistence_budget(ledger_seconds):
                            self._ledger_observer(operation, name, self._clients.get(name), results[name])
                    except Exception:
                        logging.getLogger(__name__).error("permanent ledger observation failed: LEDGER_WRITE_FAILED")
                        ledger_failed = True
                        results[name]["ledger_persistence"] = {"status": "partial", "reason_codes": ["LEDGER_WRITE_FAILED"]}
                    finally:
                        ledger_seconds -= time.monotonic() - ledger_started
        statuses = [row['result_status'] for row in results.values()]
        result_status = 'complete' if all(s == 'complete' for s in statuses) else ('unavailable' if all(s == 'unavailable' for s in statuses) else 'partial')
        return {'venues': results, 'source_status': {name:row['source_status'] for name,row in results.items()},
                'coverage': {name:row['coverage'] for name,row in results.items()},
                'result_status': result_status, 'reason_codes': sorted({code for r in results.values() for code in r['reason_codes']}),
                'account_key': {'field': 'account_id', 'scheme': 'futures_risk_hmac_sha256_v1', 'scope': account_key_scope()},
                **({'liquidation_distance': LIQUIDATION_DISTANCE} if operation == 'positions' else {})}

    def _binance(self, q, symbol):
        client = q.client
        identity = client.identity
        if q.operation == 'positions':
            for row in _rows(q.read(lambda:client.request('GET','/fapi/v3/positionRisk'))):
                position = _position(row, 'binance', identity)
                if position:
                    q.add((position['symbol'],row.get('positionSide')),position)
            balances = []
            for row in _rows(q.read(lambda:client.request('GET','/fapi/v3/balance'))):
                balances.append({**identity, 'asset': _text(row.get('asset')), 'wallet_balance': _amount(row.get('balance')),
                    'cross_wallet_balance': _amount(row.get('crossWalletBalance')),
                    'cross_unrealized_pnl': _amount(row.get('crossUnPnl')), 'available_balance': _amount(row.get('availableBalance')),
                    'equity': None, 'wallet_balance_includes_unrealized_pnl': False,
                    'equity_includes_unrealized_pnl': None})
            return {'balances': balances}
        if q.start < client.retention_start():
            q.reasons.add('RETENTION_LIMIT')
        if q.operation == 'funding':
            def consume(row, key):
                funding = _funding(row,'binance',identity)
                q.add(key,funding)
                q.times.append(funding['time'])
                if funding['amount'] is None or funding['asset'] is None:
                    q.reasons.add('MISSING_AMOUNT_OR_ASSET')
            params = {'incomeType':'FUNDING_FEE', 'limit':1000}
            if symbol:
                params['symbol'] = symbol
            q.history(lambda lo,hi:client.request('GET','/fapi/v1/income',{**params,'startTime':lo,'endTime':hi}),
                      q.start,q.end,1000,lambda r:(r.get('tranId'),r.get('incomeType'),r.get('symbol')),consume)
            return {}
        symbols = {symbol} if symbol else set()
        q.symbol_selection = 'explicit' if symbol else 'income_history_and_current_positions'
        if not symbol:
            q.reasons.add('SYMBOL_DISCOVERY_INCOMPLETE')
            for kind in ('COMMISSION','REALIZED_PNL','FUNDING_FEE'):
                q.history(lambda lo,hi:client.request('GET','/fapi/v1/income',{'incomeType':kind,'limit':1000,'startTime':lo,'endTime':hi}),
                    q.start,q.end,1000,lambda r:r.get('tranId'),lambda r,k:symbols.add(_text(r.get('symbol'))))
            positions = q.read(lambda:client.request('GET','/fapi/v3/positionRisk'))
            if positions is not None:
                symbols.update(_text(row.get('symbol')) for row in _rows(positions))
        for sym in sorted(s for s in symbols if s):
            def consume(row, key):
                fill = _fill(row,'binance',identity)
                q.add(key,fill)
                q.times.append(fill['time'])
            q.history(lambda lo,hi:client.request('GET','/fapi/v1/userTrades',{'symbol':sym,'startTime':lo,'endTime':hi,'limit':1000}),
                q.start,q.end,1000,lambda r:(sym,r.get('id')),consume)
        return {}

    def _hyperliquid_positions(self, q):
        """Unified USDC shared balance, not a verified UI portfolio valuation."""
        balances, account_values = [], []
        for address in q.client._addresses:
            identity = _identity('hyperliquid', address)
            reasons, dexes, fetched, failed = set(), [''], [], []

            def reason(code):
                reasons.add(code)
                q.reasons.add(code)

            def read(kind, **params):
                if q.pages >= q.client.max_pages:
                    reason('PAGE_BUDGET')
                    return None
                try:
                    payload = q.read(lambda: q.client.info(kind, address, **params))
                except FuturesError as exc:
                    reason(exc.code)
                    reason('HYPERLIQUID_DEX_READ_FAILED' if kind == 'clearinghouseState'
                           else 'HYPERLIQUID_ACCOUNT_READ_FAILED')
                    return None
                if payload is None:
                    reason('FUTURES_INVALID_RESPONSE')
                return payload

            mode = read('userAbstraction')
            if mode not in ('unifiedAccount', 'disabled', 'default', 'dexAbstraction', 'portfolioMargin'):
                mode = None
                reason('HYPERLIQUID_ABSTRACTION_UNAVAILABLE')
            supported = mode in ('unifiedAccount', 'disabled', 'default')
            if mode is not None and not supported:
                reason('HYPERLIQUID_UNSUPPORTED_ABSTRACTION')
            discovered = read('perpDexs')
            if not isinstance(discovered, list):
                reason('HYPERLIQUID_DEX_DISCOVERY_INCOMPLETE')
            else:
                # Bound both processing and requests, including untrusted discovery responses.
                if len(discovered) > HYPERLIQUID_MAX_NAMED_DEXES + 1:
                    reason('HYPERLIQUID_DEX_LIMIT')
                for dex in discovered[:HYPERLIQUID_MAX_NAMED_DEXES + 1]:
                    if dex is None:
                        continue
                    name = dex.get('name') if isinstance(dex, dict) else None
                    if not isinstance(name, str) or not name or not _hyperliquid_body_allowed(
                            {'type': 'clearinghouseState', 'user': address, 'dex': name}, q.client._addresses):
                        reason('HYPERLIQUID_DEX_DISCOVERY_INCOMPLETE')
                    elif name not in dexes:
                        if len(dexes) > HYPERLIQUID_MAX_NAMED_DEXES:
                            reason('HYPERLIQUID_DEX_LIMIT')
                        else:
                            dexes.append(name)

            # Read the shared wallet before dex enumeration can exhaust the budget.
            spot = read('spotClearinghouseState')
            spot_usdc = None
            if not isinstance(spot, dict) or not isinstance(spot.get('balances'), list):
                reason('HYPERLIQUID_SPOT_READ_FAILED')
            else:
                spot_usdc = Decimal(0)  # An explicit, valid empty wallet has zero USDC.
                usdc_seen = False
                for row in spot['balances']:
                    if not isinstance(row, dict) or _text(row.get('coin')) is None:
                        reason('FUTURES_INVALID_RESPONSE')
                        spot_usdc = None
                        continue
                    asset, total, hold = _text(row['coin']), _number(row.get('total')), _number(row.get('hold'))
                    if asset == 'USDC':
                        if usdc_seen or total is None:
                            reason('HYPERLIQUID_MISSING_ACCOUNT_VALUE_INPUT')
                            spot_usdc = None
                        elif spot_usdc is not None:
                            spot_usdc = total
                        usdc_seen = True
                    balances.append({**identity, 'dex': '', 'balance_scope': 'spot', 'asset': asset,
                        'equity': _amount(total) if asset == 'USDC' else None,
                        'wallet_balance': _amount(total), 'hold': _amount(hold),
                        'available_balance': _amount(total - hold) if total is not None and hold is not None else None,
                        'margin_used': None,
                        'wallet_balance_includes_unrealized_pnl': None if mode == 'unifiedAccount' else False,
                        'equity_includes_unrealized_pnl': None if mode == 'unifiedAccount' else False,
                        'equity_usable_for_aggregation': asset == 'USDC' and supported})

            pnl, dex_equity, pnl_known, equity_known = Decimal(0), Decimal(0), True, True
            for dex in dexes:
                payload = read('clearinghouseState', dex=dex)
                if not isinstance(payload, dict) or not isinstance(payload.get('assetPositions'), list):
                    reason('HYPERLIQUID_DEX_READ_FAILED')
                    failed.append(dex)
                    pnl_known = equity_known = False
                    continue
                fetched.append(dex)
                seen_positions = set()
                for row in payload['assetPositions']:
                    raw = row.get('position') if isinstance(row, dict) else None
                    if not isinstance(raw, dict):
                        reason('FUTURES_INVALID_RESPONSE')
                        pnl_known = False
                        continue
                    position = _position(raw, 'hyperliquid', {**identity, 'dex': dex})
                    if position:
                        if position['symbol'] in seen_positions:
                            reason('FUTURES_INVALID_RESPONSE')
                            pnl_known = False
                            continue
                        seen_positions.add(position['symbol'])
                        q.add((identity['account_id'], dex, position['symbol']), position)
                        amount = _number(position['unrealized_pnl'])
                        if amount is None or position['symbol'] is None or position['size'] is None:
                            pnl_known = False
                        else:
                            pnl += amount
                summary = payload.get('marginSummary')
                summary = summary if isinstance(summary, dict) else {}
                equity = _number(summary.get('accountValue'))
                if equity is None:
                    equity_known = False
                else:
                    dex_equity += equity
                balances.append({**identity, 'dex': dex, 'balance_scope': 'perpetual', 'asset': 'USDC',
                    'equity': _amount(equity), 'margin_used': _amount(summary.get('totalMarginUsed')),
                    'available_balance': _amount(payload.get('withdrawable')), 'wallet_balance': None,
                    'wallet_balance_includes_unrealized_pnl': None, 'equity_includes_unrealized_pnl': True,
                    'equity_usable_for_aggregation': mode in ('disabled', 'default')})

            unified = mode == 'unifiedAccount'
            complete_dexes = not (failed or reasons & {'HYPERLIQUID_DEX_LIMIT',
                'HYPERLIQUID_DEX_DISCOVERY_INCOMPLETE', 'FUTURES_INVALID_RESPONSE'})
            component_known = unified or equity_known
            value = None
            if supported and spot_usdc is not None and complete_dexes and component_known:
                # Spot state is authoritative in unified mode. The docs do not
                # establish that position PnL is an additional balance component.
                value = spot_usdc if unified else spot_usdc + dex_equity
            if value is None:
                reason('HYPERLIQUID_MISSING_ACCOUNT_VALUE_INPUT')
            formula = ('spot_USDC_total' if unified else
                       'spot_USDC_total + sum(all_dex_marginSummary_accountValue)' if supported else None)
            account_values.append({**identity, 'asset': 'USDC', 'value': _amount(value),
                'status': 'partial' if reasons else 'complete', 'reason_codes': sorted(reasons),
                'ui_portfolio_value_status': 'unverified',
                'provenance': {'abstraction': mode, 'formula': formula, 'default_dex': '',
                    'spot_USDC_total': _amount(spot_usdc),
                    'position_unrealized_pnl': _amount(pnl) if pnl_known and complete_dexes else None,
                    'dex_account_value_sum': _amount(dex_equity) if equity_known and complete_dexes else None,
                    'dexes_discovered': dexes, 'dexes_fetched': fetched, 'dexes_failed': failed,
                    'scope': 'USDC_shared_balance' if unified else 'USDC_spot_and_assumed_USDC_perpetual_collateral',
                    'unrealized_pnl_in_spot_total': 'unverified' if unified else 'not_applicable',
                    'assumptions': ([] if unified else [
                        'Perpetual equity is denominated in USDC; collateral metadata is not read.']) + [
                        'USDC is valued at one USD; other spot tokens are not converted.',
                        'Unified value is the shared USDC balance, not verified UI Portfolio Value.',
                        'Hold, dex equity and position PnL are not added to the unified balance.',
                        'Sequential reads are not an atomic snapshot.'],
                    'sources': ['userAbstraction', 'perpDexs', 'spotClearinghouseState', 'clearinghouseState']}})
        amounts = [_number(row['value']) for row in account_values]
        total = sum(amounts, Decimal(0)) if amounts and all(v is not None for v in amounts) else None
        return {'balances': balances, 'account_values': account_values,
            'totals_by_asset': {'USDC': {'value': _amount(total),
                'status': 'partial' if q.reasons else 'complete', 'reason_codes': sorted(q.reasons),
                'ui_portfolio_value_status': 'unverified',
                'scope': 'USDC_shared_balance' if all(row['provenance']['abstraction'] == 'unifiedAccount'
                    for row in account_values) else 'USDC_spot_and_assumed_USDC_perpetual_collateral'}}}

    def _hyperliquid(self, q, symbol):
        if q.operation == 'positions':
            return self._hyperliquid_positions(q)
        client = q.client
        if q.operation == 'fills':
            q.reasons.add('HYPERLIQUID_HISTORY_LIMIT')
        for address in client._addresses:
            identity = _identity('hyperliquid',address)
            funding = q.operation == 'funding'
            kind = 'userFunding' if funding else 'userFillsByTime'
            def consume(row,key):
                item = _funding(row,'hyperliquid',identity) if funding else _fill(row,'hyperliquid',identity)
                coin = item['symbol']
                # /info fills includes spot rows too. @ tokens and BASE/QUOTE
                # are spot; only perpetual identifiers belong in these tools.
                if coin is None or coin.startswith('@') or '/' in coin or (symbol and coin.upper() != symbol):
                    return
                item['dex'] = coin.split(':', 1)[0] if ':' in coin else ''
                q.add(key,item)
                q.times.append(item['time'])
                if funding and item['amount'] is None:
                    q.reasons.add('MISSING_AMOUNT_OR_ASSET')
            q.history(lambda lo,hi:client.info(kind,address,startTime=lo,endTime=hi),q.start,q.end,
                500 if funding else 2000,lambda r:(identity['account_id'],r.get('hash') if funding else r.get('tid'),
                    (r.get('delta') or {}).get('coin') if funding else r.get('coin'),r.get('time')),consume)
        return {}
