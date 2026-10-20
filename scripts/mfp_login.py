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
  MFP_TURNSTILE_TIMEOUT       seconds to wait for Cloudflare Turnstile before
                              clicking Log in (default 60; the whole login
                              budget if Turnstile shows an interactive box)
  MFP_LOGIN_HEADLESS          1 = headless (default 0: headed, so the user can
                              finish a captcha / 2FA in the window)

The password is read from the macOS Keychain here, kept in a local variable,
handed to the page with locator.fill() over Playwright's pipe, and never
printed, logged or put in an exception message.

Exit codes: 0 ok, 4 no Keychain item, 5 MFP showed an explicit credentials
error, 6 timed out (Turnstile/captcha/2FA not cleared), 7 anything else.
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
CHALLENGE_WORDS = ("captcha", "challenge", "verify", "verification", "code", "two-factor", "2fa")
SSO_WORDS = re.compile(r"google|facebook|apple", re.I)

# Cloudflare Turnstile. On setup day MFP's login page showed a Turnstile widget
# ("Verifying…") with the Log in button disabled; clicking during that window,
# and treating any [role=alert] as an error, produced a false "rejected".
TS_INPUT = 'input[name="cf-turnstile-response"]'
TS_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'
TS_BOX = ".cf-turnstile"
CLICK_BOX_MSG = (
    "Cloudflare pide verificación: haz clic en la casilla de Cloudflare en la "
    "ventana del navegador… (Cloudflare wants a check: click the box in the browser window)"
)

# Only an explicit credentials error counts as a rejection (exit 5). Never
# match on whole-page text, and never on Cloudflare/Turnstile's own status.
ERROR_SELECTORS = ("[role=alert]", "[aria-live]", ".error", "[class*=error]",
                   "[class*=Error]", "[data-testid*=error]")
ERROR_RE = re.compile(
    r"incorrect|invalid|inv[aá]lid|doesn.t match|does not match|wrong password|not recognized", re.I)
ERROR_IGNORE_RE = re.compile(r"cloudflare|verif|turnstile|human", re.I)

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


def on_login_page(page):
    return "/account/login" in url_path(page)


def error_banner(page):
    """First ~60 chars of a visible, explicit credentials error, else None."""
    if not on_login_page(page):
        return None
    for sel in ERROR_SELECTORS:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 10)
        except Exception:
            continue
        for i in range(n):
            try:
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                text = re.sub(r"\s+", " ", el.inner_text() or "").strip()
                if not text or ERROR_IGNORE_RE.search(text) or not ERROR_RE.search(text):
                    continue
                if el.evaluate("e => !!e.closest('.cf-turnstile, [id^=cf-chl], iframe')"):
                    continue
                return text[:60]
            except Exception:
                continue
    return None


def _count(page, sel):
    try:
        return page.locator(sel).count()
    except Exception:
        return 0


def turnstile_present(page):
    return any(_count(page, sel) > 0 for sel in (TS_INPUT, TS_IFRAME, TS_BOX))


def turnstile_solved(page):
    """The hidden response input has a token. The value is never logged."""
    try:
        loc = page.locator(TS_INPUT)
        for i in range(loc.count()):
            if (loc.nth(i).input_value() or "").strip():
                return True
    except Exception:
        pass
    return False


def turnstile_iframe_visible(page):
    try:
        loc = page.locator(TS_IFRAME)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def button_enabled(button):
    try:
        if not button.is_enabled():
            return False
        if button.get_attribute("disabled") is not None:
            return False
        return (button.get_attribute("aria-disabled") or "").lower() != "true"
    except Exception:
        return False


class Tracker:
    """Logs each state once per change, and the click-the-box hint once."""

    def __init__(self):
        self.seen = {}
        self.last = "starting"
        self.asked = False

    def note(self, key, state):
        if self.seen.get(key) != state:
            self.seen[key] = state
            say(state)
        self.last = state

    def ask_click(self):
        if not self.asked:
            self.asked = True
            say(CLICK_BOX_MSG)


def wait_for_turnstile(page, submit, overall_deadline, tracker):
    """Block until Turnstile (if any) has a token and the button is enabled.
    Returns True when it is OK to click."""
    ts_timeout = int(os.environ.get("MFP_TURNSTILE_TIMEOUT") or "60")
    deadline = min(time.monotonic() + ts_timeout, overall_deadline)
    polls = 0
    while True:
        present = turnstile_present(page)
        solved = present and turnstile_solved(page)
        enabled = button_enabled(submit)
        if present:
            tracker.note("turnstile", "turnstile solved" if solved else "turnstile pending")
        tracker.note("button", "button enabled" if enabled else "button disabled")
        if (solved or not present) and enabled:
            return True
        if present and not solved and polls >= 5 and turnstile_iframe_visible(page):
            # Interactive checkbox: give the user the whole login budget.
            tracker.ask_click()
            deadline = overall_deadline
        if time.monotonic() >= deadline:
            if enabled:
                say("turnstile wait expired but the button is enabled; submitting")
                return True
            say("turnstile pending at timeout" if present else "button still disabled at timeout")
            if present:
                tracker.last = "turnstile pending"
            return False
        page.wait_for_timeout(1000)
        polls += 1


def type_into(page, box, value):
    """Type like a person (Turnstile scores fast fills as bots). The value
    travels only over Playwright's pipe."""
    try:
        box.fill("")
        box.press_sequentially(value, delay=60)
    except AttributeError:
        box.fill(value)


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
    start = time.monotonic()
    deadline = start + timeout
    tracker = Tracker()
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    if has_session(context) and not on_login_page(page):
        say("already logged in (profile has a live session); not filling the form")
        return
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

    type_into(page, email_box, email)
    page.wait_for_timeout(700)
    type_into(page, pw_box, password_source())
    page.wait_for_timeout(1500)

    if not wait_for_turnstile(page, submit, deadline, tracker):
        raise LoginError(EXIT_TIMEOUT, f"no MFP session after {timeout}s (last state: {tracker.last})")
    submit.click()

    told_challenge = False
    ts_after_submit = False
    resubmits = 0
    while True:
        if has_session(context) and not on_login_page(page):
            return
        banner = error_banner(page)
        if banner:
            say(f"error banner: {banner}")
            raise LoginError(EXIT_REJECTED, "MFP rejected the email/password")
        login_page = on_login_page(page)
        present = login_page and turnstile_present(page)
        solved = present and turnstile_solved(page)
        if present and not solved:
            # Turnstile came back after the click (post-submit challenge).
            ts_after_submit = True
            tracker.note("state", "turnstile pending")
            if turnstile_iframe_visible(page):
                tracker.ask_click()
        elif present and ts_after_submit and resubmits < 1 and button_enabled(submit):
            say("turnstile solved again; submitting once more")
            tracker.note("state", "turnstile solved")
            resubmits += 1
            ts_after_submit = False
            submit.click()
        elif login_page and not button_enabled(submit):
            tracker.note("state", "button disabled")
        elif looks_like_challenge(page):
            if not told_challenge:
                say(f"Waiting for you to finish the challenge in the browser window… (timeout {timeout}s)")
                told_challenge = True
            tracker.note("state", "challenge")
        elif login_page:
            tracker.note("state", "still on login page")
        else:
            tracker.note("state", "waiting for session")
        if time.monotonic() >= deadline:
            raise LoginError(EXIT_TIMEOUT, f"no MFP session after {timeout}s (last state: {tracker.last})")
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
