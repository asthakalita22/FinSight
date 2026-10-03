"""Shared helpers for the Phase 6 FastAPI layer."""
from __future__ import annotations
from datetime import date
from typing import Any
import numpy as np
import pandas as pd
from fastapi import HTTPException
from backend.database import engine
from backend.utils.filters import Filters
from backend.utils.validators import validate_filters

def make_filters(*, month=None, quarter=None, start_date: date|None=None, end_date: date|None=None,
                 business_units=None, products=None, currency=None, allow_products=True, allow_currency=True):
    if month and quarter:
        raise HTTPException(400, "Use either month or quarter, not both.")
    if (month or quarter) and (start_date or end_date):
        raise HTTPException(400, "Use month/quarter or start_date/end_date, not both.")
    if not allow_products and products:
        raise HTTPException(400, "Product filtering does not apply to this endpoint.")
    if not allow_currency and currency:
        raise HTTPException(400, "Currency filtering does not apply to this endpoint.")
    try:
        kwargs=dict(business_units=tuple(business_units) if business_units else None,
                    products=tuple(products) if products else None, currency=currency)
        f=Filters.for_period(month=month, quarter=quarter, **kwargs) if (month or quarter) else Filters(
            start_date=start_date,end_date=end_date,**kwargs)
        validate_filters(engine, f, check_products=allow_products)
        return f
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

def _json_value(value: Any):
    if value is None or value is pd.NA: return None
    if isinstance(value,(pd.Timestamp,date)): return value.isoformat()
    if isinstance(value,(np.integer,np.floating,np.bool_)): return value.item()
    if isinstance(value,float) and (np.isnan(value) or np.isinf(value)): return None
    if isinstance(value,dict): return {k:_json_value(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [_json_value(v) for v in value]
    return value

def records(frame: pd.DataFrame):
    if frame is None or frame.empty: return []
    clean=frame.astype(object).where(pd.notna(frame),None)
    return [{k:_json_value(v) for k,v in row.items()} for row in clean.to_dict(orient="records")]

def clean_dict(payload: dict[str,Any]):
    return {k:_json_value(v) for k,v in payload.items()}
