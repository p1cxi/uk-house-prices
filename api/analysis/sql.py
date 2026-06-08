"""Analysis tool bodies — pure async query functions over the existing schema.

Design rules (see plan):
- Area-scoped medians come from raw `market_transactions` (true PERCENTILE_CONT),
  never from averaging `monthly_price_stats.median_price` (the median-aggregation trap).
- The all-areas screen (`rank_areas`) uses the matview for speed, with a
  volume-weighted MEAN as the price level (exact from per-segment means) — labelled
  as a mean, not a median, to stay honest.
- Registration lag: a global `last_complete_month` (most recent month whose volume is
  >= 40% of the trailing-12-month median monthly volume) trims time series by default;
  tools set meta.incomplete_recent when a request reaches past it.
- Every value the LLM can influence is a BOUND parameter; only validated enums
  (granularity, area_level) are ever interpolated.
"""
from datetime import date

from dateutil.relativedelta import relativedelta
from psycopg.rows import dict_row

from ..config import AGENT_SQL_ROW_LIMIT
from .guards import validate_readonly_sql, wrap_with_limit

PROPERTY_TYPES = {"D", "S", "T", "F", "O"}
TENURES = {"F", "L"}
NEW_BUILD = {"Y", "N"}
GRANULARITY = {"month", "quarter", "year"}
PTYPE_LABEL = {"D": "detached", "S": "semi-detached", "T": "terraced", "F": "flat", "O": "other"}

_DEFAULT_WINDOW = {"month": relativedelta(months=24), "quarter": relativedelta(years=8),
                   "year": relativedelta(years=30)}

# Whole-country/region names that are NOT a single area the area-scoped tools handle.
# When the LLM passes one of these (e.g. "in the UK"), redirect it to the right tool
# instead of silently returning empty data.
_COUNTRY_TERMS = {"UK", "U.K.", "THE UK", "UNITED KINGDOM", "ENGLAND", "WALES",
                  "ENGLAND AND WALES", "GREAT BRITAIN", "BRITAIN", "GB",
                  "NATIONWIDE", "ANYWHERE"}


def _is_country(area) -> bool:
    return bool(area) and str(area).strip().upper() in _COUNTRY_TERMS


def _country_guard(area):
    """Return a redirecting error dict if `area` is a whole country/region, else None."""
    if _is_country(area):
        return {"error": f"'{area}' is a whole country/region, not a single area this tool handles. "
                         "For nationwide budget / 'best value' / cheapest questions use "
                         "find_affordable_areas (area_scope='all'); to rank or screen areas use "
                         "rank_areas; otherwise pass a specific county (e.g. KENT) or London "
                         "borough (e.g. BEXLEY)."}
    return None


def _area_where(area_level: str, param: str = "area") -> str:
    if area_level == "county":
        return f"UPPER(county) = UPPER(%({param})s)"
    if area_level == "district":
        return f"UPPER(district) = UPPER(%({param})s)"
    return (f"(UPPER(county) = UPPER(%({param})s) "
            f"OR (UPPER(county) = 'GREATER LONDON' AND UPPER(district) = UPPER(%({param})s)))")


def _segment_clauses(params: dict, property_type="all", tenure="all", new_build="all") -> list:
    clauses = []
    if property_type and property_type != "all":
        clauses.append("property_type = %(property_type)s"); params["property_type"] = property_type
    if tenure and tenure != "all":
        clauses.append("tenure = %(tenure)s"); params["tenure"] = tenure
    if new_build and new_build != "all":
        clauses.append("new_build = %(new_build)s"); params["new_build"] = new_build
    return clauses


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


async def get_area_trend(conn, area, area_level="auto", date_from=None, date_to=None,
                         granularity="month", property_type="all", tenure="all",
                         new_build="all", metric="median", include_incomplete=False):
    """Time series of median/mean/count for one area + segment over a date range."""
    guard = _country_guard(area)
    if guard:
        return guard
    if granularity not in GRANULARITY:
        granularity = "month"
    if metric not in {"median", "mean", "count"}:
        metric = "median"
    lcm = await _last_complete_month(conn)
    d_to = _parse_date(date_to, lcm or date.today())
    if not include_incomplete and lcm:
        d_to = min(d_to, lcm)
    d_from = _parse_date(date_from, _month_first(d_to) - _DEFAULT_WINDOW[granularity])
    incomplete = bool(lcm and _parse_date(date_to, lcm) > lcm and include_incomplete)

    params = {"area": area, "gran": granularity,
              "pfrom": _month_first(d_from), "pto": _month_first(d_to)}
    where = [_area_where(area_level), "date_trunc(%(gran)s, date)::date >= %(pfrom)s",
             "date_trunc(%(gran)s, date)::date <= %(pto)s"]
    where += _segment_clauses(params, property_type, tenure, new_build)
    sql = f"""
        SELECT date_trunc(%(gran)s, date)::date AS period,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS median,
               avg(price)::bigint AS mean,
               count(*)::int AS transactions
        FROM market_transactions
        WHERE {' AND '.join(where)}
        GROUP BY 1 ORDER BY 1
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    points = [{"period": r["period"].isoformat(),
               "value": int(r[metric]) if r[metric] is not None else None,
               "transactions": r["transactions"]} for r in rows]
    return {
        "area": area, "area_level": area_level, "metric": metric, "granularity": granularity,
        "segment": {"property_type": property_type, "tenure": tenure, "new_build": new_build},
        "range": {"from": _month_first(d_from).isoformat(), "to": _month_first(d_to).isoformat()},
        "points": points,
        "meta": {"last_complete_month": lcm.isoformat() if lcm else None, "incomplete_recent": incomplete},
    }


async def get_area_profile(conn, area, area_level="auto", as_of=None):
    """Latest snapshot for one area: headline median, MoM/YoY, breakdown by property type."""
    guard = _country_guard(area)
    if guard:
        return guard
    lcm = await _last_complete_month(conn)
    month = _month_first(_parse_date(as_of, lcm or date.today()))
    where_area = _area_where(area_level)

    async def median_for(m_start):
        return await _scalar(conn, f"""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint
            FROM market_transactions
            WHERE {where_area} AND date >= %(m)s AND date < (%(m)s::date + INTERVAL '1 month')
        """, {"area": area, "m": m_start})

    cur_med = await median_for(month)
    prev_med = await median_for(month - relativedelta(months=1))
    yoy_med = await median_for(month - relativedelta(years=1))

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"""
            SELECT property_type,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS median,
                   count(*)::int AS transactions
            FROM market_transactions
            WHERE {where_area} AND date >= %(m)s AND date < (%(m)s::date + INTERVAL '1 month')
            GROUP BY property_type ORDER BY transactions DESC
        """, {"area": area, "m": month})
        by_type = await cur.fetchall()
        await cur.execute(f"""
            SELECT
                count(*)::int AS total,
                count(*) FILTER (WHERE tenure = 'F')::int AS freehold,
                count(*) FILTER (WHERE new_build = 'Y')::int AS new_build
            FROM market_transactions
            WHERE {where_area} AND date >= %(m)s AND date < (%(m)s::date + INTERVAL '1 month')
        """, {"area": area, "m": month})
        mix = await cur.fetchone()

    total = mix["total"] if mix else 0
    if not total:
        return {"error": f"No transactions found for '{area}' — it may not be a known county or "
                         "London borough (names match Land Registry, e.g. KENT, BEXLEY). For "
                         "nationwide or budget/'best value' questions use find_affordable_areas."}
    pct = lambda n: round(100.0 * n / total, 1) if total else None
    mom = round(100.0 * (cur_med - prev_med) / prev_med, 1) if cur_med and prev_med else None
    yoy = round(100.0 * (cur_med - yoy_med) / yoy_med, 1) if cur_med and yoy_med else None
    return {
        "area": area, "area_level": area_level, "reporting_month": month.isoformat(),
        "headline_median": cur_med, "transactions": total,
        "mom_change_pct": mom, "yoy_change_pct": yoy,
        "by_property_type": [{"type": PTYPE_LABEL.get(r["property_type"], r["property_type"]),
                              "median": r["median"], "transactions": r["transactions"],
                              "share_pct": pct(r["transactions"])} for r in by_type],
        "freehold_pct": pct(mix["freehold"]) if mix else None,
        "new_build_pct": pct(mix["new_build"]) if mix else None,
        "meta": {"last_complete_month": lcm.isoformat() if lcm else None,
                 "incomplete_recent": bool(lcm and month > lcm)},
    }


async def _index_series(conn, area, area_level, base_period, d_from, d_to, property_type):
    where = [_area_where(area_level)]
    params = {"area": area, "from": _month_first(d_from), "to": _month_first(d_to)}
    where += _segment_clauses(params, property_type=property_type)
    rows_sql = f"""
        SELECT date_trunc('month', date)::date AS month,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS median
        FROM market_transactions
        WHERE {' AND '.join(where)}
          AND date >= %(from)s AND date < (%(to)s::date + INTERVAL '1 month')
        GROUP BY 1 ORDER BY 1
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(rows_sql, params)
        rows = await cur.fetchall()
    if not rows:
        return None
    base_month = _month_first(base_period)
    base_val = next((r["median"] for r in rows if r["month"] == base_month), None)
    if not base_val:
        # nearest available month to the requested base
        base_val = min(rows, key=lambda r: abs((r["month"] - base_month).days))["median"]
    series = [{"month": r["month"].isoformat(), "median": r["median"],
               "index": round(100.0 * r["median"] / base_val, 1)} for r in rows]
    peak = max(series, key=lambda p: p["index"])
    current = series[-1]
    # Summary only — the full monthly series is large and would blow the LLM context.
    return {
        "area": area, "area_level": area_level, "base_period": base_month.isoformat(),
        "base_median": int(base_val),
        "current": current, "peak": peak,
        "current_vs_peak_pct": round(100.0 * (current["index"] / peak["index"]) - 100, 1),
        "months_observed": len(series),
    }


async def get_price_index(conn, areas, base_period, date_from=None, date_to=None,
                          property_type="all", area_level="auto", include_incomplete=False):
    """Rebased price index (base month = 100) for one or more areas. Answers
    'still below 2022 peak?' and 'recovered fastest after the dip?'."""
    if isinstance(areas, str):
        areas = [areas]
    areas = [a for a in (areas or []) if not _is_country(a)]
    if not areas:
        return {"error": "Pass specific areas (counties or London boroughs), not a whole "
                         "country/region. For nationwide budget questions use find_affordable_areas."}
    lcm = await _last_complete_month(conn)
    d_to = _parse_date(date_to, lcm or date.today())
    if not include_incomplete and lcm:
        d_to = min(d_to, lcm)
    d_from = _parse_date(date_from, date(1995, 1, 1))
    base = _parse_date(base_period, d_from)
    out = []
    for a in areas[:8]:
        s = await _index_series(conn, a, area_level, base, d_from, d_to, property_type)
        if s:
            out.append(s)
    return {"base_period": _month_first(base).isoformat(), "property_type": property_type,
            "metric": "median", "areas": out,
            "meta": {"last_complete_month": lcm.isoformat() if lcm else None,
                     "incomplete_recent": bool(lcm and _parse_date(date_to, lcm) > lcm and include_incomplete)}}


async def compare_areas(conn, areas, metric="median", months=12, property_type="all",
                        area_level="auto"):
    """Side-by-side: start value, end value and % change over the last N months."""
    if isinstance(areas, str):
        areas = [areas]
    areas = [a for a in (areas or []) if not _is_country(a)]
    if not areas:
        return {"error": "Pass specific areas (counties or London boroughs), not a whole "
                         "country/region. For nationwide budget questions use find_affordable_areas."}
    if metric not in {"median", "mean"}:
        metric = "median"
    lcm = await _last_complete_month(conn)
    end = _month_first(lcm or date.today())
    start = end - relativedelta(months=max(1, int(months)))
    agg = "percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint" if metric == "median" else "avg(price)::bigint"
    results = []
    for a in areas[:8]:
        where = _area_where(area_level)
        async def val(m):
            return await _scalar(conn, f"""
                SELECT {agg} FROM market_transactions
                WHERE {where} AND date >= %(m)s AND date < (%(m)s::date + INTERVAL '1 month')
            """, {"area": a, "m": m})
        sv, ev = await val(start), await val(end)
        results.append({"area": a, "start_month": start.isoformat(), "start_value": sv,
                        "end_month": end.isoformat(), "end_value": ev,
                        "change_pct": round(100.0 * (ev - sv) / sv, 1) if sv and ev else None})
    return {"metric": metric, "months": months, "area_level": area_level, "areas": results,
            "meta": {"last_complete_month": lcm.isoformat() if lcm else None, "incomplete_recent": False}}


async def rank_areas(conn, metric="current_vs_peak_pct", area_level="both", peak_since=None,
                     property_type="all", min_transactions=50, where_current_vs_peak_lt=None,
                     where_volume_momentum_gt=None, order="desc", limit=20):
    """Screen/rank ALL areas by a computed metric. Price level is a true MEDIAN from raw
    transactions (outlier-robust), with a per-month volume floor so thin months can't create
    spurious peaks. Answers 'below 2022 peak AND volume recovering'."""
    lcm = await _last_complete_month(conn)
    if lcm is None:
        return {"results": [], "meta": {"last_complete_month": None}}
    limit = max(1, min(int(limit), 50))
    params = {"peak_since": _month_first(_parse_date(peak_since, date(2022, 1, 1))),
              "lcm": _month_first(lcm), "min_tx": int(min_transactions), "min_month_tx": 30,
              "lt": where_current_vs_peak_lt, "gt": where_volume_momentum_gt, "limit": limit}
    seg_sql = ""
    if property_type and property_type != "all":
        seg_sql = " AND property_type = %(property_type)s"; params["property_type"] = property_type
    level_filter = {"county": "area_level = 'county'", "district": "area_level = 'district'",
                    "both": "TRUE"}.get(area_level, "TRUE")
    order_sql = "ASC" if order == "asc" else "DESC"
    metric_col = {"current_vs_peak_pct": "current_vs_peak_pct",
                  "volume_momentum_pct": "volume_momentum_pct",
                  "growth_pct": "current_vs_peak_pct"}.get(metric, "current_vs_peak_pct")

    sql = f"""
        WITH base AS (
            SELECT date_trunc('month', date)::date AS month,
                   CASE WHEN UPPER(county)='GREATER LONDON' THEN district ELSE county END AS area,
                   CASE WHEN UPPER(county)='GREATER LONDON' THEN 'district' ELSE 'county' END AS area_level,
                   price
            FROM market_transactions
            WHERE date >= %(peak_since)s
              AND (UPPER(county) <> 'GREATER LONDON' OR district IS NOT NULL){seg_sql}
        ),
        am AS (   -- per area-month TRUE median + count; drop thin months so peaks aren't noise
            SELECT area, area_level, month,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS med,
                   count(*) AS cnt
            FROM base GROUP BY area, area_level, month
            HAVING count(*) >= %(min_month_tx)s
        ),
        peak AS (
            SELECT area, area_level, MAX(med) AS peak_med FROM am GROUP BY area, area_level
        ),
        cur AS (
            -- Volume momentum is trailing-12-months vs the prior-12-months. Annual windows
            -- (not 3-month) so registration lag near the data edge doesn't bias every area
            -- negative — recent months are under-registered vs a fully-settled year ago.
            SELECT area, area_level,
                   (array_agg(med ORDER BY month DESC))[1] AS current_med,
                   SUM(cnt) FILTER (WHERE month >  %(lcm)s - INTERVAL '12 months')  AS vol_recent,
                   SUM(cnt) FILTER (WHERE month >  %(lcm)s - INTERVAL '24 months'
                                      AND month <= %(lcm)s - INTERVAL '12 months') AS vol_year_ago
            FROM am WHERE month <= %(lcm)s GROUP BY area, area_level
        )
        SELECT c.area, c.area_level, round(c.current_med)::bigint AS current_median,
               round(p.peak_med)::bigint AS peak_median,
               round((100.0*(c.current_med - p.peak_med)/NULLIF(p.peak_med,0))::numeric,1) AS current_vs_peak_pct,
               round((100.0*(c.vol_recent - c.vol_year_ago)/NULLIF(c.vol_year_ago,0))::numeric,1) AS volume_momentum_pct,
               c.vol_recent::int AS recent_transactions
        FROM cur c JOIN peak p USING (area, area_level)
        WHERE {level_filter}
          AND c.vol_recent >= %(min_tx)s
          AND (%(lt)s::numeric IS NULL OR 100.0*(c.current_med-p.peak_med)/NULLIF(p.peak_med,0) < %(lt)s)
          AND (%(gt)s::numeric IS NULL OR 100.0*(c.vol_recent-c.vol_year_ago)/NULLIF(c.vol_year_ago,0) > %(gt)s)
        ORDER BY {metric_col} {order_sql} NULLS LAST
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return {"metric": metric, "price_level": "median", "peak_since": params["peak_since"].isoformat(),
            "area_level": area_level, "results": rows, "meta": {"last_complete_month": lcm.isoformat()}}


async def get_market_movers(conn, change_type="yoy", direction="both", min_transactions=10, limit=10):
    """Latest MoM/YoY gainers and fallers across counties + London boroughs."""
    if change_type not in {"mom", "yoy"}:
        change_type = "yoy"
    limit = max(1, min(int(limit), 25))
    change_col = "mom_change_pct" if change_type == "mom" else "yoy_change_pct"
    sql = f"""
        WITH area_data AS (
            SELECT county AS area_name, 'County' AS area_type,
                   COUNT(*) FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                       AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') AS transactions,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') AS cur_med,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                           AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') AS prev_med,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
                           AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '12 months') AS yr_med
            FROM transactions
            WHERE ppd_type='A' AND record_status='A'
              AND date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
              AND UPPER(county) <> 'GREATER LONDON'
            GROUP BY county
            UNION ALL
            SELECT district AS area_name, 'London Borough' AS area_type,
                   COUNT(*) FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                       AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') AS transactions,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') AS cur_med,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                           AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') AS prev_med,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                       FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
                           AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '12 months') AS yr_med
            FROM transactions
            WHERE ppd_type='A' AND record_status='A'
              AND date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
              AND UPPER(county) = 'GREATER LONDON' AND district IS NOT NULL
            GROUP BY district
        ),
        changes AS (
            SELECT area_name, area_type, transactions,
                   prev_med AS reference_median,
                   round((100.0*(prev_med - yr_med)/NULLIF(yr_med,0))::numeric,1) AS yoy_change_pct,
                   round((100.0*(cur_med - prev_med)/NULLIF(prev_med,0))::numeric,1) AS mom_change_pct
            FROM area_data
        )
        SELECT area_name, area_type, transactions, reference_median, mom_change_pct, yoy_change_pct
        FROM changes
        WHERE transactions >= %(min_tx)s AND {change_col} IS NOT NULL
        ORDER BY {change_col} {{order}}
        LIMIT %(limit)s
    """
    params = {"min_tx": int(min_transactions), "limit": limit}
    out = {}
    async with conn.cursor(row_factory=dict_row) as cur:
        if direction in ("gainers", "both"):
            await cur.execute(sql.format(order="DESC"), params)
            out["gainers"] = await cur.fetchall()
        if direction in ("fallers", "both"):
            await cur.execute(sql.format(order="ASC"), params)
            out["fallers"] = await cur.fetchall()
    out["change_type"] = change_type
    out["note"] = "Compares the latest registered month vs prior month (MoM) / same month last year (YoY)."
    return out


_PGROUP_SQL = {"house": "property_type IN ('D','S','T')", "flat": "property_type = 'F'",
               "detached": "property_type = 'D'", "semi": "property_type = 'S'",
               "terraced": "property_type = 'T'", "other": "property_type = 'O'"}


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
