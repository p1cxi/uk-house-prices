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
- Floor area / £-per-m² / energy rating come from the EPC layer via the enriched view
  market_transactions_epc (LEFT JOIN, NULL where unmatched). Coverage is PARTIAL, so
  assess_value / find_affordable_areas report £/m² with its match rate and suppress it
  when too thin. Bedroom counts still do not exist — habitable rooms is only a proxy.
"""
from datetime import date

from dateutil.relativedelta import relativedelta
from psycopg.rows import dict_row

from ..config import AGENT_SQL_ROW_LIMIT
from .guards import validate_readonly_sql, wrap_with_limit

# Property types (shared by find_affordable_areas + assess_value). Each friendly token maps to
# one or more PPD property_type codes (D=detached, S=semi, T=terraced, F=flat, O=other). The
# property_type arg accepts a SINGLE token OR a LIST of tokens (OR'd together) so the model can
# pick and choose (e.g. ["flat","terraced"]); 'any' (or empty) means no filter.
_PTYPE_CODES = {"house": ("D", "S", "T"), "flat": ("F",), "detached": ("D",),
                "semi": ("S",), "terraced": ("T",), "other": ("O",)}
_PTYPE_ONE_LABEL = {"any": "all property types", "house": "houses (detached/semi/terraced)",
                    "flat": "flats", "detached": "detached houses", "semi": "semi-detached houses",
                    "terraced": "terraced houses", "other": "other property types"}


def _ptype_tokens(property_type):
    """Normalise the property_type arg (str | list | None) to a list of lowercased tokens."""
    if property_type is None:
        return []
    items = [property_type] if isinstance(property_type, str) else list(property_type)
    return [str(x).strip().lower() for x in items if x not in (None, "")]


def _ptype_codes(tokens):
    """The PPD codes to filter on for these tokens, or None for 'no filter' (any/empty/unknown)."""
    if not tokens or "any" in tokens:
        return None
    codes = sorted({c for t in tokens for c in _PTYPE_CODES.get(t, ())})
    return codes or None


def _ptype_label(tokens):
    """Human label for one or more property-type tokens ('flats or terraced houses')."""
    if not tokens or "any" in tokens:
        return "all property types"
    return " or ".join(_PTYPE_ONE_LABEL.get(t, t) for t in tokens)

# EPC-enriched view (market_transactions + matched floor area / £-per-m² / energy rating,
# NULL where unmatched). EPC coverage is PARTIAL, so £/m² is always reported with its match
# rate and the tools degrade to the price-only answer when coverage is too thin to trust.
_EPC_VIEW = "market_transactions_epc"
_SQM_MIN, _SQM_MAX = 20, 2000   # floor-area sanity band (drops 0 / sqft mis-entries / mansions)


def _coverage(matched, total, floor_pct=30, min_n=30):
    """(match_pct, trustworthy). Below the floor or min_n, £/m² is suppressed."""
    if not total or not matched:
        return (None, False)
    pct = round(100.0 * matched / total, 1)
    return (pct, matched >= min_n and pct >= floor_pct)

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
    # HM Land Registry county/district are already stored UPPERCASE (verified: 0 of ~31M rows
    # are mixed-case), so UPPER() goes on the BOUND PARAM only — never the column. Wrapping the
    # column (UPPER(county)=...) defeats the plain b-tree idx_transactions_county/_district and
    # forces a parallel seq scan of the whole transactions table (~6s/query for a London borough);
    # comparing the raw column lets the planner use the index (bitmap scan, ~5x faster).
    if area_level == "county":
        return f"county = UPPER(%({param})s)"
    if area_level == "district":
        return f"district = UPPER(%({param})s)"
    return (f"(county = UPPER(%({param})s) "
            f"OR (county = 'GREATER LONDON' AND district = UPPER(%({param})s)))")


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
        # EPC match coverage (share of sold transactions matched to an EPC certificate).
        await cur.execute("""
            SELECT sum(total_txns)::bigint AS total, sum(matched_txns)::bigint AS matched,
                   sum(total_txns)   FILTER (WHERE year >= DATE '2008-01-01')::bigint AS total_recent,
                   sum(matched_txns) FILTER (WHERE year >= DATE '2008-01-01')::bigint AS matched_recent
            FROM epc_match_coverage
        """)
        epc = await cur.fetchone()
    for r in recent:
        r["month"] = r["month"].isoformat()
        r["transactions"] = int(r["transactions"])
        r["considered_complete"] = (lcm is not None and r["month"] <= lcm.isoformat())
    epc_total = epc["total"] if epc else 0
    epc_recent = epc["total_recent"] if epc else 0
    return {
        "last_transaction_date": fresh["last_transaction_date"].isoformat() if fresh and fresh["last_transaction_date"] else None,
        "total_transactions": int(fresh["total_transactions"]) if fresh else 0,
        "last_complete_month": lcm.isoformat() if lcm else None,
        "note": "HM Land Registry registers sales with a lag; recent months marked incomplete will grow.",
        "recent_months": recent,
        "epc_match": {
            "matched_pct_all": round(100.0 * epc["matched"] / epc_total, 1) if epc_total else 0.0,
            "matched_pct_since_2008": round(100.0 * epc["matched_recent"] / epc_recent, 1) if epc_recent else 0.0,
            "note": ("Share of sold transactions matched to an EPC certificate (adds floor area / "
                     "£-per-m² / energy rating). 0% until the EPC bulk data is loaded."),
        },
    }


def _band(value, low, high, below, mid, above):
    if value is None:
        return None
    if value <= low:
        return below
    if value >= high:
        return above
    return mid


async def assess_value(conn, area, area_level="auto", property_type="any", candidate_price=None,
                       candidate_floor_area=None):
    """Is a place — or a specific asking price — good value or overpriced? Triangulates:
      • vs the area's OWN history: how far below/above its peak median, 12-month direction;
      • vs PEERS: the area's median vs the national typical for that property type;
      • vs the LOCAL distribution: a candidate_price's percentile among recent local sales;
      • vs £/m² (EPC-matched sales only, coverage permitting): the area's median £/m² vs national,
        and — given candidate_floor_area — the candidate's own £/m² and where it sits locally;
      • new-build vs resale: the median for each (and the new-build premium %) within this area+type;
      • energy: the EPC-band distribution (incl. % EPC-D or worse) + typical rating;
      • size spread: floor-area quartiles (Q1/median/Q3) and typical habitable rooms (a size proxy).
    £/m², size and energy come from EPC-matched sales only and are suppressed (price-only answer)
    when too few sales here are EPC-matched to trust; the new-build premium uses full coverage."""
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
    cand_fa = float(candidate_floor_area) if candidate_floor_area else None
    cand_ppsqm = round(cand / cand_fa) if (cand and cand_fa and cand_fa > 0) else None
    where_area = _area_where(area_level)
    tokens = _ptype_tokens(property_type)
    pt_codes = _ptype_codes(tokens)
    type_sql = " AND property_type = ANY(%(ptypes)s)" if pt_codes else ""
    params = {"area": area, "recent_start": recent_start, "prior_start": prior_start,
              "end_excl": end_excl, "cand": cand, "cand_ppsqm": cand_ppsqm, "ptypes": pt_codes,
              "sqm_min": _SQM_MIN, "sqm_max": _SQM_MAX, "min_month_tx": 30}

    async with conn.cursor(row_factory=dict_row) as cur:
        # 1) recent vs prior-year median + sales + candidate percentile, AND EPC £/m² (one scan)
        await cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE date >= %(recent_start)s)::int AS sales_recent,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                  FILTER (WHERE date >= %(recent_start)s)::bigint AS median_recent,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                  FILTER (WHERE date >= %(prior_start)s AND date < %(recent_start)s)::bigint AS median_prior,
              round(100.0 * count(*) FILTER (WHERE %(cand)s::int IS NOT NULL AND price <= %(cand)s::int
                                               AND date >= %(recent_start)s)
                    / NULLIF(count(*) FILTER (WHERE date >= %(recent_start)s), 0), 1) AS candidate_pctile,
              -- EPC-matched £/m² over the recent window (floor-area sanity band)
              count(*) FILTER (WHERE date >= %(recent_start)s AND price_per_sqm IS NOT NULL
                               AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::int AS sqm_matched,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_sqm)
                  FILTER (WHERE date >= %(recent_start)s
                          AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::bigint AS area_ppsqm,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY total_floor_area)
                  FILTER (WHERE date >= %(recent_start)s
                          AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::numeric AS median_floor_area,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY habitable_rooms)
                  FILTER (WHERE date >= %(recent_start)s AND habitable_rooms > 0)::numeric AS median_habitable_rooms,
              mode() WITHIN GROUP (ORDER BY current_energy_rating)
                  FILTER (WHERE date >= %(recent_start)s AND current_energy_rating IS NOT NULL) AS typical_rating,
              round(100.0 * count(*) FILTER (WHERE %(cand_ppsqm)s::int IS NOT NULL AND date >= %(recent_start)s
                               AND price_per_sqm IS NOT NULL AND price_per_sqm <= %(cand_ppsqm)s::int
                               AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)
                    / NULLIF(count(*) FILTER (WHERE date >= %(recent_start)s AND price_per_sqm IS NOT NULL
                               AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s), 0), 1) AS candidate_sqm_pctile,
              -- new-build vs resale (price premium within this area+type, recent window)
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                  FILTER (WHERE date >= %(recent_start)s AND new_build = 'Y')::bigint AS median_new,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY price)
                  FILTER (WHERE date >= %(recent_start)s AND new_build = 'N')::bigint AS median_resale,
              count(*) FILTER (WHERE date >= %(recent_start)s AND new_build = 'Y')::int AS sales_new,
              count(*) FILTER (WHERE date >= %(recent_start)s AND new_build = 'N')::int AS sales_resale,
              -- floor-area spread (EPC-matched, sanity band): quartiles around the median
              percentile_cont(0.25) WITHIN GROUP (ORDER BY total_floor_area)
                  FILTER (WHERE date >= %(recent_start)s
                          AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::numeric AS floor_area_q1,
              percentile_cont(0.75) WITHIN GROUP (ORDER BY total_floor_area)
                  FILTER (WHERE date >= %(recent_start)s
                          AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::numeric AS floor_area_q3,
              -- EPC energy-band distribution of rated EPC-matched recent sales (running-cost / EPC-C exposure)
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating IS NOT NULL)::int AS rated_recent,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'A')::int AS er_a,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'B')::int AS er_b,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'C')::int AS er_c,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'D')::int AS er_d,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'E')::int AS er_e,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'F')::int AS er_f,
              count(*) FILTER (WHERE date >= %(recent_start)s AND current_energy_rating = 'G')::int AS er_g
            FROM {_EPC_VIEW}
            WHERE {where_area} AND date >= %(prior_start)s AND date < %(end_excl)s{type_sql}
        """, params)
        rec = await cur.fetchone()

        if not rec or not rec["sales_recent"]:
            return {"error": f"No recent sales found for '{area}'"
                             + (f" ({_ptype_label(tokens)})" if pt_codes else "")
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

        # 3) national typical (price + £/m²) for this property type (trailing 12 complete months)
        await cur.execute(f"""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS peer_median,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_sqm)
                       FILTER (WHERE total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::bigint AS peer_ppsqm
            FROM {_EPC_VIEW}
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

    # £/m² — gated on EPC coverage for this area+type
    sqm_matched = rec["sqm_matched"]
    sqm_pct, sqm_trust = _coverage(sqm_matched, rec["sales_recent"])
    area_ppsqm = rec["area_ppsqm"] if sqm_trust else None
    peer_ppsqm = peer["peer_ppsqm"] if peer else None
    ppsqm_ratio = (round(area_ppsqm / peer_ppsqm, 2)
                   if (sqm_trust and area_ppsqm and peer_ppsqm) else None)
    median_floor_area = float(rec["median_floor_area"]) if rec["median_floor_area"] else None
    median_rooms = float(rec["median_habitable_rooms"]) if rec["median_habitable_rooms"] else None
    fa_q1 = float(rec["floor_area_q1"]) if rec["floor_area_q1"] else None
    fa_q3 = float(rec["floor_area_q3"]) if rec["floor_area_q3"] else None
    typical_rating = rec["typical_rating"]

    # New-build vs resale premium (within this area+type; full coverage, not EPC-gated).
    median_new, median_resale = rec["median_new"], rec["median_resale"]
    sales_new, sales_resale = rec["sales_new"] or 0, rec["sales_resale"] or 0
    new_block = None
    if sales_new >= 10 and sales_resale >= 10 and median_new and median_resale:
        new_block = {
            "new_build_median": median_new, "resale_median": median_resale,
            "premium_pct": round(100.0 * (median_new - median_resale) / median_resale, 1),
            "new_build_share_pct": round(100.0 * sales_new / (sales_new + sales_resale), 1),
            "new_build_sales": sales_new, "resale_sales": sales_resale,
            "note": "new-build vs resale median for this area+type; the gap reflects spec/age/size mix, "
                    "not a like-for-like uplift on the same home.",
        }

    # Energy-band distribution — gated on its own (rating) coverage, not the floor-area coverage.
    rated = rec["rated_recent"] or 0
    er_pct, er_trust = _coverage(rated, rec["sales_recent"])
    energy_block = None
    if er_trust and typical_rating:
        bands = {"A": rec["er_a"], "B": rec["er_b"], "C": rec["er_c"], "D": rec["er_d"],
                 "E": rec["er_e"], "F": rec["er_f"], "G": rec["er_g"]}
        c_or_better = bands["A"] + bands["B"] + bands["C"]
        d_or_worse = bands["D"] + bands["E"] + bands["F"] + bands["G"]
        energy_block = {
            "typical_rating": typical_rating,
            "rated_sales": rated, "match_pct": er_pct,
            "distribution_pct": {k: round(100.0 * v / rated, 1) for k, v in bands.items()},
            "pct_epc_c_or_better": round(100.0 * c_or_better / rated, 1),
            "pct_epc_d_or_worse": round(100.0 * d_or_worse / rated, 1),
            "note": "EPC ratings of recent EPC-matched sales (A=most efficient … G=least). "
                    "'% EPC-D or worse' is a running-cost / future-efficiency-rule exposure signal — "
                    "a stock proxy, not the candidate's own rating.",
        }

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
        if cand_ppsqm is not None:   # user supplied a floor area
            sqm_pctile = rec["candidate_sqm_pctile"]
            candidate["floor_area_m2"] = cand_fa
            candidate["price_per_sqm"] = cand_ppsqm
            candidate["sqm_percentile"] = float(sqm_pctile) if sqm_pctile is not None else None
            candidate["sqm_verdict"] = (
                _band(float(sqm_pctile), 35, 65,
                      "good value per m² — cheaper than most recent local sales",
                      "around the local £/m² going rate",
                      "expensive per m² vs recent local sales")
                if (sqm_trust and sqm_pctile is not None) else None)

    return {
        "area": area, "area_level": area_level,
        "property_type": property_type, "property_type_label": _ptype_label(tokens),
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
        "vs_sqm": {
            "area_median_ppsqm": area_ppsqm,
            "national_ppsqm": peer_ppsqm if sqm_trust else None,
            "ratio_to_national_ppsqm": ppsqm_ratio,
            "median_floor_area_m2": round(median_floor_area) if (sqm_trust and median_floor_area) else None,
            "floor_area_q1_m2": round(fa_q1) if (sqm_trust and fa_q1) else None,   # 25% of homes smaller
            "floor_area_q3_m2": round(fa_q3) if (sqm_trust and fa_q3) else None,   # 25% larger
            "typical_habitable_rooms": median_rooms if sqm_trust else None,  # size proxy, NOT bedrooms
            "verdict": _band(ppsqm_ratio, 0.85, 1.15, "cheaper per m² than the national typical",
                             "around the national typical per m²", "pricier per m² than the national typical")
            if ppsqm_ratio is not None else None,
            "coverage": {"matched_sales": sqm_matched, "recent_sales": rec["sales_recent"],
                         "match_pct": sqm_pct, "trustworthy": sqm_trust},
        },
        "energy": energy_block,
        "new_build": new_block,
        "candidate": candidate,
        "data_note": ("Relative value (the area's own history, national peers, the local price "
                      "distribution)"
                      + (f", plus £/m² vs the national typical from EPC-matched sales "
                         f"(~{sqm_pct}% of recent local sales matched)." if sqm_trust
                         else " — £/m² is not shown here: too few recent sales are EPC-matched to be reliable.")
                      + " Bedroom counts are not in the data; habitable rooms is only an approximate "
                        "size proxy, not a bedroom count."),
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
            SELECT CASE WHEN county='GREATER LONDON' THEN district ELSE county END AS area,
                   CASE WHEN county='GREATER LONDON' THEN 'district' ELSE 'county' END AS area_level,
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
            SELECT CASE WHEN county='GREATER LONDON' THEN district ELSE county END AS area,
                   CASE WHEN county='GREATER LONDON' THEN 'district' ELSE 'county' END AS area_level,
                   price
            FROM market_transactions
            WHERE date >= %(start)s AND date < %(end_excl)s
              AND (county <> 'GREATER LONDON' OR district IS NOT NULL)
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


async def find_affordable_areas(conn, budget, area_scope="all", county=None,
                                property_type="any", tenure="any", new_build="any",
                                min_floor_area=None, max_floor_area=None,
                                sort="affordability", min_transactions=100, limit=12):
    """Given a BUDGET, find where it actually buys (and where it goes furthest = best value).
    Per area: % of recent sales within budget (the value signal), the median (and flat median),
    whether the median fits, AND the median £/m² from EPC-matched sales. Filterable by
    property_type (house/flat/specific), tenure (freehold/leasehold) and new_build (new/resale).
    An OPTIONAL floor-area
    band (min/max m²) adds matched-only size-aware figures WITHOUT shrinking the headline budget
    answer (which stays over all sales). sort='ppsqm' ranks areas by cheapest median £/m².
    Uses ABSOLUTE price vs budget. NB: £/m² and size come from EPC-matched sales only (partial
    coverage); habitable rooms is an approximate size proxy, NOT a bedroom count."""
    budget = int(budget)
    sort = "ppsqm" if str(sort).lower() == "ppsqm" else "affordability"
    new_build = str(new_build).lower() if new_build else "any"
    min_fa = float(min_floor_area) if min_floor_area else None
    max_fa = float(max_floor_area) if max_floor_area else None
    lcm = await _last_complete_month(conn)
    end = _month_first(lcm or date.today())
    start = end - relativedelta(months=11)  # 12 complete months
    params = {"budget": budget, "start": start, "end_excl": end + relativedelta(months=1),
              "min_tx": int(min_transactions), "limit": max(1, min(int(limit), 30)),
              "sqm_min": _SQM_MIN, "sqm_max": _SQM_MAX, "min_fa": min_fa, "max_fa": max_fa}
    tokens = _ptype_tokens(property_type)
    pt_codes = _ptype_codes(tokens)
    filters = []
    if pt_codes:
        filters.append("property_type = ANY(%(ptypes)s)")
        params["ptypes"] = pt_codes
    if tenure in ("freehold", "leasehold"):
        filters.append("tenure = %(tenure)s")
        params["tenure"] = "F" if tenure == "freehold" else "L"
    if new_build in ("new", "resale"):
        filters.append("new_build = %(nb)s")
        params["nb"] = "Y" if new_build == "new" else "N"
    seg = "".join(" AND " + f for f in filters)

    # county/district are stored UPPERCASE (see _area_where): compare the raw, indexed column and
    # UPPER() only the bound param. UPPER(column)=... in a scope_filter would defeat
    # idx_transactions_county_date and force a seq scan (county/london scopes were ~10x slower).
    if area_scope == "county":
        if not county:
            return {"error": "area_scope='county' requires a 'county' name"}
        area_expr, area_level = "district", "'district'"
        scope_filter = "county = UPPER(%(county)s) AND district IS NOT NULL"
        params["county"] = county
    elif area_scope == "all":
        area_expr = "CASE WHEN county='GREATER LONDON' THEN district ELSE county END"
        area_level = "CASE WHEN county='GREATER LONDON' THEN 'district' ELSE 'county' END"
        scope_filter = "(county <> 'GREATER LONDON' OR district IS NOT NULL)"
    else:  # 'london' (~ within the M25)
        area_expr, area_level = "district", "'district'"
        scope_filter = "county = 'GREATER LONDON' AND district IS NOT NULL"

    # Rank by cheapest £/m² only over areas with enough EPC matches to be meaningful.
    sqm_having = ("" if sort != "ppsqm" else
                  " AND count(*) FILTER (WHERE price_per_sqm IS NOT NULL "
                  "AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s) >= 30")
    order_by = ("median_ppsqm ASC NULLS LAST, pct_within_budget DESC" if sort == "ppsqm"
                else "pct_within_budget DESC, median ASC")
    # Floor-area band predicate (matched rows only). NULL params => always-true (no filter).
    # ::numeric casts so Postgres can type the bound parameter when it's NULL.
    band = ("total_floor_area IS NOT NULL "
            "AND (%(min_fa)s::numeric IS NULL OR total_floor_area >= %(min_fa)s::numeric) "
            "AND (%(max_fa)s::numeric IS NULL OR total_floor_area <= %(max_fa)s::numeric)")
    sql = f"""
        WITH base AS (
            SELECT {area_expr} AS area, {area_level} AS area_level, price, property_type,
                   price_per_sqm, total_floor_area, habitable_rooms
            FROM {_EPC_VIEW}
            WHERE date >= %(start)s AND date < %(end_excl)s AND {scope_filter}{seg}
        )
        SELECT area, area_level, count(*)::int AS sales,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price)::bigint AS median,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price) FILTER (WHERE property_type='F')::bigint AS median_flat,
               round(100.0 * count(*) FILTER (WHERE price <= %(budget)s) / count(*), 1) AS pct_within_budget,
               (percentile_cont(0.5) WITHIN GROUP (ORDER BY price) <= %(budget)s) AS median_within_budget,
               -- EPC £/m² + size (matched sales only; sanity band on floor area)
               count(*) FILTER (WHERE price_per_sqm IS NOT NULL
                                AND total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::int AS sqm_matched,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_sqm)
                   FILTER (WHERE total_floor_area BETWEEN %(sqm_min)s AND %(sqm_max)s)::bigint AS median_ppsqm,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY habitable_rooms)
                   FILTER (WHERE habitable_rooms > 0)::numeric, 1) AS median_habitable_rooms,
               -- soft floor-area band: matched-only, does NOT shrink the headline pct_within_budget
               count(*) FILTER (WHERE {band})::int AS sqm_in_band,
               round(100.0 * count(*) FILTER (WHERE price <= %(budget)s AND {band})
                     / NULLIF(count(*) FILTER (WHERE {band}), 0), 1) AS pct_in_band_within_budget
        FROM base
        GROUP BY area, area_level
        HAVING count(*) >= %(min_tx)s{sqm_having}
        ORDER BY {order_by}
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    for r in rows:
        r["sqm_match_pct"] = round(100.0 * r["sqm_matched"] / r["sales"], 1) if r["sales"] else None
    any_fit = any(r["median_within_budget"] for r in rows)
    size_on = bool(min_fa or max_fa)
    if not rows:
        applied = ", ".join(
            x for x in (_ptype_label(tokens) if pt_codes else None,
                        tenure if tenure in ("freehold", "leasehold") else None,
                        f"{new_build}-build" if new_build in ("new", "resale") else None)
            if x)
        note = ("No areas matched these filters in this scope — too few sales clear the "
                f"min_transactions={int(min_transactions)} floor"
                + (f" for {applied}" if applied else "")
                + ". This combination is rare here (e.g. leasehold houses are uncommon, especially "
                  "in London). Relax tenure to 'any', try property_type='flat', widen area_scope, "
                  "or lower min_transactions — do NOT present this as 'no affordable areas'.")
    elif not any_fit:
        note = ("No area in this scope has a median at/below the budget — it only reaches the "
                "cheaper end (see pct_within_budget and median_flat). Consider cheaper property "
                "types, a wider area_scope, or areas outside this scope.")
    else:
        note = None
    return {
        "budget": budget, "area_scope": area_scope, "sort": sort,
        "property_type": property_type, "tenure": tenure, "new_build": new_build,
        "size_filter": {"min_m2": min_fa, "max_m2": max_fa} if size_on else None,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "any_area_median_within_budget": any_fit,
        "note": note,
        "data_note": ("'pct_within_budget' is the budget's percentile across ALL sales (full "
                      "coverage) for the chosen type+tenure — higher = your money buys a more "
                      "typical/better home there. 'median_ppsqm' and any size-band figures "
                      "(sqm_in_band / pct_in_band_within_budget) use EPC-matched sales only "
                      "(see sqm_match_pct per area) — treat as indicative. Habitable rooms is an "
                      "approximate size proxy, NOT a bedroom count; results are not filtered by bedrooms."),
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
