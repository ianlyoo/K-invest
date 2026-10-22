#!/usr/bin/env python3
"""Ingest a local raw capture directory into the ledger. Counts only; no live requests."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio_ledger import ingest_capture  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--db')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = ingest_capture(args.run, args.db, args.dry_run)
        # Stream names from manifests may contain private input: emit ordinal IDs.
        result['streams'] = {f'stream-{i + 1}': row for i, row in enumerate(result['streams'].values())}
        print(json.dumps(result, sort_keys=True))
        return 2 if result['totals']['conflict'] or result['totals']['skipped'] or any(
            row.get('coverage', {}).get('status') == 'partial' for row in result['streams'].values()) else 0
    except Exception:
        print(json.dumps({'error_code': 'LEDGER_CAPTURE_INGEST_FAILED'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
