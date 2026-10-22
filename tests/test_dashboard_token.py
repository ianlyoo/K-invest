"""Second direct-loopback service principal (local dashboard app), synthetic credentials only."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient
from test_auth_transport import URL
from test_service_auth import HEADERS, TOKEN, initialize, rpc

from service_auth import (
    DASHBOARD_READ_TOOLS,
    SERVICE_READ_TOOLS,
    DashboardAccessToken,
    ServiceTokenConfigurationError,
    ServiceTokenVerifier,
    load_dashboard_token,
)

DASHBOARD = 'synthetic-dashboard-token-with-43-character'  # the accepted 43-character base64url format
DASHBOARD_HEADERS = {**HEADERS, 'Authorization': 'Bearer ' + DASHBOARD}
STATIC = 'synthetic-existing-static'
ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'get_candles', 'get_quote', 'compare_quotes', 'get_stock_info', 'get_market_hours',
            'get_exchange_rate', 'get_sec_financials', 'get_ttm_financials', 'get_financials',
            'get_key_metrics', 'get_insider_trades', 'get_stock_snapshot', 'get_risk_free_rate',
            'get_toss_accounts', 'get_kis_accounts', 'get_toss_holdings', 'get_kis_domestic_balance',
            'get_kis_overseas_balance'}


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def registered_tools():
    import server
    return {tool.name for tool in server.mcp._tool_manager.list_tools()}


def application(existing=None):
    """Both principals in front of a synthetic stand-in for every registered tool."""
    import server
    fallback = existing or AsyncMock()
    if existing is None:
        fallback.verify_token.return_value = None
    verifier = ServiceTokenVerifier(digest(DASHBOARD), ServiceTokenVerifier(digest(TOKEN), fallback), DashboardAccessToken)
    app = server.ObservedFastMCP('synthetic', stateless_http=True, json_response=True, token_verifier=verifier,
        auth=AuthSettings(issuer_url=URL, resource_server_url=URL),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    called = []
    def synthetic(name):
        def read() -> dict[str, Any]:
            called.append(name)
            return {'ok': True, 'data': {'synthetic': True}}
        return read
    for name in sorted(registered_tools() | {'place_order', 'cancel_order', 'execute_trade'}):
        app.add_tool(synthetic(name), name=name)
    return app, fallback, called


def listed(client, headers):
    return {tool['name'] for tool in rpc(client, 'tools/list', headers=headers).json()['result']['tools']}


def call(client, name, headers):
    return rpc(client, 'tools/call', {'name': name, 'arguments': {}}, headers).json()['result']


def test_allowlist_is_the_exact_eighteen_registered_read_tools():
    assert DASHBOARD_READ_TOOLS == EXPECTED and len(DASHBOARD_READ_TOOLS) == 18
    assert DASHBOARD_READ_TOOLS <= registered_tools()
    # The existing principal is unchanged.
    assert SERVICE_READ_TOOLS == {'get_kis_accounts', 'get_toss_accounts', 'get_kis_trade_history',
        'get_toss_trade_history', 'get_kis_domestic_balance', 'get_kis_overseas_balance',
        'get_toss_holdings', 'get_candles'}


def test_each_token_lists_only_its_own_allowlist():
    app, fallback, called = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, DASHBOARD_HEADERS).status_code == 200
        assert listed(client, DASHBOARD_HEADERS) == DASHBOARD_READ_TOOLS
        assert initialize(client).status_code == 200
        assert listed(client, HEADERS) == SERVICE_READ_TOOLS
    assert called == []
    fallback.verify_token.assert_not_awaited()


def test_dashboard_token_dispatches_inside_and_is_forbidden_outside_its_allowlist():
    app, fallback, called = application()
    outside = sorted((registered_tools() | {'place_order', 'cancel_order', 'execute_trade', 'unregistered_tool'})
                     - DASHBOARD_READ_TOOLS)
    assert {'get_kis_trade_history', 'get_toss_trade_history', 'export_portfolio_dataset', 'get_portfolio_summary',
            'snapshot_portfolio_nav', 'preview_statement_import', 'apply_statement_import',
            'get_crypto_futures_positions', 'get_service_health'} <= set(outside)
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, DASHBOARD_HEADERS).status_code == 200
        for name in outside:
            result = call(client, name, DASHBOARD_HEADERS)
            assert result['isError'] and result['content'][0]['text'] == 'Forbidden', name
        assert called == []
        for name in sorted(DASHBOARD_READ_TOOLS):
            result = call(client, name, DASHBOARD_HEADERS)
            assert not result['isError'] and result['structuredContent']['ok'], name
    assert called == sorted(DASHBOARD_READ_TOOLS)
    fallback.verify_token.assert_not_awaited()


def test_existing_token_allowlist_is_unchanged_by_the_second_principal():
    app, _, called = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client).status_code == 200
        for name in sorted(DASHBOARD_READ_TOOLS - SERVICE_READ_TOOLS):
            result = call(client, name, HEADERS)
            assert result['isError'] and result['content'][0]['text'] == 'Forbidden', name
        for name in sorted(SERVICE_READ_TOOLS):
            assert not call(client, name, HEADERS)['isError'], name
    assert called == sorted(SERVICE_READ_TOOLS)


@pytest.mark.parametrize('token', ['existing', 'dashboard'])
@pytest.mark.parametrize('header,value', [('X-Forwarded-For', '127.0.0.1'), ('X-Real-IP', '127.0.0.1'),
    ('Forwarded', 'for=127.0.0.1'), ('X-Forwarded-For', '')])
@pytest.mark.parametrize('peer', ['127.0.0.1', '198.51.100.1'])
def test_both_tokens_rejected_through_a_proxy_or_with_forwarded_headers(token, header, value, peer):
    app, fallback, called = application()
    headers = {**(DASHBOARD_HEADERS if token == 'dashboard' else HEADERS), header: value}
    with TestClient(app.streamable_http_app(), base_url=URL, client=(peer, 12345)) as client:
        response = rpc(client, 'tools/list', headers=headers)
        assert response.status_code == 401
        assert DASHBOARD not in response.text and TOKEN not in response.text
    assert called == []
    fallback.verify_token.assert_not_awaited()


@pytest.mark.parametrize('token', ['existing', 'dashboard'])
@pytest.mark.parametrize('peer', ['::1', '127.0.0.2', '::ffff:127.0.0.1', '198.51.100.1', None])
def test_both_tokens_rejected_from_non_loopback_peers(caplog, token, peer):
    app, fallback, _ = application()
    headers = DASHBOARD_HEADERS if token == 'dashboard' else HEADERS
    with TestClient(app.streamable_http_app(), base_url=URL, client=(peer, 12345) if peer else None) as client:
        with caplog.at_level(logging.INFO, logger='k-invest'):
            assert rpc(client, 'tools/list', headers=headers).status_code == 401
    assert DASHBOARD not in caplog.text and TOKEN not in caplog.text
    fallback.verify_token.assert_not_awaited()


def test_oauth_client_named_like_the_dashboard_principal_gains_no_dashboard_restriction_or_grant():
    existing = AsyncMock()
    existing.verify_token.return_value = AccessToken(token='synthetic-other', client_id='dashboard-service', scopes=[])
    app, _, _ = application(existing)
    other = {**HEADERS, 'Authorization': 'Bearer synthetic-other'}
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, other).status_code == 200
        assert listed(client, other) == registered_tools() | {'place_order', 'cancel_order', 'execute_trade'}
        assert initialize(client, DASHBOARD_HEADERS).status_code == 200
        assert listed(client, DASHBOARD_HEADERS) == DASHBOARD_READ_TOOLS


NEAR_MISSES = {'prefix': DASHBOARD[:-1], 'suffix': DASHBOARD + 'x', 'upper': DASHBOARD.upper(), 'swapcase': DASHBOARD.swapcase(),
               'last-character': DASHBOARD[:-1] + 'X', 'padded': DASHBOARD + '=', 'doubled': DASHBOARD * 2}


@pytest.mark.parametrize('near', NEAR_MISSES)
def test_near_miss_dashboard_tokens_are_never_the_dashboard_principal(near):
    token = NEAR_MISSES[near]
    assert token != DASHBOARD
    app, fallback, called = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, {**HEADERS, 'Authorization': 'Bearer ' + token}).status_code == 401
        assert initialize(client, DASHBOARD_HEADERS).status_code == 200
    # Not the dashboard digest: it falls through to the existing verifier, which rejects it.
    fallback.verify_token.assert_awaited_once_with(token)
    assert called == []


# ── Optional credential loading ──────────────────────────


def credentials(tmp_path, monkeypatch, content=DASHBOARD + '\n', mode=0o400):
    directory = tmp_path / 'synthetic-credentials'
    directory.mkdir()
    path = directory / 'dashboard-token'
    if content is not None:
        path.write_text(content)
        path.chmod(mode)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(directory))
    return path


@pytest.mark.parametrize('case', ['no-credentials-directory', 'relative-directory', 'absent-file'])
def test_absent_credential_disables_only_the_dashboard_principal(tmp_path, monkeypatch, caplog, case):
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    if case == 'relative-directory':
        monkeypatch.setenv('CREDENTIALS_DIRECTORY', 'synthetic-relative')
    elif case == 'absent-file':
        credentials(tmp_path, monkeypatch, content=None)
    with caplog.at_level(logging.INFO, logger='k-invest'):
        assert load_dashboard_token(STATIC, digest(TOKEN)) is None
        assert load_dashboard_token(STATIC, None) is None
    assert caplog.messages == ['dashboard_token=disabled reason=DashboardTokenAbsent'] * 2


@pytest.mark.parametrize('mode', [0o400, 0o600])
@pytest.mark.parametrize('content', [DASHBOARD + '\n', DASHBOARD])
def test_systemd_credential_load_retains_only_digest(tmp_path, monkeypatch, caplog, mode, content):
    path = credentials(tmp_path, monkeypatch, content=content, mode=mode)
    with caplog.at_level(logging.INFO, logger='k-invest'):
        assert load_dashboard_token(STATIC, digest(TOKEN)) == digest(DASHBOARD)
    assert 'dashboard_token=loaded collision_guard=passed' in caplog.text
    assert DASHBOARD not in caplog.text and str(path) not in caplog.text


# Each disables only the dashboard principal: (file content or a metadata change, fixed reason).
DASHBOARD_FILE_PROBLEMS = {
    'public': ('chmod-644', 'DashboardTokenFileNotPrivate'),
    'group-readable': ('chmod-640', 'DashboardTokenFileNotPrivate'),
    'symlink': ('symlink', 'DashboardTokenFileUnavailable'),
    'directory': ('directory', 'DashboardTokenFileUnavailable'),
    'oversize': ('a' * 4097, 'DashboardTokenFileNotPrivate'),
    'empty': ('', 'DashboardTokenEmpty'),
    'whitespace-only': (' \n\t\n', 'DashboardTokenEmpty'),
    'source-missing-default': ('source-missing', 'DashboardTokenSourceMissing'),
    'base64-standard': ('synthetic+dashboard/token+with/43+character=', 'DashboardTokenMalformed'),
    'base64-padded': (DASHBOARD[:42] + '=\n', 'DashboardTokenMalformed'),
    'prefix-42': (DASHBOARD[:-1] + '\n', 'DashboardTokenMalformed'),
    'suffix-44': (DASHBOARD + 'x\n', 'DashboardTokenMalformed'),
    'legacy-32-to-256': ('synthetic-dashboard-token-distinct-with-32-char\n', 'DashboardTokenMalformed'),
    'crlf': (DASHBOARD + '\r\n', 'DashboardTokenMalformed'),
    'two-newlines': (DASHBOARD + '\n\n', 'DashboardTokenMalformed'),
    'leading-space': (' ' + DASHBOARD + '\n', 'DashboardTokenMalformed'),
    'trailing-space': (DASHBOARD + ' \n', 'DashboardTokenMalformed'),
    'spaces': ('synthetic token with spaces in forty-three c', 'DashboardTokenMalformed'),
    'non-ascii': (DASHBOARD[:41] + 'é\n', 'DashboardTokenMalformed'),
    'two-tokens': (DASHBOARD + '\n' + DASHBOARD + '\n', 'DashboardTokenMalformed'),
}


@pytest.mark.parametrize('case', DASHBOARD_FILE_PROBLEMS)
def test_bad_dashboard_credential_disables_only_the_dashboard_principal(tmp_path, monkeypatch, caplog, case):
    change, reason = DASHBOARD_FILE_PROBLEMS[case]
    path = credentials(tmp_path, monkeypatch)
    if change.startswith('chmod-'):
        path.chmod(int(change[6:], 8))
    elif change == 'symlink':
        target = tmp_path / 'synthetic-target'
        path.rename(target)
        path.symlink_to(target)
    elif change == 'directory':
        path.unlink()
        path.mkdir()
    else:
        path.chmod(0o600)
        path.write_bytes(change.encode())
    with caplog.at_level(logging.INFO, logger='k-invest'):
        assert load_dashboard_token(STATIC, digest(TOKEN)) is None
        # Even the configuration-error inputs never get that far.
        assert load_dashboard_token('', digest(DASHBOARD)) is None
    assert caplog.messages == [f'dashboard_token=disabled reason={reason}'] * 2
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    for secret in (DASHBOARD, DASHBOARD[:20], str(path), str(tmp_path)):
        assert secret not in caplog.text


@pytest.mark.parametrize('failure', ['static-collision', 'service-collision', 'no-static-bearer'])
def test_colliding_dashboard_token_is_a_configuration_error_that_refuses_startup(tmp_path, monkeypatch, failure):
    path = credentials(tmp_path, monkeypatch)
    static, service = {'static-collision': (DASHBOARD, digest(TOKEN)), 'service-collision': (STATIC, digest(DASHBOARD)),
                       'no-static-bearer': ('', digest(TOKEN))}[failure]
    with pytest.raises(ServiceTokenConfigurationError) as caught:
        load_dashboard_token(static, service)
    message = str(caught.value)
    assert message == ('ExistingStaticBearerRequired' if failure == 'no-static-bearer' else 'DashboardTokenCollision')
    assert DASHBOARD not in message and str(path) not in message and str(tmp_path) not in message



STARTUP = '''import asyncio, json, socket
def blocked(*args, **kwargs):
    raise AssertionError("Outgoing network is forbidden")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
import kinvest_common
kinvest_common.apply_env_file = lambda path: None
import server, service_auth
service_auth._direct_loopback.set(True)
verifier = server._resolve_token_verifier()
async def principal(token):
    access = await verifier.verify_token(token)
    return access and [type(access).__name__, access.client_id, len(getattr(access, 'read_tools', ()))]
print(json.dumps({name: asyncio.run(principal(token)) for name, token in
                  (("dashboard", DASHBOARD), ("service", SERVICE), ("static", STATIC))}))
'''


DASHBOARD_RUNTIME = {'valid': (DASHBOARD + '\n', None), 'malformed': ('synthetic+dashboard/token+with/43+character=\n',
    'DashboardTokenMalformed'), 'empty': ('', 'DashboardTokenEmpty'), 'source-missing': ('source-missing',
    'DashboardTokenSourceMissing')}


@pytest.mark.parametrize('service_file, dashboard_file', [(False, None), (True, None), (False, 'valid'), (True, 'valid'),
    (False, 'malformed'), (True, 'malformed'), (True, 'empty'), (True, 'source-missing')])
def test_server_starts_with_and_without_either_credential(tmp_path, service_file, dashboard_file):
    directory = tmp_path / 'synthetic-credentials'
    directory.mkdir()
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'MCP_AUTH_TOKEN': STATIC,
           'KINVEST_CACHE_DIR': str(tmp_path), 'CREDENTIALS_DIRECTORY': str(directory)}
    if service_file:
        (directory / 'service-token').write_text(TOKEN + '\n')
        (directory / 'service-token').chmod(0o400)
        env['KINVEST_SERVICE_TOKEN_FILE'] = str(directory / 'service-token')
    content, reason = DASHBOARD_RUNTIME.get(dashboard_file, (None, 'DashboardTokenAbsent'))
    if content is not None:
        (directory / 'dashboard-token').write_text(content)
        (directory / 'dashboard-token').chmod(0o400)
    script = f'DASHBOARD, SERVICE, STATIC = {DASHBOARD!r}, {TOKEN!r}, {STATIC!r}\n' + STARTUP
    result = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    # A bad dashboard credential never stops the server: the other principals still verify.
    assert result.returncode == 0, result.stderr[-2000:]
    principals = json.loads(result.stdout.strip().splitlines()[-1])
    loaded = dashboard_file == 'valid'
    assert principals['dashboard'] == (['DashboardAccessToken', 'dashboard-service', 18] if loaded else None)
    assert principals['service'] == (['ServiceAccessToken', 'loopback-service', 8] if service_file else None)
    assert principals['static'] is not None and principals['static'][0] != 'DashboardAccessToken'
    assert ('dashboard_token=loaded collision_guard=passed' in result.stderr) is loaded
    assert (f'dashboard_token=disabled reason={reason}' in result.stderr) is not loaded
    for secret in (DASHBOARD, TOKEN, STATIC, 'synthetic+dashboard'):
        assert secret not in result.stderr


# ── Account numbers stay masked in every allowed account tool ──

# Synthetic KIS accounts as (CANO, ACNT_PRDT_CD); 22 is 연금저축.
KIS_FULL = {'primary': ('81234567', '01'), 'secondary': ('82345678', '01'), 'pension': ('87654321', '22')}
ACCOUNT_TOOLS = ('get_toss_accounts', 'get_kis_accounts', 'get_toss_holdings', 'get_kis_domestic_balance',
                 'get_kis_overseas_balance')


def kis_client(cano, product):
    """A real KISClient whose HTTP returns the documented TTTC8434R, TTTS3012R and
    CTRP6504R shapes, carrying the account the way KIS does."""
    from kis_api import KISClient
    client = object.__new__(KISClient)
    client.cano, client.acnt_prdt_cd, client.seen = cano, product, []
    context = f'{cano}^{product}^N^N^01^^'  # ctx_area_fk keys echo the account
    done = {'rt_cd': '0', 'msg_cd': 'KIOK0510', 'msg1': '조회가 완료되었습니다'}
    def request(path, params, tr_id, tr_cont=''):
        assert (params['CANO'], params['ACNT_PRDT_CD']) == (cano, product)
        client.seen.append(tr_id)
        if tr_id == 'TTTC8434R':
            return {**done, 'ctx_area_fk100': context, 'ctx_area_nk100': '', 'output1': [
                {'pdno': '005930', 'prdt_name': '삼성전자', 'trad_dvsn_name': '현금', 'hldg_qty': '3',
                 'ord_psbl_qty': '3', 'pchs_avg_pric': '71000.0000', 'pchs_amt': '213000', 'prpr': '72000',
                 'evlu_amt': '216000', 'evlu_pfls_amt': '3000', 'evlu_pfls_rt': '1.41'}],
                'output2': [{'dnca_tot_amt': '1000000', 'tot_evlu_amt': '1216000', 'nass_amt': '1216000'}]}, 'D'
        if tr_id == 'TTTS3012R':
            rows = [{'cano': cano, 'acnt_prdt_cd': product, 'prdt_type_cd': '512', 'ovrs_pdno': 'AAPL',
                     'ovrs_item_name': '애플', 'frcr_evlu_pfls_amt': '20.00000', 'evlu_pfls_rt': '5.26',
                     'pchs_avg_pric': '190.0000', 'ovrs_cblc_qty': '2', 'ord_psbl_qty': '2',
                     'frcr_pchs_amt1': '380.00000', 'ovrs_stck_evlu_amt': '400.00000', 'now_pric2': '200.000000',
                     'tr_crcy_cd': 'USD', 'ovrs_excg_cd': 'NASD', 'loan_type_cd': '00', 'loan_dt': '', 'expd_dt': ''}]
            return {**done, 'ctx_area_fk200': context, 'ctx_area_nk200': '',
                    'output1': rows if params['OVRS_EXCG_CD'] == 'NASD' else [],
                    'output2': {'frcr_pchs_amt1': '380.00', 'tot_evlu_pfls_amt': '20.00'}}, 'D'
        assert tr_id == 'CTRP6504R'
        return {**done, 'output1': [], 'output2': [{'crcy_cd': 'USD', 'frcr_dncl_amt_2': '100.00'}],
                'output3': [{'tot_asst_amt': '1500000'}]}, 'D'
    client._request = request
    return client


def account_registries(monkeypatch, *, loaded=False):
    """Toss and KIS registries with synthetic numbers; returns every full number."""
    from test_toss_account_masked import FULL, Registry, fake_client

    import server
    from kis_accounts import KISAccount
    accounts = [KISAccount(label=label, cano=cano, acnt_prdt_cd=product) for label, (cano, product) in KIS_FULL.items()]
    clients = {account.label: kis_client(account.cano, account.acnt_prdt_cd) for account in accounts}
    kis = SimpleNamespace(accounts=lambda: list(accounts), accounts_metadata=lambda: [a.metadata() for a in accounts],
        account_labels=lambda: list(clients), default_label=lambda: 'primary',
        client_for=lambda label='': clients[label or 'primary'])
    toss = {label: fake_client(label) for label in FULL}
    for label, client in toss.items():
        # Nested, differently named and free-text identifiers.
        client.get_holdings = lambda symbol=None, number=FULL[label]: {'result': {'accountNo': number, 'holdings': [
            {'symbol': 'US19801212001', 'name': 'Apple', 'quantity': '1', 'memo': f'계좌 {number}',
             'account': {'accountNumber': number.replace('-', ''), 'account_no': number}}]}}
    monkeypatch.setattr(server, '_get_toss_registry', lambda: Registry(toss))
    monkeypatch.setattr(server, '_get_kis_registry', lambda: kis)
    monkeypatch.setattr(server, '_kis_accounts', kis if loaded else None)
    return [*FULL.values(), *(cano for cano, _ in KIS_FULL.values())], clients


def served(tools, existing=None):
    """Both service principals and an existing verifier in front of `tools`."""
    import server
    fallback = existing or AsyncMock()
    if existing is None:
        fallback.verify_token.return_value = None
    verifier = ServiceTokenVerifier(digest(DASHBOARD), ServiceTokenVerifier(digest(TOKEN), fallback), DashboardAccessToken)
    app = server.ObservedFastMCP('synthetic', stateless_http=True, json_response=True, token_verifier=verifier,
        auth=AuthSettings(issuer_url=URL, resource_server_url=URL),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    for name, fn in tools.items():
        app.add_tool(fn, name=name)
    return app


def tool_result(client, name, arguments, headers):
    result = rpc(client, 'tools/call', {'name': name, 'arguments': arguments}, headers).json()['result']
    text = '\n'.join(block['text'] for block in result['content'])
    return result, text + '\n' + json.dumps(result.get('structuredContent'), ensure_ascii=False)


def assert_no_full_number(text, fulls):
    for full in fulls:
        digits = full.replace('-', '')
        assert full not in text and digits not in text, full
        assert len(digits) < 10 or digits[:-4] not in text, full


@pytest.mark.parametrize('loaded', [False, True])
@pytest.mark.parametrize('account', ['', 'all'])
def test_dashboard_account_tools_emit_only_masked_account_numbers(monkeypatch, caplog, account, loaded):
    import server
    fulls, clients = account_registries(monkeypatch, loaded=loaded)
    app = served({name: getattr(server, name) for name in ACCOUNT_TOOLS})
    results = {}
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, DASHBOARD_HEADERS).status_code == 200
        for name in ACCOUNT_TOOLS:
            result, dumped = tool_result(client, name, {} if name.endswith('_accounts') else {'account': account},
                                         DASHBOARD_HEADERS)
            assert not result['isError'] and result['structuredContent']['ok'], (name, result)
            # Text and structured content carry the same masked data.
            assert json.loads(result['content'][0]['text']) == result['structuredContent'], name
            assert_no_full_number(dumped, fulls)
            results[name] = result['structuredContent']['data']
    assert DASHBOARD not in caplog.text
    assert results['get_toss_accounts']['masked'] == {'primary': '••••3456', 'secondary': '••••7890'}
    assert {row['acnt_prdt_cd'] for row in results['get_kis_accounts']['accounts']} == {'01', '22'}
    if account == 'all':
        toss = {entry['account']: entry['data'] for entry in results['get_toss_holdings']['accounts']}
        overseas = {entry['account']: entry['data'] for entry in results['get_kis_overseas_balance']['accounts']}
        domestic = {entry['account']: entry['data'] for entry in results['get_kis_domestic_balance']['accounts']}
        assert overseas['pension']['code'] == 'OVERSEAS_NOT_SUPPORTED'
    else:
        toss = {'primary': results['get_toss_holdings']}
        overseas = {'primary': results['get_kis_overseas_balance']}
        domestic = {'primary': results['get_kis_domestic_balance']}
    holding = toss['primary']['result']
    assert holding['accountNo'] == '••••3456' and holding['holdings'][0]['memo'] == '계좌 ••••3456'
    assert holding['holdings'][0]['account'] == {'accountNumber': '••••3456', 'account_no': '••••3456'}
    assert overseas['primary']['stocks'][0]['cano'] == '••••4567'
    assert overseas['primary']['stocks'][0]['acnt_prdt_cd'] == '01'
    assert overseas['primary']['stocks'][0]['ovrs_pdno'] == 'AAPL'
    assert domestic['primary']['stocks'][0]['pdno'] == '005930'
    if account == 'all':
        assert overseas['secondary']['stocks'][0]['cano'] == '••••5678'
    # Five exchanges plus present balance per overseas-capable account, one domestic call.
    assert clients['primary'].seen.count('TTTS3012R') == 5 and clients['primary'].seen.count('CTRP6504R') == 1
    assert clients['pension'].seen == (['TTTC8434R'] if account == 'all' else [])


@pytest.mark.parametrize('principal', ['loopback-service', 'oauth'])
def test_other_principals_get_the_unfiltered_byte_pinned_outputs(monkeypatch, principal):
    import server
    account_registries(monkeypatch)
    existing = AsyncMock()
    existing.verify_token.return_value = AccessToken(token='synthetic-oauth', client_id='synthetic-owner', scopes=[])
    headers = HEADERS if principal == 'loopback-service' else {**HEADERS, 'Authorization': 'Bearer synthetic-oauth'}
    app = served({name: getattr(server, name) for name in ACCOUNT_TOOLS}, existing)
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, headers).status_code == 200
        for name in ACCOUNT_TOOLS:
            arguments = {} if name.endswith('_accounts') else {'account': 'all'}
            result, _ = tool_result(client, name, arguments, headers)
            # The dashboard filter is the dashboard's alone; the eight raw tools stay byte-pinned.
            assert result['structuredContent'] == json.loads(json.dumps(getattr(server, name)(**arguments))), name


PAYLOAD = {'ok': True, 'data': {'rows': [{'CANO': '82345678', 'ACNT_PRDT_CD': '01', 'nested': {
    'accountNo': '900-11-22223456', 'ord_account_number': 9001122223456}}],
    'note': 'from 82345678-01 and 900-11-22223456', 'short': {'acnt_no': '12'}}}


@pytest.mark.parametrize('shape', ['structured', 'text', 'error'])
def test_every_dashboard_tool_output_is_masked_at_the_principal_boundary(monkeypatch, shape):
    import server
    from kis_accounts import KISAccount
    configured = KISAccount(label='primary', cano='81234567', acnt_prdt_cd='01')
    monkeypatch.setattr(server, '_kis_accounts', SimpleNamespace(accounts=lambda: [configured]))
    def structured() -> dict[str, Any]:
        return json.loads(json.dumps(PAYLOAD))
    def text() -> str:
        return 'configured 81234567-01'
    def error() -> dict[str, Any]:
        raise RuntimeError('broker rejected 81234567')
    tool = {'structured': structured, 'text': text, 'error': error}[shape]
    app = served({name: tool for name in sorted(DASHBOARD_READ_TOOLS)})
    fulls = ['81234567', '82345678', '900-11-22223456']
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client, DASHBOARD_HEADERS).status_code == 200
        for name in sorted(DASHBOARD_READ_TOOLS):
            result, dumped = tool_result(client, name, {}, DASHBOARD_HEADERS)
            assert_no_full_number(dumped, fulls)
            if shape == 'structured':
                data = result['structuredContent']['data']
                assert json.loads(result['content'][0]['text']) == result['structuredContent']
                assert data['rows'][0] == {'CANO': '••••5678', 'ACNT_PRDT_CD': '01', 'nested': {
                    'accountNo': '••••3456', 'ord_account_number': '••••3456'}}, name
                assert data['note'] == 'from ••••5678-01 and ••••3456' and data['short'] == {'acnt_no': None}
            elif shape == 'text':
                assert result['content'][0]['text'] == 'configured ••••4567-01', name
            else:
                assert result['isError'] and 'broker rejected ••••4567' in result['content'][0]['text'], name
        if shape == 'structured':
            # The loopback-service principal's overlapping tools are unchanged by the dashboard filter.
            assert initialize(client).status_code == 200
            assert tool_result(client, 'get_toss_holdings', {}, HEADERS)[0]['structuredContent'] == PAYLOAD


def test_masking_covers_a_tool_returning_call_tool_result():
    from mcp.types import CallToolResult, ImageContent, TextContent

    import server
    image = ImageContent(type='image', data='AA==', mimeType='image/png')
    result = CallToolResult(content=[TextContent(type='text', text='{"cano": "81234567"}'),
                                     TextContent(type='text', text='plain 81234567'), image],
                            structuredContent={'cano': '81234567'})
    masked = server._masked_tool_result(result, set())
    assert masked.structuredContent == {'cano': '••••4567'}
    assert json.loads(masked.content[0].text) == {'cano': '••••4567'}
    assert masked.content[1].text == 'plain ••••4567' and masked.content[2] == image
    assert result.structuredContent == {'cano': '81234567'}  # never mutated in place
