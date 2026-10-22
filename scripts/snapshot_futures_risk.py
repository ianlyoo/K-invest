#!/usr/bin/env python3
"""Collect current futures risk once; no dotenv, server import, or MCP tools.

Configuration uses the existing guarded clients only at execution time. Binance
credential-file variables (or systemd LoadCredential names) and the configured HL
address list must be supplied by the operator; this script never prints them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from futures_risk import RiskCollector, RiskStore  # noqa: E402


def main(argv=None, *, collector=None):
    parser = argparse.ArgumentParser(description='Append one prospective futures risk capture')
    parser.add_argument('--db', type=Path, help='Portfolio DB path; otherwise the existing portfolio DB setting')
    arguments = parser.parse_args(argv)
    try:
        result = (collector or RiskCollector(RiskStore(arguments.db))).capture()
    except Exception:
        print(json.dumps({'source_status': 'failed', 'reason_codes': ['RISK_SNAPSHOT_NOT_PERSISTED']}))
        return 1
    print(json.dumps({key: result[key] for key in ('snapshot_id', 'started_at', 'completed_at',
                      'source_status', 'data_status', 'reason_codes')}))
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
