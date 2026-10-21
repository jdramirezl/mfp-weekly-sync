"""Test double for patchright.sync_api: the fake Playwright, plus a check that
the launch uses the undetected configuration (installed Chrome, headed, no
viewport, and nothing else that could fingerprint automation)."""
from playwright.sync_api import *  # noqa: F401,F403
from playwright.sync_api import Chromium as _Chromium, TargetClosedError, TimeoutError, rec  # noqa: F401

ALLOWED = {"user_data_dir", "channel", "headless", "no_viewport"}


class Chromium(_Chromium):
    def launch_persistent_context(self, user_data_dir=None, **kw):
        for k in sorted(set(kw) - ALLOWED):
            rec(f"bad-launch-kwarg {k}")
        rec("launch channel={} headless={} no_viewport={}".format(
            kw.get("channel"), kw.get("headless"), kw.get("no_viewport")))
        return super().launch_persistent_context(user_data_dir=user_data_dir,
                                                 headless=kw.get("headless", True))


class PW:
    chromium = Chromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sync_playwright():
    return PW()
