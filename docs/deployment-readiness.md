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

The supported source is specifically `cc4192b0b61d395ff1ee9c9db5d2e9a015bd8ab3`.
The live `90-durable-oauth.conf` must match its recorded receipt and pinned SHA-256
`56a8e552e2862ab79ce2c2999264643851e86995a774273d70b799bf177bd271`.
Precheck accepts only that verified managed drop-in; conflicting overrides from
other drop-ins still fail. Source guards are repeated before activation.

An exclusive root-private `/var/lib/toss-mcp/rollback-EXACT_APPROVED_SHA` captures
the current drop-in and its fingerprint. Only the WorkingDirectory and non-empty
ExecStart release paths change. A private, exclusively created staging file is
atomically renamed over the guarded drop-in. Existing rollback/staging paths,
edited source/receipts and failed validation stop before activation. The existing
0700 state directory is required; upgrade creates, copies or restores no DB/state.

`deploy/release.sh rollback EXACT_APPROVED_SHA` verifies the installed fingerprint
and saved source bytes, then restores the exact `cc4192b` drop-in and runs its
readiness gate. Startup/readiness failure invokes this rollback automatically.
Owner-edited files or an existing rollback copy stop restoration. Both versions
use the same durable OAuth DB, so grants persist and no reconnect is expected.
This upgrade is intentionally not a general upgrade from arbitrary releases.
