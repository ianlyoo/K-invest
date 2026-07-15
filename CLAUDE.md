# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

K-invest: read-only investment data MCP server (FastMCP, Streamable HTTP) aggregating
Toss Securities, KIS, SEC EDGAR, yfinance, Binance USD-M Futures, and optional
margin-ta technical analysis. Binds 127.0.0.1:8100; exposed via reverse proxy with
static Bearer auth (`MCP_AUTH_TOKEN`).

## Commands

```bash
python3 -m pytest tests/                          # run all tests
python3 -m pytest tests/test_toss_accounts.py -k name  # single file / single test
ruff check .                                      # lint
ruff format .                                     # format
python3 server.py                                 # run locally (binds 127.0.0.1:8100)
```

Dependencies: `pip install -r requirements.txt` (or `pip install -e .`).

## Architecture

- `server.py` is the single entry point: FastMCP app, `StaticTokenVerifier` bearer auth, and all `@mcp.tool()` definitions (~40 read-only tools). Provider clients are lazy singletons via `_get_toss()`, `_get_kis()`, `_get_sec()`, `_get_yf()`, `_get_mt()`, `_get_binance()`.
- One module per provider: `toss_api.py`, `kis_api.py`, `sec_api.py` (+ `sec_insider/ttm/segments/quality/derived.py`), `yfinance_api.py` (+ `yfinance_metrics/consensus.py`), `binance_futures_api.py`, `margin_ta_runner.py` (optional subprocess wrapper, `MARGIN_TA_HOME`). Shared helpers in `kinvest_common.py` (`NotConfiguredError`, `cache_dir()`, env-file loaders).
- **Envelope contract**: every tool returns `{ok, data, error, meta}` via `_ok()` / `_fail()` in server.py — never raw payloads or JSON strings. Errors carry `code`, `message`, `provider`, `retryable`; unconfigured providers yield `*_NOT_CONFIGURED`.
- **READ-ONLY invariant**: never add order create/modify/cancel tools or expose credentials in responses.
- Quotes from different providers legitimately differ (exchange, session, delay) — composite tools must carry provider/session warnings rather than reconcile prices.
- LLM-friendliness: `Literal` enums for params, `detail_level="summary"|"full"` defaults to summary, percentage fields suffixed `_pct`, period fields disambiguate FY/TTM/quarter.

## Credentials & config

- All configuration is via env vars — see `.env.example` for the full reference. `server.py` also loads `./.env` (setdefault).
- Never read or print `.env` contents; permission rules in `.claude/settings.json` deny it.
- Machine-specific deployment notes live in `CLAUDE.local.md` (untracked).
