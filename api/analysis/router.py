import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .agent import run_agent
from .tools import REGISTRY, call_tool

router = APIRouter()

_TOTAL_TIMEOUT_S = 90


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


@router.post("/ask")
async def ask(req: AskRequest):
    """Natural-language question over 30 years of sold-price data. The agent picks
    analysis tools, runs them, and answers with the exact figures (echoed in
    `observations` for audit)."""
    try:
        return await asyncio.wait_for(run_agent(req.question), timeout=_TOTAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Question timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Agent failed: {e}")


@router.get("/analysis/tools")
async def list_tools():
    """Inspect the available analysis tools."""
    return {"tools": [{"name": t.name, "description": t.description,
                       "parameters": t.parameters} for t in REGISTRY.values()]}


@router.post("/analysis/call/{tool_name}")
async def call_one(tool_name: str, arguments: dict | None = None):
    """Call a single analysis tool directly (debugging / the truth-test in verification)."""
    if tool_name not in REGISTRY:
        raise HTTPException(404, f"unknown tool '{tool_name}'")
    return await call_tool(tool_name, arguments or {})
