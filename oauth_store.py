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
                if version not in (0, 1):
                    raise AuthStoreUnavailable()
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS grants (
                      kind TEXT NOT NULL, digest TEXT NOT NULL, client TEXT NOT NULL,
                      metadata TEXT NOT NULL, expires INTEGER NOT NULL,
                      family TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                      revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, digest));
                    CREATE INDEX IF NOT EXISTS grants_expiry ON grants(expires);
                    CREATE TABLE IF NOT EXISTS redirects (client TEXT PRIMARY KEY, uris TEXT NOT NULL);
                    PRAGMA user_version=1;
                """)
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
        db.execute("INSERT INTO grants(kind,digest,client,metadata,expires,family) VALUES(?,?,?,?,?,?)", (
            record["kind"], digest(record["value"]), record["client"],
            json.dumps(record["metadata"]), record["expires"], record["family"],
        ))

    def issue(self, records: list[dict[str, Any]], consume: tuple[str, str, str] | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if consume:
                kind, value, client = consume
                changed = db.execute("""UPDATE grants SET consumed=1
                  WHERE kind=? AND digest=? AND client=? AND consumed=0 AND revoked=0 AND expires>?""",
                  (kind, digest(value), client, int(time.time()))).rowcount
                if changed != 1:
                    return False
            for record in records:
                self.insert(db, record)
            # Bounded retention of expired/revoked/consumed evidence, not token values.
            db.execute("DELETE FROM grants WHERE expires < ?", (int(time.time()) - 7 * 86400,))
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
