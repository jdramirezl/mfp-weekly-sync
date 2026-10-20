"""Test double for playwright.sync_api, driven by environment variables.

FAKE_PW_MODE      success | already | challenge | rejected | timeout
FAKE_PW_LABELS    comma list of get_by_label() names present
FAKE_PW_CSS       comma list of CSS selectors present
FAKE_PW_ROLE      1 if a "Log in" role=button is present (default 1)
FAKE_PW_LOG       file recording goto/fill/click/launch calls
"""
import os
import re
import time

MODE = os.environ.get("FAKE_PW_MODE", "success")
LABELS = [x for x in os.environ.get("FAKE_PW_LABELS", "Email Address,Password").split(",") if x]
CSS = [x for x in os.environ.get(
    "FAKE_PW_CSS", "input[type=email],input[type=password],button[type=submit]").split(",") if x]
ROLE = os.environ.get("FAKE_PW_ROLE", "1") == "1"
SESSION = "__Secure-next-auth.session-token"


def rec(line):
    with open(os.environ["FAKE_PW_LOG"], "a") as fh:
        fh.write(line + "\n")


class State:
    url = "about:blank"
    submitted = False
    waits = 0
    logged_in = MODE == "already"


S = State()


class Locator:
    def __init__(self, desc, present, text=""):
        self.desc, self.present, self.text = desc, present, text

    def count(self):
        return 1 if self.present else 0

    @property
    def first(self):
        return self

    def is_visible(self):
        if self.desc == "[role=alert]":
            return MODE == "rejected" and S.submitted
        return self.present

    def inner_text(self):
        return self.text

    def fill(self, value):
        rec(f"fill {self.desc} {value}")

    def click(self):
        rec(f"click {self.desc} {self.text}")
        S.submitted = True
        if MODE == "success":
            S.logged_in = True
            S.url = "https://www.myfitnesspal.com/"
        elif MODE == "challenge":
            S.url = "https://www.myfitnesspal.com/account/verify?x=1"


class Page:
    @property
    def url(self):
        return S.url

    def goto(self, url, **kw):
        rec(f"goto {url}")
        S.url = "https://www.myfitnesspal.com/" if MODE == "already" else url

    def get_by_label(self, name, exact=False):
        return Locator(f"label={name}", name in LABELS)

    def get_by_role(self, role, name=None):
        return Locator("role-button", ROLE and bool(name.search("Log in")), "Log in")

    def locator(self, sel):
        if sel == "[role=alert]":
            return Locator(sel, MODE == "rejected" and S.submitted)
        return Locator(sel, sel in CSS, "Log in" if "button" in sel else "")

    def inner_text(self, sel):
        if MODE == "challenge" and S.submitted:
            return "Please complete the security challenge"
        if MODE == "rejected" and S.submitted:
            return "Incorrect email or password"
        return "Member Login Email Address Password Log in or Continue with Google"

    def wait_for_timeout(self, ms):
        S.waits += 1
        if MODE == "challenge" and S.waits >= 3:
            S.logged_in = True
            S.url = "https://www.myfitnesspal.com/"
        time.sleep(0.05)

    def screenshot(self, path):
        rec(f"screenshot {path}")
        open(path, "wb").close()


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
    def launch_persistent_context(self, path, headless=True):
        rec(f"launch {path} headless={headless}")
        return Context()


class PW:
    chromium = Chromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sync_playwright():
    return PW()
