"""Unit tests for the control rules - no database needed."""
from types import SimpleNamespace

import numpy as np
import pandas as pd

from backend.services import controls_service as cs

CFG = SimpleNamespace(high_value_inr=100.0, critical_value_inr=250.0,
                      variance_threshold_pct=15.0, amount_tolerance=0.01, seed=1)
RATES = {"INR": 1.0, "USD": 80.0}


def txn(rows):
    base = dict(transaction_id="T1", transaction_date=pd.Timestamp("2026-01-05 10:00"), business_unit_id=1,
                product="Equity", currency="INR", amount=100.0, amount_inr=100.0,
                transaction_type="REVENUE", created_at=pd.Timestamp("2026-01-05 10:01"))
    df = pd.DataFrame([{**base, **r} for r in rows])
    df.insert(0, "id", range(1, len(df) + 1))
    df["business_unit_id"] = df["business_unit_id"].astype("Int64")
    return df


def ledger(rows):
    base = dict(transaction_id="T1", ledger_date=pd.Timestamp("2026-01-06"), amount=100.0,
                amount_inr=100.0, currency="INR", business_unit_id=1)
    df = pd.DataFrame([{**base, **r} for r in rows])
    df.insert(0, "id", range(1, len(df) + 1))
    df["business_unit_id"] = df["business_unit_id"].astype("Int64")
    return df


def with_exposure(t, l=None):
    return cs.add_exposure(t, l if l is not None else ledger([{}]), RATES)


# ---------------------------------------------------------------- severity
def test_severity_bumps_with_exposure():
    base = pd.Series(["LOW", "MEDIUM", "HIGH", "HIGH"])
    exposure = pd.Series([50, 100, 250, 1e9])
    # 50 -> no bump | 100 -> +1 | 250 -> +2 | huge -> capped at CRITICAL
    assert list(cs.assign_severity(base, exposure, CFG)) == ["LOW", "HIGH", "CRITICAL", "CRITICAL"]


def test_severity_ignores_missing_exposure():
    out = cs.assign_severity(pd.Series(["MEDIUM"]), pd.Series([np.nan]), CFG)
    assert out.iloc[0] == "MEDIUM"


# ---------------------------------------------------------------- rules 1-5
def test_missing_transaction_id():
    t = with_exposure(txn([{"transaction_id": None}, {"transaction_id": "T2"}]))
    out = cs.rule_missing_transaction_id(t, CFG)
    assert len(out) == 1
    assert out.iloc[0]["exception_code"] == "CTL-001"
    assert out.iloc[0]["source_ref"] == "TXN:1"
    assert pd.isna(out.iloc[0]["transaction_id"])


def test_duplicates_flag_every_extra_copy_but_not_the_first():
    t = with_exposure(txn([
        {"transaction_id": "T1", "created_at": pd.Timestamp("2026-01-05 10:01")},
        {"transaction_id": "T1", "created_at": pd.Timestamp("2026-01-06 10:01")},
        {"transaction_id": "T1", "created_at": pd.Timestamp("2026-01-07 10:01")},
        {"transaction_id": "T2"},
    ]))
    out = cs.rule_duplicate_transaction(t, CFG)
    assert sorted(out["source_ref"]) == ["TXN:2", "TXN:3"]       # copies #2 and #3, not the original
    assert set(out["transaction_id"]) == {"T1"}


def test_invalid_amount_respects_transaction_type():
    t = with_exposure(txn([
        {"transaction_id": "A", "transaction_type": "REVENUE", "amount": 0.0},
        {"transaction_id": "B", "transaction_type": "COST", "amount": -5.0},
        {"transaction_id": "C", "transaction_type": "ADJUSTMENT", "amount": -5.0},   # legitimately negative
        {"transaction_id": "D", "transaction_type": "EXPENSE", "amount": 10.0},
    ]))
    assert set(cs.rule_invalid_amount(t, CFG)["transaction_id"]) == {"A", "B"}


def test_missing_currency_uses_ledger_currency_for_exposure():
    t = txn([{"transaction_id": "T1", "currency": None, "amount": 2.0, "amount_inr": np.nan}])
    l = ledger([{"transaction_id": "T1", "currency": "USD", "amount": 2.0, "amount_inr": 160.0}])
    out = cs.rule_missing_currency(cs.add_exposure(t, l, RATES), CFG)
    assert len(out) == 1
    assert out.iloc[0]["amount"] == 160.0                         # 2 USD x 80


def test_missing_business_unit():
    t = with_exposure(txn([{"business_unit_id": pd.NA}, {"transaction_id": "T2"}]))
    out = cs.rule_missing_business_unit(t, CFG)
    assert len(out) == 1 and pd.isna(out.iloc[0]["business_unit_id"])


# ---------------------------------------------------------------- rule 6
def test_ledger_rule_finds_all_three_break_types_and_ignores_matches():
    t = with_exposure(txn([
        {"transaction_id": "MATCH"},
        {"transaction_id": "MATCH", "created_at": pd.Timestamp("2026-01-09")},     # duplicate copy: not a mismatch
        {"transaction_id": "DIFF", "amount": 100.0},
        {"transaction_id": "NOLEDGER"},
        {"transaction_id": "TOL", "amount": 100.0},
    ]))
    l = ledger([
        {"transaction_id": "MATCH"},
        {"transaction_id": "DIFF", "amount": 130.0, "amount_inr": 130.0},
        {"transaction_id": "TOL", "amount": 100.004},                              # within 0.01 tolerance
        {"transaction_id": "ORPHAN"},
    ])
    out = cs.rule_ledger_mismatch(t, l, RATES, CFG)
    assert list(out["CTL-006A"]["transaction_id"]) == ["DIFF"]
    assert list(out["CTL-006B"]["transaction_id"]) == ["NOLEDGER"]
    assert list(out["CTL-006C"]["transaction_id"]) == ["ORPHAN"]
    assert out["CTL-006A"].iloc[0]["amount"] == 30.0                              # exposure = |difference|


def test_mismatch_exposure_is_converted_to_inr():
    t = with_exposure(txn([{"transaction_id": "X", "currency": "USD", "amount": 10.0, "amount_inr": 800.0}]))
    l = ledger([{"transaction_id": "X", "currency": "USD", "amount": 12.0, "amount_inr": 960.0}])
    out = cs.rule_ledger_mismatch(t, l, RATES, CFG)["CTL-006A"]
    assert out.iloc[0]["amount"] == 160.0                                          # 2 USD x 80


def test_null_id_source_rows_never_join_the_ledger():
    t = with_exposure(txn([{"transaction_id": None}]))
    l = ledger([{"transaction_id": "T9"}])
    out = cs.rule_ledger_mismatch(t, l, RATES, CFG)
    assert len(out["CTL-006B"]) == 0                # a NULL id is CTL-001's job
    assert list(out["CTL-006C"]["transaction_id"]) == ["T9"]


# ---------------------------------------------------------------- rule 7
def test_variance_threshold_and_severity():
    v = pd.DataFrame({
        "month": pd.to_datetime(["2026-01-01"] * 5), "business_unit_id": [1, 2, 3, 4, 5],
        "business_unit": list("ABCDE"), "budget": [100.0, 100.0, 100.0, 0.0, 100.0],
        "actual": [110.0, 120.0, 135.0, 50.0, 80.0],
    })
    out = cs.rule_excessive_variance(v, CFG).set_index("source_ref")
    assert set(out.index) == {"BUM:2:2026-01", "BUM:3:2026-01", "BUM:5:2026-01"}   # +10% ok, budget 0 skipped
    assert out.loc["BUM:2:2026-01", "severity"] == "HIGH"                         # +20% (>15)
    assert out.loc["BUM:3:2026-01", "severity"] == "CRITICAL"                     # +35% (>=2x threshold)
    assert out.loc["BUM:5:2026-01", "severity"] == "HIGH"                         # -20%


# ---------------------------------------------------------------- workflow simulation
def test_workflow_simulation_is_consistent():
    n = 400
    created = pd.Timestamp("2026-01-01 18:00") + pd.to_timedelta(np.arange(n) * 0.35, unit="D")
    exc = pd.DataFrame({"created_at": created, "severity": ["MEDIUM"] * n})
    as_of = pd.Timestamp("2026-06-01")
    out = cs.simulate_workflow(exc, as_of, np.random.default_rng(3))
    assert set(out["status"]) <= {"OPEN", "IN_REVIEW", "RESOLVED", "IGNORED"}
    closed = out["status"].isin(["RESOLVED", "IGNORED"])
    assert (out.loc[closed, "resolved_at"] >= out.loc[closed, "created_at"]).all()
    assert (out.loc[closed, "resolved_at"] <= as_of).all()                        # never closed in the future
    assert out.loc[~closed, "resolved_at"].isna().all()
    fresh = (as_of - out["created_at"]).dt.days < 2
    assert not out.loc[fresh, "status"].isin(["RESOLVED", "IGNORED"]).any()      # too new to be closed


def test_every_rule_has_owner_category_and_valid_base_severity():
    for code, (category, base, owner) in cs.RULES.items():
        assert category and owner and base in cs.LEVELS, code
