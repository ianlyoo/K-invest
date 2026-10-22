"""Round 3: Hyperliquid pacing at the documented weights and the shared positions cache.

The real FuturesAccounts / Hyperliquid client over mocked HTTP, a recording sleeper and an
injected clock; synthetic numbers and a synthetic address only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import crypto_futures_accounts as futures  # noqa: E402

ADDRESS = '0x' + 'a' * 36 + '7e3b'
WALL = 1792600000.0
POSITION = {'coin': 'SYNL', 'szi': '2', 'entryPx': '70', 'positionValue': '150', 'liquidationPx': '60',
            'leverage': {'type': 'cross', 'value': 4}}


def _client(sent, sleeps=None, dexes=('xyz', 'abc'), fail_dex=None, delay=0.0, hold=None):
    def handler(request):
        body = json.loads(request.content)
        sent[body['type'] + (':' + body['dex'] if body.get('dex') else '')] += 1
        if delay:
            time.sleep(delay)
        if body['type'] == 'perpDexs':
            return httpx.Response(200, json=[None, *({'name': name} for name in dexes)])
        if body['type'] == 'userAbstraction':
            return httpx.Response(200, json='default')
        if body['type'] == 'spotClearinghouseState':
            return httpx.Response(200, json={'balances': []})
        if body['type'] in ('userFunding', 'userFillsByTime'):
            if hold is not None:  # a slow paced history page: signal, then wait for the test
                hold[0].set()
                hold[1].wait(10)
            return httpx.Response(200, json=[])
        if fail_dex is not None and body.get('dex') == fail_dex and fail_dex not in failed:
            failed.add(fail_dex)
            return httpx.Response(500, json={})
        return httpx.Response(200, json={'assetPositions': [{'position': POSITION}] if not body.get('dex') else [],
                                         'marginSummary': {'accountValue': '500', 'totalMarginUsed': '40'},
                                         'withdrawable': '460'})

    failed = set()
    record = sleeps.append if sleeps is not None else (lambda _: None)
    return futures.HyperliquidAccountClient([ADDRESS], http=httpx.Client(transport=httpx.MockTransport(handler)),
                                            sleeper=record)


def _accounts(sent, now, **kwargs):
    return futures.FuturesAccounts(None, lambda: _client(sent, **kwargs), clock=lambda: now[0],
                                   wall_clock=lambda: WALL + now[0])


def test_every_allowed_info_read_has_a_documented_weight():
    assert set(futures.HYPERLIQUID_WEIGHTS) == futures.HYPERLIQUID_ALLOWLIST
    assert futures.HYPERLIQUID_WEIGHTS['clearinghouseState'] == futures.HYPERLIQUID_WEIGHTS['spotClearinghouseState'] == 2
    assert futures.HYPERLIQUID_WEIGHTS['perpDexs'] == futures.HYPERLIQUID_WEIGHTS['userAbstraction'] == 20
    # History reads keep the conservative up-front charge (extra weight per 20 rows returned).
    assert {futures.HYPERLIQUID_WEIGHTS[k] for k in ('userFillsByTime', 'userFunding')} == {120}


def test_a_positions_read_waits_weight_over_20_seconds_per_request():
    sent, sleeps = Counter(), []
    result = futures.FuturesAccounts(None, lambda: _client(sent, sleeps)).query('positions', 'hyperliquid')
    assert result['venues']['hyperliquid']['result_status'] == 'complete'
    # userAbstraction(20) -> perpDexs(20) -> spot(2) -> main, xyz, abc clearinghouseState(2):
    # each wait is the previous request's weight / 20 s (the sleeper does not advance time).
    assert sleeps == pytest.approx([0, 1.0, 1.0, 0.1, 0.1, 0.1], abs=0.05)
    assert sum(sleeps) < 2.5  # was 3 x 6 s at weight 120
    assert sent == Counter({'userAbstraction': 1, 'perpDexs': 1, 'spotClearinghouseState': 1,
                            'clearinghouseState': 1, 'clearinghouseState:xyz': 1, 'clearinghouseState:abc': 1})


def test_positions_are_served_from_cache_within_the_ttl_and_reread_after_it():
    sent, now = Counter(), [100.0]
    accounts = _accounts(sent, now)
    first = accounts.query('positions', 'all')['venues']['hyperliquid']
    assert first['cache'] == {'hit': False, 'stored': True, 'age_seconds': 0, 'ttl_seconds': 30,
                              'read_at': '2026-10-21T16:28:20+00:00'}
    now[0] += 12.34
    second = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    assert second['cache'] == {**first['cache'], 'hit': True, 'age_seconds': 12.3}
    assert {k: v for k, v in second.items() if k != 'cache'} == {k: v for k, v in first.items() if k != 'cache'}
    assert sum(sent.values()) == 6  # one read served both calls
    now[0] += 30
    third = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    assert third['cache']['hit'] is False and sum(sent.values()) == 12


def test_the_cache_serves_copies_and_never_reuses_a_partial_read():
    sent, now = Counter(), [0.0]
    accounts = _accounts(sent, now, fail_dex='xyz')
    partial = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    assert partial['result_status'] == 'partial' and partial['cache']['stored'] is False
    complete = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    assert complete['result_status'] == 'complete' and complete['cache']['hit'] is False
    complete['positions'].clear()
    again = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    assert again['cache']['hit'] is True and len(again['positions']) == 1
    assert sent['clearinghouseState:xyz'] == 2


def test_unconfigured_venues_report_no_cache_and_history_reads_are_never_cached():
    sent, now = Counter(), [0.0]
    accounts = _accounts(sent, now)
    result = accounts.query('positions', 'all')
    assert result['venues']['binance']['cache'] is None
    window = {'start_date': '2026-10-15', 'end_date': '2026-10-21'}
    for _ in range(2):
        funding = accounts.query('funding', 'hyperliquid', **window)['venues']['hyperliquid']
        assert 'cache' not in funding
    assert sent['userFunding'] == 2


def test_the_ttl_is_capped_at_sixty_seconds_and_zero_disables_the_cache():
    sent, now = Counter(), [0.0]
    capped = futures.FuturesAccounts(None, lambda: _client(sent), positions_cache_seconds=600, clock=lambda: now[0])
    assert capped.query('positions', 'hyperliquid')['venues']['hyperliquid']['cache']['ttl_seconds'] == 60
    off = futures.FuturesAccounts(None, lambda: _client(sent), positions_cache_seconds=0)
    for _ in range(2):
        assert off.query('positions', 'hyperliquid')['venues']['hyperliquid']['cache']['hit'] is False
    assert sent['perpDexs'] == 3


def test_concurrent_callers_share_one_read():
    sent, now = Counter(), [0.0]
    accounts = _accounts(sent, now, delay=0.02)
    results = []
    threads = [threading.Thread(target=lambda: results.append(accounts.query('positions', 'hyperliquid')))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r['venues']['hyperliquid']['cache']['hit'] for r in results) == [False, True, True]
    assert sent['perpDexs'] == 1


# ── a history read never holds a positions read for long ──

WINDOW = {'start_date': '2026-10-15', 'end_date': '2026-10-21'}


def _timed(call):
    started = time.monotonic()
    return call(), time.monotonic() - started


def test_a_positions_read_during_a_history_read_returns_cached_or_busy_within_the_wait():
    sent, now = Counter(), [0.0]
    entered, release = threading.Event(), threading.Event()
    accounts = _accounts(sent, now, hold=(entered, release))
    warm = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    history = threading.Thread(target=accounts.query, args=('funding', 'hyperliquid'), kwargs=WINDOW)
    history.start()
    assert entered.wait(5)
    try:
        cached, cached_s = _timed(lambda: accounts.query('positions', 'all'))
        now[0] += 31  # past the TTL: no copy left to serve
        busy, busy_s = _timed(lambda: accounts.query('positions', 'hyperliquid'))
    finally:
        release.set()
        history.join(10)
    assert cached_s < 3 and busy_s < 3
    hl = cached['venues']['hyperliquid']
    assert hl['cache']['hit'] is True and hl['positions'] == warm['positions']
    assert cached['venues']['binance']['reason_codes'] == ['BINANCE_FUTURES_NOT_CONFIGURED']
    row = busy['venues']['hyperliquid']
    assert (row['result_status'], row['source_status'], row['reason_codes'], row['positions'], row['cache']) == (
        'unavailable', 'failed', ['PERPS_READ_BUSY'], [], None)
    assert row['error']['code'] == 'PERPS_READ_BUSY' and row['error']['retryable'] is True
    assert busy['result_status'] == 'unavailable' and busy['reason_codes'] == ['PERPS_READ_BUSY']
    assert sent['perpDexs'] == 1  # neither positions call reached the venue during the history read
    after = accounts.query('positions', 'hyperliquid')['venues']['hyperliquid']
    assert after['result_status'] == 'complete' and after['cache']['hit'] is False and sent['perpDexs'] == 2


def test_a_positions_read_still_waits_for_a_slower_positions_read_and_shares_it():
    sent, now = Counter(), [0.0]
    accounts = futures.FuturesAccounts(None, lambda: _client(sent, delay=0.05), busy_wait_seconds=0.05,
                                       clock=lambda: now[0], wall_clock=lambda: WALL + now[0])
    results = []
    threads = [threading.Thread(target=lambda: results.append(accounts.query('positions', 'hyperliquid')))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # one read of 6 requests x 0.05 s outlasts the 0.05 s busy wait; the second caller shares it
    assert sorted(r['venues']['hyperliquid']['cache']['hit'] for r in results) == [False, True]
    assert sent['perpDexs'] == 1
