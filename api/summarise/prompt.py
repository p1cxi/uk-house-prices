"""Prompt building + LLM call for the monthly market briefing.

Uses only figures pre-computed by .queries — the LLM never sees raw SQL results,
so the "no invented numbers" rule is enforced by construction.
"""
from typing import Dict, List

from ..llm import chat


def _format_row(row: Dict) -> str:
    area = (row["area_name"] or "Unknown").title()
    area_type = row["area_type"] or "Area"
    tx = int(row["transactions"])
    median = row["current_month_median"]
    median_str = f"median £{int(median):,}" if median else "median N/A"
    mom = row["mom_change_pct"]
    line = f"{area} ({area_type}): {tx:,} tx, {median_str}"
    if mom is not None:
        line += f", {float(mom):+.1f}% MoM"
    return line


def _format_row_yoy(row: Dict) -> str:
    area = (row["area_name"] or "Unknown").title()
    area_type = row["area_type"] or "Area"
    yoy = float(row["yoy_change_pct"])
    prev_med = row["prev_month_median"]
    prev_str = f", median £{int(prev_med):,}" if prev_med else ""
    return f"{area} ({area_type}){prev_str}, {yoy:+.1f}% YoY"


async def generate_ai_summary(notable: Dict) -> str:
    """3-sentence push-notification briefing over the pre-selected notable areas."""
    def section(rows: List[Dict]) -> str:
        return "\n".join(f"  {_format_row(r)}" for r in rows)

    def section_yoy(rows: List[Dict]) -> str:
        return "\n".join(f"  {_format_row_yoy(r)}" for r in rows)

    with_mom = notable["with_mom"]
    with_yoy = notable["with_yoy"]

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

    top_by_volume = notable["top_by_volume"]
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
