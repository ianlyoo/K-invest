"""Stablecoin NAV at a labelled USD proxy, and no negative quote age.

Risk captures go through the real Binance / Hyperliquid clients with mocked HTTP
(test_crypto_nav_capture's fixtures); NAV through daily_nav.crypto_nav and
mark_quality. Synthetic amounts and identifiers only.
"""
import sqlite3
from datetime import timedelta

from test_crypto_nav_capture import NOW, capture

from crypto_flow_intervals import _complete
from daily_nav import crypto_nav, mark_quality


def usd(age_seconds=60):
    return {'rate_to_krw': '1300', 'priced_at': (NOW - timedelta(seconds=age_seconds)).isoformat(),
            'read_at': NOW.isoformat(), 'source': 'synthetic_fx'}


def nav(path, **summary):
    summary = {'valuation_at': NOW.isoformat(), 'crypto_max_age_seconds': 300, **summary}
    with sqlite3.connect(path) as conn:
        return crypto_nav(conn, summary)['crypto_nav']


def test_stablecoins_without_a_direct_mark_use_the_labelled_usd_rate(tmp_path):
    path = tmp_path / 'db'
    capture(path)
    values = nav(path, fx_times={'USD': usd()})
    binance, hyperliquid = values['binance'], values['hyperliquid']
    assert binance['value_krw_decimal'] == '143000.00000000' and binance['status'] == 'derived'
    assert hyperliquid['value_krw_decimal'] == '260000' and hyperliquid['reason_codes'] == []
    assert binance['fx_proxy'] == hyperliquid['fx_proxy'] == 'USD'
    assert binance['fx_proxy_currencies'] == ['USDT'] and hyperliquid['fx_proxy_currencies'] == ['USDC']
    assert binance['by_currency']['USDT']['fx_proxy'] == 'USD'
    # A proxied value is not a stored crypto return endpoint; the same value with a direct mark is.
    snapshot = {'valuation_at': NOW.isoformat(), 'crypto_nav': values}
    assert not _complete(snapshot, 'binance')
    direct = nav(path, fx_times={'USDT': usd(), 'USDC': usd()})
    assert 'fx_proxy' not in direct['binance'] and direct['binance']['value_krw_decimal'] == '143000.00000000'
    assert _complete({'valuation_at': NOW.isoformat(), 'crypto_nav': direct}, 'binance')


def test_the_proxy_fails_closed(tmp_path):
    path = tmp_path / 'db'
    capture(path)
    stale = nav(path, fx_times={'USD': usd(age_seconds=25 * 3600)})['binance']
    assert stale['value_krw'] is None and stale['reason_codes'] == ['STALE_CRYPTO_FX_RATE'] and 'fx_proxy' not in stale
    assert nav(path, fx_times={})['binance']['reason_codes'] == ['MISSING_CRYPTO_FX_RATE']
    # A direct mark that was read but is unusable never falls back to USD parity.
    failed = {'USDT': {'rate_to_krw': None, 'priced_at': None, 'source': 'upbit_public_ticker',
                       'reason_codes': ['CRYPTO_QUOTE_UNAVAILABLE']}}
    held = nav(path, fx_times={'USD': usd()}, crypto_fx_times=failed)['binance']
    assert held['value_krw'] is None and 'MISSING_CRYPTO_FX_RATE' in held['reason_codes'] and 'fx_proxy' not in held


def quote(priced, account='toss:primary'):
    return {'account': account, 'provider': 'toss', 'component_type': 'cash', 'currency': 'USD', 'priced_at': priced}


def test_a_quote_read_after_the_valuation_instant_has_age_zero():
    valued = NOW.isoformat()
    summary = {'valuation_at': valued, 'observed_at': valued,
               'nav_evidence_completed_at': (NOW + timedelta(seconds=10)).isoformat(),
               'components': [quote((NOW + timedelta(seconds=3)).isoformat()),
                              quote((NOW + timedelta(seconds=30)).isoformat()),
                              quote((NOW - timedelta(seconds=40)).isoformat())]}
    during, future, before = mark_quality(summary)['quotes']
    assert during['age_seconds'] == 0.0 and during['priced_after_valuation_seconds'] == 3.0
    assert during['status'] == 'current' and during['reason_codes'] == []
    assert future['age_seconds'] == -30.0 and future['reason_codes'] == ['FUTURE_QUOTE_TIME']
    assert before['age_seconds'] == 40.0 and 'priced_after_valuation_seconds' not in before
