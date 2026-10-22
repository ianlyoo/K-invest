"""Strict crypto windows committed atomically in the portfolio SQLite database.

Facts and coverage share one immutable row. Legacy ledger tables are read-only
vetoes; they never receive strict evidence, coverage, classifications or conflicts.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache, partial
from uuid import uuid4

from portfolio_ledger import (
    CONFLICT_TIME_SQL,
    FIELDS,
    FLOW_TIME_SQL,
    NUMERIC,
    LedgerStore,
    canonical,
    normalize_rows,
    number,
)

TABLE = 'crypto_flow_windows'
VERSION = 'crypto_flow_windows_v2'
# v1 windows predate fee denomination (window format v4/v5): a Hyperliquid fee_asset
# was the transfer currency whatever feeToken said, and no feeToken was kept. They
# remain coverage and facts, but a charged HL fee has no known asset until a v2
# window observes the same event again.
FEE_ASSET_UNTRUSTED = {'crypto_flow_windows_v1'}
FIELDS = (*FIELDS, 'flow_type', 'origin_wallet', 'destination_wallet')
FLOW_SOURCES = {'binance_transfers', 'binance_deposit', 'binance_withdraw', 'hyperliquid_flows'}
MAX_BYTES = 64 * 1024 * 1024
MAX_QUERY_BYTES = 128 * 1024 * 1024
MAX_QUERY_WINDOWS = 2048
READ_BUDGET_SECONDS = 5
METADATA = ('window_id,source,account_identity,stream,start_time,end_time,observed_at,'
            'content_sha256,native_keys_json')


def _stream(venue, account, leaf):
    return f'{venue}/{account}/flows-v3/{leaf}'


def normalize(store, conn, stream, raw):
    """Extend a copy of baseline facts for inference only; never enrich ledger."""
    rows = normalize_rows(store, conn, stream, raw, {})
    if not rows or any(row is None for row in rows):
        raise ValueError('FLOW_NORMALIZATION_UNAVAILABLE')
    result = []
    for original in rows:
        row = {k: original.get(k) for k in FIELDS}
        for field in NUMERIC:
            row[field] = number(row[field])
        if row['source'] not in FLOW_SOURCES:
            continue
        if row['source'] == 'hyperliquid_flows':
            delta = raw['delta']
            row['flow_type'] = delta['type']
            if delta['type'] == 'accountClassTransfer':
                row['origin_account_identity'] = row['destination_account_identity'] = row['account_identity']
                row['origin_wallet'], row['destination_wallet'] = row['transfer_route'].split('_')
            elif delta['type'] == 'send':
                def wallet(dex):
                    return 'spot' if dex == 'spot' else 'perp' if dex == '' else 'dex:' + dex
                row['origin_wallet'], row['destination_wallet'] = wallet(delta['sourceDex']), wallet(delta['destinationDex'])
                row['wallet'] = row['origin_wallet'] if row['direction'] == 'out' else row['destination_wallet']
            else:
                row['origin_wallet'] = row['destination_wallet'] = row['wallet']
        result.append(row)
    return result


class FlowEvidence:
    def __init__(self, store):
        self.store = store
        self.run_id = uuid4().hex

    def raw(self, stream, rows, account, lo, hi, reasons=(), failed=False):
        venue, leaf = stream.split('/')[0], stream.split('/')[-1]
        try:
            with self.store.read() as conn:
                events = [event for row in rows for event in normalize(self.store, conn, stream, row)]
        except Exception:
            events, reasons, failed = [], [*reasons, 'CRYPTO_FLOW_NORMALIZATION_UNAVAILABLE'], True
        return self.save(venue, leaf, events, account, lo, hi, reasons, failed)

    def save(self, venue, leaf, events, account, lo, hi, reasons=(), failed=False):
        start, end = _instant(lo), _instant(hi)
        if (venue not in ('binance', 'hyperliquid') or not re.fullmatch(r'acct_[a-f0-9]{32}', account or '')
                or start is None or end is None or end < start):
            raise ValueError('FLOW_EVIDENCE_SCOPE_INVALID')
        status = 'partial' if failed else 'limited' if reasons else 'complete'
        reasons = sorted(set(reasons))
        stream = _stream(venue, account, leaf)
        facts = [{k: event.get(k) for k in FIELDS} for event in events]
        keys = _keys(facts)
        payload = {'version': VERSION, 'run_id': self.run_id, 'venue': venue, 'account_identity': account,
                   'stream': stream, 'start_time': _iso(start), 'end_time': _iso(end),
                   'status': status, 'reasons': reasons, 'row_count': len(facts),
                   'verified_zero': status == 'complete' and not reasons and not facts,
                   'native_keys': keys, 'events': facts}
        encoded, keys_json = canonical(payload), canonical(keys)
        if len(encoded.encode()) + len(keys_json.encode()) > MAX_BYTES:
            raise ValueError('FLOW_EVIDENCE_BUDGET_EXHAUSTED')
        _facts(payload)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        # One INSERT contains the facts, status, reasons and complete coverage.
        # BEGIN/COMMIT/ROLLBACK belong to the existing ledger transaction API.
        with self.store.transaction() as conn:
            conn.execute('''INSERT INTO crypto_flow_windows
                (window_id,source,account_identity,stream,start_time,end_time,observed_at,
                 content_sha256,native_keys_json,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (uuid4().hex, venue, account, stream, payload['start_time'], payload['end_time'],
                 _iso(datetime.now(timezone.utc)), digest, keys_json, encoded))
        return {'status': status, 'reason_codes': reasons, 'row_count': len(facts)}


def _kind(event):
    if event.get('flow_type'):
        return event['flow_type']
    if event.get('source') == 'hyperliquid_flows':
        from crypto_validation import HL_FLOW_TYPES
        kinds = set(str(event.get('source_record_id', '')).split(':')) & HL_FLOW_TYPES
        return next(iter(kinds)) if len(kinds) == 1 else None
    return None


def _link_digest(source, native, kind, route, leg):
    return hashlib.sha256(canonical([source, native, kind, route, leg]).encode()).hexdigest()


def _link(event, encode=_link_digest):
    """A coarse native-identity key; timestamps are compared separately.

    Exclude the account so an append-only alias binding remains usable. The
    caller always pairs this key with the resolved account. Expanded wallet
    legs remain distinct. Numeric Binance identity spelling is not financial.
    """
    native = event.get('native_record_id') or event.get('source_record_id')
    route, leg, kind = event.get('transfer_route'), None, _kind(event)
    if event.get('source') == 'binance_transfers':
        try:
            native = str(int(str(native).split(':')[0]))
        except (ValueError, TypeError):
            pass
        leg = event.get('direction') or 'out'
    elif kind == 'accountClassTransfer':
        leg = event.get('direction')
    try:
        return encode(event.get('source'), native, kind, route, leg)
    except TypeError:  # Preserve validation of non-scalar fields without caching.
        return _link_digest(event.get('source'), native, kind, route, leg)


def _keys(events):
    return sorted({_link(event) for event in events})


def _instant(value):
    try:
        if isinstance(value, int) or isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _private_aliases(row, aliases):
    row = dict(row)
    for field in ('account_identity', 'origin_account_identity', 'destination_account_identity'):
        value, seen = row.get(field), set()
        while value in aliases and value not in seen:
            seen.add(value)
            value = aliases[value]
        row[field] = value
    for field in NUMERIC:
        row[field] = number(row.get(field))
    return row


def _same_identity(left, right, link=_link):
    if left['source'] != right['source'] or left['account_identity'] != right['account_identity']:
        return False
    if left['source_record_id'] == right['source_record_id']:
        return True
    return link(left) == link(right) and (left['source'] != 'hyperliquid_flows'
        or LedgerStore.same_fact('execution_time_utc', left, right))


def _differences(left, right, *, legacy=False, link=_link):
    annotations = {'source_status', 'source_timezone', 'timestamp_precision', 'identity_quality'}
    if legacy:
        annotations |= {'source_record_id', 'native_record_id'}
        if left['source'] == 'binance_transfers' and link(left) == link(right):
            # Older IDs such as "0007" hashed a different display spelling.
            # Canonical numeric identity already binds the wallet transfer.
            annotations.add('transaction_ref')
        if _kind(right) == 'send':
            annotations.add('wallet')  # b8 assigned perp before interpreting the reported DEX pair.
    return [key for key in FIELDS if key not in annotations and left.get(key) is not None
            and right.get(key) is not None and not LedgerStore.same_fact(key, left, right)]


def _legacy_facts(conn, rows, aliases, lo, hi, check=lambda: None, link=_link):
    """Contradictions are read-only vetoes, never completeness evidence."""
    sources = sorted(FLOW_SOURCES)
    query = 'SELECT * FROM ledger_events WHERE source IN (' + ','.join('?' for _ in sources) + ')'
    params = list(sources)
    if lo is not None and hi is not None:
        native = [row.get('native_record_id') for row in rows if row.get('native_record_id')]
        numeric = [int(value) for value in native if str(value).isdigit()]
        ids = [row['source_record_id'] for row in rows]
        query += f''' AND ({FLOW_TIME_SQL} IS NULL OR ({FLOW_TIME_SQL}>julianday(?) AND {FLOW_TIME_SQL}<=julianday(?))
            OR event_id IN (SELECT event_id FROM ledger_conflicts WHERE {CONFLICT_TIME_SQL} IS NULL
                OR ({CONFLICT_TIME_SQL}>julianday(?) AND {CONFLICT_TIME_SQL}<=julianday(?)))
            OR source_record_id IN (SELECT value FROM json_each(?))
            OR json_extract(payload_json,'$.native_record_id') IN (SELECT value FROM json_each(?))
            OR (source='binance_transfers' AND CAST(json_extract(payload_json,'$.native_record_id') AS INTEGER)
                IN (SELECT value FROM json_each(?))))'''
        params += [lo.isoformat(), hi.isoformat(), lo.isoformat(), hi.isoformat(),
                   canonical(ids), canonical(native), canonical(numeric)]
    query += " AND coalesce(json_extract(payload_json,'$.record_kind'),'')!='owner_csv'"
    by_link, by_exact = defaultdict(list), defaultdict(list)
    for row in rows:
        by_link[(row['account_identity'], link(row))].append(row)
        by_exact[(row['account_identity'], row['source'], row['source_record_id'])].append(row)
    stored_rows = list(conn.execute(query, params))
    selected_ids = canonical([row['event_id'] for row in stored_rows])
    enrichments_by_id, conflicts_by_id, statuses = defaultdict(list), defaultdict(list), {}
    # A fixed number of indexed reads replaces three SQL round trips per fact.
    for table, fields, order, target in (
            ('ledger_enrichments', 'content_sha256,fields_json', 'rowid', enrichments_by_id),
            ('ledger_conflicts', 'content_sha256,candidate_json', 'event_id,content_sha256', conflicts_by_id)):
        for item in conn.execute(f'SELECT event_id,{fields} FROM {table} '
                f'WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY {order}', (selected_ids,)):
            check()
            target[item['event_id']].append(item)
    for item in conn.execute('SELECT event_id,reported_status,observed_at FROM ledger_status_revisions '
            'WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY revision DESC', (selected_ids,)):
        check()
        stamp = _observation_time(item['observed_at'])
        previous = statuses.get(item['event_id'])
        if previous is None or stamp > previous[1]:
            statuses[item['event_id']] = (item['reported_status'], stamp)
    for stored in stored_rows:
        check()
        fact = json.loads(stored['payload_json'])
        for field in FIELDS:
            fact.setdefault(field, None)
        enrichments = enrichments_by_id[stored['event_id']]
        for enrichment in enrichments:
            fact.update(json.loads(enrichment['fields_json']))
        status_time = _observation_time(stored['observed_at'])
        status = statuses.get(stored['event_id'])
        if status is not None and status[1] >= status_time:
            fact['source_status'], status_time = status
        fact = _private_aliases(fact, aliases)
        conflicts = conflicts_by_id[stored['event_id']]
        times = [json.loads(row['candidate_json']).get('execution_time_utc') for row in conflicts]
        version = {'event_id': stored['event_id'], 'content_sha256': stored['content_sha256'],
            'enrichments': [row['content_sha256'] for row in enrichments],
            'conflicts': [row['content_sha256'] for row in conflicts]}
        matches = [row for row in by_link.get((fact['account_identity'], link(fact)), []) if _same_identity(fact, row, link)]
        # Exact historical IDs also bind a candidate whose native metadata changed.
        matched = {id(row) for row in matches}
        exact = (fact['account_identity'], fact['source'], fact['source_record_id'])
        matches.extend(row for row in by_exact.get(exact, ()) if id(row) not in matched)
        for row in matches:
            row['_version'].setdefault('legacy_facts', []).append(version)
            changed = _differences(fact, row, legacy=True, link=link)
            row['_conflict'] |= bool(conflicts or changed)
            row['_conflict_times'].extend([*times, *([fact.get('execution_time_utc')] if changed else [])])
            if fact['source_status'] is not None and status_time > row['_status_observed_at']:
                row['source_status'], row['_status_observed_at'] = fact['source_status'], status_time
        if not matches:
            # A later empty fetch does not erase a previously accepted movement
            # or its preserved conflict. Do not use that movement as a strict flow.
            row = {**fact, '_legacy_only': True, '_conflict': bool(conflicts), '_status_observed_at': status_time,
                   '_conflict_times': times, '_version': version}
            rows.append(row)
            by_exact[exact].append(row)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _observation_time(value):
    stamp = _instant(value)
    if stamp is None:
        raise ValueError('FLOW_EVIDENCE_OBSERVATION_TIME_INVALID')
    return stamp


def _facts(payload, cache=None, link=_link):
    expected = {'version', 'run_id', 'venue', 'account_identity', 'stream', 'start_time', 'end_time',
                'status', 'reasons', 'row_count', 'verified_zero', 'native_keys', 'events'}
    if (not isinstance(payload, dict) or set(payload) != expected or payload['version'] not in {VERSION, *FEE_ASSET_UNTRUSTED}
            or payload['status'] not in ('complete', 'partial', 'limited')
            or not isinstance(payload['reasons'], list) or any(not isinstance(r, str) for r in payload['reasons'])
            or not isinstance(payload['events'], list) or payload['row_count'] != len(payload['events'])
            or payload['verified_zero'] is not (payload['status'] == 'complete' and not payload['reasons'] and not payload['events'])):
        raise ValueError('FLOW_EVIDENCE_PAYLOAD_INVALID')
    events, keys = [], set()
    for event in payload['events']:
        if (not isinstance(event, dict) or event.get('source') not in FLOW_SOURCES
                or event['source'].split('_')[0] != payload['venue']
                or event.get('account_identity') != payload['account_identity']
                or not event.get('source_record_id')):
            raise ValueError('FLOW_EVIDENCE_EVENT_INVALID')
        # Overlapping captures repeat immutable facts. Reuse validation and the
        # object only inside this read; every window/digest remains an observation.
        signature = (event['source'], event['account_identity'], event['source_record_id'])
        try:
            cached = cache.get(signature) if cache is not None else None
        except TypeError:  # Non-scalar malformed/legacy fields still take validation.
            signature, cached = None, None
        if (cached is not None and event == cached[0] and tuple(event) == cached[2]
                and tuple(map(type, event.values())) == cached[3]):
            event, key = cached[:2]
            events.append(event)
            keys.add(key)
            continue
        if set(event) != set(FIELDS) or number(event['amount']) is None:
            raise ValueError('FLOW_EVIDENCE_EVENT_INVALID')
        key = link(event)
        events.append(event)
        keys.add(key)
        if cache is not None and signature is not None:
            cache[signature] = (event, key, tuple(event), tuple(map(type, event.values())))
    if payload['native_keys'] != sorted(keys):
        raise ValueError('FLOW_EVIDENCE_IDENTITY_INVALID')
    if payload['version'] in FEE_ASSET_UNTRUSTED:
        events = [{**event, 'fee_asset': None} if event['source'] == 'hyperliquid_flows'
                  and number(event.get('fee')) not in (None, '0') else event for event in events]
    return events


def _proof(row, payload=None):
    return {'coverage_id': row['window_id'], 'source': row['source'],
        'account_identity': row['account_identity'], 'stream': row['stream'],
        'start_time': row['start_time'], 'end_time': row['end_time'], 'observed_at': row['observed_at'],
        'status': payload['status'] if payload else 'limited',
        'reason_codes_json': canonical(payload['reasons'] if payload else ['CRYPTO_FLOW_EVIDENCE_UNAVAILABLE']),
        'row_count': payload['row_count'] if payload else 0,
        'verified_zero': int(payload['verified_zero']) if payload else 0,
        '_integrity_failure': payload is None}


def _resolved(account, aliases):
    seen = set()
    while account in aliases and account not in seen:
        seen.add(account)
        account = aliases[account]
    return account


def read_view(conn, db_path, aliases, lo=None, hi=None):
    """Read one committed SQLite snapshot, selected windows and linked history.

    The caller owns the read transaction. The compatibility db_path argument is
    unused: no evidence file or independent manifest is opened or created.
    """
    deadline = time.monotonic() + READ_BUDGET_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    coverage, versions, selected = [], defaultdict(list), {}
    fact_cache, private_cache = {}, {}
    link = partial(_link, encode=lru_cache(maxsize=8192, typed=True)(_link_digest))
    remaining = MAX_QUERY_BYTES
    try:
        def check():
            if time.monotonic() >= deadline:
                raise ValueError('FLOW_EVIDENCE_QUERY_BUDGET_EXHAUSTED')

        def metadata(sql, params=(), required_keys=None):
            nonlocal remaining
            for record in conn.execute(sql, params):
                check()
                row = dict(record)
                if row['window_id'] in selected:
                    continue
                remaining -= len(row['native_keys_json'].encode())
                if remaining < 0:
                    raise ValueError('FLOW_EVIDENCE_QUERY_BUDGET_EXHAUSTED')
                keys = json.loads(row['native_keys_json'])
                if required_keys is not None:
                    # Match SQLite json_each's values before validating selected
                    # metadata. Unrelated malformed history was never a proof.
                    values = keys.values() if isinstance(keys, dict) else keys if isinstance(keys, list) else [keys]
                    if required_keys.isdisjoint(key for key in values if isinstance(key, str)):
                        continue
                if (not isinstance(keys, list) or any(not isinstance(key, str)
                        or not re.fullmatch('[a-f0-9]{64}', key) for key in keys)):
                    raise ValueError('FLOW_EVIDENCE_IDENTITY_INVALID')
                if len(selected) >= MAX_QUERY_WINDOWS:
                    raise ValueError('FLOW_EVIDENCE_QUERY_BUDGET_EXHAUSTED')
                row['_keys'] = keys
                selected[row['window_id']] = row

        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        if exists:
            sql, params = 'SELECT ' + METADATA + ' FROM crypto_flow_windows INDEXED BY crypto_flow_windows_time', []
            if lo is not None and hi is not None:
                sql += ' WHERE end_time>? AND start_time<=?'
                params = [_iso(lo), _iso(hi)]
            metadata(sql, params)
            links = defaultdict(set)
            for row in selected.values():
                links[(row['source'], _resolved(row['account_identity'], aliases))].update(row['_keys'])
            # Same native identity outside the requested window is still evidence.
            # These derived keys share the SAME immutable row and transaction as
            # the facts; SQLite's own index has no independently committed head.
            for (venue, account), keys in links.items():
                if not keys:
                    continue
                physical = sorted({account} | {alias for alias in aliases if _resolved(alias, aliases) == account})
                metadata('SELECT ' + METADATA + """ FROM crypto_flow_windows INDEXED BY crypto_flow_windows_scope
                    WHERE source=? AND account_identity IN (SELECT value FROM json_each(?))""",
                    (venue, canonical(physical)), required_keys=keys)
            for row in sorted(selected.values(), key=lambda r: (_observation_time(r['observed_at']), r['window_id'])):
                check()
                body_bytes = conn.execute('SELECT length(CAST(payload_json AS BLOB)) FROM crypto_flow_windows WHERE window_id=?',
                                         (row['window_id'],)).fetchone()[0]
                if body_bytes > remaining or body_bytes + len(row['native_keys_json'].encode()) > MAX_BYTES:
                    raise ValueError('FLOW_EVIDENCE_QUERY_BUDGET_EXHAUSTED')
                remaining -= body_bytes
                try:
                    encoded = conn.execute('SELECT payload_json FROM crypto_flow_windows WHERE window_id=?',
                                           (row['window_id'],)).fetchone()[0]
                    if hashlib.sha256(encoded.encode()).hexdigest() != row['content_sha256']:
                        raise ValueError('FLOW_EVIDENCE_HASH_MISMATCH')
                    payload = json.loads(encoded)
                    events = _facts(payload, fact_cache, link)
                    expected = {'venue': row['source'], **{k: row[k] for k in
                                ('account_identity', 'stream', 'start_time', 'end_time')}}
                    if (any(payload[k] != value for k, value in expected.items())
                            or canonical(payload['native_keys']) != row['native_keys_json']):
                        raise ValueError('FLOW_EVIDENCE_SCOPE_MISMATCH')
                    coverage.append(_proof(row, payload))
                    for event in events:
                        cached = private_cache.get(id(event))
                        if cached is None:
                            private = _private_aliases(event, aliases)
                            cached = (event, private, LedgerStore.event_id(private))
                            private_cache[id(event)] = cached
                        _, event, ident = cached
                        versions[ident].append((event, row['content_sha256'], row['observed_at']))
                except (ValueError, TypeError, KeyError, IndexError):
                    proof = _proof(row)
                    if lo is not None and hi is not None:
                        # A broken selected identity history also vetoes the
                        # current interval, even when its own window is older.
                        proof.update(start_time=_iso(lo), end_time=_iso(hi))
                    coverage.append(proof)
        for proof in coverage:
            proof['account_identity'] = _resolved(proof['account_identity'], aliases)
        events = []
        for ident, observations in versions.items():
            row = dict(observations[0][0])
            hashes, conflicts, times = [], [], []
            status_time = _observation_time(observations[0][2])
            for candidate, digest, observed in observations:
                hashes.append(digest)
                if candidate != row:
                    different = _differences(row, candidate, link=link)
                    if different:
                        conflicts.append(digest)
                        times.append(candidate.get('execution_time_utc'))
                    else:
                        row.update({k: v for k, v in candidate.items() if row[k] is None and v is not None})
                if candidate['source_status'] is not None:
                    row['source_status'] = candidate['source_status']
                    status_time = _observation_time(observed)
            row['_status_observed_at'] = status_time
            row['_version'] = {'event_id': ident, 'content_sha256': hashes[0], 'enrichments': sorted(set(hashes[1:])),
                               'status_revision': observations[-1][2], 'conflicts': sorted(set(conflicts))}
            row['_conflict'] = bool(conflicts)
            row['_conflict_times'] = times
            events.append(row)
        _legacy_facts(conn, events, aliases, lo, hi, check, link)
        check()
        return events, coverage
    except Exception:
        # Includes SQLite lock/corruption/query-budget errors and bad legacy
        # evidence. Never retain a partial set of facts or completeness proofs.
        return [], [{'_global_failure': True}]
    finally:
        conn.set_progress_handler(None, 0)
