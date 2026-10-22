"""Source-backed instrument identity; symbol shape only selects lookup candidates.

Never derive ETF relationships or leverage from a name. Existing normalized
providers expose no verified constituent/target/reset schema; those facts remain
unavailable until a supported provider supplies them.
"""
from __future__ import annotations

import re
from typing import Any

# Provider venue aliases, not ticker-pattern classification.
_VENUES = {
    "NASDAQ": ("XNAS", "US"), "NASD": ("XNAS", "US"), "NMS": ("XNAS", "US"),
    "NGM": ("XNAS", "US"), "NCM": ("XNAS", "US"), "XNAS": ("XNAS", "US"),
    "NYSE": ("XNYS", "US"), "NYS": ("XNYS", "US"), "XNYS": ("XNYS", "US"),
    "AMEX": ("XASE", "US"), "AMS": ("XASE", "US"), "XASE": ("XASE", "US"),
    "PCX": ("ARCX", "US"), "ARCX": ("ARCX", "US"),
    "KOSPI": ("XKRX", "KR"), "KSC": ("XKRX", "KR"), "XKRX": ("XKRX", "KR"),
    "KOSDAQ": ("XKOS", "KR"), "KOE": ("XKOS", "KR"), "XKOS": ("XKOS", "KR"),
    # KRX names the exchange group and does not establish a listing segment.
    "KRX": (None, "KR"),
    "TKSE": ("XTKS", "JP"), "JPX": ("XTKS", "JP"),
}


class MarketResolutionError(ValueError):
    """Public remediation is fixed text; never expose raw provider errors."""


def market_evidence(record: dict[str, Any]) -> set[str]:
    countries = set()
    for key in ("marketCountry", "market", "countryCode"):
        value = str(record.get(key) or "").upper()
        if value in {"KR", "US", "JP", "HK", "CN"}:
            countries.add(value)
        elif value in _VENUES:
            countries.add(_VENUES[value][1])
    for key in ("mic", "exchange"):
        value = str(record.get(key) or "").upper()
        if value in _VENUES:
            countries.add(_VENUES[value][1])
    return countries


def lookup_symbols(symbol: str) -> list[str]:
    """A six-character KRX candidate is looked up, never presumed Korean."""
    symbol = symbol.strip().upper()
    if re.fullmatch(r"[0-9A-Z]{6}", symbol):
        return [symbol, f"{symbol}.KS", f"{symbol}.KQ"]
    return [symbol]


def resolve_market(symbol: str, market: str, records: list[dict[str, Any]]) -> str:
    if market in {"kr", "us"}:
        return market
    if market != "auto":
        raise ValueError('market must be "auto", "kr", or "us"')
    countries = set()
    candidates = set(lookup_symbols(symbol))
    for record in records:
        native = str(record.get("symbol") or "").upper()
        if native in candidates:
            countries.update(market_evidence(record))
    if len(countries) == 1 and countries <= {"KR", "US"}:
        return next(iter(countries)).lower()
    raise MarketResolutionError('Specify market="kr" or "us"; provider master is missing or ambiguous.')


def instrument_metadata(symbol: str, record: dict[str, Any], source: str, market: str = "auto") -> dict[str, Any]:
    native = symbol.strip().upper()
    venue = record.get("mic") or record.get("exchange")
    venue_info = _VENUES.get(str(venue or "").upper())
    countries = market_evidence(record)
    if market in {"kr", "us"}:
        countries = {market.upper()}
    country = next(iter(countries)) if len(countries) == 1 else None
    mic = venue_info[0] if venue_info and venue_info[1] == country else None
    currency = record.get("currency") or record.get("crcy_cd") or record.get("tr_crcy_cd")
    if not currency and country == "KR":
        currency = "KRW"
    kind = str(record.get("quoteType") or record.get("instrumentType") or "").upper()
    asset_class = {"ETF": "etf", "EQUITY": "equity", "STOCK": "equity", "MUTUALFUND": "mutual_fund"}.get(kind)
    reasons = []
    for value, code in ((country, "INSTRUMENT_MARKET_UNAVAILABLE"), (mic, "INSTRUMENT_MIC_UNAVAILABLE"), (asset_class, "INSTRUMENT_ASSET_CLASS_UNAVAILABLE"), (currency, "INSTRUMENT_CURRENCY_UNAVAILABLE")):
        if value is None:
            reasons.append(code)
    return {
        "instrument_id": f"{mic}:{native}" if mic else None,
        "venue": venue or None, "mic": mic, "native_symbol": native,
        "asset_class": asset_class, "currency": str(currency).upper() if currency else None,
        "market": country, "source": source,
        "status": "partial" if reasons else "available", "reason_codes": reasons,
        "product_relationship": {
            "underlying": None, "leverage_target": None, "reset_frequency": None,
            "effective_date": None, "source": None, "status": "unavailable",
            "reason_codes": ["PRODUCT_RELATIONSHIP_UNAVAILABLE"],
            "interpretation": "A daily leverage target, if reported, is neither sustained leverage nor a verified delta.",
        },
        "lookthrough": {
            "constituents": None, "observed_at": None, "coverage": None, "source": None,
            "unknown_exposure": {"value": None, "status": "unavailable", "reason_codes": ["CONSTITUENTS_UNAVAILABLE"]},
        },
    }
