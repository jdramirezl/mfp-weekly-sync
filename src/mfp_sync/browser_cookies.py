"""Read an authenticated MyFitnessPal session from a local browser.

For accounts that sign in via Google/Apple SSO there is no password, so the
native mobile OAuth flow (mfp-api) cannot be used. Instead we reuse the session
you already established by logging into myfitnesspal.com in a browser.

Supported, by platform:
  * macOS  — Chromium browsers (Chrome/Edge/Brave/Arc/Vivaldi/Opera/Chromium)
             via the macOS Keychain, and Firefox.
  * Windows — Chromium browsers (Chrome/Edge/Brave) via DPAPI, and Firefox.
  * Linux  — Firefox (Chromium Linux keyring not implemented).

Chromium cookie values are encrypted; Firefox stores them unencrypted in
`cookies.sqlite`, which makes Firefox the most reliable cross-platform path
(and the easiest on Windows, where modern Chrome uses app-bound "v20"
encryption that cannot be decrypted outside the browser process).

Security:
  * Cookie values are never logged or printed. Callers get a dict they pass
    straight into an HTTPS request; redact before surfacing anything.
  * The cookie DB is snapshotted read-only via the SQLite backup API.

Chromium decryption adapted from the approach used by
AdamWalt/myfitnesspal-mcp-python.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DOMAIN = "myfitnesspal.com"

# Chromium stores expires_utc as microseconds since 1601-01-01 (Windows epoch).
_CHROMIUM_EPOCH_OFFSET_US = 11_644_473_600_000_000  # microseconds 1601->1970

# Markers that indicate an actually-authenticated MFP session (not pre-auth).
_SESSION_MARKERS = ("session-token", "_mfp_session")


def _now_chromium_us() -> int:
    return int(time.time() * 1_000_000) + _CHROMIUM_EPOCH_OFFSET_US


def _is_windows() -> bool:
    return sys.platform.startswith("win") or platform.system() == "Windows"


def _is_macos() -> bool:
    return sys.platform == "darwin"


class CookieExtractionError(RuntimeError):
    """Raised when no usable MyFitnessPal session can be extracted."""


def has_mfp_session(cookies: dict[str, str]) -> bool:
    """True if the cookie set carries an authenticated-session marker."""
    return any(("session-token" in name or name == "_mfp_session") for name in cookies)


# ---------------------------------------------------------------------------
# Browser location tables
# ---------------------------------------------------------------------------

# macOS: relative to ~/Library/Application Support/
_CHROMIUM_ROOTS_MACOS: dict[str, str] = {
    "Arc": "Arc/User Data",
    "Chrome": "Google/Chrome",
    "Chromium": "Chromium",
    "Microsoft Edge": "Microsoft Edge",
    "Brave": "BraveSoftware/Brave-Browser",
    "Vivaldi": "Vivaldi",
    "Opera": "com.operasoftware.Opera",
}

# Windows: relative to %LOCALAPPDATA%  (User Data dir holds "Local State" + profiles)
_CHROMIUM_ROOTS_WINDOWS: dict[str, str] = {
    "Chrome": "Google/Chrome/User Data",
    "Chromium": "Chromium/User Data",
    "Microsoft Edge": "Microsoft/Edge/User Data",
    "Brave": "BraveSoftware/Brave-Browser/User Data",
    "Vivaldi": "Vivaldi/User Data",
    "Opera": "Opera Software/Opera Stable",
}

# Accept common aliases / case-insensitive names.
_BROWSER_ALIASES = {
    "chrome": "Chrome", "google chrome": "Chrome", "chromium": "Chromium",
    "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge", "msedge": "Microsoft Edge",
    "brave": "Brave", "arc": "Arc", "vivaldi": "Vivaldi", "opera": "Opera",
    "firefox": "Firefox", "mozilla firefox": "Firefox", "ff": "Firefox",
}


def _canon_browser(browser: str) -> str:
    return _BROWSER_ALIASES.get(browser.strip().lower(), browser)


# ---------------------------------------------------------------------------
# Shared Chromium helpers (platform-independent)
# ---------------------------------------------------------------------------


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


def _strip_host_prefix(plaintext: bytes, host_key: str) -> bytes:
    """Modern Chromium prepends SHA-256(host_key) to the plaintext; strip if present."""
    if host_key and len(plaintext) >= 32:
        if plaintext[:32] == hashlib.sha256(host_key.encode("utf-8")).digest():
            return plaintext[32:]
    return plaintext


def _decrypt_cbc(encrypted_value: bytes, aes_key: bytes, host_key: str) -> str | None:
    """Decrypt a macOS/Linux Chromium v10/v11 cookie (AES-128-CBC, iv=16 spaces)."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    try:
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16),
                        backend=default_backend())
        d = cipher.decryptor()
        plaintext = d.update(encrypted_value[3:]) + d.finalize()
    except Exception:
        return None
    if not plaintext:
        return None
    pad = plaintext[-1]
    if pad < 1 or pad > 16:
        return None
    plaintext = _strip_host_prefix(plaintext[:-pad], host_key)
    try:
        return plaintext.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _decrypt_gcm(encrypted_value: bytes, aes_key: bytes, host_key: str) -> str | None:
    """Decrypt a Windows Chromium v10 cookie (AES-256-GCM).

    Layout: b'v10' | 12-byte nonce | ciphertext | 16-byte tag.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        nonce = encrypted_value[3:15]
        ct_and_tag = encrypted_value[15:]
        plaintext = AESGCM(aes_key).decrypt(nonce, ct_and_tag, None)
    except Exception:
        return None
    plaintext = _strip_host_prefix(plaintext, host_key)
    try:
        return plaintext.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _decrypt_value(
    encrypted_value: bytes, aes_key: bytes, host_key: str, scheme: str
) -> str | None:
    """Dispatch to the right cookie decryptor based on prefix + platform scheme.

    scheme is "cbc" (macOS/Linux) or "gcm" (Windows). v20 app-bound cookies are
    unsupported everywhere (require the browser process) and return None.
    """
    if not encrypted_value or len(encrypted_value) < 3:
        return None
    prefix = encrypted_value[:3]
    if prefix in (b"v10", b"v11"):
        return _decrypt_gcm(encrypted_value, aes_key, host_key) if scheme == "gcm" \
            else _decrypt_cbc(encrypted_value, aes_key, host_key)
    # v20 == app-bound; not decryptable outside the browser.
    return None


def _read_cookie_rows(cookies_db_path: Path):
    fd, tmp_path = tempfile.mkstemp(suffix=".cookies.db")
    os.close(fd)
    try:
        _snapshot_db(cookies_db_path, tmp_path)
        con = sqlite3.connect(tmp_path)
        try:
            return con.execute(
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


def _rows_to_cookies(rows, aes_key: bytes, scheme: str) -> tuple[dict[str, str], int, bool]:
    """Return (live cookies, expired-session-count, saw_appbound_v20)."""
    now_us = _now_chromium_us()
    cookies: dict[str, str] = {}
    expired_session = 0
    saw_v20 = False
    for name, plain, enc, host_key, expires_utc in rows:
        if enc and enc[:3] == b"v20":
            saw_v20 = True
        is_expired = bool(expires_utc) and expires_utc <= now_us
        value = plain if plain else _decrypt_value(enc, aes_key, host_key, scheme)
        if value is None or "\ufffd" in value:
            continue
        if is_expired:
            if "session-token" in name or name == "_mfp_session":
                expired_session += 1
            continue
        cookies[name] = value
    return cookies, expired_session, saw_v20


# ---------------------------------------------------------------------------
# Platform key providers
# ---------------------------------------------------------------------------


def _macos_key(browser: str) -> bytes:
    """Derive the AES-128 key for a Chromium browser from the macOS Keychain."""
    service = f"{browser} Safe Storage"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, check=True, timeout=20,
        )
        password = result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise CookieExtractionError(
            f"could not read '{service}' from the macOS Keychain. When the "
            "permission dialog appears, click 'Always Allow'. If you previously "
            "denied it, open Keychain Access, find the entry, and grant access."
        ) from exc
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt",
                     iterations=1003, backend=default_backend())
    return kdf.derive(password)


def _windows_key(user_data_dir: Path) -> bytes:
    """Decrypt the AES-256-GCM key from a Chromium "Local State" via Windows DPAPI."""
    local_state = user_data_dir / "Local State"
    if not local_state.exists():
        raise CookieExtractionError(
            f"could not find Chromium 'Local State' at {local_state}. Is the "
            "browser installed and have you opened it at least once?"
        )
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        b64_key = data["os_crypt"]["encrypted_key"]
    except (OSError, ValueError, KeyError) as exc:
        raise CookieExtractionError(f"could not parse Chromium Local State: {exc}") from exc

    blob = base64.b64decode(b64_key)
    if blob[:5] != b"DPAPI":
        raise CookieExtractionError(
            "Chromium key is not DPAPI-wrapped (likely app-bound v20 encryption on "
            "newer Chrome/Edge). Use Firefox on Windows instead — log into "
            "myfitnesspal.com in Firefox and pass --browser firefox."
        )
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        payload = blob[5:]
        blob_in = DATA_BLOB(len(payload), ctypes.cast(ctypes.c_char_p(payload),
                                                      ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            raise CookieExtractionError(
                "Windows DPAPI could not unprotect the Chromium key. Run this as the "
                "same Windows user that uses the browser (DPAPI keys are per-user)."
            )
        key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return key
    except OSError as exc:  # pragma: no cover - windows only
        raise CookieExtractionError(f"DPAPI call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Chromium extraction (platform-dispatched)
# ---------------------------------------------------------------------------


def _chromium_user_data_dir(browser: str) -> Path | None:
    if _is_macos():
        root = _CHROMIUM_ROOTS_MACOS.get(browser)
        return (Path.home() / "Library" / "Application Support" / root) if root else None
    if _is_windows():
        root = _CHROMIUM_ROOTS_WINDOWS.get(browser)
        base = os.environ.get("LOCALAPPDATA")
        if root and base:
            return Path(base) / root
        return None
    return None  # Chromium on Linux not implemented (use Firefox)


def _resolve_chromium_db(user_data_dir: Path, profile: str) -> Path | None:
    for rel in (f"{profile}/Network/Cookies", f"{profile}/Cookies"):
        candidate = user_data_dir / rel
        if candidate.exists():
            return candidate
    return None


def _list_chromium_profiles(user_data_dir: Path) -> list[str]:
    if not user_data_dir.is_dir():
        return []
    found: list[str] = []
    for child in sorted(user_data_dir.iterdir()):
        if not child.is_dir():
            continue
        if (child / "Network" / "Cookies").exists() or (child / "Cookies").exists():
            found.append(child.name)
    found.sort(key=lambda n: (n != "Default", n))
    return found


def _extract_chromium(browser: str, profile: str | None) -> dict[str, str]:
    user_data = _chromium_user_data_dir(browser)
    if user_data is None:
        raise CookieExtractionError(
            f"{browser} is not supported for Chromium extraction on this platform "
            f"({sys.platform}). Try Firefox: --browser firefox."
        )
    profiles = [profile] if profile else _list_chromium_profiles(user_data)
    if not profiles:
        raise CookieExtractionError(
            f"no {browser} profile with a cookie database found under {user_data}. "
            "Is it installed and have you logged into myfitnesspal.com in it?"
        )

    scheme = "gcm" if _is_windows() else "cbc"
    aes_key = _windows_key(user_data) if _is_windows() else _macos_key(browser)

    saw_undecryptable = False
    saw_expired = False
    saw_v20 = False
    tried: list[str] = []
    for prof in profiles:
        db = _resolve_chromium_db(user_data, prof)
        if db is None:
            continue
        tried.append(prof)
        rows = _read_cookie_rows(db)
        cookies, expired, v20 = _rows_to_cookies(rows, aes_key, scheme)
        saw_v20 = saw_v20 or v20
        if expired and not cookies:
            saw_expired = True
        if cookies and not has_mfp_session(cookies):
            saw_undecryptable = True
        if has_mfp_session(cookies):
            return cookies

    where = f"{browser} ({', '.join(tried)})" if tried else browser
    if saw_v20:
        raise CookieExtractionError(
            f"your {where} cookies use app-bound 'v20' encryption (newer "
            "Chrome/Edge) which cannot be decrypted outside the browser. On "
            "Windows, log into myfitnesspal.com in Firefox and use "
            "--browser firefox instead."
        )
    if saw_expired:
        raise CookieExtractionError(
            f"your {where} MyFitnessPal session cookie has expired. Log in again at "
            "myfitnesspal.com, then retry."
        )
    if saw_undecryptable:
        raise CookieExtractionError(
            f"found myfitnesspal.com cookies in {where} but none look like an "
            "authenticated session. Log in again, or try --browser firefox."
        )
    raise CookieExtractionError(
        f"no decryptable myfitnesspal.com session cookies found in {where}. "
        "Log into myfitnesspal.com in the browser (or try --browser firefox)."
    )


# ---------------------------------------------------------------------------
# Firefox extraction (unencrypted sqlite; cross-platform, best on Windows)
# ---------------------------------------------------------------------------


def _firefox_profiles_dir() -> Path | None:
    if _is_windows():
        base = os.environ.get("APPDATA")
        return Path(base) / "Mozilla" / "Firefox" / "Profiles" if base else None
    if _is_macos():
        return Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
    # Linux
    p = Path.home() / ".mozilla" / "firefox"
    return p if p.exists() else None


def _firefox_cookie_dbs() -> list[Path]:
    base = _firefox_profiles_dir()
    if base is None or not base.is_dir():
        return []
    dbs = [p / "cookies.sqlite" for p in base.iterdir()
           if p.is_dir() and (p / "cookies.sqlite").exists()]
    # Prefer default-release / default profiles first.
    dbs.sort(key=lambda p: (0 if "default" in p.parent.name.lower() else 1, str(p)))
    return dbs


def _extract_firefox(profile: str | None) -> dict[str, str]:
    dbs = _firefox_cookie_dbs()
    if profile:
        base = _firefox_profiles_dir()
        if base is not None:
            dbs = [base / profile / "cookies.sqlite"]
    dbs = [p for p in dbs if p.exists()]
    if not dbs:
        raise CookieExtractionError(
            "no Firefox cookies.sqlite found. Is Firefox installed and have you "
            "logged into myfitnesspal.com in it?"
        )
    now_sec = int(time.time())
    saw_expired = False
    for db in dbs:
        fd, tmp = tempfile.mkstemp(suffix=".ff.sqlite")
        os.close(fd)
        try:
            _snapshot_db(db, tmp)
            con = sqlite3.connect(tmp)
            try:
                rows = con.execute(
                    "SELECT name, value, expiry FROM moz_cookies "
                    "WHERE host = ? OR host LIKE ?",
                    (DOMAIN, f"%.{DOMAIN}"),
                ).fetchall()
            finally:
                con.close()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        cookies: dict[str, str] = {}
        for name, value, expiry in rows:
            if expiry and expiry <= now_sec:
                if "session-token" in name or name == "_mfp_session":
                    saw_expired = True
                continue
            if value:
                cookies[name] = value
        if has_mfp_session(cookies):
            return cookies
    if saw_expired:
        raise CookieExtractionError(
            "your Firefox MyFitnessPal session cookie has expired. Log in again at "
            "myfitnesspal.com in Firefox, then retry."
        )
    raise CookieExtractionError(
        "no authenticated myfitnesspal.com session found in Firefox. Log in at "
        "myfitnesspal.com in Firefox and retry."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_mfp_cookies(browser: str = "Chrome", profile: str | None = None) -> dict[str, str]:
    """Extract a live MyFitnessPal session from a local browser.

    `browser` is case-insensitive and accepts aliases ("chrome", "edge",
    "brave", "firefox", ...). `profile` optionally pins a browser profile
    directory; otherwise profiles are searched (Default first).

    Platform support: Chromium browsers on macOS (Keychain) and Windows (DPAPI);
    Firefox on macOS, Windows, and Linux. Raises CookieExtractionError with an
    actionable message on failure.
    """
    canon = _canon_browser(browser)
    if canon == "Firefox":
        return _extract_firefox(profile)
    return _extract_chromium(canon, profile)
