"""MCP server over Streamable HTTP (stateless, JSON responses) at `/mcp`.

Auth: OAuth 2.1 via Supabase Auth's OAuth server. This API is the *resource server*: it publishes
Protected Resource Metadata (RFC 9728) naming Supabase as the authorization server, answers 401 with a
`WWW-Authenticate` pointer to it, and verifies the resulting Supabase JWT exactly like the app's API.
Access also needs the plan feature `mcp` and the account flag `mcp_enabled` (early access).

Implements the parts of the spec a tools-only server needs: initialize, ping, tools/list, tools/call,
notifications. No server-initiated streams (GET → 405).
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.channels import get_session_factory
from app.core.config import get_settings
from app.core.deps import get_verifier
from app.core.errors import AppError, Unauthorized
from app.core.ratelimit import RateLimiter
from app.core.security import TokenVerifier
from app.db.session import get_db
from app.modules.discovery_analysis.channels import SessionFactory
from app.modules.discovery_analysis.llm import LLMProvider, get_llm_provider
from app.modules.discovery_analysis.providers import ChannelProvider, get_channel_provider
from app.modules.identity.service import get_or_create_profile
from app.modules.mcp.tools import ToolContext, allowed_tools, call_tool
from app.modules.plans_credits.plans import limits_for

log = logging.getLogger("smart_content.mcp")
router = APIRouter(tags=["mcp"])
mcp_limiter = RateLimiter(limit=120, window_seconds=60)

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
SCOPES = ["openid", "email", "profile"]
INSTRUCTIONS = (
    "Smart Content gives you the user's short-form video research: tracked creators, outlier videos "
    "(views ÷ the creator's median), video breakdowns, ideas and hooks. Start with videos-watchlist or "
    "channels-recap; use analyze (1 credit) only when the user wants a breakdown of a specific video."
)


def _base() -> str:
    return get_settings().public_api_url.rstrip("/")


def _metadata_url() -> str:
    return f"{_base()}/.well-known/oauth-protected-resource/mcp"


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata() -> dict[str, Any]:
    s = get_settings()
    return {
        "resource": f"{_base()}/mcp",
        "authorization_servers": [s.jwt_issuer] if s.supabase_url else [],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_name": "Smart Content",
        "resource_documentation": f"{s.dashboard_url.rstrip('/')}/settings?tab=Connectors",
    }


def _rpc_error(id_: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _challenge(message: str, error: str = "invalid_token") -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "unauthorized", "message": message, "details": {}}},
        status_code=401,
        headers={
            "WWW-Authenticate": f'Bearer error="{error}", resource_metadata="{_metadata_url()}", scope="{" ".join(SCOPES)}"'
        },
    )


@router.get("/mcp")
async def mcp_get() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})  # stateless: no sessions to end


@router.post("/mcp")
async def mcp_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    verifier: TokenVerifier = Depends(get_verifier),
    provider: ChannelProvider = Depends(get_channel_provider),
    llm: LLMProvider = Depends(get_llm_provider),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> Response:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return _challenge("Sign in to Smart Content to use this connector.", "invalid_request")
    try:
        user = verifier.verify(token)
    except Unauthorized as e:
        return _challenge(e.message)
    profile = await get_or_create_profile(db, user)
    if "mcp" not in limits_for(profile.plan).features or not profile.mcp_enabled:
        return JSONResponse(
            {
                "error": {
                    "code": "mcp_not_enabled",
                    "message": "MCP isn't enabled for this account yet (early access). Ask support to enable it.",
                    "details": {},
                }
            },
            status_code=403,
        )
    mcp_limiter.check(str(profile.id))

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
    ctx = ToolContext(db=db, profile=profile, provider=provider, llm=llm, session_factory=session_factory)
    msgs = body if isinstance(body, list) else [body]
    replies = [r for r in [await _handle(ctx, m, request) for m in msgs] if r is not None]
    if not replies:  # only notifications / responses
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(body, list) else replies[0])


async def _handle(ctx: ToolContext, msg: Any, request: Request) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request")
    method, id_ = msg.get("method"), msg.get("id")
    if method is None or "id" not in msg:  # notification or a response from the client
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "smart-content",
                    "title": "Smart Content",
                    "version": get_settings().version,
                },
                "instructions": INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {"tools": [t.schema() for t in allowed_tools(ctx.profile)]},
        }
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _rpc_error(
                id_, -32602, "Invalid params: name (string) and arguments (object) are required."
            )
        try:
            data = await call_tool(ctx, name, args)
        except AppError as e:
            # Tool-level failures go back to the model as a result it can read and act on.
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {"content": [{"type": "text", "text": e.message}], "isError": True},
            }
        except Exception:
            log.exception("mcp tool %s failed", name)
            await ctx.db.rollback()
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {
                    "content": [{"type": "text", "text": "Something went wrong. Try again."}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "content": [{"type": "text", "text": json.dumps(data, default=str, ensure_ascii=False)}],
                "structuredContent": data,
                "isError": False,
            },
        }
    return _rpc_error(id_, -32601, f"Method not found: {method}")
