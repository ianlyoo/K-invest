"""Frozen, tool-paged audit datasets. No orders, uploads, or raw broker payloads.

Normalized inputs and supporting evidence are typed rows, not unbounded manifest
arrays. The sibling SQLite file keeps the latest 20 datasets by creation order.
Missing/evicted IDs are refused; only an explicit first call collects new inputs.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import portfolio_engine as engine
from kinvest_common import local_calendar_date
from portfolio_store import CALCULATION_VERSION as NAV_VERSION
from portfolio_store import snapshot_summary

EXPORT_VERSION = "portfolio_export_v1"
CALCULATION_VERSION = "portfolio_accounting_v3_per_account"
MAX_PAGE_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 24 * 1024
DATASETS = {"transactions", "realized_pnl", "holdings", "nav_history"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_dataset_state (key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_datasets (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL UNIQUE,
    dataset TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_dataset_rows (
    dataset_id TEXT NOT NULL REFERENCES portfolio_datasets(dataset_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, row_json TEXT NOT NULL,
    PRIMARY KEY(dataset_id, ordinal)
);
CREATE TRIGGER IF NOT EXISTS dataset_no_update BEFORE UPDATE ON portfolio_datasets BEGIN
    SELECT RAISE(ABORT, 'dataset content is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_rows_no_update BEFORE UPDATE ON portfolio_dataset_rows BEGIN
    SELECT RAISE(ABORT, 'dataset rows are immutable');
END;
"""


class DatasetError(ValueError):
    """Fixed public errors: never include a cursor, account, salt, or path."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def query_metadata(snapshot_id=None, calculation_version=CALCULATION_VERSION):
    """Live query IDs are additive and require no state access or provider reads."""
    return {"query_id": uuid4().hex, "dataset_id": None, "snapshot_id": snapshot_id,
            "calculation_version": calculation_version}


_PRIVATE_KEYS = {"token", "accesstoken", "refreshtoken", "authorization", "password", "secret",
                 "clientsecret", "clientid", "appkey", "appsecret", "apikey", "credentials",
                 "cano", "acntprdtcd", "accountnumber", "accountno", "accountseq", "note", "headers"}
_SENSITIVE_TEXT = re.compile(r"(?i)(?:bearer\s+\S+|\b(?:sk-|AKIA)[A-Za-z0-9_-]+|"
                             r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
                             r"(?:access_token|client_secret|appsecret)\s*[:=]\s*\S+|\d{8,}(?:-\d+)?)")


class _Privacy:
    def __init__(self, salt, records):
        self.salt = salt
        self.aliases = {}
        for row in records:
            data = row["data"]
            if data.get("account") and data.get("account_key"):
                self.aliases[data["account"]] = data["account_key"]

    def opaque(self, value, prefix="acct_"):
        namespace = self.aliases.get(str(value), str(value)) if prefix == "acct_" else str(value)
        digest = hmac.new(self.salt, (prefix + namespace).encode(), hashlib.sha256).hexdigest()
        return prefix + digest[:32]

    def text(self, value):
        replacements = {}
        for index, alias in enumerate(sorted(self.aliases, key=len, reverse=True)):
            if alias in value:
                placeholder = f"ACCOUNT_ALIAS_{index}"
                value = value.replace(alias, placeholder)
                replacements[placeholder] = self.opaque(alias)
        value = _SENSITIVE_TEXT.sub("[redacted]", value)
        for placeholder, opaque in replacements.items():
            value = value.replace(placeholder, opaque)
        return value

    def clean(self, value):
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            result = {}
            account = value.get("account")
            for key, item in value.items():
                compact = re.sub(r"[^a-z0-9]", "", key.lower())
                if compact in _PRIVATE_KEYS or compact.endswith(("secret", "password", "token")):
                    continue
                if key in {"account", "account_key", "account_id"} and item is not None:
                    result[key] = self.opaque(item)
                elif key == "tx_id" and item:
                    result[key] = self.opaque(item, "tx_")
                elif key == "transaction_ids":
                    result[key] = [self.opaque(v, "tx_") for v in item]
                elif key in {"snapshot_id", "execution_id", "parent_order_id"} and isinstance(item, str) and re.fullmatch(
                    r"(?:ki-v1-|ki-order-v1-)?[a-f0-9]{32,64}", item):
                    # Already public, content/UUID identities, not account numbers.
                    result[key] = item
                else:
                    result[self.text(key)] = self.clean(item)
            if account:
                result["account_id"] = self.opaque(account)
                # Only already-exposed human labels; numeric identities stay opaque.
                result["account_label"] = account if re.fullmatch(r"[A-Za-z_:-]+", account) else None
            return result
        return self.text(value) if isinstance(value, str) else value


def _amount(values, statuses=None):
    statuses = statuses or ["reported" if v is not None else "unknown" for v in values]
    known = [Decimal(str(v)) for v, s in zip(values, statuses) if v is not None and s in {"reported", "verified_zero"}]
    observed = float(sum(known)) if known else None
    complete = len(known) == len(values)
    return {"amount": observed if complete else None, "observed_amount": observed,
            "status": "verified_zero" if complete and all(s == "verified_zero" for s in statuses)
                else "reported" if complete else "partial" if known
                else "unsupported" if all(s == "unsupported" for s in statuses) else "unknown"}


def currency_totals(records):
    """Sum only input money fields, separated by record type and native currency.

    Derived symbols, reconciliation evidence and snapshots are excluded to avoid
    counting the same money twice. Quantities/prices across symbols are not summed.
    """
    groups = defaultdict(list)
    for row in records:
        if row["record_type"] in {"transaction", "holding", "cash_balance"}:
            groups[row["record_type"], row["data"]["currency"]].append(row["data"])
    result = {}
    fields = {"transaction": ("gross_amount", "net_amount", "fee", "tax"),
              "holding": ("cost_basis", "market_value"), "cash_balance": ("amount", "settled_cash",
                  "unsettled_receivables", "unsettled_payables", "withdrawable_cash", "buying_power")}
    for (kind, currency), rows in sorted(groups.items()):
        totals = {"row_count": len(rows)}
        for field in fields[kind]:
            statuses = [r.get(field + "_status", "reported" if r.get(field) is not None else "unknown") for r in rows]
            totals[field] = _amount([r.get(field) for r in rows], statuses)
            if kind == "transaction" and field in {"fee", "tax"}:
                observed = [Decimal(str(r[field + "_observed_amount"])) for r in rows
                            if r.get(field + "_observed_amount") is not None]
                if observed:
                    totals[field]["observed_amount"] = float(sum(observed))
                    if totals[field]["amount"] is None:
                        totals[field]["status"] = "partial"
        result.setdefault(kind, {})[currency] = totals
    return result


class DatasetStore:
    def __init__(self, db_path: Path | str, max_datasets: int = 20):
        self.db_path = Path(db_path)
        if not 1 <= max_datasets <= 100:
            raise ValueError("max_datasets must be between 1 and 100")
        self.max_datasets = max_datasets

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            os.chmod(self.db_path, 0o600)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO portfolio_dataset_state VALUES ('account_salt', ?)", (secrets.token_bytes(32),))
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _salt(conn):
        return conn.execute("SELECT value FROM portfolio_dataset_state WHERE key='account_salt'").fetchone()[0]

    def freeze(self, dataset, records, manifest):
        if dataset not in DATASETS:
            raise ValueError("unsupported dataset")
        with self._connect() as conn:
            privacy = _Privacy(self._salt(conn), records)
            records = privacy.clean(records)
            manifest = privacy.clean(manifest)
            # Stable ordering also distinguishes duplicate unidentified rows.
            records.sort(key=lambda row: (row["record_type"], canonical(row["data"])))
            for index, row in enumerate(records):
                row["row_id"] = "row_" + hashlib.sha256((str(index) + canonical(row)).encode()).hexdigest()[:32]
            serialized = canonical(records)
            manifest.update({"dataset": dataset, "export_version": EXPORT_VERSION,
                "row_count": len(records), "count_by_record_type": dict(sorted(Counter(r["record_type"] for r in records).items())),
                "per_currency_totals": currency_totals(records),
                "reporting_currency_totals": currency_totals([r for r in records if r["record_type"] == "transaction"
                    and r["data"].get("reporting_inclusion", {}).get("included", True)]),
                "totals_scope": "all frozen input rows; grouped by record type and native currency; reporting summaries use their stated period",
                "rows_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                "canonical_serialization": "UTF-8 JSON; sorted keys, compact separators, literal null, finite numbers; row order is part of the hash",
                "account_identity": "HMAC-SHA256 with private local salt; scopes/labels only when already exposed; no raw broker payloads",
                "retention": {"kind": "creation_order_count", "max_datasets": self.max_datasets},
                "csv_encoding": "record_type,row_id,data_json; data_json is canonical JSON, including null and status"})
            dataset_id = "ds_" + hashlib.sha256(canonical(manifest).encode()).hexdigest()
            existing = conn.execute("SELECT dataset_id FROM portfolio_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            if not existing:
                manifest.update(dataset_id=dataset_id, query_id=uuid4().hex)
                text = canonical(manifest)
                if len(text.encode()) > MAX_MANIFEST_BYTES:
                    raise DatasetError("DATASET_MANIFEST_TOO_LARGE", "Manifest exceeds the bounded export response.")
                conn.execute("INSERT INTO portfolio_datasets(dataset_id,dataset,manifest_json) VALUES (?,?,?)", (dataset_id, dataset, text))
                conn.executemany("INSERT INTO portfolio_dataset_rows VALUES (?,?,?)",
                    [(dataset_id, i, canonical(row)) for i, row in enumerate(records)])
                conn.execute("DELETE FROM portfolio_datasets WHERE dataset_id NOT IN (SELECT dataset_id FROM portfolio_datasets ORDER BY sequence DESC LIMIT ?)", (self.max_datasets,))
            return dataset_id

    def _cursor(self, salt, dataset_id, offset):
        body = canonical([dataset_id, offset]).encode()
        signed = body + hmac.new(salt, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signed).decode().rstrip("=")

    def _offset(self, salt, dataset_id, cursor):
        if cursor is None:
            return 0
        try:
            if len(cursor) > 512:
                raise ValueError()
            signed = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            body, signature = signed[:-32], signed[-32:]
            if not hmac.compare_digest(signature, hmac.new(salt, body, hashlib.sha256).digest()):
                raise ValueError()
            pinned, offset = json.loads(body)
            if pinned != dataset_id or type(offset) is not int or offset < 0:
                raise ValueError()
            return offset
        except (ValueError, TypeError, UnicodeDecodeError):
            raise DatasetError("INVALID_DATASET_CURSOR", "Cursor is invalid or belongs to another dataset.") from None

    def page(self, dataset, dataset_id, cursor=None, format="json", page_size=200):
        if type(page_size) is not int or not 1 <= page_size <= 200 or format not in {"json", "csv"}:
            raise ValueError("page_size must be 1–200 and format must be json or csv")
        with self._connect() as conn:
            entry = conn.execute("SELECT dataset,manifest_json FROM portfolio_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            if not entry:
                raise DatasetError("DATASET_EXPIRED", "Dataset is expired or unavailable. Start a new export explicitly; no refetch was performed.")
            if entry[0] != dataset:
                raise DatasetError("DATASET_KIND_MISMATCH", "Dataset ID belongs to another dataset kind.")
            salt = self._salt(conn)
            offset = self._offset(salt, dataset_id, cursor)
            manifest = json.loads(entry[1])
            if offset > manifest["row_count"]:
                raise DatasetError("INVALID_DATASET_CURSOR", "Cursor is outside this dataset.")
            candidates = conn.execute("SELECT row_json FROM portfolio_dataset_rows WHERE dataset_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?", (dataset_id, offset, page_size)).fetchall()
            selected, budget = [], 0
            for (text,) in candidates:
                # CSV quotes JSON twice; reserve for escaping as well as UTF-8.
                size = len(text.encode()) * (2 if format == "csv" else 1) + 64
                if budget + size > MAX_PAGE_BYTES:
                    if not selected:
                        raise DatasetError("DATASET_ROW_TOO_LARGE", "One audit row exceeds the page byte budget.")
                    break
                selected.append(json.loads(text))
                budget += size
            end = offset + len(selected)
            result = {"dataset_id": dataset_id, "manifest": manifest, "format": format,
                "page_start": offset, "page_count": len(selected),
                "next_cursor": self._cursor(salt, dataset_id, end) if end < manifest["row_count"] else None,
                "result_status": manifest["summary"]["result_status"]}
            if format == "json":
                result["rows"] = selected
            else:
                output = io.StringIO(newline="")
                writer = csv.writer(output, lineterminator="\n")
                writer.writerow(["record_type", "row_id", "data_json"])
                writer.writerows((r["record_type"], r["row_id"], canonical(r["data"])) for r in selected)
                result["csv"] = output.getvalue()
            return result


def _row(kind, data):
    return {"record_type": kind, "data": data}


def _transaction(t):
    data = t.to_dict()
    # Compatibility zero placeholders stay in live tools, not in audit input.
    for field in ("fee", "tax"):
        # A normalized nonzero cost can be an observed component sum even when
        # the aggregate is unknown (e.g. only one KIS fee column was supplied).
        # Unknown zero placeholders are not evidence of an observed zero.
        amount = data[field]
        complete = data[field + "_status"] in {"reported", "verified_zero"}
        data[field + "_observed_amount"] = amount if complete or amount != 0 else None
        data[field + "_observed_status"] = data[field + "_status"] if complete or amount == 0 else "partial"
        data[field] = data[field + "_reported_amount"]
    for field in ("price", "gross_amount", "net_amount"):
        data[field + "_status"] = "reported" if data[field] is not None else "unknown"
    if data["net_amount"] is not None and any(data[field] is None for field in ("fee", "tax")):
        data["net_amount_status"] = "partial"
        data["net_amount"] = None
    return data


def _mapping():
    return {
        "toss:orders": {"quantity": "execution.filledQuantity > 0 (including CANCELED/REPLACED)",
            "price": "execution.averageFilledPrice", "gross_amount": "execution.filledAmount, else quantity*price",
            "fee": "execution.commission", "tax": "execution.tax",
            "trade_date": "execution.filledAt, else orderedAt, ISO date", "settle_date": "execution.settlementDate"},
        "kis:inquire-daily-ccld": {"quantity": "tot_ccld_qty", "price": "avg_prvs, else tot_ccld_amt/tot_ccld_qty", "gross_amount": "tot_ccld_amt, else quantity*price",
            "trade_date": "ord_dt (YYYYMMDD to ISO)", "fee": "prsm_tlex_smtl: estimated total costs, commission/tax not split", "tax": "unsupported"},
        "kis:inquire-period-trans": {"quantity": "ccld_qty", "gross_amount": "tr_frcr_amt2, else quantity*native price",
            "price": "ft_ccld_unpr2, else tr_frcr_amt2/ccld_qty; ambiguous ovrs_stck_ccld_unpr excluded",
            "fee": "frcr_fee1 + dmst_frcr_fee1", "tax": "unsupported",
            "fx_rate": "erlm_exrt", "trade_date": "trad_dt", "settle_date": "sttl_dt"},
        "kis:inquire-ccnl": {"quantity": "ft_ccld_qty", "price": "ft_ccld_unpr3", "gross_amount": "ft_ccld_amt3, else quantity*price",
            "fee": "unsupported", "tax": "unsupported"},
        "holdings": {"quantity": "Toss quantity / KIS hldg_qty or ovrs_cblc_qty",
            "cost_basis": "Toss marketValue.purchaseAmount / KIS pchs_amt or frcr_pchs_amt1; else avg_price*quantity",
            "market_value": "Toss marketValue.amount / KIS evlu_amt or ovrs_stck_evlu_amt"},
        "cash_balance": "Each row's source_fields and cash_field_status identify settlement/buying-power fields.",
        "nav_snapshot": "Exact E-store payload/versions; component/adjustment arrays are separate linked audit rows.",
        "row_provenance": "Transaction price_source_field, normalizer_version, price_validation, source, execution_id and cost statuses are authoritative per row."}


def _policies(fx=None):
    return {"fx": {"rates_to_krw": {"KRW": 1.0, **(fx.rates if fx else {})},
                   "missing": sorted(fx.missing) if fx else [], "observed_at": fx.as_of if fx else None,
                   "valuation": "frozen spot rates; observed_at is fetch time, not underlying close time",
                   "trade_price_base": "recorded broker trade FX only; missing remains null",
                   "costs": "broker trade FX, else frozen spot; applied rates are cost_fx audit rows"},
            "fees": "Reported buy fees/taxes capitalized; reported sell costs deducted. Unsupported/unknown totals remain null with status. fee/tax_observed_amount retain observed component sums for diagnostic arithmetic only; partial components do not certify totals and absent components are not verified zero.",
            "basis_scope": "per_account",
            "cost_basis": "moving average per (account, symbol, currency); aggregates sum account cost and P&L; broker holdings cost kept separately with unknown fee/tax inclusion",
            "settlement": "Reported NAV retains stock value + native cash amount. Reference bridge adds separate receivables minus payables only when native cash excludes them; never adds ledger trades again. Not official NAV.",
            "rounding": "Existing engine: money/summary KRW 2dp, native book 4dp, quantities 8dp; row gross preserved. Canonical hashes use serialized input precision."}


def _bridge_rows(bridge, rows, snapshot_id=None):
    bridge = dict(bridge or {})
    for key, kind in (("adjustments", "nav_adjustment"), ("cash_balances", "bridge_cash_balance"),
                      ("broker_reported_totals", "broker_nav_total")):
        values = bridge.pop(key, [])
        rows.extend(_row(kind, dict(item, snapshot_id=snapshot_id)) for item in values)
        bridge[key + "_rows"] = {"record_type": kind, "snapshot_id": snapshot_id, "count": len(values)}
    return bridge


def collect_dataset(dataset, start, end, collector=None, nav_store=None, accounts=None,
                    account_filter="all", symbol="", input_snapshot_ids=None):
    """Capture through existing collection and engine functions; never change math."""
    rows = []
    manifest = {"calculation_version": CALCULATION_VERSION, "source_normalized_mapping": _mapping(),
                "policies": _policies(), "reporting_window": {"start": start, "end": end},
                "exclusions": {"accounts": [] if account_filter == "all" else [{"filter": account_filter,
                    "reason": "CALLER_ACCOUNT_FILTER", "scope": "accounts outside the filter; inventory not enumerated"}],
                    "periods": [{"before": start, "after": end, "reason": "OUTSIDE_REPORTING_WINDOW"}],
                    "symbol": None if not symbol else {"included": symbol, "reason": "CALLER_SYMBOL_FILTER"}},
                "snapshot_versions": {"record_type": "nav_snapshot", "id_field": "snapshot_id", "version_field": "version", "count": 0}}
    if dataset == "nav_history":
        navs = nav_store.range(start, end, limit=1000000) if input_snapshot_ids is None else nav_store.select_snapshots(input_snapshot_ids)
        navs = [n for n in navs if start <= n["snapshot_date"] <= end]
        for nav in navs:
            data = dict(nav)
            components = data.pop("components", None)
            rows.extend(_row("nav_component", dict(c, snapshot_id=nav["snapshot_id"])) for c in components or [])
            data["components_rows"] = {"record_type": "nav_component", "count": len(components) if components is not None else None,
                                       "status": "reported" if components is not None else "unknown"}
            data["nav_bridge"] = _bridge_rows(data.get("nav_bridge"), rows, nav["snapshot_id"]) if data.get("nav_bridge") else None
            rows.append(_row("nav_snapshot", data))
        statistics = engine.nav_statistics(navs, risk_free_rate=0)
        manifest["calculation_version"] = statistics["calculation_version"]
        manifest["snapshot_versions"]["count"] = len(navs)
        manifest["snapshot_versions"]["latest"] = {
            "snapshot_id": navs[-1]["snapshot_id"], "version": navs[-1]["version"]} if navs else None
        manifest["policies"]["snapshots"] = {"store_calculation_version": NAV_VERSION,
            "selection": "explicit immutable versions" if input_snapshot_ids else "daily representatives frozen at first call",
            "fx": "Each snapshot's fx_times retains recorded rate/time. Legacy missing provenance remains unknown.",
            "risk_free_rate": 0, "external_cashflows": "unknown; no zero-flow confirmation inferred"}
        manifest["summary"] = {"snapshot_count": len(navs), "first_nav_krw": navs[0]["total_value_krw"] if navs else None,
                               "last_nav_krw": navs[-1]["total_value_krw"] if navs else None,
                               "result_status": statistics["result_status"]}
        manifest["coverage"] = {"requested_window": {"start": start, "end": end},
            "observed_window": {"start": navs[0]["snapshot_date"] if navs else None, "end": navs[-1]["snapshot_date"] if navs else None},
            "fetch_complete": True, "historical_coverage": "unknown", "failed_chunks": {"record_type": "failed_chunk", "count": 0},
            "scope": "stored NAV versions only; no historical broker backfill"}
        manifest["reconciliation"] = {"status": "stored_snapshot_quality", "record_type": "nav_snapshot"}
        return rows, manifest

    positions = collector.collect_positions(accounts)
    ledger = None
    if dataset in {"transactions", "realized_pnl"}:
        ledger_start = engine.resolve_period("ALL")[0] if dataset == "realized_pnl" else start
        ledger = collector.collect_transactions(ledger_start, end, accounts, symbol or None)
        engine.reconcile_collections(ledger, positions, symbol or None)
        manifest["ledger_window"] = {"start": ledger_start, "end": end}
        if ledger_start < start:
            manifest["exclusions"]["periods"].append({"start": ledger_start, "end": (date.fromisoformat(start) - timedelta(days=1)).isoformat(),
                "reason": "BEFORE_REALIZED_REPORTING_PERIOD", "retained_for_cost_basis": True})
    holdings = [h for h in positions.holdings if not symbol or h.symbol.upper() == symbol.upper()]
    currencies = {h.currency for h in holdings} | {c.currency for c in positions.cash}
    if ledger:
        currencies |= {t.currency for t in ledger.transactions}
        for t in ledger.transactions:
            data = _transaction(t)
            included = start <= t.trade_date <= end
            data["reporting_inclusion"] = {"included": included,
                "reason": "IN_REPORTING_WINDOW" if included else "RETAINED_FOR_COST_BASIS"}
            rows.append(_row("transaction", data))
    fx = collector.build_fx(currencies)
    rows.extend(_row("holding", h.to_dict()) for h in holdings)
    rows.extend(_row("cash_balance", c.to_dict()) for c in positions.cash)
    manifest["policies"] = _policies(fx)
    # Preserve frozen total_assets_krw semantics and the reference bridge.
    nav = snapshot_summary(engine.summarize_portfolio(holdings, positions.cash, fx), positions, fx)
    bridge = _bridge_rows(nav["nav_bridge"], rows)
    summary = {"nav": {"reported_total_assets_krw": nav["total_assets_krw"],
        "settlement_adjusted_nav_krw": bridge.get("settlement_adjusted_nav_krw"),
        "status": bridge.get("reconciliation", {}).get("status", "unknown"),
        "reason_codes": nav.get("reason_codes", []), "basis": "current collection, not historic NAV"},
        "nav_bridge": bridge}
    if dataset == "realized_pnl":
        realized = engine.compute_realized_pnl(ledger.transactions, fx, start, end, input_quality=engine.ledger_input_quality(ledger))
        totals = dict(realized["totals"])
        cost_fx = dict(totals.pop("cost_fx", {}))
        rates = cost_fx.pop("rates", [])
        rows.extend(_row("cost_fx", item) for item in rates)
        cost_fx["rates"] = {"record_type": "cost_fx", "count": len(rates)}
        manifest["policies"]["fx"]["applied_cost_fx"] = cost_fx
        rows.extend(_row("realized_symbol", item) for item in realized["symbols"])
        for field in ("complete_basis", "incomplete_basis"):
            totals[field] = {k: v for k, v in totals[field].items() if k != "symbols"}
        summary.update(totals=totals, result_status=totals["status"], cost_fx={"record_type": "cost_fx", "count": len(rates)})
    elif dataset == "holdings":
        consolidated = engine.consolidate_holdings(holdings, fx)
        rows.extend(_row("consolidated_holding", item) for item in consolidated["positions"])
        summary.update(totals=consolidated["totals"], position_count=consolidated["position_count"],
                       result_status=engine.collection_result_status([positions], len(holdings) + len(positions.cash)))
    else:
        summary.update(transaction_count=len(ledger.transactions),
                       result_status=engine.collection_result_status([ledger], len(ledger.transactions)))
    quality = ledger or positions
    reconciliation = ledger.reconciliation if ledger else {"status": "unavailable", "reason_codes": ["RECONCILIATION_NOT_REQUESTED"]}
    breaks = reconciliation.get("reconciliation_breaks", [])
    for item in breaks:
        data = dict(item)
        data["provenance"] = {"transaction_record_type": "transaction", "selection": "same account, symbol, currency"}
        rows.append(_row("reconciliation_break", data))
    if breaks and summary["result_status"] not in {"partial", "unavailable"}:
        summary["result_status"] = "partial"
    if (not nav["collection_complete"] or fx.missing or nav.get("reason_codes")) and summary["result_status"] not in {"partial", "unavailable"}:
        summary["result_status"] = "partial"
    manifest["reconciliation"] = {"status": reconciliation.get("status"), "breaks": {"record_type": "reconciliation_break", "count": len(breaks)},
                                  "historical_coverage": "unknown", "reason_codes": reconciliation.get("reason_codes", [])}
    chunks = getattr(quality, "failed_chunks", [])
    rows.extend(_row("failed_chunk", c) for c in chunks)
    source_count = 0
    for role, collection in ([('ledger', ledger)] if ledger else []) + [('positions', positions)]:
        for name, entry in collection.health.items():
            rows.append(_row("source_coverage", {"source": name, "input_role": role, "health": entry,
                "metadata": collection.source_metadata.get(name)}))
            source_count += 1
    manifest["coverage"] = {"requested_window": getattr(quality, "requested_window", {}) or {"start": start, "end": end},
        "observed_window": getattr(quality, "observed_window", {}) or {"start": local_calendar_date(positions.fetched_at), "end": local_calendar_date(positions.fetched_at)},
        "fetch_complete": quality.fetch_complete if ledger else not quality.failed_sources and not quality.truncated_sources,
        "positions_fetch_complete": bool(positions.health) and not positions.failed_sources and not positions.truncated_sources,
        "historical_coverage": "unknown", "failed_chunks": {"record_type": "failed_chunk", "count": len(chunks)},
        "source_coverage": {"record_type": "source_coverage", "count": source_count},
        "positions_observed_at": positions.fetched_at}
    manifest["exclusions"]["unavailable_sources"] = {"record_type": "source_coverage", "reason": "failed/truncated sources cannot certify missing rows"}
    manifest["summary"] = summary
    return rows, manifest
