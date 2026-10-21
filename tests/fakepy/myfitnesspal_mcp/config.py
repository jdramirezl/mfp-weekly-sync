"""Test double for myfitnesspal_mcp.config."""
import os
from pathlib import Path


def cookies_path():
    return Path(os.environ["FAKE_COOKIES_JSON"])
