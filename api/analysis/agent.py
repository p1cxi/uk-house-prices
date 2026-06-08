"""The /ask loop: plan (pick a tool) -> execute -> repeat -> synthesize.

Tool selection uses JSON-object-constrained output (robust on a local Qwen3, no
--jinja dependency). The synthesiser only ever sees exact numbers returned by the
tools, so answers are grounded and auditable.
"""
import json
import re

from fastapi import HTTPException

from ..config import AGENT_MAX_STEPS, AGENT_MAX_ROWS
from ..llm import chat, extract_json_object
from .tools import REGISTRY, call_tool, tool_catalog_text

# Small local models have a stale sense of "now" and invent date ceilings (e.g. date_to
# "2024-06"), capping answers at old data. Deterministic guard: drop any date-typed arg
# whose year the user didn't actually mention — the tools then default to the latest data.
_DATE_ARGS = ("date_from", "date_to", "base_period", "as_of", "peak_since")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _strip_unmentioned_dates(question: str, args: dict) -> dict:
    years = set(_YEAR_RE.findall(question))
    for k in _DATE_ARGS:
        v = args.get(k)
        if v is not None and str(v)[:4] not in years:
            args.pop(k, None)
    return args


# --- Deterministic budget pre-router -------------------------------------------------
# The flagship question ("if I had £200k, where's the best value?") is the one a small
# planner model routes WRONG most often — it reaches for get_area_profile('UK') (empty)
# or rank_areas (which ranks price-vs-peak DROPS, not affordability). Both produce
# confidently-wrong answers. So when a question carries a real budget AND value/where
# intent, we route to find_affordable_areas deterministically and skip the planner —
# no LLM tool-pick to get wrong. The PLANNER_SYS rule remains a fallback for budget
# questions phrased without a parseable figure ("where's cheap to buy?").
_BUDGET_INTENT = re.compile(
    r"\b(afford|budget|best value|value for money|for my money|cheapest|"
    r"where (?:can|could|should|to|would|do|'?s)|spend|looking to (?:buy|spend)|"
    r"buy a|get for|stretch|i (?:had|have|'ve got|got))\b", re.I)
# £200k · £200,000 · 200k · 200 grand · 200000 · 1.5m
_MONEY_RE = re.compile(r"£?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|grand|m|mil|million)?\b", re.I)


def _extract_budget(question: str):
    """Largest plausible GBP figure in the text (≥£10k filters out '2 bedroom'/'2022')."""
    best = None
    for m in _MONEY_RE.finditer(question):
        num = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        if unit in ("k", "grand"):
            num *= 1_000
        elif unit in ("m", "mil", "million"):
            num *= 1_000_000
        val = int(round(num))
        if 10_000 <= val <= 50_000_000 and (best is None or val > best):
            best = val
    return best


def _budget_route(question: str):
    """Args for find_affordable_areas if this is a budget/value question, else None."""
    if not _BUDGET_INTENT.search(question):
        return None
    budget = _extract_budget(question)
    if budget is None:
        return None
    ql = question.lower()
    scope = "london" if re.search(r"\b(london|m25)\b", ql) else "all"
    # tenure honours an explicit word; default 'any'.
    tenure = "leasehold" if "leasehold" in ql else "freehold" if "freehold" in ql else "any"
    # property_type ONLY from a SPECIFIC type word. Bare "house" is deliberately left 'any':
    # it's usually generic ("buy a house"), and a £200k 2-bed is mostly flats — narrowing to
    # houses would hide exactly the stock that fits the budget (the wrong-narrowing trap).
    if re.search(r"\b(flat|flats|apartment|apartments)\b", ql):
        ptype = "flat"
    elif re.search(r"\bdetached\b", ql):
        ptype = "detached"
    elif re.search(r"\bsemi[- ]?detached\b|\bsemi\b", ql):
        ptype = "semi"
    elif re.search(r"\b(terraced|terrace)\b", ql):
        ptype = "terraced"
    else:
        ptype = "any"
    return {"budget": budget, "area_scope": scope, "property_type": ptype, "tenure": tenure}

PLANNER_SYS = """You are the planning step of a UK house-price analytics agent. The database holds \
HM Land Registry SOLD prices for England & Wales, 1995-present (no rentals, no asking prices, no forecasts).

Reply with ONE JSON object and nothing else, exactly one of:
  {{"tool": "<tool_name>", "args": {{...}}}}     to call a tool
  {{"final": true}}                               when the observations already answer the question
  {{"final": true, "reason": "out_of_scope"}}     if it can't be answered from sold-price data

Available tools:
{catalog}

Rules:
- Choose the single most useful next tool; prefer the typed tools over run_sql.
- Area names match Land Registry values: counties like KENT, SURREY, WEST MIDLANDS; London boroughs like BEXLEY.
- get_area_profile / get_area_trend / get_price_index / compare_areas need a SPECIFIC county or London
  borough — NEVER a country/region ('UK', 'England', 'nationwide'). A question asking WHICH areas (best
  value, cheapest, where to buy/afford) is a screen: use find_affordable_areas (budget) or rank_areas.
- Only pass date parameters (date_from / date_to / base_period / as_of) that the user EXPLICITLY names.
  Otherwise omit them entirely — the tools default to the latest complete data. NEVER assume today's date.
- BUDGET / "what can I afford" / "best value for money" / "cheapest under £X": use
  find_affordable_areas (absolute price vs budget), NOT rank_areas (current_vs_peak_pct is price
  drops, not affordability). Set area_scope by geography: 'london' for London / "within the M25";
  'county' (+county) for a named county; 'all' for nationwide / "England" / "UK" / "anywhere".
  Pass property_type ('house'/'flat' or a specific type) and tenure ('freehold'/'leasehold') only
  when the user names them; otherwise leave them 'any'. There is NO bedroom or floor-area data —
  you cannot filter by bedrooms or compute £/m².
- Do not repeat a tool call already shown in the observations.
- When you have enough numbers, return {{"final": true}}."""

SYNTH_SYS = """You are a UK property market analyst. Answer the user's question using ONLY the figures \
in the OBSERVATIONS below — they are exact numbers from the database.

Rules:
- Never invent or estimate a figure. If the observations lack a number you need, say so plainly.
- Cite the actual figures inline (area names, £ medians, transaction counts, % changes). Format like £330,000 and +4.2%.
- Be concise: 1-4 sentences; use short bullets only when listing 3+ areas.
- If the observations are empty or contain an error, say you couldn't retrieve the figures rather than guessing.
- AFFORDABILITY: only call an area affordable / within budget if its median is at or below the user's
  budget. If no area's median fits (any_area_median_within_budget=false), say so plainly — point to where
  the budget reaches the most stock (highest pct_within_budget, e.g. cheaper flats) and/or suggest cheaper
  property types or areas outside the scope. Never imply an over-budget area suits the budget.
- NO BEDROOM / SIZE DATA: never claim results are filtered by number of bedrooms, and never report a
  £/m² or price-per-bedroom figure — that data isn't in Land Registry. If the user asked for a bedroom
  count or per-size value, give the budget/tenure/type answer you DO have and note that bedroom and
  floor-area filtering isn't available yet (planned via EPC data).
- {caveat}"""

CAVEAT_ON = ("One or more figures cover a very recent month; HM Land Registry data is registered with a lag, "
             "so the latest month(s) are incomplete and will rise — add ONE short sentence noting this.")
CAVEAT_OFF = "All figures cover complete periods; do not add any data-lag caveat."

OUT_OF_SCOPE = ("I can only answer from recorded sold-price data for England & Wales (1995-present). "
                "That question is outside what this dataset covers — it doesn't include rents, current "
                "asking prices, or future forecasts.")


def _truncate(value, depth=0):
    """Cap long lists so observations stay within the context budget."""
    if isinstance(value, list):
        capped = value[:AGENT_MAX_ROWS]
        out = [_truncate(v, depth + 1) for v in capped]
        if len(value) > AGENT_MAX_ROWS:
            out.append(f"...(+{len(value) - AGENT_MAX_ROWS} more rows truncated)")
        return out
    if isinstance(value, dict):
        return {k: _truncate(v, depth + 1) for k, v in value.items()}
    return value


def _compact(result):
    return _truncate(result)


def _planner_user(question, scratch, steps_left):
    obs = _observations_text(scratch) or "(none yet)"
    return (f"Question: {question}\n\nObservations so far:\n{obs}\n\n"
            f"You have {steps_left} tool call(s) left. Return the next JSON action.")


_OBS_CHAR_CAP = 800  # per-observation cap so the prompt can't blow a small context window


def _observations_text(scratch, cap=_OBS_CHAR_CAP):
    lines = []
    for i, s in enumerate(scratch, 1):
        if not s.get("tool"):
            continue
        res = json.dumps(s["result"], default=str)
        if len(res) > cap:
            res = res[:cap] + "...(truncated)"
        lines.append(f"[{i}] {s['tool']}({json.dumps(s['args'], default=str)}) -> {res}")
    return "\n".join(lines)


def _parse_action(raw):
    try:
        obj = json.loads(extract_json_object(raw))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("final"):
        return obj
    if obj.get("tool") in REGISTRY:
        if not isinstance(obj.get("args"), dict):
            obj["args"] = {}
        return obj
    return None


async def _plan(question, scratch, steps_left):
    sys = PLANNER_SYS.format(catalog=tool_catalog_text())
    msgs = [{"role": "system", "content": sys},
            {"role": "user", "content": _planner_user(question, scratch, steps_left)}]
    raw = await chat(msgs, max_tokens=256, temperature=0.0, enable_thinking=False, json_object=True)
    action = _parse_action(raw)
    if action is None:  # one repair attempt
        msgs += [{"role": "assistant", "content": raw},
                 {"role": "user", "content": "That was not a valid JSON action object. "
                  "Reply with ONLY the JSON object (a tool call or {\"final\": true})."}]
        raw = await chat(msgs, max_tokens=256, temperature=0.0, enable_thinking=False, json_object=True)
        action = _parse_action(raw)
    return action


async def _synthesize(question, scratch):
    if any(s.get("out_of_scope") for s in scratch):
        return OUT_OF_SCOPE
    incomplete = any(
        isinstance(s.get("result"), dict) and (s["result"].get("meta") or {}).get("incomplete_recent")
        for s in scratch
    )
    sys = SYNTH_SYS.format(caveat=CAVEAT_ON if incomplete else CAVEAT_OFF)
    # Retry with progressively tighter observation caps if the context window overflows
    # (small-slot llama.cpp returns HTTP 400 when the prompt exceeds n_ctx).
    for cap in (_OBS_CHAR_CAP, 350, 150):
        user = f"OBSERVATIONS:\n{_observations_text(scratch, cap) or '(no data retrieved)'}\n\nQUESTION: {question}"
        try:
            return await chat([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                              max_tokens=400, temperature=0.1, enable_thinking=False)
        except HTTPException as e:
            if e.status_code == 503 and "400" in str(e.detail):
                continue  # context overflow — shrink and retry
            raise
    return ("I found the figures but couldn't fit them into a single answer at the current model "
            "context size. Try a narrower question (one area or a shorter period).")


async def run_agent(question: str) -> dict:
    scratch = []
    used = set()

    # Deterministic fast path for budget/value questions — bypass the planner entirely
    # so it can't mis-route to get_area_profile('UK') or rank_areas (price-drop ≠ value).
    pre = _budget_route(question)
    if pre is not None:
        result = await call_tool("find_affordable_areas", pre)
        scratch.append({"tool": "find_affordable_areas", "args": pre, "result": _compact(result)})
        answer = await _synthesize(question, scratch)
        return {"answer": answer, "steps": 1, "observations": scratch, "routed": "budget"}

    for step in range(AGENT_MAX_STEPS):
        try:
            action = await _plan(question, scratch, AGENT_MAX_STEPS - step)
        except HTTPException:
            break  # LLM/context error mid-plan — answer from whatever we've gathered
        if action is None:
            break
        if action.get("final"):
            if action.get("reason") == "out_of_scope":
                scratch.append({"out_of_scope": True})
            break
        name = action["tool"]
        args = _strip_unmentioned_dates(question, action.get("args") or {})
        key = (name, json.dumps(args, sort_keys=True, default=str))
        if key in used:
            break
        used.add(key)
        result = await call_tool(name, args)
        scratch.append({"tool": name, "args": args, "result": _compact(result)})

    answer = await _synthesize(question, scratch)
    return {
        "answer": answer,
        "steps": sum(1 for s in scratch if s.get("tool")),
        "observations": scratch,
    }
