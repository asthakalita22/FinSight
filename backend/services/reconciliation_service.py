"""
FinSight reconciliation engine
==============================

Compares the SOURCE system (`transactions`) with the FINANCE LEDGER (`ledger_entries`).

A pair is MATCHED only when all five conditions hold:
    1. transaction_id matches
    2. currency matches
    3. amount matches (within tolerance)
    4. business unit matches
    5. the ledger date is 0..N calendar days after the transaction date (N = RECON_DATE_TOLERANCE_DAYS)

Every source row and every ledger row is reconciled EXACTLY ONCE (enforced by UNIQUE constraints in the
database), and receives exactly one status:

    MATCHED                 all five conditions hold
    AMOUNT_MISMATCH         same id and currency, amounts differ
    CURRENCY_MISMATCH       both currencies present, but different
    BUSINESS_UNIT_MISMATCH  both business units present, but different
    DATE_MISMATCH           ledger posted outside the tolerance window (or before the transaction)
    MISSING_IN_LEDGER       source transaction has no ledger entry
    MISSING_IN_SOURCE       ledger entry has no source transaction
    DUPLICATE               an extra copy of a transaction_id (source side, or ledger side)
    INVALID                 cannot be evaluated: transaction_id, currency or business unit is missing on the source

Precedence when a pair has several problems (all of them are listed in `reason`):
    INVALID > CURRENCY_MISMATCH > AMOUNT_MISMATCH > BUSINESS_UNIT_MISMATCH > DATE_MISMATCH > MATCHED

Design notes
------------
* Reconciliation checks that the two SYSTEMS AGREE. It does not judge whether a value is sensible:
  an amount of 0 that appears identically in both systems is MATCHED (the control engine's CTL-003 flags it).
* Only the FIRST copy of a duplicated transaction_id is paired with the ledger; later copies are DUPLICATE.
* `reconcile()` is a pure function (DataFrames in, DataFrame out) so it is unit-testable without a database.
* `difference` = ledger - source in the transaction currency, only when both sides share that currency.
  `exposure_inr` is the value at stake in INR: |INR difference| for amount/currency breaks, the record's
  INR value for every other break, 0 for MATCHED.
* Each run REPLACES the reconciliations table (the results are fully derived, so a rebuild is safe).
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backend.config import settings
from backend.utils.calculations import data_as_of, match_rate
from backend.utils.data_access import load_ledger, load_transactions

STATUSES = ["MATCHED", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH", "BUSINESS_UNIT_MISMATCH", "DATE_MISMATCH",
            "MISSING_IN_LEDGER", "MISSING_IN_SOURCE", "DUPLICATE", "INVALID"]
MISMATCH_STATUSES = ["AMOUNT_MISMATCH", "CURRENCY_MISMATCH", "BUSINESS_UNIT_MISMATCH", "DATE_MISMATCH"]
MISSING_STATUSES = ["MISSING_IN_LEDGER", "MISSING_IN_SOURCE"]

OUT_COLUMNS = ["source_row_id", "ledger_row_id", "transaction_id", "source_amount", "ledger_amount", "difference",
               "status", "business_unit_id", "currency", "record_date", "exposure_inr", "reason"]


# ----------------------------------------------------------------------------
# Core matching (pure)
# ----------------------------------------------------------------------------
def _standalone(df: pd.DataFrame, side: str, status: str, reasons) -> pd.DataFrame:
    """Build result rows for records that exist on one side only."""
    n = len(df)
    if side == "source":
        exposure = df["amount_inr"].abs().fillna(df["amount"].abs())
        out = pd.DataFrame({
            "source_row_id": df["id"].to_numpy(), "ledger_row_id": pd.array([pd.NA] * n, dtype="Int64"),
            "transaction_id": df["transaction_id"].to_numpy(dtype=object),
            "source_amount": df["amount"].to_numpy(), "ledger_amount": np.nan,
            "business_unit_id": df["business_unit_id"].to_numpy(), "currency": df["currency"].to_numpy(dtype=object),
            "record_date": pd.to_datetime(df["transaction_date"]).dt.normalize().to_numpy()})
    else:
        exposure = df["amount_inr"].abs()
        out = pd.DataFrame({
            "source_row_id": pd.array([pd.NA] * n, dtype="Int64"), "ledger_row_id": df["id"].to_numpy(),
            "transaction_id": df["transaction_id"].to_numpy(dtype=object),
            "source_amount": np.nan, "ledger_amount": df["amount"].to_numpy(),
            "business_unit_id": df["business_unit_id"].to_numpy(), "currency": df["currency"].to_numpy(dtype=object),
            "record_date": pd.to_datetime(df["ledger_date"]).dt.normalize().to_numpy()})
    out["difference"] = np.nan
    out["status"] = status
    out["exposure_inr"] = exposure.to_numpy()
    out["reason"] = list(reasons)
    return out


def reconcile(txn: pd.DataFrame, ledger: pd.DataFrame, cfg=settings) -> pd.DataFrame:
    """Reconcile source transactions against ledger entries. See the module docstring for the rules."""
    txn = txn.copy()
    ledger = ledger.copy()
    ledger["ledger_date"] = pd.to_datetime(ledger["ledger_date"])

    # -- number the copies of each transaction_id (oldest first) -----------------------------------
    txn = txn.sort_values(["created_at", "id"], kind="mergesort")
    txn["copy_no"] = 1
    has_id = txn["transaction_id"].notna()
    txn.loc[has_id, "copy_no"] = txn[has_id].groupby("transaction_id").cumcount() + 1
    txn["copies"] = 1
    txn.loc[has_id, "copies"] = txn[has_id].groupby("transaction_id")["id"].transform("size")
    ledger = ledger.sort_values("id", kind="mergesort")
    ledger["copy_no"] = ledger.groupby("transaction_id").cumcount() + 1

    parts = []

    # -- 1. source rows without an id -> INVALID ---------------------------------------------------
    r = txn[~has_id]
    parts.append(_standalone(r, "source", "INVALID", ["Missing transaction ID: record cannot be matched to the ledger"] * len(r)))

    # -- 2. extra copies -> DUPLICATE --------------------------------------------------------------
    r = txn[has_id & (txn["copy_no"] > 1)]
    reasons = [f"Copy #{k} of {n} for {t}; the first copy is reconciled separately"
               for t, k, n in zip(r["transaction_id"], r["copy_no"], r["copies"])]
    parts.append(_standalone(r, "source", "DUPLICATE", reasons))
    r = ledger[ledger["copy_no"] > 1]
    reasons = [f"Duplicate ledger entry #{k} for {t}" for t, k in zip(r["transaction_id"], r["copy_no"])]
    parts.append(_standalone(r, "ledger", "DUPLICATE", reasons))

    # -- 3. pair the first copy on each side -------------------------------------------------------
    s1 = txn[has_id & (txn["copy_no"] == 1)]
    l1 = ledger[ledger["copy_no"] == 1]
    m = s1.merge(l1, on="transaction_id", how="outer", suffixes=("_s", "_l"), indicator=True)

    r = m[m["_merge"] == "left_only"]
    src_view = r.rename(columns={"id_s": "id", "amount_s": "amount", "amount_inr_s": "amount_inr",
                                 "currency_s": "currency", "business_unit_id_s": "business_unit_id"})
    parts.append(_standalone(src_view, "source", "MISSING_IN_LEDGER",
                             [f"No ledger entry found for {t}" for t in r["transaction_id"]]))
    r = m[m["_merge"] == "right_only"]
    led_view = r.rename(columns={"id_l": "id", "amount_l": "amount", "amount_inr_l": "amount_inr",
                                 "currency_l": "currency", "business_unit_id_l": "business_unit_id"})
    parts.append(_standalone(led_view, "ledger", "MISSING_IN_SOURCE",
                             [f"Ledger entry for {t} has no source transaction" for t in r["transaction_id"]]))

    b = m[m["_merge"] == "both"].copy()
    if len(b):
        parts.append(_evaluate_pairs(b, cfg))

    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    for c in ("source_row_id", "ledger_row_id"):
        out[c] = out[c].astype("Int64")
    out["business_unit_id"] = out["business_unit_id"].astype("Int64")
    out["exposure_inr"] = out["exposure_inr"].round(2)
    out["record_date"] = pd.to_datetime(out["record_date"])
    out = out.sort_values(["record_date", "transaction_id", "source_row_id", "ledger_row_id"],
                          na_position="last", kind="mergesort").reset_index(drop=True)
    return out[OUT_COLUMNS]


def _evaluate_pairs(b: pd.DataFrame, cfg) -> pd.DataFrame:
    """Apply the five match conditions to transactions found on both sides."""
    src_ccy_missing = b["currency_s"].isna()
    src_bu_missing = b["business_unit_id_s"].isna()
    invalid = src_ccy_missing | src_bu_missing

    cur_diff = ~src_ccy_missing & (b["currency_s"] != b["currency_l"])
    diff = b["amount_l"] - b["amount_s"]
    amt_diff = diff.abs() > cfg.amount_tolerance
    bu_diff = (~src_bu_missing & (b["business_unit_id_s"] != b["business_unit_id_l"])).fillna(False).to_numpy(dtype=bool)
    lag = (b["ledger_date"] - pd.to_datetime(b["transaction_date"]).dt.normalize()).dt.days
    date_bad = (lag < 0) | (lag > cfg.recon_date_tolerance_days)

    status = np.select(
        [invalid.to_numpy(), cur_diff.to_numpy(), amt_diff.to_numpy(), bu_diff, date_bad.to_numpy()],
        ["INVALID", "CURRENCY_MISMATCH", "AMOUNT_MISMATCH", "BUSINESS_UNIT_MISMATCH", "DATE_MISMATCH"],
        default="MATCHED")

    same_unit = (~src_ccy_missing & ~cur_diff).to_numpy()
    difference = np.where(same_unit, diff.to_numpy(), np.nan)

    delta_inr = (b["amount_inr_l"] - b["amount_inr_s"]).abs()
    at_stake = b["amount_inr_l"].abs().fillna(b["amount_inr_s"].abs()).fillna(b["amount_s"].abs())
    amount_break = np.isin(status, ["AMOUNT_MISMATCH", "CURRENCY_MISMATCH"])
    exposure = np.where(status == "MATCHED", 0.0, np.where(amount_break, delta_inr.fillna(at_stake), at_stake))

    # human-readable reasons, only for the rows that are not MATCHED
    reason = np.full(len(b), None, dtype=object)
    for i in np.flatnonzero(status != "MATCHED"):
        row = b.iloc[i]
        bits = []
        if src_ccy_missing.iloc[i]:
            bits.append("source currency is missing")
        if src_bu_missing.iloc[i]:
            bits.append("source business unit is missing")
        if cur_diff.iloc[i]:
            bits.append(f"currency: source {row['currency_s']} vs ledger {row['currency_l']}")
        if amt_diff.iloc[i]:
            bits.append(f"amount: source {row['amount_s']:,.2f} vs ledger {row['amount_l']:,.2f}")
        if bu_diff[i]:
            bits.append(f"business unit: source {int(row['business_unit_id_s'])} vs ledger {int(row['business_unit_id_l'])}")
        if date_bad.iloc[i]:
            bits.append(f"date: ledger posted {int(lag.iloc[i])} day(s) after the transaction "
                        f"(allowed 0 to {cfg.recon_date_tolerance_days})")
        reason[i] = "; ".join(bits)

    return pd.DataFrame({
        "source_row_id": b["id_s"].to_numpy(), "ledger_row_id": b["id_l"].to_numpy(),
        "transaction_id": b["transaction_id"].to_numpy(dtype=object),
        "source_amount": b["amount_s"].to_numpy(), "ledger_amount": b["amount_l"].to_numpy(),
        "difference": difference, "status": status,
        "business_unit_id": b["business_unit_id_s"].astype("Int64").fillna(b["business_unit_id_l"].astype("Int64")).to_numpy(),
        "currency": b["currency_s"].fillna(b["currency_l"]).to_numpy(dtype=object),
        "record_date": pd.to_datetime(b["transaction_date"]).dt.normalize().to_numpy(),
        "exposure_inr": exposure, "reason": reason})


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
def summarise(recon: pd.DataFrame) -> dict:
    """Total Records, Matched, Mismatched, Missing, Duplicates, Match Rate (+ invalid and exposure)."""
    counts = recon["status"].value_counts().to_dict()
    total = len(recon)
    matched = counts.get("MATCHED", 0)
    return {
        "total_records": total,
        "matched": matched,
        "mismatched": sum(counts.get(s, 0) for s in MISMATCH_STATUSES),
        "missing": sum(counts.get(s, 0) for s in MISSING_STATUSES),
        "duplicates": counts.get("DUPLICATE", 0),
        "invalid": counts.get("INVALID", 0),
        "match_rate": match_rate(matched, total),
        "unreconciled_exposure_inr": round(float(recon.loc[recon["status"] != "MATCHED", "exposure_inr"].sum()), 2),
        "by_status": {s: counts.get(s, 0) for s in STATUSES if counts.get(s, 0)},
    }


# ----------------------------------------------------------------------------
# Database run
# ----------------------------------------------------------------------------
@dataclass
class ReconciliationResult:
    metrics: dict = field(default_factory=dict)
    rows_written: int = 0
    seconds: float = 0.0


def run_reconciliation(engine, cfg=settings, verbose: bool = True) -> ReconciliationResult:
    started = time.time()
    log = print if verbose else (lambda *a, **k: None)
    txn, led = load_transactions(engine), load_ledger(engine)
    recon = reconcile(txn, led, cfg)
    recon["reconciled_at"] = data_as_of(txn, led)

    cols = OUT_COLUMNS + ["reconciled_at"]
    buf = io.StringIO()
    out = recon[cols].copy()
    out["record_date"] = out["record_date"].dt.strftime("%Y-%m-%d")
    out.to_csv(buf, index=False, header=False, float_format="%.2f", date_format="%Y-%m-%d %H:%M:%S")
    buf.seek(0)

    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute("TRUNCATE reconciliations RESTART IDENTITY")
            cur.copy_expert(f"COPY reconciliations ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv)", buf)
            written = cur.rowcount
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    result = ReconciliationResult(metrics=summarise(recon), rows_written=written,
                                  seconds=round(time.time() - started, 1))
    m = result.metrics
    log(f"  Records    : {m['total_records']:,} reconciled ({len(txn):,} source rows + {len(led):,} ledger rows, "
        f"paired where possible)")
    log(f"  Matched    : {m['matched']:,}   Mismatched: {m['mismatched']:,}   Missing: {m['missing']:,}   "
        f"Duplicates: {m['duplicates']:,}   Invalid: {m['invalid']:,}")
    log(f"  Match rate : {m['match_rate']:.2f}%   Unreconciled exposure: INR {m['unreconciled_exposure_inr'] / 1e6:,.1f} M")
    return result
