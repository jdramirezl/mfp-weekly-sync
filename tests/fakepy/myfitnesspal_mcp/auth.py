"""Test double for myfitnesspal_mcp.auth: records what would be saved."""
import json
import os

SESSION_COOKIE = "__Secure-next-auth.session-token"


def save_cookies(cookies, username=None):
    with open(os.environ["FAKE_SAVE_FILE"], "w") as fh:
        json.dump({"cookies": cookies, "username": username}, fh)


def load_cookies():
    path = os.environ.get("FAKE_COOKIES_JSON")
    if not path or not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh).get("cookies")


def saved_username():
    return "jdr"
