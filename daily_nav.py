"""Additive daily NAV evidence. No broker clients, P&L or database migrations."""
from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

VALUATION_TIMEZONE = 'Asia/Seoul'
MAX_MARK_AGE_SECONDS = 24 * 3600
MAX_CRYPTO_AGE_SECONDS = 2 * 3600
# The risk capture's own coherent-read span (futures_risk bounds Binance reads to 60 s):
# every venue NAV read and the cut must fit in one span this wide.
CRYPTO_BOUNDARY_TOLERANCE_SECONDS = 60
# An Upbit stablecoin mark's last trade may precede its read by this much (and
# follow it by clock skew). One rule for the capture, the NAV write and returns.
MAX_STABLECOIN_AGE_SECONDS = 300
# USD stablecoins a live read may value at the USD rate when it has no direct
# mark (fx_proxy: USD); crypto_nav_capture.STABLECOINS, not imported (cycle).
USD_PROXY_STABLECOINS = ('USDT', 'USDC')
STABLECOIN_CLOCK_SKEW_SECONDS = 5
FIELDS = ('account_nav', 'valuation_timezone', 'mark_quality', 'external_flows',
          'crypto_nav', 'consolidated_nav_krw', 'quality_reason_details', 'collection_source_quality',
          'nav_evidence_completed_at')
TOSS_CASH_BASIS = 'TOSS_BUYING_POWER_NO_OPEN_ORDERS'
#: consolidated_nav_krw.basis: what the broker accounts' total_assets_krw actually used, plus
#: verified futures equity. canonical: summarize_portfolio with timed quotes (MARK_POLICY), fx.policy FX;
#: broker marks: no quotes (the snapshot/export paths until 2.15.1), one broker price per symbol, fx.policy FX;
#: legacy: rows stored before 2.15 (broker-reported values, yfinance FX). Readers accept all three.
CONSOLIDATED_NAV_BASIS = 'broker_accounts_at_canonical_marks_plus_verified_futures_equity_before_settlement_adjustments'
BROKER_MARKS_CONSOLIDATED_NAV_BASIS = 'broker_accounts_at_broker_marks_plus_verified_futures_equity_before_settlement_adjustments'
LEGACY_CONSOLIDATED_NAV_BASIS = 'legacy_broker_assets_plus_verified_futures_equity_before_settlement_adjustments'
CONSOLIDATED_NAV_BASES = (CONSOLIDATED_NAV_BASIS, BROKER_MARKS_CONSOLIDATED_NAV_BASIS, LEGACY_CONSOLIDATED_NAV_BASIS)


def consolidated_nav_basis(summary):
    """The label for a summary's total, from the marks and FX its valuation actually used.

    A 2.15+ summary (mark_policy and fx.policy) states its marks in ``mark_basis``; one built before that
    field shows them in mark_status_counts (a timed quote was used or not). Legacy versions re-read
    without daily NAV evidence (portfolio_store._version_payload passes {}) keep the legacy label.
    """
    summary = summary if isinstance(summary, dict) else {}
    if not (summary.get('mark_policy') and isinstance(summary.get('fx'), dict) and summary['fx'].get('policy')):
        return LEGACY_CONSOLIDATED_NAV_BASIS
    marks = summary.get('mark_basis') or (
        'canonical_marks' if (summary.get('mark_status_counts') or {}).get('timed') else 'broker_marks')
    return CONSOLIDATED_NAV_BASIS if marks == 'canonical_marks' else BROKER_MARKS_CONSOLIDATED_NAV_BASIS


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def metric(value=None, *reasons):
    value = number(value)
    converted = float(value) if value is not None else None
    if converted is not None and not math.isfinite(converted):
        converted = None
        reasons = (*reasons, 'INVALID_NUMERIC_VALUE')
    return {'value': converted, 'status': 'derived' if converted is not None else 'unknown',
            'reason_codes': list(dict.fromkeys(reasons or (() if converted is not None else ('VALUE_UNAVAILABLE',))))}


def total(values):
    values = [number(v) for v in values]
    return sum(values, Decimal(0)) if values and all(v is not None for v in values) else None


def _account_source(name):
    parts = name.split(':')
    if parts[0] == 'toss' and len(parts) >= 2:
        return ':'.join(parts[:2])
    if parts[0] == 'kis' and len(parts) >= 3:
        return ':'.join(parts[:3])
    return name


def account_nav(collection, components=None, fx_quality=None):
    """Native NAV is holdings plus reported cash, before settlement adjustments.

    Source failures invalidate account totals. Toss buying power is derived cash
    only after a complete empty OPEN-order read: reusable unsettled sale proceeds
    are assumed included and only open orders impose holds. KRW/USD are separate
    pools. This is neither settled nor withdrawable cash; no settlement amounts
    are invented or added again. Other buying power remains unknown. An
    absent currency cannot prove zero because the normalized sources have no
    explicit complete currency inventory contract.
    Toss positions use only the separate quote-derived component value; legacy
    holdings, the settlement bridge and all other accounts remain unchanged.
    """
    health = getattr(collection, 'health', {})
    sources = {}
    groups = {}
    for name, state in health.items():
        account = _account_source(name)
        groups.setdefault(account, [])
        sources.setdefault(account, []).append((name, state.get('status')))
    for kind, items in (('holding', collection.holdings), ('cash', collection.cash)):
        for item in items:
            quantity = number(getattr(item, 'quantity', None))
            if kind == 'holding' and quantity is not None and quantity <= 0:
                continue
            groups.setdefault(item.account, []).append((kind, item))
    accounts = {}
    for account, rows in sorted(groups.items()):
        currencies = {'KRW', 'USD'} | {item.currency.upper() for _, item in rows}
        currency_health = {name.rsplit(':', 1)[-1].upper() for name, _ in sources.get(account, [])
                           if name.rsplit(':', 1)[-1] in ('krw', 'usd')}
        currencies |= currency_health
        native, cash, unsettled, exposure = {}, {}, {}, {}
        states = sources.get(account, [])
        source_reasons = [code for source, codes in getattr(collection, 'nav_source_quality_reasons', {}).items()
                          if _account_source(source) == account for code in codes]
        if account.startswith('toss:'):
            source_reasons.extend(code for currency, codes in (fx_quality or {}).items()
                                  if currency in currencies for code in codes)
        source_reasons = list(dict.fromkeys(source_reasons))
        toss_orders = (getattr(collection, 'nav_evidence', {}).get(account) or {}).get('open_orders', 'unverified')
        failed = bool(source_reasons) or any(s not in ('ok', 'empty') for _, s in states) or any(
            _account_source(name) == account for name in collection.truncated_sources)
        # A failed provider discovery also makes the corresponding account incomplete.
        failed |= any(health.get(provider, {}).get('status') not in (None, 'ok', 'empty')
                      for provider in {getattr(item, 'provider', '') for _, item in rows})
        for currency in sorted(currencies):
            positions = [item for kind, item in rows if kind == 'holding' and item.currency.upper() == currency]
            balances = [item for kind, item in rows if kind == 'cash' and item.currency.upper() == currency]
            reason = 'INCOMPLETE_ACCOUNT_SOURCE' if failed else 'ACCOUNT_SOURCE_QUALITY_UNKNOWN' if not states else None
            amounts, bases = [], []
            for balance in balances:
                settled = number(balance.settled_cash)
                if settled is not None:
                    amounts.append(settled)
                    bases.append('verified' if balance.source_fields.get('settled_cash') else 'derived')
                elif balance.kind == 'cash' and number(balance.amount) is not None:
                    amounts.append(number(balance.amount))
                    bases.append('derived')
                elif (balance.provider == 'toss' and balance.kind == 'buying_power'
                      and toss_orders == 'none' and number(balance.buying_power) is not None):
                    amounts.append(number(balance.buying_power))
                    bases.append(TOSS_CASH_BASIS)
                else:
                    amounts.append(None)
                    bases.append('unknown')
            cash_value = None if reason else total(amounts)
            cash_reason = reason or ('BUYING_POWER_IS_NOT_CASH' if balances and any(
                b.kind == 'buying_power' and b.settled_cash is None for b in balances)
                else 'MISSING_CASH_BALANCE' if not balances else 'CASH_BASIS_UNVERIFIED')
            if not reason and any(b.provider == 'toss' and b.kind == 'buying_power' for b in balances):
                cash_reason = 'TOSS_OPEN_ORDERS_PRESENT' if toss_orders == 'present' else 'TOSS_OPEN_ORDERS_UNVERIFIED'
            evidence_reasons = (getattr(collection, 'nav_evidence', {}).get(account) or {}).get('reason_codes', [])
            cash[currency] = {**metric(cash_value, *([cash_reason] if cash_value is None else [])),
                'basis': 'unknown' if cash_value is None else 'verified' if all(b == 'verified' for b in bases) else 'derived',
                'basis_reason': cash_reason if cash_value is None else 'BROKER_REPORTED_SETTLED_CASH' if all(
                    b == 'verified' for b in bases) else TOSS_CASH_BASIS if TOSS_CASH_BASIS in bases
                    else 'NORMALIZED_CASH_WITHOUT_VERIFIED_SOURCE_FIELD',
                'source_fields': [b.source_fields for b in balances]}
            if account.startswith('toss:'):
                exact = number(getattr(balances[0], 'amount_decimal', None)) if len(balances) == 1 else None
                state = health.get(f'{account}:{currency.lower()}') or {}
                cash[currency]['value_decimal'] = format(exact, 'f') if cash_value is not None and exact is not None else None
                cash[currency]['observed_at'] = state.get('last_success_at')
            if cash_value is None:
                cash[currency]['reason_codes'].extend(evidence_reasons)
                cash[currency]['reason_codes'].extend(source_reasons)
            position_reasons = []
            if account.startswith('toss:'):
                marks = [c for c in components or [] if c.get('account') == account
                         and c.get('component_type') == 'holding' and c.get('currency') == currency
                         and not (number(c.get('quantity')) is not None and number(c['quantity']) <= 0)]
                if components is None:
                    evidence = (getattr(collection, 'nav_evidence', {}).get(account) or {})
                    inventory_reasons = getattr(collection, 'nav_source_quality_reasons', {}).get(account, [])
                    marks = [toss_holding_valuation(p.to_dict(), evidence, source_reasons=inventory_reasons)
                             for p in positions]
                position_values = [(m.get('native_market_value') or {}).get('value_decimal',
                                   (m.get('native_market_value') or {}).get('value')) for m in marks]
                if len(marks) != len(positions):
                    position_values.append(None)
                for mark in marks:
                    position_reasons.extend((mark.get('native_market_value') or {}).get('reason_codes') or [])
            else:
                position_values = [getattr(p, 'market_value', None) for p in positions]
            value = total(position_values + [cash_value])
            native[currency] = metric(value, *([reason or 'MISSING_NATIVE_NAV_INPUT', *position_reasons, *source_reasons]
                                             if value is None else []))
            exposure[currency] = dict(native[currency])
            settlement = {}
            for key, field in (('receivables', 'unsettled_receivables'), ('payables', 'unsettled_payables')):
                amount = None if reason else total(getattr(b, field) for b in balances)
                if amount is not None and amount < 0:
                    amount = None
                settlement[key] = metric(amount, *([reason or 'MISSING_' + field.upper()] if amount is None else []))
            settlement['status'] = 'reported' if all(settlement[k]['value'] is not None for k in ('receivables', 'payables')) else 'unknown'
            settlement['basis'] = [{'native_cash_includes_unsettled': b.native_cash_includes_unsettled,
                                    'positions_basis': b.positions_basis,
                                    'reason': b.settlement_basis_reason} for b in balances]
            unsettled[currency] = settlement
        exposure['other'] = {c: value for c, value in exposure.items() if c not in ('KRW', 'USD')}
        accounts[account] = {'nav_native': native, 'cash': cash, 'unsettled': unsettled,
                             'currency_exposure': exposure,
                             'basis': 'holdings_plus_cash_before_settlement_adjustments',
                             'source_status': 'partial' if failed else 'complete' if states else 'unknown'}
        if account.startswith('toss:'):
            evidence = getattr(collection, 'nav_evidence', {}).get(account) or {}
            accounts[account]['cash_basis_evidence'] = {'open_orders': toss_orders,
                'open_order_count': evidence.get('open_order_count'),
                'observed_at': evidence.get('observed_at'), 'source': 'toss_orders.status=OPEN'}
            accounts[account]['instrument_bindings'] = evidence.get('instrument_bindings', {})
            accounts[account]['instrument_binding_reasons'] = evidence.get('instrument_binding_reasons', [])
    return accounts


def quality_reason_details(summary, reasons):
    """Attribute observed snapshot failures; unidentified causes remain global.

    The legacy bridge stays unchanged. Only the derived Toss native-cash basis
    resolves its settlement-basis warnings, without claiming settled cash or
    fabricating receivables/payables. All other bridge warnings remain blocking.
    """
    causes = {}

    def note(reason, *, account=None, currency=None, resolved=False):
        causes.setdefault(reason, []).append({'reason_code': reason, 'account': account,
            'currency': currency, 'resolved_for_native_nav': resolved})

    components = summary.get('components')
    if components is None:
        note('UNKNOWN_COMPONENT_PRICE_TIME')
    for component in components or []:
        for reason in component.get('reason_codes') or []:
            note(reason, account=component.get('account'), currency=component.get('currency'))
        if component.get('priced_at') is None:
            note('UNKNOWN_COMPONENT_PRICE_TIME', account=component.get('account'), currency=component.get('currency'))
    fx = summary.get('fx_times')
    if fx is None:
        note('UNKNOWN_FX_PRICE_TIME')
    for currency, mark in (fx or {}).items():
        if mark.get('priced_at') is None:
            note('UNKNOWN_FX_PRICE_TIME', currency=currency)
    quality = summary.get('mark_quality') or {}
    for quote in quality.get('quotes') or []:
        for reason in quote.get('reason_codes') or []:
            note(reason, account=quote.get('account'), currency=quote.get('currency'))
    for currency, mark in (quality.get('fx') or {}).items():
        for reason in mark.get('reason_codes') or []:
            note(reason, currency=currency)
    for account, row in (summary.get('account_nav') or {}).items():
        for metric in (row.get('nav_native') or {}).values():
            for reason in metric.get('reason_codes') or []:
                note(reason, account=account)
    for provider, row in (summary.get('crypto_nav') or {}).items():
        if isinstance(row, dict):
            for reason in row.get('reason_codes') or []:
                note(reason, account=provider)
    bridge = summary.get('nav_bridge') or {}
    for balance in bridge.get('cash_balances') or []:
        account, currency = balance.get('account'), balance.get('currency')
        native_cash = ((summary.get('account_nav') or {}).get(account) or {}).get('cash') or {}
        resolved = (str(account).startswith('toss:') and
                    (native_cash.get(currency) or {}).get('basis_reason') == TOSS_CASH_BASIS)
        if balance.get('broker_reported_totals'):
            note('BROKER_TOTAL_SCOPE_NOT_RECONCILED', account=account, currency=currency)
        if balance.get('native_cash_includes_unsettled') is None:
            note('UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS', account=account, currency=currency, resolved=resolved)
            continue
        if balance.get('positions_basis') not in ('trade_date', 'trade_date_reference_assumption'):
            note('UNKNOWN_POSITION_SETTLEMENT_BASIS', account=account, currency=currency)
            continue
        if balance.get('positions_basis') == 'trade_date_reference_assumption':
            note('SETTLEMENT_BASIS_REFERENCE_ASSUMPTION', account=account, currency=currency, resolved=resolved)
        for field in ('unsettled_receivables', 'unsettled_payables'):
            amount = number(balance.get(field))
            if amount is None or amount < 0:
                note(('MISSING_' if amount is None else 'INVALID_') + field.upper(), account=account, currency=currency)
    for component in components or []:
        account, currency = component.get('account'), component.get('currency')
        if currency != 'KRW' and number(((fx or {}).get(currency) or {}).get('rate_to_krw')) is None:
            note('MISSING_FX_RATE', account=account, currency=currency)
        if component.get('component_type') == 'holding' and (number(component.get('quantity')) or 0) > 0:
            if component.get('market_value') is None:
                native = component.get('native_market_value') or {}
                resolved = (str(account).startswith('toss:')
                            and component.get('valuation_price_source') == 'toss_prices'
                            and number(native.get('value')) is not None and not native.get('reason_codes'))
                note('MISSING_POSITION_VALUATION', account=account, currency=currency, resolved=resolved)
            if not any(b.get('account') == account for b in bridge.get('cash_balances') or []):
                note('MISSING_ACCOUNT_CASH_SETTLEMENT_DATA', account=account)
    source_quality = summary.get('collection_source_quality') or {}
    for source, codes in (source_quality.get('reason_codes_by_source') or {}).items():
        for reason in codes:
            note(reason, account=_account_source(source))
    for reason, key in (('FAILED_SOURCES', 'failed_sources'), ('TRUNCATED_SOURCES', 'truncated_sources')):
        for source in source_quality.get(key) or []:
            note(reason, account=_account_source(source))
    if source_quality.get('failed_sources') or source_quality.get('truncated_sources'):
        for source in [*(source_quality.get('failed_sources') or []), *(source_quality.get('truncated_sources') or [])]:
            note('INCOMPLETE_PORTFOLIO_SOURCES', account=_account_source(source))
    return [detail for reason in dict.fromkeys(reasons) for detail in causes.get(reason, [{
        'reason_code': reason, 'account': None, 'currency': None, 'resolved_for_native_nav': False}])]


def toss_holding_valuation(component, evidence, *, source_reasons=()):
    """Value native NAV from one quote object; keep legacy price drift visible."""
    def price(value):
        return number(value.replace(',', '').strip() if isinstance(value, str) else value)

    quote = (evidence.get('quotes') or {}).get(component.get('symbol'))
    quote = quote if isinstance(quote, dict) else {}
    legacy_price = price(component.get('legacy_price_decimal',
                         component.get('valuation_price_decimal', component.get('last_price'))))
    quote_price = price(quote.get('last_price'))
    stamp = quote.get('priced_at')
    quantity = number(component.get('quantity_decimal') or component.get('quantity'))
    reasons = list(dict.fromkeys([*(quote.get('reason_codes') or []), *source_reasons]))
    if not quote:
        reasons.append('MISSING_TOSS_QUOTE')
    elif not reasons:
        if quote.get('currency') != component.get('currency'):
            reasons.append('QUOTE_CURRENCY_MISMATCH')
        if quote_price is None or quote_price <= 0:
            reasons.append('INVALID_QUOTE_PRICE')
    if stamp is None:
        reasons.append('UNKNOWN_QUOTE_TIME')
    elif instant(stamp) is None:
        reasons.append('INVALID_QUOTE_TIME')
    if quantity is None or quantity <= 0:
        reasons.append('INVALID_POSITION_QUANTITY')
    amount = quantity * quote_price if not reasons else None
    value = metric(amount, *reasons)
    value['value_decimal'] = str(amount) if value['value'] is not None else None
    return {'valuation_price_source': 'toss_prices', 'priced_at': stamp,
            'valuation_price_decimal': str(quote_price) if quote_price is not None else None,
            'legacy_price_decimal': str(legacy_price) if legacy_price is not None else None,
            'quote_price_decimal': str(quote_price) if quote_price is not None else None,
            'quote_currency': quote.get('currency'),
            'price_drift_decimal': str(quote_price - legacy_price)
                if quote_price is not None and legacy_price is not None else None,
            'native_market_value': value, 'reason_codes': value['reason_codes']}


def _incomplete_calendar(value):
    """Inspect metadata before sanitizing, including nested response errors."""
    from toss_nav_validation import incomplete_envelope
    return incomplete_envelope(value)


def calendar_evidence(payload, country):
    """Retain complete, consistent regular sessions; discard provider metadata."""
    if not isinstance(payload, dict) or country not in ('KR', 'US') or _incomplete_calendar(payload):
        return None
    if 'result' in payload:
        payload = payload['result']
        if not isinstance(payload, dict):
            return None
    days = {}
    for key in ('previousBusinessDay', 'today', 'nextBusinessDay'):
        row = payload.get(key)
        if not isinstance(row, dict) or not isinstance(row.get('date'), str):
            return None
        sessions = row.get('integrated') if country == 'KR' else row
        if country == 'KR' and 'integrated' not in row:
            return None
        if sessions is not None and (not isinstance(sessions, dict) or 'regularMarket' not in sessions):
            return None
        regular = sessions.get('regularMarket') if sessions is not None else None
        if regular is not None and not isinstance(regular, dict):
            return None
        days[key] = {'date': row['date'], 'regularMarket': None if regular is None else {
            'startTime': regular.get('startTime'), 'endTime': regular.get('endTime')}}
    calendar = {'country': country, **days}
    return calendar if _calendar_sessions(calendar) is not None else None


def _calendar_sessions(calendar):
    """Validate stored evidence too, including each close's local trading date."""
    if (not isinstance(calendar, dict) or calendar.get('country') not in ('KR', 'US')
            or _incomplete_calendar(calendar)):
        return None
    from toss_nav_validation import calendar_shape
    if not calendar_shape(calendar):
        return None
    zone = ZoneInfo('Asia/Seoul' if calendar['country'] == 'KR' else 'America/New_York')
    try:
        previous, today, following = [calendar[key] for key in ('previousBusinessDay', 'today', 'nextBusinessDay')]
        dates = [datetime.strptime(row['date'], '%Y-%m-%d').date() for row in (previous, today, following)]
        if not dates[0] < dates[1] < dates[2] or any(
                date.isoformat() != row['date'] for date, row in zip(dates, (previous, today, following))):
            return None
        closes = []
        for index, row in enumerate((previous, today, following)):
            session = row['regularMarket']
            if session is None:
                if index != 1:
                    return None
                continue  # Explicit holiday in the calendar.
            start, close = instant(session['startTime']), instant(session['endTime'])
            if (start is None or close is None or start >= close
                    or start.astimezone(zone).date() != dates[index]
                    or close.astimezone(zone).date() != dates[index]):
                return None
            closes.append(close)
        if closes != sorted(closes):
            return None
        return zone, dates, closes
    except (KeyError, TypeError, ValueError):
        return None


def _calendar_current(calendar, priced, valued):
    """None means unsupported coverage; no inferred weekday/holiday schedule."""
    sessions = _calendar_sessions(calendar)
    if sessions is None:
        return None
    zone, dates, closes = sessions
    if dates[1] != valued.astimezone(zone).date() or not closes[0] <= valued < closes[-1]:
        return None
    # The previous business close bounds the proven interval, including
    # intervening weekends/holidays; an older mark already misses that close.
    return not any(priced < close <= valued for close in closes)


def _mark(stamp, observed, valuation, prefix, *, calendar=None, require_calendar=False,
          valid_until=None, valid_until_supplied=False, evidence_until=None):
    priced, valued = instant(stamp), instant(valuation)
    reasons = []
    age = after = None
    if priced is None:
        reasons.append('UNKNOWN_' + prefix + '_TIME' if stamp is None else 'INVALID_' + prefix + '_TIME')
    elif valued is None:
        reasons.append('UNKNOWN_VALUATION_TIME')
    else:
        age = (valued - priced).total_seconds()
        completed = instant(evidence_until)
        if age < 0 and (completed is None or priced > completed):
            reasons.append('FUTURE_' + prefix + '_TIME')
        else:
            if age < 0:
                # Read during the evidence collection, after the valuation instant
                # (collection start): no older than its read, never a negative age.
                after, age = -age, 0.0
            if prefix == 'QUOTE' and require_calendar:
                current = _calendar_current(calendar, priced, valued)
                if current is None:
                    reasons.append('MARK_FRESHNESS_UNVERIFIED')
                elif not current:
                    reasons.append('STALE_QUOTE')
            elif prefix == 'FX' and (valid_until_supplied or valid_until is not None):
                until = instant(valid_until)
                if until is None or until < priced:
                    reasons.append('INVALID_FX_VALID_UNTIL')
                elif valued > until:
                    reasons.append('STALE_FX')
            elif age > MAX_MARK_AGE_SECONDS:
                reasons.append('STALE_' + prefix)
    return {'priced_at': stamp, 'observed_at': observed, 'age_seconds': age,
            **({'priced_after_valuation_seconds': after} if after is not None else {}),
            'status': 'unknown' if age is None or any(r in reasons for r in (
                'MARK_FRESHNESS_UNVERIFIED', 'INVALID_FX_VALID_UNTIL')) else 'stale' if reasons else 'current',
            'reason_codes': reasons}


def fx_mark_quality(row, valuation):
    """Validate the rate at its own read completion, never later NAV completion.

    Legacy evidence without a per-currency read time uses valuation_at. A present
    invalid read time stays unknown; it cannot silently fall back or widen expiry.
    """
    checked_at = row.get('read_at') if 'read_at' in row else valuation
    result = {**_mark(row.get('priced_at'), row.get('observed_at'), checked_at, 'FX',
                   valid_until=row.get('valid_until'), valid_until_supplied='valid_until' in row),
            'freshness_checked_at': checked_at,
            'freshness_time_basis': 'fx_read' if 'read_at' in row else 'valuation_at'}
    if row.get('reason_codes'):
        result['reason_codes'] = list(dict.fromkeys([*result['reason_codes'], *row['reason_codes']]))
        result['status'] = 'unknown'
    return result


def mark_quality(summary):
    # Completion bounds only the optional Toss evidence reads. Legacy valuation
    # remains the collection instant for crypto selection and cash-flow cutoffs.
    evidence_at = summary.get('nav_evidence_completed_at')
    return {'max_mark_age_seconds': MAX_MARK_AGE_SECONDS,
        'quotes': [dict(_mark(c.get('priced_at'), summary.get('observed_at'),
                             summary.get('valuation_at'), 'QUOTE', calendar=c.get('market_calendar'),
                             require_calendar=c.get('component_type') == 'holding' and c.get('provider') == 'toss',
                             evidence_until=evidence_at if str(c.get('account')).startswith('toss:') else None),
                        account=c.get('account'), currency=c.get('currency'), symbol=c.get('symbol'),
                        component_type=c.get('component_type')) for c in summary.get('components') or []],
        'fx': {currency: fx_mark_quality(row, summary.get('valuation_at'))
               for currency, row in (summary.get('fx_times') or {}).items()}}


def crypto_boundary(crypto):
    """The single instant a stored crypto NAV represents: (boundary, venue_times, reasons).

    The boundary is the risk capture completion the valuation selected
    (crypto_nav.observed_at). Each venue's NAV state was read at its accounts'
    nav_price_time (observed_at before NAV evidence existed); venue_times maps a
    venue to its (first, last) read. All reads of all venues and the boundary
    must fit in one CRYPTO_BOUNDARY_TOLERANCE_SECONDS span, else there is no
    single instant and the boundary is None. Synthetic payloads without crypto
    timing metadata return (None, {}, []), as legacy scope quality does.
    """
    if 'observed_at' not in crypto:
        return None, {}, []
    boundary = instant(crypto.get('observed_at'))
    reasons, venues = [] if boundary else ['UNKNOWN_CRYPTO_OBSERVATION_TIME'], {}
    for venue in ('binance', 'hyperliquid'):
        times = [instant(row.get('nav_price_time') if 'nav_price_time' in row else row.get('observed_at'))
                 for row in (crypto.get(venue) or {}).get('accounts') or []]
        if not times or any(time is None for time in times):
            reasons.append('UNKNOWN_CRYPTO_OBSERVATION_TIME')
            continue
        venues[venue] = (min(times), max(times))
    times = [time for span in venues.values() for time in span] + ([boundary] if boundary else [])
    if not _coherent(times):
        reasons.append('CRYPTO_OBSERVATION_TIMES_INCOHERENT')
    return None if reasons else boundary, venues, list(dict.fromkeys(reasons))


def _coherent(times):
    return not times or (max(times) - min(times)).total_seconds() <= CRYPTO_BOUNDARY_TOLERANCE_SECONDS


def stablecoin_mark_fresh(priced, read):
    """The capture's stablecoin mark age rule (crypto_nav_capture.validate_tickers).

    The last trade lies at most MAX_STABLECOIN_AGE_SECONDS before the read and at
    most the clock skew after it. Upbit's thin KRW-USDC market often last traded
    minutes ago, so the age is taken where the mark was read, never at a later instant.
    """
    if priced is None or read is None:
        return False
    return -STABLECOIN_CLOCK_SKEW_SECONDS <= (read - priced).total_seconds() <= MAX_STABLECOIN_AGE_SECONDS


def _mark_read(mark, legacy):
    # As fx_mark_quality: evidence without its own read time uses the instant valued.
    return instant(mark.get('read_at')) if 'read_at' in mark else legacy


def _joins_capture(mark, boundary, reads):
    read = _mark_read(mark, boundary)
    return stablecoin_mark_fresh(instant(mark.get('priced_at')), read) and _coherent([boundary, read, *reads])


def crypto_fx_mark_current(mark, crypto):
    """Whether a stablecoin mark may value the crypto boundary (crypto_boundary).

    It must pass the capture's own age rule at its read (stablecoin_mark_fresh), and
    that read must join the capture's coherent span: with every venue read and the
    boundary it fits in one CRYPTO_BOUNDARY_TOLERANCE_SECONDS span. Without a
    coherent boundary no mark is current. The NAV write applies the same rule.
    """
    boundary, venues, _ = crypto_boundary(crypto)
    return boundary is not None and _joins_capture(
        mark, boundary, [time for span in venues.values() for time in span])


def crypto_nav(conn, summary):
    """Optional crypto evidence cannot fail a successful broker snapshot."""
    try:
        result = _crypto_nav(conn, summary)
        json.dumps(result, allow_nan=False)
        return result
    except Exception:
        reason = 'CRYPTO_NAV_EVIDENCE_UNAVAILABLE'
        unavailable = metric(None, reason)
        return {'crypto_nav': {'risk_snapshot_id': None, 'observed_at': None, 'age_seconds': None,
            'max_age_seconds': MAX_CRYPTO_AGE_SECONDS,
            **{venue: {'by_currency': {}, 'value_krw': None, 'value_krw_decimal': None,
                'status': unavailable['status'], 'reason_codes': [reason], 'accounts': []}
                for venue in ('binance', 'hyperliquid')}},
            'consolidated_nav_krw': {**unavailable,
                'basis': consolidated_nav_basis(summary)}}


def _crypto_nav(conn, summary):
    """Read the nearest immutable risk capture; never collect crypto live.

    Only one row per collateral/account may contribute. Shared wallets and
    per-dex observations are never summed. Direct collateral FX is preferred;
    stablecoin/USD parity is never silently assumed. A USD stablecoin with no
    direct mark at all (the live summary: only the 18:10 capture reads Upbit)
    is valued at the USD rate, labelled ``fx_proxy: USD`` on the currency, the
    venue and the consolidated total, when that rate passes its own FX age rule
    (fx_mark_quality) instead of the capture-span rule. A direct mark that is
    present but unusable never falls back. Stored returns refuse proxied
    endpoints (ledger_returns: no coin mark; crypto_flow_intervals._complete).
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} if conn else set()
    valuation = instant(summary.get('valuation_at'))
    max_age = summary.get('crypto_max_age_seconds', MAX_CRYPTO_AGE_SECONDS)
    crypto = {'risk_snapshot_id': None, 'observed_at': None, 'age_seconds': None,
              'max_age_seconds': max_age}
    if 'crypto_fx_times' in summary:
        crypto['fx_times'] = summary['crypto_fx_times']
    reason = 'UNKNOWN_VALUATION_TIME' if valuation is None else 'MISSING_CRYPTO_SNAPSHOT'
    selected = None
    if valuation and {'futures_risk_snapshots', 'futures_risk_accounts', 'futures_risk_venues'} <= tables:
        selected = conn.execute('''SELECT snapshot_id, completed_at, calculation_version, payload_json FROM futures_risk_snapshots
            WHERE julianday(completed_at) IS NOT NULL AND (? IS NULL OR snapshot_id=?)
            ORDER BY ABS(julianday(completed_at)-julianday(?)), completed_at DESC, snapshot_id LIMIT 1''',
            (summary.get('crypto_risk_snapshot_id'), summary.get('crypto_risk_snapshot_id'), valuation.isoformat())).fetchone()
    if selected:
        observed = instant(selected[1])
        crypto.update(risk_snapshot_id=selected[0], observed_at=selected[1], calculation_version=selected[2],
                      age_seconds=abs((valuation - observed).total_seconds()) if observed else None)
        reason = None if crypto['age_seconds'] is not None and crypto['age_seconds'] <= max_age else 'STALE_CRYPTO_SNAPSHOT'
    import json
    accounts = [json.loads(r[0]) for r in conn.execute(
        'SELECT payload_json FROM futures_risk_accounts WHERE snapshot_id=? ORDER BY ordinal', (selected[0],))] if selected else []
    venues = {r[0]: {'source_status': r[1], **json.loads(r[2])} for r in conn.execute(
        'SELECT venue, source_status, payload_json FROM futures_risk_venues WHERE snapshot_id=?', (selected[0],))} if selected else {}
    if selected:
        header = json.loads(selected[3])
        if 'nav_evidence' in header or selected[2] == 'futures_risk_v5_nav':
            # New captures keep NAV wholly separate from legacy risk rows.
            # Missing/malformed supplemental payload cannot fall back to risk.
            supplemental = header['nav_evidence']
            if supplemental.get('version') != 'crypto_risk_nav_v1':
                raise ValueError('CRYPTO_NAV_EVIDENCE_UNAVAILABLE')
            accounts = supplemental['accounts']
            venues = {r['venue']: r for r in supplemental['venues']}
    # A mark values this capture only if its read joins the capture's coherent span
    # (crypto_fx_mark_current): its completion and every venue NAV read it holds.
    completed = instant(crypto['observed_at'])
    reads = [instant(r.get('nav_price_time') if 'nav_price_time' in r else r.get('observed_at'))
             for r in accounts if r.get('venue') in ('binance', 'hyperliquid') and r.get('nav_eligible', True)]
    reads = [read for read in reads if read is not None]
    for venue in ('binance', 'hyperliquid'):
        rows = [r for r in accounts if r.get('venue') == venue and r.get('nav_eligible', True)]
        reasons = ([reason] if reason else []) + list(summary.get('crypto_capture_reasons') or [])
        evidence = venues.get(venue, {})
        reasons.extend(evidence.get('nav_reason_codes') or [])
        if not reason and (not rows or evidence.get('nav_source_status', evidence.get('source_status')) != 'complete'):
            reasons.append('INCOMPLETE_CRYPTO_VENUE')
        values, seen, provenance = {}, set(), []
        for row in rows:
            key = (row.get('account_id'), row.get('asset'))
            if key in seen:
                reasons.append('DUPLICATE_COLLATERAL_SCOPE')
            seen.add(key)
            row_time = instant(row.get('observed_at'))
            if valuation is not None and (row_time is None or abs((valuation - row_time).total_seconds()) > max_age):
                reasons.append('STALE_CRYPTO_ACCOUNT_OBSERVATION')
            if 'nav_price_time' in row:
                price_time = instant(row['nav_price_time'])
                if valuation is None or price_time is None or abs((valuation - price_time).total_seconds()) > max_age:
                    reasons.append('STALE_CRYPTO_ACCOUNT_PRICE')
            reasons.extend(row.get('nav_reason_codes') or [])
            if row.get('nav_source_status', row.get('source_status')) != 'complete':
                reasons.append('INCOMPLETE_CRYPTO_ACCOUNT')
            if venue == 'hyperliquid':
                if row.get('abstraction') == 'unifiedAccount' or row.get('scope') == 'unified_shared_wallet':
                    if not (row.get('scope') == 'unified_shared_wallet' and row.get('abstraction') == 'unifiedAccount'
                            and 'nav_equity' in row and row.get('asset') == 'USDC'):
                        reasons.append('UNIFIED_EQUITY_UNVERIFIED')
                elif (row.get('scope') != 'perp_dex' or row.get('dex') != ''
                      or row.get('collateral_status') != 'known' or row.get('asset') != 'USDC'
                      or row.get('abstraction') not in ('default', 'disabled')):
                    reasons.append('HYPERLIQUID_DEX_AGGREGATION_UNVERIFIED')
                identity = row.get('account_id')
                if sum(r.get('account_id') == identity for r in rows) != 1:
                    reasons.append('HYPERLIQUID_DEX_AGGREGATION_UNVERIFIED')
            elif row.get('scope') != 'futures_collateral' or row.get('dex') != '':
                reasons.append('UNVERIFIED_COLLATERAL_SCOPE')
            equity = row.get('nav_equity', row.get('equity')) or {}
            value = number(equity.get('value'))
            currency = row.get('asset')
            if not row.get('account_id') or not currency or value is None or equity.get('status') not in ('reported', 'derived'):
                reasons.extend(equity.get('reason_codes') or ['CRYPTO_EQUITY_UNAVAILABLE'])
            elif not reason:
                values.setdefault(currency, []).append(value)
            provenance.append({k: row.get(k) for k in ('account_id', 'ledger_account_identity', 'scope', 'dex',
                'asset', 'abstraction', 'observed_at', 'nav_price_time', 'nav_price_time_basis', 'nav_account_mode')})
        native = {c: {**metric(None if reasons else total(v), *(reasons if reasons else [])),
                      'decimal_value': str(total(v)) if not reasons else None} for c, v in values.items()}
        converted, proxied = [], []
        for currency, value in native.items():
            if value['decimal_value'] is not None and number(value['decimal_value']) == 0:
                converted.append(number('0'))
                continue  # A proven zero asset needs no invented FX mark.
            if (currency in USD_PROXY_STABLECOINS and currency not in (summary.get('crypto_fx_times') or {})
                    and currency not in (summary.get('fx_times') or {})):
                usd = (summary.get('fx_times') or {}).get('USD') or {}
                rate = number(usd.get('rate_to_krw'))
                if rate is None or rate <= 0:
                    reasons.append('MISSING_CRYPTO_FX_RATE')
                elif usd.get('reason_codes') or instant(usd.get('priced_at')) is None or valuation is None:
                    reasons.extend(usd.get('reason_codes') or ['CRYPTO_FX_TIME_UNAVAILABLE'])
                elif fx_mark_quality(usd, summary.get('valuation_at'))['status'] != 'current':
                    reasons.append('STALE_CRYPTO_FX_RATE')
                elif value['value'] is not None:
                    converted.append(number(value['decimal_value']) * rate)
                    value['fx_proxy'] = 'USD'
                    proxied.append(currency)
                continue
            mark = (summary.get('crypto_fx_times') or {}).get(currency,
                (summary.get('fx_times') or {}).get(currency, {}))
            rate = number(mark.get('rate_to_krw'))
            priced = instant(mark.get('priced_at'))
            if rate is None or rate <= 0:
                reasons.append('MISSING_CRYPTO_FX_RATE')
            elif mark.get('reason_codes') or priced is None or valuation is None:
                reasons.extend(mark.get('reason_codes') or ['CRYPTO_FX_TIME_UNAVAILABLE'])
            elif completed is None or not _joins_capture(mark, completed, reads):
                reasons.append('STALE_CRYPTO_FX_RATE')
            elif value['value'] is not None:
                converted.append(number(value['decimal_value']) * rate)
        krw = metric(None if reasons else total(converted), *reasons)
        crypto[venue] = {'by_currency': native, 'value_krw': krw['value'], 'status': krw['status'],
                         'value_krw_decimal': str(total(converted)) if not reasons and converted else None,
                         'reason_codes': krw['reason_codes'], 'accounts': provenance}
        if proxied and not reasons:
            crypto[venue].update(fx_proxy='USD', fx_proxy_currencies=proxied)
    reasons = crypto['binance']['reason_codes'] + crypto['hyperliquid']['reason_codes']
    # Legacy total remains visible even for forced partial captures; a new total
    # must refuse their missing cash/positions/FX rather than inherit that zero.
    if not summary.get('collection_complete', False):
        reasons.append('INCOMPLETE_PORTFOLIO_SOURCES')
    for account, row in (summary.get('account_nav') or {}).items():
        observed_currencies = {c.get('currency', '').upper() for c in summary.get('components') or [] if c.get('account') == account}
        if not observed_currencies or row['source_status'] != 'complete' or any(row['nav_native'][c]['value'] is None for c in observed_currencies):
            reasons.append('INCOMPLETE_NATIVE_NAV')
    if not summary.get('account_nav'):
        reasons.append('MISSING_NATIVE_NAV')
    if 'MISSING_FX_RATE' in summary.get('reason_codes', []):
        reasons.append('MISSING_FX_RATE')
    consolidated = metric(None if reasons else total([summary.get('total_assets_krw'),
        crypto['binance']['value_krw'], crypto['hyperliquid']['value_krw']]), *reasons)
    consolidated['basis'] = consolidated_nav_basis(summary)
    if consolidated['value'] is not None and any(crypto[v].get('fx_proxy') for v in ('binance', 'hyperliquid')):
        consolidated['fx_proxy'] = 'USD'
    return {'crypto_nav': crypto, 'consolidated_nav_krw': consolidated}


def external_flows(conn, valuation_at):
    """Freeze already classified ledger observations, without certifying coverage.

    Identity aliases and classification/enrichment are owned by the ledger. NAV
    reads only existing masked event identities; it never reads identity salts.
    """
    import json
    from zoneinfo import ZoneInfo

    valued = instant(valuation_at)
    result = {'status': 'unknown', 'net_by_currency': None, 'reason_codes': ['EXTERNAL_FLOW_COVERAGE_UNVERIFIED'],
              'observed_events': [], 'valuation_timezone': VALUATION_TIMEZONE}
    if valued is None or conn is None:
        return result
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'ledger_events', 'ledger_transfer_classifications'} <= tables:
        result['reason_codes'] = ['MISSING_EXTERNAL_FLOW_SOURCE']
        return result
    day = valued.astimezone(ZoneInfo(VALUATION_TIMEZONE)).date().isoformat()
    result['date'] = day
    for event_id, payload, classification in conn.execute('''SELECT e.event_id, e.payload_json,
        (SELECT c.classification FROM ledger_transfer_classifications c WHERE c.event_id=e.event_id
         ORDER BY c.revision DESC LIMIT 1) FROM ledger_events e
        WHERE e.kind IN ('deposit', 'withdrawal', 'transfer', 'transfer_internal',
                         'transfer_external_in', 'transfer_external_out') ORDER BY e.event_id'''):
        row = json.loads(payload)
        if 'ledger_enrichments' in tables:
            for enrichment in conn.execute('SELECT fields_json FROM ledger_enrichments WHERE event_id=? ORDER BY rowid', (event_id,)):
                row.update(json.loads(enrichment[0]))
        when = instant(row.get('execution_time_utc'))
        if when is None or when > valued or when.astimezone(ZoneInfo(VALUATION_TIMEZONE)).date().isoformat() != day:
            continue
        classification = classification or 'unknown'
        if classification in ('external_in', 'external_out', 'unknown'):
            result['observed_events'].append({'event_id': event_id, 'account_identity': row.get('account_identity'),
                'classification': classification, 'currency': row.get('currency'), 'amount': row.get('amount'),
                'execution_time_utc': row.get('execution_time_utc')})
    return result


def unavailable_local_evidence(summary):
    """Optional local storage must not turn a successful broker read into failure."""
    fields = crypto_nav(None, summary)
    reason = 'LOCAL_NAV_EVIDENCE_UNAVAILABLE'
    for venue in ('binance', 'hyperliquid'):
        fields['crypto_nav'][venue]['reason_codes'].append(reason)
    fields['consolidated_nav_krw']['reason_codes'].append(reason)
    fields['external_flows'] = external_flows(None, summary.get('valuation_at'))
    fields['external_flows']['reason_codes'].append(reason)
    return fields
