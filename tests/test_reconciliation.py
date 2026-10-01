"""Unit tests for the reconciliation engine - no database needed."""
from types import SimpleNamespace

import numpy as np
import pandas as pd

from backend.services import reconciliation_service as rs
from tests.test_controls import ledger, txn

CFG = SimpleNamespace(amount_tolerance=0.01, recon_date_tolerance_days=4)


def status_of(out, tid):
    return out.loc[out["transaction_id"] == tid, "status"].tolist()


def one(t_rows, l_rows):
    """Reconcile and return the single result row (for one-transaction scenarios)."""
    out = rs.reconcile(txn(t_rows), ledger(l_rows), CFG)
    assert len(out) == 1, out
    return out.iloc[0]


# ---------------------------------------------------------------- the happy path
def test_identical_records_match_with_zero_exposure():
    r = one([{}], [{}])
    assert r["status"] == "MATCHED"
    assert r["difference"] == 0 and r["exposure_inr"] == 0 and r["reason"] is None


def test_amounts_within_tolerance_still_match():
    assert one([{"amount": 100.0}], [{"amount": 100.004}])["status"] == "MATCHED"


def test_identical_zero_amount_in_both_systems_is_matched_not_judged():
    """Reconciliation checks agreement between systems; validity of the value is control CTL-003's job."""
    r = one([{"amount": 0.0, "amount_inr": 0.0}], [{"amount": 0.0, "amount_inr": 0.0}])
    assert r["status"] == "MATCHED"


# ---------------------------------------------------------------- mismatches
def test_amount_mismatch_reports_signed_difference_and_inr_exposure():
    r = one([{"amount": 100.0, "amount_inr": 100.0}], [{"amount": 70.0, "amount_inr": 70.0}])
    assert r["status"] == "AMOUNT_MISMATCH"
    assert r["difference"] == -30.0                    # ledger - source
    assert r["exposure_inr"] == 30.0
    assert "amount: source 100.00 vs ledger 70.00" in r["reason"]


def test_currency_mismatch_beats_amount_mismatch_and_has_no_difference():
    r = one([{"currency": "USD", "amount": 10.0, "amount_inr": 800.0}],
            [{"currency": "EUR", "amount": 12.0, "amount_inr": 1080.0}])
    assert r["status"] == "CURRENCY_MISMATCH"
    assert pd.isna(r["difference"])                    # different units: a raw difference would be meaningless
    assert r["exposure_inr"] == 280.0                  # |1080 - 800| in INR


def test_business_unit_mismatch():
    r = one([{"business_unit_id": 1}], [{"business_unit_id": 2}])
    assert r["status"] == "BUSINESS_UNIT_MISMATCH"
    assert r["exposure_inr"] == 100.0                  # value at stake, not a difference
    assert "business unit: source 1 vs ledger 2" in r["reason"]


def test_date_tolerance_boundaries():
    d = pd.Timestamp("2026-01-05 10:00")
    at = lambda days: one([{"transaction_date": d}], [{"ledger_date": d.normalize() + pd.Timedelta(days=days)}])["status"]  # noqa: E731
    assert at(0) == "MATCHED" and at(4) == "MATCHED"   # 4 = allowed maximum
    assert at(5) == "DATE_MISMATCH"                    # too late
    assert at(-1) == "DATE_MISMATCH"                   # posted BEFORE the transaction


def test_date_tolerance_is_configurable():
    d = pd.Timestamp("2026-01-05 10:00")
    strict = SimpleNamespace(amount_tolerance=0.01, recon_date_tolerance_days=1)
    out = rs.reconcile(txn([{"transaction_date": d}]), ledger([{"ledger_date": d.normalize() + pd.Timedelta(days=3)}]), strict)
    assert out.iloc[0]["status"] == "DATE_MISMATCH"


def test_precedence_and_all_problems_are_listed_in_reason():
    r = one([{"amount": 100.0, "business_unit_id": 1, "transaction_date": pd.Timestamp("2026-01-05")}],
            [{"amount": 90.0, "business_unit_id": 2, "ledger_date": pd.Timestamp("2026-01-20")}])
    assert r["status"] == "AMOUNT_MISMATCH"            # AMOUNT outranks BUSINESS_UNIT and DATE
    for fragment in ("amount:", "business unit:", "date:"):
        assert fragment in r["reason"]


# ---------------------------------------------------------------- missing records
def test_missing_on_either_side():
    t = txn([{"transaction_id": "ONLY_SRC", "amount": 50.0, "amount_inr": 50.0}])
    l = ledger([{"transaction_id": "ONLY_LED", "amount": 70.0, "amount_inr": 70.0}])
    out = rs.reconcile(t, l, CFG).set_index("transaction_id")
    assert out.loc["ONLY_SRC", "status"] == "MISSING_IN_LEDGER" and out.loc["ONLY_SRC", "exposure_inr"] == 50.0
    assert out.loc["ONLY_LED", "status"] == "MISSING_IN_SOURCE" and out.loc["ONLY_LED", "exposure_inr"] == 70.0
    assert pd.isna(out.loc["ONLY_SRC", "ledger_row_id"]) and pd.isna(out.loc["ONLY_LED", "source_row_id"])


# ---------------------------------------------------------------- duplicates
def test_source_duplicates_only_first_copy_is_paired():
    t = txn([{"created_at": pd.Timestamp("2026-01-05 10:01")},
             {"created_at": pd.Timestamp("2026-01-06 10:01")},
             {"created_at": pd.Timestamp("2026-01-07 10:01")}])
    out = rs.reconcile(t, ledger([{}]), CFG)
    assert len(out) == 3
    assert sorted(out["status"]) == ["DUPLICATE", "DUPLICATE", "MATCHED"]
    matched = out[out["status"] == "MATCHED"].iloc[0]
    assert matched["source_row_id"] == 1               # the OLDEST copy owns the ledger entry
    assert all("of 3" in r for r in out.loc[out["status"] == "DUPLICATE", "reason"])


def test_duplicate_ledger_entry():
    out = rs.reconcile(txn([{}]), ledger([{}, {}]), CFG)
    assert sorted(out["status"]) == ["DUPLICATE", "MATCHED"]
    dup = out[out["status"] == "DUPLICATE"].iloc[0]
    assert pd.isna(dup["source_row_id"]) and dup["ledger_row_id"] == 2


# ---------------------------------------------------------------- invalid
def test_missing_id_is_invalid_and_its_ledger_entry_is_missing_in_source():
    out = rs.reconcile(txn([{"transaction_id": None}]), ledger([{"transaction_id": "T1"}]), CFG)
    assert sorted(out["status"]) == ["INVALID", "MISSING_IN_SOURCE"]


def test_missing_currency_or_business_unit_is_invalid_even_when_amounts_differ():
    t = txn([{"transaction_id": "A", "currency": None, "amount": 1.0},
             {"transaction_id": "B", "business_unit_id": pd.NA, "amount": 1.0},
             {"transaction_id": "C"}])
    l = ledger([{"transaction_id": t_id, "amount": 999.0} for t_id in ["A", "B", "C"]])
    out = rs.reconcile(t, l, CFG).set_index("transaction_id")
    assert out.loc["A", "status"] == "INVALID" and out.loc["B", "status"] == "INVALID"
    assert out.loc["C", "status"] == "AMOUNT_MISMATCH"   # a normal pair is still evaluated
    assert pd.isna(out.loc["A", "difference"])           # currency unknown -> no difference
    assert out.loc["B", "difference"] == 998.0           # currency known -> difference is meaningful
    assert "currency is missing" in out.loc["A", "reason"]
    assert out.loc["B", "business_unit_id"] == 1         # filled from the ledger side


# ---------------------------------------------------------------- invariants
def test_every_source_and_ledger_row_is_reconciled_exactly_once():
    t = txn([{"transaction_id": "OK"}, {"transaction_id": "DUP"}, {"transaction_id": "DUP"},
             {"transaction_id": None}, {"transaction_id": "NOLED"}, {"transaction_id": "MIS"}])
    l = ledger([{"transaction_id": "OK"}, {"transaction_id": "DUP"}, {"transaction_id": "MIS", "amount": 5.0},
                {"transaction_id": "ORPHAN"}, {"transaction_id": "GHOST"}])
    out = rs.reconcile(t, l, CFG)
    src_ids, led_ids = out["source_row_id"].dropna(), out["ledger_row_id"].dropna()
    assert sorted(src_ids) == list(t["id"]) and src_ids.is_unique
    assert sorted(led_ids) == list(l["id"]) and led_ids.is_unique
    assert list(out.columns) == rs.OUT_COLUMNS
    assert set(out["status"]) <= set(rs.STATUSES)


# ---------------------------------------------------------------- metrics
def test_summarise_metrics_and_match_rate():
    recon = pd.DataFrame({
        "status": ["MATCHED"] * 90 + ["AMOUNT_MISMATCH"] * 3 + ["DATE_MISMATCH"] * 2 + ["MISSING_IN_LEDGER"] * 2
                  + ["MISSING_IN_SOURCE"] + ["DUPLICATE"] * 1 + ["INVALID"],
        "exposure_inr": [0.0] * 90 + [10.0] * 10})
    m = rs.summarise(recon)
    assert (m["total_records"], m["matched"], m["mismatched"], m["missing"], m["duplicates"], m["invalid"]) == \
           (100, 90, 5, 3, 1, 1)
    assert m["match_rate"] == 90.0
    assert m["unreconciled_exposure_inr"] == 100.0


def test_summarise_handles_empty_input():
    m = rs.summarise(pd.DataFrame({"status": pd.Series([], dtype=object), "exposure_inr": pd.Series([], dtype=float)}))
    assert m["total_records"] == 0 and m["match_rate"] == 0.0
