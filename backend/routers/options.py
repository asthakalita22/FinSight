"""Reference-data endpoints used by dashboard filters."""
import pandas as pd
from fastapi import APIRouter
from sqlalchemy import text
from backend.database import engine
router=APIRouter(prefix="/api/v1/options",tags=["Reference data"])

@router.get("")
def options():
    with engine.connect() as conn:
        bu=pd.read_sql(text("SELECT name FROM business_units ORDER BY name"),conn)["name"].tolist()
        products=pd.read_sql(text("SELECT DISTINCT product FROM transactions WHERE product IS NOT NULL ORDER BY product"),conn)["product"].tolist()
        currencies=pd.read_sql(text("SELECT currency FROM fx_rates ORDER BY currency"),conn)["currency"].tolist()
    return {"business_units":bu,"products":products,"currencies":currencies}
