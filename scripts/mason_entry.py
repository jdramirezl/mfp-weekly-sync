"""Mason's entry point, with the cookie-rotation shim loaded first.

Run with Mason's tool Python, with the same arguments as `mfp-mcp`:
    python mason_entry.py --http --host 127.0.0.1 --port 8484
    python mason_entry.py auth --check
MFP_COOKIE_SHIM=0 skips the shim. Exit codes are Mason's own.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if os.environ.get("MFP_COOKIE_SHIM", "1") != "0":
    try:
        import mason_cookie_shim

        mason_cookie_shim.install()
    except Exception:  # the shim must never stop Mason from starting
        pass

from myfitnesspal_mcp.cli import main  # noqa: E402

sys.argv[0] = "mfp-mcp"
main()
