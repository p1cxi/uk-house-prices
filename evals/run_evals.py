#!/usr/bin/env python3
"""Eval harness for the MCP / local-Qwen tool-calling path of the analytics agent.

It drives the SAME decision point the MCP chat UI exercises: the local model
(llama.cpp / Qwen3-8B) is given the analytics tool specs + a system prompt
mirroring the MCP server's honesty instructions, and we inspect the tool_calls it
emits. This is the path WITHOUT the deterministic /ask budget pre-router, so tool
and argument selection rest entirely on the tool descriptions — exactly where
routing bugs live (e.g. failing to set area_scope='london' on a follow-up turn).

Fully decoupled, stdlib only. Talks HTTP to:
  • the live LLM server  (chat completions with `tools`)            -> the decision
  • the live API         (GET /api/analysis/tools, POST .../call/X) -> specs + execution

Run on a box where both are reachable (defaults are read from ./.env if present):
  python3 evals/run_evals.py                      # all cases, 1 sample each
  python3 evals/run_evals.py --repeat 5           # 5 samples/case, report pass rate
  python3 evals/run_evals.py --case london_scope_followup -v
  python3 evals/run_evals.py --llm http://192.168.10.11:8080 --api http://localhost:8003

Exit code is non-zero if any case's pass-rate is below --threshold (default 1.0),
so it can gate CI / pre-merge.

Fidelity note: the real chat UI may prepend its own system prompt; we use the MCP
server's instructions as the controllable approximation. We assert the model's
tool/argument DECISION and (for e2e cases) the honesty of the final prose — the
transport only relays these.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from cases import CASES

# Mirrors api/mcp_server.py::_INSTRUCTIONS (the text surfaced to the model over MCP),
# plus a one-line tool-use directive. Keep in sync with the server if that changes.
SYSTEM = """You are a UK house-price assistant for first-time buyers. Use a tool whenever the \
question needs data, passing the most specific arguments the user's request (and the conversation \
so far) implies. These tools query HM Land Registry SOLD prices for England & Wales, 1995-present \
(no rentals, no current asking prices, no forecasts). When answering from their results:
- DATA CURRENCY: each result carries meta.last_complete_month — the latest fully-registered month. \
State that figures are current to that month, and note HM Land Registry registers sales with a ~2-3 \
month lag, so more recent months are incomplete and will rise. Do not present an incomplete recent \
month as a settled figure.
- £/m², FLOOR AREA & ENERGY come from EPC-matched sales only (PARTIAL coverage). Report a £/m² or size \
figure ONLY when the result supplies it, and always state the match %. If a result omits these or marks \
coverage untrustworthy, give the price-based answer and say £/m² isn't reliable there — never fabricate one.
- BEDROOMS ARE NOT IN THE DATA: EPC "habitable rooms" is an approximate size proxy, NOT a bedroom count. \
Never filter by or claim an exact bedroom count.
- Cite only numbers present in the tool results; never invent or estimate a figure."""


# --------------------------------------------------------------------------- config / http
def load_env(path=".env"):
    """Pull LLM_HOST / LLM_MODEL from a local .env (gitignored) if present, so the harness
    runs with no flags on the deployment box. Only those two keys are read."""
    out = {}
    p = Path(path)
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith(("LLM_HOST=", "LLM_MODEL=")):
                k, _, v = line.partition("=")
                out[k] = v.strip().strip('"').strip("'")
    return out


def _post(url, payload, timeout):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _wrap_specs(raw):
    """Wrap [{name, description, parameters|inputSchema}] as OpenAI-style function specs.
    Accepts both the API shape ('parameters') and the MCP shape ('inputSchema')."""
    raw = raw["tools"] if isinstance(raw, dict) else raw
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t.get("parameters") or t.get("inputSchema") or {}}}
            for t in raw]


def get_tool_specs(api_base):
    """GET /api/analysis/tools -> OpenAI-style function specs for the chat completions `tools` arg."""
    return _wrap_specs(_get(f"{api_base}/api/analysis/tools"))


def load_specs_file(path):
    """Load specs from a JSON file (the deployed API may lag the working tree; this lets the
    eval test un-deployed tool wording — dump current source with evals/dump_specs.py)."""
    with open(path) as f:
        return _wrap_specs(json.load(f))


def llm_chat(llm, model, messages, tools, timeout=120):
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM}] + messages,
               "tools": tools, "temperature": 0, "max_tokens": 512, "stream": False,
               "seed": 42, "chat_template_kwargs": {"enable_thinking": False}}
    res = _post(f"{llm}/v1/chat/completions", payload, timeout)
    return res["choices"][0]["message"]


def exec_tool(api_base, name, args, timeout=40):
    return _post(f"{api_base}/api/analysis/call/{name}", args, timeout)


# --------------------------------------------------------------------------- scoring
def first_tool_call(msg):
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return None, None
    fn = tcs[0].get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {"__unparseable__": fn.get("arguments")}
    return fn.get("name"), args


def arg_ok(expected, actual):
    """expected may be a literal, None (= arg should be absent), a list (= any-of), or a set
    (= the actual array must CONTAIN all members, case-insensitively)."""
    if isinstance(expected, set):
        if actual is None:
            return False
        items = actual if isinstance(actual, (list, tuple)) else [actual]
        norm = {str(a).strip().lower() for a in items}
        return {str(e).strip().lower() for e in expected}.issubset(norm)
    if isinstance(expected, (list, tuple)):
        return any(arg_ok(e, actual) for e in expected)
    if expected is None:
        return actual is None
    return arg_matches(expected, actual)


def arg_matches(expected, actual):
    if actual is None:
        return False
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, str):
        return str(actual).strip().lower() == expected.strip().lower()
    return actual == expected


def check_answer(text, must, must_not):
    text = text or ""
    fails = []
    for pat in must:
        if not re.search(pat, text, re.I):
            fails.append(f"missing /{pat}/")
    for pat in must_not:
        if re.search(pat, text, re.I):
            fails.append(f"forbidden /{pat}/ present")
    return fails


def run_once(case, tools, llm, model, api_base):
    """Return (passed: bool, detail: str)."""
    msg = llm_chat(llm, model, case.turns, tools)
    name, args = first_tool_call(msg)

    if case.expect_tool is None and not case.e2e:
        return (name is None, f"expected no tool, got {name or 'none'}")

    if case.expect_tool and name != case.expect_tool:
        return (False, f"tool={name or 'NONE'} (expected {case.expect_tool})"
                       + (f" args={args}" if args else ""))

    bad = [f"{k}={args.get(k, '∅')}≠{v}" for k, v in case.expect_args.items()
           if not arg_ok(v, (args or {}).get(k))]
    if bad:
        return (False, "args: " + ", ".join(bad))

    if not case.e2e:
        return (True, f"{name}({_fmt_args(args, case.expect_args)})")

    # End-to-end: execute the tool, feed the result back, get the final NL answer, check honesty.
    if name is None:
        return (False, "e2e: model answered without calling a tool")
    try:
        result = exec_tool(api_base, name, args or {})
    except Exception as e:
        return (False, f"e2e tool exec failed: {e}")
    tc = (msg.get("tool_calls") or [{}])[0]
    followup = case.turns + [
        {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": tc.get("id", "0"),
         "content": json.dumps(result, default=str)[:2800]},
    ]
    final = llm_chat(llm, model, followup, tools)
    answer = final.get("content") or ""
    fails = check_answer(answer, case.answer_must, case.answer_must_not)
    if fails:
        return (False, "answer: " + "; ".join(fails) + f"  | got: {answer[:140]!r}")
    return (True, f"{name} -> answer ok")


def _fmt_args(args, expected):
    if not expected:
        return ""
    return ", ".join(f"{k}={args.get(k)}" for k in expected)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    env = load_env()
    ap.add_argument("--llm", default=os.getenv("LLM_HOST", env.get("LLM_HOST", "http://localhost:8080")))
    ap.add_argument("--model", default=os.getenv("LLM_MODEL", env.get("LLM_MODEL", "local")))
    ap.add_argument("--api", default=os.getenv("EVAL_API_BASE", "http://localhost:8003"))
    ap.add_argument("--specs", help="load tool specs from a JSON file instead of the live API "
                                    "(test un-deployed wording; e2e still executes via --api)")
    ap.add_argument("--case", help="run only the case with this id")
    ap.add_argument("--repeat", type=int, default=1, help="samples per case (report pass rate)")
    ap.add_argument("--threshold", type=float, default=1.0, help="min pass-rate per case for green (default 1.0)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cases = [c for c in CASES if (not args.case or c.id == args.case)]
    if not cases:
        print(f"no case matching id={args.case!r}", file=sys.stderr)
        return 2

    specs_src = args.specs or f"{args.api}/api/analysis/tools"
    print(f"LLM={args.llm} model={args.model}  API={args.api}\n"
          f"specs={specs_src}  cases={len(cases)} repeat={args.repeat}\n")
    try:
        tools = load_specs_file(args.specs) if args.specs else get_tool_specs(args.api)
    except Exception as e:
        print(f"FATAL: could not load tool specs from {specs_src}: {e}", file=sys.stderr)
        return 2
    print(f"loaded {len(tools)} tool specs: {', '.join(t['function']['name'] for t in tools)}\n")

    worst = 1.0
    rows = []
    for c in cases:
        passes, last_detail = 0, ""
        details = []
        for _ in range(args.repeat):
            try:
                ok, detail = run_once(c, tools, args.llm, args.model, args.api)
            except urllib.error.URLError as e:
                ok, detail = False, f"http error: {e}"
            passes += int(ok)
            last_detail = detail
            if not ok:
                details.append(detail)
        rate = passes / args.repeat
        worst = min(worst, rate)
        mark = "✅" if rate >= args.threshold else ("🟡" if rate > 0 else "❌")
        ratestr = f"{passes}/{args.repeat}" if args.repeat > 1 else ("pass" if passes else "FAIL")
        print(f"  {mark} {c.id:<24} {ratestr:>7}  {last_detail if (passes and args.verbose) else (details[0] if details else last_detail)}")
        if args.verbose and details and args.repeat > 1:
            for d in dict.fromkeys(details):
                print(f"        ↳ {d}")
        rows.append((c.id, rate))

    n_green = sum(1 for _, r in rows if r >= args.threshold)
    print(f"\n{n_green}/{len(rows)} cases green (threshold {args.threshold:.0%}); worst pass-rate {worst:.0%}")
    return 0 if worst >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
