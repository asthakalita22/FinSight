"""Liquidity API endpoints."""
from __future__ import annotations
from datetime import date
from fastapi import APIRouter,Query
from backend.api_utils import clean_dict,make_filters,records
from backend.database import engine
from backend.services import liquidity_service
router=APIRouter(prefix="/api/v1/liquidity",tags=["Liquidity"])

def _f(month,quarter,start_date,end_date,business_unit,product=None,currency=None):
    return make_filters(month=month,quarter=quarter,start_date=start_date,end_date=end_date,
                        business_units=business_unit,products=product,currency=currency,
                        allow_products=False,allow_currency=False)

@router.get("/summary")
def summary(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None),currency=None):
    return {"data":clean_dict(liquidity_service.summary(engine,_f(month,quarter,start_date,end_date,business_unit,product,currency)))}

@router.get("/daily")
def daily(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None),currency=None):
    return {"data":records(liquidity_service.daily(engine,_f(month,quarter,start_date,end_date,business_unit,product,currency)))}

@router.get("/monthly")
def monthly(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None),currency=None):
    return {"data":records(liquidity_service.monthly(engine,_f(month,quarter,start_date,end_date,business_unit,product,currency)))}

@router.get("/business-units")
def business_units(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None),currency=None):
    return {"data":records(liquidity_service.by_business_unit(engine,_f(month,quarter,start_date,end_date,business_unit,product,currency)))}
