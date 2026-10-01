"""Read-only deployment precheck. Only names/booleans are emitted, never values.

Path precedence follows systemd's systemd.exec documentation:
https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

STATE = Path('/var/lib/toss-mcp/oauth')
SANDBOX_NAMES = ('ProtectSystem', 'ProtectHome', 'ReadOnlyPaths', 'InaccessiblePaths', 'ReadWritePaths', 'PrivateTmp')
PROPERTY_NAMES = SANDBOX_NAMES + ('User', 'FragmentPath', 'WorkingDirectory', 'ActiveState',
    'MainPID', 'DropInPaths', 'EnvironmentFiles', 'RootDirectory', 'RootImage', 'BindReadOnlyPaths', 'TemporaryFileSystem')


def unit_properties():
    raw = subprocess.check_output(['systemctl', 'show', 'toss-mcp.service',
        '--property=' + ','.join(PROPERTY_NAMES)], text=True, stderr=subprocess.DEVNULL)
    properties = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if any(name not in properties for name in PROPERTY_NAMES):
        raise ValueError('UnitPropertyUnavailable')
    return properties


def _paths(value):
    result = []
    for raw in shlex.split(value):
        path = Path(raw.lstrip('-+'))
        if not path.is_absolute() or '%' in raw:
            raise ValueError('UnsupportedSandboxPath')
        result.append(path.resolve())
    return result


def _covers(parent, child):
    return parent == child or parent in child.parents


def sandbox_failures(properties, state=STATE):
    """Predict the new drop-in's access, including create/unlink of SQLite sidecars.

    More specific writable paths can override read-only ancestors. Inaccessible
    ancestors cannot be reopened. Equal read-only/write exceptions fail closed.
    Existing write exceptions remain: the new drop-in appends only STATE.
    """
    failures = []
    for name, allowed in [('ProtectSystem', {'no', 'yes', 'full', 'strict'}),
                          ('ProtectHome', {'no', 'yes', 'read-only', 'tmpfs'}),
                          ('PrivateTmp', {'no', 'yes', 'disconnected'})]:
        if properties.get(name) not in allowed:
            failures.append(name)
    if failures:
        return failures
    # Unknown remapping could hide the proposed host path: do not guess.
    for name in ('RootDirectory', 'RootImage', 'BindReadOnlyPaths', 'TemporaryFileSystem'):
        if properties.get(name):
            failures.append(name)
    state = state.resolve()
    readonly = _paths(properties.get('ReadOnlyPaths', ''))
    inaccessible = _paths(properties.get('InaccessiblePaths', ''))
    writable = _paths(properties.get('ReadWritePaths', '')) + [state]
    if properties['ProtectSystem'] == 'strict':
        readonly.append(Path('/'))
    elif properties['ProtectSystem'] in {'yes', 'full'}:
        readonly.extend(map(Path, ('/usr', '/boot', '/efi')))
        if properties['ProtectSystem'] == 'full':
            readonly.append(Path('/etc'))
    homes = list(map(Path, ('/home', '/root', '/run/user')))
    if properties['ProtectHome'] in {'yes', 'tmpfs'}:
        inaccessible.extend(homes)
    elif properties['ProtectHome'] == 'read-only':
        readonly.extend(homes)
    if properties['PrivateTmp'] != 'no' and any(_covers(Path(p), state) for p in ('/tmp', '/var/tmp')):
        failures.append('PrivateTmp')
    targets = [state] + [state / name for name in ('oauth.sqlite3', 'oauth.sqlite3-journal', 'oauth.sqlite3-wal', 'oauth.sqlite3-shm')]
    for target in targets:
        if any(_covers(path, target) for path in inaccessible):
            failures.append('InaccessiblePaths')
        ro = max((len(path.parts) for path in readonly if _covers(path, target)), default=-1)
        rw = max((len(path.parts) for path in writable if _covers(path, target)), default=-1)
        if ro >= rw:
            failures.append('ReadOnlyPaths')
    return sorted(set(failures))


def private_unit_read(path):
    return subprocess.check_output(['sudo', '-n', 'cat', str(path)], text=True, stderr=subprocess.DEVNULL)


def main(revision):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        print('precheck_failed: RevisionFormat')
        return 1
    REV = revision
    checks, conflicts, errors = {}, [], []
    stage = 'systemctl_properties'
    # Query once, retaining effective sandbox values privately.
    try:
        properties = unit_properties()
    except Exception as exc:
        print(json.dumps({'errors': [{'class': type(exc).__name__, 'message': stage}]}))
        return 1
    def prop(name):
        return properties[name].strip()
    try:
        old = Path(prop('WorkingDirectory'))
        new = Path('/home/ubuntu/toss-mcp-releases') / REV
        checks['service_identity'] = (prop('User') == 'ubuntu' and
            prop('FragmentPath') == '/etc/systemd/system/toss-mcp.service' and old == Path('/home/ubuntu/toss-mcp'))
        checks['currently_running'] = prop('ActiveState') == 'active' and int(prop('MainPID')) > 0
        stage = 'exclusive_target_metadata'
        targets = (new, Path('/var/lib/toss-mcp/rollback-'+REV),
                   Path('/etc/systemd/system/toss-mcp.service.d/90-durable-oauth.conf'))
        checks['exclusive_targets_available'] = all(not p.exists() and not p.is_symlink() for p in targets)
        stage = 'dropin_read_and_field_parse'
        sensitive_fields = {'KINVEST_OAUTH_STATE_DIR', 'MCP_OAUTH_TOKEN_TTL',
                            'MCP_OAUTH_REFRESH_TOKEN_TTL', 'MCP_OAUTH_REFRESH_GRACE_SECONDS'}
        direct = {'WorkingDirectory', 'ExecStart', 'UMask'}
        for file in shlex.split(prop('DropInPaths')):
            lines = private_unit_read(file).replace('\\\n', '').splitlines()
            section = ''
            for raw in lines:
                line = raw.strip()
                if not line or line.startswith(('#', ';')):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    section = line[1:-1]
                    continue
                if section != 'Service' or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key = key.strip()
                if key in direct:
                    conflicts.append(key)
                elif key == 'Environment':
                    for assignment in shlex.split(val):
                        name = assignment.partition('=')[0]
                        if name in sensitive_fields:
                            conflicts.append(name)
        checks['no_conflicting_dropins'] = not conflicts
        stage = 'environment_file_reference_metadata'
        raw_files = prop('EnvironmentFiles')
        files = re.findall(r'(\S+) \(ignore_errors=(yes|no)\)', raw_files)
        # No EnvironmentFile is valid when configuration is inherited.
        def manager_test(flag, path):
            return subprocess.run(['sudo', '-n', 'test', flag, str(path)], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        checks['environment_file_references'] = (not raw_files or bool(files)) and all(
            Path(f).is_absolute() and (manager_test('-f', f) and manager_test('-r', f) or optional == 'yes')
            for f, optional in files)
        checks['existing_dotenv_still_supplied'] = not (old / '.env').exists() or any(
            Path(f).resolve() == (old / '.env').resolve() and os.access(f, os.R_OK) for f, _ in files)
        stage = 'inherited_environment_presence'
        raw = (Path('/proc') / prop('MainPID') / 'environ').read_bytes()
        runtime = dict(item.split(b'=', 1) for item in raw.split(b'\0') if b'=' in item)
        def value(name):
            return runtime.get(name.encode(), b'').decode(errors='strict').strip()
        checks['oauth_configuration_inherited'] = all(bool(value(k)) for k in (
            'MCP_AUTH_TOKEN', 'MCP_OAUTH_CLIENT_ID', 'MCP_OAUTH_CLIENT_SECRET'))
        stage = 'credential_cache_reference_metadata'
        paths = ('TOSS_CREDS_FILE', 'KIS_ENV_FILE', 'TOSS_TOKEN_CACHE', 'KIS_TOKEN_CACHE',
                 'KINVEST_CACHE_DIR', 'KINVEST_PORTFOLIO_DB', 'MARGIN_TA_HOME')
        invalid_names = []
        for key in paths:
            raw = value(key)
            if not raw:
                continue
            p = Path(raw).expanduser()
            valid = p.is_absolute()
            if key in ('TOSS_CREDS_FILE', 'KIS_ENV_FILE'):
                valid = valid and p.is_file() and os.access(p, os.R_OK)
            else:
                parent = p if p.is_dir() else p.parent
                valid = valid and parent.is_dir() and os.access(parent, os.R_OK | os.W_OK | os.X_OK)
            if not valid:
                invalid_names.append(key)
        checks['credential_cache_references_resolve'] = not invalid_names
        checks['default_cache_stable'] = bool(value('KINVEST_CACHE_DIR')) or Path('/home/ubuntu/.cache').is_dir()
        checks['public_origin_matches_approved'] = value('MCP_PUBLIC_URL').rstrip('/') == 'https://134.185.127.209.sslip.io'
        stage = 'effective_unit_sandbox'
        failures = sandbox_failures(properties)
        checks['state_and_sqlite_journal_writable'] = not failures
        if failures:
            errors.append({'class': 'SandboxConflict', 'message': stage, 'names': failures})
        state = Path('/var/lib/toss-mcp/oauth')
        ancestor = state
        while not ancestor.exists():
            ancestor = ancestor.parent
        checks['state_backing_filesystem_writable'] = not bool(os.statvfs(ancestor).f_flag & os.ST_RDONLY)
        checks['existing_state_not_symlink'] = not state.is_symlink()
        checks['existing_state_writable'] = not state.exists() or os.access(state, os.W_OK | os.X_OK)
        for name in ('oauth.sqlite3', 'oauth.sqlite3-journal', 'oauth.sqlite3-wal', 'oauth.sqlite3-shm'):
            path = state / name
            checks['existing_' + name.replace('.', '_').replace('-', '_') + '_writable'] = not path.exists() or (not path.is_symlink() and os.access(path, os.R_OK | os.W_OK))
        if invalid_names:
            errors.append({'class': 'ReferenceCheckFailed', 'message': 'credential_cache_reference_metadata', 'names': invalid_names})
    except Exception as exc:
        # Never interpolate exception text: it can contain sensitive filenames/values.
        errors.append({'class': type(exc).__name__, 'message': stage})
        checks['private_precheck_completed'] = False
    print(json.dumps({'checks': checks, 'conflicting_fields': sorted(set(conflicts)), 'errors': errors}, sort_keys=True))
    passed = bool(checks) and all(checks.values()) and not errors
    print('private_precheck=' + ('pass' if passed else 'fail'))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) == 2 else ''))
