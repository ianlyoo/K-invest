"""Local, confirmed Toss PDF BUY/SELL evidence. No orders or new providers.

Preview bytes live only in bounded process memory. The additive statement tables
share the portfolio DB but never update NAV tables. Non-trades are counted only.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import uuid4

from pypdf import PdfReader

from journal_metadata import source_metadata
from portfolio_model import Transaction, TxType

PARSER_VERSION = "toss_pdf_trade_v1"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 400
MIN_BINDING_API_MATCHES = 5
STATEMENT_START_LAG_BUSINESS_DAYS = 3


class StatementError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def mask_text(value):
    """Samples expose structure only: letters/names/codes masked as well as digits."""
    value = re.sub(r"\d", "9", value)
    return re.sub(r"[^\W\d_]+", "[TEXT]", value)[:240]


def read_inbox(inbox, name, max_bytes=MAX_FILE_BYTES):
    if not isinstance(name, str) or not name or len(name) > 200 or any(x in name for x in ("/", "\\", "..", "\x00")):
        raise StatementError("INVALID_STATEMENT_FILE", "Use a bare PDF file name inside the private inbox.")
    root = file_fd = None
    try:
        root = os.open(Path(inbox), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if stat.S_IMODE(os.fstat(root).st_mode) & 0o077:
            raise StatementError("INSECURE_STATEMENT_INBOX", "Private inbox permissions must be 0700.")
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise StatementError("INVALID_STATEMENT_FILE", "Statement must be a regular file without symlinks.")
        if info.st_size > max_bytes:
            raise StatementError("STATEMENT_TOO_LARGE", "Statement exceeds the file size cap.")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise StatementError("STATEMENT_TOO_LARGE", "Statement exceeds the file size cap.")
        return content
    except OSError:
        raise StatementError("STATEMENT_FILE_UNAVAILABLE", "Private inbox or statement file is unavailable or unsafe.") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if root is not None:
            os.close(root)


@dataclass(frozen=True, slots=True)
class StatementTransaction(Transaction):
    statement_provenance: dict | None = None

    def to_dict(self):
        data = Transaction.to_dict(self)
        for field in ("fee", "tax"):
            data[field] = data[field + "_reported_amount"]
        return {**data, "statement_provenance": self.statement_provenance}


def _payload(tx):
    # Read-through timing belongs to ledger evidence, not the persisted PDF
    # format. Preserve original statement hashes and rollback-reader compatibility.
    data = asdict(tx)
    data.pop("execution_time_utc", None)
    data.pop("execution_source_timezone", None)
    return {**data, "tx_type": tx.tx_type.value}


def _transaction(payload):
    data = dict(payload)
    data["tx_type"] = TxType(data["tx_type"])
    cls = StatementTransaction if "statement_provenance" in data else Transaction
    return cls(**data)


def _base(tx):
    # Statement activity date is a book/settlement date. API settlement date
    # wins when available. No guessed T+N date matching or ticker translation.
    return (tx.account, tx.settle_date or tx.trade_date, tx.currency.upper(),
            (tx.symbol or "").upper(), tx.tx_type.value)


def _equal(left, right):
    for field in ("quantity", "gross_amount"):
        a, b = getattr(left, field), getattr(right, field)
        if a is None or b is None or Decimal(str(a)) != Decimal(str(b)):
            return False
    for field in ("fee", "tax"):
        if left.cost_status(field) in {"reported", "verified_zero"} and right.cost_status(field) in {"reported", "verified_zero"}:
            if Decimal(str(getattr(left, field))) != Decimal(str(getattr(right, field))):
                return False
    return True


def _api_key(tx):
    if tx.execution_id:
        return tx.execution_id
    return "api-row-v1-" + hashlib.sha256(_json([tx.provider, tx.account, tx.tx_id,
        tx.trade_date, tx.settle_date, tx.currency, tx.symbol, tx.tx_type.value,
        tx.quantity, tx.gross_amount]).encode()).hexdigest()


def _statement_row(tx, **provenance):
    values = {key: getattr(tx, key) for key in Transaction.__dataclass_fields__}
    return StatementTransaction(**values, statement_provenance={
        **(getattr(tx, "statement_provenance", None) or {}), **provenance})


def _money_key(tx):
    if tx.quantity is None or tx.gross_amount is None:
        return None
    return (tx.account, tx.currency.upper(), tx.tx_type.value,
        Decimal(str(tx.quantity)), Decimal(str(tx.gross_amount)))


def _near(tx, old):
    day = date.fromisoformat(tx.settle_date or tx.trade_date)
    for reported in {old.trade_date, old.settle_date} - {None}:
        other = date.fromisoformat(reported)
        if abs((day - other).days) > 7:
            continue
        start, end = sorted((day, other))
        business_days = sum((start + timedelta(days=i)).weekday() < 5
            for i in range(1, (end - start).days + 1))
        if business_days <= 3:
            return True
    return False


def deduplicate(rows, existing):
    """Multiset matching. Potential overlaps disagreeing on known money conflict.

    Key: account + a reported activity/trade/settlement date + currency + code + side;
    then exact quantity/native gross, and known fee/tax comparisons. API rows have
    precedence; unknown fields never become verified by a match. Repeated equal
    rows use occurrence counts, not a guessed order/execution ID.
    """
    buckets = defaultdict(list)
    money_buckets = defaultdict(list)
    for index, tx in enumerate(existing):
        if tx.tx_type in {TxType.BUY, TxType.SELL}:
            if _money_key(tx) is not None:
                money_buckets[_money_key(tx)].append(tx)
            base = _base(tx)
            for day in {tx.trade_date, tx.settle_date} - {None}:
                buckets[(base[0], day, *base[2:])].append((index, tx))
    used = set()
    result = {"matched": 0, "new": [], "conflicting": [], "rows": []}
    for index, tx in enumerate(rows):
        candidates = buckets[_base(tx)]
        equal = [j for j, old in candidates if _equal(tx, old)]
        free = next((j for j in equal if j not in used), None)
        if free is not None:
            used.add(free)
            result["matched"] += 1
            result["rows"].append({"position": index, "match_status": "matched",
                "matched_api_key": _api_key(existing[free])})
        elif candidates and not equal:
            result["conflicting"].append({"row": index + 1, "reason_codes": ["STATEMENT_API_CONFLICT"],
                "explanation": "Same account, date, currency, instrument and side; quantity, native gross or known costs disagree."})
            result["rows"].append({"position": index, "match_status": "conflict", "matched_api_key": None})
        elif not equal and any(_near(tx, old) for old in money_buckets[_money_key(tx)]):
            result["conflicting"].append({"row": index + 1, "reason_codes": ["STATEMENT_NEAR_MATCH_CONFLICT"],
                "explanation": "Same account, currency, side, quantity and gross; nearby reported date or same-date code discrepancy. No alias or settlement date inferred."})
            result["rows"].append({"position": index, "match_status": "conflict", "matched_api_key": None})
        else:
            result["new"].append(tx)
            result["rows"].append({"position": index, "match_status": "new", "matched_api_key": None})
    return result


_SCHEMA = """
CREATE TABLE IF NOT EXISTS statement_import_state(key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS statement_imports(
 import_id TEXT PRIMARY KEY, file_sha256 TEXT NOT NULL, account TEXT NOT NULL,
 metadata_json TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0,
 UNIQUE(file_sha256, account));
CREATE TABLE IF NOT EXISTS statement_rows(
 row_hash TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES statement_imports(import_id),
 account TEXT NOT NULL, activity_date TEXT NOT NULL, payload_json TEXT NOT NULL);
"""


def _binding_confirmation(detected):
    """Accept only an explicit suffix proof or the complete API-overlap rule.

    A persisted confirmation remains account-binding evidence when retention
    expires. Keep that original proof separately from current fetch counts.
    """
    for proof in (detected, detected.get("previous_confirmation", {})):
        if proof.get("suffix_confirmed") is True:
            return {"binding_evidence": "registry_suffix", "suffix_confirmed": True}
        counts = [proof.get(key) for key in ("matched_inside_api_coverage", "new_inside_api_coverage", "conflicts")]
        if (proof.get("overlap_confirmed") is True and all(type(n) is int for n in counts)
                and counts[0] >= MIN_BINDING_API_MATCHES and counts[1:] == [0, 0]):
            return {"binding_evidence": "api_overlap", "overlap_confirmed": True,
                "matched_inside_api_coverage": counts[0], "new_inside_api_coverage": 0, "conflicts": 0}
    return None


class StatementStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self._salt = None
        self._salt_lock = RLock()

    @contextmanager
    def _read(self):
        if not self.db_path.exists():
            yield None
            return
        conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='statement_imports'").fetchone()
            yield conn if exists else None
        finally:
            conn.close()

    def salt(self):
        with self._salt_lock:
            if self._salt is None:
                with self._read() as conn:
                    row = conn.execute("SELECT value FROM statement_import_state WHERE key='account_salt'").fetchone() if conn else None
                self._salt = bytes(row[0]) if row else secrets.token_bytes(32)
            return self._salt

    def account_id(self, number):
        return "acct_" + hmac.new(self.salt(), number.encode(), hashlib.sha256).hexdigest()[:32]

    def binding_verified(self, account, binding):
        return self.binding_confirmation(account, binding) is not None

    def binding_confirmation(self, account, binding):
        with self._read() as conn:
            records = conn.execute("SELECT metadata_json FROM statement_imports WHERE account=?", (account,)).fetchall() if conn else []
        for row in records:
            detected = json.loads(row[0]).get("detected_account", {})
            if detected.get("account_id") == binding and (confirmation := _binding_confirmation(detected)):
                return confirmation
        return None

    def load(self, start, end, accounts=None, symbol=None):
        with self._read() as conn:
            if conn is None:
                return [], []
            rows = conn.execute("SELECT r.payload_json FROM statement_rows r JOIN statement_imports i USING(import_id) WHERE i.undone=0 AND activity_date BETWEEN ? AND ? ORDER BY activity_date,row_hash", (start, end)).fetchall()
            imports = [dict(r) for r in conn.execute("SELECT * FROM statement_imports WHERE undone=0")]
        txs = [_transaction(json.loads(r[0])) for r in rows]
        txs = [t for t in txs if (accounts is None or t.account in accounts) and (not symbol or (t.symbol or "").upper() == symbol.upper())]
        imports = [dict(i, metadata=json.loads(i["metadata_json"])) for i in imports if accounts is None or i["account"] in accounts]
        imports = [i for i in imports if not i["metadata"].get("covered_period") or
            (i["metadata"]["covered_period"]["start"] <= end and i["metadata"]["covered_period"]["end"] >= start)]
        return txs, imports

    def apply(self, file_sha256, account, rows, metadata, *, api_rows=()):
        if any(t.tx_type not in {TxType.BUY, TxType.SELL} or t.account != account for t in rows):
            raise StatementError("STATEMENT_SCOPE_VIOLATION", "Only trades in the confirmed account may be written.")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO statement_import_state VALUES ('account_salt', ?)", (self.salt(),))
            binding = metadata.get("detected_account", {}).get("account_id")
            verified = _binding_confirmation(metadata.get("detected_account", {})) is not None
            for old_account, old_metadata in conn.execute("SELECT account,metadata_json FROM statement_imports"):
                old_detected = json.loads(old_metadata).get("detected_account", {})
                old_binding = old_detected.get("account_id")
                if binding and old_binding and ((old_account == account and old_binding != binding) or (old_account != account and old_binding == binding)):
                    raise StatementError("STATEMENT_ACCOUNT_BINDING_CONFLICT", "Statement account binding disagrees with the local ledger; no account was created or remapped.")
                if old_account == account and binding and binding == old_binding and _binding_confirmation(old_detected):
                    verified = True
            if binding and not verified:
                raise StatementError("STATEMENT_ACCOUNT_UNCONFIRMED", "First account binding requires registry suffix or sufficient exact covered API overlap evidence.")
            prior = conn.execute("SELECT import_id,undone FROM statement_imports WHERE file_sha256=? AND account=?", (file_sha256, account)).fetchone()
            if prior and not prior[1]:
                return {"import_id": prior[0], "written_count": 0, "idempotent": True}
            # Revalidate against concurrent local imports while holding the lock.
            existing = [_transaction(json.loads(r[0])) for r in conn.execute("SELECT payload_json FROM statement_rows r JOIN statement_imports i USING(import_id) WHERE i.undone=0")]
            dedup = deduplicate(rows, existing)
            if dedup["conflicting"]:
                raise StatementError("STATEMENT_CONFLICT", "Local ledger changed; preview again. Conflicts are never overwritten.")
            import_id = prior[0] if prior else "imp_" + uuid4().hex
            if prior:
                conn.execute("UPDATE statement_imports SET undone=0,metadata_json=? WHERE import_id=?", (_json(metadata), import_id))
            else:
                conn.execute("INSERT INTO statement_imports VALUES (?,?,?,?,0)", (import_id, file_sha256, account, _json(metadata)))
            matches = deduplicate(rows, list(api_rows))
            if matches["conflicting"]:
                raise StatementError("STATEMENT_CONFLICT", "API evidence conflicts with parsed rows; no conflict was overwritten.")
            for index, (tx, match) in enumerate(zip(rows, matches["rows"])):
                # Immutable parsed evidence, never the unmatched subset index or
                # mutable API-match/coverage annotations, determines identity.
                row_hash = hashlib.sha256(_json([file_sha256, index, _payload(tx)]).encode()).hexdigest()
                tx = _statement_row(tx, parsed_position=index, match_status=match["match_status"],
                    matched_api_key=match["matched_api_key"])
                tx = replace(tx, source="statement:toss:" + file_sha256, tx_id="stmt_" + row_hash,
                             execution_id="ki-v1-" + row_hash, identity_quality="statement_row")
                conn.execute("INSERT OR IGNORE INTO statement_rows VALUES (?,?,?,?,?)", (row_hash, import_id, account, tx.trade_date, _json(_payload(tx))))
            return {"import_id": import_id, "written_count": len(rows), "idempotent": False}

    def undo(self, import_id):
        # Logical undo keeps provenance and the import ID for audit; no NAV edit.
        if not self.db_path.exists():
            return {"import_id": import_id, "removed_count": 0}
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute("SELECT 1 FROM statement_imports WHERE import_id=? AND undone=0", (import_id,)).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM statement_rows WHERE import_id=?", (import_id,)).fetchone()[0] if active else 0
            conn.execute("UPDATE statement_imports SET undone=1 WHERE import_id=?", (import_id,))
            return {"import_id": import_id, "removed_count": count}


def _api_rows(collection):
    return [t for t in collection.transactions if not (t.source or "").startswith("statement:toss:")]


def _statement_window_start(start):
    """Settlement evidence needs a weekday lag from the requested trade start."""
    day = date.fromisoformat(start)
    remaining = STATEMENT_START_LAG_BUSINESS_DAYS
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day.isoformat()


def _api_coverage(collection):
    """Successful requested windows and observed API evidence per account.

    Observed dates are computed from API rows only, before statement enrichment.
    Unknown broker retention is never expanded to the entire requested history.
    """
    api = _api_rows(collection)
    accounts = {t.account for t in api}
    accounts.update(name.removesuffix(":orders") for name in collection.health if name.startswith("toss:") and not name.startswith("statement:"))
    result = {}
    for account in accounts:
        requested = []
        successful = []
        for source, health in collection.health.items():
            if source not in {account, account + ":orders"}:
                continue
            metadata = collection.source_metadata.get(source, {})
            window = metadata.get("requested_window") or collection.requested_window
            if window.get("start") and window.get("end") and window not in requested:
                requested.append(dict(window))
            if "fetched_windows" in metadata:
                successful.extend(metadata["fetched_windows"])
            elif health.get("status") in {"ok", "empty"} and source not in collection.truncated_sources:
                if window.get("start") and window.get("end") and not any(c.get("source") == source for c in collection.failed_chunks):
                    successful.append(dict(window))
        days = [t.trade_date for t in api if t.account == account]
        observed = {"start": min(days, default=None), "end": max(days, default=None)}
        windows = []
        statement_windows = []
        if days:
            for window in successful:
                start, end = max(window["start"], observed["start"]), min(window["end"], observed["end"])
                if start <= end:
                    windows.append({"start": start, "end": end})
                statement_start = max(_statement_window_start(window["start"]), observed["start"])
                if statement_start <= end:
                    statement_windows.append({"start": statement_start, "end": end})
        result[account] = {"requested_windows": requested, "successful_windows": successful,
            "observed_window": observed, "windows": windows,
            "statement_windows": statement_windows,
            "statement_start_lag_business_days": STATEMENT_START_LAG_BUSINESS_DAYS,
            "historical_coverage": "unknown", "basis": "observed API dates intersected with successful requested windows"}
    return result


def _inside_coverage(tx, coverage):
    return any(w["start"] <= (tx.settle_date or tx.trade_date) <= w["end"]
               for w in coverage.get(tx.account, {}).get("statement_windows", []))


def merge_statements(collection, store, start, end, accounts, symbol):
    """Partition at read time; preserve all statement evidence in the store."""
    rows, imports = store.load(start, end, accounts, symbol)
    if not imports:
        return collection
    coverage = _api_coverage(collection)
    api = _api_rows(collection)
    # Rebuild the requested local slice, making repeated enrichment idempotent.
    retained = [t for t in collection.transactions if (t.source or "").startswith("statement:toss:")
        and not (start <= t.trade_date <= end and (accounts is None or t.account in accounts)
                 and (not symbol or (t.symbol or "").upper() == symbol.upper()))]
    groups = defaultdict(list)
    for tx in rows:
        groups[tx.source].append(tx)
    logical, decisions = [], defaultdict(dict)
    for source, group in groups.items():
        overlap = deduplicate(group, logical)
        for tx, match in zip(group, overlap["rows"]):
            if match["match_status"] == "matched":
                decisions[source][tx.tx_id] = {"row_id": tx.tx_id, "decision": "duplicate_statement"}
            elif match["match_status"] == "conflict":
                decisions[source][tx.tx_id] = {"row_id": tx.tx_id, "decision": "conflict"}
            else:
                logical.append(tx)
    matches = deduplicate(logical, api)
    used = []
    for tx, match in zip(logical, matches["rows"]):
        inside = _inside_coverage(tx, coverage)
        decision = ("api_authoritative" if match["match_status"] == "matched" else "conflict"
            if match["match_status"] == "conflict" else "unmatched_inside_api_coverage" if inside
            else "outside_api_coverage")
        detail = {"row_id": tx.tx_id, "decision": decision,
            "matched_api_key": match["matched_api_key"], "inside_api_coverage": inside,
            "api_coverage": coverage.get(tx.account, {})}
        decisions[tx.source][tx.tx_id] = detail
        # A present counterpart always wins. Preserve its execution/book data;
        # local rows only fill absent counterparts on uncovered dates.
        if decision == "outside_api_coverage":
            used.append(_statement_row(tx, coverage=detail))
    collection.transactions = [*api, *retained, *used]
    for item in imports:
        source = "statement:toss:" + item["file_sha256"]
        prior = collection.source_metadata.get(source, {}).get("coverage", {}).get("rows", [])
        details = {r["row_id"]: r for r in prior}
        details.update(decisions[source])
        counts = Counter(r["decision"] for r in details.values())
        conflicts = counts["conflict"]
        gaps = counts["unmatched_inside_api_coverage"]
        incomplete = bool(conflicts or gaps)
        count = sum(t.source == source for t in collection.transactions)
        collection.health[source] = {"status": "error" if incomplete else "ok", "account": item["account"],
            "last_error": {"kind": "statement_conflict" if conflicts else "statement_coverage_gap",
                "code": "STATEMENT_CONFLICT" if conflicts else "NEW_INSIDE_API_COVERAGE"} if incomplete else None}
        collection.source_metadata[source] = {**source_metadata(source, count, complete=not incomplete, start=start, end=end),
            "historical_coverage": "unknown", "covered_period": item["metadata"].get("covered_period"),
            "scope": "BUY/SELL only; skipped activities do not certify cash flows", "dedup_matched": counts["api_authoritative"],
            "conflicting_count": conflicts, "import_id": item["import_id"],
            "coverage": {"api_authoritative_count": counts["api_authoritative"], "statement_used_count": count,
                "duplicate_statement_count": counts["duplicate_statement"], "conflict_count": conflicts,
                "unmatched_inside_api_coverage_count": gaps,
                "rows": list(details.values()), "api": coverage.get(item["account"], {})}}
        if incomplete and not any(c["source"] == source for c in collection.failed_chunks):
            collection.failed_chunks.append({"source": source, "requested_window": {"start": start, "end": end}, "status": "failed", "retry_after_seconds": None})
    collection.transactions.sort(key=lambda t: (t.trade_date, t.provider, t.tx_id))
    dates = [t.trade_date for t in collection.transactions]
    collection.observed_window = {"start": min(dates, default=None), "end": max(dates, default=None)}
    collection.fetch_complete = bool(collection.health) and not collection.failed_chunks and not collection.failed_sources and not collection.truncated_sources
    return collection


@dataclass
class ParsedStatement:
    file_sha256: str
    page_count: int
    account_number: str  # memory only; never persisted or returned
    covered_period: dict
    rows: list
    skipped_by_type: dict
    issues: list
    skipped_reasons_by_type: dict = dataclass_field(default_factory=dict)


class StatementParser(Protocol):
    def parse(self, content: bytes) -> ParsedStatement: ...


def _number(value):
    value = value.strip().replace(",", "")
    if value in {"", "-", "—"}:
        return None
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


class TossPdfParser:
    """Recognized text table only; statement dates are activity/book dates.

    Foreign native money comes exclusively from parenthesized currency lines,
    never from the converted KRW line or an inferred FX conversion. Stock codes
    are preserved; ticker aliases are not invented. Balance columns are evidence,
    not opening quantities. Layout changes/ambiguous trades block apply.
    """
    def parse(self, content):
        if len(content) > MAX_FILE_BYTES:
            raise StatementError("STATEMENT_TOO_LARGE", "Statement exceeds the file size cap.")
        logger = logging.getLogger("pypdf")
        old = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
                raise StatementError("UNSUPPORTED_STATEMENT_PDF", "PDF must be unencrypted text within the page cap.")
            pages = []
            for page in reader.pages:
                stream = page.get_contents()
                if stream is not None and len(stream.get_data()) > 8 * 1024 * 1024:
                    raise StatementError("STATEMENT_CONTENT_TOO_LARGE", "PDF page content exceeds the extraction cap.")
                pages.append((page.extract_text(extraction_mode="layout", layout_mode_space_vertically=False) or "").replace("\xa0", " "))
            return self.parse_pages(pages, hashlib.sha256(content).hexdigest())
        except StatementError:
            raise
        except Exception:
            raise StatementError("INVALID_STATEMENT_PDF", "PDF could not be parsed safely; no rows imported.") from None
        finally:
            logger.setLevel(old)

    def parse_pages(self, pages, file_sha256="synthetic"):
        text = "\n".join(pages)
        account = re.search(r"(?:계좌\s*번호|계좌번호)\s*[:：]?\s*([\d-]{8,})", text)
        period = re.search(r"조회\s*기간\s*[:：]?\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*~\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", text)
        if "거래내역서" not in text or account is None or period is None:
            raise StatementError("UNKNOWN_STATEMENT_LAYOUT", "Unknown Toss statement layout or missing account/period header.")
        try:
            days = [date(*map(int, period.groups()[i:i + 3])).isoformat() for i in (0, 3)]
            if days[0] > days[1]:
                raise ValueError()
        except ValueError:
            raise StatementError("UNKNOWN_STATEMENT_LAYOUT", "Statement period is invalid.") from None
        output = ParsedStatement(file_sha256, len(pages), re.sub(r"\D", "", account[1]),
                                 {"start": days[0], "end": days[1]}, [], {}, [])
        header = None
        pending = None
        foreign = False
        skipped = Counter()
        def finish():
            if pending is None:
                return
            try:
                self._finish_row(pending, output, skipped)
            except (ValueError, IndexError, InvalidOperation):
                output.issues.append({"page": pending["page"], "line": pending["line"],
                    "reason_codes": ["AMBIGUOUS_STATEMENT_TRADE"], "sample": mask_text(pending["text"])})
        for page_no, page in enumerate(pages, 1):
            for line_no, line in enumerate(page.splitlines(), 1):
                clean = line.strip()
                if re.match(r"^(원화|외화)\s*거래내역", clean):
                    foreign = clean.startswith("외화")
                if clean.startswith("거래일자") and "종목명" in clean:
                    columns = re.split(r"\s+", clean)
                    required = {"거래일자", "거래구분", "종목명(종목코드)", "거래수량", "거래대금", "정산금액", "단가", "수수료", "제세금", "잔고", "잔액"}
                    if not required <= set(columns):
                        raise StatementError("UNKNOWN_STATEMENT_LAYOUT", "Unrecognized statement table column layout.")
                    header = {"columns": columns, "type_start": line.index("거래구분"), "instrument_start": line.index("종목명"),
                        "fx_start": line.index("환율") if "환율" in columns else None, "quantity_start": line.index("거래수량")}
                    continue
                if re.match(r"^\d{4}\.\d{2}\.\d{2}", clean):
                    if header is None:
                        raise StatementError("UNKNOWN_STATEMENT_LAYOUT", "Dated rows occur before a recognized statement table.")
                    finish()
                    parts = re.split(r"\s{2,}", clean, maxsplit=2)
                    if len(parts) < 2:
                        raise StatementError("UNKNOWN_STATEMENT_LAYOUT", "Statement row columns are not separable.")
                    pending = {"text": clean, "day": parts[0], "kind": parts[1], "body": parts[2] if len(parts) > 2 else "",
                        "header": header, "foreign": foreign, "page": page_no, "line": line_no, "native": [],
                        "fx_cell_blank": header["fx_start"] is not None and not line[header["fx_start"]:header["quantity_start"]].strip()}
                elif pending is not None:
                    code = re.match(r"^\s*\(([A-Za-z0-9._-]{1,32})\)(?:\s|$)", line)
                    if code:
                        pending["continued_code"] = code[1]
                    native = re.findall(r"\(\s*([$€¥])\s*([+-]?[\d,]+(?:\.\d+)?|—|-)\s*\)", clean)
                    if native:
                        pending["native"].extend(native)
                    if header:
                        continuation = line[header["type_start"]:header["instrument_start"]].strip()
                        if not line[:header["type_start"]].strip() and re.fullmatch(r"[가-힣()\s]+", continuation or " ") and continuation:
                            pending["kind"] += continuation
                if re.match(r"^\d{2,4}[./-]\d{1,2}[./-]\d{1,2}", clean) and not re.match(r"^\d{4}\.\d{2}\.\d{2}", clean):
                    output.issues.append({"page": page_no, "line": line_no, "reason_codes": ["UNRECOGNIZED_ROW_DATE"], "sample": mask_text(clean)})
        finish()
        if header is None:
            raise StatementError("UNKNOWN_STATEMENT_LAYOUT", "Missing recognized statement table layout.")
        output.skipped_by_type = dict(sorted(skipped.items()))
        return output

    def _finish_row(self, row, output, skipped):
        kind = re.sub(r"\s", "", row["kind"])
        skipped_kind = re.sub(r"\([^)]*(?:\)|$)", "([COUNTERPARTY])", kind)
        if kind in {"외화채권매수", "외화채권매도"}:
            skipped[kind] += 1
            output.skipped_reasons_by_type[kind] = ["UNSUPPORTED_INSTRUMENT_NO_STABLE_ID"]
            return
        if "환전" in kind:
            skipped[skipped_kind] += 1
            return
        trade = re.fullmatch(r"(?:장내|주식|해외주식|국내주식)?(매수|매도|구매|판매)(?:\((?:소수점|미니스탁)\))?", kind)
        if not trade:
            if any(word in kind for word in ("매수", "매도", "구매", "판매")):
                output.issues.append({"page": row["page"], "line": row["line"], "reason_codes": ["UNKNOWN_TRADE_TYPE"], "sample": mask_text(kind)})
            else:
                skipped[skipped_kind] += 1
            return
        instrument = re.match(r"(.*)\(([^()]*)\)\s+(.*)$", row["body"])
        if row.get("continued_code"):
            # Long names wrap their code onto the native-money continuation.
            # Match only the numeric suffix; numbers inside a name stay a name.
            number = r"(?:[+-]?[\d,]+(?:\.\d+)?|—|-)"
            tail = re.search(r"(?<!\S)(" + number + r"(?:\s+" + number + r")*)$", row["body"])
            if tail:
                body = row["body"][:tail.start()].rstrip() + "(" + row["continued_code"] + ") " + tail[0]
                instrument = re.match(r"(.*)\(([^()]*)\)\s+(.*)$", body)
        if instrument is None:
            raise ValueError()
        name, code, tail = instrument.groups()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", code) or not code:
            raise ValueError()
        fields = row["header"]["columns"][3:]
        values = re.findall(r"(?<!\S)(?:[+-]?[\d,]+(?:\.\d+)?|—|-)(?!\S)", tail)
        if row["fx_cell_blank"] and "환율" in fields:
            values.insert(fields.index("환율"), "-")
        if (len(values) == len(fields) - 1 and row["fx_cell_blank"] and fields[-2:] == ["잔고", "잔액"]
                and "," in values[-1] and any(len(g) > 3 for g in values[-1].lstrip("+-").split(".")[0].split(","))):
            # Touching balance/cash columns are outside trade scope. A joined
            # comma group exceeding three digits is not a normal amount group.
            # Do not guess their split or seed positions from either balance.
            values[-1:] = ["-", "-"]
        if len(values) != len(fields):
            raise ValueError()
        base = dict(zip(fields, map(_number, values)))
        numbers = base
        currency = "KRW"
        if row["foreign"] or row["native"] or base.get("환율") not in {None, Decimal(0)}:
            if not row["native"] or {c for c, _ in row["native"]} != {"$"}:
                raise ValueError()
            currency = "USD"
            money_fields = [f for f in fields if f not in {"환율", "거래수량", "잔고"}]
            if len(row["native"]) != len(money_fields):
                raise ValueError()
            numbers = dict(zip(money_fields, (_number(v) for _, v in row["native"])))
        quantity = base.get("거래수량")
        if quantity is None or quantity <= 0:
            raise ValueError()
        extra = fields[fields.index("수수료") + 1:fields.index("잔고")]
        charges = [f for f in extra if f != "변제/연체합"]
        if any(f not in {"거래세", "제세금", "인지/농특", "인지/농특세"} for f in charges):
            raise ValueError()
        taxes = [numbers.get(f) for f in charges]
        tax_known = all(v is not None for v in taxes)
        tax = sum((v for v in taxes if v is not None), Decimal(0))
        fee = numbers.get("수수료")
        gross = numbers.get("거래대금")
        price = numbers.get("단가")
        settlement = numbers.get("정산금액")
        side = TxType.BUY if trade[1] in {"매수", "구매"} else TxType.SELL
        day = date.fromisoformat(row["day"].replace(".", "-")).isoformat()
        if not output.covered_period["start"] <= day <= output.covered_period["end"]:
            raise ValueError()
        net = abs(settlement) * (-1 if side is TxType.BUY else 1) if settlement is not None else None
        amount_fields = {"gross_amount": "거래대금", "settlement_amount": "정산금액", "price": "단가", "fee": "수수료"}
        original = {"KRW": {key: str(base[field]) if base.get(field) is not None else None for key, field in amount_fields.items()}}
        if currency == "USD":
            original["USD"] = {key: str(numbers[field]) if numbers.get(field) is not None else None for key, field in amount_fields.items()}
        provenance = {"parser_version": PARSER_VERSION, "file_sha256": output.file_sha256, "page": row["page"],
            "line": row["line"], "native_code": code, "transaction_type": kind,
            "date_basis": "statement activity/book date; not verified execution date", "original_amounts": original,
            "statement_conversion_rate": str(base["환율"]) if base.get("환율") is not None else None,
            "conversion_rate_basis": "statement book conversion; execution FX unverified",
            "tax_components": {f: str(numbers[f]) if numbers.get(f) is not None else None for f in charges},
            "non_tax_components": {f: str(numbers[f]) if numbers.get(f) is not None else None for f in extra if f == "변제/연체합"},
            "balance_columns_status": "not_imported",
            "manual_corrections": [], "missing_fields": [k for k, v in (("price", price), ("gross_amount", gross), ("net_amount", settlement), ("fee", fee)) if v is None]
                + ([] if tax_known else ["tax"])}
        output.rows.append(StatementTransaction("toss", "unbound", f"page-{row['page']}-line-{row['line']}", side, day, currency,
            symbol=code, name=name, quantity=float(quantity), price=float(price) if price is not None else None,
            gross_amount=float(abs(gross)) if gross is not None else None, fee=float(fee or 0), tax=float(tax),
            fee_status="unknown" if fee is None else "verified_zero" if fee == 0 else "reported",
            tax_status="unknown" if not tax_known else "verified_zero" if tax == 0 else "reported", net_amount=float(net) if net is not None else None,
            fx_rate=None,
            market="KR" if currency == "KRW" else None, settle_date=None,
            source="statement:toss:" + output.file_sha256, identity_quality="statement_row", price_source_field="statement:단가",
            statement_provenance=provenance))


def parser_for(broker, format):
    if broker != "toss" or format != "pdf":
        raise StatementError("UNSUPPORTED_STATEMENT_FORMAT", "Only Toss text PDF trade statements are supported.")
    return TossPdfParser()


def _totals(rows):
    groups = defaultdict(list)
    for t in rows:
        groups[t.currency].append(t)
    result = {}
    for currency, txs in sorted(groups.items()):
        totals = {"row_count": len(txs)}
        for field in ("gross_amount", "net_amount", "fee", "tax"):
            available = [t for t in txs if getattr(t, field) is not None and
                (field not in {"fee", "tax"} or t.cost_status(field) in {"reported", "verified_zero"})]
            amount = float(sum(Decimal(str(getattr(t, field))) for t in available)) if available else None
            totals[field] = {"amount": amount if len(available) == len(txs) else None,
                "observed_amount": amount, "status": "reported" if len(available) == len(txs) else "partial" if available else "unknown"}
        result[currency] = totals
    return result


def _dedup_available(quality):
    return (quality.get("source_status") in {"complete", "empty"}
            and not quality.get("sources_failed") and not quality.get("truncated_sources"))


class PreviewManager:
    """Preview is a bounded, expiring, process-local capability; no disk writes."""
    def __init__(self, store, inbox, ledger=None, labels=None, ttl=900, *, registry=None):
        self.store = store
        self.inbox = Path(inbox)
        self.ledger = ledger
        self.labels = labels or (lambda: [])
        self.registry = registry
        self.ttl = ttl
        self.previews = {}
        self.lock = RLock()

    def _live_rows(self, parsed, account):
        if self.ledger is None:
            return [], {"source_status": "unavailable", "reason_codes": ["API_DEDUP_UNAVAILABLE"]}
        collection = self.ledger(parsed.covered_period["start"], parsed.covered_period["end"], [account])
        from portfolio_engine import ledger_input_quality
        return _api_rows(collection), {**ledger_input_quality(collection), "api_coverage": _api_coverage(collection)}

    def _dedup(self, rows, parsed, account, api):
        stored, _ = self.store.load(parsed.covered_period["start"], parsed.covered_period["end"], [account])
        return deduplicate(rows, [*api, *deduplicate(stored, api)["new"]])

    def _account_check(self, parsed, account, rows, api, quality, dedup):
        # Inspect registry data in-process only. Opaque API IDs cannot prove a
        # statement-number suffix; never extract coincidental digits from them.
        configured = ""
        try:
            if self.registry is not None:
                configured = self.registry().credentials_for(account.removeprefix("toss:")).account_id.strip()
        except Exception:
            pass
        suffix_confirmed = bool(re.fullmatch(r"[0-9 -]{4,}", configured)
            and re.sub(r"\D", "", configured)[-4:] == parsed.account_number[-4:])
        binding = self.store.account_id(parsed.account_number)
        previous = self.store.binding_confirmation(account, binding)
        in_period = [t for t in api if t.account == account and t.tx_type in {TxType.BUY, TxType.SELL}
            and parsed.covered_period["start"] <= t.trade_date <= parsed.covered_period["end"]]
        matches = deduplicate(rows, in_period)
        coverage = quality.get("api_coverage", {})
        matched_inside = sum(match["match_status"] == "matched" and _inside_coverage(tx, coverage)
            for tx, match in zip(rows, matches["rows"]))
        new_inside = sum(_inside_coverage(tx, coverage) for tx in matches["new"])
        conflicts = len({conflict["row"] for conflict in [*matches["conflicting"], *dedup["conflicting"]]})
        detected = {"account_id": binding, "masked_suffix": "••••" + parsed.account_number[-4:],
            "selected_account": account, "suffix_confirmed": suffix_confirmed,
            "overlap_confirmed": matched_inside >= MIN_BINDING_API_MATCHES and new_inside == 0 and conflicts == 0,
            "matched_inside_api_coverage": matched_inside, "new_inside_api_coverage": new_inside,
            "conflicts": conflicts, "previously_verified": previous is not None,
            "binding": "registry suffix or exact covered API overlap; no account created"}
        confirmation = _binding_confirmation(detected)
        if confirmation is None and previous is not None:
            detected["previous_confirmation"] = previous
            confirmation = previous
        detected["binding_evidence"] = confirmation["binding_evidence"] if confirmation else None
        reasons = []
        if confirmation is None:
            reasons.append("STATEMENT_ACCOUNT_UNCONFIRMED")
        if in_period and matches["matched"] == 0:
            reasons.append("STATEMENT_ACCOUNT_NO_API_MATCH")
        if conflicts:
            reasons.append("STATEMENT_API_CONFLICT")
        detected["reason_codes"] = reasons
        return detected

    def preview(self, broker, file, account="auto"):
        parser = parser_for(broker, "pdf")
        parsed = parser.parse(read_inbox(self.inbox, file))
        labels = self.labels()
        if account == "auto":
            if len(labels) != 1:
                raise StatementError("STATEMENT_ACCOUNT_REQUIRED", "Select an existing Toss account explicitly; auto requires one configured account.")
            account = "toss:" + labels[0]
        label = account.removeprefix("toss:")
        if label not in labels or not account.startswith("toss:"):
            raise StatementError("INVALID_STATEMENT_ACCOUNT", "Select an existing Toss account label.")
        rows = [replace(t, account=account, account_key=self.store.account_id(parsed.account_number)) for t in parsed.rows]
        api, quality = self._live_rows(parsed, account)
        dedup = self._dedup(rows, parsed, account, api)
        new_inside = sum(_inside_coverage(t, quality.get("api_coverage", {})) for t in dedup["new"])
        preview_id = "preview_" + secrets.token_urlsafe(24)
        detected = self._account_check(parsed, account, rows, api, quality, dedup)
        result = {"preview_id": preview_id, "file_sha256": parsed.file_sha256, "page_count": parsed.page_count,
            "parser_version": PARSER_VERSION, "detected_account": detected, "covered_period": parsed.covered_period,
            "count_by_type": dict(sorted(Counter(t.tx_type.value for t in rows).items())),
            "skipped_by_type": parsed.skipped_by_type, "per_currency_totals": _totals(rows),
            "skipped_reasons_by_type": parsed.skipped_reasons_by_type,
            "parse_issues": {"count": len(parsed.issues), "samples": parsed.issues[:10]},
            "dedup": {"matched": dedup["matched"], "new": len(dedup["new"]),
                "conflicting": len(dedup["conflicting"]), "conflicts": dedup["conflicting"][:10]},
            "new_inside_api_coverage": new_inside,
            "api_input_quality": quality, "preview_ttl_seconds": self.ttl,
            "result_status": "partial" if parsed.issues or dedup["conflicting"] or new_inside or detected["reason_codes"] or not _dedup_available(quality) else "complete",
            "warnings": ["BUY_SELL_ONLY", "NO_CASHFLOW_COVERAGE", "CONFIRM_ACCOUNT_BINDING", "STATEMENT_ACTIVITY_DATE_NOT_EXECUTION_TIME"]}
        with self.lock:
            now = time.monotonic()
            self.previews = {k: v for k, v in self.previews.items() if v["expires"] > now}
            if len(self.previews) >= 16:
                self.previews.pop(next(iter(self.previews)))
            self.previews[preview_id] = {"expires": now + self.ttl, "parsed": parsed, "rows": rows,
                "account": account, "result": result, "new": dedup["new"]}
        return result

    def apply(self, preview_id, confirm=False):
        if type(confirm) is not bool:
            raise StatementError("INVALID_IMPORT_CONFIRMATION", "confirm must be a boolean.")
        with self.lock:
            item = self.previews.get(preview_id)
            if item is None or item["expires"] <= time.monotonic():
                self.previews.pop(preview_id, None)
                raise StatementError("STATEMENT_PREVIEW_EXPIRED", "Preview is expired or unavailable; preview the inbox file again.")
            result = item["result"]
            planned = {"preview_id": preview_id, "confirm": confirm, "would_write_count": len(item["rows"]), "would_use_new_count": len(item["new"]),
                "new_inside_api_coverage": result["new_inside_api_coverage"],
                "detected_account": result["detected_account"],
                "blocked": bool(result["parse_issues"]["count"] or result["dedup"]["conflicting"] or result["new_inside_api_coverage"] or result["detected_account"]["reason_codes"] or not _dedup_available(result["api_input_quality"])),
                "dedup": result["dedup"], "result_status": result["result_status"]}
            if not confirm:
                return planned
            if result["parse_issues"]["count"] or result["dedup"]["conflicting"]:
                raise StatementError("STATEMENT_IMPORT_BLOCKED", "Parse issues or conflicts require a corrected statement and a fresh preview.")
            if not _dedup_available(result["api_input_quality"]):
                raise StatementError("API_DEDUP_UNAVAILABLE", "API ledger dedup is unavailable or incomplete; retry a fresh preview after source recovery.")
            if result["new_inside_api_coverage"]:
                raise StatementError("NEW_INSIDE_API_COVERAGE", "Unmatched rows inside observed API coverage require manual review and a fresh preview; no override is available.")
            if result["detected_account"]["reason_codes"]:
                raise StatementError(result["detected_account"]["reason_codes"][0], "Configured account suffix or API overlap could not confirm the binding; no account was created or remapped.")
            # Re-read the existing API ledger only through the injected collector;
            # pinned preview bytes/rows are never replaced by a changed inbox file.
            api, quality = self._live_rows(item["parsed"], item["account"])
            if not _dedup_available(quality):
                raise StatementError("API_DEDUP_UNAVAILABLE", "API ledger dedup is unavailable or incomplete; retry after source recovery.")
            dedup = self._dedup(item["rows"], item["parsed"], item["account"], api)
            if dedup["conflicting"]:
                raise StatementError("STATEMENT_CONFLICT", "API ledger changed; preview again. No conflict was overwritten.")
            if any(_inside_coverage(t, quality.get("api_coverage", {})) for t in dedup["new"]):
                raise StatementError("NEW_INSIDE_API_COVERAGE", "API coverage changed; unmatched covered rows require manual review and a fresh preview.")
            detected = self._account_check(item["parsed"], item["account"], item["rows"], api, quality, dedup)
            if detected["reason_codes"]:
                raise StatementError(detected["reason_codes"][0], "Account verification changed; obtain a fresh preview. No account was remapped.")
            metadata = {"parser_version": PARSER_VERSION, "covered_period": result["covered_period"],
                "detected_account": detected, "skipped_by_type": result["skipped_by_type"],
                "skipped_reasons_by_type": result["skipped_reasons_by_type"],
                "page_count": result["page_count"], "api_input_quality": quality,
                "scope": "BUY/SELL only; no opening positions, cash flows or corporate actions",
                "source_precedence": "Present API counterparts always win; all parsed rows stored; statement fills only absent counterparts outside coverage"}
            return {**planned, "detected_account": detected,
                **self.store.apply(result["file_sha256"], item["account"], item["rows"], metadata, api_rows=api)}
