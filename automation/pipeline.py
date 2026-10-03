"""
FinSight pipeline
=================
Pipeline:  1. generate data (if needed) -> 2. ETL into PostgreSQL -> 3. reconciliation -> 4. financial controls
           -> 5. anomaly detection (Isolation Forest)

Later phases add: KPI refresh and automation.

Usage
-----
    python automation/pipeline.py                  # full run (generates data only if the CSVs are missing)
    python automation/pipeline.py --generate       # force new data first (same seed = same data)
    python automation/pipeline.py --recon-only     # re-run just the reconciliation on what is already in the DB
    python automation/pipeline.py --controls-only  # re-run just the controls on what is already in the DB
    python automation/pipeline.py --ml-only        # re-train and re-score the anomaly model on what is in the DB
    python automation/pipeline.py --ml-only --no-retrain   # re-score with the saved ml/model.pkl (no training)
    python automation/pipeline.py --skip-ml        # everything except the anomaly model
    python automation/pipeline.py --no-workflow-sim  # leave every exception OPEN instead of simulating a workflow

The ETL step REBUILDS the database (all tables are dropped and recreated).
The reconciliation step REPLACES the reconciliations table (its content is fully derived).
The controls step is idempotent: it only adds exceptions that do not exist yet.
The anomaly step REPLACES anomaly_scores and anomalies (derived) and APPENDS a row to model_runs.
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
from backend.services.anomaly_service import run_anomaly_detection  # noqa: E402
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
    ap.add_argument("--ml-only", action="store_true", help="skip data + ETL, only run the anomaly model")
    ap.add_argument("--skip-ml", action="store_true", help="run everything except the anomaly model")
    ap.add_argument("--no-retrain", action="store_true", help="score with the saved ml/model.pkl instead of training")
    ap.add_argument("--no-workflow-sim", action="store_true", help="do not simulate exception workflow statuses")
    ap.add_argument("--seed", type=int, default=settings.seed)
    args = ap.parse_args()

    single = args.controls_only or args.recon_only or args.ml_only
    run_etl_steps = not single
    run_recon = not single or args.recon_only
    run_ctrl = not single or args.controls_only
    run_ml = (not single and not args.skip_ml) or args.ml_only
    total = 2 * run_etl_steps + run_recon + run_ctrl + run_ml
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

    if run_ml:
        n += 1
        t0 = step(n, total, "Anomaly detection: Isolation Forest")
        run_anomaly_detection(engine, retrain=not args.no_retrain)
        print(f"  {time.time() - t0:.1f}s")

    print(f"\nPipeline finished in {time.time() - started:.1f}s.  Next:  python data/verify_phase5.py")


if __name__ == "__main__":
    main()
