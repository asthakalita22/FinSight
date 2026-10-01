"""Unit tests for the shared finance calculations and filters - expected values are worked out by hand."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from backend.utils import calculations as calc
from backend.utils.filters import Filters, build_where


# ---------------------------------------------------------------- variance / growth
def test_variance_and_variance_pct():
    assert calc.variance(110, 100) == 10
    assert calc.variance_pct(110, 100) == pytest.approx(10.0)
    assert calc.variance_pct(80, 100) == pytest.approx(-20.0)


def test_variance_pct_with_negative_budget_keeps_direction():
    # budget -100, actual -50: the loss is smaller than planned = favourable (+50%), not -50%
    assert calc.variance_pct(-50, -100) == pytest.approx(50.0)


def test_undefined_ratios_are_nan_never_zero_or_inf():
    assert np.isnan(calc.variance_pct(10, 0))
    assert np.isnan(calc.pct_change(10, 0))
    assert np.isnan(calc.pct_change(10, np.nan))
    assert np.isnan(calc.pct(5, 0))


def test_series_input_returns_series_with_nan_where_undefined():
    out = calc.variance_pct(pd.Series([110.0, 5.0]), pd.Series([100.0, 0.0]))
    assert isinstance(out, pd.Series)
    assert out.iloc[0] == pytest.approx(10.0) and np.isnan(out.iloc[1])


def test_pct_change():
    assert calc.pct_change(120, 100) == pytest.approx(20.0)
    assert calc.pct_change(50, 100) == pytest.approx(-50.0)


def test_add_growth_first_period_is_nan_and_groups_are_independent():
    df = pd.DataFrame({"g": ["a", "a", "a", "b", "b"], "revenue": [100.0, 110.0, 99.0, 10.0, 15.0]})
    out = calc.add_growth(df, {"revenue": "revenue"}, "mom", by="g")
    col = out["revenue_mom_pct"].tolist()
    assert np.isnan(col[0]) and col[1] == pytest.approx(10.0) and col[2] == pytest.approx(-10.0)
    assert np.isnan(col[3]) and col[4] == pytest.approx(50.0)      # group b starts fresh, not from a's last value


# ---------------------------------------------------------------- P&L measures
def test_add_pnl_measures():
    df = pd.DataFrame({"revenue": [1000.0], "cost": [400.0], "expense": [200.0], "budget_pnl": [500.0]})
    r = calc.add_pnl_measures(df).iloc[0]
    assert r["total_expenses"] == 600 and r["gross_pnl"] == 600 and r["net_pnl"] == 400
    assert r["gross_margin_pct"] == pytest.approx(60.0) and r["net_margin_pct"] == pytest.approx(40.0)
    assert r["variance"] == -100 and r["variance_pct"] == pytest.approx(-20.0)


def test_zero_revenue_gives_undefined_margins():
    df = pd.DataFrame({"revenue": [0.0], "cost": [5.0], "expense": [5.0], "budget_pnl": [0.0]})
    r = calc.add_pnl_measures(df).iloc[0]
    assert r["net_pnl"] == -10 and np.isnan(r["net_margin_pct"]) and np.isnan(r["variance_pct"])


# ---------------------------------------------------------------- periods
def test_labels():
    assert calc.month_label("2026-03-15") == "2026-03"
    assert [calc.quarter_label(f"2026-{m:02d}-10") for m in (1, 3, 4, 9, 12)] == ["2026-Q1", "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]


def test_period_bounds():
    assert calc.period_bounds(month="2026-03") == (date(2026, 3, 1), date(2026, 3, 31))
    assert calc.period_bounds(month="2026-02")[1] == date(2026, 2, 28)
    assert calc.period_bounds(month="2028-02")[1] == date(2028, 2, 29)          # leap year
    assert calc.period_bounds(quarter="2026-Q4") == (date(2026, 10, 1), date(2026, 12, 31))
    assert calc.period_bounds(quarter="2026-q1") == (date(2026, 1, 1), date(2026, 3, 31))


@pytest.mark.parametrize("kwargs", [dict(month="2026-13"), dict(month="March"), dict(quarter="2026-Q5"),
                                    dict(quarter="Q1"), dict(month="2026-01", quarter="2026-Q1"), dict()])
def test_period_bounds_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        calc.period_bounds(**kwargs)


# ---------------------------------------------------------------- filters
def test_filters_validation_and_normalisation():
    with pytest.raises(ValueError):
        Filters(start_date=date(2026, 2, 1), end_date=date(2026, 1, 1))
    f = Filters(business_units="Equities", products=["Equity", "Fund"])
    assert f.business_units == ("Equities",) and f.products == ("Equity", "Fund")
    q = Filters.for_period(quarter="2026-Q1", business_units=("Treasury",))
    assert (q.start_date, q.end_date, q.business_units) == (date(2026, 1, 1), date(2026, 3, 31), ("Treasury",))


def test_build_where_uses_bound_parameters_only():
    assert build_where(Filters(), "p.date") == ("", {})
    f = Filters(start_date=date(2026, 1, 1), business_units=("Equities'; DROP TABLE pnl;--",), products=("Equity",), currency="inr")
    sql, params = build_where(f, "p.date", "bu.name", "p.product", "p.currency")
    assert "DROP TABLE" not in sql and "Equities" not in sql            # values never appear in the SQL text
    assert params["business_units"] == ["Equities'; DROP TABLE pnl;--"] and params["currency"] == "INR"
    assert sql.count("AND") == 3
