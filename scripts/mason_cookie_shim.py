"""Observe MyFitnessPal cookie rotation inside Mason's process (diagnostic).

Hypothesis this is here to prove or disprove: MFP rotates `remember_me`,
`_mfp_session` and possibly `__Secure-next-auth.session-token` through
Set-Cookie on legacy endpoints (e.g. /user/auth_token?refresh=true). Mason's
curl_cffi session keeps the rotated values in memory only; fresh processes then
send stale copies from cookies.json and MFP signs the session out.

install() wraps curl_cffi.requests.Session.request (every Mason request goes
through it, including the ones python-myfitnesspal makes while the client is
being built). For *.myfitnesspal.com responses it compares the watched cookies
in the jar before and after, and appends one line per change to
$MFP_LOG_DIR/cookie-rotation.log (0600): time, pid, path, status, name,
set/changed/cleared, and sha256[:8] of old and new. Never the values.

MFP_PERSIST_ROTATIONS=1 additionally merges the jar's MFP cookies (minus
__Host-*) into Mason's cookies.json via auth.save_cookies, under a file lock,
when a session cookie rotates to a new non-empty value. A response that CLEARS
one of them is never persisted. Off by default.

The shim only observes requests; any error inside it is logged by type and
swallowed, so Mason keeps working.
"""

import datetime
import hashlib
import os
from urllib.parse import urlsplit

WATCH = ("__Secure-next-auth.session-token", "remember_me", "_mfp_session",
         "p", "last_login_date")
PERSIST = ("__Secure-next-auth.session-token", "remember_me", "_mfp_session")
MARK = "_mfp_mason_shim"


def _log_path():
    base = os.environ.get("MFP_LOG_DIR") or os.path.expanduser("~/Library/Logs/mfp-mason")
    return os.path.join(base, "cookie-rotation.log")


def log(line):
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(path), 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            fh.write(f"{stamp} pid={os.getpid()} {line}\n")
    except Exception:
        pass


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "-"


def _is_mfp(url):
    try:
        host = urlsplit(str(url)).hostname or ""
    except Exception:
        return False
    return host == "myfitnesspal.com" or host.endswith(".myfitnesspal.com")


def _mfp_cookies(session, names=None):
    out = {}
    for c in session.cookies.jar:
        if "myfitnesspal.com" not in (c.domain or ""):
            continue
        if names is not None and c.name not in names:
            continue
        out[c.name] = c.value or ""
    return out


def _persist(session):
    import fcntl

    from myfitnesspal_mcp import auth

    try:
        from myfitnesspal_mcp import config

        target = str(config.cookies_path())
    except Exception:
        target = os.path.expanduser(
            "~/Library/Application Support/myfitnesspal-mcp/cookies.json")
    with open(target + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            merged = {k: v for k, v in (auth.load_cookies() or {}).items()
                      if not k.startswith("__Host-")}
            merged.update({k: v for k, v in _mfp_cookies(session).items()
                           if v and not k.startswith("__Host-")})
            auth.save_cookies(merged, username=auth.saved_username())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    log(f"persisted rotation ({len(merged)} cookies)")


def _after(session, url, response, before):
    after = _mfp_cookies(session, WATCH)
    path = urlsplit(str(url)).path or "/"
    status = getattr(response, "status_code", "?")
    rotated = cleared = False
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, ""), after.get(name, "")
        if old == new:
            continue
        kind = "set" if not old else ("cleared" if not new else "changed")
        log(f"{path} status={status} {name} {kind} {_hash(old)}->{_hash(new)}")
        if name in PERSIST:
            if kind == "cleared":
                cleared = True
            else:
                rotated = True
    if os.environ.get("MFP_PERSIST_ROTATIONS") == "1":
        if cleared:
            log("cleared by MFP: not persisted")
        elif rotated:
            _persist(session)


def _error(exc):
    log(f"shim error: {type(exc).__name__}")


def install():
    try:
        from curl_cffi import requests as cffi_requests

        session_cls = cffi_requests.Session
        original = session_cls.request
        if getattr(original, MARK, False):
            return

        def request(self, method=None, url=None, *args, **kwargs):
            before = None
            if _is_mfp(url):
                try:
                    before = _mfp_cookies(self, WATCH)
                except Exception as exc:
                    _error(exc)
            response = original(self, method, url, *args, **kwargs)
            if before is not None:
                try:
                    _after(self, url, response, before)
                except Exception as exc:
                    _error(exc)
            return response

        setattr(request, MARK, True)
        session_cls.request = request
        log("shim active")
    except Exception as exc:
        log(f"shim inactive: {type(exc).__name__}")
