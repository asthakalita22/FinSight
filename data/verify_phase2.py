"""
FinSight - Phase 2 verification
===============================
  A. ETL          amount_inr is correct, FX loaded, nothing silently dropped
  B. Controls     every injected problem produced exactly the right exception (matched ID by ID)
                  and nothing else did (no false positives)
  C. Integrity    workflow fields are consistent
  D. Idempotency  re-running controls adds nothing and never overwrites an analyst's edits

Usage:
    python data/verify_phase2.py
"""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.database import engine  # noqa: E402
from backend.services.controls_service import RULES, run_controls  # noqa: E402

MANIFEST = ROOT / "data" / "raw" / "injection_manifest.csv"
REJECTED = ROOT / "data" / "processed" / "rejected_records.csv"
results = []


def scalar(sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main():
    print("\nA. ETL")
    check("fx_rates loaded", scalar("SELECT COUNT(*) FROM fx_rates") == 5)
    n = scalar("SELECT COUNT(*) FROM transactions WHERE currency IS NOT NULL AND amount_inr IS NULL")
    check("Every transaction with a currency has amount_inr", n == 0, f"{n} missing")
    n = scalar("""SELECT COUNT(*) FROM transactions t JOIN fx_rates f ON f.currency = t.currency
                  WHERE ABS(t.amount_inr - ROUND(t.amount * f.rate_to_inr, 2)) > 0.01""")
    check("amount_inr = amount x FX rate (transactions)", n == 0, f"{n} wrong")
    n = scalar("""SELECT COUNT(*) FROM ledger_entries l JOIN fx_rates f ON f.currency = l.currency
                  WHERE ABS(l.amount_inr - ROUND(l.amount * f.rate_to_inr, 2)) > 0.01""")
    check("amount_inr = amount x FX rate (ledger)", n == 0, f"{n} wrong")
    check("No structurally invalid rows were quarantined", not REJECTED.exists(),
          "data/processed/rejected_records.csv exists" if REJECTED.exists() else "")

    print("\nB. CONTROLS vs generator answer key (matched by transaction ID)")
    if not MANIFEST.exists():
        check("injection_manifest.csv found", False, "run: python automation/pipeline.py --generate")
        return finish()
    man = pd.read_csv(MANIFEST)
    ids = lambda issue: set(man.loc[man.issue_type == issue, "transaction_id"])  # noqa: E731
    with engine.connect() as conn:
        exc = pd.read_sql(text("SELECT exception_code, transaction_id, business_unit_id, severity, status, "
                               "owner, amount, created_at, resolved_at FROM exceptions"), conn)
    found = lambda code: set(exc.loc[exc.exception_code == code, "transaction_id"].dropna())  # noqa: E731
    count = lambda code: int((exc.exception_code == code).sum())  # noqa: E731

    expected_sets = {
        "CTL-002": ids("DUPLICATE"),
        "CTL-003": ids("INVALID_AMOUNT"),
        "CTL-004": ids("MISSING_CURRENCY"),
        "CTL-005": ids("MISSING_BUSINESS_UNIT"),
        "CTL-006A": ids("AMOUNT_MISMATCH"),
        "CTL-006B": ids("MISSING_IN_LEDGER"),
        "CTL-006C": ids("ORPHAN_LEDGER_ENTRY") | ids("MISSING_TRANSACTION_ID"),
    }
    for code, expected in expected_sets.items():
        got = found(code)
        check(f"{code} {RULES[code][0]:<24} expected {len(expected):>5,} | found {len(got):>5,}",
              got == expected and count(code) == len(expected),
              "" if got == expected else f"missing {len(expected - got)}, unexpected {len(got - expected)}")

    n_expected = len(ids("MISSING_TRANSACTION_ID"))
    check(f"CTL-001 {RULES['CTL-001'][0]:<24} expected {n_expected:>5,} | found {count('CTL-001'):>5,}",
          count("CTL-001") == n_expected and found("CTL-001") == set())

    breaches = scalar(f"SELECT COUNT(*) FROM v_financial_performance "
                      f"WHERE ABS(variance_pct) > {settings.variance_threshold_pct}")
    check(f"CTL-007 {RULES['CTL-007'][0]:<24} expected {breaches:>5,} | found {count('CTL-007'):>5,}",
          count("CTL-007") == breaches, f"threshold ±{settings.variance_threshold_pct:g}%")

    expected_total = sum(len(s) for s in expected_sets.values()) + n_expected + breaches
    check("No false positives: total exceptions == injected problems", len(exc) == expected_total,
          f"{len(exc):,} vs {expected_total:,}")

    unusual = ids("UNUSUAL_TRANSACTION")
    hit = unusual & set(exc.transaction_id.dropna())
    check("Unusual transactions raise NO control exception (they belong to the ML model, Phase 5)",
          len(hit) == 0, f"{len(hit)} of {len(unusual)} wrongly flagged")

    print("\nC. INTEGRITY")
    check("Every exception has an owner", exc.owner.notna().all())
    closed = exc.status.isin(["RESOLVED", "IGNORED"])
    check("Closed exceptions have resolved_at >= created_at",
          bool(exc.loc[closed, "resolved_at"].notna().all() and
               (exc.loc[closed, "resolved_at"] >= exc.loc[closed, "created_at"]).all()))
    check("Open / in-review exceptions have no resolved_at", bool(exc.loc[~closed, "resolved_at"].isna().all()))
    check("Exposure amounts are never negative", bool((exc.amount >= 0).all()))
    sev = exc.severity.value_counts()
    check("All four severity levels are used", set(sev.index) == {"LOW", "MEDIUM", "HIGH", "CRITICAL"},
          ", ".join(f"{k} {sev.get(k, 0):,}" for k in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]))
    st = exc.status.value_counts()
    print("       status mix: " + ", ".join(f"{k} {v:,}" for k, v in st.items()))
    print(f"       total exposure: INR {exc.amount.sum() / 1e6:,.1f} M across {len(exc):,} exceptions")

    print("\nD. IDEMPOTENCY (re-running controls)")
    before = scalar("SELECT COUNT(*) FROM exceptions")
    with engine.begin() as conn:  # simulate an analyst picking up an exception
        row = conn.execute(text("SELECT id, status, owner FROM exceptions WHERE status = 'OPEN' LIMIT 1")).one()
        conn.execute(text("UPDATE exceptions SET status = 'IN_REVIEW', owner = 'Test Analyst' WHERE id = :i"),
                     {"i": row.id})
    result = run_controls(engine, verbose=False)
    after = scalar("SELECT COUNT(*) FROM exceptions")
    check("Second run inserts nothing new", result.inserted == 0 and after == before,
          f"inserted {result.inserted}, rows {before:,} -> {after:,}")
    kept = scalar(f"SELECT COUNT(*) FROM exceptions WHERE id = {row.id} "
                  f"AND status = 'IN_REVIEW' AND owner = 'Test Analyst'")
    check("Analyst's status / owner change was NOT overwritten", kept == 1)
    with engine.begin() as conn:  # put the demo data back the way it was
        conn.execute(text("UPDATE exceptions SET status = :s, owner = :o WHERE id = :i"),
                     {"s": row.status, "o": row.owner, "i": row.id})

    finish()


def finish():
    passed, total = sum(results), len(results)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{total} checks passed")
    print("Phase 2 is complete. Controls detect every injected problem with zero false positives."
          if passed == total else "Some checks failed - see FAIL lines above.")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
