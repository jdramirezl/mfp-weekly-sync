"""Test double for myfitnesspal_mcp.refresh: records the seeded cookie count."""
import os


def seed_profile(cookies):
    if os.environ.get("FAKE_SEED_FAIL") == "1":
        raise RuntimeError("the browser visit did not produce a MyFitnessPal session cookie")
    with open(os.environ["FAKE_SEED_FILE"], "w") as fh:
        fh.write(str(len(cookies)))
