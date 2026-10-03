"""Phase 6 API contract tests; service/database calls are mocked."""
from datetime import date
import pandas as pd
from fastapi.testclient import TestClient
from backend.main import app
from backend.routers import anomalies, pnl
client=TestClient(app)


def _skip_database_filter_validation(monkeypatch):
    monkeypatch.setattr("backend.api_utils.validate_filters", lambda *args, **kwargs: None)

def test_health_contract(monkeypatch):
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,*args): return None
    class Engine:
        def connect(self): return Conn()
    monkeypatch.setattr("backend.main.engine",Engine())
    r=client.get("/health")
    assert r.status_code==200 and r.json()=={"status":"ok","database":"connected"}

def test_pnl_summary_maps_filters_and_serializes(monkeypatch):
    _skip_database_filter_validation(monkeypatch)
    captured={}
    def fake_summary(_engine,filters):
        captured["filters"]=filters
        return {"revenue":123.0,"net_pnl":45.0,"undefined_ratio":float("nan")}
    monkeypatch.setattr(pnl.pnl_service,"summary",fake_summary)
    r=client.get("/api/v1/pnl/summary?month=2026-03&business_unit=Equities&business_unit=Derivatives")
    assert r.status_code==200
    assert r.json()["data"]["undefined_ratio"] is None
    assert captured["filters"].start_date==date(2026,3,1)
    assert captured["filters"].business_units==("Equities","Derivatives")

def test_pnl_rejects_conflicting_period_parameters():
    r=client.get("/api/v1/pnl/summary?month=2026-03&quarter=2026-Q1")
    assert r.status_code==400 and "not both" in r.json()["detail"]

def test_pnl_trend_serializes_timestamps_and_nan(monkeypatch):
    _skip_database_filter_validation(monkeypatch)
    frame=pd.DataFrame([{"period":pd.Timestamp("2026-03-01"),"revenue":10.0,"growth":float("nan")}])
    monkeypatch.setattr(pnl.pnl_service,"trend",lambda *a,**k: frame)
    r=client.get("/api/v1/pnl/trend")
    assert r.status_code==200
    assert r.json()["data"][0]["period"]=="2026-03-01T00:00:00"
    assert r.json()["data"][0]["growth"] is None

def test_liquidity_rejects_product_filter():
    r=client.get("/api/v1/liquidity/summary?product=Equity")
    assert r.status_code==400 and "Product filtering" in r.json()["detail"]

def test_anomaly_top_contract(monkeypatch):
    _skip_database_filter_validation(monkeypatch)
    frame=pd.DataFrame([{"transaction_id":"TXN-1","amount_inr":100.0,"anomaly_score":0.91,
                         "risk_level":"HIGH","reasons":"Amount is unusual"}])
    monkeypatch.setattr(anomalies.anomaly_service,"top_anomalies",lambda *a,**k: frame)
    r=client.get("/api/v1/anomalies/top?risk=HIGH&limit=1")
    assert r.status_code==200 and r.json()["data"][0]["risk_level"]=="HIGH"

def test_invalid_anomaly_order_is_rejected():
    assert client.get("/api/v1/anomalies/top?order_by=wrong").status_code==422

def test_openapi_contains_phase6_routes():
    paths=client.get("/openapi.json").json()["paths"]
    for p in ["/health","/api/v1/pnl/summary","/api/v1/pnl/trend","/api/v1/liquidity/summary",
              "/api/v1/reconciliation/summary","/api/v1/exceptions/recent","/api/v1/anomalies/summary",
              "/api/v1/anomalies/top","/api/v1/options"]:
        assert p in paths
