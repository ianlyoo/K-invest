"""Wire shape of tool results: one compact object, sent once as structured and once as text.

MCP 2025-06-18, Tools -> Tool Result -> Structured Content: "a tool that returns
structured content SHOULD also return the serialized JSON in a TextContent block".
Every tool result therefore carries exactly one TextContent block whose text is the
compact JSON (no indent, no spaces) of the structuredContent object sent.

Large portfolio tools additionally get a `summary` detail level (the default) that is
shaped generically here, never inside tool bodies:

- `drop`: per-tool JSON paths moved to detail_level="full" (ID lists, per-day FX
  tables, fetched windows, transaction id hashes). Each dropped path is listed in
  meta.detail.omitted with its item count, so nothing disappears silently.
- keys that are null/empty in every row of a repeated block are dropped once and
  listed in meta.detail.omitted_empty.
- identical metadata blocks repeated in one response (coverage/quality blocks,
  instrument metadata, market calendars) are stored once under `shared` and each
  occurrence becomes {"$ref": "#/shared/<name>"}.
- tool hooks (zero-balance crypto assets).

Numbers the answer needs and reason codes that explain a null are never dropped:
policies only name ID/evidence lists, and the empty/dedupe passes are lossless.
detail_level="full" returns the tool's complete object unchanged.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import pydantic_core

SUMMARY, FULL = "summary", "full"
LEVELS = (SUMMARY, FULL)
#: Repeated metadata blocks at least this large (compact bytes) are stored once.
SHARED_MIN_BYTES = 160
#: Only metadata blocks are shared, never a block carrying the answer's own numbers
#: (a metric keeps value/status/reason_codes in place).
SHARED_KEYS = frozenset({"product_relationship", "lookthrough", "market_calendar", "cash_field_status",
                         "external_cashflows", "source_fields", "instrument_bindings", "unsettled"})


def _shareable(key: Any) -> bool:
    return isinstance(key, str) and (key in SHARED_KEYS or "coverage" in key or "quality" in key)
DETAIL_DESCRIPTION = ('"summary" (default) returns the compact answer: ID lists, per-day FX tables, '
                      'fetched windows and repeated metadata blocks move to "full"; meta.detail lists '
                      'what was omitted. "full" returns the complete object.')


def compact_json(value: Any) -> str:
    """The text block: compact JSON of exactly the structured object sent."""
    return pydantic_core.to_json(value, fallback=str).decode()


@dataclass(frozen=True)
class Policy:
    #: Paths removed under summary. Tokens: key, `*` any key, `[]` each list item,
    #: `**` any depth (keys or list items). Example: "data.cost_fx.rates".
    drop: tuple[str, ...] = ()
    #: Advertise an optional detail_level parameter on a tool that has none.
    inject: bool = False
    hooks: tuple[Callable[[dict[str, Any], dict[str, Any]], None], ...] = ()
    #: Run the generic empty-key and shared-block passes.
    generic: bool = True


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 1


def _tokens(pattern: str) -> list[str]:
    out = []
    for part in pattern.split("."):
        while part.endswith("[]"):
            part = part[:-2]
            if part:
                out.append(part)
            part = ""
            out.append("[]")
        if part:
            out.append(part)
    return out


def _drop(node: Any, tokens: list[str], pattern: str, omitted: dict[str, int]) -> None:
    if not tokens:
        return
    head, rest = tokens[0], tokens[1:]
    if head == "**":
        _drop(node, rest, pattern, omitted)
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        for child in list(children):
            _drop(child, tokens, pattern, omitted)
        return
    if head == "[]":
        if isinstance(node, list):
            for item in node:
                _drop(item, rest, pattern, omitted)
        return
    if not isinstance(node, dict):
        return
    for key in (list(node) if head == "*" else [head] if head in node else []):
        if rest:
            _drop(node[key], rest, pattern, omitted)
        else:
            omitted[pattern] = omitted.get(pattern, 0) + _count(node.pop(key))


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


#: Never removed by the empty-key pass: a metric keeps value/status/reason codes.
PROTECTED_KEYS = frozenset({"value", "status", "reason_codes", "result_status"})


def _protected(key: str) -> bool:
    return key in PROTECTED_KEYS or "reason" in key


def _drop_empty_keys(rows: list[dict[str, Any]], path: str, found: set[str]) -> None:
    keys = {key for row in rows for key in row}
    for key in sorted(keys):
        if not _protected(key) and all(_empty(row.get(key)) for row in rows):
            for row in rows:
                row.pop(key, None)
            found.add(f"{path}.{key}")


def _drop_empty_columns(node: Any, path: str, found: set[str]) -> None:
    """Keys empty in every row of a repeated block are removed from all rows.

    A repeated block is a list of >= 2 dicts (`path[].key`) or a map of >= 2 dicts
    (`path.*.key`, e.g. per-source health records).
    """
    if isinstance(node, dict):
        values = list(node.values())
        if len(values) >= 2 and all(isinstance(value, dict) for value in values):
            _drop_empty_keys(values, f"{path}.*", found)
        for key, child in node.items():
            _drop_empty_columns(child, f"{path}.{key}" if path else key, found)
    elif isinstance(node, list):
        rows = [item for item in node if isinstance(item, dict)]
        if len(node) >= 2 and len(rows) == len(node):
            _drop_empty_keys(rows, f"{path}[]", found)
        for item in node:
            _drop_empty_columns(item, f"{path}[]", found)


def _serialize(node: Any, sizes: dict[int, str]) -> str:
    """Compact canonical text of every dict node, built bottom-up once."""
    if isinstance(node, dict):
        text = "{" + ",".join(json.dumps(str(k)) + ":" + _serialize(v, sizes) for k, v in node.items()) + "}"
        sizes[id(node)] = text
        return text
    if isinstance(node, list):
        return "[" + ",".join(_serialize(v, sizes) for v in node) + "]"
    return json.dumps(node, ensure_ascii=False, default=str)


def _share_repeated(env: dict[str, Any]) -> dict[str, Any]:
    texts: dict[int, str] = {}
    for key, value in env.items():
        if key not in {"meta", "shared"}:
            _serialize(value, texts)
    counts: Counter[str] = Counter()

    def tally(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, dict) and _shareable(key):
                    counts[texts[id(child)]] += 1
                else:
                    tally(child)
        elif isinstance(node, list):
            for child in node:
                tally(child)

    for key, value in env.items():
        if key not in {"meta", "shared"}:
            tally(value)
    repeated = {text for text, n in counts.items() if n >= 2 and len(text.encode()) >= SHARED_MIN_BYTES}
    if not repeated:
        return {}
    shared: dict[str, Any] = {}
    names: dict[str, str] = {}

    def visit(node: Any) -> Any:
        if isinstance(node, dict):
            for key, child in list(node.items()):
                text = texts.get(id(child)) if isinstance(child, dict) and _shareable(key) else None
                if text in repeated:
                    name = names.get(text)
                    if name is None:
                        name = key if key not in shared else next(
                            f"{key}_{i}" for i in range(2, 10_000) if f"{key}_{i}" not in shared)
                        names[text] = name
                        shared[name] = child
                    node[key] = {"$ref": f"#/shared/{name}"}
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    for key, value in env.items():
        if key not in {"meta", "shared"}:
            visit(value)
    return shared


def shape(env: Any, policy: Policy | None, level: str) -> Any:
    """Summary shaping of one envelope; `full` and unknown tools pass through.

    Works on a deep copy, so the tool's own object is never mutated.
    """
    if policy is None or not isinstance(env, dict):
        return env
    meta = env.get("meta") if isinstance(env.get("meta"), dict) else None
    if level == FULL:
        if meta is not None:
            env = {**env, "meta": {**meta, "detail": {"level": FULL}}}
        return env
    env = json.loads(compact_json(env))
    omitted: dict[str, int] = {}
    for pattern in policy.drop:
        _drop(env, _tokens(pattern), pattern, omitted)
    for hook in policy.hooks:
        hook(env, omitted)
    empty: set[str] = set()
    shared: dict[str, Any] = {}
    if policy.generic:
        for key, value in env.items():
            if key != "meta":
                _drop_empty_columns(value, key, empty)
        shared = _share_repeated(env)
    detail: dict[str, Any] = {"level": SUMMARY}
    tally = omitted.pop(_REASON_TALLY, None)
    if omitted:
        detail["omitted"] = omitted
    if tally:
        detail["omitted_reason_codes"] = tally
    if empty:
        detail["omitted_empty"] = sorted(empty)
    if shared:
        env["shared"] = shared
        detail["shared_blocks"] = len(shared)
    if omitted or empty or shared:
        detail["full"] = 'detail_level="full" returns the complete object'
    if isinstance(env.get("meta"), dict):
        env["meta"]["detail"] = detail
    else:
        env["meta"] = {"detail": detail}
    return env


# ── tool hooks ───────────────────────────────────────────────────────


def _zero_crypto_assets(env: dict[str, Any], omitted: dict[str, int]) -> None:
    """Crypto assets whose balance is exactly zero, per venue.

    An asset is zero when its crypto_nav.<venue>.by_currency entry has value 0 and no
    reason code (a zero that carries a reason code is kept). Its by_currency entry and
    its accounts[] rows move to full; the asset names stay listed in meta.detail.
    """
    data = env.get("data")
    if not isinstance(data, dict):
        return
    for path, holder in (("data", data), ("data.latest", data.get("latest"))):
        crypto = holder.get("crypto_nav") if isinstance(holder, dict) else None
        if not isinstance(crypto, dict):
            continue
        for venue_name, venue in crypto.items():
            by_currency = venue.get("by_currency") if isinstance(venue, dict) else None
            if not isinstance(by_currency, dict):
                continue
            zero = sorted(asset for asset, entry in by_currency.items() if _is_zero(entry))
            if not zero:
                continue
            for asset in zero:
                by_currency.pop(asset)
            rows = venue.get("accounts")
            if isinstance(rows, list):
                venue["accounts"] = [row for row in rows if not (isinstance(row, dict) and row.get("asset") in zero)]
            omitted[f"{path}.crypto_nav.{venue_name} zero-balance assets: {','.join(zero)}"] = len(zero)


def _is_zero(entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("reason_codes"):
        return False
    value = entry.get("value")
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _exception_rows(path: str, ok_status: str) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    """Keep only the rows of the list at `path` whose status is not `ok_status`.

    Rows with status == ok_status move to full. Their count is listed in omitted, and
    the reason codes they carried are tallied in meta.detail.omitted_reason_codes, so
    no reason code disappears from the answer.
    """
    keys = path.split(".")

    def hook(env: dict[str, Any], omitted: dict[str, Any]) -> None:
        holder: Any = env
        for key in keys[:-1]:
            holder = holder.get(key) if isinstance(holder, dict) else None
        rows = holder.get(keys[-1]) if isinstance(holder, dict) else None
        if not isinstance(rows, list):
            return
        kept, codes = [], Counter()
        for row in rows:
            if isinstance(row, dict) and row.get("status") == ok_status:
                codes.update(code for code in row.get("reason_codes") or () if isinstance(code, str))
            else:
                kept.append(row)
        if len(kept) != len(rows):
            holder[keys[-1]] = kept
            label = f"{path}[] status={ok_status}"
            omitted[label] = len(rows) - len(kept)
            if codes:
                omitted.setdefault(_REASON_TALLY, {})[label] = dict(sorted(codes.items()))
    return hook


def _grouped_quote_exceptions(path: str, ok_status: str, into: str) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    """Collapse the quote rows at `path` into groups at sibling key `into`.

    Rows with status == ok_status move to full as in `_exception_rows`. The other rows
    are grouped by (status, reason_codes, component_type, currency) with a count and the
    [account, symbol] pairs, so every non-current position and the reason codes that
    explain it stay in the answer; per-row timestamps and ages move to full. The size
    no longer grows with the number of stale quotes (a closed US session).
    """
    keys = path.split(".")

    def hook(env: dict[str, Any], omitted: dict[str, Any]) -> None:
        holder: Any = env
        for key in keys[:-1]:
            holder = holder.get(key) if isinstance(holder, dict) else None
        rows = holder.get(keys[-1]) if isinstance(holder, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return
        _exception_rows(path, ok_status)(env, omitted)
        rows = holder.pop(keys[-1])
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            group = {"status": row.get("status"), "reason_codes": row.get("reason_codes") or [],
                     "component_type": row.get("component_type"), "currency": row.get("currency")}
            entry = groups.setdefault(compact_json(group), {**group, "count": 0, "positions": []})
            entry["count"] += 1
            entry["positions"].append([row.get("account"), row.get("symbol")])
        holder[into] = list(groups.values())
        if rows:
            omitted[f"{path}[] grouped into {'.'.join(keys[:-1] + [into])}"] = len(rows)
    return hook


_REASON_TALLY = "\0reason_codes"


# ── per-tool policy table ────────────────────────────────────────────

_LEDGER_EVIDENCE = (
    "**.fetched_windows",
    "**.transaction_ids",
    "**.execution_ids",
    "**.tx_ids",
    "**.event_ids",
)
_SNAPSHOT_IDS = ("**.input_snapshot_ids", "**.snapshot_ids")
_FX_TABLES = ("**.cost_fx.rates", "**.rates_by_date", "**.daily_rates")
#: Static methodology text, identical on every call.
_METHODOLOGY = ("data.methodology",)
#: Broker field names a value was read from, and instrument quote bindings (provenance).
_PROVENANCE = ("**.source_fields", "data.account_nav.*.instrument_bindings")
#: The latest snapshot's per-position / per-account detail inside get_portfolio_history:
#: the same objects get_consolidated_holdings and get_portfolio_summary return. Its
#: headline values, status and reason codes stay.
_HISTORY_LATEST_DETAIL = (
    "data.latest.components",
    "data.latest.account_nav",
    "data.latest.nav_bridge.cash_balances",
    "data.latest.mark_quality.quotes",
    "data.latest.crypto_nav.*.accounts",
)
_ZERO_CRYPTO = _zero_crypto_assets
_QUOTE_EXCEPTIONS = _grouped_quote_exceptions("data.mark_quality.quotes", "current", "quote_groups")
_RECONCILED_POSITIONS = _exception_rows("data.reconciliation.positions", "matched_observed")

POLICIES: dict[str, Policy] = {
    "get_trade_review": Policy(drop=_LEDGER_EVIDENCE + _FX_TABLES, hooks=(_RECONCILED_POSITIONS,)),
    "get_realized_pnl": Policy(drop=_LEDGER_EVIDENCE + _FX_TABLES, inject=True),
    "get_portfolio_summary": Policy(drop=_LEDGER_EVIDENCE + _FX_TABLES + _METHODOLOGY + _PROVENANCE, inject=True,
                                    hooks=(_ZERO_CRYPTO, _QUOTE_EXCEPTIONS)),
    "get_portfolio_performance": Policy(drop=_SNAPSHOT_IDS + _LEDGER_EVIDENCE + _FX_TABLES + _METHODOLOGY),
    "get_portfolio_history": Policy(drop=_SNAPSHOT_IDS + _LEDGER_EVIDENCE + _FX_TABLES + _METHODOLOGY
                                    + _HISTORY_LATEST_DETAIL,
                                    hooks=(_ZERO_CRYPTO,)),
    "get_consolidated_holdings": Policy(drop=_LEDGER_EVIDENCE + _METHODOLOGY),
    "get_portfolio_risk": Policy(),
    "get_account_health": Policy(),
    "get_all_transactions": Policy(drop=_LEDGER_EVIDENCE),
    # The broker's raw quote payload (~80 KIS fields); normalized fields stay.
    "compare_quotes": Policy(drop=("data.quotes[].data.raw",), inject=True),
    # The same output schemas tools/list already returns for every tool.
    "get_invest_mcp_help": Policy(drop=("data.tool_contracts[].output_schema",), inject=True, generic=False),
}


def requested_level(name: str, arguments: dict[str, Any], default: str | None) -> str:
    level = arguments.get("detail_level", default or SUMMARY)
    if level not in LEVELS:
        raise ValueError(f"detail_level must be one of {LEVELS}")
    return level
