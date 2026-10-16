"""Tests for the MCP server: tool execution, envelopes, and security gates.

All tool calls run against the mock backend (no network, no real account).
Skipped if the `mcp` extra is not installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mfp_sync.mcp_server import TOKEN_ENV, build_mcp, main  # noqa: E402

PLAN = {
    "schema_version": 1, "plan_id": "mcp-test",
    "days": [{"date": "2026-10-19", "meals": {
        "breakfast": [{"entry_id": "b1", "food": "Oatmeal, dry", "quantity": 40,
                       "unit": "g", "expected_calories": 156}],
        "lunch": [{"entry_id": "l1", "food": "Chicken breast, cooked", "quantity": 125,
                   "unit": "g", "mfp_food_id": "mock-chicken-cooked"}],
    }}],
}


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(PLAN))
    return p


@pytest.fixture
def mcp(tmp_path: Path):
    return build_mcp(default_backend="mock", default_home=str(tmp_path / "state"))


def _call(mcp, name, args):
    async def go():
        result = await mcp.call_tool(name, args)
        return result[1] if isinstance(result, tuple) and len(result) > 1 else result
    return asyncio.run(go())


def test_all_tools_registered(mcp):
    async def names():
        return {t.name for t in await mcp.list_tools()}
    got = asyncio.run(names())
    assert got == {"plan_schema", "doctor", "preview_plan", "preview_plan_json",
                   "status_plan", "resolve_plan", "export_diary",
                   "apply_plan", "apply_plan_json"}


def test_expose_mode_hides_file_path_tools(tmp_path):
    m = build_mcp(default_backend="mock", default_home=str(tmp_path / "s"), expose_mode=True)

    async def names():
        return {t.name for t in await m.list_tools()}
    got = asyncio.run(names())
    # File-path tools are gone; only inline-JSON + read-only remain.
    assert got == {"plan_schema", "doctor", "preview_plan_json",
                   "export_diary", "apply_plan_json"}
    assert "preview_plan" not in got
    assert "apply_plan" not in got
    assert "resolve_plan" not in got
    assert "status_plan" not in got


def test_expose_mode_inline_tools_still_work(tmp_path):
    m = build_mcp(default_backend="mock", default_home=str(tmp_path / "s"), expose_mode=True)
    r = _call(m, "preview_plan_json", {"plan": PLAN})
    assert r["ok"] is True and r["counts"].get("add") == 2
    r2 = _call(m, "apply_plan_json", {"plan": PLAN, "confirm": True})
    assert r2["applied"] is True and len(r2["result"]["added"]) == 2


def test_plan_schema_tool(mcp):
    r = _call(mcp, "plan_schema", {})
    assert r["ok"] is True
    assert r["schema"]["title"] == "MyFitnessPal Weekly Meal Plan"


def test_doctor_tool(mcp):
    r = _call(mcp, "doctor", {})
    assert r["ok"] is True
    assert r["backend"] == "mock"
    assert r["supports_stable_ids"] is True


def test_preview_tool(mcp, plan_file):
    r = _call(mcp, "preview_plan", {"plan_path": str(plan_file)})
    assert r["ok"] is True
    assert r["counts"].get("add") == 2
    assert all(a["action"] == "add" for a in r["actions"])


def test_apply_is_idempotent(mcp, plan_file):
    r1 = _call(mcp, "apply_plan", {"plan_path": str(plan_file)})
    assert r1["ok"] is True
    assert len(r1["result"]["added"]) == 2
    r2 = _call(mcp, "apply_plan", {"plan_path": str(plan_file)})
    assert r2["ok"] is True
    assert r2["result"]["added"] == []


def test_apply_blocks_on_unresolved(mcp, tmp_path):
    bad = tmp_path / "amb.json"
    bad.write_text(json.dumps({
        "schema_version": 1, "plan_id": "amb",
        "days": [{"date": "2026-10-19", "meals": {"lunch": [
            {"entry_id": "x", "food": "chicken breast", "quantity": 100, "unit": "g"}]}}],
    }))
    r = _call(mcp, "apply_plan", {"plan_path": str(bad)})
    assert r["ok"] is False
    assert r["error_code"] == "UNRESOLVED_FOODS"


def test_error_envelope_for_missing_plan(mcp):
    r = _call(mcp, "preview_plan", {"plan_path": "/nonexistent/plan.json"})
    assert r["ok"] is False
    assert r["error_code"] == "PLAN_NOT_FOUND"


def test_resolve_reports_candidates(mcp, tmp_path):
    amb = tmp_path / "amb.json"
    amb.write_text(json.dumps({
        "schema_version": 1, "plan_id": "amb",
        "days": [{"date": "2026-10-19", "meals": {"lunch": [
            {"entry_id": "x", "food": "chicken breast", "quantity": 100, "unit": "g"}]}}],
    }))
    r = _call(mcp, "resolve_plan", {"plan_path": str(amb)})
    assert r["ok"] is False
    assert r["error_code"] == "UNRESOLVED_FOODS"
    assert r["unresolved"][0]["food"] == "chicken breast"
    assert len(r["unresolved"][0]["candidates"]) >= 2


# -- JSON-object tools (findings #4 and #5) ----------------------------------


def test_preview_plan_json_accepts_object(mcp):
    r = _call(mcp, "preview_plan_json", {"plan": PLAN})
    assert r["ok"] is True
    assert r["counts"].get("add") == 2


def test_preview_plan_json_rejects_invalid_object(mcp):
    r = _call(mcp, "preview_plan_json", {"plan": {"schema_version": 1, "days": [
        {"date": "not-a-date", "meals": {}}]}})
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_PLAN"


def test_apply_plan_json_requires_confirmation(mcp):
    # confirm=False -> preview only, nothing written.
    r = _call(mcp, "apply_plan_json", {"plan": PLAN, "confirm": False})
    assert r["applied"] is False
    assert "counts" in r  # it returned a preview
    # Verify truly nothing was written: a subsequent confirm=True adds all.
    r2 = _call(mcp, "apply_plan_json", {"plan": PLAN, "confirm": True})
    assert r2["applied"] is True
    assert len(r2["result"]["added"]) == 2


def test_apply_plan_json_is_idempotent(mcp):
    r1 = _call(mcp, "apply_plan_json", {"plan": PLAN, "confirm": True})
    assert len(r1["result"]["added"]) == 2
    r2 = _call(mcp, "apply_plan_json", {"plan": PLAN, "confirm": True})
    assert r2["result"]["added"] == []


def test_apply_plan_json_blocks_on_unresolved(mcp):
    amb = {"schema_version": 1, "plan_id": "amb",
           "days": [{"date": "2026-10-19", "meals": {"lunch": [
               {"entry_id": "x", "food": "chicken breast", "quantity": 100, "unit": "g"}]}}]}
    r = _call(mcp, "apply_plan_json", {"plan": amb, "confirm": True})
    assert r["ok"] is False
    assert r["error_code"] == "UNRESOLVED_FOODS"


# -- security gates -----------------------------------------------------------


def test_http_transport_requires_token(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit) as ei:
        main(["--transport", "streamable-http"])
    assert ei.value.code != 0  # argparse error exit


def test_http_nonlocal_bind_refused_without_optin(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "x" * 32)
    monkeypatch.delenv("MFP_MCP_ALLOW_NONLOCAL_BIND", raising=False)
    with pytest.raises(SystemExit) as ei:
        main(["--transport", "streamable-http", "--host", "0.0.0.0"])
    assert ei.value.code != 0


def test_http_short_token_refused(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "short")
    with pytest.raises(SystemExit) as ei:
        main(["--transport", "streamable-http"])
    assert ei.value.code != 0


def test_token_middleware_rate_limits(tmp_path):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from mfp_sync.mcp_server import _add_token_gate

    m = build_mcp(default_backend="mock", default_home=str(tmp_path / "s"),
                  expose_mode=True)
    # rate_limit_per_min=0 -> even a correctly-authed request is 429, so the
    # limiter is exercised without reaching the (test-uninitialized) handler.
    _add_token_gate(m, "secret-token-at-least-24-chars", rate_limit_per_min=0)
    client = starlette_testclient.TestClient(m.streamable_http_app())
    hdr = {"Authorization": "Bearer secret-token-at-least-24-chars"}
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=hdr)
    assert r.status_code == 429
    assert r.json()["error_code"] == "RATE_LIMITED"
    # And a missing token is still 401 (auth checked before rate limit).
    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r2.status_code == 401


def test_token_middleware_rejects_without_bearer(tmp_path):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from mfp_sync.mcp_server import _add_token_gate

    m = build_mcp(default_backend="mock", default_home=str(tmp_path / "s"))
    _add_token_gate(m, "secret-token")
    client = starlette_testclient.TestClient(m.streamable_http_app())
    # No Authorization header -> 401 from our gate.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_FAILED"
    # Wrong token -> 401.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_token_gate_real_http_requests(tmp_path):
    """Finding #7: verify the gate over a REAL HTTP server (not just TestClient).

    Spins up uvicorn in a thread so the MCP session manager lifespan runs, then
    sends real requests: missing token -> 401, wrong token -> 401, correct
    token -> 200 with a JSON-RPC result (gate passed AND handler executed)."""
    import threading
    import time

    httpx = pytest.importorskip("httpx")
    uvicorn = pytest.importorskip("uvicorn")
    from mfp_sync.mcp_server import _add_token_gate

    token = "real-http-token-abc"
    m = build_mcp(default_backend="mock", default_home=str(tmp_path / "s"))
    _add_token_gate(m, token)
    app = m.streamable_http_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=8773, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        base = "http://127.0.0.1:8773/mcp"
        hdr = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        with httpx.Client(timeout=10) as c:
            assert c.post(base, json=init, headers=hdr).status_code == 401
            assert c.post(base, json=init,
                          headers={**hdr, "Authorization": "Bearer WRONG"}).status_code == 401
            ok = c.post(base, json=init,
                        headers={**hdr, "Authorization": f"Bearer {token}"})
            assert ok.status_code == 200
            assert "jsonrpc" in ok.text
    finally:
        server.should_exit = True
        thread.join(timeout=5)

