"""Read an authenticated MyFitnessPal session from a local Chromium browser (macOS).

For accounts that sign in via Google/Apple SSO there is no password, so the
native mobile OAuth flow (mfp-api) cannot be used. Instead we reuse the session
you already established by logging into myfitnesspal.com in Chrome.

Chromium on macOS stores cookies in a SQLite DB with each value encrypted
AES-128-CBC under a key derived from a per-browser password in the macOS
Keychain ("<Browser> Safe Storage"). The first read triggers a Keychain
permission prompt the user must approve — we never bypass it.

Only v10/v11 cookies are decryptable here. v20 "app-bound" cookies (newest
Chrome) require the browser process and are not supported; we skip them.

Security:
  * Cookie values are never logged or printed. Callers get a dict they pass
    straight into an HTTPS request; redact before surfacing anything.
  * The cookie DB is snapshotted read-only via the SQLite backup API.

Adapted from the approach used by AdamWalt/myfitnesspal-mcp-python.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

DOMAIN = "myfitnesspal.com"

# Chromium stores expires_utc as microseconds since 1601-01-01 (Windows epoch).
# Convert the current time into that epoch to compare.
_CHROMIUM_EPOCH_OFFSET_US = 11_644_473_600_000_000  # microseconds 1601->1970


def _now_chromium_us() -> int:
    return int(time.time() * 1_000_000) + _CHROMIUM_EPOCH_OFFSET_US


# Cookie DB paths are relative to ~/Library/Application Support/<browser-root>/.
# {profile} is substituted with the chosen profile directory ("Default",
# "Profile 1", ...). Newer Chromium moved the DB into a "Network/" subdir.
_CHROMIUM_BROWSER_ROOTS_MACOS: dict[str, str] = {
    "Arc": "Arc/User Data",
    "Chrome": "Google/Chrome",
    "Chromium": "Chromium",
    "Microsoft Edge": "Microsoft Edge",
    "Brave": "BraveSoftware/Brave-Browser",
    "Vivaldi": "Vivaldi",
    "Opera": "com.operasoftware.Opera",
}

# Markers that indicate an actually-authenticated MFP session (not pre-auth).
_SESSION_MARKERS = ("session-token", "_mfp_session")


class CookieExtractionError(RuntimeError):
    """Raised when no usable MyFitnessPal session can be extracted."""


def has_mfp_session(cookies: dict[str, str]) -> bool:
    """True if the cookie set carries an authenticated-session marker."""
    return any(
        ("session-token" in name or name == "_mfp_session") for name in cookies
    )


def _keychain_password(service_name: str) -> bytes | None:
    """Fetch a Safe Storage password from the macOS Keychain.

    Triggers the one-time Keychain authorization dialog the user must approve.
    Returns None if denied or absent.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, check=True, timeout=20,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _derive_aes_key(safe_storage_password: bytes) -> bytes:
    """Chromium macOS key: PBKDF2-HMAC-SHA1, salt='saltysalt', 1003 iters, 16 bytes."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt",
        iterations=1003, backend=default_backend(),
    )
    return kdf.derive(safe_storage_password)


def _decrypt_value(encrypted_value: bytes, aes_key: bytes, host_key: str = "") -> str | None:
    """Decrypt one Chromium cookie value. Returns None on failure or v20."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypted_value or len(encrypted_value) < 3:
        return None
    if encrypted_value[:3] not in (b"v10", b"v11"):
        return None  # v20 app-bound encryption is unsupported here
    try:
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16),
                        backend=default_backend())
        d = cipher.decryptor()
        plaintext = d.update(encrypted_value[3:]) + d.finalize()
    except Exception:
        return None
    if not plaintext:
        return None
    pad_len = plaintext[-1]
    if pad_len < 1 or pad_len > 16:
        return None
    plaintext = plaintext[:-pad_len]
    # Modern Chromium prepends SHA-256(host_key) as an integrity tag; strip it
    # only when it actually matches.
    if host_key and len(plaintext) >= 32:
        if plaintext[:32] == hashlib.sha256(host_key.encode("utf-8")).digest():
            plaintext = plaintext[32:]
    try:
        return plaintext.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _snapshot_db(src: Path, dst: str) -> None:
    """Copy a live (possibly WAL-mode) SQLite DB consistently, read-only."""
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_con = sqlite3.connect(dst)
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()


def _extract_from_db(cookies_db_path: Path, aes_key: bytes) -> tuple[dict[str, str], int]:
    """Return (live cookies, count of session cookies dropped as expired).

    Expired cookies are filtered out: a stale session-token read back would only
    fail later at the API with a confusing 401. We surface expiry explicitly.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".cookies.db")
    os.close(fd)
    try:
        _snapshot_db(cookies_db_path, tmp_path)
        con = sqlite3.connect(tmp_path)
        try:
            rows = con.execute(
                "SELECT name, value, encrypted_value, host_key, expires_utc "
                "FROM cookies WHERE host_key = ? OR host_key LIKE ?",
                (DOMAIN, f"%.{DOMAIN}"),
            ).fetchall()
        finally:
            con.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    now_us = _now_chromium_us()
    cookies: dict[str, str] = {}
    expired_session_cookies = 0
    for name, plain, enc, host_key, expires_utc in rows:
        # expires_utc == 0 means a session cookie (no persistent expiry) — keep.
        # A positive expiry in the past means the cookie is dead; drop it.
        is_expired = bool(expires_utc) and expires_utc <= now_us
        value = plain if plain else _decrypt_value(enc, aes_key, host_key)
        if value is None or "\ufffd" in value:
            continue
        if is_expired:
            if "session-token" in name or name == "_mfp_session":
                expired_session_cookies += 1
            continue
        cookies[name] = value
    return cookies, expired_session_cookies


def _resolve_db_path(browser: str, profile: str) -> Path | None:
    """Locate the cookie DB for a browser + profile, trying Network/ then legacy."""
    root = _CHROMIUM_BROWSER_ROOTS_MACOS.get(browser)
    if root is None:
        return None
    appsup = Path.home() / "Library" / "Application Support"
    for rel in (f"{root}/{profile}/Network/Cookies", f"{root}/{profile}/Cookies"):
        candidate = appsup / rel
        if candidate.exists():
            return candidate
    return None


def _list_profiles(browser: str) -> list[str]:
    """Profile directories that actually have a cookie DB, 'Default' first."""
    root = _CHROMIUM_BROWSER_ROOTS_MACOS.get(browser)
    if root is None:
        return []
    base = Path.home() / "Library" / "Application Support" / root
    if not base.is_dir():
        return []
    found: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if (child / "Network" / "Cookies").exists() or (child / "Cookies").exists():
            found.append(child.name)
    # Prefer Default first, then the rest in sorted order.
    found.sort(key=lambda n: (n != "Default", n))
    return found


def extract_mfp_cookies(browser: str = "Chrome", profile: str | None = None) -> dict[str, str]:
    """Extract a live MyFitnessPal session from `browser` (default Chrome) on macOS.

    If `profile` is given (e.g. "Default", "Profile 1"), only that profile is
    tried. Otherwise every profile that has a cookie DB is searched, Default
    first, and the first one holding an authenticated, non-expired MFP session
    wins. Raises CookieExtractionError with an actionable message on failure.
    """
    if browser not in _CHROMIUM_BROWSER_ROOTS_MACOS:
        raise CookieExtractionError(
            f"unsupported browser {browser!r}. Supported: "
            f"{', '.join(sorted(_CHROMIUM_BROWSER_ROOTS_MACOS))}"
        )

    profiles = [profile] if profile else _list_profiles(browser)
    if not profiles:
        raise CookieExtractionError(
            f"no {browser} profile with a cookie database found. Is {browser} "
            "installed and have you logged into myfitnesspal.com in it?"
        )

    # The Keychain password is per-browser (not per-profile); read it once. This
    # is what triggers the one-time macOS Keychain authorization dialog.
    service = f"{browser} Safe Storage"
    password = _keychain_password(service)
    if password is None:
        raise CookieExtractionError(
            f"could not read '{service}' from the macOS Keychain. When the "
            "permission dialog appears, click 'Always Allow'. If you previously "
            "denied it, open Keychain Access, find the entry, and grant access."
        )
    aes_key = _derive_aes_key(password)

    saw_cookies_but_undecryptable = False
    saw_expired_session = False
    tried: list[str] = []
    for prof in profiles:
        db_path = _resolve_db_path(browser, prof)
        if db_path is None:
            continue
        tried.append(prof)
        cookies, expired_session = _extract_from_db(db_path, aes_key)
        if expired_session and not cookies:
            saw_expired_session = True
        if cookies and not has_mfp_session(cookies):
            saw_cookies_but_undecryptable = True
        if has_mfp_session(cookies):
            return cookies

    # Nothing usable — give the most specific reason we can.
    where = f"{browser} ({', '.join(tried)})" if tried else browser
    if saw_expired_session:
        raise CookieExtractionError(
            f"your {where} MyFitnessPal session cookie has expired. Open "
            "myfitnesspal.com in the browser, log in again, then retry."
        )
    if saw_cookies_but_undecryptable:
        raise CookieExtractionError(
            f"found myfitnesspal.com cookies in {where} but none look like an "
            "authenticated session (no session-token / _mfp_session), or they are "
            "v20 app-bound encrypted (newest Chrome) and can't be decrypted here. "
            "Log in again, or use a browser whose cookies are v10/v11."
        )
    raise CookieExtractionError(
        f"no decryptable myfitnesspal.com session cookies found in {where}. "
        "Log into myfitnesspal.com in the browser and retry."
    )
