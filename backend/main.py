"""FinSight Phase 6 FastAPI application.

Phase 6 exposes the validated analytics as a read-only REST API. Business
calculations remain in the existing service layer; routers only translate
HTTP parameters into service calls and JSON-safe responses.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from backend.database import engine
from backend.routers import anomalies, exceptions, liquidity, options, pnl, reconciliation
from backend.schemas import HealthResponse

app=FastAPI(title="FinSight API",version="0.2.0",
            description="Read-only financial reporting, reconciliation, liquidity and anomaly analytics API.")
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:8501","http://127.0.0.1:8501"],
                   allow_credentials=False,allow_methods=["GET"],allow_headers=["*"])
app.include_router(pnl.router); app.include_router(liquidity.router)
app.include_router(reconciliation.router); app.include_router(exceptions.router)
app.include_router(anomalies.router); app.include_router(options.router)

@app.get("/health",response_model=HealthResponse,tags=["System"])
def health():
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        return {"status":"ok","database":"connected"}
    except Exception as exc:
        return {"status":"degraded","database":f"error: {exc}"}
