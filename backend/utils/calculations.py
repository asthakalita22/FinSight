"""Shared finance calculations. Everything here is a pure function (no database access).

Definitions
-----------
Total Expenses  = Cost + Expense                      ("expenses" in the KPI cards)
Gross P&L       = Revenue - Cost
Net P&L         = Revenue - Cost - Expense            (= the stored actual_pnl)
Variance        = Actual - Budget
Variance %      = Variance / |Budget| x 100           (identical to the spec when Budget > 0;
                                                        |Budget| keeps the sign meaningful if a budget is negative)
Growth %        = (Current - Previous) / |Previous| x 100
Undefined ratios (zero or missing denominator) are returned as NaN / None, never as 0 or infinity.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# Reconciliation / timeline helpers (Phase 2-3)
# ----------------------------------------------------------------------------
def data_as_of(txn: pd.DataFrame, ledger: pd.DataFrame) -> pd.Timestamp:
    """Reference 'now' of the simulated data: latest business activity + 6 days (covers the ledger posting
    window). Using the data's own clock keeps runs deterministic regardless of the computer's date."""
    latest = max(txn["transaction_date"].max(), pd.Timestamp(ledger["ledger_date"].max()))
    return latest.normalize() + pd.Timedelta(days=6)


def match_rate(matched: int, total: int) -> float:
    """Matched Records / Total Records x 100 (0.0 when there are no records)."""
    return round(100.0 * matched / total, 2) if total else 0.0


# ----------------------------------------------------------------------------
# Ratios
# ----------------------------------------------------------------------------
def safe_div(numerator, denominator):
    """Element-wise division that yields NaN (not inf / error) when the denominator is 0 or missing.
    Accepts scalars or pandas Series; returns the same kind."""
    is_series = isinstance(numerator, pd.Series) or isinstance(denominator, pd.Series)
    index = numerator.index if isinstance(numerator, pd.Series) else getattr(denominator, "index", None)
    num = np.asarray(numerator, dtype=float)
    den = np.asarray(denominator, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where((den == 0) | np.isnan(den), np.nan, num / den)
    if is_series:
        return pd.Series(out, index=index)
    return float(out)


def variance(actual, budget):
    """Actual - Budget."""
    return actual - budget


def variance_pct(actual, budget):
    """(Actual - Budget) / |Budget| x 100.  NaN when the budget is 0."""
    return safe_div((actual - budget) * 100.0, abs(budget))


def pct_change(current, previous):
    """(Current - Previous) / |Previous| x 100.  NaN when there is no usable previous value."""
    return safe_div((current - previous) * 100.0, abs(previous))


def pct(part, whole):
    """part / whole x 100 (NaN when whole is 0)."""
    return safe_div(part * 100.0, whole)


# ----------------------------------------------------------------------------
# P&L measures
# ----------------------------------------------------------------------------
def add_pnl_measures(df: pd.DataFrame) -> pd.DataFrame:
    """Given columns revenue, cost, expense, budget_pnl, add the derived P&L measures."""
    out = df.copy()
    out["total_expenses"] = out["cost"] + out["expense"]
    out["gross_pnl"] = out["revenue"] - out["cost"]
    out["net_pnl"] = out["revenue"] - out["cost"] - out["expense"]
    out["gross_margin_pct"] = pct(out["gross_pnl"], out["revenue"])
    out["net_margin_pct"] = pct(out["net_pnl"], out["revenue"])
    out["variance"] = variance(out["net_pnl"], out["budget_pnl"])
    out["variance_pct"] = variance_pct(out["net_pnl"], out["budget_pnl"])
    return out


def add_growth(df: pd.DataFrame, columns: dict[str, str], suffix: str, by: str | None = None) -> pd.DataFrame:
    """Add period-over-period growth columns.  `columns` maps a source column to the base name of the growth column,
    e.g. {"revenue": "revenue"} with suffix "mom" -> revenue_mom_pct.  The frame must be sorted by period;
    with `by`, growth is computed separately inside each group. The first period of a series is NaN."""
    out = df.copy()
    for source, base in columns.items():
        previous = out.groupby(by)[source].shift(1) if by else out[source].shift(1)
        out[f"{base}_{suffix}_pct"] = pct_change(out[source], previous)
    return out


# ----------------------------------------------------------------------------
# Periods
# ----------------------------------------------------------------------------
def month_label(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m")


def quarter_label(ts) -> str:
    """Calendar quarter, e.g. 2026-Q1."""
    t = pd.Timestamp(ts)
    return f"{t.year}-Q{(t.month - 1) // 3 + 1}"


def period_bounds(month: str | None = None, quarter: str | None = None) -> tuple[date, date]:
    """'2026-03' -> (2026-03-01, 2026-03-31);  '2026-Q1' -> (2026-01-01, 2026-03-31)."""
    if month and quarter:
        raise ValueError("Give either a month or a quarter, not both")
    if month:
        m = re.fullmatch(r"(\d{4})-(\d{2})", month.strip())
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ValueError(f"Invalid month '{month}'. Use YYYY-MM, e.g. 2026-03")
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    if quarter:
        q = re.fullmatch(r"(\d{4})-Q([1-4])", quarter.strip().upper())
        if not q:
            raise ValueError(f"Invalid quarter '{quarter}'. Use YYYY-Qn, e.g. 2026-Q1")
        y, n = int(q.group(1)), int(q.group(2))
        first, last = 3 * (n - 1) + 1, 3 * n
        return date(y, first, 1), date(y, last, calendar.monthrange(y, last)[1])
    raise ValueError("Give a month or a quarter")
