"""
FinSight finance report (terminal)
==================================
Prints the Phase 4 analytics: P&L, budget variance, trends, business units, products and liquidity.
All amounts are in INR millions unless stated.

Examples
--------
    python automation/finance_report.py
    python automation/finance_report.py --quarter 2026-Q1
    python automation/finance_report.py --month 2026-03 --bu Equities --bu Derivatives
    python automation/finance_report.py --start 2026-01-15 --end 2026-02-10 --product Equity
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import engine  # noqa: E402
from backend.services import liquidity_service as LS  # noqa: E402
from backend.services import pnl_service as PS  # noqa: E402
from backend.utils.filters import Filters  # noqa: E402

M = lambda x: "-" if x is None or pd.isna(x) else f"{x / 1e6:,.1f}"          # noqa: E731  millions
P = lambda x: "-" if x is None or pd.isna(x) else f"{x:+.1f}%"               # noqa: E731  signed percent
PU = lambda x: "-" if x is None or pd.isna(x) else f"{x:.1f}%"               # noqa: E731  unsigned percent


def table(df, columns, formatters, headers):
    view = df[columns].copy()
    for c, fn in formatters.items():
        view[c] = view[c].map(fn)
    view.columns = headers
    return view.to_string(index=False, justify="right")


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def main():
    ap = argparse.ArgumentParser(description="FinSight finance report")
    ap.add_argument("--month", help="e.g. 2026-03")
    ap.add_argument("--quarter", help="e.g. 2026-Q1")
    ap.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD")
    ap.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD")
    ap.add_argument("--bu", action="append", help="business unit (repeatable)")
    ap.add_argument("--product", action="append", help="product (repeatable)")
    a = ap.parse_args()

    try:
        if a.month or a.quarter:
            f = Filters.for_period(month=a.month, quarter=a.quarter, business_units=a.bu, products=a.product)
        else:
            f = Filters(start_date=a.start, end_date=a.end, business_units=a.bu, products=a.product)
        s = PS.summary(engine, f)
        if s["business_days"] == 0:
            raise SystemExit("No data for that selection.")
        liq_f = f.with_(products=None)
        cash = LS.summary(engine, liq_f)
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}")

    scope = [f"{s['first_date']} to {s['last_date']}"]
    if f.business_units:
        scope.append("business units: " + ", ".join(f.business_units))
    if f.products:
        scope.append("products: " + ", ".join(f.products))
    print(f"\nFINSIGHT FINANCE REPORT  |  " + "  |  ".join(scope) + f"  |  {s['business_days']} business days")

    section("PROFIT & LOSS (INR M)")
    print(f"  Revenue         {M(s['revenue']):>10}")
    print(f"  Total expenses  {M(s['total_expenses']):>10}   (costs {M(s['cost'])} + operating expenses {M(s['expense'])})")
    print(f"  Gross P&L       {M(s['gross_pnl']):>10}   margin {PU(s['gross_margin_pct'])}")
    print(f"  Net P&L         {M(s['net_pnl']):>10}   margin {PU(s['net_margin_pct'])}")
    print(f"  Budget P&L      {M(s['budget_pnl']):>10}")
    print(f"  Variance        {M(s['variance']):>10}   {P(s['variance_pct'])} vs budget")
    if f.products:
        print("  (with a product filter the budget is the business-unit budget split equally across products: indicative only)")

    section("MONTHLY TREND (INR M)")
    t = PS.trend(engine, f, "month")
    print(table(t, ["period", "revenue", "total_expenses", "net_pnl", "budget_pnl", "variance_pct", "revenue_mom_pct", "net_pnl_mom_pct"],
                {"revenue": M, "total_expenses": M, "net_pnl": M, "budget_pnl": M, "variance_pct": P, "revenue_mom_pct": P, "net_pnl_mom_pct": P},
                ["month", "revenue", "expenses", "net P&L", "budget", "var %", "rev MoM", "P&L MoM"]))

    section("BUSINESS UNITS (INR M, best net P&L first)")
    b = PS.breakdown(engine, f, "business_unit")
    print(table(b, ["rank", "business_unit", "revenue", "net_pnl", "net_margin_pct", "budget_pnl", "variance_pct", "revenue_share_pct"],
                {"revenue": M, "net_pnl": M, "net_margin_pct": PU, "budget_pnl": M, "variance_pct": P, "revenue_share_pct": PU},
                ["#", "business unit", "revenue", "net P&L", "margin", "budget", "var %", "rev share"]))

    section("PRODUCTS (INR M)  [budget by product is an equal split of the unit budget: indicative only]")
    p = PS.breakdown(engine, f, "product")
    print(table(p, ["product", "revenue", "net_pnl", "net_margin_pct", "revenue_share_pct"],
                {"revenue": M, "net_pnl": M, "net_margin_pct": PU, "revenue_share_pct": PU},
                ["product", "revenue", "net P&L", "margin", "rev share"]))

    if not f.products:
        bv = PS.budget_variance(engine, f)
        hits = bv[bv.breach].sort_values("pnl_variance_pct", key=abs, ascending=False)
        section(f"BUDGET VARIANCE BREACHES (|P&L variance| above threshold): {len(hits)} of {len(bv)} business-unit months")
        if len(hits):
            print(table(hits, ["month", "business_unit", "net_pnl", "budget_pnl", "pnl_variance_pct", "revenue_variance_pct", "expense_variance_pct"],
                        {"net_pnl": M, "budget_pnl": M, "pnl_variance_pct": P, "revenue_variance_pct": P, "expense_variance_pct": P},
                        ["month", "business unit", "net P&L", "budget", "P&L var", "rev var", "exp var"]))

    section("LIQUIDITY (INR M)")
    print(f"  Opening cash {M(cash['opening_cash'])}  ->  closing cash {M(cash['closing_cash'])}   (as of {cash['as_of']})")
    print(f"  Inflows {M(cash['cash_inflow'])}   outflows {M(cash['cash_outflow'])}   net cash flow {M(cash['net_cash_flow'])}")
    print(f"  Liquidity buffer {M(cash['liquidity_buffer'])}   liquidity gap {M(cash['liquidity_gap'])}  "
          f"({'shortfall' if cash['liquidity_gap'] > 0 else 'surplus'})   coverage {cash['coverage_ratio']}x")
    print(f"  Funding requirement today {M(cash['funding_requirement'])}   peak {M(cash['peak_funding_requirement'])} "
          f"({cash['peak_funding_date'] or 'never'})   days in shortfall {cash['days_in_shortfall']} of {cash['business_days']}")
    lb = LS.by_business_unit(engine, liq_f)
    print()
    print(table(lb, ["business_unit", "closing_cash", "min_liquidity", "liquidity_gap", "funding_requirement", "peak_funding_requirement", "days_in_shortfall", "coverage_ratio"],
                {"closing_cash": M, "min_liquidity": M, "liquidity_gap": M, "funding_requirement": M, "peak_funding_requirement": M, "coverage_ratio": lambda x: f"{x:.2f}x"},
                ["business unit", "closing", "buffer", "gap", "funding now", "peak funding", "shortfall days", "coverage"]))
    print()


if __name__ == "__main__":
    main()
