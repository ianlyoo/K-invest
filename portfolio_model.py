#!/usr/bin/env python3
"""Normalized portfolio domain model shared by every broker.

Broker payloads never leave this module: `Transaction`, `Holding`, and
`CashBalance` are the only shapes the engine and the MCP tools work with.

Conventions (applied uniformly across providers):
  - `trade_date` / `settle_date` are ISO `YYYY-MM-DD` strings.
  - `quantity` is always positive; direction comes from `tx_type`.
  - `gross_amount` is quantity x price in the instrument's own currency.
  - `net_amount` is the signed cash effect in that currency: negative when cash
    leaves the account (BUY, WITHDRAWAL, FEE, TAX), positive when it arrives
    (SELL, DEPOSIT, DIVIDEND, INTEREST).
  - Buy-side fees and taxes are capitalized into cost basis; sell-side fees and
    taxes reduce proceeds. See portfolio_engine.compute_realized_pnl.
  - `fx_rate` converts the native currency to KRW as of the trade, and is only
    set when the broker reports it (KIS 등록환율/최초고시환율). Valuation uses
    current spot rates instead — see FxRates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from journal_metadata import identity

# Instrument currency → market code, used when a provider omits the market.
_CURRENCY_MARKET = {"KRW": "KR", "USD": "US", "JPY": "JP", "HKD": "HK", "CNY": "CN"}

# KIS 매도매수구분코드
_KIS_SIDE = {"01": "SELL", "02": "BUY"}


class TxType(str, Enum):
    """Ledger entry types.

    BUY/SELL/DIVIDEND/FEE/TAX are populated from broker APIs today. The cash-flow
    and corporate-action members are part of the schema so that a ledger can carry
    them once a source exists (see docs: neither Toss nor the KIS open API exposes
    account deposits, withdrawals, or corporate actions).
    """

    BUY = "BUY"
    SELL = "SELL"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    FEE = "FEE"
    TAX = "TAX"
    FX = "FX"
    TRANSFER = "TRANSFER"
    SPLIT = "SPLIT"
    SPINOFF = "SPINOFF"
    MERGER = "MERGER"
    TICKER_CHANGE = "TICKER_CHANGE"
    OTHER = "OTHER"


#: Types that change share count and therefore cost basis.
POSITION_TYPES = {TxType.BUY, TxType.SELL, TxType.SPLIT, TxType.SPINOFF, TxType.MERGER, TxType.TICKER_CHANGE}
#: Types that move cash without changing share count.
INCOME_TYPES = {TxType.DIVIDEND, TxType.INTEREST}
CASHFLOW_TYPES = {TxType.DEPOSIT, TxType.WITHDRAWAL, TxType.TRANSFER}


@dataclass(frozen=True, slots=True)
class Transaction:
    """One normalized ledger entry."""

    provider: str
    account: str
    tx_id: str
    tx_type: TxType
    trade_date: str
    currency: str
    settle_date: str | None = None
    symbol: str | None = None
    name: str | None = None
    market: str | None = None
    exchange: str | None = None
    quantity: float = 0.0
    price: float | None = None
    gross_amount: float | None = None
    fee: float = 0.0
    tax: float = 0.0
    net_amount: float | None = None
    fx_rate: float | None = None
    status: str = "FILLED"
    source: str = ""
    note: str | None = None
    execution_id: str | None = None
    identity_quality: str = "unavailable"
    account_key: str | None = None
    parent_order_id: str | None = None
    identity_date: str | None = None

    @property
    def quantity_delta(self) -> float:
        """Signed share-count change this entry applies to the position."""
        if self.tx_type is TxType.BUY:
            return self.quantity
        if self.tx_type is TxType.SELL:
            return -self.quantity
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "tx_id": self.tx_id,
            "execution_id": self.execution_id,
            "identity_quality": self.identity_quality,
            "identity_version": 1,
            "record_kind": "order_aggregate",
            "account_key": self.account_key or self.account,
            "parent_order_id": self.parent_order_id,
            "tx_type": self.tx_type.value,
            "trade_date": self.trade_date,
            "settle_date": self.settle_date,
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
            "currency": self.currency,
            "quantity": self.quantity,
            "price": self.price,
            "gross_amount": self.gross_amount,
            "fee": self.fee,
            "tax": self.tax,
            "net_amount": self.net_amount,
            "fx_rate": self.fx_rate,
            "status": self.status,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Holding:
    """One position as a single account currently reports it."""

    provider: str
    account: str
    symbol: str
    currency: str
    quantity: float
    name: str | None = None
    market: str | None = None
    exchange: str | None = None
    last_price: float | None = None
    avg_price: float | None = None
    cost_basis: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
            "currency": self.currency,
            "quantity": self.quantity,
            "last_price": self.last_price,
            "avg_price": self.avg_price,
            "cost_basis": self.cost_basis,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
        }


@dataclass(frozen=True, slots=True)
class CashBalance:
    """Cash held in one account and currency."""

    provider: str
    account: str
    currency: str
    amount: float
    kind: str = "cash"  # "cash" | "buying_power"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "currency": self.currency,
            "amount": self.amount,
            "kind": self.kind,
        }


@dataclass
class FxRates:
    """Spot rates used to express native-currency values in KRW.

    Rates are `currency -> KRW` multipliers. Missing currencies are reported via
    `missing` instead of being silently treated as 1.0, so callers can mark a
    total as partially unconverted.
    """

    rates: dict[str, float] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    as_of: str | None = None

    def rate(self, currency: str) -> float | None:
        cur = (currency or "").upper()
        if cur == "KRW":
            return 1.0
        rate = self.rates.get(cur)
        if rate is None:
            self.missing.add(cur)
            return None
        return rate

    def to_krw(self, amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        rate = self.rate(currency)
        return None if rate is None else amount * rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates_to_krw": dict(sorted(self.rates.items())),
            "as_of": self.as_of,
            "missing_currencies": sorted(self.missing),
        }


# ── Parsing helpers ──────────────────────────────────────


def to_float(value: Any) -> float | None:
    """Parse broker numerics, which arrive as comma-formatted strings."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _f(value: Any, default: float = 0.0) -> float:
    parsed = to_float(value)
    return default if parsed is None else parsed


def iso_date(value: Any) -> str | None:
    """Normalize YYYYMMDD / ISO datetime / YYYY-MM-DD into YYYY-MM-DD."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def market_for(currency: str, symbol: str = "") -> str:
    cur = (currency or "").upper()
    if cur in _CURRENCY_MARKET:
        return _CURRENCY_MARKET[cur]
    return "KR" if symbol.isdigit() else "US"


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / abs(denominator) * 100, 4)


# ── Toss normalizers ─────────────────────────────────────


def toss_holdings(payload: Any, account: str) -> list[Holding]:
    """Normalize `GET /api/v1/holdings` (HoldingsOverview) into Holdings."""
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    items = result.get("items") if isinstance(result, dict) else None
    holdings: list[Holding] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        quantity = _f(item.get("quantity"))
        if not symbol or quantity <= 0:
            continue
        currency = str(item.get("currency") or "").upper() or ("KRW" if symbol.isdigit() else "USD")
        market_value_node = item.get("marketValue") if isinstance(item.get("marketValue"), dict) else {}
        pnl_node = item.get("profitLoss") if isinstance(item.get("profitLoss"), dict) else {}
        cost_basis = to_float(market_value_node.get("purchaseAmount"))
        avg_price = to_float(item.get("averagePurchasePrice"))
        if cost_basis is None and avg_price is not None:
            cost_basis = avg_price * quantity
        market_value = to_float(market_value_node.get("amount"))
        unrealized = to_float(pnl_node.get("amount"))
        rate = to_float(pnl_node.get("rate"))
        holdings.append(
            Holding(
                provider="toss",
                account=account,
                symbol=symbol,
                name=item.get("name"),
                market=str(item.get("marketCountry") or market_for(currency, symbol)).upper(),
                exchange=None,
                currency=currency,
                quantity=quantity,
                last_price=to_float(item.get("lastPrice")),
                avg_price=avg_price,
                cost_basis=cost_basis,
                market_value=market_value,
                unrealized_pnl=unrealized,
                # Toss reports `rate` as a fraction (0.1077 = 10.77%).
                unrealized_pnl_pct=round(rate * 100, 4) if rate is not None else _pct(unrealized, cost_basis),
            )
        )
    return holdings


def toss_transactions(orders: list[dict[str, Any]], account: str) -> list[Transaction]:
    """Normalize Toss orders into BUY/SELL ledger entries.

    Orders with no fill are skipped; partially filled and partially-filled-then-
    cancelled orders contribute their filled quantity.
    """
    transactions: list[Transaction] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        execution = order.get("execution") if isinstance(order.get("execution"), dict) else {}
        quantity = _f(execution.get("filledQuantity"))
        if quantity <= 0:
            continue
        side = str(order.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        tx_type = TxType.BUY if side == "BUY" else TxType.SELL
        symbol = str(order.get("symbol") or "").strip().upper()
        currency = str(order.get("currency") or "").upper() or ("KRW" if symbol.isdigit() else "USD")
        price = to_float(execution.get("averageFilledPrice"))
        gross = to_float(execution.get("filledAmount"))
        if gross is None and price is not None:
            gross = price * quantity
        fee = _f(execution.get("commission"))
        tax = _f(execution.get("tax"))
        trade_date = iso_date(execution.get("filledAt")) or iso_date(order.get("orderedAt"))
        if trade_date is None:
            continue
        transactions.append(
            Transaction(
                provider="toss",
                account=account,
                tx_id=str(order.get("orderId") or ""),
                identity_date=iso_date(order.get("orderedAt")) or trade_date,
                **{k: v for k, v in identity("toss", account, market_for(currency, symbol), str(order.get("orderId") or ""), iso_date(order.get("orderedAt")) or trade_date).items() if k in {"execution_id", "identity_quality", "account_key", "parent_order_id"}},
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=iso_date(execution.get("settlementDate")),
                symbol=symbol,
                name=None,
                market=market_for(currency, symbol),
                exchange=None,
                currency=currency,
                quantity=quantity,
                price=price,
                gross_amount=gross,
                fee=fee,
                tax=tax,
                net_amount=_net_amount(tx_type, gross, fee, tax),
                fx_rate=None,
                status=str(order.get("status") or "FILLED"),
                source="toss:orders",
            )
        )
    return transactions


def toss_cash(payload: Any, account: str, currency: str) -> list[CashBalance]:
    """Normalize `GET /api/v1/buying-power` into a CashBalance."""
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    amount = to_float(result.get("cashBuyingPower")) if isinstance(result, dict) else None
    if amount is None:
        return []
    return [
        CashBalance(
            provider="toss",
            account=account,
            currency=str((result or {}).get("currency") or currency).upper(),
            amount=amount,
            kind="buying_power",
        )
    ]


# ── KIS normalizers ──────────────────────────────────────


def kis_domestic_holdings(payload: Any, account: str = "kis:domestic") -> list[Holding]:
    """Normalize KIS 국내주식 잔고 (TTTC8434R output1) into Holdings."""
    rows = payload.get("stocks") if isinstance(payload, dict) else None
    holdings: list[Holding] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("pdno") or "").strip()
        quantity = _f(row.get("hldg_qty"))
        if not symbol or quantity <= 0:
            continue
        cost_basis = to_float(row.get("pchs_amt"))
        avg_price = to_float(row.get("pchs_avg_pric"))
        if cost_basis is None and avg_price is not None:
            cost_basis = avg_price * quantity
        unrealized = to_float(row.get("evlu_pfls_amt"))
        holdings.append(
            Holding(
                provider="kis",
                account=account,
                symbol=symbol,
                name=row.get("prdt_name"),
                market="KR",
                exchange="KRX",
                currency="KRW",
                quantity=quantity,
                last_price=to_float(row.get("prpr")),
                avg_price=avg_price,
                cost_basis=cost_basis,
                market_value=to_float(row.get("evlu_amt")),
                unrealized_pnl=unrealized,
                unrealized_pnl_pct=to_float(row.get("evlu_pfls_rt")) or _pct(unrealized, cost_basis),
            )
        )
    return holdings


def kis_overseas_holdings(payload: Any, account: str = "kis:overseas") -> list[Holding]:
    """Normalize KIS 해외주식 잔고 (TTTS3012R output1) into Holdings.

    Rows carry the `market`/`exchange`/`currency` that KISClient.get_overseas_balance
    stamps on each per-market response.
    """
    rows = payload.get("stocks") if isinstance(payload, dict) else None
    holdings: list[Holding] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("ovrs_pdno") or row.get("pdno") or "").strip().upper()
        quantity = _f(row.get("ovrs_cblc_qty"))
        if not symbol or quantity <= 0:
            continue
        currency = str(row.get("currency") or row.get("tr_crcy_cd") or "USD").upper()
        cost_basis = to_float(row.get("frcr_pchs_amt1"))
        avg_price = to_float(row.get("pchs_avg_pric"))
        if cost_basis is None and avg_price is not None:
            cost_basis = avg_price * quantity
        unrealized = to_float(row.get("frcr_evlu_pfls_amt"))
        holdings.append(
            Holding(
                provider="kis",
                account=account,
                symbol=symbol,
                name=row.get("ovrs_item_name") or row.get("prdt_name"),
                market=str(row.get("market") or market_for(currency, symbol)).upper(),
                exchange=str(row.get("exchange") or row.get("ovrs_excg_cd") or "").upper() or None,
                currency=currency,
                quantity=quantity,
                last_price=to_float(row.get("now_pric2")),
                avg_price=avg_price,
                cost_basis=cost_basis,
                market_value=to_float(row.get("ovrs_stck_evlu_amt")),
                unrealized_pnl=unrealized,
                unrealized_pnl_pct=to_float(row.get("evlu_pfls_rt")) or _pct(unrealized, cost_basis),
            )
        )
    return holdings


def kis_domestic_cash(payload: Any, account: str = "kis:domestic") -> list[CashBalance]:
    """Normalize KIS 국내주식 잔고 output2 into a KRW CashBalance."""
    summary = payload.get("summary") if isinstance(payload, dict) else None
    rows = summary if isinstance(summary, list) else [summary] if isinstance(summary, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = to_float(row.get("dnca_tot_amt"))
        if amount is not None:
            return [CashBalance(provider="kis", account=account, currency="KRW", amount=amount)]
    return []


def kis_overseas_cash(payload: Any, account: str = "kis:overseas") -> list[CashBalance]:
    """Normalize KIS 체결기준현재잔고 output2 into per-currency CashBalances."""
    balances: list[CashBalance] = []
    for row in (payload.get("cash") if isinstance(payload, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        currency = str(row.get("crcy_cd") or "").upper()
        amount = to_float(row.get("frcr_dncl_amt_2"))
        if not currency or amount is None:
            continue
        balances.append(CashBalance(provider="kis", account=account, currency=currency, amount=amount))
    return balances


def kis_domestic_transactions(payload: Any, account: str = "kis:domestic") -> list[Transaction]:
    """Normalize KIS 국내 일별주문체결 (TTTC0081R output1) into ledger entries.

    KIS reports only an estimated total cost (`prsm_tlex_smtl`, 추정제비용합계) for
    domestic fills and does not split commission from tax, so the whole amount is
    recorded as `fee` with `tax=0`.
    """
    rows = payload.get("trades") if isinstance(payload, dict) else None
    transactions: list[Transaction] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        quantity = _f(row.get("tot_ccld_qty"))
        side = _KIS_SIDE.get(str(row.get("sll_buy_dvsn_cd") or "").strip())
        trade_date = iso_date(row.get("ord_dt"))
        if quantity <= 0 or side is None or trade_date is None:
            continue
        tx_type = TxType[side]
        gross = to_float(row.get("tot_ccld_amt"))
        price = to_float(row.get("avg_prvs"))
        if price is None and gross is not None and quantity:
            price = gross / quantity
        if gross is None and price is not None:
            gross = price * quantity
        fee = _f(row.get("prsm_tlex_smtl"))
        transactions.append(
            Transaction(
                provider="kis",
                account=account,
                tx_id=str(row.get("odno") or ""),
                **{k: v for k, v in identity("kis", account, "KR", str(row.get("odno") or ""), trade_date).items() if k in {"execution_id", "identity_quality", "account_key", "parent_order_id"}},
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=None,
                symbol=str(row.get("pdno") or "").strip(),
                name=row.get("prdt_name"),
                market="KR",
                exchange=str(row.get("excg_dvsn_cd") or "KRX").upper(),
                currency="KRW",
                quantity=quantity,
                price=price,
                gross_amount=gross,
                fee=fee,
                tax=0.0,
                net_amount=_net_amount(tx_type, gross, fee, 0.0),
                fx_rate=1.0,
                status="FILLED",
                source="kis:inquire-daily-ccld",
                note="fee is KIS estimated total cost (제비용); commission/tax not split",
            )
        )
    return transactions


def kis_overseas_transactions(payload: Any, account: str = "kis:overseas") -> list[Transaction]:
    """Normalize KIS 해외주식 일별거래내역 (CTOS4001R output1) into ledger entries.

    Preferred overseas source: unlike inquire-ccnl it reports settlement date,
    both fee columns, and the FX rate registered for the trade.
    """
    rows = payload.get("transactions") if isinstance(payload, dict) else None
    transactions: list[Transaction] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        quantity = _f(row.get("ccld_qty"))
        side = _KIS_SIDE.get(str(row.get("sll_buy_dvsn_cd") or "").strip())
        trade_date = iso_date(row.get("trad_dt"))
        if quantity <= 0 or side is None or trade_date is None:
            continue
        tx_type = TxType[side]
        symbol = str(row.get("pdno") or "").strip().upper()
        currency = str(row.get("crcy_cd") or "USD").upper()
        price = to_float(row.get("ovrs_stck_ccld_unpr")) or to_float(row.get("ft_ccld_unpr2"))
        gross = to_float(row.get("tr_frcr_amt2"))
        if gross is None and price is not None:
            gross = price * quantity
        if price is None and gross is not None and quantity:
            price = gross / quantity
        # 국내/해외 수수료가 분리 보고되므로 합산한다 (세금 항목은 없음).
        fee = _f(row.get("frcr_fee1")) + _f(row.get("dmst_frcr_fee1"))
        transactions.append(
            Transaction(
                provider="kis",
                account=account,
                tx_id=f"{trade_date}:{symbol}:{side}:{row.get('sttl_dt') or ''}:{quantity:g}",
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=iso_date(row.get("sttl_dt")),
                symbol=symbol,
                name=row.get("ovrs_item_name"),
                market=str(row.get("market") or market_for(currency, symbol)).upper(),
                exchange=str(row.get("ovrs_excg_cd") or "").upper() or None,
                currency=currency,
                quantity=quantity,
                price=price,
                gross_amount=gross,
                fee=fee,
                tax=0.0,
                net_amount=_net_amount(tx_type, gross, fee, 0.0),
                fx_rate=to_float(row.get("erlm_exrt")),
                status="FILLED",
                source="kis:inquire-period-trans",
            )
        )
    return transactions


def kis_overseas_ccnl_transactions(payload: Any, account: str = "kis:overseas") -> list[Transaction]:
    """Normalize KIS 해외주식 주문체결내역 (TTTS3035R output) into ledger entries.

    Fallback for when 일별거래내역 is unavailable: order-level detail, but KIS
    reports no fees here, so `fee`/`tax` are 0 and the note flags it.
    """
    rows = payload.get("trades") if isinstance(payload, dict) else None
    transactions: list[Transaction] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        quantity = _f(row.get("ft_ccld_qty"))
        side = _KIS_SIDE.get(str(row.get("sll_buy_dvsn_cd") or "").strip())
        trade_date = iso_date(row.get("ord_dt"))
        if quantity <= 0 or side is None or trade_date is None:
            continue
        tx_type = TxType[side]
        symbol = str(row.get("pdno") or "").strip().upper()
        currency = str(row.get("currency") or row.get("tr_crcy_cd") or "USD").upper()
        price = to_float(row.get("ft_ccld_unpr3"))
        gross = to_float(row.get("ft_ccld_amt3"))
        if gross is None and price is not None:
            gross = price * quantity
        transactions.append(
            Transaction(
                provider="kis",
                account=account,
                tx_id=str(row.get("odno") or ""),
                **{k: v for k, v in identity("kis", account, str(row.get("market") or market_for(currency, symbol)), str(row.get("odno") or ""), trade_date).items() if k in {"execution_id", "identity_quality", "account_key", "parent_order_id"}},
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=None,
                symbol=symbol,
                name=row.get("prdt_name"),
                market=str(row.get("market") or market_for(currency, symbol)).upper(),
                exchange=str(row.get("exchange") or row.get("ovrs_excg_cd") or "").upper() or None,
                currency=currency,
                quantity=quantity,
                price=price,
                gross_amount=gross,
                fee=0.0,
                tax=0.0,
                net_amount=_net_amount(tx_type, gross, 0.0, 0.0),
                fx_rate=None,
                status=str(row.get("prcs_stat_name") or "FILLED"),
                source="kis:inquire-ccnl",
                note="fees not reported by inquire-ccnl; net_amount excludes costs",
            )
        )
    return transactions


def _net_amount(tx_type: TxType, gross: float | None, fee: float, tax: float) -> float | None:
    """Signed cash effect: buys consume cash, sells return it net of costs."""
    if gross is None:
        return None
    if tx_type is TxType.BUY:
        return -(gross + fee + tax)
    if tx_type is TxType.SELL:
        return gross - fee - tax
    return gross
