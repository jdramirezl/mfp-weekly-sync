"""Tests for cross-platform browser cookie extraction.

Builds synthetic cookie DBs so no real Keychain/DPAPI/crypto is needed:
  * Chromium: plaintext `value` column (encrypted_value empty) with the key
    provider stubbed — exercises profile discovery, expiry filtering, v20
    detection, redaction, and platform dispatch.
  * Firefox: a real moz_cookies table (values are genuinely unencrypted).
Also covers the Windows AES-256-GCM decryptor with a real round-trip.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

import mfp_sync.browser_cookies as bc
from mfp_sync.browser_cookies import (
    CookieExtractionError,
    _canon_browser,
    _list_chromium_profiles,
    _now_chromium_us,
    extract_mfp_cookies,
    has_mfp_session,
)

# -- helpers -----------------------------------------------------------------


def _make_chromium_db(path: Path, rows: list[tuple]) -> None:
    """rows: (name, value, host_key, expires_utc) or (name, value, host, exp, enc_blob)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE cookies (name TEXT, value TEXT, encrypted_value BLOB, "
        "host_key TEXT, expires_utc INTEGER)"
    )
    for r in rows:
        name, value, host, expires = r[0], r[1], r[2], r[3]
        enc = r[4] if len(r) > 4 else b""
        con.execute("INSERT INTO cookies VALUES (?,?,?,?,?)", (name, value, enc, host, expires))
    con.commit()
    con.close()


def _make_firefox_db(path: Path, rows: list[tuple]) -> None:
    """rows: (name, value, host, expiry_seconds)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, expiry INTEGER)")
    for name, value, host, expiry in rows:
        con.execute("INSERT INTO moz_cookies VALUES (?,?,?,?)", (name, value, host, expiry))
    con.commit()
    con.close()


def _future_us() -> int:
    return _now_chromium_us() + 86_400 * 1_000_000


def _past_us() -> int:
    return _now_chromium_us() - 86_400 * 1_000_000


@pytest.fixture
def chromium_env(tmp_path, monkeypatch):
    """Stub the platform so Chromium extraction uses a temp dir + plaintext values."""
    user_data = tmp_path / "UserData"
    monkeypatch.setattr(bc, "_chromium_user_data_dir", lambda browser: user_data)
    # Pretend we're on macOS CBC path, but with a stub key (values are plaintext).
    monkeypatch.setattr(bc, "_is_windows", lambda: False)
    monkeypatch.setattr(bc, "_macos_key", lambda browser: b"\x00" * 16)
    return user_data


# -- generic --------------------------------------------------------------


def test_has_mfp_session_markers():
    assert has_mfp_session({"__Secure-next-auth.session-token": "x"})
    assert has_mfp_session({"_mfp_session": "x"})
    assert not has_mfp_session({"_ga": "x", "__Host-next-auth.csrf-token": "y"})


def test_browser_aliases():
    assert _canon_browser("chrome") == "Chrome"
    assert _canon_browser("EDGE") == "Microsoft Edge"
    assert _canon_browser("firefox") == "Firefox"
    assert _canon_browser("brave") == "Brave"


# -- chromium ----------------------------------------------------------------


def test_extracts_live_session(chromium_env):
    _make_chromium_db(chromium_env / "Default/Network/Cookies", [
        ("__Secure-next-auth.session-token", "live-token", ".myfitnesspal.com", _future_us()),
        ("_mfp_session", "sess", "www.myfitnesspal.com", 0),
        ("_ga", "irrelevant", ".myfitnesspal.com", _future_us()),
    ])
    cookies = extract_mfp_cookies("Chrome")
    assert cookies["__Secure-next-auth.session-token"] == "live-token"
    assert "_mfp_session" in cookies


def test_expired_session_raises_and_redacts(chromium_env):
    _make_chromium_db(chromium_env / "Default/Network/Cookies", [
        ("__Secure-next-auth.session-token", "dead-token", ".myfitnesspal.com", _past_us()),
        ("_mfp_session", "deadval", "www.myfitnesspal.com", _past_us()),
    ])
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("Chrome")
    msg = str(ei.value)
    assert "expired" in msg.lower()
    assert "dead-token" not in msg and "deadval" not in msg  # values never leak


def test_profile_discovery_finds_nondefault(chromium_env):
    _make_chromium_db(chromium_env / "Default/Network/Cookies",
                      [("_ga", "x", ".myfitnesspal.com", _future_us())])
    _make_chromium_db(chromium_env / "Profile 1/Network/Cookies",
                      [("_mfp_session", "p1", "www.myfitnesspal.com", 0)])
    profs = _list_chromium_profiles(chromium_env)
    assert profs[0] == "Default" and "Profile 1" in profs
    assert extract_mfp_cookies("Chrome")["_mfp_session"] == "p1"


def test_specific_profile(chromium_env):
    _make_chromium_db(chromium_env / "Profile 2/Network/Cookies",
                      [("_mfp_session", "p2", "www.myfitnesspal.com", 0)])
    assert extract_mfp_cookies("Chrome", profile="Profile 2")["_mfp_session"] == "p2"
    with pytest.raises(CookieExtractionError):
        extract_mfp_cookies("Chrome", profile="Nope")


def test_v20_appbound_detected(chromium_env):
    _make_chromium_db(chromium_env / "Default/Network/Cookies", [
        ("_mfp_session", "", "www.myfitnesspal.com", 0, b"v20" + b"\x01" * 40),
    ])
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("Chrome")
    assert "v20" in str(ei.value).lower() or "app-bound" in str(ei.value).lower()


def test_unsupported_platform_browser(monkeypatch):
    monkeypatch.setattr(bc, "_chromium_user_data_dir", lambda browser: None)
    with pytest.raises(CookieExtractionError):
        extract_mfp_cookies("Chrome")


# -- windows GCM round-trip --------------------------------------------------


def test_windows_gcm_decrypt_roundtrip():
    """Encrypt a value the way Windows Chromium does, then decrypt it."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)
    nonce = os.urandom(12)
    plaintext = b"my-session-token-value"
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    blob = b"v10" + nonce + ct
    got = bc._decrypt_gcm(blob, key, host_key="")
    assert got == "my-session-token-value"


def test_windows_dispatch_uses_gcm(tmp_path, monkeypatch):
    """On Windows, encrypted values are decrypted via GCM using the DPAPI key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)

    def enc(val: str) -> bytes:
        nonce = os.urandom(12)
        return b"v10" + nonce + AESGCM(key).encrypt(nonce, val.encode(), None)

    user_data = tmp_path / "UserData"
    _make_chromium_db(user_data / "Default/Network/Cookies", [
        ("_mfp_session", "", "www.myfitnesspal.com", 0, enc("win-session")),
    ])
    monkeypatch.setattr(bc, "_chromium_user_data_dir", lambda browser: user_data)
    monkeypatch.setattr(bc, "_is_windows", lambda: True)
    monkeypatch.setattr(bc, "_windows_key", lambda ud: key)
    cookies = extract_mfp_cookies("Chrome")
    assert cookies["_mfp_session"] == "win-session"


# -- firefox -----------------------------------------------------------------


@pytest.fixture
def firefox_env(tmp_path, monkeypatch):
    base = tmp_path / "ff-profiles"
    monkeypatch.setattr(bc, "_firefox_profiles_dir", lambda: base)
    return base


def test_firefox_extracts_session(firefox_env):
    _make_firefox_db(firefox_env / "abc.default-release/cookies.sqlite", [
        ("__Secure-next-auth.session-token", "ff-token", ".myfitnesspal.com",
         int(time.time()) + 9999),
        ("_mfp_session", "ff-sess", "www.myfitnesspal.com", 0),
    ])
    cookies = extract_mfp_cookies("firefox")
    assert cookies["__Secure-next-auth.session-token"] == "ff-token"


def test_firefox_expired(firefox_env):
    _make_firefox_db(firefox_env / "abc.default-release/cookies.sqlite", [
        ("_mfp_session", "old", "www.myfitnesspal.com", int(time.time()) - 10),
    ])
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("firefox")
    assert "expired" in str(ei.value).lower()


def test_firefox_none_found(firefox_env):
    with pytest.raises(CookieExtractionError):
        extract_mfp_cookies("firefox")
