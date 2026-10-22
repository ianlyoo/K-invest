"""Private flow facts and conservative append-only internal matching.

Amounts remain in their reported asset. Owner statements supply explicit external
classifications; unique reconciled broker/bank legs can become internal.
"""
import re
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from portfolio_ledger import FLOW_KINDS, canonical, digest, number, text

OWNER_FLOW_SOURCES = {'toss_cashflows', 'kis_cashflows', 'bank_cashflows'}
FLOW_SOURCES = {'binance_transfers', 'binance_deposit', 'binance_withdraw', 'hyperliquid_flows'} | OWNER_FLOW_SOURCES
# A Binance API withdrawal's `amount` is net (what the recipient receives); the wallet
# is debited amount + transactionFee (e.g. USDT 98.5 + fee 1.5 = 100, the account export's
# withdrawal debit).
BINANCE_WITHDRAWAL_FEE_BASIS = 'separate_debit'


def token(value, max_length=80):
    value = text(value)
    if not value or not re.fullmatch(r'[A-Za-z0-9_:@.\-]{1,' + str(max_length) + '}', value):
        return None
    if re.search(r'0x[0-9a-fA-F]{40}(?![0-9a-fA-F])', value):
        return None
    return value


def private_ref(store, conn, value):
    value = text(value)
    if not value:
        return None
    value = value.lower()
    if re.fullmatch(r'(?:0x)?[0-9a-f]{64}', value):
        value = value.removeprefix('0x')
    return store.account_identity('flow-ref:' + value, conn)


def private_identifiers(store, conn, event):
    """Protect typed transaction references and embedded hash/address tokens.

    Venue trade/order/transaction numbers remain readable. An opaque token is
    already protected and must not be hashed again on replay.
    """
    sensitive = re.compile(r'(?<![A-Za-z0-9_])(?:0x)?(?:[a-fA-F0-9]{64}|[a-fA-F0-9]{40})(?![A-Za-z0-9_])'
                           r'|(?<![A-Za-z0-9_])(?:bc1|tb1|bnb1)[A-Za-z0-9]{25,90}(?![A-Za-z0-9_])'
                           r'|(?<![A-Za-z0-9_])[13](?=[1-9A-HJ-NP-Za-km-z]*[A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{25,34}(?![A-Za-z0-9_])')
    native = event.get('native_record_id')
    typed_hash = event.get('source') in ('hyperliquid_flows', 'hyperliquid_funding') or event.get('_native_transaction_ref')
    if native and typed_hash and not re.fullmatch(r'acct_[a-f0-9]{32}', native):
        masked = private_ref(store, conn, native)
        source_id = event.get('source_record_id') or ''
        if source_id == native or source_id.startswith(native + ':'):
            event['source_record_id'] = masked + source_id[len(native):]
        event['native_record_id'] = masked
    for field in ('native_record_id', 'source_record_id', 'parent_order_id'):
        if event.get(field):
            event[field] = sensitive.sub(lambda match: private_ref(store, conn, match[0]), str(event[field]))
    return event


def precision(value):
    if number(value) is None:
        return None
    return format(Decimal(1).scaleb(Decimal(str(value)).as_tuple().exponent), 'f')


def flow_facts(store, conn, stream, row, event):
    """Validate mandatory flow facts, retaining absent optional fee/network facts."""
    if event['source'] not in FLOW_SOURCES:
        return event
    venue, leaf = stream.split('/')[0], stream.split('/')[-1]
    delta = row.get('delta') or {}
    event['venue'] = venue
    event['amount_precision'] = precision(event.get('amount'))
    event['currency'] = token(event.get('currency'))
    event['network'] = token(row.get('network'))
    if event['network']:
        event['network'] = event['network'].upper()
    event['transaction_ref'] = private_ref(store, conn, row.get('txId') if venue == 'binance' else row.get('hash'))
    if venue == 'binance':
        wallet_type = row.get('walletType')
        event['wallet'] = {0: 'MAIN', 1: 'FUNDING'}.get(wallet_type) if type(wallet_type) is int else None
        if leaf.startswith('transfer_'):
            from crypto_futures_accounts import BINANCE_WALLET_ROUTES
            route = leaf.removeprefix('transfer_')
            if route not in BINANCE_WALLET_ROUTES or row.get('type', route) != route:
                return None
            event.update(direction='out', wallet=route.split('_')[0])
            event['source_record_id'] += ':' + route + ':out'
            event['transaction_ref'] = private_ref(store, conn, 'binance-wallet:' + str(row.get('tranId')))
        else:
            event['direction'] = 'in' if leaf == 'deposit' else 'out'
            # Rows stored before 2.12.12 carry None; a later fetch enriches them.
            event['fee_basis'] = BINANCE_WITHDRAWAL_FEE_BASIS if leaf == 'withdraw' else 'deposit_credit'
            if row.get('address'):
                event['chain_address_identity'] = private_ref(store, conn, row['address'])
            # Deposit address is our receiving address, not its sender. Only an
            # actual sourceAddress is evidence about a deposit counterparty.
            counterparty = row.get('address') if leaf == 'withdraw' else row.get('sourceAddress')
            if counterparty:
                event['destination_account_identity' if leaf == 'withdraw' else 'origin_account_identity'] = store.account_identity(
                    'hyperliquid:' + str(counterparty).lower(), conn)
    else:
        typ = delta.get('type')
        if typ in ('deposit', 'withdraw'):
            event['direction'] = 'in' if typ == 'deposit' else 'out'
            event['fee_basis'] = 'separate_debit' if typ == 'withdraw' else 'deposit_credit'
            event['wallet'] = 'perp'
        elif typ == 'accountClassTransfer':
            if type(delta.get('toPerp')) is not bool:
                return None
            event.update(direction='out', wallet='spot' if delta['toPerp'] else 'perp',
                         transfer_route='spot_perp' if delta['toPerp'] else 'perp_spot')
        else:
            if event.get('origin_account_identity') == event['account_identity']:
                event['direction'] = 'out'
            elif event.get('destination_account_identity') == event['account_identity']:
                event['direction'] = 'in'
            elif type(delta.get('isDeposit')) is bool:
                event['direction'] = 'in' if delta['isDeposit'] else 'out'
            event['wallet'] = 'spot' if typ == 'spotTransfer' else 'perp'
        # Non-funding ledger updates represent committed movements. Preserve an
        # explicitly reported status if a future response supplies it.
        if event.get('source_status') is None:
            event['source_status'] = 'completed'
        if delta.get('fee') is not None:
            # The fee's own denomination: a named feeToken, else USDC for a usdc delta
            # whose type has no feeToken field. A present but empty feeToken names no
            # asset, which only a zero fee can do without, on every type.
            named = delta.get('feeToken')
            event['fee_asset'] = (token(named) if named else event['currency']
                                  if number(delta['fee']) == '0' or ('feeToken' not in delta and 'usdc' in delta)
                                  else None)
    for field in ('native_record_id', 'source_record_id'):
        value = event.get(field)
        if value and token(value, 256) is None:
            event[field] = private_ref(store, conn, value)
    if (event.get('direction') not in ('in', 'out') or event['currency'] is None
            or event.get('execution_time_utc') is None or event['amount_precision'] is None
            or Decimal(str(event['amount'])) <= 0):
        return None
    if event.get('fee') is not None and (number(event['fee']) is None or Decimal(str(event['fee'])) < 0):
        return None
    # ISO source strings may carry more than milliseconds; keep their precision.
    timestamp = row.get('applyTime') if venue == 'binance' and leaf == 'withdraw' else None
    if isinstance(timestamp, str):
        try:
            fraction = re.search(r'\d{2}:\d{2}:\d{2}\.(\d+)', timestamp)
            digits = len(fraction[1]) if fraction else 0
            if digits > 6:
                return None  # Python datetimes cannot retain finer source precision.
            if fraction:
                timestamp = timestamp[:fraction.start(1)] + fraction[1].ljust(6, '0') + timestamp[fraction.end(1):]
            when = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            event['execution_time_utc'] = when.replace(tzinfo=when.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
            event['timestamp_precision'] = {0: 'second', 3: 'millisecond', 6: 'microsecond'}.get(digits, f'fractional_second_{digits}')
        except ValueError:
            return None
    return event


def expand_flow(event):
    """A wallet-route observation documents both legs in the primary account."""
    if not event:
        return [None]
    if event['source'] == 'binance_transfers' or (event['source'] == 'hyperliquid_flows' and event.get('transfer_route')):
        incoming_id = event['source_record_id'].removesuffix(':out') + ':in' if event['source'] == 'binance_transfers' else event['source_record_id'] + ':in'
        incoming = {**event, 'direction': 'in', 'wallet': event['transfer_route'].split('_')[1],
                    'source_record_id': incoming_id, '_legacy_event_ids': ()}
        return [event, incoming]
    return [event]


def completed(event):
    expected = {'binance_transfers': {'CONFIRMED', 'COMPLETED'}, 'binance_deposit': {'1'},
                'binance_withdraw': {'6'}, 'hyperliquid_flows': {'COMPLETED', 'CONFIRMED'}}
    return str(event.get('source_status')).upper() in expected.get(event['source'], {'COMPLETED', 'CONFIRMED'} if event['source'] in OWNER_FLOW_SOURCES else set())


def owner_pair(out, incoming, boundary):
    """Same-currency, completed owner legs; dates retain their true precision.

    Three weekday business days follows the existing statement convention;
    holidays and cross-currency links are not inferred from amounts.
    """
    if out['source'] not in OWNER_FLOW_SOURCES or incoming['source'] not in OWNER_FLOW_SOURCES:
        return None
    if out['account_identity'] == incoming['account_identity'] or not {out['account_identity'], incoming['account_identity']} <= boundary:
        return None
    if not completed(out) or not completed(incoming) or out['currency'] != incoming['currency']:
        return None
    if any(e.get('fee') != '0' for e in (out, incoming)):
        return None
    try:
        days = sorted(date.fromisoformat(e['activity_date']) for e in (out, incoming))
    except (ValueError, TypeError):
        return None
    current, business_days = days[0], 0
    if (days[1] - current).days > 7:
        return None
    while current < days[1]:
        current += timedelta(days=1)
        business_days += current.weekday() < 5
    if business_days > 3:
        return None
    unit = min(Decimal(out['amount_precision']), Decimal(incoming['amount_precision']))
    if unit <= 0 or any(Decimal(e['amount']) <= 0 for e in (out, incoming)) or abs(Decimal(out['amount']) - Decimal(incoming['amount'])) > unit:
        return None
    return {'rule': 'owner_three_business_days', 'business_days': business_days,
            'business_calendar': 'weekdays; holiday calendar unavailable',
            'out_amount': out['amount'], 'in_amount': incoming['amount'], 'currency': out['currency'],
            'precision_unit': number(unit), 'fee': '0'}


def evidence_for(out, incoming, boundary):
    if out['source'] in OWNER_FLOW_SOURCES or incoming['source'] in OWNER_FLOW_SOURCES:
        return owner_pair(out, incoming, boundary)
    if out['currency'] != incoming['currency'] or not completed(out) or not completed(incoming):
        return None
    owner_wallet = all(e.get('record_kind') == 'owner_csv' and e['source'] == 'binance_transfers' for e in (out, incoming))
    if not owner_wallet and (out.get('identity_quality') != 'native_record' or incoming.get('identity_quality') != 'native_record'):
        return None
    if out.get('network') and incoming.get('network') and out['network'] != incoming['network']:
        return None
    if out.get('transaction_ref') and incoming.get('transaction_ref') and out['transaction_ref'] != incoming['transaction_ref']:
        return None
    if any(event.get(field) and event[field] not in boundary for event in (out, incoming)
           for field in ('origin_account_identity', 'destination_account_identity')):
        return None
    delta = abs((datetime.fromisoformat(out['execution_time_utc']) - datetime.fromisoformat(incoming['execution_time_utc'])).total_seconds())
    shared = bool(out.get('transaction_ref') and out['transaction_ref'] == incoming.get('transaction_ref'))
    amount_out, amount_in = Decimal(out['amount']), Decimal(incoming['amount'])
    wallet = (out['account_identity'] == incoming['account_identity'] and out.get('wallet') != incoming.get('wallet')
              and out.get('transfer_route') and out['transfer_route'] == incoming.get('transfer_route') and shared)
    if wallet:
        if amount_out != amount_in or delta != 0 or any(e.get('fee') not in (None, '0') for e in (out, incoming)):
            return None
        rule, fee, unit = 'wallet_reference', None, Decimal(0)
    else:
        cross_venue = ((out['source'] == 'binance_withdraw' and incoming['source'] == 'hyperliquid_flows' and incoming['kind'] == 'deposit')
                       or (out['source'] == 'hyperliquid_flows' and out['kind'] == 'withdrawal' and incoming['source'] == 'binance_deposit'))
        cross_account_hl = (out.get('venue') == incoming.get('venue') == 'hyperliquid'
                            and out['account_identity'] != incoming['account_identity'] and shared
                            and out.get('destination_account_identity') == incoming['account_identity']
                            and incoming.get('origin_account_identity') == out['account_identity'])
        if not cross_venue and not cross_account_hl:
            return None
        if not shared and delta > 86400:
            return None
        # Do not override an explicitly different counterparty in two-account HL moves.
        fee = out.get('fee')
        if fee is None or out.get('fee_asset') != out['currency']:
            return None
        fee = Decimal(fee)
        if fee < 0:
            return None
        if incoming.get('fee') not in (None, '0'):
            return None
        basis = out.get('fee_basis')
        if fee and basis not in ('separate_debit', 'deducted_from_amount'):
            return None
        net = amount_out - fee if basis == 'deducted_from_amount' else amount_out
        unit = min(Decimal(out['amount_precision']), Decimal(incoming['amount_precision']))
        if abs(net - amount_in) > unit:
            return None
        rule = 'shared_reference' if shared else 'time_amount_fee'
    return {'rule': rule, 'shared_reference': out.get('transaction_ref') if shared else None,
            'time_delta_seconds': delta, 'out_amount': out['amount'], 'in_amount': incoming['amount'],
            'currency': out['currency'], 'fee': number(fee), 'fee_asset': out.get('fee_asset'),
            'fee_basis': out.get('fee_basis'), 'precision_unit': number(unit)}


def match_transfers(store, rule_version, *, conn=None):
    if not re.fullmatch(r'[A-Za-z0-9_.\-]{1,80}', rule_version):
        raise ValueError('TRANSFER_RULE_VERSION_INVALID')
    transaction_conn = conn
    with nullcontext(conn) if conn is not None else store.read() as conn:
        snapshot = flow_snapshot(conn)
        boundary = {store.account_identity('binance:primary', conn)}
        boundary.update(r[0] for r in conn.execute('SELECT canonical_identity FROM ledger_account_aliases'))
        boundary.update(r[0] for r in conn.execute("SELECT DISTINCT account_identity FROM ledger_events WHERE source IN ('hyperliquid_flows','hyperliquid_fills','hyperliquid_funding')"))
        boundary.update(r[0] for r in conn.execute("SELECT DISTINCT account_identity FROM ledger_events WHERE source IN (?,?,?)", tuple(sorted(OWNER_FLOW_SOURCES))))
        ids = [r[0] for r in conn.execute('SELECT event_id FROM ledger_events WHERE source IN ('
                + ','.join('?' for _ in FLOW_SOURCES) + ') AND kind IN (' + ','.join('?' for _ in FLOW_KINDS)
                + ") AND coalesce(json_extract(payload_json,'$.record_kind'),'')!='owner_unknown_activity'",
                (*sorted(FLOW_SOURCES), *sorted(FLOW_KINDS)))]
        conflicts = {r[0] for r in conn.execute('SELECT DISTINCT event_id FROM ledger_conflicts')}
        # Existing pairs reserve their legs across all rule versions. A new rule
        # may append a revision for the same pair; it never silently re-pairs it.
        reserved = {}
        versions, processed = set(), set()
        for row in conn.execute('SELECT pair_id,out_event_id,in_event_id,rule_version FROM ledger_transfer_matches'):
            reserved[row[1]], reserved[row[2]] = row[2], row[1]
            versions.add((row[0], row[3]))
            if row[3] == rule_version:
                processed.update((row[1], row[2]))
        events = {key: store.effective_event(conn, key) for key in ids if key not in conflicts and key not in processed}
        if snapshot != flow_snapshot(conn):
            return 0  # Facts changed during preparation; leave them for the next run.
    eligible = {key: e for key, e in events.items() if e.get('kind') in FLOW_KINDS
                and e.get('record_kind') != 'owner_unknown_activity' and e.get('direction') in ('in', 'out') and e.get('amount')
                and e.get('amount_precision') and (e.get('execution_time_utc') or (e['source'] in OWNER_FLOW_SOURCES and e.get('activity_date')))}
    candidates, incoming_by_currency = [], {}
    for key, event in eligible.items():
        if event['direction'] == 'in':
            incoming_by_currency.setdefault(event['currency'], []).append((key, event))
    for oid, out in eligible.items():
        if out['direction'] != 'out':
            continue
        for iid, incoming in incoming_by_currency.get(out['currency'], ()):
            if (oid in reserved and reserved[oid] != iid) or (iid in reserved and reserved[iid] != oid):
                continue
            evidence = evidence_for(out, incoming, boundary)
            if evidence:
                rank = 0 if evidence['rule'] in ('wallet_reference', 'shared_reference') else 1
                candidates.append((rank, oid, iid, evidence))
    # Hash/ref matches take precedence on BOTH legs; ties never use row order.
    best = {}
    for rank, oid, iid, _ in candidates:
        for key in (oid, iid):
            best[key] = min(best.get(key, rank), rank)
    candidates = [p for p in candidates if p[0] == best[p[1]] == best[p[2]]]
    degrees = {}
    for _, oid, iid, _ in candidates:
        degrees[oid], degrees[iid] = degrees.get(oid, 0) + 1, degrees.get(iid, 0) + 1
    ambiguous = {key for _, oid, iid, _ in candidates if degrees[oid] != 1 or degrees[iid] != 1
                 for key in (oid, iid) if events[key]['source'] in OWNER_FLOW_SOURCES}
    if ambiguous:
        with nullcontext(transaction_conn) if transaction_conn is not None else store.transaction() as conn:
            if snapshot != flow_snapshot(conn):
                return 0
            for key in ambiguous:
                store.classify_transfer(conn, key, 'unknown', 'AMBIGUOUS_INTERNAL_TRANSFER')
    added = 0
    for _, oid, iid, evidence in candidates:
        if degrees[oid] != 1 or degrees[iid] != 1:
            continue
        pair_id = 'lpair_' + digest([oid, iid])
        if (pair_id, rule_version) in versions:
            continue
        with nullcontext(transaction_conn) if transaction_conn is not None else store.transaction() as conn:
            # Recheck reservations/conflicts in the writer transaction so a
            # concurrent collector cannot consume either leg twice.
            if snapshot != flow_snapshot(conn):
                break
            if conn.execute('SELECT 1 FROM ledger_transfer_matches WHERE pair_id=? AND rule_version=?', (pair_id, rule_version)).fetchone():
                continue
            if conn.execute('SELECT 1 FROM ledger_transfer_matches WHERE (out_event_id IN (?,?) OR in_event_id IN (?,?)) AND pair_id!=?',
                            (oid, iid, oid, iid, pair_id)).fetchone():
                continue
            if conn.execute('SELECT 1 FROM ledger_conflicts WHERE event_id IN (?,?)', (oid, iid)).fetchone():
                continue
            if any(conn.execute('SELECT classification FROM ledger_transfer_classifications WHERE event_id=? ORDER BY revision DESC LIMIT 1',
                                (key,)).fetchone()[0] not in (('unknown', 'internal', 'external_in', 'external_out')
                                if events[key]['source'] in OWNER_FLOW_SOURCES else ('unknown', 'internal')) for key in (oid, iid)):
                continue
            revision = conn.execute('SELECT coalesce(max(revision),0)+1 FROM ledger_transfer_matches WHERE pair_id=?', (pair_id,)).fetchone()[0]
            conn.execute('INSERT INTO ledger_transfer_matches VALUES (?,?,?,?,?,?,?,?)',
                         (pair_id, revision, oid, iid, rule_version, canonical(evidence), 'internal', datetime.now(timezone.utc).isoformat()))
            for key in (oid, iid):
                store.classify_transfer(conn, key, 'internal', 'MATCHED_INTERNAL_TRANSFER')
            added += 1
    return added


def flow_snapshot(conn):
    # Append-only rowids detect new events/conflicts/enrichments between the
    # read plan and writer lock, including a newly ambiguous counterpart.
    return tuple(conn.execute(f'SELECT coalesce(max(rowid),0) FROM ledger_{table}').fetchone()[0]
                 for table in ('events', 'conflicts', 'enrichments', 'status_revisions', 'account_aliases'))
