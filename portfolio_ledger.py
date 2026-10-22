"""Permanent, private, append-only event ledger. No valuation or return calculation.

Ledger tables and the isolated crypto_flow_windows table are created here.
Missing facts stay null. Compatible additional facts and transfer
classifications are append-only revisions, never event edits.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from portfolio_export import _Privacy
from portfolio_store import default_db_path

_REQUEST_DEADLINE = ContextVar("ledger_request_deadline", default=None)

KINDS = {"fill", "fee", "funding", "realized_pnl", "income_other", "transfer", "transfer_internal",
         "transfer_external_in", "transfer_external_out", "deposit", "withdrawal", "corporate_action"}
FIELDS = ("source", "account_identity", "source_record_id", "kind", "symbol", "execution_time_utc",
          "activity_date", "source_timezone", "timestamp_precision", "side", "quantity", "price_native",
          "gross_amount", "amount", "currency", "fee", "fee_asset", "tax", "tax_asset", "fx_rate",
          "realized_pnl", "pnl_asset", "income_type", "identity_quality", "transfer_route",
          "origin_account_identity", "destination_account_identity", "native_record_id", "parent_order_id",
          "record_kind", "source_status", "settle_date", "market", "exchange", "position_side",
          "direction", "venue", "wallet", "network", "transaction_ref", "amount_precision", "fee_basis", "chain_address_identity")
NUMERIC = {"quantity", "price_native", "gross_amount", "amount", "fee", "tax", "fx_rate", "realized_pnl"}
MINOR_UNITS = {"KRW": 0, "USD": 2}
ANNOTATIONS = {"source_timezone", "timestamp_precision", "identity_quality"}
FLOW_KINDS = {'deposit', 'withdrawal', 'transfer', 'transfer_internal', 'transfer_external_in', 'transfer_external_out'}
FLOW_TIME_SQL = "coalesce(julianday(execution_time_utc), julianday(json_extract(payload_json,'$.activity_date') || 'T00:00:00+09:00'))"
CONFLICT_TIME_SQL = "coalesce(julianday(json_extract(candidate_json,'$.execution_time_utc')), julianday(json_extract(candidate_json,'$.activity_date') || 'T00:00:00+09:00'))"
ENRICHMENT_TIME_SQL = "coalesce(julianday(json_extract(fields_json,'$.execution_time_utc')), julianday(json_extract(fields_json,'$.activity_date') || 'T00:00:00+09:00'))"
_FLOW_STREAM_LEAVES = ('cashflows', 'deposit', 'withdraw', 'transfer_MAIN_UMFUTURE', 'transfer_UMFUTURE_MAIN',
                       'transfer_MAIN_FUNDING', 'transfer_FUNDING_MAIN', 'transfer_FUNDING_UMFUTURE',
                       'transfer_UMFUTURE_FUNDING', 'nonFundingLedger', 'userNonFundingLedgerUpdates')
FLOW_STREAM_SQL = 'CASE ' + ' '.join(f"WHEN stream GLOB '*/{leaf}' THEN '{leaf}'" for leaf in _FLOW_STREAM_LEAVES) + ' END'


def coverage_time_sql(column):
    return f"CASE WHEN {column} != '' AND {column} NOT GLOB '*[^0-9]*' THEN CAST({column} AS REAL)/86400000.0+2440587.5 ELSE julianday({column}) END"


_READ_INDEX_SCHEMA = f"""
CREATE INDEX IF NOT EXISTS ledger_flow_window ON ledger_events(kind, {FLOW_TIME_SQL}, source);
CREATE INDEX IF NOT EXISTS ledger_flow_scope ON ledger_events(source, kind);
CREATE INDEX IF NOT EXISTS ledger_conflict_window ON ledger_conflicts({CONFLICT_TIME_SQL}, event_id);
CREATE INDEX IF NOT EXISTS ledger_enrichment_window ON ledger_enrichments({ENRICHMENT_TIME_SQL}, event_id);
CREATE INDEX IF NOT EXISTS ledger_flow_coverage_window ON ledger_coverage(source, {coverage_time_sql('end_time')}, {coverage_time_sql('start_time')});
CREATE INDEX IF NOT EXISTS ledger_first_flow_coverage ON ledger_coverage(source, account_identity, {FLOW_STREAM_SQL}, {coverage_time_sql('start_time')}) WHERE status='complete' AND reason_codes_json='[]';
CREATE INDEX IF NOT EXISTS ledger_account_alias_target ON ledger_account_aliases(canonical_identity);
"""

# Flow-window surface: independent strict observations, never legacy events.
# Install on the existing writer initialization path, including older databases.
CRYPTO_FLOW_WINDOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_flow_windows(
 window_id TEXT PRIMARY KEY NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('binance','hyperliquid')),
 account_identity TEXT NOT NULL,
 stream TEXT NOT NULL,
 start_time TEXT NOT NULL,
 end_time TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
 native_keys_json TEXT NOT NULL CHECK(json_valid(native_keys_json)),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 CHECK(start_time <= end_time),
 CHECK(length(CAST(payload_json AS BLOB)) + length(CAST(native_keys_json AS BLOB)) <= 67108864));
CREATE INDEX IF NOT EXISTS crypto_flow_windows_time
 ON crypto_flow_windows(end_time,start_time);
CREATE INDEX IF NOT EXISTS crypto_flow_windows_scope
 ON crypto_flow_windows(source,account_identity,end_time,start_time);
CREATE TRIGGER IF NOT EXISTS crypto_flow_windows_no_replace
 BEFORE INSERT ON crypto_flow_windows
 WHEN EXISTS(SELECT 1 FROM crypto_flow_windows WHERE window_id=NEW.window_id OR rowid=NEW.rowid) BEGIN
 SELECT RAISE(ABORT, 'crypto flow windows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS crypto_flow_windows_no_update
 BEFORE UPDATE ON crypto_flow_windows BEGIN
 SELECT RAISE(ABORT, 'crypto flow windows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS crypto_flow_windows_no_delete
 BEFORE DELETE ON crypto_flow_windows BEGIN
 SELECT RAISE(ABORT, 'crypto flow windows are immutable');
END;
"""


def ensure_crypto_flow_schema(db_path):
    """Install only strict-flow objects in an existing DB, before serving reads.

    No LedgerStore initialization, legacy DDL, identity salt or data writes.
    IF NOT EXISTS deliberately does not replace an already installed trigger.
    """
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True)
    try:
        conn.executescript(CRYPTO_FLOW_WINDOW_SCHEMA)
    finally:
        conn.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_state(key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_observations(
 observation_sha256 TEXT PRIMARY KEY, counts_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_account_aliases(
 alias_identity TEXT PRIMARY KEY, canonical_identity TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_events(
 event_id TEXT PRIMARY KEY, source TEXT NOT NULL, account_identity TEXT NOT NULL,
 source_record_id TEXT NOT NULL, kind TEXT NOT NULL, execution_time_utc TEXT,
 source_timezone TEXT, side TEXT, quantity TEXT, price_native TEXT, currency TEXT,
 fee TEXT, fee_asset TEXT, tax TEXT, tax_asset TEXT, fx_rate TEXT,
 payload_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL,
 UNIQUE(source, account_identity, source_record_id));
CREATE TABLE IF NOT EXISTS ledger_enrichments(
 event_id TEXT NOT NULL REFERENCES ledger_events(event_id), content_sha256 TEXT NOT NULL,
 fields_json TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(event_id, content_sha256));
CREATE TABLE IF NOT EXISTS ledger_status_revisions(
 event_id TEXT NOT NULL REFERENCES ledger_events(event_id), revision INTEGER NOT NULL,
 reported_status TEXT NOT NULL, effective_status TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(event_id, revision));
CREATE TABLE IF NOT EXISTS ledger_conflicts(
 event_id TEXT NOT NULL REFERENCES ledger_events(event_id), content_sha256 TEXT NOT NULL,
 candidate_json TEXT NOT NULL, differing_fields_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(event_id, content_sha256));
CREATE TABLE IF NOT EXISTS ledger_evidence(
 event_id TEXT NOT NULL REFERENCES ledger_events(event_id), file_sha256 TEXT NOT NULL,
 run_id TEXT NOT NULL, page_path TEXT NOT NULL, row_index INTEGER NOT NULL,
 raw_store_ref TEXT NOT NULL, PRIMARY KEY(event_id, run_id, page_path, row_index));
CREATE TABLE IF NOT EXISTS ledger_coverage(
 coverage_id TEXT PRIMARY KEY, source TEXT NOT NULL, account_identity TEXT NOT NULL,
 stream TEXT NOT NULL, start_time TEXT, end_time TEXT, status TEXT NOT NULL
 CHECK(status IN ('complete','partial','limited')), reason_codes_json TEXT NOT NULL,
 row_count INTEGER NOT NULL, verified_zero INTEGER NOT NULL DEFAULT 0,
 run_id TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_statement_revisions(
 import_id TEXT NOT NULL, revision INTEGER NOT NULL, source_undone INTEGER NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(import_id, revision));
CREATE TABLE IF NOT EXISTS ledger_transfer_classifications(
 event_id TEXT NOT NULL REFERENCES ledger_events(event_id), revision INTEGER NOT NULL,
 classification TEXT NOT NULL CHECK(classification IN ('unknown','internal','external_in','external_out')),
 reason_code TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(event_id, revision));
CREATE TABLE IF NOT EXISTS ledger_transfer_matches(
 pair_id TEXT NOT NULL, revision INTEGER NOT NULL,
 out_event_id TEXT NOT NULL REFERENCES ledger_events(event_id),
 in_event_id TEXT NOT NULL REFERENCES ledger_events(event_id),
 rule_version TEXT NOT NULL, evidence_json TEXT NOT NULL,
 classification TEXT NOT NULL CHECK(classification='internal'), observed_at TEXT NOT NULL,
 PRIMARY KEY(pair_id, revision), UNIQUE(pair_id, rule_version),
 UNIQUE(out_event_id, rule_version), UNIQUE(in_event_id, rule_version),
 CHECK(out_event_id != in_event_id));
"""
_EXPECTED_TRIGGERS = set()
for _table in ("state", "observations", "events", "enrichments", "status_revisions", "conflicts", "evidence", "coverage", "transfer_classifications", "transfer_matches", "statement_revisions", "account_aliases"):
    for _action in ("UPDATE", "DELETE"):
        _EXPECTED_TRIGGERS.add(f"ledger_{_table}_no_{_action.lower()}")
        _SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS ledger_{_table}_no_{_action.lower()}
BEFORE {_action} ON ledger_{_table} BEGIN
 SELECT RAISE(ABORT, 'ledger records are immutable');
END;
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            return None
        if n == 0:
            return "0"
        # normalize() rounds at the ambient Decimal precision (28), while the
        # Toss contract allows 30-character amounts. Canonicalize without loss.
        fixed = format(n, 'f')
        return fixed.rstrip('0').rstrip('.') if '.' in fixed else fixed
    except (InvalidOperation, ValueError):
        return None


def instant(value, zone="UTC"):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            when = datetime.fromtimestamp(value / 1000, timezone.utc)
        else:
            value = str(value).replace("Z", "+00:00")
            fraction = re.search(r'\d{2}:\d{2}:\d{2}\.(\d+)', value)
            if fraction and len(fraction[1]) < 6:
                value = value[:fraction.start(1)] + fraction[1].ljust(6, '0') + value[fraction.end(1):]
            when = datetime.fromisoformat(value)
            if when.tzinfo is None:
                if zone != "UTC":
                    from zoneinfo import ZoneInfo
                    when = when.replace(tzinfo=ZoneInfo(zone))
                else:
                    when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, TypeError, OverflowError):
        return None


def text(value):
    return str(value).strip() if value not in (None, "") else None


class LedgerStore:
    def __init__(self, db_path=None, *, busy_timeout=2, deadline=None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.busy_timeout, self.deadline = busy_timeout, deadline
        self._initialized, self._privacy = False, None

    @contextmanager
    def connection(self):
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.db_path.exists():
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        remaining = self.busy_timeout if self.deadline is None else min(self.busy_timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise sqlite3.OperationalError('LEDGER_WRITE_BUDGET_EXHAUSTED')
        conn = sqlite3.connect(self.db_path, timeout=remaining, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if not self._initialized:
                existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ledger_%'")}
                if _EXPECTED_TRIGGERS <= existing:
                    row = conn.execute("SELECT value FROM ledger_state WHERE key='account_salt'").fetchone()
                    if not row or not isinstance(row[0], bytes) or len(row[0]) != 32:
                        raise ValueError('LEDGER_IDENTITY_STATE_INVALID')
                    self._privacy = _Privacy(row[0], [])
                    self._initialized = True
            if not self._initialized:
                # One bounded initialization lock, including trigger installation.
                conn.executescript("BEGIN IMMEDIATE;" + _SCHEMA)
                try:
                    conn.execute("INSERT OR IGNORE INTO ledger_state VALUES ('account_salt',?)", (secrets.token_bytes(32),))
                    salt = conn.execute("SELECT value FROM ledger_state WHERE key='account_salt'").fetchone()[0]
                    if not isinstance(salt, bytes) or len(salt) != 32:
                        raise ValueError('LEDGER_IDENTITY_STATE_INVALID')
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                self._privacy = _Privacy(salt, [])
                self._initialized = True
            if not getattr(self, '_read_indexes_initialized', False):
                # Additive upgrade on the existing writer path, including DBs
                # whose immutable triggers already satisfy schema initialization.
                conn.executescript(_READ_INDEX_SCHEMA + CRYPTO_FLOW_WINDOW_SCHEMA)
                self._read_indexes_initialized = True
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read(self):
        with self.connection() as conn:
            yield conn

    def budget(self, conn):
        remaining = self.busy_timeout if self.deadline is None else min(self.busy_timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise sqlite3.OperationalError('LEDGER_WRITE_BUDGET_EXHAUSTED')
        conn.execute(f'PRAGMA busy_timeout={max(1, int(remaining * 1000))}')

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            self.budget(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                self.budget(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def account_identity(self, reference, conn=None):
        if conn is None:
            with self.read() as connection:
                return self.account_identity(reference, connection)
        identifier = self._privacy.opaque(reference)
        alias = conn.execute("SELECT canonical_identity FROM ledger_account_aliases WHERE alias_identity=?", (identifier,)).fetchone()
        return alias[0] if alias else identifier

    def hl_identity(self, address):
        # Capture legacy aliases are bound privately; no unsalted address hash is
        # put into prospective stream IDs, filenames, database keys or logs.
        with self.read() as conn:
            reference = 'hyperliquid:' + address.lower()
            self.bind_account(conn, 'hyperliquid:account-' + hashlib.sha256(address.lower().encode()).hexdigest()[:16], reference)
            return self.account_identity(reference, conn)

    def bind_account(self, conn, alias, canonical_reference):
        if not alias or alias == canonical_reference:
            return
        alias_id = self._privacy.opaque(alias)
        target = self.account_identity(canonical_reference, conn)
        previous = conn.execute("SELECT canonical_identity FROM ledger_account_aliases WHERE alias_identity=?", (alias_id,)).fetchone()
        if previous:
            if previous[0] != target:
                raise ValueError('ACCOUNT_BINDING_CONFLICT')
            return
        self.budget(conn)
        conn.execute("INSERT OR IGNORE INTO ledger_account_aliases VALUES (?,?,?)", (alias_id, target, datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def event_id(event):
        return "lev_" + digest([event[k] for k in ("source", "account_identity", "source_record_id")])

    def effective_event(self, conn, event_id):
        row = conn.execute("SELECT payload_json FROM ledger_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        for field in FIELDS:
            result.setdefault(field, None)
        for item in conn.execute("SELECT fields_json FROM ledger_enrichments WHERE event_id=? ORDER BY rowid", (event_id,)):
            result.update(json.loads(item[0]))
        status = conn.execute('SELECT effective_status FROM ledger_status_revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1', (event_id,)).fetchone()
        if status:
            result['source_status'] = status[0]
        from ledger_flows import private_identifiers
        return private_identifiers(self, conn, result)

    @staticmethod
    def effective_status(source, previous, reported):
        terminal = {'CONFIRMED', 'COMPLETED', 'FAILED', 'CANCELLED', 'CANCELED', 'SUCCESS'}
        pending = {'PENDING', 'PROCESSING'}
        if source == 'binance_deposit':
            terminal |= {'1', '2', '7'}
            pending |= {'0', '6', '8'}
        elif source == 'binance_withdraw':
            terminal |= {'1', '3', '5', '6'}
            pending |= {'0', '2', '4'}
        if str(previous).upper() in terminal and str(reported).upper() in pending:
            return previous
        return reported

    def status_change(self, conn, event_id, previous, candidate):
        if previous is None or candidate['kind'] not in FLOW_KINDS or candidate['source_status'] is None:
            return None
        last = conn.execute('SELECT reported_status,effective_status FROM ledger_status_revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1', (event_id,)).fetchone()
        effective = self.effective_status(candidate['source'], previous['source_status'], candidate['source_status'])
        if (tuple(last) if last else (previous['source_status'], previous['source_status'])) == (candidate['source_status'], effective):
            return None
        return candidate['source_status'], effective

    @staticmethod
    def same_fact(field, previous, candidate):
        if previous[field] == candidate[field]:
            return True
        if field != 'execution_time_utc':
            return False
        precision = {'millisecond': 1000, 'second': 1, 'microsecond': 1000000,
                     **{f'fractional_second_{digits}': 10 ** digits for digits in range(1, 7)}}
        scale = min(precision.get(previous.get('timestamp_precision'), 1000),
                    precision.get(candidate.get('timestamp_precision'), 1000))
        try:
            def micros(value):
                delta = datetime.fromisoformat(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
                return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
            old, new = micros(previous[field]), micros(candidate[field])
            if old // (1000000 // scale) == new // (1000000 // scale):
                return True
            # A whole-second value under a finer label is a source that truncated to
            # seconds (Binance income `time` until 2026-10-12): the same second is the
            # same fact, not a conflict. Two sub-second values still compare exactly.
            return (old % 1000000 == 0 or new % 1000000 == 0) and old // 1000000 == new // 1000000
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def legacy_toss_writer(conn, event_id):
        """The legacy Toss collector, not the strict fill path, wrote this stored row.

        The creating evidence row (first by rowid; append writes it with the event)
        names a run whose coverage holds a legacy `toss/<hash>` stream and no strict
        `toss/fills-v1/` window. No evidence or coverage proves nothing.
        """
        first = conn.execute('SELECT run_id FROM ledger_evidence WHERE event_id=? ORDER BY rowid LIMIT 1',
                             (event_id,)).fetchone()
        streams = [row[0] for row in conn.execute('SELECT DISTINCT stream FROM ledger_coverage WHERE run_id=?',
                                                  (first[0] if first else None,))]
        return (any(re.fullmatch(r'toss/[0-9a-f]{16}', stream) for stream in streams)
                and not any(stream.startswith('toss/fills-v1/') for stream in streams))

    @staticmethod
    def float_noise(field, previous, candidate):
        """Strict Toss fill vs a value the legacy collector wrote: the stored value, rounded.

        Callers pass only legacy-written values (legacy_toss_writer); digits never
        prove provenance. Field precision: 8 decimals for quantity; for money the
        currency's minor unit, or 4 decimals for USD price and amounts. Only a
        stored value showing binary-float noise qualifies: finer than that
        precision and past 15 significant digits. Rounded at the precision, or the
        new value's finer scale, it must equal the new value. Any other stored
        value compares exactly, so a correction stays a conflict.
        """
        currency = candidate['currency']
        try:
            if field == 'quantity':
                places = 8
            elif field in ('price_native', 'gross_amount', 'amount') and currency == 'USD':
                places = 4
            elif field in ('price_native', 'gross_amount', 'amount', 'fee', 'tax') and currency in MINOR_UNITS:
                places = MINOR_UNITS[currency]
            else:
                return False
            old, new = Decimal(previous[field]).as_tuple(), Decimal(candidate[field])
            if -old.exponent <= places or len(old.digits) <= 15:
                return False
            with localcontext() as ctx:
                ctx.prec = 80
                unit = Decimal(1).scaleb(-max(places, -new.as_tuple().exponent))
                return Decimal(previous[field]).quantize(unit) == new
        except (TypeError, InvalidOperation):
            return False

    def prepare(self, conn, event):
        candidate = {k: event.get(k) for k in FIELDS}
        from ledger_flows import private_identifiers
        private_identifiers(self, conn, candidate)
        for field in NUMERIC:
            candidate[field] = number(candidate[field])
        if (candidate['kind'] not in KINDS or not candidate['source_record_id']
                or not re.fullmatch(r'acct_[a-f0-9]{32}', candidate['account_identity'] or '')):
            raise ValueError("INVALID_LEDGER_EVENT")
        event_id = self.event_id(candidate)
        previous = self.effective_event(conn, event_id)
        legacy_route = False
        if previous is None:
            for old_id in event.get('_legacy_event_ids', ()):
                old = self.effective_event(conn, old_id)
                if old is None:
                    continue
                if candidate['source'] == 'binance_transfers':
                    if candidate.get('direction') != 'out' or old.get('transfer_route') != candidate.get('transfer_route') or old.get('direction') not in (None, 'out'):
                        continue
                    legacy_route = True
                event_id, previous = old_id, old
                candidate['account_identity'] = old['account_identity']
                break
        if previous is None:
            aliases = conn.execute("SELECT alias_identity FROM ledger_account_aliases WHERE canonical_identity=?", (candidate['account_identity'],)).fetchall()
            for alias in aliases:
                old_id = self.event_id({**candidate, 'account_identity': alias[0]})
                old = self.effective_event(conn, old_id)
                if old is not None:
                    event_id, previous = old_id, old
                    candidate['account_identity'] = old['account_identity']
                    break
        annotations = ANNOTATIONS | ({'source_status'} if candidate['kind'] in FLOW_KINDS else set()) | ({'source_record_id'} if legacy_route else set())
        # Only strict Toss fill evidence tolerates float noise, and only in values the
        # legacy Toss collector wrote: strict rows, enrichments, legacy saves and every
        # other source compare exactly.
        noise = set()
        if (event.get('_legacy_float_noise') and previous is not None and candidate['source'] == 'toss_transactions'
                and candidate['kind'] == 'fill' and self.legacy_toss_writer(conn, event_id)):
            base = json.loads(conn.execute('SELECT payload_json FROM ledger_events WHERE event_id=?', (event_id,)).fetchone()[0])
            noise = {k for k in NUMERIC if base.get(k) is not None}
        differences = [k for k in FIELDS if previous is not None and k not in annotations and previous[k] is not None
                       and candidate[k] is not None and not self.same_fact(k, previous, candidate)
                       and not (k in noise and self.float_noise(k, previous, candidate))]
        additions = {k: v for k, v in candidate.items() if previous is not None and previous[k] is None and v is not None
                     and not (k == 'source_status' and candidate['kind'] in FLOW_KINDS)}
        payload = canonical(candidate)
        additions_json = canonical(additions)
        return {'candidate': candidate, 'previous': previous, 'event_id': event_id, 'payload': payload,
                'content_hash': hashlib.sha256(payload.encode()).hexdigest(), 'differences': differences,
                'differences_json': canonical(differences), 'additions': additions,
                'additions_json': additions_json, 'additions_hash': hashlib.sha256(additions_json.encode()).hexdigest(),
                'status_change': self.status_change(conn, event_id, previous, candidate)}

    def append(self, conn, event, evidence=None, *, prepared=None):
        plan = prepared or self.prepare(conn, event)
        candidate, event_id = plan['candidate'], plan['event_id']
        if prepared and self.effective_event(conn, event_id) != plan['previous']:
            raise sqlite3.OperationalError('LEDGER_BATCH_CHANGED')
        observed = datetime.now(timezone.utc).isoformat()
        if plan['previous'] is None:
            columns = ("source", "account_identity", "source_record_id", "kind", "execution_time_utc",
                       "source_timezone", "side", "quantity", "price_native", "currency", "fee", "fee_asset",
                       "tax", "tax_asset", "fx_rate")
            conn.execute("INSERT INTO ledger_events VALUES (" + ','.join(['?'] * 19) + ")",
                         (event_id, *(candidate[k] for k in columns), plan['payload'], plan['content_hash'], observed))
            result = "new"
            if candidate['kind'] in FLOW_KINDS:
                self.classify_transfer(conn, event_id, 'unknown', 'COUNTERPARTY_BOUNDARY_UNVERIFIED')
        elif plan['differences']:
            conn.execute("INSERT OR IGNORE INTO ledger_conflicts VALUES (?,?,?,?,?)",
                         (event_id, plan['content_hash'], plan['payload'], plan['differences_json'], observed))
            result = 'conflict'
        else:
            if plan['additions']:
                conn.execute("INSERT OR IGNORE INTO ledger_enrichments VALUES (?,?,?,?)",
                             (event_id, plan['additions_hash'], plan['additions_json'], observed))
            if plan['status_change']:
                revision = conn.execute('SELECT coalesce(max(revision),0)+1 FROM ledger_status_revisions WHERE event_id=?', (event_id,)).fetchone()[0]
                last = conn.execute('SELECT reported_status,effective_status FROM ledger_status_revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1', (event_id,)).fetchone()
                if not last or tuple(last) != plan['status_change']:
                    conn.execute('INSERT INTO ledger_status_revisions VALUES (?,?,?,?,?)',
                                 (event_id, revision, *plan['status_change'], observed))
            result = 'duplicate'
        if evidence:
            conn.execute("INSERT OR IGNORE INTO ledger_evidence VALUES (?,?,?,?,?,?)", (event_id,
                         evidence['file_sha256'], evidence['run_id'], evidence['page_path'],
                         evidence['row_index'], evidence['raw_store_ref']))
        return result

    def append_many(self, items, *, batch_size=100):
        """Prepare outside locks; release the writer every 100 rows or 0.5s.

        One concurrent-change retry re-prepares the batch outside its transaction.
        Duplicate IDs are split across batches to keep preparation snapshots valid.
        """
        counts = dict.fromkeys(('new', 'duplicate', 'conflict', 'skipped'), 0)
        offset = 0
        while offset < len(items):
            selected, keys = [], set()
            for item in items[offset:offset + batch_size]:
                key = self.event_id(item[0]) if item[0] else None
                if key is not None and key in keys:
                    break
                keys.add(key)
                selected.append(item)
            for attempt in range(2):
                with self.read() as conn:
                    plans = [self.prepare(conn, event) if event else None for event, _ in selected]
                    unchanged = all(plan is None or plan['previous'] is not None and not plan['differences'] and not plan['additions'] and not plan['status_change']
                                    and (not evidence or conn.execute("SELECT 1 FROM ledger_evidence WHERE event_id=? AND run_id=? AND page_path=? AND row_index=?",
                                         (plan['event_id'], evidence['run_id'], evidence['page_path'], evidence['row_index'])).fetchone())
                                    for (_, evidence), plan in zip(selected, plans))
                if unchanged:
                    counts['duplicate'] += sum(plan is not None for plan in plans)
                    counts['skipped'] += sum(plan is None for plan in plans)
                    completed = len(selected)
                    break
                current = dict.fromkeys(counts, 0)
                completed = 0
                try:
                    with self.transaction() as conn:
                        started = time.monotonic()
                        for (event, evidence), plan in zip(selected, plans):
                            current[self.append(conn, event, evidence, prepared=plan) if event else 'skipped'] += 1
                            completed += 1
                            if time.monotonic() - started >= .5:
                                break
                    break
                except sqlite3.OperationalError as exc:
                    if str(exc) != 'LEDGER_BATCH_CHANGED' or attempt:
                        raise
            if not unchanged:
                for key in counts:
                    counts[key] += current[key]
            offset += completed
            time.sleep(.001)  # Yield to a pending NAV/risk writer between batches.
        return counts

    def record_coverage(self, source, account, stream, lo, hi, status, reasons, count, run_id):
        values = self.coverage_values(source, account, stream, lo, hi, status, reasons, count, run_id)
        with self.read() as conn:
            if conn.execute('SELECT 1 FROM ledger_coverage WHERE coverage_id=?', (values[0],)).fetchone():
                return
        with self.transaction() as conn:
            self.coverage(conn, source, account, stream, lo, hi, status, reasons, count, run_id, values=values)

    def match_transfers(self, *, rule_version='l2-v1', conn=None):
        from ledger_flows import match_transfers
        return match_transfers(self, rule_version, conn=conn)

    @staticmethod
    def classify_transfer(conn, event_id, classification, reason_code):
        row = conn.execute("SELECT kind FROM ledger_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None or row[0] not in {'deposit', 'withdrawal', 'transfer', 'transfer_internal', 'transfer_external_in', 'transfer_external_out'}:
            raise ValueError('NOT_A_TRANSFER_EVENT')
        if not re.fullmatch('[A-Z][A-Z0-9_]{1,80}', reason_code):
            raise ValueError('INVALID_REASON_CODE')
        previous = conn.execute("SELECT classification,reason_code,revision FROM ledger_transfer_classifications WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)).fetchone()
        if previous and tuple(previous[:2]) == (classification, reason_code):
            return
        conn.execute("INSERT INTO ledger_transfer_classifications VALUES (?,?,?,?,?)", (event_id,
                     previous[2] + 1 if previous else 1, classification, reason_code, datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def coverage_values(source, account, stream, lo, hi, status, reasons, count, run_id):
        reasons = sorted(set(reasons))
        payload = [source, account, stream, lo, hi, status, reasons, count, run_id]
        return (digest(payload), source, account, stream, str(lo) if lo is not None else None,
                str(hi) if hi is not None else None, status, json.dumps(reasons), count,
                int(status == 'complete' and not reasons and count == 0), run_id, datetime.now(timezone.utc).isoformat())

    @classmethod
    def coverage(cls, conn, source, account, stream, lo, hi, status, reasons, count, run_id, *, values=None):
        conn.execute("INSERT OR IGNORE INTO ledger_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     values or cls.coverage_values(source, account, stream, lo, hi, status, reasons, count, run_id))


def normalize_row(store, conn, stream, row, evidence):
    """Whitelist source facts; never serialize addresses, Uid, URLs or credentials."""
    if not isinstance(row, dict) or not isinstance(row.get('delta', {}), dict):
        return None
    venue = stream.split('/')[0]
    leaf = stream.rsplit('/', 1)[-1]
    if venue == 'binance':
        account = store.account_identity('binance:primary', conn)
    elif venue == 'hyperliquid' and re.search(r'/(?:account-[a-f0-9]{16}|acct_[a-f0-9]{32})/', stream):
        reference = stream.split('/')[1]
        delta = row.get('delta') or {}
        for raw_address in (delta.get('user'), delta.get('destination')):
            if isinstance(raw_address, str) and re.fullmatch(r'0x[a-fA-F0-9]{40}', raw_address):
                private_alias = 'account-' + hashlib.sha256(raw_address.lower().encode()).hexdigest()[:16]
                if reference == private_alias:
                    store.bind_account(conn, 'hyperliquid:' + reference, 'hyperliquid:' + raw_address.lower())
        account = reference if reference.startswith('acct_') else store.account_identity('hyperliquid:' + reference, conn)
    else:
        return None
    def native_id(value):
        native = text(value)
        if native is not None:
            return native
        sha, index = evidence.get('file_sha256'), evidence.get('row_index')
        if isinstance(sha, str) and re.fullmatch('[a-f0-9]{64}', sha) and type(index) is int:
            e['identity_quality'] = 'evidence_row_unverified'
            return f'evidence:{sha}:{index}'
        return None
    e = {"account_identity": account, "source_timezone": "UTC", "timestamp_precision": "millisecond",
         "identity_quality": "native_record"}
    if venue == 'binance' and (leaf.startswith('userTrades_') or leaf == 'trade_async'):
        csv_row = leaf == 'trade_async'
        symbol = text(row.get('Symbol' if csv_row else 'symbol')) or leaf.removeprefix('userTrades_')
        native = native_id(row.get('Trade Id' if csv_row else 'id'))
        if native is None:
            return None
        e.update(source='binance_fills', source_record_id=f'{symbol}:{native}', kind='fill', symbol=symbol,
                 execution_time_utc=instant(row.get('Time(UTC)' if csv_row else 'time')),
                 side=text(row.get('Side' if csv_row else 'side')), quantity=row.get('Quantity' if csv_row else 'qty'),
                 price_native=row.get('Price' if csv_row else 'price'), gross_amount=row.get('Amount' if csv_row else 'quoteQty'),
                 fee=row.get('Fee' if csv_row else 'commission'), fee_asset=None if csv_row else row.get('commissionAsset'),
                 realized_pnl=row.get('Realized Profit' if csv_row else 'realizedPnl'), pnl_asset=row.get('marginAsset'))
        e['currency'] = row.get('quoteAsset')  # export/API does not always report quote currency
        if csv_row:
            e['timestamp_precision'] = 'second'
    elif venue == 'binance' and leaf in ('income', 'income_async'):
        csv_row = leaf == 'income_async'
        kind = text(row.get('type' if csv_row else 'incomeType'))
        native = native_id(row.get('Transaction ID' if csv_row else 'tranId'))
        if native is None:
            return None
        e.update(source='binance_income', source_record_id=canonical([native, kind, text(row.get('Symbol' if csv_row else 'symbol')) or '']), income_type=kind,
                 kind={'FUNDING_FEE': 'funding', 'COMMISSION': 'fee', 'REALIZED_PNL': 'realized_pnl',
                       'TRANSFER': 'transfer'}.get(kind, 'income_other'),
                 amount=row.get('Amount' if csv_row else 'income'), currency=row.get('Asset' if csv_row else 'asset'),
                 symbol=text(row.get('Symbol' if csv_row else 'symbol')),
                 execution_time_utc=instant(row.get('Date(UTC)' if csv_row else 'time')))
        if csv_row:
            e['timestamp_precision'] = 'second'
    elif venue == 'binance' and leaf.startswith('transfer_'):
        native = native_id(row.get('tranId'))
        if native is None:
            return None
        e.update(source='binance_transfers', source_record_id=native, kind='transfer_internal',
                 execution_time_utc=instant(row.get('timestamp')), amount=row.get('amount'), currency=row.get('asset'),
                 transfer_route=leaf.removeprefix('transfer_'))
    elif venue == 'binance' and leaf in ('deposit', 'withdraw'):
        native = native_id(row.get('id') or row.get('txId'))
        if native is None:
            return None
        e.update(source='binance_' + leaf, source_record_id=native, kind='deposit' if leaf == 'deposit' else 'withdrawal',
                 execution_time_utc=instant(row.get('insertTime') if leaf == 'deposit' else row.get('applyTime')),
                 amount=row.get('amount'), currency=row.get('coin'), fee=row.get('transactionFee'), fee_asset=row.get('coin') if row.get('transactionFee') is not None else None)
    elif venue == 'hyperliquid' and leaf == 'userFillsByTime':
        native = native_id(row.get('tid'))
        if native is None:
            return None
        e.update(source='hyperliquid_fills', source_record_id=f"{row.get('coin')}:{native}", kind='fill', symbol=text(row.get('coin')),
                 execution_time_utc=instant(row.get('time')), side={'B': 'buy', 'A': 'sell'}.get(row.get('side')),
                 quantity=row.get('sz'), price_native=row.get('px'), fee=row.get('fee'), fee_asset=row.get('feeToken'),
                 realized_pnl=row.get('closedPnl'), currency=row.get('quoteAsset'))
    elif venue == 'hyperliquid' and leaf == 'userFunding':
        delta = row.get('delta') or {}
        native = native_id(row.get('hash'))
        if native is None:
            return None
        e.update(source='hyperliquid_funding', source_record_id=f"{native}:{delta.get('coin')}:{row.get('time')}",
                 kind='funding', symbol=text(delta.get('coin')), amount=delta.get('usdc'), currency='USDC',
                 execution_time_utc=instant(row.get('time')))
    elif venue == 'hyperliquid' and leaf == 'userNonFundingLedgerUpdates':
        delta = row.get('delta') or {}
        native = native_id(row.get('hash'))
        if native is None:
            return None
        typ = delta.get('type')
        kinds = {'deposit': 'deposit', 'withdraw': 'withdrawal', 'internalTransfer': 'transfer',
                 'spotTransfer': 'transfer_internal', 'subAccountTransfer': 'transfer',
                 'accountClassTransfer': 'transfer_internal', 'send': 'transfer'}
        if typ not in kinds:
            return None
        amount = delta.get('usdc', delta.get('amount'))
        e.update(source='hyperliquid_flows', source_record_id=f'{native}:{typ}:{row.get("time")}', kind=kinds[typ],
                 execution_time_utc=instant(row.get('time')), amount=amount,
                 currency='USDC' if 'usdc' in delta else text(delta.get('token')),
                 fee=delta.get('fee'), fee_asset='USDC' if 'usdc' in delta and 'fee' in delta else None)
        for key, field in (('user', 'origin_account_identity'), ('destination', 'destination_account_identity')):
            if delta.get(key):
                value = str(delta[key])
                if re.fullmatch(r'0x[a-fA-F0-9]{40}', value):
                    value = value.lower()
                elif value.startswith('[ADDRESS_REDACTED]:'):
                    value = 'account-' + value.split(':')[-1]
                e[field] = store.account_identity('hyperliquid:' + value, conn)
    else:
        return None
    e['native_record_id'] = native if not native.startswith('evidence:') else None
    e['record_kind'] = 'execution' if e['kind'] == 'fill' else 'source_record'
    e['source_status'] = text(row.get('status'))
    if venue == 'binance' and e['kind'] == 'fill':
        e['parent_order_id'] = text(row.get('Order Id') if leaf == 'trade_async' else row.get('orderId'))
        e['position_side'] = text(row.get('Position Side') if leaf == 'trade_async' else row.get('positionSide'))
    if e.get('side'):
        e['side'] = str(e['side']).lower()
    from ledger_flows import flow_facts, private_identifiers
    # Legacy lookup markers contain only event digests; raw hashes never leave
    # normalization for evidence serialization or conflict candidates.
    aliases = [account, *(r[0] for r in conn.execute('SELECT alias_identity FROM ledger_account_aliases WHERE canonical_identity=?', (account,)))]
    e['_legacy_event_ids'] = tuple(store.event_id({**e, 'account_identity': alias}) for alias in aliases)
    if venue == 'binance' and leaf in ('deposit', 'withdraw') and native == text(row.get('txId')):
        e['_native_transaction_ref'] = True
    private_identifiers(store, conn, e)
    return flow_facts(store, conn, stream, row, e)


def normalize_rows(store, conn, stream, row, evidence):
    from ledger_flows import expand_flow
    return expand_flow(normalize_row(store, conn, stream, row, evidence))


def rows_in(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get('rows'), list):
        return body['rows']
    return []


def normalize_transaction(store, conn, tx):
    row = tx.to_dict() if hasattr(tx, 'to_dict') else tx
    typ = row.get('tx_type')
    kind = {'BUY': 'fill', 'SELL': 'fill', 'DIVIDEND': 'income_other', 'INTEREST': 'income_other',
            'DEPOSIT': 'deposit', 'WITHDRAWAL': 'withdrawal', 'TRANSFER': 'transfer', 'SPLIT': 'corporate_action'}.get(typ)
    if kind is None:
        return None
    provider = row['provider']
    statement = str(row.get('source') or '').startswith('statement:')
    record = str(row.get('tx_id') or '').removeprefix('stmt_') if statement else row.get('execution_id') or row.get('tx_id')
    if not record:
        return None
    store.bind_account(conn, row.get('account'), row.get('account_key') or row.get('account'))
    currency = row.get('currency')
    fee_status = row.get('fee_status', getattr(tx, 'fee_status', None))
    tax_status = row.get('tax_status', getattr(tx, 'tax_status', None))
    when = getattr(tx, 'execution_time_utc', None)
    event = {'source': 'statement_toss' if statement else provider + '_transactions', 'account_identity': store.account_identity(row.get('account_key') or row['account'], conn),
            'source_record_id': record, 'native_record_id': row.get('tx_id'),
            'parent_order_id': row.get('parent_order_id'), 'record_kind': 'statement_row' if statement else row.get('record_kind', 'order_aggregate'),
            'source_status': row.get('status'), 'settle_date': row.get('settle_date'),
            'market': row.get('market'), 'exchange': row.get('exchange'),
            'kind': kind, 'symbol': row.get('symbol'), 'side': typ.lower() if kind == 'fill' else None,
            'execution_time_utc': when, 'activity_date': row.get('trade_date'), 'source_timezone': getattr(tx, 'execution_source_timezone', None),
            'timestamp_precision': 'millisecond' if when else 'date_only', 'identity_quality': row.get('identity_quality'),
            'quantity': row.get('quantity'), 'price_native': row.get('price'),
            'gross_amount': row.get('gross_amount_native', row.get('gross_amount')), 'currency': currency,
            'amount': row.get('net_amount'), 'fx_rate': row.get('fx_rate') if currency != 'KRW' else None,
            'fee': row.get('fee') if fee_status in {'reported', 'verified_zero'} else None,
            'fee_asset': currency if fee_status in {'reported', 'verified_zero'} else None,
            'tax': row.get('tax') if tax_status in {'reported', 'verified_zero'} else None,
            'tax_asset': currency if tax_status in {'reported', 'verified_zero'} else None}
    execution = getattr(tx, 'toss_execution', None)
    if provider == 'toss' and execution is not None:
        # Preserve exact validated API decimals, without changing legacy tools.
        for key, source in (('quantity', 'filledQuantity'), ('price_native', 'averageFilledPrice'),
                            ('gross_amount', 'filledAmount'), ('fee', 'commission'), ('tax', 'tax')):
            event[key] = execution[source]
        sign = -1 if typ == 'BUY' else 1
        with localcontext() as ctx:
            ctx.prec = 80
            event['amount'] = format(sign * Decimal(event['gross_amount']) - Decimal(event['fee']) - Decimal(event['tax']), 'f')
    return event


def ingest_statements(store, conn=None):
    if conn is None:
        result = dict.fromkeys(('new', 'duplicate', 'conflict', 'skipped'), 0)
        with store.read() as reader:
            if not reader.execute("SELECT 1 FROM sqlite_master WHERE name='statement_rows'").fetchone():
                return result
            revisions = []
            for row in reader.execute("SELECT import_id,undone FROM statement_imports").fetchall():
                prior = reader.execute("SELECT revision,source_undone FROM ledger_statement_revisions WHERE import_id=? ORDER BY revision DESC LIMIT 1", (row[0],)).fetchone()
                if prior is None or prior[1] != row[1]:
                    revisions.append((row[0], prior[0] + 1 if prior else 1, row[1], datetime.now(timezone.utc).isoformat()))
            rows = reader.execute("SELECT r.row_hash,r.payload_json,r.import_id,i.file_sha256 FROM statement_rows r JOIN statement_imports i USING(import_id) WHERE i.undone=0").fetchall()
            items, periods = [], {}
            for row in rows:
                from statement_import import _transaction
                normalized = normalize_transaction(store, reader, _transaction(json.loads(row['payload_json'])))
                if normalized:
                    normalized['source'], normalized['source_record_id'] = 'statement_toss', row['row_hash']
                    periods.setdefault((row['import_id'], normalized['account_identity']), []).append(normalized.get('activity_date'))
                evidence = dict(file_sha256=row['file_sha256'], run_id=row['import_id'], page_path='statement_rows/' + row['row_hash'], row_index=0, raw_store_ref='confirmed_statement')
                items.append((normalized, evidence))
        for revision in revisions:
            with store.transaction() as writer:
                writer.execute('INSERT OR IGNORE INTO ledger_statement_revisions VALUES (?,?,?,?)', revision)
        result = store.append_many(items)
        for (import_id, account), dates in periods.items():
            known = [d for d in dates if d]
            store.record_coverage('statement_toss', account, 'confirmed_statement', min(known, default=None), max(known, default=None),
                                  'limited', ['STATEMENT_BUY_SELL_SCOPE', 'EXECUTION_TIME_DATE_ONLY'], len(dates), import_id)
        return result
    """Read confirmed rows only; no statement schema/migration/undo mutations."""
    result = dict.fromkeys(('new', 'duplicate', 'conflict', 'skipped'), 0)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='statement_rows'").fetchone():
        return result
    for row in conn.execute("SELECT import_id,undone FROM statement_imports"):
        previous = conn.execute("SELECT revision,source_undone FROM ledger_statement_revisions WHERE import_id=? ORDER BY revision DESC LIMIT 1", (row[0],)).fetchone()
        if previous is None or previous[1] != row[1]:
            conn.execute("INSERT INTO ledger_statement_revisions VALUES (?,?,?,?)", (row[0],
                         previous[0] + 1 if previous else 1, row[1], datetime.now(timezone.utc).isoformat()))
    periods = {}
    rows = conn.execute("SELECT r.row_hash,r.payload_json,r.import_id,i.file_sha256 FROM statement_rows r JOIN statement_imports i USING(import_id) WHERE i.undone=0")
    for row in rows:
        from statement_import import _transaction
        body = _transaction(json.loads(row['payload_json']))
        normalized = normalize_transaction(store, conn, body)
        if normalized is None:
            result['skipped'] += 1
            continue
        normalized['source'] = 'statement_toss'
        normalized['source_record_id'] = row['row_hash']
        evidence = dict(file_sha256=row['file_sha256'], run_id=row['import_id'], page_path='statement_rows/' + row['row_hash'], row_index=0, raw_store_ref='confirmed_statement')
        result[store.append(conn, normalized, evidence)] += 1
        key = (row['import_id'], normalized['account_identity'])
        periods.setdefault(key, []).append(normalized.get('activity_date'))
    for (import_id, account), dates in periods.items():
        observed = [d for d in dates if d]
        store.coverage(conn, 'statement_toss', account, 'confirmed_statement', min(observed, default=None),
                       max(observed, default=None), 'limited', ['STATEMENT_BUY_SELL_SCOPE', 'EXECUTION_TIME_DATE_ONLY'],
                       len(dates), import_id)
    return result


def validated_run(run):
    root = Path(run).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('CAPTURE_PATH_UNSAFE')
    manifest = json.loads((root / 'manifest.json').read_text())
    inventory = manifest['files_sha256']
    paths = {}
    for relative, sha in inventory.items():
        parts = Path(relative)
        path = root / parts
        if parts.is_absolute() or '..' in parts.parts or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('CAPTURE_PATH_UNSAFE')
        if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('CAPTURE_FILE_UNAVAILABLE')
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('CAPTURE_HASH_MISMATCH')
        paths[relative] = path
    return root, manifest, paths


def ingest_capture(run, db_path=None, dry_run=False):
    root, manifest, paths = validated_run(run)
    store = LedgerStore(db_path)
    # Dry-run uses a private in-memory backup of existing ledger and statements;
    # no schema creation, salts, files or sidecars are written to the target DB.
    if dry_run:
        import tempfile
        with tempfile.TemporaryDirectory(prefix='ledger-dry-run-') as temporary:
            clone = Path(temporary) / 'portfolio.db'
            if store.db_path.exists():
                with sqlite3.connect(store.db_path.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(clone) as target:
                    source.backup(target)
            return _ingest_capture(root, manifest, paths, LedgerStore(clone))
    return _ingest_capture(root, manifest, paths, store)


def ledger_history_stream(stream):
    leaf = stream.rsplit('/', 1)[-1]
    return (stream.startswith('binance/') and (leaf.startswith('userTrades_') and leaf != 'userTrades_roster' or
            leaf.startswith('transfer_') or leaf in {'income', 'deposit', 'withdraw', 'trade_async', 'income_async'})) or (
            stream.startswith('hyperliquid/account-') and leaf in {'userFillsByTime', 'userFunding', 'userNonFundingLedgerUpdates'})


def private_stream(store, stream):
    if not stream.startswith('hyperliquid/') or len(stream.split('/')) < 3:
        return stream
    reference = stream.split('/')[1]
    account = reference if reference.startswith('acct_') else store.account_identity('hyperliquid:' + reference)
    return stream.replace('/' + reference + '/', '/' + account + '/', 1)


def _ingest_capture(root, manifest, paths, store):
    ingested_pages, ingest_reasons = {}, {}
    report = {'streams': {}, 'totals': dict.fromkeys(('new', 'duplicate', 'conflict', 'skipped'), 0)}
    for stream in manifest['streams']:
        report['streams'][stream] = dict.fromkeys(('new', 'duplicate', 'conflict', 'skipped'), 0)
        ingested_pages[stream], ingest_reasons[stream] = 0, []
    with store.read():
        pass  # Schema only; all files are preflighted before any event writes.
    ordered = sorted(paths, key=lambda p: (p.endswith('export.bin'), p))
    pages = []
    for relative in ordered:
        stream = relative.rsplit('/', 1)[0]
        if stream not in report['streams']:
            continue
        if not ledger_history_stream(stream):
            continue
        export = relative.endswith('/export.bin')
        if not export and (not relative.endswith('.json') or relative.endswith('.meta.json')
                           or relative.rsplit('/', 1)[-1] in {'download-id.json', 'generation-intent.json', 'export.meta.json', 'download-host.json'}):
            continue
        if export:
            with zipfile.ZipFile(paths[relative]) as archive:
                entries = archive.infolist()
                if len(entries) != 1 or entries[0].is_dir() or entries[0].file_size > 128 * 1024 * 1024 or entries[0].flag_bits & 1:
                    raise ValueError('EXPORT_FORMAT_UNAVAILABLE')
                content = archive.read(entries[0]).decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content), strict=True)
            required = {'Trade Id', 'Symbol', 'Time(UTC)', 'Quantity', 'Price'} if stream.endswith('/trade_async') else {'Transaction ID', 'Date(UTC)', 'type', 'Amount', 'Asset'}
            headers = reader.fieldnames or []
            if not required <= set(headers):
                raise ValueError('EXPORT_HEADER_UNAVAILABLE')
            if len(headers) != len(set(headers)) or any(not name.strip() for name in headers):
                raise ValueError('EXPORT_CSV_MALFORMED')
            rows = list(reader)
            # DictReader otherwise accepts short/extra rows and overwrites duplicate
            # headers. Refuse the entire capture before merging any page.
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError('EXPORT_CSV_MALFORMED')
            # A well-formed row with a blank required value is real export data
            # (e.g. income rows without a type): skip it explicitly, never refuse the file.
            complete = [row for row in rows if all(row[name].strip() for name in required)]
            if len(complete) != len(rows):
                report['streams'][stream]['skipped'] += len(rows) - len(complete)
                ingest_reasons[stream].append('EXPORT_ROW_REQUIRED_FIELD_MISSING')
                rows = complete
            ingest_reasons[stream].append('EXPORT_CONTENT_COVERAGE_UNVERIFIED')
        else:
            meta_name = relative.removesuffix('.json') + '.meta.json'
            if meta_name not in paths:
                continue
            meta = json.loads(paths[meta_name].read_text())
            if not 200 <= (meta.get('http_status') or 0) < 300:
                continue
            body = json.loads(paths[relative].read_text())
            if not (isinstance(body, list) or isinstance(body, dict) and
                    (isinstance(body.get('rows'), list) or body.get('total') == 0)):
                ingest_reasons[stream].append('RAW_HISTORY_SHAPE_UNAVAILABLE')
                continue
            rows = rows_in(body)
        pages.append((stream, relative, rows))
    for stream, relative, rows in pages:
        ingested_pages[stream] += 1
        items = []
        with store.read() as conn:
            for index, row in enumerate(rows):
                evidence = dict(file_sha256=manifest['files_sha256'][relative], run_id=root.name,
                                page_path=relative, row_index=index, raw_store_ref=str(root))
                events = normalize_rows(store, conn, stream, row, evidence)
                normalized = events[0]
                if stream.startswith('hyperliquid/'):
                    account = normalized['account_identity'] if normalized else store.account_identity('hyperliquid:' + stream.split('/')[1], conn)
                    evidence['page_path'] = relative.replace('/' + stream.split('/')[1] + '/', '/' + account + '/', 1)
                    # Original capture path is private provenance, never a stream key.
                    evidence['raw_store_ref'] = str(paths[relative])
                if normalized and normalized.get('identity_quality') == 'evidence_row_unverified':
                    ingest_reasons[stream].append('SOURCE_RECORD_ID_UNAVAILABLE')
                items.extend((event, evidence) for event in events)
        counts = store.append_many(items)
        for key in counts:
            report['streams'][stream][key] += counts[key]
    for stream, meta in manifest['streams'].items():
        if meta.get('alias_of') or not ledger_history_stream(stream):
            continue
        account = store.account_identity('binance:primary' if stream.startswith('binance/') else 'hyperliquid:' + stream.split('/')[1])
        reasons = list(meta.get('limitations', [])) + list(meta.get('unavailable_reasons', []))
        reasons += [g['reason'] for g in meta.get('gaps', [])]
        reasons += ingest_reasons[stream]
        if not ingested_pages[stream]:
            reasons.append('RAW_EVIDENCE_UNAVAILABLE')
        count = report['streams'][stream]
        if count['skipped']:
            reasons.append('UNSUPPORTED_OR_MISSING_RECORD_ID')
        if count['conflict']:
            reasons.append('LEDGER_CONTENT_CONFLICT')
        status = 'partial' if meta.get('gaps') or meta.get('status') in {'partial', 'unavailable', 'pending'} or count['skipped'] or count['conflict'] or any(r.startswith('RAW_') for r in reasons) else 'limited' if reasons or meta.get('status') == 'limited' else 'complete'
        store.record_coverage(stream.split('/')[0], account, private_stream(store, stream), meta.get('start'), meta.get('end'), status,
                              reasons, count['new'] + count['duplicate'] + count['conflict'], root.name)
        count['coverage'] = {'status': status, 'reason_codes': sorted(set(reasons))}
    report['statements'] = ingest_statements(store)
    report['transfer_matches'] = store.match_transfers()
    report['streams'] = {private_stream(store, stream): counts for stream, counts in report['streams'].items()}
    for counts in report['streams'].values():
        for key in report['totals']:
            report['totals'][key] += counts[key]
    return report


@contextmanager
def persistence_budget(seconds):
    deadline = time.monotonic() + max(0, seconds)
    outer = _REQUEST_DEADLINE.get()
    token = _REQUEST_DEADLINE.set(min(outer, deadline) if outer is not None else deadline)
    try:
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)


class PersistenceBudget:
    """One cumulative persistence allowance; broker fetch time does not spend it."""
    def __init__(self, seconds=1.8):
        self.remaining = max(0, min(seconds, 1.8))

    @contextmanager
    def attempt(self):
        if self.remaining <= 0:
            raise sqlite3.OperationalError('LEDGER_WRITE_BUDGET_EXHAUSTED')
        started = time.monotonic()
        try:
            with persistence_budget(self.remaining):
                yield
        finally:
            self.remaining -= time.monotonic() - started


def request_store(db_path=None):
    deadline = _REQUEST_DEADLINE.get() or (time.monotonic() + 1.8)
    return LedgerStore(db_path, busy_timeout=1.8, deadline=deadline)


def preserve_observation(store, stream, account, events, lo, hi, complete, reasons):
    """Content-addressed normalized evidence; identical queries are read-only."""
    body = canonical([{k: e.get(k) for k in FIELDS} if e else None for e in events]).encode()
    if len(body) > 32 * 1024 * 1024:
        raise ValueError('LEDGER_OBSERVATION_SIZE_LIMIT')
    body_hash = hashlib.sha256(body).hexdigest()
    reasons = list(reasons)
    observation = digest([stream, account, body_hash, lo, hi, complete, sorted(set(reasons))])
    with store.read() as conn:
        previous = conn.execute('SELECT counts_json FROM ledger_observations WHERE observation_sha256=?', (observation,)).fetchone()
    if previous:
        counts = json.loads(previous[0])
        counts['duplicate'] += counts['new']
        counts['new'] = 0
        return counts
    root = store.db_path.parent / 'ledger-raw' / 'observations'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / (body_hash + '.json')
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != body_hash:
            raise ValueError('LEDGER_EVIDENCE_CONTENT_MISMATCH')
    else:
        with os.fdopen(fd, 'wb') as file:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
    run_id = 'observed-' + observation
    items = [(event, dict(file_sha256=body_hash, run_id=run_id, page_path=path.name,
                         row_index=index, raw_store_ref=str(root))) for index, event in enumerate(events)]
    counts = store.append_many(items)
    if counts['skipped']:
        reasons.append('UNSUPPORTED_OR_MISSING_RECORD_ID')
    if counts['conflict']:
        reasons.append('LEDGER_CONTENT_CONFLICT')
    status = 'partial' if not complete or counts['skipped'] or counts['conflict'] else 'limited' if reasons else 'complete'
    values = store.coverage_values(stream.split('/')[0], account, stream, lo, hi, status, reasons, len(events), run_id)
    counts_json = canonical(counts)
    with store.transaction() as conn:
        store.coverage(conn, stream.split('/')[0], account, stream, lo, hi, status, reasons, len(events), run_id, values=values)
        conn.execute('INSERT OR IGNORE INTO ledger_observations VALUES (?,?)', (observation, counts_json))
    return counts


def observe_collection(collection, db_path=None):
    store = request_store(db_path)
    for source, meta in collection.source_metadata.items():
        scope = source.removesuffix(':orders')
        rows = [tx for tx in collection.transactions if not tx.source.startswith('statement:')
                and (tx.account == scope or source.startswith(tx.account + ':'))]
        with store.read() as conn:
            events = [normalize_transaction(store, conn, tx) for tx in rows]
            account = next((e['account_identity'] for e in events if e), store.account_identity(scope, conn))
        provider = source.split(':')[0]
        reasons = [meta['error_code']] if meta.get('error_code') else []
        if any(e and e['execution_time_utc'] is None for e in events):
            reasons.append('EXECUTION_TIME_DATE_ONLY')
        window = meta.get('requested_window', {})
        preserve_observation(store, provider + '/' + digest(source)[:16], account, events,
                             window.get('start'), window.get('end'), meta.get('complete') is True, reasons)
    ingest_statements(store)


#: Windows are anchored this many days before the earliest stored row, so a row a
#: broker files under an earlier order/query date than its trade date stays inside.
ANCHOR_MARGIN_DAYS = 7


def transaction_stream(source):
    """observe_collection's stream name for one fan-out transaction source."""
    return source.split(':')[0] + '/' + digest(source)[:16]


def history_anchors(sources, start, db_path=None):
    """Earliest date worth fetching per transaction source, proven by stored reads.

    Derived only from existing tables (ledger_coverage, ledger_evidence and
    ledger_events); no state is added. A source is anchored when a stored complete
    observation of that same source started on or before `start` and holds rows of
    at least two symbols: a symbol-filtered read holds one symbol at most, so its
    empty early years prove nothing for the other symbols. Every row that complete
    read returned is on or after its earliest activity date, so 90-day windows ending
    before that date (less ANCHOR_MARGIN_DAYS) are known empty and need no broker
    call. Several qualifying reads give the earliest date of any of them. A source
    with no qualifying read is absent from the result and is fully backfilled.
    Any read failure returns {} (full backfill), never a guessed anchor.
    """
    streams = {transaction_stream(source): source for source in sources}
    path = Path(db_path) if db_path else default_db_path()
    if not streams or not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            reads = {run: (stream, end) for stream, end, run in conn.execute(
                "SELECT stream, end_time, run_id FROM ledger_coverage"
                f" WHERE stream IN ({','.join('?' * len(streams))})"
                " AND status IN ('complete','limited') AND start_time<=?"
                " AND start_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", (*streams, start))}
            # One pass over the evidence of those reads (ledger_evidence has no run_id index).
            rows = conn.execute(
                "SELECT v.run_id, min(json_extract(e.payload_json,'$.activity_date')),"
                " count(DISTINCT json_extract(e.payload_json,'$.symbol')), count(*)"
                " FROM ledger_evidence v JOIN ledger_events e ON e.event_id=v.event_id"
                f" WHERE v.run_id IN ({','.join('?' * len(reads))}) GROUP BY v.run_id",
                tuple(reads)).fetchall() if reads else []
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    anchors = {}
    for run, earliest, symbols, count in rows:
        stream, end = reads[run]
        if symbols < 2 or not earliest or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(earliest)):
            continue
        source = streams[stream]
        current = anchors.get(source)
        if current is None or earliest < current['earliest_row_date']:
            anchors[source] = {'earliest_row_date': earliest, 'evidence_end': end, 'evidence_rows': count}
    for anchor in anchors.values():
        anchor['date'] = (datetime.fromisoformat(anchor['earliest_row_date'])
                          - timedelta(days=ANCHOR_MARGIN_DAYS)).date().isoformat()
    return anchors


def observe_futures(operation, venue, client, output, db_path=None):
    if operation not in ('fills', 'funding'):
        return  # R1 owns current risk snapshots.
    import crypto_futures_accounts as futures
    store = request_store(db_path)
    references = {'binance:primary': output.get('fills' if operation == 'fills' else 'funding_payments', [])}
    if venue == 'hyperliquid':
        references = {}
        for address in getattr(client, '_addresses', []):
            identifier = futures._identity('hyperliquid', address)['account_id']
            reference = 'hyperliquid:' + address.lower()
            store.hl_identity(address)
            references[reference] = [r for r in output.get('fills' if operation == 'fills' else 'funding_payments', [])
                                     if r.get('account_id') == identifier]
    if not references:
        references = {venue + ':unconfigured': []}
    for reference, rows in references.items():
        stream = 'binance/income' if venue == 'binance' and operation == 'funding' else 'binance/userTrades_observed'
        if venue == 'hyperliquid':
            stream = 'hyperliquid/' + store.account_identity(reference) + '/' + ('userFunding' if operation == 'funding' else 'userFillsByTime')
        events = []
        with store.read() as conn:
            account = store.account_identity(reference, conn)
            for row in rows:
                if venue == 'binance':
                    raw = {'symbol': row.get('symbol'), 'time': row.get('time'), 'id': row.get('fill_id'),
                           'side': (row.get('side') or '').upper(), 'qty': row.get('size'), 'price': row.get('price'),
                           'commission': row.get('fee'), 'commissionAsset': row.get('fee_asset'),
                           'realizedPnl': row.get('realized_pnl'), 'marginAsset': row.get('pnl_asset'),
                           'tranId': row.get('payment_id'), 'incomeType': 'FUNDING_FEE', 'income': row.get('amount'), 'asset': row.get('asset')}
                else:
                    raw = {'coin': row.get('symbol'), 'time': row.get('time'), 'tid': row.get('fill_id'),
                           'side': {'buy': 'B', 'sell': 'A'}.get(row.get('side')), 'sz': row.get('size'),
                           'px': row.get('price'), 'fee': row.get('fee'), 'feeToken': row.get('fee_asset'),
                           'closedPnl': row.get('realized_pnl'), 'hash': row.get('payment_id'),
                           'delta': {'coin': row.get('symbol'), 'usdc': row.get('amount')}}
                events.append(normalize_row(store, conn, stream, raw, {}))
        window = output.get('coverage', {}).get('requested_window', {})
        preserve_observation(store, stream if venue == 'hyperliquid' else venue + '/' + operation, account, events, window.get('start_time_ms'),
                             window.get('end_time_ms'), output.get('coverage', {}).get('fetch_complete') is True,
                             output.get('reason_codes', []))


def observe_broker_history(provider, account_key, account_scope, market, payload, metadata, db_path=None):
    """Account-bound direct trade tools use the same identities as fan-out."""
    from portfolio_model import (
        kis_domestic_transactions,
        kis_overseas_transactions,
        toss_transactions,
    )
    from portfolio_sources import PortfolioCollector
    store = request_store(db_path)
    if provider == 'toss':
        transactions = toss_transactions(payload, account_scope)
    elif market == 'domestic':
        transactions = kis_domestic_transactions(payload, account_scope)
    else:
        transactions = kis_overseas_transactions(payload, account_scope)
    transactions = PortfolioCollector._journal_identity(transactions, account_key)
    with store.read() as conn:
        account = store.account_identity(account_key, conn)
        events = [normalize_transaction(store, conn, tx) for tx in transactions]
    window = metadata.get('requested_window', {})
    reasons = ['RECENT_ROW_LIMIT_SCOPE'] if provider == 'toss' else []
    if metadata.get('truncated'):
        reasons.append('PAGE_BUDGET')
    if any(e and e['execution_time_utc'] is None for e in events):
        reasons.append('EXECUTION_TIME_DATE_ONLY')
    return preserve_observation(store, provider + '/direct_' + (market or 'history'), account,
                                events, window.get('start'), window.get('end'),
                                metadata.get('complete') is True, reasons)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Install the strict crypto flow schema only.')
    parser.add_argument('--ensure-schema', required=True, metavar='DB', help='existing portfolio SQLite database')
    ensure_crypto_flow_schema(parser.parse_args().ensure_schema)
