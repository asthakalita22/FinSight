"""Reconciliation reporting API endpoints."""
import pandas as pd
from fastapi import APIRouter
from sqlalchemy import text
from backend.api_utils import clean_dict,records
from backend.database import engine
router=APIRouter(prefix="/api/v1/reconciliation",tags=["Reconciliation"])

@router.get("/summary")
def summary():
    with engine.connect() as conn: row=conn.execute(text("SELECT * FROM v_reconciliation_summary")).mappings().one()
    return {"data":clean_dict(dict(row))}

@router.get("/by-business-unit")
def by_business_unit():
    with engine.connect() as conn:
        frame=pd.read_sql(text("SELECT * FROM v_reconciliation_by_bu ORDER BY breaks DESC,business_unit"),conn)
    return {"data":records(frame)}

@router.get("/daily")
def daily():
    with engine.connect() as conn:
        frame=pd.read_sql(text("SELECT * FROM v_reconciliation_daily ORDER BY date"),conn)
    return {"data":records(frame)}
