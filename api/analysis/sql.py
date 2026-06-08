"""Analysis tool bodies — pure async query functions over the existing schema.

The registry is built around what a first-time buyer actually asks:
  1. find_affordable_areas — "does £X fit, and where does it go furthest?"
  2. assess_value         — "is this place / this asking price good value or overpriced?"
  3. scan_market          — "I don't know where to look — where's the action?"
plus get_data_coverage (how fresh/complete the data is) and run_sql (escape hatch).

The older granular tools (area profile / trend / index / compare / movers) were
consolidated into the three intent tools above: a small model picks reliably from a
few orthogonal tools, and the overlap between nine "area price" tools was the main
cause of mis-routing. scan_market dispatches to internal engines (_yoy_movers,
_cheapest_areas); all figures anchor on the last COMPLETE month.

Design rules (unchanged):
- Area-scoped medians come from raw `market_transactions` (true PERCENTILE_CONT),
  never from averaging `monthly_price_stats.median_price` (the median-aggregation trap).
- Registration lag: a global `last_complete_month` (most recent month whose volume is
  >= 40% of the trailing-12-month average monthly volume) bounds "current" figures.
- Every value the LLM can influence is a BOUND parameter; only validated enums
  (area_level, focus) are ever interpolated.
- No bedroom count / floor area exists in Land Registry — tools never filter by
  bedrooms or report £/m² (that's the planned EPC layer).
"""
from datetime import date

from dateutil.relativedelta import relativedelta
from psycopg.rows import dict_row

from ..config import AGENT_SQL_ROW_LIMIT
from .guards import validate_readonly_sql, wrap_with_limit

# Property-type groups shared by find_affordable_areas and assess_value.
_PGROUP_SQL = {"house": "property_type IN ('D','S','T')", "flat": "property_type = 'F'",
               "detached": "property_type = 'D'", "semi": "property_type = 'S'",
               "terraced": "property_type = 'T'", "other": "property_type = 'O'"}
_PTYPE_FRIENDLY = {"any": "all property types", "house": "houses (detached/semi/terraced)",
                   "flat": "flats", "detached": "detached houses", "semi": "semi-detached houses",
                   "terraced": "terraced houses", "other": "other property types"}

# Whole-country/region names that are NOT a single area the area-scoped tools handle.
_COUNTRY_TERMS = {"UK", "U.K.", "THE UK", "UNITED KINGDOM", "ENGLAND", "WALES",
                  "ENGLAND AND WALES", "GREAT BRITAIN", "BRITAIN", "GB",
                  "NATIONWIDE", "ANYWHERE"}


def _is_country(area) -> bool:
    return bool(area) and str(area).strip().upper() in _COUNTRY_TERMS


def _country_guard(area):
    """Return a redirecting error dict if `area` is a whole country/region, else None."""
    if _is_country(area):
        return {"error": f"'{area}' is a whole country/region, not a single area. For nationwide "
                         "budget / 'best value' questions use find_affordable_areas (area_scope="
                         "'all'); to see where the action is use scan_market; to judge ONE place "
                         "use assess_value with a specific county (e.g. KENT) or London borough "
                         "(e.g. BROMLEY)."}
    return None


def _area_where(area_level: str, param: str = "area") -> str:
    if area_level == "county":
        return f"UPPER(county) = UPPER(%({param})s)"
    if area_level == "district":
        return f"UPPER(district) = UPPER(%({param})s)"
    return (f"(UPPER(county) = UPPER(%({param})s) "
            f"OR (UPPER(county) = 'GREATER LONDON' AND UPPER(district) = UPPER(%({param})s)))")


def _month_first(d: date) -> date:
    return d.replace(day=1)


async def _scalar(conn, sql: str, params=None):
    async with conn.cursor() as cur:
        await cur.execute(sql, params or {})
        row = await cur.fetchone()
        return row[0] if row else None


async def _last_complete_month(conn) -> date:
    """Most recent month whose volume >= 40% of the trailing-12-month average monthly volume.
    (Average, not median: percentile_cont does not support a window OVER clause, and a mean
    is fine for a volume-completeness heuristic.)"""
    return await _scalar(conn, """
        WITH monthly AS (
            SELECT month, SUM(transaction_count) AS cnt
            FROM monthly_price_stats GROUP BY month
        ),
        withavg AS (
            SELECT month, cnt,
                   avg(cnt) OVER (ORDER BY month ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING) AS tavg
            FROM monthly
        )
        SELECT MAX(month)::date FROM withavg WHERE cnt >= 0.40 * COALESCE(tavg, cnt)
    """)


def _parse_date(s, default: date) -> date:
    """Lenient: accepts a date, 'YYYY-MM-DD', 'YYYY-MM' or 'YYYY' (LLMs pass all of these)."""
    if not s:
        return default
    if isinstance(s, date):
        return s
    t = str(s).strip()[:10]
    try:
        if len(t) == 4:           # YYYY
            return date(int(t), 1, 1)
        if len(t) == 7:           # YYYY-MM
            return date.fromisoformat(t + "-01")
        return date.fromisoformat(t)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def get_data_coverage(conn, area=None, area_level="auto"):
    """Data freshness + which recent months are complete enough to trust."""
    lcm = await _last_complete_month(conn)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM get_data_freshness()")
        fresh = await cur.fetchone()
        await cur.execute("""
            SELECT month::date AS month, SUM(transaction_count) AS transactions
            FROM monthly_price_stats
            WHERE month >= (SELECT MAX(month) FROM monthly_price_stats) - INTERVAL '5 months'
            GROUP BY month ORDER BY month
        """)
        recent = await cur.fetchall()
    for r in recent:
        r["month"] = r["month"].isoformat()
        r["transactions"] = int(r["transactions"])
        r["considered_complete"] = (lcm is not None and r["month"] <= lcm.isoformat())
    return {
        "last_transaction_date": fresh["last_transaction_date"].isoformat() if fresh and fresh["last_transaction_date"] else None,
        "total_transactions": int(fresh["total_transactions"]) if fresh else 0,
        "last_complete_month": lcm.isoformat() if lcm else None,
        "note": "HM Land Registry registers sales with a lag; recent months marked incomplete will grow.",
        "recent_months": recent,
    }


def _band(value, low, high, below, mid, above):
    if value is None:
        return None
    if value <= low:
        return below
    if value >= high:
        return above
    return mid


async def assess_value(conn, area, area_level="auto", property_type="any", candidate_price=None):
    """Is a place — or a specific asking price — good value or overpriced? Triangulates three
    angles, all RELATIVE to the market (not intrinsic £/m², which needs floor area we lack):
      • vs the area's OWN history: how far below/above its peak median, and 12-month direction;
      • vs PEERS: the area's median vs the national typical for that property type;
      • vs the LOCAL distribution: if a candidate_price is given, its percentile among recent
        local sales of that type (the precise 'is this asking price fair?' signal)."""
    guard = _country_guard(area)
    if guard:
        return guard
    lcm = await _last_complete_month(conn)
    if lcm is None:
        return {"error": "No data available."}
    end = _month_first(lcm)
    recent_start = end - relativedelta(months=11)          # 12 complete months
    prior_start = recent_start - relativedelta(years=1)    # the 12 months before that
    end_excl = end + relativedelta(months=1)
    cand = int(candidate_price) if candidate_price else None
    where_area = _area_where(area_level)
    type_sql = ""
    pg = _PGROUP_SQL.get(property_type)
    if pg:
        type_sql = " AND " + pg
    params = {"area": area, "recent_start": recent_start, "prior_start": prior_start,
              "end_excl": end_excl, "cand": cand, "min_month_tx": 30}

    async with conn.cursor(row_factory=dict_row) as cur:
        # 1) recent vs prior-year median + sales + candidate-price percentile
        await cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE date >= %(recent_start)s)::int AS sales_recent,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                  FILTER (WHERE date >= %(recent_start)s)::bigint AS median_recent,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                  FILTER (WHERE date >= %(prior_start)s AND date < %(recent_start)s)::bigint AS median_prior,
              round(100.0 * count(*) FILTER (WHERE %(cand)s IS NOT NULL AND price <= %(cand)s
                                               AND date >= %(recent_start)s)
                    / NULLIF(count(*) FILTER (WHERE date >= %(recent_start)s), 0), 1) AS candidate_pctile
            FROM market_transactions
            WHERE {where_area} AND date >= %(prior_start)s AND date < %(end_excl)s{type_sql}
        """, params)
        rec = await cur.fetchone()

        if not rec or not rec["sales_recent"]:
            return {"error": f"No recent sales found for '{area}'"
                             + (f" ({_PTYPE_FRIENDLY.get(property_type, property_type)})" if pg else "")
                             + ". Use a known county (e.g. KENT) or London borough (e.g. BROMLEY); "
                               "for nationwide/budget questions use find_affordable_areas or scan_market."}

        # 2) peak (volume-floored monthly medians) + latest month median, full history
        await cur.execute(f"""
            WITH am AS (
              SELECT date_trunc('month', date)::date AS month,
                     percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS med,
                     count(*) AS cnt
              FROM market_transactions
              WHERE {where_area} AND date < %(end_excl)s{type_sql}
              GROUP BY 1 HAVING count(*) >= %(min_month_tx)s
            )
            SELECT
              (SELECT round(med)::bigint FROM am ORDER BY med DESC LIMIT 1) AS peak_med,
              (SELECT month       FROM am ORDER BY med DESC LIMIT 1) AS peak_month,
              (SELECT round(med)::bigint FROM am ORDER BY month DESC LIMIT 1) AS latest_med
        """, params)
        hist = await cur.fetchone()

        # 3) national typical for this property type (trailing 12 complete months)
        await cur.execute(f"""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS peer_median
            FROM market_transactions
            WHERE date >= %(recent_start)s AND date < %(end_excl)s{type_sql}
        """, params)
        peer = await cur.fetchone()

    median_recent = rec["median_recent"]
    median_prior = rec["median_prior"]
    peak_med = hist["peak_med"] if hist else None
    latest_med = hist["latest_med"] if hist else None
    peer_median = peer["peer_median"] if peer else None

    change_12m = (round(100.0 * (median_recent - median_prior) / median_prior, 1)
                  if median_recent and median_prior else None)
    vs_peak = (round(100.0 * (latest_med - peak_med) / peak_med, 1)
               if latest_med and peak_med else None)
    ratio = round(median_recent / peer_median, 2) if median_recent and peer_median else None

    candidate = None
    if cand is not None:
        pctile = rec["candidate_pctile"]
        candidate = {
            "price": cand,
            "local_percentile": float(pctile) if pctile is not None else None,
            "verdict": _band(float(pctile) if pctile is not None else None, 35, 65,
                             "good value — in the cheaper end of recent local sales",
                             "around the local going rate",
                             "toward the pricier end of recent local sales"),
        }

    return {
        "area": area, "area_level": area_level,
        "property_type": property_type, "property_type_label": _PTYPE_FRIENDLY.get(property_type, property_type),
        "window": {"from": recent_start.isoformat(), "to": end.isoformat()},
        "typical_price_now": median_recent, "recent_sales": rec["sales_recent"],
        "vs_history": {
            "peak_median": peak_med,
            "peak_month": hist["peak_month"].isoformat() if hist and hist["peak_month"] else None,
            "latest_month_median": latest_med,
            "current_vs_peak_pct": vs_peak,
            "change_12m_pct": change_12m,
            "verdict": _band(vs_peak, -8, -2, "well below its past peak", "roughly at its past peak",
                             "near/at its past peak") if vs_peak is not None else None,
        },
        "vs_peers": {
            "comparison": "England & Wales typical for this property type",
            "peer_median": peer_median, "ratio_to_peer": ratio,
            "verdict": _band(ratio, 0.85, 1.15, "cheaper than the national typical",
                             "around the national typical", "pricier than the national typical")
            if ratio is not None else None,
        },
        "candidate": candidate,
        "data_note": ("Value here is RELATIVE to the market (the area's own history, national "
                      "peers, and the local price distribution). It is NOT an intrinsic £/m² "
                      "valuation — Land Registry has no floor area or bedroom count (planned via "
                      "EPC data)."),
        "meta": {"last_complete_month": lcm.isoformat(),
                 "incomplete_recent": False},
    }


# --- Internal engine (dispatched by scan_market; not a registered tool) --------------------

async def _yoy_movers(conn, lcm: date, direction="gainers", min_transactions=50, limit=12):
    """Biggest YoY median movers across counties + London boroughs, anchored on the LAST
    COMPLETE month (not CURRENT_DATE — data lags ~2-3 months, so a CURRENT_DATE anchor would
    compare under-registered months and produce noise). YoY = median(lcm) vs median(lcm-12mo).
    A real volume floor on the anchor month keeps thin, volatile areas out."""
    order_sql = "DESC" if direction == "gainers" else "ASC"
    params = {"m0": _month_first(lcm), "m1": _month_first(lcm) + relativedelta(months=1),
              "myr": _month_first(lcm) - relativedelta(years=1),
              "myr1": _month_first(lcm) - relativedelta(years=1) + relativedelta(months=1),
              "min_tx": int(min_transactions), "limit": max(1, min(int(limit), 25))}
    sql = f"""
        WITH area_data AS (
            SELECT CASE WHEN UPPER(county)='GREATER LONDON' THEN district ELSE county END AS area,
                   CASE WHEN UPPER(county)='GREATER LONDON' THEN 'district' ELSE 'county' END AS area_level,
                   count(*) FILTER (WHERE date >= %(m0)s AND date < %(m1)s) AS transactions,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= %(m0)s AND date < %(m1)s) AS cur_med,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= %(myr)s AND date < %(myr1)s) AS yr_med
            FROM market_transactions
            WHERE (date >= %(m0)s AND date < %(m1)s) OR (date >= %(myr)s AND date < %(myr1)s)
            GROUP BY 1, 2
        )
        SELECT area, area_level, transactions::int AS recent_transactions,
               round(cur_med)::bigint AS median,
               round((100.0*(cur_med - yr_med)/NULLIF(yr_med,0))::numeric,1) AS yoy_change_pct
        FROM area_data
        WHERE area IS NOT NULL AND transactions >= %(min_tx)s
          AND cur_med IS NOT NULL AND yr_med IS NOT NULL
        ORDER BY yoy_change_pct {order_sql} NULLS LAST
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def _cheapest_areas(conn, lcm: date, area_level="both", min_transactions=100, limit=12):
    """Lowest absolute median over the last 12 complete months (entry-level areas), with a
    volume floor so thin areas don't dominate."""
    end = _month_first(lcm)
    params = {"start": end - relativedelta(months=11), "end_excl": end + relativedelta(months=1),
              "min_tx": int(min_transactions), "limit": max(1, min(int(limit), 25))}
    level_filter = {"county": "area_level = 'county'", "district": "area_level = 'district'",
                    "both": "TRUE"}.get(area_level, "TRUE")
    sql = f"""
        WITH base AS (
            SELECT CASE WHEN UPPER(county)='GREATER LONDON' THEN district ELSE county END AS area,
                   CASE WHEN UPPER(county)='GREATER LONDON' THEN 'district' ELSE 'county' END AS area_level,
                   price
            FROM market_transactions
            WHERE date >= %(start)s AND date < %(end_excl)s
              AND (UPPER(county) <> 'GREATER LONDON' OR district IS NOT NULL)
        )
        SELECT area, area_level, count(*)::int AS recent_transactions,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS median
        FROM base WHERE {level_filter} GROUP BY area, area_level
        HAVING count(*) >= %(min_tx)s
        ORDER BY median ASC
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


_SCAN_FOCUS = {"rising", "falling", "cheapest"}


async def scan_market(conn, focus="falling", area_level="both", limit=12):
    """Where should I look? Screen ALL counties + London boroughs to surface where the action
    is, by focus:
      • falling  — biggest YoY median fallers (cooling markets / potential entry points);
      • rising   — biggest YoY median gainers (hottest markets);
      • cheapest — lowest absolute median (entry-level areas).
    All figures are anchored on the last complete month and screened across all property types
    (for a budget+type query use find_affordable_areas)."""
    if focus not in _SCAN_FOCUS:
        focus = "falling"
    limit = max(1, min(int(limit), 25))
    lcm = await _last_complete_month(conn)
    if lcm is None:
        return {"focus": focus, "results": [], "meta": {"last_complete_month": None}}

    if focus == "cheapest":
        rows = await _cheapest_areas(conn, lcm, area_level=area_level, limit=limit)
        note = ("Lowest median over the last 12 complete months (all property types). "
                "Cheap on price alone — not a like-for-like value judgement.")
    else:
        rows = await _yoy_movers(conn, lcm, direction="gainers" if focus == "rising" else "fallers",
                                 limit=limit)
        note = (f"Biggest YoY median {'risers' if focus == 'rising' else 'fallers'} "
                f"(month {lcm.isoformat()} vs a year earlier), across counties and London "
                "boroughs. Smaller areas can swing on sale mix.")
    return {"focus": focus, "area_level": area_level,
            "price_basis": "median, all property types", "results": rows,
            "note": note, "meta": {"last_complete_month": lcm.isoformat()}}


async def find_affordable_areas(conn, budget, area_scope="london", county=None,
                                property_type="any", tenure="any",
                                min_transactions=100, limit=12):
    """Given a BUDGET, find where it actually buys (and where it goes furthest = best value).
    Per area: % of recent sales within budget (the budget's percentile = the value signal),
    the median (and flat median), and whether the median fits. Filterable by property_type
    (house = detached/semi/terraced, flat, or a specific type) and tenure (freehold/leasehold).
    Uses ABSOLUTE price vs budget (NOT current-vs-peak). NB: Land Registry has no bedroom
    count or floor area, so this CANNOT filter by bedrooms or compute £/m²."""
    budget = int(budget)
    lcm = await _last_complete_month(conn)
    end = _month_first(lcm or date.today())
    start = end - relativedelta(months=11)  # 12 complete months
    params = {"budget": budget, "start": start, "end_excl": end + relativedelta(months=1),
              "min_tx": int(min_transactions), "limit": max(1, min(int(limit), 30))}
    filters = []
    pg = _PGROUP_SQL.get(property_type)
    if pg:
        filters.append(pg)
    if tenure in ("freehold", "leasehold"):
        filters.append("tenure = %(tenure)s")
        params["tenure"] = "F" if tenure == "freehold" else "L"
    seg = "".join(" AND " + f for f in filters)

    if area_scope == "county":
        if not county:
            return {"error": "area_scope='county' requires a 'county' name"}
        area_expr, area_level = "district", "'district'"
        scope_filter = "UPPER(county) = UPPER(%(county)s) AND district IS NOT NULL"
        params["county"] = county
    elif area_scope == "all":
        area_expr = "CASE WHEN UPPER(county)='GREATER LONDON' THEN district ELSE county END"
        area_level = "CASE WHEN UPPER(county)='GREATER LONDON' THEN 'district' ELSE 'county' END"
        scope_filter = "(UPPER(county) <> 'GREATER LONDON' OR district IS NOT NULL)"
    else:  # 'london' (~ within the M25)
        area_expr, area_level = "district", "'district'"
        scope_filter = "UPPER(county) = 'GREATER LONDON' AND district IS NOT NULL"

    sql = f"""
        WITH base AS (
            SELECT {area_expr} AS area, {area_level} AS area_level, price, property_type
            FROM market_transactions
            WHERE date >= %(start)s AND date < %(end_excl)s AND {scope_filter}{seg}
        )
        SELECT area, area_level, count(*)::int AS sales,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS median,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price) FILTER (WHERE property_type='F')::bigint AS median_flat,
               round(100.0 * count(*) FILTER (WHERE price <= %(budget)s) / count(*), 1) AS pct_within_budget,
               (percentile_cont(0.5) WITHIN GROUP (ORDER BY price) <= %(budget)s) AS median_within_budget
        FROM base
        GROUP BY area, area_level
        HAVING count(*) >= %(min_tx)s
        ORDER BY pct_within_budget DESC, median ASC
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    any_fit = any(r["median_within_budget"] for r in rows)
    return {
        "budget": budget, "area_scope": area_scope,
        "property_type": property_type, "tenure": tenure,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "any_area_median_within_budget": any_fit,
        "note": (None if any_fit else
                 "No area in this scope has a median at/below the budget — it only reaches the "
                 "cheaper end (see pct_within_budget and median_flat). Consider cheaper property "
                 "types, a wider area_scope, or areas outside this scope."),
        "data_note": ("Land Registry has no bedroom count or floor area: results are NOT filtered "
                      "by bedrooms and there is no £/m² value figure. 'pct_within_budget' is the "
                      "budget's percentile for the chosen type+tenure (higher = your money buys a "
                      "more typical/better home there = better value)."),
        "results": rows,
        "meta": {"last_complete_month": lcm.isoformat() if lcm else None},
    }


async def run_sql(conn, sql, max_rows=None):
    """Escape hatch: run a validated, read-only SELECT for questions the typed tools
    don't cover (e.g. postcode/outcode grouping). Executed under the agent_ro role."""
    cap = min(int(max_rows or AGENT_SQL_ROW_LIMIT), AGENT_SQL_ROW_LIMIT)
    ok, cleaned = validate_readonly_sql(sql)
    if not ok:
        return {"error": cleaned, "rows": []}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(wrap_with_limit(cleaned, cap))
            rows = await cur.fetchall()
            cols = [d.name for d in cur.description] if cur.description else []
    except Exception as e:
        return {"error": f"SQL failed: {str(e).splitlines()[0]}", "executed_sql": cleaned, "rows": []}
    return {"columns": cols, "row_count": len(rows), "truncated": len(rows) >= cap,
            "executed_sql": cleaned, "rows": rows}
