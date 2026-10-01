"""
FinSight - synthetic data generator (Phase 1)
=============================================

Creates realistic, *internally consistent* finance data, then deliberately
injects known problems so the control / reconciliation / ML modules in later
phases have something to detect.

Outputs (CSV files in data/raw/):
    business_units.csv
    transactions.csv        <- source system (contains injected problems)
    ledger_entries.csv      <- finance ledger (contains injected problems)
    budgets.csv
    pnl.csv                 <- derived from the CLEAN economics (ground truth)
    cash_flows.csv
    injection_manifest.csv  <- answer key: exactly which records were corrupted

Usage:
    python data/generate_data.py
    python data/generate_data.py --transactions 200000 --start-date 2025-09-01 --seed 7
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402

RAW_DIR = ROOT / "data" / "raw"

# ----------------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------------
REPORTING_CCY = "INR"
# Fixed FX rates to INR (simple and deterministic for a portfolio project)
FX_TO_INR = {"INR": 1.0, "USD": 83.5, "EUR": 90.5, "GBP": 106.0, "SGD": 62.0}
CURRENCIES = list(FX_TO_INR)

REGION_CCY_PROBS = {  # probability of each currency for a given region
    "Mumbai":    [0.85, 0.10, 0.02, 0.01, 0.02],
    "Singapore": [0.15, 0.35, 0.05, 0.05, 0.40],
    "London":    [0.07, 0.20, 0.25, 0.45, 0.03],
    "New York":  [0.07, 0.75, 0.08, 0.07, 0.03],
}  # order = INR, USD, EUR, GBP, SGD

BUSINESS_UNITS = [
    # id, name, division, region, txn share, amount scale, monthly growth, budget bias, products, source system
    dict(id=1, name="Equities",           division="Markets",                 region="Mumbai",
         weight=0.18, scale=1.0, growth=0.012, bias=0.005, products=["Equity", "Fund"],
         source="TRADING_SYS"),
    dict(id=2, name="Derivatives",        division="Markets",                 region="Singapore",
         weight=0.14, scale=1.6, growth=0.015, bias=0.020, products=["Derivative", "FX"],
         source="TRADING_SYS"),
    dict(id=3, name="Fixed Income",       division="Markets",                 region="London",
         weight=0.13, scale=1.4, growth=0.006, bias=-0.015, products=["Bond", "Loan"],
         source="TRADING_SYS"),
    dict(id=4, name="Wealth Management",  division="Wealth & Asset Mgmt",     region="Mumbai",
         weight=0.15, scale=0.7, growth=0.010, bias=0.008, products=["Fund", "Equity", "Bond"],
         source="WEALTH_SYS"),
    dict(id=5, name="Treasury",           division="Treasury",                region="Mumbai",
         weight=0.08, scale=2.2, growth=0.004, bias=0.000, products=["FX", "Bond", "Loan"],
         source="TREASURY_SYS"),
    dict(id=6, name="Investment Banking", division="Investment Banking",      region="New York",
         weight=0.08, scale=2.5, growth=0.008, bias=0.012, products=["Equity", "Bond", "Loan"],
         source="DEALS_SYS"),
    dict(id=7, name="Asset Management",   division="Wealth & Asset Mgmt",     region="New York",
         weight=0.12, scale=1.1, growth=0.009, bias=-0.005, products=["Fund", "Equity", "Bond"],
         source="WEALTH_SYS"),
    dict(id=8, name="Corporate Finance",  division="Corporate",               region="Mumbai",
         weight=0.12, scale=1.2, growth=0.005, bias=-0.010, products=["Loan", "Bond", "FX"],
         source="LOANS_SYS"),
]

TX_TYPES = ["REVENUE", "COST", "EXPENSE", "ADJUSTMENT"]
TX_TYPE_PROBS = [0.44, 0.295, 0.25, 0.015]
BASE_MEDIAN_INR = {"REVENUE": 30_000, "COST": 20_000, "EXPENSE": 14_000, "ADJUSTMENT": 8_000}

# Share of transactions affected by each injected problem (spec section 22)
RATES = {
    "duplicate": 0.015,        # 1-2 %
    "missing_ledger": 0.010,   # 1 %
    "amount_mismatch": 0.010,  # 1 %
    "missing_fields": 0.005,   # 0.5 %
    "unusual": 0.005,          # 0.5 %
    # extras that make the control rules demonstrable:
    "invalid_amount": 0.002,   # amount <= 0 on a non-adjustment type (Rule 3)
    "orphan_ledger": 0.003,    # ledger entry with no source transaction
}


# ----------------------------------------------------------------------------
# Step 1 - clean base transactions
# ----------------------------------------------------------------------------
def build_base(n, start, end, rng):
    days = pd.bdate_range(start, end)  # weekdays only
    day_w = 1 + 0.25 * rng.random(len(days))
    day_w = day_w * np.where(days.day >= 26, 1.15, 1.0)  # month-end is busier
    day_i = rng.choice(len(days), size=n, p=day_w / day_w.sum())
    month_idx = ((days.year - start.year) * 12 + days.month - start.month).to_numpy()[day_i]
    month_num = days.month.to_numpy()[day_i]

    bu_weights = np.array([b["weight"] for b in BUSINESS_UNITS])
    bu_i = rng.choice(len(BUSINESS_UNITS), size=n, p=bu_weights / bu_weights.sum())
    tx_type = rng.choice(TX_TYPES, size=n, p=TX_TYPE_PROBS)

    product = np.empty(n, dtype=object)
    currency = np.empty(n, dtype=object)
    for i, bu in enumerate(BUSINESS_UNITS):
        m = bu_i == i
        product[m] = rng.choice(bu["products"], size=m.sum())
        currency[m] = rng.choice(CURRENCIES, size=m.sum(), p=REGION_CCY_PROBS[bu["region"]])

    # Amount in INR-equivalent, then converted to the transaction currency
    growth = np.array([b["growth"] for b in BUSINESS_UNITS])[bu_i]
    scale = np.array([b["scale"] for b in BUSINESS_UNITS])[bu_i]
    base = pd.Series(tx_type).map(BASE_MEDIAN_INR).to_numpy(dtype=float)
    is_rev = tx_type == "REVENUE"
    is_cost = (tx_type == "COST") | (tx_type == "EXPENSE")
    trend = np.ones(n)
    trend[is_rev] = (1 + growth[is_rev]) ** month_idx[is_rev]
    trend[is_cost] = (1 + 0.5 * growth[is_cost]) ** month_idx[is_cost]
    quarter_end = np.isin(month_num, [3, 6, 9, 12])
    trend[is_rev & quarter_end] *= 1.06  # quarter-end revenue bump

    amount_inr = base * scale * trend * rng.lognormal(0.0, 0.9, size=n)
    fx = pd.Series(currency).map(FX_TO_INR).to_numpy(dtype=float)
    amount = np.round(np.maximum(amount_inr / fx, 1.0), 2)
    adj = tx_type == "ADJUSTMENT"
    amount[adj] *= np.where(rng.random(adj.sum()) < 0.5, -1, 1)  # adjustments can be negative

    hours = np.arange(8, 18)
    hour_p = np.array([3, 6, 8, 8, 6, 5, 7, 8, 6, 3], dtype=float)
    hour = rng.choice(hours, size=n, p=hour_p / hour_p.sum())
    secs = hour * 3600 + rng.integers(0, 3600, size=n)
    ts = pd.DatetimeIndex(days[day_i]) + pd.to_timedelta(secs, unit="s")

    df = pd.DataFrame(
        {
            "ts": ts,
            "business_unit_id": np.array([b["id"] for b in BUSINESS_UNITS])[bu_i],
            "product": product,
            "currency": currency,
            "amount": amount,
            "transaction_type": tx_type,
            "source_system": np.array([b["source"] for b in BUSINESS_UNITS])[bu_i],
        }
    )
    return df, days


def inject_unusual(df, rng):
    """Unusual transactions are REAL economic events (they stay in the P&L),
    but they are far larger than normal and mostly happen at odd hours."""
    n = len(df)
    k = int(round(RATES["unusual"] * n))
    eligible = np.flatnonzero((df.transaction_type != "ADJUSTMENT").to_numpy())
    idx = rng.choice(eligible, size=k, replace=False)
    df["is_unusual"] = False
    df.loc[df.index[idx], "is_unusual"] = True
    df.loc[df.index[idx], "amount"] = np.round(df.loc[df.index[idx], "amount"] * rng.uniform(8, 30, size=k), 2)
    odd = idx[rng.random(k) < 0.7]
    odd_hours = rng.choice([0, 1, 2, 3, 4, 22, 23], size=len(odd))
    df.loc[df.index[odd], "ts"] = (
        df.loc[df.index[odd], "ts"].dt.normalize()
        + pd.to_timedelta(odd_hours * 3600 + rng.integers(0, 3600, size=len(odd)), unit="s")
    )
    return df


# ----------------------------------------------------------------------------
# Step 2 - P&L, budgets, cash flows (derived from CLEAN economics)
# ----------------------------------------------------------------------------
def build_pnl_and_budgets(df, days, rng):
    econ = df[df.transaction_type.isin(["REVENUE", "COST", "EXPENSE"])].copy()
    econ["inr"] = econ.amount * econ.currency.map(FX_TO_INR)
    econ["date"] = econ.ts.dt.normalize()
    agg = econ.pivot_table(
        index=["date", "business_unit_id", "product"],
        columns="transaction_type", values="inr", aggfunc="sum", fill_value=0.0,
    ).reset_index()
    for c in ["REVENUE", "COST", "EXPENSE"]:
        if c not in agg:
            agg[c] = 0.0

    # full grid: every business day x BU x product of that BU (zeros where no trades)
    grid = pd.DataFrame(
        [(d, b["id"], p) for b in BUSINESS_UNITS for p in b["products"] for d in days],
        columns=["date", "business_unit_id", "product"],
    )
    pnl = grid.merge(agg, on=["date", "business_unit_id", "product"], how="left").fillna(0.0)
    pnl = pnl.rename(columns={"REVENUE": "revenue", "COST": "cost", "EXPENSE": "expense"})
    for c in ["revenue", "cost", "expense"]:
        pnl[c] = pnl[c].round(2)
    pnl["actual_pnl"] = (pnl.revenue - pnl.cost - pnl.expense).round(2)
    pnl["month"] = pnl["date"].dt.to_period("M").dt.to_timestamp()

    # ---- monthly budgets per BU -------------------------------------------
    monthly = pnl.groupby(["month", "business_unit_id"]).agg(
        rev=("revenue", "sum"), exp=("cost", "sum"), exp2=("expense", "sum")).reset_index()
    monthly["exp"] = monthly.exp + monthly.exp2
    bias = {b["id"]: b["bias"] for b in BUSINESS_UNITS}
    v_rev = rng.normal(monthly.business_unit_id.map(bias), 0.010)   # actual vs budget revenue
    v_exp = rng.normal(0.0, 0.008, size=len(monthly))
    monthly["budget_revenue"] = monthly.rev / (1 + v_rev)
    monthly["budget_expense"] = monthly.exp / (1 + v_exp)

    # a few deliberately "excessive variance" BU-months (control Rule 7)
    n_bad = 3
    bad = rng.choice(len(monthly), size=n_bad, replace=False)
    monthly.loc[monthly.index[bad], "budget_revenue"] *= np.array([0.88, 1.12, 0.88])[:n_bad]

    monthly["budget_revenue"] = monthly.budget_revenue.round(2)
    monthly["budget_expense"] = monthly.budget_expense.round(2)
    monthly["budget_pnl"] = (monthly.budget_revenue - monthly.budget_expense).round(2)
    budgets = monthly[["month", "business_unit_id", "budget_revenue", "budget_expense", "budget_pnl"]].copy()

    # ---- allocate monthly budget P&L to daily rows (sums back exactly) ------
    pnl = pnl.merge(budgets[["month", "business_unit_id", "budget_pnl"]].rename(
        columns={"budget_pnl": "bu_month_budget"}), on=["month", "business_unit_id"], how="left")
    pnl = pnl.sort_values(["business_unit_id", "month", "date", "product"]).reset_index(drop=True)
    g = pnl.groupby(["business_unit_id", "month"])
    k = g.cumcount() + 1
    n_rows = g["date"].transform("size")
    total = pnl["bu_month_budget"]
    pnl["budget_pnl"] = (total * k / n_rows).round(2) - (total * (k - 1) / n_rows).round(2)
    pnl["budget_pnl"] = pnl["budget_pnl"].round(2)
    pnl["currency"] = REPORTING_CCY
    pnl = pnl.sort_values(["date", "business_unit_id", "product"]).reset_index(drop=True)
    pnl = pnl[["date", "business_unit_id", "product", "currency",
               "revenue", "cost", "expense", "actual_pnl", "budget_pnl"]]
    return pnl, budgets


def build_cash_flows(pnl, days, rng):
    daily = pnl.groupby(["business_unit_id", "date"]).agg(
        rev=("revenue", "sum"), cost=("cost", "sum"), exp=("expense", "sum")).reset_index()
    last_bday = pd.Series(days, index=days).groupby([days.year, days.month]).transform("max") == days
    rows = []
    targets = []
    for bu in BUSINESS_UNITS:
        d = daily[daily.business_unit_id == bu["id"]].set_index("date").reindex(days).fillna(0.0)
        rev = d.rev.to_numpy()
        out_base = (d.cost + d.exp).to_numpy()
        avg_rev, avg_out = rev.mean(), out_base.mean()
        opening = round(8 * avg_rev, 2)      # starting cash pool
        target = opening                     # month-end sweep keeps cash near this level
        buffer = 0.85 * target               # minimum liquidity buffer (85% of the starting cash pool)
        targets.append((bu["id"], round(buffer, 2)))
        for i, day in enumerate(days):
            inflow = round(rev[i], 2)
            outflow = out_base[i]
            if rng.random() < 0.04:          # lumpy settlement / collateral call
                outflow += rng.uniform(1.0, 3.0) * avg_rev
            closing = opening + inflow - outflow
            if last_bday.iloc[i]:            # month-end sweep of surplus to central treasury
                outflow += max(0.0, 0.75 * (closing - target))
            outflow = round(outflow, 2)
            closing = round(opening + inflow - outflow, 2)
            funding = round(max(0.0, buffer - closing), 2)
            rows.append((day, bu["id"], round(opening, 2), inflow, outflow, closing, funding))
            opening = closing
    cash = pd.DataFrame(rows, columns=["date", "business_unit_id", "opening_cash",
                                       "cash_inflow", "cash_outflow", "closing_cash", "funding"])
    return cash, pd.DataFrame(targets, columns=["business_unit_id", "min_liquidity"])


# ----------------------------------------------------------------------------
# Step 3 - ledger + injected problems
# ----------------------------------------------------------------------------
def build_source_and_ledger(df, rng):
    n = len(df)
    used = df["is_unusual"].to_numpy().copy()

    def take(k, eligible=None):
        mask = ~used if eligible is None else (~used & eligible)
        chosen = rng.choice(np.flatnonzero(mask), size=k, replace=False)
        used[chosen] = True
        return chosen

    not_adj = (df.transaction_type != "ADJUSTMENT").to_numpy()
    k = {name: int(round(rate * n)) for name, rate in RATES.items()}

    # Ledger: one entry per transaction, posted 0-2 business days later
    lag = rng.choice([0, 1, 2], size=n, p=[0.70, 0.25, 0.05])
    d0 = df.ts.dt.normalize().to_numpy().astype("datetime64[D]")
    led = pd.DataFrame(
        {
            "transaction_id": df.transaction_id.to_numpy(),
            "ledger_date": pd.to_datetime(np.busday_offset(d0, lag, roll="forward")),
            "amount": df.amount.to_numpy(),
            "currency": df.currency.to_numpy(),
            "business_unit_id": df.business_unit_id.to_numpy(),
        },
        index=df.index,
    )
    src = df[["transaction_id", "ts", "business_unit_id", "product", "currency",
              "amount", "transaction_type", "source_system", "created_at"]].copy()
    src = src.rename(columns={"ts": "transaction_date"})
    src["business_unit_id"] = src["business_unit_id"].astype("Int64")
    manifest = []

    def log(idx, issue, note=""):
        manifest.append(pd.DataFrame({"transaction_id": df.transaction_id.to_numpy()[idx],
                                      "issue_type": issue, "note": note}))

    log(np.flatnonzero(df["is_unusual"].to_numpy()), "UNUSUAL_TRANSACTION", "amount 8-30x normal, mostly off-hours")

    # 1) invalid amounts - identical in source and ledger (only the control rule should catch it)
    idx = take(k["invalid_amount"], not_adj)
    new_amt = np.where(rng.random(len(idx)) < 0.5, 0.0, -df.amount.to_numpy()[idx])
    src.iloc[idx, src.columns.get_loc("amount")] = new_amt
    led.iloc[idx, led.columns.get_loc("amount")] = new_amt
    log(idx, "INVALID_AMOUNT", "amount <= 0 on a non-adjustment type")

    # 2) missing fields in the SOURCE system (ledger keeps the true values)
    idx = take(k["missing_fields"])
    third = len(idx) // 3
    i_id, i_ccy, i_bu = idx[:third], idx[third:2 * third], idx[2 * third:]
    src.iloc[i_id, src.columns.get_loc("transaction_id")] = None
    src.iloc[i_ccy, src.columns.get_loc("currency")] = None
    src.iloc[i_bu, src.columns.get_loc("business_unit_id")] = pd.NA
    log(i_id, "MISSING_TRANSACTION_ID", "source transaction_id is NULL")
    log(i_ccy, "MISSING_CURRENCY", "source currency is NULL")
    log(i_bu, "MISSING_BUSINESS_UNIT", "source business_unit_id is NULL")

    # 3) amount mismatches - ledger amount differs from source
    idx = take(k["amount_mismatch"])
    old = led.amount.to_numpy()[idx]
    new = np.round(old * (1 + rng.choice([-1, 1], size=len(idx)) * rng.uniform(0.01, 0.20, size=len(idx))), 2)
    new = np.where(np.abs(new - old) < 0.01, old + 1.0, new)
    led.iloc[idx, led.columns.get_loc("amount")] = new
    log(idx, "AMOUNT_MISMATCH", "ledger amount differs from source amount")

    # 4) missing from ledger
    idx_missing = take(k["missing_ledger"])
    log(idx_missing, "MISSING_IN_LEDGER", "no ledger entry exists")

    # 5) duplicates in the source system (same transaction_id loaded twice)
    idx_dup = take(k["duplicate"])
    dups = src.iloc[idx_dup].copy()
    dups["created_at"] = dups["created_at"] + pd.to_timedelta(rng.integers(60, 172_800, size=len(dups)), unit="s")
    log(idx_dup, "DUPLICATE", "transaction loaded twice in source")

    # 6) ledger orphans (booked in the ledger, absent from the source system)
    n_orph = k["orphan_ledger"]
    pick = rng.choice(n, size=n_orph, replace=False)
    orph = led.iloc[pick].copy()
    orph["transaction_id"] = [f"TXN-{n + 1 + i:08d}" for i in range(n_orph)]
    orph["amount"] = np.round(orph.amount * rng.uniform(0.8, 1.2, size=n_orph), 2)
    manifest.append(pd.DataFrame({"transaction_id": orph.transaction_id.to_numpy(),
                                  "issue_type": "ORPHAN_LEDGER_ENTRY",
                                  "note": "ledger entry with no source transaction"}))

    led = led.drop(index=df.index[idx_missing])
    led = pd.concat([led, orph], ignore_index=True).sort_values(["ledger_date", "transaction_id"])
    src = pd.concat([src, dups], ignore_index=True).sort_values(["transaction_date", "created_at"])
    manifest = pd.concat(manifest, ignore_index=True)
    return src.reset_index(drop=True), led.reset_index(drop=True), manifest


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
CURRENCY_NAMES = {"INR": "Indian Rupee", "USD": "US Dollar", "EUR": "Euro",
                  "GBP": "British Pound", "SGD": "Singapore Dollar"}


def generate(transactions=150_000, start_date="2025-09-01", months=12, seed=None,
             output_dir=RAW_DIR, verbose=True):
    """Generate all CSV files. Callable from the pipeline; returns a dict of row counts."""
    seed = settings.seed if seed is None else seed
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    start = pd.Timestamp(start_date)
    end = start + pd.DateOffset(months=months) - pd.Timedelta(days=1)

    if verbose:
        print(f"Generating {transactions:,} transactions from {start.date()} to {end.date()} (seed={seed})")
    df, days = build_base(transactions, start, end, rng)
    df = inject_unusual(df, rng)
    df = df.sort_values("ts").reset_index(drop=True)
    df.insert(0, "transaction_id", [f"TXN-{i + 1:08d}" for i in range(len(df))])
    df["created_at"] = df.ts + pd.to_timedelta(rng.integers(1, 300, size=len(df)), unit="s")

    pnl, budgets = build_pnl_and_budgets(df, days, rng)
    cash, liq_targets = build_cash_flows(pnl, days, rng)
    src, led, manifest = build_source_and_ledger(df, rng)

    bu_df = pd.DataFrame([{k: b[k] for k in ("id", "name", "division", "region")} for b in BUSINESS_UNITS])
    fx_df = pd.DataFrame({"currency": CURRENCIES,
                          "name": [CURRENCY_NAMES[c] for c in CURRENCIES],
                          "rate_to_inr": [FX_TO_INR[c] for c in CURRENCIES]})

    ts_fmt = "%Y-%m-%d %H:%M:%S"
    src.to_csv(out / "transactions.csv", index=False, date_format=ts_fmt, float_format="%.2f")
    led.assign(ledger_date=led.ledger_date.dt.strftime("%Y-%m-%d")).to_csv(
        out / "ledger_entries.csv", index=False, float_format="%.2f")
    budgets.assign(month=budgets.month.dt.strftime("%Y-%m-%d")).to_csv(
        out / "budgets.csv", index=False, float_format="%.2f")
    pnl.assign(date=pnl.date.dt.strftime("%Y-%m-%d")).to_csv(out / "pnl.csv", index=False, float_format="%.2f")
    cash.assign(date=cash.date.dt.strftime("%Y-%m-%d")).to_csv(
        out / "cash_flows.csv", index=False, float_format="%.2f")
    bu_df.to_csv(out / "business_units.csv", index=False)
    fx_df.to_csv(out / "fx_rates.csv", index=False)
    liq_targets.to_csv(out / "liquidity_targets.csv", index=False, float_format="%.2f")
    manifest.to_csv(out / "injection_manifest.csv", index=False)

    frames = [("business_units", bu_df), ("fx_rates", fx_df), ("liquidity_targets", liq_targets), ("transactions", src),
              ("ledger_entries", led), ("budgets", budgets), ("pnl", pnl), ("cash_flows", cash),
              ("injection_manifest", manifest)]
    if verbose:
        print("\nFiles written to", out)
        for name, frame in frames:
            print(f"  {name + '.csv':<24}{len(frame):>10,} rows")
        print("\nInjected problems (see injection_manifest.csv):")
        print(manifest.issue_type.value_counts().to_string())
        print(f"\nTotal revenue : INR {pnl.revenue.sum() / 1e6:,.1f} M")
        print(f"Net P&L       : INR {pnl.actual_pnl.sum() / 1e6:,.1f} M")
    return {name: len(frame) for name, frame in frames}


def main():
    ap = argparse.ArgumentParser(description="FinSight synthetic data generator")
    ap.add_argument("--transactions", type=int, default=150_000, help="number of base transactions (default 150000)")
    ap.add_argument("--start-date", default="2025-09-01", help="first day of the 12-month window (default 2025-09-01)")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--seed", type=int, default=settings.seed)
    ap.add_argument("--output-dir", default=str(RAW_DIR))
    a = ap.parse_args()
    generate(a.transactions, a.start_date, a.months, a.seed, a.output_dir)
    print("\nNext step:  python automation/pipeline.py")


if __name__ == "__main__":
    main()
