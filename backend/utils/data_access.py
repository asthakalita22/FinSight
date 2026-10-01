"""Shared database readers used by the reconciliation and control engines.

NUMERIC columns are cast to float8 so pandas gets fast float columns instead of Decimal objects."""
import pandas as pd
from sqlalchemy import text


def load_transactions(engine) -> pd.DataFrame:
    txn = pd.read_sql(text("""
        SELECT id, transaction_id, transaction_date, business_unit_id, product, currency,
               amount::float8 AS amount, amount_inr::float8 AS amount_inr,
               transaction_type, created_at
        FROM transactions"""), engine)
    txn["business_unit_id"] = txn["business_unit_id"].astype("Int64")
    return txn


def load_ledger(engine) -> pd.DataFrame:
    led = pd.read_sql(text("""
        SELECT id, transaction_id, ledger_date, amount::float8 AS amount,
               amount_inr::float8 AS amount_inr, currency, business_unit_id
        FROM ledger_entries"""), engine)
    led["ledger_date"] = pd.to_datetime(led["ledger_date"])
    led["business_unit_id"] = led["business_unit_id"].astype("Int64")
    return led


def load_fx_rates(engine) -> dict:
    fx = pd.read_sql(text("SELECT currency, rate_to_inr::float8 AS rate FROM fx_rates"), engine)
    return dict(zip(fx["currency"].str.strip(), fx["rate"]))
