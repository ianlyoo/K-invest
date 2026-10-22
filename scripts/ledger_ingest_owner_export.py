#!/usr/bin/env python3
"""Append your own Toss PDF statement or Binance CSV export flows to the local ledger, offline.

Dry runs open only a temporary database clone; no target sidecars are created.
Existing WAL/SHM on a live WAL database are acceptable and remain untouched.
Uncheckpointed WAL is copied too; concurrent changes refuse the dry run.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from owner_flows import ingest_owner_export  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', required=True, choices=('toss_pdf', 'binance_csv'))
    parser.add_argument('--file', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--account-suffix')
    parser.add_argument('--owner-declared-no-activity-before')
    parser.add_argument('--coverage-floor')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = ingest_owner_export(args.kind, args.file, args.db, account_suffix=args.account_suffix, dry_run=args.dry_run,
            owner_declared_no_activity_before=args.owner_declared_no_activity_before, coverage_floor=args.coverage_floor)
    except Exception as exc:
        code = getattr(exc, 'code', str(exc))
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{2,80}', code):
            code = 'OWNER_EXPORT_INGEST_FAILED'
        print(json.dumps({'error_code': code}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 2 if result['unknown'] else 0


if __name__ == '__main__':
    sys.exit(main())
