"""Unit tests for the P&L aggregation functions - a tiny hand-checkable dataset, no database."""
import numpy as np
import pandas as pd
import pytest

from backend.services import pnl_service as ps

# date, business_unit, product, revenue, cost, expense, budget_pnl
ROWS = [
    ("2026-01-05", "A", "X", 100, 40, 10, 40),
    ("2026-01-06", "A", "Y", 200, 80, 20, 60),
    ("2026-01-05", "B", "X", 50, 20, 5, 30),
    ("2026-02-02", "A", "X", 240, 80, 20, 100),
    ("2026-02-03", "B", "X", 60, 30, 10, 20),
]
# Jan: revenue 350 cost 140 expense 35 net 175 budget 130      Feb: revenue 300 cost 110 expense 30 net 160 budget 120
# Total: revenue 650 cost 250 expense 65 net 335 budget 250 -> variance 85 (34%)


def frame():
    df = pd.DataFrame(ROWS, columns=["date", "business_unit", "product", "revenue", "cost", "expense", "budget_pnl"]).astype(
        {"revenue": float, "cost": float, "expense": float, "budget_pnl": float})
    df["date"] = pd.to_datetime(df["date"])
    df["currency"] = "INR"
    df["actual_pnl"] = df.revenue - df.cost - df.expense
    return df


def test_summary_totals_and_derived_measures():
    s = ps.summarise_pnl(frame())
    assert (s["revenue"], s["cost"], s["expense"], s["net_pnl"], s["budget_pnl"]) == (650, 250, 65, 335, 250)
    assert s["total_expenses"] == 315 and s["gross_pnl"] == 400
    assert s["gross_margin_pct"] == pytest.approx(61.54, abs=0.01) and s["net_margin_pct"] == pytest.approx(51.54, abs=0.01)
    assert s["variance"] == 85 and s["variance_pct"] == pytest.approx(34.0)
    assert s["business_days"] == 4 and (s["first_date"], s["last_date"]) == ("2026-01-05", "2026-02-03")
    assert all(v is None or isinstance(v, (int, float, str)) for v in s.values())          # JSON friendly


def test_summary_of_nothing_is_zero_and_undefined_ratios_are_none():
    s = ps.summarise_pnl(frame().iloc[0:0])
    assert s["revenue"] == 0 and s["net_pnl"] == 0 and s["business_days"] == 0
    assert s["net_margin_pct"] is None and s["variance_pct"] is None and s["first_date"] is None


def test_monthly_trend_with_mom_growth():
    t = ps.trend_frame(frame(), "month").set_index("period")
    assert list(t.index) == ["2026-01", "2026-02"]
    assert t.loc["2026-01", "revenue"] == 350 and t.loc["2026-02", "net_pnl"] == 160
    assert np.isnan(t.loc["2026-01", "revenue_mom_pct"])                               # first month: no growth
    assert t.loc["2026-02", "revenue_mom_pct"] == pytest.approx(-100 * 50 / 350)        # 300 vs 350
    assert t.loc["2026-02", "expenses_mom_pct"] == pytest.approx(-20.0)                 # 140 vs 175
    assert t.loc["2026-02", "net_pnl_mom_pct"] == pytest.approx(-100 * 15 / 175)        # 160 vs 175
    assert t.loc["2026-01", "business_days"] == 2 and t.loc["2026-02", "business_days"] == 2


def test_trend_by_business_unit_computes_growth_within_each_unit():
    t = ps.trend_frame(frame(), "month", by="business_unit")
    a = t[t.business_unit == "A"].set_index("period")
    b = t[t.business_unit == "B"].set_index("period")
    assert a.loc["2026-02", "revenue_mom_pct"] == pytest.approx(-20.0)      # 240 vs 300
    assert b.loc["2026-02", "revenue_mom_pct"] == pytest.approx(20.0)       # 60 vs 50
    assert np.isnan(b.loc["2026-01", "revenue_mom_pct"])


def test_quarter_and_day_grains():
    q = ps.trend_frame(frame(), "quarter")
    assert len(q) == 1 and q.loc[0, "period"] == "2026-Q1" and q.loc[0, "months_covered"] == 2
    assert q.loc[0, "revenue"] == 650 and "revenue_qoq_pct" in q
    d = ps.trend_frame(frame(), "day")
    assert len(d) == 4 and not any(c.endswith("_mom_pct") for c in d.columns)
    with pytest.raises(ValueError):
        ps.trend_frame(frame(), "week")


def test_period_totals_add_up_to_the_grand_total():
    df = frame()
    for grain in ps.GRAINS:
        assert ps.trend_frame(df, grain)["net_pnl"].sum() == pytest.approx(335)
    for by in ps.DIMENSIONS:
        assert ps.trend_frame(df, "month", by=by)["revenue"].sum() == pytest.approx(650)


def test_breakdown_by_business_unit_and_product():
    b = ps.breakdown_frame(frame(), "business_unit")
    assert list(b.business_unit) == ["A", "B"] and list(b["rank"]) == [1, 2]              # best net P&L first
    a = b.set_index("business_unit").loc["A"]
    assert (a.revenue, a.cost, a.expense, a.net_pnl, a.budget_pnl) == (540, 200, 50, 290, 200)
    assert a.revenue_share_pct == pytest.approx(100 * 540 / 650) and a.variance == 90
    p = ps.breakdown_frame(frame(), "product").set_index("product")
    assert (p.loc["X", "revenue"], p.loc["X", "net_pnl"], p.loc["Y", "revenue"], p.loc["Y", "net_pnl"]) == (450, 235, 200, 100)
    assert b["revenue_share_pct"].sum() == pytest.approx(100)
    with pytest.raises(ValueError):
        ps.breakdown_frame(frame(), "currency")


def test_budget_variance_flags_direction_and_breaches():
    actual = pd.DataFrame({"month": pd.to_datetime(["2026-01-01"] * 2), "business_unit": ["A", "B"],
                           "revenue": [300.0, 50.0], "total_expenses": [150.0, 25.0], "net_pnl": [150.0, 25.0]})
    budgets = pd.DataFrame({"month": pd.to_datetime(["2026-01-01"] * 2), "business_unit": ["A", "B"],
                            "budget_revenue": [250.0, 60.0], "budget_expense": [140.0, 20.0], "budget_pnl": [110.0, 40.0]})
    v = ps.budget_variance_frame(actual, budgets, threshold_pct=15).set_index("business_unit")
    a, b = v.loc["A"], v.loc["B"]
    assert a.revenue_variance == 50 and a.revenue_variance_pct == pytest.approx(20.0) and a.revenue_favourable
    assert a.expense_variance == 10 and not a.expense_favourable                # spending MORE than budget is adverse
    assert a.pnl_variance == 40 and a.pnl_variance_pct == pytest.approx(100 * 40 / 110) and a.pnl_favourable
    assert b.revenue_variance == -10 and not b.revenue_favourable and not b.pnl_favourable
    assert b.pnl_variance_pct == pytest.approx(-37.5) and bool(a.breach) and bool(b.breach)
    strict = ps.budget_variance_frame(actual, budgets, threshold_pct=40).set_index("business_unit")
    assert not strict.loc["A", "breach"] and not strict.loc["B", "breach"]        # 36.4% and 37.5% are under 40
    assert v.loc["A", "month"] == "2026-01"
