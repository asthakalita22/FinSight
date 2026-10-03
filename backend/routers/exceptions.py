"""Financial control exception API endpoints."""
from __future__ import annotations

from datetime import date

import pandas as pd
from fastapi import APIRouter, Query
from sqlalchemy import text

from backend.api_utils import records
from backend.database import engine

router = APIRouter(prefix="/api/v1/exceptions", tags=["Controls & Exceptions"])


@router.get("/summary")
def summary():
    q = text("""SELECT severity,COUNT(*) AS count,COALESCE(SUM(ABS(amount)),0) AS exposure_inr
              FROM exceptions GROUP BY severity
              ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END""")
    with engine.connect() as conn:
        frame = pd.read_sql(q, conn)
    return {"data": records(frame)}


@router.get("/by-category")
def by_category():
    q = text("""SELECT category,severity,COUNT(*) AS count,COALESCE(SUM(ABS(amount)),0) AS exposure_inr
              FROM exceptions GROUP BY category,severity ORDER BY count DESC,category,severity""")
    with engine.connect() as conn:
        frame = pd.read_sql(q, conn)
    return {"data": records(frame)}


@router.get("/recent")
def recent(
    severity: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    category: list[str] | None = Query(None),
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    clauses: list[str] = []
    params: dict[str, object] = {"limit": limit}
    if severity:
        clauses.append("severity = ANY(:severity)")
        params["severity"] = [x.upper() for x in severity]
    if status:
        clauses.append("status = ANY(:status)")
        params["status"] = [x.upper() for x in status]
    if category:
        clauses.append("category = ANY(:category)")
        params["category"] = list(category)
    if start_date:
        clauses.append("created_at::date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        clauses.append("created_at::date <= :end_date")
        params["end_date"] = end_date
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    q = text(f"""SELECT id,exception_code,source_ref,transaction_id,category,severity,
               amount AS exposure_inr,business_unit_id,description,status,owner,created_at,resolved_at
               FROM exceptions {where}
               ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
                        created_at DESC LIMIT :limit""")
    with engine.connect() as conn:
        frame = pd.read_sql(q, conn, params=params)
    return {"data": records(frame)}


@router.get("/trend")
def trend(grain: str = Query("month", pattern="^(day|month)$")):
    """Return open exceptions by creation period for executive trend reporting."""
    period = "created_at::date" if grain == "day" else "date_trunc('month', created_at)::date"
    q = text(f"""
        SELECT {period} AS period, COUNT(*) AS open_exceptions
        FROM exceptions
        WHERE status <> 'RESOLVED'
        GROUP BY 1
        ORDER BY 1
    """)
    with engine.connect() as conn:
        frame = pd.read_sql(q, conn)
    return {"data": records(frame)}
