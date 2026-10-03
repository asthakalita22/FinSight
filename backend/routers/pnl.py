"""P&L and budget-variance API endpoints."""
from __future__ import annotations
from datetime import date
from fastapi import APIRouter, Query
from backend.api_utils import clean_dict, make_filters, records
from backend.database import engine
from backend.services import pnl_service
router=APIRouter(prefix="/api/v1/pnl",tags=["P&L"])

def _f(month,quarter,start_date,end_date,business_unit,product,currency):
    return make_filters(month=month,quarter=quarter,start_date=start_date,end_date=end_date,
                        business_units=business_unit,products=product,currency=currency)

@router.get("/summary")
def summary(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,
            business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None),currency=None):
    f=_f(month,quarter,start_date,end_date,business_unit,product,currency)
    return {"data":clean_dict(pnl_service.summary(engine,f))}

@router.get("/trend")
def trend(grain:str=Query("month",pattern="^(day|month|quarter)$"),
          by:str|None=Query(None,pattern="^(business_unit|product)$"),month=None,quarter=None,
          start_date:date|None=None,end_date:date|None=None,business_unit:list[str]|None=Query(None),
          product:list[str]|None=Query(None),currency=None):
    f=_f(month,quarter,start_date,end_date,business_unit,product,currency)
    return {"data":records(pnl_service.trend(engine,f,grain=grain,by=by))}

@router.get("/breakdown")
def breakdown(dimension:str=Query("business_unit",pattern="^(business_unit|product)$"),month=None,quarter=None,
              start_date:date|None=None,end_date:date|None=None,business_unit:list[str]|None=Query(None),
              product:list[str]|None=Query(None),currency=None):
    f=_f(month,quarter,start_date,end_date,business_unit,product,currency)
    return {"data":records(pnl_service.breakdown(engine,f,dimension))}

@router.get("/budget-variance")
def budget_variance(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,
                    business_unit:list[str]|None=Query(None)):
    f=_f(month,quarter,start_date,end_date,business_unit,None,None)
    return {"data":records(pnl_service.budget_variance(engine,f))}
