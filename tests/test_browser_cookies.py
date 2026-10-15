"""Tests for Chrome cookie extraction hardening (finding #6).

These build a synthetic Chromium cookie SQLite DB with plaintext `value`s (so no
real Keychain/crypto is needed) and verify: expiry filtering, session-marker
detection, profile discovery, redaction (values never appear in errors), and
actionable error messages. The macOS Keychain call is monkeypatched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mfp_sync.browser_cookies as bc
from mfp_sync.browser_cookies import (
    CookieExtractionError,
    _list_profiles,
    _now_chromium_us,
    extract_mfp_cookies,
    has_mfp_session,
)

_CHROMIUM_EPOCH_OFFSET_US = 11_644_473_600_000_000


def _make_cookie_db(path: Path, rows: list[tuple]) -> None:
    """rows: (name, value, host_key, expires_utc). Plaintext values (enc empty)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE cookies (name TEXT, value TEXT, encrypted_value BLOB, "
        "host_key TEXT, expires_utc INTEGER, is_persistent INTEGER)"
    )
    for name, value, host, expires in rows:
        con.execute(
            "INSERT INTO cookies VALUES (?,?,?,?,?,?)",
            (name, value, b"", host, expires, 1 if expires else 0),
        )
    con.commit()
    con.close()


def _future_us() -> int:
    return _now_chromium_us() + 86_400 * 1_000_000  # +1 day


def _past_us() -> int:
    return _now_chromium_us() - 86_400 * 1_000_000  # -1 day


@pytest.fixture
def fake_chrome(tmp_path, monkeypatch):
    """Point the extractor at a temp 'Application Support' and stub the Keychain."""
    appsup = tmp_path / "Library" / "Application Support"
    monkeypatch.setattr(bc.Path, "home", staticmethod(lambda: tmp_path))
    # Keychain + key derivation stubbed (we use plaintext cookie values).
    monkeypatch.setattr(bc, "_keychain_password", lambda service: b"stub-password")
    monkeypatch.setattr(bc, "_derive_aes_key", lambda pw: b"\x00" * 16)
    return appsup


def test_has_mfp_session_markers():
    assert has_mfp_session({"__Secure-next-auth.session-token": "x"})
    assert has_mfp_session({"_mfp_session": "x"})
    assert not has_mfp_session({"_ga": "x", "__Host-next-auth.csrf-token": "y"})


def test_extracts_live_session(fake_chrome):
    db = fake_chrome / "Google/Chrome/Default/Network/Cookies"
    _make_cookie_db(db, [
        ("__Secure-next-auth.session-token", "live-token", ".myfitnesspal.com", _future_us()),
        ("_mfp_session", "sess", "www.myfitnesspal.com", 0),  # 0 == session cookie, kept
        ("_ga", "irrelevant", ".myfitnesspal.com", _future_us()),
    ])
    cookies = extract_mfp_cookies("Chrome")
    assert cookies["__Secure-next-auth.session-token"] == "live-token"
    assert "_mfp_session" in cookies


def test_expired_session_raises_clear_error(fake_chrome):
    db = fake_chrome / "Google/Chrome/Default/Network/Cookies"
    _make_cookie_db(db, [
        ("__Secure-next-auth.session-token", "dead-token", ".myfitnesspal.com", _past_us()),
        ("_mfp_session", "dead", "www.myfitnesspal.com", _past_us()),
    ])
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("Chrome")
    msg = str(ei.value)
    assert "expired" in msg.lower()
    # Redaction: the cookie VALUE must never leak into the error.
    assert "dead-token" not in msg
    assert "dead" not in msg.split()  # not as a standalone token


def test_profile_discovery_finds_nondefault(fake_chrome):
    # MFP session only in "Profile 1", not Default.
    _make_cookie_db(
        fake_chrome / "Google/Chrome/Default/Network/Cookies",
        [("_ga", "x", ".myfitnesspal.com", _future_us())],  # no session here
    )
    _make_cookie_db(
        fake_chrome / "Google/Chrome/Profile 1/Network/Cookies",
        [("_mfp_session", "p1-session", "www.myfitnesspal.com", 0)],
    )
    profiles = _list_profiles("Chrome")
    assert "Default" in profiles and "Profile 1" in profiles
    assert profiles[0] == "Default"  # Default is tried first
    cookies = extract_mfp_cookies("Chrome")  # auto-search finds Profile 1
    assert cookies["_mfp_session"] == "p1-session"


def test_specific_profile_only(fake_chrome):
    _make_cookie_db(
        fake_chrome / "Google/Chrome/Profile 2/Network/Cookies",
        [("_mfp_session", "p2", "www.myfitnesspal.com", 0)],
    )
    cookies = extract_mfp_cookies("Chrome", profile="Profile 2")
    assert cookies["_mfp_session"] == "p2"
    # A profile with no DB -> clear error.
    with pytest.raises(CookieExtractionError):
        extract_mfp_cookies("Chrome", profile="Nonexistent")


def test_no_session_cookies_actionable_error(fake_chrome):
    _make_cookie_db(
        fake_chrome / "Google/Chrome/Default/Network/Cookies",
        [("_ga", "x", ".myfitnesspal.com", _future_us()),
         ("__Host-next-auth.csrf-token", "csrf", "www.myfitnesspal.com", _future_us())],
    )
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("Chrome")
    assert "authenticated session" in str(ei.value).lower()


def test_keychain_denied_raises(fake_chrome, monkeypatch):
    _make_cookie_db(
        fake_chrome / "Google/Chrome/Default/Network/Cookies",
        [("_mfp_session", "s", "www.myfitnesspal.com", 0)],
    )
    monkeypatch.setattr(bc, "_keychain_password", lambda service: None)
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("Chrome")
    assert "keychain" in str(ei.value).lower()


def test_unsupported_browser_rejected(fake_chrome):
    with pytest.raises(CookieExtractionError) as ei:
        extract_mfp_cookies("Netscape")
    assert "unsupported browser" in str(ei.value).lower()
