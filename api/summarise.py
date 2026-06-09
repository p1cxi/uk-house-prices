import os
from typing import Dict, List, Optional, Tuple

import psycopg
from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row

from .config import DB_CONFIG
from .llm import chat
from .state import _summary_cache

router = APIRouter()


async def _query_summary_data() -> Tuple[List[Dict], Optional[Dict]]:
    try:
        conn = await psycopg.AsyncConnection.connect(**DB_CONFIG)
        await conn.set_autocommit(True)

        target_counties = os.getenv('TARGET_COUNTIES')
        area_filter = ""
        if target_counties:
            areas = [f"'{area.strip().upper()}'" for area in target_counties.split(',')]
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
        date_range = {"from": dr['min_date'], "to": dr['max_date']} if dr and dr['min_date'] else None
        return [dict(row) for row in results], date_range

    except Exception as e:
        raise HTTPException(500, f"Database query failed: {e}")


def _format_row(row: Dict) -> str:
    area = (row['area_name'] or 'Unknown').title()
    area_type = row['area_type'] or 'Area'
    tx = int(row['transactions'])
    median = row['current_month_median']
    median_str = f"median £{int(median):,}" if median else "median N/A"
    mom = row['mom_change_pct']
    line = f"{area} ({area_type}): {tx:,} tx, {median_str}"
    if mom is not None:
        line += f", {float(mom):+.1f}% MoM"
    return line


def _format_row_yoy(row: Dict) -> str:
    area = (row['area_name'] or 'Unknown').title()
    area_type = row['area_type'] or 'Area'
    yoy = float(row['yoy_change_pct'])
    prev_med = row['prev_month_median']
    prev_str = f", median £{int(prev_med):,}" if prev_med else ""
    return f"{area} ({area_type}){prev_str}, {yoy:+.1f}% YoY"


def _select_notable(data: List[Dict]) -> Dict:
    reporting_month = data[0].get('reporting_month') or 'Unknown'
    total_tx = sum(int(r['transactions']) for r in data)
    with_mom = [r for r in data if r['mom_change_pct'] is not None and r['current_month_median']]
    with_yoy = [r for r in data if r['yoy_change_pct'] is not None and r['same_month_last_year_median']]
    top_by_volume = data if len(data) <= 15 else data[:10]
    top_gainers = sorted(with_mom, key=lambda r: float(r['mom_change_pct']), reverse=True)[:3]
    top_fallers = sorted(with_mom, key=lambda r: float(r['mom_change_pct']))[:3]
    top_yoy_gainers = sorted(with_yoy, key=lambda r: float(r['yoy_change_pct']), reverse=True)[:3]
    top_yoy_fallers = sorted([r for r in with_yoy if float(r['yoy_change_pct']) < 0], key=lambda r: float(r['yoy_change_pct']))[:3]
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


def _serialise_area(row: Dict) -> Dict:
    return {
        "area_name": (row['area_name'] or 'Unknown').title(),
        "area_type": row['area_type'] or 'Area',
        "transactions": int(row['transactions']),
        "current_month_median": int(row['current_month_median']) if row['current_month_median'] else None,
        "prev_month_median": int(row['prev_month_median']) if row['prev_month_median'] else None,
        "same_month_last_year_median": int(row['same_month_last_year_median']) if row['same_month_last_year_median'] else None,
        "mom_change_pct": float(row['mom_change_pct']) if row['mom_change_pct'] is not None else None,
        "yoy_change_pct": float(row['yoy_change_pct']) if row['yoy_change_pct'] is not None else None,
    }


async def _generate_ai_summary(notable: Dict) -> str:
    def section(rows: List[Dict]) -> str:
        return "\n".join(f"  {_format_row(r)}" for r in rows)

    def section_yoy(rows: List[Dict]) -> str:
        return "\n".join(f"  {_format_row_yoy(r)}" for r in rows)

    with_mom = notable['with_mom']
    with_yoy = notable['with_yoy']

    movers_block = (
        f"\nBiggest price increases (MoM):\n{section(notable['top_gainers'])}\n\n"
        f"Biggest price falls (MoM):\n{section(notable['top_fallers'])}"
        if with_mom else
        "\nNote: Month-over-month price comparison data is not yet available for this reporting period — do not comment on MoM price direction."
    )

    yoy_block = (
        f"\nBiggest year-on-year increases:\n{section_yoy(notable['top_yoy_gainers'])}\n\n"
        f"Biggest year-on-year falls:\n{section_yoy(notable['top_yoy_fallers'])}"
        if with_yoy else
        "\nNote: Year-on-year comparison data is not available — do not comment on annual price direction."
    )

    sentence_3_instruction = (
        "Highlight the single most notable price mover (MoM or YoY) with its percentage."
        if (with_mom or with_yoy) else
        "State that price comparison data is not yet available for this period."
    )

    top_by_volume = notable['top_by_volume']
    prompt = f"""You are a UK property market analyst. Write a push notification briefing of exactly 3 sentences.

Rules:
- Use ONLY the numbers provided. Do not invent any figures.
- If a median is N/A, skip price for that area entirely — do not say "not available".
- Do not include labels like "Sentence 1" in your output — just write the sentences.

What each sentence must cover:
1. The reporting period, total transaction count, and number of areas monitored.
2. The top 2-3 areas by transaction volume, with counts and medians where available.
3. {sentence_3_instruction}

Data:
Reporting period: {notable['reporting_month']}
Total transactions: {notable['total_tx']:,}
Areas monitored: {len(top_by_volume)}

Markets by volume:
{section(top_by_volume)}
{movers_block}
{yoy_block}"""

    return await chat(
        [{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0.1,
        enable_thinking=False,
    )


@router.post("/summarise/monthly")
async def summarise_monthly():
    try:
        data, date_range = await _query_summary_data()
        if not data:
            raise HTTPException(503, "No data available for summary")
        notable = _select_notable(data)
        cache_key = notable["reporting_month"]
        if cache_key in _summary_cache:
            return _summary_cache[cache_key]
        summary = await _generate_ai_summary(notable)
        result = {
            "summary": summary,
            "data_period": notable["reporting_month"],
            "actual_date_range": date_range,
            "areas_analysed": len(data),
            "data": {
                "top_by_volume": [_serialise_area(r) for r in notable["top_by_volume"]],
                "top_gainers": [_serialise_area(r) for r in notable["top_gainers"]],
                "top_fallers": [_serialise_area(r) for r in notable["top_fallers"]],
                "top_yoy_gainers": [_serialise_area(r) for r in notable["top_yoy_gainers"]],
                "top_yoy_fallers": [_serialise_area(r) for r in notable["top_yoy_fallers"]],
            },
        }
        _summary_cache[cache_key] = result
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Summary generation failed: {e}")
