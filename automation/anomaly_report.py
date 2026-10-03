"""
FinSight anomaly report (terminal)
==================================
Shows what the Isolation Forest found: counts by risk level, rate by business unit and month,
and the highest-value anomalies WITH their plain-English explanations.

Examples
--------
    python automation/anomaly_report.py
    python automation/anomaly_report.py --risk HIGH --top 15
    python automation/anomaly_report.py --quarter 2026-Q1 --bu Equities
    python automation/anomaly_report.py --order score --top 5
"""
import argparse
import sys
import textwrap
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import engine  # noqa: E402
from backend.services import anomaly_service as AS  # noqa: E402
from backend.utils.filters import Filters  # noqa: E402

M = lambda x: f"{x / 1e6:,.2f}"            # noqa: E731


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def main():
    ap = argparse.ArgumentParser(description="FinSight anomaly report")
    ap.add_argument("--month"), ap.add_argument("--quarter")
    ap.add_argument("--start", type=date.fromisoformat), ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--bu", action="append", help="business unit (repeatable)")
    ap.add_argument("--product", action="append")
    ap.add_argument("--risk", choices=["HIGH", "MEDIUM", "LOW"], help="only list this risk level")
    ap.add_argument("--order", choices=["amount", "score"], default="amount")
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()

    try:
        if a.month or a.quarter:
            f = Filters.for_period(month=a.month, quarter=a.quarter, business_units=a.bu, products=a.product)
        else:
            f = Filters(start_date=a.start, end_date=a.end, business_units=a.bu, products=a.product)
        run, s = AS.latest_run(engine), AS.summary(engine, f)
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}")
    if run is None or s["scored"] == 0:
        raise SystemExit("No scored transactions for that selection. Run:  python automation/pipeline.py")

    print(f"\nFINSIGHT ANOMALY REPORT  |  model {run['model_version']}  |  feature set '{run['feature_set']}'  |  flags the top {100 * run['contamination']:.1f}% by score")
    section("SUMMARY")
    print(f"  Transactions scored   {s['scored']:>10,}")
    print(f"  Anomalies flagged     {s['anomalies']:>10,}   ({s['anomaly_rate_pct']}% anomaly rate)")
    print(f"  HIGH / MEDIUM / LOW   {s['high']:>10,} / {s['medium']:,} / {s['low']:,}")
    print(f"  Value flagged         INR {M(s['exposure_inr'])} M   (HIGH risk only: INR {M(s['high_risk_exposure_inr'])} M)")
    print(f"  Score thresholds      flag >= {run['threshold_flag']:.3f}   medium >= {run['threshold_medium']:.3f}   high >= {run['threshold_high']:.3f}   (0.5 = nothing stands out)")
    ex = run["excluded"]
    print("  Not scored            " + ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in ex.items() if v) + "  (handled by the control engine)")

    section("BY BUSINESS UNIT")
    b = AS.by_business_unit(engine, f)
    v = b[["business_unit", "scored", "anomalies", "anomaly_rate_pct", "high", "medium", "low", "exposure_inr"]].copy()
    v["exposure_inr"] = v["exposure_inr"].map(M)
    v["anomaly_rate_pct"] = v["anomaly_rate_pct"].map(lambda x: f"{x:.2f}%")
    v.columns = ["business unit", "scored", "anomalies", "rate", "high", "medium", "low", "value INR M"]
    print(v.to_string(index=False))

    section("BY MONTH")
    t = AS.trend(engine, f, "month")
    v = t[["period", "scored", "anomalies", "anomaly_rate_pct", "high", "exposure_inr"]].copy()
    v["exposure_inr"] = v["exposure_inr"].map(M)
    v["anomaly_rate_pct"] = v["anomaly_rate_pct"].map(lambda x: f"{x:.2f}%")
    v.columns = ["month", "scored", "anomalies", "rate", "high", "value INR M"]
    print(v.to_string(index=False))

    top = AS.top_anomalies(engine, f, n=500 if a.risk else a.top, order_by=a.order)
    if a.risk:
        top = top[top.risk_level == a.risk].head(a.top)
    section(f"TOP {len(top)} ANOMALIES BY {'VALUE' if a.order == 'amount' else 'SCORE'}" + (f" ({a.risk} only)" if a.risk else ""))
    for r in top.itertuples():
        print(f"\n  {r.transaction_id}  {r.transaction_date:%Y-%m-%d %H:%M}  {r.business_unit} / {r.product} / {r.transaction_type}"
              f"  INR {M(abs(r.amount_inr))} M   score {r.anomaly_score:.3f}  [{r.risk_level}]")
        print(textwrap.fill(r.reasons, width=110, initial_indent="    why: ", subsequent_indent="         "))
    print()


if __name__ == "__main__":
    main()
