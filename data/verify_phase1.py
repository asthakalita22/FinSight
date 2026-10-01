"""
FinSight - Phase 1 verification
===============================
Runs SQL checks against the database and prints PASS / FAIL:

  A. Tables are populated
  B. Finance integrity   (P&L identity, cash identity, budget allocation, views)
  C. Injected problems   (what SQL can detect == what the generator injected)

Usage:
    python data/verify_phase1.py
"""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import engine  # noqa: E402

MANIFEST = ROOT / "data" / "raw" / "injection_manifest.csv"
results = []


def scalar(sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main():
    print("\nA. TABLES POPULATED")
    for table, minimum in [("business_units", 8), ("transactions", 100_000), ("ledger_entries", 100_000),
                           ("budgets", 96), ("pnl", 1_000), ("cash_flows", 1_000)]:
        n = scalar(f"SELECT COUNT(*) FROM {table}")
        check(f"{table} has data", n >= minimum, f"{n:,} rows")

    print("\nB. FINANCE INTEGRITY")
    bad = scalar("SELECT COUNT(*) FROM pnl WHERE ABS(actual_pnl - (revenue - cost - expense)) > 0.01")
    check("P&L identity: actual_pnl = revenue - cost - expense", bad == 0, f"{bad} violations")

    bad = scalar("SELECT COUNT(*) FROM cash_flows WHERE ABS(closing_cash - (opening_cash + cash_inflow - cash_outflow)) > 0.01")
    check("Cash identity: closing = opening + inflow - outflow", bad == 0, f"{bad} violations")

    bad = scalar("""
        SELECT COUNT(*) FROM (
            SELECT opening_cash - LAG(closing_cash) OVER (PARTITION BY business_unit_id ORDER BY date) AS gap
            FROM cash_flows) x
        WHERE ABS(gap) > 0.01""")
    check("Cash continuity: opening(today) = closing(yesterday)", bad == 0, f"{bad} violations")

    worst = scalar("""
        SELECT COALESCE(MAX(ABS(b.budget_pnl - p.s)), 0) FROM budgets b
        JOIN (SELECT date_trunc('month', date)::date AS month, business_unit_id, SUM(budget_pnl) AS s
              FROM pnl GROUP BY 1, 2) p
          ON p.month = b.month AND p.business_unit_id = b.business_unit_id""")
    check("Daily budget P&L sums back to monthly budget", float(worst) < 0.5, f"max diff {float(worst):.2f}")

    n = scalar("SELECT COUNT(*) FROM v_financial_performance")
    check("View v_financial_performance returns 8 BUs x 12 months", n == 96, f"{n} rows")

    n = scalar("""SELECT COUNT(*) FROM v_financial_performance WHERE ABS(variance_pct) > 15""")
    check("A few BU-months breach a 15% variance threshold (for Rule 7)", 1 <= n <= 10, f"{n} BU-months")

    n = scalar("SELECT COUNT(*) FROM cash_flows WHERE funding > 0")
    check("Some liquidity stress exists (funding > 0)", n > 0, f"{n} BU-days need funding")

    lag = scalar("""SELECT MAX(l.ledger_date - t.transaction_date::date) FROM transactions t
                    JOIN ledger_entries l ON l.transaction_id = t.transaction_id""")
    check("Ledger posting lag is within 4 calendar days", lag is not None and lag <= 4, f"max lag {lag} days")

    print("\nC. INJECTED PROBLEMS: SQL detection vs generator answer key")
    if not MANIFEST.exists():
        check("injection_manifest.csv found", False, "run data/generate_data.py first")
    else:
        m = pd.read_csv(MANIFEST).issue_type.value_counts().to_dict()
        sql = {
            "DUPLICATE": """SELECT COUNT(*) FROM (SELECT transaction_id FROM transactions
                            WHERE transaction_id IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1) x""",
            "MISSING_IN_LEDGER": """SELECT COUNT(DISTINCT t.transaction_id) FROM transactions t
                            WHERE t.transaction_id IS NOT NULL AND NOT EXISTS
                            (SELECT 1 FROM ledger_entries l WHERE l.transaction_id = t.transaction_id)""",
            "AMOUNT_MISMATCH": """SELECT COUNT(DISTINCT t.transaction_id) FROM transactions t
                            JOIN ledger_entries l ON l.transaction_id = t.transaction_id
                            WHERE t.amount <> l.amount""",
            "MISSING_TRANSACTION_ID": "SELECT COUNT(*) FROM transactions WHERE transaction_id IS NULL",
            "MISSING_CURRENCY": "SELECT COUNT(*) FROM transactions WHERE currency IS NULL",
            "MISSING_BUSINESS_UNIT": "SELECT COUNT(*) FROM transactions WHERE business_unit_id IS NULL",
            "INVALID_AMOUNT": "SELECT COUNT(*) FROM transactions WHERE amount <= 0 AND transaction_type <> 'ADJUSTMENT'",
            # ledger rows without a source row = deliberate orphans + rows whose source id was blanked out
            "ORPHAN_LEDGER_ENTRY": """SELECT COUNT(*) FROM ledger_entries l WHERE NOT EXISTS
                            (SELECT 1 FROM transactions t WHERE t.transaction_id = l.transaction_id)""",
        }
        for issue, query in sql.items():
            injected = m.get(issue, 0)
            expected = injected + (m.get("MISSING_TRANSACTION_ID", 0) if issue == "ORPHAN_LEDGER_ENTRY" else 0)
            found = scalar(query)
            note = " (orphans + blank-ID rows)" if issue == "ORPHAN_LEDGER_ENTRY" else ""
            check(f"{issue:<24} injected {expected:>5,} | SQL found {found:>5,}{note}", found == expected)

        base = scalar("SELECT COUNT(*) FROM transactions") - m.get("DUPLICATE", 0)
        print(f"\n  Injection rates vs {base:,} base transactions:")
        for issue in ["DUPLICATE", "MISSING_IN_LEDGER", "AMOUNT_MISMATCH", "UNUSUAL_TRANSACTION"]:
            print(f"    {issue:<24}{100 * m.get(issue, 0) / base:5.2f}%")
        fields = sum(m.get(k, 0) for k in ["MISSING_TRANSACTION_ID", "MISSING_CURRENCY", "MISSING_BUSINESS_UNIT"])
        print(f"    {'MISSING_FIELDS (total)':<24}{100 * fields / base:5.2f}%")

    passed, total = sum(results), len(results)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{total} checks passed")
    if passed == total:
        print("Phase 1 is complete. The database is populated and consistent.")
    else:
        print("Some checks failed - see FAIL lines above.")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
