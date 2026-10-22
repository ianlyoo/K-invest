"""Stocks-only market tape validation never contacts a real provider."""
from types import SimpleNamespace

import pytest

import server


@pytest.mark.parametrize('symbol', ['BTCUSDT', 'ETHUSDC', 'BNBBUSD', 'BTCFDUSD',
    'BTCTUSD', 'BTC-PERP', 'ETHPERP', 'btcusdt', ' ETHUSDC '])
def test_crypto_market_trades_rejected_without_provider_and_alias_agrees(monkeypatch, symbol):
    def forbidden():
        pytest.fail('Crypto symbols must be rejected before constructing Toss')
    monkeypatch.setattr(server, '_get_toss', forbidden)
    result = server.get_market_trades(symbol)
    assert not result['ok']
    assert result['data'] is None
    assert result['error']['code'] == 'UNSUPPORTED_ASSET_CLASS'
    assert result['error']['retryable'] is False
    assert 'get_binance_futures_quote' in result['error']['message']
    assert 'get_crypto_futures_snapshot' in result['error']['message']
    assert set(result) == {'ok', 'data', 'error', 'meta'}
    assert set(result['error']) == {'code', 'message', 'provider', 'upstream_status', 'retryable', 'details'}
    alias = server.get_recent_trades(symbol)
    assert alias['meta'].pop('deprecated') == {'alias_of': 'get_market_trades', 'since': '2.6.0'}
    assert alias == result


@pytest.mark.parametrize('symbol', ['AAPL', 'BRK.B', '005930', '9001A0'])
def test_stock_market_trades_pass_through_and_alias_agrees(monkeypatch, symbol):
    calls = []
    def fake(value):
        calls.append(value)
        return {'symbol': value, 'trades': [{'price': 123, 'quantity': 2}]}
    monkeypatch.setattr(server, '_get_toss', lambda: SimpleNamespace(get_recent_trades=fake))
    result = server.get_market_trades(symbol)
    assert result['ok']
    assert result['data']['symbol'] == symbol
    alias = server.get_recent_trades(symbol)
    alias['meta'].pop('deprecated')
    assert alias == result
    assert calls == [symbol, symbol]
