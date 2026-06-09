"""Transport-agnostic tool registry.

Each Tool = (name, description, JSON-Schema params, async handler). This single
shape is consumed by the /ask agent now (openai_tool_specs / tool_catalog_text)
and, with no changes, by a future MCP server (mcp_tool_specs + call_tool).
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import psycopg
from jsonschema import Draft7Validator

from ..config import AGENT_DB_CONFIG
from . import sql

# statement_timeout per tool (ms). Typed analytical queries get headroom; the
# free-form run_sql escape hatch stays tight.
_DEFAULT_TIMEOUT_MS = 20000
_RUN_SQL_TIMEOUT_MS = 8000

# Reusable schema fragments
_AREA_LEVEL = {"type": "string", "enum": ["county", "district", "auto"], "default": "auto",
               "description": "county, London-borough district, or auto-detect"}
# Friendly property-type group shared by the budget + value tools.
_PTYPE_GROUP = {"type": "string",
                "enum": ["any", "house", "flat", "detached", "semi", "terraced", "other"],
                "default": "any", "description": "house = detached/semi/terraced"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict                       # JSON Schema (draft-07) for the args object
    timeout_ms: int = _DEFAULT_TIMEOUT_MS


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


# The tool set is organised around what a first-time buyer actually asks. Three orthogonal
# intent tools (a small model picks reliably from a few non-overlapping tools) plus two
# utilities. General questions ("what is a leasehold?") need no tool — the model just answers.
TOOLS = [
    Tool("find_affordable_areas",
         "Q: 'Does £X fit, and where does my money go furthest?' Given a BUDGET, returns per "
         "area the % of recent sales within budget (the budget's percentile = the value signal — "
         "higher means your money buys a more typical/better home there), the median + flat "
         "median, and whether the median fits. Filter by tenure (freehold/leasehold), new-build vs "
         "resale (new_build), and property_type (house = detached/semi/terraced, flat, or a specific "
         "type). USE THIS for "
         "any budget question ('on £200k', 'what can I afford', 'best value for my money', "
         "'leasehold house under £X'). area_scope defaults to 'all' (nationwide) — set 'london' "
         "ONLY when the user names London / 'within the M25', or 'county' (+county) for a named "
         "county. Do NOT default to London. Also returns each area's median £/m² (EPC-matched "
         "sales only — see sqm_match_pct); supports an optional floor-area band "
         "(min_floor_area/max_floor_area, m²) and sort='ppsqm' (rank by cheapest £/m²). Use "
         "sort='ppsqm' for 'cheapest/best value per square metre'. NB: £/m² & size are EPC-matched "
         "only; there is still NO bedroom count (habitable rooms is an approximate proxy, not bedrooms).",
         _obj({"budget": {"type": "integer", "minimum": 1000, "description": "max purchase price in GBP"},
               "area_scope": {"type": "string", "enum": ["all", "london", "county"], "default": "all",
                              "description": "all = nationwide (England & Wales, the default); london = Greater London boroughs (~within M25), use only if the user says London/M25; county = districts in one county (set 'county')"},
               "county": {"type": "string", "description": "county name; required when area_scope='county'"},
               "property_type": _PTYPE_GROUP,
               "tenure": {"type": "string", "enum": ["any", "freehold", "leasehold"], "default": "any"},
               "new_build": {"type": "string", "enum": ["any", "new", "resale"], "default": "any",
                             "description": "new = new-build only; resale = existing/second-hand only"},
               "min_floor_area": {"type": "number", "minimum": 10,
                                  "description": "optional min total floor area in m² (EPC-matched sales only)"},
               "max_floor_area": {"type": "number", "minimum": 10,
                                  "description": "optional max total floor area in m²"},
               "sort": {"type": "string", "enum": ["affordability", "ppsqm"], "default": "affordability",
                        "description": "ppsqm = rank areas by lowest median £/m² (EPC-matched)"},
               "min_transactions": {"type": "integer", "default": 100, "minimum": 0},
               "limit": {"type": "integer", "default": 12, "minimum": 1, "maximum": 30}},
              required=["budget"])),

    Tool("assess_value",
         "Q: 'Is this place — or this asking price — good value or overpriced?' Pass ONE area "
         "(county like KENT or London borough like BROMLEY) and optionally a candidate_price "
         "(+property_type) you're weighing up. Triangulates value three ways: vs the area's OWN "
         "history (how far below/above its peak, 12-month direction), vs PEERS (its median vs the "
         "national typical for that type), where that price sits in the LOCAL distribution (its "
         "percentile if candidate_price given), AND £/m² (EPC-matched sales): the area's median "
         "£/m² vs national, plus the candidate's own £/m² if you pass candidate_floor_area. Also "
         "reports the new-build vs resale premium, the EPC energy-band distribution (incl. % EPC-D "
         "or worse, a running-cost / efficiency-rule signal) and the floor-area spread (quartiles). "
         "USE THIS for 'is X good value?', 'am I overpaying?', 'is £350k for a 70 m² flat in Bromley "
         "fair?', '£ per m² in Bromley?', 'new-build premium in X?', 'how energy-efficient is X?'. £/m², "
         "size & energy are EPC-matched-only (coverage reported, suppressed if too thin); no bedroom counts.",
         _obj({"area": {"type": "string", "description": "a county (e.g. KENT) or London borough (e.g. BROMLEY)"},
               "area_level": _AREA_LEVEL,
               "property_type": _PTYPE_GROUP,
               "candidate_price": {"type": "integer", "minimum": 1000,
                                   "description": "optional asking price (GBP) to judge against local sales"},
               "candidate_floor_area": {"type": "number", "minimum": 10,
                                        "description": "optional floor area (m²) of the specific property, to compute its £/m²"}},
              required=["area"])),

    Tool("scan_market",
         "Q: 'I don't know where to look — where's the action?' Screens ALL counties + London "
         "boroughs by focus: 'falling' (biggest YoY median fallers — cooling markets / potential "
         "entry points), 'rising' (biggest YoY median gainers — hottest markets), 'cheapest' "
         "(lowest absolute median — entry-level areas). USE THIS for open-ended 'where should I "
         "be looking?', 'where are prices dropping/rising?', 'where are the cheapest places?'. No "
         "budget needed — for 'what fits £X' use find_affordable_areas. All property types.",
         _obj({"focus": {"type": "string", "enum": ["falling", "rising", "cheapest"],
                         "default": "falling"},
               "area_level": {"type": "string", "enum": ["county", "district", "both"], "default": "both"},
               "limit": {"type": "integer", "default": 12, "minimum": 1, "maximum": 25}})),

    Tool("get_data_coverage",
         "How fresh the data is and which recent months are complete enough to trust "
         "(Land Registry registers sales with a lag). Call this for 'how current is the data?' "
         "or before answering 'this month' / 'latest' questions.",
         _obj({"area": {"type": "string"}, "area_level": _AREA_LEVEL})),

    Tool("run_sql",
         "Escape hatch: run a single read-only SELECT against the schema for questions "
         "the other tools don't cover (e.g. grouping by postcode/outcode). Tables: "
         "transactions, postcodes, market_transactions (clean view), market_transactions_epc "
         "(adds EPC floor area / price_per_sqm / energy rating, partial coverage), monthly_price_stats. "
         "GROUP BY dimensions: district, county, town, property_type, tenure, new_build on the "
         "transaction views; region via JOIN postcodes USING (postcode); EPC local_authority / "
         "constituency live on epc_property / epc_certificates (keyed by uprn or norm_postcode).",
         _obj({"sql": {"type": "string"}, "max_rows": {"type": "integer", "minimum": 1}},
              required=["sql"]),
         timeout_ms=_RUN_SQL_TIMEOUT_MS),
]

REGISTRY = {t.name: t for t in TOOLS}

# bind handlers by name
_HANDLERS = {
    "find_affordable_areas": sql.find_affordable_areas,
    "assess_value": sql.assess_value,
    "scan_market": sql.scan_market,
    "get_data_coverage": sql.get_data_coverage,
    "run_sql": sql.run_sql,
}


# Small models fuzz enum casing / values; coerce before validating so we don't reject
# COUNTY->county, "semi-detached"->semi, "apartment"->flat, "false"->False, etc.
_ENUM_ALIASES = {
    "semi-detached": "semi", "semidetached": "semi", "terrace": "terraced",
    "apartment": "flat", "flats": "flat", "houses": "house",
    # new_build (find_affordable_areas)
    "new build": "new", "new-build": "new", "newbuild": "new", "newbuilds": "new", "new builds": "new",
    "existing": "resale", "second-hand": "resale", "secondhand": "resale", "second hand": "resale",
    "older": "resale",
}


def _coerce_args(schema: dict, args: dict) -> dict:
    props = schema.get("properties", {})
    out = {}
    for k, v in (args or {}).items():
        spec = props.get(k, {})
        t = spec.get("type")
        is_bool = t == "boolean" or (isinstance(t, list) and "boolean" in t)
        enum = spec.get("enum")
        if isinstance(v, str):
            if is_bool:
                lv = v.strip().lower()
                if lv in ("true", "1", "yes"):
                    v = True
                elif lv in ("false", "0", "no"):
                    v = False
            elif enum and v not in enum:
                low = {e.lower(): e for e in enum if isinstance(e, str)}
                if v.lower() in low:
                    v = low[v.lower()]
                elif _ENUM_ALIASES.get(v.lower()) in enum:
                    v = _ENUM_ALIASES[v.lower()]
            elif t == "integer":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    pass
        out[k] = v
    return out


async def call_tool(name: str, arguments: dict | None) -> Any:
    """Validate args, open a read-only connection, run the handler, close. Returns a
    JSON-serialisable result (or {"error": ...})."""
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"unknown tool '{name}'"}
    arguments = _coerce_args(tool.parameters, arguments or {})
    errors = sorted(Draft7Validator(tool.parameters).iter_errors(arguments), key=lambda e: e.path)
    if errors:
        return {"error": f"invalid arguments: {errors[0].message}"}
    handler = _HANDLERS[name]
    conn = await psycopg.AsyncConnection.connect(**AGENT_DB_CONFIG)
    try:
        await conn.set_autocommit(True)
        await conn.execute(f"SET statement_timeout = {tool.timeout_ms}")
        return await handler(conn, **arguments)
    finally:
        await conn.close()


def tool_catalog_text() -> str:
    """Terse catalogue for the planner system prompt (keeps the context budget tight)."""
    lines = []
    for t in TOOLS:
        args = ", ".join(t.parameters.get("properties", {}).keys())
        req = t.parameters.get("required", [])
        req_note = f" [required: {', '.join(req)}]" if req else ""
        lines.append(f"- {t.name}({args}){req_note}: {t.description}")
    return "\n".join(lines)


def openai_tool_specs() -> list:
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in TOOLS]


def mcp_tool_specs() -> list:
    """Used by the future MCP server (tools/list)."""
    return [{"name": t.name, "description": t.description, "inputSchema": t.parameters} for t in TOOLS]
