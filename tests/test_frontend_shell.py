"""Phase 7 tests for the Streamlit shell and API client."""

from pathlib import Path

import pytest

from frontend.services.api_client import APIError, FinSightAPI


ROOT = Path(__file__).resolve().parents[1]


def test_phase7_frontend_structure_exists():
    expected = [
        "frontend/app.py",
        "frontend/components/styles.py",
        "frontend/components/kpis.py",
        "frontend/components/charts.py",
        "frontend/components/tables.py",
        "frontend/services/api_client.py",
        "frontend/pages/01_Overview.py",
        "frontend/pages/02_Financial_Performance.py",
        "frontend/pages/03_Reconciliation.py",
        "frontend/pages/04_Exceptions.py",
        "frontend/pages/05_Risk_Anomalies.py",
        "frontend/pages/06_Liquidity.py",
        "frontend/pages/07_Reports.py",
    ]
    assert all((ROOT / path).is_file() for path in expected)


def test_api_client_health(monkeypatch):
    class Response:
        ok = True

        def json(self):
            return {"status": "ok", "database": "ok"}

    monkeypatch.setattr("frontend.services.api_client.requests.get", lambda *a, **k: Response())
    client = FinSightAPI("http://localhost:8000")
    assert client.health()["database"] == "ok"


def test_api_client_error(monkeypatch):
    class Response:
        ok = False
        status_code = 400
        text = "bad request"

        def json(self):
            return {"detail": "invalid filter"}

    monkeypatch.setattr("frontend.services.api_client.requests.get", lambda *a, **k: Response())
    with pytest.raises(APIError, match="invalid filter"):
        FinSightAPI().get("/api/v1/pnl/summary")


def test_api_client_connection_error(monkeypatch):
    import requests

    def fail(*args, **kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr("frontend.services.api_client.requests.get", fail)
    with pytest.raises(APIError, match="Could not reach FinSight API"):
        FinSightAPI().health()
