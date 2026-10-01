# Durable OAuth, health, and journal metadata

OAuth is still enabled only with the existing static bearer plus the manual client
ID/secret. Dynamic registration remains disabled. This patch preserves the
existing single-user authorization policy; it does not redesign login or redirects.

`KINVEST_OAUTH_STATE_DIR` selects a dedicated private directory. The standalone
default is `~/.local/state/k-invest/oauth`; the example systemd unit selects
`/var/lib/k-invest/oauth`. The actual `toss-mcp.service` deployment should select
`/var/lib/toss-mcp/oauth` as described in the delivery's DEPLOY.md. The directory
must be mode 0700, DB 0600. Startup rejects a public state directory or an unknown
schema. Missing/unavailable state during a request returns HTTP 503, not a false
401. SQLite uses rollback journaling and transactional refresh consumption; do
not restore an old DB snapshot while serving grants.

`MCP_OAUTH_TOKEN_TTL` remains 3600 seconds by default.
`MCP_OAUTH_REFRESH_TOKEN_TTL` is independently configurable, default **2592000
seconds (30 days)**. The refresh lifetime rolls forward on successful refresh;
it is not an absolute family lifetime. Both settings accept positive integer
seconds up to 366 days. Values are validated at provider construction. Authorization
codes last 600 seconds. Expiry is exclusive: a credential is invalid at its expiry
second. Codes, grants, redirect metadata, family revocation and rotation persist
across process replacement. Only token/code digests are stored, never the manual
client secret. Approved rotation + replay protection includes a short same-client
retry grace: `MCP_OAUTH_REFRESH_GRACE_SECONDS`, default **60 seconds**, accepts
integer values 0–300; 0 disables grace. The fixed window starts at the first
successful rotation and does not extend on retries. A same-client retry before
that boundary receives a fresh sibling access/refresh pair. Both pairs remain
usable during grace so responses arriving out of order cannot lock out the user.
After grace, actual use of a successor retires only unused sibling pairs; pairs
already used remain usable. This favors continuity when refresh serialization is
unknown. Scope reduction cannot be undone by replaying the parent, and the
original audience is preserved. Replay at/after the boundary, or use by a different
client, revokes the entire family. No raw successor tokens are persisted. The
transactional schema-1 to schema-2 migration preserves existing grants; previously
consumed tokens without rotation timestamps receive no retroactive grace. Grace
and branch usage survive restarts. Expiration always overrides grace.

## Diagnostics and health

`k-invest.auth` emits allowlisted structured HTTP events: UTC timestamp, generated
request ID (also `X-Request-ID` response header), path category, HTTP status, layer,
reason, grant type and protocol version category. Query strings, opaque paths,
headers, token values, account identifiers and request/response bodies are never
emitted. Uvicorn's raw access entries are suppressed; nginx logs still need
independent owner-reviewed handling. MCP stays stateless. New reason classes let
a future correlated trace distinguish connector auth from transport 400s and
broker failures; this patch does not claim to identify the historical 400 reason.

Legacy uppercase tool error codes remain, with additive `error.kind`, safe
messages, upstream status and retryability. Kinds include `broker_auth_failed`,
`provider_unavailable`, `rate_limited`, `not_configured`, `invalid_request`, and
`auth_store_unavailable`. Raw broker exception bodies are no longer returned as
error details. Connector auth retains standard OAuth wire errors.

`get_service_health(mode="passive")` is authenticated and performs no broker or
account reads, requests no broker tokens, reads no credential files, and writes no
NAV/sync state. It returns configuration presence and previously observed source
status: unknown, not_configured, ok, ok_empty, partial, truncated, stale or failure.
Counts are null for unobserved/unavailable sources. Freshness here means observation
age, not a guaranteed broker data timestamp. A market-data success does not imply
account-read success. Auth store status, configured TTLs and refresh grace are included. JSON-only
`TOSS_ACCOUNTS_JSON` configuration is recognized without parsing credentials or
constructing clients. The SDK wire error `unauthorized_client` is logged as
`client_auth_failed`. Minimal
unauthenticated `/healthz` returns only `ok`; it proves process liveness, not auth
or broker readiness. Existing live `health_check` and `get_account_health` remain
separate and can perform upstream/account reads.

## Trade-history response compatibility

The existing `get_toss_trade_history` list and KIS domestic/overseas payloads remain.
Rows gain `execution_id`, `identity_version`, `identity_quality`, `record_kind`,
`parent_order_id`, and `account_key`. The unified `get_all_transactions` ledger
adds the same fields to entries, retaining legacy `tx_id` and accounting fields.

IDs are versioned hashes of provider, account namespace, market, original order
date and native order identity. They exclude quantity, price, fee, page position
and fetch time, so partial-fill updates keep identity. Available sources are
**order aggregates**, not guaranteed individual execution records. Preferred KIS
overseas period transactions lack a native order/execution identity; those entries
have `execution_id=null`, `identity_quality="unavailable"`. Do not use the legacy
quantity-based `tx_id` as an idempotency guarantee, nor silently merge indistinguishable
records. Preferred and fallback source reconciliation belongs to invest-desk.

Optional non-sensitive `account_id` aliases in existing Toss account JSON and
`KIS_ACCOUNTS_JSON` keep namespaces stable across display-label changes. Without
an alias, the existing label is the namespace: preserve labels during sync or
coordinate an explicit migration. Do not put an account number or credential in
an alias. Configuration changes are outside this implementation's host authority.
Canonical keys such as `kis:primary` and `kis:domestic` remain distinct, even with
matching native order IDs and dates. Legacy ledger market aliases are normalized
separately before a registry's canonical account key is applied. If older IDs
from the colliding namespaces were already journaled, invest-desk must reconcile
those entries and any dropped trades; this patch adds no backfill/checkpoint work.

Trade-history envelopes gain `meta.sources` and `meta.complete`; KIS subpayloads
also gain `source_metadata`. The unified ledger adds `data.source_metadata`.
Each source includes status, result count, complete/truncated flags and reason,
error kind, fetched time, upstream `as_of` and freshness where known, requested
window and coverage limits. Unknown upstream `as_of`/freshness stay null rather
than pretending that fetch time is market time. `ok_empty` means a successful,
exhaustive query of the reported window; broker retention is explicitly not
guaranteed. Partial KIS failure preserves the successful market and represents
failed `trades` as null with an error, not an empty list. Unsupported markets are
explicit and excluded from applicable-source completeness. A single-account KIS client initialization failure returns `ok=false`, `data=null`
and a safe captured `error`, while retaining `meta.sources`. Top-level `ok` means
the composite response was obtained; always inspect source status and completeness.

Toss cap, missing/repeated cursor and page-limit conditions now report truncation.
No new cursor/checkpoint/backfill/event subsystem is included. Invest-desk owns
those responsibilities. New KIS rate-limit retries are capped at two with backoff and honor safe Retry-After values. Delays over 60 seconds return retry metadata rather than blocking or retrying early. Tool errors add `retry_after_seconds` when known. Broker failure events share the HTTP request ID.

## Validation

Synthetic tests block outgoing sockets and isolate cache/auth files. Coverage
includes a fresh interpreter recovering and rotating grants, 30-day and overridden
TTL boundaries, code replay, client/scope/audience checks, whole-family revocation,
concurrent refresh, transaction rollback, database outage and permissions, static
bearer compatibility, PKCE and MCP transport through a synthetic forwarding layer,
log redaction, passive health, stable aggregate identities, paging truncation,
complete-empty versus unavailable and partial source failure.
