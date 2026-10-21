"""Test double for myfitnesspal_mcp.cli.main: records argv; for `auth --check`
builds a curl session from the saved cookies and makes one MFP request (and,
with FAKE_URL, one to another host)."""
import os
import sys


def main():
    args = sys.argv[1:]
    with open(os.environ["FAKE_CLI_LOG"], "a") as fh:
        fh.write("cli argv0=%s %s\n" % (os.path.basename(sys.argv[0]), " ".join(args)))
    if args[:2] == ["auth", "--check"]:
        from curl_cffi import requests

        from . import auth

        s = requests.Session(impersonate="chrome")
        for name, value in (auth.load_cookies() or {}).items():
            s.cookies.set(name, value)
        s.get("https://www.myfitnesspal.com/user/auth_token?refresh=true")
        if os.environ.get("FAKE_URL"):
            s.get(os.environ["FAKE_URL"])
        print("Status:       valid, connected as fakeuser")
        raise SystemExit(int(os.environ.get("FAKE_CHECK_RC") or "0"))
