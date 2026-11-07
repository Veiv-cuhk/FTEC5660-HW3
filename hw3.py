#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# MY DESIGN (Task 1)
#
# Three points, chosen so that each evidence source has a job to do:
#   1  market potential for sales growth   -> database (growth vs industry median)
#   5  worthwhile profit margin            -> database (operating margin vs median)
#  11  competitive edge (industry-specific)-> interviews (Fisher's "scuttlebutt")
# The documents are used as claims to be audited against the database, never as
# evidence on their own (the 2025 letter is read for every company and any
# management claim that the numbers contradict is reported in the summary).
#
# Pipeline per company (all arithmetic in Python, never in the model's head):
#   plan -> collect(sources) -> rule engine -> scuttlebutt judge -> reflect/write
# ---------------------------------------------------------------------------

# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [1, 5, 11]  ### YOUR CODE HERE

# How many companies to analyse at the same time (each company makes 3 model calls).
MAX_CONCURRENT_COMPANIES = 6
MODEL_TEMPERATURE = 0.0

# A company whose metric is this close to the industry median is called
# "near the line": the verdict still follows the rule, but the summary says so.
NEAR_LINE_PCT = 0.30

# --- the rules I designed, in words (also used inside the model prompts) -----
RULE_1 = (
    "Point 1 (market potential): PASS if the company's revenue grew by at least "
    "the industry median two-year growth rate, i.e. revenue FY2025 vs FY2023 in "
    "percent >= industry_medians.revenue_growth_2y_pct for the company's own "
    "industry. FAIL otherwise."
)
RULE_5 = (
    "Point 5 (worthwhile profit margin): PASS if the FY2025 operating margin, "
    "(revenue - cost of sales - selling and admin - R&D) / revenue, is at least "
    "the industry median operating_margin_pct. FAIL otherwise."
)
RULE_11 = (
    "Point 11 (competitive edge): PASS if independent interviewees describe a "
    "durable advantage a rival cannot copy quickly (switching costs, proprietary "
    "know-how, performance leadership, customers who come back). FAIL if the "
    "product is described as a commodity bought on price."
)

POINT_RULES = {1: RULE_1, 5: RULE_5, 11: RULE_11}

# Fisher's scuttlebutt map (Section 3.3): only these roles know about a topic.
# Asking a role that cannot know the answer still burns one of the 10 interviews,
# so the interview plan is fixed instead of being delegated to the model.
INTERVIEW_PLAN = {
    "sales_team": ["customer", "competitor"],
    "working_conditions": ["current_employee", "former_employee"],
    "top_management": ["former_employee", "supplier"],
    "decision_making": ["supplier", "customer"],
    "competitive_edge": ["competitor", "customer"],
}

# SQL probes. Amounts are USD thousands, so dividing by 1000 gives USD millions.
SQL_REVENUE_BY_YEAR = (
    "SELECT substr(order_date, 1, 4) AS year, ROUND(SUM(amount) / 1000.0, 1) AS revenue_musd "
    "FROM orders GROUP BY year ORDER BY year"
)
SQL_COMPANY_INDUSTRY = "SELECT name, industry FROM company LIMIT 1"
SQL_INDUSTRY_MEDIANS = (
    "SELECT industry, revenue_growth_2y_pct, new_product_share_pct, "
    "new_product_sales_per_rd, operating_margin_pct FROM industry_medians ORDER BY industry"
)
SQL_COSTS_FY2025 = (
    "SELECT ROUND(SUM(cost_of_sales) / 1000.0, 1) AS cost_of_sales_musd, "
    "ROUND(SUM(selling_admin) / 1000.0, 1) AS selling_admin_musd, "
    "ROUND(SUM(research_development) / 1000.0, 1) AS rnd_musd "
    "FROM monthly_costs WHERE month >= '2025-01' AND month <= '2025-12'"
)
SQL_COSTS_TTM = (
    "SELECT ROUND(SUM(cost_of_sales) / 1000.0, 1) AS cost_of_sales_musd, "
    "ROUND(SUM(selling_admin) / 1000.0, 1) AS selling_admin_musd, "
    "ROUND(SUM(research_development) / 1000.0, 1) AS rnd_musd "
    "FROM monthly_costs WHERE month >= '2025-07' AND month <= '2026-06'"
)
SQL_REVENUE_TTM = (
    "SELECT ROUND(SUM(amount) / 1000.0, 1) AS revenue_musd FROM orders "
    "WHERE order_date >= '2025-07-01' AND order_date < '2026-07-01'"
)

SCUTTLEBUTT_PROMPT = """You are applying Philip Fisher's scuttlebutt method to judge one \
question about a company: does it have a competitive edge (Fisher's point 11, "other \
aspects of the business, somewhat peculiar to the industry involved, which give clues as \
to how outstanding the company may be in relation to its competition")?

You are given verbatim quotes from independent people who know this company, collected \
by a research agent. Judge only what the quotes say.

PASS: the quotes describe a durable advantage that a rival cannot copy quickly - \
switching costs, proprietary know-how, an installed base, performance leadership, \
customers who try a rival and come back.
FAIL: the quotes describe a commodity - interchangeable products, buying on price, \
brand or technology that anyone can match.
If a quote says the person cannot comment or does not deal with the topic, ignore it.

Reply with JSON only:
{"verdict": "PASS" or "FAIL", "decisive_quote": "<the sentence that decided it>", \
"reason": "<one sentence>"}"""

SUMMARY_PROMPT = """You are writing the verdict note of an equity research agent that applies \
Philip Fisher's "15 points" method. Three points have already been decided by fixed, \
mechanical rules; the numbers below were computed in Python and are correct. Do not \
recompute anything and do not change any verdict.

Write 2-4 sentences that explain the three verdicts to an investor:
- point 1 and point 5: quote the company's figure against its industry median;
- point 11: use the decisive interview quote in your own words (say who said it);
- if the 2025 shareholder letter makes a claim that the database contradicts, add one \
clause noting the gap (management said X, the accounts show Y). Do not treat the letter \
as evidence for a verdict.

Be concrete, use USD millions and percentages, no bullet points, no headings, no \
fabricated figures. Reply with the sentences only."""


def _to_float(value: Any) -> float | None:
    """Parse a cell of the sql_query output into a float, or None."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "None", "NULL", "null", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_rows(text: str) -> list[dict[str, str]]:
    """Turn the 'a | b' table printed by sql_query into a list of dictionaries."""
    lines = [line for line in str(text).splitlines() if line.strip()]
    if not lines or lines[0].startswith(("SQL error:", "Query returned", "Unknown")):
        return []
    header = [cell.strip() for cell in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if line.startswith("..."):
            break
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


async def _call_tool(tool: Any, **kwargs: Any) -> str:
    """Invoke one LangChain tool without blocking the event loop."""
    return await asyncio.to_thread(tool.invoke, kwargs)


async def _model_call(model: Any, prompt: str, attempts: int = 3) -> str:
    """One chat completion, retried a few times because a failed call is a lost verdict."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            reply = await model.ainvoke(prompt)
            return str(getattr(reply, "content", reply)).strip()
        except Exception as exc:  # noqa: BLE001 - network/API errors are all retried alike
            last = exc
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"model call failed: {last}")


def _json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return {}


def _fallback_edge_verdict(quotes: list[str]) -> str:
    """Keyword backstop, used only if the model cannot answer for point 11."""
    text = " ".join(quotes).lower()
    negative = ("commodity", "interchangeable", "price fight", "cheapest", "not hard to copy",
                "nothing they make", "no advantage")
    positive = ("hard to copy", "switching away", "nobody else can match", "go back to them",
                "installed base", "requalification", "better trained", "not a factor in any deal")
    score = sum(marker in text for marker in positive) - 2 * sum(marker in text for marker in negative)
    return "FAIL" if score < 0 else "PASS"


async def _gather_database_evidence(tools: dict[str, Any]) -> dict[str, Any]:
    """Points 1 and 5: five aggregate queries, no arithmetic left to the model."""
    sql = tools["sql_query"]
    revenue_rows, industry_rows, median_rows = await asyncio.gather(
        _call_tool(sql, query=SQL_REVENUE_BY_YEAR),
        _call_tool(sql, query=SQL_COMPANY_INDUSTRY),
        _call_tool(sql, query=SQL_INDUSTRY_MEDIANS),
    )
    costs_fy, costs_ttm, revenue_ttm = await asyncio.gather(
        _call_tool(sql, query=SQL_COSTS_FY2025),
        _call_tool(sql, query=SQL_COSTS_TTM),
        _call_tool(sql, query=SQL_REVENUE_TTM),
    )

    revenue = {}
    for row in _parse_rows(revenue_rows):
        year, amount = row.get("year"), _to_float(row.get("revenue_musd"))
        if year and amount is not None:
            revenue[year] = amount
    company_rows, medians = _parse_rows(industry_rows), _parse_rows(median_rows)
    industry = company_rows[0].get("industry", "") if company_rows else ""
    median = next((row for row in medians if row.get("industry") == industry), {})
    fy, ttm = (_parse_rows(costs_fy) or [{}])[0], (_parse_rows(costs_ttm) or [{}])[0]
    ttm_revenue = next(iter([_to_float(r.get("revenue_musd")) for r in _parse_rows(revenue_ttm)]), None)

    def cost_total(row: dict[str, str]) -> float | None:
        parts = [_to_float(row.get(key)) for key in ("cost_of_sales_musd", "selling_admin_musd", "rnd_musd")]
        return None if any(p is None for p in parts) else sum(parts)  # type: ignore[arg-type]

    def margin(revenue_value: float | None, row: dict[str, str]) -> float | None:
        total = cost_total(row)
        if not revenue_value or total is None:
            return None
        return (revenue_value - total) / revenue_value * 100.0

    rev_2023, rev_2025 = revenue.get("2023"), revenue.get("2025")
    growth = (rev_2025 / rev_2023 - 1.0) * 100.0 if rev_2023 and rev_2025 else None
    return {
        "industry": industry,
        "revenue": revenue,
        "revenue_2025": rev_2025,
        "growth_2y_pct": growth,
        "median_growth_pct": _to_float(median.get("revenue_growth_2y_pct")),
        "margin_2025_pct": margin(rev_2025, fy),
        "margin_ttm_pct": margin(ttm_revenue, ttm),
        "median_margin_pct": _to_float(median.get("operating_margin_pct")),
        "cost_of_sales_2025": _to_float(fy.get("cost_of_sales_musd")),
        "rnd_2025": _to_float(fy.get("rnd_musd")),
    }


async def _gather_scuttlebutt(tools: dict[str, Any], topic: str) -> list[dict[str, str]]:
    """Ask exactly the roles who know about *topic* (two interviews, 20% of the budget)."""
    interview = tools["interview"]
    roles = INTERVIEW_PLAN[topic]
    answers = await asyncio.gather(*(_call_tool(interview, role=role, topic=topic) for role in roles))
    return [{"role": role, "answer": str(answer).strip()} for role, answer in zip(roles, answers)]


def _decide_point_1(evidence: dict[str, Any]) -> str:
    growth, median = evidence["growth_2y_pct"], evidence["median_growth_pct"]
    if growth is None or median is None:
        return "FAIL"
    return "PASS" if growth >= median else "FAIL"


def _decide_point_5(evidence: dict[str, Any]) -> str:
    margin, median = evidence["margin_2025_pct"], evidence["median_margin_pct"]
    if margin is None or median is None:
        return "FAIL"
    return "PASS" if margin >= median else "FAIL"


def _evidence_block(name: str, evidence: dict[str, Any], quotes: list[dict[str, str]],
                    edge: dict[str, Any], verdicts: dict[int, str]) -> str:
    """The fact sheet the summary writer is allowed to use."""
    lines = [
        f"Company: {name.replace('_', ' ').title()}",
        f"Industry: {evidence['industry']}",
        f"Revenue FY2023: USD {_fmt(evidence['revenue'].get('2023'))}m; "
        f"FY2025: USD {_fmt(evidence['revenue_2025'])}m",
        f"Two-year revenue growth: {_fmt(evidence['growth_2y_pct'])}% vs industry median "
        f"{_fmt(evidence['median_growth_pct'])}% -> point 1 {verdicts[1]}",
        f"FY2025 operating margin: {_fmt(evidence['margin_2025_pct'])}% vs industry median "
        f"{_fmt(evidence['median_margin_pct'])}% -> point 5 {verdicts[5]}",
        f"TTM operating margin (Jul 2025-Jun 2026), consistency check: "
        f"{_fmt(evidence['margin_ttm_pct'])}%",
        f"Competitive-edge interviews -> point 11 {verdicts[11]}: {edge.get('reason', '')}",
    ]
    for quote in quotes:
        lines.append(f"  - {quote['role']}: \"{quote['answer']}\"")
    return "\n".join(lines)


def _fallback_summary(evidence: dict[str, Any], verdicts: dict[int, str], edge: dict[str, Any]) -> str:
    """Used only when the summary model call keeps failing."""
    return (
        f"Two-year revenue growth of {_fmt(evidence['growth_2y_pct'])}% against an industry median of "
        f"{_fmt(evidence['median_growth_pct'])}% gives point 1 {verdicts[1]}; an FY2025 operating margin "
        f"of {_fmt(evidence['margin_2025_pct'])}% against a median of {_fmt(evidence['median_margin_pct'])}% "
        f"gives point 5 {verdicts[5]}. Scuttlebutt interviews on the competitive edge give point 11 "
        f"{verdicts[11]}: {edge.get('reason', 'no usable interview evidence').strip()}"
    )


def build_agent() -> Any:
    """Create the model and the fixed prompts once; tools arrive per company."""
    from langchain_deepseek import ChatDeepSeek

    model = ChatDeepSeek(
        model=MODEL_NAME,
        temperature=MODEL_TEMPERATURE,
        max_retries=2,
        timeout=120,
    )
    return {
        "model": model,
        "prompts": {"scuttlebutt": SCUTTLEBUTT_PROMPT, "summary": SUMMARY_PROMPT},
    }


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run your agent and return its verdicts on CHOSEN_POINTS for every company.

    ``companies`` maps each company folder name to its four LangChain tools:

        list_documents()             names and dates of the company's documents
        read_document(name)          the text of one document
        sql_query(query)             one read-only SQLite query on company.db
        interview(role, topic)       one interview answer (INTERVIEW_LIMIT per company)

    Give the tools to a tool-calling agent, e.g.
    ``create_agent(model, tools=companies[name], system_prompt=...)``, or call
    them yourself with ``tool.invoke({...})``. The README explains the
    documents, the database tables and the interview roles and topics.

    Return one dictionary per company folder name, e.g. with
    CHOSEN_POINTS = [1, 7, 15]:

        {
            "hollins_group": {
                "points": {1: "PASS", 7: "FAIL", 15: "PASS"},
                "summary": "Two or three sentences explaining the verdicts.",
            },
            ...
        }

    Each chosen point needs a verdict ("PASS" or "FAIL"; True/False also
    works). Other points are ignored.

    Catch errors per company (e.g. a failed API call) and still return an
    entry for it: an exception here means no results.csv, which scores zero.
    You may analyse companies in parallel (e.g. ``asyncio.gather``).
    """
    model, prompts = agent["model"], agent["prompts"]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_COMPANIES)

    async def one_company(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return name, await _analyse_company(name, tools, model, prompts)
            except Exception as exc:  # noqa: BLE001 - one broken company must not kill the run
                return name, {
                    "points": {point: "FAIL" for point in CHOSEN_POINTS},
                    "summary": f"{name.replace('_', ' ').title()}: analysis failed ({exc}).",
                }

    pairs = await asyncio.gather(*(one_company(name, tools) for name, tools in companies.items()))
    return dict(pairs)


async def _analyse_company(name: str, tools: list[Any], model: Any,
                           prompts: dict[str, str]) -> dict[str, Any]:
    """One company: database rules, then scuttlebutt, then an audited summary."""
    by_name = {tool.name: tool for tool in tools}

    # 1) Facts from the database (the two rules that need no judgement).
    evidence = await _gather_database_evidence(by_name)
    verdicts = {1: _decide_point_1(evidence), 5: _decide_point_5(evidence)}

    # 2) Scuttlebutt for point 11: two interviews with the roles who know the topic,
    #    plus the 2025 letter, which is a claim to be audited, not evidence.
    quotes, letter = await asyncio.gather(
        _gather_scuttlebutt(by_name, "competitive_edge"),
        _read_shareholder_letter(by_name),
    )
    edge: dict[str, Any] = {}
    try:
        prompt = prompts["scuttlebutt"] + "\n\nInterview quotes:\n" + "\n".join(
            f"- {quote['role']}: {quote['answer']}" for quote in quotes
        )
        edge = _json_object(await _model_call(model, prompt))
    except Exception:  # noqa: BLE001 - fall back to the keyword backstop
        edge = {}
    verdict = str(edge.get("verdict", "")).strip().upper()
    if verdict not in VERDICTS:
        verdict = _fallback_edge_verdict([quote["answer"] for quote in quotes])
        edge["verdict"] = verdict
        edge.setdefault("reason", "judged from the interview wording alone")
    edge["decisive_quote"] = str(edge.get("decisive_quote", ""))[:400]
    verdicts[11] = verdict

    # 3) Reflection: hand the fact sheet to the writer, which explains the verdicts
    #    and flags any management claim the accounts contradict.
    fact_sheet = _evidence_block(name, evidence, quotes, edge, verdicts)
    if letter:
        fact_sheet += "\n\n2025 letter to shareholders (management claims):\n" + letter[:2500]
    try:
        summary = await _model_call(model, prompts["summary"] + "\n\n" + fact_sheet)
    except Exception:  # noqa: BLE001
        summary = ""
    if len(summary) < 40:
        summary = _fallback_summary(evidence, verdicts, edge)

    return {
        "points": {point: verdicts[point] for point in CHOSEN_POINTS},
        "summary": " ".join(summary.split()),
        "evidence": {
            "growth_2y_pct": evidence["growth_2y_pct"],
            "median_growth_pct": evidence["median_growth_pct"],
            "margin_2025_pct": evidence["margin_2025_pct"],
            "median_margin_pct": evidence["median_margin_pct"],
            "edge_quote": edge.get("decisive_quote", ""),
        },
    }


async def _read_shareholder_letter(tools: dict[str, Any]) -> str:
    """Find the latest shareholder letter through list_documents, then read it."""
    try:
        listing = await _call_tool(tools["list_documents"])
        names = [line.split(":", 1)[0].strip() for line in str(listing).splitlines() if ":" in line]
        letters = [n for n in names if "letter" in n.lower()] or names[:1]
        if not letters:
            return ""
        return str(await _call_tool(tools["read_document"], name=letters[0]))
    except Exception:  # noqa: BLE001 - the letter is optional context
        return ""


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
