"""
FinSight control engine
=======================

Every failed control becomes one row in the `exceptions` table.

Rule catalogue
--------------
CTL-001  Missing Transaction ID     source row has no transaction_id
CTL-002  Duplicate Transaction      same transaction_id loaded more than once (every extra copy)
CTL-003  Invalid Amount             amount <= 0 on a REVENUE / COST / EXPENSE transaction
CTL-004  Missing Currency           source row has no currency
CTL-005  Missing Business Unit      source row has no business unit
CTL-006A Amount Mismatch            source amount != ledger amount (beyond tolerance)
CTL-006B Missing in Ledger          source transaction has no ledger entry
CTL-006C Missing in Source          ledger entry has no source transaction
CTL-007  Excessive Variance         |Actual P&L - Budget P&L| / |Budget| above threshold (per BU-month)

Design notes
------------
* Rules are PURE functions: DataFrames in, DataFrame of exceptions out. No database access,
  so they are unit-tested with tiny hand-made frames (tests/test_controls.py).
* `amount` on an exception is the EXPOSURE IN INR, so exposures can be added up across currencies.
* Severity = rule's base severity, raised one level at "high value" exposure and two levels at
  "critical value" exposure (thresholds in backend/config.py / .env). CTL-007 is graded by variance size.
* Control runs are IDEMPOTENT: each exception has a unique (exception_code, source_ref). Re-running the
  controls inserts only NEW problems and never overwrites an analyst's status changes.
* `created_at` is the SIMULATED detection time (next-day close of the record's business date), because the
  data is a historical simulation. It gives the dashboards realistic exception trends and ageing.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sqlalchemy import text

from backend.config import settings
from backend.utils.calculations import data_as_of
from backend.utils.data_access import load_fx_rates, load_ledger, load_transactions

LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
LEVEL_INDEX = {name: i for i, name in enumerate(LEVELS)}

# code -> (category, base severity, owning team)
RULES: dict[str, tuple[str, str, str]] = {
    "CTL-001": ("Missing Transaction ID", "HIGH", "Data Quality Team"),
    "CTL-002": ("Duplicate Transaction", "LOW", "Data Quality Team"),
    "CTL-003": ("Invalid Amount", "HIGH", "Financial Control"),
    "CTL-004": ("Missing Currency", "MEDIUM", "Data Quality Team"),
    "CTL-005": ("Missing Business Unit", "MEDIUM", "Data Quality Team"),
    "CTL-006A": ("Amount Mismatch", "MEDIUM", "Reconciliation Team"),
    "CTL-006B": ("Missing in Ledger", "MEDIUM", "Reconciliation Team"),
    "CTL-006C": ("Missing in Source", "HIGH", "Reconciliation Team"),
    "CTL-007": ("Excessive Variance", "HIGH", "FP&A"),
}

EXC_COLUMNS = ["exception_code", "source_ref", "transaction_id", "category", "severity",
               "amount", "business_unit_id", "description", "detected_at"]

LEDGER_GRACE_DAYS = 5  # a missing ledger entry is raised after the normal posting window has passed


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def assign_severity(base: pd.Series, exposure: pd.Series, cfg=settings) -> pd.Series:
    """Base severity, +1 level at high-value exposure, +2 levels at critical-value exposure (max CRITICAL)."""
    idx = base.map(LEVEL_INDEX).to_numpy(dtype=int)
    exp = pd.to_numeric(exposure, errors="coerce").to_numpy(dtype=float)
    bump = np.where(exp >= cfg.critical_value_inr, 2, np.where(exp >= cfg.high_value_inr, 1, 0))
    levels = np.array(LEVELS, dtype=object)
    return pd.Series(levels[np.minimum(idx + bump, 3)], index=base.index)


def _inr(x: float) -> str:
    return f"INR {x:,.2f}"


def _next_day_close(ts: pd.Series) -> pd.Series:
    """Simulated detection time: 18:00 on the day after the record's business date."""
    return pd.to_datetime(ts).dt.normalize() + pd.Timedelta(days=1, hours=18)


def _make(code, refs, txn_ids, exposure, bu, descriptions, detected_at, cfg, severity=None) -> pd.DataFrame:
    category, base, _ = RULES[code]
    exposure = pd.Series(np.round(np.asarray(exposure, dtype=float), 2))
    if severity is None:
        severity = assign_severity(pd.Series([base] * len(exposure)), exposure, cfg)
    return pd.DataFrame({
        "exception_code": code,
        "source_ref": list(refs),
        "transaction_id": pd.Series(list(txn_ids), dtype=object),
        "category": category,
        "severity": list(severity),
        "amount": exposure.to_numpy(),
        "business_unit_id": pd.array(list(bu), dtype="Int64"),
        "description": list(descriptions),
        "detected_at": pd.to_datetime(pd.Series(list(detected_at))),
    })


def add_exposure(txn: pd.DataFrame, ledger: pd.DataFrame, rates: dict) -> pd.DataFrame:
    """exposure_inr = |amount_inr|.  If the source currency is missing, fall back to the ledger's
    currency for the same transaction_id, and finally assume INR."""
    txn = txn.copy()
    led_ccy = ledger.drop_duplicates("transaction_id").set_index("transaction_id")["currency"]
    fallback_rate = txn["transaction_id"].map(led_ccy).map(rates).fillna(1.0)
    txn["exposure_inr"] = txn["amount_inr"].abs().fillna(txn["amount"].abs() * fallback_rate)
    return txn


# ----------------------------------------------------------------------------
# Rules 1-5: single-table checks on the source transactions
# ----------------------------------------------------------------------------
def rule_missing_transaction_id(txn, cfg=settings) -> pd.DataFrame:
    r = txn[txn["transaction_id"].isna()]
    desc = [f"Transaction row #{i} dated {d:%Y-%m-%d} ({p}, {t}) has no transaction ID"
            for i, d, p, t in zip(r["id"], r["transaction_date"], r["product"], r["transaction_type"])]
    return _make("CTL-001", [f"TXN:{i}" for i in r["id"]], [None] * len(r), r["exposure_inr"],
                 r["business_unit_id"], desc, _next_day_close(r["transaction_date"]), cfg)


def rule_duplicate_transaction(txn, cfg=settings) -> pd.DataFrame:
    t = txn[txn["transaction_id"].notna()].sort_values(["created_at", "id"]).copy()
    grp = t.groupby("transaction_id")["id"]
    t["copy_no"] = grp.cumcount() + 1
    t["copies"] = grp.transform("size")
    r = t[t["copy_no"] > 1]
    desc = [f"{tid} was loaded {n} times; this is copy #{k} (originally posted {d:%Y-%m-%d})"
            for tid, n, k, d in zip(r["transaction_id"], r["copies"], r["copy_no"], r["transaction_date"])]
    return _make("CTL-002", [f"TXN:{i}" for i in r["id"]], r["transaction_id"], r["exposure_inr"],
                 r["business_unit_id"], desc, _next_day_close(r["created_at"]), cfg)


def rule_invalid_amount(txn, cfg=settings) -> pd.DataFrame:
    r = txn[(txn["amount"] <= 0) & (txn["transaction_type"] != "ADJUSTMENT")]
    desc = [f"{t} amount is {a:,.2f} {c if isinstance(c, str) else '(no currency)'}; must be greater than zero"
            for t, a, c in zip(r["transaction_type"], r["amount"], r["currency"])]
    return _make("CTL-003", [f"TXN:{i}" for i in r["id"]], r["transaction_id"], r["exposure_inr"],
                 r["business_unit_id"], desc, _next_day_close(r["transaction_date"]), cfg)


def rule_missing_currency(txn, cfg=settings) -> pd.DataFrame:
    r = txn[txn["currency"].isna()]
    desc = [f"{tid or 'Row #' + str(i)} has no currency; amount {a:,.2f} cannot be converted to INR"
            for tid, i, a in zip(r["transaction_id"], r["id"], r["amount"])]
    return _make("CTL-004", [f"TXN:{i}" for i in r["id"]], r["transaction_id"], r["exposure_inr"],
                 r["business_unit_id"], desc, _next_day_close(r["transaction_date"]), cfg)


def rule_missing_business_unit(txn, cfg=settings) -> pd.DataFrame:
    r = txn[txn["business_unit_id"].isna()]
    desc = [f"{tid or 'Row #' + str(i)} is not assigned to any business unit ({p}, {t})"
            for tid, i, p, t in zip(r["transaction_id"], r["id"], r["product"], r["transaction_type"])]
    return _make("CTL-005", [f"TXN:{i}" for i in r["id"]], r["transaction_id"], r["exposure_inr"],
                 r["business_unit_id"], desc, _next_day_close(r["transaction_date"]), cfg)


# ----------------------------------------------------------------------------
# Rule 6: source vs ledger
# ----------------------------------------------------------------------------
def rule_ledger_mismatch(txn, ledger, rates, cfg=settings) -> dict[str, pd.DataFrame]:
    """Returns {'CTL-006A': ..., 'CTL-006B': ..., 'CTL-006C': ...}. Duplicated source copies are
    ignored here (CTL-002 owns them) so one bad transaction never produces several mismatches."""
    src = (txn[txn["transaction_id"].notna()].sort_values(["created_at", "id"])
           .drop_duplicates("transaction_id", keep="first"))
    led = ledger.sort_values("id").drop_duplicates("transaction_id", keep="first")
    m = src.merge(led, on="transaction_id", how="outer", suffixes=("_src", "_led"), indicator=True)

    # 006B - in source, not in ledger
    b = m[m["_merge"] == "left_only"]
    desc = [f"{tid} ({a:,.2f} {c}) has no ledger entry after the {LEDGER_GRACE_DAYS}-day posting window"
            for tid, a, c in zip(b["transaction_id"], b["amount_src"], b["currency_src"])]
    det_b = pd.to_datetime(b["transaction_date"]).dt.normalize() + pd.Timedelta(days=LEDGER_GRACE_DAYS, hours=18)
    out_b = _make("CTL-006B", [f"TXN:{int(i)}" for i in b["id_src"]], b["transaction_id"], b["exposure_inr"],
                  b["business_unit_id_src"].astype("Int64"), desc, det_b, cfg)

    # 006C - in ledger, not in source
    c_ = m[m["_merge"] == "right_only"]
    desc = [f"Ledger entry for {tid} ({a:,.2f} {c}, posted {d:%Y-%m-%d}) has no matching source transaction"
            for tid, a, c, d in zip(c_["transaction_id"], c_["amount_led"], c_["currency_led"], c_["ledger_date"])]
    out_c = _make("CTL-006C", [f"LED:{int(i)}" for i in c_["id_led"]], c_["transaction_id"],
                  c_["amount_inr_led"].abs(), c_["business_unit_id_led"].astype("Int64"), desc,
                  _next_day_close(c_["ledger_date"]), cfg)

    # 006A - both sides exist, amounts differ
    both = m[m["_merge"] == "both"]
    diff = both["amount_led"] - both["amount_src"]
    a = both[diff.abs() > cfg.amount_tolerance]
    diff = diff[a.index]
    exposure = diff.abs() * a["currency_led"].map(rates).fillna(1.0)
    desc = [f"{tid}: source {s:,.2f} vs ledger {l:,.2f} {c} (difference {d:+,.2f})"
            for tid, s, l, c, d in zip(a["transaction_id"], a["amount_src"], a["amount_led"],
                                       a["currency_led"], diff)]
    bu = a["business_unit_id_src"].astype("Int64").fillna(a["business_unit_id_led"].astype("Int64"))
    out_a = _make("CTL-006A", [f"TXN:{int(i)}" for i in a["id_src"]], a["transaction_id"], exposure, bu, desc,
                  _next_day_close(a["ledger_date"]), cfg)
    return {"CTL-006A": out_a, "CTL-006B": out_b, "CTL-006C": out_c}


# ----------------------------------------------------------------------------
# Rule 7: budget variance per business-unit month
# ----------------------------------------------------------------------------
def rule_excessive_variance(variance: pd.DataFrame, cfg=settings) -> pd.DataFrame:
    v = variance[variance["budget"].abs() > 0].copy()
    v["variance"] = v["actual"] - v["budget"]
    v["pct"] = 100.0 * v["variance"] / v["budget"].abs()
    r = v[v["pct"].abs() > cfg.variance_threshold_pct]
    severity = pd.Series(np.where(r["pct"].abs() >= 2 * cfg.variance_threshold_pct, "CRITICAL", "HIGH"))
    month = pd.to_datetime(r["month"])
    desc = [f"{n}, {m:%b %Y}: actual P&L {_inr(a)} vs budget {_inr(b)} ({p:+.1f}%), threshold ±{cfg.variance_threshold_pct:g}%"
            for n, m, a, b, p in zip(r["business_unit"], month, r["actual"], r["budget"], r["pct"])]
    detected = month.dt.to_period("M").dt.to_timestamp() + pd.offsets.MonthBegin(1) + pd.Timedelta(hours=9)
    return _make("CTL-007", [f"BUM:{int(b)}:{m:%Y-%m}" for b, m in zip(r["business_unit_id"], month)],
                 [None] * len(r), r["variance"].abs(), r["business_unit_id"].astype("Int64"), desc,
                 detected, cfg, severity=severity)


# ----------------------------------------------------------------------------
# Workflow simulation (optional) - gives the dashboards a realistic mix of statuses
# ----------------------------------------------------------------------------
def simulate_workflow(exc: pd.DataFrame, as_of: pd.Timestamp, rng: np.random.Generator) -> pd.DataFrame:
    """Older exceptions are mostly resolved; recent ones are mostly open; critical ones linger longer."""
    exc = exc.copy()
    n = len(exc)
    age = (as_of - exc["created_at"]).dt.total_seconds().to_numpy() / 86400
    slow = exc["severity"].map({"CRITICAL": 0.75, "HIGH": 0.9}).fillna(1.0).to_numpy()
    p_resolved = np.where(age >= 2, np.clip(0.05 + 0.018 * (age - 1), 0, 0.93), 0.0) * slow
    u, v = rng.random(n), rng.random(n)
    low_sev = exc["severity"].isin(["LOW", "MEDIUM"]).to_numpy()
    old_enough = age >= 2                                  # nothing can be closed before it has had time to be worked
    status = np.where(u < p_resolved, "RESOLVED",
             np.where(v < 0.40, "IN_REVIEW",
             np.where((v < 0.47) & low_sev & old_enough, "IGNORED", "OPEN")))
    closed = np.isin(status, ["RESOLVED", "IGNORED"])
    days_to_close = rng.uniform(0.5, 1.0, n) * np.clip(age, 1, 25)
    resolved_at = exc["created_at"] + pd.to_timedelta(days_to_close, unit="D")
    exc["status"] = status
    exc["resolved_at"] = resolved_at.where(closed)
    return exc


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
@dataclass
class ControlsResult:
    detected: dict = field(default_factory=dict)      # code -> exceptions found this run
    inserted: int = 0                                 # new rows written
    already_existed: int = 0                          # found again, left untouched
    by_severity: dict = field(default_factory=dict)   # for the whole exceptions table
    by_status: dict = field(default_factory=dict)
    seconds: float = 0.0


def load_inputs(engine):
    txn = load_transactions(engine)
    led = load_ledger(engine)
    rates = load_fx_rates(engine)
    variance = pd.read_sql(text("""SELECT date_trunc('month', p.date)::date AS month, p.business_unit_id,
                           bu.name AS business_unit, SUM(p.actual_pnl)::float8 AS actual,
                           SUM(p.budget_pnl)::float8 AS budget
                    FROM pnl p JOIN business_units bu ON bu.id = p.business_unit_id GROUP BY 1, 2, 3"""), engine)
    return txn, led, rates, variance


def detect_all(txn, led, rates, variance, cfg=settings) -> pd.DataFrame:
    """Run every rule and stack the results (no database writes)."""
    txn = add_exposure(txn, led, rates)
    parts = [rule_missing_transaction_id(txn, cfg), rule_duplicate_transaction(txn, cfg),
             rule_invalid_amount(txn, cfg), rule_missing_currency(txn, cfg),
             rule_missing_business_unit(txn, cfg)]
    parts.extend(rule_ledger_mismatch(txn, led, rates, cfg).values())
    parts.append(rule_excessive_variance(variance, cfg))
    exc = pd.concat(parts, ignore_index=True)
    exc = exc.rename(columns={"detected_at": "created_at"})
    exc["owner"] = exc["exception_code"].map(lambda c: RULES[c][2])
    exc["status"] = "OPEN"
    exc["resolved_at"] = pd.NaT
    return exc


def run_controls(engine, cfg=settings, simulate: bool = True, seed: int | None = None,
                 verbose: bool = True) -> ControlsResult:
    started = time.time()
    log = print if verbose else (lambda *a, **k: None)
    txn, led, rates, variance = load_inputs(engine)
    exc = detect_all(txn, led, rates, variance, cfg)

    result = ControlsResult(detected=exc["exception_code"].value_counts().sort_index().to_dict())
    as_of = data_as_of(txn, led)
    if simulate:
        exc = simulate_workflow(exc, as_of, np.random.default_rng(cfg.seed if seed is None else seed))

    cols = EXC_COLUMNS[:-1] + ["created_at", "owner", "status", "resolved_at"]
    exc = exc.sort_values(["created_at", "exception_code", "source_ref"])[cols]
    buf = io.StringIO()
    exc.to_csv(buf, index=False, header=False, float_format="%.2f", date_format="%Y-%m-%d %H:%M:%S")
    buf.seek(0)

    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(f"CREATE TEMP TABLE tmp_exc ON COMMIT DROP AS SELECT {', '.join(cols)} "
                        f"FROM exceptions WITH NO DATA")
            cur.copy_expert(f"COPY tmp_exc ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv)", buf)
            cur.execute(f"INSERT INTO exceptions ({', '.join(cols)}) SELECT {', '.join(cols)} FROM tmp_exc "
                        f"ORDER BY created_at, exception_code, source_ref "
                        f"ON CONFLICT (exception_code, source_ref) DO NOTHING")
            result.inserted = cur.rowcount
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    result.already_existed = len(exc) - result.inserted
    with engine.connect() as conn:
        result.by_severity = dict(conn.execute(text(
            "SELECT severity, COUNT(*) FROM exceptions GROUP BY 1")).fetchall())
        result.by_status = dict(conn.execute(text(
            "SELECT status, COUNT(*) FROM exceptions GROUP BY 1")).fetchall())
    result.seconds = round(time.time() - started, 1)

    log(f"  Rules run  : {len(RULES)} ({', '.join(RULES)})")
    for code, n in result.detected.items():
        log(f"    {code:<9}{RULES[code][0]:<26}{n:>7,}")
    log(f"  Exceptions : {result.inserted:,} new, {result.already_existed:,} already existed")
    return result
