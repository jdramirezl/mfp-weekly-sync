"""MCP server exposing mfp-weekly-sync as agent tools.

Wraps the SAME engine the CLI uses (via mfp_sync.serialize), so tool output
matches the CLI's --json contract exactly. Nothing is duplicated.

Transports:
  * stdio (default)           — the client launches this as a local subprocess
                                (ChatGPT Desktop "STDIO", Claude Desktop, Cursor,
                                Kiro). Nothing is network-exposed. SAFE.
  * streamable-http / sse     — a local HTTP listener on 127.0.0.1 by default,
                                for use behind a tunnel (ChatGPT web + ngrok).

SECURITY
--------
This server can WRITE to your real MyFitnessPal diary. Therefore:

  * The HTTP transports bind to 127.0.0.1 (loopback) by default. Reaching them
    from another machine requires a tunnel YOU set up — that is an explicit,
    deliberate exposure, not the default.

  * When an HTTP transport is used, a bearer-token gate is REQUIRED and
    fail-closed: if MFP_MCP_TOKEN is not set, the server refuses to start an
    HTTP listener at all. Every HTTP request must send
    `Authorization: Bearer <token>`; anything else gets 401. This keeps random
    internet scanners that find your tunnel URL out of your diary.

  * stdio needs no token (there is no network listener to protect).

  * The token is read from the environment, never hard-coded, never logged.

Run:
    mfp-sync-mcp                              # stdio (default), backend from env
    MFP_SYNC_BACKEND=cookie-session mfp-sync-mcp
    MFP_MCP_TOKEN=$(openssl rand -hex 32) mfp-sync-mcp --transport streamable-http
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import hmac
import os
from typing import Any

from .errors import ErrorCode, MfpSyncError
from .planner import plan_json_schema
from .serialize import (
    EngineSession,
    apply_result_to_dict,
    build_settings,
    empty_result_dict,
    error_envelope,
    ok_envelope,
    plan_to_dict,
)

SERVER_NAME = "mfp-weekly-sync"
TOKEN_ENV = "MFP_MCP_TOKEN"
HTTP_TRANSPORTS = {"streamable-http", "sse"}


def _settings(backend: str | None, home: str | None, on_conflict: str | None):
    return build_settings(backend=backend, home=home, on_conflict=on_conflict)


def _preview(eng, plan, *, restore_deleted: bool) -> dict[str, Any]:
    """Build and serialize a sync plan (no mutations). Shared by both preview
    tools so file-path and JSON-object inputs behave identically."""
    sync_plan = eng.reconciler.build_plan(plan, restore_deleted=restore_deleted)
    return ok_envelope(**plan_to_dict(sync_plan))


def _apply(eng, plan, *, restore_deleted: bool) -> dict[str, Any]:
    """Reconcile + apply a plan through the existing safe writer engine.

    Agent-safe: refuses while any food is unresolved, and never overwrites a
    manual edit (confirm_conflict always returns False; set on_conflict via
    settings to change that deliberately). Shared by both apply tools."""
    sync_plan = eng.reconciler.build_plan(plan, restore_deleted=restore_deleted)

    unresolved = sync_plan.unresolved()
    if unresolved:
        return {
            "ok": False,
            "error_code": ErrorCode.UNRESOLVED_FOODS.value,
            "error": f"{len(unresolved)} entries unresolved; resolve them first",
            "unresolved": [a.source_entry_id for a in unresolved],
        }

    mutating = [a for a in sync_plan.actions if a.mutates]
    if not mutating:
        return ok_envelope(
            message="nothing to apply; diary already matches the plan",
            result=empty_result_dict(),
        )

    result = eng.reconciler.apply(sync_plan, confirm_conflict=lambda a: False)
    code = ErrorCode.OK.value if result.ok else ErrorCode.WRITE_UNCERTAIN.value
    return {"ok": result.ok, "error_code": code, "result": apply_result_to_dict(result)}


def _record_remote_deletion(eng, remote_id: str) -> None:
    eng.storage.mark_remote_deleted(remote_id)


def _guard(fn):
    """Wrap a tool body so any MfpSyncError becomes the standard error envelope.

    Uses functools.wraps so FastMCP still introspects the ORIGINAL signature
    (via __wrapped__) to build the tool's argument schema — a bare *args/**kwargs
    wrapper would make FastMCP think the tool takes 'args'/'kwargs'.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except MfpSyncError as exc:
            return error_envelope(exc)
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            return {"ok": False, "error_code": ErrorCode.UNKNOWN.value, "error": str(exc)}

    return wrapper


def build_mcp(
    *,
    default_backend: str | None = None,
    default_home: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    expose_mode: bool = False,
):
    """Construct the FastMCP app with tools registered.

    When `expose_mode` is True (used behind a public tunnel), the FILE-PATH
    tools (`preview_plan`, `status_plan`, `resolve_plan`, `apply_plan`) are NOT
    registered. They take a server-side filesystem path, which a remote caller
    (e.g. ChatGPT over a tunnel) cannot use anyway and which a leaked URL could
    abuse to read arbitrary files. Only the inline-JSON and read-only tools are
    exposed: `plan_schema`, `doctor`, `preview_plan_json`, `apply_plan_json`,
    `export_diary`.
    """
    from mcp.server.fastmcp import FastMCP
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        ts = TransportSecuritySettings(enable_dns_rebinding_protection=False) if expose_mode else None
    except ImportError:
        ts = None

    fastmcp_kwargs = {"host": host, "port": port}
    if ts is not None:
        fastmcp_kwargs["transport_security"] = ts

    mcp = FastMCP(
        SERVER_NAME,
        instructions=(
            "Prepopulate a MyFitnessPal food diary from a weekly meal plan. "
            "Call `plan_schema` to learn the plan JSON shape, `preview_plan_json` "
            "to see the exact operations for a plan object (no writes), then "
            "`apply_plan_json` with confirm=true to write. Re-running the same "
            "plan is idempotent — it never duplicates entries."
        ),
        **fastmcp_kwargs,
    )

    # File-path tools are registered only when NOT exposed. Use this decorator
    # on them so they vanish from the tool list in expose_mode.
    def file_tool():
        if expose_mode:
            return lambda fn: fn  # not registered as an MCP tool
        return mcp.tool()

    # -- read-only / informational tools ------------------------------------

    @mcp.tool()
    @_guard
    def plan_schema() -> dict[str, Any]:
        """Return the JSON Schema a weekly meal plan must conform to."""
        return ok_envelope(schema=plan_json_schema())

    @mcp.tool()
    @_guard
    def doctor(backend: str | None = None, home: str | None = None) -> dict[str, Any]:
        """Check configuration, auth, and read/write capability. Never writes."""
        settings = _settings(backend or default_backend, home or default_home, None)
        with EngineSession(settings) as eng:
            health = eng.backend.health_check()
            return ok_envelope(
                backend=health.backend,
                reachable=health.reachable,
                authenticated=health.authenticated,
                can_read=health.can_read,
                can_write=health.can_write,
                supports_stable_ids=health.supports_stable_ids,
                detail=health.detail,
            )

    @file_tool()
    @_guard
    def preview_plan(
        plan_path: str,
        backend: str | None = None,
        home: str | None = None,
        restore_deleted: bool = False,
    ) -> dict[str, Any]:
        """Show the exact sync operations for a plan FILE. Makes NO changes."""
        settings = _settings(backend or default_backend, home or default_home, None)
        with EngineSession(settings) as eng:
            return _preview(eng, eng.load(plan_path), restore_deleted=restore_deleted)

    @mcp.tool()
    @_guard
    def preview_plan_json(
        plan: dict[str, Any],
        backend: str | None = None,
        home: str | None = None,
        restore_deleted: bool = False,
    ) -> dict[str, Any]:
        """Show the exact sync operations for a plan JSON OBJECT (no file needed).

        `plan` is a complete meal-plan object matching the schema from
        `plan_schema`. Makes NO changes — ideal for an agent to validate and
        preview a plan it just generated in-memory.
        """
        settings = _settings(backend or default_backend, home or default_home, None)
        with EngineSession(settings) as eng:
            return _preview(eng, eng.load_dict(plan), restore_deleted=restore_deleted)

    @file_tool()
    @_guard
    def status_plan(
        plan_path: str, backend: str | None = None, home: str | None = None
    ) -> dict[str, Any]:
        """Compare the plan, local sync history, and the current diary."""
        settings = _settings(backend or default_backend, home or default_home, None)
        with EngineSession(settings) as eng:
            plan = eng.load(plan_path)
            sync_plan = eng.reconciler.build_plan(plan)
            rows = []
            for a in sync_plan.actions:
                rec = eng.storage.get_sync_record(sync_plan.plan_id, a.source_entry_id)
                rows.append({
                    "entry_id": a.source_entry_id,
                    "date": a.date.isoformat(),
                    "meal": a.meal,
                    "food": a.food_name,
                    "current_action": a.action.value,
                    "history_status": rec.status if rec else "never_synced",
                    "remote_entry_id": rec.remote_entry_id if rec else None,
                })
            return ok_envelope(plan_id=sync_plan.plan_id, entries=rows)

    @file_tool()
    @_guard
    def resolve_plan(
        plan_path: str, backend: str | None = None, home: str | None = None
    ) -> dict[str, Any]:
        """List foods that can't be auto-matched, with candidate choices.

        Read-only: it reports ambiguities so a human/agent can pick. It does not
        persist a choice (that is a deliberate human decision).
        """
        settings = _settings(backend or default_backend, home or default_home, None)
        with EngineSession(settings) as eng:
            plan = eng.load(plan_path)
            unresolved = []
            for _date, _cat, entry in plan.all_entries():
                res = eng.resolver.resolve(entry)
                if res.ok:
                    continue
                unresolved.append({
                    "entry_id": entry.entry_id,
                    "food": entry.food,
                    "status": res.status.value,
                    "message": res.message,
                    "candidates": [
                        {"food_id": c.food_id, "description": c.description, "brand": c.brand}
                        for c in res.candidates
                    ],
                })
            envelope = ok_envelope(unresolved=unresolved) if not unresolved else {
                "ok": False, "error_code": ErrorCode.UNRESOLVED_FOODS.value,
                "unresolved": unresolved,
            }
            return envelope

    @mcp.tool()
    @_guard
    def export_diary(
        from_date: str,
        to_date: str,
        backend: str | None = None,
        home: str | None = None,
    ) -> dict[str, Any]:
        """Export the diary for a date range (YYYY-MM-DD) as structured JSON."""
        settings = _settings(backend or default_backend, home or default_home, None)
        try:
            start = dt.date.fromisoformat(from_date)
            end = dt.date.fromisoformat(to_date)
        except ValueError as exc:
            raise MfpSyncError(f"invalid date: {exc}", code=ErrorCode.INVALID_PLAN) from exc
        with EngineSession(settings) as eng:
            days = []
            day = start
            while day <= end:
                entries = eng.backend.get_diary(day)
                days.append({
                    "date": day.isoformat(),
                    "entries": [
                        {"remote_id": e.remote_id, "meal": e.meal, "food_id": e.food_id,
                         "food": e.food_description, "servings": e.servings,
                         "serving_unit": e.serving_unit, "calories": e.calories,
                         "protein_g": e.protein_g, "carbs_g": e.carbs_g, "fat_g": e.fat_g}
                        for e in entries
                    ],
                })
                day += dt.timedelta(days=1)
            return ok_envelope(**{"from": from_date, "to": to_date, "days": days})

    # -- the one mutating tool ----------------------------------------------

    @file_tool()
    @_guard
    def apply_plan(
        plan_path: str,
        backend: str | None = None,
        home: str | None = None,
        restore_deleted: bool = False,
        on_conflict: str | None = None,
    ) -> dict[str, Any]:
        """Write a plan FILE to the diary. Idempotent; preserves manual changes.

        Conflicts (an entry you edited manually) are NEVER overwritten through
        this tool unless on_conflict='overwrite' is passed explicitly — the
        default is agent-safe (skip the conflict). Refuses to run while any food
        is unresolved (call resolve_plan first).
        """
        settings = _settings(backend or default_backend, home or default_home, on_conflict)
        with EngineSession(settings) as eng:
            return _apply(eng, eng.load(plan_path), restore_deleted=restore_deleted)

    @mcp.tool()
    @_guard
    def apply_plan_json(
        plan: dict[str, Any],
        confirm: bool,
        backend: str | None = None,
        home: str | None = None,
        restore_deleted: bool = False,
        on_conflict: str | None = None,
    ) -> dict[str, Any]:
        """Write a plan JSON OBJECT to the diary (no file needed).

        REQUIRES `confirm=true` — this is the one tool that mutates your real
        diary from an in-memory object, so the caller must opt in explicitly.
        With confirm=false (the default an agent should send first) it returns
        the preview and writes nothing, so an agent can show you the plan before
        asking you to confirm.

        Idempotent and manual-change-preserving via the same engine as the CLI:
        refuses while any food is unresolved, and never overwrites a manual edit
        unless on_conflict='overwrite' is set explicitly.
        """
        settings = _settings(backend or default_backend, home or default_home, on_conflict)
        with EngineSession(settings) as eng:
            meal_plan = eng.load_dict(plan)
            if not confirm:
                # Safety: no write without explicit confirmation. Return the
                # preview so the agent/user can review before confirming.
                preview = _preview(eng, meal_plan, restore_deleted=restore_deleted)
                preview["message"] = (
                    "confirm=false: nothing written. Review this preview, then call "
                    "again with confirm=true to apply."
                )
                preview["applied"] = False
                return preview
            result = _apply(eng, meal_plan, restore_deleted=restore_deleted)
            result["applied"] = result.get("ok", False)
            return result

    # -- deletion tools ----------------------------------------------------

    @mcp.tool()
    @_guard
    def delete_entry(
        remote_id: str,
        confirm: bool = False,
        backend: str | None = None,
        home: str | None = None,
    ) -> dict[str, Any]:
        """Delete a single diary entry by its UUID (remote_id).

        Requires `confirm=true` to perform the actual deletion. When confirm=false
        (default), it returns a safety confirmation request and deletes nothing.
        """
        settings = _settings(backend or default_backend, home or default_home, None)
        if not confirm:
            return ok_envelope(
                confirm_required=True,
                remote_id=remote_id,
                message=f"confirm=false: nothing deleted. To permanently delete "
                        f"entry '{remote_id}', call again with confirm=true.",
                deleted=False,
            )
        with EngineSession(settings) as eng:
            eng.backend.delete_entry(remote_id)
            _record_remote_deletion(eng, remote_id)
            return ok_envelope(
                deleted=True,
                remote_id=remote_id,
                message=f"Successfully deleted entry {remote_id}",
            )

    @mcp.tool()
    @_guard
    def delete_entries(
        remote_ids: list[str],
        confirm: bool = False,
        backend: str | None = None,
        home: str | None = None,
    ) -> dict[str, Any]:
        """Delete multiple diary entries by a list of UUIDs (remote_ids).

        Requires `confirm=true` to perform the deletions. When confirm=false
        (default), it previews the list and count to be deleted and makes NO changes.
        """
        settings = _settings(backend or default_backend, home or default_home, None)
        if not confirm:
            return ok_envelope(
                confirm_required=True,
                count=len(remote_ids),
                remote_ids=remote_ids,
                message=f"confirm=false: nothing deleted. Call again with "
                        f"confirm=true to permanently delete these "
                        f"{len(remote_ids)} entries.",
                deleted=False,
            )
        with EngineSession(settings) as eng:
            deleted: list[str] = []
            errors: list[dict[str, str]] = []
            for rid in remote_ids:
                try:
                    eng.backend.delete_entry(rid)
                    deleted.append(rid)
                    _record_remote_deletion(eng, rid)
                except Exception as exc:
                    errors.append({"remote_id": rid, "error": str(exc)})
            return ok_envelope(
                deleted=True,
                total_requested=len(remote_ids),
                deleted_count=len(deleted),
                deleted_ids=deleted,
                errors=errors,
                message=f"Deleted {len(deleted)} of {len(remote_ids)} entries.",
            )

    @mcp.tool()
    @_guard
    def delete_meal(
        date: str,
        meal: str,
        confirm: bool = False,
        backend: str | None = None,
        home: str | None = None,
    ) -> dict[str, Any]:
        """Delete all diary entries for a specific date (YYYY-MM-DD) and meal name
        (e.g. 'breakfast', 'lunch', 'dinner', 'snacks morning').

        Requires `confirm=true` to perform the deletions. When confirm=false
        (default), it previews the matching entries found and makes NO changes.
        """
        settings = _settings(backend or default_backend, home or default_home, None)
        try:
            day = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise MfpSyncError(f"invalid date: {exc}", code=ErrorCode.INVALID_PLAN) from exc

        with EngineSession(settings) as eng:
            all_entries = eng.backend.get_diary(day)
            target_meal = meal.strip().lower()
            matching = [
                e for e in all_entries
                if e.meal.strip().lower() == target_meal
                or target_meal in e.meal.strip().lower()
            ]

            summary = [
                {
                    "remote_id": e.remote_id,
                    "meal": e.meal,
                    "food": e.food_description,
                    "servings": e.servings,
                    "calories": e.calories,
                }
                for e in matching
            ]

            if not confirm:
                return ok_envelope(
                    confirm_required=True,
                    date=date,
                    meal=meal,
                    count=len(matching),
                    entries_to_delete=summary,
                    message=f"confirm=false: nothing deleted. Found {len(matching)} "
                            f"entries in '{meal}' on {date}. Call again with "
                            f"confirm=true to delete them.",
                    deleted=False,
                )

            deleted_ids: list[str] = []
            errors: list[dict[str, str]] = []
            for item in matching:
                if not item.remote_id:
                    continue
                try:
                    eng.backend.delete_entry(item.remote_id)
                    deleted_ids.append(item.remote_id)
                    _record_remote_deletion(eng, item.remote_id)
                except Exception as exc:
                    errors.append({"remote_id": item.remote_id, "error": str(exc)})

            return ok_envelope(
                deleted=True,
                date=date,
                meal=meal,
                deleted_count=len(deleted_ids),
                deleted_ids=deleted_ids,
                errors=errors,
                message=f"Deleted {len(deleted_ids)} entries from '{meal}' on {date}.",
            )

    @mcp.tool()
    @_guard
    def delete_day(
        date: str,
        confirm: bool = False,
        backend: str | None = None,
        home: str | None = None,
    ) -> dict[str, Any]:
        """Delete all diary entries across all meals for an entire day (YYYY-MM-DD).

        Requires `confirm=true` to perform the deletions. When confirm=false
        (default), it previews all entries found for that day and makes NO changes.
        """
        settings = _settings(backend or default_backend, home or default_home, None)
        try:
            day = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise MfpSyncError(f"invalid date: {exc}", code=ErrorCode.INVALID_PLAN) from exc

        with EngineSession(settings) as eng:
            all_entries = eng.backend.get_diary(day)
            summary = [
                {
                    "remote_id": e.remote_id,
                    "meal": e.meal,
                    "food": e.food_description,
                    "servings": e.servings,
                    "calories": e.calories,
                }
                for e in all_entries
            ]

            if not confirm:
                return ok_envelope(
                    confirm_required=True,
                    date=date,
                    count=len(all_entries),
                    entries_to_delete=summary,
                    message=f"confirm=false: nothing deleted. Found "
                            f"{len(all_entries)} entries on {date}. Call again with "
                            f"confirm=true to delete all entries for this day.",
                    deleted=False,
                )

            deleted_ids: list[str] = []
            errors: list[dict[str, str]] = []
            for item in all_entries:
                if not item.remote_id:
                    continue
                try:
                    eng.backend.delete_entry(item.remote_id)
                    deleted_ids.append(item.remote_id)
                    _record_remote_deletion(eng, item.remote_id)
                except Exception as exc:
                    errors.append({"remote_id": item.remote_id, "error": str(exc)})

            return ok_envelope(
                deleted=True,
                date=date,
                deleted_count=len(deleted_ids),
                deleted_ids=deleted_ids,
                errors=errors,
                message=f"Deleted {len(deleted_ids)} entries for date {date}.",
            )

    return mcp


# -- token gate for HTTP transports ------------------------------------------


def _add_token_gate(
    mcp, token: str, rate_limit_per_min: int = 60, allow_no_auth: bool = False
) -> None:
    """Install bearer-token + rate-limit middleware, along with RFC 8414 / RFC 9728
    OAuth 2.0 discovery and endpoints on the app for ChatGPT connector support.
    """
    import secrets
    import time as _time
    from collections import deque

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

    expected = f"Bearer {token}"
    hits: deque[float] = deque()

    def get_base_url(request):
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get(
            "x-forwarded-host", request.headers.get("host", request.url.netloc)
        )
        return f"{scheme}://{host}"

    async def oauth_auth_server_metadata(request):
        base_url = get_base_url(request)
        return JSONResponse({
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "scopes_supported": ["mfp:read", "mfp:write"],
        })

    async def oauth_protected_resource_metadata(request):
        base_url = get_base_url(request)
        return JSONResponse({
            "resource": f"{base_url}/mcp",
            "authorization_servers": [base_url],
            "scopes_supported": ["mfp:read", "mfp:write"],
        })

    async def oauth_authorize(request):
        redirect_uri = request.query_params.get("redirect_uri")
        state = request.query_params.get("state", "")
        code = "mfp_" + secrets.token_hex(16)
        if redirect_uri:
            sep = "&" if "?" in redirect_uri else "?"
            return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)
        return HTMLResponse("<html><body><h3>Authorized! You can close this window.</h3></body></html>")

    async def oauth_token(request):
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 31536000,
            "scope": "mfp:read mfp:write",
        })

    async def oauth_register(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse({
            "client_id": "chatgpt-mfp-client",
            "client_secret": "chatgpt-mfp-secret",
            "client_name": body.get("client_name", "ChatGPT"),
            "redirect_uris": body.get("redirect_uris", []),
            "token_endpoint_auth_method": "none",
        }, status_code=201)

    class _GateMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            # Let OAuth discovery, registration, authorize, and token routes bypass the bearer check
            if (
                path.startswith("/.well-known/")
                or path.startswith("/oauth/")
                or "/.well-known/" in path
            ):
                return await call_next(request)

            provided = request.headers.get("authorization", "")
            if not (allow_no_auth or (provided and hmac.compare_digest(provided, expected))):
                base_url = get_base_url(request)
                return JSONResponse(
                    {"ok": False, "error_code": "AUTH_FAILED",
                     "error": "missing or invalid bearer token"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate":
                            f'Bearer resource_metadata='
                            f'"{base_url}/.well-known/oauth-protected-resource"'
                    },
                )
            now = _time.monotonic()
            while hits and now - hits[0] > 60.0:
                hits.popleft()
            if len(hits) >= rate_limit_per_min:
                return JSONResponse(
                    {"ok": False, "error_code": "RATE_LIMITED",
                     "error": "too many requests; slow down"},
                    status_code=429,
                )
            hits.append(now)
            return await call_next(request)

    # FastMCP builds the Starlette app lazily; patch both HTTP app factories so
    # the middleware is present whichever HTTP transport is chosen.
    for attr in ("streamable_http_app", "sse_app"):
        factory = getattr(mcp, attr, None)
        if factory is None:
            continue

        def make_wrapped(orig):
            def wrapped(*a, **k):
                app = orig(*a, **k)
                if not allow_no_auth:
                    app.add_route(
                        "/.well-known/oauth-authorization-server",
                        oauth_auth_server_metadata,
                        methods=["GET"],
                    )
                    app.add_route(
                        "/.well-known/oauth-protected-resource",
                        oauth_protected_resource_metadata,
                        methods=["GET"],
                    )
                    app.add_route(
                        "/mcp/.well-known/oauth-protected-resource",
                        oauth_protected_resource_metadata,
                        methods=["GET"],
                    )
                    app.add_route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"])
                    app.add_route("/oauth/token", oauth_token, methods=["POST"])
                    app.add_route("/oauth/register", oauth_register, methods=["POST"])
                app.add_middleware(_GateMiddleware)
                return app
            return wrapped

        setattr(mcp, attr, make_wrapped(factory))


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(prog="mfp-sync-mcp", description=__doc__)
    parser.add_argument(
        "--transport", default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="stdio (default, local subprocess) or an HTTP transport (needs a token).",
    )
    parser.add_argument("--backend", default=None,
                        help="Default backend: mock|myfitnesspal|cookie-session "
                             "(env MFP_SYNC_BACKEND also works).")
    parser.add_argument("--home", default=None, help="Default local state dir.")
    parser.add_argument("--browser", default=None,
                        help="cookie-session browser: chrome|edge|brave|firefox|... "
                             "(env MFP_COOKIE_BROWSER).")
    parser.add_argument("--profile", default=None,
                        help="cookie-session browser profile dir (env MFP_COOKIE_PROFILE).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind host (default loopback; do not widen).")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port.")
    parser.add_argument("--rate-limit", type=int, default=60,
                        help="Max HTTP requests per minute when exposed (default 60).")
    parser.add_argument("--expose", action="store_true",
                        help="Lock the tool surface to inline-JSON + read-only tools "
                             "(hides file-path tools). Auto-enabled for HTTP transports.")
    parser.add_argument("--allow-no-auth", action="store_true",
                        help="Allow unauthenticated requests (useful for ChatGPT No-Auth connector).")
    args = parser.parse_args(argv)

    # cookie-session browser/profile come from flags, else env, else defaults.
    if args.browser:
        os.environ["MFP_COOKIE_BROWSER"] = args.browser
    if args.profile:
        os.environ["MFP_COOKIE_PROFILE"] = args.profile

    is_http = args.transport in HTTP_TRANSPORTS
    # Any network-reachable transport is exposed: lock the surface down.
    expose_mode = args.expose or is_http

    mcp = build_mcp(
        default_backend=args.backend, default_home=args.home,
        host=args.host, port=args.port, expose_mode=expose_mode,
    )

    if is_http:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            parser.error(
                f"{args.transport} is network-reachable, so a bearer token is "
                f"required. Set {TOKEN_ENV} to a strong secret, e.g.\n"
                '  (macOS/Linux)  export MFP_MCP_TOKEN="$(openssl rand -hex 32)"\n'
                "  (Windows PS)   "
                '$env:MFP_MCP_TOKEN=(python -c "import secrets;print(secrets.token_hex(32))")\n'
                "The server refuses to expose your diary without it."
            )
        if len(token) < 24:
            parser.error(f"{TOKEN_ENV} is too short; use at least 24 characters.")
        if args.host not in ("127.0.0.1", "::1", "localhost"):
            # Non-loopback bind is a deliberate exposure; require opt-in.
            if os.environ.get("MFP_MCP_ALLOW_NONLOCAL_BIND") != "1":
                parser.error(
                    f"refusing to bind {args.transport} to {args.host!r}. Keep the "
                    "default 127.0.0.1 and reach it through a tunnel (ngrok/Tunnels). "
                    "To override deliberately, set MFP_MCP_ALLOW_NONLOCAL_BIND=1."
                )
        allow_no_auth = (
            args.allow_no_auth
            or os.environ.get("MFP_MCP_ALLOW_NO_AUTH", "").lower() in ("1", "true", "yes")
        )
        _add_token_gate(
            mcp, token, rate_limit_per_min=args.rate_limit, allow_no_auth=allow_no_auth
        )

    mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
