"""Log the bot into MyFitnessPal with its own email + password.

Run with Mason's tool Python (it needs Mason's Playwright and
myfitnesspal_mcp.auth). Uses Mason's persistent Chromium profile, so the
session this creates is the one Mason's refresh keeps alive, and it is
independent of any browser or phone session the user has.

Inputs (environment only):
  MFP_LOGIN_EMAIL             login email (also the Keychain account)
  MFP_LOGIN_KEYCHAIN_SERVICE  Keychain service name (default mfp-login)
  MFP_MCP_DATA_DIR            Mason data dir (holds browser-profile/)
  MFP_USERNAME                MFP username to save with the session (optional)
  MFP_LOG_DIR                 where a failure screenshot goes (optional)
  MFP_LOGIN_TIMEOUT           seconds to wait for a session (default 180)
  MFP_LOGIN_HEADLESS          1 = headless (default 0: headed, so the user can
                              finish a captcha / 2FA in the window)

The password is read from the macOS Keychain here, kept in a local variable,
handed to the page with locator.fill() over Playwright's pipe, and never
printed, logged or put in an exception message.

Exit codes: 0 ok, 4 no Keychain item, 5 MFP rejected the login,
6 timed out, 7 anything else.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOGIN_URL = "https://www.myfitnesspal.com/account/login"
MFP_URL = "https://www.myfitnesspal.com/"
SESSION_COOKIE = "__Secure-next-auth.session-token"

# Tried in order; the first visible match wins. Never anything that mentions
# Google/Facebook/Apple: those would start an SSO login instead.
EMAIL_SELECTORS = [
    ("label", "Email Address"),
    ("css", "input[type=email]"),
    ("css", "input[name=email]"),
    ("css", "input[autocomplete=username]"),
    ("css", "#email"),
]
PASSWORD_SELECTORS = [
    ("label", "Password"),
    ("css", "input[type=password]"),
]
SUBMIT_SELECTORS = [
    ("role-button", r"^\s*log\s*in\s*$"),
    ("css", "button[type=submit]"),
]
CHALLENGE_WORDS = ("captcha", "challenge", "verify", "verification", "code", "cloudflare", "two-factor", "2fa")
REJECT_WORDS = ("incorrect", "invalid", "doesn't match", "does not match", "wrong password")
SSO_WORDS = re.compile(r"google|facebook|apple", re.I)

EXIT_OK, EXIT_NO_KEYCHAIN, EXIT_REJECTED, EXIT_TIMEOUT, EXIT_OTHER = 0, 4, 5, 6, 7


class LoginError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def say(msg):
    print(f"[mfp-login] {msg}", flush=True)


def read_password(service, email):
    """Return the Keychain password. Raises LoginError(4) without the value."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", email, "-w"],
            capture_output=True,
            text=True,
        )
    except OSError:
        proc = None
    if proc is None or proc.returncode != 0 or not proc.stdout.strip("\n"):
        raise LoginError(
            EXIT_NO_KEYCHAIN,
            "no Keychain password for this login. Add it with:\n"
            f"  security add-generic-password -s {service} -a <email> -w"
            "   # prompts for the password",
        )
    return proc.stdout.rstrip("\n")


def locate(page, selectors):
    """First visible locator from `selectors`; returns (locator, description)."""
    for kind, value in selectors:
        if kind == "label":
            loc = page.get_by_label(value, exact=False)
        elif kind == "role-button":
            loc = page.get_by_role("button", name=re.compile(value, re.I))
        else:
            loc = page.locator(value)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first, f"{kind}={value}"
        except Exception:
            continue
    return None, None


def has_session(context):
    return any(c.get("name") == SESSION_COOKIE for c in context.cookies(MFP_URL))


def url_path(page):
    return re.sub(r"[?#].*$", "", page.url or "")


def page_text(page):
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def rejected(page):
    try:
        alert = page.locator("[role=alert]")
        if alert.count() > 0 and alert.first.is_visible():
            return True
    except Exception:
        pass
    text = page_text(page)
    return "/account/login" in url_path(page) and any(w in text for w in REJECT_WORDS)


def looks_like_challenge(page):
    hay = (url_path(page) + " " + page_text(page)).lower()
    return any(w in hay for w in CHALLENGE_WORDS)


def screenshot(page):
    log_dir = os.environ.get("MFP_LOG_DIR")
    if not log_dir:
        return
    try:
        path = Path(log_dir) / "login-failure.png"
        page.screenshot(path=str(path))
        os.chmod(path, 0o600)
        say(f"screenshot saved to {path} (may show the email; keep it private)")
    except Exception:
        pass


def login(context, page, email, password_source, timeout):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    if has_session(context) and "/account/login" not in url_path(page):
        say("already logged in (profile has a live session); not filling the form")
    else:
        email_box, how = locate(page, EMAIL_SELECTORS)
        if email_box is None:
            if has_session(context):
                say("already logged in (profile has a live session); not filling the form")
                return
            raise LoginError(EXIT_OTHER, "could not find the email field on the login page")
        say(f"email field matched {how}")
        pw_box, how = locate(page, PASSWORD_SELECTORS)
        if pw_box is None:
            raise LoginError(EXIT_OTHER, "could not find the password field on the login page")
        say(f"password field matched {how}")
        submit, how = locate(page, SUBMIT_SELECTORS)
        if submit is None:
            raise LoginError(EXIT_OTHER, "could not find the Log in button")
        try:
            label = submit.inner_text() or ""
        except Exception:
            label = ""
        if SSO_WORDS.search(label):
            raise LoginError(EXIT_OTHER, "submit selector matched an SSO button; refusing to click it")
        say(f"submit button matched {how}")

        email_box.fill(email)
        pw_box.fill(password_source())
        submit.click()

    deadline = time.monotonic() + timeout
    told = False
    while True:
        if has_session(context) and "/account/login" not in url_path(page):
            return
        if rejected(page):
            raise LoginError(EXIT_REJECTED, "MFP rejected the email/password")
        if not told and looks_like_challenge(page):
            say(f"Waiting for you to finish the challenge in the browser window… (timeout {timeout}s)")
            told = True
        if time.monotonic() >= deadline:
            raise LoginError(EXIT_TIMEOUT, f"no MFP session after {timeout}s")
        page.wait_for_timeout(2000)


def main():
    email = os.environ.get("MFP_LOGIN_EMAIL", "").strip()
    service = os.environ.get("MFP_LOGIN_KEYCHAIN_SERVICE") or "mfp-login"
    data_dir = os.environ.get("MFP_MCP_DATA_DIR", "")
    timeout = int(os.environ.get("MFP_LOGIN_TIMEOUT") or "180")
    headless = os.environ.get("MFP_LOGIN_HEADLESS") == "1"
    if not email or not data_dir:
        say("MFP_LOGIN_EMAIL and MFP_MCP_DATA_DIR are required")
        return EXIT_OTHER

    try:
        password = read_password(service, email)
    except LoginError as exc:
        say(str(exc))
        return exc.code

    from playwright.sync_api import sync_playwright
    from myfitnesspal_mcp import auth

    profile = Path(data_dir) / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    code = EXIT_OTHER
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile), headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            login(context, page, email, lambda: password, timeout)
            cookies = {
                c["name"]: c["value"]
                for c in context.cookies(MFP_URL)
                if "myfitnesspal.com" in (c.get("domain") or "")
            }
            if SESSION_COOKIE not in cookies:
                raise LoginError(EXIT_OTHER, "logged in, but no session cookie to save")
            auth.save_cookies(cookies, username=os.environ.get("MFP_USERNAME") or None)
            say(f"final page {url_path(page)}")
            say(f"Saved {len(cookies)} cookies.")
            code = EXIT_OK
        except LoginError as exc:
            say(str(exc))
            say(f"final page {url_path(page)}")
            screenshot(page)
            code = exc.code
        except Exception as exc:  # never include the password: it is not in exc
            say(f"login failed: {type(exc).__name__}")
            screenshot(page)
            code = EXIT_OTHER
        finally:
            password = None
            context.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
