"""
FinSight ETL service
====================

    Extract  ->  Validate  ->  Transform  ->  Load (PostgreSQL)

Two kinds of data problems are handled differently on purpose:

* STRUCTURAL problems (unparseable date, non-numeric amount, missing NOT-NULL
  value, unknown transaction type, unknown business-unit FK on a table that
  requires one) cannot be stored.  These rows are QUARANTINED to
  data/processed/rejected_records.csv with a reason.

* BUSINESS-RULE problems (missing transaction_id / currency / business unit,
  duplicates, invalid amounts, ledger mismatches, ...) are LOADED as-is.  They
  are the job of the control engine (controls_service.py), which turns each of
  them into an auditable exception.  Deleting them here would hide them.

Every function is small and pure where possible so it can be unit-tested
without a database (see tests/test_etl.py).
"""
from __future__ import annotations

import io
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from backend.config import PROJECT_ROOT

RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
SQL_DIR = PROJECT_ROOT / "sql"

VALID_TRANSACTION_TYPES = {"REVENUE", "COST", "EXPENSE", "ADJUSTMENT"}

# Load order = parents before children (foreign keys).
LOAD_ORDER = ["business_units", "fx_rates", "liquidity_targets", "transactions", "ledger_entries", "budgets", "pnl", "cash_flows"]

# Contract for every input file.
#   columns     : columns that must exist in the CSV (also the COPY column order)
#   required    : NOT NULL columns - a row missing one is rejected
#   timestamps  : parsed as datetime;  dates : parsed as date;  numerics : float;  ints : nullable integer
#   derived     : extra columns created by transform() and loaded too
#   bu_fk       : "reject" = unknown business unit rejects the row, "null" = set to NULL (control Rule 5 catches it)
TABLE_SPECS: dict[str, dict] = {
    "business_units": dict(columns=["id", "name", "division", "region"],
                           required=["id", "name", "division", "region"], ints=["id"]),
    "fx_rates": dict(columns=["currency", "name", "rate_to_inr"],
                     required=["currency", "name", "rate_to_inr"], numerics=["rate_to_inr"]),
    "liquidity_targets": dict(columns=["business_unit_id", "min_liquidity"],
                              required=["business_unit_id", "min_liquidity"], numerics=["min_liquidity"],
                              ints=["business_unit_id"], bu_fk="reject"),
    "transactions": dict(
        columns=["transaction_id", "transaction_date", "business_unit_id", "product", "currency", "amount",
                 "transaction_type", "source_system", "created_at"],
        required=["transaction_date", "product", "amount", "transaction_type", "source_system", "created_at"],
        timestamps=["transaction_date", "created_at"], numerics=["amount"], ints=["business_unit_id"],
        derived=["amount_inr"], bu_fk="null"),
    "ledger_entries": dict(
        columns=["transaction_id", "ledger_date", "amount", "currency", "business_unit_id"],
        required=["transaction_id", "ledger_date", "amount", "currency", "business_unit_id"],
        dates=["ledger_date"], numerics=["amount"], ints=["business_unit_id"],
        derived=["amount_inr"], bu_fk="reject"),
    "budgets": dict(
        columns=["month", "business_unit_id", "budget_revenue", "budget_expense", "budget_pnl"],
        required=["month", "business_unit_id", "budget_revenue", "budget_expense", "budget_pnl"],
        dates=["month"], numerics=["budget_revenue", "budget_expense", "budget_pnl"],
        ints=["business_unit_id"], bu_fk="reject"),
    "pnl": dict(
        columns=["date", "business_unit_id", "product", "currency", "revenue", "cost", "expense",
                 "actual_pnl", "budget_pnl"],
        required=["date", "business_unit_id", "product", "currency", "revenue", "cost", "expense",
                  "actual_pnl", "budget_pnl"],
        dates=["date"], numerics=["revenue", "cost", "expense", "actual_pnl", "budget_pnl"],
        ints=["business_unit_id"], bu_fk="reject"),
    "cash_flows": dict(
        columns=["date", "business_unit_id", "opening_cash", "cash_inflow", "cash_outflow",
                 "closing_cash", "funding"],
        required=["date", "business_unit_id", "opening_cash", "cash_inflow", "cash_outflow", "closing_cash"],
        dates=["date"], numerics=["opening_cash", "cash_inflow", "cash_outflow", "closing_cash", "funding"],
        ints=["business_unit_id"], bu_fk="reject"),
}


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------
@dataclass
class EtlReport:
    extracted: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    loaded: dict = field(default_factory=dict)
    warnings: dict = field(default_factory=dict)      # data-quality observations (not rejections)
    seconds: dict = field(default_factory=dict)

    def total_rejected(self) -> int:
        return sum(self.rejected.values())


# ----------------------------------------------------------------------------
# 1. EXTRACT
# ----------------------------------------------------------------------------
def extract(raw_dir: Path = RAW_DIR) -> dict[str, pd.DataFrame]:
    """Read every CSV as text (no guessing of types) and enforce the column contract."""
    tables = {}
    for table in LOAD_ORDER:
        path = Path(raw_dir) / f"{table}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}\nGenerate the data first:  python data/generate_data.py"
            )
        df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
        missing = [c for c in TABLE_SPECS[table]["columns"] if c not in df.columns]
        if missing:
            raise ValueError(f"{path.name} is missing required columns: {missing}")
        tables[table] = df
    return tables


# ----------------------------------------------------------------------------
# 2. VALIDATE (structural) - returns typed rows + quarantined rows
# ----------------------------------------------------------------------------
def validate(table: str, raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    spec = TABLE_SPECS[table]
    raw = raw[spec["columns"]].copy()
    for col in raw.columns:                                   # trim whitespace
        raw[col] = raw[col].str.strip()
    df = raw.copy()
    reasons = pd.Series("", index=df.index, dtype=object)

    def flag(mask, reason):
        first_time = mask & (reasons == "")
        reasons.loc[first_time] = reason

    for col in spec.get("required", []):
        flag(df[col].isna(), f"MISSING_REQUIRED:{col}")

    for col in spec.get("timestamps", []) + spec.get("dates", []):
        parsed = pd.to_datetime(df[col], errors="coerce")
        flag(df[col].notna() & parsed.isna(), f"UNPARSEABLE_DATE:{col}")
        df[col] = parsed

    for col in spec.get("numerics", []):
        parsed = pd.to_numeric(df[col], errors="coerce")
        flag(df[col].notna() & parsed.isna(), f"NON_NUMERIC:{col}")
        df[col] = parsed.astype("float64")

    for col in spec.get("ints", []):
        parsed = pd.to_numeric(df[col], errors="coerce")
        bad = df[col].notna() & (parsed.isna() | (parsed % 1 != 0))
        flag(bad, f"NOT_INTEGER:{col}")
        df[col] = parsed.where(~bad).astype("Int64")

    if table == "transactions":
        flag(~df["transaction_type"].str.upper().isin(VALID_TRANSACTION_TYPES), "INVALID_TRANSACTION_TYPE")

    bad_rows = reasons != ""
    rejected = raw[bad_rows].copy()
    rejected.insert(0, "reject_reason", reasons[bad_rows])
    rejected.insert(0, "reject_table", table)
    return df[~bad_rows].copy(), rejected


# ----------------------------------------------------------------------------
# 3. TRANSFORM
# ----------------------------------------------------------------------------
def transform(tables: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[pd.DataFrame], dict]:
    """Standardise text, normalise currency to INR, enforce business-unit mapping.
    Returns (tables, extra_rejected_frames, warnings)."""
    out = {name: df.copy() for name, df in tables.items()}
    extra_rejected: list[pd.DataFrame] = []
    warnings: dict[str, int] = {}

    # --- reference data
    fx = out["fx_rates"]
    fx["currency"] = fx["currency"].str.upper()
    rates = fx.set_index("currency")["rate_to_inr"].astype(float)
    valid_bu = set(out["business_units"]["id"].astype(int))

    # --- transactions
    t = out["transactions"]
    t["transaction_id"] = t["transaction_id"].str.upper()
    t["currency"] = t["currency"].str.upper()
    t["transaction_type"] = t["transaction_type"].str.upper()
    t["amount"] = t["amount"].round(2)
    unmapped = t["business_unit_id"].notna() & ~t["business_unit_id"].isin(valid_bu)
    if unmapped.any():
        t.loc[unmapped, "business_unit_id"] = pd.NA               # control Rule 5 will flag these
        warnings["transactions_unmapped_business_unit_set_null"] = int(unmapped.sum())
    t["amount_inr"] = (t["amount"] * t["currency"].map(rates)).round(2)
    no_fx = int(t["amount_inr"].isna().sum())
    if no_fx:
        warnings["transactions_without_amount_inr (missing/unknown currency)"] = no_fx

    # --- ledger
    led = out["ledger_entries"]
    led["transaction_id"] = led["transaction_id"].str.upper()
    led["currency"] = led["currency"].str.upper()
    led["amount"] = led["amount"].round(2)
    led["amount_inr"] = (led["amount"] * led["currency"].map(rates)).round(2)

    # --- business-unit foreign key on tables that require one
    for name, spec in TABLE_SPECS.items():
        if spec.get("bu_fk") == "reject":
            df = out[name]
            bad = ~df["business_unit_id"].astype(int).isin(valid_bu)
            if bad.any():
                rej = df[bad].astype(str)
                rej.insert(0, "reject_reason", "UNKNOWN_BUSINESS_UNIT")
                rej.insert(0, "reject_table", name)
                extra_rejected.append(rej)
                out[name] = df[~bad].copy()

    # --- date-only columns become plain dates
    for name, spec in TABLE_SPECS.items():
        for col in spec.get("dates", []):
            out[name][col] = out[name][col].dt.strftime("%Y-%m-%d")

    return out, extra_rejected, warnings


def data_quality_warnings(tables: dict[str, pd.DataFrame]) -> dict:
    """Independent re-computation of derived measures in the reporting tables."""
    w = {}
    p = tables["pnl"]
    n = int(((p["actual_pnl"] - (p["revenue"] - p["cost"] - p["expense"])).abs() > 0.01).sum())
    if n:
        w["pnl_rows_where_actual_pnl_is_not_revenue_minus_costs"] = n
    c = tables["cash_flows"]
    n = int(((c["closing_cash"] - (c["opening_cash"] + c["cash_inflow"] - c["cash_outflow"])).abs() > 0.01).sum())
    if n:
        w["cash_rows_where_closing_is_not_opening_plus_flows"] = n
    return w


# ----------------------------------------------------------------------------
# 4. LOAD
# ----------------------------------------------------------------------------
def _copy_frame(cur, table: str, df: pd.DataFrame) -> int:
    spec = TABLE_SPECS[table]
    columns = spec["columns"] + spec.get("derived", [])
    buf = io.StringIO()
    df[columns].to_csv(buf, index=False, header=False, float_format="%.2f",
                       date_format="%Y-%m-%d %H:%M:%S")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, HEADER false)", buf)
    return cur.rowcount


def load(tables: dict[str, pd.DataFrame], conn, reset_schema: bool = True) -> dict[str, int]:
    loaded = {}
    with conn.cursor() as cur:
        if reset_schema:
            cur.execute((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
        else:
            cur.execute("TRUNCATE " + ", ".join(reversed(LOAD_ORDER)) + " RESTART IDENTITY CASCADE")
        for table in LOAD_ORDER:
            loaded[table] = _copy_frame(cur, table, tables[table])
        cur.execute((SQL_DIR / "views.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ANALYZE;")
    conn.autocommit = False
    return loaded


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def run_etl(conn, raw_dir: Path = RAW_DIR, reset_schema: bool = True, verbose: bool = True) -> EtlReport:
    report = EtlReport()
    log = print if verbose else (lambda *a, **k: None)

    t0 = time.time()
    raw = extract(raw_dir)
    report.extracted = {k: len(v) for k, v in raw.items()}
    report.seconds["extract"] = round(time.time() - t0, 2)
    log(f"  Extract    {sum(report.extracted.values()):>9,} rows from {len(raw)} files")

    t0 = time.time()
    typed, rejected_frames = {}, []
    for table, df in raw.items():
        good, bad = validate(table, df)
        typed[table] = good
        if len(bad):
            rejected_frames.append(bad)
    report.seconds["validate"] = round(time.time() - t0, 2)
    log(f"  Validate   {sum(len(f) for f in rejected_frames):>9,} structurally invalid rows quarantined")

    t0 = time.time()
    clean, extra_rejected, warns = transform(typed)
    rejected_frames.extend(extra_rejected)
    warns.update(data_quality_warnings(typed))
    report.warnings = warns
    report.seconds["transform"] = round(time.time() - t0, 2)
    n_rej_total = sum(len(f) for f in rejected_frames)
    log(f"  Transform  {sum(len(v) for v in clean.values()):>9,} rows standardised (amount_inr derived)"
        + (f", {n_rej_total} rejected in total" if n_rej_total else ""))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    rej_path = PROCESSED_DIR / "rejected_records.csv"
    if rejected_frames:
        pd.concat(rejected_frames, ignore_index=True).to_csv(rej_path, index=False)
        for f in rejected_frames:
            table = f["reject_table"].iloc[0]
            report.rejected[table] = report.rejected.get(table, 0) + len(f)
    elif rej_path.exists():
        rej_path.unlink()

    t0 = time.time()
    report.loaded = load(clean, conn, reset_schema=reset_schema)
    report.seconds["load"] = round(time.time() - t0, 2)
    log(f"  Load       {sum(report.loaded.values()):>9,} rows into PostgreSQL")

    for k, v in report.warnings.items():
        log(f"  [warning] {k}: {v:,}")
    (PROCESSED_DIR / "etl_report.json").write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    return report
