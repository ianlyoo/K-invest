"""Upgrade/rollback against temporary paths, stub processes and synthetic state."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_deploy_procedure import REV, synthetic_host

FROM = '6b6d90ecbe90130bcbefd6a63d42ada15f8d06a1'
DIGEST = 'c96c6d7a457f97820522f29e370428483d64d937b6706fb776fad42a774cc0b6'


def upgrade_host(tmp_path, fail_stage=None):
    host = synthetic_host(tmp_path, fail_stage)
    old = host.release.parent / FROM
    original = (f'[Service]\nWorkingDirectory={old}\nExecStart=\n'
                f'ExecStart={old}/.venv/bin/python {old}/server.py\n'
                f'Environment=KINVEST_OAUTH_STATE_DIR={host.backup.parent}/oauth\n'
                'UMask=0077\n' + f'ReadWritePaths={host.backup.parent}/oauth\n')
    digest = hashlib.sha256(original.encode()).hexdigest()
    text = host.script.read_text().replace(DIGEST, digest)
    host.script.write_text(text)
    (host.release / 'deploy/release.sh').write_text(text)
    host.dropin.parent.mkdir(parents=True)
    host.dropin.write_text(original)
    host.dropin.chmod(0o644)
    oldbackup = host.backup.parent / ('rollback-' + FROM)
    oldbackup.mkdir(parents=True, mode=0o700)
    (oldbackup / 'installed-dropin.sha256').write_text(f'{digest}  {host.dropin}\n')
    host.backup.parent.chmod(0o700)
    state = host.backup.parent / 'oauth'
    state.mkdir(mode=0o700)
    database = state / 'oauth.sqlite3'
    database.write_bytes(b'synthetic-persisted-grants')
    database.chmod(0o600)
    python = old / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\nset -euo pipefail\nprintf "old-readiness\\n" >> "$FIXTURE_LOG"\n')
    python.chmod(0o755)
    source = host.unit.parent / 'toss-mcp/invest-desk-token'
    source.parent.mkdir(mode=0o700)
    source.write_text('synthetic-private-service-token-with-32-characters\n')
    source.chmod(0o600)
    stat = Path(host.env['PATH'].split(':')[0]) / 'stat'
    stat.write_text('#!/bin/bash\nset -euo pipefail\nif [[ "$*" == *invest-desk-token* ]]; then printf "600:0:0:regular file\\n"; else printf "700:ubuntu:ubuntu\\n"; fi\n')
    return host, original, database


def test_upgrade_adds_only_credential_path_metadata_and_rollback_restores_bytes(tmp_path):
    host, original, database = upgrade_host(tmp_path)
    inode = database.stat().st_ino
    unit = host.unit.read_bytes()
    other = host.dropin.parent / '10-owner.conf'
    other.write_text('fixture-owner-dropin\n')
    assert host.run('upgrade-activate').returncode == 0
    expected = (original.replace(str(host.release.parent / FROM), str(host.release)) +
        f'LoadCredential=invest-desk-token:{host.unit.parent}/toss-mcp/invest-desk-token\n'
        'Environment=KINVEST_SERVICE_TOKEN_FILE=%d/invest-desk-token\n')
    assert host.dropin.read_text() == expected
    assert (host.backup / 'previous-dropin.conf').read_text() == original
    assert host.backup.stat().st_mode & 0o777 == 0o700
    assert host.run('revert').returncode == 0
    assert host.dropin.read_text() == original
    assert (host.backup / 'removed-dropin.conf').read_text() == expected
    assert host.unit.read_bytes() == unit and other.read_text() == 'fixture-owner-dropin\n'
    assert database.read_bytes() == b'synthetic-persisted-grants' and database.stat().st_ino == inode
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.parent.stat().st_mode & 0o777 == 0o700
    assert host.log.read_text().count('restart toss-mcp.service') == 2
    assert 'old-readiness' in host.log.read_text()


@pytest.mark.parametrize('stage', ['pip', 'ruff', 'pytest'])
def test_upgrade_failed_validation_preserves_live_dropin_without_restart(tmp_path, stage):
    host, original, _ = upgrade_host(tmp_path, stage)
    assert host.run('upgrade-activate').returncode != 0
    assert host.dropin.read_text() == original
    assert not host.backup.exists() and not host.log.exists()


@pytest.mark.parametrize('conflict', ['digest', 'receipt', 'backup', 'staging', 'missing-state', 'missing-database'])
def test_upgrade_rejects_tampering_existing_targets_and_missing_state(tmp_path, conflict):
    host, original, database = upgrade_host(tmp_path)
    if conflict == 'digest':
        host.dropin.write_text(original + '# fixture-owner-edit\n')
    elif conflict == 'receipt':
        (host.backup.parent / ('rollback-' + FROM) / 'installed-dropin.sha256').write_text('fixture-invalid-receipt\n')
    elif conflict == 'backup':
        host.backup.mkdir()
    elif conflict == 'staging':
        (host.dropin.parent / f'.90-durable-oauth-{REV}.tmp').write_text('fixture-staging-owner\n')
    else:
        database.unlink()
        if conflict == 'missing-state':
            database.parent.rmdir()
    before = host.dropin.read_bytes()
    assert host.run('upgrade-activate').returncode != 0
    assert host.dropin.read_bytes() == before and not host.log.exists()


def test_upgrade_readiness_failure_restores_live_release_without_touching_state(tmp_path):
    host, original, database = upgrade_host(tmp_path, 'readiness')
    result = host.run('upgrade-activate')
    assert result.returncode != 0 and 'guarded rollback completed' in result.stderr
    assert host.dropin.read_text() == original
    assert database.read_bytes() == b'synthetic-persisted-grants'
    assert host.log.read_text().count('restart toss-mcp.service') == 2
    assert 'old-readiness' in host.log.read_text()


def test_upgrade_rechecks_source_after_new_release_validation(tmp_path):
    host, original, _ = upgrade_host(tmp_path)
    builder = Path(host.env['PATH'].split(':')[0]) / 'python3'
    builder.write_text(builder.read_text() + '''cat > "$3/bin/ruff" <<'EOF'
#!/bin/bash
printf '# fixture-concurrent-edit\\n' >> "$FIXTURE_DROPIN"
EOF
chmod +x "$3/bin/ruff"
''')
    host.env['FIXTURE_DROPIN'] = str(host.dropin)
    assert host.run('upgrade-activate').returncode != 0
    assert host.dropin.read_text() == original + '# fixture-concurrent-edit\n'
    assert not host.backup.exists() and not host.log.exists()


@pytest.mark.parametrize('tamper', ['current', 'previous', 'missing-previous', 'removed-copy'])
def test_upgrade_rollback_stops_on_edits_or_existing_saved_copy(tmp_path, tamper):
    host, _, _ = upgrade_host(tmp_path)
    assert host.run('upgrade-activate').returncode == 0
    if tamper == 'current':
        host.dropin.write_text(host.dropin.read_text() + '# fixture-owner-edit\n')
    elif tamper == 'previous':
        (host.backup / 'previous-dropin.conf').write_text('fixture-owner-edit\n')
    elif tamper == 'missing-previous':
        (host.backup / 'previous-dropin.conf').unlink()
    else:
        (host.backup / 'removed-dropin.conf').write_text('fixture-owner-copy\n')
    before = host.dropin.read_bytes()
    assert host.run('revert').returncode != 0
    assert host.dropin.read_bytes() == before
    assert host.log.read_text().count('restart toss-mcp.service') == 1


@pytest.mark.parametrize('tamper', ['none', 'dropin', 'receipt'])
def test_upgrade_precheck_verifies_pinned_digest_and_exact_receipt(monkeypatch, tamper):
    from deploy import precheck
    original = 'fixture-approved-dropin\n'
    digest = hashlib.sha256(original.encode()).hexdigest()
    monkeypatch.setattr(precheck, 'UPGRADE_DIGEST', digest)
    receipt = f'{digest}  {precheck.MANAGED_DROPIN}\n'
    def read(path):
        if Path(path) == precheck.MANAGED_DROPIN:
            return original if tamper != 'dropin' else original + '# fixture-edit\n'
        return receipt if tamper != 'receipt' else 'fixture-invalid-receipt\n'
    monkeypatch.setattr(precheck, 'private_unit_read', read)
    assert precheck.upgrade_source_verified() is (tamper == 'none')


@pytest.mark.parametrize('other_override', [None, 'ExecStart', 'KINVEST_SERVICE_TOKEN_FILE', 'LoadCredential', 'quoted-LoadCredential'])
def test_upgrade_precheck_allows_only_verified_managed_dropin(monkeypatch, capsys, other_override):
    from deploy import precheck
    props = {name: '' for name in precheck.PROPERTY_NAMES}
    props.update({'User': 'ubuntu', 'FragmentPath': '/etc/systemd/system/toss-mcp.service',
        'WorkingDirectory': '/home/ubuntu/toss-mcp-releases/' + FROM,
        'ActiveState': 'active', 'MainPID': '123', 'ProtectSystem': 'strict',
        'ProtectHome': 'no', 'PrivateTmp': 'no',
        'DropInPaths': str(precheck.MANAGED_DROPIN) + ' /fixture/10-other.conf'})
    runtime = b'MCP_AUTH_TOKEN=fixture\0MCP_OAUTH_CLIENT_ID=fixture\0MCP_OAUTH_CLIENT_SECRET=fixture\0KINVEST_CACHE_DIR=/tmp\0MCP_PUBLIC_URL=https://134.185.127.209.sslip.io\0'
    monkeypatch.setattr(precheck, 'unit_properties', lambda: props)
    monkeypatch.setattr(precheck, 'upgrade_source_verified', lambda: True)
    monkeypatch.setattr(Path, 'read_bytes', lambda path: runtime)
    monkeypatch.setattr(Path, 'exists', lambda path: path in (Path('/'), precheck.STATE / 'oauth.sqlite3'))
    monkeypatch.setattr(Path, 'is_file', lambda path: path == precheck.STATE / 'oauth.sqlite3')
    monkeypatch.setattr(Path, 'is_symlink', lambda path: False)
    monkeypatch.setattr(precheck.os, 'statvfs', lambda path: SimpleNamespace(f_flag=0))
    monkeypatch.setattr(precheck.os, 'access', lambda *args: True)
    monkeypatch.setattr(precheck.subprocess, 'check_output', lambda *args, **kwargs: '600:0:0:regular file\n')
    def read(path):
        assert Path(path) != precheck.MANAGED_DROPIN  # Already verified; skip it.
        directives = {'ExecStart': 'ExecStart=/fixture/override',
            'KINVEST_SERVICE_TOKEN_FILE': 'Environment=KINVEST_SERVICE_TOKEN_FILE=/fixture/override',
            'LoadCredential': 'LoadCredential=invest-desk-token:/fixture/override',
            'quoted-LoadCredential': 'LoadCredential="invest-desk-token:/fixture/override"'}
        return '[Service]\n' + directives.get(other_override, 'Restart=always') + '\n'
    monkeypatch.setattr(precheck, 'private_unit_read', read)
    assert precheck.main('b' * 40, 'upgrade') == (1 if other_override else 0)
    output = capsys.readouterr().out
    assert 'fixture/override' not in output
    if other_override:
        assert f'"{other_override.removeprefix("quoted-")}"' in output
