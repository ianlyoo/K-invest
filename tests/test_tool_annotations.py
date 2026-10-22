"""Public registry classification including incidental maintenance."""
import asyncio

import server

EXPECTED_READ_ONLY = {
    'compare_quotes',
    'get_account_health',
    'get_all_transactions',
    'get_binance_funding_rate',
    'get_binance_funding_rate_history',
    'get_binance_futures_candles',
    'get_binance_futures_mark_price',
    'get_binance_futures_quote',
    'get_binance_open_interest',
    'get_candles',
    'get_consolidated_holdings',
    'get_crypto_futures_fills',
    'get_crypto_futures_funding_payments',
    'get_crypto_futures_positions',
    'get_crypto_futures_snapshot',
    'get_exchange_rate',
    'get_financials',
    'get_insider_trades',
    'get_invest_mcp_help',
    'get_key_metrics',
    'get_kis_accounts',
    'get_kis_cash_balance',
    'get_kis_domestic_balance',
    'get_kis_domestic_quote',
    'get_kis_overseas_balance',
    'get_kis_overseas_quote',
    'get_kis_trade_history',
    'get_market_hours',
    'get_market_series',
    'get_market_trades',
    'get_orderbook',
    'get_portfolio_history',
    'get_portfolio_performance',
    'get_portfolio_risk',
    'get_price_limits',
    'get_quote',
    'get_realized_pnl',
    'get_recent_trades',
    'get_risk_free_rate',
    'get_sec_financials',
    'get_service_health',
    'get_stock_info',
    'get_stock_snapshot',
    'get_stock_warnings',
    'get_toss_accounts',
    'get_toss_buying_power',
    'get_toss_holdings',
    'get_toss_trade_history',
    'get_trade_review',
    'get_ttm_financials',
    'health_check',
    'preview_statement_import',
    # Merged tools (tools_v2); get_portfolio_summary is read-only since T0.
    'get_status', 'get_holdings', 'get_portfolio_summary', 'get_portfolio_summary_v2', 'get_portfolio_returns',
    'get_transactions', 'get_trading_pnl', 'get_quote_v2', 'get_market_info', 'get_crypto_market',
    'get_crypto_positions', 'get_crypto_account_history',
}
EXPECTED_WRITES = {
    'apply_statement_import',
    'export_portfolio_dataset',
    'import_statement',
    'snapshot_portfolio_nav',
}
LOCAL_ONLY = {'get_kis_accounts', 'get_invest_mcp_help', 'get_service_health'}
LEDGER_OBSERVERS = {
    'preview_statement_import', 'get_toss_trade_history', 'get_kis_trade_history',
    'get_crypto_futures_fills', 'get_crypto_futures_funding_payments',
    'get_all_transactions', 'get_consolidated_holdings', 'get_realized_pnl', 'get_trade_review',
    'get_portfolio_performance',
    'get_holdings', 'get_portfolio_summary_v2', 'get_portfolio_returns', 'get_transactions', 'get_trading_pnl',
    'get_crypto_account_history',
}


def test_real_fastmcp_registry_annotations():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 68
    assert all(t.annotations is not None for t in tools)
    assert {t.name for t in tools if t.annotations.readOnlyHint} == EXPECTED_READ_ONLY
    assert {t.name for t in tools if not t.annotations.readOnlyHint} == EXPECTED_WRITES
    for tool in tools:
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is (tool.name not in LOCAL_ONLY)
        if tool.name in EXPECTED_READ_ONLY:
            assert tool.annotations.idempotentHint is True
        if tool.name in LEDGER_OBSERVERS:
            assert 'Fetched broker rows are recorded in the local append-only ledger (no broker writes).' in tool.description
