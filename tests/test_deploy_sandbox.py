"""Synthetic effective-systemd sandbox checks, including SQLite sidecar files."""
from __future__ import annotations

from pathlib import Path

import pytest

from deploy.precheck import SANDBOX_NAMES, sandbox_failures, unit_properties

STATE = Path('/var/lib/toss-mcp/oauth')


def properties(**overrides):
    return {'ProtectSystem': 'strict', 'ProtectHome': 'yes', 'PrivateTmp': 'yes',
            'ReadOnlyPaths': '/', 'InaccessiblePaths': '/root',
            'ReadWritePaths': '/home/ubuntu/toss-mcp', **overrides}


def test_strict_sandbox_allows_only_new_state_directory_and_all_sidecars():
    before = properties()
    assert sandbox_failures(before) == []
    assert before['ReadWritePaths'] == '/home/ubuntu/toss-mcp'
    assert before['ProtectSystem'] == 'strict'


@pytest.mark.parametrize('path', ['/var', '/var/lib/toss-mcp', str(STATE), str(STATE/'oauth.sqlite3'), str(STATE/'oauth.sqlite3-journal')])
def test_inaccessible_paths_cannot_be_reopened_by_readwrite_exception(path):
    assert 'InaccessiblePaths' in sandbox_failures(properties(InaccessiblePaths=path))


@pytest.mark.parametrize('name', ['oauth.sqlite3', 'oauth.sqlite3-journal', 'oauth.sqlite3-wal', 'oauth.sqlite3-shm'])
def test_readonly_file_inside_write_exception_blocks_state_or_journal(name):
    assert 'ReadOnlyPaths' in sandbox_failures(properties(ReadOnlyPaths=str(STATE/name)))


def test_more_specific_write_exception_overrides_readonly_parent():
    assert sandbox_failures(properties(ReadOnlyPaths='/var/lib/toss-mcp')) == []
    assert sandbox_failures(properties(ReadOnlyPaths=str(STATE))) == ['ReadOnlyPaths']


def test_sibling_paths_are_not_ancestors():
    assert sandbox_failures(properties(InaccessiblePaths=str(STATE)+'-other')) == []


@pytest.mark.parametrize('private_tmp', ['no', 'yes', 'disconnected'])
def test_private_tmp_has_no_effect_on_var_lib_state(private_tmp):
    assert sandbox_failures(properties(PrivateTmp=private_tmp)) == []


@pytest.mark.parametrize('mode', ['yes', 'read-only', 'tmpfs'])
def test_protect_home_cannot_hide_state_under_home(mode):
    result = sandbox_failures(properties(ProtectHome=mode), Path('/home/ubuntu/oauth'))
    if mode == 'read-only':
        assert result == []  # New more-specific directory exception is writable.
    else:
        assert 'InaccessiblePaths' in result


def test_prefixed_effective_paths_and_unknown_settings_fail_closed():
    assert sandbox_failures(properties(ReadOnlyPaths='-/var', ReadWritePaths='-/home/ubuntu/toss-mcp')) == []
    assert sandbox_failures(properties(ProtectSystem='unknown')) == ['ProtectSystem']
    with pytest.raises(ValueError, match='UnsupportedSandboxPath'):
        sandbox_failures(properties(ReadOnlyPaths='relative'))
    assert sandbox_failures(properties(RootDirectory='/fixture')) == ['RootDirectory']


def test_effective_unit_query_reads_all_required_sandbox_properties(monkeypatch):
    from deploy import precheck
    seen = []
    def output(command, **kwargs):
        seen.extend(command)
        return '\n'.join(name+'='+properties().get(name, '') for name in precheck.PROPERTY_NAMES)
    monkeypatch.setattr(precheck.subprocess, 'check_output', output)
    result = unit_properties()
    assert all(name in result and name in ' '.join(seen) for name in SANDBOX_NAMES)


def test_precheck_error_output_keeps_class_and_operation_but_no_values(monkeypatch, capsys):
    from deploy import precheck
    def fail():
        raise PermissionError('fixture-sensitive-filename')
    monkeypatch.setattr(precheck, 'unit_properties', fail)
    assert precheck.main('a'*40) == 1
    output = capsys.readouterr().out
    assert 'PermissionError' in output and 'systemctl_properties' in output
    assert 'fixture-sensitive' not in output
