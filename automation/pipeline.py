"""
FinSight pipeline
=================
Pipeline:  1. generate data (if needed) -> 2. ETL into PostgreSQL -> 3. reconciliation -> 4. financial controls

Later phases add: analytics, anomaly detection, KPI refresh.

Usage
-----
    python automation/pipeline.py                  # full run (generates data only if the CSVs are missing)
    python automation/pipeline.py --generate       # force new data first (same seed = same data)
    python automation/pipeline.py --recon-only     # re-run just the reconciliation on what is already in the DB
    python automation/pipeline.py --controls-only  # re-run just the controls on what is already in the DB
    python automation/pipeline.py --no-workflow-sim  # leave every exception OPEN instead of simulating a workflow

The ETL step REBUILDS the database (all tables are dropped and recreated).
The reconciliation step REPLACES the reconciliations table (its content is fully derived).
The controls step is idempotent: it only adds exceptions that do not exist yet.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

from backend.config import settings  # noqa: E402
from backend.database import engine, get_raw_connection  # noqa: E402
from backend.services.controls_service import run_controls  # noqa: E402
from backend.services.etl_service import RAW_DIR, run_etl  # noqa: E402
from backend.services.reconciliation_service import run_reconciliation  # noqa: E402


def step(n, total, title):
    print(f"\n[{n}/{total}] {title}")
    return time.time()


def main():
    ap = argparse.ArgumentParser(description="FinSight pipeline")
    ap.add_argument("--generate", action="store_true", help="regenerate the CSV data first")
    ap.add_argument("--recon-only", action="store_true", help="skip data + ETL, only run the reconciliation")
    ap.add_argument("--controls-only", action="store_true", help="skip data + ETL, only run the controls")
    ap.add_argument("--no-workflow-sim", action="store_true", help="do not simulate exception workflow statuses")
    ap.add_argument("--seed", type=int, default=settings.seed)
    args = ap.parse_args()

    run_etl_steps = not (args.controls_only or args.recon_only)
    run_recon = not args.controls_only
    run_ctrl = not args.recon_only
    total = 2 * run_etl_steps + run_recon + run_ctrl
    started = time.time()
    n = 0

    if run_etl_steps:
        n += 1
        t0 = step(n, total, "Data generation")
        needed = ["transactions.csv", "ledger_entries.csv", "fx_rates.csv", "liquidity_targets.csv", "injection_manifest.csv"]
        if args.generate or any(not (RAW_DIR / f).exists() for f in needed):
            from generate_data import generate
            generate(seed=args.seed, verbose=False)
            print(f"  Generated fresh CSV files in {RAW_DIR} (seed={args.seed})")
        else:
            print(f"  Using existing CSV files in {RAW_DIR}  (use --generate to recreate)")
        print(f"  {time.time() - t0:.1f}s")

        n += 1
        t0 = step(n, total, "ETL: extract -> validate -> transform -> load")
        conn = get_raw_connection()
        try:
            run_etl(conn)
        finally:
            conn.close()
        print(f"  {time.time() - t0:.1f}s")

    if run_recon:
        n += 1
        t0 = step(n, total, "Reconciliation: source system vs finance ledger")
        run_reconciliation(engine)
        print(f"  {time.time() - t0:.1f}s")

    if run_ctrl:
        n += 1
        t0 = step(n, total, "Financial controls -> exceptions")
        result = run_controls(engine, simulate=not args.no_workflow_sim, seed=args.seed)
        print("  Severity   : " + ", ".join(f"{k} {result.by_severity.get(k, 0):,}"
                                             for k in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]))
        print("  Status     : " + ", ".join(f"{k} {v:,}" for k, v in sorted(result.by_status.items())))
        print(f"  {time.time() - t0:.1f}s")

    print(f"\nPipeline finished in {time.time() - started:.1f}s.  Next:  python data/verify_phase4.py   then   python automation/finance_report.py")


if __name__ == "__main__":
    main()
