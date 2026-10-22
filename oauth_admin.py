"""Root-only local OAuth administration; never opens credentials or the network."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument("command", choices=["list-clients", "revoke-all", "prune-unconsented", "reset-registration-limit"])
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("OAuth administration requires root")
    path = args.state_path
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SystemExit("OAuth state unavailable")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True) as db:
            if args.command == "revoke-all":
                db.execute("BEGIN IMMEDIATE")
                count = db.execute("UPDATE grants SET revoked=1").rowcount
                print(json.dumps({"revoked_grants": count}))
            elif args.command == "prune-unconsented":
                db.execute("BEGIN IMMEDIATE")
                count = db.execute("""DELETE FROM registered_clients WHERE consented=0 AND NOT EXISTS
                    (SELECT 1 FROM grants WHERE grants.client=registered_clients.client
                     AND grants.kind IN ('code','access','refresh'))""").rowcount
                print(count)
            elif args.command == "reset-registration-limit":
                db.execute("BEGIN IMMEDIATE")
                print(db.execute("DELETE FROM registration_attempts").rowcount)
            else:
                clients = []
                for client_id, raw in db.execute("SELECT client,metadata FROM registered_clients ORDER BY created,client"):
                    metadata = json.loads(raw)
                    clients.append({"client_id": client_id, "client_name": metadata.get("client_name"),
                                    "redirect_hosts": sorted({urlsplit(uri).hostname for uri in metadata["redirect_uris"]})})
                print(json.dumps(clients))
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        raise SystemExit("OAuth state unavailable") from None


if __name__ == "__main__":
    main()
