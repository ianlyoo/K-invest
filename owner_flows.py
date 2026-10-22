"""Local owner-export cash-flow evidence; no providers or credential loaders.

Toss activity dates stay date-only. Native foreign amounts come only from the
statement's parenthesized money, not its converted KRW or book FX rate. CSV
timestamps are explicitly UTC+9. File contents and account numbers remain local.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import tempfile
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ledger_flows import precision, private_ref
from portfolio_ledger import LedgerStore, canonical, digest, number
from statement_import import MAX_FILE_BYTES, TossPdfParser, _number

PARSER_VERSION = 'owner_export_v1'
COUNTS = ('external_in', 'external_out', 'internal', 'evidence', 'non_cash', 'unknown', 'api_covered', 'duplicate')
CSV_COLUMNS = ['User ID', 'Time', 'Account', 'Operation', 'Coin', 'Change', 'Remark']
RETURN_KINDS = {'배당금입금', '외화증권배당금입금', '배당세출금', '이자입금', '외화이자입금',
                '외화이자세금출금', '대여료입금', '캐시백입금', '해외채권세금출금', '외화주식병합단주대금입금'}
NON_CASH_KINDS = {'대차거래대여출고', '대차거래상환입고', '주식병합출고', '주식병합입고', '외화채권매수', '외화채권매도'}
FX_KINDS = {'환전외화입금', '환전외화출금', '환전원화입금', '환전원화출금'}
TRADE_KINDS = {prefix + action for prefix in ('', '장내', '주식', '해외주식', '국내주식')
               for action in ('매수', '매도', '구매', '판매')}
ACTIVITY_KINDS = RETURN_KINDS | NON_CASH_KINDS | FX_KINDS | TRADE_KINDS | {'이체입금', '이체출금', '대출'}
MONEY_TOKEN = re.compile(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', re.ASCII)
FLOW_LABEL_KINDS = ('unknown', 'external_in', 'external_out')
CSV_RETURN_KINDS = {'Fee', 'Realized Profit and Loss', 'Funding Fee', 'Special Funding Fee', 'Insurance Fund Refund'}
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS ledger_owner_export_imports(
       import_id TEXT PRIMARY KEY, file_sha256 TEXT NOT NULL, parser_version TEXT NOT NULL,
       kind TEXT NOT NULL, account_identity TEXT NOT NULL, account_label TEXT NOT NULL,
       start_time TEXT NOT NULL, end_time TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS ledger_owner_export_rows(
       import_id TEXT NOT NULL REFERENCES ledger_owner_export_imports(import_id),
       event_id TEXT REFERENCES ledger_events(event_id), file_sha256 TEXT NOT NULL,
       parser_version TEXT NOT NULL, page INTEGER NOT NULL, line INTEGER NOT NULL,
       row_hash TEXT NOT NULL, classification TEXT NOT NULL, facts_json TEXT NOT NULL,
       PRIMARY KEY(import_id,page,line))''',
    '''CREATE TABLE IF NOT EXISTS ledger_owner_declarations(
       declaration_id TEXT PRIMARY KEY, account_identity TEXT NOT NULL,
       coverage_floor TEXT NOT NULL, no_activity_before TEXT NOT NULL,
       reason_code TEXT NOT NULL, declared_at TEXT NOT NULL)''',
)


def account_display_label(suffix):
    """The Toss account whose last four digits are KINVEST_TOSS_INVESTMCP_ACCOUNT_SUFFIX is the dedicated
    'InvestMCP 전용' account (NAV key toss:primary); every other Toss statement is labelled 'Toss'."""
    dedicated = os.environ.get('KINVEST_TOSS_INVESTMCP_ACCOUNT_SUFFIX', '')
    return 'InvestMCP 전용' if re.fullmatch(r'\d{4}', dedicated) and suffix == dedicated else 'Toss'


def activity_label(kind):
    """Only bounded Korean activity labels; discard parenthesized counterparties.

    Unexpected free text, numeric/date labels and names use an unknown bucket.
    These labels describe statement types, never prove flow classification.
    """
    match = re.fullmatch(r'([^()]+)(?:\([^()]*\)?)?', kind)
    label = match[1] if match else None
    if label in ACTIVITY_KINDS:
        return label
    return 'unknown'


def _money(value):
    if value not in ('-', '—') and not MONEY_TOKEN.fullmatch(value):
        raise ValueError('OWNER_EXPORT_AMOUNT_UNPARSED')
    return _number(value)


def api_windows(rows):
    """Index successful API windows once; CSV cashflows never supersede CSV."""
    allowed = {'HISTORY_RETENTION_UNVERIFIED', 'TRANSFER_SIX_MONTH_RETENTION'}
    windows = defaultdict(list)
    for row in rows:
        if row['status'] not in ('complete', 'limited'):
            continue
        if set(json.loads(row['reason_codes_json'])) - allowed:
            continue
        from ledger_returns import instant
        lo, hi = instant(row['start_time']), instant(row['end_time'])
        if lo and hi and lo <= hi:
            windows[row['stream'].split('/')[-1]].append((lo, hi))
    indexed = {}
    for leaf, spans in windows.items():
        merged = []
        for lo, hi in sorted(spans):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        indexed[leaf] = (tuple(lo for lo, _ in merged), tuple(hi for _, hi in merged))
    return indexed


def api_window_covers(windows, leaf, stamp):
    starts, ends = windows.get(leaf, ((), ()))
    position = bisect_right(starts, stamp) - 1
    return position >= 0 and stamp <= ends[position]


def api_covers(rows, leaf, stamp):
    """Fetch success proves API precedence even with a lifetime-retention warning."""
    return api_window_covers(api_windows(rows), leaf, stamp)


class TossFlowParser(TossPdfParser):
    """Reuse bounded PDF extraction, header validation and page/line assembly.

Only recognized activity kinds can establish cash-flow coverage. Unknown or
ambiguous non-trades are retained as unresolved movements, never counted as zero.
"""
    def parse_pages(self, pages, file_sha256='synthetic'):
        headers = re.findall(r'(?:계좌\s*번호|계좌번호)\s*[:：]?\s*([\d-]{8,})', '\n'.join(pages))
        if len({re.sub(r'\D', '', n) for n in headers}) != 1:
            raise ValueError('OWNER_EXPORT_ACCOUNT_HEADER_INVALID')
        self.account_reference = headers[0]
        output = super().parse_pages(pages, file_sha256)
        if output.issues:
            raise ValueError('OWNER_EXPORT_LAYOUT_AMBIGUOUS')
        self._link_fx(output.rows)
        return output

    def _amount_columns(self, row):
        """Restore only the verified blank FX and glued zero-balance shapes."""
        fields = row['header']['columns'][3:]
        values = re.findall(r'(?<!\S)(?:[+-]?[\d,]+(?:\.\d+)?|—|-)(?!\S)', row['body'])
        if row['fx_cell_blank'] and '환율' in fields:
            values.insert(fields.index('환율'), '-')
        if len(values) == len(fields) - 1 and fields[-2:] == ['잔고', '잔액']:
            glued = re.fullmatch(r'[+-]?0(\d[\d,]*(?:\.\d+)?)', values[-1])
            if glued:
                remainder = glued[1]
                canonical_number = r'(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d+)?'
                # Both balances are outside flow scope. Never guess malformed
                # grouping, and never shift the earlier settlement/fee cells.
                values[-1:] = ['0', remainder] if re.fullmatch(canonical_number, remainder) else ['-', '-']
        if len(values) != len(fields):
            raise ValueError()
        row['amount_tokens'] = dict(zip(fields, values))
        base = dict(zip(fields, map(_money, values)))
        money = [f for f in fields if f not in {'환율', '거래수량', '잔고'}]
        if row['native'] and len(row['native']) != len(money):
            raise ValueError()
        native = dict(zip(money, row['native']))
        for _, value in native.values():
            _money(value)
        return base, native

    @staticmethod
    def _link_fx(rows):
        groups = defaultdict(list)
        for row in rows:
            if row['record_kind'] == 'owner_fx_conversion':
                groups[row['activity_date']].append(row)
        for group in groups.values():
            # A date with multiple conversions has no unique pair proof. Retain
            # native evidence without guessing links or execution FX rates.
            if (len(group) == 2 and all(r['amount'] is not None and r['currency'] is not None for r in group)
                    and {r['direction'] for r in group} == {'in', 'out'} and len({r['currency'] for r in group}) == 2):
                link = digest([r['row_hash'] for r in group])
                for row in group:
                    row['facts']['fx_pair'] = link
                    row['facts']['linked_native_amounts'] = {r['currency']: r['amount'] for r in group}

    def _finish_row(self, row, output, skipped):
        kind = re.sub(r'\s', '', row['kind'])
        label = activity_label(kind)
        safe_kind = label
        external = label in ('이체입금', '이체출금')
        classification, event_kind, record_kind = 'unknown', 'transfer', 'owner_unknown_activity'
        direction = 'in' if kind.endswith('입금') else 'out' if kind.endswith('출금') else None
        if external:
            direction = 'in' if label == '이체입금' else 'out'
            classification = 'external_' + direction
            event_kind, record_kind = ('deposit' if direction == 'in' else 'withdrawal'), 'owner_statement_cashflow'
        elif label in FX_KINDS:
            classification, event_kind, record_kind = 'internal', 'corporate_action', 'owner_fx_conversion'
        elif label in RETURN_KINDS:
            classification, event_kind, record_kind = 'evidence', 'income_other', 'owner_investment_return'
        elif label in NON_CASH_KINDS or label in TRADE_KINDS:
            classification, event_kind, record_kind = 'non_cash', 'corporate_action', 'owner_non_cash'
        day = datetime.strptime(row['day'], '%Y.%m.%d').date().isoformat()
        if not output.covered_period['start'] <= day <= output.covered_period['end']:
            raise ValueError()
        amount, currency, fee = None, None, None
        amount_precision = None
        reason_codes = []
        if classification != 'non_cash':
            try:
                fields = row['header']['columns'][3:]
                base, native = self._amount_columns(row)
                currency = 'KRW'
                amounts = base
                foreign = label.startswith('환전외화') or (row['foreign'] and not label.startswith('환전원화')) or bool(row['native'])
                if foreign:
                    symbols = {c for c, _ in native.values()}
                    money = [f for f in fields if f not in {'환율', '거래수량', '잔고'}]
                    if len(symbols) != 1 or '정산금액' not in native:
                        raise ValueError()
                    currency = {'$': 'USD', '€': 'EUR', '¥': 'JPY'}[next(iter(symbols))]
                    amounts = {f: _money(native[f][1]) if f in native else None for f in money}
                amount = amounts.get('정산금액')
                fee = amounts.get('수수료')
                if amount is None or (classification in ('external_in', 'external_out') and amount == 0):
                    raise ValueError()
                reported = native['정산금액'][1] if foreign else row['amount_tokens']['정산금액']
                amount_precision = precision(reported.replace(',', ''))
                amount = number(abs(amount))
            except (ValueError, KeyError):
                if classification not in ('evidence', 'internal'):
                    classification, event_kind, record_kind = 'unknown', 'transfer', 'owner_unknown_activity'
                reason_codes = ['OWNER_EXPORT_AMOUNT_UNPARSED']
                amount, currency, fee, amount_precision = None, None, None, None
        facts = {'activity_kind': safe_kind if record_kind != 'owner_unknown_activity' else 'unknown',
                 'activity_label': label,
                 'reason_codes': reason_codes,
                 'native_amount': amount, 'native_currency': currency, 'reported_amount_precision': amount_precision, 'fx_pair': None,
                 'date_basis': 'statement activity/book date; execution time unverified'}
        output.rows.append(dict(classification=classification, kind=event_kind, record_kind=record_kind,
            activity_date=day, execution_time_utc=None, source_timezone='Asia/Seoul', timestamp_precision='date_only',
            amount=amount, amount_precision=amount_precision, currency=currency, fee=number(fee), fee_asset=currency if fee is not None else None,
            direction=direction, page=row['page'], line=row['line'], facts=facts,
            row_hash=digest([row['text'], row['native']])))


def _csv(content):
    reader = csv.reader(io.StringIO(content.decode('utf-8-sig')), strict=True)
    if next(reader, None) != CSV_COLUMNS:
        raise ValueError('OWNER_EXPORT_CSV_HEADER_INVALID')
    rows = []
    for values in reader:
        if len(values) != len(CSV_COLUMNS) or len(rows) >= 100_000:
            raise ValueError('OWNER_EXPORT_CSV_ROW_INVALID')
        _, timestamp, account, operation, currency, change, _ = values
        if account not in ('Spot', 'USD-M Futures') or not re.fullmatch(r'[A-Z0-9]{1,20}', currency):
            raise ValueError('OWNER_EXPORT_CSV_ROW_INVALID')
        stamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=datetime.fromisoformat('2000-01-01T00:00:00+09:00').tzinfo)
        try:
            signed = _money(change)
            if signed is None:
                raise ValueError('OWNER_EXPORT_AMOUNT_UNPARSED')
        except ValueError:
            signed = None
        classification, kind, leaf = 'unknown', 'transfer', 'owner_csv'
        if operation in ('Deposit', 'Withdraw') and signed and (signed > 0) == (operation == 'Deposit'):
            classification = 'external_in' if signed > 0 else 'external_out'
            kind, leaf = ('deposit', 'deposit') if signed > 0 else ('withdrawal', 'withdraw')
        elif operation == 'Transfer Between Spot and UM Futures' and signed:
            classification, kind = 'internal', 'transfer_internal'
            route = 'MAIN_UMFUTURE' if (account == 'Spot') == (signed < 0) else 'UMFUTURE_MAIN'
            leaf = 'transfer_' + route
        elif operation == 'Binance Convert':
            classification, kind = 'internal', 'corporate_action'
        elif operation in CSV_RETURN_KINDS:
            classification, kind = 'evidence', 'income_other'
        amount_precision = precision(change.replace(',', '')) if signed is not None else None
        rows.append(dict(classification=classification, kind=kind, leaf=leaf, record_kind='owner_csv',
            execution_time_utc=stamp.astimezone(timezone.utc).isoformat(), activity_date=stamp.date().isoformat(),
            source_timezone='Asia/Seoul', timestamp_precision='second', amount=number(abs(signed)) if signed is not None else None,
            amount_precision=amount_precision, currency=currency,
            wallet='MAIN' if account == 'Spot' else 'UMFUTURE', direction=('in' if signed > 0 else 'out') if signed is not None else None,
            transfer_route=leaf.removeprefix('transfer_') if kind == 'transfer_internal' else None,
            page=1, line=reader.line_num, row_hash=digest(values),
            facts={'activity_kind': operation if classification != 'unknown' else 'unknown',
                   'native_amount': number(signed), 'native_currency': currency, 'reported_amount_precision': amount_precision,
                   'reason_codes': ['OWNER_EXPORT_AMOUNT_UNPARSED'] if signed is None else []}))
    if not rows:
        raise ValueError('OWNER_EXPORT_CSV_WINDOW_UNAVAILABLE')
    return rows


def _account(store, conn, parser, parsed):
    """Resolve registry journal keys using existing ledger aliases, without secrets.

    Registry journal identity is toss:<account_id>. Header spelling and digit-only
    forms are tested against recorded identities; legacy statement HMAC bindings
    are also reused. A suffix alone never creates or remaps an account alias.
    """
    refs = ['toss:' + parser.account_reference, 'toss:' + parsed.account_number]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'statement_import_state' in tables:
        salt = conn.execute("SELECT value FROM statement_import_state WHERE key='account_salt'").fetchone()
        if salt:
            refs.append('acct_' + hmac.new(salt[0], parsed.account_number.encode(), hashlib.sha256).hexdigest()[:32])
    known = {r[0] for r in conn.execute('SELECT account_identity FROM ledger_events UNION SELECT account_identity FROM ledger_coverage UNION SELECT canonical_identity FROM ledger_account_aliases')}
    candidates = {store.account_identity(ref, conn) for ref in refs} & known
    if len(candidates) > 1:
        raise ValueError('OWNER_EXPORT_ACCOUNT_BINDING_AMBIGUOUS')
    return next(iter(candidates)) if candidates else store.account_identity(refs[0], conn)


def _read_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError('OWNER_EXPORT_FILE_INVALID')
        content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError('OWNER_EXPORT_FILE_INVALID')
        return content


def ingest_owner_export(kind, file, db_path, *, account_suffix=None, dry_run=False,
                        owner_declared_no_activity_before=None, coverage_floor=None):
    """Validate before writes; one append-only ingest transaction; counts only."""
    if kind not in ('toss_pdf', 'binance_csv'):
        raise ValueError('OWNER_EXPORT_KIND_INVALID')
    declaration = None
    if owner_declared_no_activity_before or coverage_floor:
        if kind != 'binance_csv' or not owner_declared_no_activity_before or not coverage_floor:
            raise ValueError('OWNER_DECLARATION_INVALID')
        if any(not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) for value in (coverage_floor, owner_declared_no_activity_before)):
            raise ValueError('OWNER_DECLARATION_INVALID')
        floor, before = map(date.fromisoformat, (coverage_floor, owner_declared_no_activity_before))
        if floor >= before:
            raise ValueError('OWNER_DECLARATION_INVALID')
        declaration = (floor.isoformat(), before.isoformat())
    content = _read_file(file)
    sha = hashlib.sha256(content).hexdigest()
    parser = parsed = None
    if kind == 'toss_pdf':
        parser = TossFlowParser()
        parsed = parser.parse(content)
        if not re.fullmatch(r'\d{4}', account_suffix or '') or parsed.account_number[-4:] != account_suffix:
            raise ValueError('ACCOUNT_SUFFIX_MISMATCH')
        rows = parsed.rows
        lo = parsed.covered_period['start'] + 'T00:00:00+09:00'
        hi = parsed.covered_period['end'] + 'T23:59:59.999+09:00'
    else:
        rows = _csv(content)
        if declaration and any(r['activity_date'] < declaration[1] for r in rows):
            raise ValueError('OWNER_DECLARATION_CONTRADICTED')
        # Owner-confirmed complete CSV: all activities on these export dates,
        # not an API observation and not lifetime proof before the first date.
        lo = min(r['activity_date'] for r in rows) + 'T00:00:00+09:00'
        hi = max(r['activity_date'] for r in rows) + 'T23:59:59.999+09:00'
    store = LedgerStore(db_path)
    if dry_run:
        with tempfile.TemporaryDirectory(prefix='owner-export-dry-run-') as directory:
            clone = Path(directory) / 'ledger.db'
            if store.db_path.exists():
                _clone_for_dry_run(store.db_path, clone)
            return _ingest(kind, rows, sha, lo, hi, LedgerStore(clone), parser, parsed, account_suffix, declaration,
                           include_kind_counts=kind == 'toss_pdf')
    return _ingest(kind, rows, sha, lo, hi, store, parser, parsed, account_suffix, declaration)


def _declare(store, conn, account, declaration):
    if declaration is None:
        return
    floor, before = declaration
    # "No activity" is stronger than "no deposits". Any preserved trade,
    # funding, fee or movement before first use contradicts the declaration.
    account_ids = _account_ids(conn, account)
    for row in conn.execute("SELECT payload_json FROM ledger_events WHERE source GLOB 'binance*' AND account_identity IN (SELECT value FROM json_each(?))", (canonical(account_ids),)):
        facts = json.loads(row[0])
        stamp = facts.get('execution_time_utc')
        if stamp:
            day = datetime.fromisoformat(stamp).astimezone(datetime.fromisoformat('2000-01-01T00:00:00+09:00').tzinfo).date().isoformat()
        else:
            day = facts.get('activity_date')
        if day is None or day < before:
            raise ValueError('OWNER_DECLARATION_CONTRADICTED')
    identifier = digest(['binance', account, floor, before, 'OWNER_DECLARED_NO_ACTIVITY'])
    conn.execute('INSERT OR IGNORE INTO ledger_owner_declarations VALUES (?,?,?,?,?,?)',
        (identifier, account, floor, before, 'OWNER_DECLARED_NO_ACTIVITY', datetime.now(timezone.utc).isoformat()))
    end = (date.fromisoformat(before) - timedelta(days=1)).isoformat() + 'T23:59:59.999+09:00'
    store.coverage(conn, 'binance', account, 'binance/cashflows', floor + 'T00:00:00+09:00', end,
                   'limited', ['OWNER_DECLARED_NO_ACTIVITY'], 0, identifier)


def _clone_for_dry_run(source, clone):
    """SQLite opens only the temporary copy, including uncheckpointed WAL data.

    Refuse concurrent changes instead of using an inconsistent file snapshot.
    The target main file and journals are read as bytes, never opened by SQLite.
    """
    paths = [source, Path(str(source) + '-wal'), Path(str(source) + '-journal')]

    def versions():
        result = {}
        for path in paths:
            try:
                info = path.stat()
                result[path] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except FileNotFoundError:
                pass
        return result

    before = versions()
    try:
        for path in before:
            shutil.copyfile(path, Path(str(clone) + str(path)[len(str(source)):]))
    except FileNotFoundError:
        raise ValueError('OWNER_EXPORT_DRY_RUN_SNAPSHOT_CHANGED') from None
    if versions() != before:
        raise ValueError('OWNER_EXPORT_DRY_RUN_SNAPSHOT_CHANGED')


def _api_counterparts(store, conn, account):
    from ledger_returns import counterpart_key
    keys = set()
    for record in conn.execute('''SELECT event_id FROM ledger_events
            WHERE source IN ('binance_deposit','binance_withdraw')
            AND account_identity IN (SELECT value FROM json_each(?))
            AND coalesce(json_extract(payload_json,'$.record_kind'),'')!='owner_csv' ''',
            (canonical(_account_ids(conn, account)),)):
        event = store.effective_event(conn, record[0])
        key = counterpart_key(event, account)
        if key is not None:
            keys.add(key)
    return keys


def _account_ids(conn, account):
    return sorted({account} | {r[0] for r in conn.execute('SELECT alias_identity FROM ledger_account_aliases WHERE canonical_identity=?', (account,))})


def _ingest(kind, rows, sha, lo, hi, store, parser, parsed, suffix, declaration, *, include_kind_counts=False):
    counts = Counter(dict.fromkeys(COUNTS, 0))
    by_kind = {classification: Counter() for classification in FLOW_LABEL_KINDS}

    def report():
        return dict(counts, **{k + '_by_kind': dict(v) for k, v in by_kind.items()}) if include_kind_counts else dict(counts)

    with store.transaction() as conn:
        for sql in SCHEMA:
            conn.execute(sql)
        for table in ('ledger_owner_export_imports', 'ledger_owner_export_rows', 'ledger_owner_declarations'):
            for action in ('UPDATE', 'DELETE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                    BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'ledger records are immutable'); END''')
        account = _account(store, conn, parser, parsed) if kind == 'toss_pdf' else store.account_identity('binance:primary', conn)
        import_id = digest([kind, sha, PARSER_VERSION, account])
        exists = conn.execute('''SELECT 1 FROM ledger_owner_export_imports WHERE kind=? AND file_sha256=?
            AND account_identity IN (SELECT value FROM json_each(?))''',
            (kind, sha, canonical(_account_ids(conn, account)))).fetchone()
        if not exists:
            conn.execute('''CREATE INDEX IF NOT EXISTS ledger_owner_export_window
                ON ledger_owner_export_imports(kind,account_identity,start_time,end_time)''')
            if conn.execute('''SELECT 1 FROM ledger_owner_export_imports WHERE kind=?
                    AND account_identity IN (SELECT value FROM json_each(?))
                    AND julianday(start_time)<=julianday(?) AND julianday(end_time)>=julianday(?) LIMIT 1''',
                    (kind, canonical(_account_ids(conn, account)), hi, lo)).fetchone():
                raise ValueError('OWNER_EXPORT_OVERLAP_UNRECONCILED')
        _declare(store, conn, account, declaration)
        if exists:
            return report()
        conn.execute('INSERT INTO ledger_owner_export_imports VALUES (?,?,?,?,?,?,?,?)',
            (import_id, sha, PARSER_VERSION, kind, account, account_display_label(suffix) if kind == 'toss_pdf' else 'Binance', lo, hi))
        api = [dict(r) for r in conn.execute("SELECT * FROM ledger_coverage WHERE source='binance' AND account_identity IN (SELECT value FROM json_each(?))", (canonical(_account_ids(conn, account)),))] if kind == 'binance_csv' else []
        api = api_windows(api)
        counterparts = _api_counterparts(store, conn, account) if kind == 'binance_csv' else set()
        from ledger_returns import counterpart_key
        for row in rows:
            classification = row['classification']
            event_id = None
            if kind == 'binance_csv' and (api_window_covers(api, row['leaf'], datetime.fromisoformat(row['execution_time_utc']))
                    or counterpart_key({**row, 'source': 'binance_' + row['leaf']}, account) in counterparts):
                classification = 'api_covered'
            else:
                source = 'toss_cashflows' if kind == 'toss_pdf' else 'binance_transfers' if row['transfer_route'] else 'binance_' + row['leaf']
                event = {**row, 'source': source, 'account_identity': account,
                    'source_record_id': 'owner:' + import_id + ':' + str(row['page']) + ':' + str(row['line']),
                    'identity_quality': 'statement_row', 'source_status': 'completed' if kind == 'toss_pdf' or row['leaf'] == 'owner_csv' else 'CONFIRMED' if row['transfer_route'] else '1' if row['leaf'] == 'deposit' else '6',
                    'amount_precision': row['amount_precision'], 'venue': 'toss' if kind == 'toss_pdf' else 'binance'}
                if kind == 'binance_csv' and row['transfer_route']:
                    event['transaction_ref'] = private_ref(store, conn, digest([row['execution_time_utc'], row['currency'], row['amount'], row['transfer_route']]))
                plan = store.prepare(conn, event)
                result = store.append(conn, event, dict(file_sha256=sha, run_id=import_id,
                    page_path='page-' + str(row['page']), row_index=row['line'], raw_store_ref='owner-export:' + sha), prepared=plan)
                event_id = plan['event_id']
                stored = store.effective_event(conn, event_id)
                if stored is None or stored['kind'] != event['kind'] or stored['source'] != event['source']:
                    raise ValueError('OWNER_EXPORT_EVENT_IDENTITY_MISMATCH')
                if row['kind'] in ('deposit', 'withdrawal', 'transfer', 'transfer_internal'):
                    store.classify_transfer(conn, event_id, classification if classification in ('external_in', 'external_out') else 'unknown',
                        'OWNER_EXPORT_UNKNOWN_ACTIVITY' if classification == 'unknown' else 'OWNER_EXPORT_REPORTED_EXTERNAL')
                if result == 'duplicate':
                    counts['duplicate'] += 1
            counts[classification] += 1
            if include_kind_counts and classification in by_kind:
                by_kind[classification][row['facts']['activity_label']] += 1
            conn.execute('INSERT INTO ledger_owner_export_rows VALUES (?,?,?,?,?,?,?,?,?)',
                (import_id, event_id, sha, PARSER_VERSION, row['page'], row['line'], row['row_hash'], classification, canonical(row['facts'])))
        unknown = counts['unknown'] > 0
        reasons = ['OWNER_EXPORT_UNKNOWN_ACTIVITY'] if unknown else []
        source, leaf = ('toss', 'cashflows') if kind == 'toss_pdf' else ('binance', 'cashflows')
        if kind == 'binance_csv':
            reasons.append('OWNER_EXPORT_COMPLETE_HISTORY')
        store.coverage(conn, source, account, source + '/' + leaf, lo, hi,
            'partial' if unknown else 'limited' if reasons else 'complete', reasons, len(rows), import_id)
        store.match_transfers(conn=conn)
    return report()
