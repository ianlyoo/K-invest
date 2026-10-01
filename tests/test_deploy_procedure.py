"""Execute the prepared procedure only against temporary paths and fake commands."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/release.sh"
REV = "a" * 40


def synthetic_host(tmp_path, fail_stage=None):
    home = tmp_path / "home"
    var = tmp_path / "var"
    etc = tmp_path / "etc"
    script = tmp_path / "procedure.sh"
    text = SCRIPT.read_text().replace("/home/ubuntu", str(home)).replace("/var/lib", str(var)).replace("/etc/systemd/system", str(etc))
    script.write_text(text)
    release = home / "toss-mcp-releases" / REV
    release.mkdir(parents=True)
    (release / "requirements-validated.txt").write_text("fixture-only\n")
    (release / "deploy").mkdir()
    (release / "deploy/release.sh").write_text(text)
    unit = etc / "toss-mcp.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("fixture-original-unit\n")
    var.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "activation.log"
    def command(name, body):
        file = commands / name
        file.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
        file.chmod(0o755)
    command("sudo", 'exec "$@"\n')
    command("id", 'printf "ubuntu\\n"\n')
    command("stat", 'printf "700:ubuntu:ubuntu\\n"\n')
    command("chown", 'exit 0\n')
    command("systemctl", 'printf "%s\\n" "$*" >> "$FIXTURE_LOG"\n')
    command("ssh", 'printf "UNEXPECTED_SSH\\n" >> "$FIXTURE_LOG"; exit 99\n')
    command("python3", '''mkdir -p "$3/bin"
cat > "$3/bin/python" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$2" == "$FIXTURE_FAIL" || "$1" == */readiness.py && "$FIXTURE_FAIL" == readiness ]]; then exit 23; fi
exit 0
EOF
cat > "$3/bin/ruff" <<'EOF'
#!/bin/bash
set -euo pipefail
[[ "$FIXTURE_FAIL" != ruff ]]
EOF
chmod +x "$3/bin/python" "$3/bin/ruff"
''')
    env = {**os.environ, "PATH": str(commands) + ":/usr/bin:/bin", "FIXTURE_FAIL": fail_stage or "none", "FIXTURE_LOG": str(log)}
    return SimpleHost(script, release, var / f"toss-mcp/rollback-{REV}", etc / "toss-mcp.service.d/90-durable-oauth.conf", unit, log, env)


class SimpleHost:
    def __init__(self, script, release, backup, dropin, unit, log, env):
        self.script, self.release, self.backup, self.dropin, self.unit, self.log, self.env = script, release, backup, dropin, unit, log, env

    def run(self, mode="activate"):
        return subprocess.run(["bash", str(self.script), mode, REV], capture_output=True, text=True, env=self.env)


def test_shell_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.parametrize("stage", ["pip", "ruff", "pytest"])
def test_failed_validation_never_changes_unit_or_restarts(tmp_path, stage):
    host = synthetic_host(tmp_path, stage)
    result = host.run()
    assert result.returncode != 0
    assert not host.dropin.exists() and not host.backup.exists() and not host.log.exists()
    assert host.unit.read_text() == "fixture-original-unit\n"


@pytest.mark.parametrize("existing", ["backup", "dropin"])
def test_existing_rollback_or_dropin_is_preserved_and_stops(tmp_path, existing):
    host = synthetic_host(tmp_path)
    path = getattr(host, existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture-owner-work\n")
    assert host.run().returncode != 0
    assert path.read_text() == "fixture-owner-work\n"
    assert not (host.release / ".venv").exists() and not host.log.exists()


def test_exclusive_dropin_creation_handles_race(tmp_path):
    host = synthetic_host(tmp_path)
    # Race between the initial guard and exclusive write, injected by sudo stub.
    sudo = Path(host.env["PATH"].split(":")[0]) / "sudo"
    sudo.write_text('#!/bin/bash\nset -euo pipefail\nif [[ "$1" == bash && "$5" == *90-durable-oauth.conf ]]; then printf "fixture-racer\\n" > "$5"; fi\nexec "$@"\n')
    result = host.run()
    assert result.returncode != 0
    assert host.dropin.read_text() == "fixture-racer\n"
    assert not host.log.exists()


def test_activation_and_guarded_rollback_preserve_other_dropins(tmp_path):
    host = synthetic_host(tmp_path)
    host.dropin.parent.mkdir()
    other = host.dropin.parent / "10-owner.conf"
    other.write_text("fixture-other\n")
    assert host.run().returncode == 0
    assert (host.backup / "toss-mcp.service").read_text() == "fixture-original-unit\n"
    assert (host.backup / "original-dropins/10-owner.conf").read_text() == "fixture-other\n"
    original = host.dropin.read_text()
    host.dropin.write_text(original + "# owner edit\n")
    assert host.run("revert").returncode != 0
    assert host.dropin.read_text().endswith("# owner edit\n")
    host.dropin.write_text(original)
    assert host.run("revert").returncode == 0
    assert not host.dropin.exists() and other.read_text() == "fixture-other\n"
    assert (host.backup / "removed-dropin.conf").read_text() == original
    assert host.backup.exists() and host.release.exists()


def test_failed_git_status_stops_before_upload_even_with_empty_output(tmp_path):
    host = synthetic_host(tmp_path)
    git = Path(host.env["PATH"].split(":")[0]) / "git"
    git.write_text(f'''#!/bin/bash
set -euo pipefail
case "$1" in
rev-parse) printf '%s\\n' '{REV}' ;;
branch) printf '%s\\n' fix/oauth-durable-health ;;
status) exit 23 ;;
*) exit 0 ;;
esac
''')
    git.chmod(0o755)
    assert host.run("deploy").returncode == 23
    assert not host.log.exists()


def test_dropin_adds_only_the_state_directory_sandbox_exception(tmp_path):
    host = synthetic_host(tmp_path)
    assert host.run().returncode == 0
    dropin = host.dropin.read_text()
    state = host.backup.parent / 'oauth'
    assert f'ReadWritePaths={state}\n' in dropin
    assert dropin.count('ReadWritePaths=') == 1
    assert f'Environment=KINVEST_OAUTH_STATE_DIR={state}\n' in dropin
    assert 'UMask=0077\n' in dropin
    for field in ('ProtectSystem=', 'ProtectHome=', 'ReadOnlyPaths=', 'InaccessiblePaths=', 'PrivateTmp='):
        assert field not in dropin


def test_failed_authenticated_readiness_automatically_uses_guarded_rollback(tmp_path):
    host = synthetic_host(tmp_path, 'readiness')
    result = host.run()
    assert result.returncode != 0
    assert 'active plus authenticated-store-ok not reached' in result.stderr
    assert 'guarded rollback completed' in result.stderr
    assert not host.dropin.exists()
    assert (host.backup / 'removed-dropin.conf').exists()
    assert (host.backup / 'installed-dropin.sha256').exists()
    assert host.unit.read_text() == 'fixture-original-unit\n'
    assert host.log.read_text().count('restart toss-mcp.service') == 2
    assert host.release.exists()
