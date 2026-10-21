"""Test double for curl_cffi.requests.Session, driven by environment variables.

FAKE_ROTATE        "name=value;name2=" applied to the jar on every request; an
                   empty value removes the cookie (what max-age=0 does)
FAKE_STATUS        response status (default 200)
FAKE_NO_JAR        1 = Session.cookies has no .jar (to break the shim)
"""
import http.cookiejar
import os


def _cookie(name, value, domain):
    return http.cookiejar.Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."), "/", True,
        True, None, False, None, None, {})


class Cookies:
    def __init__(self):
        if os.environ.get("FAKE_NO_JAR") != "1":
            self.jar = http.cookiejar.CookieJar()

    def set(self, name, value, domain=".myfitnesspal.com"):
        if hasattr(self, "jar"):
            self.jar.set_cookie(_cookie(name, value, domain))

    def remove(self, name):
        for c in list(self.jar):
            if c.name == name:
                self.jar.clear(c.domain, c.path, c.name)


class Response:
    def __init__(self, url, status):
        self.url = url
        self.status_code = status


class Session:
    def __init__(self, impersonate=None):
        self.cookies = Cookies()
        self._store = {}

    def request(self, method, url, **kwargs):
        if os.environ.get("FAKE_NO_JAR") != "1":
            for pair in (os.environ.get("FAKE_ROTATE") or "").split(";"):
                if "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                if value:
                    self.cookies.set(name, value)
                else:
                    self.cookies.remove(name)
        return Response(url, int(os.environ.get("FAKE_STATUS") or "200"))

    def get(self, url, **kwargs):
        return self.request(method="GET", url=url, **kwargs)
