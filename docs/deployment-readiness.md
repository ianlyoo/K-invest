# State sandbox and startup validation

The actual-host drop-in appends exactly
`ReadWritePaths=/var/lib/toss-mcp/oauth`. It preserves ProtectSystem, ProtectHome,
PrivateTmp and every existing sandbox setting and write exception. The private
0700 directory contains the 0600 DB and SQLite journal/sidecar files.

`deploy/precheck.py` runs remotely before archive upload. It reads effective unit
properties, including ProtectSystem, ProtectHome, ReadOnlyPaths, InaccessiblePaths,
ReadWritePaths and PrivateTmp. It checks the proposed exception against both the
state directory and database/journal paths. More-specific writable subdirectories
can override read-only ancestors; inaccessible ancestors cannot be reopened.
Equal read/write restrictions and unknown remapping fail closed. It also checks
backing-filesystem flags and existing file access without writing test data or
opening credential files. Error output retains only classes and operation/field
names. See [systemd's filesystem namespace rules](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

After restart, `deploy/readiness.py` has a 30-second deadline. It checks local
liveness, authenticates with the service's existing inherited static bearer, and
calls only `get_service_health(mode="passive")`. Auth store status must be `ok`
and restart durability true; the service must remain active/running with the
same process and restart count. No bearer enters command arguments, files or
logs. No OAuth grant is minted/refreshed, no connector is reconnected, and no
broker/order tool is invoked. This validates authenticated store readiness,
not the continued validity of any previous connector's OAuth authorization.

Restart or readiness failure triggers the same digest-guarded rollback. An
edited drop-in prevents rollback and stops for owner review. State and releases
are preserved; stale database restoration is never part of rollback. Reverting
to the original in-memory OAuth provider cannot recover grants lost at restart.

## Guarded upgrade from the live durable release

After approval for the exact clean branch HEAD, run
`deploy/release.sh upgrade EXACT_APPROVED_SHA`. This includes the catalog fix from
`ceab866` and uses the same exclusive release/venv creation, ruff/full-pytest
validation and authenticated readiness gate as initial deployment.

The supported source is specifically the live `6b6d90ecbe90130bcbefd6a63d42ada15f8d06a1`.
The live `90-durable-oauth.conf` must match its recorded receipt and pinned SHA-256
`c96c6d7a457f97820522f29e370428483d64d937b6706fb776fad42a774cc0b6`.
Precheck accepts only that verified managed drop-in; conflicting overrides from
other drop-ins still fail. Source guards are repeated before activation.

An exclusive root-private `/var/lib/toss-mcp/rollback-EXACT_APPROVED_SHA` captures
the current drop-in and its fingerprint. WorkingDirectory and non-empty ExecStart
release paths change; the upgrade also adds the two service-credential directives
below. A private, exclusively created staging file is
atomically renamed over the guarded drop-in. Existing rollback/staging paths,
edited source/receipts and failed validation stop before activation. The existing
0700 state directory is required; upgrade creates, copies or restores no DB/state.

`deploy/release.sh rollback EXACT_APPROVED_SHA` verifies the installed fingerprint
and saved source bytes, then restores the exact `6b6d90e` drop-in and runs its
readiness gate. Startup/readiness failure invokes this rollback automatically.
Owner-edited files or an existing rollback copy stop restoration. Both versions
use the same durable OAuth DB, so grants persist and no reconnect is expected.
This upgrade is intentionally not a general upgrade from arbitrary releases.

## Invest Desk service principal (55d02626 item 2a)

The root-owned 0600 source is `/etc/toss-mcp/invest-desk-token`. Provision only a
new random value under separate deployment/credential approval; never reuse the
legacy bearer, print it, put it in an environment value, or read an existing secret
to implement this code change. Deliver an `install -m 0600` copy to
`/etc/invest-desk/kinvest-token` for its existing LoadCredential configuration.
The delivery's DEPLOY.md contains the guarded host-generation procedure.

The candidate upgrade appends only:

```ini
LoadCredential=invest-desk-token:/etc/toss-mcp/invest-desk-token
Environment=KINVEST_SERVICE_TOKEN_FILE=%d/invest-desk-token
```

Systemd supplies a private runtime credential file readable by the service;
`%d` selects its credential directory. No sandbox relaxation is needed. Precheck
and activation verify only root source-file metadata (regular file, 0600).
Validation scrubs service credential variables. Startup reads the configured
file once, retains only its digest, and fails with fixed reason names on an
unreadable/unsafe/invalid file or a legacy-bearer collision. The feature is disabled
when no file path is configured; inline `KINVEST_SERVICE_TOKEN` is refused.

Use the existing port 8100 with proxy-header rewriting disabled: this preserves
Invest Desk's pinned URL while checking the socket peer and excluding nginx traffic.
Only literal `127.0.0.1`/`::1` peers without any X-Forwarded-For, X-Real-IP or
Forwarded header can authenticate as the service principal. Header presence,
including empty values, rejects it; a recognized service token never falls through
to OAuth. The packaged Python entrypoint enforces `proxy_headers=False`; other
ASGI launchers must do the same. The deployment reviewer must privately verify
that nginx attaches at least one of those headers to every public upstream request.
A headerless loopback proxy is indistinguishable from a direct peer on this port;
stop rather than enable this path if that precondition is not true.

`service_auth.SERVICE_READ_TOOLS` is the sole server permission constant, matching
the current Invest Desk client exactly: `get_kis_accounts`, `get_toss_accounts`,
`get_kis_trade_history`, `get_toss_trade_history`, `get_kis_domestic_balance`,
`get_kis_overseas_balance`, `get_toss_holdings`, `get_candles`. Tools/list is filtered;
calls to everything else return MCP `isError=true` / `Forbidden` before dispatch,
including registered order/trade mutation tools, health, quote and FX tools.
Widening requires a matching reviewed client change. Typed principal identity
prevents OAuth client-name collisions or shared SDK tool caches from granting
service permissions. Existing static-bearer and ChatGPT OAuth policies/grants
remain unchanged; service authentication creates/refreshes no OAuth grants.

Rollback restores the exact saved `6b6d90e` drop-in, removing both credential
directives and running its existing readiness gate. Preserve OAuth DB and both
token files; do not restore state snapshots or reconnect ChatGPT. Invest Desk's
new token is unavailable against the old release after rollback; coordinate its
read-path rollout separately, without falling back to the legacy bearer.
