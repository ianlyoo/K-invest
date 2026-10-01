#!/usr/bin/env bash
# Prepared procedure only: each deploy/rollback requires exact owner approval.
set -euo pipefail
set -o noclobber
umask 077
fail() { printf '%s\n' "$1" >&2; exit 1; }
mode=${1:?Usage: release.sh deploy|rollback EXACT_SHA}
rev=${2:?Exact approved revision required}
[[ "$rev" =~ ^[0-9a-f]{40}$ ]] || fail 'Invalid revision'
release=/home/ubuntu/toss-mcp-releases/$rev
backup=/var/lib/toss-mcp/rollback-$rev
dropdir=/etc/systemd/system/toss-mcp.service.d
dropin=$dropdir/90-durable-oauth.conf
unit=/etc/systemd/system/toss-mcp.service
state=/var/lib/toss-mcp/oauth

case "$mode" in
  deploy)
    procedure=$(realpath "$0")
    cd "$(dirname "$procedure")/.."
    actual_rev=$(git rev-parse HEAD)
    actual_branch=$(git branch --show-current)
    checkout_status=$(git status --porcelain)
    [[ "$actual_rev" == "$rev" ]] || fail 'HEAD is not approved revision'
    [[ "$actual_branch" == fix/oauth-durable-health ]] || fail 'Wrong branch'
    [[ -z "$checkout_status" ]] || fail 'Checkout must be clean'
    git diff --check
    ssh hermes python3 - "$rev" < deploy/precheck.py
    # Exclusive release creation; any failed archive/upload stops activation.
    git archive --format=tar "$rev" | ssh hermes bash -c "'set -euo pipefail; set -o noclobber; umask 077; r=/home/ubuntu/toss-mcp-releases/$rev; test ! -e \"\$r\"; test ! -L \"\$r\"; install -d -m 0750 /home/ubuntu/toss-mcp-releases; mkdir -m 0750 \"\$r\"; cat > \"\$r/release.tar\"; tar -xf \"\$r/release.tar\" -C \"\$r\"'"
    ssh hermes bash -s -- activate "$rev" < deploy/release.sh
    ;;
  rollback)
    ssh hermes bash -s -- revert "$rev" < "$(realpath "$0")"
    ;;
  activate)
    [[ "$(id -un)" == ubuntu ]] || fail 'Must run as ubuntu'
    [[ -d "$release" && ! -L "$release" && -f "$release/requirements-validated.txt" ]] || fail 'Release missing'
    sudo test -f "$unit" || fail 'Original unit missing'
    sudo test ! -L "$unit" || fail 'Unit symlink needs review'
    sudo test ! -e "$backup" || fail 'Rollback backup already exists'
    sudo test ! -L "$backup" || fail 'Rollback backup symlink'
    sudo test ! -e "$dropin" || fail 'Drop-in already exists'
    sudo test ! -L "$dropin" || fail 'Drop-in symlink'
    sudo test ! -L "$dropdir" || fail 'Drop-in directory symlink'
    [[ ! -e "$release/.venv" ]] || fail 'Release environment already exists'
    python3 -m venv "$release/.venv"
    cd "$release"
    # Never source an env file. Scrub inherited broker/OAuth configuration for
    # validation, including the isolated child interpreter restart test.
    unset MCP_AUTH_TOKEN MCP_OAUTH_CLIENT_ID MCP_OAUTH_CLIENT_SECRET
    unset TOSS_CLIENT_ID TOSS_CLIENT_SECRET TOSS_CREDS_FILE TOSS_ACCOUNTS_JSON
    unset KIS_APP_KEY KIS_APP_SECRET KIS_CANO KIS_ENV_FILE KIS_ACCOUNTS_JSON
    unset MCP_OAUTH_TOKEN_TTL MCP_OAUTH_REFRESH_TOKEN_TTL MCP_OAUTH_REFRESH_GRACE_SECONDS
    .venv/bin/python -m pip install --no-cache-dir -r requirements-validated.txt
    .venv/bin/ruff check .
    .venv/bin/python -m pytest -q
    # No unit, state-directory or backup changes happen before validation passes.
    for directory in /var/lib/toss-mcp "$state"; do
      if sudo test -e "$directory"; then
        sudo test -d "$directory" || fail 'Invalid state directory'
        sudo test ! -L "$directory" || fail 'State directory symlink'
        [[ "$(sudo stat -c '%a:%U:%G' "$directory")" == 700:ubuntu:ubuntu ]] || fail 'State permissions need review'
      else
        sudo mkdir -m 0700 "$directory"
        sudo chown ubuntu:ubuntu "$directory"
      fi
    done
    # mkdir reserves a unique root-private backup atomically. cp -aT copies only
    # into this new directory; no existing backup can ever be replaced.
    sudo mkdir -m 0700 "$backup"
    sudo cp -aT "$unit" "$backup/toss-mcp.service"
    if sudo test -d "$dropdir"; then
      sudo cp -aT "$dropdir" "$backup/original-dropins"
    else
      sudo mkdir -m 0755 "$dropdir"
    fi
    sudo bash -c 'set -euo pipefail; set -o noclobber; umask 077; cat > "$1"; chmod 0644 "$1"' -- "$dropin" <<EOF
[Service]
WorkingDirectory=$release
ExecStart=
ExecStart=$release/.venv/bin/python $release/server.py
Environment=KINVEST_OAUTH_STATE_DIR=$state
UMask=0077
ReadWritePaths=/var/lib/toss-mcp/oauth
EOF
    sudo sha256sum "$dropin" | sudo bash -c 'set -euo pipefail; set -o noclobber; umask 077; cat > "$1"' -- "$backup/installed-dropin.sha256"
    if ! sudo systemctl daemon-reload || ! sudo systemctl restart toss-mcp.service ||
       ! "$release/.venv/bin/python" "$release/deploy/readiness.py" "$rev"; then
      printf '%s\n' 'Startup failed: active plus authenticated-store-ok not reached; attempting guarded rollback' >&2
      if ! bash "$release/deploy/release.sh" revert "$rev"; then
        fail 'Startup failed and guarded rollback failed; stop for owner review'
      fi
      fail 'Startup validation failed; guarded rollback completed'
    fi
    systemctl show toss-mcp.service --property=ActiveState,SubState,ExecMainStartTimestamp,NRestarts
    ;;
  revert)
    [[ "$(id -un)" == ubuntu ]] || fail 'Must run as ubuntu'
    sudo test -d "$backup" || fail 'Rollback backup missing'
    sudo test ! -L "$backup" || fail 'Rollback backup symlink'
    sudo test -f "$backup/installed-dropin.sha256" || fail 'Installed fingerprint missing'
    sudo test -f "$dropin" || fail 'Installed drop-in missing'
    sudo test ! -L "$dropin" || fail 'Installed drop-in symlink'
    sudo sha256sum --status -c "$backup/installed-dropin.sha256" || fail 'Drop-in changed; review required'
    # Hard-link with no force is exclusive; unlink only after that succeeds.
    sudo ln "$dropin" "$backup/removed-dropin.conf"
    sudo rm "$dropin"
    sudo systemctl daemon-reload
    sudo systemctl restart toss-mcp.service
    systemctl is-active --quiet toss-mcp.service
    systemctl show toss-mcp.service --property=ActiveState,SubState,ExecMainStartTimestamp,NRestarts
    ;;
  *) fail 'Unknown procedure mode' ;;
esac
