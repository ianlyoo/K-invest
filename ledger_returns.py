"""Read-only ledger flow eligibility for the existing performance tools.

Only local ledger identity state is used to map NAV account keys; no provider
credentials, ingestion or NAV writer is used here.
Owner exports record per-account ``cashflows`` coverage under source toss or
binance; KIS remains uncovered. Trade history is never cash-flow coverage.
Owner-declared pre-activity windows are audited separately from observed data.
"""
import json
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_nav import CONSOLIDATED_NAV_BASES
from portfolio_export import _Privacy
from portfolio_ledger import (
    CONFLICT_TIME_SQL,
    ENRICHMENT_TIME_SQL,
    FLOW_KINDS,
    FLOW_STREAM_SQL,
    FLOW_TIME_SQL,
    coverage_time_sql,
)

KST = ZoneInfo('Asia/Seoul')
READ_BUDGET_SECONDS = 0.5
MAX_READ_ROWS = 2000
# The dedicated 'InvestMCP 전용' Toss account (owner_flows.account_display_label) is the NAV key toss:primary.
INVESTMCP_TOSS_ACCOUNT_KEY = 'toss:primary'
SCOPE_VENUES = {'equity': ('toss', 'kis'), 'consolidated': ('toss', 'kis', 'binance', 'hyperliquid'),
                'toss': ('toss',), 'toss_investmcp': ('toss',), 'crypto': ('binance', 'hyperliquid'),
                'kis': ('kis',)}
# KIS scope points (kis_nav_capture): a backstop on the derived price time. The capture
# already requires every price to equal its market's latest close; this only rejects
# an implausibly old close (calendar-free: a market holiday longer than this nulls the day).
KIS_CAPTURE_VERSION = 'kis_ledgerless_v1'
KIS_MAX_PRICE_AGE = timedelta(days=7)
STREAMS = {
    'toss': {'cashflows'}, 'kis': {'cashflows'},
    'binance': {'cashflows', 'deposit', 'withdraw', 'transfer_MAIN_UMFUTURE', 'transfer_UMFUTURE_MAIN',
                'transfer_MAIN_FUNDING', 'transfer_FUNDING_MAIN',
                'transfer_FUNDING_UMFUTURE', 'transfer_UMFUTURE_FUNDING'},
    'hyperliquid': {'nonFundingLedger', 'userNonFundingLedgerUpdates'},
}


class ReadBudgetExceeded(Exception):
    pass


class Reader:
    def __init__(self, conn, deadline):
        self.conn, self.deadline = conn, deadline
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise ReadBudgetExceeded

    def rows(self, sql, params=()):
        self.check()
        rows = self.conn.execute(sql, params).fetchmany(MAX_READ_ROWS + 1)
        self.check()
        if len(rows) > MAX_READ_ROWS:
            raise ReadBudgetExceeded
        return rows


def _ids(values):
    return json.dumps(sorted(set(values)))


def _scope_sql(venues, prefix=''):
    column = prefix + 'source'
    clauses, params = [], []
    for provider in venues:
        clauses.append(f'({column}=? OR {column} GLOB ?)')
        params.extend((provider, provider + '_*'))
    if 'toss' in venues:
        clauses.append(f'{column}=?')
        params.append('statement_toss')
    return '(' + ' OR '.join(clauses) + ')', params


def _roster(reader, tables, navs, scope, venues):
    references, errors = {}, []
    for nav in navs:
        reader.check()
        equity = nav.get('account_nav') or {}
        if not equity and scope != 'crypto':
            errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
        for reference in equity:
            provider = reference.split(':')[0]
            if provider not in venues:
                if scope in ('equity', 'consolidated') and provider not in ('toss', 'kis'):
                    errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
                continue
            if provider not in ('toss', 'kis'):
                errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
            else:
                references.setdefault(reference, {'venue': provider, 'wallets': None, 'assets': set()})
        if scope in ('consolidated', 'crypto'):
            if scope == 'consolidated' and (nav.get('consolidated_nav_krw') or {}).get('basis') not in CONSOLIDATED_NAV_BASES:
                errors.append('FLOW_NAV_BOUNDARY_UNKNOWN')
            for provider, expected_scope, wallet in (('binance', 'futures_collateral', 'UMFUTURE'), ('hyperliquid', 'perp_dex', 'perp')):
                rows = ((nav.get('crypto_nav') or {}).get(provider) or {}).get('accounts') or []
                if not rows:
                    errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
                for row in rows:
                    reference = row.get('account_id')
                    if not reference or row.get('scope') != expected_scope or row.get('dex') != '' or not row.get('asset'):
                        errors.append('FLOW_NAV_BOUNDARY_UNKNOWN')
                        continue
                    facts = references.setdefault(reference, {'venue': provider, 'wallets': {wallet}, 'assets': set()})
                    if facts['venue'] != provider:
                        errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
                    facts['assets'].add(row['asset'])
    if len(references) > 256:
        raise ReadBudgetExceeded
    state = reader.rows("SELECT value FROM ledger_state WHERE key='account_salt'") if 'ledger_state' in tables else []
    salt = state[0][0] if state else None
    if not isinstance(salt, bytes) or len(salt) != 32:
        return {}, {}, {}, ['ACCOUNT_FLOW_COVERAGE_MISSING']
    privacy = _Privacy(salt, [])
    identities = {reference: privacy.opaque(reference) for reference in references}
    aliases = dict(reader.rows('SELECT alias_identity,canonical_identity FROM ledger_account_aliases WHERE alias_identity IN (SELECT value FROM json_each(?))',
                               (_ids(identities.values()),))) if 'ledger_account_aliases' in tables else {}
    accounts, boundary = {}, {}
    for reference, facts in references.items():
        identity = identities[reference]
        # Risk IDs use a different salt/namespace. Require a recorded link, never
        # guess their ledger account from the provider or number of observations.
        if facts['venue'] not in ('toss', 'kis') and identity not in aliases:
            errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
            continue
        identity = aliases.get(identity, identity)
        accounts.setdefault(facts['venue'], set()).add(identity)
        if identity in boundary and boundary[identity]['venue'] != facts['venue']:
            errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
        else:
            boundary.setdefault(identity, facts)['assets'].update(facts['assets'])
    if 'ledger_account_aliases' in tables and boundary:
        aliases.update(reader.rows('SELECT alias_identity,canonical_identity FROM ledger_account_aliases WHERE canonical_identity IN (SELECT value FROM json_each(?))',
                                  (_ids(boundary),)))
    for provider in venues:
        if not accounts.get(provider):
            errors.append('ACCOUNT_FLOW_COVERAGE_MISSING')
    return accounts, boundary, aliases, list(dict.fromkeys(errors))


def _inside(row, boundary, aliases):
    account = aliases.get(row.get('account_identity'), row.get('account_identity'))
    facts = boundary.get(account)
    if facts is None:
        return False
    if venue(row.get('source', '')) != facts['venue']:
        return None
    if facts['wallets'] is None:
        return True
    if row.get('wallet') in facts['wallets']:
        return True if row.get('currency') in facts['assets'] else None
    if row.get('wallet') in ({'MAIN', 'FUNDING'} if facts['venue'] == 'binance' else {'spot'}):
        return False
    return None


def _event_day(row):
    when = instant(row.get('execution_time_utc'))
    day = when.astimezone(KST).date().isoformat() if when else row.get('activity_date')
    try:
        return date.fromisoformat(day).isoformat()
    except (ValueError, TypeError):
        return None


def _effective(reader, tables, records):
    events = {r['event_id']: json.loads(r['payload_json']) for r in records}
    keys = _ids(events)
    if 'ledger_enrichments' in tables:
        for row in reader.rows('SELECT event_id,fields_json FROM ledger_enrichments WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY rowid', (keys,)):
            events[row[0]].update(json.loads(row[1]))
    if 'ledger_status_revisions' in tables:
        for row in reader.rows('''SELECT s.event_id,s.effective_status FROM json_each(?) ids
            JOIN ledger_status_revisions s ON s.event_id=ids.value AND s.revision=(
                SELECT max(revision) FROM ledger_status_revisions WHERE event_id=ids.value)''', (keys,)):
            events[row[0]]['source_status'] = row[1]
    classifications = dict(reader.rows('''SELECT c.event_id,c.classification FROM json_each(?) ids
        JOIN ledger_transfer_classifications c ON c.event_id=ids.value AND c.revision=(
            SELECT max(revision) FROM ledger_transfer_classifications WHERE event_id=ids.value)''', (keys,))) if 'ledger_transfer_classifications' in tables else {}
    conflicts = {}
    if 'ledger_conflicts' in tables:
        for row in reader.rows('SELECT event_id,candidate_json FROM ledger_conflicts WHERE event_id IN (SELECT value FROM json_each(?))', (keys,)):
            conflicts.setdefault(row[0], []).append(json.loads(row[1]))
    return events, classifications, conflicts


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def instant(value):
    try:
        if str(value).isdigit():
            return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def counterpart_key(event, account):
    """An exact native counterpart, indexed once per ingest/read transaction."""
    stamp, amount = instant(event.get('execution_time_utc')), number(event.get('amount'))
    if stamp is None or amount is None or not event.get('currency'):
        return None
    return (event['source'], account, event.get('wallet'), event['currency'], amount, stamp)


def venue(source):
    return 'toss' if source == 'statement_toss' else source.split('_')[0]


def _completed(row):
    expected = {'binance_deposit': {'1'}, 'binance_withdraw': {'6'}}.get(row['source'], {'CONFIRMED', 'COMPLETED', 'SUCCESS'})
    return str(row.get('source_status')).upper() in expected


def _days(start, end):
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def _windows(days):
    windows = []
    for day in sorted(days):
        if windows and date.fromisoformat(day) == date.fromisoformat(windows[-1]['end']) + timedelta(days=1):
            windows[-1]['end'] = day
        else:
            windows.append({'start': day, 'end': day})
    return windows


def _merge(rows):
    intervals = []
    for row in rows:
        lo, hi = instant(row['start_time']), instant(row['end_time'])
        observed = row['status'] == 'complete' and not json.loads(row['reason_codes_json'])
        if (observed or row.get('_owner_verified')) and lo and hi and hi >= lo:
            intervals.append((int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)))
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def _coverage(rows, accounts, venues, start, end, reader):
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 36600:
        raise ReadBudgetExceeded
    days = set(_days(start, end))
    covered = set(days)
    before_coverage = False
    requested_start = int(datetime.fromisoformat(start + 'T00:00:00+09:00').timestamp() * 1000)
    for provider in venues:
        identities = accounts.get(provider) or {None}
        for account in identities:
            required = STREAMS[provider]
            # Both names denote the same HL stream, not two independent feeds.
            groups = [required] if provider == 'hyperliquid' else [{leaf} for leaf in required if provider != 'binance' or leaf != 'cashflows']
            for leaves in groups:
                intervals = _merge([r for r in rows if r['source'] == provider
                    and r['account_identity'] == account and (r['stream'].split('/')[-1] in leaves
                        or (provider == 'binance' and r['stream'].split('/')[-1] == 'cashflows'))])
                before_coverage |= bool(intervals and intervals[0][0] > requested_start)
                complete = set()
                for day in covered:
                    reader.check()
                    lo = int(datetime.fromisoformat(day + 'T00:00:00+09:00').timestamp() * 1000)
                    hi = lo + 86_400_000 - 1
                    if any(a <= lo and b >= hi for a, b in intervals):
                        complete.add(day)
                covered &= complete
    gaps = _windows(days - covered)
    reasons = ['FLOW_COVERAGE_GAP'] if gaps else []
    if before_coverage or (covered and min(covered) > start):
        reasons.append('PERIOD_BEFORE_FLOW_COVERAGE')
    elif not covered:
        complete_starts = [instant(r['start_time']) for r in rows
                           if r['source'] in venues and r['status'] == 'complete']
        if complete_starts and all(t and t.astimezone(KST).date().isoformat() > start for t in complete_starts):
            reasons.append('PERIOD_BEFORE_FLOW_COVERAGE')
    return covered, gaps, reasons


def _read(reader, start, end, scope, navs):
    tables = {r[0] for r in reader.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    if scope == 'toss_investmcp':
        navs = _investmcp_navs(reader, tables, navs)
    venues = SCOPE_VENUES[scope]
    source_sql, source_params = _scope_sql(venues)
    kinds = tuple(sorted(FLOW_KINDS))
    kind_sql = ','.join('?' for _ in kinds)
    if scope == 'equity':
        observed = reader.rows(f'SELECT 1 FROM ledger_events WHERE kind IN ({kind_sql}) AND {source_sql} LIMIT 1',
                               (*kinds, *source_params)) if 'ledger_events' in tables else []
        coverage_observed = reader.rows("SELECT 1 FROM ledger_coverage WHERE source IN ('toss','kis') AND stream GLOB '*/cashflows' LIMIT 1") if 'ledger_coverage' in tables else []
        if not observed and not coverage_observed:
            return None  # Older fixtures without an equity flow source stay identical.
    accounts, boundary, aliases, roster_reasons = _roster(reader, tables, navs, scope, venues)
    account_ids = set(boundary) | {a for a, c in aliases.items() if c in boundary}
    ids_json = _ids(account_ids)
    lo = datetime.fromisoformat(start + 'T00:00:00+09:00').timestamp() / 86400 + 2440587.5
    stop = (datetime.fromisoformat(end + 'T00:00:00+09:00') + timedelta(days=1)).timestamp() / 86400 + 2440587.5
    # SQLite rounds Julian timestamps to milliseconds; include a guard band,
    # then decide exact KST dates in Python so boundary microseconds survive.
    window_lo, window_stop = lo - 1 / 86400000, stop + 1 / 86400000
    records = reader.rows(f'''SELECT event_id,payload_json FROM ledger_events
        WHERE kind IN ({kind_sql}) AND {source_sql}
          AND account_identity IN (SELECT value FROM json_each(?))
          AND (({FLOW_TIME_SQL} >= ? AND {FLOW_TIME_SQL} < ?) OR {FLOW_TIME_SQL} IS NULL)
        LIMIT ?''', (*kinds, *source_params, ids_json, window_lo, window_stop, MAX_READ_ROWS + 1)) if 'ledger_events' in tables else []
    # Candidate dates can bring an otherwise out-of-window original into scope.
    # The expression index avoids scanning old conflicts to discover those IDs.
    candidate_ids = []
    if 'ledger_conflicts' in tables and 'ledger_events' in tables:
        scoped, params = _scope_sql(venues, 'e.')
        candidate_ids = [r[0] for r in reader.rows(f'''SELECT DISTINCT c.event_id
            FROM ledger_conflicts c JOIN ledger_events e ON e.event_id=c.event_id
            WHERE e.kind IN ({kind_sql}) AND {scoped}
              AND e.account_identity IN (SELECT value FROM json_each(?))
              AND (({CONFLICT_TIME_SQL} >= ? AND {CONFLICT_TIME_SQL} < ?) OR {CONFLICT_TIME_SQL} IS NULL)
            LIMIT ?''', (*kinds, *params, ids_json, window_lo, window_stop, MAX_READ_ROWS + 1))]
    if 'ledger_enrichments' in tables and 'ledger_events' in tables:
        scoped, params = _scope_sql(venues, 'e.')
        candidate_ids.extend(r[0] for r in reader.rows(f'''SELECT DISTINCT c.event_id
            FROM ledger_enrichments c JOIN ledger_events e ON e.event_id=c.event_id
            WHERE e.kind IN ({kind_sql}) AND {scoped}
              AND e.account_identity IN (SELECT value FROM json_each(?))
              AND e.execution_time_utc IS NULL
              AND {ENRICHMENT_TIME_SQL} >= ? AND {ENRICHMENT_TIME_SQL} < ?
            LIMIT ?''', (*kinds, *params, ids_json, window_lo, window_stop, MAX_READ_ROWS + 1)))
    missing = set(candidate_ids) - {r['event_id'] for r in records}
    if missing:
        records += reader.rows('SELECT event_id,payload_json FROM ledger_events WHERE event_id IN (SELECT value FROM json_each(?))', (_ids(missing),))
    if len(records) > MAX_READ_ROWS:
        raise ReadBudgetExceeded
    selected = {r['event_id'] for r in records}
    pairs = reader.rows('''SELECT out_event_id,in_event_id FROM ledger_transfer_matches
        WHERE out_event_id IN (SELECT value FROM json_each(?)) UNION
        SELECT out_event_id,in_event_id FROM ledger_transfer_matches
        WHERE in_event_id IN (SELECT value FROM json_each(?))''', (_ids(selected), _ids(selected))) if selected and 'ledger_transfer_matches' in tables else []
    peers = {key for pair in pairs for key in pair} - selected
    if peers:
        records += reader.rows('SELECT event_id,payload_json FROM ledger_events WHERE event_id IN (SELECT value FROM json_each(?))', (_ids(peers),))
    if len(records) > MAX_READ_ROWS:
        raise ReadBudgetExceeded
    events, classifications, conflicts = _effective(reader, tables, records) if records else ({}, {}, {})
    matched = {}
    for out_id, in_id in pairs:
        if (out_id in events and in_id in events and out_id not in conflicts and in_id not in conflicts
                and classifications.get(out_id) == classifications.get(in_id) == 'internal'
                and _completed(events[out_id]) and _completed(events[in_id])):
            matched[out_id], matched[in_id] = in_id, out_id
    placeholders = ','.join('?' for _ in venues)
    coverage_rows = [dict(r) for r in reader.rows(f'''SELECT source,account_identity,stream,start_time,end_time,status,reason_codes_json,run_id
        FROM ledger_coverage WHERE source IN ({placeholders})
          AND account_identity IN (SELECT value FROM json_each(?))
          AND {coverage_time_sql('end_time')} >= ? AND {coverage_time_sql('start_time')} < ?
        LIMIT ?''', (*venues, ids_json, lo, stop, MAX_READ_ROWS + 1))] if 'ledger_coverage' in tables else []
    relevant = [r for r in coverage_rows if r['stream'].split('/')[-1] in STREAMS[r['source']]]
    declarations, owner_windows, labels, declaration_errors = [], [], {}, []
    imports = {r['import_id']: dict(r) for r in reader.rows('SELECT * FROM ledger_owner_export_imports WHERE account_identity IN (SELECT value FROM json_each(?))',
                (ids_json,))} if 'ledger_owner_export_imports' in tables else {}
    declaration_rows = {r['declaration_id']: dict(r) for r in reader.rows('SELECT * FROM ledger_owner_declarations WHERE account_identity IN (SELECT value FROM json_each(?))',
                (ids_json,))} if 'ledger_owner_declarations' in tables else {}
    for row in relevant:
        codes = json.loads(row['reason_codes_json'])
        proof = imports.get(row['run_id'])
        declaration = declaration_rows.get(row['run_id'])
        if (row['source'] == 'binance' and row['stream'] == 'binance/cashflows' and row['status'] == 'limited'
                and proof and proof['kind'] == 'binance_csv' and codes == ['OWNER_EXPORT_COMPLETE_HISTORY']
                and proof['account_identity'] == row['account_identity'] and proof['start_time'] == row['start_time'] and proof['end_time'] == row['end_time']):
            row['_owner_verified'] = True
            owner_windows.append({'venue': 'binance', 'start': row['start_time'], 'end': row['end_time'],
                                  'status': 'owner_export', 'reason_codes': codes})
        if (row['source'] == 'binance' and row['stream'] == 'binance/cashflows' and row['status'] == 'limited'
                and declaration and codes == ['OWNER_DECLARED_NO_ACTIVITY']
                and declaration['account_identity'] == row['account_identity']
                and row['start_time'] == declaration['coverage_floor'] + 'T00:00:00+09:00'
                and instant(row['end_time']) == instant(declaration['no_activity_before'] + 'T00:00:00+09:00') - timedelta(milliseconds=1)):
            prior = reader.rows(f'''SELECT 1 FROM ledger_events
                WHERE source GLOB 'binance*' AND account_identity IN (SELECT value FROM json_each(?))
                  AND ({FLOW_TIME_SQL} < julianday(?) OR {FLOW_TIME_SQL} IS NULL) LIMIT 1''',
                (_ids({row['account_identity']} | {a for a, c in aliases.items() if c == row['account_identity']}),
                 declaration['no_activity_before'] + 'T00:00:00+09:00'))
            row['_owner_verified'] = not prior
            if prior:
                declaration_errors.append('OWNER_DECLARATION_CONTRADICTED')
            declarations.append({'venue': 'binance', 'coverage_floor': declaration['coverage_floor'],
                                 'no_activity_before': declaration['no_activity_before'],
                                 'status': 'contradicted' if prior else 'owner_declared',
                                 'reason_codes': codes + (['OWNER_DECLARATION_CONTRADICTED'] if prior else [])})
        if proof:
            labels.setdefault(row['source'], set()).add(proof['account_label'])
        row['account_identity'] = aliases.get(row['account_identity'], row['account_identity'])
    covered, gaps, reasons = _coverage(relevant, accounts, venues, start, end, reader)
    if gaps and 'ledger_coverage' in tables:
        # Earliest proof is scalar metadata, not a future-window scan. Each
        # source/account/stream prefix seeks the endpoint of its partial index.
        requests = [{'source': provider, 'account': physical, 'stream': leaf}
                    for provider, identities in accounts.items() for account in identities
                    for physical in ({account} | {a for a, c in aliases.items() if c == account})
                    for leaf in STREAMS[provider]]
        first_proofs = reader.rows(f'''SELECT json_extract(r.value,'$.source') AS source,
            json_extract(r.value,'$.account') AS account, json_extract(r.value,'$.stream') AS stream,
            (SELECT {coverage_time_sql('start_time')} FROM ledger_coverage
             WHERE source=json_extract(r.value,'$.source') AND account_identity=json_extract(r.value,'$.account')
               AND {FLOW_STREAM_SQL}=json_extract(r.value,'$.stream')
               AND status='complete' AND reason_codes_json='[]'
             ORDER BY {coverage_time_sql('start_time')} LIMIT 1) AS first_start
            FROM json_each(?) r''', (json.dumps(requests),))
        earliest = {}
        for row in first_proofs:
            if row['first_start'] is not None:
                leaf = 'hl_flows' if row['source'] == 'hyperliquid' else row['stream']
                key = (row['source'], aliases.get(row['account'], row['account']), leaf)
                earliest[key] = min(earliest.get(key, row['first_start']), row['first_start'])
        if any(first > lo for first in earliest.values()):
            reasons.append('PERIOD_BEFORE_FLOW_COVERAGE')
    reasons += roster_reasons
    reasons += declaration_errors
    for row in relevant:
        if row['status'] == 'partial':
            reasons.extend(code for code in json.loads(row['reason_codes_json']) if code.startswith('OWNER_EXPORT_'))
    if any(not any(r['source'] == provider and r['account_identity'] == account for r in relevant)
           for provider, identities in accounts.items() for account in identities):
        reasons.append('ACCOUNT_FLOW_COVERAGE_MISSING')
    flows, deposits, withdrawals = {}, Decimal(0), Decimal(0)
    bad_days = {}
    from owner_flows import api_window_covers, api_windows
    native_counterparts = {counterpart_key(e, aliases.get(e['account_identity'], e['account_identity']))
                           for e in events.values() if e.get('record_kind') != 'owner_csv'} - {None}
    api_by_account = {}
    for row in relevant:
        if row['source'] == 'binance':
            api_by_account.setdefault(row['account_identity'], []).append(row)
    api_by_account = {key: api_windows(rows) for key, rows in api_by_account.items()}
    for event_id in sorted(selected):
        reader.check()
        row = events[event_id]
        if row.get('record_kind') == 'owner_csv':
            leaf = 'transfer_' + row['transfer_route'] if row.get('transfer_route') else 'deposit' if row['kind'] == 'deposit' else 'withdraw'
            stamp = instant(row.get('execution_time_utc'))
            account = aliases.get(row['account_identity'], row['account_identity'])
            if stamp and api_window_covers(api_by_account.get(account, {}), leaf, stamp):
                continue  # Later API coverage also supersedes old CSV evidence.
            if counterpart_key(row, account) in native_counterparts:
                continue
        day = _event_day(row)
        included = _inside(row, boundary, aliases)
        # Inspect all conflicting timestamp candidates BEFORE excluding the
        # original's date or wallet. Unknown candidate dates remain possible.
        if event_id in conflicts:
            possibilities = [row, *conflicts[event_id]]
            affected = [_event_day(candidate) for candidate in possibilities
                        if _inside(candidate, boundary, aliases) is not False
                        and (_event_day(candidate) is None or start <= _event_day(candidate) <= end)]
            if affected:
                reasons.append('CONFLICTED_FLOWS')
                bad_days.setdefault(min(d or start for d in affected), []).append('CONFLICTED_FLOWS')
                continue
        if included is False or (day and not start <= day <= end):
            continue
        errors = []
        if included is None:
            errors.append('FLOW_NAV_BOUNDARY_UNKNOWN')
        if not day or (instant(row.get('execution_time_utc')) is None and row.get('source_timezone') != 'Asia/Seoul'):
            errors.append('MISSING_FLOW_DATE')
            day = start
        classification = classifications.get(event_id, 'unknown')
        if not _completed(row):
            errors.append('INCOMPLETE_FLOW_STATUS')
        if classification == 'internal' and event_id in matched and not errors:
            peer_inside = _inside(events[matched[event_id]], boundary, aliases)
            if peer_inside is True:
                continue
            if peer_inside is None or row.get('direction') not in ('in', 'out'):
                errors.append('FLOW_NAV_BOUNDARY_UNKNOWN')
            else:
                # This is external to NAV's wallet cut, even though the durable
                # match is internal to the owner's wider account boundary.
                classification = 'external_' + row['direction']
        if classification not in ('external_in', 'external_out'):
            errors.append('UNCLASSIFIED_FLOWS')
        elif row.get('direction') not in (None, 'in' if classification == 'external_in' else 'out'):
            errors.append('FLOW_DIRECTION_CONFLICT')
        amount = number(row.get('amount'))
        if amount is None or amount <= 0:
            errors.append('MISSING_FLOW_AMOUNT')
        if not row.get('currency'):
            errors.append('MISSING_FLOW_CURRENCY')
        rate = Decimal(1) if row.get('currency') == 'KRW' else number(row.get('fx_rate'))
        if rate is None:
            for nav in navs:
                mark = (nav.get('fx_times') or {}).get(row.get('currency'), {})
                priced = instant(mark.get('priced_at'))
                if nav.get('snapshot_date') == day and priced and priced.astimezone(KST).date().isoformat() == day:
                    rate = number(mark.get('rate_to_krw'))
                    break
        if rate is None or rate <= 0:
            errors.append('MISSING_FLOW_FX')
        if errors:
            reasons.extend(errors)
            bad_days.setdefault(day, []).extend(errors)
            continue
        amount *= rate
        if classification == 'external_in':
            deposits += amount
        else:
            withdrawals += amount
            amount = -amount
        flows[day] = flows.get(day, Decimal(0)) + amount
    reasons = list(dict.fromkeys(reasons))
    first_gap = dict(gaps[0], reason_codes=['FLOW_COVERAGE_GAP']) if gaps else None
    for day, errors in sorted(bad_days.items()):
        if first_gap is None or day < first_gap['start']:
            first_gap = {'start': day, 'end': day, 'reason_codes': list(dict.fromkeys(errors))}
        elif first_gap['start'] <= day <= first_gap['end']:
            first_gap['reason_codes'] = list(dict.fromkeys(first_gap['reason_codes'] + errors))
    if roster_reasons or 'ACCOUNT_FLOW_COVERAGE_MISSING' in reasons:
        first_gap = {'start': start, 'end': end, 'reason_codes': reasons}
        covered = set()
    windows = _windows(covered - bad_days.keys())
    if first_gap and 'PERIOD_BEFORE_FLOW_COVERAGE' in reasons:
        first_gap['reason_codes'] = list(dict.fromkeys(first_gap['reason_codes'] + ['PERIOD_BEFORE_FLOW_COVERAGE']))
    revision = {table: reader.rows(f'SELECT coalesce(max(rowid),0) FROM ledger_{table}')[0][0]
                for table in ('events', 'enrichments', 'status_revisions', 'conflicts', 'coverage', 'transfer_classifications', 'transfer_matches', 'account_aliases')
                if 'ledger_' + table in tables}
    evidence = {'status': 'unavailable' if reasons else 'complete', 'source': 'permanent_ledger',
        'scope': scope, 'venues': list(venues), 'start': start, 'end': end,
        'valuation_timezone': 'Asia/Seoul', 'reason_codes': reasons,
        'covered_window': windows[0] if windows else None, 'covered_windows': windows,
        'first_uncovered_gap': first_gap, 'ledger_revision': revision}
    if declarations or owner_windows:
        evidence.update(owner_declarations=declarations, owner_export_windows=owner_windows,
                        coverage_basis='observed_exports_and_explicit_owner_declarations')
    if labels:
        evidence['account_labels'] = {provider: sorted(values) for provider, values in labels.items()}
    reader.check()
    return {'coverage': evidence, '_nav_accounts': sorted({a for nav in navs for a in (nav.get('account_nav') or {})}),
            'flows_by_date': None if reasons else {d: float(a) for d, a in flows.items()},
            'observed_deposits_krw': float(deposits), 'observed_withdrawals_krw': float(withdrawals)}


def _investmcp_identity(reader, tables):
    selected, aliases, privacy = set(), {}, None
    if {'ledger_owner_export_imports', 'ledger_state'} <= tables:
        selected = {r[0] for r in reader.rows("SELECT account_identity FROM ledger_owner_export_imports WHERE kind='toss_pdf' AND account_label='InvestMCP 전용'")}
        salt = reader.rows("SELECT value FROM ledger_state WHERE key='account_salt'")
        if salt and isinstance(salt[0][0], bytes) and len(salt[0][0]) == 32:
            privacy = _Privacy(salt[0][0], [])
        if 'ledger_account_aliases' in tables:
            aliases = dict(reader.rows('SELECT alias_identity,canonical_identity FROM ledger_account_aliases'))
    return selected, aliases, privacy


def _investmcp_navs(reader, tables, navs):
    selected, aliases, privacy = _investmcp_identity(reader, tables)
    return [{**nav, 'account_nav': {key: value for key, value in (nav.get('account_nav') or {}).items()
        if privacy and key.startswith('toss:') and aliases.get(privacy.opaque(key), privacy.opaque(key)) in selected}} for nav in navs]


def investmcp_accounts(db_path, navs=(), *, budget_seconds=READ_BUDGET_SECONDS):
    """InvestMCP-only Toss account keys, independent of flow coverage.

    Selects exactly INVESTMCP_TOSS_ACCOUNT_KEY, only when an owner-export import
    labelled 'InvestMCP 전용' exists (with the account salt), the key is in the
    snapshots, neither the import's nor the key's identity is aliased elsewhere,
    and no other Toss key (secondary always checked) resolves to either. One
    read-only transaction within the read budget. Returns (keys, reasons); any
    other case is INVESTMCP_ACCOUNT_IDENTITY_UNAVAILABLE, never another account.
    """
    missing = ['INVESTMCP_ACCOUNT_IDENTITY_UNAVAILABLE']
    if db_path is None or not Path(db_path).is_file():
        return [], missing
    deadline = time.monotonic() + max(0, budget_seconds)
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=max(0, budget_seconds))) as conn:
            reader = Reader(conn, deadline)
            reader.check()
            conn.execute('BEGIN')
            tables = {r[0] for r in reader.rows("SELECT name FROM sqlite_master WHERE type='table'")}
            selected, aliases, privacy = _investmcp_identity(reader, tables)
    except ReadBudgetExceeded:
        return [], missing + ['FLOW_READ_BUDGET_EXCEEDED']
    except sqlite3.OperationalError as exc:
        reason = 'FLOW_READ_BUDGET_EXCEEDED' if time.monotonic() >= deadline or str(exc) == 'interrupted' else 'LOCAL_FLOW_EVIDENCE_UNAVAILABLE'
        return [], missing + [reason]
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return [], missing + ['LOCAL_FLOW_EVIDENCE_UNAVAILABLE']
    toss = {key for nav in navs for key in (nav.get('account_nav') or {}) if key.startswith('toss:')}
    if not selected or privacy is None or INVESTMCP_TOSS_ACCOUNT_KEY not in toss:
        return [], missing
    primary = privacy.opaque(INVESTMCP_TOSS_ACCOUNT_KEY)
    bound = {primary, *selected}
    others = (toss | {'toss:secondary'}) - {INVESTMCP_TOSS_ACCOUNT_KEY}
    if (aliases.get(primary, primary) not in bound or any(aliases.get(i, i) not in bound for i in selected)
            or any(aliases.get(privacy.opaque(k), privacy.opaque(k)) in bound for k in others)):
        return [], missing
    return [INVESTMCP_TOSS_ACCOUNT_KEY], []


def read_flows(db_path, start, end, scope='equity', navs=(), *, budget_seconds=READ_BUDGET_SECONDS):
    """One consistent read transaction, never create or upgrade the database."""
    if db_path is None or not Path(db_path).is_file():
        return None if scope == 'equity' else _unavailable(start, end, scope)
    deadline = time.monotonic() + max(0, budget_seconds)
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=max(0, budget_seconds))) as conn:
            conn.row_factory = sqlite3.Row
            reader = Reader(conn, deadline)
            reader.check()
            conn.execute('BEGIN')
            return _read(reader, start, end, scope, navs)
    except ReadBudgetExceeded:
        return _unavailable(start, end, scope, 'FLOW_READ_BUDGET_EXCEEDED')
    except sqlite3.OperationalError as exc:
        reason = 'FLOW_READ_BUDGET_EXCEEDED' if time.monotonic() >= deadline or str(exc) == 'interrupted' else 'LOCAL_FLOW_EVIDENCE_UNAVAILABLE'
        return _unavailable(start, end, scope, reason)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return _unavailable(start, end, scope)


def _unavailable(start, end, scope, reason='LOCAL_FLOW_EVIDENCE_UNAVAILABLE'):
    reasons = ['FLOW_COVERAGE_GAP', reason]
    return {'coverage': {'status': 'unavailable', 'source': 'permanent_ledger', 'scope': scope,
            'start': start, 'end': end, 'reason_codes': reasons, 'covered_window': None,
            'covered_windows': [], 'first_uncovered_gap': {'start': start, 'end': end, 'reason_codes': reasons}},
            'flows_by_date': None, 'observed_deposits_krw': None, 'observed_withdrawals_krw': None}


def _proven_zero(metric):
    """Exactly zero with a reported/derived status; never unknown or rounded."""
    if not isinstance(metric, dict) or metric.get('status') not in ('reported', 'derived'):
        return False
    amounts = [metric.get('value')] + ([metric['decimal_value']] if 'decimal_value' in metric else [])
    return all(number(amount) == 0 for amount in amounts)


def _crypto_evidence_bound(nav):
    """Latest instant the crypto evidence of this version can carry, or None.

    The snapshot job captures crypto after valuation_at and after the Toss
    evidence reads (nav_evidence_completed_at); upsert_snapshot then stamps this
    version's updated_at, truncated to whole seconds, before the write.
    """
    written = instant(nav.get('updated_at'))
    if written is None or written.microsecond:
        return written
    return written + timedelta(seconds=1, microseconds=-1)


def _scoped_quality(nav, accounts, scope):
    """Retain applicable snapshot failures and validate scoped timestamps.

    Legacy payloads without timestamp metadata retain their existing quality;
    present-but-missing/invalid metadata cannot be upgraded by a scope change.
    Toss and crypto scopes may exclude only causes attributed to outside-scope
    accounts; unexplained failures and valuation/date failures remain blocking.
    """
    reasons = list(nav.get('reason_codes') or [])
    if scope in ('toss', 'toss_investmcp', 'crypto'):
        from daily_nav import quality_reason_details
        details = nav.get('quality_reason_details')
        if not isinstance(details, list):
            details = quality_reason_details(nav, reasons)
        currencies = {currency for row in accounts.values() for currency in row.get('nav_native') or {}}
        known_accounts = set(nav.get('account_nav') or {}) | {
            component.get('account') for component in nav.get('components') or []}

        def applies(detail):
            account, currency = detail.get('account'), detail.get('currency')
            if scope == 'crypto':
                # Only a broker account by its own source prefix is outside crypto.
                # Roster or component presence proves nothing; unknown:*, upbit:*,
                # venue and unattributed causes stay blocking.
                return not str(account).startswith(('toss:', 'kis:'))
            if account in accounts:
                return not detail.get('resolved_for_native_nav', False)
            if account:
                if account in known_accounts or str(account).split(':', 1)[0] in ('kis', 'binance', 'hyperliquid', 'crypto'):
                    return False
                return True  # Unrecognized source ownership cannot prove exclusion.
            if currency:
                return currency in currencies
            return True

        reasons = [reason for reason in reasons if reason in (
            'SNAPSHOT_DATE_MISMATCH', 'UNKNOWN_VALUATION_TIME', 'INVALID_VALUATION_TIME')
            or not any(d.get('reason_code') == reason for d in details)
            or any(applies(d) for d in details if d.get('reason_code') == reason)]
    if nav.get('result_status') in ('partial', 'unavailable') and not nav.get('reason_codes'):
        reasons.append('INCOMPLETE_SNAPSHOT')
    day = nav.get('snapshot_date')
    valued = instant(nav.get('valuation_at'))
    toss_scope = scope in ('toss', 'toss_investmcp')
    evidence_at = nav.get('nav_evidence_completed_at') or nav.get('valuation_at')
    evidence_valued = instant(evidence_at) if toss_scope else valued
    boundary = None
    if scope == 'crypto':
        evidence_valued = _crypto_evidence_bound(nav) or valued

    def check(stamp, missing, *, valuation=False, latest=None):
        parsed = instant(stamp)
        if stamp is None:
            reasons.append(missing)
        elif parsed is None:
            reasons.append('INVALID_VALUATION_TIME')
        elif day and ((valuation and parsed.astimezone(KST).date().isoformat() != day)
                      or parsed.astimezone(KST).date().isoformat() > day
                      or (not valuation and (latest or evidence_valued) and parsed > (latest or evidence_valued))):
            reasons.append('SNAPSHOT_DATE_MISMATCH')
        return parsed

    if 'valuation_at' in nav:
        check(nav['valuation_at'], 'UNKNOWN_VALUATION_TIME', valuation=True)
    currencies = set()
    if scope in ('toss', 'toss_investmcp'):
        for account, row in accounts.items():
            currencies.update(row.get('nav_native') or {})
            for metric in (row.get('nav_native') or {}).values():
                reasons.extend(metric.get('reason_codes') or [])
            for metric in (row.get('cash') or {}).values():
                reasons.extend(metric.get('reason_codes') or [])
            for component in nav.get('components') or []:
                if component.get('account') == account:
                    check(component.get('priced_at'), 'UNKNOWN_COMPONENT_PRICE_TIME')
                    from daily_nav import _mark
                    reasons.extend(component.get('reason_codes') or [])
                    reasons.extend(_mark(component.get('priced_at'), nav.get('observed_at'), nav.get('valuation_at'),
                                         'QUOTE', calendar=component.get('market_calendar'),
                                         require_calendar=component.get('component_type') == 'holding',
                                         evidence_until=evidence_at)['reason_codes'])
    else:
        from daily_nav import MAX_CRYPTO_AGE_SECONDS, crypto_boundary
        crypto = nav.get('crypto_nav') or {}
        if 'observed_at' in crypto:
            observed = check(crypto['observed_at'], 'UNKNOWN_CRYPTO_OBSERVATION_TIME')
            if observed and valued and abs((valued - observed).total_seconds()) > MAX_CRYPTO_AGE_SECONDS:
                reasons.append('STALE_CRYPTO_SNAPSHOT')
            # The capture completion is this point's instant (scope_navs): it must
            # name the snapshot's KST day and one coherent read of both venues.
            boundary, _, incoherent = crypto_boundary(crypto)
            reasons.extend(incoherent)
            if observed and day and observed.astimezone(KST).date().isoformat() != day:
                reasons.append('SNAPSHOT_DATE_MISMATCH')
        for provider in ('binance', 'hyperliquid'):
            metric = crypto.get(provider) or {}
            by_currency = metric.get('by_currency') or {}
            # As in daily_nav.crypto_nav, a proven zero converts to 0 without an
            # FX mark; non-zero or unknown amounts keep the price-time check.
            zero = {currency for currency, value in by_currency.items() if _proven_zero(value)}
            currencies.update(set(by_currency) - zero)
            for row in metric.get('accounts') or []:
                if row.get('asset') not in zero:
                    currencies.add(row.get('asset'))
                if 'observed_at' in crypto or 'observed_at' in row:
                    observed = check(row.get('observed_at'), 'UNKNOWN_CRYPTO_OBSERVATION_TIME')
                    if observed and valued and abs((valued - observed).total_seconds()) > MAX_CRYPTO_AGE_SECONDS:
                        reasons.append('STALE_CRYPTO_ACCOUNT_OBSERVATION')
                if 'nav_price_time' in row:
                    check(row['nav_price_time'], 'UNKNOWN_CRYPTO_OBSERVATION_TIME')
    # Crypto checks the marks daily_nav.crypto_nav valued with: Upbit, then legacy.
    crypto_marks = (nav.get('crypto_nav') or {}).get('fx_times') or {} if scope == 'crypto' else {}
    for currency in currencies - {'KRW', None}:
        mark = crypto_marks.get(currency, (nav.get('fx_times') or {}).get(currency)) or {}
        priced = check(mark.get('priced_at'), 'UNKNOWN_FX_PRICE_TIME',
                       latest=instant(mark.get('read_at')) if toss_scope else None)
        if toss_scope:
            from daily_nav import fx_mark_quality
            reasons.extend(fx_mark_quality(mark, nav.get('valuation_at'))['reason_codes'])
        else:
            reasons.extend(mark.get('reason_codes') or [])
            # The capture's own age rule at the mark's read, which must join the
            # coherent capture span around the instant the NAV now represents.
            from daily_nav import crypto_fx_mark_current
            if boundary and priced and not crypto_fx_mark_current(mark, crypto):
                reasons.append('STALE_CRYPTO_FX_RATE')
    return reasons


def _kis_scoped(nav):
    """KIS scope point from the stored 18:10 KIS capture alone (``kis_capture``).

    NAV is the sum of the accounts' KRW NAV at KIS's own rates; the point sits at
    the capture completion (the instant kis_flow_inference records bind to) and its
    marks (fx_times) are the KIS rates. A version without a capture is
    KIS_CAPTURE_MISSING: history before the capture began is never back-filled from
    rows without a price time. Every configured account must be complete.
    """
    capture = nav.get('kis_capture')
    reasons, values, marks, when = [], [], {}, None
    if not isinstance(capture, dict) or not isinstance(capture.get('accounts'), dict):
        reasons.append('KIS_CAPTURE_MISSING')
    else:
        reasons.extend(capture.get('reasons') or [])
        if not capture['accounts']:
            reasons.append('KIS_CAPTURE_MISSING')
        if capture.get('version') != KIS_CAPTURE_VERSION:
            reasons.append('KIS_CAPTURE_VERSION_UNSUPPORTED')
        when = instant(capture.get('observed_at'))
        if when is None:
            reasons.append('UNKNOWN_VALUATION_TIME')
        elif nav.get('snapshot_date') and when.astimezone(KST).date().isoformat() != nav['snapshot_date']:
            reasons.append('SNAPSHOT_DATE_MISMATCH')
        for _key, account in sorted(capture['accounts'].items()):
            account = account if isinstance(account, dict) else {}
            if account.get('status') != 'complete':
                reasons.extend([*(account.get('reasons') or []), 'KIS_CAPTURE_INCOMPLETE'])
            value = number((account.get('nav_krw') or {}).get('value'))
            if value is None or (account.get('nav_krw') or {}).get('status') not in ('reported', 'derived'):
                reasons.append('MISSING_KIS_NAV')
            else:
                values.append(value)
            priced = instant(account.get('nav_price_time'))
            if priced is None:
                reasons.append('KIS_PRICE_TIME_UNKNOWN')
            elif when is not None and (priced > when or when - priced > KIS_MAX_PRICE_AGE):
                reasons.append('STALE_KIS_PRICE')
            for currency, mark in sorted((account.get('fx') or {}).items()):
                rate = number((mark or {}).get('rate_to_krw'))
                if rate is None or rate <= 0:
                    reasons.append('MISSING_NAV_FX')
                elif marks.setdefault(currency, rate) != rate:
                    reasons.append('KIS_FX_MARKS_INCONSISTENT')
    scoped = {**nav, 'total_value_krw': float(sum(values)) if values and not reasons else None,
              'reason_codes': list(dict.fromkeys(reasons)), 'result_status': 'partial' if reasons else 'complete',
              'fx_times': {c: {'rate_to_krw': format(r, 'f'), 'source': 'kis_capture'} for c, r in marks.items()}}
    if when is not None:
        scoped['valuation_at'] = capture['observed_at']
    return scoped


def scope_navs(navs, scope, *, account_references=()):
    """Copies for calculation only; immutable consolidated payloads and legacy NAV stay intact."""
    if scope == 'equity':
        return navs
    result = []
    for nav in navs:
        if scope == 'kis':
            result.append(_kis_scoped(nav))
            continue
        if scope in ('toss', 'toss_investmcp', 'crypto'):
            values, accounts = [], {}
            if scope in ('toss', 'toss_investmcp'):
                accounts = {k: r for k, r in (nav.get('account_nav') or {}).items() if k.startswith('toss:')
                            and (scope == 'toss' or k in account_references)}
            reasons = _scoped_quality(nav, accounts, scope)
            if scope in ('toss', 'toss_investmcp'):
                if not accounts:
                    reasons.append('MISSING_TOSS_NAV')
                for account, row in accounts.items():
                    if row.get('source_status') != 'complete':
                        reasons.append('INCOMPLETE_NATIVE_NAV')
                    currencies = {c.get('currency') for c in nav.get('components') or [] if c.get('account') == account}
                    currencies = currencies or set(row.get('nav_native') or {})
                    if not currencies:
                        reasons.append('MISSING_NATIVE_NAV')
                    for currency in currencies:
                        metric = (row.get('nav_native') or {}).get(currency) or {}
                        value = number(metric.get('value'))
                        rate = Decimal(1) if currency == 'KRW' else number(((nav.get('fx_times') or {}).get(currency) or {}).get('rate_to_krw'))
                        if value is None or metric.get('status') not in ('reported', 'derived'):
                            reasons.append('INCOMPLETE_NATIVE_NAV')
                        elif rate is None or rate <= 0:
                            reasons.append('MISSING_NAV_FX')
                        else:
                            values.append(value * rate)
            else:
                for provider in ('binance', 'hyperliquid'):
                    metric = (nav.get('crypto_nav') or {}).get(provider) or {}
                    value = number(metric.get('value_krw'))
                    reasons += metric.get('reason_codes') or []
                    if value is None or metric.get('status') not in ('reported', 'derived'):
                        reasons.append('MISSING_CRYPTO_NAV')
                    else:
                        values.append(value)
            status = nav.get('result_status', 'complete')
            if scope in ('toss', 'toss_investmcp') and not reasons:
                status = 'complete'
            elif reasons and status not in ('partial', 'unavailable'):
                status = 'partial'
            scoped = {**nav, 'total_value_krw': float(sum(values)) if values and not reasons else None,
                      'reason_codes': list(dict.fromkeys(reasons)), 'result_status': status}
            if scope == 'crypto':
                from daily_nav import crypto_boundary
                if crypto_boundary(nav.get('crypto_nav') or {})[0] is not None:
                    # One clock: the NAV point and crypto_flow_intervals' flow split
                    # both sit at the capture completion the valuation used.
                    scoped['valuation_at'] = nav['crypto_nav']['observed_at']
            result.append(scoped)
            continue
        metric = nav.get('consolidated_nav_krw') or {}
        reasons = list(metric.get('reason_codes') or [])
        value = number(metric.get('value'))
        if value is None or metric.get('status') not in ('reported', 'derived'):
            reasons.append('MISSING_CONSOLIDATED_NAV')
        result.append({**nav, 'total_value_krw': None if reasons else float(value),
                       'reason_codes': list(dict.fromkeys(nav.get('reason_codes', []) + reasons))})
    return result


def add_coverage(coverage, flow_coverage, reasons=()):
    if isinstance(flow_coverage, dict) and flow_coverage.get('source') == 'permanent_ledger':
        coverage.update({k: flow_coverage[k] for k in ('covered_window', 'covered_windows', 'first_uncovered_gap')})
        coverage['reason_codes'] = list(dict.fromkeys(reasons))
        if reasons and coverage['first_uncovered_gap'] is None:
            coverage['first_uncovered_gap'] = {'start': flow_coverage['start'], 'end': flow_coverage['end'],
                                               'reason_codes': list(dict.fromkeys(reasons))}
    return coverage
