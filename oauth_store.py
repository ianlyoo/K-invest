"""Transactional OAuth state. Only digests of bearer/code values are persisted."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class AuthStoreUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("OAuth state store unavailable")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthStore:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o077:
                raise AuthStoreUnavailable()
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            with self.connection() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise AuthStoreUnavailable()
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS grants (
                      kind TEXT NOT NULL, digest TEXT NOT NULL, client TEXT NOT NULL,
                      metadata TEXT NOT NULL, expires INTEGER NOT NULL,
                      family TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                      revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, digest));
                    CREATE INDEX IF NOT EXISTS grants_expiry ON grants(expires);
                    CREATE TABLE IF NOT EXISTS redirects (client TEXT PRIMARY KEY, uris TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS registered_clients (client TEXT PRIMARY KEY, metadata TEXT NOT NULL, created INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS registration_attempts (created INTEGER NOT NULL);

                """)
                db.execute("BEGIN IMMEDIATE")
                client_columns = {row[1] for row in db.execute("PRAGMA table_info(registered_clients)")}
                if "consented" not in client_columns:
                    db.execute("ALTER TABLE registered_clients ADD COLUMN consented INTEGER NOT NULL DEFAULT 0")
                    db.execute("""UPDATE registered_clients SET consented=1 WHERE EXISTS
                        (SELECT 1 FROM grants WHERE grants.client=registered_clients.client
                         AND grants.kind IN ('code','access','refresh'))""")
                columns = {row[1] for row in db.execute("PRAGMA table_info(grants)")}
                for name, definition in {"consumed_at": "INTEGER", "parent": "TEXT NOT NULL DEFAULT ''",
                                         "pair": "TEXT NOT NULL DEFAULT ''", "used_at": "INTEGER"}.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE grants ADD COLUMN {name} {definition}")
                db.execute("CREATE INDEX IF NOT EXISTS grants_parent ON grants(parent)")
                db.execute("CREATE INDEX IF NOT EXISTS grants_pair ON grants(pair)")
                db.execute("PRAGMA user_version=2")
        except (OSError, sqlite3.Error) as exc:
            raise AuthStoreUnavailable() from exc

    @contextmanager
    def connection(self):
        db = None
        try:
            # mode=rw prevents a missing DB being silently recreated after startup.
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise AuthStoreUnavailable() from exc
        finally:
            if db is not None:
                db.close()

    def get(self, kind: str, value: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM grants WHERE kind=? AND digest=?", (kind, digest(value))).fetchone()
        if row is None:
            return None
        try:
            return {**dict(row), "metadata": json.loads(row["metadata"])}
        except (ValueError, TypeError) as exc:
            raise AuthStoreUnavailable() from exc

    @staticmethod
    def insert(db, record: dict[str, Any]) -> None:
        db.execute("INSERT INTO grants(kind,digest,client,metadata,expires,family,parent,pair) VALUES(?,?,?,?,?,?,?,?)", (
            record["kind"], digest(record["value"]), record["client"],
            json.dumps(record["metadata"]), record["expires"], record["family"], record.get("parent", ""), record.get("pair", ""),
        ))

    def get_client(self, client_id):
        with self.connection() as db:
            row = db.execute("SELECT metadata FROM registered_clients WHERE client=?", (client_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def register_client(self, metadata, *, cap=10):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            db.execute("DELETE FROM registration_attempts WHERE created<=?", (now - 3600,))
            row = db.execute("SELECT COUNT(*), MIN(created) FROM registration_attempts").fetchone()
            if row[0] >= 5:
                return "registration_rate_limited", max(1, row[1] + 3600 - now)
            db.execute("INSERT INTO registration_attempts VALUES(?)", (now,))
            if db.execute("SELECT COUNT(*) FROM registered_clients").fetchone()[0] >= cap:
                return "client_limit_reached", 3600
            db.execute("INSERT INTO registered_clients(client,metadata,created) VALUES(?,?,?)", (metadata["client_id"], json.dumps(metadata), now))
        return None

    def revoke_all(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return db.execute("UPDATE grants SET revoked=1").rowcount

    def issue(self, records: list[dict[str, Any]], consume: tuple[str, str, str] | None = None,
              consumed_consent: tuple[str, str, int] | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            db.execute("DELETE FROM grants WHERE kind='consent' AND expires<=?", (now,))
            if consumed_consent:
                value, client, expires = consumed_consent
                if expires <= now or db.execute("SELECT 1 FROM grants WHERE kind='consent' AND digest=?",
                                                (digest(value),)).fetchone():
                    return False
                # Consumption and code issuance share one transaction. Keep the
                # digest until expiry, including across restarts and races.
                db.execute("""INSERT INTO grants(kind,digest,client,metadata,expires,family,consumed,consumed_at)
                              VALUES('consent',?,?, '{}',?,'',1,?)""", (digest(value), client, expires, now))
            if consume:
                kind, value, client = consume
                changed = db.execute("""UPDATE grants SET consumed=1
                  WHERE kind=? AND digest=? AND client=? AND consumed=0 AND revoked=0 AND expires>?""",
                  (kind, digest(value), client, int(time.time()))).rowcount
                if changed != 1:
                    return False
            for record in records:
                self.insert(db, record)
                if record["kind"] in {"code", "access", "refresh"}:
                    # Preserve approval history after expired grant evidence is purged.
                    db.execute("UPDATE registered_clients SET consented=1 WHERE client=?", (record["client"],))
            # Bounded retention of expired/revoked/consumed evidence, not token values.
            db.execute("DELETE FROM grants WHERE expires < ?", (int(time.time()) - 7 * 86400,))
        return True

    @staticmethod
    def _refresh_allowed(db, row, client: str, now: int, grace: int) -> bool:
        if row is None or row["revoked"]:
            return False
        wrong_client = row["client"] != client
        late_replay = row["consumed"] and (row["consumed_at"] is None or now >= row["consumed_at"] + grace)
        if wrong_client or late_replay:
            db.execute("UPDATE grants SET revoked=1 WHERE family=?", (row["family"],))
            return False
        return row["expires"] > now

    @staticmethod
    def _used(db, row, now: int, grace: int) -> None:
        if row["pair"]:
            db.execute("UPDATE grants SET used_at=COALESCE(used_at,?) WHERE pair=?", (now, row["pair"]))
        else:
            db.execute("UPDATE grants SET used_at=COALESCE(used_at,?) WHERE kind=? AND digest=?", (now, row["kind"], row["digest"]))
        # Do not choose a response winner during the grace. After it, actual use
        # chooses a branch; preserve all siblings that have already been used.
        if row["parent"]:
            parent = db.execute("SELECT consumed_at FROM grants WHERE kind='refresh' AND digest=?", (row["parent"],)).fetchone()
            if parent and parent[0] is not None and now >= parent[0] + grace:
                db.execute("""UPDATE grants SET revoked=1 WHERE parent=? AND pair!=? AND used_at IS NULL""",
                           (row["parent"], row["pair"]))

    def load_refresh(self, value: str, client: str, grace: int):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE kind='refresh' AND digest=?", (digest(value),)).fetchone()
            if not self._refresh_allowed(db, row, client, int(time.time()), grace):
                return None
            return {**dict(row), "metadata": json.loads(row["metadata"])}

    def mark_access_used(self, value: str, grace: int) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE kind='access' AND digest=?", (digest(value),)).fetchone()
            now = int(time.time())
            if row is None or row["revoked"] or row["expires"] <= now:
                return False
            self._used(db, row, now, grace)
            return True

    def rotate(self, value: str, client: str, records: list[dict[str, Any]], grace: int) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE kind='refresh' AND digest=?", (digest(value),)).fetchone()
            now = int(time.time())
            if not self._refresh_allowed(db, row, client, now, grace):
                return False
            original = json.loads(row["metadata"])
            scopes = set(records[0]["metadata"]["scopes"])
            if not scopes.issubset(original["scopes"]):
                return False
            # A retry cannot restore scopes removed by the first rotation.
            children = db.execute("SELECT metadata FROM grants WHERE kind='refresh' AND parent=? AND revoked=0", (row["digest"],)).fetchall()
            if any(not scopes.issubset(json.loads(child[0])["scopes"]) for child in children):
                return False
            self._used(db, row, now, grace)
            db.execute("UPDATE grants SET consumed=1, consumed_at=COALESCE(consumed_at,?) WHERE kind='refresh' AND digest=?", (now, row["digest"]))
            for record in records:
                self.insert(db, {**record, "parent": row["digest"]})
            db.execute("DELETE FROM grants WHERE expires < ?", (now - 7 * 86400,))
        return True

    def revoke(self, kind: str, value: str) -> None:
        with self.connection() as db:
            row = db.execute("SELECT family FROM grants WHERE kind=? AND digest=?", (kind, digest(value))).fetchone()
            if row:
                db.execute("UPDATE grants SET revoked=1 WHERE family=?", (row[0],))

    def redirect_uris(self, client: str) -> list[str]:
        with self.connection() as db:
            row = db.execute("SELECT uris FROM redirects WHERE client=?", (client,)).fetchone()
        try:
            return json.loads(row[0]) if row else []
        except (ValueError, TypeError) as exc:
            raise AuthStoreUnavailable() from exc

    def remember_redirect(self, client: str, uri: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT uris FROM redirects WHERE client=?", (client,)).fetchone()
            uris = json.loads(row[0]) if row else []
            if uri not in uris:
                uris = (uris + [uri])[-10:]
            db.execute("INSERT OR REPLACE INTO redirects VALUES(?,?)", (client, json.dumps(uris)))

    def health(self) -> str:
        try:
            with self.connection() as db:
                db.execute("SELECT 1 FROM grants LIMIT 1").fetchone()
            return "ok"
        except AuthStoreUnavailable:
            return "unavailable"
