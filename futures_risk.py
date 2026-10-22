"""Append-only prospective futures risk, in the portfolio DB's own namespace.

No NAV writes, history reconstruction, FX conversion, or MCP registration. Values
are decimal strings in native collateral units. Every numeric metric retains its
source inputs; missing inputs produce null plus a reason. Effective leverage is
gross notional / positive equity, independently of configured leverage.

Legacy risk rows retain their original meaning. Strict NAV evidence is a separate
payload, using the approved coherent Binance account reads and validated HL
observations. Captures are sequential reads, not an atomic exchange-wide snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from crypto_futures_accounts import (
    HYPERLIQUID_MAX_NAMED_DEXES,
    BinanceAccountClient,
    FuturesError,
    HyperliquidAccountClient,
    _amount,
    _hyperliquid_body_allowed,
    _number,
    _text,
)
from crypto_validation import (
    CryptoValidationError,
    binance_account_nav,
    hyperliquid_spot,
    hyperliquid_state,
)
from portfolio_store import default_db_path

CALCULATION_VERSION = 'futures_risk_v5_nav'
_TABLES = ('state', 'snapshots', 'venues', 'accounts', 'positions')
_SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_risk_state (key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS futures_risk_snapshots (
    snapshot_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
    calculation_version TEXT NOT NULL, source_status TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS futures_risk_venues (
    snapshot_id TEXT NOT NULL REFERENCES futures_risk_snapshots(snapshot_id),
    venue TEXT NOT NULL, source_status TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, venue)
);
CREATE TABLE IF NOT EXISTS futures_risk_accounts (
    snapshot_id TEXT NOT NULL REFERENCES futures_risk_snapshots(snapshot_id),
    ordinal INTEGER NOT NULL, venue TEXT NOT NULL, account_id TEXT NOT NULL,
    scope TEXT NOT NULL, dex TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, ordinal)
);
CREATE INDEX IF NOT EXISTS futures_risk_accounts_history
    ON futures_risk_accounts(venue, account_id, observed_at);
CREATE TABLE IF NOT EXISTS futures_risk_positions (
    snapshot_id TEXT NOT NULL REFERENCES futures_risk_snapshots(snapshot_id),
    ordinal INTEGER NOT NULL, venue TEXT NOT NULL, account_id TEXT NOT NULL,
    dex TEXT NOT NULL, symbol TEXT, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, ordinal)
);
""" + ''.join(f"""
CREATE TRIGGER IF NOT EXISTS futures_risk_{table}_no_{action.lower()}
BEFORE {action} ON futures_risk_{table} BEGIN
SELECT RAISE(ABORT, 'futures risk rows are immutable'); END;
""" for table in _TABLES for action in ('UPDATE', 'DELETE'))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Risk timestamp must have a timezone')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


class RiskKeys:
    """RiskStore's account_id from the identity salt it persisted, opened `?mode=ro`.

    For read tools: never creates the DB, the schema or the salt and never starts a
    write transaction. A missing DB, table or salt raises; only the writer
    (RiskStore) creates them.
    """
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_db_path()
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as connection:
            row = connection.execute('SELECT value FROM futures_risk_state WHERE key=?',
                                     ('identity_salt',)).fetchone()
        if row is None or not isinstance(row[0], bytes) or len(row[0]) != 32:
            raise ValueError('Risk identity state unavailable')
        self._salt = row[0]

    def account_id(self, venue, identifier):
        digest = hmac.new(self._salt, (venue + ':' + identifier).encode(), hashlib.sha256).hexdigest()
        return 'acct_' + digest[:32]


class RiskStore(RiskKeys):
    """Create only futures_risk_* schema; atomic inserts, stable per-DB HMAC IDs.

    This salt is internal dataset identity state, independent of client salts or
    signing credentials. Losing the DB loses its identities. Binance identity is
    keyed by the configured API identity, so API-key rotation starts a new series.
    """
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # Preserve permissions on a shared existing portfolio DB.
        else:
            os.close(descriptor)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT OR IGNORE INTO futures_risk_state VALUES (?, ?)',
                               ('identity_salt', secrets.token_bytes(32)))
            self._salt = connection.execute('SELECT value FROM futures_risk_state WHERE key=?',
                                             ('identity_salt',)).fetchone()[0]
            if not isinstance(self._salt, bytes) or len(self._salt) != 32:
                raise ValueError('Risk identity state invalid')
            connection.commit()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute('PRAGMA busy_timeout=10000')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
            yield connection
        finally:
            connection.close()

    def ledger_account_id(self, reference):
        """Bind NAV to existing ledger state; persist only the opaque identity."""
        from portfolio_export import _Privacy

        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            try:
                row = conn.execute("SELECT value FROM ledger_state WHERE key='account_salt'").fetchone()
                if row is None or not isinstance(row[0], bytes) or len(row[0]) != 32:
                    return None
                identity = _Privacy(row[0], []).opaque(reference)
                alias = conn.execute('SELECT canonical_identity FROM ledger_account_aliases WHERE alias_identity=?',
                                     (identity,)).fetchone()
                return alias[0] if alias else identity
            except sqlite3.Error:
                return None

    def append(self, snapshot):
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            header = {key: value for key, value in snapshot.items() if key not in ('accounts', 'positions', 'venues')}
            connection.execute('INSERT INTO futures_risk_snapshots VALUES (?, ?, ?, ?, ?, ?)',
                (snapshot['snapshot_id'], snapshot['started_at'], snapshot['completed_at'],
                 CALCULATION_VERSION, snapshot['source_status'], _json(header)))
            for row in snapshot['venues']:
                connection.execute('INSERT INTO futures_risk_venues VALUES (?, ?, ?, ?)',
                    (snapshot['snapshot_id'], row['venue'], row['source_status'], _json(row)))
            for ordinal, row in enumerate(snapshot['accounts']):
                connection.execute('INSERT INTO futures_risk_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (snapshot['snapshot_id'], ordinal, row['venue'], row['account_id'], row['scope'],
                     row['dex'], row['observed_at'], _json(row)))
            for ordinal, row in enumerate(snapshot['positions']):
                connection.execute('INSERT INTO futures_risk_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (snapshot['snapshot_id'], ordinal, row['venue'], row['account_id'], row['dex'],
                     row['symbol'], row['observed_at'], _json(row)))
            connection.commit()


def _metric(value=None, *, inputs=None, formula=None, reason='SOURCE_FIELD_UNAVAILABLE', scope=None):
    number = _number(value)
    return {'value': _amount(number), 'status': 'unavailable' if number is None else 'derived' if formula else 'reported',
            'reason_codes': [reason] if number is None else [], 'inputs': inputs or {}, 'formula': formula,
            **({'scope': scope} if scope else {})}


def _reported(row, field, **kwargs):
    return _metric(row.get(field), inputs={field: _amount(_number(row.get(field)))}, **kwargs)


def _nav_validation(validator, *args):
    try:
        validator(*args)
    except CryptoValidationError as exc:
        return [exc.code]
    return []


def _sum(values, *, absolute=False):
    numbers = [_number(value) for value in values]
    if any(value is None for value in numbers):
        return None
    return sum((abs(value) if absolute else value for value in numbers), Decimal(0))


def _ratio(gross, equity):
    numerator, denominator = _number(gross), _number(equity)
    return _metric(numerator / denominator if numerator is not None and denominator is not None and denominator > 0 else None,
                   inputs={'gross_notional': _amount(numerator), 'equity': _amount(denominator)},
                   formula='gross_notional / equity', reason='POSITIVE_EQUITY_AND_COMPLETE_NOTIONAL_REQUIRED')


def _quality(row, reasons=()):
    missing = {reason for value in row.values() if isinstance(value, dict)
               for reason in value.get('reason_codes', [])}
    row['reason_codes'] = sorted(missing | set(reasons))
    row['data_status'] = 'partial' if row['reason_codes'] else 'complete'
    return row


def _position(raw, venue, account_id, dex, observed_at):
    hl = venue == 'hyperliquid'
    size_field, mark_field, pnl_field = ('szi', 'markPx', 'unrealizedPnl') if hl else ('positionAmt', 'markPrice', 'unRealizedProfit')
    size = _number(raw.get(size_field))
    if size == 0:
        return None
    symbol = _text(raw.get('coin' if hl else 'symbol'))
    side = 'long' if size is not None and size > 0 else 'short' if size is not None and size < 0 else None
    notional_field = 'positionValue' if hl else 'notional'
    notional = _number(raw.get(notional_field))
    gross = abs(notional) if notional is not None else None
    signed_notional = gross if side == 'long' else -gross if side == 'short' and gross is not None else None
    mark = _reported(raw, mark_field)
    if hl and mark['value'] is None and gross is not None and size is not None and size != 0:
        mark = _metric(gross / abs(size), inputs={'positionValue': _amount(notional), 'szi': _amount(size)},
                       formula='abs(positionValue) / abs(szi)')
    liq_field = 'liquidationPx' if hl else 'liquidationPrice'
    liq = _number(raw.get(liq_field))
    liq = liq if liq is not None and liq > 0 else None
    mark_number = _number(mark['value'])
    distance = None
    if liq is not None and mark_number is not None and mark_number > 0 and side:
        distance = (mark_number - liq if side == 'long' else liq - mark_number) / mark_number * 100
    leverage = raw.get('leverage') if hl else {}
    leverage = leverage if isinstance(leverage, dict) else {}
    mode = leverage.get('type') if hl else raw.get('marginType')
    if mode == 'isolated':
        mode = 'isolated'
    elif mode in ('cross', 'crossed'):
        mode = 'cross'
    elif not hl and isinstance(raw.get('isolated'), bool):
        mode = 'isolated' if raw['isolated'] else 'cross'
    else:
        mode = None
    return _quality({'venue': venue, 'account_id': account_id, 'dex': dex,
        'observed_at': observed_at, 'source_status': 'complete', 'symbol': symbol, 'side': side,
        'position_side': 'ONE_WAY' if hl else _text(raw.get('positionSide')),
        'margin_mode': mode, 'size': _reported(raw, size_field),
        'margin_mode_inputs': {'leverage.type': leverage.get('type') if leverage.get('type') in ('cross', 'isolated') else None}
            if hl else {'marginType': raw.get('marginType') if raw.get('marginType') in ('cross', 'crossed', 'isolated') else None,
                        'isolated': raw.get('isolated') if isinstance(raw.get('isolated'), bool) else None},
        'configured_leverage': _reported(leverage, 'value') if hl else _reported(raw, 'leverage'),
        'mark_price': mark, 'liquidation_price': _metric(liq, inputs={liq_field: _amount(_number(raw.get(liq_field)))},
                                                     reason='LIQUIDATION_PRICE_UNAVAILABLE'),
        'liquidation_distance_pct': _metric(distance,
            inputs={'mark_price': mark['value'], 'liquidation_price': _amount(liq), 'side': side},
            formula='(mark - liquidation) / mark * 100 for long; (liquidation - mark) / mark * 100 for short',
            reason='LIQUIDATION_DISTANCE_INPUT_UNAVAILABLE'),
        'gross_notional': _metric(gross, inputs={notional_field: _amount(notional)}, formula=f'abs({notional_field})'),
        'net_notional': _metric(signed_notional,
            inputs={notional_field: _amount(notional), size_field: _amount(size)}, formula='side_sign * abs(notional)'),
        'unrealized_pnl': _reported(raw, pnl_field),
        'margin_used': _reported(raw, 'marginUsed' if hl else 'positionInitialMargin', scope='open_position'),
        'maintenance_margin': _metric(reason='HYPERLIQUID_POSITION_MAINTENANCE_UNAVAILABLE') if hl else _reported(raw, 'maintMargin')},
        [reason for condition, reason in [(symbol is None, 'POSITION_SYMBOL_UNAVAILABLE'),
             (side is None, 'POSITION_SIDE_UNAVAILABLE'), (mode is None, 'MARGIN_MODE_UNAVAILABLE'),
             (not hl and _text(raw.get('positionSide')) is None, 'POSITION_SIDE_MODE_UNAVAILABLE')]
         if condition])


def _notionals(positions, complete=True):
    inputs = [row['gross_notional']['value'] for row in positions]
    signed = [row['net_notional']['value'] for row in positions]
    return {'gross_notional': _metric(_sum(inputs) if complete else None,
                inputs={'position_gross_notionals': inputs, 'coverage_complete': complete}, formula='sum(abs(position notional))',
                reason='POSITION_NOTIONAL_COVERAGE_INCOMPLETE'),
            'net_notional': _metric(_sum(signed) if complete else None,
                inputs={'position_signed_notionals': signed, 'coverage_complete': complete}, formula='sum(signed position notional)',
                reason='POSITION_NOTIONAL_COVERAGE_INCOMPLETE')}


class RiskCollector:
    """Guarded read-only clients, including the approved coherent NAV reads.

    Factories are lazy. Importing this module does not load environment variables
    or credentials. Tests inject clients and an explicit DB path. Unknown source
    fields degrade legacy data_status; only legacy reads determine exit_code.
    Supplemental NAV failures remain in nav_evidence.
    """
    def __init__(self, store, *, binance_factory=BinanceAccountClient.from_environment,
                 hyperliquid_factory=HyperliquidAccountClient.from_environment,
                 clock=lambda: datetime.now(timezone.utc)):
        self.store, self.factories, self.clock = store, {'binance': binance_factory, 'hyperliquid': hyperliquid_factory}, clock

    def capture(self):
        context = {}
        result = self._capture_legacy(context)
        # All legacy reads, rows and status/exit calculations are complete.
        # NAV owns a separate payload and cannot mutate those facts.
        try:
            result['nav_evidence'] = json.loads(_json(self._capture_nav(result, context)))
        except Exception:
            result['nav_evidence'] = {'version': 'crypto_risk_nav_v1', 'accounts': [],
                'venues': [{'venue': venue, 'source_status': 'failed',
                    'reason_codes': ['CRYPTO_NAV_COLLECTION_FAILED'], 'reads': []}
                    for venue in self.factories]}
        self.store.append(result)
        return result

    def _capture_legacy(self, context):
        result = {'snapshot_id': uuid4().hex, 'calculation_version': CALCULATION_VERSION,
                  'started_at': _utc(self.clock()), 'accounts': [], 'positions': [], 'venues': []}
        for venue, factory in self.factories.items():
            outcome = {'venue': venue, 'source_status': 'complete', 'reason_codes': [], 'reads': [], '_nav_inputs': []}
            try:
                client = factory()
                context[venue] = client
                if venue == 'binance':
                    self._binance(client, result, outcome)
                else:
                    self._hyperliquid(client, result, outcome)
            except Exception:
                # Never persist exception text, provider payloads, identifiers or credential paths.
                outcome['reason_codes'].append('VENUE_COLLECTION_FAILED')
            context[venue + '_inputs'] = outcome.pop('_nav_inputs')
            outcome['reason_codes'] = sorted(set(outcome['reason_codes']))
            if outcome['reason_codes']:
                outcome['source_status'] = 'partial' if any(read['source_status'] == 'complete' for read in outcome['reads']) else 'failed'
            result['venues'].append(outcome)
        result['completed_at'] = _utc(self.clock())
        result['source_status'] = 'failed' if all(row['source_status'] == 'failed' for row in result['venues']) else (
            'partial' if any(row['source_status'] != 'complete' for row in result['venues']) else 'complete')
        result['reason_codes'] = sorted({reason for row in result['venues'] for reason in row['reason_codes']})
        result['data_status'] = 'partial' if result['reason_codes'] or any(row['data_status'] != 'complete'
            for row in result['accounts'] + result['positions']) else 'complete'
        result['exit_code'] = int(result['source_status'] != 'complete')
        return result

    def _read(self, call, outcome, source, *, account_id=None, dex=None):
        read = {'source': source, 'account_id': account_id, 'dex': dex,
                'started_at': _utc(self.clock()), 'source_status': 'complete', 'reason_codes': []}
        try:
            value = call()
        except Exception as exc:
            # FuturesError codes are fixed by the client, not interpolated wire text.
            code = exc.code if isinstance(exc, FuturesError) and re.fullmatch('[A-Z_]+', exc.code) else 'SOURCE_READ_FAILED'
            if '_nav_inputs' in outcome and code == 'FUTURES_INVALID_RESPONSE':
                # b8b19ff classified malformed JSON as a transport failure.
                # Preserve that legacy channel; strict NAV keeps its own code.
                code = 'FUTURES_TRANSPORT_ERROR'
            read['source_status'], read['reason_codes'] = 'failed', [code]
            outcome['reason_codes'].append(code)
            value = None
        read['completed_at'] = _utc(self.clock())
        outcome['reads'].append(read)
        if '_nav_inputs' in outcome:
            outcome['_nav_inputs'].append((read, value))
        return value

    def _binance(self, client, result, outcome):
        identity = self.store.account_id('binance', client._api_key)
        raw_positions = self._read(lambda: client.request('GET', '/fapi/v3/positionRisk'), outcome,
                                   'positionRisk', account_id=identity)
        balances = self._read(lambda: client.request('GET', '/fapi/v3/balance'), outcome,
                              'balance', account_id=identity)
        complete = isinstance(raw_positions, list) and all(isinstance(row, dict) for row in raw_positions)
        if not complete:
            outcome['reason_codes'].append('BINANCE_POSITIONS_UNAVAILABLE')
            raw_positions = []
        if not isinstance(balances, list) or not all(isinstance(row, dict) for row in balances):
            outcome['reason_codes'].append('BINANCE_BALANCE_UNAVAILABLE')
            balances = []
        timestamp = _utc(self.clock())
        grouped, seen_positions = {}, set()
        for raw in raw_positions:
            row = _position(raw, 'binance', identity, '', timestamp)
            if row is None:
                continue
            asset = _text(raw.get('marginAsset'))
            row['asset'] = asset
            position_key = (asset, row['symbol'], row['position_side'])
            if position_key in seen_positions:
                complete = False
                outcome['reason_codes'].append('BINANCE_DUPLICATE_POSITION')
                continue
            seen_positions.add(position_key)
            if asset is None:
                complete = False
                outcome['reason_codes'].append('BINANCE_POSITION_COLLATERAL_UNAVAILABLE')
            grouped.setdefault(asset, []).append(row)
            result['positions'].append(row)
        seen = set()
        for raw in balances:
            asset = _text(raw.get('asset'))
            if asset is None or asset in seen:
                outcome['reason_codes'].append('BINANCE_BALANCE_INVALID')
                continue
            seen.add(asset)
            positions = grouped.get(asset, [])
            pnls = [row['unrealized_pnl']['value'] for row in positions]
            wallet, pnl = _number(raw.get('balance')), _sum(pnls) if complete else None
            equity = wallet + pnl if wallet is not None and pnl is not None else None
            row = {'venue': 'binance', 'account_id': identity, 'dex': '', 'asset': asset,
                'scope': 'futures_collateral', 'observed_at': timestamp, 'source_status': 'complete' if complete else 'partial',
                'wallet_balance': _reported(raw, 'balance'),
                'equity': _metric(equity, inputs={'wallet_balance': _amount(wallet), 'position_unrealized_pnl': pnls},
                                  formula='wallet_balance + sum(all position unrealized_pnl)', reason='EQUITY_INPUT_UNAVAILABLE'),
                'unrealized_pnl': _metric(pnl, inputs={'position_unrealized_pnl': pnls, 'coverage_complete': complete},
                                          formula='sum(all position unrealized_pnl)'),
                'margin_used': _metric(reason='BINANCE_ACCOUNT_SUMMARY_NOT_ALLOWED'),
                'position_margin_used': _metric(_sum([p['margin_used']['value'] for p in positions]) if complete else None,
                    inputs={'position_initial_margins': [p['margin_used']['value'] for p in positions], 'coverage_complete': complete},
                    formula='sum(positionInitialMargin)', scope='open_positions_excluding_orders'),
                'maintenance_margin': _metric(_sum([p['maintenance_margin']['value'] for p in positions]) if complete else None,
                    inputs={'position_maintenance_margins': [p['maintenance_margin']['value'] for p in positions], 'coverage_complete': complete},
                    formula='sum(maintMargin)'), **_notionals(positions, complete)}
            row['effective_leverage'] = _ratio(row['gross_notional']['value'], row['equity']['value'])
            result['accounts'].append(_quality(row))
        if set(grouped) - seen:
            outcome['reason_codes'].append('BINANCE_POSITION_WALLET_UNAVAILABLE')

    def _capture_nav(self, legacy, context):
        nav = {'version': 'crypto_risk_nav_v1', 'accounts': [], 'venues': []}
        for prior in legacy['venues']:
            venue = prior['venue']
            outcome = {'venue': venue, 'source_status': 'complete', 'reason_codes': [], 'reads': []}
            own = {'accounts': copy.deepcopy([r for r in legacy['accounts'] if r['venue'] == venue])}
            try:
                if venue == 'binance':
                    client = context[venue].inference_client()
                    for row in own['accounts']:
                        row['nav_eligible'] = False
                    self._binance_nav(client, own, outcome,
                        self.store.account_id(venue, client._api_key), self.store.ledger_account_id('binance:primary'))
                else:
                    # HL needs no extra HTTP: validate its captured legacy
                    # responses only after every legacy account has completed.
                    outcome['reason_codes'] = list(prior['reason_codes'])
                    self._hyperliquid_nav(own, outcome, context.get(venue + '_inputs', []), context.get(venue))
            except Exception:
                own['accounts'] = []
                outcome['reason_codes'].append('CRYPTO_NAV_COLLECTION_FAILED')
            outcome['reason_codes'] = sorted(set(outcome['reason_codes'] + outcome.get('nav_reason_codes', [])))
            outcome['source_status'] = 'partial' if outcome['reason_codes'] else 'complete'
            outcome['nav_source_status'] = outcome['source_status']
            outcome['nav_reason_codes'] = list(outcome['reason_codes'])
            nav['venues'].append(outcome)
            nav['accounts'].extend(own['accounts'])
        return nav

    def _hyperliquid_nav(self, result, outcome, inputs, client):
        captured = {(read['account_id'], read['source'], read['dex'] or ''): (read, value)
                    for read, value in inputs}
        outcome['reads'] = [dict(read, reused_legacy_response=True) for read, _ in inputs]
        identities = {self.store.account_id('hyperliquid', address):
                      self.store.ledger_account_id('hyperliquid:' + address.lower())
                      for address in client._addresses}
        for row in result['accounts']:
            shared = row['scope'] == 'unified_shared_wallet'
            source = 'spotClearinghouseState' if shared else 'clearinghouseState'
            read, payload = captured.get((row['account_id'], source, row['dex']), ({}, None))
            reasons = _nav_validation(hyperliquid_spot if shared else hyperliquid_state,
                                      *([payload] if shared else [payload, row['dex']]))
            if shared:
                balances = payload['balances'] if not reasons else []
                usdc = next((r for r in balances if r['token'] == 0), {'total': '0'})
                if any(r['token'] != 0 and _number(r['total']) != 0 for r in balances):
                    reasons.append('UNIFIED_OTHER_ASSET_VALUATION_UNAVAILABLE')
                value = usdc['total'] if not reasons else None
                metric = _metric(value, inputs={'spot_USDC_total': _amount(_number(value)), 'token': 0},
                    formula='spotClearinghouseState USDC total once; no dex accountValue additions',
                    reason=reasons[0] if reasons else 'HYPERLIQUID_SHARED_WALLET_UNAVAILABLE')
                eligible, price_time, basis = True, read.get('completed_at'), 'current_state_read_completion'
            else:
                summary = payload.get('marginSummary', {}) if isinstance(payload, dict) else {}
                empty_builder = bool(row['dex'] and not reasons and not payload['assetPositions']
                    and _number(summary.get('accountValue')) == 0 and _number(summary.get('totalRawUsd')) == 0)
                value = row['equity']['value'] if not reasons else None
                metric = _metric(value, inputs={'accountValue': _amount(_number(summary.get('accountValue')))},
                    reason=reasons[0] if reasons else 'HYPERLIQUID_EQUITY_UNAVAILABLE')
                eligible = row.get('abstraction') != 'unifiedAccount' and not empty_builder
                price_time = datetime.fromtimestamp(payload['time'] / 1000, timezone.utc).isoformat() if not reasons else None
                basis = 'clearinghouseState.time'
            row.update(ledger_account_identity=identities[row['account_id']], nav_eligible=eligible,
                nav_equity=metric, nav_reason_codes=reasons, nav_price_time=price_time, nav_price_time_basis=basis)
            if eligible:
                outcome['reason_codes'].extend(reasons)

    def _binance_nav(self, client, result, outcome, identity, ledger_identity):
        first = len(outcome['reads'])
        before = self._read(lambda: client.account_config(), outcome, 'accountConfig_before', account_id=identity)
        account = self._read(lambda: client.account_information(), outcome, 'account', account_id=identity)
        after = self._read(lambda: client.account_config(), outcome, 'accountConfig_after', account_id=identity)
        reads = outcome['reads'][first:]
        reasons = [code for read in reads for code in read['reason_codes']]
        reasons.extend(_nav_validation(binance_account_nav, before, account, after))
        times = [datetime.fromisoformat(read[key].replace('Z', '+00:00'))
                 for read in reads for key in ('started_at', 'completed_at')]
        bounded = all(a <= b for a, b in zip(times, times[1:])) and (times[-1] - times[0]).total_seconds() <= 60
        if not bounded:
            reasons.append('CRYPTO_BINANCE_ACCOUNT_READ_WINDOW_EXCEEDED')
        reasons = sorted(set(reasons))
        mode_known = bounded and before is not None and after is not None and all(
            before[key] == after[key] for key in ('multiAssetsMargin', 'dualSidePosition'))
        account_mode = ('multi_assets' if before['multiAssetsMargin'] else 'single_asset') if mode_known else 'unknown'
        outcome.update(nav_source_status='partial' if reasons else 'complete', nav_reason_codes=reasons)
        account_rows = [r for r in result['accounts'] if r['venue'] == 'binance' and r['account_id'] == identity]
        by_asset = {r['asset']: r for r in account_rows}
        assets = account['assets'] if account is not None else []
        # A failed account read never falls back to the independently sampled
        # balance/positionRisk equity. Retain diagnostic rows when available.
        if not assets:
            assets = [{'asset': r['asset']} for r in account_rows]
        for raw in assets:
            asset = raw['asset']
            row = by_asset.get(asset)
            if row is None:
                row = {'venue': 'binance', 'account_id': identity, 'dex': '', 'asset': asset,
                       'scope': 'futures_collateral', 'observed_at': reads[1]['completed_at'],
                       'source_status': 'partial', 'equity': _metric(reason='LEGACY_RISK_INPUT_UNAVAILABLE')}
                result['accounts'].append(row)
            wallet, pnl = _number(raw.get('walletBalance')), _number(raw.get('unrealizedProfit'))
            value = wallet + pnl if not reasons and wallet is not None and pnl is not None else None
            row.update(ledger_account_identity=ledger_identity, nav_eligible=True,
                nav_source_status=outcome['nav_source_status'],
                nav_account_mode=account_mode,
                nav_equity=_metric(value, inputs={'walletBalance': _amount(wallet), 'unrealizedProfit': _amount(pnl),
                    'source': '/fapi/v3/account', 'mode_before_at': reads[0]['completed_at'],
                    'account_read_at': reads[1]['completed_at'], 'mode_after_at': reads[2]['completed_at']},
                    formula='walletBalance + unrealizedProfit (one account response)',
                    reason=reasons[0] if reasons else 'EQUITY_INPUT_UNAVAILABLE'),
                nav_reason_codes=reasons, nav_price_time=reads[1]['completed_at'],
                nav_price_time_basis='coherent_account_read_completion')
            _quality(row)

    def _hyperliquid(self, client, result, outcome):
        reads = 0
        def read(kind, address, **params):
            nonlocal reads
            if reads >= min(client.max_pages, 40):
                outcome['reason_codes'].append('PAGE_BUDGET')
                return None
            reads += 1
            return self._read(lambda: client.info(kind, address, **params), outcome, kind,
                              account_id=self.store.account_id('hyperliquid', address), dex=params.get('dex'))

        for address in client._addresses:
            if reads >= min(client.max_pages, 40):
                outcome['reason_codes'].append('PAGE_BUDGET')
                break
            identity = self.store.account_id('hyperliquid', address)
            mode = read('userAbstraction', address)
            supported = mode in ('default', 'disabled', 'unifiedAccount')
            if not supported:
                outcome['reason_codes'].append('HYPERLIQUID_ABSTRACTION_UNAVAILABLE_OR_UNSUPPORTED')
            discovered = read('perpDexs', address)
            dexes = ['']
            complete = isinstance(discovered, list)
            if complete:
                if len(discovered) > HYPERLIQUID_MAX_NAMED_DEXES + 1:
                    complete = False
                for item in discovered[:HYPERLIQUID_MAX_NAMED_DEXES + 1]:
                    if item is None:
                        continue
                    name = item.get('name') if isinstance(item, dict) else None
                    if not isinstance(name, str) or not name or not _hyperliquid_body_allowed(
                            {'type': 'clearinghouseState', 'user': address, 'dex': name}, client._addresses):
                        complete = False
                    elif name not in dexes:
                        if len(dexes) > HYPERLIQUID_MAX_NAMED_DEXES:
                            complete = False
                        else:
                            dexes.append(name)
            if not complete:
                outcome['reason_codes'].append('HYPERLIQUID_DEX_DISCOVERY_INCOMPLETE')
            spot = read('spotClearinghouseState', address) if mode == 'unifiedAccount' else None
            account_rows, all_positions = [], []
            for dex in dexes:
                payload = read('clearinghouseState', address, dex=dex)
                if not isinstance(payload, dict) or not isinstance(payload.get('assetPositions'), list):
                    outcome['reason_codes'].append('HYPERLIQUID_DEX_READ_FAILED')
                    complete = False
                    continue
                observed_at = _utc(self.clock())
                positions, valid = [], True
                seen = set()
                for item in payload['assetPositions']:
                    raw = item.get('position') if isinstance(item, dict) else None
                    if not isinstance(raw, dict):
                        valid = False
                        continue
                    row = _position(raw, 'hyperliquid', identity, dex, observed_at)
                    if row:
                        if row['symbol'] is None or row['symbol'] in seen:
                            valid = False
                        seen.add(row['symbol'])
                        row['asset'] = 'USDC' if dex == '' else None
                        if dex:
                            _quality(row, row['reason_codes'] + ['HYPERLIQUID_BUILDER_COLLATERAL_UNVERIFIED'])
                        positions.append(row)
                if not valid:
                    complete = False
                    outcome['reason_codes'].append('HYPERLIQUID_POSITIONS_INVALID')
                summary = payload.get('marginSummary')
                summary = summary if isinstance(summary, dict) else {}
                eligible = supported and mode != 'unifiedAccount'
                equity = _number(summary.get('accountValue')) if eligible else None
                pnls = [p['unrealized_pnl']['value'] for p in positions]
                row = {'venue': 'hyperliquid', 'account_id': identity, 'dex': dex, 'scope': 'perp_dex',
                    'asset': 'USDC' if dex == '' else None, 'collateral_status': 'known' if dex == '' else 'unverified',
                    'abstraction': mode if supported else None, 'observed_at': observed_at,
                    'source_time_ms': payload.get('time') if isinstance(payload.get('time'), int) and not isinstance(payload.get('time'), bool) else None,
                    'source_status': 'complete' if valid else 'partial',
                    'equity': _metric(equity, inputs={'accountValue': _amount(_number(summary.get('accountValue')))},
                        reason='UNIFIED_DEX_EQUITY_NON_AGGREGABLE' if mode == 'unifiedAccount' else 'HYPERLIQUID_EQUITY_UNAVAILABLE'),
                    'wallet_balance': _reported(summary, 'totalRawUsd'),
                    'margin_used': _reported(summary, 'totalMarginUsed'),
                    'cross_maintenance_margin': _reported(payload, 'crossMaintenanceMarginUsed', scope='cross_positions_only'),
                    'maintenance_margin': _reported(payload, 'crossMaintenanceMarginUsed')
                        if valid and all(p['margin_mode'] == 'cross' for p in positions)
                        else _metric(reason='HYPERLIQUID_ISOLATED_MAINTENANCE_UNAVAILABLE'),
                    'unrealized_pnl': _metric(_sum(pnls) if valid else None,
                        inputs={'position_unrealized_pnl': pnls, 'coverage_complete': valid}, formula='sum(position unrealizedPnl)'),
                    **_notionals(positions, valid)}
                row['effective_leverage'] = _ratio(row['gross_notional']['value'], row['equity']['value'])
                account_rows.append(_quality(row, [] if dex == '' else ['HYPERLIQUID_BUILDER_COLLATERAL_UNVERIFIED']))
                all_positions.extend(positions)
                result['positions'].extend(positions)
            result['accounts'].extend(account_rows)
            if mode == 'unifiedAccount':
                rows = spot.get('balances') if isinstance(spot, dict) else None
                usdc = [row for row in rows if isinstance(row, dict) and row.get('coin') == 'USDC'] if isinstance(rows, list) else []
                wallet = usdc[0] if len(usdc) == 1 else {}
                # An explicit valid empty balance list proves zero USDC, not unknown zero.
                if rows == []:
                    wallet = {'total': '0', 'hold': '0'}
                if not isinstance(rows, list) or _number(wallet.get('total')) is None:
                    outcome['reason_codes'].append('HYPERLIQUID_SHARED_WALLET_UNAVAILABLE')
                row = {'venue': 'hyperliquid', 'account_id': identity, 'dex': '', 'asset': 'USDC',
                    'scope': 'unified_shared_wallet', 'abstraction': mode, 'observed_at': _utc(self.clock()),
                    'source_status': 'complete' if complete else 'partial',
                    'wallet_balance': _metric(wallet.get('total'), inputs={'total': _amount(_number(wallet.get('total'))),
                        'hold': _amount(_number(wallet.get('hold')))}, scope='spot_USDC_shared_wallet'),
                    'wallet_hold': _reported(wallet, 'hold'),
                    'equity': _metric(reason='UNIFIED_EQUITY_UNVERIFIED'),
                    'margin_used': _metric(reason='UNIFIED_MARGIN_AGGREGATION_UNVERIFIED'),
                    'maintenance_margin': _metric(reason='UNIFIED_MAINTENANCE_AGGREGATION_UNVERIFIED'),
                    'unrealized_pnl': _metric(reason='ALL_DEX_COLLATERAL_COMPATIBILITY_UNVERIFIED'),
                    **_notionals(all_positions, complete and len(dexes) == 1)}
                row['effective_leverage'] = _ratio(row['gross_notional']['value'], None)
                result['accounts'].append(_quality(row))
