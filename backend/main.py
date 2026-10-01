"""FinSight API - Phase 0/1 stub. Only /health exists for now.
The real endpoints are added in Phase 6."""
from fastapi import FastAPI
from sqlalchemy import text

from backend.database import engine

app = FastAPI(title="FinSight API", version="0.1.0")


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "database": f"error: {exc}"}
