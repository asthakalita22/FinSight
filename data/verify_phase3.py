"""
FinSight - Phase 3 verification (reconciliation engine)
=======================================================
  A. Accounting     every source row and every ledger row is reconciled exactly once
  B. Answer key     each status matches the generator's injection manifest, ID by ID
  C. Cross-check    the reconciliation engine agrees with the (independent) control engine
  D. Metrics        views and figures are internally consistent
  E. Planted breaks known breaks injected into the real data (in memory) are all detected
  F. Idempotency    re-running gives the same result

Usage:
    python data/verify_phase3.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import engine  # noqa: E402
from backend.services import reconciliation_service as rs  # noqa: E402
from backend.utils.data_access import load_ledger, load_transactions  # noqa: E402

MANIFEST = ROOT / "data" / "raw" / "injection_manifest.csv"
results = []


def scalar(sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def main():
    with engine.connect() as conn:
        rec = pd.read_sql(text("""SELECT id, transaction_id, source_row_id, ledger_row_id, source_amount::float8 AS source_amount,
                                         ledger_amount::float8 AS ledger_amount, difference::float8 AS difference,
                                         status, exposure_inr::float8 AS exposure_inr, reason, business_unit_id
                                  FROM reconciliations"""), conn)
        exc = pd.read_sql(text("SELECT exception_code, transaction_id, amount::float8 AS amount FROM exceptions"), conn)
    n_src = scalar("SELECT COUNT(*) FROM transactions")
    n_led = scalar("SELECT COUNT(*) FROM ledger_entries")

    # ------------------------------------------------------------------ A
    print("\nA. ACCOUNTING")
    check("Reconciliation table is populated", len(rec) > 0, f"{len(rec):,} rows")
    src_ids, led_ids = rec.source_row_id.dropna(), rec.ledger_row_id.dropna()
    check("Every source row is reconciled exactly once", len(src_ids) == n_src and src_ids.is_unique,
          f"{len(src_ids):,} of {n_src:,} source rows")
    check("Every ledger row is reconciled exactly once", len(led_ids) == n_led and led_ids.is_unique,
          f"{len(led_ids):,} of {n_led:,} ledger rows")
    paired = int((rec.source_row_id.notna() & rec.ledger_row_id.notna()).sum())
    check("Total records = source rows + ledger rows - paired rows", len(rec) == n_src + n_led - paired,
          f"{len(rec):,} = {n_src:,} + {n_led:,} - {paired:,}")

    # ------------------------------------------------------------------ B
    print("\nB. STATUSES vs generator answer key (matched by transaction ID)")
    if not MANIFEST.exists():
        check("injection_manifest.csv found", False, "run: python automation/pipeline.py --generate")
        return finish()
    man = pd.read_csv(MANIFEST)
    ids = lambda issue: set(man.loc[man.issue_type == issue, "transaction_id"])  # noqa: E731
    of = lambda status: set(rec.loc[rec.status == status, "transaction_id"].dropna())  # noqa: E731
    cnt = lambda status: int((rec.status == status).sum())  # noqa: E731

    expected = {
        "DUPLICATE": ids("DUPLICATE"),
        "MISSING_IN_LEDGER": ids("MISSING_IN_LEDGER"),
        "AMOUNT_MISMATCH": ids("AMOUNT_MISMATCH"),
        "MISSING_IN_SOURCE": ids("ORPHAN_LEDGER_ENTRY") | ids("MISSING_TRANSACTION_ID"),
    }
    for status, want in expected.items():
        got = of(status)
        check(f"{status:<18} expected {len(want):>5,} | found {cnt(status):>5,}", got == want and cnt(status) == len(want),
              "" if got == want else f"missing {len(want - got)}, unexpected {len(got - want)}")

    want_invalid_ids = ids("MISSING_CURRENCY") | ids("MISSING_BUSINESS_UNIT")
    n_null_id = int(((rec.status == "INVALID") & rec.transaction_id.isna()).sum())
    want_invalid = len(want_invalid_ids) + len(ids("MISSING_TRANSACTION_ID"))
    check(f"{'INVALID':<18} expected {want_invalid:>5,} | found {cnt('INVALID'):>5,}",
          of("INVALID") == want_invalid_ids and n_null_id == len(ids("MISSING_TRANSACTION_ID")) and cnt("INVALID") == want_invalid,
          "missing id + missing currency + missing business unit")
    other = sum(cnt(s) for s in ["CURRENCY_MISMATCH", "BUSINESS_UNIT_MISMATCH", "DATE_MISMATCH"])
    check("No currency / business-unit / date mismatches in the generated data (none were injected)", other == 0,
          f"{other} found")
    matched = set(rec.loc[rec.status == "MATCHED", "transaction_id"])
    check("Unusual and invalid-amount transactions are MATCHED (both systems agree; other engines judge them)",
          (ids("UNUSUAL_TRANSACTION") | ids("INVALID_AMOUNT")) <= matched)
    problems = sum(cnt(s) for s in rs.STATUSES if s != "MATCHED")
    check("Everything else is MATCHED: MATCHED = total - injected problems", cnt("MATCHED") == len(rec) - problems,
          f"{cnt('MATCHED'):,} matched")

    # ------------------------------------------------------------------ C
    print("\nC. CROSS-CHECK vs the control engine (two independent implementations must agree)")
    pairs = [("AMOUNT_MISMATCH", ["CTL-006A"], 50.0), ("MISSING_IN_LEDGER", ["CTL-006B"], 1.0),
             ("MISSING_IN_SOURCE", ["CTL-006C"], 1.0), ("DUPLICATE", ["CTL-002"], 1.0)]
    for status, codes, tol in pairs:
        e = exc[exc.exception_code.isin(codes)]
        r = rec[rec.status == status]
        same_set = set(e.transaction_id.dropna()) == set(r.transaction_id.dropna())
        exp_diff = abs(e.amount.sum() - r.exposure_inr.sum())
        check(f"{status:<18} == {codes[0]}: {len(r):>5,} records, same IDs, exposure within INR {tol:g}",
              len(e) == len(r) and same_set and exp_diff <= tol, f"exposure gap INR {exp_diff:,.2f}")
    e_inv = int(exc.exception_code.isin(["CTL-001", "CTL-004", "CTL-005"]).sum())
    check(f"{'INVALID':<18} == CTL-001 + CTL-004 + CTL-005: {cnt('INVALID'):>5,} records", e_inv == cnt("INVALID"))

    # ------------------------------------------------------------------ D
    print("\nD. METRICS")
    view = pd.read_sql(text("SELECT * FROM v_reconciliation_summary"), engine).iloc[0]
    py = rs.summarise(rec)
    check("v_reconciliation_summary agrees with the reconciliations table",
          all(int(view[k]) == py[k] for k in ["total_records", "matched", "mismatched", "missing", "duplicates", "invalid"])
          and abs(float(view["match_rate"]) - py["match_rate"]) < 0.005,
          f"match rate {float(view['match_rate']):.2f}%")
    expected_rate = round(100 * (len(rec) - problems) / len(rec), 2)
    check("Match rate = Matched / Total x 100", abs(float(view["match_rate"]) - expected_rate) < 0.005, f"{expected_rate}%")
    by_bu = pd.read_sql(text("SELECT * FROM v_reconciliation_by_bu"), engine)
    check("v_reconciliation_by_bu adds up to the total", int(by_bu.total_records.sum()) == len(rec),
          f"{len(by_bu)} business units")
    daily = pd.read_sql(text("SELECT * FROM v_reconciliation_daily"), engine)
    check("v_reconciliation_daily adds up to the total", int(daily.total_records.sum()) == len(rec), f"{len(daily)} days")
    am = rec[rec.status == "AMOUNT_MISMATCH"]
    check("AMOUNT_MISMATCH: difference = ledger - source, and is never zero",
          bool(((am.ledger_amount - am.source_amount - am.difference).abs() < 0.011).all() and (am.difference.abs() > 0).all()))
    check("MATCHED rows carry zero exposure and no reason",
          bool((rec.loc[rec.status == "MATCHED", "exposure_inr"] == 0).all() and rec.loc[rec.status == "MATCHED", "reason"].isna().all()))
    check("Every break has a human-readable reason",
          bool(rec.loc[rec.status != "MATCHED", "reason"].fillna("").str.len().gt(5).all()))
    print(f"       total unreconciled exposure: INR {py['unreconciled_exposure_inr'] / 1e6:,.1f} M")

    # ------------------------------------------------------------------ E
    print("\nE. PLANTED BREAKS: inject known breaks into the real data (in memory) and confirm detection")
    txn, led = load_transactions(engine), load_ledger(engine)
    base = rs.reconcile(txn, led)
    clean_ids = base[(base.status == "MATCHED") & base.source_row_id.notna() & base.ledger_row_id.notna()]["transaction_id"]
    unique_src = txn.transaction_id.value_counts()
    unique_led = led.transaction_id.value_counts()
    pool = [t for t in clean_ids if unique_src.get(t) == 1 and unique_led.get(t) == 1]
    rng = np.random.default_rng(123)
    pick = list(rng.choice(pool, size=250, replace=False))
    groups = dict(zip(["amount", "currency", "bu", "late", "early", "no_ledger", "no_source", "dup_ledger", "dup_source",
                       "no_ccy"], [pick[i * 25:(i + 1) * 25] for i in range(10)]))
    t2, l2 = txn.copy(), led.copy()
    l2.loc[l2.transaction_id.isin(groups["amount"]), "amount"] += 7.5
    l2.loc[l2.transaction_id.isin(groups["currency"]), "currency"] = "CHF"
    m = l2.transaction_id.isin(groups["bu"])
    l2.loc[m, "business_unit_id"] = (l2.loc[m, "business_unit_id"].astype(int) % 8 + 1).astype("Int64")
    l2.loc[l2.transaction_id.isin(groups["late"]), "ledger_date"] += pd.Timedelta(days=10)
    tdate = t2.dropna(subset=["transaction_id"]).drop_duplicates("transaction_id").set_index("transaction_id")["transaction_date"].dt.normalize()
    m = l2.transaction_id.isin(groups["early"])
    l2.loc[m, "ledger_date"] = l2.loc[m, "transaction_id"].map(tdate) - pd.Timedelta(days=2)
    l2 = l2[~l2.transaction_id.isin(groups["no_ledger"])]
    t2 = t2[~t2.transaction_id.isin(groups["no_source"])]
    dl = l2[l2.transaction_id.isin(groups["dup_ledger"])].copy()
    dl["id"] = np.arange(l2["id"].max() + 1, l2["id"].max() + 1 + len(dl))
    l2 = pd.concat([l2, dl], ignore_index=True)
    ds = t2[t2.transaction_id.isin(groups["dup_source"])].copy()
    ds["id"] = np.arange(t2["id"].max() + 1, t2["id"].max() + 1 + len(ds))
    ds["created_at"] += pd.Timedelta(days=1)
    t2 = pd.concat([t2, ds], ignore_index=True)
    t2.loc[t2.transaction_id.isin(groups["no_ccy"]), "currency"] = None

    new = rs.reconcile(t2, l2)
    statuses_of = lambda tid: sorted(new.loc[new.transaction_id == tid, "status"])  # noqa: E731
    expect = {"amount": ["AMOUNT_MISMATCH"], "currency": ["CURRENCY_MISMATCH"], "bu": ["BUSINESS_UNIT_MISMATCH"],
              "late": ["DATE_MISMATCH"], "early": ["DATE_MISMATCH"], "no_ledger": ["MISSING_IN_LEDGER"],
              "no_source": ["MISSING_IN_SOURCE"], "dup_ledger": ["DUPLICATE", "MATCHED"],
              "dup_source": ["DUPLICATE", "MATCHED"], "no_ccy": ["INVALID"]}
    labels = {"amount": "amount +7.50 on the ledger", "currency": "ledger currency changed to CHF",
              "bu": "ledger business unit changed", "late": "ledger posted 10 days late (tolerance 4)",
              "early": "ledger posted 2 days BEFORE the transaction", "no_ledger": "ledger entry deleted",
              "no_source": "source transaction deleted", "dup_ledger": "ledger entry duplicated",
              "dup_source": "source transaction duplicated", "no_ccy": "source currency blanked"}
    for key, tids in groups.items():
        ok = all(statuses_of(t) == expect[key] for t in tids)
        check(f"{len(tids)} planted: {labels[key]:<44} -> {'+'.join(expect[key])}", ok)
    planted = set(pick)
    before = base[~base.transaction_id.isin(planted)].status.value_counts().sort_index()
    after = new[~new.transaction_id.isin(planted)].status.value_counts().sort_index()
    check("All other records are completely unaffected (no collateral damage)", before.equals(after))
    check("Planted data still satisfies exactly-once accounting",
          new.source_row_id.dropna().is_unique and new.ledger_row_id.dropna().is_unique
          and len(new.source_row_id.dropna()) == len(t2) and len(new.ledger_row_id.dropna()) == len(l2))

    # ------------------------------------------------------------------ F
    print("\nF. IDEMPOTENCY")
    before = scalar("SELECT COUNT(*) FROM reconciliations")
    status_before = rec.status.value_counts().sort_index()
    res = rs.run_reconciliation(engine, verbose=False)
    after = scalar("SELECT COUNT(*) FROM reconciliations")
    status_after = pd.read_sql(text("SELECT status FROM reconciliations"), engine).status.value_counts().sort_index()
    check("Second run gives identical row count and status mix", before == after == res.rows_written and status_before.equals(status_after),
          f"{before:,} rows")
    check("Row ids restart at 1 (table is replaced, not appended to)", scalar("SELECT MIN(id) FROM reconciliations") == 1)

    finish()


def finish():
    passed, total = sum(results), len(results)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{total} checks passed")
    print("Phase 3 is complete. The reconciliation engine is exact, exhaustive and agrees with the control engine."
          if passed == total else "Some checks failed - see FAIL lines above.")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
