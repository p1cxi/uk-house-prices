"""Streamable-HTTP MCP server exposing the analysis tool registry.

A thin transport wrapper over api/analysis/tools.py — same tools, same read-only
agent_ro DB access as the /ask endpoint. Lets an MCP-capable client (e.g. the
llama.cpp chat UI) tool-call the analytics directly. Served at /mcp.

Run: uvicorn api.mcp_server:app --host 0.0.0.0 --port 8000 --app-dir /
"""
import contextlib
import json
import os
from collections.abc import AsyncIterator

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from .analysis.tools import REGISTRY, call_tool

# Honesty / data-currency directives surfaced to the model in the MCP initialize handshake.
# The /ask path enforces these in its synthesiser; the MCP path has no synthesiser (the client
# model composes the answer directly), so without this the model never volunteers the data-lag
# caveat and may treat incomplete recent months or thin EPC coverage as solid.
_INSTRUCTIONS = """These tools query HM Land Registry SOLD prices for England & Wales, 1995-present \
(no rentals, no current asking prices, no forecasts). When answering from their results:
- DATA CURRENCY: each result carries meta.last_complete_month — the latest fully-registered month. \
State that figures are current to that month, and note HM Land Registry registers sales with a ~2-3 \
month lag, so more recent months are incomplete and will rise. Do not present an incomplete recent \
month as a settled figure.
- £/m², FLOOR AREA & ENERGY come from EPC-matched sales only (PARTIAL coverage). Report a £/m² or size \
figure ONLY when the result supplies it, and always state the match % (e.g. "based on ~68% of recent \
sales"). If a result omits these or marks coverage untrustworthy, give the price-based answer and say \
£/m² isn't reliable there — never fabricate one.
- BEDROOMS ARE NOT IN THE DATA: there is NO bedroom field — EPC "habitable rooms" is only an approximate \
size proxy, NOT a bedroom count. Never filter by, search for, or report a bedroom count, and never restate \
the user's bedroom number as if you matched it — do NOT write phrases like "2-bedroom flats" or "2-bed homes". \
If the user asks for a number of bedrooms, say plainly that sold-price data has no bedroom information so you \
can't filter by it, then answer by price, property type and (where available) floor area instead.
- Cite only numbers present in the tool results; never invent or estimate a figure."""

server = Server("uk-house-prices-analytics", instructions=_INSTRUCTIONS)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
            for t in REGISTRY.values()]


@server.call_tool()
async def handle_call(name: str, arguments: dict | None) -> list[types.TextContent]:
    # call_tool validates args, runs under the read-only agent_ro role, returns JSON-able data.
    result = await call_tool(name, arguments or {})
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


# Trusted internal network (picxibox_net + LAN). Skip DNS-rebinding host checks so the
# chat UI can reach it by LAN IP/hostname rather than only localhost.
_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
_session_manager = StreamableHTTPSessionManager(app=server, stateless=True, security_settings=_security)


async def _handle_mcp(scope, receive, send):
    await _session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def _lifespan(_app) -> AsyncIterator[None]:
    async with _session_manager.run():
        yield


# Browser-based MCP clients (e.g. the chat UI) make cross-origin requests, so CORS is
# required. expose_headers must include Mcp-Session-Id or the browser client can't read
# the session id from the response. MCP_ALLOW_ORIGINS can restrict origins (default: any).
_origins = [o.strip() for o in os.getenv("MCP_ALLOW_ORIGINS", "*").split(",") if o.strip()]
_cors = Middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "mcp-session-id"],
    # Answer Chrome's Private Network Access preflight (page on localhost -> private IP).
    allow_private_network=True,
)

# Mount the MCP handler at root so the configured URL (…:8004/mcp) is served directly
# with no trailing-slash redirect — the StreamableHTTP manager dispatches on HTTP method,
# not path, and a cross-origin 307 on every request is fragile for browser MCP clients.
app = Starlette(routes=[Mount("/", app=_handle_mcp)], middleware=[_cors], lifespan=_lifespan)
