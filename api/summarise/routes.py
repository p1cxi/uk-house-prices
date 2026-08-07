"""HTTP route for /summarise/monthly.

Cached per reporting month via `_summary_cache`; the ingest routes clear this
cache on completion so a fresh briefing is generated after new data lands.
"""
from fastapi import APIRouter, HTTPException

from ..state import _summary_cache
from .prompt import generate_ai_summary
from .queries import query_summary_data, select_notable, serialise_area

router = APIRouter()


@router.post("/summarise/monthly")
async def summarise_monthly():
    try:
        data, date_range = await query_summary_data()
        if not data:
            raise HTTPException(503, "No data available for summary")
        notable = select_notable(data)
        cache_key = notable["reporting_month"]
        if cache_key in _summary_cache:
            return _summary_cache[cache_key]
        summary = await generate_ai_summary(notable)
        result = {
            "summary": summary,
            "data_period": notable["reporting_month"],
            "actual_date_range": date_range,
            "areas_analysed": len(data),
            "data": {
                "top_by_volume": [serialise_area(r) for r in notable["top_by_volume"]],
                "top_gainers": [serialise_area(r) for r in notable["top_gainers"]],
                "top_fallers": [serialise_area(r) for r in notable["top_fallers"]],
                "top_yoy_gainers": [serialise_area(r) for r in notable["top_yoy_gainers"]],
                "top_yoy_fallers": [serialise_area(r) for r in notable["top_yoy_fallers"]],
            },
        }
        _summary_cache[cache_key] = result
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Summary generation failed: {e}")
