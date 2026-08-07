"""Monthly market-summary endpoint (SQL + LLM briefing).

The route lives in .routes; queries and prompt building are separate modules
so the SQL can be tested without touching the LLM and vice versa.
"""
from .routes import router

__all__ = ["router"]
