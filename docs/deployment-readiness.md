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
