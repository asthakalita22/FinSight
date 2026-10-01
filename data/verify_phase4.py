"""
FinSight - Phase 4 verification (financial analytics)
=====================================================
  A. Data bridges   the P&L table is rebuilt from the raw transactions, cell by cell; cash flows tie to the P&L
  B. P&L service    every figure is compared with an independent SQL implementation (views / raw queries)
  C. Liquidity      same, plus the cash identities and the "no netting" invariant, over 25 random ranges
  D. Filters        month / quarter / date / business-unit / product / currency, and bad input
  E. Integration    budget variance breaches == the Phase 2 control exceptions

Usage:
    python data/verify_phase4.py
"""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.database import engine  # noqa: E402
from backend.services import liquidity_service as LS  # noqa: E402
from backend.services import pnl_service as PS  # noqa: E402
from backend.utils.data_access import load_ledger, load_transactions  # noqa: E402
from backend.utils.filters import Filters  # noqa: E402

results = []


def q(sql, **params):
    return pd.read_sql(text(sql), engine, params=params or None)


def scalar(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def close(a, b, tol=0.05):
    """Element-wise closeness that treats NaN == NULL."""
    return bool(np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float), atol=tol, rtol=0, equal_nan=True))


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def main():
    # ==================================================================== A
    print("\nA. DATA BRIDGES (is the data the analytics stand on itself consistent?)")
    txn, led = load_transactions(engine), load_ledger(engine)
    t = txn[txn.transaction_type.isin(["REVENUE", "COST", "EXPENSE"])].sort_values(["created_at", "id"]).copy()
    dup = t.transaction_id.notna() & t.transaction_id.duplicated(keep="first")
    t = t[~dup].copy()                                              # first copy of every transaction
    led1 = led.drop_duplicates("transaction_id").set_index("transaction_id")
    # recover fields the source system lost, from the ledger
    t["bu"] = t.business_unit_id.astype("float64").fillna(t.transaction_id.map(led1.business_unit_id.astype("float64")))
    t["inr"] = t.amount_inr.fillna(t.transaction_id.map(led1.amount_inr))
    t["date"] = t.transaction_date.dt.normalize()
    t["invalid"] = (t.amount <= 0).astype(int)
    piv = t.pivot_table(index=["date", "bu", "product"], columns="transaction_type", values="inr", aggfunc="sum",
                        fill_value=0.0).reset_index()
    meta = t.groupby(["date", "bu", "product"]).agg(n=("id", "size"), invalid=("invalid", "sum")).reset_index()
    cells = piv.merge(meta, on=["date", "bu", "product"])
    pnl = q("SELECT date, business_unit_id AS bu, product, revenue::float8 AS p_rev, cost::float8 AS p_cost, expense::float8 AS p_exp FROM pnl")
    pnl["date"] = pd.to_datetime(pnl["date"])
    pnl["bu"] = pnl["bu"].astype(float)
    m = pnl.merge(cells, on=["date", "bu", "product"], how="outer", indicator=True).fillna(0.0)
    check("Every transaction lands in an existing P&L cell", not (m["_merge"] == "right_only").any(),
          f"{int((m['_merge'] == 'right_only').sum())} orphan cells")
    m["tol"] = 0.006 * m.n + 0.02
    for col, p in [("REVENUE", "p_rev"), ("COST", "p_cost"), ("EXPENSE", "p_exp")]:
        m[f"d_{col}"] = (m[p] - m[col]).abs()
    m["gap"] = m[["d_REVENUE", "d_COST", "d_EXPENSE"]].max(axis=1)
    clean = m[m.invalid == 0]
    bad_clean = clean[clean.gap > clean.tol]
    check(f"P&L rebuilt from transactions matches in ALL {len(clean):,} cells that hold no corrupted amount",
          len(bad_clean) == 0, f"{len(bad_clean)} cells differ" if len(bad_clean) else "revenue, cost and expense, to the paisa")
    dirty = m[m.invalid > 0]
    lost = float(dirty[["d_REVENUE", "d_COST", "d_EXPENSE"]].sum().sum())
    total = float(m[["p_rev", "p_cost", "p_exp"]].sum().sum())
    check(f"The only differences sit in the {len(dirty)} cells with corrupted (zero / negative) amounts, and are small",
          lost / total < 0.005, f"INR {lost / 1e6:,.2f} M of {total / 1e6:,.0f} M = {100 * lost / total:.3f}% (the true amounts were destroyed)")

    d = q("""SELECT MAX(ABS(c.cash_inflow - p.rev)) AS d FROM cash_flows c JOIN
             (SELECT date, business_unit_id, SUM(revenue) AS rev FROM pnl GROUP BY 1, 2) p
             ON p.date = c.date AND p.business_unit_id = c.business_unit_id""").iloc[0, 0]
    check("Cash inflow == P&L revenue for every business unit and day", float(d) < 0.011, f"max diff {float(d):.4f}")
    n = scalar("""SELECT COUNT(*) FROM cash_flows c JOIN
                  (SELECT date, business_unit_id, SUM(cost + expense) AS c FROM pnl GROUP BY 1, 2) p
                  ON p.date = c.date AND p.business_unit_id = c.business_unit_id WHERE c.cash_outflow < p.c - 0.011""")
    check("Cash outflow >= P&L costs + expenses on every business-unit day (extras: settlements, month-end sweep)", n == 0, f"{n} violations")
    n = scalar("""SELECT COUNT(*) FROM cash_flows c JOIN liquidity_targets t USING (business_unit_id)
                  WHERE ABS(c.funding - GREATEST(t.min_liquidity - c.closing_cash, 0)) > 0.011""")
    check("Stored funding == MAX(0, minimum liquidity - closing cash) on every row", n == 0, f"{n} violations")
    n = scalar("SELECT COUNT(*) FROM budgets WHERE ABS(budget_pnl - (budget_revenue - budget_expense)) > 0.011")
    check("Budget P&L == budget revenue - budget expense", n == 0, f"{n} violations")

    # ==================================================================== B
    print("\nB. P&L SERVICE vs independent SQL")
    s = PS.summary(engine)
    sql_tot = q("SELECT SUM(revenue) r, SUM(cost) c, SUM(expense) e, SUM(actual_pnl) a, SUM(budget_pnl) b FROM pnl").iloc[0]
    check("Summary: revenue, cost, expense, net P&L, budget = SQL totals",
          close([s["revenue"], s["cost"], s["expense"], s["net_pnl"], s["budget_pnl"]],
                [sql_tot.r, sql_tot.c, sql_tot.e, sql_tot.a, sql_tot.b], 0.01),
          f"revenue INR {s['revenue'] / 1e6:,.1f} M, net P&L INR {s['net_pnl'] / 1e6:,.1f} M ({s['net_margin_pct']}%)")
    check("Derived: total expenses, gross P&L, variance and variance % recomputed independently",
          close([s["total_expenses"], s["gross_pnl"], s["variance"]], [sql_tot.c + sql_tot.e, sql_tot.r - sql_tot.c, sql_tot.a - sql_tot.b], 0.01)
          and abs(s["variance_pct"] - 100 * (sql_tot.a - sql_tot.b) / abs(sql_tot.b)) < 0.01,
          f"variance {s['variance_pct']}%")

    mt = PS.trend(engine, grain="month")
    v = q("SELECT * FROM v_pnl_monthly_trend ORDER BY month")
    check(f"Monthly trend: {len(mt)} months = v_pnl_monthly_trend (revenue, expenses, net P&L, budget)",
          len(mt) == len(v) and close(mt.revenue, v.revenue) and close(mt.total_expenses, v.total_expenses)
          and close(mt.net_pnl, v.net_pnl) and close(mt.budget_pnl, v.budget_pnl))
    check("Month-over-month growth (revenue, expenses, net P&L) = the SQL LAG() view",
          close(mt.revenue_mom_pct, v.revenue_mom_pct, 0.006) and close(mt.expenses_mom_pct, v.expenses_mom_pct, 0.006)
          and close(mt.net_pnl_mom_pct, v.net_pnl_mom_pct, 0.006) and close(mt.variance_pct, v.variance_pct, 0.006),
          f"first month has no growth: {mt.revenue_mom_pct.isna().sum() == 1}")
    ok = all(abs(mt.revenue.iloc[i] - mt.revenue.iloc[i - 1] * (1 + mt.revenue_mom_pct.iloc[i] / 100)) < 0.01 for i in range(1, len(mt)))
    check("Growth chain is self-consistent: revenue(t) = revenue(t-1) x (1 + MoM)", ok)
    check("Month business-day counts = SQL", (mt.business_days.to_numpy() == v.business_days.to_numpy()).all())

    qt = PS.trend(engine, grain="quarter", by="business_unit")
    vq = q("SELECT quarter AS period, business_unit, net_pnl, variance_pct, months_covered FROM v_pnl_quarterly")
    mq = qt.merge(vq, on=["period", "business_unit"], suffixes=("", "_sql"))
    check(f"Quarterly by business unit: {len(qt)} rows = v_pnl_quarterly (net P&L, variance %, months covered)",
          len(mq) == len(qt) == len(vq) and close(mq.net_pnl, mq.net_pnl_sql) and close(mq.variance_pct, mq.variance_pct_sql, 0.006)
          and (mq.months_covered == mq.months_covered_sql).all(),
          f"partial quarters flagged: months_covered {sorted(set(mq.months_covered))}")

    pm = PS.trend(engine, grain="month", by="product")
    vp = q("SELECT to_char(month, 'YYYY-MM') AS period, product, revenue, net_pnl FROM v_pnl_by_product")
    mp = pm.merge(vp, on=["period", "product"], suffixes=("", "_sql"))
    check(f"Product x month: {len(pm)} rows = v_pnl_by_product", len(mp) == len(pm) == len(vp) and close(mp.revenue, mp.revenue_sql)
          and close(mp.net_pnl, mp.net_pnl_sql))

    bb = PS.breakdown(engine, dimension="business_unit")
    vb = q("SELECT business_unit, SUM(revenue) revenue, SUM(actual_pnl) net_pnl, SUM(budget_pnl) budget_pnl FROM v_financial_performance GROUP BY 1")
    mb = bb.merge(vb, on="business_unit", suffixes=("", "_sql"))
    check("Business-unit breakdown = v_financial_performance", len(mb) == len(bb) == 8 and close(mb.revenue, mb.revenue_sql)
          and close(mb.net_pnl, mb.net_pnl_sql) and close(mb.budget_pnl, mb.budget_pnl_sql),
          f"best: {bb.business_unit.iloc[0]}, weakest: {bb.business_unit.iloc[-1]}")
    check("Variance % per business unit recomputed independently",
          close(bb.variance_pct, 100 * (bb.net_pnl - bb.budget_pnl) / bb.budget_pnl.abs(), 1e-6))
    check("Business-unit revenue shares add up to 100%", abs(bb.revenue_share_pct.sum() - 100) < 1e-6)

    parts = {"business unit": PS.breakdown(engine, dimension="business_unit"), "product": PS.breakdown(engine, dimension="product"),
             "month": PS.trend(engine, grain="month"), "quarter": PS.trend(engine, grain="quarter"), "day": PS.trend(engine, grain="day")}
    check("Additivity: business units, products, months, quarters and days each add up to the grand total",
          all(abs(f.net_pnl.sum() - s["net_pnl"]) < 0.05 and abs(f.revenue.sum() - s["revenue"]) < 0.05 for f in parts.values()),
          ", ".join(f"{k} {len(f)}" for k, f in parts.items()))

    # ==================================================================== C
    print("\nC. LIQUIDITY SERVICE vs independent SQL")
    ld = LS.daily(engine)
    lv = q("SELECT * FROM v_liquidity_summary ORDER BY date")
    check(f"Company daily series: {len(ld)} days = v_liquidity_summary (opening, flows, closing, buffer, gap, funding requirement)",
          len(ld) == len(lv) and close(ld.opening_cash, lv.opening_cash) and close(ld.cash_inflow, lv.inflow)
          and close(ld.cash_outflow, lv.outflow) and close(ld.closing_cash, lv.closing_cash)
          and close(ld.min_liquidity, lv.min_liquidity) and close(ld.liquidity_gap, lv.liquidity_gap)
          and close(ld.funding_requirement, lv.funding_requirement))
    stored = q("SELECT date, SUM(funding) AS f FROM cash_flows GROUP BY date ORDER BY date")
    check("Funding requirement (computed from the buffer) == the funding the data generator stored", close(ld.funding_requirement, stored.f, 0.05))
    check("Cash identity every day: closing = opening + inflows - outflows",
          close(ld.closing_cash, ld.opening_cash + ld.cash_inflow - ld.cash_outflow, 0.05))
    check("Cash continuity: opening(today) = closing(yesterday)", close(ld.opening_cash.iloc[1:], ld.closing_cash.iloc[:-1], 0.05))
    check("Liquidity gap = minimum liquidity - closing cash, on every day", close(ld.liquidity_gap, ld.min_liquidity - ld.closing_cash, 1e-6))
    invariant = (ld.funding_requirement >= ld.liquidity_gap.clip(lower=0) - 1e-6).all()
    masked = int(((ld.liquidity_gap < 0) & (ld.funding_requirement > 0)).sum())
    check("No netting: funding requirement >= MAX(0, company gap) every day", invariant,
          f"on {masked} days the company as a whole was above its buffer while a unit still needed funding")

    ls_ = LS.summary(engine)
    first, last = scalar("SELECT MIN(date) FROM cash_flows"), scalar("SELECT MAX(date) FROM cash_flows")
    exp = {
        "opening_cash": scalar("SELECT SUM(opening_cash) FROM cash_flows WHERE date = :d", d=first),
        "closing_cash": scalar("SELECT SUM(closing_cash) FROM cash_flows WHERE date = :d", d=last),
        "cash_inflow": scalar("SELECT SUM(cash_inflow) FROM cash_flows"), "cash_outflow": scalar("SELECT SUM(cash_outflow) FROM cash_flows"),
        "peak_funding_requirement": scalar("SELECT MAX(f) FROM (SELECT SUM(funding) f FROM cash_flows GROUP BY date) x"),
        "days_in_shortfall": scalar("SELECT COUNT(*) FROM (SELECT date FROM cash_flows GROUP BY date HAVING SUM(funding) > 0) x"),
        "funding_requirement": scalar("SELECT SUM(funding) FROM cash_flows WHERE date = :d", d=last),
    }
    check("Summary (opening, closing, flows, peak funding, shortfall days, end funding) = SQL",
          all(abs(float(ls_[k]) - float(v_)) < 0.05 for k, v_ in exp.items()),
          f"cash position INR {ls_['closing_cash'] / 1e6:,.1f} M, coverage {ls_['coverage_ratio']}x, "
          f"peak funding INR {ls_['peak_funding_requirement'] / 1e6:,.2f} M on {ls_['peak_funding_date']}")
    check("Whole-period cash identity: closing = opening + inflows - outflows",
          abs(ls_["closing_cash"] - (ls_["opening_cash"] + ls_["cash_inflow"] - ls_["cash_outflow"])) < 0.05
          and abs(ls_["net_cash_flow"] - (ls_["cash_inflow"] - ls_["cash_outflow"])) < 0.05)

    lb = LS.by_business_unit(engine)
    check("Business units add up to the company: closing cash, net cash flow, funding requirement",
          abs(lb.closing_cash.sum() - ls_["closing_cash"]) < 0.05 and abs(lb.net_cash_flow.sum() - ls_["net_cash_flow"]) < 0.05
          and abs(lb.funding_requirement.sum() - ls_["funding_requirement"]) < 0.05, f"{len(lb)} business units")
    lm = LS.monthly(engine)
    check(f"Monthly liquidity ({len(lm)} months): each opening = previous closing; flows add up",
          close(lm.opening_cash.iloc[1:], lm.closing_cash.iloc[:-1], 0.05) and abs(lm.cash_inflow.sum() - ls_["cash_inflow"]) < 0.05)

    rng = np.random.default_rng(2026)
    dates = sorted(pd.to_datetime(q("SELECT DISTINCT date FROM cash_flows")["date"]))
    bus = q("SELECT name FROM business_units")["name"].tolist()
    bad = 0
    for _ in range(25):
        i, j = sorted(rng.choice(len(dates), size=2, replace=False))
        subset = tuple(rng.choice(bus, size=int(rng.integers(1, 5)), replace=False))
        r = LS.summary(engine, Filters(start_date=dates[i].date(), end_date=dates[j].date(), business_units=subset))
        want = float(scalar("""SELECT COALESCE(SUM(c.cash_inflow - c.cash_outflow), 0) FROM cash_flows c JOIN business_units b ON b.id = c.business_unit_id
                               WHERE c.date BETWEEN :a AND :b AND b.name = ANY(:n)""", a=dates[i].date(), b=dates[j].date(), n=list(subset)))
        ok = abs(r["closing_cash"] - (r["opening_cash"] + want)) < 0.05 and abs(r["net_cash_flow"] - want) < 0.05
        bad += 0 if ok else 1
    check("25 random date ranges x random business-unit subsets: closing = opening + net flow (checked against raw SQL)", bad == 0, f"{bad} failures")

    # ==================================================================== D
    print("\nD. FILTERS")
    f = Filters.for_period(month="2026-03", business_units=("Equities", "Derivatives"), products=("Equity", "Derivative"))
    got = PS.summary(engine, f)
    want = q("""SELECT COALESCE(SUM(p.revenue),0) r, COALESCE(SUM(p.actual_pnl),0) a FROM pnl p JOIN business_units b ON b.id = p.business_unit_id
                WHERE p.date BETWEEN '2026-03-01' AND '2026-03-31' AND b.name IN ('Equities','Derivatives') AND p.product IN ('Equity','Derivative')""").iloc[0]
    check("Month + business units + products together = raw SQL", close([got["revenue"], got["net_pnl"]], [want.r, want.a], 0.01),
          f"revenue INR {got['revenue'] / 1e6:,.1f} M")
    got = PS.summary(engine, Filters.for_period(quarter="2026-Q1"))
    want = scalar("SELECT SUM(revenue) FROM pnl WHERE date BETWEEN '2026-01-01' AND '2026-03-31'")
    check("Quarter filter (2026-Q1) = raw SQL", abs(got["revenue"] - float(want)) < 0.05)
    got = PS.summary(engine, Filters(start_date=date(2026, 1, 15), end_date=date(2026, 2, 10)))
    want = scalar("SELECT SUM(net) FROM (SELECT actual_pnl AS net FROM pnl WHERE date BETWEEN '2026-01-15' AND '2026-02-10') x")
    check("Arbitrary date range (15 Jan - 10 Feb) = raw SQL", abs(got["net_pnl"] - float(want)) < 0.05)
    check("Currency filter 'INR' keeps everything", abs(PS.summary(engine, Filters(currency="INR"))["revenue"] - s["revenue"]) < 0.05)
    tsum = sum(PS.summary(engine, Filters(business_units=(b,)))["net_pnl"] for b in bus)
    check("Summing the per-business-unit summaries gives the company summary", abs(tsum - s["net_pnl"]) < 0.5)
    empty = PS.summary(engine, Filters(start_date=date(2030, 1, 1), end_date=date(2030, 1, 31)))
    check("A range with no data returns zeros / None, not an error or a division by zero",
          empty["revenue"] == 0 and empty["net_margin_pct"] is None and PS.trend(engine, Filters(start_date=date(2030, 1, 1))).empty
          and LS.summary(engine, Filters(start_date=date(2030, 1, 1)))["closing_cash"] is None)
    check("Unknown business unit is rejected with a helpful message", raises(lambda: PS.summary(engine, Filters(business_units=("Nope",)))))
    check("Unknown product is rejected", raises(lambda: PS.summary(engine, Filters(products=("Crypto",)))))
    check("Currency that is not stored is rejected", raises(lambda: PS.summary(engine, Filters(currency="USD"))))
    check("Malformed month / quarter are rejected", raises(lambda: Filters.for_period(month="2026-13")) and raises(lambda: Filters.for_period(quarter="Q1")))
    check("Product filter is refused where it does not apply (budget variance, liquidity)",
          raises(lambda: PS.budget_variance(engine, Filters(products=("Equity",)))) and raises(lambda: LS.summary(engine, Filters(products=("Equity",)))))

    # ==================================================================== E
    print("\nE. BUDGET VARIANCE and the Phase 2 controls")
    bv = PS.budget_variance(engine)
    check("Budget variance covers every business unit and month", len(bv) == 96, f"{len(bv)} rows")
    fp = q("SELECT to_char(month, 'YYYY-MM') AS month, business_unit, variance, variance_pct FROM v_financial_performance")
    mv = bv.merge(fp, on=["month", "business_unit"])
    check("P&L variance and % per business-unit month = v_financial_performance", len(mv) == 96 and close(mv.pnl_variance, mv.variance)
          and close(mv.pnl_variance_pct, mv.variance_pct, 0.006))
    bud = q("SELECT to_char(month, 'YYYY-MM') AS month, bu.name AS business_unit, budget_revenue, budget_expense FROM budgets b JOIN business_units bu ON bu.id = b.business_unit_id")
    mb2 = bv.merge(bud, on=["month", "business_unit"], suffixes=("", "_sql"))
    check("Revenue and expense budgets used = the budgets table",
          len(mb2) == 96 and close(mb2.budget_revenue, mb2.budget_revenue_sql) and close(mb2.budget_expense, mb2.budget_expense_sql))
    check("Revenue / expense variance recomputed independently",
          close(bv.revenue_variance, bv.revenue - bv.budget_revenue, 1e-6) and close(bv.expense_variance, bv.total_expenses - bv.budget_expense, 1e-6))
    ids = dict(zip(q("SELECT name, id FROM business_units").name, q("SELECT name, id FROM business_units").id))
    breaches = {f"BUM:{ids[r.business_unit]}:{r.month}" for r in bv[bv.breach].itertuples()}
    ctl7 = set(q("SELECT source_ref FROM exceptions WHERE exception_code = 'CTL-007'").source_ref)
    check(f"Months breaching the ±{settings.variance_threshold_pct:g}% threshold here == the CTL-007 exceptions raised in Phase 2",
          breaches == ctl7 and len(breaches) > 0, f"{len(breaches)} breaches")
    lax = PS.budget_variance(engine, threshold_pct=1000)
    check("The threshold is a parameter: at 1000% nothing breaches", int(lax.breach.sum()) == 0)
    check("Views are complete: v_pnl_daily and v_liquidity_position hold every source row",
          scalar("SELECT COUNT(*) FROM v_pnl_daily") == scalar("SELECT COUNT(*) FROM pnl")
          and scalar("SELECT COUNT(*) FROM v_liquidity_position") == scalar("SELECT COUNT(*) FROM cash_flows"))

    passed, total = sum(results), len(results)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{total} checks passed")
    print("Phase 4 is complete. The finance analytics are validated against independent SQL and the source data."
          if passed == total else "Some checks failed - see FAIL lines above.")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
