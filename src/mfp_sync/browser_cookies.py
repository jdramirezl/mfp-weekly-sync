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
from pathlib import Path

DOMAIN = "myfitnesspal.com"

# Cookies DB locations relative to ~/Library/Application Support/.
# Newer Chromium moved the DB into a "Network/" subdir; try that first.
_CHROMIUM_COOKIES_PATHS_MACOS: dict[str, list[str]] = {
    "Arc": ["Arc/User Data/Default/Network/Cookies", "Arc/User Data/Default/Cookies"],
    "Chrome": ["Google/Chrome/Default/Network/Cookies", "Google/Chrome/Default/Cookies"],
    "Chromium": ["Chromium/Default/Network/Cookies", "Chromium/Default/Cookies"],
    "Microsoft Edge": ["Microsoft Edge/Default/Network/Cookies",
                       "Microsoft Edge/Default/Cookies"],
    "Brave": ["BraveSoftware/Brave-Browser/Default/Network/Cookies",
              "BraveSoftware/Brave-Browser/Default/Cookies"],
    "Vivaldi": ["Vivaldi/Default/Network/Cookies", "Vivaldi/Default/Cookies"],
    "Opera": ["com.operasoftware.Opera/Network/Cookies", "com.operasoftware.Opera/Cookies"],
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


def _extract_from_db(cookies_db_path: Path, aes_key: bytes) -> dict[str, str]:
    fd, tmp_path = tempfile.mkstemp(suffix=".cookies.db")
    os.close(fd)
    try:
        _snapshot_db(cookies_db_path, tmp_path)
        con = sqlite3.connect(tmp_path)
        try:
            rows = con.execute(
                "SELECT name, value, encrypted_value, host_key FROM cookies "
                "WHERE host_key = ? OR host_key LIKE ?",
                (DOMAIN, f"%.{DOMAIN}"),
            ).fetchall()
        finally:
            con.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    cookies: dict[str, str] = {}
    for name, plain, enc, host_key in rows:
        value = plain if plain else _decrypt_value(enc, aes_key, host_key)
        if value is None or "\ufffd" in value:
            continue
        cookies[name] = value
    return cookies


def _resolve_db_path(browser: str) -> Path | None:
    appsup = Path.home() / "Library" / "Application Support"
    for rel in _CHROMIUM_COOKIES_PATHS_MACOS.get(browser, []):
        candidate = appsup / rel
        if candidate.exists():
            return candidate
    return None


def extract_mfp_cookies(browser: str = "Chrome") -> dict[str, str]:
    """Extract MyFitnessPal cookies from `browser` (default Chrome) on macOS.

    Raises CookieExtractionError with an actionable message on any failure.
    """
    db_path = _resolve_db_path(browser)
    if db_path is None:
        raise CookieExtractionError(
            f"no {browser} cookie database found. Is {browser} installed and have "
            f"you logged into myfitnesspal.com in it?"
        )
    service = f"{browser} Safe Storage"
    password = _keychain_password(service)
    if password is None:
        raise CookieExtractionError(
            f"could not read '{service}' from the macOS Keychain. When the "
            "permission dialog appears, click 'Always Allow'. (If it was denied, "
            "open Keychain Access, find the entry, and grep its access control.)"
        )
    aes_key = _derive_aes_key(password)
    cookies = _extract_from_db(db_path, aes_key)
    if not cookies:
        raise CookieExtractionError(
            f"no decryptable myfitnesspal.com cookies found in {browser}. They may "
            "be v20 app-bound encrypted (newest Chrome), or you are not logged in."
        )
    if not has_mfp_session(cookies):
        raise CookieExtractionError(
            f"found {browser} cookies for myfitnesspal.com but none look like an "
            "authenticated session (no session-token / _mfp_session). Log in again."
        )
    return cookies
