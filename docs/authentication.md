# Authentication

Authentication is needed only for the live backends (`myfitnesspal`,
`cookie-session`). The default `mock` backend needs no credentials.

Credentials never leave your machine. A local coding agent driving this CLI
does **not** need — and should never be given — your MyFitnessPal password,
tokens, or cookies. The agent generates plan files and runs commands; the CLI
reads credentials/session from your local machine only.

## Which backend should I use?

| Your login method | Backend | How it authenticates |
|---|---|---|
| MyFitnessPal username + password | `myfitnesspal` | native mobile OAuth (mfp-api) |
| Google / Apple / other SSO (no password) | `cookie-session` | your existing Chrome browser session (macOS) |
| offline / testing | `mock` | none |

If you sign in with "Continue with Google" (or Apple), you have **no
MyFitnessPal password**, so the `myfitnesspal` backend cannot work — use
`cookie-session`.

## cookie-session backend (SSO / Google login) — LIVE VERIFIED

For SSO accounts, this backend reuses the authenticated session you already
have in Chrome:

1. Install the extra: `pip install -e ".[cookie]"` (needs `cryptography`).
2. Log into myfitnesspal.com in Chrome (Google login is fine).
3. Run `mfp-sync --backend cookie-session doctor`.
   - The first run triggers a **macOS Keychain prompt** ("… wants to use
     information stored in your keychain", referencing *Chrome Safe Storage*).
     Click **Always Allow**. This lets the tool decrypt Chrome's cookie store
     locally; cookie values are never printed or sent anywhere but
     MyFitnessPal's own API.

How it works: the tool extracts the `__Secure-next-auth.session-token` /
`_mfp_session` cookies from Chrome, exchanges them at
`GET www.myfitnesspal.com/user/auth_token?refresh=true` for a bearer token, and
then calls the same `api.myfitnesspal.com/v2` endpoints the web client uses
(client id `mfp-main-js`).

Requirements and limits:
- **macOS + Chrome** for the auto-extraction path (Arc/Brave/Edge/etc. share the
  same mechanism and are supported by the extractor, but Chrome is the default).
- Only **v10/v11** cookies can be decrypted. The newest Chrome "app-bound" (v20)
  cookies are not supported; if you hit that, use a browser whose MFP cookies
  are still v10/v11, or the `myfitnesspal` backend if you have a password.
- Sessions expire; when a call returns 401 the tool reports `SESSION_EXPIRED` —
  just reload myfitnesspal.com in Chrome and re-run.

### Account-specific meal names (why "snack" matters)

MyFitnessPal lets you rename/add meals. The diary API resolves `meal_name`
against **your account's own list** — sending a name it doesn't recognize makes
the server silently file the entry under **Dinner**. Live testing found this
account uses `["Breakfast","Lunch","Dinner","Snacks Morning","Snacks Noon",
"Snacks Night"]` — so literal "Snacks" was being coerced to Dinner. The
cookie-session backend now fetches your configured meal names and maps the
plan's `snack` category to your first real snack slot (e.g. "Snacks Morning").
If your account has a single "Snacks" meal, it maps there.

## How the `myfitnesspal` (password) backend authenticates

The `myfitnesspal` backend uses [`mfp-api`](https://github.com/Rift-Walker/mfp-api),
which implements MyFitnessPal's native mobile login (Under Armour UACF
Identity): your username/password are wrapped in a signed JWT and exchanged for
a short-lived bearer token plus a refresh token. This is the same flow the
official mobile app uses. It is **not** an officially published API and can
change without notice.

### Mobile identity API vs. website NextAuth (important)

There are **two different** MyFitnessPal auth surfaces, and they are not
interchangeable:

| | Website (`www.myfitnesspal.com`) | Mobile identity API (`identity-api.myfitnesspal.com`) |
|---|---|---|
| Mechanism | NextAuth session cookies + CSRF | Under Armour OAuth → bearer token |
| Used by | `python-myfitnesspal`, `myfitnesspal-mcp-python` | **`mfp-api` (this tool's live backend)** |
| Login input | browser session cookies | username + password → signed JWT |

The `myfitnesspal-mcp-python` project documents (in its own source) that
**MyFitnessPal's website moved to a NextAuth backend, so the legacy
username/password *form* POST to `www.myfitnesspal.com/account/login` no longer
logs you in** — it just returns a fresh CSRF cookie. That project's workaround is
to reuse a browser session cookie.

**That change affects the website form login, not the mobile OAuth flow this
tool uses.** During live validation we probed the mobile identity API's
credential-free steps (the app-level `client_credentials` grant and the
`/clientKeys` signing-key fetch) and both succeeded (HS512 signing key
returned). So `mfp-api`'s login path is viable at the transport level; only the
credentialed steps (signing the user JWT and exchanging it for a user token)
remain unverified until real credentials are supplied.

`mfp-sync doctor` reflects this: with no credentials set it still probes the
mobile identity API and reports "mobile identity API reachable; credentials not
configured" (or a clear failure if the API itself is unreachable/changed).

### If the mobile OAuth flow is ever rejected for your account

Because browser cookies and `mfp-api`'s bearer tokens are different auth models,
a browser session **cannot** directly drive this tool's live backend. If
`mfp-sync doctor` reports an auth failure that is not just missing credentials
(e.g. the user-token exchange is refused, or an MFA/CAPTCHA challenge appears):

1. Keep using the `mock` backend, or
2. Use the cookie-based `myfitnesspal-mcp-python` server for live writes (it
   reads a browser session you establish by logging into myfitnesspal.com in a
   Chromium browser), or
3. Wait for `mfp-api` to adapt. We do **not** bypass MFA/CAPTCHA.

## Setup

1. Install the live backend dependency (not on PyPI):

   ```bash
   pip install "git+https://github.com/Rift-Walker/mfp-api"
   ```

2. Provide credentials via environment variables (preferred) or a local `.env`:

   ```bash
   cp .env.example .env
   # edit .env:
   #   MFP_USERNAME=you@example.com
   #   MFP_PASSWORD=your-password
   #   MFP_SYNC_BACKEND=myfitnesspal
   ```

   Then load them into your shell before running (the CLI reads the process
   environment; it does not auto-parse `.env`):

   ```bash
   set -a; source .env; set +a
   ```

3. Verify, read-only:

   ```bash
   mfp-sync --backend myfitnesspal doctor
   ```

   This performs a read-only probe (today's diary). It never writes.

## Sessions and expiry

The token is short-lived; `mfp-api` holds a refresh token and refreshes
automatically within a run. If a session expires between runs you simply run the
command again — a fresh login happens from your stored credentials. If login
starts failing with an auth error, re-check `MFP_USERNAME`/`MFP_PASSWORD` and
run `mfp-sync doctor`.

## Secret hygiene

- `.env`, `config/settings.toml`, `*.sqlite`, and `session.json` are
  `.gitignore`d. Do not commit them.
- Error messages are passed through a redactor that strips bearer tokens and
  password-looking values before they are shown or logged.
- The SQLite state stores only food/catalog/sync-history data — never
  credentials or tokens.
