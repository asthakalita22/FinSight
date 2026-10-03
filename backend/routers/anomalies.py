"""Isolation Forest anomaly reporting API endpoints."""
from __future__ import annotations
from datetime import date
from fastapi import APIRouter,Query
from backend.api_utils import clean_dict,make_filters,records
from backend.database import engine
from backend.services import anomaly_service
router=APIRouter(prefix="/api/v1/anomalies",tags=["Anomalies"])

def _f(month,quarter,start_date,end_date,business_unit,product):
    return make_filters(month=month,quarter=quarter,start_date=start_date,end_date=end_date,
                        business_units=business_unit,products=product)

@router.get("/summary")
def summary(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,
            business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None)):
    return {"data":clean_dict(anomaly_service.summary(engine,_f(month,quarter,start_date,end_date,business_unit,product)))}

@router.get("/by-business-unit")
def by_business_unit(month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,
                     business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None)):
    return {"data":records(anomaly_service.by_business_unit(engine,_f(month,quarter,start_date,end_date,business_unit,product)))}

@router.get("/trend")
def trend(grain:str=Query("month",pattern="^(day|month|quarter)$"),month=None,quarter=None,start_date:date|None=None,
          end_date:date|None=None,business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None)):
    return {"data":records(anomaly_service.trend(engine,_f(month,quarter,start_date,end_date,business_unit,product),grain=grain))}

@router.get("/top")
def top(order_by:str=Query("amount",pattern="^(amount|score)$"),risk:str|None=Query(None,pattern="^(LOW|MEDIUM|HIGH)$"),
        limit:int=Query(20,ge=1,le=200),month=None,quarter=None,start_date:date|None=None,end_date:date|None=None,
        business_unit:list[str]|None=Query(None),product:list[str]|None=Query(None)):
    frame=anomaly_service.top_anomalies(engine,_f(month,quarter,start_date,end_date,business_unit,product),
                                        n=limit if risk is None else 500,order_by=order_by)
    if risk: frame=frame[frame["risk_level"]==risk].head(limit)
    else: frame=frame.head(limit)
    return {"data":records(frame)}

@router.get("/score-histogram")
def score_histogram(bins:int=Query(40,ge=5,le=100)):
    return {"data":records(anomaly_service.score_histogram(engine,bins=bins))}

@router.get("/latest-run")
def latest_run():
    return {"data":clean_dict(anomaly_service.latest_run(engine) or {})}
