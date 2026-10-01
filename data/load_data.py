"""
FinSight - load generated CSVs into PostgreSQL
==============================================
Kept from Phase 1 as a convenience: it now simply runs the ETL stages
(extract -> validate -> transform -> load) WITHOUT the control engine.
The full pipeline is:  python automation/pipeline.py

DESTRUCTIVE: drops and recreates every table.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import get_raw_connection  # noqa: E402
from backend.services.etl_service import run_etl  # noqa: E402


def main():
    started = time.time()
    conn = get_raw_connection()
    try:
        print("Running ETL (schema is rebuilt) ...")
        run_etl(conn)
        with conn.cursor() as cur:
            print("\nRow counts in the database:")
            for table in ["business_units", "fx_rates", "transactions", "ledger_entries", "budgets", "pnl",
                          "cash_flows", "reconciliations", "exceptions", "anomalies"]:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                print(f"  {table:<16}{cur.fetchone()[0]:>10,}")
    finally:
        conn.close()
    print(f"\nDone in {time.time() - started:.1f}s.  Next step:  python automation/pipeline.py")


if __name__ == "__main__":
    main()
