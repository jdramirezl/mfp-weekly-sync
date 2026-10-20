"""Hand a harvested MFP session to Mason. Runs with MASON'S tool Python.

Reads {"cookies": {...}} from the 0600 file named by MFP_COOKIE_IN (written by
mfp_login.py in the patchright login venv), saves it with Mason's public
auth.save_cookies, then seeds Mason's own headless browser-profile with
refresh.seed_profile so Mason's reactive and weekly refresh keep working.
Mason must be stopped (login-mfp.sh guarantees it). Prints counts only.

Exit codes: 0 saved (seeding may have warned), 1 bad input, 2 save failed.
"""

import json
import os
import sys


def say(msg):
    print(f"[mfp-login] {msg}", flush=True)


def main():
    path = os.environ.get("MFP_COOKIE_IN", "")
    try:
        with open(path) as fh:
            cookies = json.load(fh).get("cookies") or {}
    except (OSError, ValueError):
        say("no harvested cookies to save (MFP_COOKIE_IN missing or unreadable)")
        return 1
    if not isinstance(cookies, dict) or "__Secure-next-auth.session-token" not in cookies:
        say("harvested cookies have no MFP session; nothing saved")
        return 1

    # Defence in depth (mfp_login.py and mfp-auth already drop them): a
    # __Host- cookie seeded with a Domain makes Chromium reject the whole
    # batch in refresh.seed_profile ("Invalid cookie fields").
    host = [n for n in cookies if n.startswith("__Host-")]
    for n in host:
        del cookies[n]
    if host:
        say(f"Dropped {len(host)} __Host- cookies.")

    from myfitnesspal_mcp import auth, refresh

    try:
        auth.save_cookies(cookies, username=os.environ.get("MFP_USERNAME") or None)
    except Exception as exc:
        say(f"saving the session failed: {type(exc).__name__}")
        return 2
    try:
        refresh.seed_profile(cookies)
        say(f"Saved {len(cookies)} cookies; Mason profile seeded.")
    except Exception as exc:
        say(f"Saved {len(cookies)} cookies; WARNING: seeding Mason's profile failed "
            f"({type(exc).__name__}); auto-refresh may not work until the next login")
    return 0


if __name__ == "__main__":
    sys.exit(main())
