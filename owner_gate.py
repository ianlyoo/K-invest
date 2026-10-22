"""Shared owner consent helpers. Canonical owner_gate.py; keep copies byte-identical.

No configuration IO at import. CLI hash reads hidden terminal input or piped stdin.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import warnings
from urllib.parse import parse_qs, urlsplit

from starlette.responses import HTMLResponse, JSONResponse

SAFE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}
AUTH_FIELDS = frozenset({"client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method", "state", "scope", "resource"})
DEFAULT_REDIRECT_ORIGINS = ("https://chatgpt.com", "https://claude.ai", "https://claude.com")


class GateError(Exception):
    def __init__(self, code="access_denied", status=400, retry_after_s=None):
        super().__init__(code)
        self.code, self.status, self.retry_after_s = code, status, retry_after_s


def gate_failure(error):
    headers = dict(SAFE_HEADERS)
    if error.retry_after_s:
        headers["Retry-After"] = str(error.retry_after_s)
    return JSONResponse({"error": error.code}, status_code=error.status, headers=headers)


def load_credential(name):
    """Only the named private, regular systemd credential; no home/env fallback."""
    try:
        root = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not os.path.isabs(root) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            return None
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_uid not in (0, os.geteuid()) or info.st_size > 4096):
                return None
            value = handle.read(4097).decode("ascii")
        # One line, optionally followed by a single newline. Never include it in an error.
        value = value.removesuffix("\n")
        return value if value and not any(c in value for c in "\r\n\x00") else None
    except (OSError, ValueError, UnicodeError):
        return None


def _parse_hash(line):
    try:
        kind, n, r, p, salt, expected = line.split("$")
        if kind != "scrypt" or n not in {"16384", "32768", "65536"} or r != "8" or p != "1":
            return None
        salt_bytes = base64.b64decode(salt, validate=True)
        expected_bytes = base64.b64decode(expected, validate=True)
        if not 16 <= len(salt_bytes) <= 32 or len(expected_bytes) != 32:
            return None
        return int(n), salt_bytes, expected_bytes
    except (ValueError, TypeError, AttributeError):
        return None


def hash_passphrase(passphrase):
    if not isinstance(passphrase, str) or not 1 <= len(passphrase.encode()) <= 4096:
        raise GateError("invalid_passphrase")
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(passphrase.encode(), salt=salt, n=16384, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(hashed).decode()


class OwnerGate:
    """Global single-owner lockout per serving process; credentials reload on restart."""
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._hash = None
        self._lock = threading.Lock()
        self._failures, self._locked_until = 0, 0.0

    def configured(self):
        with self._lock:
            if self._hash is None:
                self._hash = _parse_hash(load_credential("owner-gate"))
            if self._hash is None:
                raise GateError("owner_gate_not_configured", 503)

    def signing_key(self):
        self.configured()
        return hashlib.sha256(self._hash[1] + self._hash[2] + b"owner-consent/v1").digest()

    def verify(self, passphrase):
        self.configured()
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                raise GateError("owner_gate_locked", 429, max(1, int(self._locked_until - now + .999)))
            if self._locked_until:
                self._failures, self._locked_until = 0, 0.0
            n, salt, expected = self._hash
            value = passphrase.encode() if isinstance(passphrase, str) else b""
            valid = 0 < len(value) <= 4096
            if valid:
                actual = hashlib.scrypt(value, salt=salt, n=n, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
                valid = hmac.compare_digest(actual, expected)
            if valid:
                self._failures = 0
                return
            self._failures += 1
            if self._failures >= 5:
                self._locked_until = now + 900
                raise GateError("owner_gate_locked", 429, 900)
            raise GateError()


_GATE = OwnerGate()


def owner_gate():
    return _GATE


def redirect_allowed(uri):
    """Exact HTTPS origins only. An explicit malformed override fails closed."""
    try:
        configured = os.environ.get("OWNER_GATE_REDIRECT_ORIGINS")
        origins = json.loads(configured) if configured is not None else DEFAULT_REDIRECT_ORIGINS
        if not isinstance(origins, (list, tuple)) or not 1 <= len(origins) <= 10:
            return False
        allowed = set()
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                return False
            allowed.add(parsed.hostname.lower())
        parsed = urlsplit(uri)
        return bool(parsed.scheme == "https" and parsed.hostname and parsed.hostname.lower() in allowed
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
                    and len(uri) <= 2048 and not any(ord(c) < 33 for c in uri) and "\\" not in uri)
    except (ValueError, TypeError, AttributeError):
        return False


async def fields_for(request):
    if request.method == "GET":
        body = request.scope.get("query_string", b"")
        limit = 8192
    else:
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise GateError("invalid_request")
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 16384 - len(chunks):
                raise GateError("invalid_request")
            chunks.extend(chunk)
        body, limit = bytes(chunks), 16384
    try:
        if len(body) > limit:
            raise ValueError
        # RFC 6749 §3.1: GET ignores unknown parameters (e.g. OIDC nonce/prompt); OAuth fields stay single-valued.
        get = request.method == "GET"
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=64 if get else 16)
        if any(len(values) != 1 for name, values in parsed.items() if name in AUTH_FIELDS or not get):
            raise ValueError
        return {name: values[0] for name, values in parsed.items()}
    except (ValueError, UnicodeError):
        raise GateError("invalid_request") from None


def consent_page(client_name, redirect_uri, fields, action):
    if not redirect_allowed(redirect_uri):
        raise GateError("invalid_request")
    def escape(value):
        return html.escape(str(value), quote=True)
    redirect = urlsplit(redirect_uri)
    host = redirect.hostname
    headers = {**SAFE_HEADERS, "Referrer-Policy": "same-origin",
               "Content-Security-Policy": "default-src 'none'; form-action 'self' https://" + redirect.netloc + "; frame-ancestors 'none'; base-uri 'none'"}
    hidden = "".join('<input type="hidden" name="' + escape(name) + '" value="' + escape(value) + '">' for name, value in fields.items())
    return HTMLResponse('<!doctype html><html><head><title>Approve access</title></head><body><h1>Approve access</h1>'
        '<p>Client: ' + escape(client_name or "OAuth client") + '</p><p>Redirect host: ' + escape(host) + '</p>'
        '<form method="post" action="' + escape(action) + '">' + hidden +
        '<label>Owner passphrase <input type="password" name="approval_secret" maxlength="1024" autocomplete="current-password" required></label>'
        '<button type="submit">Approve access</button></form></body></html>', headers=headers)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) != ["hash"]:
        raise SystemExit("Usage: python -m owner_gate hash")
    try:
        if sys.stdin.isatty():
            warnings.simplefilter("error", getpass.GetPassWarning)
            passphrase = getpass.getpass("Owner passphrase: ")
        else:
            passphrase = sys.stdin.buffer.readline(4098).decode("utf-8").removesuffix("\n")
        print(hash_passphrase(passphrase))
    except (GateError, UnicodeError, EOFError, getpass.GetPassWarning):
        raise SystemExit("Unable to read owner passphrase safely") from None


if __name__ == "__main__":
    main()
