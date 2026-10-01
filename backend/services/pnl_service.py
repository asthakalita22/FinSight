"""
FinSight P&L analytics
======================
Revenue, expenses, gross / net P&L, budget variance, trends (MoM / QoQ) and breakdowns by business unit / product.

Structure
---------
* `load_*`   read the filtered rows from PostgreSQL (the only functions that touch the database)
* the rest   are PURE functions on DataFrames, unit-tested with hand-computed numbers
* `summary`, `trend`, `breakdown`, `budget_variance` glue the two together (these are what the API will call)

Notes
-----
* The P&L is stored in the reporting currency (INR). The `currency` filter therefore only matches 'INR' today.
* Budget is defined per business unit and month. The daily / product budget in the `pnl` table is that monthly figure
  split EQUALLY over business days and products, so daily and product-level variances are INDICATIVE only;
  business-unit and month-level variances are exact. `budget_variance()` (from the budgets table) refuses a product filter.
* A "quarter" is a calendar quarter. Quarters at the edges of the data can be partial: see `months_covered`.
"""
from __future__ import annotations

import calendar
import math

import pandas as pd
from sqlalchemy import text

from backend.config import settings
from backend.utils import calculations as calc
from backend.utils.filters import Filters, build_where
from backend.utils.validators import validate_filters

BASE = ["revenue", "cost", "expense", "actual_pnl", "budget_pnl"]
GRAINS = ("day", "month", "quarter")
DIMENSIONS = ("business_unit", "product")
GROWTH_COLUMNS = {"revenue": "revenue", "total_expenses": "expenses", "net_pnl": "net_pnl"}
GROWTH_SUFFIX = {"month": "mom", "quarter": "qoq"}


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def load_daily_pnl(engine, f: Filters | None = None) -> pd.DataFrame:
    f = f or Filters()
    validate_filters(engine, f)
    where, params = build_where(f, "p.date", "bu.name", "p.product", "p.currency")
    df = pd.read_sql(text(f"""
        SELECT p.date, bu.name AS business_unit, p.product, p.currency,
               p.revenue::float8 AS revenue, p.cost::float8 AS cost, p.expense::float8 AS expense,
               p.actual_pnl::float8 AS actual_pnl, p.budget_pnl::float8 AS budget_pnl
        FROM pnl p JOIN business_units bu ON bu.id = p.business_unit_id{where}
        ORDER BY p.date, bu.name, p.product"""), engine, params=params)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


# ----------------------------------------------------------------------------
# Pure aggregation functions
# ----------------------------------------------------------------------------
def _clean(value):
    """numpy / NaN -> plain Python (JSON friendly); NaN becomes None."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return round(value, 2)
    return value


def summarise_pnl(df: pd.DataFrame) -> dict:
    """Grand totals for a (filtered) daily frame."""
    totals = {c: float(df[c].sum()) if len(df) else 0.0 for c in BASE}
    row = calc.add_pnl_measures(pd.DataFrame([totals])).iloc[0].to_dict()
    row["business_days"] = int(df["date"].nunique()) if len(df) else 0
    row["first_date"] = df["date"].min().strftime("%Y-%m-%d") if len(df) else None
    row["last_date"] = df["date"].max().strftime("%Y-%m-%d") if len(df) else None
    return {k: _clean(v) for k, v in row.items()}


def with_period(df: pd.DataFrame, grain: str) -> pd.DataFrame:
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {GRAINS}, not '{grain}'")
    out = df.copy()
    if grain == "day":
        out["period"] = out["date"].dt.strftime("%Y-%m-%d")
    elif grain == "month":
        out["period"] = out["date"].dt.strftime("%Y-%m")
    else:
        out["period"] = out["date"].dt.year.astype(str) + "-Q" + out["date"].dt.quarter.astype(str)
    return out


def trend_frame(df: pd.DataFrame, grain: str = "month", by: str | None = None) -> pd.DataFrame:
    """P&L per period (optionally per business unit / product). Month grain adds MoM growth of revenue, expenses and
    net P&L; quarter grain adds QoQ growth. The first period of each series has no growth (NaN)."""
    if by is not None and by not in DIMENSIONS:
        raise ValueError(f"by must be one of {DIMENSIONS}, not '{by}'")
    keys = ([by] if by else []) + ["period"]
    if df.empty:
        return pd.DataFrame(columns=keys)
    d = with_period(df, grain)
    d["_month"] = d["date"].dt.strftime("%Y-%m")
    g = d.groupby(keys, as_index=False).agg(
        revenue=("revenue", "sum"), cost=("cost", "sum"), expense=("expense", "sum"),
        actual_pnl=("actual_pnl", "sum"), budget_pnl=("budget_pnl", "sum"),
        business_days=("date", "nunique"), months_covered=("_month", "nunique"))
    g = calc.add_pnl_measures(g).sort_values(keys).reset_index(drop=True)
    if grain in GROWTH_SUFFIX:
        g = calc.add_growth(g, GROWTH_COLUMNS, GROWTH_SUFFIX[grain], by=by)
    if grain != "quarter":
        g = g.drop(columns="months_covered")
    return g


def breakdown_frame(df: pd.DataFrame, dimension: str) -> pd.DataFrame:
    """Totals per business unit or product, best net P&L first, with each member's share of total revenue."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {DIMENSIONS}, not '{dimension}'")
    if df.empty:
        return pd.DataFrame(columns=[dimension])
    g = calc.add_pnl_measures(df.groupby(dimension, as_index=False)[BASE].sum())
    g["revenue_share_pct"] = calc.pct(g["revenue"], g["revenue"].sum())
    g = g.sort_values("net_pnl", ascending=False).reset_index(drop=True)
    g.insert(1, "rank", range(1, len(g) + 1))
    return g


def budget_variance_frame(actual: pd.DataFrame, budgets: pd.DataFrame, threshold_pct: float) -> pd.DataFrame:
    """Actual vs budget per business unit and month: revenue, expenses and P&L.
    `actual`  : month, business_unit, revenue, total_expenses, net_pnl
    `budgets` : month, business_unit, budget_revenue, budget_expense, budget_pnl
    Favourable = revenue above budget, expenses below budget, P&L above budget."""
    m = actual.merge(budgets, on=["month", "business_unit"], how="inner")
    m["revenue_variance"] = calc.variance(m["revenue"], m["budget_revenue"])
    m["revenue_variance_pct"] = calc.variance_pct(m["revenue"], m["budget_revenue"])
    m["expense_variance"] = calc.variance(m["total_expenses"], m["budget_expense"])
    m["expense_variance_pct"] = calc.variance_pct(m["total_expenses"], m["budget_expense"])
    m["pnl_variance"] = calc.variance(m["net_pnl"], m["budget_pnl"])
    m["pnl_variance_pct"] = calc.variance_pct(m["net_pnl"], m["budget_pnl"])
    m["revenue_favourable"] = m["revenue_variance"] >= 0
    m["expense_favourable"] = m["expense_variance"] <= 0
    m["pnl_favourable"] = m["pnl_variance"] >= 0
    m["breach"] = m["pnl_variance_pct"].abs() > threshold_pct
    m["month"] = pd.to_datetime(m["month"]).dt.strftime("%Y-%m")
    return m.sort_values(["month", "business_unit"]).reset_index(drop=True)


# ----------------------------------------------------------------------------
# Public API (database + pure functions)
# ----------------------------------------------------------------------------
def summary(engine, f: Filters | None = None) -> dict:
    return summarise_pnl(load_daily_pnl(engine, f))


def trend(engine, f: Filters | None = None, grain: str = "month", by: str | None = None) -> pd.DataFrame:
    return trend_frame(load_daily_pnl(engine, f), grain, by)


def breakdown(engine, f: Filters | None = None, dimension: str = "business_unit") -> pd.DataFrame:
    return breakdown_frame(load_daily_pnl(engine, f), dimension)


def budget_variance(engine, f: Filters | None = None, threshold_pct: float | None = None) -> pd.DataFrame:
    """Budget vs actual per business unit and month (whole months: a date range is widened to month boundaries)."""
    f = f or Filters()
    if f.products:
        raise ValueError("Budget variance is defined per business unit and month; the product filter is not supported")
    validate_filters(engine, f, check_products=False)
    span = f.with_(start_date=f.start_date.replace(day=1) if f.start_date else None,
                   end_date=_month_end(f.end_date) if f.end_date else None)
    daily = load_daily_pnl(engine, span)
    daily["month"] = daily["date"].dt.to_period("M").dt.to_timestamp()
    actual = calc.add_pnl_measures(daily.groupby(["month", "business_unit"], as_index=False)[BASE].sum())
    actual = actual[["month", "business_unit", "revenue", "total_expenses", "net_pnl"]]

    where, params = build_where(span.with_(start_date=span.start_date, end_date=span.end_date), "b.month", "bu.name")
    budgets = pd.read_sql(text(f"""
        SELECT b.month, bu.name AS business_unit, b.budget_revenue::float8 AS budget_revenue,
               b.budget_expense::float8 AS budget_expense, b.budget_pnl::float8 AS budget_pnl
        FROM budgets b JOIN business_units bu ON bu.id = b.business_unit_id{where}"""), engine, params=params)
    budgets["month"] = pd.to_datetime(budgets["month"])
    return budget_variance_frame(actual, budgets, settings.variance_threshold_pct if threshold_pct is None else threshold_pct)
