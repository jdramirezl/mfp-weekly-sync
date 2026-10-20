"""Test double for playwright.sync_api, driven by environment variables.

FAKE_PW_MODE   success | already | challenge | rejected | spanish_rejected |
               timeout | turnstile | turnstile_interactive | turnstile_stuck |
               turnstile_widget_alert | post_submit_turnstile |
               turnstile_failed | turnstile_stuck_enabled | goto_timeout |
               manual | manual_empty
FAKE_PW_LABELS comma list of get_by_label() names present
FAKE_PW_CSS    comma list of CSS selectors present for the form fields/button
FAKE_PW_ROLE   1 if a "Log in" role=button is present (default 1)
FAKE_TS_POLLS  waits after the password is typed before Turnstile solves (3)
FAKE_PW_LOG    file recording launch/goto/fill/type/click calls

Time is simulated: every page.wait_for_timeout() is one "tick". Clicking the
submit button while it is disabled is recorded as "click-disabled", which the
tests treat as a bug (the old code clicked during Turnstile's "Verifying…").
"""
import os
import time

MODE = os.environ.get("FAKE_PW_MODE", "success")
LABELS = [x for x in os.environ.get("FAKE_PW_LABELS", "Email Address,Password").split(",") if x]
CSS = [x for x in os.environ.get(
    "FAKE_PW_CSS", "input[type=email],input[type=password],button[type=submit]").split(",") if x]
ROLE = os.environ.get("FAKE_PW_ROLE", "1") == "1"
TS_POLLS = int(os.environ.get("FAKE_TS_POLLS", "3"))
SESSION = "__Secure-next-auth.session-token"
HOME = "https://www.myfitnesspal.com/"
TS_MODES = {"turnstile", "turnstile_interactive", "turnstile_stuck",
            "turnstile_widget_alert", "post_submit_turnstile",
            "turnstile_failed", "turnstile_stuck_enabled"}
FAIL_POLLS = int(os.environ.get("FAKE_TS_FAIL_POLLS", "4"))  # turnstile_failed: polls showing "Error"


class TimeoutError(Exception):
    """Mirrors playwright.sync_api.TimeoutError."""

TS_INPUT = 'input[name="cf-turnstile-response"]'
TS_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'
TS_BOX = ".cf-turnstile"


def rec(line):
    with open(os.environ["FAKE_PW_LOG"], "a") as fh:
        fh.write(line + "\n")


class State:
    url = "about:blank"
    ticks = 0
    clicks = 0
    submitted = False
    submit_tick = 0
    logged_in = MODE in ("already", "manual")
    login_at = None  # tick at which a pending login completes
    ts_start = None  # tick when the password was typed
    ts_reset = None  # post_submit_turnstile: tick of the first submit


S = State()


def on_login():
    return "/account/login" in S.url


def ts_present():
    return MODE in TS_MODES and on_login()


def ts_failed_now():
    """turnstile_failed: the widget shows an error for FAIL_POLLS ticks after
    the password is typed; a retry (FAKE_TS_RETRY=1) then solves it."""
    return MODE == "turnstile_failed" and S.ts_start is not None and S.ticks - S.ts_start < FAIL_POLLS


def ts_solved():
    if MODE not in TS_MODES or MODE in ("turnstile_stuck", "turnstile_stuck_enabled"):
        return False
    if MODE == "turnstile_failed":
        return (os.environ.get("FAKE_TS_RETRY") == "1" and S.ts_start is not None
                and S.ticks - S.ts_start >= FAIL_POLLS + 1)
    if MODE == "post_submit_turnstile":
        return S.ts_reset is None or S.ticks >= S.ts_reset + TS_POLLS
    if S.ts_start is None:
        return False
    need = TS_POLLS + (6 if MODE == "turnstile_interactive" else 0)
    return S.ticks - S.ts_start >= need


def button_enabled():
    if MODE == "turnstile_stuck_enabled":
        return True  # the 09:00 race: looks clickable while Turnstile is unsolved
    return not ts_present() or ts_solved()


def log_in_now():
    S.logged_in = True
    S.url = HOME


def tick():
    S.ticks += 1
    if S.login_at is not None and S.ticks >= S.login_at:
        log_in_now()
    if MODE == "challenge" and S.submitted and S.ticks >= S.submit_tick + 3:
        log_in_now()


class El:
    """One element. kind: field | submit | ts_input | ts_iframe | box | text."""

    def __init__(self, kind, desc, text="", visible=True, in_ts=False):
        self.kind, self.desc, self.text = kind, desc, text
        self._visible, self.in_ts = visible, in_ts

    # Locator-ish API so .first / .nth(i) on an element still works.
    def count(self):
        return 1

    @property
    def first(self):
        return self

    def nth(self, i):
        return self

    def is_visible(self):
        if self.kind == "ts_iframe":
            return MODE in ("turnstile_interactive", "post_submit_turnstile") and not ts_solved()
        v = self._visible
        return v() if callable(v) else v

    def is_enabled(self):
        return button_enabled() if self.kind == "submit" else True

    def get_attribute(self, name):
        if self.kind == "submit" and not button_enabled():
            return "" if name == "disabled" else ("true" if name == "aria-disabled" else None)
        return None

    def inner_text(self):
        return self.text

    def input_value(self):
        if self.kind == "ts_input":
            return "TS-TOKEN-VALUE" if ts_solved() else ""
        return ""

    def evaluate(self, js, *args):
        if "closest" in js:
            return self.in_ts
        if "value" in js:
            return self.input_value()
        return None

    def _typed(self, verb, value):
        rec(f"{verb} {self.desc} {value}")
        if "assword" in self.desc and value:
            S.ts_start = S.ticks

    def fill(self, value):
        self._typed("fill", value)

    def press_sequentially(self, value, delay=0):
        self._typed("type", value)

    def click(self):
        if self.kind != "submit":
            rec(f"click {self.desc} {self.text}")
            return
        if not button_enabled():
            rec(f"click-disabled {self.desc}")
            return
        rec(f"click {self.desc} {self.text}")
        S.clicks += 1
        S.submitted = True
        S.submit_tick = S.ticks
        if MODE in ("success", "turnstile", "turnstile_interactive", "turnstile_failed",
                    "turnstile_stuck_enabled"):
            log_in_now()
        elif MODE == "turnstile_widget_alert":
            S.login_at = S.ticks + 2
        elif MODE == "challenge":
            S.url = "https://www.myfitnesspal.com/account/verify?x=1"
        elif MODE == "post_submit_turnstile":
            if S.clicks == 1:
                S.ts_reset = S.ticks
            else:
                log_in_now()


class Locator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    @property
    def first(self):
        return self.elements[0] if self.elements else El("text", "missing", visible=False)

    def nth(self, i):
        return self.elements[i]


def submit_el(desc):
    return El("submit", desc, "Log in")


class Page:
    @property
    def url(self):
        return S.url

    def goto(self, url, **kw):
        rec(f"goto {url}")
        if MODE == "goto_timeout":
            raise TimeoutError("Timeout 30000ms exceeded.")
        S.url = HOME if MODE in ("already", "manual") or not url.endswith("/login") else url

    def frame_locator(self, sel):
        return FrameLocator(sel)

    def get_by_label(self, name, exact=False):
        return Locator([El("field", f"label={name}")] if name in LABELS else [])

    def get_by_role(self, role, name=None):
        ok = ROLE and role == "button" and name is not None and bool(name.search("Log in"))
        return Locator([submit_el("role-button")] if ok else [])

    def locator(self, sel):
        if sel == TS_INPUT:
            return Locator([El("ts_input", sel, visible=False)] if ts_present() else [])
        if sel == TS_IFRAME:
            return Locator([El("ts_iframe", sel, in_ts=True)] if ts_present() else [])
        if sel == TS_BOX:
            return Locator([El("box", sel, in_ts=True)] if ts_present() else [])
        if sel == "[role=alert]":
            if MODE == "turnstile_widget_alert" and ts_present():
                # Turnstile's own status text: must never count as a rejection.
                return Locator([El("text", sel, "Verifying…", in_ts=True)])
            if MODE == "rejected":
                return Locator([El("text", sel, "Incorrect email or password", visible=lambda: S.submitted)])
            return Locator([])
        if sel == "[class*=error]" and MODE == "spanish_rejected":
            return Locator([El("text", sel, "Contraseña incorrecta", visible=lambda: S.submitted)])
        if sel in CSS:
            if "button" in sel:
                return Locator([submit_el(sel)])
            return Locator([El("field", sel)])
        return Locator([])

    def inner_text(self, sel):
        if MODE == "challenge" and S.submitted:
            return "Please complete the security challenge"
        if MODE == "turnstile_widget_alert":
            return "Member Login Email Address Password invalid-looking footer text Log in"
        return "Member Login Email Address Password Log in or Continue with Google"

    def wait_for_timeout(self, ms):
        tick()
        time.sleep(0.002)

    def screenshot(self, path):
        rec(f"screenshot {path}")
        open(path, "wb").close()


class FrameLocator:
    def __init__(self, sel):
        self.sel = sel

    def locator(self, sel):
        if self.sel == TS_IFRAME and ts_present():
            text = "Error: Verification failed" if ts_failed_now() else (
                "Success!" if ts_solved() else "Verifying…")
            return Locator([El("text", "ts-body", text, in_ts=True)])
        return Locator([])


class Context:
    def __init__(self):
        self.pages = [Page()]

    def cookies(self, url=None):
        base = [{"name": "_ga", "value": "GA1", "domain": ".myfitnesspal.com"},
                {"name": "other", "value": "x", "domain": ".example.com"}]
        if S.logged_in:
            base += [{"name": SESSION, "value": "SESSION-VALUE", "domain": "www.myfitnesspal.com"},
                     {"name": "__cf_bm", "value": "cf", "domain": ".myfitnesspal.com"}]
        return base

    def new_page(self):
        return self.pages[0]

    def close(self):
        rec("close")


class Chromium:
    def launch_persistent_context(self, user_data_dir=None, headless=True, **kw):
        rec(f"launch {user_data_dir} headless={headless}")
        return Context()


class PW:
    chromium = Chromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sync_playwright():
    return PW()
