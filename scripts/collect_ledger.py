#!/usr/bin/env python3
"""Collect an overlapping trailing ledger window daily, including weekends.

Uses injected existing read-only clients. Never imports server or loads .env.
One failing provider/stream does not prevent preservation of the others.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crypto_futures_accounts as futures  # noqa: E402
from crypto_validation import (  # noqa: E402
    CryptoValidationError,
    binance_flow_page,
    hyperliquid_flow_page,
)
from kis_accounts import KISAccountRegistry  # noqa: E402
from portfolio_ledger import (  # noqa: E402
    FIELDS,
    LedgerStore,
    canonical,
    ingest_statements,
    instant,
    normalize_rows,
    normalize_transaction,
)
from portfolio_sources import PortfolioCollector, Providers  # noqa: E402
from toss_accounts import TossAccountRegistry  # noqa: E402


def equity_collector():
    # Registry construction is lazy and isolated per provider by PortfolioCollector.
    registries = {}
    def toss():
        if 'toss' not in registries:
            registries['toss'] = TossAccountRegistry.from_sources()
        return registries['toss']
    def kis():
        if 'kis' not in registries:
            registries['kis'] = KISAccountRegistry.from_sources()
        return registries['kis']
    return PortfolioCollector(Providers(
        toss_labels=lambda: toss().account_labels(), toss_client=lambda label: toss().client_for(label),
        kis_accounts=lambda: kis().accounts(), kis_client=lambda label: kis().client_for(label),
        error_formatter=lambda exc: {'error': {'code': 'SOURCE_COLLECTION_FAILED', 'kind': 'provider_unavailable'}}),
        strict_toss_fills=True)


class CollectionRun:
    def __init__(self, store, raw_dir=None):
        self.store = store
        self.run_id = 'daily-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:12]
        self.root = Path(raw_dir) if raw_dir else store.db_path.parent / 'ledger-raw'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.root / self.run_id
        self.root.mkdir(parents=True, mode=0o700)
        self.counts, self.failed, self.sequence = {}, False, 0

    def save(self, stream, events, account, lo, hi, reasons=(), failed=False, *, affects_exit=True):
        counts = dict.fromkeys(('new', 'duplicate', 'conflict', 'skipped'), 0)
        status = 'partial' if failed else 'limited' if reasons else 'complete'
        # Store whitelisted normalized evidence only; never the credential-bearing
        # client or arbitrary upstream response. Capture ingest keeps raw file refs.
        safe = [{k: e.get(k) for k in FIELDS} if e else None for e in events]
        content = canonical(safe).encode()
        self.sequence += 1
        relative = f'page-{self.sequence:06d}.json'
        path = self.root / relative
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        content_hash = hashlib.sha256(content).hexdigest()
        counts = self.store.append_many([(event, dict(file_sha256=content_hash, run_id=self.run_id,
                         page_path=relative, row_index=index, raw_store_ref=str(self.root)))
                         for index, event in enumerate(events)])
        reasons = list(reasons)
        if any(e and e.get('identity_quality') == 'evidence_row_unverified' for e in events):
            reasons.append('SOURCE_RECORD_ID_UNAVAILABLE')
            if status == 'complete':
                status = 'limited'
        if counts['skipped']:
            reasons.append('UNSUPPORTED_OR_MISSING_RECORD_ID')
            status = 'partial'
        if counts['conflict']:
            reasons.append('LEDGER_CONTENT_CONFLICT')
            status = 'partial'
        self.store.record_coverage(stream.split('/')[0], account, stream, lo, hi, status, reasons,
                                  len(events), self.run_id)
        counts['coverage'] = {'status': status, 'reason_codes': sorted(set(reasons))}
        self.counts[f'stream-{len(self.counts) + 1}'] = counts
        # A conflict is preserved (ledger_conflicts, partial coverage) and needs
        # reconciliation, not a re-run; only a collection gap fails the run.
        if affects_exit:
            self.failed |= failed or bool(counts['skipped'])

    def raw(self, stream, rows, account, lo, hi, reasons=(), failed=False, *, affects_exit=True):
        content_hash = hashlib.sha256(canonical(rows).encode()).hexdigest()
        with self.store.read() as conn:
            normalized = [event for i, row in enumerate(rows) for event in normalize_rows(
                          self.store, conn, stream, row, {'file_sha256': content_hash, 'row_index': i})]
        self.save(stream, normalized, account, lo, hi, reasons, failed, affects_exit=affects_exit)


def collect(store, start, end, *, equity=None, binance_factory=None, hyperliquid_factory=None, raw_dir=None):
    # Freeze all legacy ingestion, statement matching, counts and exit status
    # before constructing any supplemental transport or writing strict proof.
    context = {}
    result = _collect_legacy(store, start, end, equity=equity, binance_factory=binance_factory,
        hyperliquid_factory=hyperliquid_factory, raw_dir=raw_dir, context=context)
    try:
        result['toss_inference'] = collect_toss_inference(store, context, raw_dir)
    except Exception:
        result['toss_inference'] = {'status': 'unavailable', 'reason_codes': ['FILL_EVIDENCE_UNAVAILABLE']}
    try:
        result['inference'] = collect_inference(store, start, end, context)
    except Exception:
        # Absence of strict evidence remains unresolved at query time. No
        # supplemental exception is allowed to turn legacy success into failure.
        result['inference'] = {'status': 'unavailable', 'reason_codes': ['FLOW_EVIDENCE_UNAVAILABLE']}
    return result


def _collect_legacy(store, start, end, *, equity=None, binance_factory=None, hyperliquid_factory=None, raw_dir=None, context=None):
    observed_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    context = {} if context is None else context
    context["observed_at_ms"] = observed_at_ms
    run = CollectionRun(store, raw_dir)
    # A zero-row failed source is an explicit partial window, never verified zero.
    try:
        result = (equity or equity_collector()).collect_transactions(start, end)
        context['toss_fill_windows'] = getattr(result, 'toss_fill_windows', {})
        sources = set(result.source_metadata) | set(result.health)
        for source in sorted(sources):
            metadata = result.source_metadata.get(source, {})
            provider = 'toss' if source.startswith('toss') else 'kis'
            account_scope = source.removesuffix(':orders')
            selected = [tx for tx in result.transactions if not tx.source.startswith('statement:') and (
                        tx.account == account_scope or source.startswith(tx.account + ':') or source == tx.account)]
            with store.read() as conn:
                events = [normalize_transaction(store, conn, tx) for tx in selected]
                account = events[0]['account_identity'] if events and events[0] else store.account_identity(account_scope, conn)
            reasons = [] if metadata.get('complete') is True else [metadata.get('error_code') or 'SOURCE_WINDOW_INCOMPLETE']
            if metadata.get('truncated'):
                reasons.append('PAGE_BUDGET')
            if any(e and not e['execution_time_utc'] for e in events):
                reasons.append('EXECUTION_TIME_DATE_ONLY')
            run.save(provider + '/' + hashlib.sha256(source.encode()).hexdigest()[:16], events, account,
                     start, end, reasons, failed=metadata.get('complete') is not True)
    except Exception:
        for provider in ('toss', 'kis'):
            run.save(provider + '/collection', [], store.account_identity(provider + ':unknown'), start, end,
                     ['SOURCE_COLLECTION_FAILED'], True)
    lo = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    hi = int((datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1
    binance_hi = min(hi, observed_at_ms)
    try:
        client = (binance_factory or futures.BinanceAccountClient.from_environment)()
        context['binance'] = client
        account = store.account_identity('binance:primary')
        if lo > binance_hi:
            raise ValueError('BINANCE_WINDOW_NOT_OBSERVED')
        symbols = set()
        roster_incomplete = False
        with store.read() as conn:
            for row in conn.execute("SELECT payload_json FROM ledger_events WHERE source='binance_fills' AND account_identity=?", (account,)):
                symbol = json.loads(row[0]).get('symbol')
                if symbol:
                    symbols.add(symbol)
        rows, reasons, failed = history(client, lambda a, b: client.request('GET', '/fapi/v1/income',
                                        {'startTime': a, 'endTime': b, 'limit': 1000}), lo, binance_hi, 1000,
                                        lambda r: (r.get('tranId'), r.get('incomeType'), r.get('symbol')))
        symbols.update(r['symbol'] for r in rows if r.get('symbol'))
        run.raw('binance/income', rows, account, lo, binance_hi, reasons, failed)
        try:
            positions = client.request('GET', '/fapi/v3/positionRisk')
            symbols.update(r['symbol'] for r in positions if isinstance(r, dict) and r.get('symbol'))
        except Exception:
            roster_incomplete = True
            run.save('binance/roster', [], account, lo, binance_hi, ['SYMBOL_ROSTER_POSITION_QUERY_FAILED'], True)
        if not symbols:
            roster_incomplete = True
            run.save('binance/roster', [], account, lo, binance_hi,
                     ['SYMBOL_ROSTER_EMPTY_OR_UNAVAILABLE', 'SYMBOL_DISCOVERY_INCOMPLETE'], True)
        for symbol in sorted(symbols):
            if not re_symbol(symbol):
                continue
            rows, reasons, failed = history(client, lambda a, b, symbol=symbol: client.request('GET', '/fapi/v1/userTrades',
                                            {'symbol': symbol, 'startTime': a, 'endTime': b, 'limit': 1000}), lo, binance_hi, 1000,
                                            lambda r: (symbol, r.get('id')))
            run.raw('binance/userTrades_' + symbol, rows, account, lo, binance_hi,
                    reasons + (['SYMBOL_DISCOVERY_INCOMPLETE'] if roster_incomplete else []), failed)
        for leaf in (*('transfer_' + route for route in futures.BINANCE_WALLET_ROUTES), 'deposit', 'withdraw'):
            rows, reasons, failed = legacy_flow_history(client, leaf, lo, binance_hi, observed_at_ms=observed_at_ms)
            run.raw('binance/' + leaf, rows, account, lo, binance_hi, reasons, failed)
    except Exception:
        run.save('binance/collection', [], store.account_identity('binance:primary'), lo, binance_hi, ['SOURCE_COLLECTION_FAILED'], True)
    try:
        client = (hyperliquid_factory or futures.HyperliquidAccountClient.from_environment)()
        context['hyperliquid'] = client
        for address in client._addresses:
            account = store.hl_identity(address)
            suffix = account
            for kind, limit in (('userFillsByTime', 2000), ('userFunding', 500), ('userNonFundingLedgerUpdates', 500)):
                rows, reasons, failed = history(client, lambda a, b, kind=kind: client.info(kind, address, startTime=a, endTime=b),
                                                lo, hi, limit, lambda r: (r.get('tid'), r.get('hash'), r.get('time'),
                                                                         canonical(r.get('delta'))))
                reasons += ['LATEST_10000_FILLS_CAP'] if kind == 'userFillsByTime' else ['HISTORY_CAP_UNVERIFIED']
                run.raw('hyperliquid/' + suffix + '/' + kind, rows, account, lo, hi, reasons, failed)
    except Exception:
        run.save('hyperliquid/collection', [], store.account_identity('hyperliquid:unknown'), lo, hi, ['SOURCE_COLLECTION_FAILED'], True)
    statements = ingest_statements(store)
    run.failed |= bool(statements['skipped'])
    matches = store.match_transfers()
    conflicts = sum(c['conflict'] for c in run.counts.values()) + statements['conflict']
    return {'streams': run.counts, 'statements': statements, 'transfer_matches': matches, 'conflicts': conflicts,
            'failed': run.failed}


def collect_toss_inference(store, context, raw_dir=None):
    """Persist Toss's separate fill windows after the legacy exit is frozen.

    Keep its approved additive fills/enrichments, but isolate normalization,
    file and coverage failures from every legacy source, including KIS.
    """
    windows = context.get('toss_fill_windows') or {}
    if not any(windows.values()):
        return {'status': 'not_requested', 'streams': {}}
    root = Path(raw_dir) / 'toss-fill-evidence' if raw_dir else store.db_path.parent / 'toss-fill-evidence-raw'
    run, failures = CollectionRun(store, root), []
    for source, entries in sorted(windows.items()):
        if not source.startswith('toss:'):
            continue
        account = store.account_identity(source.removesuffix(':orders'))
        stream = 'toss/fills-v1/' + hashlib.sha256(source.encode()).hexdigest()[:16]
        for window in entries:
            try:
                reasons, bounded = list(window['reasons']), []
                if not window['complete'] and not reasons:
                    reasons.append('FILL_EVIDENCE_INCOMPLETE')
                with store.read() as conn:
                    for tx in window['transactions']:
                        event = normalize_transaction(store, conn, tx)
                        if event is not None:
                            # A float-built legacy row's noise is no difference here or on save; a strict row compares exactly.
                            event['_legacy_float_noise'] = True
                        if event is None or store.prepare(conn, event)['differences']:
                            reasons.append('FILL_LEGACY_EVIDENCE_MISMATCH')
                        else:
                            bounded.append(event)
                run.save(stream, bounded, bounded[0]['account_identity'] if bounded else account,
                         window['start'], window['end'], reasons, affects_exit=False)
            except Exception:
                failures.append('FILL_EVIDENCE_UNAVAILABLE')
                # Record a failed supplemental window if its store is still
                # writable; a second write failure stays in this channel too.
                try:
                    run.save(stream, [], account, window['start'], window['end'],
                             ['FILL_EVIDENCE_UNAVAILABLE'], True, affects_exit=False)
                except Exception:
                    pass
    return {'status': 'limited' if failures or any(row['coverage']['status'] != 'complete'
             for row in run.counts.values()) else 'complete', 'streams': run.counts,
            'reason_codes': sorted(set(failures))}


def collect_inference(store, start, end, context):
    """Strict phase: no legacy saves, normalization changes, matching or exit writes."""
    from crypto_flow_evidence import FlowEvidence

    evidence, outcomes = FlowEvidence(store), []
    observed = context['observed_at_ms']
    lo = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    hi = min(int((datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1,
             observed)
    for venue in ('binance', 'hyperliquid'):
        original, client = context.get(venue), None
        try:
            client = original.inference_client()
        except Exception:
            pass
        if venue == 'binance':
            plans = [(store.account_identity('binance:primary'), None, leaf) for leaf in
                     (*('transfer_' + route for route in futures.BINANCE_WALLET_ROUTES), 'deposit', 'withdraw')]
        else:
            addresses = getattr(original, '_addresses', ())
            plans = [(store.account_identity('hyperliquid:' + address.lower()), address,
                      'userNonFundingLedgerUpdates') for address in addresses]
        for account, address, leaf in plans:
            stream = venue + '/' + account + '/' + leaf
            windows = flow_windows(leaf if venue == 'binance' else 'hyperliquid', lo, hi, observed) if lo <= hi else [(lo, lo)]
            for a, b in windows:
                try:
                    if client is None:
                        rows, reasons, failed = [], ['FLOW_EVIDENCE_UNAVAILABLE'], True
                    elif lo > hi:
                        rows, reasons, failed = [], ['FUTURE_WINDOW_UNOBSERVED'], True
                    elif venue == 'binance':
                        rows, reasons, failed = flow_history(client, leaf, a, b, observed_at_ms=observed)
                    else:
                        rows, reasons, failed = hyperliquid_flow_history(client, address, a, b)
                        if a < observed - 90 * futures.DAY_MS:
                            reasons.append('HISTORY_CAP_UNVERIFIED')
                    outcomes.append(evidence.raw(stream, rows, account, a, b, reasons, failed))
                except Exception:
                    try:
                        outcomes.append(evidence.save(venue, leaf, [], account, a, b,
                                                       ['FLOW_EVIDENCE_UNAVAILABLE'], True))
                    except Exception:
                        outcomes.append({'status': 'unavailable', 'reason_codes': ['FLOW_EVIDENCE_UNAVAILABLE']})
    return {'status': 'complete' if outcomes and all(r['status'] == 'complete' for r in outcomes) else 'limited',
            'streams': outcomes}


def flow_page(payload, transfer):
    """Validate a history page without treating an error envelope as zero."""
    if not transfer:
        if not isinstance(payload, list):
            raise ValueError('HISTORY_SHAPE_UNAVAILABLE')
        return payload
    if (not isinstance(payload, dict) or type(payload.get('total')) is not int or payload['total'] < 0
            or ('code' in payload and (type(payload['code']) is not int or payload['code'] not in (0, 200)))
            or any(payload.get(key) is not None for key in ('error', 'errors'))
            or payload.get('success', True) is not True
            or ('msg' in payload and 'code' not in payload)):
        raise ValueError('HISTORY_SHAPE_UNAVAILABLE')
    page = payload.get('rows')
    if page is None and payload['total'] == 0:
        page = []
    if not isinstance(page, list) or len(page) > payload['total'] or any(not isinstance(row, dict) for row in page):
        raise ValueError('HISTORY_SHAPE_UNAVAILABLE')
    return page


def legacy_flow_history(client, leaf, lo, hi, *, page_size=None, observed_at_ms=None):
    """Binance current/size or offset/limit paging, strictly sub-90-day windows.

    The shared per-stream page budget bounds both pages and windows. Failures
    keep successful earlier rows and leave explicit unqueried-gap coverage.
    """
    transfer = leaf.startswith('transfer_')
    reasons = {'TRANSFER_SIX_MONTH_RETENTION'} if transfer else {'HISTORY_RETENTION_UNVERIFIED'}
    rows, seen, pages, failed = [], set(), 0, False
    size = max(1, min(page_size or (100 if transfer else 1000), 100 if transfer else 1000))
    path = '/sapi/v1/asset/transfer' if transfer else (
        '/sapi/v1/capital/deposit/hisrec' if leaf == 'deposit' else '/sapi/v1/capital/withdraw/history')
    if transfer:
        observed = observed_at_ms / 1000 if observed_at_ms is not None else getattr(client, '_clock', lambda: datetime.now(timezone.utc).timestamp())()
        now = datetime.fromtimestamp(observed, timezone.utc)
        month_index = now.year * 12 + now.month - 1 - 6
        year, month = month_index // 12, month_index % 12 + 1
        cutoff = int(now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1])).timestamp() * 1000)
        if lo < cutoff:
            reasons.add('RETENTION_WINDOW_GAP')
            lo = cutoff
    cursor = lo
    while cursor <= hi:
        end = min(hi, cursor + 89 * futures.DAY_MS - 1)
        current, offset, window_seen = 1, 0, set()
        while True:
            if pages >= client.max_pages:
                reasons.update(('PAGE_BUDGET', 'UNQUERIED_WINDOW_GAP'))
                return rows, sorted(reasons), True
            params = {'startTime': cursor, 'endTime': end}
            params.update({'type': leaf.removeprefix('transfer_'), 'current': current, 'size': size} if transfer
                          else {'offset': offset, 'limit': size})
            pages += 1
            try:
                payload = client.request('GET', path, params)
            except Exception:
                reasons.update(('SOURCE_STREAM_FAILED', 'UNQUERIED_WINDOW_GAP'))
                failed = True
                break
            try:
                page = flow_page(payload, transfer)
            except ValueError:
                # The request was made, but its response cannot prove coverage.
                # Partial status and this reason preserve the unusable-data gap.
                reasons.add('HISTORY_SHAPE_UNAVAILABLE')
                failed = True
                break
            signature = canonical(page)
            if page and signature in window_seen:
                reasons.update(('PAGINATION_STALLED', 'UNQUERIED_WINDOW_GAP'))
                failed = True
                break
            window_seen.add(signature)
            for row in page:
                stamp = (row.get('timestamp') if transfer else row.get('insertTime') if leaf == 'deposit' else row.get('applyTime')) if isinstance(row, dict) else None
                if isinstance(stamp, str):
                    parsed = instant(stamp)
                    stamp = int(datetime.fromisoformat(parsed).timestamp() * 1000) if parsed else None
                if type(stamp) is int and not cursor <= stamp <= end:
                    reasons.add('OUT_OF_WINDOW_RESPONSE')
                    failed = True
                    continue
                key = canonical(row)
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
            offset += len(page)
            if transfer:
                if offset >= payload['total']:
                    break
                if not page:
                    reasons.add('PAGINATION_WINDOW_GAP')
                    failed = True
                    break
            elif len(page) < size:
                break
            current += 1
        cursor = end + 1
    return rows, sorted(reasons), failed


def flow_history(client, leaf, lo, hi, *, page_size=None, observed_at_ms=None):
    """Binance current/size or offset/limit paging, strictly sub-90-day windows.

    The shared per-stream page budget bounds both pages and windows. Failures
    keep successful earlier rows and leave explicit unqueried-gap coverage.
    """
    transfer = leaf.startswith('transfer_')
    reasons = set()
    rows, seen, pages, failed = [], set(), 0, False
    size = max(1, min(page_size or (100 if transfer else 1000), 100 if transfer else 1000))
    path = '/sapi/v1/asset/transfer' if transfer else (
        '/sapi/v1/capital/deposit/hisrec' if leaf == 'deposit' else '/sapi/v1/capital/withdraw/history')
    observed = observed_at_ms if observed_at_ms is not None else int(
        getattr(client, '_clock', lambda: datetime.now(timezone.utc).timestamp())() * 1000)
    if hi > observed:
        reasons.add('FUTURE_WINDOW_UNOBSERVED')
        hi = observed
    cutoff = flow_cutoff(leaf, observed)
    if lo < cutoff:
        reasons.add('TRANSFER_SIX_MONTH_RETENTION' if transfer else 'HISTORY_RETENTION_UNVERIFIED')
        if transfer:
            reasons.add('RETENTION_WINDOW_GAP')
            lo = cutoff
    cursor = lo
    while cursor <= hi:
        end = min(hi, cursor + 89 * futures.DAY_MS - 1)
        current, offset, window_seen, expected_total = 1, 0, set(), None
        while True:
            if pages >= client.max_pages:
                reasons.update(('PAGE_BUDGET', 'UNQUERIED_WINDOW_GAP'))
                return rows, sorted(reasons), True
            params = {'startTime': cursor, 'endTime': end}
            params.update({'type': leaf.removeprefix('transfer_'), 'current': current, 'size': size} if transfer
                          else {'offset': offset, 'limit': size})
            pages += 1
            try:
                payload = client.request('GET', path, params)
            except Exception as exc:
                reasons.update(('CRYPTO_INVALID_RESPONSE' if isinstance(exc, futures.FuturesError)
                                and exc.code == 'FUTURES_INVALID_RESPONSE' else 'SOURCE_STREAM_FAILED',
                                'UNQUERIED_WINDOW_GAP'))
                failed = True
                break
            try:
                page = binance_flow_page(payload, leaf, params)
            except CryptoValidationError as exc:
                # The request was made, but its response cannot prove coverage.
                # Partial status and this reason preserve the unusable-data gap.
                reasons.add(exc.code)
                failed = True
                break
            if transfer:
                if expected_total is not None and expected_total != payload['total']:
                    reasons.add('PAGINATION_TOTAL_CHANGED')
                    failed = True
                    break
                expected_total = payload['total']
            signature = canonical(page)
            if page and signature in window_seen:
                reasons.update(('PAGINATION_STALLED', 'UNQUERIED_WINDOW_GAP'))
                failed = True
                break
            window_seen.add(signature)
            for row in page:
                stamp = (row.get('timestamp') if transfer else row.get('insertTime') if leaf == 'deposit' else row.get('applyTime')) if isinstance(row, dict) else None
                if isinstance(stamp, str):
                    parsed = instant(stamp)
                    stamp = int(datetime.fromisoformat(parsed).timestamp() * 1000) if parsed else None
                if type(stamp) is int and not cursor <= stamp <= end:
                    reasons.add('OUT_OF_WINDOW_RESPONSE')
                    failed = True
                    continue
                key = str(row.get('tranId' if transfer else 'id'))
                if key in seen:
                    reasons.add('CRYPTO_DUPLICATE_IDENTITY')
                    failed = True
                    continue
                rows.append(row)
                seen.add(key)
            offset += len(page)
            if transfer:
                if offset >= payload['total']:
                    break
                if not page:
                    reasons.add('PAGINATION_WINDOW_GAP')
                    failed = True
                    break
            elif len(page) < size:
                break
            current += 1
        cursor = end + 1
    return rows, sorted(reasons), failed


def flow_cutoff(leaf, observed_ms):
    if not leaf.startswith('transfer_'):
        return observed_ms - 90 * futures.DAY_MS
    now = datetime.fromtimestamp(observed_ms / 1000, timezone.utc)
    month_index = now.year * 12 + now.month - 1 - 6
    year, month = month_index // 12, month_index % 12 + 1
    return int(now.replace(year=year, month=month,
        day=min(now.day, calendar.monthrange(year, month)[1])).timestamp() * 1000)


def flow_windows(leaf, lo, hi, observed_ms):
    """Keep historical advisories on their own immutable coverage windows."""
    cutoff = flow_cutoff(leaf, observed_ms)
    if lo < cutoff <= hi:
        return [(lo, cutoff - 1), (cutoff, hi)]
    return [(lo, hi)]


def hyperliquid_flow_history(client, address, lo, hi):
    """Complete terminal pages only; full pages bisect without skipping ties."""
    pending, result, seen, pages = [(lo, hi)], [], {}, 0
    while pending:
        if pages >= client.max_pages:
            return result, ['PAGE_BUDGET', 'UNQUERIED_WINDOW_GAP'], True
        a, b = pending.pop(0)
        pages += 1
        try:
            page = hyperliquid_flow_page(client.info('userNonFundingLedgerUpdates', address,
                startTime=a, endTime=b), address, a, b)
        except CryptoValidationError as exc:
            return result, [exc.code], True
        except Exception as exc:
            reason = ('CRYPTO_INVALID_RESPONSE' if isinstance(exc, futures.FuturesError)
                      and exc.code == 'FUTURES_INVALID_RESPONSE' else 'SOURCE_STREAM_FAILED')
            return result, [reason], True
        if len(page) >= 500:
            if a == b:
                return result, ['DENSE_TIMESTAMP'], True
            mid = (a + b) // 2
            pending[0:0] = [(a, mid), (mid + 1, b)]
            continue
        for row in page:
            key = (row['hash'], row['time'], row['delta']['type'])
            if key in seen:
                return result, ['CRYPTO_DUPLICATE_IDENTITY'], True
            seen[key] = True
            result.append(row)
    return result, [], False


def re_symbol(value):
    import re
    return isinstance(value, str) and re.fullmatch(r'[A-Z0-9_]{1,40}', value)


def history(client, call, lo, hi, limit, key):
    query = futures._Query(client, 'ledger', lo, hi)
    rows = []
    failed = False
    try:
        def observed_call(a, b):
            payload = call(a, b)
            from portfolio_ledger import rows_in
            if not isinstance(payload, list):
                query.reasons.add('HISTORY_SHAPE_UNAVAILABLE')
            safe = []
            for row in rows_in(payload):
                if not isinstance(row, dict) or type(row.get('time')) is not int:
                    rows.append(row)
                    query.reasons.add('MISSING_TIMESTAMP')
                    safe.append({'time': None})
                else:
                    safe.append(row)
            return safe
        query.history(observed_call, lo, hi, limit, key, lambda row, native: rows.append(row))
    except Exception:
        query.reasons.add('SOURCE_STREAM_FAILED')
        failed = True
    failed |= bool(query.reasons - {'HYPERLIQUID_HISTORY_LIMIT'})
    return rows, sorted(query.reasons), failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db')
    parser.add_argument('--raw-dir')
    parser.add_argument('--days', type=int, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 90:
        parser.error('--days must be between 1 and 90')
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    today = date.today()
    try:
        result = collect(LedgerStore(args.db), (today - timedelta(days=args.days - 1)).isoformat(), today.isoformat(), raw_dir=args.raw_dir)
        print(json.dumps(result, sort_keys=True))
        return int(result['failed'])
    except Exception:
        print(json.dumps({'error_code': 'LEDGER_COLLECTION_FAILED'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
