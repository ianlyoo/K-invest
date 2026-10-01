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

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

LOCAL = 'http://127.0.0.1:8100'


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


async def wait_until_ready(revision, timeout=30):
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
            after = service_properties()
            if after.get('ActiveState') != 'active' or after.get('SubState') != 'running' or after.get('MainPID') != before['MainPID'] or after.get('NRestarts') != before.get('NRestarts'):
                raise ReadinessFailed('service_restarted_during_readiness')
            if not ok:
                raise ReadinessFailed('authenticated_store_not_ok')
            return
    raise ReadinessFailed('startup_timeout')


def main(revision):
    old_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise ReadinessFailed('revision_format')
        asyncio.run(asyncio.wait_for(wait_until_ready(revision), timeout=30))
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
    sys.exit(main(sys.argv[1] if len(sys.argv) == 2 else ''))
