"""Log the bot into MyFitnessPal with its own email + password.

Runs with the patchright login venv's Python (NOT Mason's): patchright drives
the INSTALLED Google Chrome (channel="chrome", headed, no_viewport), which
passes Cloudflare Turnstile. Stock Playwright Chromium is flagged as
automation (navigator.webdriver, --enable-automation, the CDP Runtime.enable
leak), and on the server Mac Turnstile failed even when a human clicked.

It uses its own Chrome profile (MFP_LOGIN_PROFILE), never Mason's
browser-profile, and hands the harvested myfitnesspal.com cookies to the
wrapper through a 0600 JSON file (MFP_COOKIE_OUT). save_session.py (Mason's
Python) then calls Mason's auth.save_cookies and refresh.seed_profile.

Inputs (environment only):
  MFP_LOGIN_EMAIL             login email (also the Keychain account)
  MFP_LOGIN_KEYCHAIN_SERVICE  Keychain service name (default mfp-login)
  MFP_LOGIN_PROFILE           Chrome profile dir for logins (required)
  MFP_COOKIE_OUT              where to write {"cookies": {...}} (required)
  MFP_LOG_DIR                 where a failure screenshot goes (optional)
  MFP_LOGIN_TIMEOUT           seconds to wait for a session (default 180)
  MFP_TURNSTILE_TIMEOUT       seconds to wait for Cloudflare Turnstile before
                              clicking Log in (default 60; the whole login
                              budget if Turnstile shows an interactive box)
  MFP_MANUAL_WAIT             --manual: seconds to wait for the session (15)

--manual: the user already logged in by hand in real Chrome with this profile
(no CDP at all) and quit it. Only open the profile and harvest the cookies;
no Keychain read, no typing.

The password is read from the macOS Keychain here, kept in a local variable,
typed into the page over patchright's pipe, and never printed, logged or put
in an exception message.

Exit codes: 0 ok, 4 no Keychain item, 5 MFP showed an explicit credentials
error after a clean submit (both field lengths verified right before the
click: the password is probably wrong), 6 timed out (Turnstile/captcha/2FA or
a cookie-consent banner not cleared, fields not accepted, or a browser
TimeoutError), 7 anything else, 9 MFP rejected the login after a
cookie-consent banner or a field retype (inputs may have been lost; not
evidence of a wrong password).
"""

import json
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
# Turnstile's own failure text ("Error", "Verification failed", Spanish too).
TS_FAILED_RE = re.compile(
    r"\b(error|failed|failure|try again|verification failed|fall[oó]|intent[ae])\b", re.I)
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
EXIT_REJECTED_AFTER_CONSENT = 9

# Set once the login form has been submitted. On the server Mac, Chrome's
# "save password" / "translate" popups and the post-login navigation to /es
# closed the page right after a SUCCESSFUL submit (TargetClosedError, exit 7)
# although MFP had already logged in.
SUBMIT_STATE = {"submitted": False}
CLOSED_RE = re.compile(r"target page, context or browser has been closed|target closed|has been closed", re.I)

# Written into the login profile's Default/Preferences before launch (never as
# launch args, which can leak automation): no password-save or translate bars.
CHROME_PREFS = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False},
    "translate": {"enabled": False},
}
TRANSLATE_BLOCKED = ["en", "es"]

# Cookie-consent banners (OneTrust / Didomi / generic). On the server Mac a
# banner took focus or swallowed the typed fields, and the empty submit came
# back as "Incorrect username or password".
CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#didomi-notice-agree-button",
    "button#accept-recommended-btn-handler",
    "[data-testid*=accept i]",
]
CONSENT_BUTTON_RE = re.compile(
    r"^\s*(accept( all)?( cookies)?|allow all|agree|i agree|got it|ok|"
    r"aceptar( todo| todas)?|acepto|permitir todo)\s*$", re.I)
CONSENT_NEVER_RE = re.compile(
    r"reject|rechazar|manage|settings|preferences|personalizar|configurar", re.I)
CONSENT_IFRAMES = ('iframe[id*=sp_message]', 'iframe[title*=consent i]')
MAX_RETYPES = 2


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


def turnstile_failed_text(page):
    """Turnstile's widget text if it reports an error/failure, else None.
    Every read is best effort: the widget lives in a cross-origin iframe."""
    texts = []
    try:
        texts.append(page.frame_locator(TS_IFRAME).locator("body").first.inner_text() or "")
    except Exception:
        pass
    try:
        loc = page.locator(TS_BOX)
        if loc.count() > 0:
            texts.append(loc.first.inner_text() or "")
    except Exception:
        pass
    for text in texts:
        text = re.sub(r"\s+", " ", text).strip()
        if text and TS_FAILED_RE.search(text):
            return text[:40]
    return None


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
        self.consent_seen = False  # a consent banner was found at any point
        self.consent_stuck_told = False
        self.retyped = False  # any field had to be retyped
        self.retypes = {"email": 0, "password": 0}

    def note(self, key, state):
        if self.seen.get(key) != state:
            self.seen[key] = state
            say(state)
        self.last = state

    def ts_failed(self, text):
        if "ts_failed" not in self.seen:
            self.seen["ts_failed"] = text
            say(f"turnstile failed: {text}")
        self.last = "turnstile failed"

    def ask_click(self):
        if not self.asked:
            self.asked = True
            say(CLICK_BOX_MSG)


def wait_for_turnstile(page, submit, overall_deadline, tracker):
    """Block until Turnstile (if any) has a token and the button is enabled.
    Returns True when it is OK to click. Never True while Turnstile is
    present but unsolved, even if the button looks enabled (that race made
    the unattended 09:00 re-login submit too early)."""
    ts_timeout = int(os.environ.get("MFP_TURNSTILE_TIMEOUT") or "60")
    deadline = min(time.monotonic() + ts_timeout, overall_deadline)
    polls = 0
    while True:
        present = turnstile_present(page)
        solved = present and turnstile_solved(page)
        enabled = button_enabled(submit)
        failed = None
        if present and not solved:
            failed = turnstile_failed_text(page)
            if failed:
                tracker.ts_failed(failed)
                tracker.ask_click()
                deadline = overall_deadline  # the user can retry the box
            else:
                tracker.note("turnstile", "turnstile pending")
        elif present:
            tracker.note("turnstile", "turnstile solved")
        tracker.note("button", "button enabled" if enabled else "button disabled")
        if (solved or not present) and enabled:
            return True
        if present and not solved and polls >= 5 and turnstile_iframe_visible(page):
            # Interactive checkbox: give the user the whole login budget.
            tracker.ask_click()
            deadline = overall_deadline
        if time.monotonic() >= deadline:
            if not present and enabled:
                return True
            if present:
                say("turnstile pending at timeout")
                tracker.last = "turnstile failed" if failed else "turnstile pending"
            else:
                say("button still disabled at timeout")
                tracker.last = "button disabled"
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


def _consent_candidates(page):
    """(description, locator-factory) pairs for every consent accept button."""
    out = [(sel, (lambda sel=sel: page.locator(sel))) for sel in CONSENT_SELECTORS]
    out.append(("role-button accept",
                lambda: page.get_by_role("button", name=CONSENT_BUTTON_RE)))
    for frame in CONSENT_IFRAMES:
        out.append((f"{frame} accept",
                    (lambda frame=frame: page.frame_locator(frame).get_by_role(
                        "button", name=CONSENT_BUTTON_RE))))
    return out


def _visible_first(make):
    try:
        loc = make()
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    return None


def dismiss_consent(page, tracker):
    """Click a visible cookie-consent accept button. Returns "none" (no banner),
    "dismissed", or "stuck" (a banner is there but no click cleared it).
    Never clicks reject/settings or anything SSO-looking."""
    found = False
    for desc, make in _consent_candidates(page):
        button = _visible_first(make)
        if button is None:
            continue
        try:
            text = button.inner_text() or ""
        except Exception:
            text = ""
        if CONSENT_NEVER_RE.search(text) or SSO_WORDS.search(text):
            continue
        found = True
        tracker.consent_seen = True
        try:
            button.click()
            page.wait_for_timeout(1000)
        except Exception:
            continue
        if _visible_first(make) is None:
            say(f"consent banner dismissed ({desc})")
            return "dismissed"
    if found:
        if not tracker.consent_stuck_told:
            tracker.consent_stuck_told = True
            say("consent banner present but could not be dismissed; accept it in the browser window")
        tracker.last = "consent banner"
        return "stuck"
    return "none"


def clear_consent(page, tracker, deadline, timeout):
    """Dismiss any consent banner; if it can't be dismissed, wait (the user can
    click it in the headed window) without typing, until the deadline."""
    while dismiss_consent(page, tracker) == "stuck":
        if time.monotonic() >= deadline:
            raise LoginError(EXIT_TIMEOUT, f"no MFP session after {timeout}s (last state: consent banner)")
        page.wait_for_timeout(1000)


def field_length(box):
    try:
        return len(box.input_value() or "")
    except Exception:
        return -1


def verify_fields(page, tracker, fields, deadline, timeout):
    """fields: [(name, box, value)]. Compare typed lengths (never values) and
    retype a field that lost its input, at most MAX_RETYPES times each."""
    for name, box, value in fields:
        while True:
            got = field_length(box)
            if got == len(value):
                tracker.note(f"len-{name}", f"{name} field length ok ({got})")
                break
            say(f"{name} field length mismatch (got {max(got, 0)}, expected {len(value)})")
            tracker.seen.pop(f"len-{name}", None)
            if tracker.retypes[name] >= MAX_RETYPES:
                tracker.last = "fields not accepted"
                raise LoginError(EXIT_TIMEOUT, f"no MFP session after {timeout}s (last state: fields not accepted)")
            tracker.retypes[name] += 1
            tracker.retyped = True
            clear_consent(page, tracker, deadline, timeout)
            try:
                box.click()
            except Exception:
                pass
            type_into(page, box, value)
            page.wait_for_timeout(500)


def login(context, page, email, password_source, timeout):
    start = time.monotonic()
    deadline = start + timeout
    tracker = Tracker()
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    if has_session(context) and not on_login_page(page):
        say("already logged in (profile has a live session); not filling the form")
        return
    clear_consent(page, tracker, deadline, timeout)
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
    clear_consent(page, tracker, deadline, timeout)
    type_into(page, pw_box, password_source())
    page.wait_for_timeout(1500)
    fields = [("email", email_box, email), ("password", pw_box, password_source())]
    verify_fields(page, tracker, fields, deadline, timeout)

    if not wait_for_turnstile(page, submit, deadline, tracker):
        raise LoginError(EXIT_TIMEOUT, f"no MFP session after {timeout}s (last state: {tracker.last})")
    # A late banner or a re-render can still clear the form: check right before
    # the click, so a rejection after this point is about the credentials.
    clear_consent(page, tracker, deadline, timeout)
    verify_fields(page, tracker, fields, deadline, timeout)
    fields = None
    SUBMIT_STATE["submitted"] = True  # from here a closed target means "check the session"
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
            if tracker.consent_seen or tracker.retyped:
                say("rejected after consent banner; likely lost input, not a wrong password")
                raise LoginError(EXIT_REJECTED_AFTER_CONSENT,
                                 "MFP rejected the login after a consent-banner/field problem")
            raise LoginError(EXIT_REJECTED, "MFP rejected the email/password")
        login_page = on_login_page(page)
        if login_page:
            dismiss_consent(page, tracker)
        present = login_page and turnstile_present(page)
        solved = present and turnstile_solved(page)
        if present and not solved:
            # Turnstile came back after the click (post-submit challenge).
            ts_after_submit = True
            failed = turnstile_failed_text(page)
            if failed:
                tracker.ts_failed(failed)
            else:
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


def _deep_merge(dst, src):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def write_chrome_prefs(profile):
    """Merge CHROME_PREFS into <profile>/Default/Preferences, atomically.
    Corrupt JSON is kept as Preferences.bak and replaced."""
    default = Path(profile) / "Default"
    default.mkdir(parents=True, exist_ok=True)
    path = default / "Preferences"
    prefs = {}
    if path.exists():
        try:
            prefs = json.loads(path.read_text() or "{}")
            if not isinstance(prefs, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            try:
                os.replace(path, default / "Preferences.bak")
            except OSError:
                pass
            prefs = {}
    _deep_merge(prefs, json.loads(json.dumps(CHROME_PREFS)))
    blocked = prefs.get("translate_blocked_languages")
    if not isinstance(blocked, list):
        blocked = []
    for lang in TRANSLATE_BLOCKED:
        if lang not in blocked:
            blocked.append(lang)
    prefs["translate_blocked_languages"] = blocked
    tmp = default / "Preferences.mfp-mason.tmp"
    tmp.write_text(json.dumps(prefs))
    os.replace(tmp, path)
    say("chrome prefs: password manager off, translate off")


def is_target_closed(exc, closed_type):
    if closed_type is not None and isinstance(exc, closed_type):
        return True
    return bool(CLOSED_RE.search(str(exc) or ""))


def recheck_session(pw, context, launch, closed_type):
    """After the target closed post-submit: find or open a live page, load MFP
    and wait for the session cookie. Relaunches the context once if it is
    gone. Returns (context, page)."""
    wait = int(os.environ.get("MFP_RECHECK_WAIT") or "30")
    say("page closed after submit; re-checking the session")
    for attempt in (1, 2):
        try:
            pages = [p for p in context.pages if not _is_closed(p)]
            page = pages[-1] if pages else context.new_page()
            page.goto(MFP_URL, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + wait
            while not has_session(context):
                if time.monotonic() >= deadline:
                    raise LoginError(EXIT_TIMEOUT, "no MFP session after the page closed "
                                     "(last state: page closed after submit)")
                page.wait_for_timeout(1000)
            return context, page
        except LoginError:
            raise
        except Exception as exc:
            if attempt == 2 or not is_target_closed(exc, closed_type):
                raise
            say("browser context closed after submit; relaunching once")
            try:
                context.close()
            except Exception:
                pass
            context = launch()
    raise LoginError(EXIT_TIMEOUT, "no MFP session (last state: page closed after submit)")


def _is_closed(page):
    try:
        return bool(page.is_closed())
    except Exception:
        return False


def harvest(context, out_path):
    cookies = {
        c["name"]: c["value"]
        for c in context.cookies(MFP_URL)
        if "myfitnesspal.com" in (c.get("domain") or "")
    }
    if SESSION_COOKIE not in cookies:
        return 0
    # __Host- cookies (the NextAuth CSRF token) must have no Domain, but Mason
    # seeds its profile with Domain=.myfitnesspal.com and Chromium then rejects
    # the whole batch. Not needed for the session.
    host = [n for n in cookies if n.startswith("__Host-")]
    for n in host:
        del cookies[n]
    if host:
        say(f"Dropped {len(host)} __Host- cookies.")
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"cookies": cookies}, fh)
    os.chmod(out_path, 0o600)
    return len(cookies)


def manual_harvest(context, page):
    """--manual: the user logged in by hand in this profile; just wait for the
    session cookie to be readable."""
    wait = int(os.environ.get("MFP_MANUAL_WAIT") or "15")
    page.goto(MFP_URL, wait_until="domcontentloaded", timeout=30000)
    deadline = time.monotonic() + wait
    while not has_session(context):
        if time.monotonic() >= deadline:
            raise LoginError(
                EXIT_TIMEOUT,
                "no MFP session in the login profile; did you log in before closing Chrome?")
        page.wait_for_timeout(1000)


def main(argv):
    manual = "--manual" in argv[1:]
    email = os.environ.get("MFP_LOGIN_EMAIL", "").strip()
    service = os.environ.get("MFP_LOGIN_KEYCHAIN_SERVICE") or "mfp-login"
    profile = os.environ.get("MFP_LOGIN_PROFILE", "")
    out_path = os.environ.get("MFP_COOKIE_OUT", "")
    timeout = int(os.environ.get("MFP_LOGIN_TIMEOUT") or "180")
    missing = [n for n, v in (("MFP_LOGIN_PROFILE", profile), ("MFP_COOKIE_OUT", out_path)) if not v]
    if not manual and not email:
        missing.insert(0, "MFP_LOGIN_EMAIL")
    if missing:
        say(f"{', '.join(missing)} required")
        return EXIT_OTHER

    password = None
    if not manual:
        try:
            password = read_password(service, email)
        except LoginError as exc:
            say(str(exc))
            return exc.code

    import patchright.sync_api as patchright_api
    from patchright.sync_api import TimeoutError as PWTimeout
    from patchright.sync_api import sync_playwright
    closed_type = getattr(patchright_api, "TargetClosedError", None)

    Path(profile).mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    try:
        write_chrome_prefs(profile)
    except OSError as exc:
        say(f"could not write chrome prefs ({type(exc).__name__}); continuing")
    code = EXIT_OTHER
    SUBMIT_STATE["submitted"] = False
    with sync_playwright() as pw:
        def launch():
            # The undetected configuration: installed Chrome, headed, no
            # viewport override, and NO custom user agent, headers or args.
            return pw.chromium.launch_persistent_context(
                user_data_dir=profile, channel="chrome", headless=False, no_viewport=True)

        context = launch()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            try:
                if manual:
                    manual_harvest(context, page)
                else:
                    login(context, page, email, lambda: password, timeout)
            except Exception as exc:
                if SUBMIT_STATE["submitted"] and is_target_closed(exc, closed_type):
                    context, page = recheck_session(pw, context, launch, closed_type)
                else:
                    raise
            n = harvest(context, out_path)
            if not n:
                raise LoginError(EXIT_OTHER, "logged in, but no session cookie to save")
            say(f"final page {safe_path(page)}")
            say(f"Harvested {n} cookies.")
            code = EXIT_OK
        except LoginError as exc:
            say(str(exc))
            say(f"final page {safe_path(page)}")
            screenshot(page)
            code = exc.code
        except PWTimeout:
            say("browser timed out (page load or action); last state: browser timeout")
            screenshot(page)
            code = EXIT_TIMEOUT
        except Exception as exc:  # never include the password: it is not in exc
            say(f"login failed: {type(exc).__name__}")
            screenshot(page)
            code = EXIT_OTHER
        finally:
            password = None
            try:
                context.close()
            except Exception:
                pass
    return code


def safe_path(page):
    try:
        return url_path(page)
    except Exception:
        return "(page closed)"


if __name__ == "__main__":
    sys.exit(main(sys.argv))
