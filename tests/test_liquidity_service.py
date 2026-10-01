"""Unit tests for the liquidity aggregation functions - hand-checkable, no database."""
import numpy as np
import pandas as pd
import pytest

from backend.services import liquidity_service as ls

# business_unit, date, opening, inflow, outflow, closing, funding, min_liquidity     (A needs 100, B needs 50)
ROWS = [
    ("A", "2026-01-05", 120, 30, 20, 130, 0, 100),
    ("B", "2026-01-05", 60, 10, 30, 40, 10, 50),      # B short by 10
    ("A", "2026-01-06", 130, 10, 50, 90, 10, 100),    # A short by 10
    ("B", "2026-01-06", 40, 20, 5, 55, 0, 50),
    ("A", "2026-01-07", 90, 40, 0, 130, 0, 100),
    ("B", "2026-01-07", 55, 0, 10, 45, 5, 50),        # B short by 5
]


def frame(rows=ROWS):
    df = pd.DataFrame(rows, columns=["business_unit", "date", "opening_cash", "cash_inflow", "cash_outflow",
                                     "closing_cash", "funding", "min_liquidity"])
    df["date"] = pd.to_datetime(df["date"])
    return df.astype({c: float for c in df.columns if c not in ("business_unit", "date")})


def test_row_level_measures():
    d = ls.add_liquidity_measures(frame())
    b1 = d[(d.business_unit == "B") & (d.date == "2026-01-05")].iloc[0]
    assert b1.net_cash_flow == -20 and b1.liquidity_gap == 10 and b1.funding_requirement == 10
    a1 = d[(d.business_unit == "A") & (d.date == "2026-01-05")].iloc[0]
    assert a1.liquidity_gap == -30 and a1.funding_requirement == 0            # a surplus is not a funding need
    assert (d.funding_requirement.to_numpy() == d.funding.to_numpy()).all()    # matches the stored funding column


def test_company_daily_does_not_net_shortfalls_against_surpluses():
    c = ls.company_daily(frame()).set_index("date")
    day1 = c.loc["2026-01-05"]
    assert (day1.opening_cash, day1.cash_inflow, day1.cash_outflow, day1.closing_cash) == (180, 40, 50, 170)
    assert day1.net_cash_flow == -10 and day1.min_liquidity == 150
    assert day1.liquidity_gap == -20            # company as a whole is 20 ABOVE its buffer ...
    assert day1.funding_requirement == 10       # ... yet B still needs 10: A's surplus cannot fund B
    assert day1.units_in_shortfall == 1
    assert c.loc["2026-01-06", "liquidity_gap"] == 5 and c.loc["2026-01-06", "funding_requirement"] == 10
    assert (c.funding_requirement >= c.liquidity_gap.clip(lower=0)).all()      # the general invariant


def test_summary():
    s = ls.summarise_liquidity(frame())
    assert (s["opening_cash"], s["closing_cash"]) == (180, 175)
    assert (s["cash_inflow"], s["cash_outflow"], s["net_cash_flow"]) == (110, 115, -5)
    assert s["closing_cash"] == s["opening_cash"] + s["net_cash_flow"]           # the cash identity over a range
    assert (s["liquidity_buffer"], s["liquidity_gap"], s["funding_requirement"]) == (150, -25, 5)
    assert s["peak_funding_requirement"] == 10 and s["peak_funding_date"] == "2026-01-05"
    assert s["days_in_shortfall"] == 3 and s["min_closing_cash"] == 145
    assert s["coverage_ratio"] == pytest.approx(175 / 150, abs=0.001)
    assert (s["first_date"], s["as_of"], s["business_days"]) == ("2026-01-05", "2026-01-07", 3)


def test_cash_identity_holds_for_any_contiguous_sub_range():
    df = frame()
    for start in ("2026-01-05", "2026-01-06", "2026-01-07"):
        for end in ("2026-01-06", "2026-01-07"):
            if start > end:
                continue
            s = ls.summarise_liquidity(df[(df.date >= start) & (df.date <= end)])
            assert s["closing_cash"] == pytest.approx(s["opening_cash"] + s["cash_inflow"] - s["cash_outflow"])


def test_by_business_unit():
    b = ls.by_business_unit_frame(frame()).set_index("business_unit")
    a, bb = b.loc["A"], b.loc["B"]
    assert (a.opening_cash, a.closing_cash, a.net_cash_flow, a.min_closing_cash) == (120, 130, 10, 90)
    assert (a.liquidity_gap, a.funding_requirement, a.peak_funding_requirement, a.days_in_shortfall) == (-30, 0, 10, 1)
    assert (bb.opening_cash, bb.closing_cash, bb.net_cash_flow) == (60, 45, -15)
    assert (bb.liquidity_gap, bb.funding_requirement, bb.days_in_shortfall) == (5, 5, 2)
    assert bb.coverage_ratio == pytest.approx(0.9)
    assert list(ls.by_business_unit_frame(frame()).business_unit)[0] == "B"      # largest gap first
    assert b.closing_cash.sum() == 175 and b.net_cash_flow.sum() == -5            # units add up to the company


def test_monthly():
    m = ls.monthly_frame(frame()).iloc[0]
    assert m.month == "2026-01" and (m.opening_cash, m.closing_cash, m.net_cash_flow) == (180, 175, -5)
    assert m.peak_funding_requirement == 10 and m.funding_requirement == 5 and m.days_in_shortfall == 3


def test_empty_input_is_handled():
    s = ls.summarise_liquidity(frame().iloc[0:0])
    assert s["closing_cash"] is None and s["days_in_shortfall"] is None
    assert ls.company_daily(frame().iloc[0:0]).empty and ls.by_business_unit_frame(frame().iloc[0:0]).empty
