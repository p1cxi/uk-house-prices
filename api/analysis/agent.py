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

# Small local models have a stale sense of "now" and invent date ceilings, capping answers at
# old data. Deterministic guard: drop any date-typed arg whose year the user didn't actually
# mention — tools then default to the latest data. (Kept defensively; the current tool set
# takes no explicit date args, but run_sql or a future tool might.)
_DATE_ARGS = ("date_from", "date_to")
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
# planner model routes WRONG most often — historically it reached for a single-area profile
# of "UK" (empty) or a price-vs-peak screen (drops, not affordability), both confidently
# wrong. So when a question carries a real budget AND value/where intent, we route to
# find_affordable_areas deterministically and skip the planner —
# no LLM tool-pick to get wrong. The PLANNER_SYS rule remains a fallback for budget
# questions phrased without a parseable figure ("where's cheap to buy?").
_BUDGET_INTENT = re.compile(
    r"\b(afford|budget|best value|value for money|for my money|cheapest|"
    r"where (?:can|could|should|to|would|do|'?s)|spend|looking to (?:buy|spend)|"
    r"buy a|get for|stretch|i (?:had|have|'ve got|got)|"
    r"what (?:can|does|will|would|could)[^?]{0,25}buys?)\b", re.I)
# £200k · £200,000 · 200k · 200 grand · 200000 · 1.5m
_MONEY_RE = re.compile(r"£?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|grand|m|mil|million)?\b", re.I)
# Floor-area size tokens ("70 m²", "60-90 sqm") — EPC-backed; numbers here are never a budget (<10k).
_SIZE_UNIT = r"(?:m2|m²|sqm|sq\s?m|square\s?met(?:re|er)s?)"
_SIZE_RANGE_RE = re.compile(rf"\b(\d{{2,4}})\s*(?:-|to|–|—)\s*(\d{{2,4}})\s*{_SIZE_UNIT}\b", re.I)
_SIZE_ONE_RE = re.compile(rf"\b(\d{{2,4}})\s*{_SIZE_UNIT}\b", re.I)


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
    args = {"budget": budget, "area_scope": scope, "property_type": ptype, "tenure": tenure}
    # Optional floor-area size in a budget question ("60-90 m²", "around 70 sqm") — EPC-backed.
    rng = _SIZE_RANGE_RE.search(ql)
    if rng:
        lo, hi = sorted((int(rng.group(1)), int(rng.group(2))))
        args["min_floor_area"], args["max_floor_area"] = float(lo), float(hi)
    else:
        one = _SIZE_ONE_RE.search(ql)
        if one:
            v = int(one.group(1))
            args["min_floor_area"], args["max_floor_area"] = float(round(v * 0.85)), float(round(v * 1.15))
    return args

PLANNER_SYS = """You are the planning step of a UK house-price analytics agent. The database holds \
HM Land Registry SOLD prices for England & Wales, 1995-present (no rentals, no asking prices, no forecasts).

Reply with ONE JSON object and nothing else, exactly one of:
  {{"tool": "<tool_name>", "args": {{...}}}}     to call a tool
  {{"final": true}}                               when the observations already answer the question
  {{"final": true, "reason": "out_of_scope"}}     if it can't be answered from sold-price data

Available tools:
{catalog}

Rules — match the user's intent to ONE tool:
- BUDGET / "what can I afford" / "where does £X go" / "best value for my money" / "under £X":
  find_affordable_areas. Set area_scope by geography: 'london' for London / "within the M25";
  'county' (+county) for a named county; 'all' for nationwide / "England" / "UK" / "anywhere".
  Pass property_type ('house'/'flat'/a specific type) and tenure ('freehold'/'leasehold') only
  when the user names them; otherwise leave them 'any'.
- "Is X good value / overpriced?", "am I overpaying?", "is £Y for a <type> in <area> fair?":
  assess_value. Pass the specific area; add candidate_price (+property_type) when the user names a
  price/type. Needs a SPECIFIC county or London borough — never a country/region.
- "Where should I look?", "where are prices rising / falling?", "where are the cheapest areas?":
  scan_market with focus = falling / rising / cheapest.
- £/m², floor area, "value per m²", "is £X for an N m² place good value", "cheapest by £/m²":
  these are VALUE questions. ONE area or a specific price+size -> assess_value (pass candidate_price
  AND candidate_floor_area when the user gives a size). "Cheapest/best value per m² across areas" or a
  size-constrained budget -> find_affordable_areas (sort='ppsqm', min_floor_area/max_floor_area). £/m²
  comes from EPC-matched sales only — never route to scan_market for it.
- "How fresh / how complete is the data?": get_data_coverage.
- A novel cut the tools don't cover (postcode/outcode grouping, custom percentile): run_sql.
- Area names match Land Registry values: counties like KENT, SURREY, WEST MIDLANDS; London boroughs
  like BROMLEY, BEXLEY. There is still NO bedroom count — never filter by bedrooms.
- Only pass date parameters the user EXPLICITLY names; otherwise omit them (tools default to the
  latest complete data). NEVER assume today's date. Do not repeat a call already in the observations.
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
- £/m² & SIZE (EPC-matched, PARTIAL coverage): report a £/m² figure ONLY when the observations supply
  one (vs_sqm / median_ppsqm), and ALWAYS state it's from EPC-matched sales with the coverage (e.g.
  "£/m² based on ~68% of recent sales"). If the figure is null or coverage.trustworthy is false, DO NOT
  report £/m² — give the price-based value answer (history / peers / local percentile) and say £/m²
  isn't reliable enough here. Never invent or extrapolate a £/m² or floor area.
- BEDROOMS ARE NOT IN THE DATA: EPC gives "habitable rooms" (living rooms + kitchen + bedrooms), NOT a
  bedroom count. You may mention habitable rooms as an APPROXIMATE size proxy, explicitly noting it is
  not the bedroom count, but NEVER claim results are filtered by bedrooms or imply an exact bedroom match.
- ENERGY RATING: if an energy rating is provided, you may note it as a running-cost / EPC-C signal.
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
    # so it can't mis-route a budget question to assess_value or scan_market.
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
