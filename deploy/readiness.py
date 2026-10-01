"""Bounded authenticated passive-health check; no OAuth mint/refresh or broker calls.

The existing bearer is read privately from the service's inherited environment,
never a credential file, command argument, output, log or persisted artifact.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from service_auth import SERVICE_READ_TOOLS

LOCAL = 'http://127.0.0.1:8100'
SERVICE_CREDENTIAL = Path('/run/credentials/toss-mcp.service/invest-desk-token')


class ReadinessFailed(RuntimeError):
    pass


def service_properties():
    raw = subprocess.check_output(['systemctl', 'show', 'toss-mcp.service',
        '--property=ActiveState,SubState,MainPID,NRestarts,WorkingDirectory'], text=True, stderr=subprocess.DEVNULL)
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def inherited_bearer(pid):
    raw = (Path('/proc') / str(pid) / 'environ').read_bytes()
    for item in raw.split(b'\0'):
        name, _, value = item.partition(b'=')
        if name == b'MCP_AUTH_TOKEN' and value:
            return value.decode()
    raise ReadinessFailed('inherited_bearer_missing')


def inherited_service_bearer(pid):
    raw = (Path('/proc') / str(pid) / 'environ').read_bytes()
    runtime = dict(item.split(b'=', 1) for item in raw.split(b'\0') if b'=' in item)
    # systemd 249 ignores unsupported %d assignments instead of failing startup.
    # Presence and the actual path are required, not merely LoadCredential alone.
    if (runtime.get(b'KINVEST_SERVICE_TOKEN_FILE') != str(SERVICE_CREDENTIAL).encode()
            or runtime.get(b'CREDENTIALS_DIRECTORY') != str(SERVICE_CREDENTIAL.parent).encode()):
        raise ReadinessFailed('service_token_runtime_path_missing_or_invalid')
    try:
        path = Path('/proc') / str(pid) / 'root' / str(SERVICE_CREDENTIAL).lstrip('/')
        token = path.read_text().strip()
    except OSError:
        raise ReadinessFailed('service_token_runtime_file_unavailable') from None
    if not token:
        raise ReadinessFailed('service_token_runtime_file_empty')
    return token


def store_ok(result):
    if result.isError:
        return False
    envelope = result.structuredContent
    if not isinstance(envelope, dict):
        return False
    data = envelope.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('auth'), dict):
        return False
    auth = data['auth']
    return envelope.get('ok') is True and auth.get('store_status') == 'ok' and auth.get('restart_durable') is True


async def authenticated_store_ok(bearer, client=None):
    async def check(http):
        async with streamable_http_client(LOCAL + '/mcp', http_client=http, terminate_on_close=False) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool('get_service_health', {'mode': 'passive'})
                return store_ok(result)
    if client is not None:
        return await check(client)
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + bearer}, trust_env=False,
                                 follow_redirects=False, timeout=2) as http:
        return await check(http)


async def service_readiness_checks(bearer, client=None):
    """Collect every safe restriction check before choosing success or rollback."""
    async def check(http):
        results = {}
        for name in ('service_tools_list_exact_eight', 'non_allowlisted_call_forbidden'):
            try:
                async with streamable_http_client(LOCAL + '/mcp', http_client=http, terminate_on_close=False) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        if name == 'service_tools_list_exact_eight':
                            tools = (await session.list_tools()).tools
                            results[name] = len(tools) == 8 and {tool.name for tool in tools} == SERVICE_READ_TOOLS
                        else:
                            result = await session.call_tool('get_service_health', {'mode': 'passive'})
                            results[name] = result.isError and any(getattr(item, 'text', None) == 'Forbidden' for item in result.content)
            except Exception:
                results[name] = False
        try:
            response = await http.get(LOCAL + '/mcp', headers={'X-Forwarded-For': '127.0.0.1'})
            results['service_token_forwarded_header_401'] = response.status_code == 401
        except Exception:
            results['service_token_forwarded_header_401'] = False
        return results
    if client is not None:
        return await check(client)
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + bearer}, trust_env=False,
                                 follow_redirects=False, timeout=2) as http:
        return await check(http)


async def wait_until_ready(revision, timeout=30, service_token_required=False):
    deadline = time.monotonic() + timeout
    first_pid = None
    expected = '/home/ubuntu/toss-mcp-releases/' + revision
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as http:
        while time.monotonic() < deadline:
            before = service_properties()
            if before.get('ActiveState') != 'active' or before.get('SubState') != 'running':
                raise ReadinessFailed('service_not_active_running')
            if before.get('WorkingDirectory') != expected:
                raise ReadinessFailed('wrong_release')
            pid = int(before['MainPID'])
            if pid <= 0 or first_pid is not None and pid != first_pid:
                raise ReadinessFailed('service_restarted_during_readiness')
            first_pid = pid
            try:
                health = await http.get(LOCAL + '/healthz')
            except httpx.ConnectError:
                await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))
                continue
            if health.status_code != 200 or health.text != 'ok':
                raise ReadinessFailed('liveness_failed')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ok = await asyncio.wait_for(authenticated_store_ok(inherited_bearer(pid)), timeout=remaining)
            if service_token_required:
                results = await asyncio.wait_for(service_readiness_checks(inherited_service_bearer(pid)),
                                                timeout=max(0, deadline - time.monotonic()))
                print(json.dumps({'service_token_checks': results}, sort_keys=True))
                if not all(results.values()):
                    raise ReadinessFailed('service_token_restriction_checks_failed')
            after = service_properties()
            if after.get('ActiveState') != 'active' or after.get('SubState') != 'running' or after.get('MainPID') != before['MainPID'] or after.get('NRestarts') != before.get('NRestarts'):
                raise ReadinessFailed('service_restarted_during_readiness')
            if not ok:
                raise ReadinessFailed('authenticated_store_not_ok')
            return
    raise ReadinessFailed('startup_timeout')


def main(revision, service_token_required=False):
    old_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise ReadinessFailed('revision_format')
        asyncio.run(asyncio.wait_for(wait_until_ready(revision, service_token_required=service_token_required), timeout=30))
    except Exception as exc:
        # ReadinessFailed contains only fixed reason names. Other exception text
        # may contain credentials/response bodies, so retain the class name only.
        reason = str(exc) if isinstance(exc, ReadinessFailed) else type(exc).__name__
        print(json.dumps({'readiness': 'failed', 'reason': reason}))
        return 1
    finally:
        logging.disable(old_logging)
    print('readiness=active_authenticated_store_ok')
    return 0


if __name__ == '__main__':
    valid = len(sys.argv) == 2 or len(sys.argv) == 3 and sys.argv[2] == '--service-token'
    sys.exit(main(sys.argv[1] if valid else '', len(sys.argv) == 3 and valid))
