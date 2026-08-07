"""SQL and result shaping for the monthly summary.

Queries `transactions` for county- and London-borough-level medians over the
last month vs the previous month vs the same month last year, filters to
TARGET_COUNTIES if set, and selects the notable rows (top by volume, top MoM/YoY
movers).
"""
import os
from typing import Dict, List, Optional, Tuple

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from ..config import DB_CONFIG


async def query_summary_data() -> Tuple[List[Dict], Optional[Dict]]:
    """Return (rows_per_area, actual_date_range) for the last complete month."""
    try:
        conn = await psycopg.AsyncConnection.connect(**DB_CONFIG)
        await conn.set_autocommit(True)

        target_counties = os.getenv("TARGET_COUNTIES")
        area_filter = ""
        if target_counties:
            areas = [f"'{area.strip().upper()}'" for area in target_counties.split(",")]
            area_filter = f"AND UPPER(area_name) IN ({','.join(areas)})"

        query = f"""
        WITH area_data AS (
            -- County-level data
            SELECT
                county as area_name,
                'County' as area_type,
                COUNT(*) FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                    AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') as transactions,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price)
                    FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') as current_month_median,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price)
                    FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                        AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') as prev_month_median,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price)
                    FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
                        AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '12 months') as same_month_last_year_median,
                to_char(date_trunc('month', CURRENT_DATE) - INTERVAL '1 month', 'FMMonth YYYY') as reporting_month
            FROM transactions
            WHERE ppd_type = 'A'
              AND record_status = 'A'
              AND date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
              AND county != 'GREATER LONDON'   -- county is stored UPPERCASE; no UPPER() (it defeats the index)
            GROUP BY county

            UNION ALL

            -- London borough data
            SELECT
                district as area_name,
                'London Borough' as area_type,
                COUNT(*) FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                    AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') as transactions,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price)
                    FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') as current_month_median,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price)
                    FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
                        AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month') as prev_month_median,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price)
                    FILTER (WHERE date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
                        AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '12 months') as same_month_last_year_median,
                to_char(date_trunc('month', CURRENT_DATE) - INTERVAL '1 month', 'FMMonth YYYY') as reporting_month
            FROM transactions
            WHERE ppd_type = 'A'
              AND record_status = 'A'
              AND date >= date_trunc('month', CURRENT_DATE) - INTERVAL '13 months'
              AND county = 'GREATER LONDON'    -- county is stored UPPERCASE; no UPPER() (it defeats the index)
              AND district IS NOT NULL
            GROUP BY district
        ),
        with_changes AS (
            SELECT *,
                ROUND((100.0 * (prev_month_median - same_month_last_year_median)
                    / NULLIF(same_month_last_year_median, 0))::NUMERIC, 1) as yoy_change_pct,
                ROUND((100.0 * (current_month_median - prev_month_median)
                    / NULLIF(prev_month_median, 0))::NUMERIC, 1) as mom_change_pct
            FROM area_data
        )
        SELECT area_name, area_type, transactions, current_month_median, prev_month_median,
               same_month_last_year_median, mom_change_pct, yoy_change_pct, reporting_month
        FROM with_changes
        WHERE transactions > 10
          {area_filter}
        ORDER BY transactions DESC;
        """

        date_range_query = """
        SELECT
            to_char(MIN(date), 'YYYY-MM-DD') as min_date,
            to_char(MAX(date), 'YYYY-MM-DD') as max_date
        FROM transactions
        WHERE ppd_type = 'A'
          AND record_status = 'A'
          AND date >= date_trunc('month', CURRENT_DATE) - INTERVAL '2 months'
          AND date < date_trunc('month', CURRENT_DATE) - INTERVAL '1 month'
        """

        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query)
            results = await cursor.fetchall()
            await cursor.execute(date_range_query)
            dr = await cursor.fetchone()

        await conn.close()
        date_range = {"from": dr["min_date"], "to": dr["max_date"]} if dr and dr["min_date"] else None
        return [dict(row) for row in results], date_range

    except Exception as e:
        raise HTTPException(500, f"Database query failed: {e}")


def select_notable(data: List[Dict]) -> Dict:
    """Pick the highlights the LLM briefing + response payload needs."""
    reporting_month = data[0].get("reporting_month") or "Unknown"
    total_tx = sum(int(r["transactions"]) for r in data)
    with_mom = [r for r in data if r["mom_change_pct"] is not None and r["current_month_median"]]
    with_yoy = [r for r in data if r["yoy_change_pct"] is not None and r["same_month_last_year_median"]]
    top_by_volume = data if len(data) <= 15 else data[:10]
    top_gainers = sorted(with_mom, key=lambda r: float(r["mom_change_pct"]), reverse=True)[:3]
    top_fallers = sorted(with_mom, key=lambda r: float(r["mom_change_pct"]))[:3]
    top_yoy_gainers = sorted(with_yoy, key=lambda r: float(r["yoy_change_pct"]), reverse=True)[:3]
    top_yoy_fallers = sorted([r for r in with_yoy if float(r["yoy_change_pct"]) < 0],
                             key=lambda r: float(r["yoy_change_pct"]))[:3]
    return {
        "reporting_month": reporting_month,
        "total_tx": total_tx,
        "with_mom": with_mom,
        "with_yoy": with_yoy,
        "top_by_volume": top_by_volume,
        "top_gainers": top_gainers,
        "top_fallers": top_fallers,
        "top_yoy_gainers": top_yoy_gainers,
        "top_yoy_fallers": top_yoy_fallers,
    }


def serialise_area(row: Dict) -> Dict:
    """Convert a raw DB row into the JSON shape returned by /summarise/monthly."""
    return {
        "area_name": (row["area_name"] or "Unknown").title(),
        "area_type": row["area_type"] or "Area",
        "transactions": int(row["transactions"]),
        "current_month_median": int(row["current_month_median"]) if row["current_month_median"] else None,
        "prev_month_median": int(row["prev_month_median"]) if row["prev_month_median"] else None,
        "same_month_last_year_median": int(row["same_month_last_year_median"]) if row["same_month_last_year_median"] else None,
        "mom_change_pct": float(row["mom_change_pct"]) if row["mom_change_pct"] is not None else None,
        "yoy_change_pct": float(row["yoy_change_pct"]) if row["yoy_change_pct"] is not None else None,
    }
