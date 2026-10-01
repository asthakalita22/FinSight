"""
FinSight liquidity analytics
============================
Definitions (per business unit and day, then added up):

    Net Cash Flow        = Inflows - Outflows
    Closing Cash         = Opening Cash + Inflows - Outflows
    Liquidity Gap        = Minimum Liquidity - Closing Cash        (positive = shortfall, negative = surplus)
    Funding Requirement  = MAX(0, Liquidity Gap)  per business unit, then SUMMED

Funding requirement is deliberately NOT netted across business units: a surplus in one unit does not fund a
shortfall in another (cash sits in separate pools). So at company level

    funding_requirement  >=  MAX(0, company liquidity_gap)

and the company `liquidity_gap` (which nets surpluses against shortfalls) can be negative while units still need funding.

Over a date range: opening = cash at the start of the first day, closing = cash at the end of the last day, flows are summed,
and `peak_funding_requirement` is the WORST single day (funding is a level, not a flow, so adding days up would double-count).
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from backend.utils import calculations as calc
from backend.utils.filters import Filters, build_where
from backend.utils.validators import validate_filters
from backend.services.pnl_service import _clean

CASH_COLUMNS = ["opening_cash", "cash_inflow", "cash_outflow", "closing_cash", "funding", "min_liquidity"]


def load_cash(engine, f: Filters | None = None) -> pd.DataFrame:
    f = f or Filters()
    if f.products or f.currency:
        raise ValueError("Liquidity is reported per business unit and day; product / currency filters do not apply")
    validate_filters(engine, f, check_products=False)
    where, params = build_where(f, "c.date", "bu.name")
    df = pd.read_sql(text(f"""
        SELECT c.date, bu.name AS business_unit,
               c.opening_cash::float8 AS opening_cash, c.cash_inflow::float8 AS cash_inflow,
               c.cash_outflow::float8 AS cash_outflow, c.closing_cash::float8 AS closing_cash,
               c.funding::float8 AS funding, t.min_liquidity::float8 AS min_liquidity
        FROM cash_flows c
        JOIN business_units bu ON bu.id = c.business_unit_id
        JOIN liquidity_targets t ON t.business_unit_id = c.business_unit_id{where}
        ORDER BY c.date, bu.name"""), engine, params=params)
    df["date"] = pd.to_datetime(df["date"])
    return df


# ----------------------------------------------------------------------------
# Pure functions
# ----------------------------------------------------------------------------
def add_liquidity_measures(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["net_cash_flow"] = out["cash_inflow"] - out["cash_outflow"]
    out["liquidity_gap"] = out["min_liquidity"] - out["closing_cash"]
    out["funding_requirement"] = out["liquidity_gap"].clip(lower=0)
    return out


def company_daily(df: pd.DataFrame) -> pd.DataFrame:
    """One row per date for the selected business units (all of them by default)."""
    if df.empty:
        return pd.DataFrame(columns=["date"])
    d = add_liquidity_measures(df)
    d["in_shortfall"] = d["funding_requirement"] > 0
    g = d.groupby("date", as_index=False).agg(
        opening_cash=("opening_cash", "sum"), cash_inflow=("cash_inflow", "sum"), cash_outflow=("cash_outflow", "sum"),
        closing_cash=("closing_cash", "sum"), min_liquidity=("min_liquidity", "sum"),
        funding_requirement=("funding_requirement", "sum"), units_in_shortfall=("in_shortfall", "sum"))
    g["net_cash_flow"] = g["cash_inflow"] - g["cash_outflow"]
    g["liquidity_gap"] = g["min_liquidity"] - g["closing_cash"]
    g["coverage_ratio"] = calc.safe_div(g["closing_cash"], g["min_liquidity"])
    g["units_in_shortfall"] = g["units_in_shortfall"].astype(int)
    return g.sort_values("date").reset_index(drop=True)


def monthly_frame(df: pd.DataFrame) -> pd.DataFrame:
    daily = company_daily(df)
    if daily.empty:
        return pd.DataFrame(columns=["month"])
    daily["month"] = daily["date"].dt.strftime("%Y-%m")
    g = daily.groupby("month", as_index=False).agg(
        opening_cash=("opening_cash", "first"), cash_inflow=("cash_inflow", "sum"), cash_outflow=("cash_outflow", "sum"),
        closing_cash=("closing_cash", "last"), min_closing_cash=("closing_cash", "min"),
        liquidity_gap=("liquidity_gap", "last"), funding_requirement=("funding_requirement", "last"),
        peak_funding_requirement=("funding_requirement", "max"),
        days_in_shortfall=("funding_requirement", lambda s: int((s > 0).sum())),
        business_days=("date", "nunique"))
    g["net_cash_flow"] = g["cash_inflow"] - g["cash_outflow"]
    return g[["month", "opening_cash", "cash_inflow", "cash_outflow", "net_cash_flow", "closing_cash", "min_closing_cash",
              "liquidity_gap", "funding_requirement", "peak_funding_requirement", "days_in_shortfall", "business_days"]]


def by_business_unit_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Position of each business unit over the selected range (opening = first day, closing = last day)."""
    if df.empty:
        return pd.DataFrame(columns=["business_unit"])
    d = add_liquidity_measures(df).sort_values("date")
    g = d.groupby("business_unit").agg(
        opening_cash=("opening_cash", "first"), cash_inflow=("cash_inflow", "sum"), cash_outflow=("cash_outflow", "sum"),
        closing_cash=("closing_cash", "last"), min_closing_cash=("closing_cash", "min"),
        min_liquidity=("min_liquidity", "last"), liquidity_gap=("liquidity_gap", "last"),
        funding_requirement=("funding_requirement", "last"), peak_funding_requirement=("funding_requirement", "max"),
        days_in_shortfall=("funding_requirement", lambda s: int((s > 0).sum()))).reset_index()
    g["net_cash_flow"] = g["cash_inflow"] - g["cash_outflow"]
    g["coverage_ratio"] = calc.safe_div(g["closing_cash"], g["min_liquidity"])
    return g.sort_values("liquidity_gap", ascending=False).reset_index(drop=True)


def summarise_liquidity(df: pd.DataFrame) -> dict:
    daily = company_daily(df)
    if daily.empty:
        return {k: None for k in ["opening_cash", "closing_cash", "cash_inflow", "cash_outflow", "net_cash_flow",
                                  "liquidity_buffer", "liquidity_gap", "funding_requirement", "peak_funding_requirement",
                                  "peak_funding_date", "days_in_shortfall", "min_closing_cash", "coverage_ratio",
                                  "business_days", "first_date", "as_of"]}
    first, last = daily.iloc[0], daily.iloc[-1]
    peak = daily.loc[daily["funding_requirement"].idxmax()]
    out = {
        "opening_cash": first["opening_cash"],
        "closing_cash": last["closing_cash"],                     # = the "Cash Position" KPI
        "cash_inflow": daily["cash_inflow"].sum(),
        "cash_outflow": daily["cash_outflow"].sum(),
        "net_cash_flow": daily["net_cash_flow"].sum(),
        "liquidity_buffer": last["min_liquidity"],
        "liquidity_gap": last["liquidity_gap"],
        "funding_requirement": last["funding_requirement"],
        "peak_funding_requirement": peak["funding_requirement"],
        "peak_funding_date": peak["date"].strftime("%Y-%m-%d") if peak["funding_requirement"] > 0 else None,
        "days_in_shortfall": int((daily["funding_requirement"] > 0).sum()),
        "min_closing_cash": daily["closing_cash"].min(),
        "coverage_ratio": last["coverage_ratio"],
        "business_days": int(len(daily)),
        "first_date": first["date"].strftime("%Y-%m-%d"),
        "as_of": last["date"].strftime("%Y-%m-%d"),
    }
    coverage = out.pop("coverage_ratio")
    out = {k: _clean(v) for k, v in out.items()}
    out["coverage_ratio"] = None if pd.isna(coverage) else round(float(coverage), 3)   # a ratio keeps 3 decimals
    return out


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def daily(engine, f: Filters | None = None) -> pd.DataFrame:
    return company_daily(load_cash(engine, f))


def monthly(engine, f: Filters | None = None) -> pd.DataFrame:
    return monthly_frame(load_cash(engine, f))


def by_business_unit(engine, f: Filters | None = None) -> pd.DataFrame:
    return by_business_unit_frame(load_cash(engine, f))


def summary(engine, f: Filters | None = None) -> dict:
    return summarise_liquidity(load_cash(engine, f))
