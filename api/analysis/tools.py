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
_PROPERTY_TYPE = {"type": "string", "enum": ["D", "S", "T", "F", "O", "all"], "default": "all",
                  "description": "D=detached S=semi T=terraced F=flat O=other, or all"}
_TENURE = {"type": "string", "enum": ["F", "L", "all"], "default": "all"}
_NEW_BUILD = {"type": "string", "enum": ["Y", "N", "all"], "default": "all"}
_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_AREAS = {"type": ["array", "string"], "items": {"type": "string"},
          "description": "one or more area names (county or London borough)"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict                       # JSON Schema (draft-07) for the args object
    timeout_ms: int = _DEFAULT_TIMEOUT_MS


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


TOOLS = [
    Tool("get_data_coverage",
         "How fresh the data is and which recent months are complete enough to trust "
         "(Land Registry registers sales with a lag). Call this before answering "
         "'this month' / 'latest' questions.",
         _obj({"area": {"type": "string"}, "area_level": _AREA_LEVEL})),

    Tool("get_area_trend",
         "Time series of median/mean price or transaction count for ONE area over a "
         "date range. Use granularity=year for spans over ~3 years to keep it readable.",
         _obj({"area": {"type": "string"}, "area_level": _AREA_LEVEL,
               "date_from": _DATE, "date_to": _DATE,
               "granularity": {"type": "string", "enum": ["month", "quarter", "year"], "default": "month"},
               "property_type": _PROPERTY_TYPE, "tenure": _TENURE, "new_build": _NEW_BUILD,
               "metric": {"type": "string", "enum": ["median", "mean", "count"], "default": "median"},
               "include_incomplete": {"type": "boolean", "default": False}},
              required=["area"])),

    Tool("get_area_profile",
         "Latest snapshot for ONE area: headline median, MoM and YoY change, and the "
         "breakdown by property type, tenure and new-build share.",
         _obj({"area": {"type": "string"}, "area_level": _AREA_LEVEL, "as_of": _DATE},
              required=["area"])),

    Tool("get_price_index",
         "Rebased price index (base month = 100) for one or more areas. Use to answer "
         "'is X still below its 2022 peak?' or 'which recovered fastest after the dip?'. "
         "Returns peak, current index and current-vs-peak %.",
         _obj({"areas": _AREAS, "base_period": _DATE, "date_from": _DATE, "date_to": _DATE,
               "property_type": _PROPERTY_TYPE, "area_level": _AREA_LEVEL,
               "include_incomplete": {"type": "boolean", "default": False}},
              required=["areas", "base_period"])),

    Tool("compare_areas",
         "Compare 2+ areas side by side: start value, end value and % change over the "
         "last N months.",
         _obj({"areas": _AREAS, "metric": {"type": "string", "enum": ["median", "mean"], "default": "median"},
               "months": {"type": "integer", "default": 12, "minimum": 1},
               "property_type": _PROPERTY_TYPE, "area_level": _AREA_LEVEL},
              required=["areas"])),

    Tool("rank_areas",
         "Rank/screen ALL areas by a metric (median-based, outlier-robust). For 'furthest "
         "below their 2022 peak': metric=current_vs_peak_pct, order=asc, and NO where_ filters. "
         "Add where_current_vs_peak_lt / where_volume_momentum_gt ONLY for a compound screen "
         "the user explicitly asks for (e.g. 'below peak AND volume recovering').",
         _obj({"metric": {"type": "string",
                          "enum": ["current_vs_peak_pct", "volume_momentum_pct", "growth_pct"],
                          "default": "current_vs_peak_pct"},
               "area_level": {"type": "string", "enum": ["county", "district", "both"], "default": "both"},
               "peak_since": _DATE,
               "property_type": _PROPERTY_TYPE,
               "min_transactions": {"type": "integer", "default": 50, "minimum": 0},
               "where_current_vs_peak_lt": {"type": "number"},
               "where_volume_momentum_gt": {"type": "number"},
               "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
               "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50}})),

    Tool("get_market_movers",
         "Latest MoM or YoY median price gainers/fallers across counties and London "
         "boroughs (the conversational twin of the monthly briefing).",
         _obj({"change_type": {"type": "string", "enum": ["mom", "yoy"], "default": "yoy"},
               "direction": {"type": "string", "enum": ["gainers", "fallers", "both"], "default": "both"},
               "min_transactions": {"type": "integer", "default": 10, "minimum": 0},
               "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 25}})),

    Tool("find_affordable_areas",
         "Given a BUDGET, find where it actually buys AND where it goes furthest (best value): "
         "per area returns % of recent sales within budget (the budget's percentile = the value "
         "signal — higher means your money buys a more typical/better home there), median + flat "
         "median, and whether the median fits. Filter by tenure (freehold/leasehold) and "
         "property_type (house = detached/semi/terraced, flat, or a specific type). USE THIS for "
         "any budget/value question ('on £200k', 'best value for money', 'leasehold house under "
         "£X'). area_scope='london' ≈ within the M25. NOT rank_areas (that's price drops). "
         "NB: no bedroom count or floor area in the data — cannot filter by bedrooms or give £/m².",
         _obj({"budget": {"type": "integer", "minimum": 1000, "description": "max purchase price in GBP"},
               "area_scope": {"type": "string", "enum": ["london", "all", "county"], "default": "london",
                              "description": "london = Greater London boroughs (~within M25); county = districts in one county (set 'county'); all = everywhere"},
               "county": {"type": "string", "description": "county name; required when area_scope='county'"},
               "property_type": {"type": "string",
                                 "enum": ["any", "house", "flat", "detached", "semi", "terraced", "other"],
                                 "default": "any", "description": "house = detached/semi/terraced"},
               "tenure": {"type": "string", "enum": ["any", "freehold", "leasehold"], "default": "any"},
               "min_transactions": {"type": "integer", "default": 100, "minimum": 0},
               "limit": {"type": "integer", "default": 12, "minimum": 1, "maximum": 30}},
              required=["budget"])),

    Tool("run_sql",
         "Escape hatch: run a single read-only SELECT against the schema for questions "
         "the other tools don't cover (e.g. grouping by postcode/outcode). Tables: "
         "transactions, postcodes, market_transactions (clean view), monthly_price_stats.",
         _obj({"sql": {"type": "string"}, "max_rows": {"type": "integer", "minimum": 1}},
              required=["sql"]),
         timeout_ms=_RUN_SQL_TIMEOUT_MS),
]

REGISTRY = {t.name: t for t in TOOLS}

# bind handlers by name
_HANDLERS = {
    "get_data_coverage": sql.get_data_coverage,
    "get_area_trend": sql.get_area_trend,
    "get_area_profile": sql.get_area_profile,
    "get_price_index": sql.get_price_index,
    "compare_areas": sql.compare_areas,
    "rank_areas": sql.rank_areas,
    "get_market_movers": sql.get_market_movers,
    "find_affordable_areas": sql.find_affordable_areas,
    "run_sql": sql.run_sql,
}


# Small models fuzz enum casing / values; coerce before validating so we don't reject
# COUNTY->county, median_price->median, "false"->False, "flat"->F, etc.
_ENUM_ALIASES = {
    "median_price": "median", "mean_price": "mean", "avg": "mean", "average": "mean",
    "transaction_count": "count", "transactions": "count", "volume": "count", "price": "median",
    "detached": "D", "semi": "S", "semi-detached": "S", "terraced": "T", "terrace": "T",
    "flat": "F", "apartment": "F", "other": "O", "freehold": "F", "leasehold": "L",
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
