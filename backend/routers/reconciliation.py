"""Reconciliation reporting API endpoints."""
from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Query
from sqlalchemy import text

from backend.api_utils import clean_dict, records
from backend.database import engine

router = APIRouter(prefix="/api/v1/reconciliation", tags=["Reconciliation"])


@router.get("/summary")
def summary():
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM v_reconciliation_summary")).mappings().one()
    return {"data": clean_dict(dict(row))}


@router.get("/by-business-unit")
def by_business_unit():
    with engine.connect() as conn:
        frame = pd.read_sql(text("SELECT * FROM v_reconciliation_by_bu ORDER BY breaks DESC,business_unit"), conn)
    return {"data": records(frame)}


@router.get("/daily")
def daily():
    with engine.connect() as conn:
        frame = pd.read_sql(text("SELECT * FROM v_reconciliation_daily ORDER BY date"), conn)
    return {"data": records(frame)}


@router.get("/details")
def details(
    status: list[str] | None = Query(None),
    limit: int = Query(250, ge=1, le=500),
):
    """Return a bounded read-only reconciliation detail population for UI review."""
    clauses: list[str] = []
    params: dict[str, object] = {"limit": limit}

    if status:
        clauses.append("r.status = ANY(:status)")
        params["status"] = [value.upper() for value in status]

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = text(f"""
        SELECT
            r.id,
            r.transaction_id,
            r.source_amount,
            r.ledger_amount,
            r.difference,
            r.exposure_inr,
            r.status,
            r.reason,
            r.reconciled_at
        FROM reconciliations r
        {where}
        ORDER BY
            CASE r.status
                WHEN 'INVALID' THEN 1
                WHEN 'AMOUNT_MISMATCH' THEN 2
                WHEN 'CURRENCY_MISMATCH' THEN 3
                WHEN 'BUSINESS_UNIT_MISMATCH' THEN 4
                WHEN 'DATE_MISMATCH' THEN 5
                WHEN 'MISSING_IN_LEDGER' THEN 6
                WHEN 'MISSING_IN_SOURCE' THEN 7
                WHEN 'DUPLICATE' THEN 8
                ELSE 9
            END,
            r.reconciled_at DESC
        LIMIT :limit
    """)
    with engine.connect() as conn:
        frame = pd.read_sql(query, conn, params=params)
    return {"data": records(frame)}
