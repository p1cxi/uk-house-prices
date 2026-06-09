"""Golden buyer-question cases for the MCP/Qwen tool-calling eval.

Each Case asserts the DECISION the local model makes when it sees the analytics
tool specs + the MCP honesty instructions: which tool, and which arguments. A few
cases also run end-to-end (execute the tool, get the final NL answer) and assert
honesty properties of the prose.

Add cases freely — this file is plain data. Keep `expect_args` to the arguments
that genuinely matter for the case (the runner ignores extra args the model adds).
"""
from dataclasses import dataclass, field


@dataclass
class Case:
    id: str
    desc: str
    turns: list                                       # OpenAI-format messages; runner prepends the system prompt
    expect_tool: str | None = None                    # expected tool name; None = model should answer with NO tool
    expect_args: dict = field(default_factory=dict)   # arg -> expected value (case/number-insensitive exact match)
    e2e: bool = False                                 # execute the tool + get the final answer, then run answer checks
    answer_must: list = field(default_factory=list)       # regexes the final answer MUST match (re.I)
    answer_must_not: list = field(default_factory=list)   # regexes the final answer must NOT match (re.I)


def u(text):
    return {"role": "user", "content": text}


def a(text):
    return {"role": "assistant", "content": text}


CASES = [
    # --- Routing: budget questions go to find_affordable_areas, scoped by geography ----------
    Case("budget_county", "budget + named county -> find_affordable_areas, county scope",
         [u("What can I afford with £250k in Kent?")],
         expect_tool="find_affordable_areas",
         expect_args={"budget": 250000, "area_scope": "county", "county": "Kent"}),

    Case("budget_nationwide", "budget, no place named -> must NOT scope to london/county (absent or 'all' both fine)",
         [u("Where's the best value for my money if I've got £200k to spend?")],
         expect_tool="find_affordable_areas",
         expect_args={"budget": 200000, "area_scope": [None, "all"]}),

    Case("london_scope_direct", "names London -> area_scope=london",
         [u("What does £300k buy me in London?")],
         expect_tool="find_affordable_areas",
         expect_args={"budget": 300000, "area_scope": "london"}),

    # --- THE REGRESSION: a follow-up that adds 'closer to London' must re-scope to london ----
    Case("london_scope_followup", "follow-up 'anything closer to london?' must set area_scope=london",
         [u("If I had £200k and wanted to buy a house, where's the best value for my money? "
            "Leasehold, 2 bed maybe."),
          a("With £200k, the best value nationwide is in areas like Suffolk (median £135,000, "
            "91.5% of sales within budget), Lincolnshire and Norfolk — your money goes furthest there."),
          u("anything closer to london?")],
         expect_tool="find_affordable_areas",
         expect_args={"area_scope": "london"}),

    # Faithful repro of the reported failure: the model's OWN prior tool_call is in the history
    # (no area_scope), and 8Bs tend to copy prior tool_call args verbatim on the follow-up. This
    # is the version that should reproduce the live bug — and prove whether the wording fix lands.
    Case("london_followup_toolhist", "follow-up WITH prior tool_call in history must still re-scope to london",
         [u("If I had £200k and wanted to buy a house, where's the best value? Leasehold, 2 bed maybe."),
          {"role": "assistant", "content": "",
           "tool_calls": [{"id": "call_1", "type": "function",
                           "function": {"name": "find_affordable_areas",
                                        "arguments": '{"budget": 200000, "property_type": "house", '
                                                     '"tenure": "leasehold", "sort": "affordability", "limit": 12}'}}]},
          {"role": "tool", "tool_call_id": "call_1",
           "content": '{"budget":200000,"area_scope":"all","any_area_median_within_budget":true,'
                      '"results":[{"area":"SUFFOLK","median":135000,"pct_within_budget":91.5},'
                      '{"area":"LINCOLNSHIRE","median":109500,"pct_within_budget":84.2},'
                      '{"area":"NORFOLK","median":140000,"pct_within_budget":81.9}]}'},
          a("With £200k your money goes furthest in Suffolk (median £135,000, 91.5% of sales within "
            "budget), Lincolnshire and Norfolk."),
          u("anything closer to london?")],
         expect_tool="find_affordable_areas",
         expect_args={"area_scope": "london"}),

    # --- £/m² routing -----------------------------------------------------------------------
    Case("ppsqm_cheapest", "'cheapest per square metre' -> find_affordable_areas sort=ppsqm",
         [u("Which areas give me the cheapest price per square metre on a £400k budget?")],
         expect_tool="find_affordable_areas",
         expect_args={"budget": 400000, "sort": "ppsqm"}),

    Case("assess_candidate_sqm", "specific price + size -> assess_value with candidate_price + candidate_floor_area",
         [u("Is £350k for a 70 m² flat in Bromley good value?")],
         expect_tool="assess_value",
         expect_args={"area": "Bromley", "candidate_price": 350000, "candidate_floor_area": 70}),

    Case("assess_basic", "'is X good value' -> assess_value",
         [u("Is Reading good value to buy in right now?")],
         expect_tool="assess_value",
         expect_args={"area": "Reading"}),

    # --- scan_market ------------------------------------------------------------------------
    Case("scan_falling", "'where are prices falling' -> scan_market focus=falling",
         [u("Where are house prices falling at the moment?")],
         expect_tool="scan_market",
         expect_args={"focus": "falling"}),

    Case("coverage", "'how current is the data' -> get_data_coverage",
         [u("How up to date is your data?")],
         expect_tool="get_data_coverage"),

    # --- New params from PR #8 --------------------------------------------------------------
    Case("new_build_filter", "'new builds' -> find_affordable_areas new_build=new",
         [u("What new-build homes can I get for £300k around Manchester?")],
         expect_tool="find_affordable_areas",
         expect_args={"new_build": "new"}),

    # property_type accepts a LIST — the model should pick BOTH types named (set = "contains all").
    Case("multi_property_type", "'flats or terraced houses' -> property_type includes flat AND terraced",
         [u("What can I get for £300k in Surrey — flats or terraced houses?")],
         expect_tool="find_affordable_areas",
         expect_args={"budget": 300000, "property_type": {"flat", "terraced"}}),

    # --- End-to-end honesty -----------------------------------------------------------------
    Case("e2e_freshness", "'sold this month' answer must carry the registration-lag caveat",
         [u("What sold in Leeds in the last month?")],
         e2e=True,
         answer_must=[r"lag|incomplete|registered|current to|complete month|will (rise|grow)|up to"]),

    Case("e2e_no_bedroom_claim", "a bedroom request must not claim results were filtered by bedrooms",
         [u("Find me a 2-bedroom flat under £250k in Leeds.")],
         e2e=True,
         answer_must_not=[r"filtered (by|to|for) .{0,20}bedroom",
                          r"\b2[- ]?bed(room)?s?\b (homes|flats|properties|houses) (that|which|with)"]),
]
