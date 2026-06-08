"""Shared llama.cpp client (OpenAI-compatible API).

Both the monthly summariser and the /ask analytics agent talk to the same
local llama.cpp server, so the HTTP call, thinking-suppression and <think>
stripping live here in one place.
"""
import re

import httpx
from fastapi import HTTPException

from .config import LLM_HOST, LLM_MODEL

# Qwen3 (and other reasoning models) wrap chain-of-thought in <think>...</think>.
# We disable thinking via the request, but strip it defensively in case a chat
# template ignores the flag.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def extract_json_object(text: str) -> str:
    """Return the first {...} block in text (lenient fallback if the model wraps JSON)."""
    text = strip_think(text)
    m = _JSON_OBJ_RE.search(text)
    return m.group(0) if m else text


async def chat(
    messages,
    *,
    max_tokens: int = 400,
    temperature: float = 0.1,
    enable_thinking: bool = False,
    json_object: bool = False,
    timeout: float = 120.0,
) -> str:
    """POST a chat completion to llama.cpp and return the assistant text (think-stripped).

    json_object=True asks the server to constrain output to a single JSON object
    (llama.cpp honours OpenAI-style response_format); we still validate/repair caller-side.
    """
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        # Keep the briefing/answer free of a reasoning block; honoured by llama.cpp.
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{LLM_HOST}/v1/chat/completions", json=payload)
            response.raise_for_status()
            result = response.json()
            try:
                content = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise HTTPException(503, "Invalid response format from AI service")
            return strip_think(content)
    except httpx.TimeoutException:
        raise HTTPException(503, "AI service timeout - please try again")
    except httpx.HTTPStatusError as e:
        raise HTTPException(503, f"AI service error: {e.response.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"AI service unavailable: {e}")
