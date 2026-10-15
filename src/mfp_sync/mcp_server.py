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
):
    """Construct the FastMCP app with all tools registered."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        SERVER_NAME,
        instructions=(
            "Prepopulate a MyFitnessPal food diary from a weekly meal plan. "
            "Call `plan_schema` to learn the plan JSON shape, `preview_plan` to "
            "see the exact operations (no writes), `resolve_plan` to surface "
            "ambiguous foods, then `apply_plan` to write. Re-running the same "
            "plan is idempotent — it never duplicates entries."
        ),
        host=host,
        port=port,
    )

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

    @mcp.tool()
    @_guard
    def preview_plan(
        plan_path: str,
        backend: str | None = None,
        home: str | None = None,
        restore_deleted: bool = False,
    ) -> dict[str, Any]:
        """Show the exact sync operations for a plan file. Makes NO changes."""
        settings = _settings(backend or default_backend, home or default_home, None)
        with EngineSession(settings) as eng:
            plan = eng.load(plan_path)
            sync_plan = eng.reconciler.build_plan(plan, restore_deleted=restore_deleted)
            return ok_envelope(**plan_to_dict(sync_plan))

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    @_guard
    def apply_plan(
        plan_path: str,
        backend: str | None = None,
        home: str | None = None,
        restore_deleted: bool = False,
        on_conflict: str | None = None,
    ) -> dict[str, Any]:
        """Write the plan to the diary. Idempotent; preserves manual changes.

        Conflicts (an entry you edited manually) are NEVER overwritten through
        this tool unless on_conflict='overwrite' is passed explicitly — the
        default is agent-safe (skip the conflict). Refuses to run while any food
        is unresolved (call resolve_plan first).
        """
        settings = _settings(backend or default_backend, home or default_home, on_conflict)
        with EngineSession(settings) as eng:
            plan = eng.load(plan_path)
            sync_plan = eng.reconciler.build_plan(plan, restore_deleted=restore_deleted)

            unresolved = sync_plan.unresolved()
            if unresolved:
                return {
                    "ok": False,
                    "error_code": ErrorCode.UNRESOLVED_FOODS.value,
                    "error": f"{len(unresolved)} entries unresolved; call resolve_plan first",
                    "unresolved": [a.source_entry_id for a in unresolved],
                }

            mutating = [a for a in sync_plan.actions if a.mutates]
            if not mutating:
                return ok_envelope(
                    message="nothing to apply; diary already matches the plan",
                    result=empty_result_dict(),
                )

            # Agent-safe: never overwrite a manual edit unless the caller set
            # on_conflict='overwrite' (handled inside the writer via settings).
            result = eng.reconciler.apply(sync_plan, confirm_conflict=lambda a: False)
            code = ErrorCode.OK.value if result.ok else ErrorCode.WRITE_UNCERTAIN.value
            return {"ok": result.ok, "error_code": code,
                    "result": apply_result_to_dict(result)}

    return mcp


# -- token gate for HTTP transports ------------------------------------------


def _add_token_gate(mcp, token: str) -> None:
    """Install fail-closed bearer-token middleware on the Starlette app.

    Every request must send `Authorization: Bearer <token>`. Health/no-path
    probes and anything else are rejected with 401. Uses a constant-time
    comparison. The token value is never logged.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected = f"Bearer {token}"

    class _TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            provided = request.headers.get("authorization", "")
            if not hmac.compare_digest(provided, expected):
                return JSONResponse(
                    {"ok": False, "error_code": "AUTH_FAILED",
                     "error": "missing or invalid bearer token"},
                    status_code=401,
                )
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
                app.add_middleware(_TokenMiddleware)
                return app
            return wrapped

        setattr(mcp, attr, make_wrapped(factory))


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind host (default loopback; do not widen).")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port.")
    args = parser.parse_args(argv)

    mcp = build_mcp(
        default_backend=args.backend, default_home=args.home,
        host=args.host, port=args.port,
    )

    if args.transport in HTTP_TRANSPORTS:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            parser.error(
                f"{args.transport} is network-reachable, so a bearer token is "
                f"required. Set {TOKEN_ENV} to a strong secret, e.g.\n"
                f'  export {TOKEN_ENV}="$(openssl rand -hex 32)"\n'
                "The server refuses to expose your diary without it."
            )
        if args.host not in ("127.0.0.1", "::1", "localhost"):
            # Non-loopback bind is a deliberate exposure; require opt-in.
            if os.environ.get("MFP_MCP_ALLOW_NONLOCAL_BIND") != "1":
                parser.error(
                    f"refusing to bind {args.transport} to {args.host!r}. Keep the "
                    "default 127.0.0.1 and reach it through a tunnel (ngrok/Tunnels). "
                    "To override deliberately, set MFP_MCP_ALLOW_NONLOCAL_BIND=1."
                )
        _add_token_gate(mcp, token)

    mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
